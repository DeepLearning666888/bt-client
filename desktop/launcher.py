#!/usr/bin/env python3
"""引擎发现与拉起。

这个文件有两种身份：
  1. 现在：开发入口。拉起引擎并把 URL/TOKEN 打印成环境变量，供 vite 使用。
  2. M2b：Tauri 壳沿用同一套判断逻辑（读 endpoint 文件 → 查 /health → 必要时 spawn）。

三种情形必须分开处理，它们对应真实的失败模式：
  1. 没有 endpoint 文件          → 拉起
  2. 有文件且 /health 通         → 复用（盲目再拉一个会被单实例锁顶掉，退出码 2）
  3. 有文件但 /health 不通        → 上次异常退出留下的残留，删掉再拉起。
                                   只读文件不查健康，就会连到一个已经不存在的端口。

用法：
    python desktop/launcher.py --print-env
    python desktop/launcher.py --status
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ENGINE_EXE = ROOT / "engine" / "build" / "engine.exe"
ENDPOINT_NAME = "engine.endpoint.json"
SPAWN_TIMEOUT = 20.0


def endpoint_path(data_dir: Path) -> Path:
    return data_dir / ENDPOINT_NAME


def read_endpoint(data_dir: Path) -> dict | None:
    """只读文件，不判断可用性。返回含 port/token 的字典或 None。"""
    path = endpoint_path(data_dir)
    if not path.exists():
        return None
    try:
        info = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    if not isinstance(info, dict) or "port" not in info or "token" not in info:
        return None
    return info


def request(info: dict, method: str, path: str, body: bytes | None = None,
            timeout: float = 2.0) -> tuple[int, str]:
    req = urllib.request.Request(
        f"http://127.0.0.1:{int(info['port'])}{path}",
        data=body,
        method=method,
        headers={"Authorization": f"Bearer {info['token']}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")


def health_ok(info: dict, timeout: float = 1.5) -> bool:
    try:
        status, _ = request(info, "GET", "/health", timeout=timeout)
        return status == 200
    except (urllib.error.URLError, OSError, ValueError):
        return False


def discover(data_dir: Path) -> dict | None:
    """返回可用的端点信息；文件缺失、损坏或健康检查失败都返回 None。"""
    info = read_endpoint(data_dir)
    if info is None:
        return None
    return info if health_ok(info) else None


def spawn(data_dir: Path, engine_exe: Path = ENGINE_EXE,
          no_network: bool = False,
          extra_args: list[str] | None = None) -> dict:
    """起一个引擎并等到能收请求。

    `extra_args` 原样追加到命令行。用途是让测试缩短那些「等 30 秒」的窗口
    （例如 `--periodic-save-ms 200`），而不是在代码里留一个测试专用的开关。
    """
    if not engine_exe.exists():
        raise FileNotFoundError(f"找不到引擎 {engine_exe}，先跑 python toolchain/build.py engine")

    data_dir.mkdir(parents=True, exist_ok=True)
    # 残留文件会让下面的等待循环立刻误判为「已就绪」，先清掉
    stale = endpoint_path(data_dir)
    if stale.exists():
        stale.unlink()

    args = [str(engine_exe), "--data-dir", str(data_dir), "--http-port", "0"]
    if no_network:
        args.append("--no-network")
    if extra_args:
        args.extend(extra_args)

    proc = subprocess.Popen(
        args, stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
    )

    deadline = time.time() + SPAWN_TIMEOUT
    while time.time() < deadline:
        if proc.poll() is not None:
            err = proc.stderr.read().decode("utf-8", "replace") if proc.stderr else ""
            raise RuntimeError(f"引擎提前退出 code={proc.returncode}\n{err}")
        info = read_endpoint(data_dir)
        # 发现文件写了不等于能收请求，补一次健康检查
        if info is not None and health_ok(info):
            return info
        time.sleep(0.05)

    proc.kill()
    raise TimeoutError(f"等待引擎就绪超时（{SPAWN_TIMEOUT:.0f}s）")


def discover_or_spawn(data_dir: Path, **kwargs) -> tuple[dict, bool]:
    """返回 (端点信息, 是否新拉起)。"""
    info = discover(data_dir)
    if info is not None:
        return info, False
    return spawn(data_dir, **kwargs), True


def shutdown(info: dict) -> int:
    try:
        status, _ = request(info, "POST", "/shutdown", timeout=2.0)
        return status
    except (urllib.error.URLError, OSError):
        return 0


def main(argv: list[str]) -> int:
    data_dir = ROOT / "engine" / "data"
    for i, arg in enumerate(argv[1:]):
        if arg == "--data-dir" and i + 2 <= len(argv) - 1:
            data_dir = Path(argv[i + 2])

    if "--status" in argv:
        info = discover(data_dir)
        if info is None:
            print(f"未运行（{data_dir}）")
            return 1
        print(f"运行中 port={info['port']} pid={info.get('pid', '?')}")
        return 0

    info, spawned = discover_or_spawn(data_dir)
    action = "已拉起" if spawned else "复用已有实例"
    print(f"# {action}  port={info['port']} pid={info.get('pid', '?')}", file=sys.stderr)

    print(f"VITE_ENGINE_URL=http://127.0.0.1:{info['port']}")
    print(f"VITE_ENGINE_TOKEN={info['token']}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
