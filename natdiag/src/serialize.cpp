#include <natdiag/serialize.hpp>

#include <cstdio>
#include <string>

namespace natdiag {
namespace {

// 手写转义。字段固定，不值得为它引一个 JSON 库；
// 但错误文本里有 Windows 路径和网关返回的字符串，反斜杠和引号必须处理。
void append_json_string(std::string& out, std::string const& v) {
    out.push_back('"');
    for (unsigned char c : v) {
        switch (c) {
            case '"':  out += "\\\""; break;
            case '\\': out += "\\\\"; break;
            case '\n': out += "\\n";  break;
            case '\r': out += "\\r";  break;
            case '\t': out += "\\t";  break;
            default:
                if (c < 0x20) {
                    char buf[8];
                    std::snprintf(buf, sizeof buf, "\\u%04x", static_cast<unsigned>(c));
                    out += buf;
                } else {
                    // UTF-8 原样输出，JSON 允许
                    out.push_back(static_cast<char>(c));
                }
        }
    }
    out.push_back('"');
}

void append_string_array(std::string& out, std::vector<std::string> const& v) {
    out.push_back('[');
    for (std::size_t i = 0; i < v.size(); ++i) {
        if (i) out += ", ";
        append_json_string(out, v[i]);
    }
    out.push_back(']');
}

char const* display_transport(Transport t) {
    return t == Transport::upnp ? "UPnP-IGD" : "NAT-PMP/PCP";
}

std::string verdict_sentence_zh(Snapshot const& s) {
    switch (s.verdict) {
        case Verdict::working: {
            for (int i = 0; i < kSlotCount; ++i) {
                if (s.slots[i].state == SlotState::mapped) {
                    char buf[160];
                    std::snprintf(buf, sizeof buf, "%s %s 已建立映射，外部端口 %d",
                                  display_transport(s.slots[i].transport),
                                  to_string(s.slots[i].protocol),
                                  s.slots[i].external_port);
                    return buf;
                }
            }
            return "已建立入站映射";
        }
        case Verdict::refused:
            return "网关/设备明确拒绝了映射请求，看错误码";
        case Verdict::silent: {
            char buf[200];
            std::snprintf(buf, sizeof buf,
                          "%d 秒内所有映射请求均无应答（各类协议均发过请求但仍无回音）",
                          s.sampled_seconds);
            return buf;
        }
        case Verdict::disabled:
            return "端口映射协议未启用，没有任何映射请求发出";
        case Verdict::insufficient: {
            char buf[200];
            std::snprintf(buf, sizeof buf,
                          "只采样了 %d 秒，短于 %d 秒阈值，不下结论",
                          s.sampled_seconds, s.min_sample_seconds);
            return buf;
        }
    }
    return "";
}

// CLI 与日志走英文：Windows 控制台代码页不一定能显示中文，
// 而这段文本是给人肉眼扫和给 diff 做回归的，不能出现乱码或编码漂移。
std::string verdict_sentence_en(Snapshot const& s) {
    char buf[240];
    switch (s.verdict) {
        case Verdict::working:
            for (int i = 0; i < kSlotCount; ++i) {
                if (s.slots[i].state == SlotState::mapped) {
                    std::snprintf(buf, sizeof buf, "mapping established, external port %d",
                                  s.slots[i].external_port);
                    return buf;
                }
            }
            return "mapping established";
        case Verdict::refused:
            return "gateway refused the mapping request";
        case Verdict::silent:
            std::snprintf(buf, sizeof buf,
                          "all mapping requests got no reply within %ds", s.sampled_seconds);
            return buf;
        case Verdict::disabled:
            return "port mapping protocols disabled, nothing was requested";
        case Verdict::insufficient:
            std::snprintf(buf, sizeof buf, "sampled %ds, shorter than %ds threshold",
                          s.sampled_seconds, s.min_sample_seconds);
            return buf;
    }
    return "";
}

}  // namespace

std::string to_json(Snapshot const& s) {
    std::string o;
    o.reserve(1024);
    o += "{\n  ";

    o += "\"verdict\": ";
    append_json_string(o, to_string(s.verdict));
    o += ",\n  \"verdict_text\": ";
    append_json_string(o, verdict_sentence_zh(s));

    o += ",\n  \"remediation\": [";
    for (std::size_t i = 0; i < s.remediation.size(); ++i) {
        if (i) o += ", ";
        append_json_string(o, to_string(s.remediation[i]));
    }
    o += "]";

    char buf[96];
    std::snprintf(buf, sizeof buf, ",\n  \"sampled_seconds\": %d,\n  \"min_sample_seconds\": %d",
                  s.sampled_seconds, s.min_sample_seconds);
    o += buf;

    o += ",\n  \"slots\": [\n";
    for (int i = 0; i < kSlotCount; ++i) {
        SlotInfo const& sl = s.slots[i];
        o += "    { \"transport\": ";
        append_json_string(o, to_string(sl.transport));
        o += ", \"protocol\": ";
        append_json_string(o, to_string(sl.protocol));
        o += ", \"state\": ";
        append_json_string(o, to_string(sl.state));

        if (sl.state == SlotState::mapped) {
            std::snprintf(buf, sizeof buf, ", \"external_port\": %d", sl.external_port);
            o += buf;
        } else {
            o += ", \"external_port\": null";
        }
        std::snprintf(buf, sizeof buf, ", \"log_lines\": %d }", sl.log_lines);
        o += buf;
        o += (i + 1 < kSlotCount) ? ",\n" : "\n";
    }
    o += "  ]";

    o += ",\n  \"listen\": { \"ok\": ";
    append_string_array(o, s.listen_ok);
    o += ", \"failed\": ";
    append_string_array(o, s.listen_fail);
    o += " }";

    o += ",\n  \"unattributed_errors\": ";
    append_string_array(o, s.unattributed_errors);

    o += ",\n  \"external_ip\": ";
    append_json_string(o, s.external_ip);
    o += ",\n  \"external_ip_reliable\": ";
    o += s.external_ip_reliable ? "true" : "false";
    o += "\n}\n";
    return o;
}

std::string to_text(Snapshot const& s) {
    std::string o;
    o.reserve(1024);
    char buf[512];

    // 统一 15 列对齐。原来手写空格，slot 是 15、其余是 14，差一位很难看。
    auto label = [](char const* l) {
        char b[48];
        std::snprintf(b, sizeof b, "%-16s: ", l);
        return std::string(b);
    };

    std::snprintf(buf, sizeof buf, "=== natdiag ===\n%s%zu socket(s) bound\n",
                  label("listen").c_str(), s.listen_ok.size());
    o += buf;
    for (auto const& l : s.listen_ok) {
        o += "                  " + l + "\n";
    }
    for (auto const& l : s.listen_fail) {
        o += "                  failed: " + l + "\n";
    }

    // label 沿用旧版格式，回归 diff 时能直接对上
    for (int i = 0; i < kSlotCount; ++i) {
        SlotInfo const& sl = s.slots[i];
        o += label((std::string(display_transport(sl.transport)) + " " +
                    (sl.protocol == Protocol::tcp ? "TCP" : "UDP"))
                       .c_str());

        switch (sl.state) {
            case SlotState::mapped:
                std::snprintf(buf, sizeof buf, "MAPPED -- external port %d\n", sl.external_port);
                o += buf;
                break;
            case SlotState::pending:
                std::snprintf(buf, sizeof buf, "no inbound mapping reply (%ds, %d log line(s))\n",
                              s.sampled_seconds, sl.log_lines);
                o += buf;
                break;
            case SlotState::unmapped:
                // 主路径：有日志的 slot 已在 collector 里推进为 pending，
                // 走到这里通常是真的没试过。
                // 保留 log_lines>0 的兜底：展示层若在上游异常时硬说
                // "not attempted" 等于撒谎，宁可显示得含糊一点。
                if (sl.log_lines > 0) {
                    std::snprintf(buf, sizeof buf,
                                  "in flight, no result yet (%ds, %d log line(s))\n",
                                  s.sampled_seconds, sl.log_lines);
                } else {
                    std::snprintf(buf, sizeof buf, "not attempted\n");
                }
                o += buf;
                break;
        }
    }

    for (auto const& e : s.unattributed_errors) {
        o += label("unattributed") + e + "\n";
    }

    if (s.external_ip.empty()) {
        o += label("external ip") + "not reported\n";
    } else if (s.external_ip_reliable) {
        o += label("external ip") + s.external_ip + "\n";
    } else {
        // 这条提示是代码级的防误读：拿到外网 IP 不代表能入站
        o += label("external ip") + s.external_ip +
             " (unreliable -- no mapping established)\n";
    }

    o += label("reason") + verdict_sentence_en(s) + "\n";
    o += label("action");
    for (std::size_t i = 0; i < s.remediation.size(); ++i) {
        if (i) o += ", ";
        o += to_string(s.remediation[i]);
    }
    o += "\n";

    o += "\nVERDICT: ";
    o += (s.verdict == Verdict::working)
             ? "automatic inbound mapping WORKS on this network"
             : "automatic inbound mapping UNAVAILABLE on this network";
    o += "\n";
    return o;
}

}  // namespace natdiag
