"""AC-08 本地构造种子完成校验后重启引擎，任务自动恢复为 seeding。

全程零外网：种子由 mktorrent 现场生成，载荷文件预先放好，
libtorrent 一校验就能判定为完整，因此不依赖任何 peer 或 tracker。
"""

import shutil
import tempfile
from pathlib import Path

import common as c
import mktorrent

NAME = "ac08-payload.bin"


def main() -> None:
    work = Path(tempfile.mkdtemp(prefix="eng-ac08-"))
    try:
        payload = bytes(range(256)) * 256  # 64 KiB，piece 16 KiB，共 4 块
        downloads = work / "downloads"
        downloads.mkdir()
        (downloads / NAME).write_bytes(payload)

        infohash, torrent_bytes = mktorrent.build(payload, NAME, piece_length=16384)
        torrent_path = work / "ac08.torrent"
        torrent_path.write_bytes(torrent_bytes)

        first = c.Engine(data_dir=work)
        first.start()
        try:
            status, body = first.add_torrent_file(torrent_path)
            c.check_eq(status, 200, "添加 .torrent 返回码")
            c.check_eq(body.get("infohash"), infohash, "infohash 与本地计算一致")

            task = first.wait_state(infohash, {"seeding", "finished"}, timeout=30)
            c.check(task is not None, "校验后进入 seeding / finished")
            if task:
                c.check_eq(task["state"] in {"seeding", "finished"}, True,
                           f"实际状态 {task['state']}")
        finally:
            c.check_eq(first.shutdown(), 0, "优雅退出码")
            first.stop()

        resume = work / "resume" / f"{infohash}.fastresume"
        c.check(resume.exists(), "退出时写出了 resume 文件")

        second = c.Engine(data_dir=work)
        second.start()
        try:
            task = second.wait_state(infohash, {"seeding", "finished"}, timeout=30)
            c.check(task is not None, "重启后任务自动恢复并回到 seeding")
            if task:
                c.check_eq(task["infohash"], infohash, "恢复后的 infohash 一致")
            c.check_eq(second.state()["torrent_count"], 1, "任务数量一致")
        finally:
            second.shutdown()
            second.stop()
    finally:
        shutil.rmtree(work, ignore_errors=True)
    c.finish()


if __name__ == "__main__":
    main()
