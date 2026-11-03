"""AC-15：种子文件上传 → 真实引擎 → 列表出现任务。

与 AC-12 的区别只在输入形态：那边发磁力（JSON），这边发 `.torrent` 原始字节
（`application/octet-stream`）。两条链路在引擎里走的是不同分支
（`add_magnet` vs `add_torrent_bytes`），所以必须各验一次。

种子用仓库里的 fixture（Debian netinst 的完整 .torrent），零外网依赖：
只解析不下载，`--no-network` 关掉 DHT。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "desktop"))
sys.path.insert(0, str(ROOT / "engine" / "tests"))

import common as c  # noqa: E402
import launcher  # noqa: E402

# 文件名里带 infohash 前 8 位：debian-13.7.0-netinst-7acf8fb5.torrent
TORRENT = ROOT / "engine" / "tests" / "fixtures" / "debian-13.7.0-netinst-7acf8fb5.torrent"
EXPECT_PREFIX = "7acf8fb5"


def main() -> None:
    c.check(TORRENT.is_file(), f"fixture 存在：{TORRENT.name}")
    work = Path(tempfile.mkdtemp(prefix="desk-addfile-"))
    info = None
    infohash = ""
    try:
        info, spawned = launcher.discover_or_spawn(work, no_network=True)
        c.check(spawned is True, "引擎已拉起")
        print(f"  engine port={info['port']} torrent={TORRENT.name} ({TORRENT.stat().st_size} 字节)")

        env = dict(os.environ)
        env["ENGINE_URL"] = f"http://127.0.0.1:{int(info['port'])}"
        env["ENGINE_TOKEN"] = str(info["token"])
        env["TORRENT_PATH"] = str(TORRENT)
        env["EXPECT_PREFIX"] = EXPECT_PREFIX

        print("\n  交给前端上传链路跑 live 用例：")
        result = subprocess.run(
            [sys.executable, "toolchain/build.py", "desktop", "--test", "add-file-live"],
            cwd=str(ROOT), env=env, capture_output=True, text=True,
        )
        tail = (result.stdout + result.stderr).strip().splitlines()
        for line in tail[-10:]:
            print("   ", line)
        c.check(result.returncode == 0, f"live 用例退出码 0（实际 {result.returncode}）")

        # 从引擎侧独立确认：任务真的在，且 infohash 前缀对得上
        status, raw = launcher.request(info, "GET", "/state?since=-1")
        c.check_eq(status, 200, "/state 可读")
        state = json.loads(raw)
        torrents = state.get("torrents", [])
        c.check(len(torrents) == 1, f"/state 里有 1 个任务（实际 {len(torrents)}）")
        infohash = torrents[0]["infohash"] if torrents else ""
        c.check(infohash.startswith(EXPECT_PREFIX),
                f"infohash 前缀为 {EXPECT_PREFIX}（实际 {infohash[:8]}）")

        # 收摊：删掉任务，证明文件任务与磁力任务一样可清理
        status, _ = launcher.request(info, "DELETE", f"/torrents/{infohash}")
        c.check(status in (200, 204), f"删除任务返回 {status}")
    finally:
        if info is not None:
            launcher.shutdown(info)
        shutil.rmtree(work, ignore_errors=True)

    c.finish()


if __name__ == "__main__":
    main()
