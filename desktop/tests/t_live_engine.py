"""AC-06：真实引擎 → 前端数据层 → 渲染。

不 mock fetch。流程：
  1. 本地造一个 64 KiB 种子，把 payload 预先放到引擎的下载目录（校验即完成）
  2. 拉起真实 engine.exe，通过 HTTP 添加种子，等到 state=seeding
  3. 把 URL/TOKEN 传给 vitest 的 live 用例，让前端 parseSnapshot + TorrentList 读真实数据
  4. 断言任务名出现在 DOM 里，并把引擎优雅停掉

零外网依赖：种子是本地构造的，--no-network 关掉了 DHT。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "desktop"))
sys.path.insert(0, str(ROOT / "engine" / "tests"))

import common as c  # noqa: E402
import launcher  # noqa: E402
import mktorrent  # noqa: E402


def add_torrent_file(info: dict, torrent_bytes: bytes) -> tuple[int, str]:
    import urllib.error
    import urllib.request

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
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")


def wait_state(info: dict, infohash: str, wanted: set[str], timeout: float = 25.0) -> str:
    deadline = time.time() + timeout
    last = ""
    while time.time() < deadline:
        status, body = launcher.request(info, "GET", "/state")
        if status == 200:
            for task in json.loads(body).get("torrents", []):
                if task.get("infohash") == infohash:
                    last = str(task.get("state", ""))
                    if last in wanted:
                        return last
        time.sleep(0.1)
    return last


def main() -> None:
    work = Path(tempfile.mkdtemp(prefix="desk-live-"))
    info = None
    try:
        payload = bytes(range(256)) * 256
        downloads = work / "downloads"
        downloads.mkdir(parents=True, exist_ok=True)
        (downloads / "p.bin").write_bytes(payload)

        infohash, torrent_bytes = mktorrent.build(payload, "p.bin")
        torrent_path = work / "t.torrent"
        torrent_path.write_bytes(torrent_bytes)

        info, spawned = launcher.discover_or_spawn(work, no_network=True)
        c.check(spawned is True, "引擎已拉起")
        print(f"  engine port={info['port']} infohash={infohash[:12]}…")

        status, body = add_torrent_file(info, torrent_bytes)
        c.check(status in (200, 201, 202), f"添加种子返回 {status}")
        state = wait_state(info, infohash, {"seeding", "finished"})
        c.check(state in ("seeding", "finished"), f"任务进入 seeding（实际 {state!r}）")

        env = dict(os.environ)
        env["ENGINE_URL"] = f"http://127.0.0.1:{int(info['port'])}"
        env["ENGINE_TOKEN"] = str(info["token"])
        env["EXPECT_NAME"] = "p.bin"

        print("\n  交给前端数据层跑 live 用例：")
        result = subprocess.run(
            [sys.executable, "toolchain/build.py", "desktop", "--test", "live"],
            cwd=str(ROOT), env=env, capture_output=True, text=True,
        )
        tail = (result.stdout + result.stderr).strip().splitlines()
        for line in tail[-8:]:
            print("   ", line)
        c.check(result.returncode == 0, f"live 用例退出码 0（实际 {result.returncode}）")
    finally:
        if info is not None:
            launcher.shutdown(info)
        shutil.rmtree(work, ignore_errors=True)

    c.finish()


if __name__ == "__main__":
    main()
