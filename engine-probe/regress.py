#!/usr/bin/env python3
"""新旧二进制回归比对。

目的：证明 natdiag 抽出来后行为没变。
判据：VERDICT 行必须一字不差；四个 slot 的语义状态必须一致。
      log_lines 允许浮动（libtorrent 重试次数随网络时序变化）。

为什么是 Python 而不是 shell：本环境的 bash 脚本里 fork Windows 子进程
会直接返回 127（无任何错误输出），而 Python 的 subprocess 直接走
CreateProcess，不受影响。

用法：
    python regress.py            # 默认 60 秒
    python regress.py 20         # 快速跑，低于 15 秒阈值会变 insufficient
"""

import re
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
TORRENT = HERE.parent / "m0-baseline" / "tests" / "data" / "debian-13.7.0-amd64-netinst.iso.torrent"
SECONDS_ARG = sys.argv[1] if len(sys.argv) > 1 else "60"

SLOTS = ["NAT-PMP/PCP TCP", "NAT-PMP/PCP UDP", "UPnP-IGD TCP", "UPnP-IGD UDP"]


def run(exe: Path, log: Path, save_dir: Path) -> int:
    cmd = [
        str(exe),
        "--torrent", str(TORRENT),
        "--seconds", SECONDS_ARG,
        "--save-path", str(save_dir),
    ]
    proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    log.write_bytes(proc.stdout)
    return proc.returncode


def slot_state(text: str, label: str) -> str:
    for line in text.splitlines():
        if not line.startswith(label):
            continue
        rest = line[len(label):].lstrip(" :")
        if "MAPPED" in rest:
            return "mapped"
        if "not attempted" in rest:
            return "no_request"
        if "no inbound mapping reply" in rest:
            return "no_reply"
        if "in flight, no result" in rest:
            return "no_reply"
        return "unknown"
    return "MISSING"


def verdict_of(text: str) -> str:
    for line in text.splitlines():
        if line.startswith("VERDICT:"):
            return line.strip()
    return ""


def log_lines_of(text: str) -> str:
    m = re.search(r"\((?:60s|\d+s), (\d+) log line", text)
    if m:
        return m.group(1)
    m = re.search(r"NAT-PMP/PCP = (\d+)", text)
    return m.group(1) if m else "?"


def main() -> int:
    legacy_exe = HERE / "build" / "engine-probe-legacy.exe"
    new_exe = HERE / "build" / "engine-probe.exe"
    for p in (legacy_exe, new_exe, TORRENT):
        if not p.exists():
            print(f"FAIL: 缺少 {p}", file=sys.stderr)
            return 2

    print(f"seed    : {TORRENT}")
    print(f"duration: {SECONDS_ARG}s\n")

    legacy_exit = run(legacy_exe, HERE / "probe-run-legacy.log", HERE / "probe-legacy")
    new_exit = run(new_exe, HERE / "probe-run-new.log", HERE / "probe-new")

    legacy_text = (HERE / "probe-run-legacy.log").read_text(encoding="utf-8", errors="replace")
    new_text = (HERE / "probe-run-new.log").read_text(encoding="utf-8", errors="replace")

    failed = False

    print("=== VERDICT ===")
    lv, nv = verdict_of(legacy_text), verdict_of(new_text)
    print(f"legacy: {lv}")
    print(f"new   : {nv}")
    if lv and lv == nv:
        print("  -> PASS")
    else:
        print("  -> FAIL")
        failed = True
    print()

    print("=== SLOTS ===")
    for label in SLOTS:
        a, b = slot_state(legacy_text, label), slot_state(new_text, label)
        ok = a == b and a != "MISSING"
        print(f"  {label:<16} legacy={a:<11} new={b:<11} {'PASS' if ok else 'FAIL'}")
        if not ok:
            failed = True
    print()

    print("=== 参考量（允许浮动，仅记录） ===")
    print(f"natpmp log lines: legacy={log_lines_of(legacy_text)}  new={log_lines_of(new_text)}")
    print(f"exit code legacy={legacy_exit} new={new_exit} （working 才为 0）")
    print()

    if failed:
        print("RESULT: FAIL — 结论不一致，看上面标 FAIL 的行")
        return 1
    print("RESULT: PASS — 新旧实现结论一致")
    return 0


if __name__ == "__main__":
    sys.exit(main())
