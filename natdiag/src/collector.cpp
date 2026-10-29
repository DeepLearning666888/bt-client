#include <natdiag/natdiag.hpp>

namespace natdiag {

char const* to_string(Transport t) {
    switch (t) {
        case Transport::natpmp: return "natpmp";
        case Transport::upnp:   return "upnp";
    }
    return "unknown";
}

char const* to_string(Protocol p) {
    switch (p) {
        case Protocol::tcp: return "tcp";
        case Protocol::udp: return "udp";
    }
    return "unknown";
}

char const* to_string(SlotState s) {
    switch (s) {
        case SlotState::unmapped: return "unmapped";
        case SlotState::pending:  return "pending";
        case SlotState::mapped:   return "mapped";
    }
    return "unknown";
}

char const* to_string(Verdict v) {
    switch (v) {
        case Verdict::working:      return "working";
        case Verdict::refused:      return "refused";
        case Verdict::silent:       return "silent";
        case Verdict::disabled:     return "disabled";
        case Verdict::insufficient: return "insufficient";
    }
    return "unknown";
}

char const* to_string(Remediation r) {
    switch (r) {
        case Remediation::none:                return "none";
        case Remediation::manual_port_forward: return "manual_port_forward";
        case Remediation::check_firewall:      return "check_firewall";
        case Remediation::enable_protocol:     return "enable_protocol";
        case Remediation::sample_longer:       return "sample_longer";
    }
    return "unknown";
}

int slot_index(Transport t, Protocol p) {
    int base = (t == Transport::upnp) ? 2 : 0;
    return base + ((p == Protocol::udp) ? 1 : 0);
}

Transport slot_transport(int idx) {
    return (idx >= 2) ? Transport::upnp : Transport::natpmp;
}

Protocol slot_protocol(int idx) {
    return (idx % 2 == 0) ? Protocol::tcp : Protocol::udp;
}

Collector::Collector(int min_sample_seconds)
    : min_sample_seconds_(min_sample_seconds) {
    for (int i = 0; i < kSlotCount; ++i) {
        slots_[i].transport = slot_transport(i);
        slots_[i].protocol = slot_protocol(i);
    }
}

void Collector::set_elapsed(int seconds) {
    if (seconds > sampled_seconds_) sampled_seconds_ = seconds;
}

void Collector::feed(NatEvent const& ev) {
    switch (ev.kind) {
        case NatEvent::Kind::listen_ok:
            listen_ok_.push_back(ev.detail);
            return;

        case NatEvent::Kind::listen_fail:
            listen_fail_.push_back(ev.detail);
            return;

        case NatEvent::Kind::external_ip:
            external_ip_ = ev.detail;
            return;

        case NatEvent::Kind::map_log: {
            // 日志来自同一个 mapper，无法区分 tcp/udp：
            // 该 transport 下两个 slot 都计一份证据。
            for (int i = 0; i < kSlotCount; ++i) {
                if (slots_[i].transport != ev.transport) continue;
                ++slots_[i].log_lines;
                // 日志说明请求确实发出去了，不只是"没试过"。
                // 已 mapped 的不能被日志降级回 pending。
                if (slots_[i].state == SlotState::unmapped) {
                    slots_[i].state = SlotState::pending;
                }
            }
            return;
        }

        case NatEvent::Kind::map_ok: {
            SlotInfo& slot = slots_[slot_index(ev.transport, ev.protocol)];
            slot.state = SlotState::mapped;
            slot.external_port = ev.port;
            return;
        }

        case NatEvent::Kind::map_err: {
            // 用 vector 而不是 map：原实现对同一 transport 的多条错误
            // 会互相覆盖，只剩最后一条。不覆盖才看得见"两个协议都试过、都没成"。
            std::string line = to_string(ev.transport);
            if (ev.error_code != 0) line += " error " + std::to_string(ev.error_code);
            if (!ev.detail.empty()) line += " -- " + ev.detail;
            unattributed_.push_back(line);
            return;
        }
    }
}

bool Collector::has_inbound_mapping() const {
    for (int i = 0; i < kSlotCount; ++i) {
        if (slots_[i].state == SlotState::mapped) return true;
    }
    return false;
}

}  // namespace natdiag
