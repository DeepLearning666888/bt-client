"""AC-21 前端视角：一次真实传输期间，带 since 的轮询必须持续拿到新快照。

AC-20 只证明了「**静止**时 seq 不动」。那是必要条件，不是充分条件 ——
把 seq 冻成常量、或者在传输时压根不更新列表，AC-20 也全绿。

这条补另一半：**动的时候必须动**。而且完全按前端的用法写：持有 lastSeq、
高频带 `?since=lastSeq` 轮询、只看 `unchanged` 与全量两种响应，不去读引擎的
内部计时器。前端怎么活，它就怎么测。

为什么不能用「加个任务看 seq 涨」代替：加任务改变的是列表**长度**，一次就结束。
传输期间变的是 `rate_down` / `rate_up` / `done_bytes` / `eta_s` —— 每 100ms 都在变。
如果有人把 `done_bytes` 从 `/state` 里误删、或者把 seq 判定改成只比部分字段，
加任务那条照样绿，这条会红。

为什么要有真 peer：零 peer 的任务 rate 恒为 0、done_bytes 恒为 0，
「传输中」和「静止」在数据上完全一样，这条就会退化成 AC-20。
所以复用 local_tracker + 两端 `--no-network` 那套，让字节真的流起来。

段：
  A  种子端自己校验到 seeding（真 peer 的来源）
  B  下载端用 `?since=lastSeq` 轮询，统计「非 unchanged」次数
  C  每次应用时 done_bytes 不减，且 seq 严格递增，最终到全量
  D  传完之后静止，轮询必须**全部**回到 unchanged
     —— 「动的时候动、静的时候静」，两个方向都锁住
"""

from __future__ import annotations

import json
import random
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common as c  # noqa: E402
import mktorrent  # noqa: E402
from local_tracker import LocalTracker  # noqa: E402

PIECE_LEN = 32768
PIECE_COUNT = 1024
PAYLOAD = random.Random(20261105).randbytes(PIECE_LEN * PIECE_COUNT)  # 32 MiB
SIZE = len(PAYLOAD)
NAME = "seed.bin"

# 前端 poller 是 100ms 一拍。这里压到 20ms：引擎快照 100ms 重建一次，
# 同频采样有一半会撞在同一个窗口里，密一倍不够，密五倍才稳。
POLL_INTERVAL_S = 0.02

# 传输期间至少要看到这么多次「内容变了」。
#
# **不能按「前端 10Hz 跑 10 秒 = 100 次」来定。** 本地回环实测 32 MiB 只用
# **0.54 秒**（约 62 MB/s），全程只撞上 5 个 100ms 快照窗口 —— 样本上限就是 5。
# 一开始写 ≥20，测出来 4~6，看着像引擎有问题，其实是判据建在错误的传输时长假设上。
# 要求「首帧 + 至少 3 次增量 + 完成帧」，正好卡在实测下限，且足够抓住退化：
#   把 done_bytes 从 /state 删掉 → add/finish 之外不会有增量，只剩 2~3 次；
#   把 seq 冻成常量 → 1 次都不会有。
MIN_UPDATES = 4

QUIET_TICKS = 15          # 静下来之后连续多少拍必须全 unchanged
QUIET_SETTLE_S = 20.0     # 最多允许这么久才静下来


def wait_gone(engine: c.Engine, infohash: str, timeout: float = 10.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if engine.find(infohash) is None:
            return True
        time.sleep(0.1)
    return False


def stage_seeder(engine: c.Engine, blob: bytes) -> tuple[str, dict]:
    """把完整内容预置进去，让引擎自己校验到 seeding。返回 (infohash, 最终快照)。"""
    torrent_file = engine.data_dir / "seed.torrent"
    torrent_file.write_bytes(blob)
    status, body = engine.add_torrent_file(torrent_file)
    if status != 200:
        return "", {}
    infohash = body.get("infohash", "")
    task = engine.wait_state(infohash, {"downloading", "checking", "metadata"}, timeout=15)
    if task is None:
        return "", {}

    save_dir = Path(task["save_path"])
    save_dir.mkdir(parents=True, exist_ok=True)
    (save_dir / NAME).write_bytes(PAYLOAD)

    if engine.delete(infohash)[0] != 200:
        return "", {}
    if not wait_gone(engine, infohash):
        return "", {}
    if engine.add_torrent_file(torrent_file)[0] != 200:
        return "", {}
    done = engine.wait_state(infohash, {"seeding", "finished"}, timeout=40)
    return infohash, (done or {})


def poll_once(engine: c.Engine, since: int) -> tuple[str, dict | None]:
    """一次前端式轮询。返回 ("unchanged" | "applied" | "failed", 快照或 None)。"""
    try:
        status, body = engine.get(f"/state?since={since}")
    except Exception:  # noqa: BLE001 — 引擎在传输中，读失败算失败一次
        return "failed", None
    if status != 200:
        return "failed", None
    try:
        parsed = json.loads(body)
    except json.JSONDecodeError:
        return "failed", None
    if parsed.get("unchanged") is True:
        return "unchanged", None
    return "applied", parsed


def main() -> None:
    with LocalTracker() as tracker:
        _infohash, blob = mktorrent.build(
            PAYLOAD, NAME, piece_length=PIECE_LEN, announce=tracker.announce_url)

        seeder = c.Engine().start()
        learner = None
        try:
            # ---- A) 种子端就绪
            infohash, done = stage_seeder(seeder, blob)
            if not c.check(bool(infohash), "A 种子端备好，拿到 infohash"):
                return
            c.check(done.get("state") in ("seeding", "finished"),
                    f"A 种子端进入做种态（实际 {done.get('state')}）")
            c.check_eq(done.get("done_bytes"), SIZE, "A 种子端内容完整")

            # ---- B) 下载端用前端的方式轮询
            learner = c.Engine().start()
            learner_file = learner.data_dir / "seed.torrent"
            learner_file.write_bytes(blob)
            status, body = learner.add_torrent_file(learner_file)
            if not c.check_eq(status, 200, "B 下载端加种子返回 200"):
                return
            c.check_eq(body.get("infohash", ""), infohash, "B 两边是同一个 infohash")

            last_seq = 0
            applied = 0
            unchanged = 0
            failed = 0
            applied_done: list[int] = []
            applied_seqs: list[int] = []
            last_done = 0
            regressions: list[str] = []
            final_task: dict = {}

            deadline = time.time() + 120
            while time.time() < deadline:
                outcome, snap = poll_once(learner, last_seq)
                if outcome == "unchanged":
                    unchanged += 1
                elif outcome == "failed":
                    failed += 1
                else:
                    applied += 1
                    if snap is None:
                        failed += 1
                        applied -= 1
                        time.sleep(POLL_INTERVAL_S)
                        continue
                    new_seq = int(snap.get("seq", last_seq))
                    if new_seq <= last_seq:
                        regressions.append(f"seq {last_seq} → {new_seq} 没有递增")
                    last_seq = new_seq
                    applied_seqs.append(new_seq)

                    task = next((t for t in snap.get("torrents", [])
                                 if t["infohash"] == infohash), None)
                    if task is not None:
                        current = int(task.get("done_bytes", 0))
                        if current < last_done:
                            regressions.append(f"done_bytes {last_done} → {current} 倒退了")
                        last_done = current
                        applied_done.append(current)
                        final_task = task
                        if task.get("state") in ("seeding", "finished"):
                            break
                time.sleep(POLL_INTERVAL_S)

            c.check_eq(failed, 0, f"B 全程没有失败的轮询（{failed} 次失败）")
            c.check(applied >= MIN_UPDATES,
                    f"B 传输期间拿到 {applied} 次「内容变了」的响应"
                    f"（要求 ≥{MIN_UPDATES}）—— seq 在动的时候真的在动")
            c.check(applied_done == sorted(applied_done),
                    f"B 进度不倒退（采样 {len(applied_done)} 个点）")
            c.check(not regressions,
                    f"B 没有 seq 不增或 done_bytes 倒退的情况（{regressions[:3]}）")

            # ---- C) 走到全量
            c.check_eq(final_task.get("state") in ("seeding", "finished"), True,
                       f"C 前端视角看到任务下完（最后状态 {final_task.get('state')}）")
            c.check_eq(final_task.get("done_bytes"), SIZE,
                       f"C 前端视角最后的 done_bytes={final_task.get('done_bytes')}"
                       f"（期望 {SIZE}）")
            c.check(len(applied_seqs) == len(set(applied_seqs)),
                    f"C 每次应用的 seq 互不相同（{len(applied_seqs)} 个）")
            c.check(applied_seqs == sorted(applied_seqs), "C seq 序列单调递增")

            # ---- D) 传完之后必须静下来
            #
            # 不能「传完等 2 秒就要求全 unchanged」：实测下完瞬间 rate 还在衰减
            # （滑动平均里还留着最后几个分片），seq 会再涨一两次。那不是缺陷，
            # 是统计窗口的尾巴。所以先等它自己收敛，收敛后再要求一直静。
            #
            # 顺带记下收敛耗时 —— 这个数字有用户意义：**下完之后界面还会抖多久**。
            settle_start = time.time()
            quiet_streak = 0
            while time.time() - settle_start < QUIET_SETTLE_S:
                outcome, snap = poll_once(learner, last_seq)
                if outcome == "unchanged":
                    quiet_streak += 1
                    if quiet_streak >= 5:
                        break
                else:
                    quiet_streak = 0
                    if snap is not None:
                        last_seq = int(snap.get("seq", last_seq))
                time.sleep(POLL_INTERVAL_S)
            settle_s = time.time() - settle_start

            c.check(quiet_streak >= 5,
                    f"D 下完之后 {settle_s:.1f} 秒内静下来（连续 5 拍 unchanged）"
                    f"—— 速率统计的尾巴收敛了")

            quiet_total = 0
            quiet_unchanged = 0
            quiet_seqs: list[int] = []
            for _ in range(QUIET_TICKS):
                outcome, snap = poll_once(learner, last_seq)
                quiet_total += 1
                if outcome == "unchanged":
                    quiet_unchanged += 1
                elif snap is not None:
                    last_seq = int(snap.get("seq", last_seq))
                    quiet_seqs.append(last_seq)
                time.sleep(POLL_INTERVAL_S)

            c.check_eq(quiet_unchanged, quiet_total,
                       f"D 静下来后 {quiet_total} 拍全部回 unchanged"
                       f"（实际 {quiet_unchanged} 拍）—— 界面不再空转重绘")
            c.check(not quiet_seqs,
                    f"D 静下来之后 seq 一次都没再涨（期间涨过：{quiet_seqs[:5]}）")

            # ---- E) 暂停 / 继续：不依赖传输速度的确定性更新
            #
            # D 段的样本数受限于传输速度（本地回环 32 MiB 只跑 0.54 秒、5 个快照
            # 窗口）。这一段补一个**时序完全确定**的检查：点了暂停，列表里的 state
            # 字段变了，seq 就必须涨、前端就必须收到 —— 否则用户点了暂停，界面还
            # 写着「下载中」。它顺带锁住 state 确实在 list 的判定范围内。
            def wait_for_state(want: str, budget: float = 8.0) -> dict | None:
                nonlocal last_seq
                end = time.time() + budget
                while time.time() < end:
                    outcome, snap = poll_once(learner, last_seq)
                    if snap is not None:
                        last_seq = int(snap.get("seq", last_seq))
                        task = next((t for t in snap.get("torrents", [])
                                     if t["infohash"] == infohash), None)
                        if task is not None and task.get("state") == want:
                            return task
                    time.sleep(POLL_INTERVAL_S)
                return None

            c.check_eq(learner.action(infohash, "pause")[0], 200, "E 点暂停返回 200")
            paused = wait_for_state("paused")
            c.check(paused is not None,
                    "E 前端视角看到了 paused —— 点暂停后界面真的会变")
            if paused is not None:
                c.check_eq(paused.get("progress"), 1.0,
                           "E 暂停后进度仍是 1.0（没有因为暂停把进度清零）")

            c.check_eq(learner.action(infohash, "resume")[0], 200, "E 点继续返回 200")
            resumed = wait_for_state("seeding", budget=10.0)
            c.check(resumed is not None,
                    "E 前端视角回到 seeding —— 点继续后界面也会变")
        finally:
            if learner is not None:
                learner.stop()
            seeder.stop()
    c.finish()


if __name__ == "__main__":
    main()
