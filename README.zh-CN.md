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

### 思考 / 推理模式

`extra_params` 的三个键控制模型思考/推理，**同时支持 OpenAI 格式和 Anthropic 格式**
provider，并且**每个角色都能单独配**（见下）：

- `thinking` - 思考开关。接受 `bool`、字符串 `"enabled"`/`"disabled"`/`"on"`/`"off"`/`"true"`/`"false"`，
  或原样透传的 `dict`（方舟/DeepSeek 的 `{"type": "enabled"}`、Claude 扩展思考的
  `{"type": "enabled", "budget_tokens": 8192}`、厂商支持时的 `{"type": "auto"}`／`{"type": "adaptive"}`）。
- `reasoning_effort` - 思考强度：`"none"`、`"minimal"`、`"low"`、`"medium"`、`"high"`、`"xhigh"`、`"max"`。
  整套枚举原样转发、不按模型裁剪，但**越界取值各家反应不同**：方舟文档说「不生效」（静默忽略），
  DashScope 则校验后返回 400（实测传 `max` 报 `'reasoning_effort' must be one of: 'none',
  'minimal', 'low', 'medium', 'high', 'xhigh'`）。传出 OpenAI SDK 取值集之外的值时会打告警。
- `effort` - `reasoning_effort` 的简写别名（同时写时前者赢）。

线路翻译：

| 配置 | OpenAI 格式 | Anthropic 格式 |
|---|---|---|
| `thinking` 开/关 | `extra_body={"thinking":{"type":"enabled/disabled"}}`（厂商扩展，SDK 无此 kwarg） | 原生 `thinking=` kwarg；`true` 翻成 `{"type":"enabled","budget_tokens":N}`，因为官方 SDK 把 budget 标为必填 |
| `reasoning_effort` / `effort` | 顶级 `reasoning_effort` | 原生 `output_config={"effort":<级别>}`；`none`/`minimal` 改用开关表达 |

不无中生有：没写的键不会产生任何线路字段。显式关闭 + 又给了思考级别时，丢弃级别并告警
（方舟文档明确这一组合报错）。

说明：

- 思维链在 OpenAI 格式里通过 `reasoning_content`（方舟摘要类模型另加 `encrypted_content`）返回，
  在 Anthropic 格式里是 `thinking` 块。两个 provider 都会捕获并在下一轮通过 assistant prefix 块回传：
  DeepSeek 的工具调用轮次不回传 `reasoning_content` 会 400；方舟不报错，但回传后思维链可参与后续推理，
  且 `encrypted_content` 的优先级高于摘要。
- 本项目不建模的厂商私有字段（例如 DeepSeek Anthropic 格式的 `{"reasoning": {"effort": ...}}`）
  写在 `extra_params.extra_body` 里，两个 provider 都原样透传——不在通用层猜方言。
- 对方舟 Anthropic 兼容端点 `/api/coding` 实测（glm-5.2，2026-08-13）：**只有 `thinking.type` 生效**，
  `output_config`、`reasoning`、`reasoning_effort` 都被接受但静默忽略。需要调 effort 请走 OpenAI 格式端点。

### 按角色配置模型

六个角色都能单独配：**lead**、**worker**、**vl**（visual_verify / locate / arbiter /
reality_check 共用）、**vl_captcha**（验证码自解）、**plan_validator**、**claim_extractor**。

lead 与 worker 默认用顶层那份模型，可选的 `lead` / `worker` 段覆盖它——**两种合并规则**：

- 标量（`provider` / `model_id` / `api_key` / `base_url` / 超时重试）**整体替换**，
  所以某个角色可以整个换到另一家厂商；
- `extra_params` **浅合并**，改一个旋钮不会把顶层其它参数冲掉。

换了 `provider` 却没给那家的 `base_url` / `api_key` 时，会沿用顶层另一家的连接并在调用时报错，
config 装载会提前告警。其余角色各有自己的段（都带 `provider` / `model_id` / `api_key`）。

```json
{
  "provider": "anthropic",
  "model_id": "glm-5.2",
  "base_url": "https://ark.cn-beijing.volces.com/api/coding",
  "extra_params": { "thinking": { "type": "enabled" }, "max_tokens": 24000 },

  "lead":   { "extra_params": { "thinking": { "type": "enabled" } } },
  "worker": {
    "provider": "openai",
    "model_id": "glm-5.2",
    "base_url": "https://ark.cn-beijing.volces.com/api/coding/v3",
    "api_key": "<另一把 key>",
    "extra_params": { "thinking": { "type": "disabled" } }
  },

  "vl": {
    "extra_params": { "thinking": { "type": "enabled" } },
    "captcha_solve_extra_params": { "max_tokens": 2500 }
  },
  "plan_validator":  { "extra_params": { "thinking": { "type": "enabled" } } },
  "claim_extractor": { "extra_params": { "thinking": { "type": "disabled" } } }
}
```

`{"type": "enabled"}` 这种 dict 写法在两种 wire 格式上都实测可用（方舟 `/api/coding`、
DashScope compatible-mode），且不会像 `true` 那样在 Anthropic 路上额外合成 `budget_tokens`——
需要 Claude 扩展思考时再显式写 `budget_tokens`。

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
    "max_task_fleets": 3,
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
- `max_task_fleets`: 单个任务最多占用的 fleet（浏览器实例）数，`0` 表示不限。harness 不会主动关闭 fleet，所以开出来的 fleet 会一直占着额度，直到平台的权威库存不再报告它——从 owner 库存消失的 fleet 会把额度释放回去。计数只统计绑定到本任务 worker 的 fleet，不看 Agent 全局的 `Fleet.list`。任务显式指定的 fleet（`--fleet-id` 固定实例、`worker_contract.fleet_id`、已绑定的 `session_key`、`reuse_from_worker_id`）永远照用不被拦截，但同样计入总数。到达上限后，没指定 fleet 的 worker 自动复用本任务已有的 fleet（优先挑没有在跑的 worker 占着的那个），`worker_session_isolation_enabled` 的默认隔离让位于上限。只有两种情况没法这么服务，返回 `task_fleet_limit_reached` 回执：一是要求独立身份（显式声明 `needs_isolated_session` 或新开 `session_key`）；二是本任务的 fleet 全部绑给了具名会话——登录态的 cookie jar 不外借。这两种**等待都解不开**（harness 不关 fleet，worker 结束后 fleet 还在；具名会话的绑定也不随 worker 结束而释放），所以回执给的是：改用已有 fleet、走可信恢复流程释放 session binding、或调高上限。拒绝之前 cap 会强制重读一次权威 `Fleet.list`。dispatcher 是从整张 fleets 表作答、不按连接分域，所以一次成功的读取既能找到别的 slot 刚建的 fleet，也能退役任何已被平台回收的 fleet（不论原属哪个 slot）并把额度还回来。读取失败则一律不当作"消失"的证据。
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

按 phase 粒度恢复中断任务：

```bash
python main.py --resume worktree/<task_id> --task "补充指令"
```

交互提示符中的等价写法是 `/resume <任务目录> [补充指令]`。已经
`validated_done` 的 phase 及其当前有效 artifact 会保留；未完成 phase
会整段重跑。若进程停止时某个 phase 正在运行，重跑前必须由用户确认；
非交互模式需显式传入 `--resume-retry-interrupted`。历史 Fleet/page 仅作为
当前任务拥有的弱恢复候选，必须重新通过浏览器 inventory 验证；失效时回落
到普通路由。

任务目录、`task_plan.json` 或 `task_state.json` 被删除或损坏时，resume
会严格失败。校验发生在创建 `RunLogger` 之前，因此不会把已删除的 worktree
静默重建成一个空任务。

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
