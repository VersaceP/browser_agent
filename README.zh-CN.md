# ABCP Agent Harness

[English](README.md)

ABCP Agent Harness 将 LLM 的 tool calling 接到 ABCP Browser 的 WebSocket 能力上。Agent 不直接驱动 CDP、Playwright、截图识别或手写 selector，而是调用 `Page.navigate`、`DOM.getAXTree`、`Input.click` 等 ABCP method，并根据浏览器 observation 决定下一步。

## 环境要求

- Python 3.9 或更高版本。
- 一个可通过 WebSocket 访问的 ABCP Browser 服务。
- OpenAI-compatible 或 Anthropic API key。

## 快速开始

安装 Python 依赖：

```bash
python -m pip install -r requirements.txt
```

启动或连接你的 ABCP Browser 服务。默认配置使用：

```text
ws://localhost:9300/ws
```

设置 `config.json` 中声明的模型 API key：

```bash
export OPENAI_API_KEY="your-openai-key"
```

运行一个任务：

```bash
python main.py --task "打开 https://example.com 并总结页面标题和正文。"
```

CLI 会输出最终答案、任务 ID、任务目录和运行日志路径。运行日志与 artifacts 默认写入：

```text
worktree/<task_id>/
```

## 配置

CLI 默认读取 `config.json`。可以通过 `--config` 指定其他配置文件：

```bash
python main.py --config ./my-config.json --task "检查当前 Fleet 列表。"
```

### 模型

OpenAI-compatible 示例：

```json
{
  "provider": "openai",
  "model_id": "gpt-4.1",
  "api_key_env": "OPENAI_API_KEY",
  "base_url_env": "OPENAI_BASE_URL",
  "extra_params": {
    "temperature": 0.2,
    "max_tokens": 4096
  }
}
```

Anthropic 示例：

```json
{
  "provider": "anthropic",
  "model_id": "claude-sonnet-4-20250514",
  "api_key_env": "ANTHROPIC_AUTH_TOKEN",
  "base_url_env": "ANTHROPIC_BASE_URL",
  "extra_params": {
    "temperature": 0.2,
    "max_tokens": 4096
  }
}
```

`cache_control_mode` 控制显式 prompt-cache marker：

- `auto`（默认）：Anthropic 风格 provider 和已知支持的 OpenAI-compatible base URL 自动开启。
- `on`：强制发送 marker；如果 provider 拒绝，会自动无 marker 重试一次。
- `off`：永不发送 marker。

```json
{
  "extra_params": {
    "cache_control_mode": "auto",
    "temperature": 0.2,
    "max_tokens": 4096
  }
}
```

旧配置 `enable_cache_control` 仍兼容，但仅在未设置 `cache_control_mode` 时生效。

### 浏览器

默认浏览器请求格式是 `flat`：

```json
{
  "browser": {
    "agent_id": "abcp-agent",
    "ws_url": "ws://localhost:9300/ws",
    "jwt_token_env": "ABCP_JWT_TOKEN",
    "request_shape": "flat"
  }
}
```

如果 ABCP 服务端使用 JSON-RPC 请求格式：

```json
{
  "browser": {
    "request_shape": "jsonrpc"
  }
}
```

### Harness

常用 harness 配置：

```json
{
  "harness": {
    "lead_max_steps": 20,
    "worker_max_steps": 30,
    "max_browser_agent_instances": 3,
    "max_browser_agents": 3,
    "fleet_reuse_enabled": true,
    "same_fleet_multiworker_enabled": false,
    "fleet_auth_barrier_enabled": true,
    "fleet_auth_barrier_wait_seconds": 120,
    "auth_fleet_ledger_path": ".auth_fleet_ledger.json",
    "fleet_slot_reconnect_attempts": 2,
    "fleet_slot_reconnect_backoff_seconds": 0.25,
    "fleet_slot_manual_reset_after_failures": 3,
    "hitl_poll_interval_seconds": 2,
    "hitl_wait_timeout_seconds": 600,
    "worktree_dir": "worktree",
    "context_file": null
  }
}
```

- `lead_max_steps`: LeadAgent 最大决策轮数。
- `worker_max_steps`: BrowserAgent 最大执行轮数。
- `max_browser_agent_instances`: 可复用池里最多保留的长期存活 BrowserAgent slot 数。idle slot 会保留 ABCP 连接和页面 registry。普通新 worker 只复用连接并从新页面开始；显式 continuation 才会复用旧页面候选。
- `max_browser_agents`: 同时运行的 browser worker 上限；实际 browser slot 并发仍受 `max_browser_agent_instances` 限制。
- `fleet_reuse_enabled`: 由协调器为 worker 确定性分配 fleet，并将无 fleetId 的 `Page.create` 收敛到该分配。具名/隔离 fleet 不会进入通用复用池。
- `same_fleet_multiworker_enabled`: 多 slot 共享 task/session fleet 的灰度开关，默认 `false`；启用后各 worker 使用独立 page，owner 连接保持不变，通知由 harness 中继，同 page 调用串行化。
- `fleet_auth_barrier_enabled`: 登录/验证码按 fleet 全域加门，非 resolver 有界等待且超时不放行。等待时间由 `fleet_auth_barrier_wait_seconds` 控制。
- `auth_fleet_ledger_path`: 持久化的非敏感已验证会话索引；重启回收的 fleet 在账本对账前不会进入通用复用池。
- `fleet_slot_reconnect_attempts`: 具名会话 slot 每轮使用原 `agentId` 进行的有界重连次数；transport 故障本身不等于 fleet 已丢失。
- `fleet_slot_reconnect_backoff_seconds`: 重连间隔的基础时间；不会重放失败的浏览器写操作。
- `fleet_slot_manual_reset_after_failures`: 连续恢复失败轮次达到该值后返回 `session_manual_reset_required`；只有 host/operator 能使用回执中的 fleet id 和 generation 显式重置。
- `hitl_poll_interval_seconds`: `Hitl.requestPause` 后轮询恢复状态的间隔。
- `hitl_wait_timeout_seconds`: 等待人工介入的最长时间。
- `worktree_dir`: 运行日志和 artifacts 的根目录。
- `context_file`: 可选静态 prompt 上下文文件。任务期间应保持稳定，否则会降低 prompt cache 复用率。

## 运行任务

通过 LeadAgent 编排器运行任务：

```bash
python main.py --task "打开 https://example.com 并总结页面。"
```

从 stdin 读取任务：

```bash
echo "打开 https://example.com 并总结页面。" | python main.py
```

覆盖 agent id 或最大步数：

```bash
python main.py --agent-id demo-agent --max-steps 20 --task "检查当前 Fleet 列表。"
```

## 日志与 Artifacts

每次运行都会创建任务目录：

```text
worktree/<task_id>/
  run.jsonl
  artifacts/
```

`run.jsonl` 是 JSON Lines 格式。常见事件类型包括：

- `lead.model` / `agent.model`: 模型文本和 tool calls。
- `browser.call.result`: ABCP method 调用结果。
- `llm.usage`: 单次 LLM 调用的 token 与 prompt cache 指标。
- `llm.usage_summary`: 任务级 token 与 prompt cache 汇总。
- `lead.final` / `agent.final`: 最终答案。

截图类响应会保存到 `artifacts/`；大体积 base64 会从模型上下文中省略。

## Prompt Cache 可观察性

Harness 会记录 provider 返回的单次调用 cache 指标：

- `cache_read`
- `cache_creation`
- `uncached_input`
- `output`
- `cache_read_rate`
- `cache_reuse_rate`
- `cache_diagnostics.marker_count`
- `cache_diagnostics.marker_positions`
- `cache_diagnostics.cache_control_signature`
- `cache_diagnostics.cache_control`

`estimated_cost_usd` 当前预留为 `null`；后续可以通过配置模型价格启用成本估算。

`harness.context_file` 默认关闭。启用后，文件内容会注入静态 system prompt，并在 usage diagnostics 中记录 sha256。只建议用于稳定上下文；变化快的上下文应由调用方追加到动态上下文末尾。

## Lead Agent 工具

`LeadAgent` 不直接操作浏览器。它通过以下工具做任务规划、派发和汇总：

- `spawn_browser_agent`: 启动一个隔离的 BrowserAgent。
- `wait_browser_agents`: 等待一个或多个 browser worker。
- `list_browser_agents`: 查看当前 worker 状态。
- `lead_save_artifact`: 基于可信 extraction 证据保存 LeadAgent 重塑后的结构化行。
- `final_answer`: 结束 LeadAgent 运行。

LeadAgent 应通过 BrowserAgent phase 编排任务。BrowserAgent 的 `browser_call` 使用：

```json
{
  "method": "Page.navigate",
  "params": {
    "pageId": "...",
    "url": "https://example.com"
  },
  "reason": "导航到目标页面"
}
```

## 典型编排流程

```text
LeadAgent 接收任务
  -> emit_task_plan: 按 task_type 和 phase 拆分
  -> spawn_browser_agent: 用精确 expected fields 处理第一个 pending phase
  -> 校验 extraction artifacts 和 resultLevels
  -> lead_save_artifact: 仅在 schema_mismatch 且证据可信时重塑并保存
  -> 缺证/错证时 replan，或只派一个更聚焦的 continuation
  -> final_answer: 汇总成功、失败和阻塞项
```
