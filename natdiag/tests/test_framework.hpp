// 零依赖断言框架。
// 不引 Catch2 的理由：这个环境装包成本高于收益，而这里要测的东西
// 只有枚举和整数比较，40 行够用。

#ifndef NATDIAG_TEST_FRAMEWORK_HPP
#define NATDIAG_TEST_FRAMEWORK_HPP

#include <cstdio>
#include <string>
#include <vector>

namespace testing {

struct TestCase {
    char const* name;
    void (*fn)();
};

std::vector<TestCase>& registry();

extern int g_failures;
extern int g_checks;

struct Registrar {
    Registrar(char const* name, void (*fn)());
};

void report_failure(char const* file, int line, char const* expr, std::string const& detail);

}  // namespace testing

#define TEST_CASE(name)                                          \
    static void name();                                          \
    static ::testing::Registrar reg_##name(#name, &name);         \
    static void name()

#define CHECK(cond)                                                              \
    do {                                                                         \
        ++::testing::g_checks;                                                    \
        if (!(cond)) ::testing::report_failure(__FILE__, __LINE__, #cond, "");    \
    } while (0)

#define CHECK_INT_EQ(actual, expected)                                            \
    do {                                                                          \
        ++::testing::g_checks;                                                     \
        long long a_ = static_cast<long long>(actual);                              \
        long long e_ = static_cast<long long>(expected);                            \
        if (a_ != e_) {                                                             \
            ::testing::report_failure(__FILE__, __LINE__, #actual,                   \
                std::to_string(a_) + " != expected " + std::to_string(e_));          \
        }                                                                           \
    } while (0)

#define CHECK_STR_EQ(actual, expected)                                            \
    do {                                                                          \
        ++::testing::g_checks;                                                     \
        std::string a_ = (actual);                                                  \
        std::string e_ = (expected);                                                \
        if (a_ != e_) {                                                             \
            ::testing::report_failure(__FILE__, __LINE__, #actual,                   \
                "\"" + a_ + "\" != expected \"" + e_ + "\"");                        \
        }                                                                           \
    } while (0)

#endif  // NATDIAG_TEST_FRAMEWORK_HPP
