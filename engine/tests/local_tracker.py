"""最小可用 HTTP tracker，只为一个目的存在：让同机两个引擎互相发现。

为什么不用 LSD 或公网 tracker：

    LSD 是 UDP 组播，同机多进程能不能互通取决于网卡与防火墙，判据会随机器
    漂移；公网 tracker 则把确定性交给外部（real_swarm.py 已明确因此不进卡点）。
    HTTP tracker 是进程内的，起停都由测试控制，失败能指到具体环节。

为什么不能靠 add_peer 直连：

    引擎的 BT 监听口是内核分配的（listen_interfaces="0.0.0.0:0"），既没有参数
    也没有接口能读到实际端口。所以「谁知道对方端口」这个问题只能由 tracker
    回答 —— 它从 announce 请求里天然拿到。

只实现 announce 的最小集：协议版本、key、numwant 等一概不看。返回 interleaved
以外的唯一格式是 compact peers（BEP 23），libtorrent 默认就吃这个。
"""

from __future__ import annotations

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote_to_bytes


def _bencode(obj) -> bytes:
    if isinstance(obj, int) and not isinstance(obj, bool):
        return b"i" + str(obj).encode() + b"e"
    if isinstance(obj, bytes):
        return str(len(obj)).encode() + b":" + obj
    if isinstance(obj, dict):
        items = sorted((k.encode() if isinstance(k, str) else k, v)
                       for k, v in obj.items())
        return b"d" + b"".join(_bencode(k) + _bencode(v) for k, v in items) + b"e"
    raise TypeError(f"bencode 不支持 {type(obj)!r}")


def _parse_query(raw: str) -> dict[str, bytes]:
    """query 里 info_hash / peer_id 是 URL 编码的原始字节，不能当文本解。"""
    out: dict[str, bytes] = {}
    for part in raw.split("&"):
        if not part:
            continue
        key, _, value = part.partition("=")
        out[key] = unquote_to_bytes(value)
    return out


def _compact(ip: str, port: int) -> bytes:
    return bytes(int(x) for x in ip.split(".")) + port.to_bytes(2, "big")


class LocalTracker:
    """进程内 tracker。用法：with LocalTracker() as t: ... t.announce_url"""

    def __init__(self, interval: int = 2, peer_port_offset: int = 0) -> None:
        self.interval = interval
        # 只在负向验证里用：给下发的 peer 端口加上偏移，制造「会合失败」。
        # 默认 0，正常运行不改变任何行为。
        self.peer_port_offset = peer_port_offset
        self.lock = threading.Lock()
        # info_hash -> {(ip, port): peer_id}
        self._peers: dict[bytes, dict[tuple[str, int], bytes]] = {}
        self.announce_count = 0
        self.peer_events: list[tuple[str, str, int]] = []  # (info_hash 前 8 位, 事件, 端口)
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    # ---- 生命周期 -------------------------------------------------------
    def __enter__(self) -> "LocalTracker":
        self.start()
        return self

    def __exit__(self, *exc) -> None:
        self.stop()

    def start(self) -> None:
        tracker = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args) -> None:  # 默认会往 stderr 刷，测试里是噪音
                pass

            def do_GET(self) -> None:
                if not self.path.startswith("/announce"):
                    self._reply(404, b"not found")
                    return
                _, _, raw = self.path.partition("?")
                query = _parse_query(raw)
                info_hash = query.get("info_hash", b"")
                if len(info_hash) != 20:
                    self._reply(200, _bencode({"failure reason": b"bad info_hash"}))
                    return
                peer_id = query.get("peer_id", b"")
                try:
                    port = int(query.get("port", b"0"))
                except ValueError:
                    port = 0
                event = query.get("event", b"").decode("latin-1") or "regular"
                ip = self.client_address[0]

                with tracker.lock:
                    tracker.announce_count += 1
                    tracker.peer_events.append((info_hash.hex()[:8], event, port))
                    table = tracker._peers.setdefault(info_hash, {})
                    if event == "stopped":
                        table.pop((ip, port), None)
                    elif port:
                        table[(ip, port)] = peer_id
                    others = [(_ip, _port) for (_ip, _port) in table
                              if (_ip, _port) != (ip, port)]

                payload = {
                    "interval": tracker.interval,
                    "peers": b"".join(
                        _compact(_ip, _port + tracker.peer_port_offset)
                        for _ip, _port in others),
                }
                self._reply(200, _bencode(payload))

            def _reply(self, code: int, body: bytes) -> None:
                self.send_response(code)
                self.send_header("Content-Type", "text/plain")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None

    # ---- 状态 -----------------------------------------------------------
    @property
    def port(self) -> int:
        assert self._server is not None, "tracker 还没启动"
        return self._server.server_address[1]

    @property
    def announce_url(self) -> str:
        return f"http://127.0.0.1:{self.port}/announce"

    def peer_ports(self) -> list[int]:
        with self.lock:
            return [port for table in self._peers.values() for (_ip, port) in table]
