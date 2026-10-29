"""AC-09 50 个任务时 GET /state 响应时间小于 50ms（20 次采样中位数）。

这条不是在测网络，而是在测架构：如果 /state 在请求时遍历会话、抓 status，
任务一多就会线性变慢。现在的实现是在 10Hz 定时器里算好快照，请求只拷贝字符串，
所以这条标准同时也是对「快照与请求解耦」这个设计的回归保护。
"""

import time

import common as c

TARGET_COUNT = 50
SAMPLES = 20
BUDGET_SECONDS = 0.050


def main() -> None:
    engine = c.Engine().start()
    try:
        for i in range(TARGET_COUNT):
            status, _ = engine.add_magnet(c.fake_magnet(c.fake_infohash(200 + i)))
            if status != 200:
                c.check(False, f"第 {i + 1} 个任务添加失败（{status}）")
                return

        deadline = time.time() + 15
        while time.time() < deadline:
            if engine.state()["torrent_count"] >= TARGET_COUNT:
                break
            time.sleep(0.1)
        c.check(engine.state()["torrent_count"] == TARGET_COUNT,
                f"快照中包含 {TARGET_COUNT} 个任务")

        for _ in range(3):  # 预热，避免把首次连接耗时算进中位数
            engine.raw("GET", "/state")

        samples = []
        for _ in range(SAMPLES):
            start = time.perf_counter()
            status, _ = engine.raw("GET", "/state")
            samples.append(time.perf_counter() - start)
            if status != 200:
                c.check(False, f"/state 返回 {status}")
                return

        samples.sort()
        median = samples[len(samples) // 2]
        print(f"  采样 {SAMPLES} 次，中位 {median * 1000:.1f} ms，"
              f"最长 {samples[-1] * 1000:.1f} ms")
        c.check(median < BUDGET_SECONDS,
                f"中位延迟 {median * 1000:.1f} ms < {BUDGET_SECONDS * 1000:.0f} ms")
    finally:
        engine.stop()
    c.finish()


if __name__ == "__main__":
    main()
