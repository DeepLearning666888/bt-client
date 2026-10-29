#!/usr/bin/env python3
"""AC-14：非 ASCII 数据目录下端点文件必须是合法 UTF-8。

## 这条测的是什么

Windows 上 `main(int, char**)` 拿到的参数是系统 ANSI 代码页（简中为 GBK）编码的字节串，
不是 UTF-8。进程内部用 ANSI 是对的 —— `std::filesystem` 的窄字符串在 Windows 上也按
ANSI 解读，两者一致，目录能正常创建。但只要把这些字节**原样写进 JSON** 就错了：
JSON 要求 UTF-8，接收方按 UTF-8 解码会直接失败。

失败链条长这样，而且每一环都不报错：

    engine 把 GBK 字节写进 endpoint.json
      → 壳用 read_to_string 读（要求合法 UTF-8）→ 返回 Err
      → 判定「端点不存在」→ 重新拉起 → 超时 20s
      → 用户看到「应用打开了但一直转圈」，两侧日志都没有错误信息

## 为什么既有测试一条都没抓到

所有测试的数据目录都是 `tempfile.mkdtemp()` 生成的纯 ASCII 路径，
开发态的 `%APPDATA%\\com.btclient.desktop` 也是纯 ASCII。
中文 Windows 的用户名目录（`C:\\Users\\张三\\...`）才是真实场景，而它恰好从未被测过。

## 判据

不只是「能启动」—— `Engine.start()` 本身就要求端点文件能按 UTF-8 解析，
所以单靠启动成功会漏掉「端口令牌读到了但路径字段坏了」这种部分损坏。这里逐字段核对。
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
from pathlib import Path

from common import Engine, EngineError, check, finish


def verify(label: str, data_dir: Path) -> None:
    """在一个指定名字的目录下跑一遍，核对端点文件编码与内容。"""
    print(f"\n--- {label}：{data_dir}")

    with Engine(data_dir=data_dir, no_network=True) as eng:
        check(eng.port > 0, "引擎启动并监听了端口")
        status, body = eng.raw("GET", "/health", timeout=2.0)
        check(status == 200, f"/health 返回 200（实际 {status}）")

        endpoint_path = data_dir / "engine.endpoint.json"
        raw = endpoint_path.read_bytes()

        # ① 整文件必须是合法 UTF-8。这一条就是缺陷本身的判据。
        try:
            text = raw.decode("utf-8")
            check(True, "端点文件是合法 UTF-8")
        except UnicodeDecodeError as exc:
            check(False, f"端点文件是合法 UTF-8（解码失败：{exc}）")
            print(f"       原始字节片段：{raw[160:220]!r}")
            return

        # ② JSON 结构完整
        try:
            info = json.loads(text)
            check(True, "端点文件是合法 JSON")
        except json.JSONDecodeError as exc:
            check(False, f"端点文件是合法 JSON（{exc}）")
            return

        # ③ data_dir 字段必须逐字符等于真实路径。
        #    单看「合法 UTF-8」不够：如果转换时用错代码页，得到的仍可能是合法 UTF-8，
        #    只是内容已经变成了乱码。
        recorded = info.get("data_dir")
        check(recorded == str(data_dir),
              f"data_dir 与真实路径一致（记录 {recorded!r}，期望 {str(data_dir)!r}）")

        # ④ 引擎确实在这个目录下建好了工作子目录 —— 证明 ANSI 处理链路本身也是通的
        for sub in ("downloads", "resume", "tmp"):
            check((data_dir / sub).is_dir(), f"子目录 {sub}/ 已创建")


def main() -> int:
    root = Path(tempfile.mkdtemp(prefix="enc-"))
    try:
        # 只含中文：最容易踩 GBK 双字节边界
        verify("纯中文", root / "中文数据目录")
        # 中文 + 空格：命令行参数解析与 ANSI 转换同时受压
        verify("中文与空格", root / "中文 数据 目录")
        # 混合符号：括号、下划线、连字符都不该被特殊对待
        verify("混合符号", root / "数据(1)_a-b")
    except EngineError as exc:
        check(False, f"引擎未能启动：{exc}")
    finally:
        shutil.rmtree(root, ignore_errors=True)

    finish()
    return 0


if __name__ == "__main__":
    sys.exit(main())
