// engine-probe: the first thing to run once libtorrent is built.
//
// Answers the one question M0 could not: can libtorrent actually get an
// inbound port on this network? aria2 could not (no UPnP compiled, TCP-only),
// so the hole in the evidence is exactly here.
//
// It starts a session with every port-mapping protocol enabled and reports,
// per transport x protocol, what libtorrent really got back from the gateway.
//
// Build (依赖来源见 CMakeLists.txt):
//   cmake -B build -DLibtorrentRasterbar_DIR=C:/libtorrent/install/lib/cmake/LibtorrentRasterbar
//   cmake --build build
// Run:
//   engine-probe --magnet "magnet:?xt=urn:btih:..." --seconds 60

#include <libtorrent/alert_types.hpp>
#include <libtorrent/magnet_uri.hpp>
#include <libtorrent/session.hpp>
#include <libtorrent/session_params.hpp>
#include <libtorrent/settings_pack.hpp>
#include <libtorrent/torrent_info.hpp>
#include <libtorrent/version.hpp>

#include <chrono>
#include <cstddef>
#include <cstdio>
#include <cstdlib>
#include <map>
#include <string>
#include <thread>
#include <vector>

namespace lt = libtorrent;

namespace {

struct Options {
    std::string magnet;
    std::string torrent_file;
    std::string save_path = "./probe-download";
    int seconds = 60;
    int listen_port = 6881;
};

Options parse_args(int argc, char** argv) {
    Options o;
    for (int i = 1; i < argc; ++i) {
        std::string a = argv[i];
        auto next = [&](const char* name) -> std::string {
            if (i + 1 >= argc) {
                std::fprintf(stderr, "error: %s needs a value\n", name);
                std::exit(2);
            }
            return argv[++i];
        };
        if (a == "--magnet") {
            o.magnet = next("--magnet");
        } else if (a == "--torrent") {
            o.torrent_file = next("--torrent");
        } else if (a == "--save-path") {
            o.save_path = next("--save-path");
        } else if (a == "--seconds") {
            o.seconds = std::stoi(next("--seconds"));
        } else if (a == "--port") {
            o.listen_port = std::stoi(next("--port"));
        } else if (a == "--help" || a == "-h") {
            std::printf(
                "engine-probe --magnet <uri> | --torrent <file>\n"
                "             [--seconds N] [--port N] [--save-path DIR]\n");
            std::exit(0);
        } else {
            std::fprintf(stderr, "error: unknown argument %s\n", a.c_str());
            std::exit(2);
        }
    }
    if (o.magnet.empty() && o.torrent_file.empty()) {
        std::fprintf(stderr, "error: need --magnet or --torrent\n");
        std::exit(2);
    }
    return o;
}

// sha1_hash 没有十六进制 to_string()，直接打 data() 会是二进制乱码
std::string hex_of(char const* p, std::size_t n) {
    static const char* d = "0123456789abcdef";
    std::string s;
    s.reserve(n * 2);
    for (std::size_t i = 0; i < n; ++i) {
        const unsigned char c = static_cast<unsigned char>(p[i]);
        s.push_back(d[c >> 4]);
        s.push_back(d[c & 0x0f]);
    }
    return s;
}

const char* transport_name(lt::portmap_transport t) {
    switch (t) {
        case lt::portmap_transport::natpmp: return "NAT-PMP/PCP";
        case lt::portmap_transport::upnp:   return "UPnP-IGD";
    }
    return "unknown";
}

const char* protocol_name(lt::portmap_protocol p) {
    switch (p) {
        case lt::portmap_protocol::none: return "-";
        case lt::portmap_protocol::tcp:  return "TCP";
        case lt::portmap_protocol::udp:  return "UDP";
    }
    return "?";
}

std::string key_of(lt::portmap_transport t, lt::portmap_protocol p) {
    return std::string(transport_name(t)) + " " + protocol_name(p);
}

// libtorrent 里有 lt::socket_type_name()，但它没有从 DLL 导出，链接不到；
// 这里用不同名字自己实现，避免 ADL 同时看到两个候选。
const char* sock_type_str(lt::socket_type_t t) {
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

}  // namespace

int main(int argc, char** argv) {
    Options opt = parse_args(argc, argv);

    lt::settings_pack sp = lt::default_settings();
    // every automatic mapping protocol, explicitly on
    sp.set_bool(lt::settings_pack::enable_upnp, true);
    sp.set_bool(lt::settings_pack::enable_natpmp, true);
    sp.set_bool(lt::settings_pack::enable_lsd, true);
    sp.set_bool(lt::settings_pack::enable_dht, true);
    // log every port-mapping step so failures are visible, not silent
    sp.set_int(lt::settings_pack::alert_mask, lt::alert_category::all);
    sp.set_str(lt::settings_pack::listen_interfaces,
               "0.0.0.0:" + std::to_string(opt.listen_port));

    lt::session ses(sp);

    std::printf("=== engine-probe ===\n");
    std::printf("libtorrent : %s\n", lt::version());
    std::printf("listen port: %d (TCP + uTP)\n", opt.listen_port);
    std::printf("protocols  : UPnP=on NAT-PMP/PCP=on LSD=on DHT=on\n");
    std::printf("duration   : %ds\n\n", opt.seconds);

    lt::add_torrent_params atp;
    if (!opt.torrent_file.empty()) {
        atp.ti = std::make_shared<lt::torrent_info>(opt.torrent_file);
        std::printf("source     : torrent file, %d piece(s), %lld bytes\n",
                    atp.ti->num_pieces(),
                    static_cast<long long>(atp.ti->total_size()));
    } else {
        atp = lt::parse_magnet_uri(opt.magnet);
        std::printf("source     : magnet\n");
    }
    atp.save_path = opt.save_path;
    ses.async_add_torrent(std::move(atp));

    std::map<std::string, std::string> mapping_ok;
    std::map<std::string, std::string> mapping_err;
    std::map<std::string, int> mapping_log_count;
    std::vector<std::string> listen_events;
    std::vector<std::string> listen_failures;
    std::vector<std::string> external_ips;
    bool metadata_received = false;
    lt::torrent_handle handle;

    auto started = std::chrono::steady_clock::now();
    while (std::chrono::duration_cast<std::chrono::seconds>(
               std::chrono::steady_clock::now() - started).count() < opt.seconds) {
        std::this_thread::sleep_for(std::chrono::milliseconds(500));

        std::vector<lt::alert*> alerts;
        ses.pop_alerts(&alerts);
        for (lt::alert const* a : alerts) {
            if (auto* ls = lt::alert_cast<lt::listen_succeeded_alert>(a)) {
                char buf[160];
                std::snprintf(buf, sizeof buf, "%s %s:%d",
                              sock_type_str(ls->socket_type),
                              ls->address.to_string().c_str(), ls->port);
                listen_events.emplace_back(buf);
                std::printf("[listen ] %s\n", buf);
                continue;
            }
            if (auto* lf = lt::alert_cast<lt::listen_failed_alert>(a)) {
                char buf[220];
                std::snprintf(buf, sizeof buf, "%s %s:%d -- %s",
                              sock_type_str(lf->socket_type),
                              lf->address.to_string().c_str(), lf->port,
                              lf->error.message().c_str());
                listen_failures.emplace_back(buf);
                std::printf("[listen!] FAILED %s\n", buf);
                continue;
            }
            if (auto* pm = lt::alert_cast<lt::portmap_alert>(a)) {
                const std::string k = key_of(pm->map_transport, pm->map_protocol);
                char buf[200];
                std::snprintf(buf, sizeof buf, "external port %d (gateway seen at %s)",
                              pm->external_port,
                              pm->local_address.to_string().c_str());
                mapping_ok[k] = buf;
                std::printf("[map ok ] %-16s %s\n", k.c_str(), buf);
                continue;
            }
            if (auto* pe = lt::alert_cast<lt::portmap_error_alert>(a)) {
                const std::string k = key_of(pe->map_transport, lt::portmap_protocol::none);
                mapping_err[k] = pe->error.message();
                std::printf("[map err] %-16s %s\n", k.c_str(),
                            pe->error.message().c_str());
                continue;
            }
            if (auto* pl = lt::alert_cast<lt::portmap_log_alert>(a)) {
                const std::string k = transport_name(pl->map_transport);
                ++mapping_log_count[k];
                std::printf("[map log] %-16s %s\n", k.c_str(), pl->log_message());
                continue;
            }
            if (auto* ip = lt::alert_cast<lt::external_ip_alert>(a)) {
                external_ips.emplace_back(ip->external_address.to_string());
                std::printf("[ext ip ] %s\n", ip->external_address.to_string().c_str());
                continue;
            }
            if (lt::alert_cast<lt::metadata_received_alert>(a)) {
                metadata_received = true;
                std::printf("[metadata] received\n");
                continue;
            }
            if (auto* at = lt::alert_cast<lt::add_torrent_alert>(a)) {
                if (at->error) {
                    std::printf("[torrent!] add failed: %s\n",
                                at->error.message().c_str());
                } else {
                    handle = at->handle;
                    const auto ih = handle.info_hashes().v1;
                    std::printf("[torrent] added, info-hash %s\n",
                                hex_of(ih.data(), 20).c_str());
                }
                continue;
            }
        }
    }

    std::printf("\n=== diagnosis ===\n");
    if (listen_events.empty()) {
        std::printf("listen        : NO SUCCESSFUL SOCKET\n");
    } else {
        std::printf("listen        : %zu socket(s) bound\n", listen_events.size());
    }
    for (const auto& f : listen_failures) {
        std::printf("                failed: %s\n", f.c_str());
    }

    // 报告中固定列出四种组合，没有回应的也要显示出来，避免"静默失败看着像没开"
    const char* transports[] = {"NAT-PMP/PCP", "UPnP-IGD"};
    const char* protocols[] = {"TCP", "UDP"};
    bool any_mapping = false;
    for (const char* t : transports) {
        for (const char* p : protocols) {
            const std::string k = std::string(t) + " " + p;
            auto ok = mapping_ok.find(k);
            auto er = mapping_err.find(std::string(t) + " -");
            if (ok != mapping_ok.end()) {
                std::printf("%-16s: MAPPED -- %s\n", k.c_str(), ok->second.c_str());
                any_mapping = true;
            } else if (er != mapping_err.end()) {
                std::printf("%-16s: failed -- %s\n", k.c_str(), er->second.c_str());
            } else {
                std::printf("%-16s: no inbound mapping reply in %ds\n",
                            k.c_str(), opt.seconds);
            }
        }
    }
    for (const auto& lc : mapping_log_count) {
        std::printf("log lines     : %s = %d\n", lc.first.c_str(), lc.second);
    }

    if (external_ips.empty()) {
        std::printf("external ip   : not reported (no successful inbound mapping)\n");
    } else {
        std::printf("external ip   : %s\n", external_ips.back().c_str());
    }

    if (!opt.torrent_file.empty()) {
        // 用 .torrent 启动时元数据本来就有，metadata_received_alert 不会触发，
        // 这里不能报 "NOT received"，否则会被读成磁力链路失败
        std::printf("metadata      : known (.torrent input)\n");
    } else {
        std::printf("metadata      : %s\n",
                    metadata_received ? "received via magnet" : "NOT received");
    }
    if (handle.is_valid()) {
        lt::torrent_status st = handle.status();
        std::printf("progress      : %.2f%%\n", st.progress * 100.0f);
        std::printf("peers         : %d connected, %d seeds\n",
                    st.num_peers, st.num_seeds);
        std::printf("rates         : down %.2f MB/s, up %.2f MB/s\n",
                    st.download_rate / 1048576.0f, st.upload_rate / 1048576.0f);
    }

    std::printf("\nVERDICT: %s\n",
                any_mapping
                    ? "automatic inbound mapping WORKS on this network"
                    : "automatic inbound mapping UNAVAILABLE on this network");
    return any_mapping ? 0 : 1;
}
