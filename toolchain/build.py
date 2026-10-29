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

    python toolchain/build.py desktop --install  # npm ci（无锁文件时退为 npm install）
    python toolchain/build.py desktop --check    # tsc --noEmit
    python toolchain/build.py desktop --test model  # 跑单个测试文件（子串匹配）
    python toolchain/build.py desktop --build    # 生产构建
    python toolchain/build.py desktop --perf     # 帧率探针：构建 + 内联成单文件

    python toolchain/build.py crossmachine       # 组装跨机测试包（依赖 --perf 产物）

为什么前端也要包装：gate.py 用 shell=False 跑配置里的命令（不存在 `cd x && y` 这种写法），
而且 node 不在系统 PATH 里。这里负责定位 node.exe 与 npm-cli.js，绕开 npm.cmd 对 cmd.exe
的隐含依赖。
"""

from __future__ import annotations

import os
import shutil
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


NODE_VERSIONS = Path.home() / ".workbuddy" / "binaries" / "node" / "versions"


def find_node() -> tuple[Path, Path]:
    """定位 node.exe 与 npm-cli.js。

    不用 npm.cmd：它是批处理垫片，需要 cmd.exe 解释，而 gate.py 用 shell=False 执行命令。
    直接调 npm-cli.js 只依赖 node.exe，行为确定。
    """
    candidates: list[Path] = []
    if os.environ.get("NODE_BIN"):
        candidates.append(Path(os.environ["NODE_BIN"]))
    if NODE_VERSIONS.exists():
        found = [p for p in NODE_VERSIONS.iterdir() if (p / "node.exe").exists()]
        candidates.extend(sorted(found, reverse=True))
    which = shutil.which("node")
    if which:
        candidates.append(Path(which).resolve().parent)

    for d in candidates:
        exe = d / "node.exe"
        cli = d / "node_modules" / "npm" / "bin" / "npm-cli.js"
        if exe.exists() and cli.exists():
            return exe, cli
    raise SystemExit("找不到 node.exe + npm-cli.js，请设置 NODE_BIN 指向 node 安装目录")


def desktop_target(rest: list[str]) -> int:
    app = ROOT / "desktop"
    if not (app / "package.json").exists():
        print(f"找不到 {app}/package.json，桌面端尚未脚手架")
        return 2

    node, cli = find_node()
    env = dict(os.environ)
    env["PATH"] = str(node.parent) + os.pathsep + env.get("PATH", "")
    # 证据日志要能 diff、能人工核对；ANSI 颜色码会写进去变成噪声
    env["NO_COLOR"] = "1"
    env["FORCE_COLOR"] = "0"

    def npm(*args: str) -> int:
        cmd = [str(node), str(cli), *args]
        print("[npm]", " ".join(args))
        return subprocess.run(cmd, cwd=str(app), env=env).returncode

    has_modules = (app / "node_modules").exists()

    if "--install" in rest:
        # 有锁文件时必须用 ci：install 会改写锁文件，那就不再是「可复现安装」
        return npm("ci") if (app / "package-lock.json").exists() else npm("install")

    if "--check" in rest:
        if not has_modules:
            print("node_modules 不存在，先跑 --install")
            return 3
        return npm("run", "typecheck")

    if "--build" in rest:
        if not has_modules:
            print("node_modules 不存在，先跑 --install")
            return 3
        return npm("run", "build")

    if "--perf" in rest:
        # 探针页单独构建：多入口会被 rollup 拆成共享 chunk，内联成单文件时
        # 跨 chunk 的 import 无法解析。见 desktop/vite.perf.config.ts 的注释。
        if not has_modules:
            print("node_modules 不存在，先跑 --install")
            return 3
        rc = npm("run", "build:perf")
        if rc != 0:
            return rc
        # 内联用当前解释器执行，不依赖 python 在 PATH 里
        inline = ROOT / "desktop" / "tools" / "inline_perf.py"
        print("[inline]", inline.relative_to(ROOT).as_posix())
        return subprocess.run([sys.executable, str(inline)], cwd=str(ROOT)).returncode

    if "--test" in rest:
        idx = rest.index("--test")
        if idx + 1 >= len(rest):
            print("--test 需要一个测试名（文件子串，例如 model）")
            return 2
        name = rest[idx + 1]
        vitest = app / "node_modules" / "vitest" / "vitest.mjs"
        if not vitest.exists():
            print("vitest 未安装，先跑 --install")
            return 3
        cmd = [str(node), str(vitest), "run", name]
        print("[vitest]", name)
        return subprocess.run(cmd, cwd=str(app), env=env).returncode

    print(__doc__)
    return 2


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(__doc__)
        return 2

    if argv[1] == "desktop":
        return desktop_target(argv[2:])

    if argv[1] == "crossmachine":
        pack = ROOT / "desktop" / "tools" / "pack_crossmachine.py"
        print("[pack]", pack.relative_to(ROOT).as_posix())
        return subprocess.run([sys.executable, str(pack)], cwd=str(ROOT)).returncode

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
