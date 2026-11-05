"""AC-20 空闲时 seq 必须恒定，`?since=N` 才真的能省掉重绘。

`/state` 的 seq 是对内容做比较算出来的，前端 10Hz 轮询时带 `?since=N`，
未变则引擎只回 `{"seq":N,"unchanged":true}`，前端据此**零回调**（不产生新对象、
不进 setState）。这条优化是「任务多也不卡」的地基。

它此前从未生效过：判定式是 `list + "|" + nat`，而 nat 里有每秒递增的
`sampled_seconds`，于是空载时 seq 也每秒在涨（实测 1→3→4→5）。每拍都收到全量
快照、整树重渲，且没有任何报错 —— 一个静默失效的性能设计。

修法是把 nat 从判定式里摘出去，seq 只由任务列表决定。**但这条判据单独写很危险**：
把 seq 冻成常量就能全绿，哪怕引擎已经卡死。所以 B 段反过来盯住同一时刻的
nat.sampled_seconds —— 它必须在涨，证明「跳过的是 nat 的噪声」而不是「引擎停了」。

段：
  A  空载 12 拍，seq 恒定，且每拍 `?since=N` 都回 unchanged
  B  同一时间段里 nat.sampled_seconds 确实在涨（否则 A 的绿是假的）
  C  加一个任务，seq 必须变 —— 证明任务列表没被一起冻住
  D  删掉任务，seq 再次变（列表回到空，内容与"从未加过"一致，但 seq 只增不减）
  E  unchanged 响应体必须极简，不能偷偷塞全量快照
"""

import json
import time

import common as c

# 12 拍 × 0.5 秒 = 6 秒。natdiag 的 sampled_seconds 每秒 +1，6 秒足以看清趋势。
TICKS = 12
TICK_INTERVAL_S = 0.5


def main() -> None:
    engine = c.Engine().start()
    try:
        # nat 第一拍可能还没采到，等它有个初值再开始
        deadline = time.time() + 10
        while time.time() < deadline:
            if engine.state().get("nat", {}).get("sampled_seconds", 0) > 0:
                break
            time.sleep(0.2)

        # ---- A / B：空载期间 seq 不动，但 nat 在动
        seqs: list[int] = []
        samples: list[int] = []
        unchanged = 0
        bodies: list[str] = []
        for _ in range(TICKS):
            state = engine.state()
            seq = state["seq"]
            seqs.append(seq)
            samples.append(int(state.get("nat", {}).get("sampled_seconds", -1)))

            status, body = engine.get(f"/state?since={seq}")
            c.check_eq(status, 200, "A 带 since 的 /state 返回 200")
            bodies.append(body)
            if '"unchanged":true' in body:
                unchanged += 1
            time.sleep(TICK_INTERVAL_S)

        c.check(len(set(seqs)) == 1,
                f"A 空载 {TICKS} 拍 seq 恒定（实测 {seqs}）—— "
                f"不变说明 nat 的 sampled_seconds 没再污染判定式")
        c.check_eq(unchanged, TICKS,
                   f"A 每一拍都回 unchanged（{unchanged}/{TICKS}）—— "
                   f"前端这 6 秒一次重绘都没发生")
        c.check(seqs == sorted(seqs), "A seq 单调不减")

        # ---- A2：拿住一个固定的 seq，隔几秒再问 —— 这才是前端真实的用法
        #
        # A1 是每拍重新取当前 seq 再问，两个请求只隔几毫秒，落在引擎同一个 100ms
        # 重建窗口里，所以**旧代码下也照样全绿**（实测 12/12）。它测不出失效。
        #
        # 前端不是这么用的：它持有 lastSeq，每 100ms 带同一个 `?since=lastSeq`。
        # 只要任务列表没变，引擎就应当一直回 unchanged，跨过几秒也一样。
        anchor = seqs[0]
        time.sleep(3.0)
        status, body = engine.get(f"/state?since={anchor}")
        c.check_eq(status, 200, "A2 用 3 秒前的 seq 请求返回 200")
        c.check('"unchanged":true' in body,
                f"A2 3 秒后仍回 unchanged（seq 锚在 {anchor}）—— "
                f"旧实现这里会塞回全量快照，因为 nat 把 seq 推着涨了")
        c.check_eq(engine.state()["seq"], anchor,
                   f"A2 3 秒后 seq 仍是 {anchor}")

        # B 段是 A 段的反向对照。没有它，把 seq 写死成常量也能全绿。
        growth = samples[-1] - samples[0]
        c.check(growth >= 2,
                f"B 同一时间段里 nat.sampled_seconds 涨了 {growth} 秒"
                f"（{samples[0]} → {samples[-1]}）—— 引擎一直在跑，"
                f"seq 不动是因为该不动，不是因为卡死")
        c.check(all(s >= 0 for s in samples), "B 每一拍都拿得到 nat.sampled_seconds")

        # ---- E：unchanged 响应必须极简
        longest = max(len(b) for b in bodies)
        c.check(longest < 100,
                f"E unchanged 响应体最长 {longest} 字节（< 100）—— "
                f"没有把全量快照偷偷塞回来")
        for body in bodies[:1]:
            parsed = json.loads(body)
            c.check_eq(parsed.get("unchanged"), True, "E unchanged 是布尔 true")
            c.check("torrents" not in parsed, "E unchanged 响应里不含 torrents")

        # ---- C：任务列表变了，seq 必须跟着变
        before = engine.state()["seq"]
        status, _ = engine.add_magnet(c.fake_magnet(c.fake_infohash(7), "seq-probe"))
        c.check_eq(status, 200, "C 加磁力任务返回 200")

        after_add = None
        deadline = time.time() + 10
        while time.time() < deadline:
            current = engine.state()["seq"]
            if current != before:
                after_add = current
                break
            time.sleep(0.1)
        c.check(after_add is not None and after_add > before,
                f"C 加任务后 seq 递增（{before} → {after_add}）—— "
                f"任务列表没被一起冻住")

        # 加了任务之后，同一份快照再带 since 应当重新拿到全量
        if after_add is not None:
            status, body = engine.get(f"/state?since={before}")
            c.check_eq(status, 200, "C 用旧 seq 请求返回 200")
            c.check('"unchanged"' not in body,
                    "C 用旧 seq 请求拿到的是全量快照，不是 unchanged")

        # ---- D：删掉任务，seq 再次变
        if after_add is not None:
            ih = c.fake_infohash(7)
            status, _ = engine.delete(ih)
            c.check_eq(status, 200, "D 删除任务返回 200")
            deadline = time.time() + 10
            after_delete = None
            while time.time() < deadline:
                current = engine.state()["seq"]
                if current != after_add:
                    after_delete = current
                    break
                time.sleep(0.1)
            c.check(after_delete is not None and after_delete > after_add,
                    f"D 删任务后 seq 再次递增（{after_add} → {after_delete}）")
            c.check_eq(len(engine.state().get("torrents", [])), 0,
                       "D 删完后任务列表为空")

        # ---- AC-12 的契约不能被这条改动带坏：不带 since 时 nat 照样是完整体
        full = engine.state()
        c.check("nat" in full, "D 不带 since 的 /state 仍含 nat（AC-12 契约不变）")
        c.check(isinstance(full["nat"].get("sampled_seconds"), int),
                "D nat.sampled_seconds 仍是整数")
        c.check_eq(len(full["nat"].get("slots", [])), 4, "D nat 仍是 4 个槽位")
    finally:
        engine.stop()
    c.finish()


if __name__ == "__main__":
    main()
