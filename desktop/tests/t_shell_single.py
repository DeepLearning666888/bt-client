#!/usr/bin/env python3
"""AC-05：第二个实例不再拉起第二个引擎。

为什么这条值得单独测：引擎自己有单实例锁，第二个会以退出码 2 立刻死掉。
所以「多开了」的现场表现不是崩溃，而是**点了图标没反应** —— 壳起来了、
窗口出现了、然后引擎死了、界面永远转圈。这类问题在日志里也只留下两行，
不专门测就发现不了。

判据是「引擎进程恰好一个」，而不是「第二个壳退出了」：
后者可以通过任何方式达成（比如第二个壳压根没启动引擎），
只有前者能证明单实例锁真的挡住了重复的 spawn。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
EXE = ROOT / "desktop" / "src-tauri" / "target" / "release" / "btclient-shell.exe"
READY_TIMEOUT_S = 90.0
SECOND_EXIT_TIMEOUT_S = 15.0

READY_RE = re.compile(r"frontend_ready port=\d+ seq=\d+")


def read_log(path: Path) -> str:
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8", errors="replace")


def wait_for_text(needle: str, log_path: Path, timeout: float) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if needle in read_log(log_path):
            return True
        time.sleep(0.2)
    return False


def count_engines(data_dir: Path) -> int:
    """数属于这个数据目录的 engine.exe 进程。

    按命令行过滤而不是数全部 engine.exe：开发机上可能还有别的实例在跑
    （比如用户自己开着壳），那不属于本测试的判定范围。
    """
    script = (
        "$dir = $env:BTCLIENT_COUNT_DIR; "
        "Get-CimInstance Win32_Process -Filter \"Name='engine.exe'\" | "
        "Where-Object { $_.CommandLine -and $_.CommandLine.Contains($dir) } | "
        "Select-Object -Property ProcessId, CommandLine | ConvertTo-Json -Compress"
    )
    result = subprocess.run(
        ["powershell", "-NoProfile", "-Command", script],
        capture_output=True, text=True, timeout=60,
        env={**os.environ, "BTCLIENT_COUNT_DIR": str(data_dir)},
    )
    text = (result.stdout or "").strip()
    if text == "" or text == "null":
        return 0
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return -1
    return 1 if isinstance(parsed, dict) else len(parsed)


def close_window(pid: int) -> None:
    subprocess.run(
        [
            "powershell", "-NoProfile", "-Command",
            f"$p = Get-Process -Id {pid} -ErrorAction SilentlyContinue; "
            f"if ($p) {{ $p.CloseMainWindow() }}",
        ],
        capture_output=True, text=True, timeout=30,
    )


def main() -> int:
    if not EXE.exists():
        print(f"找不到 {EXE}")
        print("先跑：python toolchain/build.py shell build --release")
        return 4

    data_dir = Path(tempfile.mkdtemp(prefix="btclient-single-"))
    log_path = data_dir / "shell.log"
    env = {**os.environ, "BTCLIENT_DATA_DIR": str(data_dir)}
    first: subprocess.Popen[bytes] | None = None
    problems: list[str] = []

    try:
        print(f"数据目录：{data_dir}")
        first = subprocess.Popen(
            [str(EXE)], env=env, stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        if not wait_for_text("frontend_ready", log_path, READY_TIMEOUT_S):
            print("第一个实例没能就绪，后续判定无意义")
            print(read_log(log_path) or "（日志为空）")
            return 1
        print("① 第一个实例就绪")

        engines_after_first = count_engines(data_dir)
        print(f"② 引擎进程数 {engines_after_first}")
        if engines_after_first != 1:
            problems.append(f"第一个实例就绪后引擎数应为 1，实际 {engines_after_first}")

        print("③ 启动第二个实例")
        second = subprocess.Popen(
            [str(EXE)], env=env, stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        try:
            second_code = second.wait(timeout=SECOND_EXIT_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            second_code = None
            second.kill()

        log_text = read_log(log_path)
        blocked = "another instance is running" in log_text
        print(f"③ 第二个实例退出码 {second_code}，单实例拦截 {'✓' if blocked else '✗'}")
        if second_code is None:
            problems.append("第二个实例没有退出，说明单实例锁没生效")
        elif second_code != 0:
            problems.append(f"第二个实例应以 0 退出（安静让路），实际 {second_code}")
        if not blocked:
            problems.append("日志里没有 another instance is running")

        # 核心判据：引擎数量没有因为第二个实例而增加
        engines_after_second = count_engines(data_dir)
        print(f"④ 引擎进程数 {engines_after_second}（第二个实例之后）")
        if engines_after_second != 1:
            problems.append(
                f"引擎数应保持 1，实际 {engines_after_second} —— 第二个实例重复拉起了引擎"
            )

        # 第一个实例的端点仍然可用（没被第二个实例的 spawn 抢走）
        match = re.search(r"engine decision=\w+ port=(\d+)", log_text)
        if match is None:
            problems.append("日志里没有 engine decision 行")
        else:
            port = int(match.group(1))
            import socket

            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                sock.settimeout(1.0)
                alive = sock.connect_ex(("127.0.0.1", port)) == 0
            print(f"⑤ 原引擎端口 {port} {'仍可用' if alive else '已失效'}")
            if not alive:
                problems.append(f"第二个实例启动后原引擎端口 {port} 失效了")
    finally:
        if first is not None and first.poll() is None:
            close_window(first.pid)
            try:
                first.wait(timeout=20)
            except subprocess.TimeoutExpired:
                first.kill()
        shutil.rmtree(data_dir, ignore_errors=True)

    print()
    if problems:
        for problem in problems:
            print(f"  ✗ {problem}")
        print(f"失败 {len(problems)} 项")
        return 1
    print("通过：第二个实例安静退出，引擎始终只有一个")
    return 0


if __name__ == "__main__":
    sys.exit(main())
