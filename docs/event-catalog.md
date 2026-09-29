<!-- GENERATED FILE - do not edit by hand.
     Regenerate: python3 devtools/event_catalog.py
     Verify:     python3 devtools/event_catalog.py --check -->

# 事件与 trace 目录（自动生成）

本文件由 `devtools/event_catalog.py` 从源码 AST 提取，用于重构前证明"哪些事件
真的被产生、哪些真的被消费"。手工维护的清单会在第二天过期，所以这里只放机械
可判定的事实；语义判断留在实施计划里。

两个方向分开统计，不要混读：

- **出站**（§2-§6）：本 harness 自己写的 run event 与 trace，落在 run log /
  SQLite，读者是 Console、replay 和审计。
- **入站**（§7-§9）：WebCross 平台通过 `System.notification` 推给 harness 的
  事件。harness 只消费不产生；名字属于平台的目录，不属于本仓库。事件名与
  Action 名同形（`Page.navigate` 两者都是），所以入站侧一律以平台目录为准，
  不按字符串形状猜。


## 1. 汇总

| 指标 | 数量 |
|---|---:|
| 出站 run event 产生点 | 537 |
| 出站 run event 类型 | 431 |
| 动态（非字面量）事件名 | 43 |
| 出站 trace 产生点 | 49 |
| 出站 trace type | 38 |
| 入站平台事件（目录内） | 43 |
| 入站平台事件（安装包内） | 42 |
| 入站平台事件（Agent 可见） | 25 |
| 入站平台事件（harness 有引用） | 23 |
| 入站平台事件（Agent 可见但无引用） | 3 |

### 1.1 事件族与方向

| 族 | 方向 | 谁产生 | 谁消费 | 章节 |
|---|---|---|---|---|
| run event | 出站：harness → run log / SQLite | `logger.write`、`_write_agent_event`、`_log` 包装器 | `read_events`、Console、replay、审计 | §2-§4、§6 |
| trace | 出站：harness → `agent.trace` / `worker_trace_events` | `*.trace.append({"type": ...})` | judge、worker trace 表读者 | §5、§6 |
| WebCross 平台事件 | 入站：Dispatcher → harness（`System.notification`） | 平台（不在本仓库） | `event_observer`、reducers、`fleet.runtime`、`page_lifecycle` | §7-§9 |

入站事件不会产生 §4 里的任何一行；它们被消费后**才**可能触发一条出站 run event（已核实的两条链：`Hitl.paused` → `workflow.hitl_barrier.claimed_by_event_observer`；`Page.dialogOpened` → `page.dialog.ledger`）。这条因果只能靠 §7 的引用点与 §4 的位置对照读出，机械提取不会替它编造联系。

### 1.2 证据来源与扫描范围

- 平台源码快照 `abcp-platform/`：commit `b96c440`（`v0.9.0-53-gb96c440`，2026-09-20）→ §7 的定义、行号与 Agent 投影
- 已安装 CLI：`0.9.3-beta`
- 已安装运行时包：`/Applications/WebCross.app/Contents/Resources/app.asar`（50.2 MB，mtime 2026-09-22 23:14）→ §7 `安装包` 列的证据来源
- 出站扫描范围：`git ls-files --cached --others --exclude-standard '*.py'` 去掉 `tests/`、`docs/` 与本生成器，共 200 个文件

| run event 产生通道 | 产生点 |
|---|---:|
| `logger.write` | 419 |
| `_log` | 91 |
| `_write_agent_event` | 26 |
| `log_event=` | 1 |

## 2. 事件命名空间分布

| 前缀 | 产生点 |
|---|---:|
| `skill` | 66 |
| `spawner` | 61 |
| `task_plan` | 37 |
| `lead` | 34 |
| `browser` | 29 |
| `vl` | 22 |
| `agent` | 21 |
| `hitl` | 19 |
| `event` | 18 |
| `workflow` | 17 |
| `schema` | 15 |
| `context` | 14 |
| `{}` | 13 |
| `direct_mode` | 11 |
| `memory` | 11 |
| `auth_fleet` | 10 |
| `axtree` | 9 |
| `fleet_click_gate` | 9 |
| `fast_path` | 7 |
| `plan_validator` | 6 |
| `progress` | 6 |
| `resume` | 6 |
| `download` | 5 |
| `event_type` | 5 |
| `page` | 5 |
| `runtime` | 5 |
| `task` | 5 |
| `observation` | 4 |
| `pacing` | 4 |
| `storage` | 4 |
| `task_phase` | 4 |
| `tool` | 4 |
| `batch_source` | 3 |
| `exec` | 3 |
| `local_path` | 3 |
| `page_session` | 3 |
| `repair` | 3 |
| `tool_result` | 3 |
| `collect_items` | 2 |
| `content_completeness` | 2 |
| `loop_guard` | 2 |
| `row_ledger` | 2 |
| `strategy_attempts` | 2 |
| `task_state` | 2 |
| `transport` | 2 |
| `challenge` | 1 |
| `completion_receipt` | 1 |
| `dismiss_overlay` | 1 |
| `event_name` | 1 |
| `event_observer` | 1 |
| `extension_event` | 1 |
| `field_semantic_review` | 1 |
| `harness` | 1 |
| `log_event` | 1 |
| `loop_nudge` | 1 |
| `microloop` | 1 |
| `page_inventory` | 1 |
| `page_stats` | 1 |
| `prompt` | 1 |
| `semantic_terminal` | 1 |
| `snapshot_diff` | 1 |
| `spawn` | 1 |
| `tool_batch` | 1 |
| `worker` | 1 |

## 3. 产生点最多的文件

| 文件 | run event 产生点 |
|---|---:|
| `agent_harness.py` | 110 |
| `harness/tools/browser_tools/capability.py` | 38 |
| `harness/skill/dispatch.py` | 35 |
| `harness/spawner/spawner_slots.py` | 34 |
| `harness/tools/lead_tools.py` | 31 |
| `harness/spawner/spawner_core.py` | 27 |
| `harness/spawner/spawner_worker.py` | 18 |
| `main.py` | 15 |
| `harness/tools/browser_tools/visual.py` | 13 |
| `harness/runtime/hitl.py` | 12 |
| `harness/fleet/runtime.py` | 11 |
| `harness/observation/event_observer.py` | 11 |
| `harness/skill/control.py` | 11 |
| `harness/tools/browser_tools/hitl.py` | 11 |
| `harness/vl/captcha.py` | 11 |
| `harness/tools/browser_tools/dispatch.py` | 10 |
| `harness/skill/autoheal.py` | 8 |
| `harness/skill/contract.py` | 8 |
| `harness/tools/browser_tools/captcha_autosolve.py` | 8 |
| `harness/context/compaction.py` | 7 |

## 4. run event 类型全表（出站）

| 事件类型 | 产生点 | 通道 | 位置 |
|---|---:|---|---|
| `agent.cancelled` | 1 | `_write_agent_event` | `agent_harness.py:2326` |
| `agent.compaction_skipped` | 1 | `_write_agent_event` | `agent_harness.py:646` |
| `agent.error` | 1 | `_write_agent_event` | `agent_harness.py:2353` |
| `agent.final` | 1 | `_write_agent_event` | `agent_harness.py:3954` |
| `agent.interrupted` | 1 | `_write_agent_event` | `agent_harness.py:2429` |
| `agent.model` | 1 | `_write_agent_event` | `agent_harness.py:1769` |
| `agent.model_connection_error` | 1 | `_write_agent_event` | `agent_harness.py:1648` |
| `agent.model_degenerate_response` | 1 | `_write_agent_event` | `agent_harness.py:1627` |
| `agent.model_input_moderation_refused` | 1 | `_write_agent_event` | `agent_harness.py:1708` |
| `agent.model_protocol_error` | 1 | `_write_agent_event` | `agent_harness.py:1683` |
| `agent.model_rate_limited` | 1 | `_write_agent_event` | `agent_harness.py:2333` |
| `agent.model_timeout` | 1 | `_write_agent_event` | `agent_harness.py:1667` |
| `agent.multimodal_compaction_deferred` | 1 | `_write_agent_event` | `agent_harness.py:677` |
| `agent.multimodal_images_expired` | 1 | `_write_agent_event` | `agent_harness.py:1723` |
| `agent.multimodal_screenshot` | 1 | `_write_agent_event` | `agent_harness.py:2128` |
| `agent.step.start` | 1 | `_write_agent_event` | `agent_harness.py:1593` |
| `agent.step_cap.reminder` | 1 | `_write_agent_event` | `agent_harness.py:3585` |
| `agent.step_extension.denied` | 1 | `_write_agent_event` | `agent_harness.py:3766` |
| `agent.step_extension.granted` | 1 | `_write_agent_event` | `agent_harness.py:3797` |
| `agent.step_extension.requested` | 1 | `_write_agent_event` | `agent_harness.py:3666` |
| `agent.truncated_response` | 1 | `_write_agent_event` | `agent_harness.py:1870` |
| `auth_fleet.ledger_handler_failed` | 1 | `logger.write` | `harness/tools/browser_tools/hitl.py:1644` |
| `auth_fleet.lost_handler_failed` | 1 | `logger.write` | `harness/tools/browser_tools/page_create.py:142` |
| `auth_fleet.operator_reset` | 1 | `logger.write` | `harness/spawner/spawner_slots.py:2698` |
| `auth_fleet.reconciled` | 1 | `logger.write` | `harness/spawner/spawner_slots.py:1573` |
| `auth_fleet.resolver_claimed_for_page_create` | 1 | `logger.write` | `harness/tools/browser_tools/bindings.py:891` |
| `auth_fleet.resolver_relinquished` | 1 | `logger.write` | `harness/tools/browser_tools/bindings.py:935` |
| `auth_fleet.resolver_relinquished_after_page_create` | 1 | `logger.write` | `harness/tools/browser_tools/bindings.py:965` |
| `auth_fleet.session_release_conflict` | 1 | `logger.write` | `harness/spawner/spawner_core.py:1954` |
| `auth_fleet.session_released` | 1 | `logger.write` | `harness/spawner/spawner_slots.py:2599` |
| `auth_fleet.verified_record` | 1 | `logger.write` | `harness/spawner/spawner_slots.py:2569` |
| `axtree.event.invalidated` | 1 | `_log` | `harness/observation/event_observer.py:326` |
| `axtree.event.other_page` | 1 | `_log` | `harness/observation/event_observer.py:284` |
| `axtree.event.updated` | 1 | `_log` | `harness/observation/event_observer.py:314` |
| `axtree.invalidated` | 1 | `logger.write` | `harness/tools/browser_tools/axtree_state.py:975` |
| `axtree.invalidation_superseded_by_event` | 1 | `logger.write` | `harness/tools/browser_tools/axtree_state.py:648` |
| `axtree.parse_inconsistent` | 1 | `logger.write` | `harness/tools/browser_tools/axtree_state.py:556` |
| `axtree.rematch_observed` | 1 | `logger.write` | `harness/tools/browser_tools/axtree_state.py:918` |
| `axtree.snapshot` | 1 | `logger.write` | `harness/tools/browser_tools/axtree_state.py:590` |
| `axtree.target_resolution_fallback` | 1 | `logger.write` | `harness/tools/browser_tools/axtree_state.py:868` |
| `batch_source.derived` | 1 | `logger.write` | `harness/tools/lead_tools.py:3641` |
| `batch_source.materialized` | 1 | `logger.write` | `harness/task_control/cohorts.py:665` |
| `batch_source.not_derived` | 1 | `logger.write` | `harness/tools/lead_tools.py:3654` |
| `browser.bootstrap` | 1 | `logger.write` | `agent_harness.py:2521` |
| `browser.call.arguments_prepared` | 1 | `logger.write` | `harness/tools/browser_tools/capability.py:459` |
| `browser.call.captcha_auto_solved` | 1 | `logger.write` | `harness/tools/browser_tools/capability.py:508` |
| `browser.call.contract_violation` | 1 | `logger.write` | `harness/tools/browser_tools/capability.py:324` |
| `browser.call.cross_task_memory_rejected` | 1 | `logger.write` | `harness/tools/browser_tools/capability.py:335` |
| `browser.call.dialog_rejected` | 1 | `logger.write` | `harness/tools/browser_tools/capability.py:404` |
| `browser.call.dom_get_img_output_normalized` | 1 | `logger.write` | `harness/tools/browser_tools/validation.py:576` |
| `browser.call.fleet_auth_gated` | 3 | `logger.write` | `harness/tools/browser_tools/capability.py:379`, `harness/tools/browser_tools/capability.py:492`, `harness/tools/browser_tools/capability.py:520` |
| `browser.call.fleet_binding_rejected` | 1 | `logger.write` | `harness/tools/browser_tools/capability.py:350` |
| `browser.call.internal` | 1 | `logger.write` | `harness/tools/browser_tools/capability.py:1530` |
| `browser.call.lifecycle_gated` | 1 | `logger.write` | `harness/tools/browser_tools/capability.py:393` |
| `browser.call.navigation_context_rejected` | 1 | `logger.write` | `harness/tools/browser_tools/capability.py:242` |
| `browser.call.page_binding_rejected` | 1 | `logger.write` | `harness/tools/browser_tools/capability.py:363` |
| `browser.call.params_error` | 2 | `logger.write` | `harness/tools/browser_tools/capability.py:197`, `harness/tools/browser_tools/capability.py:414` |
| `browser.call.purpose_added` | 1 | `logger.write` | `harness/tools/browser_tools/capability.py:450` |
| `browser.call.rejected` | 1 | `logger.write` | `harness/tools/browser_tools/capability.py:216` |
| `browser.call.result` | 2 | `logger.write` | `harness/tools/browser_tools/capability.py:1039`, `harness/tools/browser_tools/capability.py:1561` |
| `browser.call.schema_rejected` | 1 | `logger.write` | `harness/tools/browser_tools/capability.py:473` |
| `browser.call.screenshot_output_normalized` | 2 | `logger.write` | `harness/tools/browser_tools/capability.py:254`, `harness/tools/browser_tools/capability.py:1279` |
| `browser.call.select_replay_blocked` | 1 | `logger.write` | `harness/tools/browser_tools/navigate.py:2497` |
| `browser.call.stale_axtree_target` | 2 | `logger.write` | `harness/tools/browser_tools/capability.py:435`, `harness/tools/browser_tools/capability.py:1320` |
| `browser.tool.routing_rejected` | 1 | `logger.write` | `harness/tools/browser_tools/dispatch.py:791` |
| `browser.transport.fatal` | 1 | `logger.write` | `harness/tools/browser_tools/capability.py:808` |
| `challenge.navigation_cleared` | 1 | `logger.write` | `harness/tools/browser_tools/navigate.py:1122` |
| `collect_items.result` | 2 | `logger.write` | `harness/tools/browser_tools/composites/collect_items.py:813`, `harness/tools/browser_tools/composites/collect_items.py:1300` |
| `completion_receipt.persisted` | 1 | `logger.write` | `harness/results/completion_receipt.py:652` |
| `content_completeness.artifact_region_credit` | 1 | `logger.write` | `harness/tools/browser_tools/record_extraction.py:143` |
| `content_completeness.observed` | 1 | `logger.write` | `harness/tools/browser_tools/navigate.py:996` |
| `context.compacted` | 1 | `logger.write` | `harness/context/compaction.py:1123` |
| `context.compaction_failed` | 1 | `logger.write` | `harness/context/compaction.py:1074` |
| `context.compaction_fallback` | 1 | `logger.write` | `harness/context/compaction.py:996` |
| `context.compaction_rejected` | 1 | `logger.write` | `harness/context/compaction.py:1097` |
| `context.compaction_requested` | 4 | `logger.write` | `agent_harness.py:3614`, `agent_harness.py:6927`, `agent_harness.py:6979`, `agent_harness.py:7592` |
| `context.compaction_skipped` | 3 | `logger.write` | `harness/context/compaction.py:913`, `harness/context/compaction.py:941`, `harness/context/compaction.py:1044` |
| `context.snapshot.failed` | 2 | `logger.write` | `agent_harness.py:2424`, `agent_harness.py:7479` |
| `context.snapshot.saved` | 1 | `logger.write` | `harness/utils.py:1076` |
| `direct_mode.classification_retry_abandoned` | 1 | `logger.write` | `main.py:2617` |
| `direct_mode.classification_retry_recovered` | 1 | `logger.write` | `main.py:2610` |
| `direct_mode.classification_retry_started` | 1 | `logger.write` | `main.py:2596` |
| `direct_mode.classify.fallback` | 3 | `logger.write` | `harness/planning/task_classifier.py:277`, `harness/planning/task_classifier.py:305`, `harness/planning/task_classifier.py:315` |
| `direct_mode.classify.result\|direct_mode.classify.fallback` ⚠动态 | 1 | `logger.write` | `harness/planning/task_classifier.py:333` |
| `direct_mode.plan_repair_abandoned` | 1 | `logger.write` | `main.py:2670` |
| `direct_mode.plan_repair_started` | 1 | `logger.write` | `main.py:2661` |
| `direct_mode.plan_resynthesized` | 1 | `logger.write` | `main.py:2675` |
| `direct_mode.plan_synthesized` | 1 | `logger.write` | `main.py:2637` |
| `dismiss_overlay.result` | 1 | `logger.write` | `harness/tools/browser_tools/visual.py:81` |
| `download.event_dropped` | 1 | `logger.write` | `harness/tools/browser_tools/downloads.py:1072` |
| `download.event_observed` | 1 | `logger.write` | `harness/tools/browser_tools/downloads.py:1096` |
| `download.operation_reused` | 1 | `logger.write` | `harness/tools/browser_tools/capability.py:577` |
| `download.reuse_invalidated` | 1 | `logger.write` | `harness/tools/browser_tools/downloads.py:569` |
| `download.timeout_reconciled` | 1 | `logger.write` | `harness/tools/browser_tools/capability.py:612` |
| `event` ⚠动态 | 18 | `logger.write` | `agent_harness.py:2614`, `agent_harness.py:4691`, `harness/fleet/runtime.py:1296`, `harness/observation/event_observer.py:331` 等 18 处 |
| `event_name` ⚠动态 | 1 | `logger.write` | `harness/tools/browser_tools/navigate.py:968` |
| `event_observer.error` | 1 | `logger.write` | `harness/observation/event_observer.py:132` |
| `event_type` ⚠动态 | 5 | `logger.write` | `agent_harness.py:1464`, `harness/evidence/extraction_artifacts.py:233`, `harness/tools/browser_tools/visual.py:637`, `harness/utils.py:912` 等 5 处 |
| `exec.segment.projected` | 1 | `logger.write` | `harness/tools/browser_tools/capability.py:1120` |
| `exec.segment.projection_skipped` | 1 | `logger.write` | `harness/tools/browser_tools/capability.py:1112` |
| `exec.segment.result` | 1 | `logger.write` | `harness/tools/browser_tools/capability.py:1208` |
| `extension_event` ⚠动态 | 1 | `_write_agent_event` | `agent_harness.py:2296` |
| `fast_path.replan_checkpoint` | 1 | `logger.write` | `harness/task_control/replan.py:520` |
| `fast_path.replan_checkpoint_business_contract_unavailable` | 1 | `logger.write` | `harness/task_control/replan.py:629` |
| `fast_path.replan_checkpoint_contract_degraded` | 1 | `logger.write` | `harness/task_control/replan.py:336` |
| `fast_path.replan_checkpoint_invalidated` | 1 | `logger.write` | `harness/task_control/replan.py:183` |
| `fast_path.replan_checkpoint_predecessor_mismatch` | 2 | `logger.write` | `harness/task_control/replan.py:293`, `harness/task_control/replan.py:309` |
| `fast_path.replan_checkpoint_progress_mismatch` | 1 | `logger.write` | `harness/task_control/replan.py:387` |
| `field_semantic_review.cache_hit` | 1 | `logger.write` | `harness/tools/lead_tools.py:5934` |
| `fleet_click_gate.acquired` | 1 | `_log` | `harness/fleet/runtime.py:912` |
| `fleet_click_gate.disabled` | 1 | `logger.write` | `harness/spawner/spawner_core.py:182` |
| `fleet_click_gate.outcome` | 2 | `_log` | `harness/fleet/runtime.py:1952`, `harness/fleet/runtime.py:1990` |
| `fleet_click_gate.reconciliation_error` | 2 | `_log` | `harness/fleet/runtime.py:1869`, `harness/fleet/runtime.py:1906` |
| `fleet_click_gate.redirected_to_auth_barrier` | 1 | `_log` | `harness/fleet/runtime.py:1462` |
| `fleet_click_gate.rejected` | 1 | `_log` | `harness/fleet/runtime.py:891` |
| `fleet_click_gate.released` | 1 | `_log` | `harness/fleet/runtime.py:762` |
| `harness.config` | 1 | `logger.write` | `agent_harness.py:6529` |
| `hitl.auto_request_pause` | 1 | `logger.write` | `harness/tools/browser_tools/hitl.py:1255` |
| `hitl.input.failed` | 1 | `logger.write` | `harness/runtime/hitl.py:648` |
| `hitl.pause_snapshot.captured` | 1 | `logger.write` | `harness/tools/browser_tools/hitl.py:1382` |
| `hitl.pause_snapshot.failed` | 1 | `logger.write` | `harness/tools/browser_tools/hitl.py:1356` |
| `hitl.post_resume.confirmation_input_failed` | 1 | `logger.write` | `harness/tools/browser_tools/hitl.py:523` |
| `hitl.post_resume.confirmation_non_tty` | 1 | `logger.write` | `harness/tools/browser_tools/hitl.py:506` |
| `hitl.post_resume.raw_call` | 1 | `logger.write` | `harness/tools/browser_tools/hitl.py:695` |
| `hitl.refused` | 1 | `logger.write` | `harness/tools/browser_tools/hitl.py:1117` |
| `hitl.wait.branch_error` | 1 | `logger.write` | `harness/runtime/hitl.py:787` |
| `hitl.wait.page_settled_after_hitl` | 2 | `logger.write` | `harness/runtime/hitl.py:872`, `harness/runtime/hitl.py:1066` |
| `hitl.wait.resumed` | 2 | `logger.write` | `harness/runtime/hitl.py:887`, `harness/runtime/hitl.py:1082` |
| `hitl.wait.settlement_check` | 2 | `logger.write` | `harness/runtime/hitl.py:906`, `harness/runtime/hitl.py:997` |
| `hitl.wait.stale_pause_deadlock` | 2 | `logger.write` | `harness/runtime/hitl.py:845`, `harness/runtime/hitl.py:1037` |
| `hitl.wait.start` | 1 | `logger.write` | `harness/runtime/hitl.py:717` |
| `hitl.wait.timeout` | 1 | `logger.write` | `harness/runtime/hitl.py:811` |
| `lead.artifact_supersession` | 1 | `logger.write` | `harness/tools/lead_tools.py:5649` |
| `lead.cancelled` | 1 | `logger.write` | `agent_harness.py:7372` |
| `lead.completion_receipt` | 2 | `logger.write` | `agent_harness.py:7437`, `harness/tools/lead_tools.py:5871` |
| `lead.completion_receipt_failed` | 1 | `logger.write` | `agent_harness.py:7448` |
| `lead.connection_blocked` | 1 | `logger.write` | `agent_harness.py:6738` |
| `lead.direct_worker.attempt` | 1 | `logger.write` | `harness/tools/lead_tools.py:1874` |
| `lead.direct_worker.continuation` | 1 | `logger.write` | `harness/tools/lead_tools.py:1946` |
| `lead.direct_worker.resumed` | 1 | `logger.write` | `harness/tools/lead_tools.py:1845` |
| `lead.empty_model_response` | 1 | `logger.write` | `agent_harness.py:7168` |
| `lead.error` | 1 | `logger.write` | `agent_harness.py:7398` |
| `lead.field_semantic_mismatch` | 1 | `logger.write` | `harness/tools/lead_tools.py:5832` |
| `lead.final` | 1 | `logger.write` | `agent_harness.py:7502` |
| `lead.interrupted` | 1 | `logger.write` | `agent_harness.py:7485` |
| `lead.model` | 1 | `logger.write` | `agent_harness.py:7067` |
| `lead.model.effective_config` | 1 | `logger.write` | `agent_harness.py:6698` |
| `lead.model.request` | 1 | `logger.write` | `agent_harness.py:6828` |
| `lead.model_connection_error` | 1 | `logger.write` | `agent_harness.py:6953` |
| `lead.model_degenerate_response` | 1 | `logger.write` | `agent_harness.py:6860` |
| `lead.model_protocol_error` | 1 | `logger.write` | `agent_harness.py:7000` |
| `lead.model_rate_limited` | 1 | `logger.write` | `agent_harness.py:7386` |
| `lead.model_timeout` | 1 | `logger.write` | `agent_harness.py:6899` |
| `lead.numeric_reconciliation` | 1 | `logger.write` | `harness/tools/lead_tools.py:6200` |
| `lead.phase_continuation.attempt` | 1 | `logger.write` | `harness/tools/lead_tools.py:4340` |
| `lead.phase_continuation.blocked` | 1 | `logger.write` | `harness/tools/lead_tools.py:4223` |
| `lead.phase_downstream_dispatch` | 1 | `logger.write` | `harness/tools/lead_tools.py:4678` |
| `lead.planning_history_handoff` | 1 | `logger.write` | `agent_harness.py:6749` |
| `lead.prompt_stage` | 1 | `logger.write` | `agent_harness.py:6770` |
| `lead.step.start` | 1 | `logger.write` | `agent_harness.py:6797` |
| `lead.step_cap.reminder` | 1 | `logger.write` | `agent_harness.py:7568` |
| `lead.tool.error` | 1 | `logger.write` | `harness/tools/lead_tools.py:2821` |
| `lead.tool.params_error` | 1 | `logger.write` | `harness/tools/parsers.py:104` |
| `lead.tool.result` | 1 | `logger.write` | `agent_harness.py:7282` |
| `lead.wait.woken` | 1 | `logger.write` | `harness/tools/lead_tools.py:5066` |
| `local_path.authorization.denied` | 1 | `_log` | `harness/tools/path_authorization.py:198` |
| `local_path.authorization.granted` | 1 | `_log` | `harness/tools/path_authorization.py:207` |
| `local_path.authorization.requested` | 1 | `_log` | `harness/tools/path_authorization.py:187` |
| `log_event` ⚠动态 | 1 | `_log` | `harness/observation/event_observer.py:181` |
| `loop_guard.observed` | 1 | `logger.write` | `harness/tools/loop_guard.py:139` |
| `loop_guard.spend_limit` | 1 | `logger.write` | `harness/tools/loop_guard.py:111` |
| `loop_nudge.detected` | 1 | `logger.write` | `agent_harness.py:2085` |
| `memory.bootstrap` | 1 | `logger.write` | `agent_harness.py:2597` |
| `memory.bootstrap.foreign_context` | 1 | `logger.write` | `agent_harness.py:2575` |
| `memory.bootstrap.get_failed` | 1 | `logger.write` | `agent_harness.py:2564` |
| `memory.bootstrap.skipped` | 1 | `logger.write` | `agent_harness.py:2554` |
| `memory.bootstrap.unsupported_contract` | 1 | `logger.write` | `agent_harness.py:2549` |
| `memory.heartbeat` | 1 | `_write_agent_event` | `agent_harness.py:2681` |
| `memory.heartbeat.failed` | 1 | `_write_agent_event` | `agent_harness.py:2691` |
| `memory.heartbeat.stop_failed` | 1 | `_write_agent_event` | `agent_harness.py:2708` |
| `memory.terminal_checkpoint.failed` | 3 | `logger.write` | `harness/spawner/spawner_worker.py:393`, `harness/spawner/spawner_worker.py:407`, `harness/spawner/spawner_worker.py:417` |
| `microloop.telemetry` | 1 | `logger.write` | `harness/tools/browser_tools/auto_intercept.py:113` |
| `observation.watch_closed` | 1 | `_log` | `harness/tools/browser_tools/observation_view.py:237` |
| `observation.watch_events_delivered` | 1 | `_log` | `harness/tools/browser_tools/observation_view.py:250` |
| `observation.watch_opened` | 1 | `_log` | `harness/tools/browser_tools/observation_view.py:204` |
| `observation.watch_wait` | 1 | `_log` | `harness/tools/browser_tools/observation_view.py:218` |
| `pacing.phase.wait_completed` | 1 | `logger.write` | `harness/spawner/spawner_core.py:1451` |
| `pacing.phase.wait_started` | 1 | `logger.write` | `harness/spawner/spawner_core.py:1449` |
| `pacing.row.wait_completed` | 1 | `logger.write` | `harness/planning/pacing.py:105` |
| `pacing.row.wait_started` | 1 | `logger.write` | `harness/planning/pacing.py:102` |
| `page.dialog.ledger` | 1 | `log_event=` | `harness/observation/event_observer.py:162` |
| `page.lifecycle.after_action` | 1 | `logger.write` | `harness/tools/browser_tools/dispatch.py:277` |
| `page.lifecycle.event` | 1 | `_log` | `harness/observation/event_observer.py:119` |
| `page.lifecycle.settlement_wait` | 1 | `logger.write` | `harness/tools/browser_tools/dispatch.py:164` |
| `page.lifecycle.timeout_resync` | 1 | `logger.write` | `harness/tools/browser_tools/dispatch.py:184` |
| `page_inventory.changed` | 1 | `_log` | `harness/observation/event_observer.py:213` |
| `page_session.record_failed` | 1 | `logger.write` | `harness/observation/page_session.py:682` |
| `page_session.recorded` | 1 | `logger.write` | `harness/observation/page_session.py:675` |
| `page_session.render_failed` | 1 | `logger.write` | `harness/observation/page_session.py:754` |
| `page_stats.detected` | 1 | `logger.write` | `agent_harness.py:2062` |
| `plan_validator.error_deduplicated` | 1 | `logger.write` | `agent_harness.py:4596` |
| `plan_validator.mechanical_invalid` | 2 | `logger.write` | `agent_harness.py:4383`, `agent_harness.py:4443` |
| `plan_validator.operational_continuation` | 1 | `logger.write` | `agent_harness.py:4537` |
| `plan_validator.review_deduplicated` | 1 | `logger.write` | `agent_harness.py:4569` |
| `plan_validator.review_retry` | 1 | `logger.write` | `agent_harness.py:4666` |
| `progress.history_navigation_unverified` | 1 | `logger.write` | `harness/tools/browser_tools/navigate.py:1215` |
| `progress.mandatory_recovery_credit_used` | 1 | `logger.write` | `harness/tools/browser_tools/progress_obs.py:361` |
| `progress.observed` | 1 | `logger.write` | `harness/tools/browser_tools/progress_obs.py:402` |
| `progress.repair_advanced` | 1 | `logger.write` | `harness/tools/browser_tools/progress_obs.py:435` |
| `progress.snapshot` | 1 | `logger.write` | `harness/tools/browser_tools/progress_obs.py:443` |
| `progress.unrecorded_rows_observed` | 1 | `logger.write` | `harness/tools/browser_tools/progress_obs.py:140` |
| `prompt.guides.degraded` | 1 | `logger.write` | `agent_harness.py:221` |
| `repair.visual_evidence_abandoned` | 1 | `logger.write` | `harness/tools/browser_tools/record_extraction.py:223` |
| `repair.visual_evidence_satisfied` | 1 | `logger.write` | `harness/tools/browser_tools/visual.py:363` |
| `repair.visual_page_rejected` | 1 | `logger.write` | `harness/tools/browser_tools/visual.py:449` |
| `resume.hitl_phases_reactivated` | 1 | `logger.write` | `harness/task_control/phase_lifecycle.py:339` |
| `resume.instruction.audit_failed` | 1 | `logger.write` | `harness/tools/lead_tools.py:3320` |
| `resume.instruction.reviewed` | 2 | `logger.write` | `agent_harness.py:5841`, `harness/tools/lead_tools.py:3332` |
| `resume.projection_built` | 1 | `logger.write` | `main.py:2972` |
| `resume.started` | 1 | `logger.write` | `main.py:2954` |
| `row_ledger.error` | 1 | `logger.write` | `harness/spawner/spawner_worker.py:269` |
| `row_ledger.recorded` | 1 | `logger.write` | `harness/spawner/spawner_worker.py:274` |
| `runtime.evaluate.prepared` | 1 | `logger.write` | `harness/tools/browser_tools/capability.py:276` |
| `runtime.evaluate.rejected` | 2 | `logger.write` | `harness/tools/browser_tools/capability.py:268`, `harness/tools/browser_tools/capability.py:1297` |
| `runtime.evaluate.trusted_collection_template` | 1 | `logger.write` | `harness/tools/browser_tools/runtime_eval.py:85` |
| `runtime.evaluate.world_evidence_degraded` | 1 | `logger.write` | `harness/tools/browser_tools/capability.py:703` |
| `schema.bootstrap.cached` | 3 | `logger.write` | `agent_harness.py:6192`, `agent_harness.py:6212`, `agent_harness.py:6238` |
| `schema.bootstrap.done` | 1 | `logger.write` | `agent_harness.py:6295` |
| `schema.bootstrap.failed` | 3 | `logger.write` | `agent_harness.py:6145`, `agent_harness.py:6274`, `agent_harness.py:6322` |
| `schema.bootstrap.lock_timeout` | 1 | `logger.write` | `agent_harness.py:6224` |
| `schema.bootstrap.timing` | 1 | `logger.write` | `agent_harness.py:6342` |
| `schema.bundle.loaded` | 1 | `logger.write` | `harness/capabilities/schema_loader.py:171` |
| `schema.bundle.reused` | 1 | `logger.write` | `harness/spawner/spawner_worker.py:1401` |
| `schema.bundle.stale_revision` | 1 | `logger.write` | `harness/spawner/spawner_worker.py:1390` |
| `schema.contract.fallback` | 1 | `logger.write` | `agent_harness.py:6360` |
| `schema.describeAction.error` | 1 | `logger.write` | `harness/capabilities/schema_loader.py:136` |
| `schema.describeAction.stale_catalog` | 1 | `logger.write` | `harness/capabilities/schema_loader.py:146` |
| `semantic_terminal.counterevidence` | 1 | `logger.write` | `harness/spawner/spawner_worker.py:884` |
| `skill.autoheal.attempt` | 1 | `_log` | `harness/skill/autoheal.py:120` |
| `skill.autoheal.distill_error` | 1 | `_log` | `harness/skill/autoheal.py:114` |
| `skill.autoheal.error` | 2 | `_log`, `logger.write` | `harness/skill/autoheal.py:134`, `harness/spawner/spawner_worker.py:340` |
| `skill.autoheal.no_candidate` | 1 | `_log` | `harness/skill/autoheal.py:117` |
| `skill.autoheal.result` | 1 | `_log` | `harness/skill/autoheal.py:137` |
| `skill.autoheal.skipped` | 2 | `_log` | `harness/skill/autoheal.py:106`, `harness/skill/autoheal.py:109` |
| `skill.context.error` | 1 | `logger.write` | `harness/spawner/spawner_worker.py:538` |
| `skill.contract.enrich_error` | 1 | `logger.write` | `harness/skill/contract.py:445` |
| `skill.contract.enriched` | 1 | `logger.write` | `harness/skill/contract.py:438` |
| `skill.control.auth_barrier_claimed` | 1 | `_log` | `harness/skill/control.py:146` |
| `skill.control.challenge_onset` | 1 | `_log` | `harness/skill/control.py:430` |
| `skill.control.challenge_resolved` | 1 | `_log` | `harness/skill/control.py:436` |
| `skill.control.degraded` | 1 | `_log` | `harness/skill/dispatch.py:1831` |
| `skill.control.open_failed` | 1 | `_log` | `harness/skill/control.py:116` |
| `skill.control.request_pause_error` | 1 | `_log` | `harness/skill/control.py:463` |
| `skill.control.resolve_error` | 1 | `_log` | `harness/skill/control.py:435` |
| `skill.control.vl_solve_error` | 1 | `_log` | `harness/skill/control.py:626` |
| `skill.control.vl_solve_gated` | 1 | `_log` | `harness/skill/control.py:605` |
| `skill.control.wait_resume_error` | 1 | `_log` | `harness/skill/control.py:475` |
| `skill.fast_path.auto_rows` | 1 | `_log` | `harness/skill/dispatch.py:1946` |
| `skill.fast_path.auto_rows_ambiguous` | 1 | `_log` | `harness/skill/dispatch.py:868` |
| `skill.fast_path.batch_completed` | 1 | `_log` | `harness/skill/dispatch.py:1693` |
| `skill.fast_path.batch_handoff` | 1 | `_log` | `harness/skill/dispatch.py:1544` |
| `skill.fast_path.completed` | 1 | `_log` | `harness/skill/dispatch.py:2126` |
| `skill.fast_path.disabled` | 1 | `_log` | `harness/skill/dispatch.py:1920` |
| `skill.fast_path.error` | 1 | `logger.write` | `harness/spawner/spawner_worker.py:204` |
| `skill.fast_path.fell_back` | 1 | `_log` | `harness/skill/dispatch.py:2021` |
| `skill.fast_path.hints_only` | 1 | `_log` | `harness/skill/dispatch.py:1903` |
| `skill.fast_path.hitl_required` | 1 | `_log` | `harness/skill/dispatch.py:2011` |
| `skill.fast_path.partial_persist_error` | 1 | `_log` | `harness/skill/dispatch.py:1310` |
| `skill.fast_path.persist_error` | 2 | `_log` | `harness/skill/dispatch.py:1647`, `harness/skill/dispatch.py:2079` |
| `skill.fast_path.persisted_contract_unmet` | 2 | `_log` | `harness/skill/dispatch.py:1659`, `harness/skill/dispatch.py:2092` |
| `skill.fast_path.repair_fallback` | 1 | `logger.write` | `harness/tools/browser_tools/record_extraction.py:221` |
| `skill.fast_path.repair_handoff` | 2 | `_log` | `harness/skill/dispatch.py:1671`, `harness/skill/dispatch.py:2104` |
| `skill.fast_path.row_completed` | 1 | `_log` | `harness/skill/dispatch.py:1623` |
| `skill.fast_path.rows_enriched` | 1 | `_log` | `harness/skill/dispatch.py:1058` |
| `skill.fast_path.rows_enrichment_rejected` | 3 | `_log` | `harness/skill/dispatch.py:985`, `harness/skill/dispatch.py:1003`, `harness/skill/dispatch.py:1014` |
| `skill.fast_path.rows_enrichment_skipped` | 3 | `_log` | `harness/skill/dispatch.py:941`, `harness/skill/dispatch.py:947`, `harness/skill/dispatch.py:957` |
| `skill.fast_path.skipped` | 3 | `_log` | `harness/skill/dispatch.py:1952`, `harness/skill/dispatch.py:1967`, `harness/skill/dispatch.py:1984` |
| `skill.fast_path.visual_contract_violated` | 1 | `_log` | `harness/skill/dispatch.py:2049` |
| `skill.fast_path.workflow_auth_fenced` | 1 | `_log` | `harness/skill/dispatch.py:1998` |
| `skill.fast_path.{}` ⚠动态 | 1 | `_log` | `harness/skill/dispatch.py:2038` |
| `skill.forced` | 1 | `logger.write` | `harness/skill/contract.py:348` |
| `skill.forced.ranked` | 1 | `logger.write` | `harness/skill/contract.py:331` |
| `skill.forced.unknown` | 1 | `logger.write` | `harness/skill/contract.py:297` |
| `skill.forced.{}` ⚠动态 | 1 | `logger.write` | `harness/skill/contract.py:318` |
| `skill.guidance.recorded` | 1 | `logger.write` | `harness/skill/guidance.py:545` |
| `skill.guidance.signal_error` | 1 | `logger.write` | `harness/spawner/spawner_worker.py:369` |
| `skill.guidance.stage_mismatch` | 1 | `logger.write` | `harness/skill/guidance.py:526` |
| `skill.registry.load_failed` | 1 | `logger.write` | `harness/spawner/spawner_worker.py:160` |
| `skill.selected_workflow.executed` | 1 | `logger.write` | `harness/tools/browser_tools/dispatch.py:1311` |
| `skill.selection.error` | 1 | `logger.write` | `harness/skill/contract.py:581` |
| `skill.selection.required` | 1 | `logger.write` | `harness/skill/contract.py:568` |
| `skill.visual_contract.error` | 1 | `_log` | `harness/skill/dispatch.py:2148` |
| `skill.visual_contract.result` | 1 | `_log` | `harness/skill/visual_contract.py:261` |
| `snapshot_diff.detected` | 1 | `logger.write` | `agent_harness.py:2070` |
| `spawn.sibling_route_attached` | 1 | `logger.write` | `harness/tools/lead_tools.py:3737` |
| `spawner.browser.failure_cleanup.cancel_suppressed` | 1 | `logger.write` | `harness/spawner/spawner_worker.py:444` |
| `spawner.browser.failure_cleanup.failed` | 1 | `logger.write` | `harness/spawner/spawner_worker.py:450` |
| `spawner.browser.result` | 2 | `logger.write` | `harness/spawner/spawner_worker.py:1094`, `harness/spawner/spawner_worker.py:1339` |
| `spawner.browser.spawn` | 1 | `logger.write` | `harness/spawner/spawner_core.py:2098` |
| `spawner.browser.start_rejected` | 4 | `logger.write` | `harness/spawner/spawner_core.py:1420`, `harness/spawner/spawner_core.py:1426`, `harness/spawner/spawner_core.py:1461`, `harness/spawner/spawner_core.py:1467` |
| `spawner.browser_context.persist_failed` | 2 | `logger.write` | `harness/spawner/spawner_core.py:1123`, `harness/spawner/spawner_registry.py:820` |
| `spawner.browser_context.persist_skipped` | 2 | `logger.write` | `harness/spawner/spawner_core.py:820`, `harness/spawner/spawner_core.py:831` |
| `spawner.browser_context.persisted` | 1 | `logger.write` | `harness/spawner/spawner_core.py:997` |
| `spawner.connection_recovery` | 1 | `logger.write` | `harness/spawner/spawner_core.py:2263` |
| `spawner.fleet.assigned` | 1 | `logger.write` | `harness/spawner/spawner_slots.py:2117` |
| `spawner.fleet.assignment_rejected` | 1 | `logger.write` | `harness/spawner/spawner_core.py:1968` |
| `spawner.fleet.cap_blocked` | 1 | `logger.write` | `harness/spawner/spawner_slots.py:294` |
| `spawner.fleet.cap_released` | 1 | `logger.write` | `harness/spawner/spawner_slots.py:284` |
| `spawner.fleet.cap_reuse` | 1 | `logger.write` | `harness/spawner/spawner_slots.py:330` |
| `spawner.fleet.inventory_retired` | 1 | `logger.write` | `harness/spawner/spawner_slots.py:277` |
| `spawner.fleet.notification_relay_attached` | 1 | `logger.write` | `harness/spawner/spawner_slots.py:2515` |
| `spawner.fleet.readiness_failed` | 2 | `logger.write` | `harness/spawner/spawner_slots.py:2292`, `harness/spawner/spawner_slots.py:2331` |
| `spawner.fleet.readiness_ready` | 1 | `logger.write` | `harness/spawner/spawner_slots.py:2255` |
| `spawner.fleet.readiness_started` | 1 | `logger.write` | `harness/spawner/spawner_slots.py:2180` |
| `spawner.fleet.worker_isolation_applied` | 1 | `logger.write` | `harness/spawner/spawner_slots.py:145` |
| `spawner.fleet.worker_isolation_skipped` | 1 | `logger.write` | `harness/spawner/spawner_slots.py:134` |
| `spawner.resume_browser_hint.ignored` | 3 | `logger.write` | `harness/spawner/spawner_core.py:785`, `harness/spawner/spawner_slots.py:1873`, `harness/spawner/spawner_slots.py:1900` |
| `spawner.resume_browser_hint.page_probe_failed` | 1 | `logger.write` | `harness/spawner/spawner_core.py:1682` |
| `spawner.resume_browser_hint.used` | 1 | `logger.write` | `harness/spawner/spawner_slots.py:1882` |
| `spawner.similar_task_reuse.matched` | 1 | `logger.write` | `harness/spawner/spawner_slots.py:2016` |
| `spawner.similar_task_reuse.miss` | 1 | `logger.write` | `harness/spawner/spawner_slots.py:1940` |
| `spawner.similar_task_reuse.readiness_fallback` | 1 | `logger.write` | `harness/spawner/spawner_slots.py:1993` |
| `spawner.similar_task_reuse.rejected` | 1 | `logger.write` | `harness/spawner/spawner_slots.py:2004` |
| `spawner.slot.acquire_exhausted` | 1 | `logger.write` | `harness/spawner/spawner_core.py:1503` |
| `spawner.slot.acquire_failed` | 1 | `logger.write` | `harness/spawner/spawner_core.py:2034` |
| `spawner.slot.bootstrap_timing` | 1 | `logger.write` | `harness/spawner/spawner_slots.py:1223` |
| `spawner.slot.created` | 1 | `logger.write` | `harness/spawner/spawner_slots.py:1240` |
| `spawner.slot.event_replay` | 2 | `logger.write` | `harness/spawner/spawner_slots.py:1094`, `harness/spawner/spawner_slots.py:1137` |
| `spawner.slot.page_quarantine_cleared` | 1 | `logger.write` | `harness/spawner/spawner_registry.py:635` |
| `spawner.slot.page_quarantine_retired` | 1 | `logger.write` | `harness/spawner/spawner_registry.py:594` |
| `spawner.slot.page_quarantined` | 1 | `logger.write` | `harness/spawner/spawner_registry.py:508` |
| `spawner.slot.protocol_identity_changed` | 1 | `logger.write` | `harness/spawner/spawner_slots.py:1026` |
| `spawner.slot.recovered` | 1 | `logger.write` | `harness/spawner/spawner_slots.py:1376` |
| `spawner.slot.recovery_deferred` | 1 | `logger.write` | `harness/spawner/spawner_slots.py:1278` |
| `spawner.slot.recovery_failed` | 1 | `logger.write` | `harness/spawner/spawner_slots.py:1353` |
| `spawner.slot.reserved` | 1 | `logger.write` | `harness/spawner/spawner_slots.py:992` |
| `spawner.slot.retired` | 1 | `logger.write` | `harness/spawner/spawner_slots.py:1407` |
| `spawner.slot.start_cancelled` | 1 | `logger.write` | `harness/spawner/spawner_core.py:1917` |
| `spawner.slot.sync_warning` | 1 | `logger.write` | `harness/spawner/spawner_registry.py:271` |
| `spawner.task_session_binding.candidate_recorded` | 1 | `logger.write` | `harness/spawner/spawner_core.py:613` |
| `spawner.task_session_binding.expired` | 1 | `logger.write` | `harness/spawner/spawner_core.py:284` |
| `spawner.task_session_binding.handler_failed` | 1 | `logger.write` | `harness/tools/browser_tools/hitl.py:1686` |
| `spawner.task_session_binding.load_ambiguous` | 1 | `logger.write` | `harness/spawner/spawner_core.py:360` |
| `spawner.task_session_binding.load_failed` | 1 | `logger.write` | `harness/spawner/spawner_core.py:305` |
| `spawner.task_session_binding.operator_reset` | 1 | `logger.write` | `harness/spawner/spawner_core.py:566` |
| `spawner.task_session_binding.write_failed` | 1 | `logger.write` | `harness/spawner/spawner_core.py:629` |
| `storage.download_registration_failed` | 1 | `logger.write` | `harness/tools/browser_tools/downloads.py:280` |
| `storage.dual_verify` | 1 | `logger.write` | `main.py:1855` |
| `storage.external_file_unregistered` | 1 | `logger.write` | `agent_harness.py:3442` |
| `storage.revision_conflict` | 1 | `logger.write` | `main.py:1852` |
| `strategy_attempts.appended` | 1 | `logger.write` | `harness/planning/strategy_telemetry.py:83` |
| `strategy_attempts.write_failed` | 1 | `logger.write` | `harness/planning/strategy_telemetry.py:78` |
| `task.fleet_reference.bound` | 1 | `logger.write` | `main.py:3056` |
| `task.fleet_reference.injected` | 1 | `logger.write` | `harness/tools/lead_tools.py:3846` |
| `task.fleet_reference.rejected` | 2 | `logger.write` | `main.py:3035`, `main.py:3048` |
| `task.fleet_reference.routing_normalized` | 1 | `logger.write` | `harness/tools/lead_tools.py:3612` |
| `task_phase.artifacts_revalidated` | 1 | `logger.write` | `harness/task_control/revalidate.py:91` |
| `task_phase.blocked_by_dependency` | 2 | `logger.write` | `harness/task_control/phase_lifecycle.py:1427`, `harness/task_control/phase_lifecycle.py:1553` |
| `task_phase.exhausted` | 1 | `logger.write` | `harness/task_control/phase_lifecycle.py:1523` |
| `task_plan.accepted` | 2 | `logger.write` | `harness/task_control/plan_validation.py:2884`, `harness/task_control/plan_validation.py:2966` |
| `task_plan.accepted_with_warnings` | 1 | `logger.write` | `agent_harness.py:5722` |
| `task_plan.approval_classified` | 1 | `logger.write` | `harness/planning/approval_intent.py:68` |
| `task_plan.approval_requested` | 1 | `logger.write` | `agent_harness.py:4796` |
| `task_plan.approval_{}` ⚠动态 | 1 | `logger.write` | `agent_harness.py:4828` |
| `task_plan.auto_repaired` | 1 | `logger.write` | `harness/tools/lead_tools.py:2925` |
| `task_plan.draft_appended` | 1 | `logger.write` | `agent_harness.py:5060` |
| `task_plan.draft_started` | 1 | `logger.write` | `agent_harness.py:4992` |
| `task_plan.rejected` | 23 | `logger.write` | `agent_harness.py:5268`, `agent_harness.py:5286`, `agent_harness.py:5350`, `agent_harness.py:5385` 等 23 处 |
| `task_plan.review_unavailable` | 1 | `logger.write` | `agent_harness.py:5515` |
| `task_plan.user_approved` | 1 | `logger.write` | `agent_harness.py:4856` |
| `task_plan.validate.degraded` | 1 | `logger.write` | `agent_harness.py:5304` |
| `task_plan.validate.warning` | 1 | `logger.write` | `agent_harness.py:5295` |
| `task_plan.versioned` | 1 | `logger.write` | `harness/task_control/plan_validation.py:2969` |
| `task_state.initialized` | 1 | `logger.write` | `harness/task_control/plan_validation.py:3138` |
| `task_state.resume_prepared` | 1 | `logger.write` | `harness/task_control/phase_lifecycle.py:1167` |
| `tool.direct_capability_wrapped` | 1 | `logger.write` | `harness/tools/browser_tools/capability.py:142` |
| `tool.error` | 2 | `logger.write` | `harness/tools/browser_tools/capability.py:157`, `harness/tools/browser_tools/dispatch.py:780` |
| `tool.final_answer` | 1 | `logger.write` | `harness/tools/browser_tools/dispatch.py:1504` |
| `tool_batch.deferred` | 1 | `logger.write` | `agent_harness.py:2217` |
| `tool_result.model_visible` | 1 | `logger.write` | `agent_harness.py:1004` |
| `tool_result.offloaded` | 1 | `logger.write` | `harness/context/offload.py:560` |
| `tool_result.preserve_failed` | 1 | `logger.write` | `harness/context/offload.py:431` |
| `transport.recovery.probe_recorded` | 1 | `logger.write` | `harness/task_control/transport_recovery.py:191` |
| `transport.recovery.required` | 1 | `logger.write` | `harness/task_control/transport_recovery.py:161` |
| `vl.captcha_autosolve.failed` | 1 | `logger.write` | `harness/tools/browser_tools/hitl.py:765` |
| `vl.captcha_autosolve.result` | 1 | `logger.write` | `harness/tools/browser_tools/captcha_autosolve.py:904` |
| `vl.captcha_autosolve.screenshot_attempt_failed` | 1 | `logger.write` | `harness/tools/browser_tools/captcha_autosolve.py:242` |
| `vl.captcha_autosolve.screenshot_retry_recovered` | 1 | `logger.write` | `harness/tools/browser_tools/captcha_autosolve.py:235` |
| `vl.captcha_autosolve.viewport_exhausted` | 1 | `logger.write` | `harness/tools/browser_tools/captcha_autosolve.py:439` |
| `vl.captcha_autosolve.viewport_fallback` | 1 | `logger.write` | `harness/tools/browser_tools/captcha_autosolve.py:429` |
| `vl.captcha_screenshot.retain_failed` | 1 | `logger.write` | `harness/tools/browser_tools/captcha_autosolve.py:285` |
| `vl.captcha_screenshot.retained` | 1 | `logger.write` | `harness/tools/browser_tools/captcha_autosolve.py:292` |
| `vl.captcha_solve` | 1 | `logger.write` | `harness/tools/browser_tools/captcha_autosolve.py:786` |
| `vl.locate.promotion` | 1 | `logger.write` | `harness/tools/browser_tools/visual.py:1770` |
| `vl.locate.promotion_error` | 1 | `logger.write` | `harness/tools/browser_tools/visual.py:1905` |
| `vl.locate.promotion_guard` | 1 | `_log` | `harness/vl/locate.py:836` |
| `vl.locate.result` | 1 | `_log` | `harness/vl/locate.py:681` |
| `vl.overlay_adjudication` | 1 | `logger.write` | `harness/tools/browser_tools/auto_intercept.py:190` |
| `vl.reality_check` | 1 | `logger.write` | `harness/tools/browser_tools/visual.py:1491` |
| `vl.reality_check.capture_unavailable` | 1 | `logger.write` | `harness/tools/browser_tools/visual.py:1369` |
| `vl.reality_check.delivered` | 1 | `_write_agent_event` | `agent_harness.py:3514` |
| `vl.reality_check.error` | 2 | `logger.write` | `harness/tools/browser_tools/visual.py:1299`, `harness/tools/browser_tools/visual.py:1505` |
| `vl.reality_check.persist_failed` | 1 | `logger.write` | `harness/tools/browser_tools/visual.py:1463` |
| `vl.visual_recovery_hint` | 1 | `logger.write` | `harness/tools/browser_tools/visual.py:831` |
| `vl.visual_verify` | 1 | `logger.write` | `harness/tools/browser_tools/visual.py:643` |
| `worker.failure_trace_save_failed` | 1 | `logger.write` | `harness/spawner/spawner_worker.py:1182` |
| `workflow.auth_fence.before` | 2 | `_log`, `logger.write` | `harness/tools/browser_tools/bindings.py:626`, `harness/workflow/workflow_auth_fence.py:74` |
| `workflow.auth_generation_changed` | 3 | `_log`, `logger.write` | `harness/tools/browser_tools/bindings.py:631`, `harness/tools/browser_tools/bindings.py:750`, `harness/workflow/workflow_auth_fence.py:106` |
| `workflow.definition.prepared` | 1 | `logger.write` | `harness/tools/browser_tools/dispatch.py:1085` |
| `workflow.definition.used` | 2 | `logger.write` | `harness/tools/browser_tools/dispatch.py:1165`, `harness/tools/browser_tools/dispatch.py:1291` |
| `workflow.execute.rejected` | 1 | `logger.write` | `harness/tools/browser_tools/capability.py:315` |
| `workflow.execute.runtime_disabled` | 1 | `logger.write` | `harness/tools/browser_tools/capability.py:296` |
| `workflow.hitl_barrier.claimed_by_event_observer` | 1 | `_log` | `harness/fleet/runtime.py:1189` |
| `workflow.hitl_barrier.event_bridge_error` | 1 | `_log` | `harness/observation/event_observer.py:246` |
| `workflow.hitl_barrier.event_ignored` | 1 | `_log` | `harness/observation/event_observer.py:255` |
| `workflow.hitl_barrier.settled` | 1 | `_log` | `harness/fleet/runtime.py:1766` |
| `workflow.row_quarantined` | 2 | `_log`, `logger.write` | `harness/tools/browser_tools/bindings.py:751`, `harness/workflow/workflow_auth_fence.py:107` |
| `workflow.row_replayed_after_reperception` | 1 | `_log` | `harness/skill/dispatch.py:1165` |
| `{}.model_input_moderation_folded` ⚠动态 | 1 | `logger.write` | `agent_harness.py:766` |
| `{}.transient_retry` ⚠动态 | 1 | `_log` | `harness/skill/dispatch.py:1218` |
| `{}.vl_budget_exceeded` ⚠动态 | 1 | `_log` | `harness/vl/captcha.py:515` |
| `{}.vl_exhausted` ⚠动态 | 1 | `_log` | `harness/vl/captcha.py:511` |
| `{}.vl_no_screenshot` ⚠动态 | 1 | `_log` | `harness/vl/captcha.py:314` |
| `{}.vl_reclassify` ⚠动态 | 1 | `_log` | `harness/vl/captcha.py:426` |
| `{}.vl_solve_error` ⚠动态 | 1 | `_log` | `harness/vl/captcha.py:521` |
| `{}.vl_solved` ⚠动态 | 1 | `_log` | `harness/vl/captcha.py:505` |
| `{}.vl_unsafe_point` ⚠动态 | 1 | `_log` | `harness/vl/captcha.py:490` |
| `{}.vl_verdict` ⚠动态 | 1 | `_log` | `harness/vl/captcha.py:369` |
| `{}.vl_verification_unavailable` ⚠动态 | 1 | `_log` | `harness/vl/captcha.py:276` |
| `{}.vl_viewport_unavailable` ⚠动态 | 1 | `_log` | `harness/vl/captcha.py:301` |
| `{}.{}` ⚠动态 | 1 | `logger.write` | `harness/utils.py:960` |

## 5. trace type 全表（出站）

| trace type | 产生点 | 位置 |
|---|---:|---|
| `browser_call` | 2 | `harness/tools/browser_tools/capability.py:1051`, `harness/tools/browser_tools/capability.py:1566` |
| `browser_call_params_error` | 2 | `harness/tools/browser_tools/capability.py:206`, `harness/tools/browser_tools/capability.py:420` |
| `browser_call_rejected` | 1 | `harness/tools/browser_tools/capability.py:222` |
| `browser_call_schema_rejected` | 1 | `harness/tools/browser_tools/capability.py:481` |
| `captcha_auto_solved` | 1 | `harness/tools/browser_tools/capability.py:509` |
| `contract_violation` | 2 | `harness/tools/browser_tools/capability.py:330`, `harness/tools/browser_tools/dispatch.py:803` |
| `cross_task_memory_guard` | 1 | `harness/tools/browser_tools/capability.py:338` |
| `dialog_guard` | 1 | `harness/tools/browser_tools/capability.py:405` |
| `execute_selected_skill` | 1 | `harness/tools/browser_tools/dispatch.py:1314` |
| `final_answer` | 1 | `harness/tools/browser_tools/dispatch.py:1505` |
| `fleet_auth_gate` | 3 | `harness/tools/browser_tools/capability.py:380`, `harness/tools/browser_tools/capability.py:493`, `harness/tools/browser_tools/capability.py:521` |
| `fleet_binding_guard` | 1 | `harness/tools/browser_tools/capability.py:351` |
| `lead_tool_after_call_exception` | 1 | `harness/tools/lead_tools.py:2804` |
| `lead_tool_arguments_rejected` | 1 | `harness/tools/lead_tools.py:2772` |
| `lead_tool_exception` | 1 | `harness/tools/lead_tools.py:2785` |
| `loop_guard` | 1 | `harness/tools/browser_tools/dispatch.py:761` |
| `loop_nudge` | 1 | `agent_harness.py:2086` |
| `loop_observation` | 1 | `harness/tools/loop_guard.py:142` |
| `model` | 2 | `agent_harness.py:1808`, `harness/events/sinks.py:184` |
| `navigation_context_rejected` | 1 | `harness/tools/browser_tools/capability.py:246` |
| `page_binding_guard` | 2 | `harness/tools/browser_tools/capability.py:364`, `harness/tools/browser_tools/dispatch.py:792` |
| `page_lifecycle_gate` | 1 | `harness/tools/browser_tools/capability.py:394` |
| `page_stats` | 1 | `agent_harness.py:2063` |
| `progress_observation` | 2 | `harness/tools/browser_tools/progress_obs.py:143`, `harness/tools/browser_tools/progress_obs.py:405` |
| `record` | 1 | `harness/tools/browser_tools/dispatch.py:1090` |
| `root` | 1 | `harness/diagnostics/judge_trace.py:56` |
| `runtime_evaluation_rejected` | 1 | `harness/tools/browser_tools/capability.py:269` |
| `skill_fast_path` | 1 | `harness/skill/dispatch.py:2166` |
| `snapshot_diff` | 1 | `agent_harness.py:2071` |
| `stale_axtree_target` | 2 | `harness/tools/browser_tools/capability.py:436`, `harness/tools/browser_tools/capability.py:1321` |
| `step` | 2 | `harness/observation/exec_observer.py:257`, `harness/observation/exec_observer.py:261` |
| `tool_after_call_exception` | 1 | `harness/tools/browser_tools/dispatch.py:628` |
| `tool_arguments_rejected` | 1 | `harness/tools/browser_tools/dispatch.py:608` |
| `tool_error` | 2 | `harness/tools/browser_tools/capability.py:158`, `harness/tools/browser_tools/dispatch.py:781` |
| `tool_exception` | 1 | `harness/tools/browser_tools/dispatch.py:441` |
| `trace_entry` | 1 | `harness/tools/browser_tools/dispatch.py:819` |
| `workflow_policy_rejected` | 1 | `harness/tools/browser_tools/capability.py:316` |
| `workflow_runtime_disabled` | 1 | `harness/tools/browser_tools/capability.py:297` |

## 6. 消费端

机械匹配，可能含误报；用于证明某条产生链是否真的有读者。

### `read_events(` — 读取持久化 run event（4 处）

| 位置 | 代码 |
|---|---|
| `harness/storage/dual_store.py:490` | `return self.primary.read_events(` |
| `harness/storage/dual_store.py:1006` | `rows = store.read_events(task_id=task_id, after_event_id=cursor, limit=1000)` |
| `harness/storage/dual_store.py:1035` | `rows = store.read_events(task_id=task_id, after_event_id=cursor, limit=page)` |
| `harness/storage/sqlite_store.py:376` | `return dao.read_events(` |

### `.trace` — 读取 agent.trace 列表（72 处）

| 位置 | 代码 |
|---|---|
| `agent_harness.py:1374` | `self.trace: List[JsonDict] = []` |
| `agent_harness.py:1389` | `runtime, logger, self.trace, actor_type="browser",` |
| `agent_harness.py:1808` | `self.trace.append({` |
| `agent_harness.py:2063` | `self.trace.append({` |
| `agent_harness.py:2071` | `self.trace.append({` |
| `agent_harness.py:2086` | `self.trace.append({` |
| `agent_harness.py:3705` | `for item in self.trace` |
| `agent_harness.py:3871` | `for item in (self.trace or []):` |
| `harness/events/sinks.py:133` | `"""Rebuild the legacy ``agent.trace`` entries a consumer still expects.` |
| `harness/observation/exec_observer.py:128` | `self.trace = ExecTrace()` |
| `harness/observation/exec_observer.py:145` | `self.trace.duration_ms = int((time.monotonic() - started) * 1000)` |
| `harness/observation/exec_observer.py:153` | `return self.trace` |
| `harness/observation/exec_observer.py:176` | `if self.trace.cursor_first is None:` |
| `harness/observation/exec_observer.py:177` | `self.trace.cursor_first = cursor` |
| `harness/observation/exec_observer.py:178` | `self.trace.cursor_last = cursor` |
| `harness/observation/exec_observer.py:190` | `self.trace.page_events[event] = self.trace.page_events.get(event, 0) + 1` |
| `harness/observation/exec_observer.py:196` | `if workflow_id and not self.trace.workflow_id:` |
| `harness/observation/exec_observer.py:197` | `self.trace.workflow_id = str(workflow_id)` |
| `harness/observation/exec_observer.py:201` | `and self.trace.workflow_id` |
| `harness/observation/exec_observer.py:202` | `and str(workflow_id) != self.trace.workflow_id` |
| `harness/observation/exec_observer.py:211` | `self.trace.variables = dict(variables)` |
| `harness/observation/exec_observer.py:214` | `self.trace.store_revision = revision` |
| `harness/observation/exec_observer.py:217` | `self.trace.phase = phase` |
| `harness/observation/exec_observer.py:219` | `self.trace.failure = {` |
| `harness/observation/exec_observer.py:227` | `self.trace.phase = "running"` |
| `harness/observation/exec_observer.py:252` | `self.trace.total_steps += 1` |
| `harness/observation/exec_observer.py:254` | `self.trace.total_succeeded += 1` |
| `harness/observation/exec_observer.py:256` | `if len(self.trace.steps) < limit:` |
| `harness/observation/exec_observer.py:257` | `self.trace.steps.append(step)` |
| `harness/observation/exec_observer.py:260` | `self.trace.steps = self.trace.steps[:head] + self.trace.steps[head + 1:]` |
| `harness/observation/exec_observer.py:261` | `self.trace.steps.append(step)` |
| `harness/observation/exec_observer.py:262` | `self.trace.dropped_steps += 1` |
| `harness/skill/workflow.py:152` | `trace = observer.trace` |
| `harness/skill/workflow.py:178` | `trace = observer.trace` |
| `harness/spawner/spawner_worker.py:813` | `trace_path = self._write_worker_trace(worker_id, harness.trace)` |
| `harness/spawner/spawner_worker.py:814` | `trace_summary = self._summarize_worker_trace(harness.trace)` |
| `harness/spawner/spawner_worker.py:868` | `harness.trace,` |
| `harness/tools/browser_tools/capability.py:158` | `agent.trace.append({"type": "tool_error", "result": result})` |
| `harness/tools/browser_tools/capability.py:206` | `agent.trace.append({"type": "browser_call_params_error", "result": result})` |
| `harness/tools/browser_tools/capability.py:222` | `agent.trace.append({"type": "browser_call_rejected", "result": result})` |
| … | 另有 32 处 |

### `worker_trace_events` — 读取 worker trace 表（6 处）

| 位置 | 代码 |
|---|---|
| `harness/storage/dao.py:623` | `"SELECT COALESCE(MAX(sequence_no), 0) FROM worker_trace_events"` |
| `harness/storage/dao.py:629` | `"INSERT INTO worker_trace_events("` |
| `harness/storage/dao.py:657` | `sql = "SELECT * FROM worker_trace_events WHERE task_id = ?"` |
| `harness/storage/virtual_fs.py:13` | `traces/<worker>.jsonl     -> worker_trace_events` |
| `harness/storage/virtual_fs.py:123` | `" FROM worker_trace_events WHERE task_id = ?"` |
| `harness/storage/virtual_fs.py:275` | `"SELECT trace_event_id, trace_json FROM worker_trace_events"` |

### `on_event` — 事件回调（Console/transport）（24 处）

| 位置 | 代码 |
|---|---|
| `abcp_client.py:320` | `on_event: Optional[EventCallback] = None,` |
| `abcp_client.py:323` | `self.on_event = on_event` |
| `abcp_client.py:935` | `if not self.on_event:` |
| `abcp_client.py:941` | `self.on_event(event_type, sanitize(payload, table))` |
| `agent_harness.py:2291` | `extension_event = (` |
| `agent_harness.py:2296` | `self._write_agent_event(extension_event, {` |
| `agent_harness.py:6121` | `async with ABCPClient(browser_config, on_event=event_logger) as browser:` |
| `harness/fleet/runtime.py:1706` | `name, event_payload = _notification_event(message)` |
| `harness/planning/fast_path.py:226` | `if (candidate := _successful_collection_event(event)) is not None` |
| `harness/spawner/spawner_core.py:2198` | `client = _sp().ABCPClient(self.runtime.browser, on_event=make_browser_event_logger(` |
| `harness/spawner/spawner_slots.py:1039` | `revision = _registration_event_catalog_revision(registration)` |
| `harness/spawner/spawner_slots.py:1170` | `client = _sp().ABCPClient(self.runtime.browser, on_event=event_logger)` |
| `harness/spawner/spawner_slots.py:1194` | `_registration_event_cursor(registration),` |
| `harness/spawner/spawner_slots.py:1312` | `client = _sp().ABCPClient(self.runtime.browser, on_event=event_logger)` |
| `harness/spawner/spawner_slots.py:1334` | `registration_cursor=_registration_event_cursor(registration),` |
| `harness/spawner/spawner_slots.py:1468` | `slot.client.on_event = slot.idle_event_logger` |
| `harness/spawner/spawner_slots.py:1503` | `_registration_event_cursor(registration),` |
| `harness/spawner/spawner_worker.py:511` | `slot.client.on_event = event_logger` |
| `harness/utils.py:671` | `on_event: Optional[EventSink] = None,` |
| `harness/utils.py:684` | `self.on_event = on_event` |
| `harness/utils.py:748` | `self.on_event(event_type, payload) if self.on_event else None` |
| `harness/utils.py:962` | `return on_event` |
| `main.py:2861` | `on_event=ConsoleProgressReporter(),` |
| `main.py:3002` | `on_event=ConsoleProgressReporter(),` |

### `iter_events(` — 遍历事件流（1 处）

| 位置 | 代码 |
|---|---|
| `harness/diagnostics/selector_audit.py:251` | `for event in _iter_events(run_jsonl_path):` |

## 7. WebCross 平台事件目录（入站）

平台自己发出的事件，harness 只消费不产生。名字与元数据取自 §1.2 的两处证据：`安装包` 列是已安装运行时包里能否找到该事件的目录定义；`引用点` 是产品源码里按这个名字消费它的位置数（字符串常量精确匹配，不按形状猜）。`Agent 可见` 指平台目录里该事件带 Agent 投影，即 Agent 订阅得到的那一部分；标 `—` 的只投给 User 界面，harness 永远收不到。

注意：`引用点` 对 §8 里事件名与 Action 名同形的三行是**混合计数**（同一字符串既可能是等待的事件，也可能是调用的 Action）；机械提取不替它做语义判断，要分辨得看位置上的代码。

| 事件 | category | severity | audience | Agent 可见 | 安装包 | 引用点 | 位置 |
|---|---|---|---|---|---|---:|---|
| `Action.failed` | `action` | `error` | `user` | — | ✅ | 0 | *无* |
| `Action.started` | `action` | `info` | `user` | — | ✅ | 0 | *无* |
| `Action.succeeded` | `action` | `info` | `user` | — | ✅ | 0 | *无* |
| `Browser.snapshot` | `browser` | `info` | `user` | — | ✅ | 0 | *无* |
| `Connection.changed` | `connection` | `info` | `user` | — | ✅ | 0 | *无* |
| `DOM.axTreeUpdated` | `page` | `info` | `both` | ✅ | ❌ | 2 | `harness/observation/event_observer.py:127`, `harness/observation/event_observer.py:319` |
| `Download.progressed` | `blocking` | `info` | `both` | ✅ | ✅ | 3 | `harness/observation/event_observer.py:123`, `harness/tools/browser_tools/downloads.py:929` 等 3 处 |
| `Download.started` | `blocking` | `info` | `both` | ✅ | ✅ | 3 | `harness/observation/event_observer.py:122`, `harness/tools/browser_tools/downloads.py:928` 等 3 处 |
| `Download.stateChanged` | `blocking` | `info` | `both` | ✅ | ✅ | 6 | `devtools/download_path_live_canary.py:86`, `devtools/download_path_live_canary.py:106` 等 6 处 |
| `Download.waiting` | `blocking` | `info` | `both` | ✅ | ✅ | 3 | `harness/observation/event_observer.py:122`, `harness/tools/browser_tools/downloads.py:927` 等 3 处 |
| `File.chooserClosed` | `blocking` | `info` | `both` | ✅ | ✅ | 2 | `harness/observation/page_lifecycle.py:208`, `harness/workflow/workflow_policy.py:345` |
| `File.chooserOpened` | `blocking` | `blocking` | `both` | ✅ | ✅ | 0 | *无* |
| `File.operationCompleted` | `blocking` | `success` | `both` | ✅ | ✅ | 0 | *无* |
| `File.operationFailed` | `blocking` | `error` | `both` | ✅ | ✅ | 0 | *无* |
| `Fleet.ready` | `fleet` | `info` | `both` | ✅ | ✅ | 1 | `harness/spawner/spawner_slots.py:2124` |
| `Fleet.restartFailed` | `fleet` | `error` | `user` | — | ✅ | 0 | *无* |
| `Fleet.restartScheduled` | `fleet` | `info` | `user` | — | ✅ | 0 | *无* |
| `Fleet.stopped` | `fleet` | `error` | `both` | ✅ | ✅ | 2 | `harness/spawner/spawner_slots.py:2125`, `harness/spawner/spawner_slots.py:2284` |
| `Hitl.paused` | `hitl` | `blocking` | `both` | ✅ | ✅ | 6 | `harness/fleet/runtime.py:1118`, `harness/fleet/runtime.py:1713` 等 6 处 |
| `Hitl.resumed` | `hitl` | `info` | `both` | ✅ | ✅ | 3 | `harness/fleet/runtime.py:1713`, `harness/runtime/hitl.py:70` 等 3 处 |
| `Page.certificateError` | `blocking` | `error` | `user` | — | ✅ | 0 | *无* |
| `Page.close` | `page` | `info` | `both` | ✅ | ✅ | 25 | `harness/fleet/runtime.py:1621`, `harness/fleet/runtime.py:1746` 等 25 处 |
| `Page.crashed` | `navigation` | `error` | `both` | ✅ | ✅ | 5 | `harness/fleet/runtime.py:1746`, `harness/observation/event_observer.py:114` 等 5 处 |
| `Page.dialogClosed` | `blocking` | `info` | `both` | ✅ | ✅ | 6 | `harness/observation/browser_reducers.py:164`, `harness/observation/browser_reducers.py:215` 等 6 处 |
| `Page.dialogOpened` | `blocking` | `blocking` | `both` | ✅ | ✅ | 3 | `harness/observation/browser_reducers.py:164`, `harness/observation/browser_reducers.py:255` 等 3 处 |
| `Page.loadFailed` | `navigation` | `error` | `both` | ✅ | ✅ | 5 | `harness/observation/page_lifecycle.py:187`, `harness/tools/browser_tools/navigate.py:844` 等 5 处 |
| `Page.loaded` | `navigation` | `info` | `both` | ✅ | ✅ | 7 | `harness/observation/page_lifecycle.py:183`, `harness/runtime/hitl.py:78` 等 7 处 |
| `Page.navigate` | `navigation` | `info` | `both` | ✅ | ✅ | 36 | `harness/fleet/runtime.py:1746`, `harness/observation/content_completeness.py:1252` 等 36 处 |
| `Page.navigationUpdated` | `page` | `info` | `user` | — | ✅ | 0 | *无* |
| `Page.open` | `page` | `info` | `both` | ✅ | ✅ | 4 | `harness/fleet/runtime.py:1740`, `harness/observation/event_observer.py:116` 等 4 处 |
| `Page.popupRequested` | `blocking` | `blocking` | `user` | — | ✅ | 0 | *无* |
| `Page.recovered` | `navigation` | `info` | `both` | ✅ | ✅ | 5 | `harness/observation/event_observer.py:114`, `harness/observation/page_lifecycle.py:201` 等 5 处 |
| `Page.startedLoading` | `navigation` | `info` | `both` | ✅ | ✅ | 1 | `harness/observation/page_lifecycle.py:175` |
| `Page.switchTo` | `page` | `info` | `both` | ✅ | ✅ | 5 | `harness/observation/content_completeness.py:1314`, `harness/observation/progress.py:33` 等 5 处 |
| `Page.titleUpdated` | `page` | `info` | `both` | ✅ | ✅ | 1 | `harness/runtime/hitl.py:74` |
| `System.agentBootstrap` | `system` | `info` | `internal` | — | ✅ | 0 | *无* |
| `Task.artifactAdded` | `task` | `info` | `user` | — | ✅ | 0 | *无* |
| `Task.created` | `task` | `info` | `user` | — | ✅ | 0 | *无* |
| `Task.progressed` | `task` | `info` | `user` | — | ✅ | 0 | *无* |
| `Task.statusChanged` | `task` | `info` | `user` | — | ✅ | 0 | *无* |
| `Task.stepFinished` | `task` | `info` | `user` | — | ✅ | 0 | *无* |
| `Task.stepStarted` | `task` | `info` | `user` | — | ✅ | 0 | *无* |
| `Workflow.progress` | `system` | `info` | `both` | ✅ | ✅ | 1 | `harness/observation/exec_observer.py:179` |

## 8. 事件名与 Action 名同形（区分用）

同一个名字既是平台 Action（可以调用）又是平台事件（只能等待）。指南里的“事件名不是 Action”说的就是这一类：`引用点` 是字符串出现次数，两侧混计。

| 名字 | 事件目录 | Action 目录 | 引用点（两侧混计） | 位置 |
|---|---|---|---:|---|
| `Page.close` | ✅ | ✅ | 25 | `harness/fleet/runtime.py:1621`, `harness/fleet/runtime.py:1746` 等 25 处 |
| `Page.navigate` | ✅ | ✅ | 36 | `harness/fleet/runtime.py:1746`, `harness/observation/content_completeness.py:1252` 等 36 处 |
| `Page.switchTo` | ✅ | ✅ | 5 | `harness/observation/content_completeness.py:1314`, `harness/observation/progress.py:33` 等 5 处 |

## 9. 平台事件漂移与覆盖差

### 9.1 源码快照有、安装包没有（该运行时已移除）

引用点大于 0 的行是**死分支**：代码在等一个该运行时不会再发的事件。

| 事件 | Agent 可见 | 源码定义 | 引用点 | 位置 |
|---|---|---|---:|---|
| `DOM.axTreeUpdated` | ✅ | `abcp-platform/packages/events/src/domains/DOM/events.ts:11` | 2 | `harness/observation/event_observer.py:127`, `harness/observation/event_observer.py:319` |

### 9.2 安装包有、源码快照没有（本地快照落后）

*无。*

### 9.3 Agent 可见但 harness 没有任何引用（无人消费）

| 事件 | category | severity | 安装包 |
|---|---|---|---|
| `File.chooserOpened` | `blocking` | `blocking` | ✅ |
| `File.operationCompleted` | `blocking` | `success` | ✅ |
| `File.operationFailed` | `blocking` | `error` | ✅ |

### 9.4 harness 里出现的非事件平台形名

形如 `Domain.name` 的字符串常量，但不在事件目录里：绝大多数是 Action 名（正常，调用侧）。`类别` 为 `⚠未知` 且 `安装包字符串` 为 `❌` 的行排在最前：代码引用的名字既不属于任何目录，已安装运行时包里也找不到（已删除的 Action或旧版事件名）。`前缀` 类是故意的前缀匹配常量，不是名字。

| 名字 | 类别 | 安装包字符串 | 出现次数 | 位置 |
|---|---|---|---:|---|
| `DOM.getAttribute` | ⚠未知 | ❌ | 2 | `skills/_tools/distill_trace.py:155`, `skills/_tools/distill_trace.py:173` |
| `DOM.getSemanticTree` | ⚠未知 | ❌ | 1 | `skills/_tools/distill_trace.py:22` |
| `DOM.getText` | ⚠未知 | ❌ | 3 | `skills/_tools/distill_trace.py:155`, `skills/_tools/distill_trace.py:167` 等 3 处 |
| `File.download` | ⚠未知 | ❌ | 5 | `agent_harness.py:3343`, `agent_harness.py:3376` 等 5 处 |
| `DOM.get` | 前缀 | ❌ | 1 | `agent_harness.py:335` |
| `System.describe` | 前缀 | ❌ | 1 | `agent_harness.py:334` |
| `System.list` | 前缀 | ❌ | 1 | `agent_harness.py:333` |
| `Bookmark.folder` | Action | ✅ | 1 | `harness/tools/tool_policy.py:442` |
| `Bookmark.list` | Action | ✅ | 2 | `agent_harness.py:339`, `harness/tools/tool_policy.py:443` |
| `Bookmark.remove` | Action | ✅ | 1 | `harness/tools/tool_policy.py:444` |
| `Bookmark.rename` | Action | ✅ | 1 | `harness/tools/tool_policy.py:445` |
| `Bookmark.upsert` | Action | ✅ | 1 | `harness/tools/tool_policy.py:446` |
| `DOM.getAXTree` | Action | ✅ | 69 | `abcp_client.py:555`, `harness/constants.py:37` 等 69 处 |
| `DOM.getImg` | Action | ✅ | 11 | `agent_harness.py:3342`, `agent_harness.py:3375` 等 11 处 |
| `DOM.inspectSelect` | Action | ✅ | 14 | `harness/constants.py:267`, `harness/diagnostics/error_classification.py:50` 等 14 处 |
| `Download.control` | Action | ✅ | 2 | `agent_harness.py:3386`, `agent_harness.py:3392` |
| `Download.list` | Action | ✅ | 9 | `agent_harness.py:336`, `agent_harness.py:3386` 等 9 处 |
| `Download.start` | Action | ✅ | 14 | `devtools/download_path_live_canary.py:104`, `devtools/download_path_live_canary.py:140` 等 14 处 |
| `File.handleChooser` | Action | ✅ | 8 | `agent_harness.py:3344`, `agent_harness.py:3377` 等 8 处 |
| `Fleet.close` | Action | ✅ | 5 | `devtools/download_path_live_canary.py:103`, `devtools/download_path_live_canary.py:185` 等 5 处 |
| `Fleet.create` | Action | ✅ | 7 | `devtools/download_path_live_canary.py:103`, `devtools/download_path_live_canary.py:110` 等 7 处 |
| `Fleet.list` | Action | ✅ | 5 | `harness/observation/progress.py:105`, `harness/spawner/spawner_core.py:2208` 等 5 处 |
| `History.list` | Action | ✅ | 2 | `agent_harness.py:340`, `harness/tools/tool_policy.py:447` |
| `History.remove` | Action | ✅ | 1 | `harness/tools/tool_policy.py:448` |
| `Hitl.requestPause` | Action | ✅ | 20 | `harness/diagnostics/__init__.py:111`, `harness/diagnostics/error_classification.py:923` 等 20 处 |
| `Hitl.resolvePause` | Action | ✅ | 7 | `harness/runtime/hitl.py:307`, `harness/runtime/hitl.py:311` 等 7 处 |
| `Input.click` | Action | ✅ | 27 | `harness/diagnostics/selector_audit.py:45`, `harness/diagnostics/selector_audit.py:53` 等 27 处 |
| `Input.drag` | Action | ✅ | 8 | `harness/diagnostics/selector_audit.py:49`, `harness/observation/loop_nudge.py:175` 等 8 处 |
| `Input.press` | Action | ✅ | 18 | `harness/diagnostics/selector_audit.py:46`, `harness/diagnostics/selector_audit.py:53` 等 18 处 |
| `Input.scroll` | Action | ✅ | 15 | `harness/observation/content_completeness.py:1373`, `harness/observation/loop_nudge.py:152` 等 15 处 |
| `Input.select` | Action | ✅ | 21 | `harness/diagnostics/error_classification.py:50`, `harness/diagnostics/error_classification.py:53` 等 21 处 |
| `Input.type` | Action | ✅ | 15 | `harness/diagnostics/selector_audit.py:48`, `harness/diagnostics/selector_audit.py:53` 等 15 处 |
| `Memory.delete` | Action | ✅ | 1 | `harness/tools/tool_policy.py:406` |
| `Memory.get` | Action | ✅ | 14 | `agent_harness.py:337`, `agent_harness.py:2540` 等 14 处 |
| `Memory.list` | Action | ✅ | 2 | `agent_harness.py:338`, `harness/tools/tool_policy.py:450` |
| `Memory.save` | Action | ✅ | 13 | `agent_harness.py:2540`, `agent_harness.py:2542` 等 13 处 |
| `Page.click` | Action | ✅ | 16 | `harness/fleet/runtime.py:578`, `harness/fleet/runtime.py:1643` 等 16 处 |
| `Page.create` | Action | ✅ | 44 | `devtools/download_path_live_canary.py:103`, `devtools/download_path_live_canary.py:112` 等 44 处 |
| `Page.getState` | Action | ✅ | 63 | `agent_harness.py:343`, `devtools/download_path_live_canary.py:103` 等 63 处 |
| `Page.go` | Action | ✅ | 18 | `harness/observation/content_completeness.py:1252`, `harness/observation/content_completeness.py:1260` 等 18 处 |
| `Page.handleDialog` | Action | ✅ | 5 | `harness/tools/browser_tools/axtree_state.py:47`, `harness/tools/browser_tools/capability.py:148` 等 5 处 |
| `Page.list` | Action | ✅ | 26 | `agent_harness.py:344`, `harness/fleet/runtime.py:1613` 等 26 处 |
| `Page.reload` | Action | ✅ | 12 | `harness/observation/content_completeness.py:1252`, `harness/observation/page_fingerprint.py:263` 等 12 处 |
| `Page.screenshot` | Action | ✅ | 13 | `agent_harness.py:345`, `agent_harness.py:2105` 等 13 处 |
| `Page.wheel` | Action | ✅ | 13 | `harness/observation/content_completeness.py:1373`, `harness/observation/loop_nudge.py:224` 等 13 处 |
| `Runtime.evaluate` | Action | ✅ | 26 | `agent_harness.py:3883`, `harness/constants.py:265` 等 26 处 |
| `System.describeAction` | Action | ✅ | 8 | `devtools/download_path_live_canary.py:105`, `harness/capabilities/schema_loader.py:133` 等 8 处 |
| `System.describeEvent` | Action | ✅ | 5 | `devtools/download_path_live_canary.py:107`, `harness/observation/progress.py:36` 等 5 处 |
| `System.get` | 前缀 | ✅ | 1 | `agent_harness.py:332` |
| `System.getCapabilities` | Action | ✅ | 11 | `agent_harness.py:6135`, `devtools/download_path_live_canary.py:101` 等 11 处 |
| `System.listEvents` | Action | ✅ | 1 | `devtools/download_path_live_canary.py:102` |
| `System.notification` | 协议方法 | ✅ | 4 | `abcp_client.py:697`, `harness/fleet/runtime.py:599` 等 4 处 |
| `System.register` | Action | ✅ | 12 | `abcp_client.py:563`, `agent_harness.py:2441` 等 12 处 |
| `Workflow.execute` | Action | ✅ | 34 | `abcp_client.py:495`, `agent_harness.py:3301` 等 34 处 |
| `Workflow.getStatus` | Action | ✅ | 1 | `harness/skill/workflow.py:194` |

