# installer — M2c 安装包

## 目标

M2b 交付了可双击运行的 `btclient-shell.exe`，但它是**裸 exe**：没有开始菜单项、
没有卸载入口、没有版本信息，用户得自己决定放哪、自己记得怎么删。

本轮要解决的是「分发形态」。但真正要验证的不是「装上了」，而是**装到别的位置还能跑**。

## 为什么这件事不能靠「双击 exe 能跑」推断

M2b 的 AC-06 只证明了 `target/release/btclient-shell.exe` 能启动。那个目录里
`engine.exe` 与 `torrent-rasterbar.dll` 恰好也躺在旁边 —— 因为编译时 Tauri 把 resource
复制过去了。这掩盖了一个真实风险：

- 开发态 `locate_engine()` 有回退分支 `CARGO_MANIFEST_DIR/../../engine/build/`，**编译它的
  机器上这个路径一定存在**，所以回退分支永远不会暴露问题。
- 装到 `Program Files` 后 `CARGO_MANIFEST_DIR` 指向的源码路径不存在，只能靠
  `BaseDirectory::Resource`。这条分支到今天为止**从未在打包形态下被执行过**。

这正是 M2b 里 `0xc0000135` 那个缺陷的同一类：DLL 与 exe 是否还在一起、路径是否还成立。
所以本轮的核心 AC 必须是「装到临时目录 → 从安装目录启动 → 前端拿到数据」，
不能用任何开发态捷径。

## 关键设计决策

**打包用 `cargo tauri build`，不用手工拼 NSIS 脚本。** Tauri 的 NSIS 模板已经处理了
WebView2 引导、快捷方式、卸载注册表项。手写一遍这几件事的价值是负的 —— 会引入自己的 bug，
而且要跟着 Tauri 版本维护。

**装包验证走静默安装，不点下一步。** NSIS 支持 `/S` 静默安装，`/D=<path>` 指定目录
（必须是最后一个参数且不加引号）。这样整个安装-启动-卸载流程能进自动化，否则只能靠人工。

**安装目录用临时目录，不用 `Program Files`。** 两个理由：`Program Files` 需要提权，
自动化测试里会弹 UAC；而临时目录能验证到同样的事情（resource 解析、DLL 同层、数据目录
隔离）。`Program Files` 特有的风险是「路径含空格」—— 这一条靠临时目录名带一个空格来覆盖。

**卸载验证「清干净」而不是「能删掉」。** 真正的卸载缺陷是残留：装完之后用户数据目录
（`%APPDATA%\com.btclient.desktop`）应保留，而程序目录应清空。两者混在一起就说明
卸载脚本把用户的下载记录一起删了。

**测试用独立的数据目录，不污染真实用户数据。** 壳已支持 `BTCLIENT_DATA_DIR` 覆盖
（M2b 加的），安装态测试沿用它，避免测试装上就把用户的真实任务列表挤掉。

## 验收标准

- [AC-01] 打包命令产出 NSIS 安装包，体积在 3–40 MB | 判: `python desktop/tests/t_installer_build.py` 退出码 0（检查产物路径、后缀、体积下限，体积下限防「只打了壳没带引擎」，上限防「误把 node_modules / target 打进包」）
- [AC-02] 静默安装到含空格的临时目录后，程序目录结构正确 | 判: `python desktop/tests/t_installer_install.py` 退出码 0（断言：主 exe 存在、`engine.exe` 存在、`torrent-rasterbar.dll` 存在，且后两者与主 exe 同层）
- [AC-03] 从安装目录启动，前端拿到真实数据 | 判: 同上脚本（断言：`shell.log` 出现 `frontend_ready` 且 seq 非空；这是「装完能跑」的唯一直接证据）
- [AC-04] 安装目录启动时走的是 resource 路径，不是开发态回退 | 判: 同上脚本（断言 `shell.log` 里 `engine_exe=` 的值位于安装目录内，且不含 `engine/build`）
- [AC-05] 卸载后程序目录被清理，用户数据目录保留 | 判: `python desktop/tests/t_installer_uninstall.py` 退出码 0
- [AC-06] 安装包不携带开发产物 | 判: `python desktop/tests/t_installer_install.py` 退出码 0（安装后程序目录无子目录、文件集恰为 4 个预期文件、总体积 < 25 MB）

## 不做范围

- **不做代码签名。** 没有证书。未签名安装包会触发 SmartScreen 警告，这是分发形态问题
  而非功能缺陷，本轮只记录现象。
- **不做自动更新。** 需要托管服务，且要先有签名。
- **不做 MSI。** NSIS 已够用；MSI 的优势是组策略部署，本项目没有企业场景。
- **不做 WebView2 固定版运行时捆绑。** 当前依赖系统运行时（本机 156 已装）。
  捆固定版会多 100 MB，等确认目标机器可能缺失时再议。
- **不做 `Program Files` 安装验证。** 需要提权，无法自动化；风险已用「路径含空格」
  覆盖主要部分。
- **不做卸载后的注册表残留检查。** NSIS 的注册表清理由模板负责，且在临时目录装一次
  再查 HKLM 会污染真实注册表。

## 实现期偏离（与批准版本的差异）

1. **AC-06 的判据从「解压安装包列条目」改成「安装后目录清单」。** NSIS 产物是自解压
   exe，不是 zip，没有第三方工具无法直接列条目；而安装后的清单是更强的判据 ——
   它反映的是真实落地结果，不是包内声明。判据因此移到 `t_installer_install.py`。

2. **AC-01 的体积下限从 3 MB 调到 2.5 MB。** 按 13.2 MB 原始体积估的压缩率偏高，
   实测 LZMA 压到 2.99 MB。阈值贴着实测值定会变成「一改代码就误报」，
   所以留出余量，同时保证仍能卡住「漏了 DLL」（少 3.78 MB 原始、约 1.4 MB 压缩）。

3. **新增 `toolchain/fetch_nsis.py`。** 没预料到的问题：本机出口代理对 github.com
   返回 502，直连也不通，而 `tauri build` 打 NSIS 必须从 github 下载
   `nsis-3.13.zip` 与 `nsis_tauri_utils.dll`。crates.io 与 npm registry 都可达，
   唯独 github 不行。脚本走镜像下载，并对每个文件核对硬编码 SHA1 ——
   这些 SHA1 逐字取自 tauri-bundler 源码的 `NSIS_SHA1` / `NSIS_EXE_SHA1` /
   `NSIS_TAURI_UTILS_SHA1` 常量，哈希一致即等价于拿到官方文件，镜像不需要被信任。

4. **修了引擎的一个编码缺陷（原属 engine-service）。** 安装后第一次启动时引擎起不来，
   表现为壳等 20 秒报超时。根因：Windows 上 `main(int, char**)` 拿到的是 ANSI（简中为
   GBK）编码参数，引擎把这些字节原样写进 `engine.endpoint.json` 的 `data_dir` 字段，
   而 JSON 要求 UTF-8 —— 壳按 UTF-8 读，整文件解析失败，判定「未就绪」。
   触发条件只是数据目录或安装路径含中文，而中文 Windows 的用户名目录很常见。
   **这个缺陷在开发态永远不会出现**：`%APPDATA%\com.btclient.desktop` 是纯 ASCII，
   而所有既有测试用的都是 ASCII 临时目录。修法是引擎内部保持 ANSI（文件操作才正确）、
   写 JSON 时转 UTF-8（`platform::native_to_utf8`）。

5. **壳侧加了两道防线，不只是修引擎。** `read_endpoint` 不再要求整个文件是合法
   UTF-8（端口与令牌都是 ASCII，一个字段编码坏了不该拖垮整个应用）；`spawn` 超时时
   把端点文件的真实状态写进错误信息。原来只有一句「超时」，而它覆盖了三种完全不同的
   原因：文件没生成（引擎起不来）、生成了但解析不出（编码/字段）、解析出来但连不上
   （端口没监听），排查方向完全不同。

6. **测试路径故意带空格与中文。** 这两样是真实用户环境里最常见、开发机上最少见的。
   证明它们确实有用：中文那次直接挖出了上面第 4 条的编码缺陷。

7. **默认安装位置实测为 `%LOCALAPPDATA%\BT 客户端`**（currentUser 模式，不需要提权，
   双击不会弹 UAC）。这个结论是实测的，不是从文档推的 —— 因为一开始注册表有残留导致
   装回了旧路径，清干净后重测才得到真实默认值。

## 未验证

- **`Program Files` 下安装。** 需要提权，自动化测试里会弹 UAC 卡住。风险已用「路径含
  空格与中文」覆盖主要部分（resource 解析、DLL 同层、编码），但 `Program Files` 特有的
  「普通用户对安装目录无写权限」没有验证 —— 如果将来改成 machine 安装模式，日志写到
  安装目录会失败，这条要重新评估。
- **开始菜单快捷方式与桌面图标。** 装到自定义 `/D` 目录时未观察到创建；默认位置装了两次
  也只在卸载后检查过（已清除）。安装界面（非静默）会不会创建，没有验证。
- **代码签名与 SmartScreen。** 无证书，未签名。用户首次运行会看到 SmartScreen 警告，
  这是分发形态问题而非功能缺陷，本轮只记录现象。
- **覆盖安装 / 版本升级。** 未验证「旧版在装 → 装新版」的路径。已知 NSIS 的
  `InstallDirRegKey` 会让重装沿用上次位置（`HKCU\Software\btclient\<产品名>`），
  卸载时这一项**不会**被清除 —— 属于 NSIS 模板行为，影响是卸载后重装装回原路径。
- **卸载后的注册表彻底性。** 只验证了卸载项（`...\CurrentVersion\Uninstall\BT 客户端`）
  与程序目录被清除，未逐一核对 NSIS 写入的所有键。测试过程本身会在
  `HKCU\Software\btclient` 下留 `InstallDir` 记录，脚本未自动清理这一项
  （本轮是手工清的）。
- **`__TAURI_BUNDLE_TYPE` 警告。** 打包时提示 `tauri` crate（2.12.5）与 CLI（2.12.4）
  版本不一致，导致该变量未写入二进制。只影响 updater 插件，本项目未使用。未升级对齐。
