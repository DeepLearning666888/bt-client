"""AC-04 带令牌 GET /health 返回 200 且响应体为合法 JSON。"""

import json

import common as c


def main() -> None:
    engine = c.Engine().start()
    try:
        status, body = engine.raw("GET", "/health")
        c.check_eq(status, 200, "状态码")
        try:
            data = json.loads(body)
        except json.JSONDecodeError as exc:
            c.check(False, f"响应体不是合法 JSON：{exc}")
            return
        c.check(True, "响应体可被 json 解析")
        c.check(data.get("ok") is True, "ok 为 true")
        c.check(isinstance(data.get("version"), str) and data["version"] != "",
                "version 是非空字符串")
        c.check(isinstance(data.get("pid"), int) and data["pid"] > 0, "pid 是正整数")
    finally:
        engine.stop()
    c.finish()


if __name__ == "__main__":
    main()
