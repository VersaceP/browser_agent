# harness 层机械门禁分析

> 生成日期:2026-08-05
> 范围:一次 `browser_call` 从 LLM 产出到 ABCP 落地再返回的完整门禁链路。
> 目的:厘清每个机械门禁的入口/出口、针对场景、相对 `runner.call` 的前后位置、死锁/冗余/边界硬度。
> 所有引用带 `file:line`,便于跳转源码。

---

## 目录

- [一、结论先行](#一结论先行)
- [二、调用脊:三层架构](#二调用脊三层架构)
- [三、完整时序图](#三完整时序图)
- [四、逐点详解](#四逐点详解)
  - [A. 主循环层](#a-主循环层-agent_harnessruncagent_harnesspy606)
  - [B. spawn 一次性门(worker 启动前)](#b-spawn-一次性门worker-启动前spawnerpy)
  - [C. dispatch wrapper](#c-dispatch-wrapperbrowser_tools456)
  - [D. 中层 execute_browser_tool](#d-中层-execute_browser_toolbrowser_tools482)
  - [E. 内层前置门(runner.call 之前)](#e-内层前置门-_execute_browser_capability_toolbrowser_tools2066)
  - [F. 实际调用](#f-实际调用)
  - [F.bis 内部调用路径 _invoke_browser_method](#fbis-内部调用路径-_invoke_browser_methodbrowser_tools2907)
  - [G. 内层后置门(runner.call 之后)](#g-内层后置门runcall-之后)
  - [H. 收尾(回到主循环)](#h-收尾回到主循环)
- [五、死锁分析](#五死锁分析)
- [六、冗余分析](#六冗余分析全部为良性-defense-in-depth)
- [七、边界过硬分析](#七边界过硬分析)
- [八、总评与待修项](#八总评与待修项)

---

## 一、结论先行

- **门禁分四层**:主循环层(`agent_harness.py:run`)、dispatch wrapper 层(`browser_tools:dispatch`)、中层(`execute_browser_tool`)、内层(`_execute_browser_capability_tool`)。**唯一 model-initiated 的落地点**:`runner.call`(内层 `browser_tools/__init__.py:2481`);harness 自发起的调用另走 `_invoke_browser_method` 减配链路(见 [F.bis](#fbis-内部调用路径-_invoke_browser_methodbrowser_tools2907)),门禁**不是全覆盖**的。
- **没有死锁**。`progress_gate` ↔ HITL 之间**互不等待**;`FleetAuthBarrier` 走 fail-closed + `abandon_worker` 三路径回收 + takeover 恢复,最坏是 120s 有界延迟,不是死锁。
- **唯一活锁风险(中危)**:page quarantine 无 TTL——`page_settled_after_hitl` 下页面永久 quarantined、无自动关页重建。
- **没有真正的冗余**。`_check_progress_before` 5 处调用互斥;forbidden methods 双检、dependency 双检都是 defense-in-depth。
- **边界过硬**:全是设计意图的语义终态(`target_absent` / `objective_exhausted` / `phase_locked` / `blocked_by_dependency`),都需要 replan/final_answer 解;瞬时 infra 故障已被隔离不误烧预算。**没有"该降级却硬拒"的实例**。

---

## 二、调用脊:三层架构

```
spawner._run_browser_worker (spawner.py:3726)
  └─ await harness.run(worker_task)            (spawner.py:3990)  ← 控制权交出
      └─ BrowserAgent.run(task)                (agent_harness.py:561)  ← per-step 主循环
          └─ dispatch(tool_call, step)         (agent_harness.py:963)
              = build_browser_tool_dispatcher.dispatch  (browser_tools/__init__.py:456)
```

- **spawner.py 不含 per-step 循环**,只负责建 slot/客户端/fleet 绑定 + pre-worker 一次性门禁,然后 `await harness.run()`。
- 主循环真正包住 `dispatch` 的是 `agent_harness.py:run()`。
- `dispatch` 内部三层:`dispatch` wrapper → `execute_browser_tool` → `_execute_browser_capability_tool`。

---

## 三、完整时序图

时序图分两层:**上层**是 worker 生命周期(spawn 一次性门 S1-S5,包住整个 `run()`),**下层**是 per-step 主循环(`run()` 内部的一次 step)。S1-S5 任一 reject 即不 spawn worker,短路返回 rejection,不进入下层。

### 3.1 上层:worker 生命周期(spawn -> run -> 回收)

```
spawn_browser_agent (spawner.py:552)
│  ╔════ worker 启动前一次性门(任一 reject 即不 spawn,短路返回) ════╗
├─[S1] phase_start_rejection              (task_control:4391 / spawner:737)
│        phase terminal / already_running / attempts≥max
├─[S2] phase_pacing_remaining_seconds      (task_control:4244 / spawner:746)
│        phase_interval_seconds 未到期 -> sleep 后重跑 S1(@770)
├─[S3] repeated_phase_attempt_guard        (task_control:3663 / spawner:785)
│        同契约同签名连续失败 -> phase_locked_must_finalize(硬锁)
├─[S4] spawn_acquisition_rejection         (task_control:3415 / spawner:804)
│        同 fingerprint 启动错误超限 -> spawn_infrastructure_exhausted(硬锁)
│  ╚══════════════════════════════════════════════════════════════╝
│
├─ slot 获取流程
│   └─ _sync_slot_registry (spawner:2708,调用点 1860/2117/2434)
│       ├─[S5] Page.getState 报 paused / 错误文本含 err_page_paused
│       │        -> _mark_page_quarantined (spawner:3023,doNotUse=True)
│       │        清除:见非 paused -> _clear_page_quarantine (spawner:2822)
│
├─ fleet 绑定 / worker_contract 组装
└─ _run_browser_worker (spawner:3726)
    ├─ 建 worker_runtime / provider / event_logger / harness
    ├─ skill fast-path handoff (spawner:3987)
    │  ┌────────────────────────────────────────────────┐
    └─►│ await harness.run(worker_task) (spawner:3990)   │
       │   ↓ 进入 per-step 主循环(见 3.2)↓              │
       └────────────────────────────────────────────────┘
    └─ worker 终止(正常/异常/取消)
        └─ abandon_worker (spawner:4285/4336)  回收 FleetAuthBarrier(门关清 owner)
```

> S5 的 page quarantine 是这一层唯一的活锁风险点:无 TTL、清除条件苛刻(见[死锁分析 §五.3](#五死锁分析))。`abandon_worker` 是 FleetAuthBarrier fail-closed 回收的兜底(见[死锁分析 §五.2](#五死锁分析))。

### 3.2 下层:per-step 主循环(`harness.run()` 内部一次 step)

```
agent_harness.run()  step ∈ [1, max_steps]                    agent_harness.py:606
│
├─[G1] compact_messages_if_needed ……………………… 不阻断(可被cache压力强制)
├─[G2] lifecycle.agent_before_step …………………… 默认 no-op(空中间件)
├─[G3] provider.generate_response + 5类异常收容  LLM调用;异常降级为空turn
├─[G4] if not tool_calls: truncation_streak 守卫  streak≥3 -> WORKER_INCOMPLETE 终止
├─[G5] mixed_runtime_indices 检测 ……………… Runtime.evaluate 与他tool混批
│
└─ for tool_call in tool_calls:
   ├─[G6] runtime_batch_rejected -> 短路(跳过 dispatch)        ← 主循环层唯一的tool拒绝
   │
   └─ dispatch(tool_call, step)                               browser_tools:456
      ├─[D0]  lifecycle.tool_pre_call ……………………… 默认 identity
      ├─[D1]  execute_browser_tool                            browser_tools:482
      │   ├─ terminal handler?(final_answer等)-> 走终态分支
      │   ├─[D1a] _call_extraction_progress_gate … 未落盘行门(MAX_BLOCKS=2降级)
      │   ├─[D1b] check_tool_call_loop ……………… loop_guard(warn@3-10/force@5-20,按method分桶)
      │   │
      │   └─[D2] _execute_browser_capability_tool ……………… browser_tools:2066
      │       ╔══════════ 前置门禁(runner.call 之前) ══════════╗
      │       ├─[P1]  parse_browser_call_params       params_error
      │       ├─[P2]  capability_methods 成员校验       未知method
      │       ├─[P3]  _prepare_navigation_context
      │       ├─[P4]  _normalize_screenshot_output
      │       ├─[P5]  Runtime.evaluate policy+escalation   严格world+main fallback授权
      │       ├─[P6]  Workflow.execute enable+validate     workflow_execution_enabled
      │       ├─[P7]  _check_worker_contract              contract_check
      │       ├─[P8]  _check_cross_task_memory_scope      Memory跨任务
      │       ├─[P9]  _apply_fleet_binding                fleet绑定
      │       ├─[P10] _check_page_binding                 page绑定/认领
      │       ├─[P11] _fleet_auth_barrier_before_call     fleet auth gate(等≤120s,retryable)
      │       ├─[P12] _page_lifecycle_guard_before        loading等settlement/resync/ax_refresh
      │       ├─[P13] _check_screenshot_misuse
      │       ├─[P14] _check_target_param_requirements
      │       ├─[P15] _check_stale_axtree_target          过期AXTree id
      │       ├─[P16] _check_progress_before              产出预算门(主路径)
      │       ├─[P17] ensure_required_purpose / _ensure_hitl_request_reason
      │       ├─[P18] _claim_ownerless_fleet_auth_barrier_for_page_create  Page.create抢占
      │       ├─[P19] _maybe_autosolve_before_model_pause  captcha VL自解(成功则不pause,短路)
      │       ├─[P20] _claim_fleet_auth_barrier_for_hitl  HITL claim barrier
      │       ╚══════════════════════════════════════════════════╝
      │       │
      │       │  ┌─────────────────────────────────────────┐
      │       └─►│[CALL] runner.call(method, params)        │ ← 唯一真正打ABCP的点
      │          │   (render_recovery_runner;含Download/   │
      │          │    Runtime main-world fallback)          │
      │          └─────────────────────────────────────────┘
      │       ╔══════════ 后置观察/补救(runner.call 之后) ═════════╗
      │       ├─[Q1]  Download.list/start 对账
      │       ├─[Q2]  Runtime.evaluate main-world fallback + world证据校验
      │       ├─[Q3]  _page_lifecycle_after_action
      │       ├─[Q4]  _capture_artifacts / detect_structural_challenge  AXTree挑战帧
      │       ├─[Q5]  Page.list过滤 / _precompute_axtree_snapshot
      │       ├─[Q6]  _offload_response
      │       ├─[Q7]  Hitl.requestPause成功->_enrich_pause_with_wait  等resume(7出口)
      │       ├─[Q8]  _quarantine_workflow_result_after_auth_change  auth gen变更隔离
      │       ├─[Q9]  _relinquish_fleet_auth_resolver_after_failed_pause
      │       ├─[Q10] _assigned_fleet_lost_result / _recover_page_create_32005
      │       ├─[Q11] _relinquish_..._after_failed_recovery_page_create
      │       ├─[Q12] _observe_page_binding_after
      │       ├─[Q13] _fleet_auth_barrier_after_call
      │       ├─[Q14] _maybe_auto_hitl_for_challenge  挑战->auto HITL(带cooldown+post-resume guard)
      │       ├─[Q15] _observe_content_completeness_after
      │       ├─[Q16] _observe_navigation_progress_after
      │       ├─[Q17] _observe_axtree_state_after  staleness
      │       ├─[Q18] _maybe_auto_intercept_overlay  P0/P1自动dismiss(P2/P3只建议)
      │       ├─[Q19] _maybe_vl_arbitrate  VL Role D仲裁(每worker≤2)
      │       ├─[Q20] offload_large_tool_result
      │       └─[Q21] _observe_progress_after
      │
      ├─[D0b] lifecycle.tool_post_call ……………………… 默认 identity
      └─[D3]  _maybe_reality_check ………… VL视觉现实核查(shortfall≥3,每worker≤1)
   │
   ├─[O1] _observe_tool_result -> diagnostics ……… HITL/auth/captcha信号入口
   ├─[O2] page_observer / loop_nudge …………………… 观测,不阻断
   ├─[O3] offload_tool_result_for_model
   ├─[O4] _tool_call_state_boundary …………………… 状态变更边界
   ├─[O5] should_stop/boundary -> 后续tool_call deferred(tool_was_executed=False)
   └─[O6] should_stop -> should_finish=True, break
│
└─[POST] classify_terminal_status ……… 读diagnostics判终态,可覆盖model报的done
```

---

## 四、逐点详解

每点给四要素:**位置 / 触发 / 逻辑 / 作用**。

### A. 主循环层 `agent_harness.run()`(agent_harness.py:606)

这一层包住整个 step,只管"LLM 还能不能正常产出 tool_call"和"批内能不能混批",**不碰** HITL/契约/进度(全在 dispatch 内)。

#### G1 · compact_messages_if_needed(agent_harness.py:613)
- **触发**:每个 step 入口;或上一步 `_observe_cache_pressure` 置位了 `_forced_compaction_reason`。
- **逻辑**:按 token 预算/上下文膨胀阈值压缩 messages,可走 lifecycle 的 `compact_before/after` 钩子。
- **作用**:防 context 溢出。**不阻断**——压缩完照常进 LLM 调用。cache 压力可强制触发,避免软性提醒被模型无视。

#### G2 · lifecycle.agent_before_step(agent_harness.py:625)
- **触发**:每步。
- **逻辑**:中间件 fold。`default_lifecycle_manager()` 无 middleware,基类直接 return payload。
- **作用**:预留扩展点。**当前 no-op**,不是真门禁。

#### G3 · provider.generate_response + 5 类异常收容(agent_harness.py:638)
- **触发**:每步调 LLM。
- **逻辑**:catch `LLMEmptyResponseError` / `LLMConnectionError` / `LLMRequestTimeoutError` / `LLMProviderProtocolError`(4 类异常 + 正常返回 = 5 种),统一置 `model_call_failed=True`,产出 `text="", tool_calls=[], stop_reason=<kind>`,usage 只带 retry 次数。
- **作用**:把瞬态网关故障**降级成空 turn**,交给 G4 处理,而不是让异常冒泡烧掉整个 phase attempt。`model_call_failed` 单独走 `record_llm_retries`,避免空调用当真实 cache miss 干扰下一条真实调用的 cache 度量。

#### G4 · truncation streak 守卫(agent_harness.py:769)
- **触发**:`not tool_calls`(空/截断/连接断/超时/协议错/bare end_turn)。
- **逻辑**:按 stop_reason 分类 incident(connection/timeout/protocol/truncated/empty),`truncation_streak += 1`。streak < `TRUNCATION_STREAK_LIMIT`(=3):注入 recovery 提示后 `continue`;≥3:`should_finish=True` break,终态 `WORKER_STATUS_INCOMPLETE`。
- **作用**:防 LLM 卡死在无 tool turn,且不让空 turn 被误判成"模型自报 done"绕过 step-cap 和 final_answer blocker 通道。**硬边界**:连续 3 次空 turn 烧 worker。

#### G5/G6 · Runtime.evaluate 混批短路(agent_harness.py:931)
- **触发**:同批 `len(tool_calls) > 1` 且含 `_is_model_runtime_evaluate_call`。
- **逻辑**:标记 `mixed_runtime_indices`;遍历命中即 `result = _runtime_batch_boundary_rejection()`、`should_stop=False`,**跳过 dispatch**。
- **作用**:Runtime.evaluate 改隔离 world 状态,与别的 tool 混批会踩状态边界。**主循环层唯一的 tool 拒绝**。

### B. spawn 一次性门(worker 启动前,spawner.py)

非 per-step,worker 生命周期门,任一 reject 即不 spawn。在 `spawn_browser_agent` 里跑一次。**时序位置见 §3.1 上层。**

#### S1 · phase_start_rejection(task_control.py:4391,接线 spawner.py:737)
- **触发**:phase 状态 terminal / already_running / attempts≥max_attempts(默认3)。
- **逻辑**:读 phase state,返回 `phase_not_startable` / `phase_already_running` / `phase_exhausted`。
- **作用**:防终态 phase 重起、防并发同 phase、防重试预算耗尽后无脑再起。

#### S2 · phase_pacing_remaining_seconds(task_control.py:4244,接线 spawner.py:746)
- **它不是"每 N 秒才能 spawn 一次"的限流器**。docstring 原话:*"Remaining **dependency-to-start delay** for a phase."* 锚点是**依赖 phase 的完成时刻**,不是上次 spawn 时刻。语义:依赖做完之后先晾一会儿再开始下一步(反爬节流)。
- **触发**(任一不满足即返 0.0,不等):
  1. phase 在 plan 里存在;
  2. **`depends_on == []`(显式空)-> 直接 0.0**,独立 phase 永不等待(`_phase_dependency_ids` task_control:4222 区分 `None`=省略=隐式依赖所有前序 phase / `[]`=显式独立);
  3. 合并后 `phase_interval_seconds > 0`(**`DEFAULT_PACING` 全 0,不配就完全不生效**);
  4. 每个依赖 phase 在 task_state 里有记录;
  5. 每个依赖有 **`validated_done`** 的 attempt(`_attempt_was_validated_done` task_control:4317)——只是"跑过"不算;
  6. `now - max(依赖完成时刻) < interval`。
- **逻辑**:`interval = jittered_interval(phase_interval_seconds, jitter_ratio)`(pacing.py:53)——**带随机抖动**,在 `[interval×(1-jitter), interval×(1+jitter)]` 上均匀取样(固定间隔本身就是机器特征,抖动是反爬核心而非可选装饰)。返回 `max(0, interval - elapsed)`;spawner 侧 `asyncio.sleep(phase_wait)` 后**重跑 S1**。
- **pacing 三层合并**:`merge_pacing(plan.pacing, phase.pacing, contract.pacing)`(pacing.py:42),**后覆盖前**,优先级 `worker_contract > phase > plan`。Lead 在 emit_task_plan 的 plan 级与 phase 级均可写(lead_tools.py:320),上限 86400s,`jitter_ratio` 限 0~1。
- **等待期间不占 slot**:spawner 顺序是 S1 -> S2(sleep) -> **S1 再跑一次** -> S3 -> S4 -> 之后才 reserve slot;`wait_payload` 里的 `"slotReserved": False` 就是记录这点。故不阻塞其他 phase 抢 slot。
- **为何醒来必须重跑 S1**:`await asyncio.sleep()` 是让出点,Lead 可并发发多个 spawn。睡眠期间同一 phase 可能被另一路抢先跑完(转 terminal)或正在跑(running),不重查会起重复 worker。
- **兄弟机制**:同一 `pacing` 对象里的 `row_interval_seconds` 走 `wait_between_rows`(pacing.py:69),是 worker 内的**行级**节流(最后一行不等),与本门共用 `jitter_ratio`。
- **作用**:依赖完成 -> 本 phase 启动之间的反爬节流延迟。良性,不互等。

#### S3 · repeated_phase_attempt_guard(task_control.py:3663,接线 spawner.py:785)
- **触发**:同 contract_hash 的 phase 最近 2 次 attempt 均失败、同 failureSignature、连续同签名≥2。
- **逻辑**:`rejectionCount<3` -> `phase_classification_repeated`;≥`REPEAT_GUARD_REJECTION_LOCK_THRESHOLD`(=3)-> `phase_locked_must_finalize`(**硬锁**)。
- **作用**:防"同契约同签名反复失败"的无变化重试。硬锁后只能 LeadAgent 发新 phase id 绕过。

#### S4 · spawn_acquisition_rejection(task_control.py:3415,接线 spawner.py:804)
- **管辖范围**:worker **还没被造出来之前**的失败——拿 slot / 开 fleet / 建连接。设计理由见 task_control.py:120-127 注释:这类故障*"cannot legitimately consume either phase attempts or the business objective budget"*(它不证明业务目标不可行),所以既不烧 phase attempts 也不烧 objective 预算,但需要**独立的第三本账**兜底,否则 Lead 可以对同一条坏路由无限 replan/respawn 直到自己的 step 预算耗尽。
- **不留痕是坐实的**:启动失败路径第一件事就是 `cancel_phase_running_reservation`(spawner:980),把刚 append 的那条 running attempt 删掉;且本门位于 `mark_phase_running`(spawner:816)**之前**,被它拒绝时连 attempt 都不产生。
- **账本是二层的**:`ledger[acquisition_fingerprint].signatures[error_signature] = {count, retryAtEpoch, lastError}`。
  - **第一层 = 路由指纹**(`spawn_acquisition_fingerprint` task_control:3374,**与 objective_fingerprint 不同**):`{objective(或 phase.id), reuseScope, pagePolicy, sessionKey, fleetReference, needsIsolatedSession, preferredSlotId, reuseFromWorkerId}` -> sha1[:16]。即"同一目标 + 同一套 fleet/slot/session 路由"。**改路由 = 新指纹 = 新预算**,这是设计留的正当出路。
  - **第二层 = 错误签名**(`spawn_acquisition_error_signature` task_control:3403):`f"{type(exc).__name__}:{归一化消息}"`,消息做小写/压空白/16位以上 hex->`<id>`/4位以上数字->`<n>`。故 `Fleet abc123... open timeout after 30000ms` 与 `Fleet 987fed... open timeout after 45000ms` 归一为同一签名——要认的是"同一类故障反复出现",不能被 volatile id 和毫秒数骗过。
- **两种拒绝**(for 循环内先判 cooldown 后判 exhausted):
  - `count < 2` 且 `retryAtEpoch > now` -> `spawn_acquisition_cooldown`(可恢复,回执带 `retryAfterMs`)。next_instruction 特意写 *"Do not rename or replan the phase to bypass this cooldown"*——因为 replan 会重置 phase attempts,但**这本账按路由指纹存、与 phase id 无关**,改名无效。
  - `count >= SPAWN_ACQUISITION_MAX_FAILURES`(=2)-> `spawn_infrastructure_exhausted`(**硬锁**)。next_instruction 明确区分 *"a bounded startup infrastructure failure, **not evidence that the business objective is infeasible**"*,防 Lead 把基础设施故障误报成 target_absent。
- **易看漏:cooldown 不是每次失败都设**(task_control:3507-3513)。只有两类会设 30s(`SPAWN_ACQUISITION_FLEET_COOLDOWN_SECONDS`)冷却:`FleetReadinessError`(类属性 `requires_spawn_acquisition_cooldown = True`,spawner.py:92-101)与 ABCP `-32012 fleet open timeout`。**其余失败第一次不设 cooldown**,故实际路径是:第 1 次失败 -> 立刻可重试 -> 第 2 次失败 -> 直接 exhausted。这道门比 `MAX_FAILURES=2` 字面看上去更严。
- **闭环:成功即清账**。worker 成功构造后 `clear_spawn_acquisition_failures`(spawner:1054)把整条路由的账本 pop 掉——*"A successfully started worker proves this acquisition route recovered."*
- **作用**:防基础设施级故障被无限重试,同时不让它污染 phase/objective 两层业务预算。

#### S5 · _sync_slot_registry page paused 检测(spawner.py:2813)
- **触发**:分配 worker 前同步 page 状态。
- **逻辑**:Page.getState 报 paused 或 error 文本含 `err_page_paused` -> `_mark_page_quarantined`(doNotUse=True),该 page 不分给 worker。
- **作用**:不把 paused 页分给新 worker。清除条件苛刻(见[死锁分析](#五死锁分析)的 quarantine 活锁)。

### C. dispatch wrapper(browser_tools:456)

#### D0 · lifecycle.tool_pre_call(browser_tools:458)
- **触发**:每次 dispatch。
- **逻辑**:中间件 fold,默认 identity。
- **作用**:预留钩子,当前 no-op。

#### D0b · lifecycle.tool_post_call(browser_tools:467)
- 同 D0,事后 fold。当前 no-op。

#### D3 · _maybe_reality_check(browser_tools:6273 / 接线 :476)
- **触发**:dispatch 返回后。条件:VL `enabled` + `reality_check_enabled` + `classify_target_yield` 判该 result 是"target 短缺" + `target_shortfall_streak ≥ threshold`(默认3)+ 每worker已跑次数 <1。
- **逻辑**:全页截图 + VL 对"从 worker_contract 合成的 claim"裁定,通过 `record_extraction` 落盘(让 savedPath 成为 ledger-valid evidence),附 `realityCheck` 到 result。失败不消费预算(streak 保持 armed);成功消费预算(每worker 1 次)。
- **作用**:Layer-2 视觉现实核查。专治"误归属行"——模型持续产出看似有效但实际没命中 target 的行。task-agnostic,触发靠 streak 不靠 validator 类型。best-effort,不抛异常。

### D. 中层 execute_browser_tool(browser_tools:482)

#### terminal handler 检查(browser_tools:493)
- **触发**:tool 是 `final_answer` 等终态工具。
- **逻辑**:直接跑 handler;若 `tool_was_executed is False`(软拒,如 final_answer 声明 target_absent 但没视觉核查),则 `should_stop=False` 把调用弹回模型重做。
- **作用**:终态工具可"软拒"自身,带 `next_instruction` 引导模型合规后重新 final_answer,而非一刀切终止。

#### D1a · _call_extraction_progress_gate(browser_tools:9294 / 接线 :506)
- **触发**:每个非 terminal tool 调用。条件:`next_tool ≠ record_extraction` 且 `subject_tool ∉ PROGRESS_GATE_RECOVERY_TOOLS` 且 `agent.pending_unrecorded_extraction` 存在。
- **逻辑**:首过只记账(`turns += 1`,return None);`gateBlocks ≥ PROGRESS_GATE_MAX_BLOCKS`(=2)降级清 pending 放行;否则 `gateBlocks += 1` 返回 `status=progress_gate`(`tool_was_executed=False`)。
- **作用**:Layer-3 "已抽取未落盘"门。模型已抽出结构化行但没 `record_extraction` 持久化时,强制它先 persist 或走 recovery 工具。`extraction_artifact_count` 只认路径含 `/artifacts/extractions/` 的 artifact——"信账本不信声明"。MAX_BLOCKS=2 保证不永久拦死 recovery 工具。

#### D1b · check_tool_call_loop / loop_guard(harness/tools/loop_guard.py:101,接线 browser_tools:515)

> **治的病**:模型卡在同一个调用上反复发同样的请求。模块 docstring 记着真实案例——kimi-2.6 对 `local_fs_search` 连发了 **23 次一模一样的调用**,拿到结果、没看懂、又发一遍,把 step 预算烧光。
>
> 本门只做一件事:**发现在原地打转,先警告,再掐断**。

- **触发**:tool 无注册 handler 或 `action.loop_guard=True`。`final_answer` 豁免。

- **逻辑 · 三步走**

  **第一步:给每次调用按指纹**

```
tool_call_signature(name, tool_input) = md5(json.dumps({name, input}, sort_keys=True))
```

  把「工具名 + 参数」打包成 JSON 算 md5。`sort_keys=True` 意味着 `{"a":1,"b":2}` 与 `{"b":2,"a":1}` 指纹相同——**模型换个参数顺序骗不过去**。

  **第二步:数「连着」几次,不是「总共」几次**

```python
def trailing_streak(history, signature):
    streak = 1                        # 当前这次算 1
    for prior in reversed(history):   # 从最近往前倒着看
        if prior == signature: streak += 1
        else: break                   # ← 一遇到不同的,立刻停
```

  关键是那个 `break`——只数**末尾连续**的那一段:

  | 调用序列 | 最后一次的 streak |
  |---|---|
  | `A A A A` | 4 |
  | `A A B A` | **1**(被 B 打断,从头算) |
  | `A A A B A A` | 2 |

  所以它只惩罚**真正的原地打转**。模型中间只要干点别的正事,计数就清零。这让本门比看上去宽容得多。

  **第三步:查表定阈值**

  按"这件事重复做有没有可能是合理的"分档:

  | 档 | 谁 | warn / force | 为什么 |
  |---|---|---|---|
  | **严** | `local_fs_*`、Lead 控制工具(`emit_task_plan`/`spawn_browser_agent`/`wait_browser_agents`/`list_browser_agents`/`lead_save_artifact`) | **3 / 5** | 重复调永远没意义——再搜一遍文件系统也不会变 |
  | **宽** | `Input.scroll` | **10 / 20** | 滚动本就该反复做,滚 15 次找内容很正常 |
  | | `DOM.getAXTree` / `Page.getState` / `DOM.getText` / `DOM.getAttribute` | 6 / 12 | 反复感知页面有时合理 |
  | | `Input.click` / `type` / `press` | 5 / 10 | |
  | | `Runtime.evaluate` | 4 / 8 | |
  | **默认** | 其他全部 | **4 / 8** | |

  查表前有个必要的解包:模型的 ABCP 调用几乎全包在 `browser_call` 这一个工具名下,所以要先掏出 `tool_input["method"]` 再查——否则所有 ABCP 调用都落进默认档,`Input.scroll` 的宽容就失效了。

- **两段式反应**

  **warn 阶段**(streak 达到 warn 线)——**工具不执行**,返回指令把模型顶回去:上一次的 tool_result 还在你的 context 里,先去读;已经读过就换路子——(a) 换 pattern/路径,(b) 换个 ABCP method,(c) 数据真拿不到就 `final_answer(status="extraction_inconclusive")`。回执里用 `next_stop = force_stop_at - streak` **明确告知还剩几次**,不玩含糊。`should_stop=False`,worker 继续跑,只是这一步白费。

  **force 阶段**(streak 达到 force 线)——返回 `should_stop=True`,worker 当场终止,状态 `extraction_inconclusive`,answer 写明:之前的 tool_result 已包含这次调用会返回的一切,模型没有据此行动,交给 LeadAgent 换策略或升级。

- **两个容易看错的点**

  1. **warn 拦下没执行,但这次照样计数**。`history.append(signature)` 在判定**之前**就执行,不管后面走哪个分支。所以模型无视警告继续发,streak 会一路涨 `4 → 5 → 6 → 7 → 8` 到强停——**不会因为"这次没真执行"就不算账**,否则模型可以在 warn 线上无限横跳。
  2. **`final_answer` 豁免**(注册时 `loop_guard=False`)。终态工具被软拒弹回重做时参数很可能一模一样,不豁免的话模型第 4 次尝试合规收尾就被掐死了。

- **作用**:专治"同调用同参数"死循环。warn 阶段推向 pivot/terminate,force 阶段硬停。history window=24(`HISTORY_WINDOW`),阈值常量见 loop_guard.py:28-52。

#### browser_call 直通(browser_tools:532)
- **触发**:`name == "browser_call"`。
- **逻辑**:绕过注册 handler(它只能返 JsonDict 会丢 should_stop),直通 `_execute_browser_capability_tool`。
- **作用**:让 `page_create_should_stop`(死浏览器硬停)能正确透传。

### E. 内层前置门 `_execute_browser_capability_tool`(browser_tools:2066)

runner.call 之前的 20 道门,按执行顺序。任意一道返非 None 即短路返回该 guard result。

#### P1 · parse_browser_call_params(browser_tools:2075 / 2119)
- **逻辑**:解析 `tool_input` 的 `method`/`params`/`reason`。params 不是 JSON object 即 `params_error`。
- **作用**:最早期参数合法性。失败仍跑一次 `_check_progress_before`(charge_diagnostic=False)让进度门有机会干预。

#### P2 · capability_methods 成员校验(browser_tools:2142)
- **逻辑**:`method not in agent.capability_methods` -> `ABCP capability not found`,带 known_methods 列表。
- **作用**:防模型调不存在的 ABCP method。同样跑一次 progress_before。

#### P3 · _prepare_navigation_context(browser_tools:2161)
- **逻辑**:校验/规范化 `navigation_context`(声明式导航上下文,如 route_recovery_claimed_page)。
- **作用**:让 harness 跟踪"这次调用是某个未决导航声明的延续",事后(Q15/Q16)能正确判定声明是否被消费,防重复 replay 已消费声明。

#### P4 · _normalize_screenshot_output(browser_tools:2182)
- **逻辑**:规范化 Page.screenshot 的 output 参数(格式/路径)。
- **作用**:统一截图输出契约,便于后续 offload 和 evidence 落盘。

#### P5 · Runtime.evaluate policy + escalation(browser_tools:2225;底层 harness/runtime_evaluation.py)

> `Runtime.evaluate` 能执行任意 JS,是模型能碰到的**最危险的 method**。本门因此设了**三道互相独立的关卡**,它们常被混成一件事,分开看才清楚:
>
> **① 这段 JS 该不该写成 JS** → **② 现在允不允许跑** → **③ 在哪个 world 跑**

- **触发**:`method == "Runtime.evaluate"`。

- **关卡一 · 表达式本身合不合法**(`_prepare_runtime_evaluation`)

  两条硬拒,与 world 无关:

  | 拒绝码 | 拦什么 |
  |---|---|
  | `runtime_structured_interaction_bypass` | 用 JS 替代 `Input.*` / 表单 / 上传 / 权限等**结构化交互动作**——点击就该用 `Input.click`,不该用 `el.click()` |
  | `runtime_cross_check_required` | 抽取类调用没有给出具体的 **DOM 交叉验证计划** |

  另有 `reason_kind` 必须属于 `EVAL_JS_REASON_KINDS`(runtime_evaluation.py:159),否则 `runtime_reason_kind_invalid`。

- **关卡二 · 允不允许跑(穷尽证明)**(`runtime_last_resort_evidence` runtime_evaluation.py:239)

  这道关卡的授权者**不是任何人**,是 harness 自己的记账。docstring 一句话点透:

  > The proof is derived from **harness trace entries, never from model prose**.

  模型既不能"申请"授权,也不能声称自己试过了。它必须先把所有结构化读取手段**真的调一遍**:

```python
RUNTIME_STRUCTURED_ALTERNATIVES = (
    "Page.getState", "DOM.getAXTree", "DOM.getSemanticTree",
    "DOM.getText", "DOM.getAttribute",
)
```

  取 `capability_methods ∩ 上面 5 个` 得到 `required`,倒扫 `agent.trace` 找同一 pageId 的 `browser_call` 记录。全都出现过 -> 放行;差一个 -> 拒绝 `runtime_structured_alternatives_not_exhausted`,并把 `missingAlternatives` 列给模型。

  **关键是 epoch 边界**:倒扫时一旦遇到 `_RUNTIME_EPOCH_BOUNDARIES`(`Page.navigate`/`reload`/`go`/`switchTo`/`create`、`Input.click`/`type`/`press`/`scroll`/`drag`、`Hitl.*`)就 `break`。含义是**页面状态一变,之前的尝试全部作废,得在新页面上重新试一遍**——否则模型可以在 A 页面试满 5 种读法,然后导航到 B 页面直接跑 JS。

- **关卡三 · 在哪个 world 跑**(runtime_evaluation.py:181-201)

  ⚠️ **这道关卡与关卡二完全独立**,别混:关卡二决定「能不能跑」,关卡三决定「跑在哪」。

  - 模型必须**显式声明 `world=isolated`**,否则 `runtime_isolated_world_required`。main / auto / 隐式 world **一律禁止**——错误信息原文:*"only the harness may authorize a second strict main-world attempt"*。
  - schema 不支持 isolated -> `runtime_isolated_world_unavailable`。
  - **只有 `reason_kind == "non_dom_state"`** 才把 `mainFallbackAuthorized` 置真,授权 harness 做**第二次** main-world 尝试(即 Q2 的 fallback)。且还要同时满足两条,否则仍拒:
    - schema 支持 main world,否则 `runtime_main_world_unavailable`
    - 表达式必须包含 `ABCP_MAIN_WORLD_REQUIRED:<global>` 的 throw 信号(`_MAIN_FALLBACK_SIGNAL_RE`),否则 `runtime_main_fallback_signal_required`——**必须证明"这个 global 在 isolated world 确实不存在",而不是空口要 main**

- **作用**:三道关卡各管一层——不该用 JS 的别用、没穷尽替代方案的别跑、跑也只准在隔离 world。main-world 是唯一的逃生口,且必须靠一个可验证的信号自证必要性。事后(Q2)还会校验平台**真正**在哪个 world 执行,metadata 不符判 `world_evidence_mismatch`,**防平台撒谎**。

#### P6 · Workflow.execute enable + validate(browser_tools:2224)
- **触发**:`method == "Workflow.execute"`。
- **逻辑**:`workflow_execution_enabled` 关即返 `workflow_runtime_disabled`;开则 `validate_workflow_params`(allow_runtime=False, enforce_lifecycle=True)。
- **作用**:workflow 执行默认关(skill 降级为 guidance)。开了也要校验不能内嵌 Runtime.evaluate、必须跟生命周期。

#### P7 · _check_worker_contract(browser_tools:2254)
- **逻辑**:读 `agent.worker_contract`,检查 skill_selection_declined / 任务约束。
- **作用**:worker 契约层硬约束。如 repair manifest 激活时禁止重跑全 workflow。manifest 是一张"这些字段可信、只补这几个"的契约，重跑全 workflow 等于把契约撕了。注意 disabledReason 这个逃生口——manifest 可以被标记失效（比如基线 artifact 读不到了），标记后这道拦截就不生效，退回正常路径

#### P8 · _check_cross_task_memory_scope(browser_tools:9378 / 2270)
- **逻辑**:拦 `Memory.get/save` 针对其他任务 scope。
- **作用**:防 worker 读写别的任务的 memory。

#### P9 · _apply_fleet_binding(browser_tools:2281)
- **逻辑**:校验 fleet 绑定是否齐全/冲突,返 `fleet_binding_guard`。
- **作用**:确保调用带正确的 fleetId,防跨 fleet 误操作。

#### P10 · _check_page_binding(browser_tools:2296)
- **逻辑**:校验 pageId 是否被本 worker 认领,返 `page_binding_guard`。
- **作用**:防 worker 操作别人认领的 page。事后(Q12)还观察绑定状态变化。

#### P11 · _fleet_auth_barrier_before_call(包装 def browser_tools:1739;底层 `FleetAuthBarrier` fleet_runtime.py:2087;接线 browser_tools:2345 model 路径 / :3008 internal 路径)

> **一个 fleet = 一套共享登录态(同一个 cookie jar)。** 弹出验证码时会出两种事故:解题时多个 worker 同时去点,互相刷新互相覆盖;解完后登录身份变了,各 worker 手上的页面和 AXTree id 可能全失效却不自知。
>
> 本门因此管两件**互相独立**的事——**① 挑战期间只让一个 worker 动**(门)、**② 解决之后所有 worker 重新感知**(版本号)。把这两件事拆开看,是读懂本门的关键。

- **触发**:任何 fleet 上的 browser call。

- **逻辑一 · 门(现在谁能动)**

```
门开着吗?
├─ 开着 ──────────────► 过
│
└─ 关着 ─── 解题人是谁?
            ├─ 是我 ────► 过(我正在解,当然要让我操作)
            ├─ 是别人 ──► 等最多 120s,超时返 fleet_auth_gated(retryable=True)
            └─ 没有人 ──► 返 _resolver_required_receipt,引导去 claim
```

  "关着但没人解"是最易困惑的状态,出现在解题人中途死亡或主动放弃时:挑战**还在**(门不能开),但无人处理(要找人接手)。
  **最重要的一条规则:超时永不开门**(类 docstring 原文 *"A timeout never opens the gate."*)。等满 120s 不代表验证码消失了。

  改变门状态只有 4 个动作,注意后两个的共同点是**放弃身份但不开门**:

  | 动作 | 效果 | 时机 | 位置 |
  |---|---|---|---|
  | `claim` | **关门**,我当解题人 | 我发现挑战 | fleet_runtime:2286 |
  | `resolve` | **开门**,版本号 +1 | 我确认解决 ← **唯一开门的路** | fleet_runtime:2426 |
  | `relinquish` | 门**仍关**,位子空出 | 我解不了,主动放弃 | fleet_runtime:2447 |
  | `abandon_worker` | 门**仍关**,位子空出 | 我死了,spawner 代为清理 | fleet_runtime:2478 |

- **逻辑二 · 版本号(你的信息过期了吗)**

  门每开一次 `generation += 1`。worker 自带 `seen_generation`,对不上就意味着:**在你不知情时有人解决过一次挑战,你的世界变了**。此时被锁进重感知模式(browser_tools:1808),只剩 `_REPERCEPTION_ALLOWED_METHODS` = {`Page.getState`, `DOM.getAXTree`, `Hitl.requestPause`} 可用,其余一律返 `fleet_reperception_required`。

  **`Page.getState` 和 `DOM.getAXTree` 两个都做完才算数。** 这里藏过一个死锁,注释专门记着:若每次检查都重置进度标记,两步会**永远互相擦掉对方的记录**。所以目标 generation 是**锁存**的,`seen_generation` 在两步都完成前不更新。

- **三个特殊通道**

  1. **门关着且无主时放行 4 个方法**(browser_tools:1796):`Page.getState` / `Page.create` / `DOM.getAXTree` / `Hitl.requestPause`。道理很朴素——**你得先看清现状,才能决定要不要接手**。但只给页面级诊断:`Page.list`(跨页面)不放行,也绝不让某个随便的业务调用意外变成 resolver。
  2. **`Workflow.execute` 走独立快速预检**(browser_tools:1751 -> `workflow_fence_before` fleet_runtime:2174),与普通路径有两处实质差异:

     | | 普通调用 | Workflow.execute |
     |---|---|---|
     | 撞上别人在解 | 等 120s | **立即拒绝,不等** |
     | 我自己就是解题人 | **放行** | **照样拒绝** |

     第二条最反直觉:**即使你正是那个在解验证码的 worker,也不能跑 workflow**。因为 workflow 是**不透明的批量执行**,发出去后 harness 看不见也拦不住;鉴权态正在变化时跑它,事后无法判断其中几步是在旧身份下完成的。返回值 `status=fleet_auth_gated` + `reasonKind=workflow_auth_barrier_closed`;generation 变更时则为 `status=fleet_reperception_required` + `reasonKind=workflow_auth_generation_changed`,均 retryable。
  3. **`Page.create` / `Hitl.requestPause` 的放行是为了让它们去"报名"**:它们从通道 1 出来后走 P18 / P20 做真正的原子 claim。P11 放它们过不是不管,是放它们去真正的认领点。

- **作用**:**门管"现在谁能动",版本号管"你的信息是否过期"。** 同 fleet 的 auth 挑战(captcha/login)由此串行化,只有一个 worker 当 resolver;Workflow.execute 用独立 fence 防止 opaque workflow 在 auth 变更期间起跑或被信任;generation 变更强制两步重感知。fail-closed,非硬拒——门只有 `resolve` 能开,超时不开、解题人死了也不开,只是空出位子等人接手。

- **术语对照**(代码名 ↔ 上文说法):`resolving=True` ↔ 门关着;`resolver_worker_id` ↔ 解题人;`generation` ↔ 版本号;`fleet_auth_gated` ↔ 稍后重试;`fleet_auth_resolver_required` ↔ 位子空着你来解吗;`fleet_reperception_required` ↔ 先去重新感知。状态定义(5 个字段)见 `_AuthBarrierState` fleet_runtime:2087。

#### P12 · _page_lifecycle_guard_before(def browser_tools:254;接线 browser_tools:2364 model 路径 / :3023 internal 路径)

> 模型拿到一批 AXTree nodeId 后点了个链接,页面开始跳转。此刻它手上的**所有 id 都指向一个正在消失的 DOM**——继续操作要么打空,要么打错元素。
>
> 本门职责:**页面状态变过之后,逼模型重新感知,禁止用过期句柄**。它是**事件驱动**的,这是它区别于其他门的最大特点——不轮询,等平台推事件。

- **触发**:每个 call,读 `PageLifecycleTracker.state(page_id)`。

- **先分清两类东西**(混在一起就读不懂本门)

  | | `status` | `requires_*` 义务位 |
  |---|---|---|
  | 语义 | 页面**现在**是什么状态(loading/settled/failed/crashed) | 你**欠**一次重新感知 |
  | 谁改 | 平台事件(`Page.loaded` 等) | 导航/恢复/对话框/下载 |
  | 怎么消 | 等事件到达 | 必须**实际调用**对应方法 |

  注释特意点明二者不是一回事(page_lifecycle.py:43):*"`requires_state_resync` and `requires_ax_refresh` **deliberately survive** a settle."*
  **页面加载完 ≠ 你的义务清了。** settled 只说明"不在加载中",不说明"你已经重新看过了"。

- **逻辑 · 三段**

  **第一段:DOM 探针撞上 loading 页 -> 等事件**

```python
if method.startswith("DOM.") and state.status == "loading":
    settled = await tracker.wait_for_settlement(page_id, 15.0)
```

  **只有 `DOM.*` 会等**,其他方法不等——只有 DOM 读取才依赖"页面结构已稳定"。`wait_for_settlement` 等的是一个 `asyncio.Event`,由平台推来的 `Page.loaded` / `Page.loadFailed` / `Page.crashed` 触发,**不是轮询**。超时秒数取 `page_settlement_timeout_seconds`(默认 15.0)。

  超时后**只补发一次** `Page.getState`(purpose 写明 *"One-shot resynchronization after settlement event timeout"*)为漏事件兜底:

  - 补发失败 -> `page_settlement_unknown`,原始调用**依然被拦**
  - 补发成功但仍在 loading -> `page_still_loading`

  **两个出口都明令禁止轮询**(*"Do not poll"* / *"Wait for a lifecycle event; do not poll Page.getState"*)。轮询会烧 P16 的有界诊断预算,而事件迟早会来。

  **第二段:`requires_state_resync` -> 强制 `Page.getState`**

  置位来源(page_lifecycle.py `before_action` + `observe_event`):

  | 触发 | 置哪些位 |
  |---|---|
  | `Page.navigate` / `reload` / `go` | resync + ax_refresh(**在调用发出前**就置,不等事件回来) |
  | `File.download` / `Download.pause`&#124;`resume`&#124;`cancel` | 仅 resync |
  | `Page.crashed` | resync + ax_refresh |
  | `Page.recovered` | resync + ax_refresh,`generation += 1` |
  | `Page.dialogClosed` / `File.chooserClosed` | 仅 resync |

  清除只有一条路:`observe_state_response` 收到**成功且含 data** 的 `Page.getState` 响应。有一段专门防御——render-recovery 的建议性响应、畸形响应、`tool_was_executed: False` **都不算销账**,反而把 status 打成 `failed`:

  > A render-recovery advisory and any malformed/failed getState response **must never discharge** the resynchronization obligation.

  **第三段:`requires_ax_refresh` -> 强制 `DOM.getAXTree`**

  导航/崩溃/恢复后所有 AXTree id 全部失效,只有真正调了 `DOM.getAXTree` 才清(`observe_ax_refresh`)。

  另有一个反向操作 `invalidate_ax_refresh`:Q17 事后若证明某棵树是 mid-call 竞态下取的、属于**上一个导航代次**,需要把已清的义务**加回去**,但又不能伪造一次新导航。注释原文:*"roll back that optimistic transition **without manufacturing another navigation**"*。

- **两组豁免——都是防自锁**

```python
lifecycle_recovery_methods = {"Page.getState", "Page.navigate", "Page.reload", "Page.go", "Page.close"}
is_file_control = method == "File.download" or method.startswith("Download.")
```

  1. **恢复方法自身豁免**——要求你调 `Page.getState` 来清账,就不能拦 `Page.getState`。`DOM.getAXTree` 在 ax_refresh 那道门里单独豁免。
  2. **File/Download 豁免**——不明显,注释写了理由:*"Download controls are **mutually composable** (pause -> resume/cancel)... but must not **deadlock each other**."*
     死锁长这样:`Download.pause` 置了 `requires_state_resync` -> 想 `Download.resume` -> 被拦要求先 `Page.getState` -> 但页面可能正忙/不可用 -> resume 永远发不出去,**下载卡死**。
     所以下载控制之间互不拦截,代价是"这些操作弄脏的页面状态推迟到后续 DOM 操作时才结算"——`requires_state_resync` 位仍置着,只是不拦 Download 自己。

- **作用**:导航/恢复/对话框/下载状态变更后强制重新感知,禁止用过期 DOM handle。与 P11 是同构设计但作用域不同——**P11 管 fleet 级(跨 worker 的共享登录态),P12 管 page 级(worker 内的单页状态)**;两者都在解决同一类问题:*你脚下的地基变了,先重新看一眼*。

#### P13 · _check_screenshot_misuse(browser_tools:8040 / 2338)
- **逻辑**:检测截图滥用(如无目的连续截图)。
- **作用**:防模型把截图当万能诊断浪费预算。

#### P14 · _check_target_param_requirements(browser_tools:2344)
- **逻辑**:按 method schema 校验必要 target 参数(如 click 缺 nodeId)。
- **作用**:早期参数完整性,失败也跑 progress_before。

#### P15 · _check_stale_axtree_target(browser_tools:2362)
- **逻辑**:检测 params 里的 nodeId/handle 是否来自过期 AXTree 快照。`allow_rematch` 仅在 `_browser_side_rematch_mode=="on"` 时开。
- **作用**:防模型用旧 axtree 的 id 打现在的页(导航后 id 全失效)。composite 工具可按调用 opt-in rematch。

#### P16 · _check_progress_before(progress.py:226,接线 browser_tools:2375)
- **触发**:主路径(及 4 个错误分支,见[冗余分析](#六冗余分析全部为良性-defense-in-depth))。
- **逻辑**:调 `ProgressAccountant.before_tool`,8 个子门:
  1. `diagnostic_budget_exhausted`:每导航 epoch 有界诊断预算(getSemanticTree=12/screenshot=3/reload=2),耗尽即拦。
  2. `productive_primitives_without_artifact`:30 turn 无 artifact 的原始操作,mandatory_recovery 命中可一次性穿越。
  3. `infra_error_diagnostic_bypass`:传输错误后放行 System./Fleet./Page.list 诊断。
  4. `no_artifact_progress`:非产出工具 + 无 artifact + ≥8 turn。
  5. `local_fs own_artifact_read`:读本 run 自己的 artifact(账本分析)放行。
  6. `local_fs pending_intervention`:重放上次的 local_fs 重复指令。
  7. `local_fs_without_extraction`:local_fs 搜索无 extraction ≥5。
  8. `local_fs_without_browser_action`:连续只读本地文件无 browser 动作 ≥5。
- **作用**:产出预算门,防 worker 空转。每个子门都有旁路/降级。`charge_diagnostic=False` 在错误分支用——前置拒绝不收诊断预算但仍计 stall。

#### P17 · ensure_required_purpose / _ensure_hitl_request_reason(browser_tools:2380 / 2394)
- **逻辑**:对需要 purpose 的 method 补 purpose;对 Hitl.requestPause 补 reason。
- **作用**:规范化调用元数据,让事后审计/日志能解释"为什么调"。

#### P18 · _claim_ownerless_fleet_auth_barrier_for_page_create(browser_tools:1953 / 2395)
- **触发**:`method == "Page.create"`。
- **逻辑**:认领"已关且无主"的 barrier(不开健康 fleet 的闸),用于 Page.create 恢复路径。
- **作用**:让 Page.create 能在 auth 挑战期间抢占认领 barrier 走恢复,而不是被 P11 挡死。

#### P19 · _maybe_autosolve_before_model_pause(browser_tools:8940 / 2413)
- **触发**:`method == "Hitl.requestPause"` + `autosolve_enabled` + 有挑战证据。
- **逻辑**:跑有界 VL 自解(`captcha_solve_max_retries=3` / 单次 `captcha_solve_timeout_seconds=150.0` / 总 `captcha_solve_budget_seconds=240.0` / `captcha_solve_max_episodes_per_worker=2`,见 runtime_config.py:345-360)。`_autosolve_cleared` 为真 -> 返 `captcha_auto_solved` 短路(pause 不发,barrier 在 autosolver 内部 claim+verify+release);为假 -> 把尝试写入 reason,pause 照发。
- **作用**:**在 pause 发出前**先试机器自解,成功就不打扰人。结构化证据优先:AXTree 确认的 `structural_confirmed` 不会被 VL 的 normal_loading 否掉。

#### P20 · _claim_fleet_auth_barrier_for_hitl(browser_tools:1917 / 2425)
- **触发**:P19 没短路(即将发 requestPause)。
- **逻辑**:claim barrier 成当 resolver(门关);他 worker 已 claim -> `fleet_auth_gated`。
- **作用**:确保发 pause 的 worker 持有 barrier,后续 Q7 等 resume 期间其他 worker 被 P11 挡住。

### F. 实际调用

#### CALL · runner.call(method, params)(browser_tools:2481)
- **逻辑**:经 `render_recovery_runner` 调 ABCP。含 Download.start 超时对账、Runtime.evaluate main-world fallback(条件触发二次 call)。
- **作用**:**唯一 model-initiated 的落地点**。所有前置门都是为了保证这一刻的调用合法、安全、不过期、不重复、不越权;所有后置门都是为了消化这一刻的返回。
- ⚠️ **这不是全库唯一打 ABCP 的点**。harness 自发起的调用走另一条独立链路 `_invoke_browser_method`(见 [F.bis](#fbis-内部调用路径-_invoke_browser_methodbrowser_tools2907)),带一套更薄的门,成建制地绕过 P1-P3 / P5-P10 / P13-P17。这是设计意图,但意味着门禁**不是全覆盖**的。

### F.bis · 内部调用路径 `_invoke_browser_method`(browser_tools:2907)

model 路径(CALL)之外的**第二条真实调用链路**。harness 自发起的 browser 调用都走这里,落地点在 `browser_tools:3036`(`await runner.call(method, params, **runner_kwargs)`)。

- **调用方**:composites(`navigate_verified` / `collect_items` / `fill_field_verified` / `dismiss_overlay`)、captcha autosolve、Q18 auto-intercept 的 tree refresh、post-HITL recovery、screenshot/axtree 重取等。
- **前置门(减配,5 道拦截门 + Runtime 路径禁止)**,按 L2927-3002 顺序:
  1. `_normalize_screenshot_output`(L2927,规范化,非拦截)
  2. Runtime.evaluate internal 路径禁止(L2936)--比 P5 更严:internal 路径默认全禁,只有 `_trusted_collection_runtime_token` + internal + read_only_eval 的 collect_items 模板才放行。
  3. `_check_stale_axtree_target`(L2962)--**仅 `allow_rematch=True` 时跑**(composite opt-in);默认 False 不跑(legacy 行为,注释明说"no stale guard at this layer")。
  4. `_fleet_auth_barrier_before_call`(L2972)--同 P11。
  5. `_workflow_auth_started_generation`(L2977,记账,非拦截)
  6. `_page_lifecycle_guard_before`(L2987)--同 P12,但 `lifecycle_cleanup_bypass=True` 时跳过。
  7. `_claim_ownerless_fleet_auth_barrier_for_page_create`(L2991)--同 P18。
  8. `_claim_fleet_auth_barrier_for_hitl`(L2998)--同 P20。
- **绕过的 model 路径门**:P1-P3(参数解析/方法校验/导航上下文)、P5-P8(Runtime policy/Workflow/contract/memory scope)、P9-P10(fleet/page binding)、P13-P17(screenshot_misuse/target_param/progress_before/purpose)。
- **后置减配**:`internal=True` 跳过观测链(L2922 注释:no challenge adjudication / diagnostics / progress / model-facing trace),只保留 `_page_lifecycle_after_action` / `_capture_artifacts` / `detect_structural_challenge` 等调用副作用处理。即不跑 Q14 auto_hitl、不跑 Q19 vl_arbitrate、不计 progress、不进 model trace。
- **作用(设计意图)**:harness 自发起的调用可信、不需要 model 路径的全套防护;且**不能污染观测链**--否则 composite 内部的一次 click 会被当成 model 的进度/挑战信号,把账本和 HITL 判定搅乱。`count_progress=False`、不进 diagnostics 是这条路径的核心约束。
- **风险提示**:internal 路径默认不跑 stale guard、不跑 progress 门、不跑 contract。若 composite/autosolve 构造的 params 本身有 bug(过期 id、错 page),不会被 P14/P15/P16 兜住,只能靠它自己的 5 道门和调用方的构造正确性。

### G. 内层后置门(runner.call 之后)

#### Q1 · Download 对账(browser_tools:2491 / 2496 / 2553)
- **逻辑**:Download.list 标记已知 download;Download.start 超时走 `_reconcile_download_start_timeout`(用 Download.list 证明操作是否存在);成功记录 download receipt。
- **作用**:Download.start 是"可能已开始但 JSON-RPC 超时"的灰区,对账防重复下载。

#### Q2 · Runtime.evaluate main-world fallback + world 证据校验(browser_tools:2562)
- **逻辑**:isolated 失败 + `mainFallbackAuthorized` + 平台发 main-world 信号 -> 二次 call 用 main world。校验平台返回的 world metadata:metadata 不符 -> 判 `world_evidence_mismatch`;metadata 缺失 -> 降级只信"harness 派发的是哪个 world",不信"平台执行的是哪个"。
- **作用**:防平台对 world 撒谎。Runtime.evaluate 失败最终被标 `blocked` + `runtimeBlocker`(final=True),禁止模型再请求 main 或重试。

#### Q3 · _page_lifecycle_after_action(browser_tools:2625 / 383)
- **逻辑**:据本次 call 更新 page lifecycle 状态(如点击后标 requires_ax_refresh)。
- **作用**:喂给下次 P12 的判断。

#### Q4 · _capture_artifacts / detect_structural_challenge(browser_tools:2626 / 2627)
- **逻辑**:capture artifacts;`detect_structural_challenge` 仅对 DOM.getAXTree 扫 lines 找挑战帧,附 `structuralChallenge`。
- **作用**:把"页面有验证码/挑战"的结构信号抽出来,供 Q14 判定是否 auto HITL。

#### Q5 · Page.list 过滤 / _precompute_axtree_snapshot(browser_tools:2633 / 2642)
- **逻辑**:Page.list 按 fleet 绑定过滤(只给模型看本 worker 该看的 page);预计算 axtree snapshot 供 Q17 staleness 比对。
- **作用**:防 page 列表泄露其他 worker 的 page;为 staleness 检测留"调用前的树"。

#### Q6 · _offload_response(browser_tools:2643)
- **逻辑**:大 response 卸盘,只留摘要进 model context。
- **作用**:防大 AXTree/截图撑爆 context。

#### Q7 · _enrich_pause_with_wait(browser_tools:2645)
- **触发**:`method == "Hitl.requestPause"` 且 pause 成功。
- **逻辑**:调 `wait_for_hitl_resume`(hitl.py:615),7 个出口:
  1. `timeout`:人工未在超时内完成。
  2. `resumed`(explicit):平台报 Hitl.resumed + `_confirm_unpaused_after_settlement` 确认。
  3. `STALE_PAUSE_DEADLOCK`(explicit):explicit_resume 后 resolvePause 被 ERR_PAGE_PAUSED 阻塞 -> `_close_deadlocked_page` 关页。
  4. `PAGE_SETTLED_AFTER_HITL`(explicit):resume 信号到了但 Page.getState 仍 paused(非 deadlock)。
  5. `resumed`(verified_settlement):settlement 事件 -> VL 裁定 passed -> 确认。
  6/7. 同 5/4 但 verified 路径的死锁/settled。
  - 子机制:`challenge_ever_confirmed` 锁存(一旦 VL 判 confirmed,后续 title 证据永久失效,只能视觉清除或 explicit resume);verifier 预算 3;title 兜底仅预算用尽且 `_title_clears_challenge` 通过(title-only 禁止的落实)。
- **作用**:等人工/VL 解决挑战并裁定真解除了。stale_pause_deadlock 是"平台不清 pause flag"的兜底——关页让新 worker 重新 claim。

#### Q8 · _quarantine_workflow_result_after_auth_change(browser_tools:2703 / 1842)
- **触发**:auth generation 在本次 call 期间变了。
- **逻辑**:隔离 workflow 结果(发起时记的 `workflow_auth_started_generation`)。
- **作用**:auth generation 变更意味着共享鉴权态变了,本次 workflow 结果可能基于旧态,隔离防误用。

#### Q9 · _relinquish_fleet_auth_resolver_after_failed_pause(browser_tools:2007 / 2710)
- **触发**:`pause_succeeded=False`(pause 根本没成功)。
- **逻辑**:弃 barrier 所有权(`resolver_worker_id=""`),**门仍关**。
- **作用**:pause 没成功的 worker 不该继续当 resolver,弃权让别的 worker takeover。注意:pause 成功但 wait 非 resumed 时**不触发**这里(见[死锁分析](#五死锁分析)的 120s 窗口)。

#### Q10 · _assigned_fleet_lost_result / _recover_page_create_32005(调用 browser_tools:2718 / 2724;def `_assigned_fleet_lost_result` :7022)
- **逻辑**:assigned fleet 丢了 -> `lost_fleet_result` + `page_create_should_stop=True`(硬停);Page.create 32005 失败 -> 走恢复。
- **作用**:fleet 丢失是致命的,硬停 worker 别再敲死 browser。

#### Q11 · _relinquish_..._after_failed_recovery_page_create(browser_tools:2035 / 2730)
- **触发**:Page.create takeover 失败。
- **逻辑**:同 Q9,弃权保持门关。
- **作用**:恢复路径失败也释放 resolver 身份。

#### Q12 · _observe_page_binding_after(browser_tools:2740 / 1473)
- **逻辑**:观察本次 call 后 page 绑定变化(如 Page.create 新建了 page)。
- **作用**:更新绑定状态供下次 P10。

#### Q13 · _fleet_auth_barrier_after_call(browser_tools:2778 / 1892)
- **逻辑**:据 result 调整 barrier 状态(成功开闸/失败保持)。
- **作用**:与 P11/P20 配对,保证 barrier 状态机闭合。

#### Q14 · _maybe_auto_hitl_for_challenge(browser_tools:8101 / 2782)
- **触发**:非 requestPause 的 call 返回后。
- **逻辑**:
  - result 已含 paused-error -> 附"别再 pause"指引(已是 paused 态)。
  - 喂 `challenge_tracker`,`cleanup_stale` 后判 adjudication:`cooldown`(`hitl_no_repause_until` 未过)/ `post_hitl_recheck`(`hitl_post_resume_guards` 每 page guard,刚 resume 的页不立即再 pause)/ `not_ready`(证据不足)/ `adjudicate`。
  - adjudicate -> `_adjudicate_and_maybe_hitl`,可能 auto 发 requestPause。
- **作用**:把结构挑战(Q4)+ 行为信号累积成"该 HITL 了"的判定,**自动**发 pause,不依赖模型自觉。cooldown + post-resume guard 防 pause 风暴。

#### Q15 · _observe_content_completeness_after(browser_tools:2797 / 4145)
- **逻辑**:跟踪页面内容完整性(如懒挂载模块是否 mount)。
- **作用**:专治淘宝详情页下半区懒挂载类问题——内容没 mount 时 target_absent 是假的。

#### Q16 · _observe_navigation_progress_after(browser_tools:2855 / 4404)
- **逻辑**:据 navigation_context 判声明是否被消费,附 `navigationContext.accepted`。
- **作用**:与 P3 配对,告诉模型"你的导航声明被采纳了吗",防 replay 已消费声明。

#### Q17 · _observe_axtree_state_after(browser_tools:2862)
- **逻辑**:用 Q5 的 precomputed snapshot + `event_serial_before`/`page_before` 检测 mid-call 的 DOM.axTreeUpdated 竞态,正确标记 stale/clean。
- **作用**:axtree staleness 追踪,喂给 P15。必须先记本次 call 的树,再跑 Q18 auto-intercept,否则 dismiss 后的树会被旧快照覆盖成 clean。

#### Q18 · _maybe_auto_intercept_overlay(browser_tools:7549 / 2877)
- **触发**:config mode ∈ {p0, p0p1}(off/suggest 跳过)。
  - P0:`errorClassification == occlusion_blocked`。
  - P1(mode=p0p1):AXTree layer `occlusionState == occluded`。
- **逻辑**:跑 `_dismiss_overlay`(button->Escape->backdrop 阶梯),每 page 上限 `AUTO_INTERCEPT_MAX_PER_PAGE`。dismiss 后:非 blocked 则 invalidate axtree snapshot(dismiss 改了页);DOM.getAXTree 调用且清掉了 overlay 则重取树。auth/paywall overlay 返 `blocked`,不自动点,保留原 error。
- **作用**:省模型一步,自动 dismiss 遮罩。P2/P3(文本软检测)有假阳,**只建议不自动跑**。auth/paywall 永不自动点。用 `_invoke_browser_method`(非 model 路径)防递归。

#### Q19 · _maybe_vl_arbitrate(browser_tools:6218 / 2883)
- **触发**:VL `arbiter_enabled` + result 有 error_text + `is_visual_failure` + 每worker `vl_arbiter_count < max_checks_per_worker`(默认2)。
- **逻辑**:VL 仲裁,附 `vlArbiter` recommendation(resolvedId/hitl/dismiss/reperceive)+ next_instruction。
- **作用**:Role D 视觉仲裁,给确定性恢复救不回的视觉类失败一个 VL 第二意见。best-effort,不抛异常。

#### Q20 · offload_large_tool_result(browser_tools:2886)
- **逻辑**:超大 result 卸盘,只留摘要给 model。
- **作用**:与 Q6 类似但针对最终 model_result,防 context 撑爆。

#### Q21 · _observe_progress_after(browser_tools:2894 / 9624)
- **逻辑**:调 `ProgressAccountant.after_tool`,artifact 增长时清零 stall 计数;记录 repairMerge。
- **作用**:与 P16 配对,产出有进展就清账,让 stall 门重新计数。

### H. 收尾(回到主循环)

#### O1 · _observe_tool_result -> diagnostics(agent_harness.py:967)
- **逻辑**:把 result 喂 `diagnostics.observe_browser_call`。
- **作用**:**HITL/auth/captcha/contract_error 等硬信号进入分类器的唯一入口**。是事后 POST 判定的数据源。

#### O2 · page_observer / loop_nudge(agent_harness.py:968 / 990)
- **逻辑**:观察 page 状态变化、重复模式 nudge。loop_nudge 明确"never blocks tool execution",只注入文本。
- **作用**:观测层,不阻断。

#### O3 · offload_tool_result_for_model(agent_harness.py:1004)
- **逻辑**:再卸盘一次给 model。
- **作用**:防 context 撑爆的最后一道。

#### O4 · _tool_call_state_boundary(agent_harness.py:1018)
- **逻辑**:判该 tool 是否"可能改浏览器状态"(非稳定读)。
- **作用**:决定是否断当前 tool 批。

#### O5 · deferred(agent_harness.py:1023)
- **触发**:`should_stop or boundary` 且批里还有后续 tool_call。
- **逻辑**:后续 tool_call 全标 `tool_was_executed=False` + `_deferred_tool_result`。
- **作用**:状态变更后,批内剩余 tool 基于过期状态,直接 defer 不执行。

#### O6 · should_stop break(agent_harness.py:1041)
- **逻辑**:`should_stop=True` -> 取 answer/status,`should_finish=True`,break。
- **作用**:终态 tool(如 final_answer 成功、loop_guard force)终止 worker。

#### POST · classify_terminal_status(agent_harness.py:1079)
- **逻辑**:读 `self.diagnostics`(O1 累积的硬信号:last_pause_pageId / hitl_resumed_observed / hitl_stale_pause_deadlock_observed / hitl_page_settled_observed / hitl_wait_timed_out / routing_failure_status / contract_errors / recent_calls)+ model_reported_status + reached_step_cap + has_extraction_artifact,判终态。
- **作用**:**HITL/auth/captcha 的最终判定在此,不在循环内**。inescapable——即使模型报 done 也会被覆盖成 hitl_*。唯一例外:stale_pause_deadlock + 模型报 done/partial + 有 artifact + 未到 step cap -> 放行(死锁页已关、在新页恢复)。

---

## 五、死锁分析

### 1. progress_gate ↔ HITL 互等?——不存在(证伪)
`ProgressAccountant.before_tool`(progress.py:226)是同步前置门,立即返回,不触碰 pause 状态;`wait_for_hitl_resume`(hitl.py:615)是 pause 后异步等待,不读 progress 计数。两者无依赖。

### 2. FleetAuthBarrier——fail-closed,无永久死锁,有 120s 延迟窗口
- claim 在 requestPause 路径(browser_tools:1931),resolve 仅在 `wait_result=="resumed"` 时调(`_verify_and_open_fleet_auth_barrier`,def browser_tools:10612,调用 :10783)。
- **关键窗口**:timeout / page_settled / stale_deadlock 三种非 resumed 出口下,barrier **既不 resolve 也不 relinquish**(因 pause 已成功,relinquish 仅 `pause_succeeded=False` 触发)。要等 worker 退出 `abandon_worker`(spawner:4285/4336,三路径都调)才释放,其他 worker 最多等 120s 拿 `fleet_auth_gated`(retryable=True)。
- 回收完整:`abandon_worker` 保持门关清 owner;其他 worker `before_call` 拿 `_resolver_required_receipt`,被引导重新 claim(takeover)或 `claim_ownerless` 走 Page.create 恢复。
- 唯一缺口:抛 `BaseException`(SystemExit/KeyboardInterrupt)且未走 finally——但 barrier 是 in-memory,进程死即消亡,无持久死锁。

### 3. page quarantine 活锁(中危,唯一的真问题)
`_mark_page_quarantined`(spawner:3023)三入口:`spawner:2814`(sync 时 Page.getState 报 paused)/ `spawner:2833`(sync 时错误文本含 `err_page_paused`)/ `spawner:3251`(stale_pause_deadlock 收尾),清除只有一处 `spawner:2822`(sync 时见非 paused)。`page_settled_after_hitl` 场景下页面实际已过挑战但 ABCP 仍报 paused -> 每次 sync 重新 quarantine。`quarantinedAt` 记了但**无 TTL/过期检查**,page 永久丢出池,无自动关页重建。

### 4. "置位后清除条件永不成立"排查
- `blocked_by_dependency`:永久 sticky,仅 replan 清除(task_control:1687)。**设计意图**(依赖终态失败不该自愈)。
- `challenge_ever_confirmed` 锁存(hitl.py:874):VL 误报后 title 证据永久失效,但被 `timeout_seconds` 兜底,非永久。
- `phase_locked_must_finalize`(rejectionCount≥3)/ `objective_exhausted`(6):硬锁,需 replan/final_answer。设计意图。
- `history_navigation_credits` 用尽:不阻塞 Page.go,只不重置 stall。不死锁。

---

## 六、冗余分析(全部为良性 defense-in-depth)

| 项 | 位置 | 判定 |
|---|---|---|
| `_check_progress_before` 5 处调用 | browser_tools:2129/2151/2260/2352/2375 | **非冗余**。4 个错误分支各自 `return` 互斥,单次调用至多触发一条或主路径。`charge_diagnostic=False` 语义="前置拒绝不收诊断预算但仍计 stall",是优先级覆盖(progress_intervention 压过原始 error)。 |
| forbidden methods 双检 | agent_harness:296 剥除 tool surface + dispatch 内 tool_policy 拦截 | 良性。模型看不到 + 调到也拦。 |
| `_dependency_blocker` 双调 | task_control:4619(选phase)+4494(spawn时) | TOCTOU 防御,轻微重复,内存 dict 查找开销可接受。 |
| HITL 信号双路径 | `_observe_tool_result`->diagnostics + `should_stop` 直接 break | 两条一致指向终止,无冲突。 |
| overlay 检测多处 | challenge_detector / overlay_detector / loop_nudge / reality_check | 各管不同层(结构帧/遮挡层/重复模式/视觉现实),非重复。 |

---

## 七、边界过硬分析

### 硬熔断(触发即不可同 phase 重试,需 replan/final_answer)

| 门禁 | file:line | 恢复方式 | 该降级却硬拒? |
|---|---|---|---|
| target_absent / instruction_infeasible / blocked_content_suppression | task_control:76 | replan 改 target / final_answer | 否(语义终态) |
| objective_exhausted(6) | task_control:4471 | 改 objective | 否,且 infra 错误不烧此预算(task_control:4049) |
| phase_locked_must_finalize(3) | task_control:3709 | replan 实质变更 | 否 |
| phase_exhausted | task_control:4445 | replan / final_answer | 否 |
| blocked_by_dependency | task_control:4381 | 仅 replan | 否(依赖终态失败) |
| stale_pause_deadlock | hitl.py:756 | 关页+新 worker 重 claim | 否(resolvePause 被 ERR_PAGE_PAUSED 阻塞时无更软选项) |

### 已正确降级的(无"该降级却硬拒")
- progress 8 个子门:诊断有导航重置、productive_primitives 有 mandatory_recovery 旁路、PROGRESS_GATE MAX_BLOCKS=2 降级、history_navigation 不阻塞只不重置。
- captcha autosolve:`max_retries=3` / `timeout=150s` / `budget=240s` / 每 worker 2 episodes,上限触发后**降级为照常发 pause**(不硬拒),交人工。
- VL arbiter / reality_check:超预算即 no-op,不硬拒。

### 潜在过硬(经核查可接受)
- **truncation streak 3 次混计硬终**(agent_harness:159):连接断/超时这类瞬态故障跨 kind 混计,3 次就烧 worker,无"对 connection/timeout 宽限更多"的降级。G3 已把瞬态异常降级成空 turn 复用 streak,但混计仍可能误杀。**低-中危,可考虑按 kind 分桶**。
- **page quarantine 无 TTL**(见死锁 3)。**中危**。

---

## 八、总评与待修项

### 总评
门禁体系分工清晰、层次正确:**主循环层**只管 LLM 退化与混批,**中层**管调用重复与未落盘,**内层前置 20 门**管参数/契约/绑定/鉴权/生命周期/产出预算,**内层后置**管挑战/遮挡/auth 变更/视觉仲裁。死锁防护靠三件事——progress 同步不持锁、barrier fail-closed + abandon_worker 回收、lifecycle 门豁免恢复方法防自锁。

### 待修项(按优先级)

| 优先级 | 问题 | 位置 | 建议 |
|---|---|---|---|
| 中危 | page quarantine 无 TTL,`page_settled_after_hitl` 下页面永久丢出池 | spawner.py:3023-3060 | 加 TTL + 自动关页重建;quarantinedAt 已记录但无人查过期 |
| 低-中危 | truncation streak 3 次跨 kind 混计硬终,瞬态故障可能误杀 | agent_harness.py:159 | 按 incident kind 分桶(connection/timeout 宽限更多) |

其余硬边界都是语义终态,本就该靠 replan/final_answer 解,不是 bug。
