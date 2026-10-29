"""AC-08：引擎发现的三情形。

三种情形对应三种真实故障模式，缺一条这个测试就是摆设：
  1. 目录里没有端点文件      → 必须拉起
  2. 已有实例在跑            → 必须复用（再拉一个会被单实例锁顶掉）
  3. 端点文件是过期残留      → 必须重新拉起，而不是连到一个已经死掉的端口

第 3 条最容易写错：只读文件不问健康，就会「发现成功」然后所有请求超时。
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "desktop"))
sys.path.insert(0, str(ROOT / "engine" / "tests"))

import common as c  # noqa: E402
import launcher  # noqa: E402


def main() -> None:
    print("情形 1：空目录 → 应拉起")
    work = Path(tempfile.mkdtemp(prefix="disc-empty-"))
    try:
        info, spawned = launcher.discover_or_spawn(work, no_network=True)
        c.check(spawned is True, "空目录时 spawned=True")
        c.check(launcher.health_ok(info), "新实例 /health 通过")
        first = info

        print("\n情形 2：已有实例 → 应复用")
        info2, spawned2 = launcher.discover_or_spawn(work, no_network=True)
        c.check(spawned2 is False, "已有实例时 spawned=False")
        c.check(info2["port"] == first["port"], "复用同一端口")
        c.check(info2.get("pid") == first.get("pid"), "复用同一进程 pid")

        print("\n情形 3：过期端点文件 → 应重新拉起")
        stale_dir = Path(tempfile.mkdtemp(prefix="disc-stale-"))
        try:
            fake = {"port": 1, "token": "deadbeef", "pid": 999999, "version": "0.0.0"}
            (stale_dir / launcher.ENDPOINT_NAME).write_text(
                json.dumps(fake), encoding="utf-8")
            c.check(launcher.read_endpoint(stale_dir) is not None, "过期文件仍可被读出")
            c.check(launcher.discover(stale_dir) is None, "健康检查失败 → discover 返回 None")

            info3, spawned3 = launcher.discover_or_spawn(stale_dir, no_network=True)
            c.check(spawned3 is True, "过期端点下 spawned=True")
            c.check(info3["port"] != 1, "没有连到过期文件里的死端口")
            c.check(launcher.health_ok(info3), "新实例 /health 通过")
            launcher.shutdown(info3)
        finally:
            shutil.rmtree(stale_dir, ignore_errors=True)

        launcher.shutdown(first)
    finally:
        shutil.rmtree(work, ignore_errors=True)

    c.finish()


if __name__ == "__main__":
    main()
