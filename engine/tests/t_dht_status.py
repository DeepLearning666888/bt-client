"""DHT 可观测性：/dht 只读诊断端点。

为什么需要这个端点：在此之前引擎完全不暴露 DHT 状态。于是「无 tracker 的种子
零 peer」这件事只能从外部旁证（UDP 探测、DNS 对比），无法区分两种完全不同的情况：

    A) DHT 根本没 bootstrap —— 一个节点都连不上
    B) DHT 起来了，但这个 infohash 在 DHT 网络上确实没人挂

两者的处置完全不同：A 是网络/配置问题，B 是「没有人做种」，都是正常结论，
但混在一起就只能写「不知道」。有了 /dht 就能一次分开。

为什么不并进 /state：/state 的 seq 是对内容做比较算出来的，空闲时不变，前端据此
跳过重绘。DHT 节点数每秒都在跳，塞进去会让空闲时也每拍重渲。B 段专门锁住这条，
免得以后有人图省事把它并回去。

（这句话在 2026-11-05 之前其实是假的 —— 那时 seq 里还含 nat 的 sampled_seconds，
空载也每秒在涨。AC-20 修掉之后才真正成立。）

为什么不读 session::status()：整个 session_status 结构被
`#if TORRENT_ABI_VERSION == 1` 包住，本构建是 ABI v2，那个类型不存在。
ABI v2 的入口是 post_dht_stats() + dht_stats_alert。
"""
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common as c


def wait_dht(engine: c.Engine, timeout: float = 15.0) -> dict | None:
    """等第一份 DHT 快照。启动后最多 2 秒采到，它挂 503 表示还没采到。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        status, body = engine.get("/dht")
        if status == 200 and body:
            try:
                return json.loads(body)
            except json.JSONDecodeError:
                pass
        time.sleep(0.3)
    return None


def main() -> None:
    engine = None
    off = None
    try:
        # ---- A) DHT 开启：端点可用、进程里有 node id
        engine = c.Engine(no_network=False).start()
        started = time.time()
        dht = wait_dht(engine)
        if not c.check(dht is not None, "A /dht 在 15 秒内返回了 JSON"):
            return

        c.check_eq(dht.get("running"), True,
                   "A DHT 报告 running=true（--no-network 时才是 false）")

        nid = str(dht.get("node_id", ""))
        c.check(len(nid) == 40 and all(ch in "0123456789abcdef" for ch in nid),
                f"A node_id 是 40 位小写 hex（实际 {nid!r}）")
        c.check(nid != "0" * 40, f"A node_id 不是全 0（未启动时才是全 0）")

        c.check(dht.get("buckets", -1) >= 1,
                f"A 路由表至少有 1 个桶（实际 {dht.get('buckets')}）—— "
                f"证明 DHT 不只是「开了」，内部结构真的建起来了")

        for key in ("routing_table_nodes", "replacements", "active_requests"):
            c.check(isinstance(dht.get(key), int) and dht[key] >= 0,
                    f"A {key} 是非负整数（实际 {dht.get(key)!r}）")

        ts = dht.get("ts_ms", 0)
        c.check(abs(ts - time.time() * 1000) < 60_000,
                "A ts_ms 是近期时间戳 —— 快照是活的，不是常量")
        print(f"        采样 {time.time() - started:.1f}s 时："
              f"节点 {dht.get('routing_table_nodes')} / 桶 {dht.get('buckets')} / "
              f"在查 {dht.get('active_requests')}")

        # ---- B) DHT 状态没混进 /state
        #
        # 没有这条，以后有人图省事把 dht_nodes 并回 /state 也能全绿 —— 而那是把
        # 一个 2 秒级的诊断信号塞进 10Hz 的界面状态契约。
        #
        # 这里**不**断言「seq 稳定」—— 那是 AC-20（t_state_seq.py）的职责，那边有
        # 反向对照（nat.sampled_seconds 必须同时在涨，否则「seq 不动」可能只是引擎
        # 卡死）。2026-11-05 之前写不了那条断言：/state 的 content 里含 nat 的
        # sampled_seconds，空载时 seq 也每秒在涨（实测 1→3→4→5）。现在 seq 只看任务列表。
        state = engine.state()
        for key in ("dht", "dht_nodes", "dht_routing_table", "node_id"):
            c.check(key not in state,
                    f"B /state 顶层没有 {key} 字段 —— DHT 诊断走独立端点")
        c.check("dht" not in json.dumps(state.get("nat", {})),
                "B nat 块里也没有夹带 DHT 字段")
        engine.stop()
        engine = None

        # ---- C) DHT 关闭：必须如实报告未运行，不能沿用上一份快照
        off = c.Engine(no_network=True).start()
        dht_off = wait_dht(off)
        if not c.check(dht_off is not None, "C --no-network 下 /dht 仍可读（报告未运行）"):
            return
        c.check_eq(dht_off.get("running"), False, "C --no-network 时 running=false")
        c.check_eq(dht_off.get("buckets"), 0,
                   f"C --no-network 时桶数为 0（实际 {dht_off.get('buckets')}）")
        c.check_eq(dht_off.get("node_id"), "0" * 40,
                   "C --no-network 时 node_id 全 0 —— 没有编一个假 ID 出来")
    finally:
        if engine is not None:
            engine.stop()
        if off is not None:
            off.stop()
    c.finish()


if __name__ == "__main__":
    main()
