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

**落盘时机：加任务即写，之后每 30 秒写变化过的。** 既然 resume 文件是任务列表的唯一来源，那么「什么时候写」就等于「什么时候算存在」。原先只在退出、暂停、恢复时写，代价是加完任务直接被强杀（任务管理器结束、断电、蓝屏、崩溃）后，那些任务在重启时**整条不存在**，界面显示「还没有任务」，用户以为种子没加进去。实测过：加任务 → `resume/` 目录是空的 → `taskkill /F` → 重启 → 0 条。

现在两道保险：拿到 `handle` 的第一时间（`add_torrent_alert`）就写一份；运行期每 30 秒补一次，只写 `total_done` 变过的任务。30 秒是 libtorrent 侧的折中 —— `save_resume_data` 要序列化整份元数据，调太勤会吃 CPU 和 IO；而 30 秒的进度损失意味着重启后最多多算 30 秒的 hash。`--periodic-save-ms` 可覆盖，这不是调优旋钮，是为了让测试用 200ms 验证「到点会不会写」，而不是每个用例干等 30 秒。

## 实现期偏离（与批准版本的差异）

三处，都是实现时才暴露的，已就地改到上面：

1. **HTTP 层从 Boost.Asio 改为原生 Winsock。** 既然只用阻塞式收发，Asio 带来的只有链接配置的不确定性（Boost 1.86 的 header-only 模式下 `boost_system` 是否需要链接、`_WIN32_WINNT` 由谁定义）。原生 socket 只依赖 `ws2_32.lib`，行为完全可预测。代价是 M2 之后要跨平台得重写这一层，已记入未验证。
2. **去掉 index.json，改 resume 单源。** 原设计是「resume + index 双写」，实现时发现两道真理必然漂移：index 说 paused、resume 说没 paused 时以谁为准没有答案。改成只认 resume，条目发现改为扫描 `data/resume/` 目录。无元数据的磁力存不出 resume，另存 `.magnet` 兜底。
3. **新增 `POST /shutdown`。** 原 spec 没有退出接口，测试只能杀进程；而杀进程不会触发落盘，AC-08 就只能靠时序赌运气。加这个端点后优雅退出有了确定性入口，代价是多一个需要鉴权的破坏性操作。
4. **M2b 追加 CORS 支持（AC-13）。** 原设计假定「前端与引擎同源」，实际不是：壳的前端在
   `http://tauri.localhost`，vite 在 `http://localhost:5173`，引擎在 `http://127.0.0.1:<port>`。
   缺 CORS 头时浏览器在拿到响应前就丢弃，**引擎侧连一行日志都不会有**，现场表现是「窗口
   打开了但界面永远转圈」。桌面端全部单测跑在 jsdom 里，而 jsdom 不做同源检查 —— 这个缺陷
   直到装壳第一次真跑才暴露。允许任意 Origin 是刻意的：认证靠 Bearer 令牌，同源策略在这里
   不承担安全职责；防 DNS rebinding 靠 Host 检查。预检必须在认证之前放行，因为它按规范
   不带凭证，被 401 拦下同样等于放弃请求。

5. **M2c 追加路径编码支持（AC-14）。** Windows 上 `main(int, char**)` 拿到的是 ANSI
   （简中为 GBK）字节串。引擎内部用它是对的 —— `std::filesystem` 的窄字符串在 Windows 上
   同样按 ANSI 解读，目录能正常创建。错的是把它**原样写进 `engine.endpoint.json` 的
   `data_dir` 字段**：JSON 要求 UTF-8，壳按 UTF-8 读整文件直接失败，判定「端点不存在」
   后重新拉起，最终超时 20 秒，两侧日志都不报错。
   触发条件只是数据目录或安装路径含中文，而中文 Windows 的用户名目录很常见。
   **既有测试一条都没抓到**，因为数据目录全是 `tempfile.mkdtemp()` 的纯 ASCII 路径。
   修法是内部保持 ANSI、只在写 JSON 时转 UTF-8（`platform::native_to_utf8`）。
   本题不该由「安装包」单元发现 —— 它是引擎自身在真实路径下的正确性问题。

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
- [AC-13] 跨源请求可被浏览器放行：OPTIONS 预检返回 204 且带全套 CORS 头，正常与错误响应同样带跨源许可，方法白名单不放宽 | 判: `python engine/tests/t_cors.py` 退出码 0，12 项检查全通过
- [AC-14] 非 ASCII 数据目录下端点文件是合法 UTF-8，且 `data_dir` 字段逐字符等于真实路径 | 判: `python engine/tests/t_path_encoding.py` 退出码 0（纯中文 / 中文+空格 / 混合符号三种目录，各 8 项检查）
- [AC-15] `delete_files` 的真实语义：删文件时文件真的消失、不删时文件真的留着、删不掉时**响应里带 warning 说明及具体路径** | 判: `python engine/tests/t_delete_files.py` 退出码 0，25 项检查（三个本地种子各校验到 seeding → A `delete_files=1` 断言文件消失且无 warning → B `delete_files=0` 断言文件仍在 → C 用裸句柄占住文件后 `delete_files=1`，断言任务仍被移除、响应带 warning、warning 含「占用」与该文件名、文件确实还在、耗时 < 12s → D 松开句柄后同一路径可正常删除，证明路径没算错）
- [AC-16] 任务能活过「没走优雅退出」的死亡：加完任务立刻硬杀引擎，重启后任务仍在且状态一致；进度没变化时不重复写盘 | 判: `python engine/tests/t_persist_crash.py` 退出码 0，11 项检查（A 加种子 → 断言 resume 文件已经存在（不是等到退出才写）→ `taskkill /F` → 重启断言仍是 1 条且状态一致；B 加无元数据的磁力 → 断言落盘 → 硬杀重启后两条都在；C 把周期落盘缩到 200ms（`--periodic-save-ms`），等 1.5 秒断言 resume 文件 size 与 mtime 都没变）
- [AC-17] `/state` 的 `total_bytes` 在**已完成**的任务上必须等于真实大小，不能塌成 0；无元数据时必须如实给 0 | 判: `python engine/tests/t_total_bytes.py` 退出码 0，16 项检查（A 下载中态 `total_bytes` 等于 payload 大小且 `done_bytes=0` → B 把完整内容预置进 `save_path`、删任务重加让它自己校验到 seeding，断言 `total_bytes` 与 `done_bytes` 都等于 payload 大小且 `total_bytes >= done_bytes` → C 无元数据磁力 `has_metadata=false` 且 `total_bytes=0`）
- [AC-18] 两个引擎之间能**真传数据**：从零开始的客户端靠 peer 拿到完整 32 MiB 并落盘，内容逐字节一致；没有 peer 时一字节都拿不到 | 判: `python engine/tests/t_local_swarm.py` 退出码 0，26 项检查（进程内 HTTP tracker 促成会合，两端都带 `--no-network` 以隔离会合途径 → A 种子端预置完整内容校验到 seeding 且向 tracker 上报 → B 下载端在独立目录加同一份种子、断言两端 `save_path` 不同 → C 采样 17+ 个中间态、`done_bytes` 序列非递减、最终等于 32 MiB → D 下载端文件逐字节等于源且 mtime 落在本次测试期间 → E 关掉两端后同样的种子等满 8 秒仍是 0 字节）

## 不做范围

- 不做界面。M2 才有 Tauri + React。
- 不做文件优先级、限速策略、代理、WebSeed、RSS、做种规则。
- 不做 HTTPS tracker 与 SSL peer —— 当前 libtorrent 无 OpenSSL，实测后果见「环境依赖验证」。
- 不做多用户、远程访问、TLS。接口只在本机回环可用，这是设计前提而非缺陷。
- 不做种子创建的完整实现，测试用的 .torrent 由测试脚本用 Python 现场构造。
- 不做 SSE/WebSocket 推送，M1b 用轮询 + `seq` 变化检测。

## 环境依赖验证（不在 G3 卡点内）

与 AC 的区别：AC 必须确定性 —— 断网机器上也该得到同一结论。真实 swarm 依赖外网、
依赖种子仍有人做种，塞进 G3 会让卡点从「证据」退化成「抽奖」。所以单独成节，按需执行。

| 场景 | 种子 | 命令 | 结果 |
|---|---|---|---|
| 真实 swarm（HTTP tracker） | debian-13.7.0-netinst，infohash `7acf8fb5…`，tracker `http://bttracker.debian.org:6969` | `python engine/tests/real_swarm.py` | **PASS**：8 MiB 落盘，峰值 58 peer / 53 seed，1.07–1.21 MiB/s，约 20s |
| 反面对照（HTTPS tracker） | ubuntu-26.10-desktop，tracker `https://torrent.ubuntu.com` | `python engine/tests/real_swarm.py --torrent <该种子>` | 75s 内 0 peer / 0 字节 |

证据：`evidence/engine-service/NET-real-swarm.log`、`NET-https-tracker.log`。
两次独立运行均 PASS，说明不是单次侥幸。

脚本退出码语义 —— 关键是区分「引擎坏了」和「环境不通」，否则一次断网就会让人
误判成代码缺陷，反过来也会让真缺陷藏进「网络问题」里：

| 码 | 含义 | 判据 |
|---|---|---|
| 0 | 通过 | 真实进度达到阈值 |
| 1 | 引擎缺陷 | 连上 peer 但零字节落盘，或任务异常消失 |
| 2 | 未验证 | 全程零 peer，环境不可达，不算交付失败 |
| 4 | 缺 fixture | 找不到种子文件 |

区分依据：peer 连接由 libtorrent 负责，与本地构造种子时是同一套代码；而「把收到的
分片写进文件」走我们自己的 save_path 路径。所以「有 peer 无字节」能指向引擎，
「无 peer 无字节」只能指向环境。

## 未验证

- **（2026-11-05 已验，AC-18）两个引擎之间能真传数据。** 在此之前所有测试都是零 peer
  的：种子自己造、校验走本地磁盘、progress 要么 0 要么满，「收到别人的数据并写对」
  这件事没有任何判据碰过。现在用进程内 HTTP tracker 让两个实例会合，32 MiB 逐字节一致。
  补测时发现一个**会让人假通过的行为**：开着 LSD（默认开）时，同机的两个引擎会通过
  UDP 组播自行配对，根本不理 tracker 下发的地址 —— 把 peer 端口故意改错，它们照样
  下完。所以这条测试必须让两端带 `--no-network`（只关 DHT/LSD/UPnP，不关监听与传输），
  否则判据永远变不红。反过来看这也是一条产品事实：同一台机器上装两个客户端实例，
  即使 tracker 不可达，它们也能互相发现。
- **（2026-11-04 已修，AC-17）`total_bytes` 在已完成的任务上塌成 0。** 实测：`state=seeding`、
  `done_bytes=10240`、`progress=1.0`，而 `total_bytes=0`。现在改为 `torrent_status::total`
  为 0 时回落到 `torrent_info::total_size()`（后者不随任务状态变化）。
  **根因仍只是推断**：`st.total` 在这个 libtorrent 版本上对已完成任务返回 0，我推测是
  元数据被释放，但没有去读 libtorrent 源码确认。修法绕开了这个字段，所以推断对不对
  不影响结论；但它确实意味着**其他读 `st.total` 的地方可能有同样问题** —— 目前代码里
  只剩这一处。
  顺带记一条教训：**这个字段当初差点让周期落盘的判据假通过** —— 我拿 `st.total > 0`
  当门禁，而它恒为 0，于是落盘从来没执行，判据却「通过」了。判据通过不等于代码跑过。
- **HTTPS tracker 路径实测不可用，但归因未定。** 已实测：同网络下 HTTP tracker 的真实种子能正常下载，而 HTTPS tracker 的种子 75s 内零 peer、零字节。两个候选原因无法区分——SSL 缺失使 announce 根本发不出去，或该种子 DHT 未在 75s 内出结果。所以「HTTPS tracker 的种子下不动」是观测事实，「因为缺 OpenSSL」仍是推断，需补 OpenSSL 后复测才能定论。
- **SSL-only peer（强制加密连接）未覆盖。** 无 OpenSSL 时 MSE 仍可用（RC4，不依赖 TLS），但只接受 SSL 握手的 peer 连不上。
- **DHT 独立可用性未单独验证。** 上面那个零 peer 案例让「没有 tracker 时 DHT 能否独立找到 peer」变成关键未知，M1b 没做 DHT 单独开关的对照测试。AC-18 走的是本地 tracker 且**关掉了** DHT/LSD，所以这条仍然完全未测 —— 本地能传不等于公网能靠 DHT 找到人。
- **resume data 的免校验恢复未逐字节验证。** AC-08 只断言重启后状态到达 seeding，未断言「校验过程被完全跳过」——后者需要对比磁盘读量，当前没有测量手段。
- **崩溃隔离只做了进程级设计，未注入真实崩溃。** 没有主动触发 libtorrent abort 来验证引擎死亡后界面侧的表现，因为 M1b 尚无界面。
- **10 Hz 快照在高任务数下的 CPU 占用未测。** AC-09 只测响应延迟，不测定时器本身的开销。
- **跨平台未验证。** 仅 Windows + MSVC 19.44。HTTP 层直接依赖 Winsock，换平台要重写。
- **并发上限未压测。** 服务端是 4 个 worker 线程 + 64 条排队上限，这两个数按「单客户端 10Hz 轮询」估的，没有做过并发连接压测。超过上限的请求会被直接断开，行为正确但容量未知。
- **`/shutdown` 只有一个 Bearer 令牌作为边界。** 本机任意进程只要读到发现文件就能停引擎；这是「回环可用即受信」的假设，没有做进程身份校验。
