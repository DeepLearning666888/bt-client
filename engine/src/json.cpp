#include "json.hpp"

#include <cstdio>
#include <cstdint>

namespace json {
namespace {

// UTF-8 编码一个码点
void append_utf8(std::string& out, std::uint32_t cp) {
    if (cp < 0x80) {
        out.push_back(static_cast<char>(cp));
    } else if (cp < 0x800) {
        out.push_back(static_cast<char>(0xC0 | (cp >> 6)));
        out.push_back(static_cast<char>(0x80 | (cp & 0x3F)));
    } else if (cp < 0x10000) {
        out.push_back(static_cast<char>(0xE0 | (cp >> 12)));
        out.push_back(static_cast<char>(0x80 | ((cp >> 6) & 0x3F)));
        out.push_back(static_cast<char>(0x80 | (cp & 0x3F)));
    } else {
        out.push_back(static_cast<char>(0xF0 | (cp >> 18)));
        out.push_back(static_cast<char>(0x80 | ((cp >> 12) & 0x3F)));
        out.push_back(static_cast<char>(0x80 | ((cp >> 6) & 0x3F)));
        out.push_back(static_cast<char>(0x80 | (cp & 0x3F)));
    }
}

int hex_val(char c) {
    if (c >= '0' && c <= '9') return c - '0';
    if (c >= 'a' && c <= 'f') return c - 'a' + 10;
    if (c >= 'A' && c <= 'F') return c - 'A' + 10;
    return -1;
}

struct Scanner {
    std::string_view s;
    std::size_t i = 0;

    bool eof() const { return i >= s.size(); }

    void skip_ws() {
        while (i < s.size()) {
            char c = s[i];
            if (c == ' ' || c == '\t' || c == '\n' || c == '\r') ++i;
            else break;
        }
    }

    bool eat(char c) {
        skip_ws();
        if (i < s.size() && s[i] == c) { ++i; return true; }
        return false;
    }

    bool peek(char c) {
        skip_ws();
        return i < s.size() && s[i] == c;
    }

    bool scan_string(std::string& out) {
        skip_ws();
        if (eof() || s[i] != '"') return false;
        ++i;
        out.clear();
        while (i < s.size()) {
            char c = s[i++];
            if (c == '"') return true;
            if (c != '\\') { out.push_back(c); continue; }
            if (eof()) return false;
            char e = s[i++];
            switch (e) {
                case '"':  out.push_back('"');  break;
                case '\\': out.push_back('\\'); break;
                case '/':  out.push_back('/');  break;
                case 'b':  out.push_back('\b'); break;
                case 'f':  out.push_back('\f'); break;
                case 'n':  out.push_back('\n'); break;
                case 'r':  out.push_back('\r'); break;
                case 't':  out.push_back('\t'); break;
                case 'u': {
                    if (i + 4 > s.size()) return false;
                    std::uint32_t cp = 0;
                    for (int k = 0; k < 4; ++k) {
                        int v = hex_val(s[i + k]);
                        if (v < 0) return false;
                        cp = (cp << 4) | static_cast<std::uint32_t>(v);
                    }
                    i += 4;
                    // 代理对不做合并：控制面只接受磁力链接，出现代理对说明输入非法
                    if (cp >= 0xD800 && cp <= 0xDFFF) return false;
                    append_utf8(out, cp);
                    break;
                }
                default: return false;
            }
        }
        return false;  // 未闭合
    }

    bool skip_value(int depth = 0) {
        if (depth > 8) return false;
        skip_ws();
        if (eof()) return false;
        char c = s[i];
        if (c == '"') { std::string tmp; return scan_string(tmp); }
        if (c == '{' || c == '[') {
            char open = c;
            char close = (c == '{') ? '}' : ']';
            ++i;
            if (eat(close)) return true;
            while (true) {
                if (open == '{') {
                    std::string k;
                    if (!scan_string(k)) return false;
                    if (!eat(':')) return false;
                }
                if (!skip_value(depth + 1)) return false;
                if (eat(',')) continue;
                return eat(close);
            }
        }
        // 数字或字面量：吃到分隔符为止
        std::size_t start = i;
        while (i < s.size()) {
            char d = s[i];
            if (d == ',' || d == '}' || d == ']' || d == ' ' || d == '\t' || d == '\n' || d == '\r') break;
            ++i;
        }
        return i > start;
    }
};

}  // namespace

void append_escaped(std::string& out, std::string_view s) {
    for (char ch : s) {
        unsigned char c = static_cast<unsigned char>(ch);
        switch (c) {
            case '"':  out += "\\\""; break;
            case '\\': out += "\\\\"; break;
            case '\b': out += "\\b";  break;
            case '\f': out += "\\f";  break;
            case '\n': out += "\\n";  break;
            case '\r': out += "\\r";  break;
            case '\t': out += "\\t";  break;
            default:
                if (c < 0x20) {
                    char buf[7];
                    std::snprintf(buf, sizeof buf, "\\u%04x", c);
                    out += buf;
                } else {
                    out.push_back(ch);
                }
        }
    }
}

std::string quote(std::string_view s) {
    std::string out;
    out.reserve(s.size() + 2);
    out.push_back('"');
    append_escaped(out, s);
    out.push_back('"');
    return out;
}

bool get_string(std::string_view body, std::string_view key, std::string& out) {
    Scanner sc{body};
    if (!sc.eat('{')) return false;
    if (sc.eat('}')) return false;
    while (true) {
        std::string k;
        if (!sc.scan_string(k)) return false;
        if (!sc.eat(':')) return false;
        if (k == key) {
            std::string v;
            if (!sc.scan_string(v)) return false;
            // 命中字段后仍要求能走到对象结束，避免半截 JSON 被当成合法输入
            if (!sc.eat('}') && !sc.eat(',')) return false;
            out = std::move(v);
            return true;
        }
        if (!sc.skip_value()) return false;
        if (sc.eat(',')) continue;
        return false;
    }
}

std::string number(double v) {
    char buf[64];
    std::snprintf(buf, sizeof buf, "%.6f", v);
    return buf;
}

}  // namespace json
