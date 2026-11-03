# desktop — M2 桌面端

## 目标

给 M1b 的引擎配一个真正能用的桌面界面：Tauri 2 壳 + React/TS 前端，通过回环 HTTP 接
`engine.exe` 的控制面与状态面。这是本项目第一次有用户可见的产物 —— 此前全部是引擎、
诊断模块与流程规范。

为什么现在做：引擎接口只有测试在用。**UI 是暴露接口设计缺陷的唯一手段** —— 等真去渲染
文件树、画速率曲线，才会发现「缺文件列表接口」「状态字段不够用」这类问题。先补 OpenSSL
再上 UI，接口要返工两次。

## 关键设计决策

**UI 负责拉起引擎，而不是要求用户先开命令行。** Tauri sidecar 打包 `engine.exe`，应用启动
时读 `<data-dir>/engine.endpoint.json`：文件存在且 `/health` 通过则直接复用已有实例；否则
spawn 引擎并等发现文件出现。应用退出时调 `POST /shutdown` 让它落盘。

**复用判定必须查 `/health` 而不只看文件。** 上次异常退出会留下过期的 endpoint 文件，
只读文件就会连到一个已经不存在的端口。

**10Hz 轮询配合 `since=seq` 去重。** `/state?since=N` 在 seq 未变时返回
`{"seq":N,"unchanged":true}`，此时不产生新对象、不触发渲染。**这是「界面不卡」的地基** ——
每秒 10 次全量 setState 会让整棵树重渲，任务一多就掉帧。

**状态放在模块级外部 store，不用 Context。** Context 值一变，所有消费者重渲；外部 store 让
每个组件只订阅自己关心的切片。代价是不能用 `useContext` 的天然嵌套，需手动传 store。

**列表虚拟化，固定行高。** 1000 个任务的 DOM 节点数必须与视口相关、与任务总数无关。
固定行高换掉「测量每行高度」的复杂度。

**动效只碰 `transform` 与 `opacity`。** 这条是 `docs-motion/` 已经验证过的结论，直接沿用：
`width`/`top` 触发逐帧重排，`backdrop-filter` 在滚动时必然掉帧，两者在 BT 客户端
（高频速率刷新 + 长列表）里都是致命的。

**类型严格模式，不用 `any` 逃生。** 引擎返回的 JSON 是不可信输入，用 zod 之类的运行时校验
会引依赖；改为手写 `parseSnapshot()` 做结构校验，字段缺失/类型不对时降级为「未知」而不是
让 undefined 渗进渲染层。

**所有前端命令走 `toolchain/build.py desktop`，不在配置里直接写 `npm`。** 两个硬约束：
`gate.py` 用 `shell=False` 执行配置里的命令，`cd desktop && npm run x` 这种 shell 片段根本
不成立；且 node 不在系统 PATH 里（实际在 `.workbuddy/binaries/node/versions/<ver>/`），
换机器就找不到。包装器负责定位 `node.exe` 与 `npm-cli.js`（绕开 `npm.cmd` 对 cmd.exe 的
隐含依赖），把路径不确定性收敛到一处 —— 与 engine 自带 MSVC 环境是同一套做法。

**添加任务走引擎已有的 `POST /torrents`，界面只补入口。** 引擎从 M1b 起就支持
「磁力 JSON」与「.torrent 二进制」两种 body，但界面上一直没有入口 —— 结果是软件装完
打开就是空列表，用户无处可点。这一轮只补磁力那条（最常用），`.torrent` 文件选择与拖拽
留给下一轮：两者都是「把字节交给引擎」，但前者只涉及输入校验，后者还要接文件对话框和
拖拽事件，混在一起做会让这一轮的判据不干净。

**输入校验、请求构造、错误文案三层分开。** 校验和文案是纯函数（`add-task.ts`），能直接
单测边界；请求层只管把合法输入按契约发出去；对话框只负责显示错误和关闭自己。代价是
多一个文件，收益是「复制时少一位哈希」这类问题能用 10 行单测锁死，不必靠点界面。

**引擎侧顺手修了一个错误分类缺陷。** `parse_magnet_uri` 失败与「缺 btih」原来没有
`bad_input:` 前缀，被 `map_error` 兜底成 500，界面只能显示「引擎处理失败」——
把用户输错链接说成服务端故障。改成 400 后，界面能直接透传「磁力链接解析失败：…」。

**拖拽不新建添加通道，只在最外层把内容导向已有两条路。** 拖入 `.torrent` 走
`prepareTorrentFile()` → `addTorrentFile()`，拖入 `magnet:` 文本走 `normalizeMagnet()` →
`addTorrent()`，与对话框调用的是同一批函数。这样拖拽只承担一个新增风险：**该不该接**，
而不承担「怎么发请求」—— 后者已被 AC-12/AC-15 锁过。

三处刻意的取舍：

- **多文件直接拒绝，不静默取第一个。** 一次加多个要引擎批量接口 + 部分失败处理，
  超出本轮；而静默只加第一个会让用户以为都加进去了 —— 这比拒绝更坏。
- **无关文本静默忽略，只有 `magnet:` 开头才报错。** 拖拽是顺手动作，不是明确提交。
  随手拖一段选中的文字进来就弹红字，属于把系统的困惑转嫁给用户。
- **遮罩只按 `dataTransfer.types` 判定。** dragover 阶段浏览器不允许读出实际内容
  （安全限制），所以遮罩能知道的只有「拖的是文件还是文本」。要按真实内容亮遮罩，
  只能等 drop —— 那时遮罩已经没用了。

**删除要确认，暂停不要。** 判据是「能不能撤销」：暂停随时能点回来，加一层确认只是
让常用动作变慢；删除把任务从列表里拿掉（还能选择连磁盘文件一起删），撤不回来。
所以暂停一下都不问，删除必须点两下。

**删除时问「文件要不要一起删」，而不是默认一个答案。** 两个默认都错：默认删文件，
用户不小心点了就丢数据；默认不删文件，用户以为任务没了文件也没了，磁盘被悄悄占着。
把选择摆到眼前、默认停在「只删任务」这个更保守的选项上，代价是一次点击，收益是
两种误操作都不会静默发生。

**infohash 拼进 URL 之前必须校验是 40 位 hex。** 它来自引擎响应而不是用户输入，
看起来可信，但这个值要拼进路径。不校验的话，一个 `../shutdown` 形状的字符串会让
前端替任何人发一次删除请求 —— 校验一行，把「上游被污染」的爆炸半径收窄到一个 400。

**操作成功后立刻 `poller.tick()`，不等下一个 100ms 周期。** 与添加任务同一个理由：
点完 100ms 内界面没反应，用户会再点一次，第二次就撞上「已经在列表里」这类错误。

**`.torrent` 文件与磁力是两条独立的入口，不是一个入口的两种输入。** 两者的请求体
根本不同（JSON `{"magnet":"…"}` vs `application/octet-stream` 原始字节），校验规则
也完全不同（磁力查 `btih` 语法，文件查大小上限与 bencode 首字节）。硬塞进一个
`if` 里会让 ACL 变成「这条判据到底在验哪个」说不清，所以拆成 `addTorrent()` 与
`addTorrentFile()` 两个函数、两条 AC。共用的是错误映射（`describeAddFailure`），
因为引擎的状态码语义对两者一致。

**发送前拦截，而不是让引擎回 413。** 引擎的 HTTP 层上限是 8 MiB（`http.cpp:321`），
超了直接 413 且不会进入 `add_torrent_bytes`。前端在 `File` 对象上就能读到 `size`，
先拦下来给「这个种子文件 9.2 MiB，超过 8 MiB 上限」这种带数值的文案，比让用户
看着「请求体超过 8 MiB」猜自己哪个文件超了要有用。上限常量与引擎对齐并写进注释，
两边改动时必须同步。

**文件模式复用同一个对话框，用 Tab 切换。** 不新开窗口的理由：用户的心智模型是
「添加任务」，不是「添加磁力」和「添加文件」两件事；两个入口意味着两次点击。
拖拽仍然不做 —— 它要窗口级 drop 遮罩、多文件取舍、拖入非种子文件的反馈，是独立
的交互层，混进来判据不干净。这一轮只做「点按钮 → 选文件」。

## 验收标准

- [AC-01] 依赖可从锁文件复现安装 | 判: `python toolchain/build.py desktop --install` 退出码 0，且 `desktop/package-lock.json` 存在
- [AC-02] TypeScript 严格模式零错误 | 判: `python toolchain/build.py desktop --check` 退出码 0
- [AC-03] 引擎 JSON → UI 模型的结构校验覆盖边界 | 判: `python toolchain/build.py desktop --test model` 退出码 0，覆盖空列表/字段缺失/类型错误/无元数据四类
- [AC-04] seq 未变时不产生新快照对象、不触发渲染 | 判: `python toolchain/build.py desktop --test poller` 退出码 0，断言 unchanged 响应下监听器调用次数为 0
- [AC-05] 1000 个任务时 DOM 行数不随总数增长 | 判: `python toolchain/build.py desktop --test virtual` 退出码 0，断言渲染行数 ≤ 40
- [AC-06] 真实引擎对接：起 `engine.exe` 后 UI 数据层能拉到并渲染任务名 | 判: `python desktop/tests/t_live_engine.py` 退出码 0（本机回环，零外网依赖）
- [AC-07] 生产构建产出静态资源 | 判: `python toolchain/build.py desktop --build` 退出码 0，且 `desktop/dist/index.html` 存在
- [AC-08] 引擎发现逻辑：无实例时拉起、已有实例时复用、残留过期端点时不误连 | 判: `python desktop/tests/t_engine_discovery.py` 退出码 0，三种情形各断言一次
- [AC-09] 磁力输入校验与错误文案是纯函数，边界可单测 | 判: `python toolchain/build.py desktop --test add-task.test` 退出码 0，覆盖空串/纯空白、缺 `magnet:?` 前缀、`http(s)` 下载地址、缺 btih、btih 长度不对、32 位 base32、40 位 hex、大小写混合八类输入，并覆盖 400/401/409/5xx/未知状态码五类错误文案
- [AC-10] 添加请求的契约固定：`POST <base>/torrents` + Bearer 令牌 + JSON body | 判: 同上命令，用 stub fetch 断言 URL、method、`Authorization: Bearer <token>`、`Content-Type: application/json`、body 为 `{"magnet":"…"}`；非 2xx 抛带中文文案的 Error、缺 infohash 抛错、连不上引擎与超时各有独立文案
- [AC-11] 对话框交互：非法输入不发起请求，成功即关闭，失败留窗并显示原因 | 判: `python toolchain/build.py desktop --test add-task-ui` 退出码 0，断言四种情形各一次
- [AC-12] 真实引擎端到端：界面的添加动作能让任务出现在列表里 | 判: `python desktop/tests/t_add_task_live.py` 退出码 0（起真引擎 → 前端 `addTorrent()` 发磁力 → 断言返回 infohash 与输入一致 → 轮询 `/state` → 渲染 `App` 断言该任务出现在列表且状态为「获取元数据中」→ 重复添加给出「已经在列表」文案 → 清理并停引擎）
- [AC-13] `.torrent` 文件校验是纯函数，边界可在发送前拦下 | 判: `python toolchain/build.py desktop --test add-file.test` 退出码 0，覆盖空文件（0 字节）、超过 8 MiB、扩展名不是 `.torrent`、内容是纯文本（首字节不是 bencode 的 `d`）、合法种子五类输入，且「拦截原因」互不相同（不能所有非法都返回同一句）
- [AC-14] 文件添加的请求契约与磁力不同：`Content-Type: application/octet-stream` + 原始字节 body | 判: 同上命令，用 stub fetch 断言 URL、method、`Authorization`、`Content-Type` 精确值、body 是 `Uint8Array` 而非 JSON 字符串；失败文案与磁力共用一套状态码映射
- [AC-15] 真实引擎端到端：选一个真种子文件 → 任务出现在列表且 infohash 与本地计算一致 | 判: `python desktop/tests/t_add_file_live.py` 退出码 0（起真引擎 → 前端 `addTorrentFile()` 发 `fixtures/` 里的真种子 → 断言返回 infohash 等于 Python 侧独立算出的 infohash → 轮询 `/state` 断言任务出现 → 重复添加给出「已经在列表」→ 清理并停引擎）
- [AC-16] 任务操作的请求契约：删除带 `delete_files` 查询参数、暂停/恢复走子路径，且 infohash 非 40 位 hex 时拒绝发请求 | 判: `python toolchain/build.py desktop --test row-action.test` 退出码 0，用 stub fetch 断言 `DELETE /torrents/<ih>?delete_files=0|1`、`POST /torrents/<ih>/pause|resume`、四类状态码（400/404/409/5xx）各有独立中文文案、非 40 位 hex 的 infohash 直接抛错且 `fetch` 未被调用
- [AC-17] 行内按钮显示哪个动作由任务状态决定，且是纯函数 | 判: 同上命令，覆盖 downloading/seeding/checking/metadata（显示「暂停」）、paused（显示「继续」）、finished/queued/allocating/pending/other/unknown（不显示动作）共十类状态，并断言 `paused` 标志与状态矛盾时以 `paused` 为准
- [AC-18] 行内交互：暂停/继续点击即调对应回调、删除必须二次确认、删除时可选择是否连文件一起删、失败就地显示原因且不误删行 | 判: `python toolchain/build.py desktop --test row-action-ui` 退出码 0，断言「点暂停未确认即生效」「删除未确认前不发请求」「确认后按选择传 true/false」「失败时行仍在且显示错误」「操作进行中按钮禁用防重复点击」五类情形
- [AC-19] 真实引擎端到端：暂停 → 状态变 paused → 继续 → 状态离开 paused → 删除 → 列表空 | 判: `python desktop/tests/t_row_action_live.py` 退出码 0（起真引擎 → 前端 `addTorrent()` 加合成磁力 → `pauseTorrent()` → 轮询断言 `paused=true` → `resumeTorrent()` → 断言 `paused=false` → `removeTorrent()` → 断言列表回到 0 条 → 停引擎）
- [AC-20] 拖入内容的路径判定：单文件走文件、磁力文本走磁力、多文件与非法文件给出可读拒绝、无关文本静默忽略 | 判: `python toolchain/build.py desktop --test drag-drop.test` 退出码 0
- [AC-21] 遮罩判定只依赖 `dataTransfer.types`（dragover 阶段读不到实际内容），含文件或文本才亮遮罩，其他类型不亮 | 判: 同 AC-20 的命令
- [AC-22] 窗口级拖拽交互：拖入亮遮罩、离开与放下都撤遮罩、放下后按判定分发到磁力/文件回调、拒绝时显示文案 | 判: `python toolchain/build.py desktop --test drag-drop-ui` 退出码 0

## 实现期偏离（与批准版本的差异）

1. **M2a 用构建期环境变量传引擎地址与令牌，而不是读 `engine.endpoint.json`。**
   浏览器里没有文件系统，读不到引擎写的端点文件。开发流程改为：先跑
   `python desktop/launcher.py --print-env` 拉起引擎并打印 `VITE_ENGINE_URL` /
   `VITE_ENGINE_TOKEN`，再启动 vite。**这是分阶段落地，不是设计改变**：读文件与
   sidecar 拉起的逻辑已经在 `desktop/launcher.py` 里实现并有 AC-08 覆盖，
   M2b 的 Tauri 壳直接复用它。「配置从哪来」收在 `src/config.ts` 一个函数里，替换只动一处。
2. **AC-06 的实现是多语言串联，但对外仍是一条命令。** `python desktop/tests/t_live_engine.py`
   内部：拉起引擎 → 本地造种子 → 等到 seeding → 以环境变量把 URL/TOKEN 传给
   `--test live`（vitest + jsdom）→ 断言真实数据经解析层后被渲染出来。
   判定入口没变，变的是内部编排。
3. **测试栈新增 vitest / jsdom / @testing-library/react**，仍然不引组件库。
   组件测试需要 DOM，这是唯一新增的运行时依赖，且只在开发期。

## 发布前验证（不在 G3 卡点内）

`npm run tauri build` 产出 exe 与安装包。**不进 G3 的理由**：首次编译约 400 个 Rust crate，
单次 15–25 分钟；塞进卡点会让每次验收都付这个代价，而它验证的是「打包链路通」，
不随代码改动频繁失效。做法同 `engine/tests/real_swarm.py` —— 单独命令、单独证据、写明排除理由。

## 不做范围

- 不做文件树（种子内部文件选择）。需要引擎先补 `/torrents/{ih}/files` 接口，属接口扩展。
- 不做速率曲线与历史图表。需要引擎侧聚合历史数据，M1b 只提供瞬时值。
- 不做设置页、代理、限速、RSS、做种规则。
- 不做主题切换与国际化。当前只有深色一套。
- 不做拖拽排序、多选批量操作。
- 不引入组件库（MUI/Ant/Radix 全不引）。理由：BT 客户端界面元素少，组件库带来的包体积与
  样式覆盖成本大于收益；且它们的默认动效不符合「只碰 transform/opacity」的约束。
- 不做自动更新。

## 未验证

> 2026-11-03 清理：删掉五条已经完成的（Tauri 壳与打包、真实浏览器渲染、帧率实测、
> 浏览器端配置来源、200% 缩放），它们当初是真的没做，现在都做完了 ——
> 留着会让「还差什么」的判断建立在错的前提上。合并了两条重复的拖拽条目。
> 同日 M2g：拖拽添加已实现，条目从「功能未做」移到「验证待做」。

### 功能未做

- **拖拽不支持一次多个文件。** 明确拒绝并说明原因，不是静默取第一个 —— 见关键设计决策。
  要支持得先有引擎批量接口与部分失败处理，那是独立一轮。
- **删除与暂停/恢复已在 M2f 补齐，但「删除时是否删本地文件」只做了二选一提示，
  没做「文件正在被其他程序占用」的失败路径。** 引擎侧 `remove_torrent` 会先关句柄，
  但 Windows 上杀毒软件扫描时仍可能删不掉，此时界面只能显示错误文案，未验证实际表现。

### 验证待做（只能在真机或换机器上做）

- **添加对话框与新的行内按钮在真实壳（WebView2）里的观感未验证。** jsdom 只证明交互与
  文案，不证明焦点、遮罩层、长链接换行、按钮在 64px 行高里的位置在 WebView2 里长得对。
- **拖拽在真实壳（WebView2）里未验证。** 已知并处理掉的是「Tauri 替换 WebView2 拖放
  处理器」这一层：`lib.rs` 里加了 `.disable_drag_drop_handler()`，否则页面的
  `dragenter` / `drop` 在 Windows 上根本收不到（tauri 2.12.5 的源码注释原话是
  "required to use HTML5 drag and drop APIs on the frontend on Windows"）。
  剩下未验的是真实 OLE 拖放能不能被转成标准事件：`dataTransfer.types` 里给的是不是
  `Files`、`dataTransfer.files` 能不能读。这两点只有真机能答。

- **引擎崩溃时 UI 的表现未验证。** 当前只有「轮询失败则显示离线」的设计，
  未做进程退出事件的实测 —— 尤其是「引擎死了但界面还写着 0 个任务」这种骗人的形态。
- **`.torrent` 文件的「内容与扩展名不一致」由引擎兜底。** 前端只查首字节是不是
  bencode 的 `d`，改名的 zip 会被送去引擎，由 `torrent_info` 解析失败回 400。
  分工是刻意的：前端不做 bencode 解析，解析失败的原话由引擎给，比我猜的准。
- **添加后不校验「任务真的开始下载」。** AC-12/AC-15 断言的是「请求被接受、任务出现在
  列表里」，不是「种子能下动」——后者依赖外网与做种情况，属于 `real_swarm.py` 的范围。
- **长路径（>260 字符）未验证。** 中文路径已在真机上验证可用（M2c 修过编码缺陷），
  但 `\\?\` 前缀相关的路径长度问题在下载根目录上仍未解决，UI 展示路径时不做假设。
- **150% 缩放未验证。** 200%（dpr=2）已在主电脑上跑通，100% 在笔记本上跑通，
  中间档位没有机器。
