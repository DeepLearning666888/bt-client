"""AC-10 引擎只监听回环，从局域网地址连不上。

如果没有可用的非回环地址，这条标准无法判定 —— 宁可显式失败，
也不要在一片绿里假装验证过。
"""

import socket

import common as c


def main() -> None:
    engine = c.Engine().start()
    try:
        ip = c.local_ipv4()
        if ip is None:
            c.check(False, "本机没有非回环 IPv4，无法验证监听范围（不是通过，是无法判定）")
            return
        print(f"  使用本机局域网地址 {ip} 尝试连接端口 {engine.port}")

        connected = False
        try:
            with socket.create_connection((ip, engine.port), timeout=3):
                connected = True
        except OSError as exc:
            print(f"  连接失败（预期）：{exc.__class__.__name__}")

        c.check(not connected, f"从 {ip}:{engine.port} 连接未被接受")
        c.check_eq(engine.raw("GET", "/health")[0], 200, "回环地址仍可用")
    finally:
        engine.stop()
    c.finish()


if __name__ == "__main__":
    main()
