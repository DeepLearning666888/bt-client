# BT 客户端

基于 **libtorrent 2.x** 引擎 + **Tauri 2** 壳 + **React / TypeScript** 前端的 BT（BitTorrent）下载客户端，Windows 优先。

引擎用 C++ 封装成 HTTP 服务，前端通过 Tauri 壳发现引擎端点并驱动，实现「引擎崩溃可恢复、壳强杀可接管、任务加即落盘」的健壮性目标。

> **状态**：代码与自动化判据已完成（86 条验收标准全绿）。剩 5 条「现场验收」项需要干净机器冷启动或换网络复测（见 [PROGRESS.md](PROGRESS.md)）。

---

## 架构

```
┌─────────────────────────────────────────────┐
│  Tauri 2 壳 (btclient-shell)                │
│  ├─ 发现引擎端点 (engine.endpoint.json)      │
│  ├─ 托管 React 前端 (tauri.localhost)        │
│  └─ 生命周期：接管孤儿引擎 / 关窗语义        │
├─────────────────────────────────────────────┤
│  React + TypeScript 前端                     │
│  ├─ 任务列表 / 状态词表 / 实时轮询           │
│  └─ 磁力 + .torrent + 拖拽 三入口添加        │
├─────────────────────────────────────────────┤
│  C++ 引擎 (engine)                          │
│  ├─ libtorrent 2.x 封装                     │
│  ├─ HTTP 服务 (127.0.0.1:<动态端口>)         │
│  └─ token 认证 + 状态快照                     │
└─────────────────────────────────────────────┘
```

引擎在随机端口启动，把 `host/port/token/pid` 写入发现文件，壳读取后把 `baseUrl` + `token` 注入前端，避免端口冲突且前端零参数发现。

---

## 功能

- **三入口添加任务**：磁力链接、`.torrent` 文件、拖拽
- **任务管理**：暂停 / 继续 / 删除（含删除确认、文件占用时如实告知）
- **非法输入校验**：非磁力、重复添加、纯文本拖入等均有明确提示
- **健壮性**：引擎崩溃界面如实显示离线态；壳强杀后重开能接管孤儿引擎；任务加即落盘，每 30 秒补写
- **安装分发**：NSIS 安装包（currentUser 模式，不弹 UAC）

---

## 目录结构

| 目录 | 内容 |
|---|---|
| `engine/` | C++ 引擎（libtorrent 封装 + HTTP 服务） |
| `desktop/` | Tauri 2 壳 + React 前端 |
| `specs/` | 各单元的验收标准（86 条 AC） |
| `evidence/` | 验收证据日志 |
| `crossmachine/` | 跨机渲染测试包 |
| `handoff/` | 真机执行包（前置检查 + 两条轨道手册） |
| `natdiag/` | NAT 诊断模块 |
| `m0-baseline/` | 技术选型阶段的基线测量 |
| `delivery-standard/` | 交付标准与自检 |

---

## 构建

### 引擎（C++，需 libtorrent 2.x）

引擎依赖 libtorrent 2.x，构建细节见 `engine/` 目录与 `specs/engine-service.md`。

### 前端 + 壳（需 Node.js + Rust）

```bash
cd desktop
npm ci          # 从锁文件复现安装（package-lock.json 已入库）
npm run build   # 前端构建
# Tauri 壳构建
cd src-tauri
cargo build --release
```

完整安装包构建见 `specs/installer.md`（NSIS 打包）。

---

## 验证代码真伪（防投毒）

本项目 commit 与发布 tag 均使用 **GPG 签名**，Release 附带 **SHA-256 校验清单**。克隆或下载后请按以下方式核验，防止第三方投毒（恶意 fork、中间人篡改）。

### 1. 验证 commit 签名

```bash
git log --show-signature -1   # 查看最近一次提交的签名是否 Verified
git verify-commit HEAD        # 显式验签
```

若输出 `Good signature` 且指纹与下方公布的一致，说明提交确由本项目作者签名。

### 2. 验证 Release 二进制

下载 Release 附件（如 `setup.exe`）后，用同目录的 `SHA256SUMS` 校验：

```bash
sha256sum -c SHA256SUMS
```

### 3. 校验签名密钥指纹（可信锚点）

本项目的签名密钥指纹（在 GitHub 提交页与 Release 页均可核对）：

```
# 指纹在此处公布（生成后回填）
```

> **重要**：SHA-256 只能证明「文件未被篡改」，无法证明「文件出自作者」。
> 只有「SHA-256 + GPG 签名」两者结合，才能同时保证**完整性**与**来源**。
> 请勿信任任何来源不明、未验签的 fork 或二次分发包。

### 为什么不是 MD5

MD5 已被攻破（碰撞攻击），攻击者可构造「不同内容、相同 MD5」的文件绕过校验。
本项目一律使用 **SHA-256**（Git 自身即基于 SHA-256 内容寻址，天然防篡改）。

---

## 已知限制

- 5 条现场验收项待完成：首启耗时、启动横幅、二次启动（需干净机器冷启动）、界面观感、滚动条（审美判断）
- DHT 在本机网络被 DNS 污染，需换网络复测
- `Program Files` 安装、代码签名、覆盖安装等链路需另一台机器验证

详见 [PROGRESS.md](PROGRESS.md) 的卡点分类。

---

## 许可证

许可证待定（尚未选定开源协议）。在选定前，本仓库代码**默认保留所有权利（All Rights Reserved）**，
请勿在未获作者明确授权的情况下复制、分发或二次发布。
