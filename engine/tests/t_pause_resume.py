"""AC-06 暂停使 state 变为 paused，恢复后离开 paused。"""

import common as c

RUNNING_STATES = {"metadata", "downloading", "checking", "other", "finished", "seeding"}


def main() -> None:
    engine = c.Engine().start()
    try:
        infohash = c.fake_infohash(102)
        engine.add_magnet(c.fake_magnet(infohash, "ac06"))
        c.check(engine.wait_state(infohash, RUNNING_STATES, timeout=5) is not None,
                "任务已就绪")

        c.check_eq(engine.action(infohash, "pause")[0], 200, "pause 返回码")
        task = engine.wait_state(infohash, {"paused"}, timeout=5)
        c.check(task is not None and task["state"] == "paused", "暂停后 state 为 paused")

        c.check_eq(engine.action(infohash, "resume")[0], 200, "resume 返回码")
        task = engine.wait_state(infohash, RUNNING_STATES, timeout=5)
        c.check(task is not None, "恢复后离开 paused")
        if task:
            c.check(task["state"] != "paused", f"恢复后 state={task['state']}")
            c.check(task["paused"] is False, "paused 标志为 false")

        c.check_eq(engine.action(c.fake_infohash(999), "pause")[0], 404, "对不存在的任务暂停返回 404")
    finally:
        engine.stop()
    c.finish()


if __name__ == "__main__":
    main()
