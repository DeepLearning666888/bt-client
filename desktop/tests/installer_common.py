#!/usr/bin/env python3
"""安装包测试的公共部分：定位、静默安装、静默卸载、读日志。

三个测试脚本（build / install / uninstall）共用的东西放这里，
尤其是安装那一步 —— 它有一个**静默失败**的陷阱，重复实现三次就会错三次。
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
BUNDLE = ROOT / "desktop" / "src-tauri" / "target" / "release" / "bundle" / "nsis"

# 安装后应当恰好出现的文件。多一个少一个都要报出来 ——
# 「装上了但少了 DLL」和「装上了并多带了 src/」是两类完全不同的缺陷。
EXPECTED_FILES = {
    "btclient-shell.exe",
    "engine.exe",
    "torrent-rasterbar.dll",
    "uninstall.exe",
}

# 绝不该出现在安装目录里的东西。它们出现说明打包范围失控。
FORBIDDEN_NAMES = {"node_modules", "src", "src-tauri", "Cargo.toml", "Cargo.lock",
                   "package.json", "package-lock.json", "index.html", "tests"}

READY_RE = re.compile(r"frontend_ready port=(\d+) seq=(\d+)")
DECISION_RE = re.compile(r"engine decision=(\w+) port=(\d+)")
ENGINE_EXE_RE = re.compile(r"engine_exe=(.+)")


def find_setup() -> Path:
    """定位安装包。文件名含中文与版本号，所以按后缀找而不是写死。"""
    if not BUNDLE.is_dir():
        raise SystemExit(
            f"找不到 {BUNDLE}\n先跑：python toolchain/build.py desktop --build && python toolchain/build.py shell bundle"
        )
    candidates = sorted(BUNDLE.glob("*setup.exe"))
    if not candidates:
        raise SystemExit(f"{BUNDLE} 里没有 *setup.exe，打包可能没成功")
    if len(candidates) > 1:
        # 多版本共存时取最新的，但要说出来 —— 静默挑一个是另一种误导
        newest = max(candidates, key=lambda p: p.stat().st_mtime)
        print(f"  注意：发现 {len(candidates)} 个安装包，取最新的 {newest.name}")
        return newest
    return candidates[0]


def make_workspace(tag: str) -> tuple[Path, Path]:
    """建一个含空格与中文的临时根目录，返回 (安装目录, 数据目录)。

    路径故意带空格和中文：这两样恰好是真实用户环境里最常见、而开发机上最少见的东西。
    安装目录带空格验证 NSIS 与 CreateProcess 的引号处理；带中文验证 ANSI/UTF-8 编码。
    两者都踩过坑 —— 中文那次是引擎把 GBK 字节写进 JSON，表现为壳等 20 秒超时。
    """
    base = Path(tempfile.mkdtemp(prefix=f"inst-{tag}-")) / "有 空格 的 根 目录"
    install = base / "安装 目录"
    data = base / "数据 目录"
    install.mkdir(parents=True)
    return install, data


def run_setup(install: Path, timeout: float = 180.0) -> int:
    """静默安装到指定目录。

    **命令行必须是一条字符串，不能是参数列表。**
    NSIS 的 `/D=` 后面跟着的整个剩余部分被当作路径，且**不能加引号**。
    用列表形式传 `[setup, "/S", f"/D={path}"]` 时，Python 的 list2cmdline 会因为路径
    含空格而自动补引号，变成 `/D="C:\\...\\安装 目录"`；NSIS 把它当成带引号的合法路径，
    创建失败，但因为静默模式下不报错，**退出码仍然是 0**，最后表现为「装完了但目录是空的」。

    这个坑的杀伤力在于退出码 0：任何只看返回码的脚本都会认为成功。
    所以下面额外断言目录非空，而不是相信退出码。
    """
    setup = find_setup()
    cmdline = f'"{setup}" /S /D={install}'
    result = subprocess.run(cmdline, capture_output=True, timeout=timeout)
    return result.returncode


def wait_installed(install: Path, timeout: float = 60.0) -> set[str]:
    """等安装落地。NSIS 静默安装可能把实际工作放给子进程，退出码不能作为完成信号。"""
    deadline = time.time() + timeout
    names: set[str] = set()
    while time.time() < deadline:
        names = {p.name for p in install.iterdir()}
        if "uninstall.exe" in names and "btclient-shell.exe" in names:
            return names
        time.sleep(0.3)
    return names


def run_uninstall(install: Path, timeout: float = 180.0) -> int:
    """静默卸载。卸载器要删掉自己，所以不能指望它退出后目录立刻空。

    **别用 bash 手工验证这一步。** Git Bash（MSYS）会把不成对出现的 `/S`
    当成路径做转换，变成 `S:\\` 之类传给卸载器。卸载器收到一个不认识的参数，
    不走静默分支，返回 0 却什么都没删 —— 看起来就像「卸载功能坏了」。
    用 Python 或 cmd 调用才是可信的。
    """
    uninstaller = install / "uninstall.exe"
    if not uninstaller.exists():
        raise SystemExit(f"找不到 {uninstaller}")
    result = subprocess.run([str(uninstaller), "/S"], capture_output=True, timeout=timeout)
    return result.returncode


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
        if proc.poll() is not None:
            # 进程退出后再给它一次机会：日志可能已写完但还没被读到
            match = pattern.search(read_log(log_path))
            if match is not None:
                return match
            return None
        time.sleep(0.2)
    return None


def stop(proc: subprocess.Popen[bytes], timeout: float = 15.0) -> None:
    """结束壳进程树。壳拉起引擎，终止壳不会自动带走引擎。"""
    if proc.poll() is not None:
        return
    subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                   capture_output=True, check=False)
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()


def cleanup(path: Path) -> None:
    """删临时工作区。Windows 上文件句柄可能还没释放，重试几次。"""
    for _ in range(5):
        try:
            shutil.rmtree(path)
            return
        except OSError:
            time.sleep(0.4)


def check(label: str, ok: bool, detail: str = "", problems: list[str] | None = None) -> bool:
    """就地输出一条检查结果。

    就地打印而不是攒到最后统一输出：攒起来会让章节标题和它的内容在输出里分开，
    读的人得来回对照才知道哪条失败属于哪一步。
    """
    print(f"  {'✓' if ok else '✗'} {label}" + (f"（{detail}）" if detail else ""))
    if not ok and problems is not None:
        problems.append(label)
    return ok


def report(problems: list[str]) -> int:
    """统一汇总。每条检查已经就地打印过，这里只负责结论与退出码。"""
    print()
    if problems:
        print(f"失败 {len(problems)} 项：")
        for problem in problems:
            print(f"  · {problem}")
        return 1
    print("全部通过")
    return 0
