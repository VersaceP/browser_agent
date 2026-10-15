# ABCP Agent Harness

[English](README.md)

ABCP Agent Harness 将 LLM 的 tool calling 接到 ABCP Browser 的 WebSocket 能力上。Agent 不直接驱动 CDP、Playwright、截图识别或手写 selector，而是调用 `Page.navigate`、`DOM.getAXTree`、`Input.click` 等 ABCP method，并根据浏览器 observation 决定下一步。

## 环境要求

- Browser 需要 Python 3.9 或更高版本；Skill Builder 需要 Python 3.12 或更高版本。
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
填写 `worker` 及可选 `skill_orchestrator` 的模型连接，
确认 `browser.ws_url` 指向已启动的浏览器服务。模型接入说明见
[供应商与模型配置指南](docs/provider-model-configuration.md)。

若填写了 `api_key_env`，在启动终端设置对应变量；例如 Browser 模型凭据：

```bash
export WORKER_API_KEY="your-worker-key"
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

`worker` 及显式配置的 `skill_orchestrator` 各自填写完整连接；未配置后者时，
Skill 和 Workflow 主 Agent 复用 `worker`。顶层不放任何模型字段（写了会启动报错）。`provider` 是供应商，`api` 是请求协议，
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
    "worker_max_steps": 30,
    "max_browser_agents": 3,
    "max_task_fleets": 3,
    "worktree_dir": "worktree"
  }
}
```

- `worker_max_steps`：委派 Browser worker 的轮数上限；独立 `/browser` 不受此轮数限制。
- `max_browser_agents` / `max_task_fleets`：并行 worker 数和单项任务的浏览器实例数。
- `worktree_dir`：SQLite 数据库和交付文件的根目录。

HITL 提问没有累计次数或自动等待超时，回答和运行中引导没有固定字符上限。
旧 `hitl_wait_timeout_seconds`、`hitl_max_pause_rounds_per_page/worker` 配置兼容读取但不再生效；
问题身份、显式 attendance、取消和权限校验继续保留。

Browser、Workflow、Skill 运行时和 Skill Builder 共用本地文件授权。
模型可通过 `local_fs_authorize` 一次申请材料目录或一批文件；读取和写入分别确认。
终端回复 `/answer <问题编号> yes` 批准列出的全部范围；如果显示了目录选项，
回复 `/answer <问题编号> directory` 批准该确切目录及其子目录，`no` 拒绝。
目录权限在本任务恢复后继续有效，并可供本任务的 Browser 和所属只读试运行复用。
引用历史来源任务不会自动继承其权限。上传仍须传入实际文件清单。
已运行的 Python 进程不会热加载源码修改；升级后重启 Harness，再 `/resume <任务ID>`。

独立 `/browser` 自动维护持久 TODO，并在清单变化、主动请求复核和提交完成时进行独立证据复核。
任务完成必须通过最终复核。无需配置清单或复核的内部参数。
部署策略、项目指令及详细参数见 [Harness 高级配置](docs/harness-advanced-configuration.md)。

## 运行任务

运行独立 Browser 任务（默认模式）：

```bash
python main.py --task "打开 https://example.com 并总结页面。"
```

从 stdin 读取任务：

```bash
echo "打开 https://example.com 并总结页面。" | python main.py
```

覆盖 agent id 或最大步数：

```bash
python main.py --agent-id demo-agent --task "检查当前 Fleet 列表。"
```

交互启动后，先选择本次编排入口再输入任务：`/browser` 直达单个
BrowserAgent；`/workflow` 开发并执行可复用程序；`/skill` 进入独立 Skill 编排及命令区。终端会确认本次选择，且不会修改
`config.json`。

CLI 的固定提示、帮助和控制词统一使用英文。任务正文和原始证据可继续使用原语言。
Skill 名称支持不区分大小写的包含匹配，精确 ID 和精确名称优先。
唯一匹配自动选择；多个匹配列出全部名称和 ID，等待用户精确指定。
`/skill` 显示正式 Skill 和草稿，随后使用简短的 `[skill]>` 提示。

保留的操作为 `/<Skill名称或ID> [@FleetID] [需求]`、
`/skill-create [Skill名称或ID] @<任务ID> [要求]`、`/skill-publish <名称或ID>`、
`/skill-delete <名称或ID>`、`/resume <任务ID> [补充要求]`。
创建和恢复的任务 ID 必填；Fleet 可省略。`/skill-create` 不带名称时新建，
带名称时根据指定任务证据更新该 Skill，生成草稿后另行发布。发布和删除共用名称解析，
同一个 Skill 有多个草稿或删除版本时展示版本供交互选择，不按时间默选；
内容哈希和版本冲突由程序内部复核。`/resume` 自动按保存的模式恢复执行任务，也可继续 Builder 草稿，无需先切换模式。
`/skill`、`/browser`、`/workflow` 保留为模式导航。
详细用法见 [Skill 命令说明](skills/README.md)。

Skill 模式与 Builder 要求 Python 3.12+。执行已发布 Skill 时启动独立 Orchestrator：
绑定本轮变量，由 Harness 连续运行已决定的 JS/TS Workflow，
在判断点或异常处交回模型。Browser 处理有界判断及恢复；程序缺陷记录后通过
`/resume <Builder任务ID> 根据 @<执行任务ID> 优化` 另行处理。
复用不启动后台重放或 Coding 修复循环；发布仍需用户命令，业务完成经过独立审查。
当前不接入共享 Python/Shell。可选顶层 `skill_orchestrator` 单独配置模型；
未配置时复用 `worker` 连接。

`/workflow [@<Fleet ID> 需求]` 由主 Agent 直接编写、运行任务内文件并读取回执；
同一个主 Agent 在独立测试页验证当前源码，测试检查点与主页面隔离。Browser 可承担有界语义操作或恢复，
业务交付和程序验证分别审查。通过 `/skill-create @<Workflow任务ID> 需求` 将确切源码及
执行证据导入 Builder 草稿，用户发布后由 `/skill` 换变量复用。开发、恢复与验收边界见
[实施计划](docs/tau-skill-builder-agent-loop-plan.md)。

Dispatcher 断线时，Browser、Workflow 和 Skill 共用默认 60 秒的自动重连窗口。
恢复期间保留连接身份、事件订阅和任务上下文；已发送但丢失回执的动作返回“结果未知”，不会自动重放。
Workflow 会保存中断阶段，并在已知原 `workflowId` 时查询其状态；状态查询不能代替丢失的执行输出。
重连超时或身份、协议校验失败后，保留任务记录供 `/resume <task_id>` 恢复。
这个窗口不限制工作流执行时长或 HITL 人工回答时间。可在 `browser.reconnect_timeout_seconds`
配置 0–60 秒（默认 60，0 表示不自动等待重连）。

按 phase 粒度恢复原任务，也可以在同一任务 ID 下追加用户指令：

```bash
python main.py --resume <task_id> --task "补充指令"
```

交互提示符中的等价写法是 `/resume <task_id> [补充指令]`；完整 ID 可带 `@` 前缀，
旧任务目录路径仍兼容。省略补充指令则按原任务恢复。
若任务保存了传输阻塞状态，首次浏览器派发前会重新探测连接、能力与 Fleet 清单；
成功后更新阻塞状态，失败则保留具体原因和任务身份。探测不会重放断线时的浏览器操作。
原始请求保留在任务 manifest，追加文本作为带来源的用户输入记录。Browser 模式在原阶段
仍可继续时沿用该阶段；阶段已结束时新建后续阶段，保留旧回执，不自动重放旧提交。
启动失败前已保存的补充指令仍会在下次普通 resume 时接续；补充指令不能将现有任务改绑到
不同的显式 `@Fleet`。已经
`validated_done` 的 phase 及其当前有效 artifact 会保留；中断阶段重新执行前应核验已产生的
外部结果。若进程停止时某个 phase 正在运行，重跑前必须由用户确认；
非交互模式需显式传入 `--resume-retry-interrupted`。历史 Fleet/page 仅作为
当前任务拥有的弱恢复候选，必须重新通过浏览器 inventory 验证；失效时回落
到普通路由。

DB 模式根据 SQLite 任务记录恢复，不要求实体任务目录存在。计划、状态记录被删除或
损坏时，resume 会严格失败，不会静默创建空任务。文件模式仍要求原任务目录存在。

## 日志与 Artifacts

Browser、Skill 和 Workflow 的任务记录均保存在 SQLite。DB 模式不预建任务目录或日志子目录；
运行锁存于公共控制目录，只有下载、截图、实际本地中间文件或交付文件需要时才创建任务目录：

```text
worktree/harness.db
worktree/.run-locks/<task_id>/owner.json  # 仅运行期间存在
worktree/<task_id>/...                  # 按需创建的实际文件
```

任务事件是 SQLite 记录；任务内读取工具可通过虚拟 `run.jsonl` 视图查看。常见事件类型包括：

- `agent.model`: 模型文本和 tool calls。
- `browser.call.result`: ABCP method 调用结果。
- `llm.usage`: 单次 LLM 调用的 token 与 prompt cache 指标。
- `llm.usage_summary`: 任务级 token 与 prompt cache 汇总。
- `agent.final`: 最终答案。

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

## Browser 工具

BrowserAgent 使用 `browser_call` 调用已声明的平台能力：

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

## 运行入口

`/browser` 通过 BrowserTaskRunner 启动 BrowserAgent；`/skill` 启动独立
Skill Orchestrator；`/workflow` 进入 Workflow 开发。三者共用浏览器工具、租约、
权限、存储、证据及恢复模块。

Lead 模式已删除。旧配置的 `lead` 模型连接应迁到 `skill_orchestrator`，保持
Skill／Workflow 的模型选择；移除 `plan_validator`、`claim_extractor` 和
Lead 专属 harness 参数，模式选择 `browser`、`skill` 或 `workflow`。历史
Lead 记录保留，但无法继续通过已删除的运行时恢复。
