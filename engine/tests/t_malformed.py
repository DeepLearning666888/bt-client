"""AC-11 畸形请求返回 4xx 且引擎不崩溃。

四类都走原始 socket，因为 urllib 不会发出这些形状的请求。
每一条之后都要确认引擎还活着——只回 4xx 但进程死了不算通过。
"""

import common as c


def main() -> None:
    engine = c.Engine().start()
    try:
        auth = f"Authorization: Bearer {engine.token}\r\n".encode()
        cases = [
            ("超长请求头", b"GET /health HTTP/1.1\r\n" + auth +
             b"X-Pad: " + b"a" * 20000 + b"\r\n\r\n", (400, 431)),
            ("Content-Length 非数字", b"POST /torrents HTTP/1.1\r\n" + auth +
             b"Content-Length: abc\r\n\r\n", (400,)),
            ("分块传输", b"POST /torrents HTTP/1.1\r\n" + auth +
             b"Transfer-Encoding: chunked\r\n\r\n0\r\n\r\n", (411,)),
            ("超大 body 声明", b"POST /torrents HTTP/1.1\r\n" + auth +
             b"Content-Length: 999999999\r\n\r\n", (413,)),
        ]

        for label, payload, expected in cases:
            response = c.raw_socket_request(engine.port, payload)
            code = c.status_line(response)
            c.check(code in expected, f"{label} 返回 {code}（期望 {expected}）")
            c.check(engine.proc.poll() is None, f"{label} 之后进程仍存活")

        c.check_eq(engine.raw("GET", "/health")[0], 200, "全部畸形请求之后 /health 仍 200")

        # 未授权 + 畸形：鉴权在读取请求体之前，所以这里必须是 401 而不是 413
        response = c.raw_socket_request(
            engine.port, b"POST /torrents HTTP/1.1\r\nContent-Length: 999999999\r\n\r\n")
        c.check_eq(c.status_line(response), 401, "未授权的大 body 请求先被鉴权拦下")
    finally:
        engine.stop()
    c.finish()


if __name__ == "__main__":
    main()
