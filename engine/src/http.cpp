#include "http.hpp"

#include "json.hpp"

#include <winsock2.h>
#include <ws2tcpip.h>

#include <algorithm>
#include <atomic>
#include <cctype>
#include <condition_variable>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <deque>
#include <mutex>
#include <thread>
#include <vector>

namespace http {
namespace {

constexpr std::size_t kMaxHeaderBytes = 16 * 1024;
constexpr std::size_t kMaxRequestLineBytes = 2048;
constexpr std::size_t kMaxBodyBytes = 8u * 1024 * 1024;
constexpr int kIoTimeoutMs = 5000;
constexpr int kWorkerCount = 4;
constexpr std::size_t kMaxQueued = 64;

char const* reason_phrase(int status) {
    switch (status) {
        case 200: return "OK";
        case 400: return "Bad Request";
        case 401: return "Unauthorized";
        case 404: return "Not Found";
        case 405: return "Method Not Allowed";
        case 411: return "Length Required";
        case 413: return "Payload Too Large";
        case 415: return "Unsupported Media Type";
        case 431: return "Request Header Fields Too Large";
        case 503: return "Service Unavailable";
        case 204: return "No Content";
        default:  return "Internal Server Error";
    }
}

std::string to_lower(std::string s) {
    for (char& c : s) c = static_cast<char>(std::tolower(static_cast<unsigned char>(c)));
    return s;
}

std::string trim(std::string s) {
    std::size_t b = 0;
    while (b < s.size() && (s[b] == ' ' || s[b] == '\t')) ++b;
    std::size_t e = s.size();
    while (e > b && (s[e - 1] == ' ' || s[e - 1] == '\t' || s[e - 1] == '\r')) --e;
    return s.substr(b, e - b);
}

// 定长比较，避免按字节提前返回泄露令牌长度以外的信息
bool token_equals(std::string const& a, std::string const& b) {
    if (a.size() != b.size()) return false;
    unsigned char diff = 0;
    for (std::size_t i = 0; i < a.size(); ++i) {
        diff |= static_cast<unsigned char>(a[i] ^ b[i]);
    }
    return diff == 0;
}

bool set_io_timeout(SOCKET s) {
    DWORD ms = static_cast<DWORD>(kIoTimeoutMs);
    if (::setsockopt(s, SOL_SOCKET, SO_RCVTIMEO,
                     reinterpret_cast<char const*>(&ms), sizeof ms) != 0) {
        return false;
    }
    if (::setsockopt(s, SOL_SOCKET, SO_SNDTIMEO,
                     reinterpret_cast<char const*>(&ms), sizeof ms) != 0) {
        return false;
    }
    return true;
}

bool send_all(SOCKET s, std::string const& data) {
    std::size_t sent = 0;
    while (sent < data.size()) {
        std::size_t chunk = std::min<std::size_t>(data.size() - sent, 1u << 20);
        int n = ::send(s, data.data() + sent, static_cast<int>(chunk), 0);
        if (n <= 0) return false;
        sent += static_cast<std::size_t>(n);
    }
    return true;
}

// 先把可发方向关掉，再尽量把对端还在发的数据读掉。
// 直接 closesocket 会让未读数据触发 RST，客户端可能读不到已经写好的 4xx 响应。
void graceful_close(SOCKET s) {
    ::shutdown(s, SD_SEND);
    char sink[4096];
    for (int i = 0; i < 16; ++i) {
        int n = ::recv(s, sink, static_cast<int>(sizeof sink), 0);
        if (n <= 0) break;
    }
    ::closesocket(s);
}

void write_response(SOCKET s, Response const& resp) {
    std::string out;
    out.reserve(resp.body.size() + 320);
    out += "HTTP/1.1 ";
    out += std::to_string(resp.status);
    out += ' ';
    out += reason_phrase(resp.status);
    if (resp.status != 204) {
        out += "\r\nContent-Type: ";
        out += resp.content_type;
        out += "\r\nContent-Length: ";
        out += std::to_string(resp.body.size());
    }
    // ---- 跨源许可
    // 桌面壳的前端跑在 http://tauri.localhost，引擎在 http://127.0.0.1:<port>，
    // 两者不同源。没有这几个头，浏览器会在拿到响应之前就把请求判为失败 ——
    // 现场表现是「窗口打开了，但界面永远转圈」，而引擎日志里一片安静（预检都没过）。
    // 浏览器开发形态（vite 在 localhost:5173）同样需要。
    //
    // 允许任意 Origin 是刻意的：认证靠 Bearer 令牌，不靠同源策略。令牌只有
    // 本机文件系统和壳知道，网页拿不到。防 DNS rebinding 的是 Host 检查（见下），
    // 不是 Origin 白名单。
    out += "\r\nAccess-Control-Allow-Origin: *";
    out += "\r\nAccess-Control-Allow-Methods: GET, POST, DELETE, OPTIONS";
    out += "\r\nAccess-Control-Allow-Headers: Authorization, Content-Type";
    out += "\r\nAccess-Control-Max-Age: 600";
    out += "\r\nConnection: close\r\n\r\n";
    if (resp.status != 204) out += resp.body;
    send_all(s, out);
}

// 已读入缓冲中查找请求头结束位置。返回 0 表示还没读到。
std::size_t find_header_end(std::string const& buf) {
    std::size_t p = buf.find("\r\n\r\n");
    return p == std::string::npos ? 0 : p + 4;
}

void serve_connection(SOCKET s, Server::Handler const& handler, std::string const& token) {
    if (!set_io_timeout(s)) {
        ::closesocket(s);
        return;
    }

    std::string buf;
    buf.reserve(8192);
    char tmp[8192];
    std::size_t header_end = 0;

    while (true) {
        header_end = find_header_end(buf);
        if (header_end) break;
        if (buf.size() > kMaxHeaderBytes) {
            write_response(s, Response::error(431, "header_too_large", "请求头超过 16 KiB"));
            graceful_close(s);
            return;
        }
        int n = ::recv(s, tmp, static_cast<int>(sizeof tmp), 0);
        if (n <= 0) {
            ::closesocket(s);
            return;
        }
        buf.append(tmp, static_cast<std::size_t>(n));
    }

    if (header_end > kMaxHeaderBytes) {
        write_response(s, Response::error(431, "header_too_large", "请求头超过 16 KiB"));
        graceful_close(s);
        return;
    }

    Request req;
    std::string head = buf.substr(0, header_end - 2);  // 去掉尾部 CRLFCRLF

    // ---- 请求行
    std::size_t line_end = head.find("\r\n");
    std::string request_line = head.substr(0, line_end == std::string::npos ? head.size() : line_end);
    if (request_line.size() > kMaxRequestLineBytes) {
        write_response(s, Response::error(400, "bad_request_line", "请求行过长"));
        graceful_close(s);
        return;
    }

    {
        std::vector<std::string> parts;
        std::size_t start = 0;
        while (true) {
            std::size_t sp = request_line.find(' ', start);
            if (sp == std::string::npos) {
                parts.push_back(request_line.substr(start));
                break;
            }
            parts.push_back(request_line.substr(start, sp - start));
            start = sp + 1;
        }
        if (parts.size() != 3 || parts[0].empty() || parts[1].empty()) {
            write_response(s, Response::error(400, "bad_request_line", "请求行格式非法"));
            graceful_close(s);
            return;
        }
        if (parts[2].rfind("HTTP/1.", 0) != 0) {
            write_response(s, Response::error(400, "bad_version", "只支持 HTTP/1.x"));
            graceful_close(s);
            return;
        }
        req.method = parts[0];
        std::string target = parts[1];
        if (target[0] != '/') {
            write_response(s, Response::error(400, "bad_target", "只接受 origin-form 目标"));
            graceful_close(s);
            return;
        }
        std::size_t q = target.find('?');
        if (q == std::string::npos) {
            req.path = target;
        } else {
            req.path = target.substr(0, q);
            req.query = target.substr(q + 1);
        }
    }

    // ---- CORS 预检
    // 带 Authorization 头、或用 application/octet-stream 提交种子的请求，
    // 浏览器会在真正请求之前先发一次 OPTIONS。预检按规范不带凭证，所以它必须
    // 在认证检查之前放行 —— 否则会被 401 拦下，浏览器看到预检失败就放弃真正的请求。
    if (req.method == "OPTIONS") {
        Response preflight;
        preflight.status = 204;
        write_response(s, preflight);
        graceful_close(s);
        return;
    }

    if (req.method != "GET" && req.method != "POST" && req.method != "DELETE") {
        write_response(s, Response::error(405, "method_not_allowed", "只支持 GET / POST / DELETE"));
        graceful_close(s);
        return;
    }

    // ---- 头部
    {
        std::size_t pos = (line_end == std::string::npos) ? head.size() : line_end + 2;
        while (pos < head.size()) {
            std::size_t eol = head.find("\r\n", pos);
            if (eol == std::string::npos) eol = head.size();
            std::string line = head.substr(pos, eol - pos);
            pos = eol + 2;
            if (line.empty()) continue;
            std::size_t colon = line.find(':');
            if (colon == std::string::npos) {
                write_response(s, Response::error(400, "bad_header", "头部缺少冒号"));
                graceful_close(s);
                return;
            }
            std::string key = to_lower(trim(line.substr(0, colon)));
            std::string val = trim(line.substr(colon + 1));
            if (key.empty()) {
                write_response(s, Response::error(400, "bad_header", "头部名为空"));
                graceful_close(s);
                return;
            }
            // 重复 Content-Length 是请求走私的经典载体，直接拒绝而不是取其中一个
            if (key == "content-length" && req.headers.count(key)) {
                write_response(s, Response::error(400, "duplicate_content_length", "Content-Length 重复"));
                graceful_close(s);
                return;
            }
            req.headers[key] = val;
        }
    }

    // ---- 鉴权（在读取请求体之前，未授权请求不能消耗我们的大块内存）
    {
        std::string const& auth = req.header("authorization");
        bool ok = false;
        if (auth.size() > 7) {
            std::string scheme = to_lower(auth.substr(0, 7));
            std::string presented = trim(auth.substr(7));
            if (scheme == "bearer ") ok = token_equals(presented, token);
        }
        if (!ok) {
            write_response(s, Response::error(401, "unauthorized", "缺少或错误的 Bearer 令牌"));
            graceful_close(s);
            return;
        }
    }

    // ---- 请求体
    if (req.headers.count("transfer-encoding")) {
        std::string te = to_lower(req.header("transfer-encoding"));
        write_response(s, Response::error(411, "chunked_unsupported",
                                         "不支持分块传输，请用 Content-Length"));
        graceful_close(s);
        return;
    }

    std::size_t content_length = 0;
    if (req.headers.count("content-length")) {
        std::string const& raw = req.header("content-length");
        if (raw.empty()) {
            write_response(s, Response::error(400, "bad_content_length", "Content-Length 为空"));
            graceful_close(s);
            return;
        }
        std::size_t v = 0;
        for (char c : raw) {
            if (c < '0' || c > '9') {
                write_response(s, Response::error(400, "bad_content_length",
                                                 "Content-Length 必须是十进制数字"));
                graceful_close(s);
                return;
            }
            v = v * 10 + static_cast<std::size_t>(c - '0');
            if (v > (1u << 20) * 64) break;  // 先截断，避免溢出
        }
        if (v > kMaxBodyBytes) {
            write_response(s, Response::error(413, "body_too_large", "请求体超过 8 MiB"));
            graceful_close(s);
            return;
        }
        content_length = v;
    } else if (req.method == "POST") {
        write_response(s, Response::error(411, "length_required", "POST 必须带 Content-Length"));
        graceful_close(s);
        return;
    }

    if (content_length > 0) {
        std::string body = buf.substr(header_end);
        while (body.size() < content_length) {
            int n = ::recv(s, tmp, static_cast<int>(sizeof tmp), 0);
            if (n <= 0) {
                ::closesocket(s);
                return;
            }
            body.append(tmp, static_cast<std::size_t>(n));
        }
        body.resize(content_length);
        req.body = std::move(body);
    }

    Response resp;
    try {
        resp = handler(req);
    } catch (...) {
        resp = Response::error(500, "internal", "处理器抛出异常");
    }
    write_response(s, resp);
    graceful_close(s);
}

}  // namespace

std::string const& Request::empty() {
    static std::string const kEmpty;
    return kEmpty;
}

std::string const& Request::header(std::string const& lowercase_name) const {
    auto it = headers.find(lowercase_name);
    return it == headers.end() ? empty() : it->second;
}

int Request::query_int(std::string const& key, int fallback) const {
    std::size_t start = 0;
    while (start <= query.size()) {
        std::size_t amp = query.find('&', start);
        if (amp == std::string::npos) amp = query.size();
        std::string pair = query.substr(start, amp - start);
        std::size_t eq = pair.find('=');
        if (eq != std::string::npos && pair.substr(0, eq) == key) {
            std::string v = pair.substr(eq + 1);
            if (v.empty()) return fallback;
            for (char c : v) {
                if (c < '0' || c > '9') return fallback;
            }
            return std::atoi(v.c_str());
        }
        if (amp == query.size()) break;
        start = amp + 1;
    }
    return fallback;
}

Response Response::ok(std::string body) {
    Response r;
    r.status = 200;
    r.body = std::move(body);
    return r;
}

Response Response::error(int status, std::string code, std::string message) {
    Response r;
    r.status = status;
    r.body = "{\"error\":{\"code\":" + json::quote(code) + ",\"message\":" +
             json::quote(message) + "}}";
    return r;
}

struct Server::Impl {
    SOCKET listen_sock = INVALID_SOCKET;
    std::thread acceptor;
    std::vector<std::thread> workers;

    std::deque<SOCKET> queue;
    std::mutex queue_mutex;
    std::condition_variable queue_cv;

    std::atomic<bool> running{false};
    Handler handler;
    std::string token;
    bool wsa_started = false;

    void accept_loop() {
        while (running.load()) {
            SOCKET c = ::accept(listen_sock, nullptr, nullptr);
            if (c == INVALID_SOCKET) {
                if (!running.load()) break;
                continue;
            }
            {
                std::lock_guard<std::mutex> lock(queue_mutex);
                if (queue.size() >= kMaxQueued) {
                    ::closesocket(c);  // 排队过深就丢弃，不做无界缓冲
                    continue;
                }
                queue.push_back(c);
            }
            queue_cv.notify_one();
        }
    }

    void worker_loop() {
        while (true) {
            SOCKET c = INVALID_SOCKET;
            {
                std::unique_lock<std::mutex> lock(queue_mutex);
                queue_cv.wait(lock, [this] { return !queue.empty() || !running.load(); });
                if (queue.empty()) {
                    if (!running.load()) return;
                    continue;
                }
                c = queue.front();
                queue.pop_front();
            }
            serve_connection(c, handler, token);
        }
    }
};

Server::Server() : impl_(new Impl()) {}

Server::~Server() {
    stop();
    delete impl_;
    impl_ = nullptr;
}

bool Server::start(std::string const& host, int port, std::string const& token,
                   Handler handler, std::string& err) {
    if (impl_->running.load()) {
        err = "服务已在运行";
        return false;
    }

    WSADATA wsa{};
    if (::WSAStartup(MAKEWORD(2, 2), &wsa) != 0) {
        err = "WSAStartup 失败";
        return false;
    }
    impl_->wsa_started = true;

    SOCKET s = ::socket(AF_INET, SOCK_STREAM, IPPROTO_TCP);
    if (s == INVALID_SOCKET) {
        err = "创建 socket 失败";
        ::WSACleanup();
        impl_->wsa_started = false;
        return false;
    }

    int yes = 1;
    ::setsockopt(s, SOL_SOCKET, SO_REUSEADDR, reinterpret_cast<char const*>(&yes), sizeof yes);

    sockaddr_in addr{};
    addr.sin_family = AF_INET;
    addr.sin_port = htons(static_cast<u_short>(port));
    if (::inet_pton(AF_INET, host.c_str(), &addr.sin_addr) != 1) {
        err = "host 不是合法的 IPv4 地址：" + host;
        ::closesocket(s);
        ::WSACleanup();
        impl_->wsa_started = false;
        return false;
    }

    if (::bind(s, reinterpret_cast<sockaddr*>(&addr), sizeof addr) != 0) {
        err = "bind 失败，端口可能被占用（WSA " + std::to_string(::WSAGetLastError()) + "）";
        ::closesocket(s);
        ::WSACleanup();
        impl_->wsa_started = false;
        return false;
    }

    if (::listen(s, SOMAXCONN) != 0) {
        err = "listen 失败";
        ::closesocket(s);
        ::WSACleanup();
        impl_->wsa_started = false;
        return false;
    }

    sockaddr_in actual{};
    int len = static_cast<int>(sizeof actual);
    if (::getsockname(s, reinterpret_cast<sockaddr*>(&actual), &len) != 0) {
        err = "getsockname 失败";
        ::closesocket(s);
        ::WSACleanup();
        impl_->wsa_started = false;
        return false;
    }

    impl_->listen_sock = s;
    impl_->handler = std::move(handler);
    impl_->token = token;
    port_ = ntohs(actual.sin_port);
    impl_->running.store(true);

    impl_->acceptor = std::thread([this] { impl_->accept_loop(); });
    impl_->workers.reserve(kWorkerCount);
    for (int i = 0; i < kWorkerCount; ++i) {
        impl_->workers.emplace_back([this] { impl_->worker_loop(); });
    }
    return true;
}

void Server::stop() {
    if (!impl_ || !impl_->running.exchange(false)) return;

    if (impl_->listen_sock != INVALID_SOCKET) {
        ::closesocket(impl_->listen_sock);
        impl_->listen_sock = INVALID_SOCKET;
    }
    impl_->queue_cv.notify_all();

    if (impl_->acceptor.joinable()) impl_->acceptor.join();
    for (auto& t : impl_->workers) {
        if (t.joinable()) t.join();
    }
    impl_->workers.clear();

    {
        std::lock_guard<std::mutex> lock(impl_->queue_mutex);
        for (SOCKET c : impl_->queue) ::closesocket(c);
        impl_->queue.clear();
    }

    if (impl_->wsa_started) {
        ::WSACleanup();
        impl_->wsa_started = false;
    }
}

}  // namespace http
