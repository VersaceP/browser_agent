# ABCP Browser System 全链路说明：从用户请求到 final answer

> 生成日期:2026-08-16
> 范围:一次用户请求从 CLI 进入，经 LeadAgent 分解、PlanValidator 审计、spawn 前置门、Worker Agent Loop、
> 每次 browser call 的前/后机械门锁、runner.call 驱动 ABCP 平台，到最终 final answer 输出的完整链路。
> 前后机械门禁（第七、九章）已包含逐点详解（位置/触发/逻辑/作用四要素）。

---

## 目录

- [一、总览](#一总览)
  - [1.1 系统是什么](#11-系统是什么)
  - [1.2 端到端总时序图](#12-端到端总时序图)
  - [1.3 门禁体系现状：机械门 vs 观察器](#13-门禁体系现状机械门-vs-观察器)
- [二、入口层：用户请求如何进来](#二入口层用户请求如何进来)
- [三、LeadAgent：任务分解与 Worker 编排](#三leadagent任务分解与-worker-编排)
  - [3.1 计划审计：Lead 与 PlanValidator 的协作](#31-计划审计lead-与-planvalidator-的协作)
  - [3.2 编排时序](#32-编排时序)
- [四、Worker 生命周期前置门（spawn 一次性门）](#四worker-生命周期前置门spawn-一次性门)
- [五、Agent Loop：BrowserAgent.run 主循环](#五agent-loopbrowseragentrun-主循环)
- [六、工具分发层与重复调用观察](#六工具分发层与重复调用观察)
- [七、前机械门锁：runner.call 之前](#七前机械门锁runnercall-之前)
- [八、驱动工具层：runner.call -> ABCP 平台](#八驱动工具层runnercall---abcp-平台)
- [九、后机械门锁：runner.call 之后](#九后机械门锁runnercall-之后)
- [十、终态裁定与 final answer 输出](#十终态裁定与-final-answer-输出)
- [附录 A：FleetAuthBarrier 状态机](#附录-afleetauthbarrier-状态机)
- [附录 B：代码地图](#附录-b代码地图)

---

# 一、总览

## 1.1 系统是什么

本系统是一个 **LLM 驱动的浏览器自动化 Harness**：用户给一句自然语言任务（如"去某电商网站把前
50 条商品的标题价格抓下来存成表"），系统通过 LLM 规划、驱动真实浏览器（经 ABCP 协议）完成
抓取、表单、下载等操作，最终返回结构化结果与证据文件。

角色分三类 agent（外加编排设施与平台）：

| 角色 | 类 | 职责 | 代码 |
|---|---|---|---|
| **LeadAgent** | `LeadAgent` | 理解任务 -> 产出 task_plan（phase 分解）-> 交 PlanValidator 审计 -> 调 `spawn_browser_agent` 派发 worker -> 收结果/复盘/replan -> `final_answer` | `agent_harness.py:2138` |
| **PlanValidator**（独立审计 agent） | 无独立类；由 `plan_validator_provider`（一个**与 Lead 不同模型**的 LLM）+ 审计协议承担 | 对 Lead 的 task_plan 候选做独立语义审计：目标是否被削弱/删除、数量放宽是否有授权链、证据引用是否真实；裁决经 `submit_plan_validation` 工具返回 approved/rejected | `agent_harness.py:2263`（`review_task_plan_candidate`）、`harness/planning/validator.py:1061`（`review_plan_revision`） |
| **BrowserAgent（worker）** | `BrowserAgent` | 单个 phase 的执行者：per-step LLM 循环 + `browser_call` 驱动 ABCP 能力 + `record_extraction` 落盘 + `final_answer` | `agent_harness.py:676` |
| **Spawner**（非 agent，编排设施） | `BrowserAgentSpawner` | worker 的生命周期管理：slot/fleet 分配、启动门禁、结果校验、回收 | `harness/spawner/spawner_core.py:59` |
| **ABCP 平台** | 平台侧（Electron 浏览器宿主） | 真正执行 `Input.click`/`DOM.getAXTree` 等能力，通过 JSON-RPC 返回结果 | `abcp_client.py:267`（客户端） |

**三类 agent 的制衡关系**：Lead 写计划但不能自证--计划候选必须通过 PlanValidator 的独立
模型审计（配置层强制 `plan_validator.model_id` 不得与 Lead 模型相同，`agent_harness.py:2192`，
"一个模型确认自己的 prose 不构成独立审阅"）；PlanValidator 只审计不执行；BrowserAgent 只执行
本 phase 的 worker_contract，越权调用被门禁拦截。另有一个派生只读角色 **claim extractor**
（数值声明抽取）：优先用独立 `claim_extractor` 配置，否则复用 PlanValidator 的模型另开连接
（`agent_harness.py:2210-2243`），服务于 lead final_answer 的数值对账（第十章）。

用户心智模型中的链路是：

```
用户请求 -> agent -> 前机械门锁 -> agent loop -> 后机械门锁 -> 驱动工具 -> final answer
```

映射到代码后需要修正一点：**前/后机械门锁不是包住整个 agent loop 的，而是包住 agent loop
内部每一次 `runner.call`（即每一次真正打到浏览器的调用）的**。此外还有一组 **worker 启动前
的一次性门**（spawn gates）包住整个 agent loop。准确的嵌套关系：

```
用户请求 (main.py CLI)
└─ LeadAgent.run()  编排循环
   └─ spawn_browser_agent()        ← 【spawn 一次性门 S1~S5】包住整个 worker 生命周期
      └─ _run_browser_worker()
         └─ BrowserAgent.run()     ← 【Agent Loop】per-step 循环
            └─ dispatch(tool_call) ← 【分发层】terminal/loop-guard/routing
               └─ _execute_browser_capability_tool()
                  ├─【前机械门锁 P1~P20】runner.call 之前
                  ├─ runner.call()  ←【驱动工具】唯一 model-initiated 落地点
                  └─【后机械门锁 Q1~Q23】runner.call 之后
```

## 1.2 端到端总时序图

```
用户                main.py            LeadAgent              Spawner                BrowserAgent(worker)         ABCP平台
 │  输入任务          │                    │                      │                        │                        │
 ├──────────────────►│                    │                      │                        │                        │
 │                   │ run_cli()          │                      │                        │                        │
 │                   │ 读config/建logger   │                      │                        │                        │
 │                   ├───────────────────►│ LeadAgent(...)       │                        │                        │
 │                   │  harness.run(task) │                      │                        │                        │
 │                   │                    │ ┌────────────────────────────────────────┐   │                        │
 │                   │                    │ │ lead loop: LLM 产出 task_plan 等       │   │                        │
 │                   │                    │ └───────────┬────────────────────────────┘   │                        │
 │                   │                    │  emit_task_plan(候选计划)                    │                        │
 │                   │                    │  ├─ 机械校验 validate_task_plan             │                        │
 │                   │                    │  ├─ scope未变 -> operational_continuation  │                        │
 │                   │                    │  └─ scope变了 -> PlanValidator独立模型审计  │                        │
 │                   │                    │     approved/rejected(submit_plan_validation)│                       │
 │                   │                    │  (rejected则打回Lead重写计划)                │                        │
 │                   │                    │  spawn_browser_agent(phase)                 │                        │
 │                   │                    ├─────────────►│                               │                        │
 │                   │                    │               │ ╔═══ S1 phase_start_rejection ═══╗                  │
 │                   │                    │               │ ╔═══ S2 phase_pacing(sleep+重跑S1)╗                  │
 │                   │                    │               │ ╔═══ S4 spawn_acquisition ════════╗                  │
 │                   │                    │               │ ╚═══ slot/fleet 分配(含S5隔离) ═══╝                  │
 │                   │                    │               │  _run_browser_worker()          │                        │
 │                   │                    │               ├───────────────────────────────►│ BrowserAgent.run()   │
 │                   │                    │               │                                │ ┌──────────────────┐ │
 │                   │                    │               │                                │ │ step: LLM ->     │ │
 │                   │                    │               │                                │ │ tool_calls       │ │
 │                   │                    │               │                                │ │  dispatch        │ │
 │                   │                    │               │                                │ │   P门(前)        │ │
 │                   │                    │               │                                │ │   runner.call ───┼─┼─► 执行
 │                   │                    │               │                                │ │   Q门(后) ◄──────┼─┼─┘ 结果
 │                   │                    │               │                                │ │ ...循环...       │ │
 │                   │                    │               │                                │ │ final_answer     │ │
 │                   │                    │               │                                │ └──────────────────┘ │
 │                   │                    │               │  结果校验/终态分类               │                        │
 │                   │                    │◄──────────────┤  worker result                 │                        │
 │                   │                    │ wait/replan/继续spawn...                      │                        │
 │                   │                    │ final_answer (含completion receipt对账)        │                        │
 │                   │◄───────────────────┤                                               │                        │
 │◄──────────────────│ print(answer)      │                                               │                        │
```

下层的 per-step 细节（Agent Loop 内部一次 step）见[第五章图](#五agent-loopbrowseragentrun-主循环)，
单次 `runner.call` 的前后门细节见[第七、九章](#七前机械门锁runnercall-之前)。

## 1.3 门禁体系现状：机械门 vs 观察器

**这是理解当前系统最重要的一点**。旧文档描述的"机械门禁"体系中，相当一部分门已经按
`docs/tau-informed-simplification-plan.md` 的决策**从"拦截"降级为"观察"**——它们仍然计算
同样的事实，但不再拒绝工具执行，而是把事实附在 result 上（`progressObservations` /
`loopObservations`），让模型自己读。剩下的才是真正的"机械门锁"。

**分类一览**（详见后续章节）：

| 类别 | 成员 | 行为 |
|---|---|---|
| **保留的机械门（硬拦截）** | 参数/方法校验、Runtime.evaluate 三关卡、Workflow 门、worker contract、memory scope、fleet/page binding、**FleetAuthBarrier**、**PageLifecycleGuard**、stale AXTree、HITL claim、screenshot misuse、target 参数 | 返回拒绝 receipt，`tool_was_executed=False`，不触达浏览器 |
| **观察器（不再拦截）** | ProgressAccountant 各子门、unrecorded-extraction 门、loop guard（warn 层已删） | 事实附在 result 上（`execute_browser_tool` dispatch.py:394 的 docstring 明言 *"production execution no longer treats its interpretation as permission to run the tool"*） |
| **花费上限（唯一保留的 loop 硬边界）** | `DUPLICATE_CALL_STOP_AT=20`：同一字节级相同调用连发 20 次才停 | `harness/tools/loop_guard.py:42`；每个调用照常执行到上限 |
| **语义终态（不可同 phase 重试）** | `phase_not_startable` / `phase_exhausted` / `spawn_infrastructure_exhausted` / `lost_fleet_result` 等 | 需要 replan / 新 phase id / final_answer 解 |

设计哲学的转向（loop_guard.py 模块 docstring 原文）：*"An identical request is an arithmetic
fact. It is not proof of an identical world state"*——重复调用是算术事实，不是"卡死"的证据；
全局预算足以约束普通重复，不可逆副作用另有幂等/确认保护。旧的 warn/force 分桶表因过拟合
（`Input.scroll` 连滚 5 次是正常阅读行为）被整体删除。

---

# 二、入口层：用户请求如何进来

**背景**：系统只有一个 CLI 入口。用户请求有两种形态：命令行参数直接给任务，或交互式 REPL
输入；另有 `--resume <worktree>` 从已有任务目录按 phase 粒度恢复。

**实现**（`main.py`）：

1. `main()`（main.py:1912）解析参数后 `asyncio.run(run_cli(args))`（main.py:1936）。
2. `run_cli()`（main.py:1544）依次完成：
   - `load_runtime_config(args.config)` 加载 `config.json` → `RuntimeConfig`；
   - `read_task(args)` 读任务；交互模式下 `input("请输入浏览器任务（/resume ... 恢复任务；...")`（main.py:1242）；
   - `--fleet-id/--page-id` 钉住已有浏览器上下文（`_validated_pinned_browser_context`，main.py:52——page 必须有 fleet、必须是 UUID）；
   - resume 路径：`acquire_run_lock` → 加载 task_plan/task_state/manifest → `prepare_resume_state` 决定保留哪些 validated phase、重置哪些被中断/产物失效的 phase，并记录浏览器 Fleet/Page 恢复候选；
   - 新任务路径：创建任务目录（worktree），写 task_manifest；
   - **创建 `LeadAgent` 并 `await harness.run(task_for_agent)`**（main.py:1829-1836）。
3. finally 中写 usage summary、关存储、释放 run lock；`print(answer)` 把 final answer 交给用户。

**要点**：CLI 层不碰浏览器。它的全部职责是"把一句用户任务 + 一份配置 + 可选的恢复上下文，
安全地交到 LeadAgent 手里，并在结束后归还一个答案"。

---

# 三、LeadAgent：任务分解与 Worker 编排

**背景**：长任务（多目标、多页面、需并行）不能靠一个上下文窗口跑完。LeadAgent 是" brains"：
它不直接摸浏览器，而是产出**任务计划（task_plan，phase 列表）**，把每个 phase 交给一个
worker，再根据 worker 回执决定继续 spawn、replan 还是收尾。

**实现**（`agent_harness.py:3319` `LeadAgent.run`）：

1. **上下文组装**（一次）：`<user_task>` + 策略库索引（strategy_bank，advisory）+ 已知技能
   digest + `<resumed_state>`（resume 时）+ `<runtime_limits>` + `<pinned_browser_context>`。
   resume 场景下 prompt 明确指示三种新指令处置：`extend_task_plan`（只加新 phase）/
   完整 replan / `resume_keep_plan`。
2. **per-step 循环**（`lead_max_steps`）：
   - `compact_and_track_prefix_rebuild`（agent_harness.py:636）上下文压缩；
   - `generate_response_surviving_moderation`（agent_harness.py:383）调 LLM，5 类异常
     （degenerate/connection/timeout/protocol/moderation）step 内重试后降级为空 turn；
   - tool_calls 分发给 lead 工具（`harness/tools/lead_tools.py`）：`emit_task_plan` /
     `spawn_browser_agent` / `wait_browser_agents` / `list_browser_agents` /
     `extend_task_plan` / `replan_task_plan` / `resume_keep_plan` / `lead_save_artifact` /
     `final_answer` 等。
3. **spawn 派发**：Lead 调 `spawn_browser_agent(task, phase_id, worker_contract, ...)` →
   进入[第四章的启动门禁](#四worker-生命周期前置门spawn-一次性门)。
4. **收结果**：`wait_browser_agents` 等待；spawner 对每个 worker 做**产物校验**
   （`validate_worker_artifacts`，含 advertisedMethodsNeverCalled 分析——区分"worker 没做 X"
   和"这个 phase 做不了 X"，防 Lead 误 replan）。
5. **收尾**：`final_answer`（见[第十章](#十终态裁定与-final-answer-输出)）。

## 3.1 计划审计：Lead 与 PlanValidator 的协作

**背景**：Lead 产出的 task_plan 是整个任务的契约（phase 目标、数量指标、artifact schema、
validators）。若 Lead 在 replan 时悄悄把"抓 50 条"放宽成"抓 10 条"、或删掉一个难做的目标，
任务会在"看似合规"中失真。因此计划候选在落盘前要过一道**独立模型的语义审计**。

**实现**：`LeadAgent.review_task_plan_candidate`（`agent_harness.py:2263`）+
`review_plan_revision`（`harness/planning/validator.py:1061`）。`emit_task_plan` /
`extend_task_plan` / `replan_task_plan`（`harness/tools/lead_tools.py:1104`）都先走它：

```
emit_task_plan(候选计划)
  │
  ├─[1] 机械校验 validate_task_plan (task_control/plan_validation.py)
  │      schema/未知ABCP方法/未知harness工具/依赖拓扑/用户任务覆盖
  │      失败 -> mechanical_invalid（不进审计）
  │
  ├─[2] scope 签名比对 _plan_review_scope_signature
  │      用户scope/phase拓扑/能力边界/交付物都没变 -> operational_continuation
  │      （战术、阶段笔记、selector 等运营性修改不过审计，直接放行）
  │
  ├─[3] 独立语义审计 review_plan_revision (planning/validator.py:1061)
  │      输入：原始用户任务、immutable 初始计划、上一版计划、候选计划、
  │            task_state 证据目录、structural_plan_diff、数量放宽/世系分析、
  │            worker handoff 摘要（明确标注"未验证"）
  │      审计者规则（prompt 内嵌，节选）：
  │        - 不得发明证据 ID；evidenceIds 必须逐字来自 evidenceCatalog
  │        - 每个数量放宽必须给 quantityDecision；higher_priority_user_objective
  │          只能由不可变的原始用户任务本身授权
  │        - 每个 overriddenObjective 必须有 assessed 为 weakened/removed 的
  │          objectiveChecks 佐证
  │        - replanReason 是 Lead 的一面之词，永远不构成用户授权
  │      裁决：审计模型必须恰好返回一次 submit_plan_validation 工具调用
  │            （approved / rejected + 逐项 findings）
  │      失败缓存：同 candidate_hash+evidence_snapshot 的 error 结果去重，不重复烧审计调用
  │
  ├─[4] 落审计凭证 write_plan_review_audit -> task_plan_reviews/
  │      计划历史不可变记录 -> task_plan_history/（plan_hash / candidate_hash）
  │
  └─ rejected -> emit_task_plan 返回 failed + "保持当前计划不变，修正后重发完整计划"
     approved/operational_continuation/error -> accept_task_plan 落盘 task_plan.json
```

**硬约束**：

- `plan_validator.model_id` **必须**与 Lead 模型不同（`agent_harness.py:2192` 直接 raise）--
  独立性是配置层强制的，不是提示词约定。
- resume 场景 fail-closed：初始计划历史丢失时，一般性 replan 直接拒绝（无法建立不可变
  基线就无法审计改写了多少，`agent_harness.py:2277-2290`）；扩展（extend）不受此限，
  因为扩展结构上不可能重写基线。
- 审计不可用（transport error）与语义否决是两种结果：前者绑定 candidateHash、可缓存去重，
  **不**自动变成拒批。

## 3.2 编排时序

```
LeadAgent.run step N
  │ LLM
  ├─ emit_task_plan ──► [机械校验] ──► [PlanValidator 独立审计] ──► accept/reject
  │      （rejected 打回 Lead 修正重发；见 3.1）
  ├─ spawn_browser_agent(phase_i) ──► [S1..S5 门] ──► _run_browser_worker ──► BrowserAgent.run
  ├─ spawn_browser_agent(phase_j) ──► ...（可并发多个 worker）
  ├─ wait_browser_agents ──► worker result（含 artifact 校验、trace summary）
  ├─ (可选) replan_task_plan / extend_task_plan ──► 同样过 3.1 审计
  └─ final_answer ──► completion receipt 对账 ──► 返回 answer 给 main.py
```

---

# 四、Worker 生命周期前置门（spawn 一次性门）

**背景**：worker 启动前要防三类事故——①对已终态/正在跑/预算耗尽的 phase 重复起 worker；
②反爬节流（依赖 phase 刚完成就立刻开跑下一个是机器特征）；③基础设施故障（拿不到 slot/
开不了 fleet）被无限重试烧预算。这些门每个 worker 生命周期只跑一次，任一 reject 即不 spawn，
短路返回拒绝 receipt 给 LeadAgent。

**实现**：`BrowserAgentSpawner.spawn_browser_agent`（`harness/spawner/spawner_core.py:583`）。

```
spawn_browser_agent (spawner_core.py:583)
│  ╔════ worker 启动前一次性门（任一 reject 即不 spawn） ════╗
├─[S0] 路由参数硬校验 (spawner_core.py:590-720)
│        pinned context 与 fleet_id/session_key/isolated 互斥、
│        fleet_id 与 session_key 互斥、reuse_scope/page_policy 归一化
├─[S1] phase_start_rejection            (task_control/phase_lifecycle.py:1018，接线 spawner_core.py:773)
│        phase 已 terminal → phase_not_startable
│        phase running     → phase_already_running
│        显式 max_attempts 用尽 → phase_exhausted
├─[S2] phase_pacing_remaining_seconds   (phase_lifecycle.py:856，接线 spawner_core.py:782)
│        依赖 phase 全部 validated_done 后，再晾 jittered interval 才准起
│        （锚点是依赖完成时刻，非上次 spawn；DEFAULT 全 0，不配不生效）
│        sleep 是让出点 → 醒来必须重跑 S1（spawner_core.py:806）
│        等待期间不占 slot（slotReserved=False）
├─[S4] spawn_acquisition_rejection      (task_control/fingerprints.py:342，接线 spawner_core.py:833)
│        路由指纹(objective+fleet/slot/session路由) × 错误签名(归一化消息)
│        二层账本；count<2 且 cooldown 中 → spawn_acquisition_cooldown；
│        count≥2 → spawn_infrastructure_exhausted（硬锁，明确"不代表业务不可行"）
│        成功启动即清账（一条好路由证明恢复）
│        注：旧文档的 S3 repeated_phase_attempt_guard 已删除
│  ╚════════════════════════════════════════════════════════╝
│
├─ mark_phase_running (spawner_core.py:876)   ← 此后才产生 attempt 记录
├─ slot 获取 + _sync_slot_registry (spawner_registry.py:37)
│   └─[S5] Page.getState 报 paused / err_page_paused
│        → _mark_page_quarantined（doNotUse=True，不分给 worker）
│        ← 现已带 TTL：page_quarantine_ttl_seconds 默认 300s
│          (_quarantine_expired spawner_registry.py:502)
│          过期后由 Page.getState 复检，仍 paused 才 _retire_expired_quarantined_page
│          关页重建 (spawner_registry.py:519)——旧"永久隔离活锁"已修
│
├─ fleet 绑定/worker_contract 组装/readiness receipt
└─ _run_browser_worker (spawner/spawner_worker.py:280)
    ├─ 建 worker_runtime/provider/event_logger/capability bundle(预热)
    ├─ _try_skill_fast_path (spawner_worker.py:77)
    │    选中 workflow skill 时先跑冻结配方，成功则直接产出 answer；
    │    批量中途停则带 handoff_note+repair_manifest 走慢路径
    └─ answer = await harness.run(worker_task)  (spawner_worker.py:560)
        │  ← 控制权交给 BrowserAgent（下一章）
        └─ worker 终止(正常/异常/取消)
            └─ fleet_auth_barrier.abandon_worker (spawner_worker.py:845/896)
               保持门关、清 owner，让位子空出来等人接手
```

**要点**：

- S4 的账本**与 phase id 无关**（按路由指纹存），所以 Lead 改名/replan 绕不过它；正当出路
  是换路由（换 fleet/slot/session），路由变了指纹就变。
- S2 的 interval 带 jitter（`pacing.py`），固定间隔本身是机器特征。
- worker 终止**必经** `abandon_worker`：这是 FleetAuthBarrier fail-closed 回收的兜底，
  防"解题人死了但门永远关着"。

---

# 五、Agent Loop：BrowserAgent.run 主循环

**背景**：worker 拿到一个 phase 的任务描述 + worker_contract + slot 上下文，要在一个**有界
step 预算**（`worker_max_steps`）内自主完成。主循环只管三件事：LLM 还能不能正常产出
tool_call、批内 tool call 的执行边界、终态裁定。**所有与浏览器语义相关的门都不在这层**
（在 dispatch 内，第七、九章）。

**实现**：`BrowserAgent.run`（`agent_harness.py:775`）。

```
BrowserAgent.run()  step ∈ [1, worker_max_steps]                     agent_harness.py:775
│
├─ _bootstrap_browser (agent_harness.py:1596)
│    System.register(预热)、capability bundle、Memory 任务上下文初始化
├─ build system_prompt / tool specs / dispatcher / render_recovery_runner
├─ event_observer.attach   ← Layer-0：DOM.axTreeUpdated 事件自动刷新 id 快照
├─ messages = [user_task + dynamic_context]
│
├─ for step in 1..max_steps:
│  ├─[G1] compact_and_track_prefix_rebuild        压缩上下文，不阻断
│  ├─[G2] lifecycle.agent_before_step             默认 no-op（扩展点）
│  ├─[G3] generate_response_surviving_moderation  LLM 调用
│  │      异常收容：degenerate/connection/timeout/protocol/moderation
│  │      一律降级为空 turn（text="", tool_calls=[], stop_reason=<kind>），
│  │      不让网关抖动烧掉整个 phase attempt
│  ├─[G4] if not tool_calls:  incident 分类 → truncation_streak += 1
│  │      streak_limit = _effective_streak_limit(streak_kinds)   (:217)
│  │        - infra 类(connection/timeout/moderation)上限 5
│  │        - 其他(truncated/protocol/empty)上限 3
│  │      未到限：注入 recovery 提示（infra 类原样重问）后 continue
│  │      到限：WORKER_STATUS_INCOMPLETE 终止，blocker 写明 kind 混合计
│  ├─     text-only turn → 模型自报 done（分类器仍可覆盖，见下）
│  │
│  └─ for tool_call in tool_calls:
│     ├─[G5/G6] Runtime.evaluate 混批 → runtime_batch_boundary_rejected
│     │         短路跳过 dispatch（主循环层唯一的 tool 拒绝）
│     └─ result, should_stop = dispatch(tool_call, step)     ← 第六~九章
│        ├─[O1] _observe_tool_result → diagnostics
│        │      （HITL/auth/captcha/contract_error 硬信号进入分类器的唯一入口）
│        ├─[O2] page_observer / loop_nudge（观测，不阻断）
│        ├─[O3] offload_tool_result_for_model
│        ├─[O4] _tool_call_state_boundary(agent_harness.py:264)
│        │      该 tool 可能改浏览器状态 → 断批
│        ├─[O5] should_stop or boundary 且批内还有后续 →
│        │      后续 tool_call 全部 _deferred_tool_result（不执行）
│        └─[O6] should_stop → 取 answer/status，should_finish=True，break
│
└─[POST] classify_terminal_status (harness/diagnostics/__init__.py:173)
        读 diagnostics 累积的硬信号 + model_reported_status +
        reached_step_cap + has_extraction_artifact → 最终 worker 终态。
        ★ 即使模型自报 done，HITL/auth/契约错误等硬信号也会覆盖成
          hitl_timeout / page_settled_after_hitl / api_contract_error 等。
        → _write_agent_final + write_context_snapshot（finally，必落盘）
```

**要点**：

- **G3 的异常收容是"防误烧预算"的核心**：一次网关抖动只损失一个空 turn，而不是整个
  phase attempt（attempt 记录在 mark_phase_running 时已产生，worker 崩溃=白烧一次）。
- **G4 是 LLM 退化守卫**：空 turn 永远不会被误判成"模型自报 done"，从而绕过 step-cap
  和 final_answer 通道。infra/model 两档上限是旧文档"混计误杀"待修项的修复。
- **终态裁定在循环外**（POST），不在任何单个 tool 里——它是 inescapable 的最后一道。

---

# 六、工具分发层与重复调用观察

**背景**：主循环把每个 tool_call 交给 `dispatch`。分发层要处理：终态工具（可软拒）、
重复调用观察、未落盘抽取观察、browser_call 直通、路由绑定，然后才进入能力执行器。

**实现**：`harness/tools/browser_tools/dispatch.py`。

```
build_browser_tool_dispatcher.dispatch (dispatch.py:349)
├─[D0]  lifecycle.tool_pre_call / tool_post_call     默认 identity（扩展点）
├─[D3]  _maybe_reality_check (dispatch 返回后)        VL 视觉现实核查，best-effort
└─ execute_browser_tool (dispatch.py:394)
   │  ★ 现在的职责是"附上非阻断的进度观察"：
   │    ProgressAccountant 的事实算出来后作为 progressObservations/
   │    loopObservations 附到 result 和 trace receipt 上，并明文声明
   │    "它们没有决定调用是否运行"（docstring + notice 文本）
   └─ _execute_browser_tool_impl (dispatch.py:465)
      ├─ terminal handler? (final_answer / lead final_answer)
      │    走终态分支；handler 可软拒(tool_was_executed=False)把调用弹回模型重做
      ├─ _observe_unrecorded_extraction_before      ← 观察（旧 extraction gate）
      ├─ loop_guard.check_tool_call_loop            ← 观察 + 花费上限
      │    同字节级调用连发 DUPLICATE_CALL_STOP_AT=20 次才 should_stop；
      │    每次调用照常 dispatch；final_answer 豁免
      ├─ name == "browser_call" → 直通 _execute_browser_capability_tool
      │    （注册 handler 只能返 JsonDict 会丢 should_stop，直通保住
      │     page_create_should_stop 死浏览器硬停的透传）
      ├─ direct capability name（如直接调 Input.click 名）→ 同上
      ├─ 注册工具（navigate_verified / collect_items / fill_field_verified /
      │  dismiss_overlay / visual_verify / record_extraction / local_fs_* / ...）
      │    → routing guard(fleet/page binding) → contract_check → handler
      └─ 其他 → Unknown harness tool
```

**要点**：

- **观察与拦截的分界线在这层最清晰**：`execute_browser_tool` 包一层"事实附注"，
  `_execute_browser_tool_impl` 内才是少量硬门（terminal 软拒、loop 花费上限、routing、
  contract）。
- 复合工具（composites）内部用 `_invoke_browser_method`（[第八章](#八驱动工具层runnercall---abcp-平台)），
  不走模型路径，防递归、防污染观察链。

---

# 七、前机械门锁：runner.call 之前

**背景**：`runner.call` 是模型发起的调用**唯一**真正打到浏览器的落地点。前门禁的使命是
保证这一刻的调用：**参数合法、方法存在、不过期（AXTree id / 页面状态）、不越权
（fleet/page/memory scope）、不重复（下载）、授权充分（Runtime.evaluate / Workflow）**。
任一门返回非 None 即短路返回拒绝 receipt（`tool_was_executed=False`），不触达浏览器。

**实现**：`_execute_browser_capability_tool`（`harness/tools/browser_tools/capability.py:42`）。

## 7.1 快速索引

| # | 门 | 位置 | 拦什么 |
|---|---|---|---|
| P1 | `parse_browser_call_params` | capability.py:95（parsers.py） | params 非 JSON object -> params_error |
| P2 | capability 成员校验 | capability.py:115 | 未知 ABCP method（附 known_methods） |
| P3 | navigation_context 校验 | capability.py:149（dispatch.py:74） | 声明式导航上下文非法 |
| P4 | 输出参数归一 | capability.py:145/170 | DOM.getImg 输出 / 截图输出契约 |
| P5 | **Runtime.evaluate 三关卡** | capability.py:177 + runtime_evaluation.py | 见 7.2 详述 |
| P6 | Workflow.execute enable+validate | capability.py:212 | 见 7.2 |
| P7 | `_check_worker_contract` | capability.py:241（progress_obs.py） | worker 契约硬约束 |
| P8 | `_check_cross_task_memory_scope` | capability.py:254 | Memory 读写其他任务 scope |
| P9 | `_apply_fleet_binding` | capability.py:267（bindings.py:18） | fleetId 缺失/冲突 |
| P10 | `_check_page_binding` | capability.py:280（bindings.py:174） | 操作未认领的 page |
| P11 | **`_fleet_auth_barrier_before_call`** | capability.py:293（bindings.py:550，底层 fleet/runtime.py:2030） | fleet 鉴权挑战期间的非授权操作，见 7.2 与附录 A |
| P12 | **`_page_lifecycle_guard_before`** | capability.py:312（dispatch.py:155，底层 observation/page_lifecycle.py） | 页面 loading / 过期 DOM 义务位，见 7.2 |
| P13 | `_check_screenshot_misuse` | capability.py:322（validation.py） | 截图滥用 |
| P14 | `_check_target_param_requirements` | capability.py:328 | 按 schema 校验必要 target 参数 |
| P15 | `_check_stale_axtree_target` | capability.py:343（axtree_state.py） | 用旧导航代次的 AXTree id 打现在的页 |
| - | `_observe_progress_before` | capability.py:356 | ★观察（旧进度拦截门现只附事实） |
| P16 | `ensure_required_purpose` / `_ensure_hitl_request_reason` | capability.py:358/370 | 规范化 purpose/reason 元数据 |
| P17 | `_claim_ownerless_fleet_auth_barrier_for_page_create` | capability.py:373 | Page.create 恢复路径抢占认领 barrier |
| P18 | `_maybe_autosolve_before_model_pause` | capability.py:391 | pause 前的有界 VL 自解，成功则短路 |
| P19 | `_claim_fleet_auth_barrier_for_hitl` | capability.py:403 | 发 requestPause 前必须持有 barrier |

## 7.2 逐点详解

### P1 · parse_browser_call_params（capability.py:47/95，parsers.py）

- **触发**：每个 browser_call / direct-capability 调用的入口。
- **逻辑**：解析 `tool_input` 的 `method`/`params`/`reason`。params 不是 JSON object 即
  `params_error`，附期望格式说明与 method schema。
- **作用**：最早期参数合法性。失败分支仍跑一次 `_observe_progress_before(charge_diagnostic=False)`
  --前置拒绝不收诊断预算但仍计 stall（观察语义）。
- **细节**：direct-capability 名（模型直接以 ABCP method 名调用）与 `browser_call` 包装在此
  归一为同一条后续路径，区别只在参数解析函数（`parse_direct_capability_params`）。

### P2 · capability_methods 成员校验（capability.py:115）

- **逻辑**：`method not in agent.capability_methods` -> `ABCP capability not found`，
  带 known_methods 列表。
- **作用**：防模型调用平台不存在（或被 harness 剥除）的 method。同样附 schema 与观察。

### P3 · _prepare_navigation_context（capability.py:149，dispatch.py:74）

- **逻辑**：校验/规范化 `navigation_context`（声明式导航上下文，如
  `route_recovery_claimed_page`）。
- **作用**：让 harness 跟踪"这次调用是某个未决导航声明的延续"，事后（Q17）才能正确判定
  声明是否被消费，防模型 replay 已消费的声明。非法声明直接拒，不进调用。

### P4 · 输出参数归一（capability.py:145/170）

- **逻辑**：`_normalize_dom_get_img_output` 与 `_normalize_screenshot_output`
  规范化 DOM.getImg / Page.screenshot 的 output 参数（格式/路径），必要时落
  `normalizedFields` 回执。
- **作用**：统一输出契约，便于后续 offload 与 evidence 落盘。

### P5 · Runtime.evaluate policy + escalation（capability.py:177；harness/runtime_evaluation.py）

> `Runtime.evaluate` 能执行任意 JS，是模型能碰到的**最危险的 method**。本门设**三道互相独立
> 的关卡**：①这段 JS 该不该写成 JS -> ②现在允不允许跑 -> ③在哪个 world 跑。

- **触发**：`method == "Runtime.evaluate"`。

- **关卡一 · 表达式本身合不合法**（`_prepare_runtime_evaluation`，RuntimeEvaluationService）

  两条硬拒，与 world 无关：

  | 拒绝码 | 拦什么 |
  |---|---|
  | `runtime_structured_interaction_bypass` | 用 JS 替代 `Input.*` / 表单 / 上传 / 权限等**结构化交互动作**--点击就该用 `Input.click`，不该用 `el.click()` |
  | `runtime_cross_check_required` | 抽取类调用没有给出具体的 **DOM 交叉验证计划** |

  另有 `reason_kind` 必须属于 `EVAL_JS_REASON_KINDS`（runtime_evaluation.py:11），否则
  `runtime_reason_kind_invalid`。

- **关卡二 · 允不允许跑（穷尽证明）**（`runtime_last_resort_evidence` runtime_evaluation.py:255）

  这道关卡的授权者**不是任何人，是 harness 自己的记账**。docstring 一句话点透：

  > The proof is derived from **harness trace entries, never from model prose**.

  模型既不能"申请"授权，也不能声称自己试过了。它必须先把所有结构化读取手段**真的调一遍**：

```python
RUNTIME_STRUCTURED_ALTERNATIVES = (      # runtime_evaluation.py:43
    "Page.getState", "DOM.getAXTree", "DOM.getSemanticTree",
    "DOM.getText", "DOM.getAttribute",
)
```

  取 `capability_methods ∩ 上面 5 个` 得到 `required`，倒扫 `agent.trace` 找同一 pageId 的
  `browser_call` 记录。全都出现过 -> 放行；差一个 -> 拒绝
  `runtime_structured_alternatives_not_exhausted`，并把 `missingAlternatives` 列给模型。

  **关键是 epoch 边界**：倒扫时一旦遇到 `_RUNTIME_EPOCH_BOUNDARIES`
  （`Page.navigate`/`reload`/`go`/`switchTo`/`create`、`Input.click`/`type`/`press`/
  `scroll`/`drag`、`Hitl.*`，runtime_evaluation.py:51）就 `break`。含义是**页面状态一变，
  之前的尝试全部作废，得在新页面上重新试一遍**--否则模型可以在 A 页面试满 5 种读法，
  然后导航到 B 页面直接跑 JS。

- **关卡三 · 在哪个 world 跑**（runtime_evaluation.py:181-230）

  ⚠️ **这道关卡与关卡二完全独立**：关卡二决定「能不能跑」，关卡三决定「跑在哪」。

  - 模型必须**显式声明 `world=isolated`**，否则 `runtime_isolated_world_required`。main /
    auto / 隐式 world **一律禁止**--错误信息原文（runtime_evaluation.py:206）：*"only the
    harness may authorize a second strict main-world attempt"*。
  - schema 不支持 isolated -> `runtime_isolated_world_unavailable`。
  - **只有 `reason_kind == "non_dom_state"`** 才把 `mainFallbackAuthorized` 置真，授权
    harness 做**第二次** main-world 尝试（即 Q2 的 fallback）。且还要同时满足两条，否则仍拒：
    - schema 支持 main world，否则 `runtime_main_world_unavailable`；
    - 表达式必须包含 `ABCP_MAIN_WORLD_REQUIRED:<global>` 的 throw 信号
      （`_MAIN_FALLBACK_SIGNAL_RE` runtime_evaluation.py:23），否则
      `runtime_main_fallback_signal_required`--**必须证明"这个 global 在 isolated world
      确实不存在"，而不是空口要 main**。

- **作用**：三道关卡各管一层--不该用 JS 的别用、没穷尽替代方案的别跑、跑也只准在隔离
  world。main-world 是唯一逃生口，且必须靠一个可验证的信号自证必要性。事后（Q2）还会校验
  平台**真正**在哪个 world 执行，metadata 不符判 `world_evidence_mismatch`，**防平台撒谎**。

### P6 · Workflow.execute enable + validate（capability.py:212）

- **触发**：`method == "Workflow.execute"`。
- **逻辑**：`workflow_execution_enabled` 关（agent_harness.py 的运行时开关，默认关，
  skill 降级为 guidance）即返 `workflow_runtime_disabled`；开则
  `validate_workflow_params`（harness/workflow_policy.py，allow_runtime=False，
  enforce_lifecycle=True）。
- **作用**：workflow 执行默认关。开了也要校验不能内嵌 Runtime.evaluate、必须跟生命周期。

### P7 · _check_worker_contract（capability.py:241，progress_obs.py）

- **逻辑**：读 `agent.worker_contract`，检查 skill_selection_declined / 任务约束。
- **作用**：worker 契约层硬约束。典型如 repair manifest 激活时禁止重跑全 workflow--
  manifest 是一张"这些字段可信、只补这几个"的契约，重跑全 workflow 等于把契约撕了。
  注意 disabledReason 逃生口：manifest 可被标记失效（如基线 artifact 读不到了），标记后
  这道拦截不生效，退回正常路径。

### P8 · _check_cross_task_memory_scope（capability.py:254，progress_obs.py）

- **逻辑**：拦 `Memory.get/save` 针对其他任务 scope。
- **作用**：防 worker 读写别的任务的 memory。

### P9 · _apply_fleet_binding（capability.py:267，bindings.py:18）

- **逻辑**：校验 fleet 绑定是否齐全/冲突，返 `fleet_binding_guard`。
- **作用**：确保调用带正确的 fleetId，防跨 fleet 误操作。

### P10 · _check_page_binding（capability.py:280，bindings.py:174）

- **逻辑**：校验 pageId 是否被本 worker 认领，返 `page_binding_guard`。
- **作用**：防 worker 操作别人认领的 page。事后（Q12）还观察绑定状态变化。

### P11 · _fleet_auth_barrier_before_call（capability.py:293，bindings.py:550；底层 FleetAuthBarrier fleet/runtime.py:2030）

> **一个 fleet = 一套共享登录态（同一个 cookie jar）。** 弹出验证码时会出两种事故：解题时
> 多个 worker 同时去点，互相刷新互相覆盖；解完后登录身份变了，各 worker 手上的页面和
> AXTree id 可能全失效却不自知。本门管两件**互相独立**的事--**①挑战期间只让一个 worker 动**
> （门）、**②解决之后所有 worker 重新感知**（版本号）。状态迁移表与术语对照见附录 A。

- **触发**：任何 fleet 上的 browser call。

- **逻辑一 · 门（现在谁能动）**

```
门开着吗?
├─ 开着 ──────────────► 过
└─ 关着 ─── 解题人是谁?
            ├─ 是我 ────► 过（我正在解，当然要让我操作）
            ├─ 是别人 ──► 等最多 120s（wait_timeout_seconds fleet/runtime.py:2038），
            │             超时返 fleet_auth_gated(retryable=True)
            └─ 没有人 ──► 返 _resolver_required_receipt，引导去 claim
```

  "关着但没人解"是最易困惑的状态，出现在解题人中途死亡或主动放弃时：挑战**还在**（门不能
  开），但无人处理（要找人接手）。**最重要的一条规则：超时永不开门**（fleet/runtime.py:2034
  docstring：*"A timeout never opens the gate"*）。等满 120s 不代表验证码消失了。

- **逻辑二 · 版本号（你的信息过期了吗）**

  门每开一次 `generation += 1`。worker 自带 `seen_generation`，对不上就意味着：**在你不知情
  时有人解决过一次挑战，你的世界变了**。此时被锁进重感知模式（bindings.py:544
  `_REPERCEPTION_ALLOWED_METHODS` = {`Page.getState`, `DOM.getAXTree`,
  `Hitl.requestPause`}），其余一律返 `fleet_reperception_required`。

  **`Page.getState` 和 `DOM.getAXTree` 两个都做完才算数。** 这里藏过一个死锁：若每次检查都
  重置进度标记，两步会**永远互相擦掉对方的记录**。所以目标 generation 是**锁存**的，
  `seen_generation` 在两步都完成前不更新。

- **三个特殊通道**

  1. **门关着且无主时放行 4 个方法**：`Page.getState` / `Page.create` / `DOM.getAXTree` /
     `Hitl.requestPause`。道理很朴素--**你得先看清现状，才能决定要不要接手**。但只给页面级
     诊断：`Page.list`（跨页面）不放行，也绝不让某个随便的业务调用意外变成 resolver。
  2. **`Workflow.execute` 走独立快速预检**（workflow fence），与普通路径有两处实质差异：

     | | 普通调用 | Workflow.execute |
     |---|---|---|
     | 撞上别人在解 | 等 120s | **立即拒绝，不等** |
     | 我自己就是解题人 | **放行** | **照样拒绝** |

     第二条最反直觉：**即使你正是那个在解验证码的 worker，也不能跑 workflow**。因为
     workflow 是**不透明的批量执行**，发出去后 harness 看不见也拦不住；鉴权态正在变化时
     跑它，事后无法判断其中几步是在旧身份下完成的。返回 `status=fleet_auth_gated` +
     `reasonKind=workflow_auth_barrier_closed`；generation 变更时为
     `fleet_reperception_required` + `workflow_auth_generation_changed`，均 retryable。
  3. **`Page.create` / `Hitl.requestPause` 的放行是为了让它们去"报名"**：它们从通道 1 出来后
     走 P17 / P19 做真正的原子 claim。P11 放它们过不是不管，是放它们去真正的认领点。

- **作用**：**门管"现在谁能动"，版本号管"你的信息是否过期"。** 同 fleet 的 auth 挑战
  （captcha/login）由此串行化，只有一个 worker 当 resolver；fail-closed，非硬拒--门只有
  `resolve` 能开，超时不开、解题人死了也不开，只是空出位子等人接手。

### P12 · _page_lifecycle_guard_before（capability.py:312，dispatch.py:155；底层 observation/page_lifecycle.py）

> 模型拿到一批 AXTree nodeId 后点了个链接，页面开始跳转。此刻它手上的**所有 id 都指向一个
> 正在消失的 DOM**--继续操作要么打空，要么打错元素。本门职责：**页面状态变过之后，逼模型
> 重新感知，禁止用过期句柄**。它是**事件驱动**的，这是它区别于其他门的最大特点--不轮询，
> 等平台推事件。

- **触发**：每个 call，读 `PageLifecycleTracker.state(page_id)`。

- **先分清两类东西**（混在一起就读不懂本门）

  | | `status` | `requires_*` 义务位 |
  |---|---|---|
  | 语义 | 页面**现在**是什么状态（loading/settled/failed/crashed） | 你**欠**一次重新感知 |
  | 谁改 | 平台事件（`Page.loaded` 等） | 导航/恢复/对话框/下载 |
  | 怎么消 | 等事件到达 | 必须**实际调用**对应方法 |

  注释特意点明二者不是一回事（page_lifecycle.py:74）：*"`requires_state_resync` and
  `requires_ax_refresh` **deliberately survive** a settle."* **页面加载完 ≠ 你的义务清了。**
  settled 只说明"不在加载中"，不说明"你已经重新看过了"。

- **逻辑 · 三段**

  **第一段：DOM 探针撞上 loading 页 -> 等事件**

```python
if method.startswith("DOM.") and state.status == "loading":
    settled = await tracker.wait_for_settlement(page_id, timeout)
```

  **只有 `DOM.*` 会等**，其他方法不等--只有 DOM 读取才依赖"页面结构已稳定"。
  `wait_for_settlement`（page_lifecycle.py:252）等的是一个 `asyncio.Event`，由平台推来的
  `Page.loaded` / `Page.loadFailed` / `Page.crashed` 触发，**不是轮询**。超时取
  `page_settlement_timeout_seconds`（默认 15.0）。

  超时后**只补发一次** `Page.getState`（purpose 写明一次性再同步）为漏事件兜底：

  - 补发失败 -> `page_settlement_unknown`，原始调用**依然被拦**；
  - 补发成功但仍在 loading -> `page_still_loading`。

  **两个出口都明令禁止轮询**（"Do not poll" / "Wait for a lifecycle event; do not poll
  Page.getState"）。轮询会烧诊断预算，而事件迟早会来。

  **第二段：`requires_state_resync` -> 强制 `Page.getState`**

  置位来源（page_lifecycle.py `before_action`:97 + `observe_event`）：

  | 触发 | 置哪些位 |
  |---|---|
  | `Page.navigate` / `reload` / `go` | resync + ax_refresh（**在调用发出前**就置，不等事件回来） |
  | `File.download` / `Download.pause`\|`resume`\|`cancel` | 仅 resync |
  | `Page.crashed` | resync + ax_refresh |
  | `Page.recovered` | resync + ax_refresh，`generation += 1` |
  | `Page.dialogClosed` / `File.chooserClosed` | 仅 resync |

  清除只有一条路：`observe_state_response`（page_lifecycle.py:168）收到**成功且含 data** 的
  `Page.getState` 响应。有一段专门防御--render-recovery 的建议性响应、畸形响应、
  `tool_was_executed: False` **都不算销账**，反而把 status 打成 `failed`：

  > A render-recovery advisory and any malformed/failed getState response
  > **must never discharge** the resynchronization obligation.

  **第三段：`requires_ax_refresh` -> 强制 `DOM.getAXTree`**

  导航/崩溃/恢复后所有 AXTree id 全部失效，只有真正调了 `DOM.getAXTree` 才清
  （`observe_ax_refresh`:235）。

  另有一个反向操作 `invalidate_ax_refresh`（page_lifecycle.py:240）：Q18 事后若证明某棵树是
  mid-call 竞态下取的、属于**上一个导航代次**，需要把已清的义务**加回去**，但又不能伪造一次
  新导航--注释原文：*"roll back that optimistic transition **without manufacturing another
  navigation**"*。

- **两组豁免--都是防自锁**

```python
lifecycle_recovery_methods = {"Page.getState", "Page.navigate", "Page.reload",
                              "Page.go", "Page.close"}
is_file_control = method == "File.download" or method.startswith("Download.")
```

  1. **恢复方法自身豁免**--要求你调 `Page.getState` 来清账，就不能拦 `Page.getState`。
     `DOM.getAXTree` 在 ax_refresh 那道门里单独豁免。
  2. **File/Download 豁免**--注释写了理由：*"Download controls are **mutually composable**
     (pause -> resume/cancel)... but must not **deadlock each other**."*
     死锁长这样：`Download.pause` 置了 `requires_state_resync` -> 想 `Download.resume` ->
     被拦要求先 `Page.getState` -> 但页面可能正忙/不可用 -> resume 永远发不出去，**下载卡死**。
     所以下载控制之间互不拦截，代价是"这些操作弄脏的页面状态推迟到后续 DOM 操作时才结算"--
     `requires_state_resync` 位仍置着，只是不拦 Download 自己。

- **作用**：导航/恢复/对话框/下载状态变更后强制重新感知，禁止用过期 DOM handle。与 P11 是
  同构设计但作用域不同--**P11 管 fleet 级（跨 worker 的共享登录态），P12 管 page 级
  （worker 内的单页状态）**；两者都在解决同一类问题：*你脚下的地基变了，先重新看一眼*。

### P13 · _check_screenshot_misuse（capability.py:322，validation.py）

- **逻辑**：按 method + purpose 正则（`SCREENSHOT_ALLOWED_PURPOSE_RE` /
  `SCREENSHOT_MISUSE_RE`，dispatch.py）检测截图滥用（如无目的连续截图）。
- **作用**：防模型把截图当万能诊断浪费预算。

### P14 · _check_target_param_requirements（capability.py:328）

- **逻辑**：按 method schema 校验必要 target 参数（如 click 缺 nodeId）。含
  `_check_id_param_format` / `_check_nested_id_format` / `_check_scroll_param_requirements` /
  `_check_select_param_requirements` 等格式子检。
- **作用**：早期参数完整性，失败也走观察分支（不收诊断预算）。

### P15 · _check_stale_axtree_target（capability.py:343，axtree_state.py）

- **逻辑**：检测 params 里的 nodeId/handle 是否来自过期 AXTree 快照（比对
  `agent.axtree_ids` / `axtree_page_id` / `axtree_epoch`）。`allow_rematch` 仅在
  `_browser_side_rematch_mode(agent) == "on"` 时开。
- **作用**：防模型用旧 axtree 的 id 打现在的页（导航后 id 全失效）。composite 工具走
  internal 路径时可按调用 opt-in rematch（见第八章）。
- **配合**：Layer-0 事件观察器订阅 `DOM.axTreeUpdated`，浏览器侧 auto-rematch 后自动刷新
  id 快照（`BrowserAgent.run` 里 `event_observer.attach`），减少误拦。

### （观察）· _observe_progress_before（capability.py:356，progress.py）

- **旧 P16 拦截门现只为观察**：ProgressAccountant 的算术事实（诊断预算、artifact 停滞、
  local_fs 模式等）以 `progressObservations` 附在 result 上并声明"未决定调用是否运行"。
  错误分支以 `charge_diagnostic=False` 调用--前置拒绝不收诊断预算但仍计 stall。

### P16 · ensure_required_purpose / _ensure_hitl_request_reason（capability.py:358/370，parsers.py / hitl.py）

- **逻辑**：对需要 purpose 的 method 补 purpose；对 `Hitl.requestPause` 补 reason。
- **作用**：规范化调用元数据，让事后审计/日志能解释"为什么调"。

### P17 · _claim_ownerless_fleet_auth_barrier_for_page_create（capability.py:373，bindings.py）

- **触发**：`method == "Page.create"`。
- **逻辑**：认领"已关且无主"的 barrier（不开健康 fleet 的闸），用于 Page.create 恢复路径。
- **作用**：让 Page.create 能在 auth 挑战期间抢占认领 barrier 走恢复，而不是被 P11 挡死。

### P18 · _maybe_autosolve_before_model_pause（capability.py:391，hitl.py:851 + captcha_autosolve.py）

- **触发**：`method == "Hitl.requestPause"` + autosolve_enabled + 有挑战证据。
- **逻辑**：跑有界 VL 自解（`captcha_solve_max_retries=3` / 单次
  `captcha_solve_timeout_seconds=150.0` / 总 `captcha_solve_budget_seconds=240.0` /
  每 worker `captcha_solve_max_episodes_per_worker=2`，runtime_config.py:612-633）。
  `_autosolve_cleared` 为真 -> 返 `captcha_auto_solved` 短路（**pause 不发**，barrier 在
  autosolver 内部 claim+verify+release）；为假 -> 把尝试写入 reason，pause 照发。
- **作用**：**在 pause 发出前**先试机器自解，成功就不打扰人。结构化证据优先：AXTree 确认的
  `structural_confirmed` 不会被 VL 的 normal_loading 否掉。

### P19 · _claim_fleet_auth_barrier_for_hitl（capability.py:403，bindings.py）

- **触发**：P18 没短路（即将发 requestPause）。
- **逻辑**：claim barrier 成当 resolver（门关）；他 worker 已 claim -> `fleet_auth_gated`。
- **作用**：确保发 pause 的 worker 持有 barrier，后续 Q7 等 resume 期间其他 worker 被 P11 挡住。

### CALL · runner.call(method, params)（capability.py:459）

- **逻辑**：经 `render_recovery_runner` 调 ABCP（含 Download.start 超时对账、
  Runtime.evaluate main-world fallback 条件触发二次 call :558）。
- **作用**：**唯一 model-initiated 的落地点**。所有前置门都是为了保证这一刻的调用合法、
  安全、不过期、不重复、不越权；所有后置门都是为了消化这一刻的返回。
- ⚠️ **这不是全库唯一打 ABCP 的点**。harness 自发起的调用走另一条独立链路
  `_invoke_browser_method`（见[第八章](#八驱动工具层runnercall---abcp-平台)），带一套更薄
  的门。这是设计意图，但意味着门禁**不是全覆盖**的。

---

# 八、驱动工具层：runner.call -> ABCP 平台

**背景**：前门全过之后，调用终于落地。"驱动工具"是三层包装：

```
_execute_browser_capability_tool (capability.py)
└─ runner = render_recovery_runner (observation/render_recovery.py:80)
   └─ RenderRecoveryRunner.call(method, params)      (render_recovery.py:62)
      │  包装一层渲染恢复：检测到页面渲染异常时的建议性恢复
      └─ ABCPClient.call(method, params)             (abcp_client.py:353)
         └─ JSON-RPC over ABCP transport ──────────► ABCP 平台（Electron 浏览器宿主）
                                                        真正执行 Input.click /
                                                        DOM.getAXTree / Page.navigate...
```

**两个关键细节**：

1. **Download.start 的灰区处理**（capability.py:459-468）：JSON-RPC 超时可能发生在 Electron
   已开始下载之后。传输错误被**本地收容**（不冒泡），转给 `_reconcile_download_start_timeout`
   用 `Download.list` 对账：证实操作存在 → 合成成功 receipt（"Do not retry it"）；
   证实 terminal failed → 允许一次有界重试的建议；无法证实 → 明确告知不要重发同一 URL。
2. **Runtime.evaluate main-world fallback**（capability.py:536-545）：isolated 失败 +
   `mainFallbackAuthorized` + 平台发出 main-world 必需信号 → 二次 `runner.call`（world=main）。
   每次尝试都记 receipt（requestedWorld/executedWorld/evidenceStrength）。

**第二条调用链路：`_invoke_browser_method`（capability.py:888）**

harness **自发起**的浏览器调用（composites：navigate_verified / collect_items /
fill_field_verified / dismiss_overlay；captcha autosolve；auto-intercept 的树刷新；
post-HITL recovery）走这条减配链路，落地点 capability.py:1017：

- **减配前置门**（保留的硬门只有）：screenshot 归一、internal 路径 Runtime.evaluate
  全禁（唯一例外：`_TRUSTED_COLLECTION_RUNTIME_TOKEN` + collect_items 只读模板）、
  `allow_rematch=True` 时的 stale guard（composite 逐调用 opt-in）、fleet auth barrier、
  page lifecycle guard（`lifecycle_cleanup_bypass` 仅限 internal）、page_create/HITL claim。
- **绕过**的模型路径门：P1-P3、P5-P10、P13-P16。
- **后置减配**：`internal=True` 跳过整个观察链（无挑战裁定/诊断/进度/model trace），
  只留调用副作用处理。这是设计意图：①harness 自发调用可信；②**不能污染观察链**——
  composite 内部一次 click 若计入 model 的 progress/diagnostics，会把账本和 HITL 判定搅乱。
- **风险提示**：此路径不跑 contract/progress 门，composite 构造的 params 若有 bug
  （过期 id、错 page）只能靠调用方正确性兜底。

**FleetClickGate**（fleet/runtime.py:779，新增）：同 fleet 上的点击串行化闸门，
超时抛 `FleetClickGateTimeout`（capability.py 中作为可收容异常处理，转结构化 receipt）。

---

# 九、后机械门锁：runner.call 之后

**背景**：调用返回不代表结束。后门的职责是**消化这一刻的返回**：对账灰区结果、更新生命
周期状态机、捕捉挑战/遮挡信号、等 HITL、隔离 auth 变更污染的结果、自动补救、卸载大
结果--最后把一份**干净、可审计、不撑爆 context** 的 result 交回 agent loop。

**实现**：`_execute_browser_capability_tool` 的 call 之后部分（capability.py:461 起至函数尾）。

## 9.1 快速索引

| # | 门 | 位置 | 做什么 |
|---|---|---|---|
| Q1 | Download 对账 | capability.py:461-546（downloads.py） | start 超时灰区对账 |
| Q2 | Runtime world 证据校验 | capability.py:547-602 | main fallback + 防平台撒谎 |
| Q3 | `_page_lifecycle_after_action` | capability.py:603 | 置位下次 P12 义务位 |
| Q4 | artifacts + 结构挑战 | capability.py:604-605 | 落证据、扫挑战帧 |
| Q5 | Page.list 过滤 + axtree 快照 | capability.py:612-620 | 页面可见性 + staleness 基线 |
| Q6 | `_offload_response` | capability.py:621 | 大 response 卸盘 |
| Q7 | **`_enrich_pause_with_wait`** | capability.py:628（hitl.py:615） | HITL 等待与裁定 |
| Q8 | workflow 结果隔离 | capability.py:686 | auth 变更污染隔离 |
| Q9 | pause 失败弃权 | capability.py:693 | resolver 回收 |
| Q10 | fleet 丢失 / Page.create 恢复 | capability.py:701-708 | 硬停 / takeover |
| Q11 | 恢复失败弃权 | capability.py:714 | resolver 回收 |
| Q12 | page binding 观察 | capability.py:723 | 喂下次 P10 |
| Q13 | Runtime 失败终判 | capability.py:742-760 | runtimeBlocker(final) |
| Q14 | barrier 状态闭合 | capability.py:761 | 配对 P11/P19 |
| Q15 | 导航检查 / 策略提示 | capability.py:762-763 | 附声明消费结果 |
| Q16 | **auto HITL** | capability.py:765（hitl.py） | 挑战累积自动 pause |
| Q17 | 清单/完整性/导航观察 | capability.py:767-838 | 三类页面观察 |
| Q18 | axtree staleness | capability.py:845 | mid-call 竞态检测 |
| Q19 | **auto overlay 拦截** | capability.py:860（auto_intercept.py） | 自动 dismiss 遮罩 |
| Q20 | **VL 仲裁** | capability.py:866（visual.py） | 视觉失败第二意见 |
| Q21 | 大结果卸载 | capability.py:869 | 最终 model_result 卸盘 |
| Q22 | 进度观察 | capability.py:877 | artifact 增长清 stall |
| Q23 | trace 记录 | capability.py:880 | model-facing trace |

## 9.2 逐点详解

### Q1 · Download 对账（capability.py:461-546，downloads.py）

- **逻辑**：`Download.list` 标记已知 download（对账 receipt store）；`Download.start` 超时
  走 `_reconcile_download_start_timeout`--用 `Download.list` 证明操作是否存在：
  - 证实 completed/active -> 合成成功 receipt，observation 写明 *"Do not retry it"*；
  - 证实 terminal failed -> 建议检查页面后允许**一次**有界重试；
  - 无法证实 -> 明确告知重定向可能已把文件存到默认下载目录，**不要重发同一 URL**，
    如页面有最终直链可换直链重试一次。
- **作用**：`Download.start` 是"可能已开始但 JSON-RPC 超时"的灰区，对账防重复下载。

### Q2 · Runtime.evaluate main-world fallback + world 证据校验（capability.py:547-602，runtime_eval.py）

- **逻辑**：isolated 失败 + `mainFallbackAuthorized` + 平台发 main-world 信号 ->
  二次 `runner.call`（world=main，:558）。校验平台返回的 world metadata：
  metadata 不符 -> 判 `world_evidence_mismatch`（该次 attempt 标 failed）；metadata 缺失 ->
  降级只信"harness 派发的是哪个 world"（`runtime.evaluate.world_evidence_degraded` 日志），
  不信"平台执行的是哪个"。
- **作用**：**防平台对 world 撒谎**。Runtime.evaluate 最终失败由 Q13 收尾。

### Q3 · _page_lifecycle_after_action（capability.py:603，dispatch.py:282）

- **逻辑**：据本次 call 更新 page lifecycle 状态（如点击后标 requires_ax_refresh）。
- **作用**：喂给下次 P12 的判断。与 P12 配对构成"调用前预告脏、调用后确认脏"的闭环。

### Q4 · _capture_artifacts / detect_structural_challenge（capability.py:604-605）

- **逻辑**：capture artifacts（截图等落盘）；`detect_structural_challenge`（challenge_detector.py）
  对 DOM.getAXTree 扫 lines 找挑战帧，附 `structuralChallenge`。
- **作用**：把"页面有验证码/挑战"的结构信号抽出来，供 Q16 判定是否 auto HITL。

### Q5 · Page.list 过滤 / _precompute_axtree_snapshot（capability.py:612-620，bindings.py）

- **逻辑**：`Page.list` 按 fleet 绑定过滤（`_filter_page_list_response`，只给模型看本 worker
  该看的 page）；`_precompute_axtree_snapshot` 预留"调用前的树"供 Q18 staleness 比对。
- **作用**：防 page 列表泄露其他 worker 的 page；为 staleness 检测留基线。

### Q6 · _offload_response（capability.py:621，offload.py）

- **逻辑**：大 response（AXTree/截图）卸盘，只留摘要进 model context，并附
  `_annotate_axtree_offload`（标注"卸盘的树在内存索引里也能查，且只在当前 epoch 内有效"）。
- **作用**：防大 AXTree/截图撑爆 context。

### Q7 · _enrich_pause_with_wait（capability.py:628；wait_for_hitl_resume harness/hitl.py:615）

- **触发**：`method == "Hitl.requestPause"` 且 pause 成功。
- **逻辑**：调 `wait_for_hitl_resume`，出口（常量 hitl.py:44-45）：
  1. `timeout`：人工未在超时内完成；
  2. `resumed`（explicit）：平台报 Hitl.resumed + `_confirm_unpaused_after_settlement` 确认；
  3. `STALE_PAUSE_DEADLOCK`（explicit）：explicit_resume 后 resolvePause 被 ERR_PAGE_PAUSED
     阻塞 -> `_close_deadlocked_page` 关页；
  4. `PAGE_SETTLED_AFTER_HITL`（explicit）：resume 信号到了但 Page.getState 仍 paused
     （非 deadlock）；
  5. `resumed`（verified_settlement）：settlement 事件 -> VL 裁定 passed -> 确认；
  6/7. 同 5/4 但 verified 路径的死锁/settled。
  - 子机制：`challenge_ever_confirmed` 锁存（一旦 VL 判 confirmed，后续 title 证据永久失效，
    只能视觉清除或 explicit resume）；verifier 预算 3；title 兜底仅预算用尽且
    `_title_clears_challenge` 通过（title-only 禁止的落实）。
- **作用**：等人工/VL 解决挑战并裁定真解除了。stale_pause_deadlock 是"平台不清 pause flag"
  的兜底--关页让新 worker 重新 claim。resume 成功后由 `_verify_and_open_fleet_auth_barrier`
  开闸（唯一开门路径）。

### Q8 · _quarantine_workflow_result_after_auth_change（capability.py:686，bindings.py）

- **触发**：auth generation 在本次 call 期间变了（比对 P11 时记的
  `workflow_auth_started_generation`）。
- **逻辑**：隔离 workflow 结果。
- **作用**：auth generation 变更意味着共享鉴权态变了，本次 workflow 结果可能基于旧态，
  隔离防误用。

### Q9 · _relinquish_fleet_auth_resolver_after_failed_pause（capability.py:693，bindings.py）

- **触发**：`pause_succeeded=False`（pause 根本没成功）。
- **逻辑**：弃 barrier 所有权（`resolver_worker_id=""`），**门仍关**。
- **作用**：pause 没成功的 worker 不该继续当 resolver，弃权让别的 worker takeover。
  注意：pause 成功但 wait 非 resumed 时**不触发**这里（等待期间仍持有 barrier，
  最终由 worker 退出的 abandon_worker 回收，其他 worker 最多等 120s 有界延迟）。

### Q10 · _assigned_fleet_lost_result / _recover_page_create_32005（capability.py:701-708，page_create.py）

- **逻辑**：assigned fleet 丢了 -> `lost_fleet_result` + `page_create_should_stop=True`
  （**硬停**，经 browser_call 直通透传到主循环）；Page.create 32005 失败 -> 走恢复
  （`_recover_page_create_32005`，takeover 重建）。
- **作用**：fleet 丢失是致命的，硬停 worker 别再敲死 browser。

### Q11 · _relinquish_..._after_failed_recovery_page_create（capability.py:714）

- **触发**：Page.create takeover 失败。
- **逻辑**：同 Q9，弃权保持门关。
- **作用**：恢复路径失败也释放 resolver 身份。

### Q12 · _observe_page_binding_after（capability.py:723，bindings.py）

- **逻辑**：观察本次 call 后 page 绑定变化（如 Page.create 新建了 page）。
- **作用**：更新绑定状态供下次 P10。

### Q13 · Runtime 失败终判（capability.py:742-760）

- **逻辑**：Runtime.evaluate 最终失败 -> 按 attempts 分类
  （`runtime_execution_world_unverified` / `runtime_main_evaluation_failed` /
  `runtime_isolated_context_blocked` / `runtime_isolated_evaluation_failed`），
  `status=blocked` + `runtimeBlocker(final=True)`。
- **作用**：禁止模型再请求 main 或重试；next_instruction 写明"报告这个 blocker"。

### Q14 · _fleet_auth_barrier_after_call（capability.py:761，bindings.py）

- **逻辑**：据 result 调整 barrier 状态（成功开闸/失败保持）。
- **作用**：与 P11/P19 配对，保证 barrier 状态机闭合。

### Q15 · _attach_navigation_check / _attach_runtime_strategy_hints（capability.py:762-763）

- **逻辑**：附导航声明消费结果与 runtime 策略提示。
- **作用**：与 P3 配对，告诉模型"你的导航声明被采纳了吗"。

### Q16 · _maybe_auto_hitl_for_challenge（capability.py:765，hitl.py）

- **触发**：非 requestPause 的 call 返回后。
- **逻辑**：
  - result 已含 paused-error -> 附"别再 pause"指引（已是 paused 态）；
  - 喂 `challenge_tracker`，`cleanup_stale` 后判 adjudication：`cooldown`
    （`hitl_no_repause_until` 未过）/ `post_hitl_recheck`（`hitl_post_resume_guards`
    每 page guard，刚 resume 的页不立即再 pause）/ `not_ready`（证据不足）/
    `adjudicate`；
  - adjudicate -> `_adjudicate_and_maybe_hitl`，可能 auto 发 requestPause。
- **作用**：把结构挑战（Q4）+ 行为信号累积成"该 HITL 了"的判定，**自动**发 pause，不依赖
  模型自觉。cooldown + post-resume guard 防 pause 风暴。

### Q17 · _settle_page_inventory_signal / _observe_content_completeness_after / _observe_navigation_progress_after（capability.py:767-838）

- **逻辑**：页面清单信号结算；内容完整性跟踪（懒挂载模块是否 mount）；
  navigation_context 判声明是否被消费，附 `navigationContext.accepted`。
  accepted 的判定按声明种类分别问真正消费它的机制（route_recovery 问
  `last_declaration_accepted`，其余问 pending map）--统一探测曾把成功的 claimed-page 绑定
  误报为 rejected，诱导模型 replay 已消费的声明。
- **作用**：内容没 mount 时 target_absent 是假的（专治懒挂载类页面）；导航声明防 replay。

### Q18 · _observe_axtree_state_after（capability.py:845，axtree_state.py）

- **逻辑**：用 Q5 的 precomputed snapshot + `event_serial_before`/`page_before` 检测
  mid-call 的 `DOM.axTreeUpdated` 竞态，正确标记 stale/clean。
- **作用**：axtree staleness 追踪，喂给 P15。**必须先记本次 call 的树，再跑 Q19
  auto-intercept**--dismiss 会改页，若顺序反了，dismiss 后的新树会被旧快照覆盖成 clean。

### Q19 · _maybe_auto_intercept_overlay（capability.py:860，auto_intercept.py）

- **触发**：config mode ∈ {p0, p0p1}（off/suggest 跳过）。
  - P0：`errorClassification == occlusion_blocked`；
  - P1（mode=p0p1）：AXTree layer `occlusionState == occluded`。
- **逻辑**：跑 `_dismiss_overlay`（button->Escape->backdrop 阶梯，见 composites/dismiss_overlay.py），
  每 page 上限 `AUTO_INTERCEPT_MAX_PER_PAGE`。dismiss 后：非 blocked 则 invalidate axtree
  snapshot（dismiss 改了页）；DOM.getAXTree 调用且清掉了 overlay 则重取树。
  auth/paywall overlay 返 `blocked`，**不自动点**，保留原 error。
- **作用**：省模型一步，自动 dismiss 遮罩。P2/P3（文本软检测）有假阳，只建议不自动跑。
  用 `_invoke_browser_method`（非 model 路径）防递归。

### Q20 · _maybe_vl_arbitrate（capability.py:866，visual.py）

- **触发**：VL `arbiter_enabled` + result 有 error_text + `is_visual_failure` + 每 worker
  `vl_arbiter_count < max_checks_per_worker`（默认 2）。
- **逻辑**：VL 仲裁，附 `vlArbiter` recommendation（resolvedId/hitl/dismiss/reperceive）
  + next_instruction。
- **作用**：Role D 视觉仲裁，给确定性恢复救不回的视觉类失败一个 VL 第二意见。
  best-effort，不抛异常。

### Q21 · offload_large_tool_result（capability.py:869，offload.py）

- **逻辑**：超大 result 卸盘，只留摘要给 model。
- **作用**：与 Q6 类似但针对最终 model_result，防 context 撑爆的最后一道。

### Q22 · _observe_progress_after（capability.py:877，progress_obs.py）

- **逻辑**：调 `ProgressAccountant.after_tool`，artifact 增长时清零 stall 计数；记录
  repairMerge。
- **作用**：与（观察版）progress_before 配对，产出有进展就清账，让 stall 观察重新计数。

### Q23 · trace.append(browser_call)（capability.py:880）

- **逻辑**：清洗后的 model_result 进 model-facing trace。
- **作用**：既是审计/回放的数据源，也是 P5 关卡二"穷尽证明"的取证来源。

**收尾（回到主循环）**：result 回到第五章的 O1-O6：diagnostics 观测 -> offload ->
状态边界断批 -> should_stop 终止 -> 循环外 classify_terminal_status。

---

# 十、终态裁定与 final answer 输出

**背景**：final answer 不是一句话那么简单。worker 层要防"模型谎报 done"，Lead 层要对账
"计划里的 phase 是否真的全部 validated"。整条输出链是三级瀑布：

## 10.1 Worker 级：final_answer 与终态分类

1. 模型调 `final_answer` 工具（`dispatch.py:1026 _browser_final_answer`）：
   收 answer/status/artifacts。**terminal handler 可软拒**——如声明 `target_absent` 但
   没有视觉核查记录时 `tool_was_executed=False` 弹回模型重做（而非一刀切终止）。
2. loop_guard 对 final_answer 豁免（软拒后参数一样的重试不该被掐）。
3. `should_stop=True` → 主循环取 answer/status → **`classify_terminal_status`**
   （`harness/diagnostics/__init__.py:173`）：diagnostics 硬信号（HITL 等待/超时、
   page_settled、契约错误、路由失败）可**覆盖**模型自报的 done。如
   `WORKER_STATUS_PAGE_SETTLED_AFTER_HITL` 的语义：页面过了挑战但 ABCP 仍报 paused，
   平台尚未释放控制通道。
4. finally 必落盘 context snapshot；worker 正常/异常退出都经 spawner 回收
   （`abandon_worker` 清 barrier 所有权）。

## 10.2 Phase 级：产物校验与状态落账

spawner 在 worker 结束后（`spawner_worker.py:560` 之后）：

- `_write_worker_trace` + `_summarize_worker_trace`（含 `advertisedMethodsNeverCalled`
  ——区分"worker 没做 X"与"phase 做不了 X"，防 Lead 误判能力边界而错误 replan）；
- `validate_worker_artifacts`（task_control）：契约要求的 artifact 是否真的存在且有效、
  修复 manifest 的视觉证据是否补齐；失败可把 phase 判 failed 触发重试/replan；
- phase attempt 记账（`validated_done` 才算依赖完成，喂 S2 pacing 与 S1）。

## 10.3 Lead 级：final_answer 三重对账

`_lead_final_answer`（`harness/tools/lead_tools.py:2517`）：

```
lead 调 final_answer(status, answer)
├─[1] resume instruction gate（resume 场景指令未消化则拒）
├─[2] build_completion_receipt (results/completion_receipt.py:318)
│      从 task_state+spawner 汇总：哪些 phase validated、哪些还有缺口
├─[3] terminal_consistency_contradictions (:15)
│      提议的 done 与原始 worker 回执矛盾 → rejected_terminal_inconsistency 软拒，
│      next_instruction 指示"继续跑完或改报非 done"
├─[4] _reconcile_final_answer_numbers
│      answer 里的数字声明 vs record_extraction artifact 行数对账，
│      不一致 → 拒绝并要求修正后重新 final_answer
└─ 通过 → 返回 {status, answer, trigger: lead_decided, completionReceipt}
```

之后 LeadAgent.run 返回 answer → main.py `print(answer)` + 任务 ID/目录 → **用户拿到
final answer**（数值都经过账本对账，phase 缺口在 completionReceipt 里可见）。

**完整输出时序**：

```
BrowserAgent           Spawner                LeadAgent              main.py   用户
  │ final_answer(软拒?)  │                        │                     │        │
  │ classify_terminal   │                        │                     │        │
  │ _write_agent_final  │                        │                     │        │
  ├────────────────────►│ validate artifacts     │                     │        │
  │                     │ phase attempt 落账      │                     │        │
  │                     ├─── worker result ─────►│                     │        │
  │                     │ abandon_worker(回收)   │ wait/replan/spawn…  │        │
  │                     │                        │ final_answer(三重对账)│        │
  │                     │                        ├────────────────────►│ print  │
  │                     │                        │                     ├───────►│
```

---

# 附录 A：FleetAuthBarrier 状态机

**背景**：一个 fleet = 一套共享登录态（同一 cookie jar）。弹出验证码时出两种事故：
多个 worker 同时解题互相覆盖；解完后登录身份变了，各 worker 手上的页面/AXTree id 全失效
却不自知。Barrier 管两件**互相独立**的事：**①门（现在谁能动）②版本号（你的信息过期了吗）**。

**实现**：`harness/fleet/runtime.py:2030`（接线 P11/Q14，bindings.py:550）。

```
门开着吗?
├─ 开着 ────────────► 过
└─ 关着 ── 解题人是谁?
          ├─ 是我 ──► 过
          ├─ 是别人 ► 等最多 120s，超时返 fleet_auth_gated(retryable)
          └─ 没人 ──► _resolver_required_receipt，引导去 claim/takeover

状态迁移（4 个动作）：
  claim          关门，我当解题人        fleet/runtime.py:2219
  claim_ownerless 抢占无主 barrier        fleet/runtime.py:2252
  resolve        开门，generation +1      fleet/runtime.py:2359  ← 唯一开门的路
  relinquish     门仍关，位子空出         fleet/runtime.py:2380
  abandon_worker 门仍关，位子空出         fleet/runtime.py:2411  ← worker 死亡时 spawner 代清
```

- **超时永不开门**（类 docstring：*"A timeout never opens the gate"*）——等满 120s 不代表
  验证码消失。
- **门关且无主时放行 4 个方法**（Page.getState / Page.create / DOM.getAXTree /
  Hitl.requestPause）：先看清现状才能决定是否接手；它们随后走 P17/P19 做真正的原子 claim。
- **Workflow.execute 独立 fence**：撞上别人在解**立即拒**（不等）；**即使自己是解题人也拒**
  ——workflow 是不透明批量执行，鉴权态变化期间无法事后判定哪几步基于旧身份。
- **generation 版本号**：门每开一次 +1。worker 的 seen_generation 对不上 → 锁进重感知模式
  （`_REPERCEPTION_ALLOWED_METHODS`），`Page.getState` 和 `DOM.getAXTree` **两步都做完才算数**
  （目标 generation 锁存，防两步互相擦记录的死锁）。
- **fail-closed 无永久死锁**：claim 在 requestPause 路径（P19）；resolve 仅在
  `wait_result=="resumed"` 时；pause 失败/恢复失败走 Q9/Q11 relinquish；worker 死亡走
  abandon_worker。最坏是有界 120s 延迟，非死锁。

---

# 附录 B：代码地图

| 关注点 | 文件 |
|---|---|
| CLI 入口 / resume | `main.py`（run_cli:1544、main:1912） |
| 双 Agent 主循环 | `agent_harness.py`（BrowserAgent:676、run:775；LeadAgent:2138、run:3319） |
| spawn 门禁 / worker 生命周期 | `harness/spawner/spawner_core.py`（spawn_browser_agent:583）、`spawner_worker.py`（_run_browser_worker:280）、`spawner_registry.py`（slot sync/quarantine TTL）、`spawner_slots.py` |
| phase 状态机 / pacing / 指纹账本 | `harness/task_control/`（phase_lifecycle.py、fingerprints.py、plan_validation.py、replan.py、state_store.py） |
| 工具分发 | `harness/tools/browser_tools/dispatch.py`（dispatcher:349、execute_browser_tool:394、impl:465） |
| 前后门禁 + runner.call | `harness/tools/browser_tools/capability.py`（model 路径 :42、internal 路径 :888） |
| fleet/page 绑定、auth barrier 接线 | `harness/tools/browser_tools/bindings.py` |
| HITL / autosolve / post-HITL 恢复 | `harness/tools/browser_tools/hitl.py`、`captcha_autosolve.py`、`harness/hitl.py`（wait_for_hitl_resume:615） |
| 页面生命周期守卫 | `harness/observation/page_lifecycle.py`（接线 dispatch.py:155） |
| Runtime.evaluate 政策 | `harness/runtime_evaluation.py` + `browser_tools/runtime_eval.py` |
| 进度观察（原进度门） | `harness/progress.py` + `browser_tools/progress_obs.py` |
| 重复调用观察 | `harness/tools/loop_guard.py`（DUPLICATE_CALL_STOP_AT:42） |
| overlay 自动拦截 / VL 仲裁 / 现实核查 | `browser_tools/auto_intercept.py`、`visual.py`、`harness/vl/` |
| FleetAuthBarrier / FleetClickGate | `harness/fleet/runtime.py`（:2030 / :779）、`fleet/coordinator.py` |
| 计划机械校验 / PlanValidator 独立审计 | `harness/task_control/plan_validation.py`、`harness/planning/validator.py`（review_plan_revision:1061、审计规则/证据目录/数量放宽分析）、`agent_harness.py:2263`（review_task_plan_candidate） |
| 终态分类 | `harness/diagnostics/__init__.py`（classify_terminal_status:173） |
| 完成回执 / 终态一致性 / 数值对账 | `harness/results/completion_receipt.py`、`harness/tools/lead_tools.py:2517` |
| ABCP 客户端 / 渲染恢复 runner | `abcp_client.py`（ABCPClient:267）、`harness/observation/render_recovery.py`（:48/:80） |
| 上下文压缩 / 卸载 | `harness/compaction.py`、`harness/offload.py` |
| 存储层 | `harness/storage/`（sqlite/file 双后端、schema.sql） |
