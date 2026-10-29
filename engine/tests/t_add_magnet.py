"""AC-05 添加磁力后 infohash 出现在 /state，且含 state 与 progress 字段。

刻意断言「任务被接受」而不是「种子能下载」：后者要等元数据与 peer，
会把一条可以在 200ms 内判定的标准变成依赖外网的赌局。
"""

import common as c

READY_STATES = {"metadata", "downloading", "checking", "paused", "other", "finished", "seeding"}


def main() -> None:
    engine = c.Engine().start()
    try:
        infohash = c.fake_infohash(101)
        status, body = engine.add_magnet(c.fake_magnet(infohash, "ac05"))
        c.check_eq(status, 200, "添加返回码")
        c.check_eq(body.get("infohash"), infohash, "返回的 infohash")

        task = engine.wait_state(infohash, READY_STATES, timeout=5)
        c.check(task is not None, "任务出现在 /state")
        if task:
            c.check("state" in task, "含 state 字段")
            c.check("progress" in task, "含 progress 字段")
            c.check(isinstance(task["progress"], (int, float)), "progress 是数值")
            c.check_eq(task["infohash"], infohash, "/state 中的 infohash 一致")
        c.check_eq(engine.add_magnet(c.fake_magnet(infohash))[0], 409, "重复添加被拒为 409")
    finally:
        engine.stop()
    c.finish()


if __name__ == "__main__":
    main()
