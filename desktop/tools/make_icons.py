#!/usr/bin/env python3
"""生成 Tauri 需要的图标资源。

手写 PNG 与 ICO 编码，不引 Pillow：这个脚本只在图标变更时跑一次，
为它加一个几十 MB 的依赖不值当。

ICO 用 DIB 条目而不是嵌入 PNG —— 老版本的 Windows 资源编译器只认前者，
而 ico 要参与 exe 资源链接（tauri-build 会读它）。用 PNG 压缩的 ICO
在部分工具链上会直接报「不是有效图标」。

画的是一个下载意象：圆角方块 + 向下的箭头 + 底线。用 4 倍超采样再降采样，
32×32 上也能有平滑边缘。
"""

from __future__ import annotations

import struct
import zlib
from pathlib import Path

OUT = Path(__file__).resolve().parents[1] / "src-tauri" / "icons"

BG = (27, 33, 48, 255)          # #1b2130 深蓝灰
ACCENT = (56, 189, 248, 255)    # #38bdf8 天蓝
SS = 4                          # 超采样倍数


def inside_rounded(x: float, y: float, size: float, radius: float) -> bool:
    """圆角矩形的内部判定。坐标以像素中心为准。"""
    left, top = radius, radius
    right, bottom = size - radius, size - radius
    if x < left and y < top:
        return (x - left) ** 2 + (y - top) ** 2 <= radius**2
    if x > right and y < top:
        return (x - right) ** 2 + (y - top) ** 2 <= radius**2
    if x < left and y > bottom:
        return (x - left) ** 2 + (y - bottom) ** 2 <= radius**2
    if x > right and y > bottom:
        return (x - right) ** 2 + (y - bottom) ** 2 <= radius**2
    return 0 <= x <= size and 0 <= y <= size


def shape(x: float, y: float) -> tuple[int, int, int, int]:
    """归一化坐标 (0..1) 上的绘制。返回 RGBA。"""
    # 背景
    if not inside_rounded(x * 1.0, y * 1.0, 1.0, 0.22):
        return (0, 0, 0, 0)

    # 竖杆
    if 0.455 <= x <= 0.545 and 0.24 <= y <= 0.58:
        return ACCENT

    # 箭头三角：顶点在 (0.5, 0.76)，底边在 y=0.56
    if 0.56 <= y <= 0.76:
        half = 0.23 * (0.76 - y) / (0.76 - 0.56)
        if abs(x - 0.5) <= half:
            return ACCENT

    # 底线
    if 0.28 <= x <= 0.72 and 0.815 <= y <= 0.865:
        return ACCENT

    return BG


def render(size: int) -> list[list[tuple[int, int, int, int]]]:
    """渲染一张 size×size 的图。返回逐行像素。"""
    big = size * SS
    # 先在超采样网格上取色
    acc = [[[0, 0, 0, 0] for _ in range(size)] for _ in range(size)]
    for by in range(big):
        ny = (by + 0.5) / big
        for bx in range(big):
            nx = (bx + 0.5) / big
            r, g, b, a = shape(nx, ny)
            cell = acc[by // SS][bx // SS]
            cell[0] += r * a
            cell[1] += g * a
            cell[2] += b * a
            cell[3] += a

    rows = []
    n = SS * SS
    for y in range(size):
        row = []
        for x in range(size):
            sr, sg, sb, sa = acc[y][x]
            if sa == 0:
                row.append((0, 0, 0, 0))
            else:
                # 预乘后还原，避免半透明边缘发黑
                row.append((sr // sa, sg // sa, sb // sa, sa // n))
        rows.append(row)
    return rows


def png_bytes(rows: list[list[tuple[int, int, int, int]]]) -> bytes:
    size = len(rows)
    raw = bytearray()
    for row in rows:
        raw.append(0)  # filter type 0
        for r, g, b, a in row:
            raw.extend((r, g, b, a))

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + tag
            + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        )

    ihdr = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)
    # 固定压缩级别，保证同一份代码产出同样的字节
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(bytes(raw), 9))
        + chunk(b"IEND", b"")
    )


def dib_bytes(rows: list[list[tuple[int, int, int, int]]]) -> bytes:
    """ICO 里的 BMP 条目：BITMAPINFOHEADER + 自下而上的 BGRA + AND 掩码。"""
    size = len(rows)
    header = struct.pack(
        "<IiiHHIIiiII",
        40,          # biSize
        size,        # biWidth
        size * 2,    # biHeight（颜色 + 掩码两张图，ICO 约定）
        1,           # biPlanes
        32,          # biBitCount
        0,           # biCompression = BI_RGB
        0,           # biSizeImage
        0, 0, 0, 0,  # 分辨率与调色板
    )
    pixels = bytearray()
    for y in range(size - 1, -1, -1):
        for r, g, b, a in rows[y]:
            pixels.extend((b, g, r, a))

    # AND 掩码：32 位色下按 alpha 已能正确合成，但结构上必须存在且按 4 字节对齐
    mask_row = ((size + 31) // 32) * 4
    mask = bytes(mask_row * size)
    return header + bytes(pixels) + mask


def ico_bytes(sizes: list[int]) -> bytes:
    images = []
    for size in sizes:
        rows = render(size)
        images.append((size, dib_bytes(rows)))

    out = struct.pack("<HHH", 0, 1, len(images))
    offset = 6 + 16 * len(images)
    entries = bytearray()
    for size, data in images:
        entries.extend(
            struct.pack(
                "<BBBBHHII",
                0 if size >= 256 else size,  # 256 用 0 表示
                0 if size >= 256 else size,
                0, 0, 1, 32,
                len(data),
                offset,
            )
        )
        offset += len(data)
    return bytes(out) + bytes(entries) + b"".join(data for _, data in images)


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    plan = [
        ("32x32.png", 32),
        ("128x128.png", 128),
        ("128x128@2x.png", 256),
        ("Square44x44Logo.png", 44),
        ("Square150x150Logo.png", 150),
        ("StoreLogo.png", 50),
    ]
    for name, size in plan:
        data = png_bytes(render(size))
        (OUT / name).write_bytes(data)
        print(f"{name:24s} {size:>3d}px  {len(data):>6d} 字节")

    # 只放到 128：256 档的 DIB 条目单独占 260 KB，而壳的窗口图标与任务栏
    # 用不到那么大。少一个档位换来 exe 资源瘦 260 KB，是划算的。
    sizes = [16, 32, 48, 128]
    ico = ico_bytes(sizes)
    (OUT / "icon.ico").write_bytes(ico)
    print(f"{'icon.ico':24s} {f'{len(sizes)} 档':>4s}  {len(ico):>6d} 字节")
    print(f"输出目录：{OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
