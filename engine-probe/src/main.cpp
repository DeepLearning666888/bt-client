// engine-probe —— natdiag 的实机验证壳。
//
// 入站诊断逻辑全部在 natdiag 里；这里只负责：起 session、喂 alert、打印结果。
// 旧实现保留在 main.legacy.cpp，用于双二进制回归比对。

#include <natdiag/lt_adapt.hpp>
#include <natdiag/natdiag.hpp>
#include <natdiag/serialize.hpp>

#include <libtorrent/alert_types.hpp>
#include <libtorrent/magnet_uri.hpp>
#include <libtorrent/session.hpp>
#include <libtorrent/torrent_info.hpp>
#include <libtorrent/version.hpp>

#include <chrono>
#include <cstdio>
#include <cstdlib>
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
    bool json = false;
};

std::string hex_of(char const* p, std::size_t n) {
    static char const* d = "0123456789abcdef";
    std::string s;
    s.reserve(n * 2);
    for (std::size_t i = 0; i < n; ++i) {
        unsigned char const c = static_cast<unsigned char>(p[i]);
        s.push_back(d[c >> 4]);
        s.push_back(d[c & 0x0f]);
    }
    return s;
}

Options parse_args(int argc, char** argv) {
    Options o;
    for (int i = 1; i < argc; ++i) {
        std::string a = argv[i];
        auto next = [&](char const* name) -> std::string {
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
        } else if (a == "--json") {
            o.json = true;
        } else if (a == "--help" || a == "-h") {
            std::printf(
                "engine-probe --magnet <uri> | --torrent <file>\n"
                "             [--seconds N] [--port N] [--save-path DIR] [--json]\n");
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

}  // namespace

int main(int argc, char** argv) {
    Options const opt = parse_args(argc, argv);

    // --json 模式下 stdout 只留 JSON，过程信息走 stderr，免得污染管道
    std::FILE* const info = opt.json ? stderr : stdout;

    lt::settings_pack sp = lt::default_settings();
    // 每种自动映射协议都显式打开，失败的也要留下痕迹
    sp.set_bool(lt::settings_pack::enable_upnp, true);
    sp.set_bool(lt::settings_pack::enable_natpmp, true);
    sp.set_bool(lt::settings_pack::enable_lsd, true);
    sp.set_bool(lt::settings_pack::enable_dht, true);
    sp.set_int(lt::settings_pack::alert_mask, lt::alert_category::all);
    sp.set_str(lt::settings_pack::listen_interfaces,
               "0.0.0.0:" + std::to_string(opt.listen_port));

    lt::session ses(sp);

    std::fprintf(info, "=== engine-probe ===\n");
    std::fprintf(info, "libtorrent : %s\n", lt::version());
    std::fprintf(info, "listen port: %d (TCP + uTP)\n", opt.listen_port);
    std::fprintf(info, "protocols  : UPnP=on NAT-PMP/PCP=on LSD=on DHT=on\n");
    std::fprintf(info, "duration   : %ds\n\n", opt.seconds);

    lt::add_torrent_params atp;
    if (!opt.torrent_file.empty()) {
        atp.ti = std::make_shared<lt::torrent_info>(opt.torrent_file);
        std::fprintf(info, "source     : torrent file, %d piece(s), %lld bytes\n",
                     atp.ti->num_pieces(), static_cast<long long>(atp.ti->total_size()));
    } else {
        atp = lt::parse_magnet_uri(opt.magnet);
        std::fprintf(info, "source     : magnet\n");
    }
    atp.save_path = opt.save_path;
    ses.async_add_torrent(std::move(atp));

    natdiag::Collector collector;
    lt::torrent_handle handle;
    bool metadata_received = false;

    auto const started = std::chrono::steady_clock::now();
    for (;;) {
        int const elapsed = static_cast<int>(
            std::chrono::duration_cast<std::chrono::seconds>(
                std::chrono::steady_clock::now() - started)
                .count());
        if (elapsed >= opt.seconds) break;
        collector.set_elapsed(elapsed);

        std::this_thread::sleep_for(std::chrono::milliseconds(500));

        std::vector<lt::alert*> alerts;
        ses.pop_alerts(&alerts);
        for (lt::alert const* a : alerts) {
            // torrent 层的事自己处理：natdiag 只管入站诊断
            if (auto* at = lt::alert_cast<lt::add_torrent_alert>(a)) {
                if (at->error) {
                    std::fprintf(info, "[torrent!] add failed: %s\n", at->error.message().c_str());
                } else {
                    handle = at->handle;
                    auto const ih = handle.info_hashes().v1;
                    std::fprintf(info, "[torrent] added, info-hash %s\n",
                                 hex_of(ih.data(), 20).c_str());
                }
                continue;
            }
            if (lt::alert_cast<lt::metadata_received_alert>(a)) {
                metadata_received = true;
                std::fprintf(info, "[metadata] received\n");
                continue;
            }

            natdiag::NatEvent ev;
            if (!natdiag::from_alert(*a, ev)) continue;

            // map_log 太吵（实测 60 秒 83 行），只计数不打印
            switch (ev.kind) {
                case natdiag::NatEvent::Kind::listen_ok:
                    std::fprintf(info, "[listen ] %s\n", ev.detail.c_str());
                    break;
                case natdiag::NatEvent::Kind::listen_fail:
                    std::fprintf(info, "[listen!] FAILED %s\n", ev.detail.c_str());
                    break;
                case natdiag::NatEvent::Kind::map_ok:
                    std::fprintf(info, "[map ok ] %-10s %s\n",
                                 natdiag::to_string(ev.transport), ev.detail.c_str());
                    break;
                case natdiag::NatEvent::Kind::map_err:
                    std::fprintf(info, "[map err] %-10s error %d -- %s\n",
                                 natdiag::to_string(ev.transport), ev.error_code,
                                 ev.detail.c_str());
                    break;
                case natdiag::NatEvent::Kind::external_ip:
                    std::fprintf(info, "[ext ip ] %s\n", ev.detail.c_str());
                    break;
                case natdiag::NatEvent::Kind::map_log:
                    break;
            }
            collector.feed(ev);
        }
    }
    collector.set_elapsed(opt.seconds);

    natdiag::Snapshot const snap = collector.snapshot();
    int const code = (snap.verdict == natdiag::Verdict::working) ? 0 : 1;

    if (opt.json) {
        std::fputs(natdiag::to_json(snap).c_str(), stdout);
        return code;
    }

    std::printf("\n=== torrent ===\n");
    if (!opt.torrent_file.empty()) {
        // 用 .torrent 启动时元数据本来就有，metadata_received_alert 不会触发，
        // 这里不能报 NOT received，否则会被读成磁力链路失败
        std::printf("metadata      : known (.torrent input)\n");
    } else {
        std::printf("metadata      : %s\n",
                    metadata_received ? "received via magnet" : "NOT received");
    }
    if (handle.is_valid()) {
        lt::torrent_status const st = handle.status();
        std::printf("progress      : %.2f%%\n", st.progress * 100.0f);
        std::printf("peers         : %d connected, %d seeds\n", st.num_peers, st.num_seeds);
        std::printf("rates         : down %.2f MB/s, up %.2f MB/s\n",
                    st.download_rate / 1048576.0f, st.upload_rate / 1048576.0f);
    }

    std::printf("\n%s", natdiag::to_text(snap).c_str());
    return code;
}
