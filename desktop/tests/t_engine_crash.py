"""引擎被强杀之后，界面怎么表现。

为什么单独立一条：这不是「起引擎 → 断言」的常规形态，而是「起引擎 → 用着 →
中途把引擎干掉 → 断言界面还诚实」。杀掉之后没有可用的引擎供收尾查询，所以
Python 侧要自己承担「确认进程真死」和「确认界面确实察觉」两件事。

杀进程放在前端测试里做（`process.kill(pid, 'SIGKILL')`），理由：失效时点必须
夹在「界面已显示真实数据」和「断言离线」中间，只有测试自己知道那个时点在哪。
Python 侧负责两件测试不该自己给自己作证的事：
  1. 杀之前确认这个 pid 真的是 engine.exe —— 杀错进程是灾难性的；
  2. 杀之后独立确认 /health 不通 —— 不能只信前端说它连不上了。

零外网依赖：合成磁力 + `--no-network`。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "desktop"))
sys.path.insert(0, str(ROOT / "engine" / "tests"))

import common as c  # noqa: E402
import launcher  # noqa: E402


def process_name(pid: int) -> str | None:
    """返回 pid 对应的映像名（小写）。进程不存在或查不到返回 None。

    Windows 专用：项目只在 Windows 上交付，杀进程也是 Windows 语义。
    `tasklist` 不依赖 vcvars，比 psutil 少一个依赖。

    进程已消失时 tasklist 会打印一行「INFO: No tasks are running…」（中文系统是
    另一句），首字符不是引号，因此按「查不到」处理。stdout 也可能是空/None，
    一律用 `or ""` 兜住 —— 这里只做判断，不值得为它的输出格式加异常分支。
    """
    try:
        result = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
            capture_output=True, text=True, errors="replace",
        )
    except OSError:
        return None
    first = ((result.stdout or "").strip().splitlines() or [""])[0].strip()
    # CSV 首字段带引号："engine.exe","1234","Console","1","12,345 K"
    if not first.startswith('"'):
        return None
    return first.split('"')[1].lower()


def main() -> None:
    work = Path(tempfile.mkdtemp(prefix="desk-crash-"))
    info = None
    try:
        info, spawned = launcher.discover_or_spawn(work, no_network=True)
        c.check(spawned is True, "引擎已拉起")

        pid = info.get("pid")
        c.check(isinstance(pid, int) and pid > 4, f"端点文件里有可用 pid（实际 {pid!r}）")

        # 杀错进程的代价远大于一条测试失败，所以先核对映像名。
        # 查不到就失败而不是跳过 —— 「不知道杀的是谁」等于没有保护。
        name = process_name(pid)
        c.check(name is not None, f"能查到 pid {pid} 的映像名")
        c.check("engine" in name, f"pid {pid} 确实是引擎进程（实际 {name}）")

        infohash = c.fake_infohash(913)
        print(f"  engine port={info['port']} pid={pid} ih={infohash[:12]}…")

        env = dict(os.environ)
        env["ENGINE_URL"] = f"http://127.0.0.1:{int(info['port'])}"
        env["ENGINE_TOKEN"] = str(info["token"])
        env["ENGINE_PID"] = str(pid)
        env["EXPECT_IH"] = infohash

        print("\n  交给前端跑崩溃用例（那个进程会被真的杀掉）：")
        result = subprocess.run(
            [sys.executable, "toolchain/build.py", "desktop", "--test", "engine-crash"],
            cwd=str(ROOT), env=env, capture_output=True, text=True,
        )
        tail = (result.stdout + result.stderr).strip().splitlines()
        for line in tail[-15:]:
            print("   ", line)
        c.check(result.returncode == 0, f"崩溃用例退出码 0（实际 {result.returncode}）")

        # 独立佐证：前端说「连不上了」，这里验证「确实连不上」。
        # 只信前端自己的话，等于让被测对象给自己出证明。
        # 两条分开查：health 不通可能是别的原因（防火墙、端口被占），
        # 进程从任务列表消失才是「它真的死了」。
        c.check(not launcher.health_ok(info), "引擎确实已停止响应")
        after = process_name(pid)
        c.check(after is None, f"进程已从任务列表消失（实际查到 {after!r}）")
    finally:
        # 引擎可能已被杀，shutdown 内部会吞掉连接错误，失败也无副作用
        if info is not None:
            launcher.shutdown(info)
        shutil.rmtree(work, ignore_errors=True)

    c.finish()


if __name__ == "__main__":
    main()
