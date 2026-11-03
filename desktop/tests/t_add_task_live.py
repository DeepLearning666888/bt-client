"""AC-12：界面添加动作 → 真实引擎 → 列表出现任务。

不 mock fetch，也不自己拼 HTTP 请求：真正的「添加」由前端 `addTorrent()` 发出，
本脚本只负责把引擎拉起来、把合成磁力交给 vitest、最后收摊。

零外网依赖：磁力是合成的（随机 infohash 但格式合法），--no-network 关掉 DHT。
所以这条验的是「添加链路通不通」，不是「种子下不下得动」——
后者由 engine/tests/real_swarm.py 单独验，两者刻意分开。
"""

from __future__ import annotations

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


def main() -> None:
    work = Path(tempfile.mkdtemp(prefix="desk-add-"))
    info = None
    try:
        info, spawned = launcher.discover_or_spawn(work, no_network=True)
        c.check(spawned is True, "引擎已拉起")

        infohash = c.fake_infohash(777)
        print(f"  engine port={info['port']} infohash={infohash[:12]}…")

        env = dict(os.environ)
        env["ENGINE_URL"] = f"http://127.0.0.1:{int(info['port'])}"
        env["ENGINE_TOKEN"] = str(info["token"])
        env["EXPECT_IH"] = infohash

        print("\n  交给前端添加链路跑 live 用例：")
        result = subprocess.run(
            [sys.executable, "toolchain/build.py", "desktop", "--test", "add-task-live"],
            cwd=str(ROOT), env=env, capture_output=True, text=True,
        )
        tail = (result.stdout + result.stderr).strip().splitlines()
        for line in tail[-10:]:
            print("   ", line)
        c.check(result.returncode == 0, f"live 用例退出码 0（实际 {result.returncode}）")

        # 收摊：把磁力任务删掉，证明添加出来的任务也是可清理的
        status, _ = launcher.request(info, "DELETE", f"/torrents/{infohash}")
        c.check(status in (200, 204), f"删除任务返回 {status}")
    finally:
        if info is not None:
            launcher.shutdown(info)
        shutil.rmtree(work, ignore_errors=True)

    c.finish()


if __name__ == "__main__":
    main()
