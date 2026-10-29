"""构造合法的 .torrent 文件，供端到端测试使用。

为什么不用真种子：AC-08 需要「校验通过 → 存 resume → 重启 → 回到 seeding」
这一整条链路可重复，用公网种子会引入网络与时效依赖。自己造一边完全确定，
一边还能顺带验证 infohash 计算与分片哈希无关的部分。
"""

from __future__ import annotations

import hashlib


def bencode(obj) -> bytes:
    if isinstance(obj, bool):
        raise TypeError("bencode 不接受 bool")
    if isinstance(obj, int):
        return b"i" + str(obj).encode() + b"e"
    if isinstance(obj, str):
        return bencode(obj.encode("utf-8"))
    if isinstance(obj, bytes):
        return str(len(obj)).encode() + b":" + obj
    if isinstance(obj, list):
        return b"l" + b"".join(bencode(x) for x in obj) + b"e"
    if isinstance(obj, dict):
        items = sorted((k.encode("utf-8") if isinstance(k, str) else k, v)
                       for k, v in obj.items())
        return b"d" + b"".join(bencode(k) + bencode(v) for k, v in items) + b"e"
    raise TypeError(f"bencode 不支持的类型 {type(obj)!r}")


def build(payload: bytes, name: str, piece_length: int = 16384,
          announce: str = "http://127.0.0.1:1/announce") -> tuple[str, bytes]:
    """返回 (infohash hex, .torrent 内容)。"""
    if not payload:
        raise ValueError("payload 不能为空")
    pieces = b"".join(
        hashlib.sha1(payload[i:i + piece_length]).digest()
        for i in range(0, len(payload), piece_length))
    info = {
        "length": len(payload),
        "name": name,
        "piece length": piece_length,
        "pieces": pieces,
    }
    infohash = hashlib.sha1(bencode(info)).hexdigest()
    return infohash, bencode({"announce": announce, "info": info})
