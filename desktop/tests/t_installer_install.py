#!/usr/bin/env python3
"""AC-02 / AC-03 / AC-04 / AC-06：装到别的位置后仍然能跑。

这是整个 installer 单元里唯一不可替代的判定。M2b 的 AC-06 证明了
`target/release/btclient-shell.exe` 能启动，但那个目录里 `engine.exe` 和
`torrent-rasterbar.dll` 恰好躺在旁边（编译时 Tauri 把 resource 复制过去了），
而且 `locate_engine()` 的开发态回退分支指向源码目录 —— 在那台机器上永远成立。

换句话说：**resource 解析这条分支从来没有在打包形态下被执行过**。
装到临时目录，"源码目录" 不存在了，回退分支失效，才真正测到它。

四条 AC 共用一次安装与启动：
- AC-02 装完的目录结构（缺 DLL 或少了引擎都算失败）
- AC-03 前端拿到数据（`frontend_ready` 带非空 seq）
- AC-04 引擎来自安装目录（`engine_exe=` 位于安装目录内）
- AC-06 不含开发产物（无子目录、无源码文件）
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import installer_common as common  # noqa: E402

READY_TIMEOUT_S = 90.0


def main() -> int:
    problems: list[str] = []

    install, data = common.make_workspace("run")
    print(f"安装目录：{install}")
    print(f"数据目录：{data}")

    try:
        print("\n① 静默安装（AC-02）")
        code = common.run_setup(install)
        names = common.wait_installed(install)
        print(f"  安装器退出码 {code}，落地 {len(names)} 个条目")

        # 退出码 0 不代表成功：/D 被加引号时 NSIS 静默失败并返回 0
        common.check("安装目录非空（退出码 0 不足以证明安装成功）",
                     bool(names), "空目录说明 /D 参数解析失败" if not names else "", problems)
        common.check("文件集与预期完全一致", names == common.EXPECTED_FILES,
                     f"实际 {sorted(names)}" if names != common.EXPECTED_FILES else "", problems)

        # 引擎与 DLL 必须和主程序同层：Windows 按 exe 所在目录找 DLL，
        # 分开放会得到 0xc0000135（STATUS_DLL_NOT_FOUND），报错只出现在事件日志里
        for name in ("engine.exe", "torrent-rasterbar.dll"):
            common.check(f"{name} 与主程序同层", (install / name).is_file(), "", problems)

        print("\n② 从安装目录启动（AC-03 / AC-04）")
        env = dict(os.environ)
        # 独立数据目录：不能让测试把用户真实的任务列表挤掉
        env["BTCLIENT_DATA_DIR"] = str(data)
        proc = subprocess.Popen([str(install / "btclient-shell.exe")], env=env,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        log_path = data / "shell.log"
        ready = common.wait_for(common.READY_RE, log_path, proc, READY_TIMEOUT_S)
        log = common.read_log(log_path)

        if ready is None:
            common.stop(proc)
            print("  日志内容：")
            for line in log.splitlines() or ["（无日志）"]:
                print(f"    {line}")
            common.check("前端回报 frontend_ready", False, "壳没能在超时内拿到首帧数据", problems)
            common.check("引擎来自安装目录", False, "未启动成功，无从判断", problems)
        else:
            port, seq = ready.group(1), ready.group(2)
            common.check("前端回报 frontend_ready", True, f"port={port} seq={seq}", problems)
            common.check("seq 为非空正数", seq.isdigit() and int(seq) > 0, f"seq={seq}", problems)

            # AC-04：这一条才是「装完能跑」与「开发态能跑」的分界线
            match = common.ENGINE_EXE_RE.search(log)
            engine_path = match.group(1).strip() if match else ""
            print(f"  engine_exe={engine_path}")
            common.check("日志记录 engine_exe", bool(engine_path), "", problems)
            normalized = engine_path.replace("\\\\?\\", "").replace("/", "\\")
            common.check("引擎路径位于安装目录内",
                         normalized.lower().startswith(str(install).lower()),
                         "" if normalized.lower().startswith(str(install).lower())
                         else "说明走的是开发态回退分支，打包形态下这条分支应当失效", problems)
            common.check("引擎路径不含 engine\\build（未走源码回退）",
                         "engine\\build" not in normalized,
                         engine_path if "engine\\build" in normalized else "", problems)
            common.check("日志记录了引擎决策与同一端口",
                         re.search(rf"engine decision=\w+ port={port}", log) is not None, "", problems)

        common.stop(proc)

        print("\n③ 安装内容不含开发产物（AC-06）")
        entries = sorted(p.name for p in install.iterdir())
        subdirs = sorted(p.name for p in install.iterdir() if p.is_dir())
        leaked = sorted(common.FORBIDDEN_NAMES & set(entries))
        total = sum(p.stat().st_size for p in install.rglob("*") if p.is_file())
        print(f"  目录内容：{entries}")
        common.check("安装目录无子目录", not subdirs, f"存在 {subdirs}" if subdirs else "", problems)
        common.check("无源码/工程文件", not leaked, f"发现 {leaked}" if leaked else "", problems)
        common.check(f"安装体积 {total / 1024 / 1024:.1f} MB < 25 MB", total < 25 * 1024 * 1024, "", problems)

    finally:
        common.cleanup(install.parent.parent)

    return common.report(problems)


if __name__ == "__main__":
    sys.exit(main())
