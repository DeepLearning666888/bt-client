"""DHT 连通性采样（换网复测）：带网络跑 90 秒，观察 routing_table_nodes 是否增长。

结果直接写 stdout + 退出码表达结论，便于脚本化判定。
"""
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common as c


def wait_dht(engine, timeout=20.0):
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


def main():
    engine = c.Engine(no_network=False).start()
    dht = wait_dht(engine)
    if dht is None:
        print("FAIL: 20 秒内拿不到 DHT 快照")
        engine.stop()
        sys.exit(2)

    print(f"首个快照: running={dht.get('running')} buckets={dht.get('buckets')} "
          f"nodes={dht.get('routing_table_nodes')}", flush=True)

    started = time.time()
    max_nodes = 0
    first = None
    while time.time() - started < 90:
        status, body = engine.get("/dht", timeout=3.0)
        if status == 200:
            d = json.loads(body)
            n = d.get("routing_table_nodes", 0)
            if n > max_nodes:
                max_nodes = n
                if first is None:
                    first = time.time() - started
            if n > 0 or int((time.time() - started) % 10) == 0:
                print(f"t={time.time()-started:5.1f}s nodes={n:3d} buckets={d.get('buckets')}",
                      flush=True)
        time.sleep(2.0)

    engine.stop()
    print()
    if max_nodes > 0:
        print(f"PASS: 90 秒内 DHT 路由表最多 {max_nodes} 个节点，首个出现在 {first:.1f}s")
        sys.exit(0)
    else:
        print("FAIL: 90 秒内 routing_table_nodes 始终为 0（DHT 未连上任何节点）")
        sys.exit(1)


if __name__ == "__main__":
    main()
