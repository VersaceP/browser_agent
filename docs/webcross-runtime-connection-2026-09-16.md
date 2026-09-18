# WebCross 动态端口与 /resume 连接修复

## 事故与责任边界

任务 `9afbdcb41379441182165f8aec704abf` 的恢复流程已经加载原计划和阶段状态，
但 Harness 配置仍指向 `ws://127.0.0.1:61990/ws`。WebCross 重启后的 WebSocket
端口变成 `55298`；旧端点不可达，不代表新端点不可用。

此前恢复执行中，schema bootstrap 已记录连接失败，Lead 仍进行了 6 次模型调用。
这部分是 Harness 的连接适配和失败处理问题。WebCross 曾退出/重启的原因是另一件事，
本次改动不声称修复 dispatcher 的退出，也不修改 WebCross 安装包。

## 核对的实际协议

依据安装的 WebCross `0.9.0-beta`，构建
`wc-cac0fe42c9f8-ab3c724d5be7d6b6d89eabc7`，核对了安装包内的
`@abcp/dispatcher-host/dist/localControlServer.js`、
`@abcp/control-contracts/dist/local-control.js`、WebSocket adapter，以及已安装 CLI 的实现。
这不是根据另一个工作区版本推断协议。

官方 CLI 默认读取 `~/.webcross/runtime/dispatcher-host.json`。描述文件只有
`protocolVersion`、`instanceId`、`socketPath`，没有 WebSocket URL。
本机 socket 当前位于 `~/Library/Application Support/webcross/runtime/dispatcher-host.sock`。

本地协议为四字节大端长度前缀加 UTF-8 JSON。通过 `hello` / `welcome` 建立 Agent
身份，`request` / `response` 调用 ABCP Action，通过 `watch` / `replay` 接收和恢复事件。
它与 WebSocket 共用 WebCross 的 Action 调度与权限检查。

## 实现

### 连接来源

新增 `browser.transport`，取值为 `websocket` 或 `local`；未配置时仍为 `websocket`，
保持原有远端/手工指定 URL 的行为。新增 `browser.runtime_descriptor`，默认指向官方
CLI 描述文件路径。本机 `config.json` 已显式切到：

```json
{
  "browser": {
    "transport": "local",
    "runtime_descriptor": "~/.webcross/runtime/dispatcher-host.json"
  }
}
```

这是向现有 browser 配置新增字段的示例，不是完整配置。现有其余字段和凭据均保留。
`local` 模式不读取旧 `ws_url`，也不会发送 WebSocket JWT。当前服务允许官方本地
unauthenticated 会话，并已在线验证其可见原任务 Fleet。`websocket` 模式仍按原方式发送 JWT。

`ABCPClient.connect()` 是启动、schema bootstrap、worker 新连接及恢复探针的共同入口。
`local` 模式每次连接重新读取描述文件；不缓存旧 socket 地址，不扫描端口，不从日志猜端口，
不把描述文件当 WebSocket 地址。描述文件与 welcome 的实例和协议版本必须一致。

### 请求与事件

本地适配器把正式 local-control 帧转换为客户端已有的 RPC/事件信封，因此继续使用原有：

- 按请求 ID 关联并发调用与乱序响应的机制。
- 结构化 RPC 错误与 `request_sent` 事实。
- NotificationHub、事件 cursor 与重放去重。
- Fleet 绑定、权限、租约、合同、阶段状态和尝试预算检查。

本地协议不会像 WebSocket 一样在注册时自动建立事件订阅。适配器在成功
`System.register` 后显式执行一次 `watch`，从 welcome 提供的 cursor 开始，之后才返回
注册结果。没有后台轮询或每次 Action 启动一个 CLI 子进程。

同一客户端关闭再连接时保留其本地 Agent 路由句柄；新进程仍是原有的阶段级恢复，
不声称恢复旧 worker 协程、内存中的订阅者或页面操作目标。原任务 Fleet 必须继续通过原闸门。

### 失败处理

连接错误记录实际来源、端点、阶段、异常类型和 errno。WebSocket URL 的账号、密码及
查询参数不会进入这组诊断。恢复探针额外报告失败时执行到的协议操作。

schema bootstrap 中的连接建立失败或已证实的致命传输错误，直接结束本轮 Lead 为
`blocked` / `browser_connection_unavailable`，不调用模型、不派发 worker、不消耗阶段尝试。
仍走正常收尾，写完成回执和上下文快照，保留原计划和产物。单纯 schema 缓存/目录问题仍沿用
原有降级逻辑，不把所有 schema 错误都当成连接中断。

这里的阻断依据是实际连接失败这一状态事实，不判断业务意图。恢复方式是服务恢复后再次
`/resume` 或使用现有恢复探针；不会自动重放下载、Workflow 等结果不确定的操作。

## 边界与回滚

- 当前 `local` 适配器支持官方 unauthenticated 本地会话，不实现 paired Profile 签名。
  需要认证的服务将拒绝握手，Harness 明确报告失败，不自动降级或读取 CLI 私钥。
  需要 JWT 的服务继续使用 `websocket` 模式。
- 本次没有实现“断线后从 Workflow 第 N 步继续”，也没有改变阶段续跑决策。
- 本次没有重启 WebCross、创建 Fleet 或实际恢复用户业务任务。
- 回滚连接方式只需设置 `browser.transport` 为 `websocket` 并提供当前有效的 `ws_url`。
  旧配置保留的 `61990` 已失效，不能直接当作有效回滚端点。
- 修改后应重新启动 Harness，再执行 `/resume`；已运行进程不会自动加载修改后的 Python 代码和配置。

## 验证

新增 `tests/test_webcross_local_transport.py` 使用真实临时 Unix socket，覆盖描述文件切换、
同客户端重连、新客户端恢复、拆分帧、并发乱序响应、通知、事件重放、RPC 错误、握手超时、
认证拒绝、实例不匹配、无 WebSocket 回退、凭据隔离和失败诊断脱敏。
另覆盖实际 spawner 恢复入口，以及带 ResumeContext 的 Lead 在不可连接时不调用模型、
不改变原阶段状态和原计划。

在线只读核验使用当前配置完成 `System.register`、`System.getCapabilities`、
`System.listEvents`、`Fleet.list`。事件订阅建立成功，原 Fleet
`6d1a5c11-9cdf-480a-9214-2d1e030a05af` 可见，业务动作重放数为 0。
这是协议连接与身份可见性核验，不等于完整业务任务恢复已经成功。

验证结果（conda `agent` 环境）：

- 完整测试集：`4206 passed, 3 skipped, 1022 subtests passed`，181.67 秒。
- 最后校正为官方 4 MiB 本地帧上限后，新模块再次运行：`11 passed, 3 subtests passed`。
- 修改模块的 `py_compile` 和 `git diff --check` 通过。
