# ABCP Agent Harness

[English](README.md)

ABCP Agent Harness 将 LLM 的 tool calling 接到 ABCP Browser 的 WebSocket 能力上。Agent 不直接驱动 CDP、Playwright、截图识别或手写 selector，而是调用 `Page.navigate`、`DOM.getAXTree`、`Input.click` 等 ABCP method，并根据浏览器 observation 决定下一步。

## 环境要求

- Browser/Lead 需要 Python 3.9 或更高版本；Skill Builder 需要 Python 3.12 或更高版本。
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

首次配置时，将 [`config example.json`](config%20example.json) 复制为 `config.json`。
已有配置可继续使用。填写 `lead`、`worker`、`plan_validator` 的模型连接，
确认 `browser.ws_url` 指向已启动的浏览器服务。模型接入说明见
[供应商与模型配置指南](docs/provider-model-configuration.md)。

若填写了 `api_key_env`，在启动终端设置对应变量；例如示例中的计划审计模型凭据：

```bash
export PLAN_VALIDATOR_API_KEY="your-validator-key"
```

运行一个任务：

```bash
python main.py --task "打开 https://example.com 并总结页面标题和正文。"
```

CLI 会输出最终答案和任务 ID。运行日志、TODO 和复核证据默认保存在 SQLite：

```text
worktree/harness.db
```

交付文件使用任务实际返回的文件路径。

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

### Harness（可选）

`harness` 可以省略。首次使用无需设置内部阈值、复用或恢复参数；
默认值统一在 `runtime_config.py`，旧配置的显式覆写继续生效。
常用的使用选项如下，需要时再添加：

```json
{
  "harness": {
    "lead_max_steps": 20,
    "worker_max_steps": 30,
    "max_browser_agents": 3,
    "max_task_fleets": 3,
    "hitl_wait_timeout_seconds": 600,
    "worktree_dir": "worktree"
  }
}
```

- `lead_max_steps` / `worker_max_steps`：Lead 的决策轮数及其委派 worker 的轮数上限；独立 `/browser` 不受这两个轮数限制。
- `max_browser_agents` / `max_task_fleets`：并行 worker 数和单项任务的浏览器实例数。
- `hitl_wait_timeout_seconds`：等待人工协助的最长时间。
- `worktree_dir`：SQLite 数据库和交付文件的根目录。

独立 `/browser` 自动维护持久 TODO，并在清单变化、主动请求复核和提交完成时进行独立证据复核。
任务完成必须通过最终复核。无需配置清单或复核的内部参数。
部署策略、项目指令及详细参数见 [Harness 高级配置](docs/harness-advanced-configuration.md)。

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

按 phase 粒度恢复原任务，也可以在同一任务 ID 下追加用户指令：

```bash
python main.py --resume worktree/<task_id> --task "补充指令"
```

交互提示符中的等价写法是 `/resume <任务目录> [补充指令]`；省略补充指令则按原任务恢复。
原始请求保留在任务 manifest，追加文本作为带来源的用户输入记录。Browser 模式在原阶段
仍可继续时沿用该阶段；阶段已结束时新建后续阶段，保留旧回执，不自动重放旧提交。
启动失败前已保存的补充指令仍会在下次普通 resume 时接续；补充指令不能将现有任务改绑到
不同的显式 `@Fleet`。Lead 模式由 Lead 判断沿用、追加或修订 assignment，新增 assignment
仍经复核和所需批准。已经
`validated_done` 的 phase 及其当前有效 artifact 会保留；中断阶段重新执行前应核验已产生的
外部结果。若进程停止时某个 phase 正在运行，重跑前必须由用户确认；
非交互模式需显式传入 `--resume-retry-interrupted`。历史 Fleet/page 仅作为
当前任务拥有的弱恢复候选，必须重新通过浏览器 inventory 验证；失效时回落
到普通路由。

DB 模式根据 SQLite 任务记录恢复，不要求实体任务目录存在。计划、状态记录被删除或
损坏时，resume 会严格失败，不会静默创建空任务。文件模式仍要求原任务目录存在。

## 日志与 Artifacts

Browser 和 Lead 的任务记录均保存在 SQLite。DB 模式不预建任务目录或日志子目录；
运行锁存于公共控制目录，只有下载、截图、实际本地中间文件或交付文件需要时才创建任务目录：

```text
worktree/harness.db
worktree/.run-locks/<task_id>/owner.json  # 仅运行期间存在
worktree/<task_id>/...                  # 按需创建的实际文件
```

任务事件是 SQLite 记录；任务内读取工具可通过虚拟 `run.jsonl` 视图查看。常见事件类型包括：

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
