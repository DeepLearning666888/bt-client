// 引擎：libtorrent 会话的拥有者，只跑在自己的线程上。
//
// 线程模型（这是整个设计的核心）：
//   * 会话与全部任务状态只由 worker 线程触碰，因此不需要给它们加锁
//   * HTTP 处理器不直接调用 libtorrent，而是把任务投进队列并等结果
//   * 状态快照每 100ms 在 worker 线程算好，HTTP 只拷贝一个字符串
// 结果：/state 的延迟与任务数量无关，也不必为每个字段设计锁。

#ifndef ENGINE_ENGINE_HPP
#define ENGINE_ENGINE_HPP

#include <cstdint>
#include <memory>
#include <string>

namespace eng {

struct Options {
    std::string data_dir;
    bool no_network = false;                              // 测试用：关掉 DHT/LSD/UPnP/NAT-PMP
    std::string listen_interfaces = "0.0.0.0:0";          // BT 监听口，0 表示由内核分配
    // 运行期周期落盘的间隔（毫秒）。≤0 表示关掉周期落盘，只保留「加任务即落盘」
    // 与退出时落盘。做成可配置不是为了调优 —— 是为了让「到点会不会落盘」这件事
    // 能在测试里用 200ms 验证，而不是让每个用例干等 30 秒。
    std::int64_t periodic_save_ms = 30'000;
};

class Engine {
public:
    explicit Engine(Options opts);
    ~Engine();

    Engine(Engine const&) = delete;
    Engine& operator=(Engine const&) = delete;

    bool start(std::string& err);
    void stop();

    void set_http_listen(std::string const& addr);

    // 返回完整响应体，调用方直接写 socket
    std::string state_json() const;
    std::uint64_t state_seq() const;

    bool add_magnet(std::string const& uri, std::string& infohash, std::string& err);
    bool add_torrent_bytes(std::string const& bytes, std::string& infohash, std::string& err);
    bool pause(std::string const& infohash, std::string& err);
    bool resume(std::string const& infohash, std::string& err);
    bool remove_torrent(std::string const& infohash, bool delete_files, std::string& err);

private:
    struct Impl;
    std::unique_ptr<Impl> impl_;
};

}  // namespace eng

#endif  // ENGINE_ENGINE_HPP
