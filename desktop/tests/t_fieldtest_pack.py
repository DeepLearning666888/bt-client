#!/usr/bin/env python3
"""真机执行包验收（field-test 单元 AC-01..AC-04）。

这个包的全部价值在于「拷到另一台电脑就能用」。所以判定不是「文件存在」，
而是四件更难的事：

1. 包里的东西和清单对得上，且压缩包可复现（AC-01）
2. 前置检查脚本在真实 Windows 上真能跑出结论（AC-02）
3. 它在缺东西的时候**会说不**（AC-03）—— 只会说「一切正常」的检查器等于没有
4. 说明书提到的每个文件都真的在包里，每个人工观察项都写了「记什么」（AC-04）

第 3 条用 `-SelfTest` 注入合成样本：这条路径不碰真实系统，纯粹验证判定逻辑。
第 2 条必须用真的 `powershell.exe` 跑 —— 用 Python 模拟脚本行为证明不了
PowerShell 5.1 能不能解析它。
"""

from __future__ import annotations

import hashlib
import re
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PACK_ROOT = ROOT / "handoff" / "dist"
PACK_NAME = "field-test-pack-v1"
PACK = PACK_ROOT / PACK_NAME
ARCHIVE = PACK_ROOT / f"{PACK_NAME}.zip"
SUMS = "SHA256SUMS.txt"
PERF_ZIP = "01-perf/crossmachine-pack-v1.zip"

DOCS = ("README.md", "CHECKLIST.md", "preflight.ps1")

# 说明里会提到、但**不该**在包里的文件名：
# - 安装目录里的四个文件，是装完才出现的
# - 目标机上跑起来才产生的产物
# - README 里举的「反例」（说明哪些东西不该出现在安装目录里）
INSTALLED_FILES = {"btclient-shell.exe", "engine.exe", "torrent-rasterbar.dll", "uninstall.exe"}
RUNTIME_FILES = {
    "engine.endpoint.json",
    "out/preflight.txt",
    "preflight.txt",
}
# 带日期戳的采集结果，名字不可能枚举，只能按前缀认
RUNTIME_PREFIXES = ("perf-", "env-", "preflight-selftest-")
NOT_SHIPPED = {"package.json"}
# 反引号里出现的文件名候选
FILE_TOKEN = re.compile(r"^[A-Za-z0-9._/\-]+\.(md|ps1|zip|exe|json|txt|dll|sha256)$")
INLINE_CODE = re.compile(r"`([^`\n]+)`")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def check_sums(problems: list[str]) -> int:
    """① 目录文件集 = 清单文件集，逐条哈希一致。"""
    before = len(problems)
    if not PACK.is_dir():
        problems.append(f"缺少执行包目录 {PACK.relative_to(ROOT).as_posix()}")
        return 1

    sums_path = PACK / SUMS
    if not sums_path.is_file():
        problems.append(f"缺少 {SUMS}")
        return 1

    declared: dict[str, str] = {}
    for line in sums_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split(None, 1)
        if len(parts) != 2:
            problems.append(f"{SUMS} 有一行格式不对：{line!r}")
            continue
        declared[parts[1].strip()] = parts[0]

    actual = {
        path.relative_to(PACK).as_posix()
        for path in PACK.rglob("*")
        if path.is_file() and path.name != SUMS
    }
    # 清单是「哪些文件该在包里」的唯一声明。多一个少一个都要报出来：
    # 少了 = 说明里写的东西不存在；多了 = 拷了不该拷的东西（比如上次的采集结果）。
    if set(declared) != actual:
        missing = sorted(set(declared) - actual)
        extra = sorted(actual - set(declared))
        if missing:
            problems.append(f"清单里有但包里没有：{missing}")
        if extra:
            problems.append(f"包里有但清单没声明：{extra}")

    for name, expected in sorted(declared.items()):
        path = PACK / name
        if not path.is_file():
            continue
        if sha256(path) != expected:
            problems.append(f"{name} 的 sha256 与清单不符（传输或组装出错）")

    # PowerShell 5.1 按 ANSI 解码无 BOM 的 UTF-8，非 ASCII 会静默吞语句。
    # 这个包里的 ps1 必须纯 ASCII，注释也不行。
    ps1 = PACK / "preflight.ps1"
    if ps1.is_file():
        raw = ps1.read_bytes()
        if raw.startswith(b"\xef\xbb\xbf"):
            problems.append("preflight.ps1 带 UTF-8 BOM")
        bad = [i + 1 for i, line in enumerate(raw.split(b"\n")) if any(b > 127 for b in line)]
        if bad:
            problems.append(f"preflight.ps1 含非 ASCII 字符（行 {bad[:5]}），PowerShell 5.1 下可能静默失效")

    if not problems[before:]:
        print(f"① 目录完整 ✓ {len(declared)} 个文件，清单逐条哈希一致，ps1 纯 ASCII")
    return len(problems) - before


def check_archive(problems: list[str]) -> int:
    """② 压缩包条目对齐、路径全 ASCII、可复现、旁附 .sha256。"""
    before = len(problems)
    if not ARCHIVE.is_file():
        problems.append(f"缺少压缩包 {ARCHIVE.relative_to(ROOT).as_posix()}")
        return 1

    local = {
        path.relative_to(PACK).as_posix(): sha256(path)
        for path in PACK.rglob("*")
        if path.is_file()
    }

    with zipfile.ZipFile(ARCHIVE) as handle:
        infos = handle.infolist()
        names = [info.filename for info in infos]

        expected = sorted(f"{PACK_NAME}/{rel}" for rel in local)
        if names != expected:
            problems.append(f"压缩包条目与目录不符：{names}，期望 {expected}")

        for name in names:
            # 中文条目名在 Windows 资源管理器解压时按 OEM 代码页解释，会变乱码
            if not name.isascii():
                problems.append(f"压缩包条目名含非 ASCII：{name}")
            parts = [part for part in name.split("/") if part]
            if name.startswith("/") or ".." in parts:
                problems.append(f"压缩包条目路径不安全：{name}")

        for info in infos:
            rel = info.filename.split("/", 1)[-1]
            payload = handle.read(info)
            if hashlib.sha256(payload).hexdigest() != local.get(rel):
                problems.append(f"压缩包里的 {rel} 与目录内容不一致")

        stamps = {info.date_time for info in infos}
        if stamps != {(1980, 1, 1, 0, 0, 0)}:
            problems.append(f"压缩包时间戳不固定：{sorted(stamps)[:3]}，不可复现")
        if {info.create_system for info in infos} != {0}:
            problems.append("压缩包 create_system 不固定，跨平台打包会漂移")

    original = sha256(ARCHIVE)
    rerun = subprocess.run(
        [sys.executable, str(ROOT / "toolchain" / "pack_handoff.py")],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if rerun.returncode != 0:
        problems.append(f"重跑组装脚本失败（退出码 {rerun.returncode}）：{(rerun.stderr or '')[-300:]}")
    elif sha256(ARCHIVE) != original:
        problems.append("重打同一份输入得到不同的压缩包，sha256 无法作为包的标识")

    sidecar = ARCHIVE.with_name(ARCHIVE.name + ".sha256")
    if not sidecar.is_file():
        problems.append(f"缺少 {sidecar.name}")
    elif original not in sidecar.read_text(encoding="utf-8"):
        problems.append(f"{sidecar.name} 里的哈希与压缩包实际值不符")

    if not problems[before:]:
        size_mb = ARCHIVE.stat().st_size / 1024 / 1024
        print(f"② 压缩包 ✓ {size_mb:.2f} MB  {len(local)} 条目  全部 ASCII  重打一致")
    return len(problems) - before


def run_preflight(args: list[str], out_dir: Path) -> subprocess.CompletedProcess:
    cmd = [
        "powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
        "-File", str(PACK / "preflight.ps1"), "-OutDir", str(out_dir), *args,
    ]
    return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")


def check_real_run(problems: list[str]) -> int:
    """③ 真的跑一遍，断言它给出结论。"""
    before = len(problems)
    with tempfile.TemporaryDirectory() as tmp:
        out_dir = Path(tmp)
        result = run_preflight([], out_dir)
        report = out_dir / "preflight.txt"

        if result.returncode != 0:
            problems.append(
                f"前置检查退出码 {result.returncode}（期望 0 = READY）。"
                f"stdout 尾部：{(result.stdout or '')[-300:]}"
                f" stderr：{(result.stderr or '')[-200:]}"
            )
        for name in ("OS", "WEBVIEW2", "VCRUNTIME", "BROWSER"):
            if name not in (result.stdout or ""):
                problems.append(f"前置检查输出里没有 {name} 这一项")
        if "VERDICT:" not in (result.stdout or ""):
            problems.append("前置检查输出里没有 VERDICT 行")

        if not report.is_file():
            problems.append(f"前置检查没有写出报告 {report.name}（目标机上要靠它回传）")
            return len(problems) - before

        text = report.read_text(encoding="utf-8-sig")
        lines = [line for line in text.splitlines() if line.strip()]
        verdicts = [line for line in lines if line.startswith("VERDICT:")]
        if not verdicts:
            problems.append("报告里没有 VERDICT 行")
        elif not verdicts[0].startswith("VERDICT: READY"):
            problems.append(f"本机应当是 READY，实际是 {verdicts[0]!r}")
        # 判定行的前缀只有三种，多出来的格式说明输出被污染了
        odd = [line for line in lines if line.startswith("[") and not re.match(r"^\[(PASS|FAIL|WARN)\]", line)]
        if odd:
            problems.append(f"报告里有格式异常的判定行：{odd[:2]}")
        fails = [line for line in lines if line.startswith("[FAIL]")]
        if fails:
            problems.append(f"本机出现 FAIL，前置检查判定它跑不了：{fails}")

    if not problems[before:]:
        print("③ 真实运行 ✓ 退出码 0，报告落盘，判定 READY")
    return len(problems) - before


def check_negative(problems: list[str]) -> int:
    """④ 负向自检：缺东西时必须说「不」，且指得出缺的是哪一项。"""
    before = len(problems)
    cases = [
        ("ok", 0, "VERDICT: READY", None),
        ("no-vcruntime", 2, "VERDICT: BLOCKED", "VCRUNTIME"),
        ("no-webview2", 2, "VERDICT: BLOCKED", "WEBVIEW2"),
    ]
    for case, want_code, want_text, want_item in cases:
        with tempfile.TemporaryDirectory() as tmp:
            result = run_preflight(["-SelfTest", case], Path(tmp))
        if result.returncode != want_code:
            problems.append(f"自检 {case}：退出码 {result.returncode}，期望 {want_code}")
        if want_text not in (result.stdout or ""):
            problems.append(f"自检 {case}：输出里没有 {want_text!r}")
        if want_item:
            # 只说「有问题」不够，得指到具体是哪个检查项
            flagged = [
                line for line in (result.stdout or "").splitlines()
                if line.startswith("[FAIL]") and want_item in line
            ]
            if not flagged:
                problems.append(f"自检 {case}：没有把 {want_item} 标成 FAIL")
    if not problems[before:]:
        print(f"④ 负向自检 ✓ {len(cases)} 个样本，缺项时退出码 2 并指出缺的是哪一项")
    return len(problems) - before


def check_docs(problems: list[str]) -> int:
    """⑤ 文档闭环：说明里提到的文件都在包里；观察项都写了「记什么」。"""
    before = len(problems)

    inner_names: set[str] = set()
    perf_zip = PACK / PERF_ZIP
    if perf_zip.is_file():
        with zipfile.ZipFile(perf_zip) as handle:
            for name in handle.namelist():
                inner_names.add(name.split("/", 1)[-1])

    pack_names = {
        path.relative_to(PACK).as_posix()
        for path in PACK.rglob("*")
        if path.is_file()
    }

    missing_files: list[str] = []
    for doc in ("README.md", "CHECKLIST.md"):
        text = (PACK / doc).read_text(encoding="utf-8-sig")
        for token in INLINE_CODE.findall(text):
            candidate = token.strip().replace("\\", "/").lstrip("./")
            if not FILE_TOKEN.match(candidate):
                continue
            if candidate in pack_names:
                continue
            leaf = candidate.rsplit("/", 1)[-1]
            if leaf in INSTALLED_FILES or leaf in RUNTIME_FILES or leaf in NOT_SHIPPED:
                continue
            if leaf.startswith(RUNTIME_PREFIXES):
                continue
            if candidate in RUNTIME_FILES or leaf in inner_names:
                continue
            missing_files.append(f"{doc} 提到 `{token}`，但包里没有")

    problems.extend(sorted(set(missing_files)))

    # 观察表的价值全在「记什么」那一列。少于 3 列的行等于让人自由发挥，
    # 回来的数据就没法横向比较。
    checklist = (PACK / "CHECKLIST.md").read_text(encoding="utf-8-sig")
    thin: list[str] = []
    rows = 0
    for line in checklist.splitlines():
        line = line.strip()
        if not line.startswith("|"):
            continue
        cells = [cell.strip() for cell in line.strip("|").split("|")]
        if all(set(cell) <= set("-: ") and cell for cell in cells):
            continue  # 分隔行
        rows += 1
        if len(cells) < 3:
            thin.append(line[:60])
    if rows < 30:
        problems.append(f"CHECKLIST 表格只有 {rows} 行，覆盖不到安装向导的各个页面")
    if thin:
        problems.append(f"CHECKLIST 有 {len(thin)} 行少于 3 列：{thin[:2]}")

    if not problems[before:]:
        print(f"⑤ 文档闭环 ✓ 提及文件全部存在，观察表 {rows} 行均含判定列")
    return len(problems) - before


def main() -> int:
    if not PACK.is_dir():
        print(f"缺少执行包 {PACK.relative_to(ROOT).as_posix()}")
        print("先跑：python toolchain/pack_handoff.py")
        return 4

    problems: list[str] = []
    check_sums(problems)
    check_archive(problems)
    if problems:
        # 目录/压缩包都不完整时，往下跑验证没有意义：验的是同一个坏包
        for problem in problems:
            print(f"  ✗ {problem}")
        print(f"失败 {len(problems)} 项")
        return 1

    check_real_run(problems)
    check_negative(problems)
    check_docs(problems)

    print()
    if problems:
        for problem in problems:
            print(f"  ✗ {problem}")
        print(f"失败 {len(problems)} 项")
        return 1

    print("通过：包可复现、前置检查真跑并会说「不」、说明书与包内容一致")
    return 0


if __name__ == "__main__":
    sys.exit(main())
