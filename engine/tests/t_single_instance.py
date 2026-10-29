"""AC-02 同一数据目录只允许一个实例。"""

import subprocess

import common as c


def main() -> None:
    engine = c.Engine().start()
    try:
        second = subprocess.run(
            [str(c.EXE), "--data-dir", str(engine.data_dir), "--no-network"],
            capture_output=True, timeout=20)
        c.check_eq(second.returncode, 2, "第二个实例退出码")
        stderr = second.stderr.decode("utf-8", "replace")
        c.check("已有实例" in stderr, "第二个实例给出占用提示")
        c.check_eq(engine.raw("GET", "/health")[0], 200, "第一个实例仍健康")
    finally:
        engine.stop()
    c.finish()


if __name__ == "__main__":
    main()
