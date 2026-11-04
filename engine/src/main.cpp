// 引擎进程入口。
//
// 启动顺序是有讲究的：
//   1. 先拿单实例锁（拿不到就退出，不做任何有副作用的事）
//   2. 起引擎会话
//   3. 起 HTTP 服务（端口由内核分配）
//   4. 拿到实际端口后才写发现文件 —— 早写会让客户端读到 0
//   5. 退出时先删发现文件，再停服务，最后让引擎落盘

#include "engine.hpp"
#include "http.hpp"
#include "json.hpp"
#include "platform.hpp"

#include <atomic>
#include <chrono>
#include <csignal>
#include <cstdio>
#include <filesystem>
#include <string>
#include <thread>

#include <windows.h>

namespace fs = std::filesystem;

namespace {

char const kVersion[] = "0.1.0-m1b";

std::atomic<bool> g_stop{false};

BOOL WINAPI on_console_ctrl(DWORD type) {
    if (type == CTRL_C_EVENT || type == CTRL_BREAK_EVENT || type == CTRL_CLOSE_EVENT) {
        g_stop.store(true);
        return TRUE;
    }
    return FALSE;
}

void print_usage() {
    std::printf(
        "engine %s\n"
        "用法: engine [选项]\n"
        "  --data-dir <路径>   数据目录（默认 ./engine-data）\n"
        "  --http-port <端口>  HTTP 端口，0 表示由内核分配（默认 0）\n"
        "  --no-network        关闭 DHT/LSD/UPnP/NAT-PMP，测试用\n"
        "  --version           打印版本后退出\n",
        kVersion);
}

std::string lower(std::string s) {
    for (char& c : s) {
        if (c >= 'A' && c <= 'Z') c = static_cast<char>(c - 'A' + 'a');
    }
    return s;
}

// 引擎返回的错误串带前缀，用于把「不存在」「已存在」和其他失败区分开，
// 免得在 main 里靠匹配中文文案判断状态码。
struct Mapped {
    int status;
    std::string message;
};

Mapped map_error(std::string const& raw) {
    if (raw.rfind("not_found: ", 0) == 0) return {404, raw.substr(11)};
    if (raw.rfind("conflict: ", 0) == 0) return {409, raw.substr(10)};
    if (raw.rfind("bad_input: ", 0) == 0) return {400, raw.substr(11)};
    return {500, raw.empty() ? "内部错误" : raw};
}

http::Response from_error(std::string const& raw, std::string const& code) {
    Mapped m = map_error(raw);
    return http::Response::error(m.status, code, m.message);
}

bool parse_infohash_path(std::string const& path, std::string& ih, std::string& action) {
    // /torrents/<40 位 hex>[/pause|/resume]
    static std::string const prefix = "/torrents/";
    if (path.rfind(prefix, 0) != 0) return false;
    std::string rest = path.substr(prefix.size());
    std::size_t slash = rest.find('/');
    if (slash == std::string::npos) {
        ih = rest;
        action.clear();
    } else {
        ih = rest.substr(0, slash);
        action = rest.substr(slash + 1);
    }
    if (ih.size() != 40) return false;
    return true;
}

http::Response route(eng::Engine& engine, std::string const& version, std::string const& data_dir,
                     http::Request const& req) {
    // ---- GET /health
    if (req.path == "/health") {
        if (req.method != "GET") return http::Response::error(405, "method_not_allowed", "请用 GET");
        std::string body = "{\"ok\":true,\"version\":" + json::quote(version) +
                           ",\"pid\":" + std::to_string(::GetCurrentProcessId()) + "}";
        return http::Response::ok(std::move(body));
    }

    // ---- GET /state
    if (req.path == "/state") {
        if (req.method != "GET") return http::Response::error(405, "method_not_allowed", "请用 GET");
        std::uint64_t cur = engine.state_seq();
        int since = req.query_int("since", -1);
        if (since >= 0 && static_cast<std::uint64_t>(since) == cur) {
            return http::Response::ok("{\"seq\":" + std::to_string(cur) + ",\"unchanged\":true}");
        }
        std::string body = engine.state_json();
        if (body.empty()) {
            return http::Response::error(503, "state_not_ready", "状态快照尚未生成");
        }
        return http::Response::ok(std::move(body));
    }

    // ---- POST /torrents
    if (req.path == "/torrents") {
        if (req.method != "POST") return http::Response::error(405, "method_not_allowed", "请用 POST");
        std::string infohash, err;

        std::string const& ctype = req.header("content-type");
        bool is_octet = lower(ctype).rfind("application/octet-stream", 0) == 0;

        bool ok = false;
        if (is_octet) {
            ok = engine.add_torrent_bytes(req.body, infohash, err);
        } else {
            std::string magnet;
            if (!json::get_string(req.body, "magnet", magnet)) {
                return http::Response::error(400, "bad_request", "缺少 magnet 字段或 JSON 非法");
            }
            ok = engine.add_magnet(magnet, infohash, err);
        }

        if (!ok) return from_error(err, "add_failed");
        return http::Response::ok("{\"infohash\":" + json::quote(infohash) + "}");
    }

    // ---- /torrents/<ih>...
    std::string ih, action;
    if (parse_infohash_path(req.path, ih, action)) {
        std::string err;
        if (action.empty()) {
            if (req.method != "DELETE") {
                return http::Response::error(405, "method_not_allowed", "请用 DELETE");
            }
            bool del_files = req.query_int("delete_files", 0) != 0;
            if (!engine.remove_torrent(ih, del_files, err)) return from_error(err, "remove_failed");
            // err 在成功路径上非空，表示「任务移除了，但有件事没做成」——
            // 目前唯一来源是文件删不掉。仍然是 200（任务确实没了），
            // 用 warning 字段让界面能说清「磁盘上的东西还在」。
            std::string body = "{\"removed\":" + json::quote(lower(ih));
            if (!err.empty()) body += ",\"warning\":" + json::quote(err);
            body += "}";
            return http::Response::ok(body);
        }
        if (action == "pause" || action == "resume") {
            if (req.method != "POST") {
                return http::Response::error(405, "method_not_allowed", "请用 POST");
            }
            bool ok = (action == "pause") ? engine.pause(ih, err) : engine.resume(ih, err);
            if (!ok) return from_error(err, action + "_failed");
            return http::Response::ok("{\"ok\":true,\"action\":" + json::quote(action) + "}");
        }
        return http::Response::error(404, "unknown_action", "未知的子路径");
    }

    // ---- POST /shutdown
    // 加这个端点是为了给「优雅退出」一条确定的路：测试要能在不杀进程的前提下
    // 触发落盘，否则 AC-08 只能靠时序赌运气。
    if (req.path == "/shutdown") {
        if (req.method != "POST") return http::Response::error(405, "method_not_allowed", "请用 POST");
        g_stop.store(true);
        return http::Response::ok("{\"ok\":true}");
    }

    (void)data_dir;
    return http::Response::error(404, "not_found", "没有这个端点");
}

}  // namespace

int main(int argc, char** argv) {
    std::string data_dir;
    int http_port = 0;
    bool no_network = false;

    for (int i = 1; i < argc; ++i) {
        std::string arg = argv[i];
        if (arg == "--version") {
            std::printf("engine %s\n", kVersion);
            return 0;
        }
        if (arg == "--help" || arg == "-h") {
            print_usage();
            return 0;
        }
        if (arg == "--data-dir" && i + 1 < argc) {
            data_dir = argv[++i];
        } else if (arg == "--http-port" && i + 1 < argc) {
            http_port = std::atoi(argv[++i]);
        } else if (arg == "--no-network") {
            no_network = true;
        } else {
            std::fprintf(stderr, "未知参数: %s\n", arg.c_str());
            print_usage();
            return 2;
        }
    }

    if (data_dir.empty()) data_dir = (fs::current_path() / "engine-data").string();

    std::error_code ec;
    fs::create_directories(data_dir, ec);
    if (ec) {
        std::fprintf(stderr, "无法创建数据目录 %s: %s\n", data_dir.c_str(), ec.message().c_str());
        return 3;
    }

    platform::InstanceLock lock;
    {
        std::string err;
        if (!lock.acquire((fs::path(data_dir) / "engine.lock").string(), err)) {
            std::fprintf(stderr, "启动失败: %s\n", err.c_str());
            return 2;
        }
    }

    std::string token = platform::random_token_hex(16);
    if (token.empty()) {
        std::fprintf(stderr, "启动失败: 无法获取系统随机数，拒绝使用可预测令牌\n");
        return 3;
    }

    eng::Options opts;
    opts.data_dir = data_dir;
    opts.no_network = no_network;
    eng::Engine engine(opts);
    {
        std::string err;
        if (!engine.start(err)) {
            std::fprintf(stderr, "启动失败: %s\n", err.c_str());
            return 3;
        }
    }

    http::Server server;
    std::string listen_err;
    auto handler = [&engine, data_dir](http::Request const& req) {
        return route(engine, kVersion, data_dir, req);
    };
    if (!server.start("127.0.0.1", http_port, token, handler, listen_err)) {
        std::fprintf(stderr, "启动失败: %s\n", listen_err.c_str());
        engine.stop();
        return 3;
    }

    std::string listen = "127.0.0.1:" + std::to_string(server.port());
    engine.set_http_listen(listen);

    fs::path endpoint_path = fs::path(data_dir) / "engine.endpoint.json";
    std::string endpoint = "{\"host\":\"127.0.0.1\",\"port\":" + std::to_string(server.port()) +
                           ",\"token\":" + json::quote(token) +
                           ",\"pid\":" + std::to_string(::GetCurrentProcessId()) +
                           ",\"version\":" + json::quote(kVersion) +
                           ",\"data_dir\":" + json::quote(platform::native_to_utf8(data_dir)) + "}";
    {
        std::string err;
        if (!platform::write_file_atomic(endpoint_path.string(), endpoint, err)) {
            std::fprintf(stderr, "写发现文件失败: %s\n", err.c_str());
            server.stop();
            engine.stop();
            return 3;
        }
    }

    ::SetConsoleCtrlHandler(on_console_ctrl, TRUE);

    std::printf("READY %s\n", endpoint.c_str());
    std::fflush(stdout);

    while (!g_stop.load()) {
        std::this_thread::sleep_for(std::chrono::milliseconds(50));
    }

    // 先让正在写的响应发完，再关服务；否则调用方可能拿到半截响应
    std::this_thread::sleep_for(std::chrono::milliseconds(100));
    std::remove(endpoint_path.string().c_str());
    server.stop();
    engine.stop();
    std::printf("STOPPED\n");
    std::fflush(stdout);
    return 0;
}
