#include <cstdio>
#include <windows.h>
#include <string>

int main() {
    std::printf("MSVC toolchain OK\n");
    std::printf("_MSC_VER = %d\n", _MSC_VER);
    std::printf("WINAPI_FAMILY default ok\n");

    // pull one real symbol from the SDK so the linker actually runs
    char buf[MAX_PATH] = {0};
    DWORD n = GetWindowsDirectoryA(buf, MAX_PATH);
    std::printf("GetWindowsDirectoryA -> %lu, \"%s\"\n", n, buf);

    SYSTEM_INFO si{};
    GetNativeSystemInfo(&si);
    std::printf("processor arch = %u, cores = %lu\n",
                si.wProcessorArchitecture, si.dwNumberOfProcessors);
    return 0;
}
