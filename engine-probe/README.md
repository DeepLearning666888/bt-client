# engine-probe

M1 的第一步验证工具。目的只有一个：**回答 M0 回答不了的那个问题——libtorrent 在这张网络上能否拿到入站端口。**

M0 用的是 aria2，而 aria2 1.37 的 Windows 构建根本没编译 UPnP（`miniupnpc` 符号命中 0 次）、且纯 TCP 无 uTP。所以「自动端口映射不可用」这个结论，严格说只对 aria2 成立。这张网络的真实上限，必须用最终产品的内核（libtorrent）自己跑一遍才算数。

**结论已经跑出来了**，见文末「实测结果」。当前状态：本机引擎可用，自动入站映射不可用。

> **诊断逻辑已抽成独立模块 `../natdiag/`。** 现在这个探针只剩三件事：起 session、
> 把 alert 喂给 `natdiag::Collector`、打印结果。判定规则、JSON 契约、状态机都在
> `natdiag` 里，M1b 的引擎服务会直接链接同一份代码，不再复制粘贴。
> 词表和判定优先级见 `../natdiag/README.md`。

---

## 前置依赖

| 组件 | 版本 | 位置 |
|---|---|---|
| MSVC | 14.44.35207 | `C:\BuildTools` |
| Windows SDK | 10.0.22621.0 | `C:\Program Files (x86)\Windows Kits\10` |
| CMake | 4.4.4 | `C:\vcpkg-tools\cmake\...\bin` |
| Ninja | 1.13.2 | `C:\vcpkg-tools\ninja` |
| Boost | 1.86.0（**仅头文件**） | `C:\boost\boost_1_86_0` |
| libtorrent | 2.0.11 | `C:\libtorrent\install` |

### 为什么不用 vcpkg

本机安全策略把 `reg.exe` 列进了程序黑名单，而 `vcvars64.bat` 靠它查 SDK 安装位置。结果是 vcpkg 拿到的构建环境残缺：

```
INCLUDE = 只有 MSVC\include + VC\Auxiliary\VS\include   ← 缺 ucrt/um/shared/winrt
LIB     = 只有 MSVC\lib\x64                            ← 缺 um\x64 / ucrt\x64
PATH    = 被整体覆盖，SDK bin、cmake、ninja 全不见
```

CMake 因此探不到 `rc.exe` / `mt.exe`（缓存里留下 `CMAKE_MT-NOTFOUND`），编译器测试链接失败，所有 port 编译不过。连带症状还有 `Could not fetch ninja`（其实 ninja 单独跑正常）和 `curl error 7` 连不上 github（vcpkg 读 IE 代理同样靠 reg.exe）。

**绕开方式：手工拼环境，不碰注册表。** 等价于 vcvars 的产出，见 `env.cmd`。

> `env.ps1` 保留作为文档，但本机 PowerShell 执行策略禁止运行 `.ps1`，实际用 `env.cmd`（`call env.cmd`）。

---

## 构建

### 1. Boost 头文件

只需头文件——libtorrent 2.x 要求 Boost ≥ 1.69，而 Boost.System 从 1.69 起已是 header-only，无需编译任何 Boost 库。

```bash
# 约 104 MB
curl -L -o boost_1_86_0.7z https://archives.boost.io/release/1.86.0/source/boost_1_86_0.7z
"C:\Program Files\7-Zip\7z.exe" x boost_1_86_0.7z -oC:\boost
```

### 2. libtorrent 源码

**必须用 GitHub Release 的 dist 包**。`codeload.github.com` 的 tag 压缩包不含 submodule，`deps/try_signal` 和 `deps/asio-gnutls` 会是空目录，CMake 报 `Cannot find source file: deps/try_signal/try_signal.cpp`。

```bash
curl -L -o libtorrent-rasterbar-2.0.11.tar.gz \
  https://github.com/arvidn/libtorrent/releases/download/v2.0.11/libtorrent-rasterbar-2.0.11.tar.gz
```

### 3. 编译 libtorrent

```bash
call engine-probe\env.cmd

cmake -S C:/libtorrent/libtorrent-rasterbar-2.0.11 -B C:/libtorrent/build -G Ninja ^
  -DCMAKE_BUILD_TYPE=Release -DBUILD_SHARED_LIBS=ON ^
  -Dbuild_tests=OFF -Dbuild_examples=OFF -Dbuild_tools=OFF -Ddeprecated-functions=OFF ^
  -DBOOST_ROOT=C:/boost/boost_1_86_0 -DBoost_NO_SYSTEM_PATHS=ON ^
  -DCMAKE_INSTALL_PREFIX=C:/libtorrent/install ^
  -DCMAKE_POLICY_VERSION_MINIMUM=3.5

cmake --build C:/libtorrent/build --target install
```

20 核约 4 分钟。产出 `torrent-rasterbar.dll` 3.8 MB + `torrent-rasterbar.lib` 1.1 MB。

`-DCMAKE_POLICY_VERSION_MINIMUM=3.5` 是给 CMake 4.x 准备的：4.x 移除了对 `cmake_minimum_required(VERSION <3.5)` 的兼容。

### 4. 编译探针

```bash
call engine-probe\env.cmd

cmake -S engine-probe -B engine-probe/build -G Ninja -DCMAKE_BUILD_TYPE=Release ^
  -DLibtorrentRasterbar_DIR=C:/libtorrent/install/lib/cmake/LibtorrentRasterbar
cmake --build engine-probe/build
```

`env.cmd` 已把 `C:\libtorrent\install\bin` 加进 PATH，运行时能找到 DLL。

---

## 运行

带 .torrent（首包快，适合验证协议链路 + 映射能力）：

```bash
./engine-probe/build/engine-probe.exe --torrent "path/to/some.torrent" --seconds 60
```

带磁力（真实用户体验，首包 22 s 级）：

```bash
./engine-probe/build/engine-probe.exe --magnet "magnet:?xt=urn:btih:..." --seconds 90
```

| 参数 | 默认 | 说明 |
|---|---|---|
| `--port N` | 6881 | 监听端口（TCP + uTP） |
| `--save-path DIR` | `./probe-download` | 落盘目录 |
| `--seconds N` | 60 | 观测时长 |

---

## 输出怎么读

```
=== diagnosis ===
listen        : 6 socket(s) bound
NAT-PMP/PCP TCP : no inbound mapping reply in 60s
NAT-PMP/PCP UDP : no inbound mapping reply in 60s
UPnP-IGD TCP    : no inbound mapping reply in 60s
UPnP-IGD UDP    : no inbound mapping reply in 60s
log lines     : NAT-PMP/PCP = 21
log lines     : UPnP-IGD = 62
external ip   : 203.0.113.7
metadata      : known (.torrent input)
progress      : 86.37%
peers         : 132 connected, 114 seeds
rates         : down 7.70 MB/s, up 0.37 MB/s

VERDICT: automatic inbound mapping UNAVAILABLE on this network
```

诊断段固定列出四种「传输 × 协议」组合，**没有回应的也要显示**，否则静默失败看着像没开。

### 一个必须注意的坑

日志里的 `==> port map [ transport: PCP ... external: 6881 ]` **不是成功回调，是发出的请求**。见 libtorrent 源码 `src/natpmp.cpp:535`——这行在 `send_map_request()` 内部、`m_socket.send_to()` 之前打印。看到它只能说明「请求发出去了」。

真正的成功信号只有 `portmap_alert`（程序里打 `[map ok ]`），失败信号是 `portmap_error_alert`（`[map err]`）。

### 四种最需要注意的告警

| 告警 | 含义 | 产品上要做什么 |
|---|---|---|
| `portmap_alert` | 映射成功，拿到外部端口 | UI 显示「已映射」，可以拼外部回连自检 |
| `portmap_error_alert` | 网关明确拒绝 | 「未获得公网端口」 |
| `portmap_log_alert` | 逐步细节（发现设备 / 取描述 / 发请求） | 诊断面板打开；发布版关掉，日志量很大 |
| `listen_failed_alert` | 端口被占用或被系统拦 | 换端口重试 → 仍失败则提示防火墙 |
| `external_ip_alert` | 拿到公网 IP | 与映射端口拼接做外部回连自检 |
| `metadata_received_alert` | 磁力元数据到手 | 驱动 UI 从「正在获取元数据」切到「下载中」 |

---

## 实测结果（2026-10-27，Debian 13.7 netinst 种子，792 MB / 3024 分片）

| 指标 | 45 s | 60 s |
|---|---|---|
| 监听套接字 | 6 | 6 |
| 连接 peer | 95（90 seed） | 132（114 seed） |
| 下载 | 7.11 MB/s | 7.70 MB/s |
| 上传 | 0.32 MB/s | 0.37 MB/s |
| 完成度 | 33.19% | 86.37% |
| 映射成功回调 | 0 | 0 |

四种组合全断，但**失败形态不同**，这个差别是产品价值所在：

| 组合 | 形态 |
|---|---|
| NAT-PMP / PCP | 网关已识别（`192.168.1.1`），请求发出，60 s 无回包 |
| UPnP-IGD | SSDP 发现 13 个设备，取描述文件返回 `403 Forbidden` |

手工验证设备身份：

```
GET http://192.168.50.1:37443/upnpap.xml
HTTP/1.1 403 Forbidden
SERVER: Linux/5.10.0, UPnP/1.0, Portable SDK for UPnP devices/1.14.12
```

设备跑的是标准 pupnp 协议栈，**具备** UPnP 能力但拒绝服务；具体授权原因未定位。

> 顺带修正 M0 的两条结论：①「路由器对 SSDP 不响应」是错判，SSDP 是通的，失败在 HTTP 取描述这一步；②「无入站则上传恒 0」不成立，本轮无任何映射仍有 0.37 MB/s 上传。

## 已知限制

- 当前构建**没有 OpenSSL**（编译期未找到，走降级分支）。后果： HTTPS tracker 不可用、SSL peer 连不上。上线前必须补。
- 只测单协议栈，不测 BEP-55 UDP 打洞（需双方都是 libtorrent 且能协调，单机无法构造）
- 不做 DHT bootstrap 完整性验证，只依赖默认节点
- 未处理 Windows 长路径（260 字符），真实种子若含深层中文目录可能创建失败——M2 要解决
- `sha1_hash` 没有十六进制 `to_string()`，直接打 `data()` 是二进制乱码，程序里自己转的十六进制

---

## 双二进制回归比对

抽 `natdiag` 时把旧实现整份留了下来（`src/main.legacy.cpp`），编成独立的
`engine-probe-legacy.exe`，和新实现同场比拼：

```bash
python regress.py        # 60 秒，跑两个二进制并逐项比对
python regress.py 20     # 快速跑
```

判据：`VERDICT` 行一字不差，四个 slot 的语义状态一致。`log_lines` 允许浮动
（实测 15~24 之间，随 libtorrent 重试时序变化）。

最近一次 60 秒结果：

```
=== VERDICT ===
legacy: VERDICT: automatic inbound mapping UNAVAILABLE on this network
new   : VERDICT: automatic inbound mapping UNAVAILABLE on this network
  -> PASS

=== SLOTS ===
  NAT-PMP/PCP TCP  legacy=no_reply    new=no_reply    PASS
  NAT-PMP/PCP UDP  legacy=no_reply    new=no_reply    PASS
  UPnP-IGD TCP     legacy=no_reply    new=no_reply    PASS
  UPnP-IGD UDP     legacy=no_reply    new=no_reply    PASS

RESULT: PASS — 新旧实现结论一致
```

结论一致后 `main.legacy.cpp` 就可以删了。

### 两个环境坑

- **不要用 shell 脚本包这个比对**。本环境的 bash 脚本里 fork Windows 子进程会直接
  返回 127，且没有任何错误输出（日志 0 字节），排查方向会被带到二进制本身去。
  Python 的 `subprocess` 走 CreateProcess，没这个问题。
- **不要把运行期 DLL 交给 PATH**。改用 CMake `POST_BUILD` 把
  `torrent-rasterbar.dll` 复制到 exe 同目录。脚本继承的 PATH 里混着
  `C:\Program Files\...` 这类条目，冒号会被当成路径分隔符切碎。
