#!/usr/bin/env python3
"""组装跨机测试包。

包是「静态说明 + 一个构建产物」的混合目录：RUN.md 与 collect-env.ps1 手写并入库，
perf.html 每次从构建产物复制（不入库，避免每次构建都产生 diff 噪声）。

复制时算 sha256 并写进清单：AC-08 靠它判定包里的页面确实是本次构建的产物，
而不是上次遗留的旧版本 —— 拿到旧页面测出来的数据会误导人。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from inline_perf import find_external_refs  # noqa: E402

REQUIRED = ("RUN.md", "collect-env.ps1")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main(argv: list[str]) -> int:
    root = Path(__file__).resolve().parents[2]
    parser = argparse.ArgumentParser(description="组装跨机测试包")
    parser.add_argument("--src", default="desktop/dist-perf/perf-standalone.html")
    parser.add_argument("--out", default="crossmachine")
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

    files = []
    for name in (*REQUIRED, "perf.html"):
        path = out_dir / name
        files.append(
            {
                "name": name,
                "bytes": path.stat().st_size,
                "sha256": sha256(path),
            }
        )

    manifest = {
        "schema": "crossmachine-pack-v1",
        "source": src.relative_to(root).as_posix(),
        "sourceSha256": sha256(src),
        "files": files,
    }
    (out_dir / "MANIFEST.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )

    print(f"已复制 {src.relative_to(root).as_posix()} → {target.relative_to(root).as_posix()}")
    for entry in files:
        print(f"  {entry['name']:20s} {entry['bytes']:>8d} 字节  {entry['sha256'][:16]}")
    print(f"清单 → {(out_dir / 'MANIFEST.json').relative_to(root).as_posix()}")
    print("打包完成：把整个 crossmachine 目录拷到目标机器即可")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
