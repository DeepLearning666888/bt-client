#!/usr/bin/env python3
"""任务与界面能不能活过「壳和引擎都突然没了」。

已有的三条测试各管一段，谁也不覆盖这条链路：

    t_persist_crash.py    只跑引擎，从头到尾没有壳
    t_shell_e2e.py        只跑壳，不加任务、不杀任何进程
    restart_takeover.rs   在 Rust 里直接调 resolve()，不起真 exe

真实用户遇上的是两者叠在一起：壳起引擎 → 通过界面加任务 → 引擎被任务管理器结束
→ 壳跟着关 → 重开。没有任何一条测试站在这条链路上。

A 段（C28/C29）：加任务 → 硬杀引擎 → 硬杀壳 → 重开 → 任务还在、状态一致、引擎是新进程
B 段（C32/C33）：加任务 → 只杀壳（引擎变孤儿）→ 立刻重开 → 接管同一个引擎，任务还在

两段都必须用 `taskkill /F`，不能用 WM_CLOSE：优雅退出会走 `flush_all` 和
`/shutdown`，那两条路本来就写盘，测到的不是用户真实的失败场景。

两段都在独立的 `BTCLIENT_DATA_DIR` 里跑，不碰 `%APPDATA%` 下的真实数据。
单实例锁按数据目录区分（见 lib.rs 的 instance_key），所以不会跟正在使用的壳抢锁。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
EXE = ROOT / "desktop" / "src-tauri" / "target" / "release" / "btclient-shell.exe"
sys.path.insert(0, str(ROOT / "engine" / "tests"))

import mktorrent  # noqa: E402

READY_TIMEOUT_S = 90.0
SETTLE_TIMEOUT_S = 20.0
ENGINE_DIR = "engine"
PAYLOAD = bytes(range(256)) * 40

READY_RE = re.compile(r"frontend_ready port=(\d+) seq=(\d+)")
# `pid={:?}` 打的是 Option<u32>，格式是 Some(1234) 或 None
DECISION_RE = re.compile(r"engine decision=(\w+) port=(\d+) pid=(?:Some\((\d+)\)|None)")

problems: list[str] = []

# 本次测试拉起的引擎。清理时**只按 pid 杀这些**，绝不 `taskkill /IM engine.exe`：
# 用户可能正开着自己的实例，一条“省事”的通配杀会把人家正在下载的任务全部打掉。
spawned_engines: list[dict] = []


def expect(condition: bool, message: str) -> bool:
    print(f"  {'✓' if condition else '✗'} {message}")
    if not condition:
        problems.append(message)
    return condition


# ---------- 日志 ----------

def log_path(data_dir: Path) -> Path:
    return data_dir / "shell.log"


def read_since(path: Path, offset: int) -> str:
    if not path.exists():
        return ""
    with path.open("rb") as handle:
        handle.seek(offset)
        return handle.read().decode("utf-8", "replace")


def wait_in_log(pattern: re.Pattern[str], data_dir: Path, offset: int,
                proc: subprocess.Popen[bytes] | None = None,
                timeout: float = READY_TIMEOUT_S) -> re.Match[str] | None:
    """只在新写入的部分里找。

    shell.log 是追加写（shelllog.rs 用 OpenOptions.append）。重启之后，上一轮那行
    `frontend_ready` 还在文件里；不记偏移就会搜到旧行，把「新实例根本没起来」
    判成「起来了」—— 后面所有断言都会在一个没起来的界面上通过。
    """
    path = log_path(data_dir)
    deadline = time.time() + timeout
    while True:
        match = pattern.search(read_since(path, offset))
        if match is not None:
            return match
        if proc is not None and proc.poll() is not None:
            # 进程已经退了，但日志可能刚写完，再读一次再放弃
            return pattern.search(read_since(path, offset))
        if time.time() >= deadline:
            return None
        time.sleep(0.2)


# ---------- 壳 ----------

def start_shell(data_dir: Path) -> tuple[subprocess.Popen[bytes], int]:
    path = log_path(data_dir)
    offset = path.stat().st_size if path.exists() else 0
    env = {**os.environ, "BTCLIENT_DATA_DIR": str(data_dir)}
    proc = subprocess.Popen(
        [str(EXE)],
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        # 不能给 PIPE：壳退出后 Popen 的后台线程会用 UTF-8 读管道，
        # 而中文 Windows 上 WebView2 写的是 GBK，一有字节就抛 UnicodeDecodeError，
        # 满屏堆栈把真正的失败信息埋掉。壳的 stderr 本来也不必看 —— shell.log 才是日志。
        stderr=subprocess.DEVNULL,
    )
    return proc, offset


def hard_kill(pid: int, label: str) -> bool:
    """TerminateProcess，没有收尾机会 —— 和任务管理器「结束进程」是同一条路。"""
    # 不能给 text=True：它按 locale 解码，中文 Windows 上 taskkill 输出 GBK，
    # 一旦 Python 跑在 UTF-8 模式就在读取线程里抛 UnicodeDecodeError。
    result = subprocess.run(["taskkill", "/F", "/PID", str(pid)],
                            capture_output=True, timeout=30)
    ok = result.returncode == 0
    print(f"  {'✓' if ok else '✗'} 强杀{label} pid={pid}")
    if not ok:
        detail = (result.stderr or result.stdout).decode("gbk", "replace").strip()
        problems.append(f"强杀{label}失败：{detail}")
    return ok


def wait_resume_file(data_dir: Path, prefix: str, timeout: float = 5.0) -> bool:
    """等 resume 文件出现。

    `save_resume_data` 是异步的：调用只是把请求排进 libtorrent 的队列，真正落盘发生在
    下一个 alert 周期。加完任务立刻去 glob，文件多半还没写出来 —— 一开始就这么写，
    结果是偶发红（同一份代码跑三次两次绿一次红），而失败原因和落盘逻辑毫无关系。
    这里给的是「不等退出也能落盘」这个语义，不是「同一毫秒内写完」。
    """
    resume_dir = data_dir / ENGINE_DIR / "resume"
    deadline = time.time() + timeout
    while time.time() < deadline:
        if resume_dir.is_dir() and any(resume_dir.glob(f"{prefix}*")):
            return True
        time.sleep(0.1)
    return False


def stale_engine() -> str | None:
    """壳加载的引擎是不是刚构建的那个。

    `tauri.conf.json` 把 `engine/build/engine.exe` 声明成 resource，`cargo build --release`
    会把它**复制**成 `target/release/engine.exe`；壳优先用这份副本，找不到才回退到 build 目录。
    问题是这个复制只在跑 `shell build --release` 时发生 —— 只重建引擎就来跑这条测试，
    跑的是上一版二进制，结论照样全绿。今天真的踩到了：撤掉落盘代码后测试仍然通过，
    查了半天才发现壳加载的是 15:51 那份旧 exe。

    这里不做自动复制：静默同步会让「构建链路漏了一步」继续隐形，下次照样骗人。
    """
    built = ROOT / "engine" / "build" / "engine.exe"
    bundled = EXE.parent / "engine.exe"
    if not built.exists():
        return f"找不到引擎 {built}，先跑：python toolchain/build.py engine"
    if not bundled.exists():
        return None  # 没有副本时壳会回退到 build 目录，那条路径是对的
    if _digest(bundled) != _digest(built):
        return (
            f"壳加载的引擎是旧版：{bundled}\n"
            f"        与刚构建的 {built} 不一致\n"
            f"        先跑：python toolchain/build.py shell build --release"
        )
    return None


def _digest(path: Path) -> bytes:
    import hashlib

    return hashlib.sha256(path.read_bytes()).digest()


def cleanup_engines() -> None:
    """收尾只杀本次拉起过的引擎 pid。

    端口还开着才动手：pid 可能已经被系统回收给别人，那是另一个进程。
    """
    for info in spawned_engines:
        pid, port = info.get("pid"), int(info["port"])
        if pid and port_open(port):
            subprocess.run(["taskkill", "/F", "/PID", str(pid)],
                           capture_output=True, check=False, timeout=30)


# ---------- 引擎端点 ----------

def endpoint_file(data_dir: Path) -> Path:
    return data_dir / ENGINE_DIR / "engine.endpoint.json"


def read_endpoint(data_dir: Path) -> dict | None:
    try:
        info = json.loads(endpoint_file(data_dir).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(info, dict) or "port" not in info or "token" not in info:
        return None
    return info


def wait_endpoint(data_dir: Path, timeout: float = SETTLE_TIMEOUT_S) -> dict | None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        info = read_endpoint(data_dir)
        if info is not None:
            return info
        time.sleep(0.1)
    return None


def http(info: dict, method: str, path: str, body: bytes | None = None,
         content_type: str | None = None,
         timeout: float = 5.0) -> tuple[int, str]:
    headers = {"Authorization": f"Bearer {info['token']}"}
    if content_type:
        headers["Content-Type"] = content_type
    req = urllib.request.Request(
        f"http://127.0.0.1:{int(info['port'])}{path}",
        data=body, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")
    except (urllib.error.URLError, OSError) as exc:
        return 0, str(exc)


def torrents(info: dict) -> list[dict]:
    status, body = http(info, "GET", "/state?since=-1")
    if status != 200:
        return []
    try:
        return json.loads(body).get("torrents", [])
    except json.JSONDecodeError:
        return []


def wait_torrents(info: dict, count: int, timeout: float = 20.0) -> list[dict]:
    deadline = time.time() + timeout
    while time.time() < deadline:
        items = torrents(info)
        if len(items) >= count:
            return items
        time.sleep(0.2)
    return torrents(info)


def port_open(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.5)
        return sock.connect_ex(("127.0.0.1", port)) == 0


def wait_port_closed(port: int, timeout: float = 10.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not port_open(port):
            return True
        time.sleep(0.1)
    return False


# ---------- 场景 ----------

def add_one_torrent(endpoint: dict, blob: bytes) -> bool:
    status, body = http(endpoint, "POST", "/torrents", body=blob,
                        content_type="application/octet-stream")
    return expect(status == 200, f"加种子返回 200（实际 {status} {body[:120]}）")


def wait_shell_ready(data_dir: Path, offset: int,
                     proc: subprocess.Popen[bytes]) -> re.Match[str] | None:
    ready = wait_in_log(READY_RE, data_dir, offset, proc)
    if ready is None:
        print("  日志尾部：")
        print(read_since(log_path(data_dir), offset)[-1200:] or "（空）")
    return ready


def case_a(work: Path, blob: bytes) -> None:
    print("\nA 段：加任务 → 硬杀引擎 → 硬杀壳 → 重开")
    data_dir = work / "a"
    data_dir.mkdir()
    proc: subprocess.Popen[bytes] | None = None
    try:
        proc, offset = start_shell(data_dir)
        ready = wait_shell_ready(data_dir, offset, proc)
        if not expect(ready is not None, "A1 壳起来且前端取到数据"):
            return

        decision = wait_in_log(DECISION_RE, data_dir, offset, None, timeout=5.0)
        first_pid = int(decision.group(3)) if decision and decision.group(3) else None
        expect(decision is not None and decision.group(1) == "spawn",
               f"A1 首次启动引擎决策是 spawn（实际 {decision.group(1) if decision else '?'}）")
        expect(first_pid is not None, "A1 日志里拿到了引擎 pid")

        endpoint = wait_endpoint(data_dir)
        if not expect(endpoint is not None, "A1 读到了引擎端点"):
            return
        spawned_engines.append({**endpoint, "pid": first_pid})
        if not add_one_torrent(endpoint, blob):
            return
        before = wait_torrents(endpoint, 1)
        if not expect(len(before) == 1, f"A1 列表里有 1 条任务（实际 {len(before)}）"):
            return

        before_hash = before[0].get("infohash", "")
        before_state = before[0].get("state", "")
        before_done = int(before[0].get("done_bytes", 0))
        expect(wait_resume_file(data_dir, before_hash[:16]),
               "A1 加完任务就已经落了 resume 文件（不等退出）")

        # ---- 引擎先猝死
        if not hard_kill(first_pid, "引擎"):
            return
        expect(wait_port_closed(int(endpoint["port"])), "A2 引擎端口已关闭")

        # ---- 壳再猝死（不复用优雅退出，那会走 flush_all）
        shell_pid = proc.pid
        hard_kill(shell_pid, "壳")
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()
        proc = None

        # ---- 重开
        proc, offset2 = start_shell(data_dir)
        ready2 = wait_shell_ready(data_dir, offset2, proc)
        if not expect(ready2 is not None, "A3 重开后壳起来且前端取到数据"):
            return

        new_endpoint = wait_endpoint(data_dir)
        if not expect(new_endpoint is not None, "A3 重开后读到了引擎端点"):
            return
        decision2 = wait_in_log(DECISION_RE, data_dir, offset2, None, timeout=5.0)
        second_pid = int(decision2.group(3)) if decision2 and decision2.group(3) else None
        spawned_engines.append({**new_endpoint, "pid": second_pid})

        after = wait_torrents(new_endpoint, 1)
        expect(len(after) == 1,
               f"A3 重开后任务还在（实际 {len(after)} 条，硬杀前是 1 条）")
        if not after:
            return

        after_hash = after[0].get("infohash", "")
        expect(after_hash == before_hash, "A3 是同一个任务（infohash 一致）")
        expect(after[0].get("state", "") == before_state,
               f"A3 状态与硬杀前一致（前 {before_state!r}，后 {after[0].get('state')!r}）")

        # 进度这里**不断言**。本地种子没有 peer，done_bytes 前后都是 0，
        # 「0 >= 0」永远成立 —— 那是假装测过，比不测更坏。
        # 真正的「进度不倒退」需要一份有 peer 的种子，目前没有覆盖（已记入未验证）。
        print(f"  · A3 进度 {before_done} → {after[0].get('done_bytes')}"
              f"（无 peer，恒为 0，不作断言）")

        expect(decision2 is not None and decision2.group(1) != "reuse",
               f"A3 重开时没有误判 reuse（实际 {decision2.group(1) if decision2 else '?'}）"
               f" —— 旧引擎确实死了")
        second_pid = int(decision2.group(3)) if decision2.group(3) else None
        expect(second_pid is not None and second_pid != first_pid,
               f"A3 引擎是新进程（前 {first_pid}，后 {second_pid}）—— 证明任务是从磁盘恢复的，"
               f"不是旧引擎还在内存里")
    finally:
        if proc is not None and proc.poll() is None:
            proc.kill()


def case_b(work: Path, blob: bytes) -> None:
    print("\nB 段：加任务 → 只杀壳（引擎成孤儿）→ 立刻重开")
    data_dir = work / "b"
    data_dir.mkdir()
    proc: subprocess.Popen[bytes] | None = None
    try:
        proc, offset = start_shell(data_dir)
        ready = wait_shell_ready(data_dir, offset, proc)
        if not expect(ready is not None, "B1 壳起来且前端取到数据"):
            return

        decision = wait_in_log(DECISION_RE, data_dir, offset, None, timeout=5.0)
        first_pid = int(decision.group(3)) if decision and decision.group(3) else None
        if not expect(first_pid is not None, "B1 日志里拿到了引擎 pid"):
            return

        endpoint = wait_endpoint(data_dir)
        if not expect(endpoint is not None, "B1 读到了引擎端点"):
            return
        spawned_engines.append({**endpoint, "pid": first_pid})
        if not add_one_torrent(endpoint, blob):
            return
        before = wait_torrents(endpoint, 1)
        if not expect(len(before) == 1, f"B1 列表里有 1 条任务（实际 {len(before)}）"):
            return
        before_hash = before[0].get("infohash", "")

        # ---- 只杀壳，引擎不动：这正是「任务管理器结束 BT 客户端」的样子
        hard_kill(proc.pid, "壳")
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()
        proc = None
        expect(port_open(int(endpoint["port"])), "B2 壳死了但引擎还活着（孤儿状态成立）")

        # ---- 立刻重开
        proc, offset2 = start_shell(data_dir)
        ready2 = wait_shell_ready(data_dir, offset2, proc)
        if not expect(ready2 is not None, "B3 重开后壳正常打开（不是「点了图标没反应」）"):
            print("  日志尾部：")
            print(read_since(log_path(data_dir), offset2)[-1200:] or "（空）")
            return

        decision2 = wait_in_log(DECISION_RE, data_dir, offset2, None, timeout=5.0)
        if not expect(decision2 is not None, "B3 日志里有引擎决策行"):
            return
        expect(decision2.group(1) == "reuse",
               f"B3 决策是 reuse 而不是 respawn（实际 {decision2.group(1)}）")
        second_pid = int(decision2.group(3)) if decision2.group(3) else None
        expect(second_pid == first_pid,
               f"B3 接管的是同一个引擎（pid {first_pid} → {second_pid}），没有另起一个")

        after = wait_torrents(endpoint, 1)
        expect(len(after) == 1, f"B3 任务还在（实际 {len(after)} 条）")
        if after:
            expect(after[0].get("infohash", "") == before_hash, "B3 是同一个任务（infohash 一致）")
    finally:
        if proc is not None and proc.poll() is None:
            proc.kill()


def main() -> int:
    if not EXE.exists():
        print(f"找不到 {EXE}")
        print("先跑：python toolchain/build.py shell build --release")
        return 4

    stale = stale_engine()
    if stale is not None:
        print(f"✗ {stale}")
        return 4

    work = Path(tempfile.mkdtemp(prefix="shell-restart-"))
    try:
        downloads = work / "seed"
        downloads.mkdir()
        (downloads / "seed.bin").write_bytes(PAYLOAD)
        _infohash, blob = mktorrent.build(PAYLOAD, "seed.bin")

        case_a(work, blob)
        case_b(work, blob)
    finally:
        cleanup_engines()
        shutil.rmtree(work, ignore_errors=True)

    print()
    if problems:
        for problem in problems:
            print(f"  ✗ {problem}")
        print(f"失败 {len(problems)} 项")
        return 1
    print("通过：任务活过了引擎猝死与壳猝死，壳能接管孤儿引擎")
    return 0


if __name__ == "__main__":
    sys.exit(main())
