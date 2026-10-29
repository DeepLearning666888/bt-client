#!/usr/bin/env python3
"""AC-06：release 产物存在、是 GUI 子系统、且大到足以说明前端资源被打进去。

三项检查各自对应一种真实的失败：

1. **存在** —— 编译失败时 gate 已经会拦，但产物被误删（`cargo clean`、杀软隔离）
   时退出码仍然是 0。单独查一次不贵。
2. **GUI 子系统** —— `windows_subsystem = "windows"` 没生效的话，双击会先弹一个
   黑框再出窗口。这个属性只在 release 生效，debug 下测不出来，所以必须查产物本身。
3. **体积** —— 前端 227 KB（gzip 71 KB）会被嵌进二进制。如果忘了设 `frontendDist`
   或资源没打进去，exe 会瘦到 2 MB 出头，运行起来是一片白。

不做「删掉 dist 再启动」那种验证：它要改名目录、启动 GUI、再改回来，
中途失败会把工作区留在不一致状态。体积检查已经能覆盖同一件事。
"""

from __future__ import annotations

import struct
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
EXE = ROOT / "desktop" / "src-tauri" / "target" / "release" / "btclient-shell.exe"

MIN_BYTES = 3 * 1024 * 1024
SUBSYSTEM_GUI = 2


def pe_subsystem(path: Path) -> int | None:
    """读 PE 可选头里的 Subsystem 字段。2 = GUI，3 = 控制台。"""
    data = path.read_bytes()[:4096]
    if data[:2] != b"MZ":
        return None
    e_lfanew = struct.unpack_from("<I", data, 0x3C)[0]
    if data[e_lfanew : e_lfanew + 4] != b"PE\0\0":
        return None
    # Signature(4) + COFF(20) 之后是可选头；Subsystem 在可选头偏移 68
    offset = e_lfanew + 24 + 68
    if offset + 2 > len(data):
        return None
    return struct.unpack_from("<H", data, offset)[0]


def main() -> int:
    problems: list[str] = []

    if not EXE.exists():
        print(f"缺少 {EXE}")
        print("先跑：python toolchain/build.py shell build --release")
        return 4

    size = EXE.stat().st_size
    print(f"① 产物 {EXE.name}  {size / 1024 / 1024:.2f} MB")
    if size < MIN_BYTES:
        problems.append(
            f"体积只有 {size / 1024 / 1024:.2f} MB，低于 {MIN_BYTES // 1024 // 1024} MB ——"
            "前端资源很可能没被打进二进制"
        )

    subsystem = pe_subsystem(EXE)
    print(f"② PE 子系统 {subsystem}（2=GUI，3=控制台）")
    if subsystem != SUBSYSTEM_GUI:
        problems.append(
            f"子系统是 {subsystem}，不是 GUI —— 双击会先弹控制台窗口"
        )

    print()
    if problems:
        for problem in problems:
            print(f"  ✗ {problem}")
        print(f"失败 {len(problems)} 项")
        return 1
    print("通过：release 产物存在、为 GUI 子系统、体积符合内嵌前端的预期")
    return 0


if __name__ == "__main__":
    sys.exit(main())
