#include <natdiag/lt_adapt.hpp>

#include <libtorrent/alert_types.hpp>

#include <cstdio>
#include <string>

namespace natdiag {
namespace {

namespace lt = libtorrent;

// libtorrent 里有 lt::socket_type_name()，但没有从 DLL 导出，链接不到；
// 用不同名字自己实现，避免 ADL 同时看到两个候选。
char const* sock_type_str(lt::socket_type_t t) {
    switch (t) {
        case lt::socket_type_t::tcp:     return "TCP";
        case lt::socket_type_t::tcp_ssl: return "TCP/SSL";
        case lt::socket_type_t::utp:     return "uTP";
        case lt::socket_type_t::utp_ssl: return "uTP/SSL";
        case lt::socket_type_t::i2p:     return "i2p";
        case lt::socket_type_t::socks5:  return "socks5";
    }
    return "other";
}

Transport to_transport(lt::portmap_transport t) {
    return t == lt::portmap_transport::upnp ? Transport::upnp : Transport::natpmp;
}

Protocol to_protocol(lt::portmap_protocol p) {
    return p == lt::portmap_protocol::udp ? Protocol::udp : Protocol::tcp;
}

}  // namespace

bool from_alert(lt::alert const& a, NatEvent& out) {
    if (auto* ls = lt::alert_cast<lt::listen_succeeded_alert>(&a)) {
        char buf[160];
        std::snprintf(buf, sizeof buf, "%s %s:%d", sock_type_str(ls->socket_type),
                      ls->address.to_string().c_str(), ls->port);
        out = NatEvent{};
        out.kind = NatEvent::Kind::listen_ok;
        out.port = ls->port;
        out.detail = buf;
        return true;
    }

    if (auto* lf = lt::alert_cast<lt::listen_failed_alert>(&a)) {
        char buf[240];
        std::snprintf(buf, sizeof buf, "%s %s:%d -- %s", sock_type_str(lf->socket_type),
                      lf->address.to_string().c_str(), lf->port,
                      lf->error.message().c_str());
        out = NatEvent{};
        out.kind = NatEvent::Kind::listen_fail;
        out.port = lf->port;
        out.error_code = lf->error.value();
        out.detail = buf;
        return true;
    }

    if (auto* pm = lt::alert_cast<lt::portmap_alert>(&a)) {
        out = NatEvent{};
        out.kind = NatEvent::Kind::map_ok;
        out.transport = to_transport(pm->map_transport);
        out.protocol = to_protocol(pm->map_protocol);
        out.port = pm->external_port;
        std::string const local_addr = pm->local_address.to_string();
        char buf[200];
        std::snprintf(buf, sizeof buf, "external port %d (gateway seen at %s)", pm->external_port,
                      local_addr.c_str());
        out.detail = buf;
        return true;
    }

    if (auto* pe = lt::alert_cast<lt::portmap_error_alert>(&a)) {
        // 这个 alert 不带 protocol，只有 mapping 索引，而索引是 per-socket 的。
        // 归属到具体协议需要赌未公开实现的索引分配顺序，这里不猜，
        // 原样交给 collector 记进 unattributed_errors。
        out = NatEvent{};
        out.kind = NatEvent::Kind::map_err;
        out.transport = to_transport(pe->map_transport);
        out.error_code = pe->error.value();
        out.detail = pe->error.message();
        return true;
    }

    if (auto* pl = lt::alert_cast<lt::portmap_log_alert>(&a)) {
        out = NatEvent{};
        out.kind = NatEvent::Kind::map_log;
        out.transport = to_transport(pl->map_transport);
        // collector 只按 transport 计数，不存日志正文，避免无谓的内存增长
        return true;
    }

    if (auto* ip = lt::alert_cast<lt::external_ip_alert>(&a)) {
        out = NatEvent{};
        out.kind = NatEvent::Kind::external_ip;
        out.detail = ip->external_address.to_string();
        return true;
    }

    return false;
}

}  // namespace natdiag
