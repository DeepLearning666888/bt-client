#ifndef NATDIAG_SERIALIZE_HPP
#define NATDIAG_SERIALIZE_HPP

#include <natdiag/natdiag.hpp>

#include <string>

namespace natdiag {

// 给 M2 桌面端 / HTTP 接口用。字段与 natdiag-module-spec.html 5.2 一致。
std::string to_json(Snapshot const& s);

// 给 CLI 探针与日志用。会额外输出人读的处置建议。
std::string to_text(Snapshot const& s);

}  // namespace natdiag

#endif  // NATDIAG_SERIALIZE_HPP
