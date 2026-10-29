"""AC-12 /state 内含 nat 字段且结构符合 natdiag 快照契约。

这条是引擎与 natdiag 之间的接口契约。natdiag 自己的单测只保证模块内部正确，
不保证引擎把它挂对了位置、没丢字段。
"""

import common as c

TOP_KEYS = ("verdict", "verdict_text", "remediation", "sampled_seconds",
            "min_sample_seconds", "slots", "listen", "external_ip",
            "external_ip_reliable", "unattributed_errors")
SLOT_KEYS = ("transport", "protocol", "state", "external_port", "log_lines")
VERDICTS = {"working", "refused", "silent", "disabled", "insufficient"}


def main() -> None:
    engine = c.Engine().start()
    try:
        nat = engine.state().get("nat")
        if nat is None:
            c.check(False, "/state 缺少 nat 字段")
            return
        c.check(True, "/state 含 nat 字段")

        for key in TOP_KEYS:
            c.check(key in nat, f"nat 含 {key}")

        c.check_eq(len(nat["slots"]), 4, "槽位数量为 4")
        for index, slot in enumerate(nat["slots"]):
            missing = [k for k in SLOT_KEYS if k not in slot]
            c.check(not missing, f"槽位 {index} 字段完整（缺 {missing}）")
            c.check(slot["transport"] in ("natpmp", "upnp"), f"槽位 {index} transport 合法")
            c.check(slot["protocol"] in ("tcp", "udp"), f"槽位 {index} protocol 合法")
            c.check(slot["state"] in ("unmapped", "pending", "mapped"),
                    f"槽位 {index} state 合法")

        c.check(nat["verdict"] in VERDICTS, f"verdict 取值合法（{nat['verdict']}）")
        c.check(isinstance(nat["external_ip_reliable"], bool), "external_ip_reliable 是布尔")
        c.check(isinstance(nat["listen"], dict) and "ok" in nat["listen"]
                and "failed" in nat["listen"], "listen 含 ok / failed")
        c.check(isinstance(nat["unattributed_errors"], list), "unattributed_errors 是数组")
        c.check(isinstance(nat["remediation"], list), "remediation 是数组")
        c.check_eq(nat["min_sample_seconds"], 15, "最小采样阈值与 natdiag 默认一致")
    finally:
        engine.stop()
    c.finish()


if __name__ == "__main__":
    main()
