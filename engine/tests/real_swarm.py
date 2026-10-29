"""真实公网 swarm 端到端下载验证。

为什么单独成文件、而不是第 13 条 AC：

    gate.py 的判定必须是确定性的 —— 离线机器、断网环境、产线 CI 上都该得到
    同一结论。把外网依赖塞进卡点，卡点就从「证据」退化成「抽奖」：今天绿
    明天红，最后没人再信它。所以这里独立判定，并且明确区分三种结局。

退出码（判定语义）：

    0  通过      真实进度达到阈值，证明引擎能对公网 swarm 落盘
    1  引擎缺陷  连上了 peer 但一个字节都没写下来 —— 分片/写盘路径有问题
    2  未验证    全程零 peer —— 网络、防火墙或 tracker 不可达，环境问题不算失败
    4  缺 fixture 找不到种子文件

区分 1 和 2 的依据：peer 连接由 libtorrent 负责（与本地构造种子时同一套代码），
而「把收到的分片写进文件」是我们引擎的 save_path 路径。所以
「有 peer 无字节」能指向引擎，「无 peer 无字节」只能指向环境。

用法：
    python engine/tests/real_swarm.py
    python engine/tests/real_swarm.py --max-bytes 4194304 --timeout 90
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common as c  # noqa: E402

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "debian-13.7.0-netinst-7acf8fb5.torrent"
EXPECT_IH = "7acf8fb590b2060dd9c3146ef770169d593433b0"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--torrent", type=Path, default=FIXTURE)
    ap.add_argument("--max-bytes", type=int, default=8 * 1024 * 1024,
                    help="下到这么多字节就停，避免真的拖完整 ISO")
    ap.add_argument("--timeout", type=float, default=120.0)
    args = ap.parse_args()

    if not args.torrent.exists():
        print(f"[SKIP] 找不到种子 {args.torrent}")
        return 4

    print(f"种子    : {args.torrent.name} ({args.torrent.stat().st_size} B)")
    print(f"预算    : 最多 {args.max_bytes / 1048576:.1f} MiB / {args.timeout:.0f}s")

    eng = c.Engine(no_network=False)
    peak_peers = 0
    peak_seeds = 0
    peak_rate = 0
    done = 0
    got_metadata = False
    rc = 2
    t0 = time.time()

    try:
        eng.start()
        print(f"引擎    : port={eng.port}  data={eng.data_dir}")

        status, body = eng.add_torrent_file(args.torrent)
        print(f"添加    : HTTP {status}  {body}")
        if status not in (200, 201, 202):
            print("[FAIL] 种子被拒绝")
            return 1

        ih = body.get("infohash", "")
        if ih and ih != EXPECT_IH:
            print(f"[WARN] infohash 与预期不符：{ih} != {EXPECT_IH}")

        # 快照每 100ms 才算一帧，添加后头一两次轮询拿到的仍是上一帧。
        # 把「还没刷到」当「已消失」会让测试假失败 —— 这个坑在 wait_state 里
        # 已经踩过一次，这里用连续缺失时长来区分。
        print("\n  t(s)  state        peers seeds   down(B/s)      done(B)")
        deadline = t0 + args.timeout
        missing_since = None
        while time.time() < deadline:
            snap = eng.state()
            task = next((t for t in snap.get("torrents", []) if t["infohash"] == ih), None)
            if task is None:
                if missing_since is None:
                    missing_since = time.time()
                elif time.time() - missing_since > 5.0:
                    print("  任务持续 5s 不在快照里，判定为异常移除")
                    rc = 1
                    break
                time.sleep(0.2)
                continue
            missing_since = None

            peers = int(task.get("peers", 0))
            seeds = int(task.get("seeds", 0))
            rate = int(task.get("rate_down", 0))
            done = int(task.get("done_bytes", 0))
            got_metadata = got_metadata or bool(task.get("has_metadata"))
            peak_peers = max(peak_peers, peers)
            peak_seeds = max(peak_seeds, seeds)
            peak_rate = max(peak_rate, rate)

            print(f"  {time.time() - t0:5.1f}  {task['state']:<12} {peers:5d} {seeds:5d}"
                  f" {rate:10d} {done:12d}")

            if done >= args.max_bytes:
                rc = 0
                break
            time.sleep(1.0)

        # 收尾：删任务并连带删文件，避免残留半个 ISO
        eng.delete(ih, delete_files=True)
        exit_code = eng.shutdown()
    finally:
        eng.stop()

    print(f"\n峰值    : {peak_peers} peer / {peak_seeds} seed / {peak_rate} B/s")
    print(f"元数据  : {'已取得' if got_metadata else '未取得'}")
    print(f"落盘    : {done} B")
    print(f"引擎退出: {exit_code}")

    if rc == 0:
        print(f"\n[PASS] 真实 swarm 下载成功，{done} B 已落盘")
        print(f"       实际用时 {time.time() - t0:.1f}s，峰值速率 {peak_rate / 1048576:.2f} MiB/s")
        return 0

    if peak_peers > 0:
        print(f"\n[FAIL] 连上了 {peak_peers} 个 peer 但零字节落盘 —— 引擎写盘路径有问题")
        return 1

    if rc == 1:
        print("\n[FAIL] 任务在下载过程中异常消失 —— 引擎的任务生命周期有问题")
        return 1

    print("\n[UNVERIFIED] 全程零 peer，无法判定引擎好坏。")
    print("             这属于环境不可达（网络/防火墙/tracker），不是本次交付的缺陷。")
    print("             排查顺序：能出网吗 → tracker 可达吗 → 种子还有人做种吗")
    return 2


if __name__ == "__main__":
    sys.exit(main())
