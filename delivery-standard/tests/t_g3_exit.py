#!/usr/bin/env python3
"""G3 退出码核验的端到端验证。

**为什么有这个文件。** 2026-11-03 之前，`gate.py g3` 只查「证据文件存在 + 编码合规」，
不读日志首行记录的退出码。后果是 `gen` 打出 `[FAIL] AC-01 (exit=1)` 之后，`all`
照样报 `G3 PASS` —— 一条失败的证据和一条通过的证据，对判定器来说长得完全一样，
绿灯也就不构成证据。config 里的 `expect` 同理：它只影响 `gen` 的打印，没有任何
一处读它来判定，是个装饰品。

本文件用临时目录构造样本，逐条验证判定器对退出码的处理。全部走 import 调用，
不依赖项目里的真实证据，因此可以在任何机器上随时重跑。

用法:
    python delivery-standard/tests/t_g3_exit.py

退出码:
    0  全部通过
    1  有用例不符合预期
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

TOOLS = Path(__file__).resolve().parent.parent / "tools"
sys.path.insert(0, str(TOOLS))

import gate  # noqa: E402

SPEC = """# demo

## 目标
用于验证 G3 退出码判定的合成单元

## 验收标准
- [AC-01] 合成用例 | 判: 合成命令退出码 0

## 不做范围
无

## 未验证
无
"""

HEAD = "# 2026-11-03 12:00:00  $ demo command  (exit={code})\n输出正文\n"


def run_g3(log_text: str | None, expects: dict[str, int] | None) -> tuple[str, str]:
    """在临时目录里造一份 spec 与证据，返回 (状态, 详情)。"""
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        spec_path = root / "demo.md"
        spec_path.write_text(SPEC, encoding="utf-8")

        ev_root = root / "ev"
        unit_dir = ev_root / "demo"
        unit_dir.mkdir(parents=True)
        if log_text is not None:
            (unit_dir / "AC-01.log").write_text(log_text, encoding="utf-8")

        result = gate.gate_g3(gate.parse_spec(spec_path), "demo", ev_root, expects)
        return result.status, result.detail


CASES: list[tuple[str, tuple[str, str], str]] = [
    (
        "退出码 0 且期望 0 → 通过",
        run_g3(HEAD.format(code=0), {"AC-01": 0}),
        "pass",
    ),
    (
        "退出码 1 但期望 0 → 失败",
        run_g3(HEAD.format(code=1), {"AC-01": 0}),
        "fail",
    ),
    (
        "退出码 1 且期望 1 → 通过（负向用例）",
        run_g3(HEAD.format(code=1), {"AC-01": 1}),
        "pass",
    ),
    (
        "退出码 0 但期望 1 → 失败（负向用例失去力度）",
        run_g3(HEAD.format(code=0), {"AC-01": 1}),
        "fail",
    ),
    (
        "退出码 2 且未声明 expect → 失败（默认按 0）",
        run_g3(HEAD.format(code=2), None),
        "fail",
    ),
    (
        "无退出码但 config 声明了命令 → 失败（无法核验）",
        run_g3("ok\n", {"AC-01": 0}),
        "fail",
    ),
    (
        "无退出码且未声明命令 → 通过（手工证据，只校验格式）",
        run_g3("ok\n", None),
        "pass",
    ),
    (
        "证据文件缺失 → 失败",
        run_g3(None, {"AC-01": 0}),
        "fail",
    ),
    (
        "正文里出现 exit= 不影响首行判定 → 通过",
        run_g3(HEAD.format(code=0) + "历史记录：上一次 (exit=1)\n", {"AC-01": 0}),
        "pass",
    ),
    (
        "首行无退出码、正文里贴了别人的日志 → 仍按手工证据，通过",
        run_g3("人工观察记录\n附录：另一条命令 (exit=1)\n", None),
        "pass",
    ),
]

# 详情里必须出现具体数字，否则「失败」本身也说不清哪里失败
DETAIL_MUST_MENTION = {
    "退出码 1 但期望 0 → 失败": ["AC-01", "exit=0", "exit=1"],
    "退出码 0 但期望 1 → 失败（负向用例失去力度）": ["AC-01", "exit=1", "exit=0"],
    "无退出码但 config 声明了命令 → 失败（无法核验）": ["AC-01", "无退出码"],
}


def main() -> int:
    failures = 0
    for name, (status, detail), want in CASES:
        ok = status == want
        if ok:
            for token in DETAIL_MUST_MENTION.get(name, []):
                if token not in detail:
                    ok = False
                    detail = f"详情里缺 {token!r}：{detail}"
        print(f"[{'PASS' if ok else 'FAIL'}] {name}  → {status}")
        if not ok:
            print(f"       期望 {want}；实际详情：{detail}")
            failures += 1

    total = len(CASES)
    print(f"\n{total - failures}/{total} 项通过")
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
