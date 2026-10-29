"""AC-13 跨源请求：预检与响应头。

为什么这条必须存在：桌面壳的前端跑在 `http://tauri.localhost`，浏览器开发形态
跑在 `http://localhost:5173`，引擎在 `http://127.0.0.1:<port>` —— 三者都不同源。
缺 CORS 头时浏览器会在拿到响应之前就丢弃它，**引擎侧什么日志都不会有**
（预检都没到），现场表现是「窗口打开了，但界面永远转圈」。

桌面端的单元测试发现不了这件事：jsdom 不做同源检查。M2a 的四条性能约束
全部跑在 jsdom 里，所以这个缺陷一路活到了装壳的那一天。

预检按规范不带凭证，所以它必须在认证之前放行 —— 被 401 拦下同样等于放弃请求。
"""

from __future__ import annotations

import common as c

ORIGIN = b"http://tauri.localhost"


def main() -> None:
    engine = c.Engine().start()
    try:
        port = engine.port

        # ---- ① 预检：必须放行，且不能要求令牌
        preflight = c.raw_socket_request(
            port,
            b"OPTIONS /state HTTP/1.1\r\n"
            b"Host: 127.0.0.1\r\n"
            b"Origin: " + ORIGIN + b"\r\n"
            b"Access-Control-Request-Method: GET\r\n"
            b"Access-Control-Request-Headers: authorization\r\n"
            b"\r\n",
        )
        c.check_eq(c.status_line(preflight), 204, "预检状态码（不能是 401/405）")

        text = preflight.decode("latin-1")
        c.check("access-control-allow-origin: *" in text.lower(), "预检允许任意 Origin")
        c.check("access-control-allow-headers" in text.lower(), "预检声明了允许的请求头")
        c.check("authorization" in text.lower(), "预检放行 Authorization 头")
        c.check("access-control-allow-methods" in text.lower(), "预检声明了允许的方法")
        c.check("get" in text.lower() and "post" in text.lower(), "预检覆盖 GET 与 POST")
        # 204 不该带正文；带了虽然多数浏览器能容忍，但不符合规范
        c.check(b"\r\n\r\n" in preflight and not preflight.split(b"\r\n\r\n", 1)[1],
                "204 预检响应没有正文")

        # ---- ② 真实请求同样要带跨源头，否则浏览器只放行预检、仍旧丢弃响应
        authorized = c.raw_socket_request(
            port,
            b"GET /health HTTP/1.1\r\n"
            b"Host: 127.0.0.1\r\n"
            b"Origin: " + ORIGIN + b"\r\n"
            b"Authorization: Bearer " + engine.token.encode() + b"\r\n"
            b"\r\n",
        )
        c.check_eq(c.status_line(authorized), 200, "带 Origin 的正常请求状态码")
        c.check("access-control-allow-origin: *" in authorized.decode("latin-1").lower(),
                "正常响应也带跨源许可")

        # ---- ③ 错误响应也要带：否则前端拿不到失败的细节，只能看到 network error
        #      401 是最常见的分支（令牌对不上），它被浏览器吞掉时最难排查
        unauthorized = c.raw_socket_request(
            port,
            b"GET /health HTTP/1.1\r\n"
            b"Host: 127.0.0.1\r\n"
            b"Origin: " + ORIGIN + b"\r\n"
            b"Authorization: Bearer wrong-token\r\n"
            b"\r\n",
        )
        c.check_eq(c.status_line(unauthorized), 401, "错误令牌仍是 401")
        c.check("access-control-allow-origin: *" in unauthorized.decode("latin-1").lower(),
                "401 响应也带跨源许可（前端才看得到真实错误）")

        # ---- ④ 预检不能被当成「什么都能干」，未知方法仍然拒绝
        c.check_eq(
            c.status_line(c.raw_socket_request(
                port,
                b"PUT /health HTTP/1.1\r\nHost: 127.0.0.1\r\n"
                b"Authorization: Bearer " + engine.token.encode() + b"\r\n\r\n",
            )),
            405,
            "PUT 仍然 405（CORS 不该放宽方法白名单）",
        )
    finally:
        engine.stop()
    c.finish()


if __name__ == "__main__":
    main()
