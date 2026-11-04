"""两个引擎在同一台机器上真传数据。

为什么要这条：

    其余所有测试都是零 peer 的 —— 种子自己造、校验走本地磁盘、progress 要么 0
    要么满。也就是说「引擎能不能从别的客户端下载到字节并落盘」这件事，至今
    没有任何一条判据碰过。它是产品的地基，却恰好是唯一没验的地方。

怎么让对方互相发现：

    进程内 HTTP tracker（local_tracker.py）。引擎的 BT 监听口由内核分配，既没有
    参数也没有接口能读到实际端口，所以「谁在哪个端口」只能由 tracker 从 announce
    请求里回答。

    两个引擎都带 `--no-network`（即关掉 DHT / LSD / UPnP）。**这不是怕联网，是
    为了把会合途径隔离成唯一一条。** 实测过：开着 LSD 时，把 tracker 下发的 peer
    端口故意改错，两个引擎照样下完 —— 说明同机 UDP 组播会绕过 tracker 自行配对。
    那样的话「会合由 tracker 完成」就无法证明，负向验证也永远变不红。
    `--no-network` 只关发现机制，监听口和 BEP 传输不受影响（见 engine.cpp 的
    settings_pack 那几行）。

路径：

    A 种子端预置完整内容 → 自动校验到 seeding，且 tracker 收到 left=0 的上报
    B 下载端零数据起步 → done_bytes 真的从 0 涨到全量
    C 采样序列非递减 —— 进度不会中途塌回 0 再涨回去
    D 下载完的文件字节与源逐字节一致 —— 这是「真传了」唯一无法伪造的证据

D 是重点。B/C 只能证明「有个数字在涨」，D 才能证明涨的是对的内容。
"""

from __future__ import annotations

import random
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common as c  # noqa: E402
import mktorrent  # noqa: E402
from local_tracker import LocalTracker  # noqa: E402

PIECE_LEN = 32768
PIECE_COUNT = 1024
PAYLOAD = random.Random(20261105).randbytes(PIECE_LEN * PIECE_COUNT)  # 32 MiB
SIZE = len(PAYLOAD)
NAME = "seed.bin"
SAMPLE_INTERVAL = 0.02


def wait_gone(engine: c.Engine, infohash: str, timeout: float = 10.0) -> bool:
    """等任务真的从 /state 消失。

    `DELETE` 返回 200 只表示受理了，libtorrent 移除 handle 是异步的。不等就重加
    同一个 infohash，新任务会被旧 handle 挡住，状态卡在旧的 progress。
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        if engine.find(infohash) is None:
            return True
        time.sleep(0.1)
    return False


def stage_seeder(engine: c.Engine, blob: bytes) -> str:
    """让引擎自己校验出一个 seeding 任务，返回 infohash（失败返回空串）。

    做法：先加任务拿到 save_path，把完整内容写进去，删掉任务（保留文件）再加
    一次 —— libtorrent 会对已存在的文件做校验，通过就直接进 seeding。
    不需要 recheck 接口，也不需要 peer。
    """
    torrent_file = engine.data_dir / "seed.torrent"
    torrent_file.write_bytes(blob)

    status, body = engine.add_torrent_file(torrent_file)
    if not c.check_eq(status, 200, "A 种子端加种子返回 200"):
        return ""
    infohash = body.get("infohash", "")
    task = engine.wait_state(infohash, {"downloading", "checking", "metadata"}, timeout=15)
    if not c.check(task is not None, "A 种子端任务出现在列表里"):
        return ""

    save_dir = Path(task["save_path"])
    save_dir.mkdir(parents=True, exist_ok=True)
    (save_dir / NAME).write_bytes(PAYLOAD)

    if not c.check_eq(engine.delete(infohash)[0], 200, "A 删掉占位任务（保留文件）"):
        return ""
    if not c.check(wait_gone(engine, infohash), "A 占位任务已从列表消失"):
        return ""
    if not c.check_eq(engine.add_torrent_file(torrent_file)[0], 200,
                      "A 重新加同一份种子返回 200"):
        return ""
    return infohash


def main() -> None:
    started_at = time.time()
    with LocalTracker() as tracker:
        _infohash, blob = mktorrent.build(
            PAYLOAD, NAME, piece_length=PIECE_LEN, announce=tracker.announce_url)

        seeder = c.Engine().start()
        learner = None
        try:
            # ---- A) 种子端就绪
            infohash = stage_seeder(seeder, blob)
            if not c.check(bool(infohash), "A 拿到 infohash"):
                return
            done = seeder.wait_state(infohash, {"seeding", "finished"}, timeout=40)
            if not c.check(done is not None and done["state"] in ("seeding", "finished"),
                           f"A 种子端校验完成进入做种态"
                           f"（实际 {done['state'] if done else '无'}）"):
                return
            c.check_eq(done["done_bytes"], SIZE,
                       f"A 种子端 done_bytes={done['done_bytes']}（期望 {SIZE}）")
            events_a = [e for e in tracker.peer_events if e[0] == infohash[:8]]
            c.check(len(events_a) >= 1,
                    f"A 种子端向 tracker 上报过（{len(events_a)} 次）")

            # ---- B) 下载端从零开始
            learner = c.Engine().start()
            learner_file = learner.data_dir / "seed.torrent"
            learner_file.write_bytes(blob)
            status, body = learner.add_torrent_file(learner_file)
            if not c.check_eq(status, 200, "B 下载端加种子返回 200"):
                return
            c.check_eq(body.get("infohash", ""), infohash, "B 两边是同一条任务")

            first = learner.wait_state(infohash, {"downloading", "checking", "metadata"},
                                       timeout=15)
            if not c.check(first is not None, "B 下载端任务出现在列表里"):
                return

            # 「从零开始」不能用「起步 done_bytes=0」来断言：本地回环快到第一次轮询
            # 之前就可能下完，这条会假红。改成两条不受速度影响的判据 —— 目录不同
            # （不可能直接读到源文件）+ 文件是在测试开始之后才创建的。
            c.check(Path(first["save_path"]) != Path(done["save_path"]),
                    f"B 两端 save_path 不同（种子端 {done['save_path']}，"
                    f"下载端 {first['save_path']}）—— 下载端读不到源文件")

            # ---- C) 采样，等它下完
            samples: list[int] = []
            final = None
            deadline = time.time() + 120
            while time.time() < deadline:
                snap = learner.state()
                task = next((t for t in snap.get("torrents", [])
                             if t["infohash"] == infohash), None)
                if task is not None:
                    samples.append(int(task["done_bytes"]))
                    if task["state"] in ("seeding", "finished"):
                        final = task
                        break
                time.sleep(SAMPLE_INTERVAL)

            if not c.check(final is not None,
                           f"C 下载端在 120 秒内下完（最后观测 done_bytes="
                           f"{samples[-1] if samples else '无'}，state 可能是 downloading —— "
                           f"说明两个引擎没能连上）"):
                print(f"        tracker 收到 {len(tracker.peer_events)} 次上报："
                      f"{[(e[1], e[2]) for e in tracker.peer_events]}")
                print(f"        下载端 final={samples[-1] if samples else '无'}/{SIZE}")
                return

            moved = [s for s in samples if s > 0]
            c.check(bool(moved), f"C 观测到真实字节增长（{len(moved)} 次采样 >0）")
            c.check(all(b >= a for a, b in zip(samples, samples[1:])),
                    f"C 采样序列非递减（共 {len(samples)} 个点，"
                    f"最大回退 {max((a - b for a, b in zip(samples, samples[1:])), default=0)}）")
            partial = [s for s in samples if 0 < s < SIZE]
            print(f"        中间态采样 {len(partial)} 个"
                  f"{'（传输太快，只有 0 和满两个点，单调性覆盖有限）' if not partial else ''}")

            c.check_eq(final["done_bytes"], SIZE,
                       f"C 下载端 done_bytes={final['done_bytes']}（期望 {SIZE}）")
            c.check_eq(final["total_bytes"], SIZE,
                       f"C 下载端 total_bytes={final['total_bytes']}（期望 {SIZE}）")

            # ---- D) 字节级比对
            got = Path(final["save_path"]) / NAME
            if not c.check(got.exists(), f"D 下载端文件存在：{got}"):
                return
            data = got.read_bytes()
            c.check_eq(len(data), SIZE, "D 文件大小一致")
            c.check(data == PAYLOAD, "D 文件内容与源逐字节一致（这是真传了的证据）")
            c.check(got.stat().st_mtime >= started_at - 1,
                    f"D 文件是本次测试期间新写的（mtime "
                    f"{time.strftime('%H:%M:%S', time.localtime(got.stat().st_mtime))}）"
                    f"—— 排除了「读到磁盘上早就存在的文件」")

            # 两个引擎都出现在 tracker 上，说明会合确实由 tracker 完成
            ports = {e[2] for e in tracker.peer_events}
            c.check(len(ports) >= 2,
                    f"D tracker 见过 2 个不同监听口（{sorted(ports)}）")

            # ---- E) 反向对照：peer 都撤了，同样一条种子必须一字节也拿不到
            # 这是 D 段结论的支撑。否则「文件字节一致」排除不了「引擎自己有办法
            # 弄到完整内容」（本地缓存、路径短路之类）。把两个引擎都关掉，只留
            # tracker 里还没过期的记录，重新加同一份种子 —— 数据必须原地不动。
            seeder.stop()
            learner.stop()

            lonely = c.Engine().start()
            try:
                lonely_file = lonely.data_dir / "seed.torrent"
                lonely_file.write_bytes(blob)
                status, body = lonely.add_torrent_file(lonely_file)
                c.check_eq(status, 200, "E 无 peer 环境加种子返回 200")
                lonely_ih = body.get("infohash", "")
                c.check_eq(lonely_ih, infohash, "E 是同一条任务")

                peak = 0
                last_state = "未出现"
                deadline = time.time() + 8.0
                while time.time() < deadline:
                    task = lonely.find(lonely_ih)
                    if task is not None:
                        peak = max(peak, int(task["done_bytes"]))
                        last_state = task["state"]
                    time.sleep(0.2)
                c.check_eq(peak, 0,
                           f"E 等满 8 秒仍拿到 0 字节（state={last_state}）—— "
                           f"没有 peer 就没有数据，D 段的字节只可能来自网络")
            finally:
                lonely.stop()
        finally:
            if learner is not None:
                learner.stop()
            seeder.stop()
            c.finish()


if __name__ == "__main__":
    main()
