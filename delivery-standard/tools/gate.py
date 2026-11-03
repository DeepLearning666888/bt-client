#!/usr/bin/env python3
"""单人项目交付规范 —— 卡点判定器

对应 standard.md v1.0。四个卡点全部由本脚本判定，人不给自己签字（依据 3.2）。

用法:
    gate.py g1|g2|g3|g4|all --unit <name> [--json]
    gate.py gen --unit <name>
    gate.py doctor
    gate.py selftest

退出码:
    0  全部通过
    1  有卡点不通过
    2  用法或配置错误
"""

from __future__ import annotations

import argparse
import json
import re
import shlex
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

EXIT_OK, EXIT_FAIL, EXIT_USAGE = 0, 1, 2

HERE = Path(__file__).resolve().parent
STANDARD_DIR = HERE.parent
PROJECT_ROOT = STANDARD_DIR.parent
CONFIG_PATH = STANDARD_DIR / "config.toml"

TIER_GATES = {
    "T0": ("g2",),
    "T1": ("g2", "g3"),
    "T2": ("g1", "g2", "g3", "g4"),
}

GATE_TITLES = {
    "g1": "G1 验收标准已声明",
    "g2": "G2 构建通过",
    "g3": "G3 每条 AC 有证据",
    "g4": "G4 未验证项已声明",
}

SECTION_ALIASES = {
    "goal": ("目标", "目的", "背景与目标"),
    "ac": ("验收标准", "验收条件"),
    "out_of_scope": ("不做范围", "不做的事", "排除范围", "不做什么"),
    "unverified": ("未验证", "未验证项", "已知限制", "已知弱点"),
}

HEADING_RE = re.compile(r"^##\s+(?P<title>.+?)\s*$")
NUM_PREFIX_RE = re.compile(r"^[\s\d一二三四五六七八九十]+[、.．]\s*")
AC_LINE_RE = re.compile(
    r"^\s*[-*]\s*\[(?P<id>AC-\d+)\]\s*(?P<desc>.+?)\s*[|｜]\s*判\s*[:：]\s*(?P<how>.+?)\s*$"
)
AC_LIKE_RE = re.compile(r"^\s*[-*]\s*\[(?P<id>AC-\d+)\](?P<rest>.*)$")

# 占位符必须被拦住，否则从模板抄来不填也能过 G1——那 G1 就退化成形式主义
PLACEHOLDER_RE = re.compile(r"待填写|TODO|TBD", re.IGNORECASE)


# ---------------------------------------------------------------- 数据结构


@dataclass
class Criterion:
    """一条验收标准（AC）。"""

    cid: str
    desc: str
    how: str


@dataclass
class Spec:
    """解析后的 spec，缺失的节保持为 None 以便区分「没写」和「写了空」。"""

    path: Path
    sections: dict[str, str | None] = field(default_factory=dict)
    criteria: list[Criterion] = field(default_factory=list)
    malformed: list[str] = field(default_factory=list)
    duplicate_ids: list[str] = field(default_factory=list)


@dataclass
class GateResult:
    gate_id: str
    status: str  # pass / fail / skip
    detail: str
    evidence: str = ""


# ---------------------------------------------------------------- 配置


def load_config() -> dict:
    """读取 config.toml。缺失即报错退出，不提供默认值兜底。"""
    if not CONFIG_PATH.is_file():
        die(f"找不到配置：{CONFIG_PATH}")

    try:
        import tomllib
    except ModuleNotFoundError:
        die("需要 Python 3.11+ 以获得 tomllib")

    with CONFIG_PATH.open("rb") as fh:
        try:
            return tomllib.load(fh)
        except tomllib.TOMLDecodeError as exc:
            die(f"配置解析失败 {CONFIG_PATH}：{exc}")


def unit_config(cfg: dict, unit: str) -> dict:
    units = cfg.get("units") or {}
    if unit not in units:
        known = "、".join(sorted(units)) or "（无）"
        die(f"配置里没有单元 {unit!r}。已声明：{known}")
    node = units[unit]
    tier = node.get("tier")
    if tier not in TIER_GATES:
        die(f"单元 {unit} 的 tier={tier!r} 非法，只能是 {'/'.join(TIER_GATES)}")
    return node


def paths(cfg: dict) -> tuple[Path, Path]:
    root = cfg.get("paths") or {}
    spec_dir = PROJECT_ROOT / root.get("spec_dir", "specs")
    evidence_dir = PROJECT_ROOT / root.get("evidence_dir", "evidence")
    return spec_dir, evidence_dir


# ---------------------------------------------------------------- spec 解析


def _normalize_heading(title: str) -> str:
    return NUM_PREFIX_RE.sub("", title).strip()


def _match_section(title: str) -> str | None:
    norm = _normalize_heading(title)
    for key, aliases in SECTION_ALIASES.items():
        for alias in aliases:
            if alias in norm:
                return key
    return None


def parse_spec(path: Path) -> Spec:
    """按 standard.md 附录 A 的格式解析单元 spec。

    刻意保持极简：只认二级标题 + `- [AC-nn] 描述 | 判: 方式`。
    格式歪掉的行进 malformed 并单独报错，不静默丢弃——静默丢弃会让
    「写错格式」看起来像「写对了但内容空」。
    """
    spec = Spec(path=path)
    if not path.is_file():
        return spec

    text = path.read_text(encoding="utf-8-sig", errors="replace")
    current: str | None = None
    buffers: dict[str, list[str]] = {}

    for raw in text.splitlines():
        heading = HEADING_RE.match(raw)
        if heading:
            current = _match_section(heading.group("title"))
            if current and current not in buffers:
                buffers[current] = []
            continue
        if raw.startswith("# "):
            current = None
            continue
        if current:
            buffers[current].append(raw)

    for key in SECTION_ALIASES:
        spec.sections[key] = "\n".join(buffers[key]) if key in buffers else None

    ac_text = spec.sections.get("ac") or ""
    seen: set[str] = set()
    for line in ac_text.splitlines():
        good = AC_LINE_RE.match(line)
        if good:
            cid = good.group("id")
            if cid in seen:
                spec.duplicate_ids.append(cid)
            seen.add(cid)
            spec.criteria.append(
                Criterion(cid=cid, desc=good.group("desc").strip(), how=good.group("how").strip())
            )
            continue
        looks_like_ac = AC_LIKE_RE.match(line)
        if looks_like_ac:
            spec.malformed.append(line.strip())

    return spec


# ---------------------------------------------------------------- 证据检查


def check_evidence_file(path: Path) -> tuple[bool, str]:
    """校验证据文件是否可被 grep/diff 正常消费（standard.md 6.2）。

    PowerShell 默认写 UTF-16LE，这类文件在 UTF-8 工具链里表现为「搜不到、
    不报错」，历史上已导致两次静默失败。此处直接拒绝。
    """
    if not path.is_file():
        return False, "文件不存在"
    if path.stat().st_size == 0:
        return False, "文件为空"

    head = path.read_bytes()[:8192]
    if head.startswith(b"\xef\xbb\xbf"):
        return False, "带 UTF-8 BOM，grep 首行匹配会失效，请转无 BOM"
    if b"\x00" in head:
        return False, "含 NUL 字节，疑似 UTF-16LE，请转 UTF-8"
    return True, "ok"


EXIT_RE = re.compile(r"\(exit=(-?\d+)\)")


def read_exit_code(path: Path) -> int | None:
    """从证据日志首行取 `(exit=n)`。手工证据（非 _run_command 产出）返回 None。"""
    try:
        with path.open("rb") as fh:
            first = fh.readline(4096)
    except OSError:
        return None
    match = EXIT_RE.search(first.decode("utf-8", "replace"))
    return int(match.group(1)) if match else None


def expect_map_for(node: dict) -> dict[str, int]:
    """config 中每条 AC 期望的退出码。未写 expect 的按 0 处理（与 cmd_gen 一致）。"""
    out: dict[str, int] = {}
    for entry in node.get("evidence") or []:
        ac = entry.get("ac")
        if ac:
            out[ac] = int(entry.get("expect", 0))
    return out


# ---------------------------------------------------------------- 卡点实现


def gate_g1(spec: Spec) -> GateResult:
    if not spec.path.is_file():
        return GateResult("g1", "fail", f"spec 不存在：{spec.path}")

    problems: list[str] = []
    for key in ("goal", "ac", "out_of_scope"):
        if spec.sections.get(key) is None:
            problems.append(f"缺「## {SECTION_ALIASES[key][0]}」节")
        elif not (spec.sections.get(key) or "").strip():
            problems.append(f"「{SECTION_ALIASES[key][0]}」节为空")

    if not spec.criteria:
        problems.append("验收标准条目为 0")
    if spec.malformed:
        problems.append(f"{len(spec.malformed)} 条 AC 缺「| 判: 判定方式」")
    if spec.duplicate_ids:
        problems.append(f"AC 编号重复：{', '.join(sorted(set(spec.duplicate_ids)))}")

    dirty = [c.cid for c in spec.criteria if PLACEHOLDER_RE.search(f"{c.desc} {c.how}")]
    if dirty:
        problems.append(f"AC 仍含占位符未填：{', '.join(dirty)}")

    if problems:
        return GateResult("g1", "fail", "；".join(problems))
    return GateResult(
        "g1",
        "pass",
        f"{len(spec.criteria)} 条 AC，全部携带判定方式",
        evidence=_rel(spec.path),
    )


def _rel(path: Path) -> str:
    """展示用相对路径；临时目录不在项目内时退回绝对路径。"""
    try:
        return str(path.relative_to(PROJECT_ROOT))
    except ValueError:
        return str(path)


def _run_command(cmd: str, cwd: Path, log_path: Path | None) -> tuple[int, str]:
    """执行一条配置中的命令，返回 (退出码, 合并输出)。

    刻意用 shell=False：配置里写的是「程序 + 参数」，不是 shell 片段。
    输出重定向由本脚本接管，因此不需要在命令里写 > 或 2>&1。
    """
    try:
        argv = shlex.split(cmd, posix=True)
    except ValueError as exc:
        return 127, f"命令无法解析：{cmd!r}：{exc}"
    if not argv:
        return 127, "空命令"

    try:
        proc = subprocess.run(
            argv,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=900,
        )
    except FileNotFoundError:
        return 127, f"可执行文件不存在：{argv[0]}"
    except subprocess.TimeoutExpired:
        return 124, f"超时 900s：{cmd}"

    out = (proc.stdout or "") + (proc.stderr or "")
    if log_path is not None:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        header = f"# {stamp}  $ {cmd}  (exit={proc.returncode})\n"
        log_path.write_text(header + out, encoding="utf-8", newline="\n")
    return proc.returncode, out


def gate_g2(cfg: dict, unit: str, node: dict, evidence_dir: Path) -> GateResult:
    build_cmds = node.get("build") or []
    if not build_cmds:
        return GateResult("g2", "fail", "config 中该单元未声明 build 命令")

    log_path = evidence_dir / unit / "G2-build.log"
    chunks: list[str] = []
    for cmd in build_cmds:
        code, out = _run_command(cmd, PROJECT_ROOT, log_path)
        chunks.append(f"exit={code}  {cmd}")
        if code != 0:
            tail = "\n".join(out.strip().splitlines()[-12:])
            return GateResult(
                "g2",
                "fail",
                f"命令退出码 {code}：{cmd}\n{tail}",
                evidence=_rel(log_path),
            )

    return GateResult(
        "g2",
        "pass",
        f"{len(build_cmds)} 条命令全部退出码 0",
        evidence=_rel(log_path),
    )


def gate_g3(
    spec: Spec, unit: str, evidence_dir: Path, expects: dict[str, int] | None = None
) -> GateResult:
    """证据卡点：文件在、编码能用、**退出码与预期一致**。

    第三条是 2026-11-03 补的。此前只查前两条，于是 `gen` 打出 `[FAIL] AC-01
    (exit=1)` 之后 `all` 照样报 G3 PASS —— 一条失败的证据和一条通过的证据，
    对判定器来说长得完全一样，绿灯也就不构成证据。config 里的 `expect` 字段
    同样只是装饰：它只影响 `gen` 的打印，没有任何一处读它来判定。

    现在的判定：
      - 首行有 `(exit=n)` 且该 AC 在 config 里声明了 expect → 必须相等
      - 首行有 `(exit=n)` 但 config 里没声明 → 必须为 0
      - 首行没有 `(exit=n)` 但 config 里声明了 cmd → 失败，证据无法核验
      - 首行没有 `(exit=n)` 且没声明 cmd → 视为手工证据，只校验格式
    """
    if not spec.criteria:
        return GateResult("g3", "fail", "无 AC 可核对（G1 未通过）")

    expects = expects or {}
    unit_dir = evidence_dir / unit
    missing: list[str] = []
    bad: list[str] = []
    nocode: list[str] = []
    mismatch: list[str] = []
    manual: list[str] = []
    nonzero_ok = 0

    for crit in spec.criteria:
        matches = sorted(unit_dir.glob(f"{crit.cid}.*")) if unit_dir.is_dir() else []
        if not matches:
            missing.append(crit.cid)
            continue
        target = matches[0]
        ok, why = check_evidence_file(target)
        if not ok:
            bad.append(f"{crit.cid}（{why}）")
            continue

        code = read_exit_code(target)
        want = expects.get(crit.cid)
        if code is None:
            if want is not None:
                nocode.append(crit.cid)
            else:
                manual.append(crit.cid)
            continue
        if want is None:
            want = 0
        if code != want:
            mismatch.append(f"{crit.cid} 期望 exit={want} 实际 exit={code}")
        elif want != 0:
            nonzero_ok += 1

    problems = []
    if missing:
        problems.append(f"缺证据：{', '.join(missing)}")
    if bad:
        problems.append("证据不可用：" + "；".join(bad))
    if nocode:
        problems.append(
            "证据无退出码可核验（config 声明了命令却没记录 (exit=n)）：" + ", ".join(nocode)
        )
    if mismatch:
        problems.append("退出码与预期不符：" + "；".join(mismatch))
    if problems:
        return GateResult("g3", "fail", "；".join(problems))

    detail = f"{len(spec.criteria)} 条 AC 证据齐备，编码合规，退出码与预期一致"
    if nonzero_ok:
        detail += f"（其中 {nonzero_ok} 条为期望非 0 的负向用例）"
    if manual:
        detail += f"；{len(manual)} 条为手工证据，仅校验格式（{', '.join(manual)}）"
    return GateResult("g3", "pass", detail, evidence=_rel(unit_dir))


def gate_g4(spec: Spec) -> GateResult:
    body = spec.sections.get("unverified")
    if body is None:
        return GateResult("g4", "fail", "缺「## 未验证」节")
    if not body.strip():
        return GateResult("g4", "fail", "「## 未验证」节为空，请至少写「无」")
    return GateResult("g4", "pass", "未验证项已声明")


# ---------------------------------------------------------------- gen


def cmd_gen(cfg: dict, unit: str, node: dict, evidence_dir: Path) -> int:
    entries = node.get("evidence") or []
    if not entries:
        die(f"单元 {unit} 未在 config 中声明 [[units.{unit}.evidence]]，无证据可产出")

    unit_dir = evidence_dir / unit
    unit_dir.mkdir(parents=True, exist_ok=True)
    failures = 0

    for entry in entries:
        ac = entry.get("ac")
        cmd = entry.get("cmd")
        ext = entry.get("ext", "log")
        if not ac or not cmd:
            print(f"[FAIL] 证据条目缺 ac 或 cmd：{entry}", file=sys.stderr)
            failures += 1
            continue

        expect = entry.get("expect", 0)
        target = unit_dir / f"{ac}.{ext}"
        code, out = _run_command(cmd, PROJECT_ROOT, target)
        status = "PASS" if code == expect else "FAIL"
        note = "" if expect == 0 else f"（期望 exit={expect}）"
        print(f"[{status}] {ac}  exit={code}{note}  -> {_rel(target)}")
        if code != expect:
            failures += 1

    return EXIT_OK if failures == 0 else EXIT_FAIL


# ---------------------------------------------------------------- selftest


def cmd_selftest() -> int:
    """负向自测：证明判定器对异常输入确实返回失败。

    一个只会返回通过的判定器，其绿灯不构成任何证据。这里逐条验证拒绝能力。
    全程使用临时目录，不读写项目文件。
    """
    import tempfile

    good = """# demo

## 目标
演示用完整 spec

## 验收标准
- [AC-01] 能跑 | 判: 退出码 0

## 不做范围
无

## 未验证
无
"""

    cases: list[tuple[str, bool]] = []

    def run_g1(content: str | None) -> str:
        with tempfile.TemporaryDirectory() as td:
            if content is None:
                return gate_g1(Spec(path=Path(td) / "absent.md")).status
            path = Path(td) / "u.md"
            path.write_text(content, encoding="utf-8")
            return gate_g1(parse_spec(path)).status

    def run_g4(content: str) -> str:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "u.md"
            path.write_text(content, encoding="utf-8")
            return gate_g4(parse_spec(path)).status

    def run_g3(writer, expects: dict[str, int] | None = None) -> str:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            path = root / "u.md"
            path.write_text(good, encoding="utf-8")
            ev_root = root / "ev"
            unit_dir = ev_root / "demo"
            unit_dir.mkdir(parents=True)
            writer(unit_dir / "AC-01.log")
            return gate_g3(parse_spec(path), "demo", ev_root, expects).status

    def head(code: int):
        """产出一条 _run_command 格式的日志（首行带退出码）。"""
        return lambda p: p.write_text(
            f"# 2026-11-03 12:00:00  $ demo command  (exit={code})\noutput\n", encoding="utf-8"
        )

    cases.append(("完整 spec 应通过", run_g1(good) == "pass"))
    cases.append(("spec 缺失应失败", run_g1(None) == "fail"))
    cases.append(("缺验收标准节应失败", run_g1(good.replace("## 验收标准", "## 备注")) == "fail"))
    cases.append(("AC 缺判定方式应失败", run_g1(good.replace(" | 判: 退出码 0", "")) == "fail"))
    cases.append(("AC 含待填写应失败", run_g1(good.replace("能跑", "待填写")) == "fail"))
    cases.append(
        ("AC 编号重复应失败", run_g1(good.replace("- [AC-01] 能跑", "- [AC-01] 甲\n- [AC-01] 乙")) == "fail")
    )
    cases.append(("缺不做范围节应失败", run_g1(good.replace("## 不做范围", "## 其他")) == "fail"))
    cases.append(("未验证节为空应失败", run_g4(good.replace("## 未验证\n无", "## 未验证\n")) == "fail"))
    cases.append(("未验证节缺失应失败", run_g4(good.replace("## 未验证", "## 附注")) == "fail"))
    cases.append(("证据缺失应失败", run_g3(lambda p: None) == "fail"))
    cases.append(
        ("证据带 BOM 应失败", run_g3(lambda p: p.write_bytes(b"\xef\xbb\xbfok")) == "fail")
    )
    cases.append(("证据含 NUL 应失败", run_g3(lambda p: p.write_bytes(b"o\x00k")) == "fail"))
    cases.append(
        ("证据合规应通过", run_g3(lambda p: p.write_text("ok", encoding="utf-8")) == "pass")
    )
    # 退出码校验（2026-11-03 补的窟窿）。这几条是本次改动的核心证据：
    # 前四条里只要少一条，就说明判定又退回到「文件在就算过」。
    cases.append(("退出码 0 且期望 0 应通过", run_g3(head(0), {"AC-01": 0}) == "pass"))
    cases.append(("退出码 1 但期望 0 应失败", run_g3(head(1), {"AC-01": 0}) == "fail"))
    cases.append(("退出码 1 且期望 1 应通过", run_g3(head(1), {"AC-01": 1}) == "pass"))
    cases.append(("退出码 0 但期望 1 应失败", run_g3(head(0), {"AC-01": 1}) == "fail"))
    cases.append(
        ("非 0 退出码未声明 expect 应失败", run_g3(head(2)) == "fail")
    )
    cases.append(
        (
            "声明了命令却无退出码应失败",
            run_g3(lambda p: p.write_text("ok\n", encoding="utf-8"), {"AC-01": 0}) == "fail",
        )
    )

    passed = 0
    for name, ok in cases:
        print(f"[{'PASS' if ok else 'FAIL'}] {name}")
        passed += 1 if ok else 0
    print(f"\n{passed}/{len(cases)} 项通过")
    return EXIT_OK if passed == len(cases) else EXIT_FAIL


# ---------------------------------------------------------------- 输出


def render(results: list[GateResult], unit: str, tier: str) -> None:
    print(f"== {unit} · {tier} · 需过 {' '.join(g.upper() for g in TIER_GATES[tier])} ==\n")
    width = 0
    for res in results:
        label = f"[{res.status.upper()}] {GATE_TITLES[res.gate_id]}"
        width = max(width, len(label))
        detail = res.detail.replace("\n", " / ")
        print(f"{label}  —  {detail}")
        if res.evidence:
            print(f"{' ' * width}     {res.evidence}")
        print()

    failed = [r for r in results if r.status == "fail"]
    if failed:
        print(f"结论：不通过（{len(failed)} 项失败）")
    else:
        print("结论：通过")
    sys.stdout.flush()


def die(msg: str) -> None:
    print(f"错误：{msg}", file=sys.stderr)
    raise SystemExit(EXIT_USAGE)


# ---------------------------------------------------------------- main


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="gate.py", description="单人项目交付规范卡点判定器（standard.md v1.0）"
    )
    parser.add_argument(
        "gate",
        choices=["g1", "g2", "g3", "g4", "all", "gen", "doctor", "selftest"],
        help="要执行的卡点，all 按档位执行，gen 产出证据，selftest 做负向自测",
    )
    parser.add_argument("--unit", help="工作单元名，须在 config.toml 中声明")
    parser.add_argument("--json", action="store_true", help="以 JSON 输出")
    args = parser.parse_args()

    if args.gate == "doctor":
        print(f"项目根   : {PROJECT_ROOT}")
        print(f"规范目录 : {STANDARD_DIR}")
        print(f"配置文件 : {CONFIG_PATH}  ({'存在' if CONFIG_PATH.is_file() else '缺失'})")
        cfg = load_config()
        spec_dir, evidence_dir = paths(cfg)
        print(f"spec 目录: {spec_dir}  ({'存在' if spec_dir.is_dir() else '缺失'})")
        print(f"证据目录 : {evidence_dir}  ({'存在' if evidence_dir.is_dir() else '缺失'})")
        units = cfg.get("units") or {}
        print(f"已声明单元: {', '.join(sorted(units)) or '（无）'}")
        for name in sorted(units):
            print(f"  - {name:12s} tier={units[name].get('tier')}")
        return EXIT_OK

    if args.gate == "selftest":
        return cmd_selftest()

    if not args.unit:
        die("--unit 必填")

    cfg = load_config()
    node = unit_config(cfg, args.unit)
    spec_dir, evidence_dir = paths(cfg)
    tier = node["tier"]
    spec = parse_spec(spec_dir / f"{args.unit}.md")

    if args.gate == "gen":
        return cmd_gen(cfg, args.unit, node, evidence_dir)

    wanted = TIER_GATES[tier] if args.gate == "all" else (args.gate,)
    results: list[GateResult] = []
    for gate_id in ("g1", "g2", "g3", "g4"):
        if gate_id not in wanted:
            if args.gate == "all":
                reason = f"该档（{tier}）不需要此卡点"
            else:
                reason = "本次未请求（可用 all 按档位全跑）"
            results.append(GateResult(gate_id, "skip", reason))
            continue
        if gate_id == "g1":
            results.append(gate_g1(spec))
        elif gate_id == "g2":
            results.append(gate_g2(cfg, args.unit, node, evidence_dir))
        elif gate_id == "g3":
            results.append(gate_g3(spec, args.unit, evidence_dir, expect_map_for(node)))
        else:
            results.append(gate_g4(spec))

    if args.json:
        payload = {
            "unit": args.unit,
            "tier": tier,
            "ok": not any(r.status == "fail" for r in results),
            "gates": [
                {
                    "id": r.gate_id.upper(),
                    "status": r.status,
                    "detail": r.detail,
                    "evidence": r.evidence,
                }
                for r in results
            ],
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        render(results, args.unit, tier)

    return EXIT_FAIL if any(r.status == "fail" for r in results) else EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
