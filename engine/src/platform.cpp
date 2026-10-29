#include "platform.hpp"

#include <windows.h>
#include <bcrypt.h>

#include <chrono>
#include <cstdio>
#include <vector>

namespace platform {
namespace {

char const* kHex = "0123456789abcdef";

bool move_replace(std::string const& from, std::string const& to, std::string& err) {
    if (::MoveFileExA(from.c_str(), to.c_str(),
                      MOVEFILE_REPLACE_EXISTING | MOVEFILE_WRITE_THROUGH)) {
        return true;
    }
    err = "替换目标文件失败，Win32 错误码 " + std::to_string(::GetLastError());
    return false;
}

}  // namespace

InstanceLock::~InstanceLock() {
    if (handle_) {
        ::CloseHandle(static_cast<HANDLE>(handle_));
        handle_ = nullptr;
    }
}

InstanceLock::InstanceLock(InstanceLock&& other) noexcept : handle_(other.handle_) {
    other.handle_ = nullptr;
}

bool InstanceLock::acquire(std::string const& lock_path, std::string& err) {
    if (handle_) return true;
    // 共享模式 0：同一时刻只允许一个进程持有
    HANDLE h = ::CreateFileA(lock_path.c_str(), GENERIC_WRITE, 0, nullptr,
                             OPEN_ALWAYS, FILE_ATTRIBUTE_NORMAL, nullptr);
    if (h == INVALID_HANDLE_VALUE) {
        DWORD e = ::GetLastError();
        if (e == ERROR_SHARING_VIOLATION) {
            err = "已有实例正在使用该数据目录";
        } else {
            err = "无法创建锁文件 " + lock_path + "（Win32 错误码 " + std::to_string(e) + "）";
        }
        return false;
    }
    handle_ = h;
    return true;
}

std::string random_token_hex(int bytes) {
    if (bytes <= 0) bytes = 16;
    std::vector<unsigned char> buf(static_cast<std::size_t>(bytes));
    if (::BCryptGenRandom(nullptr, buf.data(), static_cast<ULONG>(buf.size()),
                          BCRYPT_USE_SYSTEM_PREFERRED_RNG) != 0) {
        // 拿不到系统随机数就不能起服务：退化成可预测令牌比直接失败更危险
        return {};
    }
    std::string out;
    out.reserve(buf.size() * 2);
    for (unsigned char c : buf) {
        out.push_back(kHex[c >> 4]);
        out.push_back(kHex[c & 0x0F]);
    }
    return out;
}

bool write_file_atomic(std::string const& path, std::string_view data, std::string& err) {
    std::string tmp = path + ".tmp";
    FILE* f = std::fopen(tmp.c_str(), "wb");
    if (!f) {
        err = "无法写入临时文件 " + tmp;
        return false;
    }
    std::size_t written = data.empty() ? 0 : std::fwrite(data.data(), 1, data.size(), f);
    bool flushed = std::fflush(f) == 0;
    std::fclose(f);
    if (written != data.size() || !flushed) {
        err = "写入不完整 " + tmp;
        std::remove(tmp.c_str());
        return false;
    }
    if (!move_replace(tmp, path, err)) {
        std::remove(tmp.c_str());
        return false;
    }
    return true;
}

std::string native_to_utf8(std::string const& s) {
    if (s.empty()) return s;

    // 第一步：ANSI → 宽字符。失败时原样返回 —— 宁可少一次转码，也不要把路径变成空串，
    // 那会让调用方拿到一个"合法"但错误的值，比明显的错误更难查。
    int wide_len = ::MultiByteToWideChar(CP_ACP, 0, s.data(), static_cast<int>(s.size()), nullptr, 0);
    if (wide_len <= 0) return s;
    std::wstring wide(static_cast<std::size_t>(wide_len), L'\0');
    if (::MultiByteToWideChar(CP_ACP, 0, s.data(), static_cast<int>(s.size()), wide.data(), wide_len) <= 0) {
        return s;
    }

    // 第二步：宽字符 → UTF-8
    int utf8_len = ::WideCharToMultiByte(CP_UTF8, 0, wide.data(), wide_len, nullptr, 0, nullptr, nullptr);
    if (utf8_len <= 0) return s;
    std::string out(static_cast<std::size_t>(utf8_len), '\0');
    if (::WideCharToMultiByte(CP_UTF8, 0, wide.data(), wide_len, out.data(), utf8_len, nullptr, nullptr) <= 0) {
        return s;
    }
    return out;
}

std::int64_t epoch_ms() {
    using namespace std::chrono;
    return duration_cast<milliseconds>(system_clock::now().time_since_epoch()).count();
}

}  // namespace platform
