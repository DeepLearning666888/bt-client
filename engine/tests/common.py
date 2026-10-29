"""引擎测试公共层。

约定：每个 t_*.py 自起自停，外部只看退出码。这样 AC 才能保持
「一条命令可判定」的形态，交给 gate.py 时不需要额外的编排。
"""

from __future__ import annotations

import json
import os
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
EXE = ROOT / "engine" / "build" / "engine.exe"

_failures: list[str] = []
_checks = 0


def check(cond: bool, label: str) -> bool:
    global _checks
    _checks += 1
    if cond:
        print(f"  [ ok ] {label}")
        return True
    print(f"  [FAIL] {label}")
    _failures.append(label)
    return False


def check_eq(actual, expected, label: str) -> bool:
    return check(actual == expected, f"{label}（期望 {expected!r}，实际 {actual!r}）")


def finish() -> None:
    print(f"\n{_checks} 项检查，{len(_failures)} 项失败")
    if _failures:
        for f in _failures:
            print(f"  失败：{f}")
        sys.exit(1)
    sys.exit(0)


class EngineError(RuntimeError):
    pass


class Engine:
    """启动一个引擎实例，退出时保证进程被回收。"""

    def __init__(self, data_dir: str | os.PathLike | None = None, no_network: bool = True,
                 http_port: int = 0, keep_data: bool = False):
        self._tmp = data_dir is None and not keep_data
        self.data_dir = Path(data_dir) if data_dir else Path(tempfile.mkdtemp(prefix="eng-"))
        self.no_network = no_network
        self.http_port = http_port
        self.proc: subprocess.Popen | None = None
        self.port = 0
        self.token = ""

    # ---------------------------------------------------------------- 生命周期
    def start(self, timeout: float = 20.0) -> "Engine":
        if not EXE.exists():
            raise EngineError(f"找不到引擎可执行文件 {EXE}，请先构建")
        args = [str(EXE), "--data-dir", str(self.data_dir), "--http-port", str(self.http_port)]
        if self.no_network:
            args.append("--no-network")
        self.proc = subprocess.Popen(
            args, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

        endpoint = self.data_dir / "engine.endpoint.json"
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.proc.poll() is not None:
                out = self.proc.stdout.read().decode("utf-8", "replace") if self.proc.stdout else ""
                err = self.proc.stderr.read().decode("utf-8", "replace") if self.proc.stderr else ""
                raise EngineError(f"引擎提前退出 code={self.proc.returncode}\nstdout={out}\nstderr={err}")
            if endpoint.exists():
                try:
                    info = json.loads(endpoint.read_text(encoding="utf-8"))
                    self.port = int(info["port"])
                    self.token = info["token"]
                    # 发现文件写了不代表已经能收请求，补一次健康检查
                    status, _ = self.raw("GET", "/health", timeout=2.0)
                    if status == 200:
                        return self
                except (json.JSONDecodeError, KeyError, OSError):
                    pass
            time.sleep(0.05)
        raise EngineError("等待引擎就绪超时")

    def shutdown(self, timeout: float = 12.0) -> int:
        if self.proc is None:
            return 0
        try:
            self.raw("POST", "/shutdown", timeout=2.0)
        except OSError:
            pass
        try:
            return self.proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait(timeout=5)
            return -1

    def stop(self) -> None:
        if self.proc is not None and self.proc.poll() is None:
            self.shutdown()
        if self.proc is not None:
            for stream in (self.proc.stdout, self.proc.stderr):
                if stream:
                    stream.close()
            self.proc = None
        if self._tmp:
            shutil.rmtree(self.data_dir, ignore_errors=True)

    def __enter__(self) -> "Engine":
        return self.start()

    def __exit__(self, *exc) -> None:
        self.stop()

    # -------------------------------------------------------------------- 请求
    def raw(self, method: str, path: str, body: bytes | None = None,
            extra_headers: dict[str, str] | None = None, token: str | None = None,
            timeout: float = 5.0) -> tuple[int, str]:
        headers = {"Authorization": f"Bearer {token if token is not None else self.token}"}
        if extra_headers:
            headers.update(extra_headers)
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}", data=body, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.status, resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode("utf-8", "replace")

    def get(self, path: str, **kw) -> tuple[int, str]:
        return self.raw("GET", path, **kw)

    def state(self, timeout: float = 5.0) -> dict:
        status, body = self.raw("GET", "/state", timeout=timeout)
        if status != 200:
            raise EngineError(f"/state 返回 {status}: {body[:200]}")
        return json.loads(body)

    def add_magnet(self, magnet: str) -> tuple[int, dict]:
        status, body = self.raw(
            "POST", "/torrents", json.dumps({"magnet": magnet}).encode(),
            {"Content-Type": "application/json"})
        try:
            return status, json.loads(body)
        except json.JSONDecodeError:
            return status, {"raw": body}

    def add_torrent_file(self, path: Path) -> tuple[int, dict]:
        status, body = self.raw(
            "POST", "/torrents", path.read_bytes(),
            {"Content-Type": "application/octet-stream"})
        try:
            return status, json.loads(body)
        except json.JSONDecodeError:
            return status, {"raw": body}

    def delete(self, infohash: str, delete_files: bool = False) -> tuple[int, str]:
        q = "?delete_files=1" if delete_files else ""
        return self.raw("DELETE", f"/torrents/{infohash}{q}")

    def action(self, infohash: str, name: str) -> tuple[int, str]:
        return self.raw("POST", f"/torrents/{infohash}/{name}")

    # ------------------------------------------------------------------ 等待
    def wait_state(self, infohash: str, wanted: set[str], timeout: float = 20.0) -> dict | None:
        """轮询直到该任务的 state 落入 wanted，返回命中或最后一次看到的任务对象。

        注意「还没出现」不等于「已消失」：早期版本在这里提前返回 None，
        导致刚添加完就查询的测试全部假失败。
        """
        deadline = time.time() + timeout
        last = None
        while time.time() < deadline:
            snap = self.state()
            match = next((t for t in snap.get("torrents", []) if t["infohash"] == infohash), None)
            if match is not None:
                last = match
                if match["state"] in wanted:
                    return match
            time.sleep(0.1)
        return last

    def find(self, infohash: str) -> dict | None:
        for t in self.state().get("torrents", []):
            if t["infohash"] == infohash:
                return t
        return None


# ------------------------------------------------------------------ 工具函数
def raw_socket_request(port: int, payload: bytes, timeout: float = 5.0,
                       read_all: bool = True) -> bytes:
    """不经 urllib，直接发字节，用于构造畸形请求。"""
    with socket.create_connection(("127.0.0.1", port), timeout=timeout) as s:
        try:
            s.sendall(payload)
        except OSError:
            # 服务端可能在我们发完之前就回了 4xx 并关闭连接，这不是测试失败
            pass
        chunks = []
        try:
            while True:
                data = s.recv(65536)
                if not data:
                    break
                chunks.append(data)
                if not read_all:
                    break
        except socket.timeout:
            pass
    return b"".join(chunks)


def status_line(response: bytes) -> int:
    """从原始响应里取状态码，取不到返回 0。"""
    if not response:
        return 0
    first = response.split(b"\r\n", 1)[0].decode("latin-1", "replace")
    parts = first.split(" ")
    if len(parts) < 2 or not parts[1].isdigit():
        return 0
    return int(parts[1])


def local_ipv4() -> str | None:
    """取本机对外网卡的 IPv4，用于验证引擎没有监听在非回环地址上。"""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
            return None if ip.startswith("127.") else ip
    except OSError:
        return None


def fake_magnet(infohash_hex: str, name: str = "synthetic") -> str:
    return f"magnet:?xt=urn:btih:{infohash_hex}&dn={name}"


def fake_infohash(seed: int) -> str:
    """确定性合成 infohash：不需要外网，也不依赖随机源。"""
    import hashlib
    return hashlib.sha1(f"synthetic-{seed}".encode()).hexdigest()
