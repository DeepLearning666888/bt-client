"""`delete_files` 的语义：任务被移除时，磁盘上的文件到底怎么了。

AC-07 只验「infohash 不再出现在 /state」—— 任务没了，但文件呢？从来没验过。
这条补三种情形：

    A) delete_files=1，文件正常            → 任务没了，文件也没了
    B) delete_files=0                      → 任务没了，文件还在
    C) delete_files=1，但文件被别的程序占着 → 任务没了，文件还在，
                                             **响应里必须带 warning**

C 是这条的重点，也是这次新发现的一个静默失败：libtorrent 的 `delete_files` 对
删除失败保持沉默 —— 实测文件被占用时它照样回「成功」，任务消失、文件还在磁盘上。
用户勾了「同时删除文件」，界面显示成功，空间却没回来，而且永远不会知道。

判据只能看两处：文件系统的真实状态（`Path.exists()`），和响应体里有没有 warning。
「接口返回 200」这件事本身不构成证据 —— 那正是修复前的假象。

零外网依赖：种子本地构造，副本预置在下载目录，`--no-network` 关掉 DHT。
"""

from __future__ import annotations

import json
import shutil
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

PAYLOAD = bytes(range(256)) * 40  # 10240 字节，够切成多个分片


def add_torrent(info: dict, torrent_bytes: bytes) -> int:
    req = urllib.request.Request(
        f"http://127.0.0.1:{int(info['port'])}/torrents",
        data=torrent_bytes,
        method="POST",
        headers={
            "Authorization": f"Bearer {info['token']}",
            "Content-Type": "application/octet-stream",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            resp.read()
            return resp.status
    except urllib.error.HTTPError as exc:
        exc.read()
        return exc.code


def wait_seeding(info: dict, infohash: str, timeout: float = 20.0) -> str:
    deadline = time.time() + timeout
    last = ""
    while time.time() < deadline:
        status, body = launcher.request(info, "GET", "/state?since=-1")
        if status == 200:
            for task in json.loads(body).get("torrents", []):
                if task.get("infohash") == infohash:
                    last = str(task.get("state", ""))
                    if last in ("seeding", "finished"):
                        return last
        time.sleep(0.1)
    return last


def present_in_state(info: dict, infohash: str) -> bool:
    status, body = launcher.request(info, "GET", "/state?since=-1")
    if status != 200:
        return True
    return any(t.get("infohash") == infohash for t in json.loads(body).get("torrents", []))


def wait_gone(info: dict, infohash: str, timeout: float = 5.0) -> bool:
    """移除是异步的：接口返回后 libtorrent 还要一小会儿才真的丢掉 handle。
    这段时间里 /state 仍会返回它 —— 所以判定要轮询，不能看完就下结论。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not present_in_state(info, infohash):
            return True
        time.sleep(0.1)
    return not present_in_state(info, infohash)


def main() -> None:
    work = Path(tempfile.mkdtemp(prefix="eng-delfiles-"))
    info = None
    try:
        downloads = work / "downloads"
        downloads.mkdir(parents=True, exist_ok=True)
        info, spawned = launcher.discover_or_spawn(work, no_network=True)
        c.check(spawned is True, "引擎已拉起")

        cases: dict[str, tuple[str, Path]] = {}
        for name in ("drop.bin", "keep.bin", "locked.bin"):
            target = downloads / name
            target.write_bytes(PAYLOAD)
            infohash, torrent_bytes = mktorrent.build(PAYLOAD, name)
            c.check_eq(add_torrent(info, torrent_bytes), 200, f"{name} 加入成功")
            state = wait_seeding(info, infohash)
            c.check(state in ("seeding", "finished"), f"{name} 校验完成（实际 {state!r}）")
            c.check(target.exists(), f"{name} 已经在磁盘上")
            cases[name] = (infohash, target)

        print(f"  engine port={info['port']}，三个任务均 seeding")

        # ---- A) 正常删除，文件应当一起消失
        ih, target = cases["drop.bin"]
        status, body = launcher.request(info, "DELETE", f"/torrents/{ih}?delete_files=1", timeout=15)
        c.check_eq(status, 200, "A 删除返回 200")
        payload = json.loads(body)
        c.check("warning" not in payload, f"A 没有 warning（实际 {body[:120]}）")
        c.check(not target.exists(), "A 文件真的从磁盘消失")
        c.check(wait_gone(info, ih), "A 任务从 /state 消失")

        # ---- B) 只删任务，文件必须留着
        ih, target = cases["keep.bin"]
        status, body = launcher.request(info, "DELETE", f"/torrents/{ih}?delete_files=0", timeout=15)
        c.check_eq(status, 200, "B 删除返回 200")
        c.check(target.exists(), "B 文件仍在（没有误删用户的文件）")
        c.check(wait_gone(info, ih), "B 任务从 /state 消失")

        # ---- C) 文件被占用：任务照删，但必须如实说出「文件没删掉」
        ih, target = cases["locked.bin"]
        handle = open(target, "rb")  # Windows 上裸句柄不带 FILE_SHARE_DELETE，足以阻止删除
        try:
            started = time.time()
            status, body = launcher.request(info, "DELETE", f"/torrents/{ih}?delete_files=1", timeout=15)
            spent = time.time() - started
        finally:
            handle.close()

        c.check_eq(status, 200, "C 任务仍被移除（任务本身不该因为文件删不掉而卡住）")
        payload = json.loads(body)
        c.check("warning" in payload, f"C 响应带 warning（实际 {body[:200]}）")
        warning = str(payload.get("warning", ""))
        c.check("占用" in warning, f"C warning 说清原因（实际 {warning!r}）")
        c.check("locked.bin" in warning, f"C warning 指明是哪个文件（实际 {warning!r}）")
        c.check(target.exists(), "C 文件确实还在磁盘上（warning 说的与事实一致）")
        c.check(wait_gone(info, ih), "C 任务从 /state 消失")
        # 修复前这里是 0 秒返回 200 且无 warning —— 用户永远不知道空间没回来。
        # 现在最多等一个等待窗。这里只要求它没有无限期挂着。
        c.check(spent < 12, f"C 在合理时间内返回（实际 {spent:.2f}s）")

        # 松开句柄后文件应当能被正常删掉：证明「删不掉」是被占用导致的，
        # 而不是我们把路径算错了、或者根本没尝试删。
        target.unlink()
        c.check(not target.exists(), "D 松开占用后可正常删除（路径没算错）")
    finally:
        if info is not None:
            launcher.shutdown(info)
        shutil.rmtree(work, ignore_errors=True)

    c.finish()


if __name__ == "__main__":
    main()
