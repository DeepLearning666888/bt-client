# 构建环境记录

重编 libtorrent 会覆盖 `C:/libtorrent/install`，且**无 SSL 的这套产物一旦被覆盖就拿不回来**。
本文件记录当前这套产物的全部构建参数，供重编时对照、出问题时回退。
所有数值取自各 `CMakeCache.txt` 与头文件，不是凭记忆填写。

## 工具链

| 组件 | 版本 | 路径 |
|---|---|---|
| MSVC 编译器 | 14.44.35207（自报 19.44.35229.0） | `C:/BuildTools/VC/Tools/MSVC/14.44.35207` |
| CMake | 4.4.4 | `C:/vcpkg-tools/cmake/cmake-4.4.4-windows-x86_64/bin/cmake.exe` |
| Ninja | 1.13.2 | `C:/vcpkg-tools/ninja/ninja.exe` |
| Boost | 1.86.0（`BOOST_VERSION 108600`） | `C:/boost/boost_1_86_0`，**header-only，无编译库** |

`cmake` 与 `ninja` **不在 PATH 里**，所有构建命令必须写绝对路径。

## vcpkg 不可用（已知且未解决）

根因：`reg.exe` 被安全策略列入黑名单 → `vcvars64.bat` 查不到 SDK 位置 → `INCLUDE` 缺 ucrt/um、`LIB` 缺 SDK、`PATH` 被整体覆盖 → 编译器测试链接失败。

因此**所有依赖均为手工获取**，不存在「装一下就好」的路径。若要恢复 vcpkg，唯一方式是让 `reg.exe` 出白名单，属系统级改动。

## libtorrent

| 项 | 值 |
|---|---|
| 版本 | 2.0.11 |
| 源码 | `C:/libtorrent/libtorrent-rasterbar-2.0.11` |
| 构建目录 | `C:/libtorrent/build` |
| 安装前缀 | `C:/libtorrent/install` |
| `BUILD_SHARED_LIBS` | ON |
| `CMAKE_BUILD_TYPE` | Release |
| `build_tests` | OFF |
| `deprecated-functions` | OFF |
| `dht` | ON |
| `encryption` | ON |
| `OPENSSL_INCLUDE_DIR` | **NOTFOUND → 无 SSL** |

产物指纹：

```
C:/libtorrent/install/bin/torrent-rasterbar.dll
  3,959,808 字节
  md5 26bc1219df958ab7c55f02562cdfa9e9
```

**源码包必须是 GitHub Release 的 dist 包。** 用 `codeload` 的 tag 压缩包会缺 submodule：`deps/asio-gnutls` 与 `deps/try_signal` 为空目录，编译中途才报错。dist 包这两个目录是有内容的。

## 下游构建

`natdiag` 与 `engine-probe` 均用 Ninja + Release，两个关键缓存项：

```
Boost_INCLUDE_DIR        = C:/boost/boost_1_86_0
LibtorrentRasterbar_DIR  = C:/libtorrent/install/lib/cmake/LibtorrentRasterbar
```

CMake 4.x 配置部分依赖时需要 `-DCMAKE_POLICY_VERSION_MINIMUM=3.5`，否则因旧 policy 被拒。

**不要手敲 cmake 命令**，统一走 `toolchain/build.py`（它自带上面这套 MSVC 环境，
因此在任何 shell 里都能跑，不要求先 source 任何脚本）：

```bash
python toolchain/build.py engine          # 需要时自动 configure
python toolchain/build.py engine --configure
python toolchain/build.py natdiag
python toolchain/build.py engine-probe
```

手敲 cmake 的后果是踩到 `CMAKE_MT-NOTFOUND` / `rc.exe 找不到`——本机 `vcvars64.bat`
因 `reg.exe` 黑名单产不出完整环境，这个坑已经踩过两次。

## 缺失的能力

| 缺失 | 后果 | 补的前提 |
|---|---|---|
| OpenSSL | HTTPS tracker、SSL peer 全部不可用 | 先装 Perl 与 NASM（当前**均未安装**），再源码编译 OpenSSL，再带 SSL 重编 libtorrent |
| 回退备份 | 重编覆盖后无法还原当前 DLL | 已复制到 `artifacts/libtorrent-nosssl/`（本目录被 git 忽略） |

## 行尾约定（不要改）

`natdiag/tests/golden/probe-run2-utf8.log` 是**纯 CRLF：114 个 CRLF、0 个纯 LF**。

因此本仓库**不做任何行尾归一化**，`core.autocrlf` 必须保持 `false`。
一旦按常规加上 `* text=auto`，全新 clone 会把这份基线转成 LF，而探针实际输出仍是 CRLF——L2 golden 回归会静默失配，看起来在跑，结论已错。

`.gitattributes` 只标二进制类型，不碰文本。
