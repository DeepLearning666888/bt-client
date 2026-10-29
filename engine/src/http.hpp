// 最小 HTTP/1.1 服务，只服务本地控制面。
//
// 能力边界是刻意划死的：
//   * 只支持 GET / POST / DELETE
//   * 只认 Content-Length，收到 Transfer-Encoding: chunked 直接 411
//   * 只发 Connection: close，不做 keep-alive
//   * 不实现 TLS
// 部分支持比明确不支持更危险：客户端会以为能用，直到线上才发现。
//
// 鉴权在这里统一做，处理器拿到的请求一定已通过令牌校验。
// 这样任何一个新加的路由都不可能忘记鉴权——写循环而不是写约定。

#ifndef ENGINE_HTTP_HPP
#define ENGINE_HTTP_HPP

#include <cstdint>
#include <functional>
#include <string>
#include <string_view>
#include <unordered_map>

namespace http {

struct Request {
    std::string method;
    std::string path;   // 已按 '?' 切分
    std::string query;
    std::string body;
    std::unordered_map<std::string, std::string> headers;  // key 一律小写

    static const std::string& empty();

    // 未命中返回空串引用
    std::string const& header(std::string const& lowercase_name) const;

    // 取 query 里的整数值，缺省或非法时返回 fallback
    int query_int(std::string const& key, int fallback) const;
};

struct Response {
    int status = 200;
    std::string content_type = "application/json; charset=utf-8";
    std::string body;

    static Response ok(std::string body);
    static Response error(int status, std::string code, std::string message);
};

class Server {
public:
    using Handler = std::function<Response(Request const&)>;

    Server();
    ~Server();

    Server(Server const&) = delete;
    Server& operator=(Server const&) = delete;

    // host 只会是回环地址；port 传 0 由内核分配，成功后用 port() 取实际值
    bool start(std::string const& host, int port, std::string const& token,
               Handler handler, std::string& err);

    int port() const { return port_; }

    void stop();

private:
    struct Impl;
    Impl* impl_ = nullptr;
    int port_ = 0;
};

}  // namespace http

#endif  // ENGINE_HTTP_HPP
