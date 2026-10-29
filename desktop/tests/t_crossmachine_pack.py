#!/usr/bin/env python3
"""AC-08：跨机测试包完整性。

核心是那条字节一致性断言：包里必须是**本次构建**的产物。
拿到上次遗留的旧页面，测出来的数据会指向一个已经不存在的代码版本，
而这种错误在数据本身上看不出来 —— 数字依然漂亮，只是没意义。
"""

from __future__ import annotations

import hashlib
import json
import sys
from html.parser import HTMLParser
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
from inline_perf import find_external_refs  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
DIST_PAGE = ROOT / "desktop" / "dist" / "perf-standalone.html"
PACK = ROOT / "crossmachine"
MANIFEST_NAME = "MANIFEST.json"
REQUIRED = ("RUN.md", "collect-env.ps1", "perf.html", MANIFEST_NAME)


class TagCounter(HTMLParser):
    """用真正的 HTML 解析器数标签。

    不能用 `html.count("<script")`：打包后的 JS 代码里本身就可能出现
    `<script` 字面量（React 源码里有），字符串计数会把它们算进去，
    得出「标签不配对」的假警报。解析器在 script 内部走 CDATA 模式，
    不会误判。
    """

    def __init__(self) -> None:
        super().__init__()
        self.scripts = 0
        self.links = 0
        self.parsed_fully = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "script":
            self.scripts += 1
        elif tag == "link":
            self.links += 1

    def error(self, message: str) -> None:  # pragma: no cover - 仅用于兼容旧版
        raise AssertionError(message)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> int:
    problems: list[str] = []

    if not DIST_PAGE.exists():
        print(f"缺少构建产物 {DIST_PAGE}")
        print("先跑：python toolchain/build.py desktop --perf")
        return 4

    if not PACK.is_dir():
        print(f"缺少测试包目录 {PACK}")
        print("先跑：python toolchain/build.py crossmachine")
        return 4

    for name in REQUIRED:
        path = PACK / name
        if not path.is_file():
            problems.append(f"缺少 {name}")
        elif path.stat().st_size == 0:
            problems.append(f"{name} 是空文件")

    if problems:
        for problem in problems:
            print(f"  ✗ {problem}")
        return 1

    print("① 文件齐全")
    for name in REQUIRED:
        path = PACK / name
        print(f"  ✓ {name:20s} {path.stat().st_size:>8d} 字节")

    # ② 字节一致：包里必须是本次构建的产物
    pack_page = PACK / "perf.html"
    dist_hash = sha256(DIST_PAGE)
    pack_hash = sha256(pack_page)
    if dist_hash != pack_hash:
        problems.append(
            f"包内页面与构建产物不一致（包 {pack_hash[:16]} ≠ 产物 {dist_hash[:16]}），"
            "说明包是旧的，重新跑 toolchain/build.py crossmachine"
        )
        print("② 字节一致 ✗")
    else:
        print(f"② 字节一致 ✓ sha256={dist_hash[:16]}…")

    # ③ 清单与实际文件对应。清单不列自己 —— 写进去就要求它包含自己的哈希，
    # 那是不可能满足的自指。
    manifest = json.loads((PACK / MANIFEST_NAME).read_text(encoding="utf-8"))
    if manifest.get("sourceSha256") != dist_hash:
        problems.append("MANIFEST.sourceSha256 与构建产物不一致")
    for entry in manifest.get("files", []):
        actual = sha256(PACK / entry["name"])
        if actual != entry["sha256"]:
            problems.append(f"MANIFEST 里 {entry['name']} 的 sha256 与实际文件不符")
    declared = {entry["name"] for entry in manifest.get("files", [])}
    expected = set(REQUIRED) - {MANIFEST_NAME}
    if declared != expected:
        problems.append(f"MANIFEST 声明的文件集不对：{sorted(declared)}，期望 {sorted(expected)}")
    print(f"③ 清单校验（{len(manifest.get('files', []))} 项）")

    # ④ 页面真自包含
    html = pack_page.read_text(encoding="utf-8")
    external = find_external_refs(html)
    if external:
        problems.append(f"页面仍有外链：{external}")
    print(f"④ 自包含 {'✓' if not external else '✗'}（{len(html)} 字符）")

    # ⑤ 用解析器验证内联没把结构搞坏
    counter = TagCounter()
    counter.feed(html)
    counter.close()
    if counter.scripts != 1:
        problems.append(f"期望恰好 1 个内联 script，实际 {counter.scripts} 个（转义可能失效）")
    if counter.links != 0:
        problems.append(f"仍有 {counter.links} 个 <link> 标签未内联")
    if html.count("</script>") != counter.scripts:
        problems.append("</script> 结束标签数量与 script 元素数量不符，页面可能被截断")
    print(f"⑤ 结构解析 script={counter.scripts} link={counter.links}")

    # ⑥ 页面带得出结果所需的静态骨架。`data-perf-ready` 是运行时才写的，
    # 静态文件里查不到 —— 检查静态 id 才是对的判据。
    for needle, label in [
        ("开始测量", "启动按钮文案"),
        ('id="perf-stage-mount"', "列表挂载点"),
        ('id="perf-log"', "日志区"),
        ('id="perf-report"', "报告回写区（headless 抓取用）"),
        ('id="perf-save"', "保存按钮"),
    ]:
        ok = needle in html
        if not ok:
            problems.append(f"页面缺少{label}（找不到 {needle!r}）")
        print(f"⑥ {label} {'✓' if ok else '✗'}")

    # ⑦ 编码。ps1 额外要求全 ASCII：PowerShell 5.1 把无 BOM 的 UTF-8 当 ANSI 解码，
    # 中文注释的字节被误读后可能吞掉换行、把下一行语句并进注释里。这个失败是静默的
    # ——脚本照常运行，只是某些赋值凭空消失。踩过一次，用断言锁住。
    bom_found = False
    for name in ("RUN.md", "collect-env.ps1", MANIFEST_NAME):
        raw = (PACK / name).read_bytes()
        if raw.startswith(b"\xef\xbb\xbf"):
            problems.append(f"{name} 带 UTF-8 BOM")
            bom_found = True
        try:
            raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            problems.append(f"{name} 不是合法 UTF-8：{exc}")
            bom_found = True
    print(f"⑦ 编码 {'✗' if bom_found else '✓'}（UTF-8 无 BOM）")

    ps1 = (PACK / "collect-env.ps1").read_text(encoding="utf-8")
    non_ascii = [(i + 1, line) for i, line in enumerate(ps1.splitlines()) if not line.isascii()]
    if non_ascii:
        where = "；".join(f"第 {ln} 行 {txt.strip()[:40]!r}" for ln, txt in non_ascii[:3])
        problems.append(
            f"collect-env.ps1 含非 ASCII 字符（{where}）。PowerShell 5.1 会按 ANSI 解码"
            "无 BOM 的 UTF-8，可能静默吞掉语句"
        )
        print(f"⑦b ps1 ASCII ✗（{len(non_ascii)} 行非 ASCII）")
    else:
        print("⑦b ps1 ASCII ✓（PowerShell 5.1 下编码安全）")

    # ⑧ 说明文档覆盖了用户必须做的动作
    run_md = (PACK / "RUN.md").read_text(encoding="utf-8")
    doc_issues = 0
    for needle, label in [
        ("双击", "双击打开"),
        ("开始测量", "点开始"),
        ("保存结果", "保存结果"),
        ("远程桌面", "远程会话会让数据失真的提醒"),
        ("env-limited", "环境受限的说明"),
    ]:
        if needle not in run_md:
            problems.append(f"RUN.md 没有说明「{label}」")
            doc_issues += 1
    print(f"⑧ 操作说明覆盖度 {'✓' if doc_issues == 0 else f'缺 {doc_issues} 项'}")

    print()
    if problems:
        for problem in problems:
            print(f"  ✗ {problem}")
        print(f"失败 {len(problems)} 项")
        return 1

    print("通过：包内文件齐全、与构建产物字节一致、页面自包含、说明可照做")
    return 0


if __name__ == "__main__":
    sys.exit(main())
