# 跨机测试包 — 界面渲染与帧率

这个包回答一个问题：**任务列表在 1000 条的时候，滚动到底卡不卡。**

打包成一个 HTML 文件，不用装任何东西。全程不联网。

---

## 怎么用

1. 把 `crossmachine-pack-v1.zip` 拷到目标电脑，解压（右键「全部解压缩」即可）。
   解压出来是一个 `crossmachine-pack-v1` 文件夹，四个文件平铺在里面。
2. 双击其中的 `perf.html`，用 **Chrome 或 Edge** 打开。
   （不要用 IE、不要用微信内置浏览器）
3. 点「开始测量」，等约 10 秒。
4. 跑完点「保存结果 JSON」，文件会进「下载」目录。
5. 把那个 JSON 发回来。想更准确的话，双击 `collect-env.ps1` 采一份机器信息一起发。

**建议跑两次，发第二次的。** 第一次可能包含磁盘冷启动和字体加载的额外开销。

### 需要装什么

**什么都不用装。** 页面是纯静态 HTML，无外链、无模块解析；脚本是 PowerShell 5.1 语法。
Windows 10/11 自带 Edge 和 PowerShell，解压即用。

唯一前提是有一个现代浏览器（Edge 或 Chrome 任一）和一个能解压 zip 的东西
（Windows 自带资源管理器就够）。这条也写在 `MANIFEST.json` 的 `target` 段里，
不是口头承诺。

### 怎么确认这份包没被换过

`crossmachine-pack-v1.zip` 旁边有一个 `crossmachine-pack-v1.zip.sha256`。
在 PowerShell 里：

```powershell
Get-FileHash .\crossmachine-pack-v1.zip -Algorithm SHA256
```

输出里的 `Hash` 值和 `.sha256` 文件里那一串一致，就是原包。不一致说明传输过程中
损坏，或者拿到的是别的版本 —— 换一份再试，别拿它测数据。

包内每个文件的哈希记在 `MANIFEST.json` 的 `files` 数组里，解压后可以逐个比对。

---

## 跑之前请注意

这几件事会显著影响数字，跑之前检查一下：

| 事项 | 为什么 |
|---|---|
| 别用远程桌面 / 向日葵 / ToDesk 远程操作 | 远程会话下浏览器会退化成软件渲染，测出来必然掉帧，跟代码无关 |
| 把窗口拉大到占满屏幕，别最小化 | 窗口最小化或被遮挡时浏览器会把动画帧降到 1Hz，会显示 0 帧 |
| 关掉视频、直播、游戏 | 抢占 GPU 和 CPU，数据不可比 |
| 笔记本插上电源，电源计划选「高性能」 | 省电模式会主动压降刷新率 |
| 关掉「减少动态效果」辅助功能 | 开着的话动画被浏览器直接禁用，测出来的数字偏乐观（页面会提示） |

页面顶部会显示识别到的渲染器和 DPI，如果写的是 `SwiftShader` 或 `Microsoft Basic Render Driver`，
说明这台机器在用 CPU 画图，**那份帧率数据不能用来判断代码好坏**，页面会自动标注为「环境受限」。

---

## 结果怎么解读

JSON 里有三个场景，它们的作用不一样：

| 场景 | 含义 | 看什么 |
|---|---|---|
| S1 静态渲染 | 1000 条铺上去后不动 | 看 `mountMs`：首次渲染耗时。超过 200ms 说明挂载太重 |
| S2 纯滚动 | 只滚，数据不更新 | 看 `fps` 和 `p95`：这是虚拟滚动本身的开销 |
| S3 滚动 + 更新 | 边滚边每 100ms 刷新数据 | 和 S2 比。**这一项才是真实使用场景** |

**关键看 S2 与 S3 的差值**，不是单看某一个 fps：

- S2 好、S3 差 → 10Hz 数据更新在拖累滚动，需要优化渲染路径
- S2 就差 → 虚拟滚动或 CSS 有问题，跟数据更新无关

`verdict.level` 是页面自己给的结论，四档：

- `pass` — 都在阈值内
- `warn` — 有指标超线但没到严重程度，列表里写了是哪一项、超了多少
- `fail` — 明显不达标
- `env-limited` — 环境受限（软渲染），数据只作记录

阈值和判定理由都写在 JSON 里（`thresholds` 和 `verdict.reasons`），不用猜。

---

## 另一份文件：`collect-env.ps1`

双击（或在 PowerShell 里右键「使用 PowerShell 运行」）它会采集机器信息，写到 `out\` 目录。
**只读，不改任何设置。** 内容全是英文 —— 这是故意的：中文脚本在部分系统的 PowerShell 里
会因为编码判断错误而静默丢字段，英文能保证在任何语言版本的 Windows 上都采全。

它和帧率 JSON 配套看。重点字段：

| 字段 | 为什么看它 |
|---|---|
| `browsers.chrome.version` | 不同浏览器版本的光栅化行为有差异 |
| `display.scalePercent` / `env.dpr` | 150% 或 200% 缩放下栅格化成本翻倍，帧率不可与 100% 直接比 |
| `power.hint` | `power-saver` 会主动压刷新率 |
| `gpus[].isVirtual` | 出现虚拟显示器（ToDesk / Parsec 等）说明合成路径可能被改写 |
| `virtualization.caveat` | 四档：`clean` / `virtual-display-present`（仅装了虚拟显示器）/ `remote-tool-running`（远程工具在跑）/ `remote-session`（真的在远程会话里）。后两档的帧率数据要打折看 |

---

## 这个包不测什么

- 不测下载速度、不测网络、不测引擎。这是纯前端渲染测试，不连任何服务器。
- 不测真实种子。要测下载得用另一套工具，需要两台机器配合。
- 不采集任何个人信息。`collect-env.ps1` 只读系统型号、CPU、显卡、显示器、浏览器版本，
  不读文件、不上传、不写注册表。

---

## 文件说明

| 文件 | 作用 | 需要安装 |
|---|---|---|
| `perf.html` | 测试页面，单文件自包含，双击即开 | 否 |
| `collect-env.ps1` | 采集机器环境，结果写到 `out\` 目录 | 否 |
| `MANIFEST.json` | 清单：每个文件的 sha256、用途、是否需安装 | 否 |
| `RUN.md` | 本文件 | 否 |

压缩包本身在项目里是 `crossmachine/dist/crossmachine-pack-v1.zip`，
换电脑只需要拷这一个文件。

`perf.html` 与 `MANIFEST.json` 是构建产物，不在版本库里。如果你拿到的是一个空目录或
缺这两个文件，在本机项目根目录跑：

```
python toolchain/build.py desktop --perf
python toolchain/build.py crossmachine
```

第二条命令会同时更新目录内容和重新压出 zip。压缩包是可复现的 —— 同一份输入
重打多少次，哈希都一样，所以上面的核对方法才有意义。

