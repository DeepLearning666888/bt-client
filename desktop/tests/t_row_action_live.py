"""AC-19：暂停 / 恢复 / 删除，全部走真实引擎。

与 AC-12/AC-15 的分界：那两条验「任务进得来」，这条验「任务管得住」。
引擎侧对应三个分支（`pause` / `resume` / `remove_torrent`），前端各自有薄封装。

零外网依赖：合成磁力 + `--no-network`。
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


def main() -> None:
    work = Path(tempfile.mkdtemp(prefix="desk-rowaction-"))
    info = None
    try:
        info, spawned = launcher.discover_or_spawn(work, no_network=True)
        c.check(spawned is True, "引擎已拉起")

        infohash = c.fake_infohash(909)
        print(f"  engine port={info['port']} infohash={infohash[:12]}…")

        env = dict(os.environ)
        env["ENGINE_URL"] = f"http://127.0.0.1:{int(info['port'])}"
        env["ENGINE_TOKEN"] = str(info["token"])
        env["EXPECT_IH"] = infohash

        print("\n  交给前端操作链路跑 live 用例：")
        result = subprocess.run(
            [sys.executable, "toolchain/build.py", "desktop", "--test", "row-action-live"],
            cwd=str(ROOT), env=env, capture_output=True, text=True,
        )
        tail = (result.stdout + result.stderr).strip().splitlines()
        for line in tail[-12:]:
            print("   ", line)
        c.check(result.returncode == 0, f"live 用例退出码 0（实际 {result.returncode}）")

        # 从引擎侧独立确认列表真的空了：前端的断言可能因为解析层降级而"看起来"是空的
        status, raw = launcher.request(info, "GET", "/state?since=-1")
        c.check_eq(status, 200, "/state 可读")
        torrents = json.loads(raw).get("torrents", [])
        c.check(len(torrents) == 0, f"引擎侧列表为空（实际 {len(torrents)} 条）")
    finally:
        if info is not None:
            launcher.shutdown(info)
        shutil.rmtree(work, ignore_errors=True)

    c.finish()


if __name__ == "__main__":
    main()
