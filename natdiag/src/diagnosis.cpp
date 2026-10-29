#include <natdiag/natdiag.hpp>

namespace natdiag {
namespace {

struct Evaluation {
    Verdict verdict;
    std::vector<Remediation> remediation;
};

// 判定优先级（顺序即优先级）：
//   1. 任一 slot 拿到映射 —— 硬证据，任何采样时长下都算 working
//   2. 存在明确错误码 —— 也是硬证据，不受采样时长限制
//   3. 采样不足 —— 没有硬证据时才不敢下结论
//   4. 有请求发出但无回音 —— silent
//   5. 什么都没有 —— 协议没启用
Evaluation evaluate(SlotInfo const* slots, int n,
                    std::vector<std::string> const& unattributed,
                    int sampled_seconds, int min_sample_seconds) {
    bool any_mapped = false;
    bool any_request_evidence = false;

    for (int i = 0; i < n; ++i) {
        if (slots[i].state == SlotState::mapped) any_mapped = true;
        // log_lines > 0 也算发过请求：日志来自 mapper，
        // 说明协议确实在跑，只是没走到成功或明确的失败。
        if (slots[i].state == SlotState::pending || slots[i].log_lines > 0) {
            any_request_evidence = true;
        }
    }

    if (any_mapped) {
        return {Verdict::working, {Remediation::none}};
    }
    if (!unattributed.empty()) {
        return {Verdict::refused, {Remediation::manual_port_forward}};
    }
    if (sampled_seconds < min_sample_seconds) {
        return {Verdict::insufficient, {Remediation::sample_longer}};
    }
    if (any_request_evidence) {
        return {Verdict::silent,
                {Remediation::manual_port_forward, Remediation::check_firewall}};
    }
    return {Verdict::disabled, {Remediation::enable_protocol}};
}

}  // namespace

Snapshot Collector::snapshot() const {
    Snapshot s;
    s.listen_ok = listen_ok_;
    s.listen_fail = listen_fail_;
    for (int i = 0; i < kSlotCount; ++i) s.slots[i] = slots_[i];
    s.external_ip = external_ip_;
    s.unattributed_errors = unattributed_;
    s.sampled_seconds = sampled_seconds_;
    s.min_sample_seconds = min_sample_seconds_;
    s.external_ip_reliable = has_inbound_mapping();

    Evaluation const e =
        evaluate(slots_, kSlotCount, unattributed_, sampled_seconds_, min_sample_seconds_);
    s.verdict = e.verdict;
    s.remediation = e.remediation;
    return s;
}

}  // namespace natdiag
