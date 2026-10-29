#include "test_framework.hpp"

#include <cstdio>

namespace testing {

std::vector<TestCase>& registry() {
    static std::vector<TestCase> r;
    return r;
}

int g_failures = 0;
int g_checks = 0;

Registrar::Registrar(char const* name, void (*fn)()) {
    registry().push_back(TestCase{name, fn});
}

void report_failure(char const* file, int line, char const* expr, std::string const& detail) {
    ++g_failures;
    std::printf("       %s:%d  %s%s%s\n", file, line, expr,
                detail.empty() ? "" : "  --  ", detail.c_str());
}

}  // namespace testing

int main() {
    int cases = 0;
    int failed_cases = 0;

    for (auto const& tc : testing::registry()) {
        ++cases;
        int const before = testing::g_failures;
        tc.fn();
        bool const ok = (testing::g_failures == before);
        if (!ok) ++failed_cases;
        std::printf("[%s] %s\n", ok ? " ok " : "FAIL", tc.name);
    }

    std::printf("\n%d case(s), %d check(s), %d failure(s)\n", cases, testing::g_checks,
                testing::g_failures);
    return testing::g_failures == 0 ? 0 : 1;
}
