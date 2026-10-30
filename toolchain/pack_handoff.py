#!/usr/bin/env python3
"""组装「真机测试执行包」：手写文档 + 前置检查脚本 + 安装包 + 帧率测试包。

为什么要一个脚本而不是手工复制：这个包里有四个东西来自四个不同地方
（仓库文档、构建产物、工具链的 Redist 目录、另一个包的 zip），
手工拷一次能对，拷第二次一定漏一样。而且漏了看不出来 ——
压缩包里少一个 DLL，要到目标机上装完才发现。

两件事同时做：

1. **目录形态**（`handoff/dist/field-test-pack-v1/`）—— 拷进 U 盘前可以直接翻。
2. **压缩包形态**（`handoff/dist/field-test-pack-v1.zip`）—— 过网盘/邮件时只要传一个文件。

压缩包可复现：条目顺序、时间戳、`create_system` 全固定，同一份输入重打哈希一致。
条目名全部 ASCII —— 中文名在 Windows 资源管理器解压时按 OEM 代码页解释，会变乱码。
文档内容可以是中文，**路径不能**。

用法：
    python toolchain/pack_handoff.py
"""

from __future__ import annotations

import hashlib
import json
import shutil
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

PACK_NAME = "field-test-pack-v1"
OUT_DIR = ROOT / "handoff" / "dist"

# 手写件：仓库里已经有，原样进包
DOC_SOURCES = {
    "README.md": ROOT / "handoff" / "README.md",
    "CHECKLIST.md": ROOT / "handoff" / "CHECKLIST.md",
    "preflight.ps1": ROOT / "handoff" / "preflight.ps1",
}

# 帧率包：由 toolchain/build.py crossmachine 产出（可复现 zip + 旁附 sha256）
PERF_ZIP = ROOT / "crossmachine" / "dist" / "crossmachine-pack-v1.zip"

# 安装包：tauri build 的 NSIS 产物。源名含中文与空格，进包时改成 ASCII 名，
# 避免目标机上解压/传输环节把它改坏。
INSTALLER_GLOB = "desktop/src-tauri/target/release/bundle/nsis/*setup.exe"
TAURI_CONF = ROOT / "desktop" / "src-tauri" / "tauri.conf.json"

# 备用 VC++ 运行库：只在 preflight 报 VCRUNTIME 缺失时才需要。
# 来源是构建工具链的 Redist 目录（MS 允许 app-local 部署这三个文件）。
VCRUNTIME_SRC = Path(r"C:\BuildTools\VC\Redist\MSVC\14.44.35112\x64\Microsoft.VC143.CRT")
VCRUNTIME_FILES = ("msvcp140.dll", "vcruntime140.dll", "vcruntime140_1.dll")

ZIP_EPOCH = (1980, 1, 1, 0, 0, 0)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def human(size: int) -> str:
    if size < 1024:
        return f"{size} B"
    if size < 1024 * 1024:
        return f"{size / 1024:.1f} KB"
    return f"{size / 1024 / 1024:.2f} MB"


def collect() -> dict[str, Path]:
    """决定「包内相对路径 → 源文件」。返回的字典顺序就是打进 zip 的顺序。"""
    items: dict[str, Path] = {}

    for name, source in DOC_SOURCES.items():
        if not source.exists():
            raise SystemExit(f"缺少手写件：{source}")
        items[name] = source

    if not PERF_ZIP.exists():
        raise SystemExit(
            f"缺少帧率包：{PERF_ZIP}\n先跑：python toolchain/build.py desktop --perf"
            "\n再跑：python toolchain/build.py crossmachine"
        )
    items["01-perf/crossmachine-pack-v1.zip"] = PERF_ZIP
    sidecar = PERF_ZIP.with_name(PERF_ZIP.name + ".sha256")
    if not sidecar.exists():
        raise SystemExit(f"缺少帧率包的校验文件：{sidecar}")
    items["01-perf/crossmachine-pack-v1.zip.sha256"] = sidecar

    version = json.loads(TAURI_CONF.read_text(encoding="utf-8"))["version"]
    installers = sorted(ROOT.glob(INSTALLER_GLOB))
    if not installers:
        raise SystemExit(
            "缺少安装包。先跑：\n  python toolchain/build.py desktop --build\n"
            "  python toolchain/build.py shell bundle"
        )
    installer = installers[-1]
    if version not in installer.name:
        raise SystemExit(
            f"安装包版本对不上：tauri.conf.json 是 {version}，产物是 {installer.name}\n"
            "先重新打包，别把旧安装包发出去"
        )
    items[f"02-installer/BT-client_{version}_x64-setup.exe"] = installer

    missing = [name for name in VCRUNTIME_FILES if not (VCRUNTIME_SRC / name).exists()]
    if missing:
        raise SystemExit(f"备用运行库缺失：{'、'.join(missing)}（源目录 {VCRUNTIME_SRC}）")
    for name in VCRUNTIME_FILES:
        items[f"02-installer/plan-b-vcruntime/{name}"] = VCRUNTIME_SRC / name

    return items


def clear_dir(path: Path) -> None:
    """清空本脚本自己的产物目录：先删文件、再删空目录。

    刻意不用 shutil.rmtree：递归删整棵树这种动作一旦路径算错，代价是别人的整个目录。
    这里只删自己刚才生成的层级，删不掉就报错，不硬来。
    """
    for entry in sorted(path.rglob("*"), key=lambda p: len(p.parts), reverse=True):
        if entry.is_file() or entry.is_symlink():
            entry.unlink()
        elif entry.is_dir():
            entry.rmdir()


def write_pack(items: dict[str, Path]) -> Path:
    pack_dir = OUT_DIR / PACK_NAME
    if pack_dir.exists():
        clear_dir(pack_dir)
    pack_dir.mkdir(parents=True, exist_ok=True)

    for rel, source in items.items():
        target = pack_dir / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)

    # SHA256SUMS 放在包根、不列自己（自指哈希不可能成立）。
    # 目标机拿到后可以逐个核对，也可以在传输怀疑损坏时定位到底是哪个文件。
    lines = [f"{sha256(pack_dir / rel)}  {rel}" for rel in items]
    (pack_dir / "SHA256SUMS.txt").write_text(
        "\n".join(lines) + "\n", encoding="utf-8", newline="\n"
    )
    return pack_dir


def build_archive(pack_dir: Path, target: Path) -> None:
    staging = target.with_suffix(".zip.part")
    names = sorted(
        [p.relative_to(pack_dir).as_posix() for p in pack_dir.rglob("*") if p.is_file()]
    )
    with zipfile.ZipFile(staging, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for name in names:
            info = zipfile.ZipInfo(f"{PACK_NAME}/{name}", date_time=ZIP_EPOCH)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.create_system = 0
            info.external_attr = 0o644 << 16
            archive.writestr(info, (pack_dir / name).read_bytes())
    staging.replace(target)


def main() -> int:
    items = collect()
    pack_dir = write_pack(items)

    print(f"目录 → {pack_dir.relative_to(ROOT).as_posix()}")
    print()
    print(f"  {'包内路径':46s} {'大小':>10s}  sha256")
    for rel in items:
        path = pack_dir / rel
        print(f"  {rel:46s} {human(path.stat().st_size):>10s}  {sha256(path)[:16]}")
    print(f"  {'SHA256SUMS.txt':46s} {human((pack_dir / 'SHA256SUMS.txt').stat().st_size):>10s}")
    print()

    archive = OUT_DIR / f"{PACK_NAME}.zip"
    build_archive(pack_dir, archive)
    digest = sha256(archive)
    (archive.parent / f"{archive.name}.sha256").write_text(
        f"{digest}  {archive.name}\n", encoding="utf-8", newline="\n"
    )

    print(f"压缩包 → {archive.relative_to(ROOT).as_posix()}")
    print(f"         {human(archive.stat().st_size)}  sha256 {digest}")
    print()
    print("目标机上先跑：powershell -NoProfile -ExecutionPolicy Bypass -File .\\preflight.ps1")
    return 0


if __name__ == "__main__":
    sys.exit(main())
