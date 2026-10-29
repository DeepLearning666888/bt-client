"""AC-07 删除后该 infohash 不再出现在 /state。"""

import time

import common as c

RUNNING_STATES = {"metadata", "downloading", "checking", "paused", "other", "finished", "seeding"}


def main() -> None:
    engine = c.Engine().start()
    try:
        infohash = c.fake_infohash(103)
        engine.add_magnet(c.fake_magnet(infohash, "ac07"))
        c.check(engine.wait_state(infohash, RUNNING_STATES, timeout=5) is not None,
                "任务已就绪")
        before = engine.state()["torrent_count"]

        c.check_eq(engine.delete(infohash)[0], 200, "删除返回码")

        deadline = time.time() + 5
        gone = False
        while time.time() < deadline:
            if engine.find(infohash) is None:
                gone = True
                break
            time.sleep(0.1)
        c.check(gone, "删除后不再出现在 /state")
        c.check(engine.state()["torrent_count"] < before, "任务计数下降")
        c.check_eq(engine.delete(infohash)[0], 404, "重复删除返回 404")
    finally:
        engine.stop()
    c.finish()


if __name__ == "__main__":
    main()
