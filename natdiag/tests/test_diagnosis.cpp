#include "helpers.hpp"
#include "test_framework.hpp"

#include <natdiag/natdiag.hpp>
#include <natdiag/serialize.hpp>

#include <string>

using natdiag::Collector;
using natdiag::Protocol;
using natdiag::Transport;
using natdiag::Verdict;

// —— 判定表逐行 ——

TEST_CASE(verdict_working) {
    Collector c;
    c.set_elapsed(60);
    c.feed(th::map_ok(Transport::natpmp, Protocol::tcp, 6881));
    CHECK(c.snapshot().verdict == Verdict::working);
}

TEST_CASE(verdict_refused) {
    Collector c;
    c.set_elapsed(60);
    c.feed(th::map_err(Transport::upnp, 501, "Action Failed"));
    CHECK(c.snapshot().verdict == Verdict::refused);
}

TEST_CASE(verdict_silent) {
    Collector c;
    c.set_elapsed(60);
    for (int i = 0; i < 5; ++i) c.feed(th::map_log(Transport::natpmp));
    for (int i = 0; i < 8; ++i) c.feed(th::map_log(Transport::upnp));
    CHECK(c.snapshot().verdict == Verdict::silent);
}

TEST_CASE(verdict_disabled) {
    Collector c;
    c.set_elapsed(60);
    CHECK(c.snapshot().verdict == Verdict::disabled);
}

TEST_CASE(verdict_insufficient) {
    Collector c;
    c.set_elapsed(2);
    CHECK(c.snapshot().verdict == Verdict::insufficient);
}

// —— 优先级 ——

TEST_CASE(priority_mapped_over_refused) {
    Collector c;
    c.set_elapsed(60);
    c.feed(th::map_ok(Transport::upnp, Protocol::tcp, 6881));
    c.feed(th::map_err(Transport::natpmp, 718, "conflict"));
    // 只要有一个组合通了，整体就是通的
    CHECK(c.snapshot().verdict == Verdict::working);
}

TEST_CASE(priority_refused_over_insufficient) {
    Collector c;
    c.set_elapsed(2);
    c.feed(th::map_err(Transport::natpmp, 403, "Forbidden"));
    // 错误码是硬证据，不受采样时长限制
    CHECK(c.snapshot().verdict == Verdict::refused);
}

TEST_CASE(insufficient_does_not_override_silent_evidence) {
    Collector c;
    c.set_elapsed(60);
    c.feed(th::map_log(Transport::upnp));
    CHECK(c.snapshot().verdict == Verdict::silent);
}

// —— 序列化契约 ——

TEST_CASE(json_has_required_fields) {
    Collector c;
    c.set_elapsed(60);
    c.feed(th::external_ip("203.0.113.7"));
    c.feed(th::listen_ok("TCP 192.168.1.100:6881"));
    c.feed(th::map_log(Transport::natpmp));

    std::string const j = natdiag::to_json(c.snapshot());
    CHECK(j.find("\"verdict\": \"silent\"") != std::string::npos);
    CHECK(j.find("\"external_ip_reliable\": false") != std::string::npos);
    CHECK(j.find("\"sampled_seconds\": 60") != std::string::npos);
    CHECK(j.find("\"min_sample_seconds\": 15") != std::string::npos);
    CHECK(j.find("\"state\": \"unmapped\"") != std::string::npos);
    CHECK(j.find("\"external_port\": null") != std::string::npos);
    CHECK(j.find("\"TCP 192.168.1.100:6881\"") != std::string::npos);
    // 四个 slot 一个都不能少
    CHECK(j.find("\"transport\": \"natpmp\"") != std::string::npos);
    CHECK(j.find("\"transport\": \"upnp\"") != std::string::npos);
    // 收尾必须是合法 JSON 的右括号
    CHECK(j.rfind('}') != std::string::npos);
}

// D-01 回归：pending 必须真的出现在 JSON 输出里。
// 只改内部状态而不验证序列化，等于契约仍停留在纸面——
// 这个项目已经出现过两次"代码写了但实际不产生"的情况。
TEST_CASE(json_reports_pending_state_after_map_log) {
    Collector c;
    c.set_elapsed(60);
    c.feed(th::map_log(Transport::upnp));

    std::string const j = natdiag::to_json(c.snapshot());
    CHECK(j.find("\"state\": \"pending\"") != std::string::npos);
    // 未被触动的 transport 仍报 unmapped，两种状态能区分开
    CHECK(j.find("\"state\": \"unmapped\"") != std::string::npos);
}

TEST_CASE(json_escapes_quotes_in_error_text) {
    Collector c;
    c.set_elapsed(60);
    c.feed(th::map_err(Transport::upnp, 403, "device said \"no\" B\\s"));

    std::string const j = natdiag::to_json(c.snapshot());
    CHECK(j.find("\\\"no\\\"") != std::string::npos);
    CHECK(j.find("B\\\\s") != std::string::npos);
}

TEST_CASE(text_reports_unavailable_verdict_line) {
    Collector c;
    c.set_elapsed(60);
    c.feed(th::map_log(Transport::natpmp));

    std::string const t = natdiag::to_text(c.snapshot());
    CHECK(t.find("VERDICT: automatic inbound mapping UNAVAILABLE on this network") !=
          std::string::npos);
    // 旧版固定的四种组合标签都要在，回归 diff 才能直接对上
    CHECK(t.find("NAT-PMP/PCP TCP") != std::string::npos);
    CHECK(t.find("NAT-PMP/PCP UDP") != std::string::npos);
    CHECK(t.find("UPnP-IGD TCP") != std::string::npos);
    CHECK(t.find("UPnP-IGD UDP") != std::string::npos);
}

TEST_CASE(text_reports_works_verdict_line) {
    Collector c;
    c.set_elapsed(60);
    c.feed(th::map_ok(Transport::natpmp, Protocol::tcp, 6881));

    std::string const t = natdiag::to_text(c.snapshot());
    CHECK(t.find("VERDICT: automatic inbound mapping WORKS on this network") != std::string::npos);
    CHECK(t.find("MAPPED -- external port 6881") != std::string::npos);
}
