#include "helpers.hpp"
#include "test_framework.hpp"

#include <natdiag/natdiag.hpp>

using natdiag::Collector;
using natdiag::Protocol;
using natdiag::SlotState;
using natdiag::Transport;
using natdiag::Verdict;

TEST_CASE(empty_collector_is_disabled) {
    Collector c;
    c.set_elapsed(60);
    natdiag::Snapshot s = c.snapshot();

    CHECK(s.verdict == Verdict::disabled);
    CHECK(!c.has_inbound_mapping());
    for (int i = 0; i < natdiag::kSlotCount; ++i) {
        CHECK(s.slots[i].state == SlotState::unmapped);
        CHECK_INT_EQ(s.slots[i].log_lines, 0);
    }
}

TEST_CASE(map_ok_makes_verdict_working) {
    Collector c;
    c.set_elapsed(60);
    c.feed(th::map_ok(Transport::natpmp, Protocol::tcp, 6881));

    natdiag::Snapshot s = c.snapshot();
    CHECK(s.verdict == Verdict::working);
    CHECK(c.has_inbound_mapping());
    CHECK_INT_EQ(s.slots[0].external_port, 6881);
    CHECK(s.slots[0].state == SlotState::mapped);
    // 其它三个组合不应被顺带标成成功
    CHECK(s.slots[1].state == SlotState::unmapped);
    CHECK(s.slots[2].state == SlotState::unmapped);
    CHECK(s.slots[3].state == SlotState::unmapped);
}

// 回归：原实现把错误存进 map，同一 transport 的第二条会覆盖第一条。
TEST_CASE(errors_on_same_transport_are_all_kept) {
    Collector c;
    c.set_elapsed(60);
    c.feed(th::map_err(Transport::natpmp, 701, "tcp attempt failed"));
    c.feed(th::map_err(Transport::natpmp, 702, "udp attempt failed"));

    natdiag::Snapshot s = c.snapshot();
    CHECK_INT_EQ(s.unattributed_errors.size(), 2);
    CHECK(s.verdict == Verdict::refused);
}

TEST_CASE(explicit_error_makes_verdict_refused) {
    Collector c;
    c.set_elapsed(60);
    c.feed(th::map_err(Transport::upnp, 403, "Forbidden"));

    natdiag::Snapshot s = c.snapshot();
    CHECK(s.verdict == Verdict::refused);
    CHECK_INT_EQ(s.remediation.size(), 1);
    CHECK(s.remediation[0] == natdiag::Remediation::manual_port_forward);
    CHECK_STR_EQ(s.unattributed_errors[0], "upnp error 403 -- Forbidden");
}

// 外部 IP 只有一个来源不够：libtorrent 也会从 peer 处得知。
TEST_CASE(external_ip_alone_does_not_imply_inbound) {
    Collector c;
    c.set_elapsed(60);
    c.feed(th::external_ip("203.0.113.7"));

    natdiag::Snapshot s = c.snapshot();
    CHECK(!s.external_ip_reliable);
    CHECK(s.verdict != Verdict::working);
    CHECK_STR_EQ(s.external_ip, "203.0.113.7");
}

TEST_CASE(external_ip_becomes_reliable_only_with_mapping) {
    Collector c;
    c.set_elapsed(60);
    c.feed(th::external_ip("203.0.113.7"));
    c.feed(th::map_ok(Transport::upnp, Protocol::udp, 6881));

    CHECK(c.snapshot().external_ip_reliable);
}

TEST_CASE(log_lines_mark_request_evidence) {
    Collector c;
    c.set_elapsed(60);
    c.feed(th::map_log(Transport::natpmp));
    c.feed(th::map_log(Transport::natpmp));
    c.feed(th::map_log(Transport::upnp));

    natdiag::Snapshot s = c.snapshot();
    // 有日志说明协议在跑，不能报"没启用"
    CHECK(s.verdict == Verdict::silent);
    // 日志只带 transport，同 transport 的两个 slot 各计一份
    CHECK_INT_EQ(s.slots[0].log_lines, 2);
    CHECK_INT_EQ(s.slots[1].log_lines, 2);
    CHECK_INT_EQ(s.slots[2].log_lines, 1);
    CHECK_INT_EQ(s.slots[3].log_lines, 1);
    // 状态推进为 pending：请求确实发出去了，只是既没成功也没失败。
    // 修复前这里断言的是 unmapped，那会让"试过没回音"和"根本没试"
    // 在数据上无法区分——这正是 D-01 要修的问题。
    CHECK(s.slots[0].state == SlotState::pending);
}

TEST_CASE(short_sample_is_insufficient) {
    Collector c;
    c.set_elapsed(5);
    c.feed(th::map_log(Transport::natpmp));

    natdiag::Snapshot s = c.snapshot();
    CHECK(s.verdict == Verdict::insufficient);
    CHECK_INT_EQ(s.sampled_seconds, 5);
    CHECK_INT_EQ(s.min_sample_seconds, natdiag::kDefaultMinSampleSeconds);
}

TEST_CASE(mapping_success_wins_over_short_sample) {
    Collector c;
    c.set_elapsed(3);
    c.feed(th::map_ok(Transport::natpmp, Protocol::udp, 6881));

    CHECK(c.snapshot().verdict == Verdict::working);
}

TEST_CASE(listen_events_are_recorded) {
    Collector c;
    c.set_elapsed(60);
    c.feed(th::listen_ok("TCP 192.168.1.100:6881"));
    c.feed(th::listen_ok("uTP 192.168.1.100:6881"));
    c.feed(th::listen_fail("TCP 0.0.0.0:80 -- permission denied"));

    natdiag::Snapshot s = c.snapshot();
    CHECK_INT_EQ(s.listen_ok.size(), 2);
    CHECK_INT_EQ(s.listen_fail.size(), 1);
    CHECK_STR_EQ(s.listen_fail[0], "TCP 0.0.0.0:80 -- permission denied");
}

TEST_CASE(slot_order_is_stable) {
    Collector c;
    c.feed(th::map_ok(Transport::upnp, Protocol::udp, 7000));

    natdiag::Snapshot s = c.snapshot();
    CHECK(s.slots[0].transport == Transport::natpmp && s.slots[0].protocol == Protocol::tcp);
    CHECK(s.slots[1].transport == Transport::natpmp && s.slots[1].protocol == Protocol::udp);
    CHECK(s.slots[2].transport == Transport::upnp && s.slots[2].protocol == Protocol::tcp);
    CHECK(s.slots[3].transport == Transport::upnp && s.slots[3].protocol == Protocol::udp);
    CHECK(s.slots[3].state == SlotState::mapped);
    CHECK_INT_EQ(s.slots[3].external_port, 7000);
}

TEST_CASE(snapshot_is_repeatable) {
    Collector c;
    c.set_elapsed(60);
    c.feed(th::map_log(Transport::upnp));

    natdiag::Snapshot a = c.snapshot();
    natdiag::Snapshot b = c.snapshot();
    CHECK(a.verdict == b.verdict);
    CHECK_INT_EQ(a.slots[2].log_lines, b.slots[2].log_lines);
}

// D-01 回归：map_log 必须把 slot 从 unmapped 推进到 pending。
// 修复前 SlotState::pending 从未被任何代码设置，是死枚举，
// 导致 spec 5.2 承诺的 state 契约（"pending" 表示请求在飞）无法兑现。
TEST_CASE(map_log_advances_slot_to_pending) {
    Collector c;
    c.set_elapsed(60);
    c.feed(th::map_log(Transport::natpmp));

    natdiag::Snapshot s = c.snapshot();
    // 日志来自该 transport 的 mapper，无法区分 tcp/udp，两个 slot 都推进
    CHECK(s.slots[0].state == SlotState::pending);
    CHECK(s.slots[1].state == SlotState::pending);
    CHECK_INT_EQ(s.slots[0].log_lines, 1);
    CHECK_INT_EQ(s.slots[1].log_lines, 1);
    // 另一个 transport 不受影响，仍为未尝试
    CHECK(s.slots[2].state == SlotState::unmapped);
    CHECK(s.slots[3].state == SlotState::unmapped);
    CHECK_INT_EQ(s.slots[2].log_lines, 0);
}

// 已 mapped 的 slot 不能被后续日志降级回 pending
TEST_CASE(map_log_does_not_downgrade_mapped_slot) {
    Collector c;
    c.set_elapsed(60);
    c.feed(th::map_ok(Transport::upnp, Protocol::tcp, 6881));
    c.feed(th::map_log(Transport::upnp));

    natdiag::Snapshot s = c.snapshot();
    CHECK(s.slots[2].state == SlotState::mapped);
    CHECK_INT_EQ(s.slots[2].external_port, 6881);
    // 同 transport 下未映射的那个才被推进
    CHECK(s.slots[3].state == SlotState::pending);
    // 有映射就是硬证据，不能被降级
    CHECK(s.verdict == Verdict::working);
}
