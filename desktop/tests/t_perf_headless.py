#!/usr/bin/env python3
"""AC-07：真实浏览器（headless Chrome/Edge）跑完整探针流程并回传合法报告。

这一项证明的是「探针能跑」，不是「性能达标」。

区别很重要：headless 没有真实 vsync 与 GPU 合成，帧率数字与真机不可比。
所以这里只断言「三个场景都采到了帧、报告结构合法、能 JSON 往返」，
**不断言 verdict.level**。把 headless 的数字当性能结论，就会得出
「本地 120fps 所以没问题」这种自欺的结论。

两条路径各验一次：
  A. http:// 加载 —— 常规 Web 语境
  B. file:// 加载 —— 用户双击打开的实际路径，走不同的模块解析与 CORS 分支

踩过的坑（写在这里免得后人重踩）：Windows 上 chrome.exe 是 GUI 子系统程序，
`--dump-dom` 的输出接不到管道；`--screenshot` 与 `--dump-dom` 这类一次性任务
模式在本机启动后直接 rc=0 退出、什么都不产出。可靠做法是
`--remote-debugging-port=0` 让浏览器常驻，页面自己把结果 POST 回来。
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

ROOT = Path(__file__).resolve().parents[2]
# 探针产物在 dist-perf，不与主应用的 dist 混放：
# vite 构建主应用时会清空 outDir，共用目录会把探针产物一并删掉
DIST = ROOT / "desktop" / "dist-perf"
PAGE = DIST / "perf-standalone.html"
REPORT_TIMEOUT_S = 120.0

CHROME_CANDIDATES = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
]

BASE_FLAGS = [
    "--headless=new",
    "--no-first-run",
    "--no-default-browser-check",
    "--disable-extensions",
    "--disable-sync",
    "--disable-default-apps",
    "--disable-popup-blocking",
    # 这三条防止后台节流把 rAF 降到 1Hz，否则会采到 0 帧
    "--disable-background-timer-throttling",
    "--disable-renderer-backgrounding",
    "--disable-backgrounding-occluded-windows",
    # 不隐藏滚动条：滚动条占宽度，会影响布局与虚拟滚动的可视行数
    # 用 1080p 视口：headless 默认 800x600，那是手机尺寸，不能代表桌面布局
    "--window-size=1920,1080",
]


def find_browser() -> str:
    for path in CHROME_CANDIDATES:
        if Path(path).exists():
            return path
    raise SystemExit("找不到 Chrome/Edge，无法做真实浏览器验证")


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def cdp_ready(port: int) -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=1) as resp:
            return resp.status == 200
    except (urllib.error.URLError, OSError, ValueError):
        return False


def kill_browser(exe_name: str, marker: str) -> None:
    """按 profile 目录名精确杀进程。

    不能靠 Popen 返回的 pid：Chrome 的启动器进程在 Windows 上会立即分离退出
    （rc=0），真正的浏览器进程是另一个 pid。同理 `proc.poll()` 不能用来判断
    「浏览器还活着吗」—— 那会得出完全相反的结论。

    用 profile 目录名做匹配而不是杀所有 chrome.exe：用户自己开着的浏览器
    不能被误杀。
    """
    script = (
        f"Get-CimInstance Win32_Process -Filter \"Name='{exe_name}'\" | "
        f"Where-Object {{ $_.CommandLine -like '*{marker}*' }} | "
        "ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"
    )
    subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True, timeout=90, check=False,
    )


class Handler(BaseHTTPRequestHandler):
    """静态服务 dist 目录，并在 /report 上接收页面回传。"""

    root: Path = DIST
    reports: list[dict] = []
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args: object) -> None:  # 静默，别污染证据日志
        return

    def _cors(self) -> None:
        # file:// 的 origin 是 opaque(null)，不加这个头连预检都过不去
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        self._cors()
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self) -> None:
        path = unquote(urlparse(self.path).path)
        if path in ("/", ""):
            path = "/perf-standalone.html"
        target = (self.root / path.lstrip("/")).resolve()
        try:
            target.relative_to(self.root.resolve())
        except ValueError:
            self.send_error(403)
            return
        if not target.is_file():
            self.send_error(404)
            return

        data = target.read_bytes()
        ctype = "text/html; charset=utf-8" if target.suffix == ".html" else "application/octet-stream"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length") or "0")
        body = self.rfile.read(length)
        if urlparse(self.path).path != "/report":
            self.send_error(404)
            return
        try:
            Handler.reports.append(json.loads(body.decode("utf-8")))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            self.send_error(400, str(exc))
            return
        self.send_response(204)
        self._cors()
        self.send_header("Content-Length", "0")
        self.end_headers()


class QuietServer(ThreadingHTTPServer):
    """浏览器被强制结束时连接会 RST，那是清理动作的副作用，不是测试失败。
    默认实现会把整段 traceback 打到 stderr，污染证据日志。"""

    daemon_threads = True

    def handle_error(self, request: object, client_address: object) -> None:
        return


def serve() -> tuple[QuietServer, int, threading.Thread]:
    server = QuietServer(("127.0.0.1", 0), Handler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, port, thread


def kill_tree(proc: subprocess.Popen[bytes]) -> None:
    if proc.poll() is not None:
        return
    subprocess.run(
        ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
        capture_output=True, check=False,
    )
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()


def run_probe(browser: str, url: str, profile: Path) -> dict:
    before = len(Handler.reports)
    port = free_port()
    profile.mkdir(parents=True, exist_ok=True)

    proc = subprocess.Popen(
        [
            browser,
            *BASE_FLAGS,
            f"--remote-debugging-port={port}",
            f"--user-data-dir={profile}",
            url,
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env={**os.environ, "NO_COLOR": "1"},
    )

    try:
        # 等浏览器真的起来：判据是 CDP 端口能应答，不是启动器进程是否存活
        ready_deadline = time.time() + 30
        while time.time() < ready_deadline:
            if cdp_ready(port):
                break
            time.sleep(0.2)
        else:
            raise SystemExit(f"浏览器在 30s 内没有监听 CDP 端口 {port}，无法验证")

        deadline = time.time() + REPORT_TIMEOUT_S
        while time.time() < deadline:
            if len(Handler.reports) > before:
                return Handler.reports[before]
            time.sleep(0.2)
        raise SystemExit(f"等待报告超时（{REPORT_TIMEOUT_S:.0f}s），页面可能卡住或没触发 auto")
    finally:
        kill_tree(proc)
        kill_browser(Path(browser).name, profile.name)


def validate(report: dict) -> list[str]:
    problems: list[str] = []

    if report.get("schema") != "perf-v2":
        problems.append(f"schema 不是 perf-v2：{report.get('schema')!r}")

    scenarios = report.get("scenarios")
    if not isinstance(scenarios, list):
        problems.append("scenarios 不是数组，字段可能坏了")
        return problems

    if [s.get("id") for s in scenarios] != ["s1", "s2", "s3"]:
        problems.append(f"场景不是三个或顺序不对：{[s.get('id') for s in scenarios]}")

    for scene in scenarios:
        stats = scene.get("stats") or {}
        frames = stats.get("frames")
        if not isinstance(frames, int) or frames <= 0:
            problems.append(f"{scene.get('id')} 没采到帧（frames={frames}），rAF 可能没跑起来")
        if not isinstance(stats.get("p95"), (int, float)):
            problems.append(f"{scene.get('id')} 缺 p95")

    mount_ms = (scenarios[0].get("extra") or {}).get("mountMs") if scenarios else None
    if not isinstance(mount_ms, (int, float)) or mount_ms <= 0:
        problems.append(f"s1 的 mountMs 无效：{mount_ms!r}")

    env = report.get("env") or {}
    if not isinstance(env.get("ua"), str) or env["ua"] == "":
        problems.append("env.ua 为空，环境指纹没采到")
    if not isinstance(env.get("dpr"), (int, float)) or env["dpr"] <= 0:
        problems.append(f"env.dpr 无效：{env.get('dpr')!r}")
    for axis in ("screen", "viewport"):
        dims = env.get(axis) or {}
        if not isinstance(dims.get("w"), int) or dims["w"] <= 0:
            problems.append(f"env.{axis} 无效：{dims!r}")
    if not isinstance(env.get("renderer"), str):
        problems.append("env.renderer 缺失")

    # 刷新率校准：headless 下也必须有结果（rAF 由虚拟合成器驱动，约 59-60Hz）。
    # 这里只验「采到了、口径自洽」，不验具体 Hz —— 虚拟合成器的节拍不是真屏。
    display = env.get("display") or {}
    if display.get("source") not in {"measured", "unavailable"}:
        problems.append(f"env.display.source 非法：{display.get('source')!r}")
    if display.get("source") == "measured":
        if not isinstance(display.get("samples"), int) or display["samples"] < 5:
            problems.append(f"校准样本过少：{display.get('samples')!r}")
        if not isinstance(display.get("refreshHz"), (int, float)) or display["refreshHz"] <= 0:
            problems.append(f"env.display.refreshHz 无效：{display.get('refreshHz')!r}")
    target_hz = display.get("targetHz")
    if not isinstance(target_hz, (int, float)) or not (15 <= target_hz <= 60):
        problems.append(f"env.display.targetHz 越界：{target_hz!r}")

    thresholds = report.get("thresholds") or {}
    if "thresholds" not in report:
        problems.append("缺 thresholds，跨机数据没法对齐口径")
    elif isinstance(target_hz, (int, float)) and abs(thresholds.get("targetHz", -1) - target_hz) > 1e-9:
        problems.append(
            f"thresholds.targetHz={thresholds.get('targetHz')!r} 与 display.targetHz={target_hz} 不一致",
        )
    if isinstance(target_hz, (int, float)) and isinstance(thresholds.get("budgetMs"), (int, float)):
        if abs(thresholds["budgetMs"] - 1000.0 / target_hz) > 1e-6:
            problems.append(
                f"帧预算没跟目标帧率走：budgetMs={thresholds['budgetMs']!r} targetHz={target_hz}",
            )

    verdict = report.get("verdict") or {}
    if verdict.get("level") not in {"pass", "warn", "fail", "env-limited"}:
        problems.append(f"verdict.level 不是四档之一：{verdict.get('level')!r}")
    if not isinstance(verdict.get("reasons"), list) or not verdict["reasons"]:
        problems.append("verdict.reasons 为空，结论没有理由等于没结论")

    if not isinstance(report.get("cssAudit"), list):
        problems.append("cssAudit 不是数组")

    try:
        round_trip = json.loads(json.dumps(report))
    except (TypeError, ValueError) as exc:
        problems.append(f"报告无法 JSON 往返：{exc}")
    else:
        if round_trip != report:
            problems.append("报告 JSON 往返后不相等（可能有 undefined 字段）")

    return problems


def describe(report: dict) -> None:
    for scene in report.get("scenarios", []):
        stats = scene.get("stats", {})
        extra = scene.get("extra", {})
        print(
            f"  {scene.get('id')} {scene.get('name')}: "
            f"{stats.get('frames')} 帧 {stats.get('fps')}fps "
            f"p50={stats.get('p50')} p95={stats.get('p95')} p99={stats.get('p99')} "
            f"卡顿={stats.get('jankRatio')} 挂载={extra.get('mountMs')}ms"
        )
    env = report.get("env", {})
    print(f"  env: dpr={env.get('dpr')} renderer={env.get('renderer')} "
          f"screen={env.get('screen')} viewport={env.get('viewport')}")
    display = env.get("display") or {}
    thresholds = report.get("thresholds") or {}
    print(f"  display: source={display.get('source')} refreshHz={display.get('refreshHz')} "
          f"nominalHz={display.get('nominalHz')} targetHz={display.get('targetHz')} "
          f"median={display.get('medianFrameMs')}ms samples={display.get('samples')}")
    print(f"  thresholds: targetHz={thresholds.get('targetHz')} "
          f"budget={thresholds.get('budgetMs')} p95Max={thresholds.get('p95MaxMs')} "
          f"failFps={thresholds.get('failFps')}")
    verdict = report.get("verdict", {})
    print(f"  verdict: {verdict.get('level')}")
    for reason in verdict.get("reasons", []):
        print(f"    · {reason}")
    print(f"  cssAudit: {len(report.get('cssAudit', []))} 处违规")


def main() -> int:
    if not PAGE.exists():
        print(f"找不到 {PAGE}")
        print("先跑：python toolchain/build.py desktop --perf")
        return 4

    browser = find_browser()
    print(f"浏览器：{browser}")

    profile_root = Path(tempfile.mkdtemp(prefix="perf-probe-"))
    failures = 0
    server = None
    thread = None
    try:
        server, port, thread = serve()
        print(f"本地服务：http://127.0.0.1:{port} （dist-perf 目录 + /report 回传）")
        with socket.create_connection(("127.0.0.1", port), timeout=2):
            pass

        cases = [
            (
                "http:// 加载",
                f"http://127.0.0.1:{port}/perf-standalone.html"
                f"?auto=1&report=http://127.0.0.1:{port}/report",
            ),
            (
                "file:// 双击路径",
                f"{PAGE.as_uri()}?auto=1&report=http://127.0.0.1:{port}/report",
            ),
        ]

        for index, (label, url) in enumerate(cases, start=1):
            print(f"\n[{index}/{len(cases)}] {label}")
            try:
                report = run_probe(browser, url, profile_root / f"p{index}-{os.getpid()}")
            except SystemExit as exc:
                failures += 1
                print(f"  ✗ {exc}")
                continue

            describe(report)
            problems = validate(report)
            if problems:
                failures += 1
                print("  报告校验失败：")
                for problem in problems:
                    print(f"    ✗ {problem}")
            else:
                print("  ✓ 报告结构合法，三场景均采到帧")

        if server is not None:
            server.shutdown()
            if thread is not None:
                thread.join(timeout=5)
    finally:
        shutil.rmtree(profile_root, ignore_errors=True)

    print()
    if failures:
        print(f"失败 {failures} 项")
        return 1
    print("通过：探针在真实浏览器里能跑完并产出合法报告（帧率数值不参与判定）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
