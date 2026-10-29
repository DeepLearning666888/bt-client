#!/usr/bin/env python3
"""组装跨机测试包，并压成单个可拷走的 zip。

包是「静态说明 + 一个构建产物」的混合目录：RUN.md 与 collect-env.ps1 手写并入库，
perf.html 每次从构建产物复制（不入库，避免每次构建都产生 diff 噪声）。

两件事同时做：

1. **目录形态**（`crossmachine/`）—— 在本机直接打开调试用。
2. **压缩包形态**（`crossmachine/dist/*.zip`）—— 换电脑时唯一需要拷贝的那一个文件。
   解压后是 `crossmachine-pack-v1/` 一个文件夹，不会散落到桌面。

压缩包做成**可复现**的：固定时间戳、固定顺序、固定 create_system。
同样的输入打两次，sha256 必须一致 —— 否则「这个包是不是那次构建的」就无法靠哈希回答，
而 sha256 的全部价值就在于此。`t_crossmachine_pack.py` 会连打两次来验这件事。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from inline_perf import find_external_refs  # noqa: E402

SCHEMA = "crossmachine-pack-v1"
ARCHIVE_STEM = "crossmachine-pack-v1"
ARCHIVE_ROOT = "crossmachine-pack-v1"

# 包内文件的语义：用途 + 目标机是否需要安装。
# 这一列不是装饰：用户问的是「换电脑要装什么」，而这项的答案是「什么都不用装」。
# 结论必须写在清单里，否则拿到包的人还得自己判断一遍。
META: dict[str, tuple[str, bool]] = {
    "RUN.md": ("操作说明：怎么跑、看什么、怎么避免环境干扰", False),
    "collect-env.ps1": ("采集机器环境（只读，结果写到 out 目录）", False),
    "perf.html": ("测试页面，单文件自包含，双击即开", False),
}
# 手写文件，打包前必须已在目录里；perf.html 由构建产出，不在此列
REQUIRED = ("RUN.md", "collect-env.ps1")
# 进包的完整顺序，固定下来清单才可复现
ORDER = (*REQUIRED, "perf.html")
MANIFEST_NAME = "MANIFEST.json"
# 压缩包条目 = 上面三项 + 清单自身。清单必须在包里，否则收到包的人无从核对。
# 清单的 files 数组不列自己（自指 sha256 不可能成立），所以另用 archiveEntries 显式声明全集。
ARCHIVE_ENTRIES = (*ORDER, MANIFEST_NAME)

# 目标机前提。installRequired 为空是实测结论，不是推测：
# 页面是纯静态 HTML（无外链、无模块解析），脚本是 PowerShell 5.1 语法（Windows 10/11 自带）。
PREREQUISITES = [
    {
        "item": "Chrome 或 Edge",
        "installRequired": False,
        "note": "Windows 10/11 自带 Edge；装了 Chrome 更好，两者取其一即可",
        "check": "双击 perf.html 时选浏览器",
    },
    {
        "item": "PowerShell 5.1+",
        "installRequired": False,
        "note": "Windows 10/11 自带，仅在需要采集机器环境时才用到",
        "check": "可选步骤，不装也能跑帧率测试",
    },
]

# zip 时间戳下限。1980-01-01 是 ZIP 格式能表示的最早时间，也是可复现打包的通行做法。
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


def build_archive(out_dir: Path, target: Path, names: tuple[str, ...]) -> None:
    """写可复现 zip：条目顺序、时间戳、权限位全部固定。

    用临时文件再改名，避免中途失败留下一个「能打开但内容不全」的包 ——
    这种半成品最危险，它能通过「文件存在」的检查。
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    staging = target.with_suffix(".zip.part")
    with zipfile.ZipFile(staging, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for name in names:
            info = zipfile.ZipInfo(f"{ARCHIVE_ROOT}/{name}", date_time=ZIP_EPOCH)
            info.compress_type = zipfile.ZIP_DEFLATED
            # create_system 固定为 0（MS-DOS）：不同平台默认值不同，会把机器差异写进包里
            info.create_system = 0
            info.external_attr = 0o644 << 16
            archive.writestr(info, (out_dir / name).read_bytes())
    staging.replace(target)


def main(argv: list[str]) -> int:
    root = Path(__file__).resolve().parents[2]
    parser = argparse.ArgumentParser(description="组装跨机测试包并压成 zip")
    parser.add_argument("--src", default="desktop/dist-perf/perf-standalone.html")
    parser.add_argument("--out", default="crossmachine")
    parser.add_argument("--no-zip", action="store_true", help="只组装目录，不压包")
    args = parser.parse_args(argv[1:])

    src = root / args.src
    out_dir = root / args.out

    if not src.exists():
        print(f"找不到 {src}")
        print("先跑：python toolchain/build.py desktop --perf")
        return 2

    missing = [name for name in REQUIRED if not (out_dir / name).exists()]
    if missing:
        print("测试包缺少必需文件：" + "、".join(missing))
        return 2

    problems = find_external_refs(src.read_text(encoding="utf-8"))
    if problems:
        print("构建产物仍有外链，拒绝打包：")
        for problem in problems:
            print(f"  - {problem}")
        return 3

    target = out_dir / "perf.html"
    shutil.copyfile(src, target)

    # 顺序固定：MANIFEST 里的 files 数组顺序必须稳定，否则清单本身不可复现
    order = ORDER
    files = []
    for name in order:
        path = out_dir / name
        purpose, install_required = META.get(name, ("构建产物：测试页面本体", False))
        files.append(
            {
                "name": name,
                "bytes": path.stat().st_size,
                "sha256": sha256(path),
                "purpose": purpose,
                "installRequired": install_required,
            }
        )

    archive_path = out_dir / "dist" / f"{ARCHIVE_STEM}.zip"
    manifest = {
        "schema": SCHEMA,
        "source": src.relative_to(root).as_posix(),
        "sourceSha256": sha256(src),
        "archive": None if args.no_zip else archive_path.relative_to(root).as_posix(),
        "archiveRoot": ARCHIVE_ROOT,
        "archiveEntries": None if args.no_zip else list(ARCHIVE_ENTRIES),
        "target": {
            "os": "Windows 10 / 11 x64",
            "installRequired": [item["item"] for item in PREREQUISITES if item["installRequired"]],
            "prerequisites": PREREQUISITES,
        },
        "files": files,
    }
    (out_dir / MANIFEST_NAME).write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )

    print(f"已复制 {src.relative_to(root).as_posix()} → {target.relative_to(root).as_posix()}")
    print()
    print("目标机：Windows 10 / 11 x64")
    need = manifest["target"]["installRequired"]
    print(f"需要安装：{'、'.join(need) if need else '无（解压即用）'}")
    print()
    print(f"包内文件（{len(files)}）：")
    print(f"  {'文件':22s} {'大小':>9s}  {'sha256':18s} {'安装':6s} 用途")
    for entry in files:
        install = "需安装" if entry["installRequired"] else "免安装"
        print(
            f"  {entry['name']:22s} {human(entry['bytes']):>9s}  "
            f"{entry['sha256'][:16]}  {install:6s} {entry['purpose']}"
        )
    print()
    print(f"清单 → {(out_dir / MANIFEST_NAME).relative_to(root).as_posix()}")

    if args.no_zip:
        print("已跳过压缩（--no-zip）：把整个 crossmachine 目录拷到目标机器即可")
        return 0

    build_archive(out_dir, archive_path, ARCHIVE_ENTRIES)
    digest = sha256(archive_path)
    # 校验文件放在 zip 外面：放进包里就是自指，哈希永远对不上自己
    (archive_path.parent / f"{archive_path.name}.sha256").write_text(
        f"{digest}  {archive_path.name}\n", encoding="utf-8", newline="\n"
    )

    print()
    print(f"压缩包 → {archive_path.relative_to(root).as_posix()}")
    print(f"        {human(archive_path.stat().st_size)}  sha256 {digest}")
    print(f"解压后 → {ARCHIVE_ROOT}/（{len(ARCHIVE_ENTRIES)} 个文件，同一层，无子目录）")
    print()
    print("换电脑只需要拷这一个 zip。到目标机解压，双击 perf.html 即可，无需安装任何东西。")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
