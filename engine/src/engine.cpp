#include "engine.hpp"

#include "json.hpp"
#include "platform.hpp"

#include <libtorrent/add_torrent_params.hpp>
#include <libtorrent/alert_types.hpp>
#include <libtorrent/error_code.hpp>
#include <libtorrent/magnet_uri.hpp>
#include <libtorrent/read_resume_data.hpp>
#include <libtorrent/session.hpp>
#include <libtorrent/settings_pack.hpp>
#include <libtorrent/sha1_hash.hpp>
#include <libtorrent/torrent_handle.hpp>
#include <libtorrent/torrent_info.hpp>
#include <libtorrent/torrent_status.hpp>
#include <libtorrent/write_resume_data.hpp>

#include <natdiag/lt_adapt.hpp>
#include <natdiag/natdiag.hpp>
#include <natdiag/serialize.hpp>

#include <algorithm>
#include <atomic>
#include <cctype>
#include <chrono>
#include <condition_variable>
#include <cstdio>
#include <deque>
#include <filesystem>
#include <fstream>
#include <functional>
#include <future>
#include <map>
#include <memory>
#include <mutex>
#include <thread>
#include <unordered_map>
#include <vector>

namespace eng {
namespace fs = std::filesystem;
namespace lt = libtorrent;
namespace {

char const kVersion[] = "0.1.0-m1b";

std::string hex_of(lt::sha1_hash const& h) {
    static char const* digits = "0123456789abcdef";
    char const* p = h.data();
    std::string s;
    s.reserve(40);
    for (int i = 0; i < 20; ++i) {
        unsigned char c = static_cast<unsigned char>(p[i]);
        s.push_back(digits[c >> 4]);
        s.push_back(digits[c & 0x0F]);
    }
    return s;
}

std::string hex_of(lt::info_hash_t const& ih) {
    return hex_of(ih.get_best());
}

bool is_hex40(std::string const& s) {
    if (s.size() != 40) return false;
    for (char c : s) {
        bool ok = (c >= '0' && c <= '9') || (c >= 'a' && c <= 'f') || (c >= 'A' && c <= 'F');
        if (!ok) return false;
    }
    return true;
}

char const* state_name(lt::torrent_status const& st, bool paused) {
    if (paused) return "paused";
    switch (st.state) {
        case lt::torrent_status::checking_files:        return "checking";
        case lt::torrent_status::downloading_metadata:  return "metadata";
        case lt::torrent_status::downloading:           return "downloading";
        case lt::torrent_status::finished:              return "finished";
        case lt::torrent_status::seeding:               return "seeding";
        default:                                        return "other";
    }
}

bool read_whole_file(fs::path const& p, std::string& out) {
    std::ifstream f(p, std::ios::binary);
    if (!f) return false;
    out.assign(std::istreambuf_iterator<char>(f), std::istreambuf_iterator<char>());
    return true;
}

}  // namespace

struct Engine::Impl {
    Options opts;

    // ---- 仅 worker 线程访问
    std::unique_ptr<lt::session> session;
    natdiag::Collector collector{};

    struct Entry {
        lt::torrent_handle handle;
        bool has_handle = false;
        bool paused = false;
        std::int64_t added_ms = 0;
        std::string save_path;
        std::string magnet;       // 无元数据时的持久化退路
        std::string last_error;
    };
    std::unordered_map<std::string, Entry> entries;

    // ---- 线程调度
    std::thread worker;
    std::mutex task_mutex;
    std::condition_variable task_cv;
    std::deque<std::function<void()>> tasks;
    std::atomic<bool> running{false};

    // ---- 对外快照（受锁保护，HTTP 线程只读）
    mutable std::mutex state_mutex;
    std::uint64_t seq = 0;
    std::string state_json;
    std::string last_content;

    std::string http_listen = "-";
    std::int64_t started_ms = 0;

    // ------------------------------------------------------------------ 路径
    fs::path dir_resume() const { return fs::path(opts.data_dir) / "resume"; }
    fs::path dir_tmp() const { return fs::path(opts.data_dir) / "tmp"; }
    fs::path default_save_path() const { return fs::path(opts.data_dir) / "downloads"; }
    fs::path resume_file(std::string const& ih) const { return dir_resume() / (ih + ".fastresume"); }
    fs::path magnet_file(std::string const& ih) const { return dir_resume() / (ih + ".magnet"); }

    // ------------------------------------------------------------- 同步调用
    bool run_sync(std::function<bool(std::string&)> fn, std::string& err, int timeout_ms = 3000) {
        struct Box {
            std::promise<std::pair<bool, std::string>> promise;
        };
        auto box = std::make_shared<Box>();
        auto fut = box->promise.get_future();
        {
            std::lock_guard<std::mutex> lock(task_mutex);
            if (!running.load()) {
                err = "引擎未运行";
                return false;
            }
            tasks.push_back([box, fn = std::move(fn)]() {
                std::string e;
                bool ok = false;
                try {
                    ok = fn(e);
                } catch (...) {
                    ok = false;
                    e = "引擎内部异常";
                }
                box->promise.set_value({ok, e});
            });
        }
        task_cv.notify_one();
        if (fut.wait_for(std::chrono::milliseconds(timeout_ms)) != std::future_status::ready) {
            err = "引擎响应超时";
            return false;
        }
        auto pair = fut.get();
        err = pair.second;
        return pair.first;
    }

    // ---------------------------------------------------------------- 持久化
    void persist_request(std::string const& ih) {
        auto it = entries.find(ih);
        if (it == entries.end() || !it->second.has_handle) return;
        it->second.handle.save_resume_data(lt::torrent_handle::save_info_dict);
    }

    void persist_all() {
        for (auto& kv : entries) {
            if (kv.second.has_handle) {
                kv.second.handle.save_resume_data(lt::torrent_handle::save_info_dict);
            }
        }
    }

    void on_save_ok(lt::save_resume_data_alert const* a) {
        std::string ih = hex_of(a->params.info_hashes);
        if (!is_hex40(ih)) return;
        auto buf = lt::write_resume_data_buf(a->params);
        std::string err;
        if (!platform::write_file_atomic(resume_file(ih).string(),
                                        std::string_view(buf.data(), buf.size()), err)) {
            std::fprintf(stderr, "[engine] 写 resume 失败 %s: %s\n", ih.c_str(), err.c_str());
            return;
        }
        std::error_code ec;
        fs::remove(magnet_file(ih), ec);  // resume 已包含元数据，退路文件不再需要
    }

    void on_save_failed(std::string const& ih) {
        // 没有元数据时 libtorrent 存不出 resume，用磁力串兜底，避免任务在重启后消失
        auto it = entries.find(ih);
        if (it == entries.end() || it->second.magnet.empty()) return;
        std::string err;
        platform::write_file_atomic(magnet_file(ih).string(), it->second.magnet, err);
    }

    void restore() {
        std::error_code ec;
        if (!fs::exists(dir_resume(), ec)) return;

        std::vector<fs::path> files;
        for (auto const& de : fs::directory_iterator(dir_resume(), ec)) {
            if (!de.is_regular_file()) continue;
            files.push_back(de.path());
        }
        std::sort(files.begin(), files.end());

        for (auto const& p : files) {
            std::string const ext = p.extension().string();
            std::string ih = p.stem().string();
            if (!is_hex40(ih)) continue;

            lt::add_torrent_params atp;
            bool usable = false;

            if (ext == ".fastresume") {
                std::string raw;
                if (!read_whole_file(p, raw)) continue;
                lt::error_code lec;
                atp = lt::read_resume_data(lt::span<char const>(raw.data(), raw.size()), lec);
                usable = !lec;
            } else if (ext == ".magnet") {
                std::string uri;
                if (!read_whole_file(p, uri)) continue;
                lt::error_code lec;
                lt::parse_magnet_uri(uri, atp, lec);
                usable = !lec;
            } else {
                continue;
            }
            if (!usable) continue;

            Entry e;
            e.added_ms = platform::epoch_ms();
            e.save_path = atp.save_path.empty() ? default_save_path().string() : atp.save_path;
            atp.save_path = e.save_path;

            // 注意：add_torrent_params::flags 的默认值（torrent_flags::default_flags）
            // 是带 paused 的，只清 auto_managed 会让新任务一上来就停着。
            // 磁力兜底文件里没有可信的暂停状态，一律按未暂停处理。
            bool was_paused = (ext == ".fastresume") && bool(atp.flags & lt::torrent_flags::paused);
            atp.flags &= ~lt::torrent_flags::auto_managed;
            atp.flags &= ~lt::torrent_flags::paused;
            if (was_paused) atp.flags |= lt::torrent_flags::paused;
            e.paused = was_paused;

            session->async_add_torrent(atp);
            entries[ih] = std::move(e);
        }
    }

    // ------------------------------------------------------------------ 告警
    void handle_alerts(std::vector<lt::alert*> const& alerts) {
        for (lt::alert* a : alerts) {
            // NAT 相关的先交给 natdiag，它自己判断是否关心
            natdiag::NatEvent ev;
            if (natdiag::from_alert(*a, ev)) {
                collector.feed(ev);
                continue;
            }

            if (auto* add = lt::alert_cast<lt::add_torrent_alert>(a)) {
                std::string ih = hex_of(add->params.info_hashes);
                auto it = entries.find(ih);
                if (it == entries.end()) continue;
                if (add->error) {
                    it->second.last_error = add->error.message();
                    continue;
                }
                it->second.handle = add->handle;
                it->second.has_handle = true;
                if (it->second.paused) {
                    add->handle.unset_flags(lt::torrent_flags::auto_managed);
                    add->handle.set_flags(lt::torrent_flags::paused);
                }
            } else if (auto* ok = lt::alert_cast<lt::save_resume_data_alert>(a)) {
                on_save_ok(ok);
            } else if (auto* bad = lt::alert_cast<lt::save_resume_data_failed_alert>(a)) {
                on_save_failed(hex_of(bad->handle.info_hashes()));
            } else if (auto* meta = lt::alert_cast<lt::metadata_received_alert>(a)) {
                // 有了元数据立刻存 resume，这样重启不依赖 DHT 重新取元数据
                meta->handle.save_resume_data(lt::torrent_handle::save_info_dict);
            } else if (auto* err = lt::alert_cast<lt::torrent_error_alert>(a)) {
                entries[hex_of(err->handle.info_hashes())].last_error = err->error.message();
            } else if (auto* gone = lt::alert_cast<lt::torrent_removed_alert>(a)) {
                entries.erase(hex_of(gone->info_hashes));
            }
        }
    }

    // ------------------------------------------------------------------ 快照
    void rebuild_state() {
        std::vector<std::pair<std::int64_t, std::string>> order;
        order.reserve(entries.size());
        for (auto const& kv : entries) order.emplace_back(kv.second.added_ms, kv.first);
        // 按加入时间排序，保证输出顺序稳定（unordered_map 顺序不可依赖）
        std::stable_sort(order.begin(), order.end(), [](auto const& x, auto const& y) {
            return x.first < y.first;
        });

        std::string list = "[";
        bool first = true;
        for (auto const& item : order) {
            auto const& ih = item.second;
            auto& e = entries[ih];

            std::string state = "pending";
            std::string name = ih;
            std::string save_path = e.save_path;
            std::string error = e.last_error;
            double progress = 0.0;
            std::int64_t total_bytes = 0, done_bytes = 0, eta_s = -1;
            int rate_down = 0, rate_up = 0, peers = 0, seeds = 0;
            bool has_metadata = false;

            if (e.has_handle) {
                lt::torrent_status st = e.handle.status(
                    lt::torrent_handle::query_name | lt::torrent_handle::query_save_path);
                bool paused = bool(st.flags & lt::torrent_flags::paused);
                state = state_name(st, paused);
                if (!st.name.empty()) name = st.name;
                if (!st.save_path.empty()) save_path = st.save_path;
                if (st.errc) error = st.errc.message();
                progress = static_cast<double>(st.progress);
                total_bytes = st.total > 0 ? st.total : 0;
                done_bytes = st.total_done > 0 ? st.total_done : 0;
                rate_down = st.download_rate;
                rate_up = st.upload_rate;
                peers = st.num_peers;
                seeds = st.num_seeds;
                has_metadata = st.has_metadata;
                if (rate_down > 0 && total_bytes > done_bytes) {
                    eta_s = (total_bytes - done_bytes) / rate_down;
                }
            }

            if (!first) list += ",";
            first = false;
            list += "{\"infohash\":" + json::quote(ih);
            list += ",\"name\":" + json::quote(name);
            list += ",\"state\":" + json::quote(state);
            list += ",\"paused\":";
            list += (e.has_handle ? (state == "paused" ? "true" : "false") : "false");
            list += ",\"has_metadata\":";
            list += has_metadata ? "true" : "false";
            list += ",\"progress\":" + json::number(progress);
            list += ",\"total_bytes\":" + std::to_string(total_bytes);
            list += ",\"done_bytes\":" + std::to_string(done_bytes);
            list += ",\"rate_down\":" + std::to_string(rate_down);
            list += ",\"rate_up\":" + std::to_string(rate_up);
            list += ",\"peers\":" + std::to_string(peers);
            list += ",\"seeds\":" + std::to_string(seeds);
            list += ",\"eta_s\":" + std::to_string(eta_s);
            list += ",\"save_path\":" + json::quote(save_path);
            list += ",\"error\":";
            list += error.empty() ? "null" : json::quote(error);
            list += ",\"added_ms\":" + std::to_string(e.added_ms);
            list += "}";
        }
        list += "]";

        namespace nd = natdiag;
        std::string nat = nd::to_json(collector.snapshot());

        // seq 只在内容真的变了才递增，前端据此跳过无意义的重绘
        std::string content = list + "|" + nat;
        bool changed = content != last_content;
        if (changed) {
            last_content = std::move(content);
            ++seq;
        }

        std::int64_t now = platform::epoch_ms();
        std::string out;
        out.reserve(list.size() + 512);
        out += "{\"seq\":" + std::to_string(seq);
        out += ",\"ts_ms\":" + std::to_string(now);
        out += ",\"uptime_ms\":" + std::to_string(now - started_ms);
        out += ",\"version\":" + json::quote(kVersion);
        out += ",\"http_listen\":" + json::quote(http_listen);
        out += ",\"data_dir\":" + json::quote(opts.data_dir);
        out += ",\"torrent_count\":" + std::to_string(entries.size());
        out += ",\"torrents\":" + list;
        out += ",\"nat\":" + nat;
        out += "}";

        {
            std::lock_guard<std::mutex> lock(state_mutex);
            state_json = std::move(out);
        }
    }

    // ------------------------------------------------------------------- 循环
    void tick() {
        std::vector<lt::alert*> alerts;
        session->pop_alerts(&alerts);
        if (!alerts.empty()) handle_alerts(alerts);

        collector.set_elapsed(static_cast<int>((platform::epoch_ms() - started_ms) / 1000));
        rebuild_state();
    }

    void loop() {
        while (true) {
            std::function<void()> task;
            bool has_task = false;
            {
                std::unique_lock<std::mutex> lock(task_mutex);
                task_cv.wait_for(lock, std::chrono::milliseconds(100), [this] {
                    return !tasks.empty() || !running.load();
                });
                if (!tasks.empty()) {
                    task = std::move(tasks.front());
                    tasks.pop_front();
                    has_task = true;
                } else if (!running.load()) {
                    break;
                }
            }
            if (has_task) {
                task();
            } else {
                tick();
            }
        }
    }

    // 退出前落盘。给了上限：卡住不如丢掉最后 100ms 的状态。
    void flush_all(int timeout_ms) {
        if (!session) return;
        persist_all();

        std::map<std::string, bool> pending;
        for (auto const& kv : entries) {
            if (kv.second.has_handle) pending[kv.first] = true;
        }
        if (pending.empty()) return;

        std::int64_t deadline = platform::epoch_ms() + timeout_ms;
        while (!pending.empty() && platform::epoch_ms() < deadline) {
            std::vector<lt::alert*> alerts;
            session->wait_for_alert(lt::milliseconds(100));
            session->pop_alerts(&alerts);
            for (lt::alert* a : alerts) {
                if (auto* ok = lt::alert_cast<lt::save_resume_data_alert>(a)) {
                    on_save_ok(ok);  // 先落盘再清待办，否则退出时状态会悄悄丢掉
                    pending.erase(hex_of(ok->params.info_hashes));
                } else if (auto* bad = lt::alert_cast<lt::save_resume_data_failed_alert>(a)) {
                    std::string ih = hex_of(bad->handle.info_hashes());
                    pending.erase(ih);
                    on_save_failed(ih);
                }
                natdiag::NatEvent ev;
                if (natdiag::from_alert(*a, ev)) collector.feed(ev);
            }
        }
        if (!pending.empty()) {
            std::fprintf(stderr, "[engine] 退出时仍有 %zu 个任务未落盘\n", pending.size());
        }
    }

    bool start(std::string& err) {
        std::error_code ec;
        fs::create_directories(dir_resume(), ec);
        if (ec) {
            err = "无法创建 resume 目录：" + ec.message();
            return false;
        }
        fs::create_directories(dir_tmp(), ec);
        fs::create_directories(default_save_path(), ec);

        lt::settings_pack pack;
        pack.set_str(lt::settings_pack::listen_interfaces, opts.listen_interfaces);
        pack.set_bool(lt::settings_pack::enable_dht, !opts.no_network);
        pack.set_bool(lt::settings_pack::enable_lsd, !opts.no_network);
        pack.set_bool(lt::settings_pack::enable_upnp, !opts.no_network);
        pack.set_bool(lt::settings_pack::enable_natpmp, !opts.no_network);
        pack.set_int(lt::settings_pack::alert_mask, lt::alert_category::all);
        pack.set_str(lt::settings_pack::user_agent, std::string("bt-engine/") + kVersion);

        try {
            session = std::make_unique<lt::session>(pack);
        } catch (std::exception const& e) {
            err = std::string("创建会话失败：") + e.what();
            return false;
        }

        started_ms = platform::epoch_ms();
        restore();          // 此时 worker 尚未启动，entries 无并发访问

        // 先算一帧再放行：否则启动后头 100ms 里 /state 会返回空体，
        // 客户端拿到 "" 会当成解析失败而不是「还没准备好」。
        rebuild_state();

        running.store(true);
        worker = std::thread([this] { loop(); });
        return true;
    }

    void stop() {
        if (!session) return;
        if (running.load()) {
            // 先落盘再停循环
            std::promise<void> done;
            auto fut = done.get_future();
            {
                std::lock_guard<std::mutex> lock(task_mutex);
                tasks.push_back([this, &done] {
                    flush_all(4000);
                    done.set_value();
                });
            }
            task_cv.notify_one();
            if (fut.wait_for(std::chrono::seconds(6)) != std::future_status::ready) {
                std::fprintf(stderr, "[engine] 落盘超时，强制退出\n");
            }
        }
        running.store(false);
        task_cv.notify_all();
        if (worker.joinable()) worker.join();
        session.reset();
    }
};

Engine::Engine(Options opts) : impl_(new Impl()) {
    impl_->opts = std::move(opts);
}

Engine::~Engine() {
    stop();
}

bool Engine::start(std::string& err) {
    return impl_->start(err);
}

void Engine::stop() {
    if (impl_) impl_->stop();
}

void Engine::set_http_listen(std::string const& addr) {
    impl_->http_listen = addr;
    // 立刻刷新一次，避免第一帧读到 "-"
    std::lock_guard<std::mutex> lock(impl_->state_mutex);
    if (!impl_->state_json.empty()) {
        std::size_t pos = impl_->state_json.find("\"http_listen\":\"-\"");
        if (pos != std::string::npos) {
            impl_->state_json.replace(pos, std::string("\"http_listen\":\"-\"").size(),
                                      "\"http_listen\":" + json::quote(addr));
        }
    }
}

std::string Engine::state_json() const {
    std::lock_guard<std::mutex> lock(impl_->state_mutex);
    return impl_->state_json;
}

std::uint64_t Engine::state_seq() const {
    std::lock_guard<std::mutex> lock(impl_->state_mutex);
    return impl_->seq;
}

bool Engine::add_magnet(std::string const& uri, std::string& infohash, std::string& err) {
    return impl_->run_sync(
        [this, uri, &infohash](std::string& e) {
            lt::error_code ec;
            lt::add_torrent_params atp;
            lt::parse_magnet_uri(uri, atp, ec);
            if (ec) {
                // bad_input 前缀让 main.cpp 的 map_error 映射成 400。
                // 曾经这里没有前缀，用户输错磁力会收到 500「引擎处理失败」，
                // 把客户端输入问题伪装成服务端故障。
                e = "bad_input: 磁力链接解析失败：" + ec.message();
                return false;
            }
            std::string ih = hex_of(atp.info_hashes);
            if (!is_hex40(ih)) {
                e = "bad_input: 磁力链接缺少有效的 btih";
                return false;
            }
            if (impl_->entries.count(ih)) {
                e = "conflict: 该任务已存在";
                infohash = ih;
                return false;
            }
            atp.save_path = impl_->default_save_path().string();
            atp.flags &= ~lt::torrent_flags::auto_managed;
            atp.flags &= ~lt::torrent_flags::paused;
            impl_->session->async_add_torrent(atp);

            Impl::Entry entry;
            entry.added_ms = platform::epoch_ms();
            entry.save_path = atp.save_path;
            entry.magnet = uri;
            impl_->entries[ih] = std::move(entry);
            infohash = ih;
            return true;
        },
        err);
}

bool Engine::add_torrent_bytes(std::string const& bytes, std::string& infohash,
                               std::string& err) {
    if (bytes.empty()) {
        err = "种子文件为空";
        return false;
    }
    return impl_->run_sync(
        [this, bytes, &infohash](std::string& e) {
            // 先落成临时文件再解析：比猜 torrent_info 的 buffer 构造签名可靠
            std::string tmp_name = "add-" + std::to_string(platform::epoch_ms()) + "-" +
                                   std::to_string(std::hash<std::string>{}(bytes) & 0xFFFF) +
                                   ".torrent";
            fs::path tmp = impl_->dir_tmp() / tmp_name;
            std::string werr;
            if (!platform::write_file_atomic(tmp.string(), bytes, werr)) {
                e = "写入临时种子失败：" + werr;
                return false;
            }

            lt::error_code ec;
            auto ti = std::make_shared<lt::torrent_info>(tmp.string(), ec);
            std::error_code rm;
            fs::remove(tmp, rm);
            if (ec) {
                e = "种子文件解析失败：" + ec.message();
                return false;
            }

            std::string ih = hex_of(ti->info_hashes());
            if (!is_hex40(ih)) {
                e = "无法从种子文件计算 infohash";
                return false;
            }
            if (impl_->entries.count(ih)) {
                e = "conflict: 该任务已存在";
                infohash = ih;
                return false;
            }

            lt::add_torrent_params atp;
            atp.ti = ti;
            atp.save_path = impl_->default_save_path().string();
            atp.flags &= ~lt::torrent_flags::auto_managed;
            atp.flags &= ~lt::torrent_flags::paused;
            impl_->session->async_add_torrent(atp);

            Impl::Entry entry;
            entry.added_ms = platform::epoch_ms();
            entry.save_path = atp.save_path;
            impl_->entries[ih] = std::move(entry);
            infohash = ih;
            return true;
        },
        err);
}

bool Engine::pause(std::string const& raw_ih, std::string& err) {
    std::string ih = raw_ih;
    std::transform(ih.begin(), ih.end(), ih.begin(),
                   [](unsigned char c) { return static_cast<char>(std::tolower(c)); });
    return impl_->run_sync(
        [this, ih](std::string& e) {
            auto it = impl_->entries.find(ih);
            if (it == impl_->entries.end()) {
                e = "not_found: 任务不存在";
                return false;
            }
            if (!it->second.has_handle) {
                it->second.paused = true;
                return true;
            }
            it->second.handle.unset_flags(lt::torrent_flags::auto_managed);
            it->second.handle.set_flags(lt::torrent_flags::paused);
            impl_->persist_request(ih);
            return true;
        },
        err);
}

bool Engine::resume(std::string const& raw_ih, std::string& err) {
    std::string ih = raw_ih;
    std::transform(ih.begin(), ih.end(), ih.begin(),
                   [](unsigned char c) { return static_cast<char>(std::tolower(c)); });
    return impl_->run_sync(
        [this, ih](std::string& e) {
            auto it = impl_->entries.find(ih);
            if (it == impl_->entries.end()) {
                e = "not_found: 任务不存在";
                return false;
            }
            if (!it->second.has_handle) {
                it->second.paused = false;
                return true;
            }
            it->second.handle.unset_flags(lt::torrent_flags::paused);
            impl_->persist_request(ih);
            return true;
        },
        err);
}

bool Engine::remove_torrent(std::string const& raw_ih, bool delete_files, std::string& err) {
    std::string ih = raw_ih;
    std::transform(ih.begin(), ih.end(), ih.begin(),
                   [](unsigned char c) { return static_cast<char>(std::tolower(c)); });
    return impl_->run_sync(
        [this, ih, delete_files](std::string& e) {
            auto it = impl_->entries.find(ih);
            if (it == impl_->entries.end()) {
                e = "not_found: 任务不存在";
                return false;
            }
            if (it->second.has_handle) {
                if (delete_files) {
                    impl_->session->remove_torrent(it->second.handle, lt::session::delete_files);
                } else {
                    impl_->session->remove_torrent(it->second.handle);
                }
            }
            std::error_code ec;
            fs::remove(impl_->resume_file(ih), ec);
            fs::remove(impl_->magnet_file(ih), ec);
            impl_->entries.erase(ih);
            return true;
        },
        err);
}

}  // namespace eng
