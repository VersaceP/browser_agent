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
    "mode": "lead",
    "lead_max_steps": 20,
    "worker_max_steps": 30,
    "max_browser_agents": 8,
    "default_worker_concurrency": 3,
    "hitl_poll_interval_seconds": 2,
    "hitl_wait_timeout_seconds": 600,
    "hitl_max_step_retries": 1,
    "worktree_dir": "worktree",
    "context_file": null
  }
}
```

- `mode`: `lead` 使用多 agent 编排；`single` 直接运行单个 BrowserAgent。
- `lead_max_steps`: LeadAgent 最大决策轮数。
- `worker_max_steps`: BrowserAgent 最大执行轮数。
- `max_browser_agents`: 同时运行的 browser worker 上限。
- `default_worker_concurrency`: batch browser/plan 工具的默认并发数。
- `hitl_poll_interval_seconds`: `Hitl.requestPause` 后轮询恢复状态的间隔。
- `hitl_wait_timeout_seconds`: 等待人工介入的最长时间。
- `hitl_max_step_retries`: HITL 恢复后重试当前 ABCP step 的次数。
- `worktree_dir`: 运行日志和 artifacts 的根目录。
- `context_file`: 可选静态 prompt 上下文文件。任务期间应保持稳定，否则会降低 prompt cache 复用率。

## 运行任务

默认多 agent 模式：

```bash
python main.py --task "打开 https://example.com 并总结页面。"
```

单 BrowserAgent 模式：

```bash
python main.py --mode single --task "打开 https://example.com 并总结页面。"
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
- `run_skill_agent`: 将浏览器 trace 总结为可复用策略或 ABCP step 模板。
- `execute_abcp_plan`: 对单个 item 执行确定性的 ABCP method steps。
- `run_abcp_plan_batch`: 对多个 item 复用同一个确定性 plan，并支持先验证再并发。
- `run_browser_batch`: 为异构页面或需要 LLM 判断的任务派生多个 BrowserAgent。
- `final_answer`: 结束 LeadAgent 运行。

复杂结构参数会以 JSON 字符串形式传入，以兼容 strict tool schema。例如：

```json
{
  "items_json": "[{\"url\":\"https://example.com\"}]",
  "variables_json": "{}",
  "steps_json": "[{\"method\":\"Page.navigate\",\"params\":{\"pageId\":\"...\",\"url\":\"{item.url}\"},\"save_as\":\"page\"}]",
  "context_template": "采集 {item.url}",
  "concurrency": 3,
  "validate_first_n": 1
}
```

BrowserAgent 的 `browser_call` 使用：

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

## 典型批量流程

```text
LeadAgent 接收任务
  -> spawn_browser_agent: 探索列表页并收集详情页 URL
  -> spawn_browser_agent: 探索一个详情页并验证字段/selector
  -> run_skill_agent: 将 trace 转成确定性 ABCP steps
  -> run_abcp_plan_batch(validate_first_n=2 or 3): 先验证样本，再并发执行剩余 item
     -> validation_failed: 查看 failed_details，修正 steps，重试样本
     -> validation_hitl_required: 等待人工介入后重试
     -> partial_failed / partial_hitl_required: 仅重试或降级失败 item
  -> final_answer: 汇总成功、失败和阻塞项
```

只有在确定性 ABCP plan 不可复用，或页面确实需要 LLM 判断时，才建议使用 `run_browser_batch`。
