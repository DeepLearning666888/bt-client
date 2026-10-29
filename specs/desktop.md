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

## 验收标准

- [AC-01] 依赖可从锁文件复现安装 | 判: `python toolchain/build.py desktop --install` 退出码 0，且 `desktop/package-lock.json` 存在
- [AC-02] TypeScript 严格模式零错误 | 判: `python toolchain/build.py desktop --check` 退出码 0
- [AC-03] 引擎 JSON → UI 模型的结构校验覆盖边界 | 判: `python toolchain/build.py desktop --test model` 退出码 0，覆盖空列表/字段缺失/类型错误/无元数据四类
- [AC-04] seq 未变时不产生新快照对象、不触发渲染 | 判: `python toolchain/build.py desktop --test poller` 退出码 0，断言 unchanged 响应下监听器调用次数为 0
- [AC-05] 1000 个任务时 DOM 行数不随总数增长 | 判: `python toolchain/build.py desktop --test virtual` 退出码 0，断言渲染行数 ≤ 40
- [AC-06] 真实引擎对接：起 `engine.exe` 后 UI 数据层能拉到并渲染任务名 | 判: `python desktop/tests/t_live_engine.py` 退出码 0（本机回环，零外网依赖）
- [AC-07] 生产构建产出静态资源 | 判: `python toolchain/build.py desktop --build` 退出码 0，且 `desktop/dist/index.html` 存在
- [AC-08] 引擎发现逻辑：无实例时拉起、已有实例时复用、残留过期端点时不误连 | 判: `python desktop/tests/t_engine_discovery.py` 退出码 0，三种情形各断言一次

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

- **Tauri 壳与打包完全未做。** 本轮只交付 M2a（React 数据层 + 列表 UI）。sidecar 拉起、
  读取端点文件、进程退出事件都还没在 Tauri 环境里跑过；`launcher.py` 的三情形判断有
  AC-08 覆盖，但那是 Python 实现，Rust 侧要照着重写一遍。打包本身见「发布前验证」。
- **没有真实浏览器渲染验证。** AC-06 的渲染断言在 jsdom 里做，覆盖「数据 → DOM」，
  不覆盖真实排版、字体与滚动合成层。headless 截图此前已验证不稳定（高度上限 940px），
  没有再花时间在上面。
- **帧率没有实测。** AC-05 只证明 DOM 节点数与任务总数脱钩，不证明 60fps。
  真实帧率需要在有 GPU 的机器上用 DevTools Performance 面板录。
- **浏览器端读不到本地文件。** 配置靠构建期环境变量注入，所以 `dist/` 产物里烧死了
  当次的端口与令牌；换端口就要重新构建。M2b 改用端点文件后这条消失。
- **中文路径与长路径。** M0 遗留的 `\\?\` 问题在下载根目录上仍未解决，UI 侧展示路径时不做假设。
- **高 DPI 缩放未验证。** 未在 150%/200% 缩放下检查布局。
- **引擎崩溃时 UI 的表现未验证。** 当前只有「/health 失败则显示离线」的设计，未做进程退出事件的实测。
