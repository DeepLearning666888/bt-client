#!/usr/bin/env python3
"""构建脚本：自带 MSVC 环境，不依赖 vcvars64.bat。

为什么需要它：本机 reg.exe 被安全策略拦截，vcvars 读不到 VS/SDK 注册信息，
产出的 INCLUDE/LIB/PATH 是残缺的，CMake 会卡在编译器探测（找不到 rc.exe）。
这里按已知路径手工拼装，等价于 vcvars 的产出且不碰注册表。

用法：
    python toolchain/build.py engine            # 需要时自动 configure，然后构建
    python toolchain/build.py engine --configure # 强制重新 configure
    python toolchain/build.py natdiag
    python toolchain/build.py engine-probe
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

MSVC_VER = "14.44.35207"
SDK_VER = "10.0.22621.0"
MSVC = Path(rf"C:\BuildTools\VC\Tools\MSVC\{MSVC_VER}")
SDK = Path(r"C:\Program Files (x86)\Windows Kits\10")
CMAKE = Path(r"C:\vcpkg-tools\cmake\cmake-4.4.4-windows-x86_64\bin\cmake.exe")
NINJA = Path(r"C:\vcpkg-tools\ninja\ninja.exe")
BOOST = Path(r"C:\boost\boost_1_86_0")
LIBTORRENT_DIR = Path(r"C:\libtorrent\install\lib\cmake\LibtorrentRasterbar")

# 三个工程共用同一套外部依赖定位参数
COMMON_CMAKE_ARGS = [
    "-G", "Ninja",
    "-DCMAKE_BUILD_TYPE=Release",
    f"-DCMAKE_MAKE_PROGRAM={NINJA.as_posix()}",
    f"-DCMAKE_CXX_COMPILER={(MSVC / 'bin/Hostx64/x64/cl.exe').as_posix()}",
    f"-DBoost_INCLUDE_DIR={BOOST.as_posix()}",
]


def build_env() -> dict[str, str]:
    missing = [str(p) for p in (MSVC, SDK, CMAKE, NINJA, BOOST) if not p.exists()]
    if missing:
        raise SystemExit("工具链路径不存在：" + "、".join(missing))

    include = ";".join([
        str(MSVC / "include"),
        r"C:\BuildTools\VC\Auxiliary\VS\include",
        str(SDK / f"Include/{SDK_VER}/ucrt"),
        str(SDK / f"Include/{SDK_VER}/shared"),
        str(SDK / f"Include/{SDK_VER}/um"),
        str(SDK / f"Include/{SDK_VER}/winrt"),
    ])
    lib = ";".join([
        str(MSVC / "lib/x64"),
        str(SDK / f"Lib/{SDK_VER}/ucrt/x64"),
        str(SDK / f"Lib/{SDK_VER}/um/x64"),
    ])
    path = ";".join([
        str(MSVC / "bin/Hostx64/x64"),
        str(SDK / f"bin/{SDK_VER}/x64"),
        str(CMAKE.parent),
        str(NINJA.parent),
        os.environ.get("PATH", ""),
    ])

    env = dict(os.environ)
    env.update({"INCLUDE": include, "LIB": lib, "PATH": path, "BOOST_ROOT": str(BOOST)})
    return env


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(__doc__)
        return 2

    target = argv[1]
    force_configure = "--configure" in argv[2:]

    source = ROOT / target
    build_dir = source / "build"
    if not (source / "CMakeLists.txt").exists():
        print(f"找不到 {source}/CMakeLists.txt")
        return 2

    env = build_env()

    if force_configure or not (build_dir / "CMakeCache.txt").exists():
        cmd = [str(CMAKE), "-S", str(source), "-B", str(build_dir)] + COMMON_CMAKE_ARGS
        if (source / "CMakeLists.txt").read_text(encoding="utf-8").find("LibtorrentRasterbar") >= 0:
            cmd.append(f"-DLibtorrentRasterbar_DIR={LIBTORRENT_DIR.as_posix()}")
        print("[configure]", " ".join(cmd))
        result = subprocess.run(cmd, env=env)
        if result.returncode != 0:
            return result.returncode

    cmd = [str(CMAKE), "--build", str(build_dir)]
    print("[build]", " ".join(cmd))
    return subprocess.run(cmd, env=env).returncode


if __name__ == "__main__":
    sys.exit(main(sys.argv))
