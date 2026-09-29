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
ws://127.0.0.1:61168/ws
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

供应商、协议端点、凭据、各角色连接、推理强度、输出预算、缓存、配置示例和接入排错，
统一见 [供应商与模型配置指南](docs/provider-model-configuration.md)。

`lead`、`worker`、`plan_validator` 三段各自完整配置、互不继承；顶层不放任何模型字段
（写了会启动报错），计划审计必须开启。`provider` 是供应商，`api` 是请求协议，
命名供应商需显式指定 `api`。辅助模型有独立配置规则。
配置说明只维护上述文档，避免多份示例互相矛盾。

### 浏览器

默认浏览器请求格式是 `flat`：

```json
{
  "browser": {
    "agent_id": "abcp-agent",
    "ws_url": "ws://127.0.0.1:61168/ws",
    "jwt_token_env": "ABCP_JWT_TOKEN",
    "request_shape": "flat"
  }
}
```

`agent_id` 仅是 harness 的本地路由与日志标识。WebCross 的协议身份由
WebSocket 连接分配；配置该值既不能认证，也不能恢复 WebCross 会话。

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
    "similar_task_fleet_reuse_enabled": true,
    "similar_task_reuse_threshold": 0.78,
    "similar_task_running_stale_seconds": 86400,
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
    "context_file": null,
    "project_context_files": [
      {"path": "ORG_INSTRUCTIONS.md", "scope": "organization"},
      {"path": "CLAUDE.md", "scope": "project"}
    ],
    "append_system_prompt": ""
  }
}
```

- `lead_max_steps`: LeadAgent 最大决策轮数。
- `worker_max_steps`: BrowserAgent 最大执行轮数。
- `max_browser_agent_instances`: 可复用池长期保留的 BrowserAgent slot 目标数。idle slot 会保留 ABCP 连接和页面 registry；有效池容量至少等于 `max_browser_agents`。普通新 worker 只复用连接并从新页面开始；显式 continuation 才会复用旧页面候选。
- `max_browser_agents`: 同时运行的 browser worker 权威上限。
- `fleet_reuse_enabled`: 由协调器为 worker 确定性分配 fleet，并将无 fleetId 的 `Page.create` 收敛到该分配。通用任务可复用合格 slot 的 fleet；新建 `session_key` 或独立 worker 会开新 fleet。已有 Fleet 只可在原始用户任务中写作 `@<完整 UUID 或唯一前缀>`；运行时以权威库存解析，目标不存在或歧义时失败，绝不创建替代 Fleet。具名/隔离 fleet 不会进入通用复用池。
- `similar_task_fleet_reuse_enabled`: 未携带显式路由约束的任务只在第一次成功获取 fleet 前，用原始用户目标匹配当前 fleet memory 中由 harness 维护的可信任务索引。命中后只复用 fleet，并始终新建页面；resume、任务级 `@Fleet`、`session_key`、continuation 和阶段显式隔离始终优先。活动生命周期按顶层 task 与 worker 联合键控，避免同一 fleet 上一个 worker 完成时覆盖另一个仍在运行的 worker。终态历史按任务折叠并保留最近 12 个任务；running 身份最多保留 32 条 worker 记录，溢出部分压缩成一条向后兼容的合成 running fence。该 fence 不会早于所有被压缩的有限租约过期；不可可信时间戳或永不过期租约的溢出会永久 fail-closed。准入要求存在已完成的任务记录和可信版本的 fleet 级策略，且从未被具名会话、任务绑定或硬隔离永久阻断；无身份旧记录和外部 memory 不作为候选。`prepared` fleet 可由 readiness 自动唤醒，真正的 readiness 失败则不提交候选，并只降级一次到普通路由/创建。
- `similar_task_reuse_threshold`: `[0, 1]` 范围内的确定性归一化文本/字符 n-gram 排序阈值，默认 `0.78`。它不能单独授予复用：归一化任务还必须相同或构成高重合连续扩展，显式 URL origin 不得冲突，双方都有数字 token 时必须一致。复用的只是浏览器上下文，历史 page 和任务结果都不会被当作当前证据。
- `similar_task_running_stale_seconds`: `running` fleet-memory 记录的租约 TTL，默认 `86400`（24 小时）。通过相似任务复用进入 fleet 的 worker 必须在浏览器工作开始前先写入首条租约；符合自动复用条件的存活 worker 随后会在后台刷新租约，并在写终态之前先停止心跳。取消和异常路径仍会限时尽力写入终态。正数配置最小收敛到 `60` 秒，避免一次写入期间就过期并限制写放大；设为 `0` 表示不失效并永久 fail-closed。
- `same_fleet_multiworker_enabled`: 多 slot 共享 task/session fleet 的灰度开关，默认 `false`；启用后各 worker 使用独立 page，owner 连接保持不变，通知由 harness 中继，同 page 调用串行化。
- `max_task_fleets`: 单个任务最多占用的 fleet（浏览器实例）数，`0` 表示不限。harness 不会主动关闭 fleet，所以开出来的 fleet 会一直占着额度，直到平台的权威库存不再报告它——从 owner 库存消失的 fleet 会把额度释放回去。计数只统计绑定到本任务 worker 的 fleet，不看 Agent 全局的 `Fleet.list`。任务级 `@<id>`、已绑定的 `session_key`、`reuse_from_worker_id` 指向的 fleet 都计入额度；任务级引用仅在当前权威库存确认时才能使用。到达上限后，没指定 fleet 的 worker 自动复用本任务已有的 fleet（优先挑没有在跑的 worker 占着的那个），`worker_session_isolation_enabled` 的默认隔离让位于上限。只有两种情况没法这么服务，返回 `task_fleet_limit_reached` 回执：一是要求独立身份（显式声明 `needs_isolated_session` 或新开 `session_key`）；二是本任务的 fleet 全部绑给了具名会话——登录态的 cookie jar 不外借。这两种**等待都解不开**（harness 不关 fleet，worker 结束后 fleet 还在；具名会话的绑定也不随 worker 结束而释放），所以回执给的是：改用已有 fleet、走可信恢复流程释放 session binding、或调高上限。拒绝之前 cap 会强制重读一次权威 `Fleet.list`。dispatcher 是从整张 fleets 表作答、不按连接分域，所以一次成功的读取既能找到别的 slot 刚建的 fleet，也能退役任何已被平台回收的 fleet（不论原属哪个 slot）并把额度还回来。读取失败则一律不当作"消失"的证据。
- `fleet_auth_barrier_enabled`: 登录/验证码按 fleet 全域加门，非 resolver 有界等待且超时不放行。等待时间由 `fleet_auth_barrier_wait_seconds` 控制。
- `auth_fleet_ledger_path`: 持久化的非敏感已验证会话索引；重启回收的 fleet 在账本对账前不会进入通用复用池。
- `fleet_slot_reconnect_attempts`: 每轮恢复的有界重连次数。只有服务端返回的协议身份保持连续时，已认证的 fleet 绑定才可复用；transport 故障本身不等于 fleet 已丢失。
- `fleet_slot_reconnect_backoff_seconds`: 重连间隔的基础时间；不会重放失败的浏览器写操作。
- `fleet_slot_manual_reset_after_failures`: 连续恢复失败轮次达到该值后返回 `session_manual_reset_required`；只有 host/operator 能使用回执中的 fleet id 和 generation 显式重置。
- `hitl_poll_interval_seconds`: `Hitl.requestPause` 后轮询恢复状态的间隔。
- `hitl_wait_timeout_seconds`: 等待人工介入的最长时间。
- `worktree_dir`: 运行日志和 artifacts 的根目录。
- `context_file`: 兼容保留的单个静态 prompt 上下文文件。新配置优先使用 `project_context_files`。
- `project_context_files`: 有序的静态项目指令文件；每项可为路径字符串，或 `{ "path": "...", "scope": "..." }`。Harness 会按顺序以转义后的 `<project_context><project_instructions ...>` XML 注入；重复文件只注入一次。只应放稳定文件。
- `append_system_prompt`: 受部署方信任的静态 prompt 尾部，位于项目上下文之前并包装为转义后的 `<append_system_prompt>`。仅用于稳定策略补充，不可放任务事实。

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

交互启动后，先选择本次编排入口再输入任务：`/browser` 直达单个
BrowserAgent；`/lead` 走计划、并发与汇总。终端会确认本次选择，且不会修改
`config.json`。

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

静态 prompt 上下文字段默认关闭。最终 XML 会连同 guide manifest 一起进入完整 prompt 指纹，并记录在 usage diagnostics 中。任务期间保持稳定才能复用 prefix cache；不要把当前日期、cwd 或任务过程观察放进这些字段。

## Lead Agent 工具

`LeadAgent` 不直接操作浏览器。它通过以下工具做任务规划、派发和汇总：

- `spawn_browser_agent`: 启动一个隔离的 BrowserAgent。
- `wait_browser_agents`: 等待一个或多个 browser worker。
- `list_browser_agents`: 查看当前 worker 状态。
- `lead_save_artifact`: 基于可信 extraction 证据保存 LeadAgent 重塑后的结构化行。
- `read_harness_guide`: 当复杂回执或恢复规则相关时，从 prompt 内的轻量 guide 索引按页读取带版本的操作指南。
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
  -> spawn_browser_agent: 提交一个经过审查的 assignment
  -> wait_browser_agents: 返回 Worker 证据和剩余工作
  -> 校验 extraction artifacts 和 resultLevels
  -> lead_save_artifact: 仅在 schema_mismatch 且证据可信时重塑并保存
  -> 缺证/错证时修订 assignment，或用 phase_id 显式继续
  -> final_answer: 汇总成功、失败和阻塞项
```
