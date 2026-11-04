"""AC-17 `total_bytes` 在已完成的任务上不能塌成 0。

实测到的缺陷：`torrent_status::total` 在 **seeding** 态返回 0（元数据被释放），
而同一时刻 `done_bytes` 是完整大小 —— 对外表现是「已下完 10240 字节，总大小 0」，
ETA 也跟着算成 0。反直觉的是**下载中态正常**（10240），只有完成之后才塌。

修法是从 `torrent_info::total_size()` 兜底，因为它不随任务状态变化。

两种状态都必须断言。只测下载中的话，把实现改回 `st.total` 依然全绿 ——
缺陷恰好只出现在另一半状态里。

「已完成」不靠 peer 造：把完整内容预先放进 `save_path`，删掉任务再加一次，
libtorrent 会自己校验到 seeding。整条判据零外网依赖、零 recheck 接口依赖。

最后一条是反向约束：**没有元数据的磁力任务必须给 0**。不能因为「别是 0」就编一个
数字出来 —— 那会让界面显示一个根本不存在的大小，比显示 0 更坏。
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common as c  # noqa: E402
import mktorrent  # noqa: E402

PAYLOAD = bytes(range(256)) * 40  # 10240 字节
SIZE = len(PAYLOAD)


def wait_gone(engine: c.Engine, infohash: str, timeout: float = 10.0) -> bool:
    """等任务真的从 /state 消失。

    `DELETE` 返回 200 只表示引擎受理了；libtorrent 移除 handle 是异步的。
    不等就重新添加同一个 infohash，新任务会被旧 handle 挡住，状态停在旧的
    progress=0 —— 表现为「重新加完一直是 downloading，永远不校验」。
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        if engine.find(infohash) is None:
            return True
        time.sleep(0.1)
    return False


def main() -> None:
    engine = c.Engine().start()
    try:
        _infohash, blob = mktorrent.build(PAYLOAD, "seed.bin")
        torrent_file = engine.data_dir / "seed.torrent"
        torrent_file.write_bytes(blob)

        # ---- A) 下载中：total_bytes 必须是完整大小
        status, body = engine.add_torrent_file(torrent_file)
        c.check_eq(status, 200, "A 加种子返回 200")
        infohash = body.get("infohash", "")
        c.check(bool(infohash), f"A 拿到 infohash（{infohash}）")

        task = engine.wait_state(infohash, {"downloading", "checking", "metadata"},
                                 timeout=10)
        if not c.check(task is not None, "A 任务出现在列表里"):
            return
        c.check_eq(task["total_bytes"], SIZE,
                   f"A 下载中 total_bytes={task['total_bytes']}（期望 {SIZE}）")
        c.check_eq(task["done_bytes"], 0, "A 下载中 done_bytes 是 0")

        # ---- B) 已完成：核心断言，这里曾经是 0
        save_dir = Path(task["save_path"])
        save_dir.mkdir(parents=True, exist_ok=True)
        (save_dir / "seed.bin").write_bytes(PAYLOAD)

        c.check_eq(engine.delete(infohash)[0], 200, "B 删掉任务（保留文件）")
        c.check((save_dir / "seed.bin").exists(),
                "B 文件还在磁盘上，等待被重新校验")
        if not c.check(wait_gone(engine, infohash), "B 旧任务已从列表消失"):
            return

        status, body = engine.add_torrent_file(torrent_file)
        c.check_eq(status, 200, "B 重新加同一份种子返回 200")

        done = engine.wait_state(infohash, {"seeding", "finished"}, timeout=20)
        if not c.check(done is not None and done["state"] in ("seeding", "finished"),
                       f"B 校验完成进入做种态（实际 {done['state'] if done else '无'}）"):
            return
        c.check_eq(done["done_bytes"], SIZE,
                   f"B 已完成 done_bytes={done['done_bytes']}（期望 {SIZE}）")
        c.check_eq(done["total_bytes"], SIZE,
                   f"B 已完成 total_bytes={done['total_bytes']}（期望 {SIZE}，"
                   f"这个字段曾经塌成 0）")
        c.check(done["total_bytes"] >= done["done_bytes"],
                "B total_bytes 不小于 done_bytes（否则进度条会超过 100%）")

        # ---- C) 无元数据的磁力：必须是 0，不能编
        ghost = c.fake_infohash(401)
        engine.add_magnet(c.fake_magnet(ghost, "ac17"))
        meta = engine.wait_state(ghost, {"metadata", "downloading"}, timeout=10)
        if c.check(meta is not None, "C 磁力任务出现在列表里"):
            c.check_eq(meta["has_metadata"], False, "C 磁力还没有元数据")
            c.check_eq(meta["total_bytes"], 0,
                       f"C 无元数据时 total_bytes=0（实际 {meta['total_bytes']}）")
    finally:
        engine.stop()
        # 必须放在 finally 里：中途 `return` 会跳过它，判据就变成「失败但退出码 0」，
        # 卡点会把它当成通过。这条踩过 —— 上面 B 段失败时 EXIT 就是 0。
        c.finish()


if __name__ == "__main__":
    main()
