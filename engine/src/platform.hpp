// Windows 平台相关的最小封装：单实例锁、随机令牌、原子写文件。
// 单独成文件是为了让 engine.cpp / main.cpp 不直接出现 Win32 调用。

#ifndef ENGINE_PLATFORM_HPP
#define ENGINE_PLATFORM_HPP

#include <cstdint>
#include <string>
#include <string_view>

namespace platform {

// 独占打开锁文件。第二个实例会失败，并在 err 里给出原因。
// 返回空 unique_ptr 表示获取失败。进程退出（含崩溃）时句柄自动释放。
class InstanceLock {
public:
    InstanceLock() = default;
    ~InstanceLock();
    InstanceLock(InstanceLock const&) = delete;
    InstanceLock& operator=(InstanceLock const&) = delete;
    InstanceLock(InstanceLock&&) noexcept;

    bool acquire(std::string const& lock_path, std::string& err);
    bool held() const { return handle_ != nullptr; }

private:
    void* handle_ = nullptr;  // HANDLE，避免在头里引 windows.h
};

// OS 级随机数转十六进制。令牌不能用 rand()/mt19937：可预测即等于没有。
std::string random_token_hex(int bytes);

// 先写临时文件再替换目标，避免写一半断电留下半截 JSON。
bool write_file_atomic(std::string const& path, std::string_view data, std::string& err);

// 系统 ANSI 代码页的字节串转 UTF-8。
//
// 为什么需要它：Windows 上 `main(int, char**)` 拿到的参数是 ANSI（简中系统为 GBK）
// 编码的，不是 UTF-8。进程内部全程用 ANSI 字节串是对的 —— std::filesystem 的窄
// 字符串在 Windows 上也按 ANSI 解读，两者一致。但只要把它原样写进 JSON 就是错的：
// JSON 要求 UTF-8，接收方按 UTF-8 解码会直接失败。
//
// 这不是理论问题：数据目录或安装路径里带中文（中文 Windows 的用户名目录很常见）
// 就会触发，表现为壳一直等不到引擎就绪，而两侧日志都不报错。
std::string native_to_utf8(std::string const& s);

std::int64_t epoch_ms();

}  // namespace platform

#endif  // ENGINE_PLATFORM_HPP
