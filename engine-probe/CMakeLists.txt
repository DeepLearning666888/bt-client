cmake_minimum_required(VERSION 3.20)
project(engine_probe LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

# libtorrent 的导出包名统一是 "LibtorrentRasterbar"，两种获取方式都能用：
#   A) vcpkg:      -DCMAKE_TOOLCHAIN_FILE=<vcpkg>/scripts/buildsystems/vcpkg.cmake
#   B) 手工构建:   -DLibtorrentRasterbar_DIR=C:/libtorrent/install/lib/cmake/LibtorrentRasterbar
# 本机走的是 B（vcpkg 的 vcvars 依赖被黑名单拦掉的 reg.exe，见 README）。
find_package(LibtorrentRasterbar CONFIG QUIET)
if (NOT LibtorrentRasterbar_FOUND)
    message(FATAL_ERROR
        "找不到 libtorrent。二选一：\n"
        "  -DLibtorrentRasterbar_DIR=C:/libtorrent/install/lib/cmake/LibtorrentRasterbar\n"
        "  -DCMAKE_TOOLCHAIN_FILE=<vcpkg>/scripts/buildsystems/vcpkg.cmake")
endif()

# natdiag 是独立模块，测试在它自己的构建里跑，这里不重复编
set(NATDIAG_BUILD_TESTS OFF CACHE BOOL "" FORCE)
add_subdirectory(../natdiag "${CMAKE_BINARY_DIR}/natdiag")

add_executable(engine-probe src/main.cpp)
target_link_libraries(engine-probe PRIVATE natdiag_lt)

# 旧实现，仅用于双二进制回归比对；结论一致后可以删掉
add_executable(engine-probe-legacy src/main.legacy.cpp)
target_link_libraries(engine-probe-legacy PRIVATE LibtorrentRasterbar::torrent-rasterbar)

# 把运行期 DLL 放到 exe 同目录。
# 不依赖 PATH：脚本环境里的 PATH 可能混着 Windows 风格条目，
# ':' 分隔符会把 "C:\..." 切碎，导致 exe 起来就 127。
foreach (_tgt engine-probe engine-probe-legacy)
    add_custom_command(TARGET ${_tgt} POST_BUILD
        COMMAND ${CMAKE_COMMAND} -E copy_if_different
            "$<TARGET_FILE:LibtorrentRasterbar::torrent-rasterbar>"
            "$<TARGET_FILE_DIR:${_tgt}>"
        VERBATIM)
endforeach()

if (MSVC)
    # the binary is a diagnostic tool, not shipping code: keep warnings visible
    # but do not let them block the build
    target_compile_options(engine-probe PRIVATE /W3 /utf-8)
    target_compile_options(engine-probe-legacy PRIVATE /W3 /utf-8)
endif()
