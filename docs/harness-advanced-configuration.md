# Harness 高级配置 / Advanced configuration

首次使用只需配置模型连接和浏览器连接。内部阈值、复用与恢复参数由
[`HarnessConfig`](../runtime_config.py) 提供默认值，Browser 与 Lead 共用。
需要部署调优时，仍可在 `config.json` 的 `harness` 段显式覆写；旧配置继续生效。

For initial setup, configure the models and browser connection. Internal tuning
uses `HarnessConfig` defaults for both Browser and Lead. Explicit `harness`
overrides remain supported, including existing configurations.

## 内置默认值 / Internal defaults

| 配置项 / Option | 默认值 / Default | 说明 / Meaning |
| --- | --- | --- |
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
- `worker_max_steps`: Lead 委派的 BrowserAgent 最大执行轮数；独立 `/browser` 任务不受此轮数限制，以显式完成或实际阻塞为终态。
- 独立 `/browser` 的清单使用任务逻辑路径 `scratchpad/todo.md`，默认存于 SQLite。清单变化、主动调用 `request_goal_review`、以及 `final_answer(done)` 会触发独立证据复核。完成声明只有通过最终复核才会结束任务。复核器可读取授权文件和任务页面观察结果，但不能修改页面。
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
- `hitl_poll_interval_seconds`: HITL 解除后的页面状态确认间隔；等待人工回复本身由通知或终端输入驱动。
- `hitl_wait_timeout_seconds`: 等待人工介入的最长时间。
- `worktree_dir`: 运行日志和 artifacts 的根目录。
- `context_file`: 兼容保留的单个静态 prompt 上下文文件。新配置优先使用 `project_context_files`。
- `project_context_files`: 有序的静态项目指令文件；每项可为路径字符串，或 `{ "path": "...", "scope": "..." }`。Harness 会按顺序以转义后的 `<project_context><project_instructions ...>` XML 注入；重复文件只注入一次。只应放稳定文件。
- `append_system_prompt`: 受部署方信任的静态 prompt 尾部，位于项目上下文之前并包装为转义后的 `<append_system_prompt>`。仅用于稳定策略补充，不可放任务事实。

## English reference

Optional advanced configuration example:

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

- `lead_max_steps`: maximum LeadAgent decision rounds.
- `worker_max_steps`: maximum rounds for a BrowserAgent delegated by Lead. A standalone `/browser` task has no step limit and ends on an explicit completion or real blocker.
- Standalone `/browser` maintains its checklist at the logical task path `scratchpad/todo.md`, stored in SQLite by default. Checklist changes, an explicit `request_goal_review`, and `final_answer(done)` trigger independent evidence review. A done answer ends the task only after the final review verifies it. The reviewer can read authorized files and task-bound page observations but cannot change the page.
- `max_browser_agent_instances`: target number of live BrowserAgent slots kept in the reusable pool. Idle slots keep their ABCP connection and page registry. The effective pool is raised to at least `max_browser_agents`.
- `max_browser_agents`: authoritative maximum number of concurrently running browser workers.
- `fleet_reuse_enabled`: deterministically assign each worker a fleet and force `Page.create` into it. Generic work may reuse an eligible slot fleet; a new `session_key` or isolated worker gets a fresh fleet. An existing Fleet is bound only when the original user task contains `@<full UUID or unique prefix>`; the runtime resolves it from authoritative inventory and never creates a replacement. Named/isolated fleets never become the generic slot default. Lost named sessions fail with `session_fleet_lost` instead of silently rebinding. Model-initiated `Fleet.create`/`Fleet.close` and out-of-assignment fleet ids fail closed; explicit page continuations may receive prior page candidates.
- `similar_task_fleet_reuse_enabled`: before the first successful Fleet acquisition of an unconstrained task, compare the original user objective with the trusted harness-owned task index in current Fleet memory. A match reuses the Fleet only and always opens a fresh page. Resume, task-level `@Fleet` routing, session/continuation routing, plus phase-declared isolation, take precedence. Active lifecycle is keyed by top-level task plus worker so one worker cannot mark a Fleet completed while another worker still uses it. Terminal history collapses by task and keeps the latest 12 tasks. Running identity keeps at most 32 worker records plus one backward-compatible synthetic running fence for overflow; the fence cannot release before every omitted finite lease expires, and untrustworthy or non-expiring overflow remains permanently fail-closed. Admission requires a completed task record and a versioned Fleet-level policy that has never been blocked by named-session, task-bound, or hard-isolation use; identity-free legacy history and foreign memory are not candidates. A stopped `prepared` Fleet may auto-wake through readiness, while a real readiness failure falls back once to ordinary routing/Fleet creation without committing the candidate.
- `similar_task_reuse_threshold`: deterministic normalized text/character-ngram ranking threshold in `[0, 1]`, default `0.78`. It cannot grant reuse by itself: normalized tasks must first be equal or a high-overlap contiguous extension, explicit URL origins must not conflict, and numeric tokens must agree when both tasks contain them. Reuse transfers browser context only—prior pages and results are never accepted as current evidence.
- `similar_task_running_stale_seconds`: lease TTL for a `running` Fleet-memory record, default `86400` (24 hours). A worker entering through similar-task reuse must save its initial lease before browser work starts. Eligible live workers then refresh the lease in the background and stop heartbeating before terminal state is written; cancellation and failure paths also attempt a bounded terminal checkpoint. Positive configured values are clamped to at least `60` seconds to avoid expiring inside one write and to bound write amplification. Set it to `0` to disable expiry and retain indefinite fail-closed blocking.
- `same_fleet_multiworker_enabled`: opt-in canary for sharing one task/session fleet across parallel slots while keeping separate pages. It defaults to `false`; when enabled, the owner socket remains authoritative, notifications are relayed to delegates, and equal-page calls are serialized.
- `max_task_fleets`: ceiling on how many distinct fleets (browser instances) one task may occupy; `0` disables it. The harness never closes a fleet, so one it opens holds its budget slot until the platform stops reporting it — a fleet that disappears from the owner inventory releases its slot again. Counted over fleets bound to this task's workers, never over the Agent-global `Fleet.list`. A Fleet named by task-level `@<id>`, a bound `session_key`, or `reuse_from_worker_id` consumes the budget and is honored if current authoritative inventory confirms it. At the ceiling a fleetless worker reuses one of the task's existing fleets, preferring one no running worker holds, and deployment-default `worker_session_isolation_enabled` yields to the cap. Two cases cannot be served that way and get a `task_fleet_limit_reached` receipt instead: a spawn demanding a separate identity (a phase-declared `needs_isolated_session`, or a new `session_key`), and a ceiling where every task fleet is bound to a named session, since a logged-in cookie jar is never lent to a generic worker. Waiting does not clear either one — the harness closes no fleets and a session binding outlives its worker — so the receipt tells the Lead to continue on an existing fleet, release a session binding through auth recovery, or raise the ceiling. Before refusing, the cap re-reads the authoritative `Fleet.list` once. The dispatcher answers that from the whole fleets table with no per-connection scoping, so one successful read both finds a fleet another slot created seconds ago and retires any fleet the platform has dropped — whichever slot owned it — handing its budget back. A failed read is never treated as proof of disappearance.
- `fleet_auth_barrier_enabled`: make login/CAPTCHA resolution fleet-wide and fail closed for non-resolver workers. `fleet_auth_barrier_wait_seconds` controls the bounded wait.
- `auth_fleet_ledger_path`: persistent, non-secret verified session index, relative to `worktree_dir` unless absolute. Reclaimed fleets are quarantined until ledger reconciliation restores their restrictions.
- `fleet_slot_reconnect_attempts`: bounded reconnect attempts per recovery cycle. Each reconnect must retain the server-assigned protocol identity before an authenticated fleet binding is reused; transport loss never proves that the fleet is lost.
- `fleet_slot_reconnect_backoff_seconds`: base delay between those reconnect attempts. Failed browser mutations are never replayed.
- `fleet_slot_manual_reset_after_failures`: recovery cycles before spawn returns `session_manual_reset_required`. The binding remains fail-closed until a host/operator explicitly resets it with the reported fleet id and generation.
- `hitl_poll_interval_seconds`: page-state confirmation interval after HITL release; human feedback is awaited through notifications or terminal input.
- `hitl_wait_timeout_seconds`: maximum wait time for human intervention.
- `worktree_dir`: root directory for run logs and artifacts.
- `context_file`: legacy optional single static prompt-context file. It remains supported; prefer `project_context_files` for new configuration.
- `project_context_files`: ordered static project-instruction files. An item is either a path string or `{ "path": "...", "scope": "..." }`. The harness emits them as escaped `<project_context><project_instructions ...>` XML in that order; duplicate files are injected once. Use stable files only.
- `append_system_prompt`: trusted deployment-owned static suffix, emitted before project context as escaped `<append_system_prompt>`. It is for stable policy additions, never task-specific facts.
