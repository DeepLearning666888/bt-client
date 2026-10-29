#!/usr/bin/env python3
"""AC-01：安装包已产出且体积合理。

体积上下限都不是凑数的：

- **下限 2.5 MB**：主程序 8.57 MB + 引擎 0.37 MB + DLL 3.78 MB，LZMA 后 2.99 MB。
  这个下限专门用来卡「引擎和 DLL 没进包」—— 那种包装得上、点得开、然后引擎起不来，
  排查方向会完全跑偏到代码里。
- **上限 40 MB**：超了说明打包范围失控（把 node_modules、target 或旧产物一起装进去了）。

只看体积证明不了内容对，内容由 t_installer_install.py 的 AC-02/AC-06 判定 ——
这一项只负责「产物存在且量级正常」，让失败信息能指对方向。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import installer_common as common  # noqa: E402

MIN_BYTES = 2_500_000
MAX_BYTES = 40 * 1024 * 1024


def main() -> int:
    problems: list[str] = []

    print("① 定位安装包")
    try:
        setup = common.find_setup()
    except SystemExit as exc:
        print(f"  ✗ {exc}")
        return 1
    size = setup.stat().st_size
    print(f"  产物：{setup.name}")
    common.check("文件名以 setup.exe 结尾", setup.name.endswith("setup.exe"), setup.name, problems)
    common.check(
        f"体积 {size / 1024 / 1024:.2f} MB 在 {MIN_BYTES / 1e6:.1f}–{MAX_BYTES / 1024 / 1024:.0f} MB 之间",
        MIN_BYTES <= size <= MAX_BYTES,
        "" if MIN_BYTES <= size <= MAX_BYTES else "过小多半漏了引擎/DLL，过大多半误打了开发产物",
        problems,
    )

    print("\n② 产物来源可追溯")
    exe = common.ROOT / "desktop" / "src-tauri" / "target" / "release" / "btclient-shell.exe"
    common.check("主程序 exe 存在", exe.exists(),
                 str(exe.relative_to(common.ROOT)) if exe.exists() else "缺失", problems)
    if exe.exists():
        shell_size = exe.stat().st_size
        # 主程序小于 3 MB 说明前端资源没嵌进去，装出来会是个白窗口
        common.check(f"主程序 {shell_size / 1024 / 1024:.2f} MB > 3 MB（前端资源已嵌入）",
                     shell_size > 3 * 1024 * 1024, "", problems)

    engine = common.ROOT / "engine" / "build" / "engine.exe"
    common.check("引擎产物存在", engine.exists(),
                 str(engine.relative_to(common.ROOT)) if engine.exists() else "缺失", problems)

    return common.report(problems)


if __name__ == "__main__":
    sys.exit(main())
