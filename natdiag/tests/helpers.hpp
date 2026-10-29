#ifndef NATDIAG_TEST_HELPERS_HPP
#define NATDIAG_TEST_HELPERS_HPP

#include <natdiag/natdiag.hpp>

namespace th {

using natdiag::NatEvent;
using natdiag::Protocol;
using natdiag::Transport;

inline NatEvent map_ok(Transport t, Protocol p, int external_port) {
    NatEvent e;
    e.kind = NatEvent::Kind::map_ok;
    e.transport = t;
    e.protocol = p;
    e.port = external_port;
    return e;
}

inline NatEvent map_err(Transport t, int error_code, char const* msg) {
    NatEvent e;
    e.kind = NatEvent::Kind::map_err;
    e.transport = t;
    e.error_code = error_code;
    e.detail = msg;
    return e;
}

inline NatEvent map_log(Transport t) {
    NatEvent e;
    e.kind = NatEvent::Kind::map_log;
    e.transport = t;
    return e;
}

inline NatEvent external_ip(char const* ip) {
    NatEvent e;
    e.kind = NatEvent::Kind::external_ip;
    e.detail = ip;
    return e;
}

inline NatEvent listen_ok(char const* text) {
    NatEvent e;
    e.kind = NatEvent::Kind::listen_ok;
    e.detail = text;
    return e;
}

inline NatEvent listen_fail(char const* text) {
    NatEvent e;
    e.kind = NatEvent::Kind::listen_fail;
    e.detail = text;
    return e;
}

}  // namespace th

#endif  // NATDIAG_TEST_HELPERS_HPP
