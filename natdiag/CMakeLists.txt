cmake_minimum_required(VERSION 3.20)

# 被 add_subdirectory 引入时不重复声明 project()，避免覆盖宿主工程名
if(CMAKE_SOURCE_DIR STREQUAL CMAKE_CURRENT_SOURCE_DIR)
    project(natdiag LANGUAGES CXX)
    set(NATDIAG_STANDALONE ON)
endif()

# 核心库：不依赖 libtorrent。单元测试只需要它，编译秒级完成。
add_library(natdiag STATIC
    src/collector.cpp
    src/diagnosis.cpp
    src/serialize.cpp)
target_include_directories(natdiag PUBLIC include PRIVATE src)
target_compile_features(natdiag PUBLIC cxx_std_17)
target_compile_options(natdiag PRIVATE $<$<CXX_COMPILER_ID:MSVC>:/W3 /utf-8>)

# 适配层：整个模块里唯一接触 libtorrent 的目标。
# 拆成独立 target 是为了让核心逻辑和测试不背上引擎依赖。
find_package(LibtorrentRasterbar CONFIG QUIET)
if (LibtorrentRasterbar_FOUND)
    add_library(natdiag_lt STATIC src/adapt.cpp)
    # PUBLIC：lt_adapt.hpp 是公开头且暴露了 libtorrent 类型，
    # 消费者的 include 路径必须能解析 <libtorrent/...>。
    target_link_libraries(natdiag_lt
        PUBLIC natdiag LibtorrentRasterbar::torrent-rasterbar)
    target_include_directories(natdiag_lt PUBLIC include)
    target_compile_features(natdiag_lt PUBLIC cxx_std_17)
    target_compile_options(natdiag_lt PRIVATE $<$<CXX_COMPILER_ID:MSVC>:/W3 /utf-8>)
else()
    message(STATUS "natdiag: libtorrent 未找到，跳过适配层（核心库与测试仍可用）")
endif()

option(NATDIAG_BUILD_TESTS "Build natdiag unit tests" ON)
if (NATDIAG_BUILD_TESTS)
    enable_testing()
    add_executable(natdiag-tests
        tests/test_main.cpp
        tests/test_collector.cpp
        tests/test_diagnosis.cpp)
    target_link_libraries(natdiag-tests PRIVATE natdiag)
    target_compile_options(natdiag-tests PRIVATE $<$<CXX_COMPILER_ID:MSVC>:/W3 /utf-8>)
    add_test(NAME natdiag-tests COMMAND natdiag-tests)
endif()
