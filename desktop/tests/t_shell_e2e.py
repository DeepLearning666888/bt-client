#!/usr/bin/env python3
"""AC-04：壳端到端连通。

判定的不是「窗口开了」，而是「前端拿到数据了」。这两件事的区别在于：
窗口存在、进程活着、DOM 有节点，在「连不上引擎」的错误态里同样全部成立。
只有日志里那行 `frontend_ready port=<p> seq=<n>` 才能证明 `/state` 真的返回过快照 ——
它由前端在**首帧应用之后**主动回报。

用独立的 `BTCLIENT_DATA_DIR` 跑，不碰 `%APPDATA%` 下的真实数据。
单实例锁按数据目录区分（见 lib.rs 的 instance_key），所以这里不会跟正在使用的壳抢锁。
"""

from __future__ import annotations

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
EXIT_TIMEOUT_S = 20.0

READY_RE = re.compile(r"frontend_ready port=(\d+) seq=(\d+)")
DECISION_RE = re.compile(r"engine decision=(\w+) port=(\d+)")


def read_log(path: Path) -> str:
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8", errors="replace")


def wait_for(pattern: re.Pattern[str], log_path: Path, proc: subprocess.Popen[bytes],
             timeout: float) -> re.Match[str] | None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        match = pattern.search(read_log(log_path))
        if match is not None:
            return match
        # 进程死了就没什么可等的了，别耗满超时
        if proc.poll() is not None:
            return None
        time.sleep(0.2)
    return None


def close_window(pid: int) -> bool:
    """发 WM_CLOSE 而不是 taskkill /F。

    强制终止不会触发 Tauri 的 Exit 事件，引擎就走不到 `/shutdown`，
    用它测「退出时优雅关闭引擎」等于测了个寂寞。
    """
    result = subprocess.run(
        [
            "powershell", "-NoProfile", "-Command",
            f"$p = Get-Process -Id {pid} -ErrorAction SilentlyContinue; "
            f"if ($p) {{ $p.CloseMainWindow() }} else {{ 'gone' }}",
        ],
        capture_output=True, text=True, timeout=30,
    )
    return result.returncode == 0


def main() -> int:
    if not EXE.exists():
        print(f"找不到 {EXE}")
        print("先跑：python toolchain/build.py shell build --release")
        return 4

    data_dir = Path(tempfile.mkdtemp(prefix="btclient-e2e-"))
    log_path = data_dir / "shell.log"
    problems: list[str] = []
    proc: subprocess.Popen[bytes] | None = None

    try:
        print(f"数据目录：{data_dir}")
        print(f"启动：{EXE.name}")
        env = {**os.environ, "BTCLIENT_DATA_DIR": str(data_dir)}
        proc = subprocess.Popen(
            [str(EXE)],
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )

        # ① 引擎决策
        decision_match = wait_for(DECISION_RE, log_path, proc, READY_TIMEOUT_S)
        if decision_match is None:
            stderr = proc.stderr.read().decode("utf-8", "replace") if proc.stderr else ""
            problems.append("日志里没有 engine decision 行，壳没走到引擎解析")
            print("  日志内容：")
            print(read_log(log_path) or "（空）")
            if stderr:
                print(f"  stderr：{stderr[-800:]}")
        else:
            decision, port = decision_match.group(1), int(decision_match.group(2))
            print(f"① 引擎决策 {decision} port={port}")
            if decision != "spawn":
                # 全新空目录必须是 spawn。reuse 说明有残留实例，
                # 那前面的「干净启动」前提就不成立了。
                problems.append(f"空目录首次启动应当是 spawn，实际 {decision}")
            if not (0 < port < 65536):
                problems.append(f"端口不合法：{port}")

        # ② 前端就绪（核心断言）
        ready = wait_for(READY_RE, log_path, proc, READY_TIMEOUT_S)
        if ready is None:
            problems.append("日志里没有 frontend_ready —— 前端没拿到端点或没取到数据")
        else:
            ready_port, seq = int(ready.group(1)), int(ready.group(2))
            print(f"② 前端就绪 port={ready_port} seq={seq}")
            if decision_match is not None and ready_port != int(decision_match.group(2)):
                problems.append(
                    f"前端回报的端口 {ready_port} 与壳解析的 "
                    f"{decision_match.group(2)} 不一致，注入的端点不是这一份"
                )
            if seq < 0:
                problems.append(f"seq 不合法：{seq}")

        # ③ 窗口显示
        log_text = read_log(log_path)
        shown = "window shown" in log_text
        print(f"③ 窗口显示 {'✓' if shown else '✗'}")
        if not shown:
            problems.append("日志里没有 window shown")

        # ④ 正常关闭 → 引擎应当被 shutdown
        if proc.poll() is None and decision_match is not None:
            print("④ 发送关闭请求（WM_CLOSE）")
            close_window(proc.pid)
            try:
                proc.wait(timeout=EXIT_TIMEOUT_S)
            except subprocess.TimeoutExpired:
                problems.append("收到 WM_CLOSE 后壳没有退出")
                proc.kill()

            final = read_log(log_path)
            if decision_match.group(1) in ("spawn", "respawn"):
                shutdown_ok = "shutdown engine ok=true" in final
                print(f"   引擎优雅退出 {'✓' if shutdown_ok else '✗'}")
                if not shutdown_ok:
                    problems.append("退出时没有成功调用 /shutdown")
            else:
                print("   复用实例，不动它")

        # ⑤ 壳退出后引擎不该还活着（自己拉起的那种）
        if decision_match is not None and decision_match.group(1) in ("spawn", "respawn"):
            port = int(decision_match.group(2))
            # shutdown 返回 200 只说明「请求收到了」，引擎还要落盘再退。
            # 立刻查端口必然误判，所以给一段收敛时间。
            closed = wait_port_closed(port)
            print(f"⑤ 引擎已停止 {'✓' if closed else '✗'}（端口 {port}）")
            if not closed:
                problems.append(f"壳退出 10s 后引擎仍在监听 {port}")
    finally:
        if proc is not None and proc.poll() is None:
            proc.kill()
        shutil.rmtree(data_dir, ignore_errors=True)

    print()
    if problems:
        for problem in problems:
            print(f"  ✗ {problem}")
        print(f"失败 {len(problems)} 项")
        return 1
    print("通过：壳启动了引擎、前端取到首帧、退出时引擎被优雅关闭")
    return 0


def check_port(port: int) -> bool:
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.5)
        return sock.connect_ex(("127.0.0.1", port)) == 0


def wait_port_closed(port: int, timeout: float = 10.0) -> bool:
    """等端口停止监听。引擎收到 /shutdown 后还要落盘，进程不会立刻消失。"""
    import time as _time

    deadline = _time.time() + timeout
    while _time.time() < deadline:
        if not check_port(port):
            return True
        _time.sleep(0.1)
    return False


if __name__ == "__main__":
    sys.exit(main())
