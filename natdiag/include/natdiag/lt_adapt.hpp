// 适配层：整个 natdiag 里唯一接触 libtorrent 的头。
// 只做一件事 —— 把 alert 翻译成 NatEvent，不做任何判定。

#ifndef NATDIAG_ADAPT_HPP
#define NATDIAG_LT_ADAPT_HPP

#include <natdiag/natdiag.hpp>

#include <libtorrent/alert.hpp>

namespace natdiag {

// 返回 false 表示这个 alert 与本模块无关，调用方直接忽略。
bool from_alert(libtorrent::alert const& a, NatEvent& out);

}  // namespace natdiag

#endif  // NATDIAG_ADAPT_HPP
