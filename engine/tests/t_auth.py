"""AC-03 无令牌或错误令牌一律 401，/health 不设例外。"""

import common as c


def main() -> None:
    engine = c.Engine().start()
    try:
        raw = c.raw_socket_request(engine.port, b"GET /health HTTP/1.1\r\nHost: x\r\n\r\n")
        c.check_eq(c.status_line(raw), 401, "缺少 Authorization 头")

        c.check_eq(engine.raw("GET", "/health", token="0" * 32)[0], 401, "错误令牌")
        c.check_eq(engine.raw("GET", "/health", token="")[0], 401, "空令牌")

        status, _ = engine.raw("GET", "/health",
                               extra_headers={"Authorization": "Basic dXNlcjpwdw=="})
        c.check_eq(status, 401, "错误认证方案")

        c.check_eq(engine.raw("GET", "/state", token="1" * 32)[0], 401, "/state 同样强制鉴权")
        c.check_eq(engine.raw("GET", "/health")[0], 200, "正确令牌放行")
    finally:
        engine.stop()
    c.finish()


if __name__ == "__main__":
    main()
