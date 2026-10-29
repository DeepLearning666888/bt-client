# engine-service（M1b）

## 目标

把 libtorrent 会话做成常驻独立进程，通过本地 HTTP/JSON 提供控制面与状态面，供桌面端（M2）连接。本单元只交付引擎与它的可测接口，不交付任何界面。

做这件事的理由：项目至今 1805 行代码全是诊断模块、构建脚本与流程规范，下载器本体为零。natdiag 已能独立构建并被链接，接入引擎的时机成熟。

## 关键设计决策

**独立进程，不是 Tauri 内嵌。** libtorrent 遇到畸形种子可能直接 abort，隔离后界面不会跟着挂；重启界面不掉任务；引擎可无头测试，`curl` 输出即可作为验收证据，正好套进 gate.py 的证据模型。

**HTTP 层手写，不引第三方库。** 直接用 Windows 原生 socket（`ws2_32`），连 Boost.Asio 也不用。原因是 vcpkg 被 `reg.exe` 黑名单堵死，装不了新依赖；而本接口只需支持 `GET/POST/DELETE` + `Content-Length` + `Connection: close`，是一个真·小子集，手写可控且无需信任外部代码。代价是不支持分块传输、keep-alive、TLS —— 全部明确拒绝而非部分支持。

**鉴权一律强制，`/health` 也不例外。** 本机任何进程都能访问回环端口，不设例外可以避免「留一个免鉴权端点」变成后续默认习惯。

**状态在定时器里算，请求时不算。** 10 Hz 定时器生成不可变快照（`shared_ptr<const Snapshot>`），HTTP 处理器只读指针。这样 `/state` 的延迟与任务数量无关，50 个任务和 1 个任务的响应时间一样。

**M1b 不引入 SQLite，也不引入索引文件。** 持久化只有一个来源：libtorrent 原生 `fastresume`。会话状态的唯一权威格式就是它；自己再存一份 index 必然与它不同步。无元数据的磁力存不出 resume，退化为一个 `.magnet` 兜底文件。SQLite 在 M2 承接设置与历史记录。

**端口与令牌写入发现文件。** 引擎监听 `127.0.0.1:0`（内核分配），启动后把 `{host, port, token, pid, version}` 写入 `data/engine.endpoint.json`。避免固定端口冲突，同时让前端无需启动参数即可发现引擎。

## 实现期偏离（与批准版本的差异）

三处，都是实现时才暴露的，已就地改到上面：

1. **HTTP 层从 Boost.Asio 改为原生 Winsock。** 既然只用阻塞式收发，Asio 带来的只有链接配置的不确定性（Boost 1.86 的 header-only 模式下 `boost_system` 是否需要链接、`_WIN32_WINNT` 由谁定义）。原生 socket 只依赖 `ws2_32.lib`，行为完全可预测。代价是 M2 之后要跨平台得重写这一层，已记入未验证。
2. **去掉 index.json，改 resume 单源。** 原设计是「resume + index 双写」，实现时发现两道真理必然漂移：index 说 paused、resume 说没 paused 时以谁为准没有答案。改成只认 resume，条目发现改为扫描 `data/resume/` 目录。无元数据的磁力存不出 resume，另存 `.magnet` 兜底。
3. **新增 `POST /shutdown`。** 原 spec 没有退出接口，测试只能杀进程；而杀进程不会触发落盘，AC-08 就只能靠时序赌运气。加这个端点后优雅退出有了确定性入口，代价是多一个需要鉴权的破坏性操作。

## 验收标准

- [AC-01] 引擎可独立构建产出 engine.exe | 判: `cmake --build engine/build` 退出码 0，`engine/build/engine.exe` 存在
- [AC-02] 同一数据目录只允许一个实例，第二个启动退出码 2 | 判: `python engine/tests/t_single_instance.py` 退出码 0
- [AC-03] 无令牌或错误令牌的请求一律 401 | 判: `python engine/tests/t_auth.py` 退出码 0，覆盖缺失/错误/空令牌三种
- [AC-04] 带令牌 GET /health 返回 200 且响应体为合法 JSON | 判: `python engine/tests/t_health.py` 退出码 0
- [AC-05] 添加磁力后该 infohash 出现在 /state，且含 state 与 progress 字段 | 判: `python engine/tests/t_add_magnet.py` 退出码 0（不依赖外网取元数据）
- [AC-06] 暂停使 state 变为 paused，恢复后离开 paused | 判: `python engine/tests/t_pause_resume.py` 退出码 0
- [AC-07] 删除后该 infohash 不再出现在 /state | 判: `python engine/tests/t_delete.py` 退出码 0
- [AC-08] 本地构造的种子完成校验后重启引擎，任务自动恢复为 seeding | 判: `python engine/tests/t_restart.py` 退出码 0（零外网依赖）
- [AC-09] 50 个任务时 GET /state 响应时间小于 50ms | 判: `python engine/tests/t_latency.py` 退出码 0，取 20 次采样中位数
- [AC-10] 引擎只监听回环，局域网地址连接被拒 | 判: `python engine/tests/t_bind_loopback.py` 退出码 0
- [AC-11] 畸形请求返回 4xx 且引擎不崩溃 | 判: `python engine/tests/t_malformed.py` 退出码 0，覆盖超长头/错误 Content-Length/分块传输/超大 body 四类，之后 /health 仍 200
- [AC-12] /state 内含 nat 字段且结构符合 natdiag 快照契约 | 判: `python engine/tests/t_nat_field.py` 退出码 0

## 不做范围

- 不做界面。M2 才有 Tauri + React。
- 不做文件优先级、限速策略、代理、WebSeed、RSS、做种规则。
- 不做 HTTPS tracker 与 SSL peer —— 当前 libtorrent 无 OpenSSL，见「未验证」。
- 不做多用户、远程访问、TLS。接口只在本机回环可用，这是设计前提而非缺陷。
- 不做种子创建的完整实现，测试用的 .torrent 由测试脚本用 Python 现场构造。
- 不做 SSE/WebSocket 推送，M1b 用轮询 + `seq` 变化检测。

## 未验证

- **无 OpenSSL，HTTPS tracker 与 SSL peer 不可用。** 影响真实种子的可下载范围，M1b 的测试全部走本地构造种子与合成磁力，绕开了这个问题，因此本单元无法证明「对真实公网种子可用」。
- **resume data 的免校验恢复未逐字节验证。** AC-08 只断言重启后状态到达 seeding，未断言「校验过程被完全跳过」——后者需要对比磁盘读量，当前没有测量手段。
- **崩溃隔离只做了进程级设计，未注入真实崩溃。** 没有主动触发 libtorrent abort 来验证引擎死亡后界面侧的表现，因为 M1b 尚无界面。
- **10 Hz 快照在高任务数下的 CPU 占用未测。** AC-09 只测响应延迟，不测定时器本身的开销。
- **跨平台未验证。** 仅 Windows + MSVC 19.44。HTTP 层直接依赖 Winsock，换平台要重写。
- **并发上限未压测。** 服务端是 4 个 worker 线程 + 64 条排队上限，这两个数按「单客户端 10Hz 轮询」估的，没有做过并发连接压测。超过上限的请求会被直接断开，行为正确但容量未知。
- **`/shutdown` 只有一个 Bearer 令牌作为边界。** 本机任意进程只要读到发现文件就能停引擎；这是「回环可用即受信」的假设，没有做进程身份校验。
