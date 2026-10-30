#!/usr/bin/env python3
"""AC-05：卸载清程序、留数据。

「能删掉」和「清干净」是两个判据，而且最容易出的缺陷不是删不掉，是**删多了**：

- 程序目录残留 → 用户重装后看到两个版本混在一起，或者旧 DLL 覆盖新的
- 用户数据被删 → 下载记录、已完成任务列表一起消失，这是不可挽回的

所以这一项同时断言「该删的删了」和「不该删的还在」。

数据目录用 `BTCLIENT_DATA_DIR` 指到临时位置，不碰 `%APPDATA%` 下的真实数据；
同时记录真实数据目录在卸载前后的状态，确认卸载器没有绕过覆盖去动它。
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import installer_common as common  # noqa: E402

# 卸载器要删除自身，Windows 上会先把自己复制到临时目录再延迟删除，所以不能要求立刻为空
SETTLE_TIMEOUT_S = 60.0


def snapshot(path: Path) -> tuple[int, int]:
    """(文件数, 总字节数)。用来确认某个目录没被动过。"""
    if not path.exists():
        return (0, 0)
    files = [p for p in path.rglob("*") if p.is_file()]
    return (len(files), sum(p.stat().st_size for p in files))


def main() -> int:
    problems: list[str] = []

    install, data = common.make_workspace("uninst")
    real_data = Path(os.environ.get("APPDATA", "")) / "com.btclient.desktop"
    print(f"安装目录：{install}")
    print(f"临时数据目录：{data}")
    print(f"真实数据目录（只观察，不动）：{real_data}")

    try:
        print("\n① 先装上并跑一次，制造出用户数据")
        common.run_setup(install)
        names = common.wait_installed(install)
        if not names:
            print("  ✗ 安装没落地，后续卸载判定无意义")
            return 1

        env = dict(os.environ)
        env["BTCLIENT_DATA_DIR"] = str(data)
        ready, attempts = common.launch_and_wait(install, data, timeout=90.0)
        if attempts > 1:
            print(f"  （启动重试了 {attempts - 1} 次：WebView2 用户数据目录被上一轮占用）")
        common.check("启动成功并产生数据", ready is not None,
                     f"seq={ready.group(2)}" if ready else "没拿到 frontend_ready", problems)

        data_before = snapshot(data)
        real_before = snapshot(real_data)
        print(f"  临时数据：{data_before[0]} 个文件 / {data_before[1]} 字节")

        print("\n② 静默卸载")
        code = common.run_uninstall(install)
        print(f"  卸载器退出码 {code}")

        remained: set[str] = set()
        deadline = time.time() + SETTLE_TIMEOUT_S
        while time.time() < deadline:
            remained = {p.name for p in install.iterdir()} if install.exists() else set()
            # uninstall.exe 会自我删除，允许它短暂残留
            if not (remained - {"uninstall.exe"}):
                break
            time.sleep(0.5)

        leftover = sorted(remained - {"uninstall.exe"})
        print(f"  卸载后剩余：{sorted(remained) or '（空）'}")
        common.check("程序文件已清除", not leftover, f"残留 {leftover}" if leftover else "", problems)
        common.check("主程序已删除", not (install / "btclient-shell.exe").exists(), "", problems)
        common.check("引擎与 DLL 已删除",
                     not (install / "engine.exe").exists() and not (install / "torrent-rasterbar.dll").exists(),
                     "", problems)

        print("\n③ 用户数据未被删除")
        data_after = snapshot(data)
        print(f"  临时数据：{data_after[0]} 个文件 / {data_after[1]} 字节")
        common.check("临时数据目录内容不变", data_after == data_before,
                     f"卸载前 {data_before}，卸载后 {data_after}" if data_after != data_before else "", problems)
        real_after = snapshot(real_data)
        common.check("真实 %APPDATA% 数据目录未被触碰", real_after == real_before,
                     f"卸载前 {real_before}，卸载后 {real_after}" if real_after != real_before else "", problems)

    finally:
        common.cleanup(install.parent.parent)

    return common.report(problems)


if __name__ == "__main__":
    sys.exit(main())
