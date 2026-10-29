# natdiag

入站 / 端口映射诊断。从 `engine-probe` 里抽出来的独立模块，M1b 的引擎服务和
M2 的桌面端都会直接消费它。

它回答一个问题：**在这台机器、这个网络上，BT 客户端能不能建立入站端口？**
如果不行，是**网关拒绝**、**发出去没回音**，还是**协议压根没启用**——这三种的
处置动作完全不同，而 aria2 那类工具只会静默失败。

---

## 为什么不直接搬 engine-probe 的代码

原来那段逻辑混在 `main.cpp` 里，有几处会跟着进生产代码的坑：

| 原实现 | 问题 | 现在 |
|---|---|---|
| 状态存 `map<string,string>`，键是格式化后的文案 | 输出即逻辑，UI 要拿 JSON 只能正则拆字符串 | 结构化 `Snapshot` + `to_json()` |
| 错误存 map，同一 transport 的第二条覆盖第一条 | 数据有损，看不见「两个协议都试过都没成」 | `unattributed_errors` 是 vector，一条不丢 |
| 结论只有一个 `any_mapping` 布尔 | 区分不了拒绝 / 无应答 / 未启用 | `Verdict` 五态 |
| 「四种组合固定列出」写死在 printf 循环 | 换消费者就得重写，且没法测 | slot 固定顺序 + 判定表 |
| `external_ip` 有值就当作能入站 | 错判。libtorrent 也会从 peer / tracker 处得知外网地址 | `external_ip_reliable` 仅在存在 mapped slot 时为 true |

---

## 结构

```
natdiag/
├── include/natdiag/
│   ├── natdiag.hpp        公共 API：枚举、NatEvent、Snapshot、Collector
│   ├── serialize.hpp      to_json / to_text
│   └── lt_adapt.hpp       libtorrent alert → NatEvent（唯一接触引擎的头）
├── src/
│   ├── collector.cpp      事件状态机
│   ├── diagnosis.cpp      判定（Collector::snapshot）
│   ├── serialize.cpp      JSON + 人读文本
│   └── adapt.cpp          alert 适配
└── tests/
    ├── test_framework.hpp 零依赖断言
    ├── helpers.hpp        事件构造 helper
    ├── test_main.cpp
    ├── test_collector.cpp 状态机（12 例）
    ├── test_diagnosis.cpp 判定表 + 序列化（12 例）
    └── golden/probe-run2-utf8.log   回归基线
```

### 两个 target，不是一个

| target | 依赖 libtorrent | 用途 |
|---|---|---|
| `natdiag` | **否** | 核心逻辑。单元测试只链它，编译秒级，不需要引擎环境 |
| `natdiag_lt` | 是（PUBLIC） | 适配层。只含 `adapt.cpp` |

拆开是为了让**判定逻辑能脱离网络做穷举测试**。代价是 110 行适配代码，
换来的是一条 `ctest` 就能覆盖全部五种 verdict。

`lt_adapt.hpp` 是公开头且暴露 libtorrent 类型，所以 `torrent-rasterbar`
必须以 PUBLIC 传播——写成 PRIVATE 会让宿主编译不过。

---

## 用法

```cpp
#include <natdiag/natdiag.hpp>
#include <natdiag/lt_adapt.hpp>
#include <natdiag/serialize.hpp>

natdiag::Collector collector;

// 在主循环里
for (lt::alert const* a : alerts) {
    natdiag::NatEvent ev;
    if (natdiag::from_alert(*a, ev)) collector.feed(ev);   // 与引擎无关的事件返回 false
}
collector.set_elapsed(60);

natdiag::Snapshot const snap = collector.snapshot();
std::puts(natdiag::to_json(snap).c_str());   // 给 UI
std::puts(natdiag::to_text(snap).c_str());   // 给 CLI / 日志
```

`Collector` 不持有 session、不改 settings、不跑任何协议，只消费事件。

---

## 判定优先级

顺序即优先级，实现在 `diagnosis.cpp`：

1. 任一 slot `mapped` → **working** —— 硬证据，任何采样时长都成立
2. `unattributed_errors` 非空 → **refused** —— 有错误码也是硬证据，不受时长限制
3. `sampled_seconds < min_sample_seconds` → **insufficient**
4. 有 slot 为 `pending` 或 `log_lines > 0` → **silent**（本机实测就是这一种）
5. 其余 → **disabled**

`silent` 与 `refused` 分开是有意义的：前者要手动端口转发 + 查防火墙，
后者要换协议或看网关日志。混成一个「不通」等于什么都没说。

---

## 构建与测试

需要 MSVC + CMake + Ninja，以及 libtorrent（手工构建的走：

```bash
cmake -S natdiag -B natdiag/build -G Ninja -DCMAKE_BUILD_TYPE=Release \
      -DLibtorrentRasterbar_DIR=C:/libtorrent/install/lib/cmake/LibtorrentRasterbar
cmake --build natdiag/build
./natdiag/build/natdiag-tests.exe        # 24 cases / 75 checks
```

即使找不到 libtorrent，核心库和测试也照常构建（只是没有 `natdiag_lt`）。

`test_framework.hpp` 是手写的 40 行断言，没引 Catch2 —— 这里要测的只有枚举和
整数比较，这个环境装包的成本高于收益。

---

## 回归比对

```bash
cd engine-probe
python regress.py        # 60 秒，跑新旧两个二进制并逐项比对
python regress.py 20     # 快速跑
```

判据：`VERDICT` 行必须一字不差，四个 slot 的语义状态必须一致。
`log_lines` 允许浮动（libtorrent 的重试次数随网络时序变化，实测 15~21 之间）。

**用 Python 不用 shell 是踩过坑的**：本环境的 bash 脚本里 fork Windows 子进程
会直接返回 127，且没有任何错误输出。Python 的 `subprocess` 走 CreateProcess，
不受影响。同理 `probe-run2.log` 是 PowerShell `*>` 重定向产生的 UTF-16LE，
`grep` 对它静默返回空——基线已转成 UTF-8 存在 `tests/golden/`。

---

## 不做的事

| 不做 | 理由 |
|---|---|
| STUN 客户端 | libtorrent 已通过 `external_ip_alert` 给外网 IP，自实现会与引擎结论冲突 |
| 端口扫描 / 连通性探测 | 那是验证不是诊断，且会触发安全软件的扫描告警 |
| UI | M2 的事。这里只保证 JSON 契约 |
| 改 libtorrent settings | 模块只观察。settings 片段由宿主决定 |
| 判定「是哪个协议被拒」 | **做不到**。`portmap_error_alert` 只有 per-socket 的 mapping 索引，没有 protocol 字段。虽然 `remap_ports()` 内部固定先 tcp 后 udp，但 `delete_mapping` 后索引是否复位未写在文档里，靠这个推断是在赌实现细节。所以错误一律进 `unattributed_errors`，不假装知道协议 |

---

## 已知限制

- 采样阈值默认 15 秒，**是假设值**。依据只是「比实测 60 秒窗口短」，没有协议层
  重试周期的数据支撑。要等 M1b 联调时测出来再校准。
- `listen_ok` / `listen_fail` 只记录、不参与判定。监听失败和入站映射失败是两件事，
  现在没有把两者的因果关系建模。
