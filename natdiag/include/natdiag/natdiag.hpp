// natdiag —— 入站 / 端口映射诊断
//
// 设计约束：
//   * 本头文件不包含任何 libtorrent 头，宿主不需要为了用诊断而引引擎头。
//   * 不持有 session、不自己跑协议，只消费事件并给出结论。
//   * 输入单元是自定义的 NatEvent，不是 lt::alert —— 这样判定逻辑能脱离
//     网络做穷举测试。lt::alert 只在 adapt.cpp 里出现。

#ifndef NATDIAG_NATDIAG_HPP
#define NATDIAG_NATDIAG_HPP

#include <cstdint>
#include <string>
#include <vector>

namespace natdiag {

inline constexpr int kSlotCount = 4;
inline constexpr int kDefaultMinSampleSeconds = 15;

enum class Transport : std::uint8_t { natpmp = 0, upnp = 1 };
enum class Protocol : std::uint8_t { tcp = 0, udp = 1 };

// 注意这里没有 "rejected"。
// libtorrent 的 portmap_error_alert 不带 protocol 字段，只有 per-socket 的
// mapping 索引。虽然 remap_ports() 内部固定先 tcp 后 udp，索引看似可以推断，
// 但 delete_mapping 之后索引是否复位并没有写在文档里，而 remap_ports 会被
// 网络变化反复触发。靠这个推断去标某个协议"被拒"，是在赌未公开的实现细节。
// 所以明确错误一律进 Snapshot::unattributed_errors，不假装知道是哪个协议。
enum class SlotState : std::uint8_t {
    unmapped,   // 从未有过任何相关事件
    pending,    // 请求已发出，没有回音
    mapped,     // 拿到外部端口
};

enum class Verdict : std::uint8_t {
    working,        // 至少一个组合拿到映射，上传能力可恢复
    refused,        // 网关/设备明确拒绝
    silent,         // 请求已发出，无应答
    disabled,       // 协议压根没启用
    insufficient,   // 采样窗口太短，不下结论
};

enum class Remediation : std::uint8_t {
    none,
    manual_port_forward,
    check_firewall,
    enable_protocol,
    sample_longer,
};

char const* to_string(Transport t);
char const* to_string(Protocol p);
char const* to_string(SlotState s);
char const* to_string(Verdict v);
char const* to_string(Remediation r);

// slot 固定顺序，保证 4 项输出稳定：
//   0=(natpmp,tcp) 1=(natpmp,udp) 2=(upnp,tcp) 3=(upnp,udp)
int slot_index(Transport t, Protocol p);
Transport slot_transport(int idx);
Protocol slot_protocol(int idx);

// 唯一输入单元。
struct NatEvent {
    enum class Kind : std::uint8_t {
        listen_ok,
        listen_fail,
        map_ok,
        map_err,
        map_log,
        external_ip,
    };

    Kind kind = Kind::map_log;
    Transport transport = Transport::natpmp;
    Protocol protocol = Protocol::tcp;  // 只有 map_ok 可信，map_err 时无意义
    int port = 0;
    int error_code = 0;
    std::string detail;
};

struct SlotInfo {
    Transport transport = Transport::natpmp;
    Protocol protocol = Protocol::tcp;
    SlotState state = SlotState::unmapped;
    int external_port = 0;  // 仅 mapped 有效
    int log_lines = 0;      // 证据计数，用于区分"发了没人回"和"没发"
};

struct Snapshot {
    std::vector<std::string> listen_ok;
    std::vector<std::string> listen_fail;
    SlotInfo slots[kSlotCount];

    std::string external_ip;
    // 只有在至少一个 slot 是 mapped 时才为 true。
    // libtorrent 也会从 peer / tracker 处得知外网地址，那不能证明能入站。
    bool external_ip_reliable = false;

    // 所有"明确错误"都在这里，形如 "natpmp: error 718 -- ..."。
    // 拿不到协议归属，但一条都不能丢 —— 否则会把"被拒绝"误判成"没启用"。
    std::vector<std::string> unattributed_errors;

    Verdict verdict = Verdict::insufficient;
    std::vector<Remediation> remediation;
    int sampled_seconds = 0;
    // 与 sampled_seconds 一起输出，消费者才解释得清 insufficient 的由来
    int min_sample_seconds = kDefaultMinSampleSeconds;
};

class Collector {
public:
    explicit Collector(int min_sample_seconds = kDefaultMinSampleSeconds);

    void feed(NatEvent const& ev);
    void set_elapsed(int seconds);

    bool has_inbound_mapping() const;
    Snapshot snapshot() const;  // 纯查询，不改变状态

private:
    SlotInfo slots_[kSlotCount];
    std::vector<std::string> listen_ok_;
    std::vector<std::string> listen_fail_;
    std::vector<std::string> unattributed_;
    std::string external_ip_;
    int sampled_seconds_ = 0;
    int min_sample_seconds_;
};

}  // namespace natdiag

#endif  // NATDIAG_NATDIAG_HPP
