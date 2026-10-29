// 极简 JSON 工具。
//
// 只做两件事，够用即止：
//   * 生成：转义 + 拼串，供 /state 输出
//   * 提取：从请求体里取一个顶层字符串字段（如 "magnet"）
//
// 不引入 JSON 库的理由见 spec：vcpkg 装不了新依赖，而控制面只接受一种形状的
// 输入。但"够用"不等于"放过错误"——解析失败必须返回 false 让上层回 400，
// 不能静默取到空串。

#ifndef ENGINE_JSON_HPP
#define ENGINE_JSON_HPP

#include <string>
#include <string_view>

namespace json {

// 按 RFC 8259 转义并加上两侧引号
std::string quote(std::string_view s);

// 追加转义后的内容（不含引号）
void append_escaped(std::string& out, std::string_view s);

// 取顶层对象里的字符串字段。成功返回 true 并写入 out。
// 仅支持：顶层为 object、字段值必须是字符串、不支持嵌套路径。
bool get_string(std::string_view body, std::string_view key, std::string& out);

// 固定 6 位小数的数值文本，不依赖 locale（全程不调用 setlocale）
std::string number(double v);

}  // namespace json

#endif  // ENGINE_JSON_HPP
