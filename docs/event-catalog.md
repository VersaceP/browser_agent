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
| 出站 run event 产生点 | 414 |
| 出站 run event 类型 | 346 |
| 动态（非字面量）事件名 | 35 |
| 出站 trace 产生点 | 49 |
| 出站 trace type | 36 |
| 入站平台事件（目录内） | 43 |
| 入站平台事件（安装包内） | 41 |
| 入站平台事件（Agent 可见） | 30 |
| 入站平台事件（harness 有引用） | 22 |
| 入站平台事件（Agent 可见但无引用） | 8 |

### 1.1 事件族与方向

| 族 | 方向 | 谁产生 | 谁消费 | 章节 |
|---|---|---|---|---|
| run event | 出站：harness → run log / SQLite | `logger.write`、`_write_agent_event`、`_log` 包装器 | `read_events`、Console、replay、审计 | §2-§4、§6 |
| trace | 出站：harness → `agent.trace` / `worker_trace_events` | `*.trace.append({"type": ...})` | judge、worker trace 表读者 | §5、§6 |
| WebCross 平台事件 | 入站：Dispatcher → harness（`System.notification`） | 平台（不在本仓库） | `event_observer`、reducers、`fleet.runtime`、`page_lifecycle` | §7-§9 |

入站事件不会产生 §4 里的任何一行；它们被消费后**才**可能触发一条出站 run event（已核实的两条链：`Hitl.paused` → `workflow.hitl_barrier.claimed_by_event_observer`；`Page.dialogOpened` → `page.dialog.ledger`）。这条因果只能靠 §7 的引用点与 §4 的位置对照读出，机械提取不会替它编造联系。

### 1.2 证据来源与扫描范围

- 平台源码快照 `abcp-platform/`：commit `6b9647d`（`v0.9.35-25-g6b9647d`，2026-10-07）→ §7 的定义、行号与 Agent 投影
- 已安装 CLI：`0.9.6-beta`
- 已安装运行时包：`/Applications/WebCross.app/Contents/Resources/app.asar`（6.7 MB，mtime 2026-10-12 10:52）→ §7 `安装包` 列的证据来源
- 出站扫描范围：`git ls-files --cached --others --exclude-standard '*.py'` 去掉 `tests/`、`docs/` 与本生成器，共 218 个文件

| run event 产生通道 | 产生点 |
|---|---:|
| `logger.write` | 340 |
| `_log` | 41 |
| `_write_agent_event` | 32 |
| `log_event=` | 1 |

## 2. 事件命名空间分布

| 前缀 | 产生点 |
|---|---:|
| `spawner` | 60 |
| `browser` | 44 |
| `hitl` | 22 |
| `vl` | 22 |
| `agent` | 21 |
| `workflow` | 20 |
| `schema` | 15 |
| `skill_builder` | 15 |
| `event` | 12 |
| `{}` | 12 |
| `memory` | 11 |
| `auth_fleet` | 10 |
| `context` | 10 |
| `axtree` | 9 |
| `fleet_click_gate` | 9 |
| `event_type` | 6 |
| `fast_path` | 6 |
| `progress` | 6 |
| `storage` | 6 |
| `download` | 5 |
| `page` | 5 |
| `runtime` | 5 |
| `direct_mode` | 4 |
| `exec` | 4 |
| `local_path` | 4 |
| `observation` | 4 |
| `pacing` | 4 |
| `skill` | 4 |
| `task_phase` | 4 |
| `tool` | 4 |
| `page_session` | 3 |
| `repair` | 3 |
| `resume` | 3 |
| `task` | 3 |
| `task_plan` | 3 |
| `tool_result` | 3 |
| `artifact` | 2 |
| `collect_items` | 2 |
| `content_completeness` | 2 |
| `loop_guard` | 2 |
| `row_ledger` | 2 |
| `task_state` | 2 |
| `transport` | 2 |
| `'hitl` | 1 |
| `batch_source` | 1 |
| `challenge` | 1 |
| `completion_receipt` | 1 |
| `dismiss_overlay` | 1 |
| `event_name` | 1 |
| `event_observer` | 1 |
| `extension_event` | 1 |
| `log_event` | 1 |
| `loop_nudge` | 1 |
| `microloop` | 1 |
| `numeric_claim_extractor` | 1 |
| `page_inventory` | 1 |
| `page_stats` | 1 |
| `prompt` | 1 |
| `semantic_terminal` | 1 |
| `snapshot_diff` | 1 |
| `tool_batch` | 1 |
| `worker` | 1 |

## 3. 产生点最多的文件

| 文件 | run event 产生点 |
|---|---:|
| `harness/tools/browser_tools/capability.py` | 45 |
| `harness/spawner/spawner_slots.py` | 34 |
| `harness/spawner/spawner_core.py` | 26 |
| `harness/agents/browser/loop.py` | 21 |
| `harness/agents/browser/agent.py` | 19 |
| `harness/runtime/hitl.py` | 14 |
| `harness/tools/browser_tools/dispatch.py` | 14 |
| `main.py` | 14 |
| `harness/spawner/spawner_worker.py` | 13 |
| `harness/tools/browser_tools/visual.py` | 13 |
| `harness/tools/browser_tools/hitl.py` | 12 |
| `harness/fleet/runtime.py` | 11 |
| `harness/observation/event_observer.py` | 11 |
| `harness/vl/captcha.py` | 11 |
| `harness/capabilities/bootstrap.py` | 10 |
| `harness/skill_builder/browser.py` | 10 |
| `harness/tools/browser_tools/captcha_autosolve.py` | 8 |
| `harness/context/compaction.py` | 7 |
| `harness/tools/browser_tools/axtree_state.py` | 7 |
| `harness/tools/browser_tools/bindings.py` | 7 |

## 4. run event 类型全表（出站）

| 事件类型 | 产生点 | 通道 | 位置 |
|---|---:|---|---|
| `'hitl.wait.' + str(outcome['status'])` ⚠动态 | 1 | `logger.write` | `harness/runtime/hitl.py:688` |
| `agent.cancelled` | 1 | `_write_agent_event` | `harness/agents/browser/loop.py:1028` |
| `agent.compaction_skipped` | 1 | `_write_agent_event` | `harness/runtime/model_support.py:418` |
| `agent.error` | 1 | `_write_agent_event` | `harness/agents/browser/loop.py:1055` |
| `agent.final` | 1 | `_write_agent_event` | `harness/agents/browser/agent.py:1399` |
| `agent.interrupted` | 1 | `_write_agent_event` | `harness/agents/browser/loop.py:1131` |
| `agent.model` | 1 | `_write_agent_event` | `harness/agents/browser/loop.py:363` |
| `agent.model_connection_error` | 1 | `_write_agent_event` | `harness/agents/browser/loop.py:242` |
| `agent.model_degenerate_response` | 1 | `_write_agent_event` | `harness/agents/browser/loop.py:221` |
| `agent.model_input_moderation_refused` | 1 | `_write_agent_event` | `harness/agents/browser/loop.py:302` |
| `agent.model_protocol_error` | 1 | `_write_agent_event` | `harness/agents/browser/loop.py:277` |
| `agent.model_rate_limited` | 1 | `_write_agent_event` | `harness/agents/browser/loop.py:1035` |
| `agent.model_timeout` | 1 | `_write_agent_event` | `harness/agents/browser/loop.py:261` |
| `agent.multimodal_compaction_deferred` | 1 | `_write_agent_event` | `harness/runtime/model_support.py:449` |
| `agent.multimodal_images_expired` | 1 | `_write_agent_event` | `harness/agents/browser/loop.py:317` |
| `agent.multimodal_screenshot` | 1 | `_write_agent_event` | `harness/agents/browser/loop.py:817` |
| `agent.step.start` | 1 | `_write_agent_event` | `harness/agents/browser/loop.py:169` |
| `agent.step_cap.reminder` | 1 | `_write_agent_event` | `harness/agents/browser/agent.py:1030` |
| `agent.step_extension.denied` | 1 | `_write_agent_event` | `harness/agents/browser/agent.py:1211` |
| `agent.step_extension.granted` | 1 | `_write_agent_event` | `harness/agents/browser/agent.py:1242` |
| `agent.step_extension.requested` | 1 | `_write_agent_event` | `harness/agents/browser/agent.py:1111` |
| `agent.truncated_response` | 1 | `_write_agent_event` | `harness/agents/browser/loop.py:464` |
| `artifact.delivery_exported` | 1 | `logger.write` | `main.py:2418` |
| `artifact.delivery_failed` | 1 | `logger.write` | `main.py:2412` |
| `auth_fleet.ledger_handler_failed` | 1 | `logger.write` | `harness/tools/browser_tools/hitl.py:1736` |
| `auth_fleet.lost_handler_failed` | 1 | `logger.write` | `harness/tools/browser_tools/page_create.py:142` |
| `auth_fleet.operator_reset` | 1 | `logger.write` | `harness/spawner/spawner_slots.py:2700` |
| `auth_fleet.reconciled` | 1 | `logger.write` | `harness/spawner/spawner_slots.py:1575` |
| `auth_fleet.resolver_claimed_for_page_create` | 1 | `logger.write` | `harness/tools/browser_tools/bindings.py:891` |
| `auth_fleet.resolver_relinquished` | 1 | `logger.write` | `harness/tools/browser_tools/bindings.py:935` |
| `auth_fleet.resolver_relinquished_after_page_create` | 1 | `logger.write` | `harness/tools/browser_tools/bindings.py:965` |
| `auth_fleet.session_release_conflict` | 1 | `logger.write` | `harness/spawner/spawner_core.py:1950` |
| `auth_fleet.session_released` | 1 | `logger.write` | `harness/spawner/spawner_slots.py:2601` |
| `auth_fleet.verified_record` | 1 | `logger.write` | `harness/spawner/spawner_slots.py:2571` |
| `axtree.event.invalidated` | 1 | `_log` | `harness/observation/event_observer.py:326` |
| `axtree.event.other_page` | 1 | `_log` | `harness/observation/event_observer.py:284` |
| `axtree.event.updated` | 1 | `_log` | `harness/observation/event_observer.py:314` |
| `axtree.invalidated` | 1 | `logger.write` | `harness/tools/browser_tools/axtree_state.py:1075` |
| `axtree.invalidation_superseded_by_event` | 1 | `logger.write` | `harness/tools/browser_tools/axtree_state.py:739` |
| `axtree.parse_inconsistent` | 1 | `logger.write` | `harness/tools/browser_tools/axtree_state.py:639` |
| `axtree.rematch_observed` | 1 | `logger.write` | `harness/tools/browser_tools/axtree_state.py:1016` |
| `axtree.snapshot` | 1 | `logger.write` | `harness/tools/browser_tools/axtree_state.py:681` |
| `axtree.target_resolution_fallback` | 1 | `logger.write` | `harness/tools/browser_tools/axtree_state.py:966` |
| `batch_source.materialized` | 1 | `logger.write` | `harness/task_control/cohorts.py:661` |
| `browser.bootstrap` | 1 | `logger.write` | `harness/agents/browser/agent.py:302` |
| `browser.call.arguments_prepared` | 1 | `logger.write` | `harness/tools/browser_tools/capability.py:538` |
| `browser.call.arguments_repaired` | 2 | `logger.write` | `harness/tools/browser_tools/capability.py:241`, `harness/tools/browser_tools/capability.py:393` |
| `browser.call.captcha_auto_solved` | 1 | `logger.write` | `harness/tools/browser_tools/capability.py:611` |
| `browser.call.contract_violation` | 1 | `logger.write` | `harness/tools/browser_tools/capability.py:401` |
| `browser.call.cross_task_memory_rejected` | 1 | `logger.write` | `harness/tools/browser_tools/capability.py:412` |
| `browser.call.dialog_rejected` | 1 | `logger.write` | `harness/tools/browser_tools/capability.py:483` |
| `browser.call.dom_get_img_output_normalized` | 1 | `logger.write` | `harness/tools/browser_tools/validation.py:508` |
| `browser.call.fleet_auth_gated` | 3 | `logger.write` | `harness/tools/browser_tools/capability.py:456`, `harness/tools/browser_tools/capability.py:595`, `harness/tools/browser_tools/capability.py:623` |
| `browser.call.fleet_binding_rejected` | 1 | `logger.write` | `harness/tools/browser_tools/capability.py:427` |
| `browser.call.internal` | 1 | `logger.write` | `harness/tools/browser_tools/capability.py:1708` |
| `browser.call.lifecycle_gated` | 1 | `logger.write` | `harness/tools/browser_tools/capability.py:472` |
| `browser.call.navigation_context_rejected` | 1 | `logger.write` | `harness/tools/browser_tools/capability.py:260` |
| `browser.call.page_binding_rejected` | 1 | `logger.write` | `harness/tools/browser_tools/capability.py:440` |
| `browser.call.params_error` | 2 | `logger.write` | `harness/tools/browser_tools/capability.py:200`, `harness/tools/browser_tools/capability.py:493` |
| `browser.call.purpose_added` | 1 | `logger.write` | `harness/tools/browser_tools/capability.py:529` |
| `browser.call.rejected` | 1 | `logger.write` | `harness/tools/browser_tools/capability.py:219` |
| `browser.call.result` | 2 | `logger.write` | `harness/tools/browser_tools/capability.py:1151`, `harness/tools/browser_tools/capability.py:1739` |
| `browser.call.schema_rejected` | 2 | `logger.write` | `harness/tools/browser_tools/capability.py:236`, `harness/tools/browser_tools/capability.py:558` |
| `browser.call.screenshot_output_normalized` | 2 | `logger.write` | `harness/tools/browser_tools/capability.py:272`, `harness/tools/browser_tools/capability.py:1457` |
| `browser.call.select_replay_blocked` | 1 | `logger.write` | `harness/tools/browser_tools/navigate.py:2509` |
| `browser.call.stale_axtree_target` | 2 | `logger.write` | `harness/tools/browser_tools/capability.py:514`, `harness/tools/browser_tools/capability.py:1498` |
| `browser.task_checkpoint` | 1 | `logger.write` | `harness/tools/browser_tools/dispatch.py:1484` |
| `browser.task_review` | 1 | `_write_agent_event` | `harness/agents/browser/control.py:310` |
| `browser.task_review.checkpoint` | 1 | `logger.write` | `harness/agents/browser/review/task.py:1410` |
| `browser.task_review.deferred` | 1 | `_write_agent_event` | `harness/agents/browser/control.py:243` |
| `browser.task_review.model` | 1 | `logger.write` | `harness/agents/browser/review/task.py:1471` |
| `browser.task_review.session_unavailable` | 2 | `logger.write` | `harness/agents/browser/review/task.py:1417`, `harness/agents/browser/review/task.py:1436` |
| `browser.task_review.skipped` | 2 | `_write_agent_event` | `harness/agents/browser/control.py:205`, `harness/agents/browser/control.py:255` |
| `browser.task_review.start` | 1 | `_write_agent_event` | `harness/agents/browser/control.py:298` |
| `browser.task_review.tool` | 1 | `logger.write` | `harness/agents/browser/review/task.py:1587` |
| `browser.task_review.verdict` | 1 | `logger.write` | `harness/agents/browser/review/task.py:1549` |
| `browser.tool.routing_rejected` | 1 | `logger.write` | `harness/tools/browser_tools/dispatch.py:813` |
| `browser.transport.fatal` | 1 | `logger.write` | `harness/tools/browser_tools/capability.py:918` |
| `challenge.navigation_cleared` | 1 | `logger.write` | `harness/tools/browser_tools/navigate.py:1131` |
| `collect_items.result` | 2 | `logger.write` | `harness/tools/browser_tools/composites/collect_items.py:792`, `harness/tools/browser_tools/composites/collect_items.py:1279` |
| `completion_receipt.persisted` | 1 | `logger.write` | `harness/results/completion_receipt.py:618` |
| `content_completeness.artifact_region_credit` | 1 | `logger.write` | `harness/tools/browser_tools/record_extraction.py:143` |
| `content_completeness.observed` | 1 | `logger.write` | `harness/tools/browser_tools/navigate.py:1005` |
| `context.compacted` | 1 | `logger.write` | `harness/context/compaction.py:1177` |
| `context.compaction_failed` | 1 | `logger.write` | `harness/context/compaction.py:1128` |
| `context.compaction_fallback` | 1 | `logger.write` | `harness/context/compaction.py:1050` |
| `context.compaction_rejected` | 1 | `logger.write` | `harness/context/compaction.py:1151` |
| `context.compaction_requested` | 1 | `logger.write` | `harness/agents/browser/agent.py:1059` |
| `context.compaction_skipped` | 3 | `logger.write` | `harness/context/compaction.py:966`, `harness/context/compaction.py:994`, `harness/context/compaction.py:1098` |
| `context.snapshot.failed` | 1 | `logger.write` | `harness/agents/browser/loop.py:1126` |
| `context.snapshot.saved` | 1 | `logger.write` | `harness/utils.py:1153` |
| `direct_mode.amendment_registered` | 1 | `logger.write` | `harness/agents/browser/task.py:282` |
| `direct_mode.assignment_started` | 1 | `logger.write` | `harness/agents/browser/task.py:198` |
| `direct_mode.final` | 1 | `logger.write` | `main.py:2451` |
| `direct_mode.task_registered` | 1 | `logger.write` | `harness/agents/browser/task.py:434` |
| `dismiss_overlay.result` | 1 | `logger.write` | `harness/tools/browser_tools/visual.py:81` |
| `download.event_dropped` | 1 | `logger.write` | `harness/tools/browser_tools/downloads.py:1072` |
| `download.event_observed` | 1 | `logger.write` | `harness/tools/browser_tools/downloads.py:1096` |
| `download.operation_reused` | 1 | `logger.write` | `harness/tools/browser_tools/capability.py:680` |
| `download.reuse_invalidated` | 1 | `logger.write` | `harness/tools/browser_tools/downloads.py:569` |
| `download.timeout_reconciled` | 1 | `logger.write` | `harness/tools/browser_tools/capability.py:715` |
| `event` ⚠动态 | 12 | `logger.write` | `harness/agents/browser/agent.py:395`, `harness/fleet/runtime.py:1296`, `harness/observation/event_observer.py:331`, `harness/spawner/spawner_core.py:395` 等 12 处 |
| `event_name` ⚠动态 | 1 | `logger.write` | `harness/tools/browser_tools/navigate.py:977` |
| `event_observer.error` | 1 | `logger.write` | `harness/observation/event_observer.py:132` |
| `event_type` ⚠动态 | 6 | `logger.write` | `harness/agents/browser/agent.py:170`, `harness/agents/browser/task.py:29`, `harness/evidence/extraction_artifacts.py:319`, `harness/tools/browser_tools/visual.py:637` 等 6 处 |
| `exec.segment.projected` | 1 | `logger.write` | `harness/tools/browser_tools/capability.py:1264` |
| `exec.segment.projection_skipped` | 1 | `logger.write` | `harness/tools/browser_tools/capability.py:1256` |
| `exec.segment.result` | 2 | `logger.write` | `harness/tools/browser_tools/capability.py:1356`, `harness/tools/browser_tools/capability.py:1384` |
| `extension_event` ⚠动态 | 1 | `_write_agent_event` | `harness/agents/browser/loop.py:998` |
| `fast_path.replan_checkpoint` | 1 | `logger.write` | `harness/task_control/replan.py:508` |
| `fast_path.replan_checkpoint_business_contract_unavailable` | 1 | `logger.write` | `harness/task_control/replan.py:617` |
| `fast_path.replan_checkpoint_invalidated` | 1 | `logger.write` | `harness/task_control/replan.py:183` |
| `fast_path.replan_checkpoint_predecessor_mismatch` | 2 | `logger.write` | `harness/task_control/replan.py:293`, `harness/task_control/replan.py:309` |
| `fast_path.replan_checkpoint_progress_mismatch` | 1 | `logger.write` | `harness/task_control/replan.py:380` |
| `fleet_click_gate.acquired` | 1 | `_log` | `harness/fleet/runtime.py:912` |
| `fleet_click_gate.disabled` | 1 | `logger.write` | `harness/spawner/spawner_core.py:182` |
| `fleet_click_gate.outcome` | 2 | `_log` | `harness/fleet/runtime.py:2010`, `harness/fleet/runtime.py:2048` |
| `fleet_click_gate.reconciliation_error` | 2 | `_log` | `harness/fleet/runtime.py:1927`, `harness/fleet/runtime.py:1964` |
| `fleet_click_gate.redirected_to_auth_barrier` | 1 | `_log` | `harness/fleet/runtime.py:1462` |
| `fleet_click_gate.rejected` | 1 | `_log` | `harness/fleet/runtime.py:891` |
| `fleet_click_gate.released` | 1 | `_log` | `harness/fleet/runtime.py:762` |
| `hitl.auto_request_pause` | 1 | `logger.write` | `harness/tools/browser_tools/hitl.py:1304` |
| `hitl.feedback_model_request` | 1 | `_write_agent_event` | `harness/agents/browser/loop.py:196` |
| `hitl.feedback_received` | 1 | `logger.write` | `harness/tools/browser_tools/hitl.py:1888` |
| `hitl.input.failed` | 1 | `logger.write` | `harness/runtime/hitl.py:663` |
| `hitl.operator_input_received` | 1 | `logger.write` | `harness/runtime/hitl.py:653` |
| `hitl.pause_snapshot.captured` | 1 | `logger.write` | `harness/tools/browser_tools/hitl.py:1431` |
| `hitl.pause_snapshot.failed` | 1 | `logger.write` | `harness/tools/browser_tools/hitl.py:1405` |
| `hitl.post_resume.confirmation_input_failed` | 1 | `logger.write` | `harness/tools/browser_tools/hitl.py:572` |
| `hitl.post_resume.confirmation_non_tty` | 1 | `logger.write` | `harness/tools/browser_tools/hitl.py:555` |
| `hitl.post_resume.raw_call` | 1 | `logger.write` | `harness/tools/browser_tools/hitl.py:744` |
| `hitl.refused` | 1 | `logger.write` | `harness/tools/browser_tools/hitl.py:1166` |
| `hitl.wait.branch_error` | 1 | `logger.write` | `harness/runtime/hitl.py:806` |
| `hitl.wait.page_settled_after_hitl` | 2 | `logger.write` | `harness/runtime/hitl.py:891`, `harness/runtime/hitl.py:1085` |
| `hitl.wait.resumed` | 2 | `logger.write` | `harness/runtime/hitl.py:906`, `harness/runtime/hitl.py:1101` |
| `hitl.wait.settlement_check` | 2 | `logger.write` | `harness/runtime/hitl.py:925`, `harness/runtime/hitl.py:1016` |
| `hitl.wait.stale_pause_deadlock` | 2 | `logger.write` | `harness/runtime/hitl.py:864`, `harness/runtime/hitl.py:1056` |
| `hitl.wait.start` | 1 | `logger.write` | `harness/runtime/hitl.py:736` |
| `hitl.wait.timeout` | 1 | `logger.write` | `harness/runtime/hitl.py:830` |
| `local_path.access_intent.preflight` | 1 | `_log` | `harness/tools/path_authorization.py:309` |
| `local_path.authorization.denied` | 1 | `_log` | `harness/tools/path_authorization.py:198` |
| `local_path.authorization.granted` | 1 | `_log` | `harness/tools/path_authorization.py:207` |
| `local_path.authorization.requested` | 1 | `_log` | `harness/tools/path_authorization.py:187` |
| `log_event` ⚠动态 | 1 | `_log` | `harness/observation/event_observer.py:181` |
| `loop_guard.observed` | 1 | `logger.write` | `harness/tools/loop_guard.py:139` |
| `loop_guard.spend_limit` | 1 | `logger.write` | `harness/tools/loop_guard.py:111` |
| `loop_nudge.detected` | 1 | `logger.write` | `harness/agents/browser/loop.py:780` |
| `memory.bootstrap` | 1 | `logger.write` | `harness/agents/browser/agent.py:378` |
| `memory.bootstrap.foreign_context` | 1 | `logger.write` | `harness/agents/browser/agent.py:356` |
| `memory.bootstrap.get_failed` | 1 | `logger.write` | `harness/agents/browser/agent.py:345` |
| `memory.bootstrap.skipped` | 1 | `logger.write` | `harness/agents/browser/agent.py:335` |
| `memory.bootstrap.unsupported_contract` | 1 | `logger.write` | `harness/agents/browser/agent.py:330` |
| `memory.heartbeat` | 1 | `_write_agent_event` | `harness/agents/browser/agent.py:462` |
| `memory.heartbeat.failed` | 1 | `_write_agent_event` | `harness/agents/browser/agent.py:472` |
| `memory.heartbeat.stop_failed` | 1 | `_write_agent_event` | `harness/agents/browser/agent.py:489` |
| `memory.terminal_checkpoint.failed` | 3 | `logger.write` | `harness/spawner/spawner_worker.py:251`, `harness/spawner/spawner_worker.py:265`, `harness/spawner/spawner_worker.py:275` |
| `microloop.telemetry` | 1 | `logger.write` | `harness/tools/browser_tools/auto_intercept.py:113` |
| `numeric_claim_extractor.call` | 1 | `logger.write` | `harness/results/numeric_facts.py:684` |
| `observation.watch_closed` | 1 | `_log` | `harness/tools/browser_tools/observation_view.py:298` |
| `observation.watch_events_delivered` | 1 | `_log` | `harness/tools/browser_tools/observation_view.py:414` |
| `observation.watch_opened` | 1 | `_log` | `harness/tools/browser_tools/observation_view.py:360` |
| `observation.watch_wait` | 1 | `_log` | `harness/tools/browser_tools/observation_view.py:388` |
| `pacing.phase.wait_completed` | 1 | `logger.write` | `harness/spawner/spawner_core.py:1441` |
| `pacing.phase.wait_started` | 1 | `logger.write` | `harness/spawner/spawner_core.py:1439` |
| `pacing.row.wait_completed` | 1 | `logger.write` | `harness/planning/pacing.py:105` |
| `pacing.row.wait_started` | 1 | `logger.write` | `harness/planning/pacing.py:102` |
| `page.dialog.ledger` | 1 | `log_event=` | `harness/observation/event_observer.py:162` |
| `page.lifecycle.after_action` | 1 | `logger.write` | `harness/tools/browser_tools/dispatch.py:293` |
| `page.lifecycle.event` | 1 | `_log` | `harness/observation/event_observer.py:119` |
| `page.lifecycle.navigation_resync\|page.lifecycle.timeout_resync` ⚠动态 | 1 | `logger.write` | `harness/tools/browser_tools/dispatch.py:201` |
| `page.lifecycle.settlement_wait` | 1 | `logger.write` | `harness/tools/browser_tools/dispatch.py:173` |
| `page_inventory.changed` | 1 | `_log` | `harness/observation/event_observer.py:213` |
| `page_session.record_failed` | 1 | `logger.write` | `harness/observation/page_session.py:693` |
| `page_session.recorded` | 1 | `logger.write` | `harness/observation/page_session.py:686` |
| `page_session.render_failed` | 1 | `logger.write` | `harness/observation/page_session.py:765` |
| `page_stats.detected` | 1 | `logger.write` | `harness/agents/browser/loop.py:757` |
| `progress.history_navigation_unverified` | 1 | `logger.write` | `harness/tools/browser_tools/navigate.py:1224` |
| `progress.mandatory_recovery_credit_used` | 1 | `logger.write` | `harness/tools/browser_tools/progress_obs.py:343` |
| `progress.observed` | 1 | `logger.write` | `harness/tools/browser_tools/progress_obs.py:384` |
| `progress.repair_advanced` | 1 | `logger.write` | `harness/tools/browser_tools/progress_obs.py:417` |
| `progress.snapshot` | 1 | `logger.write` | `harness/tools/browser_tools/progress_obs.py:425` |
| `progress.unrecorded_rows_observed` | 1 | `logger.write` | `harness/tools/browser_tools/progress_obs.py:139` |
| `prompt.guides.degraded` | 1 | `logger.write` | `harness/runtime/model_support.py:37` |
| `repair.visual_evidence_abandoned` | 1 | `logger.write` | `harness/tools/browser_tools/record_extraction.py:223` |
| `repair.visual_evidence_satisfied` | 1 | `logger.write` | `harness/tools/browser_tools/visual.py:363` |
| `repair.visual_page_rejected` | 1 | `logger.write` | `harness/tools/browser_tools/visual.py:449` |
| `resume.instruction_received` | 1 | `logger.write` | `main.py:2128` |
| `resume.projection_built` | 1 | `logger.write` | `main.py:2225` |
| `resume.started` | 1 | `logger.write` | `main.py:2207` |
| `row_ledger.error` | 1 | `logger.write` | `harness/spawner/spawner_worker.py:217` |
| `row_ledger.recorded` | 1 | `logger.write` | `harness/spawner/spawner_worker.py:222` |
| `runtime.evaluate.prepared` | 1 | `logger.write` | `harness/tools/browser_tools/capability.py:294` |
| `runtime.evaluate.rejected` | 2 | `logger.write` | `harness/tools/browser_tools/capability.py:286`, `harness/tools/browser_tools/capability.py:1475` |
| `runtime.evaluate.trusted_collection_template` | 1 | `logger.write` | `harness/tools/browser_tools/runtime_eval.py:85` |
| `runtime.evaluate.world_evidence_degraded` | 1 | `logger.write` | `harness/tools/browser_tools/capability.py:806` |
| `schema.bootstrap.cached` | 3 | `logger.write` | `harness/capabilities/bootstrap.py:124`, `harness/capabilities/bootstrap.py:144`, `harness/capabilities/bootstrap.py:170` |
| `schema.bootstrap.done` | 1 | `logger.write` | `harness/capabilities/bootstrap.py:227` |
| `schema.bootstrap.failed` | 3 | `logger.write` | `harness/capabilities/bootstrap.py:77`, `harness/capabilities/bootstrap.py:206`, `harness/capabilities/bootstrap.py:254` |
| `schema.bootstrap.lock_timeout` | 1 | `logger.write` | `harness/capabilities/bootstrap.py:156` |
| `schema.bootstrap.timing` | 1 | `logger.write` | `harness/capabilities/bootstrap.py:278` |
| `schema.bundle.loaded` | 1 | `logger.write` | `harness/capabilities/schema_loader.py:171` |
| `schema.bundle.reused` | 1 | `logger.write` | `harness/spawner/spawner_worker.py:1209` |
| `schema.bundle.stale_revision` | 1 | `logger.write` | `harness/spawner/spawner_worker.py:1198` |
| `schema.contract.fallback` | 1 | `logger.write` | `harness/capabilities/bootstrap.py:296` |
| `schema.describeAction.error` | 1 | `logger.write` | `harness/capabilities/schema_loader.py:136` |
| `schema.describeAction.stale_catalog` | 1 | `logger.write` | `harness/capabilities/schema_loader.py:146` |
| `semantic_terminal.counterevidence` | 1 | `logger.write` | `harness/spawner/spawner_worker.py:720` |
| `skill.fast_path.repair_fallback` | 1 | `logger.write` | `harness/tools/browser_tools/record_extraction.py:221` |
| `skill.invocation.result` | 1 | `logger.write` | `harness/tools/browser_tools/dispatch.py:1046` |
| `skill.invocation.started` | 1 | `logger.write` | `harness/tools/browser_tools/dispatch.py:1032` |
| `skill.workflow.scope_rejected` | 1 | `logger.write` | `harness/tools/browser_tools/capability.py:367` |
| `skill_builder.base.current_copy` | 1 | `logger.write` | `harness/skill_builder/session.py:173` |
| `skill_builder.browser.call` | 1 | `logger.write` | `harness/skill_builder/browser.py:201` |
| `skill_builder.file.written` | 1 | `logger.write` | `harness/skill_builder/session.py:243` |
| `skill_builder.fleet.close_failed` | 1 | `logger.write` | `harness/skill_builder/browser.py:75` |
| `skill_builder.fleet.closed` | 1 | `logger.write` | `harness/skill_builder/browser.py:72` |
| `skill_builder.model_usage` | 1 | `logger.write` | `harness/skill_builder/session.py:414` |
| `skill_builder.page.authorization` | 1 | `logger.write` | `harness/skill_builder/browser.py:131` |
| `skill_builder.page.created` | 1 | `logger.write` | `harness/skill_builder/browser.py:168` |
| `skill_builder.platform.capabilities` | 1 | `logger.write` | `harness/skill_builder/browser.py:96` |
| `skill_builder.platform.registration` | 1 | `logger.write` | `harness/skill_builder/browser.py:90` |
| `skill_builder.started` | 1 | `logger.write` | `harness/skill_builder/session.py:187` |
| `skill_builder.tool` | 1 | `logger.write` | `harness/skill_builder/session.py:478` |
| `skill_builder.transport` | 1 | `logger.write` | `harness/skill_builder/browser.py:45` |
| `skill_builder.trial.result` | 1 | `logger.write` | `harness/skill_builder/browser.py:308` |
| `skill_builder.trial.start` | 1 | `logger.write` | `harness/skill_builder/browser.py:267` |
| `snapshot_diff.detected` | 1 | `logger.write` | `harness/agents/browser/loop.py:765` |
| `spawner.browser.failure_cleanup.cancel_suppressed` | 1 | `logger.write` | `harness/spawner/spawner_worker.py:302` |
| `spawner.browser.failure_cleanup.failed` | 1 | `logger.write` | `harness/spawner/spawner_worker.py:308` |
| `spawner.browser.result` | 2 | `logger.write` | `harness/spawner/spawner_worker.py:910`, `harness/spawner/spawner_worker.py:1147` |
| `spawner.browser.spawn` | 1 | `logger.write` | `harness/spawner/spawner_core.py:2105` |
| `spawner.browser.start_rejected` | 4 | `logger.write` | `harness/spawner/spawner_core.py:1410`, `harness/spawner/spawner_core.py:1416`, `harness/spawner/spawner_core.py:1451`, `harness/spawner/spawner_core.py:1457` |
| `spawner.browser_context.persist_failed` | 2 | `logger.write` | `harness/spawner/spawner_core.py:1113`, `harness/spawner/spawner_registry.py:849` |
| `spawner.browser_context.persist_skipped` | 1 | `logger.write` | `harness/spawner/spawner_core.py:821` |
| `spawner.browser_context.persisted` | 1 | `logger.write` | `harness/spawner/spawner_core.py:987` |
| `spawner.connection_recovery` | 1 | `logger.write` | `harness/spawner/spawner_core.py:2272` |
| `spawner.fleet.assigned` | 1 | `logger.write` | `harness/spawner/spawner_slots.py:2119` |
| `spawner.fleet.assignment_rejected` | 1 | `logger.write` | `harness/spawner/spawner_core.py:1964` |
| `spawner.fleet.cap_blocked` | 1 | `logger.write` | `harness/spawner/spawner_slots.py:294` |
| `spawner.fleet.cap_released` | 1 | `logger.write` | `harness/spawner/spawner_slots.py:284` |
| `spawner.fleet.cap_reuse` | 1 | `logger.write` | `harness/spawner/spawner_slots.py:330` |
| `spawner.fleet.inventory_retired` | 1 | `logger.write` | `harness/spawner/spawner_slots.py:277` |
| `spawner.fleet.notification_relay_attached` | 1 | `logger.write` | `harness/spawner/spawner_slots.py:2517` |
| `spawner.fleet.readiness_failed` | 2 | `logger.write` | `harness/spawner/spawner_slots.py:2294`, `harness/spawner/spawner_slots.py:2333` |
| `spawner.fleet.readiness_ready` | 1 | `logger.write` | `harness/spawner/spawner_slots.py:2257` |
| `spawner.fleet.readiness_started` | 1 | `logger.write` | `harness/spawner/spawner_slots.py:2182` |
| `spawner.fleet.worker_isolation_applied` | 1 | `logger.write` | `harness/spawner/spawner_slots.py:145` |
| `spawner.fleet.worker_isolation_skipped` | 1 | `logger.write` | `harness/spawner/spawner_slots.py:134` |
| `spawner.resume_browser_hint.ignored` | 3 | `logger.write` | `harness/spawner/spawner_core.py:786`, `harness/spawner/spawner_slots.py:1875`, `harness/spawner/spawner_slots.py:1902` |
| `spawner.resume_browser_hint.page_probe_failed` | 1 | `logger.write` | `harness/spawner/spawner_core.py:1678` |
| `spawner.resume_browser_hint.used` | 1 | `logger.write` | `harness/spawner/spawner_slots.py:1884` |
| `spawner.similar_task_reuse.matched` | 1 | `logger.write` | `harness/spawner/spawner_slots.py:2018` |
| `spawner.similar_task_reuse.miss` | 1 | `logger.write` | `harness/spawner/spawner_slots.py:1942` |
| `spawner.similar_task_reuse.readiness_fallback` | 1 | `logger.write` | `harness/spawner/spawner_slots.py:1995` |
| `spawner.similar_task_reuse.rejected` | 1 | `logger.write` | `harness/spawner/spawner_slots.py:2006` |
| `spawner.slot.acquire_exhausted` | 1 | `logger.write` | `harness/spawner/spawner_core.py:1493` |
| `spawner.slot.acquire_failed` | 1 | `logger.write` | `harness/spawner/spawner_core.py:2038` |
| `spawner.slot.bootstrap_timing` | 1 | `logger.write` | `harness/spawner/spawner_slots.py:1219` |
| `spawner.slot.created` | 1 | `logger.write` | `harness/spawner/spawner_slots.py:1236` |
| `spawner.slot.event_replay` | 2 | `logger.write` | `harness/spawner/spawner_slots.py:1090`, `harness/spawner/spawner_slots.py:1133` |
| `spawner.slot.page_quarantine_cleared` | 1 | `logger.write` | `harness/spawner/spawner_registry.py:665` |
| `spawner.slot.page_quarantine_retired` | 1 | `logger.write` | `harness/spawner/spawner_registry.py:624` |
| `spawner.slot.page_quarantined` | 1 | `logger.write` | `harness/spawner/spawner_registry.py:538` |
| `spawner.slot.protocol_identity_changed` | 1 | `logger.write` | `harness/spawner/spawner_slots.py:1022` |
| `spawner.slot.recovered` | 1 | `logger.write` | `harness/spawner/spawner_slots.py:1378` |
| `spawner.slot.recovery_deferred` | 1 | `logger.write` | `harness/spawner/spawner_slots.py:1274` |
| `spawner.slot.recovery_failed` | 1 | `logger.write` | `harness/spawner/spawner_slots.py:1349` |
| `spawner.slot.reserved` | 1 | `logger.write` | `harness/spawner/spawner_slots.py:988` |
| `spawner.slot.retired` | 1 | `logger.write` | `harness/spawner/spawner_slots.py:1409` |
| `spawner.slot.start_cancelled` | 1 | `logger.write` | `harness/spawner/spawner_core.py:1913` |
| `spawner.slot.sync_warning` | 1 | `logger.write` | `harness/spawner/spawner_registry.py:271` |
| `spawner.task_session_binding.candidate_recorded` | 1 | `logger.write` | `harness/spawner/spawner_core.py:613` |
| `spawner.task_session_binding.expired` | 1 | `logger.write` | `harness/spawner/spawner_core.py:284` |
| `spawner.task_session_binding.handler_failed` | 1 | `logger.write` | `harness/tools/browser_tools/hitl.py:1778` |
| `spawner.task_session_binding.load_ambiguous` | 1 | `logger.write` | `harness/spawner/spawner_core.py:360` |
| `spawner.task_session_binding.load_failed` | 1 | `logger.write` | `harness/spawner/spawner_core.py:305` |
| `spawner.task_session_binding.operator_reset` | 1 | `logger.write` | `harness/spawner/spawner_core.py:566` |
| `spawner.task_session_binding.write_failed` | 1 | `logger.write` | `harness/spawner/spawner_core.py:629` |
| `storage.download_registration_failed` | 1 | `logger.write` | `harness/tools/browser_tools/downloads.py:280` |
| `storage.dual_verify` | 2 | `logger.write` | `main.py:1123`, `main.py:1153` |
| `storage.external_file_unregistered` | 1 | `logger.write` | `harness/agents/browser/agent.py:887` |
| `storage.revision_conflict` | 2 | `logger.write` | `main.py:1122`, `main.py:1150` |
| `task.fleet_reference.bound` | 1 | `logger.write` | `main.py:2307` |
| `task.fleet_reference.rejected` | 2 | `logger.write` | `main.py:2286`, `main.py:2299` |
| `task_phase.artifacts_revalidated` | 1 | `logger.write` | `harness/task_control/revalidate.py:129` |
| `task_phase.blocked_by_dependency` | 2 | `logger.write` | `harness/task_control/phase_lifecycle.py:1220`, `harness/task_control/phase_lifecycle.py:1351` |
| `task_phase.exhausted` | 1 | `logger.write` | `harness/task_control/phase_lifecycle.py:1321` |
| `task_plan.accepted` | 2 | `logger.write` | `harness/task_control/plan_validation.py:2505`, `harness/task_control/plan_validation.py:2601` |
| `task_plan.versioned` | 1 | `logger.write` | `harness/task_control/plan_validation.py:2604` |
| `task_state.initialized` | 1 | `logger.write` | `harness/task_control/plan_validation.py:2800` |
| `task_state.resume_prepared` | 1 | `logger.write` | `harness/task_control/phase_lifecycle.py:960` |
| `tool.direct_capability_wrapped` | 1 | `logger.write` | `harness/tools/browser_tools/capability.py:145` |
| `tool.error` | 2 | `logger.write` | `harness/tools/browser_tools/capability.py:160`, `harness/tools/browser_tools/dispatch.py:802` |
| `tool.final_answer` | 1 | `logger.write` | `harness/tools/browser_tools/dispatch.py:1534` |
| `tool_batch.deferred` | 1 | `logger.write` | `harness/agents/browser/loop.py:906` |
| `tool_result.model_visible` | 1 | `logger.write` | `harness/runtime/model_support.py:765` |
| `tool_result.offloaded` | 1 | `logger.write` | `harness/context/offload.py:577` |
| `tool_result.preserve_failed` | 1 | `logger.write` | `harness/context/offload.py:448` |
| `transport.recovery.probe_recorded` | 1 | `logger.write` | `harness/task_control/transport_recovery.py:191` |
| `transport.recovery.required` | 1 | `logger.write` | `harness/task_control/transport_recovery.py:161` |
| `vl.captcha_autosolve.failed` | 1 | `logger.write` | `harness/tools/browser_tools/hitl.py:814` |
| `vl.captcha_autosolve.result` | 1 | `logger.write` | `harness/tools/browser_tools/captcha_autosolve.py:909` |
| `vl.captcha_autosolve.screenshot_attempt_failed` | 1 | `logger.write` | `harness/tools/browser_tools/captcha_autosolve.py:247` |
| `vl.captcha_autosolve.screenshot_retry_recovered` | 1 | `logger.write` | `harness/tools/browser_tools/captcha_autosolve.py:240` |
| `vl.captcha_autosolve.viewport_exhausted` | 1 | `logger.write` | `harness/tools/browser_tools/captcha_autosolve.py:444` |
| `vl.captcha_autosolve.viewport_fallback` | 1 | `logger.write` | `harness/tools/browser_tools/captcha_autosolve.py:434` |
| `vl.captcha_screenshot.retain_failed` | 1 | `logger.write` | `harness/tools/browser_tools/captcha_autosolve.py:290` |
| `vl.captcha_screenshot.retained` | 1 | `logger.write` | `harness/tools/browser_tools/captcha_autosolve.py:297` |
| `vl.captcha_solve` | 1 | `logger.write` | `harness/tools/browser_tools/captcha_autosolve.py:791` |
| `vl.locate.promotion` | 1 | `logger.write` | `harness/tools/browser_tools/visual.py:1758` |
| `vl.locate.promotion_error` | 1 | `logger.write` | `harness/tools/browser_tools/visual.py:1893` |
| `vl.locate.promotion_guard` | 1 | `_log` | `harness/vl/locate.py:836` |
| `vl.locate.result` | 1 | `_log` | `harness/vl/locate.py:681` |
| `vl.overlay_adjudication` | 1 | `logger.write` | `harness/tools/browser_tools/auto_intercept.py:190` |
| `vl.reality_check` | 1 | `logger.write` | `harness/tools/browser_tools/visual.py:1491` |
| `vl.reality_check.capture_unavailable` | 1 | `logger.write` | `harness/tools/browser_tools/visual.py:1369` |
| `vl.reality_check.delivered` | 1 | `_write_agent_event` | `harness/agents/browser/agent.py:959` |
| `vl.reality_check.error` | 2 | `logger.write` | `harness/tools/browser_tools/visual.py:1299`, `harness/tools/browser_tools/visual.py:1505` |
| `vl.reality_check.persist_failed` | 1 | `logger.write` | `harness/tools/browser_tools/visual.py:1463` |
| `vl.visual_recovery_hint` | 1 | `logger.write` | `harness/tools/browser_tools/visual.py:831` |
| `vl.visual_verify` | 1 | `logger.write` | `harness/tools/browser_tools/visual.py:643` |
| `worker.failure_trace_save_failed` | 1 | `logger.write` | `harness/spawner/spawner_worker.py:998` |
| `workflow.auth_fence.before` | 2 | `_log`, `logger.write` | `harness/tools/browser_tools/bindings.py:626`, `harness/workflow/workflow_auth_fence.py:74` |
| `workflow.auth_generation_changed` | 3 | `_log`, `logger.write` | `harness/tools/browser_tools/bindings.py:631`, `harness/tools/browser_tools/bindings.py:750`, `harness/workflow/workflow_auth_fence.py:106` |
| `workflow.definition.prepared` | 1 | `logger.write` | `harness/tools/browser_tools/dispatch.py:924` |
| `workflow.definition.used` | 4 | `logger.write` | `harness/tools/browser_tools/dispatch.py:1104`, `harness/tools/browser_tools/dispatch.py:1153`, `harness/tools/browser_tools/dispatch.py:1289`, `harness/tools/browser_tools/dispatch.py:1319` |
| `workflow.execute.rejected` | 2 | `logger.write` | `harness/tools/browser_tools/capability.py:378`, `harness/tools/browser_tools/capability.py:388` |
| `workflow.execute.runtime_disabled` | 1 | `logger.write` | `harness/tools/browser_tools/capability.py:350` |
| `workflow.hitl_barrier.claimed_by_event_observer` | 1 | `_log` | `harness/fleet/runtime.py:1189` |
| `workflow.hitl_barrier.event_bridge_error` | 1 | `_log` | `harness/observation/event_observer.py:246` |
| `workflow.hitl_barrier.event_ignored` | 1 | `_log` | `harness/observation/event_observer.py:255` |
| `workflow.hitl_barrier.settled` | 1 | `_log` | `harness/fleet/runtime.py:1824` |
| `workflow.row_quarantined` | 2 | `_log`, `logger.write` | `harness/tools/browser_tools/bindings.py:751`, `harness/workflow/workflow_auth_fence.py:107` |
| `workflow.script.scope` | 1 | `logger.write` | `harness/tools/browser_tools/capability.py:583` |
| `{}.model_input_moderation_folded` ⚠动态 | 1 | `logger.write` | `harness/runtime/model_support.py:527` |
| `{}.vl_budget_exceeded` ⚠动态 | 1 | `_log` | `harness/vl/captcha.py:514` |
| `{}.vl_exhausted` ⚠动态 | 1 | `_log` | `harness/vl/captcha.py:510` |
| `{}.vl_no_screenshot` ⚠动态 | 1 | `_log` | `harness/vl/captcha.py:313` |
| `{}.vl_reclassify` ⚠动态 | 1 | `_log` | `harness/vl/captcha.py:425` |
| `{}.vl_solve_error` ⚠动态 | 1 | `_log` | `harness/vl/captcha.py:520` |
| `{}.vl_solved` ⚠动态 | 1 | `_log` | `harness/vl/captcha.py:504` |
| `{}.vl_unsafe_point` ⚠动态 | 1 | `_log` | `harness/vl/captcha.py:489` |
| `{}.vl_verdict` ⚠动态 | 1 | `_log` | `harness/vl/captcha.py:368` |
| `{}.vl_verification_unavailable` ⚠动态 | 1 | `_log` | `harness/vl/captcha.py:275` |
| `{}.vl_viewport_unavailable` ⚠动态 | 1 | `_log` | `harness/vl/captcha.py:300` |
| `{}.{}` ⚠动态 | 1 | `logger.write` | `harness/utils.py:1021` |

## 5. trace type 全表（出站）

| trace type | 产生点 | 位置 |
|---|---:|---|
| `browser_call` | 2 | `harness/tools/browser_tools/capability.py:1195`, `harness/tools/browser_tools/capability.py:1744` |
| `browser_call_params_error` | 2 | `harness/tools/browser_tools/capability.py:209`, `harness/tools/browser_tools/capability.py:499` |
| `browser_call_rejected` | 1 | `harness/tools/browser_tools/capability.py:225` |
| `browser_call_schema_rejected` | 2 | `harness/tools/browser_tools/capability.py:237`, `harness/tools/browser_tools/capability.py:566` |
| `browser_completion_proposal` | 1 | `harness/agents/browser/loop.py:685` |
| `browser_task_checkpoint` | 1 | `harness/tools/browser_tools/dispatch.py:1485` |
| `captcha_auto_solved` | 1 | `harness/tools/browser_tools/capability.py:612` |
| `contract_violation` | 2 | `harness/tools/browser_tools/capability.py:407`, `harness/tools/browser_tools/dispatch.py:825` |
| `cross_task_memory_guard` | 1 | `harness/tools/browser_tools/capability.py:415` |
| `dialog_guard` | 1 | `harness/tools/browser_tools/capability.py:484` |
| `final_answer` | 1 | `harness/tools/browser_tools/dispatch.py:1535` |
| `fleet_auth_gate` | 3 | `harness/tools/browser_tools/capability.py:457`, `harness/tools/browser_tools/capability.py:596`, `harness/tools/browser_tools/capability.py:624` |
| `fleet_binding_guard` | 1 | `harness/tools/browser_tools/capability.py:428` |
| `loop_guard` | 1 | `harness/tools/browser_tools/dispatch.py:783` |
| `loop_nudge` | 1 | `harness/agents/browser/loop.py:781` |
| `loop_observation` | 1 | `harness/tools/loop_guard.py:142` |
| `model` | 2 | `harness/agents/browser/loop.py:402`, `harness/events/sinks.py:182` |
| `navigation_context_rejected` | 1 | `harness/tools/browser_tools/capability.py:264` |
| `page_binding_guard` | 2 | `harness/tools/browser_tools/capability.py:441`, `harness/tools/browser_tools/dispatch.py:814` |
| `page_lifecycle_gate` | 1 | `harness/tools/browser_tools/capability.py:473` |
| `page_stats` | 1 | `harness/agents/browser/loop.py:758` |
| `progress_observation` | 2 | `harness/tools/browser_tools/progress_obs.py:142`, `harness/tools/browser_tools/progress_obs.py:387` |
| `record` | 1 | `harness/tools/browser_tools/dispatch.py:929` |
| `root` | 1 | `harness/diagnostics/judge_trace.py:56` |
| `runtime_evaluation_rejected` | 1 | `harness/tools/browser_tools/capability.py:287` |
| `snapshot_diff` | 1 | `harness/agents/browser/loop.py:766` |
| `stale_axtree_target` | 2 | `harness/tools/browser_tools/capability.py:515`, `harness/tools/browser_tools/capability.py:1499` |
| `step` | 2 | `harness/observation/exec_observer.py:290`, `harness/observation/exec_observer.py:294` |
| `tool_after_call_exception` | 1 | `harness/tools/browser_tools/dispatch.py:645` |
| `tool_arguments_rejected` | 1 | `harness/tools/browser_tools/dispatch.py:625` |
| `tool_error` | 2 | `harness/tools/browser_tools/capability.py:161`, `harness/tools/browser_tools/dispatch.py:803` |
| `tool_exception` | 1 | `harness/tools/browser_tools/dispatch.py:458` |
| `trace_entry` | 1 | `harness/tools/browser_tools/dispatch.py:841` |
| `workflow_definition_outcome` | 1 | `harness/tools/browser_tools/dispatch.py:910` |
| `workflow_policy_rejected` | 2 | `harness/tools/browser_tools/capability.py:379`, `harness/tools/browser_tools/capability.py:389` |
| `workflow_runtime_disabled` | 1 | `harness/tools/browser_tools/capability.py:351` |

## 6. 消费端

机械匹配，可能含误报；用于证明某条产生链是否真的有读者。

### `read_events(` — 读取持久化 run event（8 处）

| 位置 | 代码 |
|---|---|
| `main.py:1664` | `page = storage.read_events(` |
| `harness/skill_builder/context.py:47` | `return self.sqlite.read_events(task_id=self.task_id, after_event_id=after,` |
| `harness/skill_builder/context.py:49` | `return self.file.read_events(task_id=self.task_id, after_event_id=after,` |
| `harness/agents/browser/review/evidence.py:127` | `rows = store.read_events(task_id=self.task_id,` |
| `harness/storage/dual_store.py:444` | `return self.primary.read_events(` |
| `harness/storage/dual_store.py:883` | `rows = store.read_events(task_id=task_id, after_event_id=cursor, limit=1000)` |
| `harness/storage/dual_store.py:905` | `rows = store.read_events(task_id=task_id, after_event_id=cursor, limit=page)` |
| `harness/storage/sqlite_store.py:412` | `return dao.read_events(` |

### `.trace` — 读取 agent.trace 列表（80 处）

| 位置 | 代码 |
|---|---|
| `harness/tools/browser_tools/dispatch.py:783` | `agent.trace.append({"type": "loop_guard", "result": guard_result})` |
| `harness/tools/browser_tools/dispatch.py:803` | `agent.trace.append({"type": "tool_error", "result": result})` |
| `harness/tools/browser_tools/dispatch.py:814` | `agent.trace.append({` |
| `harness/tools/browser_tools/dispatch.py:825` | `agent.trace.append({"type": "contract_violation", "result": contract_result})` |
| `harness/tools/browser_tools/dispatch.py:832` | `if action.trace_type:` |
| `harness/tools/browser_tools/dispatch.py:834` | `trace_entry: JsonDict = {"type": action.trace_type, "result": result}` |
| `harness/tools/browser_tools/dispatch.py:841` | `agent.trace.append(trace_entry)` |
| `harness/tools/browser_tools/dispatch.py:910` | `ctx.agent.trace.append({` |
| `harness/tools/browser_tools/dispatch.py:928` | `ctx.agent.trace = trace` |
| `harness/tools/browser_tools/dispatch.py:1485` | `ctx.agent.trace.append({"type": "browser_task_checkpoint", "result": checkpoint})` |
| `harness/tools/browser_tools/dispatch.py:1535` | `ctx.agent.trace.append({"type": "final_answer", "result": result})` |
| `harness/tools/browser_tools/capability.py:161` | `agent.trace.append({"type": "tool_error", "result": result})` |
| `harness/tools/browser_tools/capability.py:209` | `agent.trace.append({"type": "browser_call_params_error", "result": result})` |
| `harness/tools/browser_tools/capability.py:225` | `agent.trace.append({"type": "browser_call_rejected", "result": result})` |
| `harness/tools/browser_tools/capability.py:237` | `agent.trace.append({"type": "browser_call_schema_rejected", "result": result})` |
| `harness/tools/browser_tools/capability.py:264` | `agent.trace.append({` |
| `harness/tools/browser_tools/capability.py:287` | `agent.trace.append({` |
| `harness/tools/browser_tools/capability.py:351` | `agent.trace.append({` |
| `harness/tools/browser_tools/capability.py:379` | `agent.trace.append({"type": "workflow_policy_rejected", "result": workflow_error})` |
| `harness/tools/browser_tools/capability.py:389` | `agent.trace.append({"type": "workflow_policy_rejected", "result": result})` |
| `harness/tools/browser_tools/capability.py:407` | `agent.trace.append({"type": "contract_violation", "result": contract_result})` |
| `harness/tools/browser_tools/capability.py:415` | `agent.trace.append({` |
| `harness/tools/browser_tools/capability.py:428` | `agent.trace.append({` |
| `harness/tools/browser_tools/capability.py:441` | `agent.trace.append({` |
| `harness/tools/browser_tools/capability.py:457` | `agent.trace.append({` |
| `harness/tools/browser_tools/capability.py:473` | `agent.trace.append({` |
| `harness/tools/browser_tools/capability.py:484` | `agent.trace.append({"type": "dialog_guard", "result": dialog_guard})` |
| `harness/tools/browser_tools/capability.py:499` | `agent.trace.append({"type": "browser_call_params_error", "result": target_param_guard})` |
| `harness/tools/browser_tools/capability.py:515` | `agent.trace.append({"type": "stale_axtree_target", "result": stale_target})` |
| `harness/tools/browser_tools/capability.py:566` | `agent.trace.append({"type": "browser_call_schema_rejected", "result": result})` |
| `harness/tools/browser_tools/capability.py:596` | `agent.trace.append({` |
| `harness/tools/browser_tools/capability.py:612` | `agent.trace.append({` |
| `harness/tools/browser_tools/capability.py:624` | `agent.trace.append({` |
| `harness/tools/browser_tools/capability.py:954` | `result, exec_observer.trace, agent, step,` |
| `harness/tools/browser_tools/capability.py:1195` | `agent.trace.append({` |
| `harness/tools/browser_tools/capability.py:1499` | `agent.trace.append({"type": "stale_axtree_target", "result": stale_target})` |
| `harness/tools/browser_tools/capability.py:1650` | `result, exec_observer.trace, agent, step,` |
| `harness/tools/browser_tools/capability.py:1744` | `agent.trace.append({` |
| `harness/agents/browser/agent.py:70` | `self.trace: List[JsonDict] = []` |
| `harness/agents/browser/agent.py:85` | `runtime, logger, self.trace, actor_type="browser",` |
| … | 另有 40 处 |

### `worker_trace_events` — 读取 worker trace 表（6 处）

| 位置 | 代码 |
|---|---|
| `harness/storage/virtual_fs.py:13` | `traces/<worker>.jsonl     -> worker_trace_events` |
| `harness/storage/virtual_fs.py:141` | `" FROM worker_trace_events WHERE task_id = ?"` |
| `harness/storage/virtual_fs.py:294` | `"SELECT trace_event_id, trace_json FROM worker_trace_events"` |
| `harness/storage/dao.py:623` | `"SELECT COALESCE(MAX(sequence_no), 0) FROM worker_trace_events"` |
| `harness/storage/dao.py:629` | `"INSERT INTO worker_trace_events("` |
| `harness/storage/dao.py:657` | `sql = "SELECT * FROM worker_trace_events WHERE task_id = ?"` |

### `on_event` — 事件回调（Console/transport）（26 处）

| 位置 | 代码 |
|---|---|
| `abcp_client.py:322` | `on_event: Optional[EventCallback] = None,` |
| `abcp_client.py:325` | `self.on_event = on_event` |
| `abcp_client.py:1107` | `if not self.on_event:` |
| `abcp_client.py:1109` | `event_types = getattr(self.on_event, "abcp_event_types", None)` |
| `abcp_client.py:1119` | `self.on_event(event_type, sanitize(payload, table))` |
| `main.py:1129` | `on_event=ConsoleProgressReporter(),` |
| `harness/utils.py:718` | `on_event: Optional[EventSink] = None,` |
| `harness/utils.py:737` | `self.on_event = on_event` |
| `harness/utils.py:800` | `self.on_event(event_type, payload) if self.on_event else None` |
| `harness/utils.py:1026` | `on_event.abcp_event_types = frozenset({"failure", "orphan_response"})` |
| `harness/utils.py:1027` | `return on_event` |
| `harness/skill_builder/browser.py:49` | `self.client = ABCPClient(config, on_event=record_transport)` |
| `harness/capabilities/bootstrap.py:53` | `async with ABCPClient(browser_config, on_event=event_logger) as browser:` |
| `harness/planning/fast_path.py:226` | `if (candidate := _successful_collection_event(event)) is not None` |
| `harness/agents/browser/loop.py:993` | `extension_event = (` |
| `harness/agents/browser/loop.py:998` | `self._write_agent_event(extension_event, {` |
| `harness/spawner/spawner_worker.py:372` | `slot.client.on_event = event_logger` |
| `harness/spawner/spawner_slots.py:1035` | `revision = _registration_event_catalog_revision(registration)` |
| `harness/spawner/spawner_slots.py:1166` | `client = _sp().ABCPClient(self.runtime.browser, on_event=event_logger)` |
| `harness/spawner/spawner_slots.py:1190` | `_registration_event_cursor(registration),` |
| `harness/spawner/spawner_slots.py:1308` | `client = _sp().ABCPClient(self.runtime.browser, on_event=event_logger)` |
| `harness/spawner/spawner_slots.py:1330` | `registration_cursor=_registration_event_cursor(registration),` |
| `harness/spawner/spawner_slots.py:1470` | `slot.client.on_event = slot.idle_event_logger` |
| `harness/spawner/spawner_slots.py:1505` | `_registration_event_cursor(registration),` |
| `harness/spawner/spawner_core.py:2205` | `client = _sp().ABCPClient(self.runtime.browser, on_event=make_browser_event_logger(` |
| `harness/fleet/runtime.py:1764` | `name, event_payload = _notification_event(message)` |

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
| `Connection.changed` ⚠两处不一致 | `connection` | `info` | `user` | ✅ | ✅ | 0 | *无* |
| `Download.progressed` | `blocking` | `info` | `both` | ✅ | ✅ | 3 | `harness/observation/event_observer.py:123`, `harness/tools/browser_tools/downloads.py:929` 等 3 处 |
| `Download.started` | `blocking` | `info` | `both` | ✅ | ✅ | 3 | `harness/observation/event_observer.py:122`, `harness/tools/browser_tools/downloads.py:928` 等 3 处 |
| `Download.stateChanged` | `blocking` | `info` | `both` | ✅ | ✅ | 6 | `devtools/download_path_live_canary.py:86`, `devtools/download_path_live_canary.py:106` 等 6 处 |
| `Download.waiting` | `blocking` | `info` | `both` | ✅ | ✅ | 3 | `harness/observation/event_observer.py:122`, `harness/tools/browser_tools/downloads.py:927` 等 3 处 |
| `File.chooserClosed` | `blocking` | `info` | `both` | ✅ | ✅ | 2 | `harness/observation/page_lifecycle.py:218`, `harness/workflow/workflow_policy.py:350` |
| `File.chooserOpened` | `blocking` | `blocking` | `both` | ✅ | ✅ | 0 | *无* |
| `File.operationCompleted` | `blocking` | `success` | `both` | ✅ | ✅ | 0 | *无* |
| `File.operationFailed` | `blocking` | `error` | `both` | ✅ | ✅ | 0 | *无* |
| `Fleet.ready` | `fleet` | `info` | `both` | ✅ | ✅ | 1 | `harness/spawner/spawner_slots.py:2126` |
| `Fleet.restartFailed` | `fleet` | `error` | `user` | — | ✅ | 0 | *无* |
| `Fleet.restartScheduled` | `fleet` | `info` | `user` | — | ✅ | 0 | *无* |
| `Fleet.stopped` | `fleet` | `error` | `both` | ✅ | ✅ | 2 | `harness/spawner/spawner_slots.py:2127`, `harness/spawner/spawner_slots.py:2286` |
| `Hitl.paused` | `hitl` | `blocking` | `both` | ✅ | ✅ | 5 | `harness/fleet/runtime.py:1118`, `harness/fleet/runtime.py:1771` 等 5 处 |
| `Hitl.resumed` | `hitl` | `info` | `both` | ✅ | ✅ | 2 | `harness/fleet/runtime.py:1771`, `harness/runtime/hitl.py:72` |
| `Page.certificateError` | `blocking` | `error` | `user` | — | ✅ | 0 | *无* |
| `Page.close` | `page` | `info` | `both` | ✅ | ✅ | 27 | `harness/fleet/runtime.py:1679`, `harness/fleet/runtime.py:1804` 等 27 处 |
| `Page.crashed` | `navigation` | `error` | `both` | ✅ | ✅ | 5 | `harness/fleet/runtime.py:1804`, `harness/observation/event_observer.py:114` 等 5 处 |
| `Page.dialogClosed` | `blocking` | `info` | `both` | ✅ | ✅ | 6 | `harness/observation/browser_reducers.py:164`, `harness/observation/browser_reducers.py:215` 等 6 处 |
| `Page.dialogOpened` | `blocking` | `blocking` | `both` | ✅ | ✅ | 3 | `harness/observation/browser_reducers.py:164`, `harness/observation/browser_reducers.py:255` 等 3 处 |
| `Page.loadFailed` | `navigation` | `error` | `both` | ✅ | ✅ | 5 | `harness/observation/page_lifecycle.py:187`, `harness/tools/browser_tools/navigate.py:853` 等 5 处 |
| `Page.loaded` | `navigation` | `info` | `both` | ✅ | ✅ | 6 | `harness/observation/page_lifecycle.py:183`, `harness/runtime/hitl.py:80` 等 6 处 |
| `Page.navigate` | `navigation` | `info` | `both` | ✅ | ✅ | 27 | `harness/fleet/runtime.py:1804`, `harness/observation/content_completeness.py:1252` 等 27 处 |
| `Page.navigationUpdated` | `page` | `info` | `user` | — | ✅ | 0 | *无* |
| `Page.open` | `page` | `info` | `both` | ✅ | ✅ | 4 | `harness/fleet/runtime.py:1798`, `harness/observation/event_observer.py:116` 等 4 处 |
| `Page.popupRequested` | `blocking` | `blocking` | `user` | — | ✅ | 0 | *无* |
| `Page.recovered` | `navigation` | `info` | `both` | ✅ | ✅ | 6 | `harness/observation/event_observer.py:114`, `harness/observation/page_lifecycle.py:206` 等 6 处 |
| `Page.startedLoading` | `navigation` | `info` | `both` | ✅ | ✅ | 1 | `harness/observation/page_lifecycle.py:175` |
| `Page.switchTo` | `page` | `info` | `both` | ✅ | ✅ | 8 | `harness/observation/content_completeness.py:1314`, `harness/observation/progress.py:34` 等 8 处 |
| `Page.titleUpdated` | `page` | `info` | `both` | ✅ | ✅ | 1 | `harness/runtime/hitl.py:76` |
| `System.agentBootstrap` | `system` | `info` | `internal` | — | ✅ | 0 | *无* |
| `Task.artifactAdded` ⚠两处不一致 | `task` | `info` | `user` | ✅ | ✅ | 0 | *无* |
| `Task.callChanged` | `task` | `info` | `user` | ✅ | ✅ | 0 | *无* |
| `Task.created` | `task` | `info` | `user` | — | ✅ | 0 | *无* |
| `Task.progressed` ⚠两处不一致 | `task` | `info` | `user` | ✅ | ✅ | 0 | *无* |
| `Task.statusChanged` ⚠两处不一致 | `task` | `info` | `user` | ✅ | ✅ | 0 | *无* |
| `Task.stepFinished` | `task` | `info` | `user` | — | ❌ | 0 | *无* |
| `Task.stepStarted` | `task` | `info` | `user` | — | ❌ | 0 | *无* |
| `Workflow.progress` | `system` | `info` | `both` | ✅ | ✅ | 2 | `harness/observation/exec_observer.py:191`, `harness/skill_builder/browser.py:41` |

## 8. 事件名与 Action 名同形（区分用）

同一个名字既是平台 Action（可以调用）又是平台事件（只能等待）。指南里的“事件名不是 Action”说的就是这一类：`引用点` 是字符串出现次数，两侧混计。

| 名字 | 事件目录 | Action 目录 | 引用点（两侧混计） | 位置 |
|---|---|---|---:|---|
| `Page.close` | ✅ | ✅ | 27 | `harness/fleet/runtime.py:1679`, `harness/fleet/runtime.py:1804` 等 27 处 |
| `Page.navigate` | ✅ | ✅ | 27 | `harness/fleet/runtime.py:1804`, `harness/observation/content_completeness.py:1252` 等 27 处 |
| `Page.switchTo` | ✅ | ✅ | 8 | `harness/observation/content_completeness.py:1314`, `harness/observation/progress.py:34` 等 8 处 |

## 9. 平台事件漂移与覆盖差

### 9.1 源码快照有、安装包没有（该运行时已移除）

引用点大于 0 的行是**死分支**：代码在等一个该运行时不会再发的事件。

| 事件 | Agent 可见 | 源码定义 | 引用点 | 位置 |
|---|---|---|---:|---|
| `Task.stepFinished` | — | `abcp-platform/packages/events/src/domains/Task/events.ts:12` | 0 | *无* |
| `Task.stepStarted` | — | `abcp-platform/packages/events/src/domains/Task/events.ts:11` | 0 | *无* |

### 9.2 安装包有、源码快照没有（本地快照落后）

| 事件 | category | severity | audience | Agent 可见 | 引用点 |
|---|---|---|---|---|---:|
| `Task.callChanged` | `task` | `info` | `user` | ✅ | 0 |

### 9.3 Agent 可见但 harness 没有任何引用（无人消费）

| 事件 | category | severity | 安装包 |
|---|---|---|---|
| `Connection.changed` | `connection` | `info` | ✅ |
| `File.chooserOpened` | `blocking` | `blocking` | ✅ |
| `File.operationCompleted` | `blocking` | `success` | ✅ |
| `File.operationFailed` | `blocking` | `error` | ✅ |
| `Task.artifactAdded` | `task` | `info` | ✅ |
| `Task.callChanged` | `task` | `info` | ✅ |
| `Task.progressed` | `task` | `info` | ✅ |
| `Task.statusChanged` | `task` | `info` | ✅ |

### 9.4 harness 里出现的非事件平台形名

形如 `Domain.name` 的字符串常量，但不在事件目录里：绝大多数是 Action 名（正常，调用侧）。`类别` 为 `⚠未知` 且 `安装包字符串` 为 `❌` 的行排在最前：代码引用的名字既不属于任何目录，已安装运行时包里也找不到（已删除的 Action或旧版事件名）。`前缀` 类是故意的前缀匹配常量，不是名字。

| 名字 | 类别 | 安装包字符串 | 出现次数 | 位置 |
|---|---|---|---:|---|
| `DOM.axTreeUpdated` | ⚠未知 | ❌ | 2 | `harness/observation/event_observer.py:127`, `harness/observation/event_observer.py:319` |
| `File.download` | ⚠未知 | ❌ | 5 | `harness/agents/browser/agent.py:788`, `harness/agents/browser/agent.py:821` 等 5 处 |
| `Page.wheel` | ⚠未知 | ❌ | 8 | `harness/observation/content_completeness.py:1373`, `harness/observation/loop_nudge.py:227` 等 8 处 |
| `Workflow.cancel` | ⚠未知 | ❌ | 4 | `harness/skill_builder/browser.py:20`, `harness/skill_builder/browser.py:66` 等 4 处 |
| `Bookmark.list` | Action | ❌ | 1 | `harness/runtime/model_support.py:106` |
| `DOM.get` | 前缀 | ❌ | 1 | `harness/runtime/model_support.py:102` |
| `DOM.getDetails` | Action | ❌ | 20 | `abcp_client.py:679`, `harness/agents/browser/review/task.py:41` 等 20 处 |
| `DOM.getImg` | Action | ❌ | 12 | `harness/agents/browser/agent.py:787`, `harness/agents/browser/agent.py:820` 等 12 处 |
| `DOM.inspectSelect` | Action | ❌ | 14 | `harness/constants.py:238`, `harness/diagnostics/error_classification.py:50` 等 14 处 |
| `Download.control` | Action | ❌ | 2 | `harness/agents/browser/agent.py:831`, `harness/agents/browser/agent.py:837` |
| `Fleet.close` | Action | ❌ | 6 | `devtools/download_path_live_canary.py:103`, `devtools/download_path_live_canary.py:185` 等 6 处 |
| `Fleet.create` | Action | ❌ | 6 | `devtools/download_path_live_canary.py:103`, `devtools/download_path_live_canary.py:110` 等 6 处 |
| `Fleet.list` | Action | ❌ | 5 | `harness/observation/progress.py:108`, `harness/spawner/spawner_core.py:2215` 等 5 处 |
| `History.list` | Action | ❌ | 1 | `harness/runtime/model_support.py:107` |
| `Hitl.requestPause` | Action | ❌ | 19 | `harness/diagnostics/__init__.py:111`, `harness/diagnostics/error_classification.py:923` 等 19 处 |
| `Hitl.resolvePause` | Action | ❌ | 6 | `harness/runtime/hitl.py:309`, `harness/runtime/hitl.py:313` 等 6 处 |
| `Input.click` | Action | ❌ | 26 | `harness/diagnostics/selector_audit.py:45`, `harness/diagnostics/selector_audit.py:53` 等 26 处 |
| `Input.drag` | Action | ❌ | 8 | `harness/diagnostics/selector_audit.py:49`, `harness/observation/loop_nudge.py:178` 等 8 处 |
| `Input.press` | Action | ❌ | 15 | `harness/diagnostics/selector_audit.py:46`, `harness/diagnostics/selector_audit.py:53` 等 15 处 |
| `Input.scroll` | Action | ❌ | 18 | `harness/observation/content_completeness.py:1373`, `harness/observation/loop_nudge.py:152` 等 18 处 |
| `Input.select` | Action | ❌ | 21 | `harness/diagnostics/error_classification.py:50`, `harness/diagnostics/error_classification.py:53` 等 21 处 |
| `Input.type` | Action | ❌ | 16 | `harness/diagnostics/selector_audit.py:48`, `harness/diagnostics/selector_audit.py:53` 等 16 处 |
| `Memory.delete` | Action | ❌ | 1 | `harness/tools/tool_policy.py:475` |
| `Memory.get` | Action | ❌ | 8 | `harness/agents/browser/agent.py:321`, `harness/agents/browser/agent.py:341` 等 8 处 |
| `Memory.list` | Action | ❌ | 1 | `harness/runtime/model_support.py:105` |
| `Memory.save` | Action | ❌ | 7 | `harness/agents/browser/agent.py:321`, `harness/agents/browser/agent.py:323` 等 7 处 |
| `Network.clearCache` | Action | ❌ | 1 | `harness/tools/tool_policy.py:481` |
| `Network.getCookies` | Action | ❌ | 1 | `harness/tools/tool_policy.py:482` |
| `Network.readApi` | Action | ❌ | 9 | `abcp_client.py:463`, `abcp_client.py:640` 等 9 处 |
| `Network.setCookies` | Action | ❌ | 1 | `harness/tools/tool_policy.py:483` |
| `Network.setInterception` | Action | ❌ | 1 | `harness/tools/tool_policy.py:484` |
| `Page.click` | Action | ❌ | 16 | `harness/fleet/runtime.py:578`, `harness/fleet/runtime.py:1701` 等 16 处 |
| `Page.create` | Action | ❌ | 41 | `devtools/download_path_live_canary.py:103`, `devtools/download_path_live_canary.py:112` 等 41 处 |
| `Page.go` | Action | ❌ | 18 | `harness/observation/content_completeness.py:1252`, `harness/observation/content_completeness.py:1260` 等 18 处 |
| `Page.handleDialog` | Action | ❌ | 4 | `harness/tools/browser_tools/axtree_state.py:47`, `harness/tools/browser_tools/capability.py:826` 等 4 处 |
| `Page.reload` | Action | ❌ | 12 | `harness/observation/content_completeness.py:1252`, `harness/observation/page_fingerprint.py:263` 等 12 处 |
| `Page.screenshot` | Action | ❌ | 18 | `harness/agents/browser/loop.py:794`, `harness/agents/browser/review/task.py:41` 等 18 处 |
| `Runtime.evaluate` | Action | ❌ | 20 | `harness/agents/browser/agent.py:1328`, `harness/constants.py:236` 等 20 处 |
| `System.describe` | 前缀 | ❌ | 1 | `harness/runtime/model_support.py:101` |
| `System.describeAction` | Action | ❌ | 9 | `devtools/download_path_live_canary.py:105`, `harness/capabilities/schema_loader.py:133` 等 9 处 |
| `System.describeEvent` | Action | ❌ | 4 | `devtools/download_path_live_canary.py:107`, `harness/observation/progress.py:37` 等 4 处 |
| `System.get` | 前缀 | ❌ | 1 | `harness/runtime/model_support.py:99` |
| `System.getCapabilities` | Action | ❌ | 12 | `devtools/download_path_live_canary.py:101`, `harness/capabilities/bootstrap.py:67` 等 12 处 |
| `System.list` | 前缀 | ❌ | 1 | `harness/runtime/model_support.py:100` |
| `System.notification` | 协议方法 | ❌ | 4 | `abcp_client.py:821`, `harness/fleet/runtime.py:599` 等 4 处 |
| `System.register` | Action | ❌ | 11 | `abcp_client.py:687`, `devtools/download_path_live_canary.py:97` 等 11 处 |
| `Workflow.execute` | Action | ❌ | 45 | `abcp_client.py:584`, `abcp_client.py:615` 等 45 处 |
| `Workflow.getGuide` | Action | ❌ | 2 | `harness/skill_builder/browser.py:19`, `harness/skill_builder/browser.py:111` |
| `Workflow.getStatus` | Action | ❌ | 2 | `harness/skill_builder/browser.py:20`, `harness/skill_builder/browser.py:180` |
| `Workflow.pause` | Action | ❌ | 2 | `harness/skill_builder/browser.py:20`, `harness/skill_builder/browser.py:180` |
| `Workflow.resume` | Action | ❌ | 2 | `harness/skill_builder/browser.py:20`, `harness/skill_builder/browser.py:180` |
| `DOM.getAXTree` | Action | ✅ | 75 | `abcp_client.py:679`, `harness/agents/browser/review/evidence.py:37` 等 75 处 |
| `Download.list` | Action | ✅ | 9 | `devtools/download_path_live_canary.py:104`, `devtools/download_path_live_canary.py:153` 等 9 处 |
| `Download.start` | Action | ✅ | 14 | `devtools/download_path_live_canary.py:104`, `devtools/download_path_live_canary.py:140` 等 14 处 |
| `File.handleChooser` | Action | ✅ | 9 | `harness/agents/browser/agent.py:789`, `harness/agents/browser/agent.py:822` 等 9 处 |
| `Page.getState` | Action | ✅ | 61 | `devtools/download_path_live_canary.py:103`, `devtools/download_path_live_canary.py:115` 等 61 处 |
| `Page.list` | Action | ✅ | 29 | `harness/fleet/runtime.py:1671`, `harness/fleet/runtime.py:1924` 等 29 处 |
| `System.listEvents` | Action | ✅ | 1 | `devtools/download_path_live_canary.py:102` |

