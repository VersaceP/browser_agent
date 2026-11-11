# ABCP Agent Harness

[English](README.md)

ABCP Agent Harness 将 LLM 的 tool calling 接到 ABCP Browser 的 WebSocket 能力上。Agent 不直接驱动 CDP、Playwright、截图识别或手写 selector，而是调用 `Page.navigate`、`DOM.getAXTree`、`Input.click` 等 ABCP method，并根据浏览器 observation 决定下一步。

## 环境要求

- Browser 需要 Python 3.9 或更高版本；Workflow 模式在 Python 3.13 上测试。
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
填写 `worker` 及可选 `workflow_agent` 的模型连接，
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

`worker` 及显式配置的 `workflow_agent` 各自填写完整连接；未配置后者时，
Workflow 的各 Agent 复用 `worker`。顶层不放任何模型字段（写了会启动报错）。`provider` 是供应商，`api` 是请求协议，
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
    "reviewer_enabled": true,
    "worker_max_steps": 30,
    "max_browser_agents": 3,
    "max_task_fleets": 3,
    "worktree_dir": "worktree"
  }
}
```

- `reviewer_enabled`：Browser 任务的独立语义复核开关，默认 `true`。设为 `false` 后由任务执行者直接判断是否结束，结果记录跳过复核；权限、协议和程序验证仍然生效。
- `worker_max_steps`：委派 Browser worker 的轮数上限；独立 `/browser` 不受此轮数限制。
- `max_browser_agents` / `max_task_fleets`：并行 worker 数和单项任务的浏览器实例数。
- `worktree_dir`：SQLite 数据库和交付文件的根目录。

HITL 提问没有累计次数或自动等待超时，回答和运行中引导没有固定字符上限。
问题身份、显式 attendance、取消和权限校验继续保留。

Browser 和 Workflow 共用本地文件授权，由 `harness.local_access_auto_approve` 控制，默认 `true`：
本地读写和 Workflow 的 Fleet 使用自动放行，不再询问，每次放行仍写审计事件。
受保护位置始终拒绝：WebCross 安装包与其数据、运行目录（`/Applications/WebCross.app`、
`~/Library/Application Support/webcross`、`~/.webcross`、`~/.local/bin/webcross`），`.ssh`、`.env`、`.git`、
`credentials` 等敏感名称，Harness 安装目录、其它任务目录，以及 `protected_local_roots` 中追加的路径。
设为 `false` 时恢复逐项确认：模型通过 `local_fs_authorize` 一次申请材料目录或一批文件，读取和写入分别确认；
终端回复 `/answer <问题编号> yes` 批准列出的全部范围；如果显示了目录选项，
回复 `/answer <问题编号> directory` 批准该确切目录及其子目录，`no` 拒绝。
目录权限在本任务恢复后继续有效。
引用历史来源任务不会自动继承其权限。上传仍须传入实际文件清单。
已运行的 Python 进程不会热加载源码修改；升级后重启 Harness，再 `/resume <任务ID>`。

独立 `/browser` 使用持久 TODO 记录环节进展、依据和下一步安排，保存清单不触发复核。
开启 reviewer 时，Browser 在必要资料核实、初步方案形成后通过 `request_goal_review` 请求一次计划复核，
执行中按具体疑问主动请求复核，提交完成时进行最终证据复核。
默认情况下，任务完成必须通过最终复核。`reviewer_enabled: false` 保留 TODO，
关闭进度、主动请求和最终复核。修改配置后需重启 Harness；恢复的任务也使用当前配置。
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

交互启动后，输入 `/browser` 直达单个 BrowserAgent，或直接输入 `/workflow` 命令。
本次选择不会修改 `config.json`。

CLI 的固定提示、帮助和控制词统一使用英文。任务正文和原始证据可继续使用原语言。

### Workflow 模式

Workflow 模式把用户目标做成可复用的 WebCross Workshop 包，并在复用时处理参数、
执行证据和必要的修复。包执行已经明确的操作并返回结果事实；Agent 判断事实意味着什么、
下一步怎么办；Harness 保证每次执行对应正确的目标、版本、页面与权限。

```text
/workflow                                查看 WebCross Workshop
/workflow <名称|workshop-id> [需求]        查看或运行选定远端版本
/workflow-drafts                         查看本地开发稿
/workflow-create <需求>                   新建本地 draft
/workflow-edit <draft|workshop-id|名称> <需求> 编辑本地稿或检出远端版本
/workflow-rename <draft> <名称>           本地改名，下次 publish 同步远端
/workflow-delete <draft> [--yes]          删除本地工作文件，保留历史
/workflow-publish <draft> [--workshop <id>] 打包并上传／更新 Workshop
/workflow-unpublish <名称|workshop-id>    下架远端记录，保留本地稿和源码历史
/workflow-stop [任务ID|批次ID|all]         停止任务并取消其 WebCross 执行
/resume <任务ID> [补充]                   续跑原任务及固定版本
```

WebCross Workshop 是唯一可执行市场。`/workflow` 先解析远端身份和完整 manifest，
再固定版本并施加页面、文件与能力范围；不会自动改为运行未上传的本地稿。
本地作者文件统一在 `workflows/<slug>/`，包含 `draft.json`、
`history/versions/<hash>/`、文本差异和 `history/changes.jsonl`，用于开发与版本追踪。
`/workflow-publish` 调用官方 CLI 打包，再通过 WebCross `Workflow.upsert` 上传；
`/workflow-unpublish` 对应 `Workflow.remove`。当前 CLI 只接受 JSON 字面量参数，
大包通过已配置的原生连接传输，避免命令行参数长度限制。上传与下架均读取远端结果核验，
结果未知时不自动重放。更新前核对远端 revision/hash；平台没有条件更新参数，不能将读回说成原子 CAS。

Browser 可通过 `browser_call` 调用 `Workflow.execute({workshopId,input,binding})`：
Harness 读取完整 manifest，固定 revision/source hash，经权限包装后执行源码快照。
返回的 `definitionRef/definitionHash` 可复用同一任务快照；Browser/run 无权 upsert/remove 远端记录。
每个任务固定使用启动时的版本；每个运行任务绑定一个页面，
包只作用于该页面，新标签页由 Agent 在 Harness 核对来源后绑定。验收要点由 Agent 从原始
目标形成，Harness 检查引用与身份，Agent 判断业务是否满足。单项直接进入运行 Agent；独立业务项才按需拆分并发。运行时仅可委派只读观察，使用包已有的恢复入口；没有可用恢复路径时交付部分结果、剩余身份和证据。Browser 补完已停用，包修复交给 `/workflow-edit`。历史补完回执仍可读取，其 Browser 子任务不能继续恢复；独立 Browser 任务与作者探索保留原有能力。
Workflow 记录四类 token、模型轮次、工具与平台 I/O，以及净时长（扣除可观测的人工等待，保留子任务执行时间），
不设 token／时长上限，不询问追加预算。恢复任务时保留历史用量，旧 `harness.workflow_budget` 配置不再生效。详见[实施计划](docs/tau-skill-builder-agent-loop-plan.md)。

本地稿保留独立作者任务、上下文与用量；`/workflow-edit` 和 `/resume` 沿用同一任务。
远端工作流没有本地稿时先检出为本地稿。删除本地工作文件保留历史，不删除远端记录；
下架远端前保留准确源码，并保留尚未上传的本地修改。历史运行使用当时固定的快照；
已清理的旧作者分支应从当前开发稿继续，不能用最新远端源码伪造历史。
详见 [Workshop 迁移说明](docs/workshop-only-20261110.md)。

`/workflow-create` 先明确包的复用场景，业务范围不清楚时在实现前询问用户。新草稿包含
`coverage.json`，记录范围内控件、前置条件、选项、依赖和代表性场景，包括首个样例没用到的
可选分支，并区分已发现、已记录、已实现、已验证。动态候选保留读取与刷新条件；运行逻辑
写入 `src/`，作者清单不会自动嵌入导出的 Workshop。旧包未提供清单时仍可使用。

通用编写约定是：将有依据的确定性要求编入包，一次本地预检汇总独立缺项，
让正常操作和读回在包内连续执行。新草稿含 `src/preflight.js` 纯检查入口；`validate_input`
批量检查完整输入及显式素材清单，保留 unknown 和完整回执，不新增站点业务门禁。
详见[创建与复用约定](docs/workflow-authoring-contract.md)和[阶段、报告与运行协议](docs/workflow-runtime-refactor-20261102.md)。

Dispatcher 断线时，Browser 和 Workflow 共用默认 60 秒的自动重连窗口。
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

Browser 和 Workflow 的任务记录均保存在 SQLite。DB 模式不预建任务目录或日志子目录；
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

`/browser` 通过 BrowserTaskRunner 启动 BrowserAgent；`/workflow*` 命令在共享 Tau
循环上启动 Workflow 任务、作者或调度 Agent（`harness/workflow_mode/`）。两者共用存储、
权限、运行中引导和传输恢复。

Lead 与 Skill 模式已删除。旧配置的 `skill_orchestrator` 模型段改名为 `workflow_agent`；
`harness.agent_mode` 只接受 `browser` 或 `workflow`。历史 Lead、Skill 记录保留，
但无法通过已删除的运行时恢复。
