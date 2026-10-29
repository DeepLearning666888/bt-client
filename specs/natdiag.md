# natdiag

## 目标

NAT 端口映射诊断模块：把引擎层 6 类 alert 归一成统一事件流，产出可判定的映射状态与结论。本单元只验收「可独立构建、可重复验证」的部分。

## 验收标准

- [AC-01] 核心库单元测试全部通过 | 判: `natdiag/build/natdiag-tests.exe` 退出码 0，输出中失败数为 0

## 不做范围

- 不验收两种「外部网络路径」判定结论的准确性：本网络环境下构造不出 `working` 与 `refused` 的真实场景（13 台 UPnP 设备拒绝提供描述文件），这两条判定路径只有合成事件的单测覆盖。
- 不验收跨平台：只在 Windows + MSVC 19.44 下验证。
- 不验收性能：无吞吐、延迟、内存指标。

## 未验证

- **working / refused 判定路径无真实环境验证**：仅由单测合成 alert 事件覆盖。真实路由器上是否按预期产生 `portmap_alert`、`portmap_error_alert` 未取得证据。
- **OpenSSL 缺失**：当前 libtorrent 构建未启用 OpenSSL，BT 协议加密（MSE）不可用。是否影响真实 swarm 连通性未测。
- **golden 回归与 JSON 契约未纳入本 spec 的 AC**：这两项依赖运行中的 DHT 网络和 peer 采样，不满足「一条命令可判定」，因此降级为手工复核项。这是本规范的已知边界（standard.md 9.2）。
