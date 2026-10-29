# 单人项目交付规范 · 快速上手

规范正文见 [`standard.md`](standard.md)。本文件只讲怎么用，3 分钟读完。

## 它解决什么

一句话：**让「做完了」这个结论能被脚本核对，而不是靠自觉。**

单人项目没有第二个人签字，最大的风险不是做错，是自己给自己放行。所以这里把「人审计」整个换成「脚本判定」——四个卡点全部由 `gate.py` 出结论，证据全部由命令产出，人不参与打分。

## 四个卡点

| 卡点 | 时机 | 一句话 |
|---|---|---|
| G1 | 开工前 | 验收标准写好了吗，每条带判定方式吗 |
| G2 | 交付前 | 构建真的过了吗 |
| G3 | 交付前 | 每条验收标准都有证据文件吗 |
| G4 | 交付前 | 没验证的部分声明了吗 |

只保留 4 个是刻意的。单人能长期维持的流程开销上限约 10% 工时，再多就会整体弃用（不是逐渐减少，是一次性废止）。

## 三步跑起来

```bash
# 1 从模板复制，写验收标准
cp delivery-standard/templates/unit-spec.md specs/myunit.md
#    编辑后校验结构
python delivery-standard/tools/gate.py g1 --unit myunit

# 2 产出证据（命令写在 config.toml 里，不手写文件）
python delivery-standard/tools/gate.py gen --unit myunit

# 3 按档位执行全部卡点
python delivery-standard/tools/gate.py all --unit myunit
```

退出码 `0` 全过 / `1` 有卡点不通过 / `2` 用法或配置错误。

其他命令：

```bash
python delivery-standard/tools/gate.py doctor      # 打印环境与已声明单元
python delivery-standard/tools/gate.py selftest    # 13 项负向自测，证明判定器会说不
python delivery-standard/tools/gate.py all --unit myunit --json   # 机读输出
```

## 写一个 spec

固定四节，缺一不可：

```markdown
# myunit

## 目标
一句话说清要达成什么。

## 验收标准
- [AC-01] 单测全绿 | 判: xxx-tests.exe 退出码 0
- [AC-02] 产物存在 | 判: 文件大小 > 0

## 不做范围
明确不处理的事。

## 未验证
没验的、为什么、需要什么条件才能验。没有就写「无」。
```

三条硬规则：

- 每条 AC 必须带 `| 判: 方式`，且方式要能执行或能测量。「功能正常」这类不算
- 写「待填写 / TODO / TBD」会被 G1 拦下——模板抄来不填过不了
- 「未验证」节空着 G4 不通过

## 配置

`config.toml` 里每个单元声明档位、构建命令、每个 AC 的证据产出命令：

```toml
[units.myunit]
tier = "T2"
build = ["C:/path/to/cmake.exe --build myunit/build"]

[[units.myunit.evidence]]
ac = "AC-01"
cmd = "myunit/build/myunit-tests.exe"

[[units.myunit.evidence]]
ac = "AC-02"
cmd = "python -c \"import os,sys;sys.exit(0 if os.path.getsize('myunit/build/a.lib')>0 else 1)\""
expect = 1     # 可选，期望退出码，默认 0。负向验证用
```

## 常见坑

**命令含空格的参数必须加引号。** 命令按 shell 规则分词（`shell=False`），`python -c import sys;sys.exit(3)` 会被拆成多个参数。正确写法：`['python -c "import sys;sys.exit(3)"']`。

**不要写 `>` 或 `2>&1`。** 输出重定向由 `gate.py` 接管，自己写会和它冲突。

**证据必须是 UTF-8 无 BOM。** PowerShell 的 `>` 和 `*>` 默认写 UTF-16LE，这类文件在 grep / diff 里表现为「搜不到、也不报错」。本项目已因此静默失败两次。`gate.py` 会直接拒绝带 BOM 或含 NUL 的证据文件。

**工具不在 PATH 时写绝对路径。** 例如本机 cmake 实际在 `C:/vcpkg-tools/cmake/cmake-4.4.4-windows-x86_64/bin/cmake.exe`。

## 目录

```
delivery-standard/
  standard.md          规范正文，唯一权威来源
  standard.html        渲染版，给人看
  config.toml          单元声明
  tools/gate.py        卡点判定器
  templates/
    unit-spec.md       spec 模板
    exceptions.md      偏离记录模板
specs/                 各单元 spec
evidence/<unit>/       证据，脚本产出
```

## 当前已声明单元

| 单元 | 档位 | 用途 |
|---|---|---|
| `delivery-standard` | T2 | 自举，用规范验收规范自身 |
| `natdiag` | T2 | 真实模块 |
| `__negative__` | T1 | 负向对照：无 spec，验证 G1 会失败 |
| `__failbuild__` | T1 | 负向对照：构建必然失败，验证 G2 会失败 |

后两个是常驻对照组，不要删。判定器如果只会说通过，它的绿灯一文不值。
