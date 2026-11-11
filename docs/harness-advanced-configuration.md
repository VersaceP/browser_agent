# Harness 高级配置 / Advanced configuration

首次使用只需配置模型连接和浏览器连接。内部阈值、复用与恢复参数由
[`HarnessConfig`](../runtime_config.py) 提供默认值，Browser 与 Workflow 共用。
需要部署调优时，仍可在 `config.json` 的 `harness` 段显式覆写；旧配置继续生效。

For initial setup, configure the models and browser connection. Internal tuning
uses `HarnessConfig` defaults for Browser and Workflow. Explicit `harness`
overrides remain supported, including existing configurations.

## 内置默认值 / Internal defaults

模型连接段（`worker` / `workflow_agent`）的短期 429 重试独立于超时重试：
`llm_rate_limit_max_retries` 默认 **3**（设为 0 可关闭），
`llm_rate_limit_backoff_seconds` 默认 **5**，无服务端时间提示时指数退避并增加
最多 20% 的随机抖动；`llm_rate_limit_max_wait_seconds` 默认 **90**，限制单次
模型调用内累计等待。优先遵守 `Retry-After` 秒数/HTTP 日期及可解析的 reset 时间；
若服务端要求的等待超过剩余预算，保留回执并退出，不缩短等待后抢跑。
正常请求及流解码后的非流回退共享此限流预算。额度耗尽不自动重试。
等待可以取消，且只重发当前模型请求，不重放浏览器操作。
失败请求的 request ID、原因、等待与尝试记录在回执中；缺少 usage 时不推算 token。

Model sections use a separate bounded HTTP 429 budget: `llm_rate_limit_max_retries`
defaults to **3**, `llm_rate_limit_backoff_seconds` to **5**, and
`llm_rate_limit_max_wait_seconds` to **90** cumulative waiting seconds per model
call. Without a provider hint, backoff doubles with up to 20% jitter. Retry-After
seconds/HTTP dates and unambiguous reset times take precedence. Delays beyond the
remaining budget stop recovery rather than being shortened. Normal requests and
the reserved non-stream fallback share the budget. Exhausted quotas are not
retried. Cancellation remains available; browser actions are never replayed.
Receipts preserve attempt IDs, delays and errors without inventing missing usage.

`browser.reconnect_timeout_seconds` 默认 **60**，允许 0–60 秒。原生连接的启动、
运行中断线、Workflow 执行器和 HITL 通知读取共用恢复窗口；所有重连尝试、退避、
注册、事件回放及会话清单查询共享截止时间。每次协议尝试最多 5 秒。
并发调用共享原连接的恢复任务，同一任务的连接槽共享故障截止时间；已经耗尽的
恢复回执不会再触发一轮自动探测。显式恢复或新进程可开始新的恢复窗口。
只有传输暂时不可用才重试；认证拒绝、协议身份不一致、事件游标缺口等返回结构化阻塞。
这些校验只确认协议与身份连续性；缺失 Fleet/Page 作为事实返回，不自动创建替代资源。
原身份无法恢复时可在权限允许的前提下通过显式 `/resume` 重新建立任务会话。
任何已发送的浏览器操作或 `Workflow.execute` 都不会因为重连而重放。

`browser.reconnect_timeout_seconds` defaults to **60** (range 0–60). Native
startup, active Browser/Workflow execution and HITL notification readers use the
same recovery mechanism. Attempts, backoff, registration, event replay and session
inventory share one outage deadline; each protocol attempt is capped at 5 seconds.
Calls on one connection share a recovery task, and a task's slots share the
deadline. An exhausted live recovery is not followed by another automatic probe.
Explicit recovery or a new process may start a new window. Authentication,
identity and event-cursor failures remain explicit blockers. Missing targets are
reported as facts; no replacement resources or business actions are dispatched.
Workflow lifecycle status is recorded separately from missing execution output.

| 配置项 / Option | 默认值 / Default | 说明 / Meaning |
| --- | --- | --- |
| `reviewer_enabled` | `true` | Browser 任务的独立语义复核；关闭后由任务执行者判断结束，保留权限、协议和程序验证。 / Independent semantic review for Browser tasks; disabling it leaves completion to the task owner while retaining permission, protocol and program validation. |
| `similar_task_fleet_reuse_enabled` | `true` | 为相似任务尝试复用合格 Fleet，显式会话/隔离/恢复路由优先。 / Attempt eligible Fleet reuse; explicit routing takes precedence. |
| `similar_task_reuse_threshold` | `0.78` | 相似任务候选阈值。 / Similar-task candidate threshold. |
| `similar_task_running_stale_seconds` | `86400` | 正在运行的复用记录租约为 24 小时，存活 worker 刷新租约。 / Running reuse lease lasts 24 hours and is refreshed by live workers. |
| `cache_pressure_uncached_input_threshold` | `40000` | 单轮 uncached_input 超过此值才计入连续缓存压力。 / Count pressure only above this per-turn uncached input. |
| `cache_pressure_consecutive_steps` | `3` | 连续满足缓存压力条件的轮数；重建缓存造成的未命中不计入。 / Consecutive pressure turns; misses caused by prefix rebuilding are excluded. |
| `auto_intercept` | `"suggest"` | 默认仅建议处理遮挡；`p0` / `p0p1` 允许对应等级自动处理，`off` 关闭提示。 / Suggest handling by default; `p0` / `p0p1` enable automatic handling at those levels, and `off` disables suggestions. |
| `hitl_attendance` | `"attended"` | 默认等待人工回复；`unattended` 直接返回 needs_human，不暂停等待。 / Wait for a human by default; unattended mode returns needs_human without waiting. |

`auto_intercept` 与 `hitl_attendance` 是部署行为选择，不能从平台自动推断。
清理旧配置时需保留非默认覆写；例如原有 `auto_intercept: "p0p1"` 删除后会变成
仅建议处理。缓存压力默认值已采用现有部署的 `40000` / `3`，无需再重复填写。
缓存压力不是任务总预算，也不改变上下文容量或容量压缩阈值。

These two policy options are deployment choices. Keep non-default overrides when
cleaning an existing configuration. In particular, removing `auto_intercept: "p0p1"`
changes automatic handling into suggestions. Cache pressure defaults now use
`40000` / `3`; this is neither a task budget nor a change to the context window
or its compaction ratio.

## 中文参考

可选的高级配置示例：

```json
{
  "harness": {
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

- `worker_max_steps`: 委派 BrowserAgent 最大执行轮数；独立 `/browser` 任务不受此轮数限制，以显式完成或实际阻塞为终态。
- 独立 `/browser` 在任务逻辑路径 `scratchpad/todo.md` 中记录阶段进展、证据引用和下一步执行方式，默认存于 SQLite。保存待办不触发复核；开启复核时，Browser 读完所需资料后通过 `request_goal_review` 请求初始计划复核，后续针对具体疑点或实质性的计划、证据变化请求复核。`final_answer(done)` 触发最终证据复核，通过后才结束任务。`ready_for_final_review` 只是保存时的提示，不安排延后的进度复核。续跑和压缩保留既往复核版本与结论，不因恢复上下文而安排复核。复核器可读取授权文件和任务页面观察结果，但不能修改页面。
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
- `fleet_slot_reconnect_attempts`: 旧式或外部注入连接槽的恢复次数；原生连接使用上述 60 秒窗口，不叠加重试预算。
- `fleet_slot_reconnect_backoff_seconds`: 旧式连接槽的重连基础间隔；原生连接从 0.5 秒退避至最多 4 秒。
- `fleet_slot_manual_reset_after_failures`: 连续恢复失败轮次达到该值后返回 `session_manual_reset_required`；只有 host/operator 能使用回执中的 fleet id 和 generation 显式重置。
- `hitl_poll_interval_seconds`: HITL 解除后的页面状态确认间隔；等待人工回复本身由通知或终端输入驱动。
- HITL 等待人工回复不限时、不按累计次数拒绝提问，回答及运行中引导不设固定字符上限。协议请求和回复后的页面状态确认仍有界。
- 验证码关键词、隐藏控件和嵌入组件仅提供绑定页面及观测版本的事实，由 BrowserAgent 结合原始任务、动作回执和用户反馈决定是否调用 `Hitl.requestPause`。此链路不调用专属 VL 或自动解题，也不因组件仍存在而自动再次暂停。
- 仅终端人工回复或匹配的 `Hitl.resumed` 通知交回控制；页面加载、标题变化和视觉判断不会自动解除暂停。恢复后的页面暂停状态及 Fleet 身份检查保留，业务是否可继续交由模型判断。旧的 `hitl_no_repause_cooldown_seconds`、`hitl_post_resume_guard_seconds` 设置已移除；`hitl_post_resume_confirm_max_rounds` 继续限定回复后的只读页面就绪检查。
- `worktree_dir`: 运行日志和 artifacts 的根目录。
- `context_file`: 兼容保留的单个静态 prompt 上下文文件。新配置优先使用 `project_context_files`。
- `project_context_files`: 有序的静态项目指令文件；每项可为路径字符串，或 `{ "path": "...", "scope": "..." }`。Harness 会按顺序以转义后的 `<project_context><project_instructions ...>` XML 注入；重复文件只注入一次。只应放稳定文件。
- `append_system_prompt`: 受部署方信任的静态 prompt 尾部，位于项目上下文之前并包装为转义后的 `<append_system_prompt>`。仅用于稳定策略补充，不可放任务事实。

## English reference

Optional advanced configuration example:

```json
{
  "harness": {
    "reviewer_enabled": true,
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

- `local_access_auto_approve`: default `true`. Local READ/WRITE outside the task and the Workflow Fleet confirmation are granted without asking; each grant is still logged (`local_path.authorization.granted` with `selection: auto`, `workflow.fleet.authorization`). Protected locations are denied either way: the WebCross installation, data and runtime directories (`/Applications/WebCross.app`, `~/Library/Application Support/webcross`, `~/.webcross`, `~/.local/bin/webcross`), sensitive names (`.ssh`, `.env`, `.git`, `credentials`, ...), the Harness installation, other tasks, and any `protected_local_roots`. `false` restores per-scope terminal confirmation. Workflow packages may call `Download.start`, `DOM.getImg` (file output), `Download.list/control`, `File.write`, `File.handleChooser` and `Network.readApi/readBody` without declaring them; write paths pass the same rules inside the sandbox. Only `Runtime.evaluate` needs a declaration.
- `workflow_parallelism`: default `2`, positive integer. Bounds WorkflowPlatform executions for one endpoint in the current process/event loop. `execute_workflow_batch` reuses one Agent context, explicit work-item inputs and owned receipt slices; same-page executions serialize. Shared resource keys are explicit caller declarations. Native WebCross capacity still applies; other processes and Browser workers are outside this pool. Set to `1` for serial comparison. See [concurrency and recovery](workflow-concurrency-20261110.md).
- `workflow_budget`: per-task cost guard for `/workflow*` sessions, default `{"run": {"tokens": 300000, "net_minutes": 20}, "author": {"tokens": 1500000, "net_minutes": 60}}`. Tokens count `uncached_input + cache_write + output` (cache_read is reported, not charged); net minutes exclude waiting for the user or HITL. On exhaustion the agent stops starting model turns after the running execution returns and asks the user to extend; non-interactive runs stop with `budget_exhausted`.
- Workflow sessions expose `local_fs_batch` and `run_local` directly. Run/coordinator profiles accept only fixed local operations (`media_info`, `convert_media`) with data paths and a format; arbitrary commands/scripts are available only to the author profile. The shared macOS process backend enforces file scopes, denies network and package/control writes, and preserves receipts, timeout, cancellation and task stop. Run-mode Browser delegation is observation-only on the bound page; author exploration retains Browser actions. Completion is not an available mode or configuration switch. Historical completion child tasks cannot resume through /browser; resume their parent for supported package recovery. Existing completion receipts remain evidence. See [local processing and delivery](workflow-authoring-contract.md#主-agent-的本地处理与交付) for arguments and boundaries. Restart an already-running Harness process to load these changes; published Workshop packages and WebCross's exports policy are unchanged.
- `worker_max_steps`: maximum rounds for a BrowserAgent delegated by a task owner. A standalone `/browser` task has no step limit and ends on an explicit completion or real blocker.
- `reviewer_enabled`: defaults to `true` for Browser tasks and resumed tasks. With `false`, Browser keeps its TODO but does not run progress/final review or offer `request_goal_review`. Workflow tasks use their own acceptance checks. Endings record `review.status=skipped` and `reason=reviewer_disabled`, with no fabricated verdict or review approval reference. Permission, identity, protocol, structure/consistency and program validation still apply. Use JSON booleans, not strings. Restart the Harness to load a changed setting; setting it back to `true` restores review and retains historical evidence.
- Standalone `/browser` maintains stage progress, evidence references and next execution steps at the logical task path `scratchpad/todo.md`, stored in SQLite by default. Saves do not trigger review. With review enabled, Browser requests an initial plan check through `request_goal_review` after inspecting needed sources; further progress checks are requested for concrete concerns or material plan/evidence changes. `final_answer(done)` triggers final evidence review and ends only after verification. `ready_for_final_review` is a save-time hint, not a deferred progress-review trigger. Resume/compaction retain prior review versions and findings without scheduling review merely for restoration. The reviewer can read authorized files and task-bound page observations but cannot change the page.
- `max_browser_agent_instances`: target number of live BrowserAgent slots kept in the reusable pool. Idle slots keep their ABCP connection and page registry. The effective pool is raised to at least `max_browser_agents`.
- `max_browser_agents`: authoritative maximum number of concurrently running browser workers.
- `fleet_reuse_enabled`: deterministically assign each worker a fleet and force `Page.create` into it. Generic work may reuse an eligible slot fleet; a new `session_key` or isolated worker gets a fresh fleet. An existing Fleet is bound only when the original user task contains `@<full UUID or unique prefix>`; the runtime resolves it from authoritative inventory and never creates a replacement. Named/isolated fleets never become the generic slot default. Lost named sessions fail with `session_fleet_lost` instead of silently rebinding. Model-initiated `Fleet.create`/`Fleet.close` and out-of-assignment fleet ids fail closed; explicit page continuations may receive prior page candidates.
- `similar_task_fleet_reuse_enabled`: before the first successful Fleet acquisition of an unconstrained task, compare the original user objective with the trusted harness-owned task index in current Fleet memory. A match reuses the Fleet only and always opens a fresh page. Resume, task-level `@Fleet` routing, session/continuation routing, plus phase-declared isolation, take precedence. Active lifecycle is keyed by top-level task plus worker so one worker cannot mark a Fleet completed while another worker still uses it. Terminal history collapses by task and keeps the latest 12 tasks. Running identity keeps at most 32 worker records plus one backward-compatible synthetic running fence for overflow; the fence cannot release before every omitted finite lease expires, and untrustworthy or non-expiring overflow remains permanently fail-closed. Admission requires a completed task record and a versioned Fleet-level policy that has never been blocked by named-session, task-bound, or hard-isolation use; identity-free legacy history and foreign memory are not candidates. A stopped `prepared` Fleet may auto-wake through readiness, while a real readiness failure falls back once to ordinary routing/Fleet creation without committing the candidate.
- `similar_task_reuse_threshold`: deterministic normalized text/character-ngram ranking threshold in `[0, 1]`, default `0.78`. It cannot grant reuse by itself: normalized tasks must first be equal or a high-overlap contiguous extension, explicit URL origins must not conflict, and numeric tokens must agree when both tasks contain them. Reuse transfers browser context only—prior pages and results are never accepted as current evidence.
- `similar_task_running_stale_seconds`: lease TTL for a `running` Fleet-memory record, default `86400` (24 hours). A worker entering through similar-task reuse must save its initial lease before browser work starts. Eligible live workers then refresh the lease in the background and stop heartbeating before terminal state is written; cancellation and failure paths also attempt a bounded terminal checkpoint. Positive configured values are clamped to at least `60` seconds to avoid expiring inside one write and to bound write amplification. Set it to `0` to disable expiry and retain indefinite fail-closed blocking.
- `same_fleet_multiworker_enabled`: opt-in canary for sharing one task/session fleet across parallel slots while keeping separate pages. It defaults to `false`; when enabled, the owner socket remains authoritative, notifications are relayed to delegates, and equal-page calls are serialized.
- `max_task_fleets`: ceiling on how many distinct fleets (browser instances) one task may occupy; `0` disables it. The harness never closes a fleet, so one it opens holds its budget slot until the platform stops reporting it — a fleet that disappears from the owner inventory releases its slot again. Counted over fleets bound to this task's workers, never over the Agent-global `Fleet.list`. A Fleet named by task-level `@<id>`, a bound `session_key`, or `reuse_from_worker_id` consumes the budget and is honored if current authoritative inventory confirms it. At the ceiling a fleetless worker reuses one of the task's existing fleets, preferring one no running worker holds, and deployment-default `worker_session_isolation_enabled` yields to the cap. Two cases cannot be served that way and get a `task_fleet_limit_reached` receipt instead: a spawn demanding a separate identity (a phase-declared `needs_isolated_session`, or a new `session_key`), and a ceiling where every task fleet is bound to a named session, since a logged-in cookie jar is never lent to a generic worker. Waiting does not clear either one — the harness closes no fleets and a session binding outlives its worker — so the receipt tells the task owner to continue on an existing fleet, release a session binding through auth recovery, or raise the ceiling. Before refusing, the cap re-reads the authoritative `Fleet.list` once. The dispatcher answers that from the whole fleets table with no per-connection scoping, so one successful read both finds a fleet another slot created seconds ago and retires any fleet the platform has dropped — whichever slot owned it — handing its budget back. A failed read is never treated as proof of disappearance.
- `fleet_auth_barrier_enabled`: make login/CAPTCHA resolution fleet-wide and fail closed for non-resolver workers. `fleet_auth_barrier_wait_seconds` controls the bounded wait.
- `auth_fleet_ledger_path`: persistent, non-secret verified session index, relative to `worktree_dir` unless absolute. Reclaimed fleets are quarantined until ledger reconciliation restores their restrictions.
- `fleet_slot_reconnect_attempts`: legacy or externally supplied slot recovery attempts. Native connections use the shared outage window without another retry budget.
- `fleet_slot_reconnect_backoff_seconds`: legacy slot backoff; native recovery backs off from 0.5 seconds to at most 4 seconds.
- `fleet_slot_manual_reset_after_failures`: recovery cycles before spawn returns `session_manual_reset_required`. The binding remains fail-closed until a host/operator explicitly resets it with the reported fleet id and generation.
- `hitl_poll_interval_seconds`: page-state confirmation interval after HITL release; human feedback is awaited through notifications or terminal input.
- Human HITL waits have no time or cumulative-question quota; answers and live guidance have no fixed character cap. Protocol calls and page confirmation after a reply remain bounded.
- Challenge keywords, hidden controls and embedded components are page/version-bound observations. The BrowserAgent decides whether to request HITL using the original goal, action receipts and human feedback. This path has no dedicated VL judge, CAPTCHA autosolver or component-driven re-pause loop.
- Only terminal human feedback or a matching `Hitl.resumed` notification returns control. Page loads, title changes and visual verdicts do not release a pause. Protocol pause-state and Fleet identity checks remain; task accessibility is a model decision. The old `hitl_no_repause_cooldown_seconds`, `hitl_post_resume_guard_seconds` settings were removed; `hitl_post_resume_confirm_max_rounds` still bounds read-only readiness checks after human handoff.
- `worktree_dir`: root directory for run logs and artifacts.
- `context_file`: legacy optional single static prompt-context file. It remains supported; prefer `project_context_files` for new configuration.
- `project_context_files`: ordered static project-instruction files. An item is either a path string or `{ "path": "...", "scope": "..." }`. The harness emits them as escaped `<project_context><project_instructions ...>` XML in that order; duplicate files are injected once. Use stable files only.
- `append_system_prompt`: trusted deployment-owned static suffix, emitted before project context as escaped `<append_system_prompt>`. It is for stable policy additions, never task-specific facts.
