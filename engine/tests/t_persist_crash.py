"""任务能不能活过「没走优雅退出」的那一次死亡。

`restore()` 靠扫 `data/resume/` 重建任务列表 —— 也就是说，**resume 文件就是任务本身**。
而落盘原先只有三个时机：退出（`flush_all`）、暂停、恢复。

于是：加完任务直接被强杀（任务管理器结束引擎、断电、蓝屏、崩溃），重启后这些任务
**整条不存在**，不只是进度丢了。实测过一次：加任务 → resume 目录是空的 → taskkill →
重启 → 0 个任务。界面会说「还没有任务」，用户以为种子没加进去。

三种情形：

    A) 加种子 → 立刻硬杀 → 重启 → 任务还在，状态一致
    B) 加磁力（永远拿不到元数据）→ 硬杀 → 重启 → 任务还在（靠 .magnet 兜底）
    C) 进度没变时，周期落盘不重复写盘（否则每 30 秒重写一遍所有种子，纯磨损 SSD）

C 用 `--periodic-save-ms 200` 把窗口缩到 200ms，否则这条要跑 30 秒。
缩短的是间隔，不是逻辑 —— 200ms 到点不落盘，30 秒到点也不会。

零外网依赖：种子本地构造，`--no-network` 关掉 DHT/LSD/UPnP/NAT-PMP。
磁力拿不到元数据是刻意的：那条路径要验的就是 `.magnet` 兜底，而不是 resume。
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "desktop"))
sys.path.insert(0, str(ROOT / "engine" / "tests"))

import common as c  # noqa: E402
import launcher  # noqa: E402
import mktorrent  # noqa: E402

PAYLOAD = bytes(range(256)) * 40


def add_bytes(info: dict, blob: bytes) -> int:
    req = urllib.request.Request(
        f"http://127.0.0.1:{int(info['port'])}/torrents", data=blob, method="POST",
        headers={"Authorization": f"Bearer {info['token']}",
                 "Content-Type": "application/octet-stream"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code


def add_magnet(info: dict, uri: str) -> int:
    req = urllib.request.Request(
        f"http://127.0.0.1:{int(info['port'])}/torrents",
        data=json.dumps({"magnet": uri}).encode(), method="POST",
        headers={"Authorization": f"Bearer {info['token']}",
                 "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code


def torrents(info: dict) -> list[dict]:
    status, body = launcher.request(info, "GET", "/state?since=-1")
    if status != 200:
        return []
    return json.loads(body).get("torrents", [])


def states(info: dict) -> dict[str, str]:
    return {t["infohash"]: t.get("state", "") for t in torrents(info)}


def hard_kill(info: dict) -> None:
    """SIGKILL。这是「任务管理器结束进程 / 断电 / 蓝屏」的共同之处：没有收尾机会。"""
    subprocess.run(["taskkill", "/F", "/PID", str(info["pid"])],
                   capture_output=True, check=False)
    deadline = time.time() + 10
    while time.time() < deadline:
        if not launcher.health_ok(info, timeout=0.3):
            return
        time.sleep(0.1)


def resume_files(data_dir: Path) -> list[str]:
    d = data_dir / "resume"
    if not d.exists():
        return []
    return sorted(p.name for p in d.iterdir() if p.is_file())


def main() -> int:
    work = Path(tempfile.mkdtemp(prefix="persist-crash-"))
    first = None
    second = None
    try:
        downloads = work / "downloads"
        downloads.mkdir(parents=True, exist_ok=True)
        (downloads / "seed.bin").write_bytes(PAYLOAD)
        info_hash, torrent_bytes = mktorrent.build(PAYLOAD, "seed.bin")

        # ---- A) 种子：加完立刻硬杀
        first = launcher.spawn(work, no_network=True, extra_args=["--periodic-save-ms", "200"])
        c.check_eq(add_bytes(first, torrent_bytes), 200, "A 加种子返回 200")
        time.sleep(1.5)

        before_states = states(first)
        c.check_eq(len(before_states), 1, "A 加完任务后列表里有 1 条")
        saved = resume_files(work)
        c.check(any(n.startswith(info_hash) and n.endswith(".fastresume") for n in saved),
                "A 加完任务就已经有 resume 文件（不是等到退出才写）")

        hard_kill(first)
        first = None

        # ---- A2) 重启后任务必须还在
        second = launcher.spawn(work, no_network=True,
                                extra_args=["--periodic-save-ms", "200"])
        time.sleep(2.0)
        after_states = states(second)
        c.check_eq(len(after_states), 1, "A2 硬杀重启后任务还在（不是 0 条）")
        c.check_eq(after_states.get(info_hash), before_states.get(info_hash),
                   "A2 任务状态与硬杀前一致")

        # ---- B) 磁力：拿不到元数据，只能靠 .magnet 兜底
        ghost = "0123456789abcdef0123456789abcdef01234567"
        magnet = f"magnet:?xt=urn:btih:{ghost}&dn=ghost.bin"
        c.check_eq(add_magnet(second, magnet), 200, "B 加磁力返回 200")
        time.sleep(1.5)
        c.check(near(second, ghost), "B 磁力任务出现在列表里")

        recovery = resume_files(work)
        # 只看「这个 infohash 有没有落盘记录」，不指定后缀：libtorrent 2.x 在
        # 没有元数据时也能写出 fastresume（里面带 info-hashes），实测就是这样。
        # `.magnet` 是 save_resume_data 真的失败时的兜底，不该由测试去假设哪条路会走。
        c.check(any(n.startswith(ghost) for n in recovery),
                "B 没有元数据时也落了盘（不必等到退出）")

        hard_kill(second)
        second = None

        second = launcher.spawn(work, no_network=True,
                                extra_args=["--periodic-save-ms", "200"])
        time.sleep(1.5)
        after_magnet = states(second)
        c.check(near(second, ghost), "B2 硬杀重启后磁力任务还在")
        c.check_eq(len(after_magnet), 2, "B2 两条任务都在（种子 + 磁力）")

        # ---- C) 进度没变时不该重复写盘
        target = work / "resume" / f"{info_hash}.fastresume"
        # 等校验彻底稳定，否则 total_done 还在变，周期落盘本来就该写
        time.sleep(3.0)
        sig_before = signature(target)
        time.sleep(1.5)  # 200ms 周期至少跑 7 次
        sig_after = signature(target)
        c.check(sig_before is not None and sig_before == sig_after,
                "C 进度没变化时周期落盘不重写文件")

        return c.finish()
    finally:
        for info in (first, second):
            if info is not None:
                launcher.shutdown(info)
        shutil.rmtree(work, ignore_errors=True)


def near(info: dict, infohash: str) -> bool:
    """磁力任务在 /state 里的 infohash 可能带 v1/v2 前缀差异，按前缀匹配。"""
    return any(t.get("infohash", "").startswith(infohash[:16]) for t in torrents(info))


def signature(path: Path):
    if not path.exists():
        return None
    st = path.stat()
    return (st.st_size, st.st_mtime_ns)


if __name__ == "__main__":
    sys.exit(main())
