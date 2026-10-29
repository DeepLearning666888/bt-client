cmake_minimum_required(VERSION 3.20)
project(bt_engine LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

# 本机 libtorrent 为手工构建，导出包名固定是 LibtorrentRasterbar
find_package(LibtorrentRasterbar CONFIG QUIET)
if (NOT LibtorrentRasterbar_FOUND)
    message(FATAL_ERROR
        "找不到 libtorrent，需指定：\n"
        "  -DLibtorrentRasterbar_DIR=C:/libtorrent/install/lib/cmake/LibtorrentRasterbar")
endif()

# natdiag 的测试在它自己的构建里跑，这里只取库
set(NATDIAG_BUILD_TESTS OFF CACHE BOOL "" FORCE)
add_subdirectory(../natdiag "${CMAKE_BINARY_DIR}/natdiag")

add_executable(engine
    src/main.cpp
    src/http.cpp
    src/json.cpp
    src/engine.cpp
    src/platform.cpp)

target_link_libraries(engine PRIVATE
    natdiag_lt
    ws2_32      # Winsock：HTTP 层直接用原生 socket，不引第三方 HTTP 库
    bcrypt)     # BCryptGenRandom：令牌必须是 OS 级随机，不能用 rand()

# 只监听回环，HTTP 层是 Windows 原生实现；跨平台留待后续（见 spec 未验证项）
target_compile_definitions(engine PRIVATE
    WIN32_LEAN_AND_MEAN
    NOMINMAX
    _WIN32_WINNT=0x0601)

if (MSVC)
    target_compile_options(engine PRIVATE /W3 /utf-8)
endif()

# 运行期 DLL 放到 exe 同目录，不依赖 PATH
add_custom_command(TARGET engine POST_BUILD
    COMMAND ${CMAKE_COMMAND} -E copy_if_different
        "$<TARGET_FILE:LibtorrentRasterbar::torrent-rasterbar>"
        "$<TARGET_FILE_DIR:engine>"
    VERBATIM)
