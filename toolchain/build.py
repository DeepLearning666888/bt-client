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

    python toolchain/build.py shell build --release   # 编译 Tauri 壳
    python toolchain/build.py shell test              # 壳的单元测试

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


def find_cargo() -> Path:
    """定位 cargo 可执行文件。

    和 node 同样的理由：gate.py 用 shell=False，命令里不能指望 PATH 查找，
    也不接受 `cd x && cargo y` 这种写法。找到绝对路径再执行。
    """
    candidates = []
    if os.environ.get("CARGO_BIN"):
        candidates.append(Path(os.environ["CARGO_BIN"]))
    which = shutil.which("cargo")
    if which:
        candidates.append(Path(which))
    candidates.append(Path.home() / ".cargo" / "bin" / "cargo.exe")
    for path in candidates:
        if path.exists() and path.is_file():
            return path
    raise SystemExit("找不到 cargo，请安装 Rust 工具链或设置 CARGO_BIN")


def shell_target(rest: list[str]) -> int:
    """跑 cargo，附带手工拼装的 MSVC 环境。

    Rust 自己不需要 vcvars，但它的**链接阶段**要 link.exe 和 LIB 里的
    kernel32.lib 等导入库。本机 reg.exe 被拦截导致 vcvars 产出残缺，
    所以复用 build_env() 那套已知路径拼装 —— 与 C++ 侧同一个来源，
    两边不会因为环境不一致而出现「C++ 能编、Rust 链接失败」这种分裂。
    """
    cargo = find_cargo()
    manifest = ROOT / "desktop" / "src-tauri" / "Cargo.toml"
    if not manifest.exists():
        print(f"找不到 {manifest}")
        return 2

    env = build_env()
    # 关掉颜色码：证据日志里混 ANSI 转义会让 grep 统计结果为 0（踩过一次）
    env["CARGO_TERM_COLOR"] = "never"
    env["NO_COLOR"] = "1"

    # --manifest-path 是子命令选项，不是 cargo 全局选项：必须放在 build/test 之后，
    # 否则 cargo 直接报 "unexpected argument"
    cmd = [str(cargo), rest[0], "--manifest-path", str(manifest), *rest[1:]]
    print("[cargo]", " ".join(rest))
    # 逐行转发：首次编译十几分钟，没有输出会让人以为卡死
    return subprocess.run(cmd, cwd=str(ROOT), env=env).returncode


def bundle_target(rest: list[str]) -> int:
    """打 NSIS 安装包。

    走 `tauri build` 而不是手拼 NSIS 脚本：WebView2 引导、快捷方式、卸载注册表项
    这几件事的模板由 Tauri 维护，手写一遍只会引入自己的 bug。

    两个必要前提，缺一不可：
    - **cargo 必须在 PATH 里**。tauri CLI 内部用 `cargo build` 而非绝对路径，
      本机 cargo 不在 PATH（同 node 的情况），所以显式补进去。
    - **MSVC 环境**。链接阶段要 link.exe，理由同 shell_target()。

    前端产物不在这里构建 —— `config.toml` 的 build 列表里 `desktop --build` 已排在前面。
    这也意味着单独跑本函数时 dist/ 必须已存在，否则 tauri 会用上一次的陈旧产物，
    那种「改了代码但装出来的还是旧界面」最难查。
    """
    app = ROOT / "desktop"
    dist = app / "dist" / "index.html"
    if not dist.exists():
        print(f"找不到 {dist}，先跑：python toolchain/build.py desktop --build")
        return 3

    cli = app / "node_modules" / "@tauri-apps" / "cli" / "tauri.js"
    if not cli.exists():
        print(f"找不到 {cli}，先跑：python toolchain/build.py desktop --install")
        return 3

    node, _ = find_node()
    cargo = find_cargo()

    env = build_env()
    env["CARGO_TERM_COLOR"] = "never"
    env["NO_COLOR"] = "1"
    # tauri CLI 靠 `cargo` 这个名字去 PATH 找，不能给它绝对路径（它不接受）
    env["PATH"] = str(cargo.parent) + os.pathsep + env.get("PATH", "")

    cmd = [str(node), str(cli), "build", "--no-bundle" if "--no-bundle" in rest else ""]
    cmd = [part for part in cmd if part]
    print("[tauri]", " ".join(cmd[2:]))
    rc = subprocess.run(cmd, cwd=str(app), env=env).returncode
    if rc != 0 or "--no-bundle" in rest:
        return rc
    # tauri 直接出的包记录源文件 mtime（AC-01 不可复现的根因），重打一次
    return repack_deterministic(app, env)


def repack_deterministic(app: Path, env: dict[str, str]) -> int:
    """用 `SetDateSave off` 重打 NSIS 安装包，保证产物字节可复现。

    AC-01 要求「同输入重打包，两次哈希一致」。tauri 调 makensis 时不带
    SetDateSave，NSIS 数据块默认把源文件 mtime 写进安装包；而每次构建后
    输入文件的 mtime 都被刷新（cargo 重新链接 shell.exe、tauri 重新生成
    installer.nsi 等四份脚本），solid LZMA 压缩流随之一字节变、整包哈希变。

    这里的做法：tauri 出包后，用 makensis 的 -X 选项在脚本处理前注入
    `SetDateSave off`，重跑同一份 installer.nsi，用结果覆盖 tauri 的包。
    数据块不再记录 mtime，只要输入文件字节一致，输出字节就一致。
    """
    nsis_x64 = app / "src-tauri" / "target" / "release" / "nsis" / "x64"
    script = nsis_x64 / "installer.nsi"
    bundle_dir = app / "src-tauri" / "target" / "release" / "bundle" / "nsis"
    if not script.exists() or not bundle_dir.exists():
        print(f"找不到 {script} 或 {bundle_dir}，跳过确定性重打包")
        return 4

    candidates = []
    if os.environ.get("TAURI_MAKENSIS"):
        candidates.append(Path(os.environ["TAURI_MAKENSIS"]))
    candidates.append(Path.home() / "AppData" / "Local" / "tauri" / "NSIS" / "makensis.exe")
    which = shutil.which("makensis")
    if which:
        candidates.append(Path(which))
    makensis = next((p for p in candidates if p.exists() and p.is_file()), None)
    if makensis is None:
        print("找不到 makensis.exe（本机安装在 Tauri 的 NSIS 目录），跳过确定性重打包")
        return 4

    cmd = [str(makensis), "-INPUTCHARSET", "UTF8", "-OUTPUTCHARSET", "UTF8",
           "-V3", "-XSetDateSave off", str(script)]
    print("[makensis]", " ".join(cmd[1:]))
    rc = subprocess.run(cmd, cwd=str(nsis_x64), env=env).returncode
    if rc != 0:
        print("确定性重打包失败，保留 tauri 原包")
        return rc

    out = nsis_x64 / "nsis-output.exe"
    setups = sorted(p for p in bundle_dir.glob("*.exe") if p.name.endswith("setup.exe"))
    if not out.exists() or len(setups) != 1:
        print(f"产物定位异常（out={out.exists()} setups={len(setups)}），保留 tauri 原包")
        return 4
    shutil.copyfile(out, setups[0])
    print(f"[repack] 已用 SetDateSave off 产物覆盖 {setups[0].name}")
    return 0



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

    if argv[1] == "shell":
        if len(argv) < 3:
            print("用法：python toolchain/build.py shell <cargo 子命令...>")
            print("例如：python toolchain/build.py shell build --release")
            print("      python toolchain/build.py shell bundle   # 打 NSIS 安装包")
            return 2
        if argv[2] == "bundle":
            return bundle_target(argv[3:])
        return shell_target(argv[2:])

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
