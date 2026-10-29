"""把 NSIS 工具链预置到 Tauri 的缓存目录，绕开 github.com 不可达。

## 为什么需要这个脚本

`tauri build` 打 NSIS 安装包时，会从 github.com 下载两个文件：

- `nsis-3.13.zip`（含 makensis.exe 与 stubs）
- `nsis_tauri_utils.dll`（Tauri 自己的 NSIS 插件）

本项目所在环境的出口代理对 github.com 返回 502，直连也不通，所以这一步必失败。
crates.io 与 npm registry 都可达，唯独 github 不行 —— 这决定了只能走镜像。

## 它到底期望什么结构（不是猜的）

结构由 tauri-bundler 源码（docs.rs 上的 `src/bundle/windows/nsis/mod.rs`）确定：

    tauri_tools_path = dirs::cache_dir() / "tauri"        // Windows: %LOCALAPPDATA%\tauri
    nsis_toolset_path = tauri_tools_path / "NSIS"
    // zip 解压到 tauri_tools_path，再把 nsis-3.13 重命名为 NSIS
    // ⇒ NSIS/makensis.exe，不是 NSIS/nsis-3.13/makensis.exe

判定顺序（任何一步不过就删库重下）：

    1. !nsis_toolset_path.exists()                        → 下载
    2. NSIS_REQUIRED_FILES 任一缺失                        → 删库重下（"missing some files"）
    3. NSIS.exe 的 sha1 != NSIS_EXE_SHA1                  → 删库重下（"outdated"）
    4. NSIS_REQUIRED_FILES_HASH 里任一文件哈希不符          → 只重下那几个文件

所以要让它彻底跳过下载，必须同时满足 1–4。少一样就会看到 "Recreating it" 然后
回到 502，报错信息停在同一行，看不出是校验失败 —— 这个脚本把四步都显式断言出来。

## 镜像的可信度

镜像只做原样转发，脚本对**每个**下载物核对硬编码 SHA1 —— 这些 SHA1 逐字取自
tauri-bundler 源码里 `NSIS_SHA1` / `NSIS_EXE_SHA1` / `NSIS_TAURI_UTILS_SHA1` 常量。
哈希一致即等价于拿到官方文件，镜像本身不需要被信任。

用法：

    python toolchain/fetch_nsis.py              # 用默认镜像
    python toolchain/fetch_nsis.py --check      # 只检查现状，不下载
    python toolchain/fetch_nsis.py --mirror https://ghproxy.net/https://github.com
"""

from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import sys
import time
import tempfile
import urllib.request
import zipfile
from pathlib import Path

# ---- 与 tauri-bundler 源码逐字一致（改动前先核对上游）----
NSIS_URL = "https://github.com/tauri-apps/binary-releases/releases/download/nsis-3.13/nsis-3.13.zip"
NSIS_SHA1 = "DB14F8AF2EA346B786C6AE19343F61AAF5E09B8E"
NSIS_EXE_SHA1 = "6D45D4E8CCF18FE1FDAC9164058F356102A2ECF2"
UTILS_URL = "https://github.com/tauri-apps/nsis-tauri-utils/releases/download/nsis_tauri_utils-v0.5.3/nsis_tauri_utils.dll"
UTILS_SHA1 = "75197FEE3C6A814FE035788D1C34EAD39349B860"

# 与源码 NSIS_REQUIRED_FILES 一致，外加 NSIS.exe（第三步单独校验它）
REQUIRED = (
    "makensis.exe",
    "NSIS.exe",
    "Bin/makensis.exe",
    "Stubs/lzma-x86-unicode",
    "Stubs/lzma_solid-x86-unicode",
    "Plugins/x86-unicode/additional/nsis_tauri_utils.dll",
    "Include/MUI2.nsh",
    "Include/FileFunc.nsh",
    "Include/x64.nsh",
    "Include/nsDialogs.nsh",
    "Include/WinMessages.nsh",
    "Include/Win/COM.nsh",
    "Include/Win/Propkey.nsh",
    "Include/Win/RestartManager.nsh",
)

DEFAULT_MIRROR = "https://ghfast.top/https://github.com"
ZIP_TOP = "nsis-3.13"  # zip 内的顶层目录名，解压时剥掉


def cache_root() -> Path:
    base = os.environ.get("LOCALAPPDATA") or tempfile.gettempdir()
    return Path(base) / "tauri"


def sha1_of(path: Path) -> str:
    digest = hashlib.sha1()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def probe(toolset: Path) -> list[str]:
    """返回不合格的原因列表，空列表 = 完全就绪。"""
    problems: list[str] = []
    if not toolset.exists():
        return ["目录不存在"]

    missing = [p for p in REQUIRED if not (toolset / p).exists()]
    if missing:
        problems.append(f"缺 {len(missing)} 个必需文件：{', '.join(missing[:4])}" + ("..." if len(missing) > 4 else ""))

    exe = toolset / "NSIS.exe"
    if exe.exists() and sha1_of(exe) != NSIS_EXE_SHA1:
        problems.append("NSIS.exe 哈希不符（会被判为 outdated 并重下）")

    dll = toolset / "Plugins" / "x86-unicode" / "additional" / "nsis_tauri_utils.dll"
    if dll.exists() and sha1_of(dll) != UTILS_SHA1:
        problems.append("nsis_tauri_utils.dll 哈希不符（会被单独重下）")

    return problems


def load_local(directory: Path, expected: str, label: str) -> bytes | None:
    """从本地目录取已下载好的文件。返回 None 表示这个目录里没有。"""
    candidate = directory / label
    if not candidate.is_file():
        return None
    data = candidate.read_bytes()
    actual = hashlib.sha1(data).hexdigest().upper()
    if actual != expected:
        # 本地文件同样要校验：离线包也可能是旧的或传坏的
        raise SystemExit(f"{candidate} 的 SHA1 是 {actual}，期望 {expected}，拒绝使用")
    print(f"  复用本地 {candidate}（{len(data)} 字节，SHA1 与上游一致）")
    return data


def fetch(url: str, mirror: str, expected: str, label: str) -> bytes:
    candidates = [url]
    if mirror:
        # 镜像是「替换 https://github.com 这一段」，不是「加在 scheme 之后」。
        # 写成 mirror + 去掉 scheme 的 URL 会得到 ghfast.top/https://github.com/github.com/...
        # —— 多一层 github.com，镜像返回 502，报错还看不出是拼接问题。
        upstream = "https://github.com"
        if not url.startswith(upstream + "/"):
            raise SystemExit(f"镜像模式只支持 github.com 的地址，当前是 {url}")
        candidates.insert(0, mirror.rstrip("/") + url[len(upstream):])

    last_error = ""
    for candidate in candidates:
        for attempt in range(1, 4):
            try:
                print(f"  下载 {label}: {candidate}" + (f"（第 {attempt} 次）" if attempt > 1 else ""))
                with urllib.request.urlopen(candidate, timeout=120) as response:
                    data = response.read()
            except Exception as exc:  # 网络异常种类很多，统一降级到下一个候选
                last_error = f"{type(exc).__name__}: {exc}"
                print(f"    失败（{last_error}）")
                # 出口代理会间歇性返回 502，等一会儿再试同一地址往往就通了
                time.sleep(2 * attempt)
                continue

            actual = hashlib.sha1(data).hexdigest().upper()
            if actual != expected:
                # 哈希不符绝不能接受：镜像返回了别的东西，或者传输损坏
                last_error = f"SHA1 不符：拿到 {actual}，期望 {expected}"
                print(f"    {last_error}")
                break
            print(f"    成功，{len(data)} 字节，SHA1 与上游一致")
            return data

    raise SystemExit(f"{label} 下载失败，最后错误：{last_error}")


def install(toolset: Path, zip_bytes: bytes, dll_bytes: bytes) -> None:
    # 先清空：残留的半套工具链是最坏情况，会让下一步的判定自相矛盾
    if toolset.exists():
        shutil.rmtree(toolset)
    toolset.mkdir(parents=True)

    with tempfile.TemporaryDirectory(prefix="nsis-x-") as tmp:
        archive = Path(tmp) / "nsis.zip"
        archive.write_bytes(zip_bytes)
        with zipfile.ZipFile(archive) as bundle:
            for member in bundle.namelist():
                if not member.startswith(ZIP_TOP + "/"):
                    continue
                relative = member[len(ZIP_TOP) + 1:]
                if not relative:
                    continue
                # 防目录穿越：zip 条目的路径必须落在目标目录内
                target = (toolset / relative).resolve()
                target.relative_to(toolset.resolve())
                if member.endswith("/"):
                    target.mkdir(parents=True, exist_ok=True)
                else:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with bundle.open(member) as src, target.open("wb") as dst:
                        shutil.copyfileobj(src, dst)

    dll = toolset / "Plugins" / "x86-unicode" / "additional" / "nsis_tauri_utils.dll"
    dll.parent.mkdir(parents=True, exist_ok=True)
    dll.write_bytes(dll_bytes)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="预置 Tauri 的 NSIS 工具链")
    parser.add_argument("--mirror", default=DEFAULT_MIRROR, help="GitHub 镜像前缀，传空串则直连")
    parser.add_argument("--check", action="store_true", help="只检查，不下载")
    parser.add_argument("--force", action="store_true", help="即使已就绪也重新铺开")
    parser.add_argument(
        "--local-dir",
        default=None,
        help="优先从这个目录取 nsis-3.13.zip 与 nsis_tauri_utils.dll（离线安装用，仍校验 SHA1）",
    )
    args = parser.parse_args(argv[1:])

    toolset = cache_root() / "NSIS"
    print(f"目标目录：{toolset}")

    problems = probe(toolset)
    if not problems and not args.force:
        print("✓ 已就绪，无需下载（tauri build 会跳过全部下载分支）")
        return 0

    if problems:
        print("当前状态不合格：")
        for problem in problems:
            print(f"  · {problem}")

    if args.check:
        print("（--check 模式，未做任何改动）")
        return 1 if problems else 0

    local = Path(args.local_dir) if args.local_dir else None

    zip_bytes = (load_local(local, NSIS_SHA1, "nsis-3.13.zip") if local else None) or fetch(
        NSIS_URL, args.mirror, NSIS_SHA1, "nsis-3.13.zip"
    )
    dll_bytes = (load_local(local, UTILS_SHA1, "nsis_tauri_utils.dll") if local else None) or fetch(
        UTILS_URL, args.mirror, UTILS_SHA1, "nsis_tauri_utils.dll"
    )

    install(toolset, zip_bytes, dll_bytes)

    remaining = probe(toolset)
    if remaining:
        print("✗ 铺开之后仍不合格：")
        for problem in remaining:
            print(f"  · {problem}")
        return 1

    total = sum(p.stat().st_size for p in toolset.rglob("*") if p.is_file())
    print(f"✓ 完成：{len(REQUIRED)} 项必需文件齐全，共 {total / 1024 / 1024:.1f} MB")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
