# tauri-shell — M2b 桌面壳

## 目标

把 M2a 的前端从浏览器搬进一个真正的 Windows 应用：双击图标就开，不需要用户先开命令行
拉起引擎、再把 URL 和令牌复制进终端。

M2a 交付的前端能跑，但启动方式是「先用 `launcher.py` 打印两个环境变量，再启动 vite」。
这个流程开发时能用，给用户用不了 —— 它把「引擎在哪、令牌是什么」变成人的记忆负担。
壳要接管的就是这两件事：**发现引擎、拉起引擎、把端点递给前端**。

## 关键设计决策

**三情形判断照抄 `launcher.py`，不重新设计。** 那套逻辑已有 `t_desktop_discovery.py`
的 10 项断言覆盖，是验证过的：无 endpoint 文件 → 拉起；有文件且 `/health` 200 → 复用；
有文件但 `/health` 不通 → 清残留再拉起。第三种是最容易写错的一种 —— 只读文件不查健康，
就会连到一个已经不存在的端口，表现为「应用打开了但永远转圈」。

**端点用注入而不是 IPC 查询。** 窗口创建前引擎已经就绪，把 `{baseUrl, token}` 写进
`window.__ENGINE__` 初始化脚本。前端只改 `config.ts` 一处读取顺序（先注入、后构建期环境
变量），不引入 `@tauri-apps/api`，也不需要把 `main.tsx` 改成异步初始化。
代价是端点不可在运行期变更 —— 换引擎端口要重启窗口，这是可接受的。

**HTTP 客户端手写，不引 reqwest。** 判断健康只需要「TCP 连得上 + 首行是 200」。
引擎侧的 HTTP 也是手写 Winsock，两侧保持同一量级，不为一句话的需求拉进 tokio 全家桶。

**单实例用命名 Mutex，不用插件。** `CreateMutexW("Local\\<app>-shell")`，已存在就把焦点
还给已有窗口之后退出。第二次启动绝不能 spawn 第二个引擎 —— 引擎自己有 `InstanceLock`，
第二个会以退出码 2 死掉，但用户的观感是「点了图标没反应」。

**退出时 `POST /shutdown`，不 taskkill。** 引擎要靠优雅退出写 resume 数据；直接杀进程会
丢任务列表（这个坑 M1b 已经用 AC-08 验证过）。复用已有实例时则**不关机** —— 那个引擎
不是壳拉起的，关它等于替用户决定。

**日志写到文件，不当证据靠猜。** 壳把「决策 + 端点 + 前端就绪」写进 `<data-dir>/shell.log`。
前端拿到首帧数据后回报一次 `frontend_ready`。这条日志是端到端连通的**唯一直接证据**：
它证明的不是「窗口开了」，而是「React 树渲染了、`/state` 请求成功了、数据到了」。

**引擎作为 resource 打包，不用 externalBin。** `externalBin` 会把文件名改成
`engine-x86_64-pc-windows-msvc.exe`，路径要靠 `sidecar()` 反查；resource 保留原名，
路径 `BaseDirectory::Resource` 直接可得，调试时也能用资源管理器看见它。

## 验收标准

- [AC-01] 三情形解析与端点文件降级正确 | 判: `cargo test --manifest-path desktop/src-tauri/Cargo.toml` 退出码 0，覆盖无文件/健康复用/不健康重建/文件损坏/缺字段
- [AC-02] 真引擎可被拉起并在超时内就绪，退出走 `/shutdown` | 判: `cargo test --manifest-path desktop/src-tauri/Cargo.toml --test live_engine` 退出码 0（需要 `engine/build/engine.exe`）
- [AC-03] 前端优先读注入端点，注入缺失时回退构建期环境变量 | 判: `python toolchain/build.py desktop --test config` 退出码 0
- [AC-04] 壳端到端连通：窗口启动后日志出现 `frontend_ready` 且带非空 seq | 判: `python desktop/tests/t_shell_e2e.py` 退出码 0
- [AC-05] 第二个实例不再拉起第二个引擎 | 判: `python desktop/tests/t_shell_single.py` 退出码 0（两个壳进程 + 恰好一个 `engine.exe`）
- [AC-06] release 产物可双击运行且内嵌前端资源 | 判: `cargo build --release --manifest-path desktop/src-tauri/Cargo.toml` 退出码 0，且 `target/release/btclient-shell.exe` 存在且 > 3 MB（小于此值说明前端资源没打进二进制）

## 不做范围

- **不做安装包（msi/nsis）。** 首次构建要额外下载 NSIS 与 WiX，且签名、升级策略都没定。
  本轮交付的是可双击运行的 release exe；安装包等发布流程确定后再做。
- **不做自动更新、不做托盘、不做开机自启。**
- **不做前端 UI 层的新功能。** 壳只管进程与端点，界面沿用 M2a 的产物。
- **不做 WebView2 版本自举。** 目标机若真缺运行时，Tauri 会把用户引导到微软下载页，
  这是可接受的降级；把固定版运行时（+100 MB）打进包里留到发布形态确定后再说。
- **不做渲染差异验证。** WebView2 与 Chrome 的合成路径不同，需要在真机上跑
  `crossmachine` 那套探针；那属于 P1 的跨机部分，不在本轮。

## 实现期偏离（与批准版本的差异）

1. **引擎必须一起打 `torrent-rasterbar.dll`。** 原设计只声明了 `engine.exe` 作为 resource。
   结果壳一启动就 `0xc0000135`（STATUS_DLL_NOT_FOUND）—— 引擎是动态链接 libtorrent 的，
   `tauri-build` 只复制了 exe，DLL 没跟过去。这个失败在编译期完全看不见，只在运行时炸。
2. **单实例锁按数据目录加哈希，不是固定名字。** 原设计用 `Local\btclient-shell-single`。
   用不同数据目录的两个壳本来就该能并存（连不同引擎、不同端口，互不干扰），
   固定名字会把这个正常需求也堵死。用 `DefaultHasher`（种子固定，跨进程一致）对
   小写化的路径取哈希。同一个数据目录才是真冲突：会抢同一个 `engine.endpoint.json`。
3. **测试跑 release 而不是 debug。** `cargo test` 默认 debug，会把 405 个依赖按 debug
   再编一遍（约 4 分钟）。壳的测试没有「需要 debug 断言」的部分，用 `--release` 复用
   已有产物，增量重编 1.6 秒。
4. **新增 `BTCLIENT_DATA_DIR` 覆盖。** 否则测试要么污染 `%APPDATA%` 下的真实数据，
   要么没法验证「全新安装的首次启动」这条路径。
5. **`Resolved` 不能 derive Debug。** 它持有 `std::process::Child`，后者没有 Debug 实现，
   导致测试里的 `expect_err` 编译不过。改用 `match`。
6. **引擎优雅退出时会删掉端点文件** —— 这是测试跑出来的事实，不是设计文档里的假设，
   所以 Respawn 分支的真实触发条件比原以为的窄：只有**异常退出**（崩溃、被强杀）
   才会留下「文件在、进程死」的残留。测试因此改为手写旧端点来构造该状态，
   比 kill 进程更可控。
7. **引擎加 CORS（AC-13，改在 engine-service 单元）。** 详见该 spec 的实现期偏离第 4 条。
   这里只记一句教训：**jsdom 不做同源检查**，所以桌面端「全部通过」的单测
   对真实浏览器里能否连通没有任何保证。

## 未验证

- **（已过期，2026-11-03 更正）安装包已在 M2c 完成**：NSIS 单文件安装器，中文路径、
  带空格路径、静默卸载都验过（见 `specs/installer.md`）。仍然没验的是
  **装到 `Program Files` 的模式** —— 那要提权、普通用户对安装目录无写权限，
  resource 目录的解析路径会跟着变。「双击 exe 能跑」与「装在受保护目录能跑」不是同一件事。
- **窗口必须关掉 Tauri 内部的拖放处理器，否则前端 HTML5 拖放收不到事件。** 这是
  M2g 加拖拽功能时发现的：tauri 2.12.5 在 Windows 上替换 WebView2 的拖放处理器来生成
  自己的 `DragDropEvent`，源码注释写明要用 HTML5 拖放就得
  `disable_drag_drop_handler()`。已在 `lib.rs` 关掉。**这条没有自动化判据** ——
  它是「删掉就静默失效」的护栏，只能靠真机拖一次才发现。写在这里是为了让下一个改
  窗口构建参数的人知道它不能碰。
- **WebView2 缺失时的降级行为未测。** 本机有 156.0.4314.50，无法制造「未安装」状态。
  Tauri 理论上会把用户引导到微软下载页，实际表现未知。
- **首次启动耗时未测。** `SPAWN_TIMEOUT` 是 20s 上限，实测复用路径 <1s、全新 spawn
  约 1–2s，但没有系统测量冷启动（磁盘缓存空、WebView2 首次初始化）。
- **壳崩溃时的引擎残留未验证。** 设计上残留可被下次启动复用（`Reuse` 分支），
  但没测过「壳被强杀后重启能否正确接管」。用 Job Object 绑定子进程是备选方案。
- **高 DPI / 多显示器未验证。** 窗口尺寸固定 1100×720，150% 缩放下的实际观感没看。
- **前端在 WebView2 下的渲染与 Chrome 是否有差异未验证。** 这正是 P1 探针要回答的问题，
  但那个包现在跑不了 WebView2（它是给外部机器准备的 Chrome/Edge 测试页）。
