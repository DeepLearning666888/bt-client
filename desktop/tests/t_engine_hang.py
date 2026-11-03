"""引擎假死：端口开着、连接建得上、就是永远不回数据。

为什么单独立一条，而不是并进 t_engine_crash.py：
  - 崩溃是「进程没了」，假死是「进程在，但不响应」。两者的前端代码路径不同 ——
    崩溃走 fetch 立即 reject，假死走超时分支。只测前者的话，超时分支等于没验。
  - 假死的判据里有一个「耗时」维度：必须真的等了 STATE_TIMEOUT_MS，而不是
    碰巧快速失败。这条只能靠墙钟时间验。

怎么造假死：起一个裸 TCP 监听，accept 之后一个字节都不发。HTTP 客户端发完请求
就等响应头，因此会一直挂着 —— 和真实引擎被调试器挂住、卡在超长阻塞调用里的
表现一致。用 Python 标准库起，不需要外部工具。

前端用**默认的 source**（真 fetch），不是注入的桩：要验的正是「AbortSignal 能不能
真的取消一个真实的 undici 请求」。桩取消了不算数。

由 ENGINE_HANG_URL / ENGINE_HANG_TOKEN 注入；没有变量时前端用例自动跳过。
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "engine" / "tests"))

import common as c  # noqa: E402


class HangServer:
    """接受连接，但永不响应。每条连接留一个线程持有，不关闭。"""

    def __init__(self) -> None:
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(8)
        self.port = self.sock.getsockname()[1]
        self._held: list[socket.socket] = []
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        self.sock.settimeout(0.2)
        while not self._stop.is_set():
            try:
                conn, _ = self.sock.accept()
            except (TimeoutError, OSError):
                continue
            # 收下连接就握住，不给任何响应字节
            self._held.append(conn)

    def close(self) -> None:
        self._stop.set()
        self._thread.join(timeout=2.0)
        for conn in self._held:
            try:
                conn.close()
            except OSError:
                pass
        try:
            self.sock.close()
        except OSError:
            pass


def main() -> None:
    server = HangServer()
    try:
        print(f"  假死服务监听 127.0.0.1:{server.port}（收连接，永不响应）")

        # 先自证「它确实会挂住」：一个普通请求在两秒内不该拿到任何响应。
        # 这一步不成立的话，下面的超时用例就是在一个假前提上跑。
        probe = socket.create_connection(("127.0.0.1", server.port), timeout=2.0)
        try:
            probe.sendall(b"GET /health HTTP/1.1\r\nHost: x\r\n\r\n")
            probe.settimeout(1.0)
            try:
                data = probe.recv(64)
            except (TimeoutError, OSError):
                data = b""
            c.check(data == b"", "假死服务确实不回任何数据")
        finally:
            probe.close()

        env = dict(os.environ)
        env["ENGINE_HANG_URL"] = f"http://127.0.0.1:{server.port}"
        env["ENGINE_HANG_TOKEN"] = "hang-token"

        print("\n  交给前端验超时分支：")
        result = subprocess.run(
            [sys.executable, "toolchain/build.py", "desktop", "--test", "engine-hang"],
            cwd=str(ROOT), env=env, capture_output=True, text=True,
        )
        tail = (result.stdout + result.stderr).strip().splitlines()
        for line in tail[-15:]:
            print("   ", line)
        c.check(result.returncode == 0, f"假死用例退出码 0（实际 {result.returncode}）")
    finally:
        server.close()

    c.finish()


if __name__ == "__main__":
    main()
