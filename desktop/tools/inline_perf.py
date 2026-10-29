#!/usr/bin/env python3
"""把 vite 产出的探针页内联成单文件。

为什么必须内联：`file://` 下 `<script type="module" src="...">` 会因 origin 为 null
被 CORS 拦掉，页面白屏。而跨机使用不能假设目标机器上有 Node 起 HTTP 服务，
也不能要求用户去装任何东西。单文件是唯一同时满足「能跑」和「零安装」的做法。

只做四件事：
  1. `<script src>` → 内联（转义 `</script`）
  2. `<link rel=stylesheet>` → 内联成 `<style>`
  3. `<link rel=modulepreload>` → 删除（已经内联，预加载无意义）
  4. 校验产物不含任何加载型外链
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

SCRIPT_SRC = re.compile(r'<script\b([^>]*?)\bsrc="([^"]+)"([^>]*)>\s*</script>', re.I)
LINK_TAG = re.compile(r'<link\b[^>]*>', re.I)
REL_ATTR = re.compile(r'\brel="([^"]*)"', re.I)
HREF_ATTR = re.compile(r'\bhref="([^"]+)"', re.I)


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def escape_script(text: str) -> str:
    # 内联脚本里出现 </script 会提前结束标签
    return re.sub(r'</script', r'<\\/script', text, flags=re.I)


def escape_style(text: str) -> str:
    return re.sub(r'</style', r'<\\/style', text, flags=re.I)


def inline(html_path: Path, out_path: Path) -> dict[str, object]:
    html = read_text(html_path)
    base = html_path.parent
    inlined: list[str] = []
    removed: list[str] = []

    def script_repl(match: re.Match[str]) -> str:
        src = match.group(2)
        asset = (base / src).resolve()
        if not asset.exists():
            raise SystemExit(f"内联失败：找不到 {asset}")
        code = escape_script(read_text(asset))
        inlined.append(asset.name)
        # 内联的 module 脚本没有 crossorigin 语义，去掉以免被当成跨源
        return f'<script type="module">\n{code}\n</script>'

    html = SCRIPT_SRC.sub(script_repl, html)

    def link_repl(match: re.Match[str]) -> str:
        tag = match.group(0)
        rel_match = REL_ATTR.search(tag)
        rel = (rel_match.group(1) if rel_match else "").lower()
        if rel == "modulepreload":
            removed.append(tag)
            return ""
        if rel != "stylesheet":
            return tag

        href_match = HREF_ATTR.search(tag)
        if href_match is None:
            raise SystemExit(f"内联失败：stylesheet 缺 href —— {tag}")
        asset = (base / href_match.group(1)).resolve()
        if not asset.exists():
            raise SystemExit(f"内联失败：找不到 {asset}")
        css = escape_style(read_text(asset))
        inlined.append(asset.name)
        return f"<style>\n{css}\n</style>"

    html = LINK_TAG.sub(link_repl, html)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(html, encoding="utf-8", newline="\n")

    return {
        "out": out_path,
        "bytes": out_path.stat().st_size,
        "inlined": inlined,
        "removed": len(removed),
    }


def find_external_refs(html: str) -> list[str]:
    """列出所有「还需要额外请求」的引用。空列表表示真正自包含。"""
    problems: list[str] = []

    for match in SCRIPT_SRC.finditer(html):
        problems.append(f"<script src={match.group(2)}>")

    for tag in LINK_TAG.findall(html):
        rel_match = REL_ATTR.search(tag)
        rel = (rel_match.group(1) if rel_match else "").lower()
        if rel in {"stylesheet", "modulepreload", "preload"}:
            href_match = HREF_ATTR.search(tag)
            problems.append(f"<link rel={rel} href={href_match.group(1) if href_match else '?'}>")

    if re.search(r'@import\s+(?:url\()?["\']?(?:https?:)?//', html, re.I):
        problems.append("@import 远程样式")

    if re.search(r'url\(\s*["\']?https?://', html, re.I):
        problems.append("url() 远程资源")

    return problems


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="探针页内联为单文件")
    parser.add_argument("--in", dest="src", default="desktop/dist-perf/perf.html")
    parser.add_argument("--out", dest="dst", default="desktop/dist/perf-standalone.html")
    args = parser.parse_args(argv[1:])

    root = Path(__file__).resolve().parents[2]
    src = (root / args.src) if not Path(args.src).is_absolute() else Path(args.src)
    dst = (root / args.dst) if not Path(args.dst).is_absolute() else Path(args.dst)

    if not src.exists():
        print(f"找不到输入 {src}，先跑 npm run build:perf")
        return 2

    info = inline(src, dst)
    html = read_text(dst)
    problems = find_external_refs(html)

    print(f"内联 {info['inlined']} → {dst}")
    print(f"体积 {info['bytes']} 字节，移除 modulepreload {info['removed']} 个")

    if problems:
        print("产物仍有外链，单文件化失败：")
        for problem in problems:
            print(f"  - {problem}")
        return 3

    print("自包含校验通过：无加载型外链")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
