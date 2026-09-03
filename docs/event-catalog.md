<!-- GENERATED FILE - do not edit by hand.
     Regenerate: python3 devtools/event_catalog.py
     Verify:     python3 devtools/event_catalog.py --check -->

# 事件与 trace 目录（自动生成）

本文件由 `devtools/event_catalog.py` 从源码 AST 提取，用于重构前证明"哪些事件
真的被产生、哪些真的被消费"。手工维护的清单会在第二天过期，所以这里只放机械
可判定的事实；语义判断留在实施计划里。


## 1. 汇总

| 指标 | 数量 |
|---|---:|
| run event 产生点 | 375 |
| 不同 run event 类型 | 299 |
| 动态（非字面量）事件名 | 27 |
| trace 产生点 | 48 |
| 不同 trace type | 39 |

## 2. 事件命名空间分布

| 前缀 | 产生点 |
|---|---:|
| `spawner` | 54 |
| `browser` | 30 |
| `task_plan` | 25 |
| `lead` | 21 |
| `hitl` | 18 |
| `agent` | 17 |
| `event` | 17 |
| `skill` | 17 |
| `vl` | 17 |
| `schema` | 13 |
| `context` | 12 |
| `memory` | 11 |
| `auth_fleet` | 10 |
| `runtime` | 8 |
| `fast_path` | 7 |
| `axtree` | 6 |
| `progress` | 6 |
| `workflow` | 6 |
| `event_type` | 5 |
| `resume` | 5 |
| `tool` | 5 |
| `download` | 4 |
| `pacing` | 4 |
| `plan_validator` | 4 |
| `semantic_index` | 4 |
| `storage` | 4 |
| `batch_source` | 3 |
| `page` | 3 |
| `repair` | 3 |
| `task_phase` | 3 |
| `tool_result` | 3 |
| `collect_items` | 2 |
| `content_completeness` | 2 |
| `loop_guard` | 2 |
| `row_ledger` | 2 |
| `semantic_tree` | 2 |
| `strategy_attempts` | 2 |
| `task_state` | 2 |
| `{}` | 2 |
| `challenge` | 1 |
| `completion_receipt` | 1 |
| `dismiss_overlay` | 1 |
| `event_name` | 1 |
| `event_observer` | 1 |
| `extension_event` | 1 |
| `fleet_click_gate` | 1 |
| `harness` | 1 |
| `loop_nudge` | 1 |
| `microloop` | 1 |
| `page_stats` | 1 |
| `semantic_terminal` | 1 |
| `snapshot_diff` | 1 |
| `tool_batch` | 1 |

## 3. 产生点最多的文件

| 文件 | run event 产生点 |
|---|---:|
| `agent_harness.py` | 89 |
| `harness/tools/browser_tools/capability.py` | 39 |
| `harness/spawner/spawner_slots.py` | 30 |
| `harness/spawner/spawner_core.py` | 24 |
| `harness/spawner/spawner_worker.py` | 16 |
| `harness/tools/lead_tools.py` | 16 |
| `harness/tools/browser_tools/visual.py` | 12 |
| `harness/hitl.py` | 11 |
| `harness/tools/browser_tools/hitl.py` | 11 |
| `harness/skill/contract.py` | 8 |
| `harness/tools/browser_tools/captcha_autosolve.py` | 8 |
| `harness/task_control/replan.py` | 7 |
| `harness/tools/browser_tools/axtree_state.py` | 7 |
| `harness/tools/browser_tools/bindings.py` | 7 |
| `harness/tools/browser_tools/dispatch.py` | 7 |
| `harness/tools/browser_tools/navigate.py` | 6 |
| `harness/tools/browser_tools/progress_obs.py` | 6 |
| `harness/compaction.py` | 5 |
| `harness/spawner/spawner_registry.py` | 5 |
| `main.py` | 5 |

## 4. run event 类型全表

| 事件类型 | 产生点 | 位置 |
|---|---:|---|
| `agent.cancelled` | 1 | `agent_harness.py:1835` |
| `agent.error` | 1 | `agent_harness.py:1862` |
| `agent.final` | 1 | `agent_harness.py:3085` |
| `agent.interrupted` | 1 | `agent_harness.py:1936` |
| `agent.model` | 1 | `agent_harness.py:1373` |
| `agent.model_connection_error` | 1 | `agent_harness.py:1261` |
| `agent.model_degenerate_response` | 1 | `agent_harness.py:1240` |
| `agent.model_input_moderation_refused` | 1 | `agent_harness.py:1320` |
| `agent.model_protocol_error` | 1 | `agent_harness.py:1295` |
| `agent.model_rate_limited` | 1 | `agent_harness.py:1842` |
| `agent.model_timeout` | 1 | `agent_harness.py:1280` |
| `agent.step.start` | 1 | `agent_harness.py:1210` |
| `agent.step_cap.reminder` | 1 | `agent_harness.py:2771` |
| `agent.step_extension.denied` | 1 | `agent_harness.py:2902` |
| `agent.step_extension.granted` | 1 | `agent_harness.py:2938` |
| `agent.step_extension.requested` | 1 | `agent_harness.py:2847` |
| `agent.truncated_response` | 1 | `agent_harness.py:1463` |
| `auth_fleet.ledger_handler_failed` | 1 | `harness/tools/browser_tools/hitl.py:1547` |
| `auth_fleet.lost_handler_failed` | 1 | `harness/tools/browser_tools/page_create.py:142` |
| `auth_fleet.operator_reset` | 1 | `harness/spawner/spawner_slots.py:2349` |
| `auth_fleet.reconciled` | 1 | `harness/spawner/spawner_slots.py:1262` |
| `auth_fleet.resolver_claimed_for_page_create` | 1 | `harness/tools/browser_tools/bindings.py:869` |
| `auth_fleet.resolver_relinquished` | 1 | `harness/tools/browser_tools/bindings.py:913` |
| `auth_fleet.resolver_relinquished_after_page_create` | 1 | `harness/tools/browser_tools/bindings.py:943` |
| `auth_fleet.session_release_conflict` | 1 | `harness/spawner/spawner_core.py:1901` |
| `auth_fleet.session_released` | 1 | `harness/spawner/spawner_slots.py:2250` |
| `auth_fleet.verified_record` | 1 | `harness/spawner/spawner_slots.py:2220` |
| `axtree.invalidated` | 1 | `harness/tools/browser_tools/axtree_state.py:936` |
| `axtree.invalidation_superseded_by_event` | 1 | `harness/tools/browser_tools/axtree_state.py:637` |
| `axtree.parse_inconsistent` | 1 | `harness/tools/browser_tools/axtree_state.py:552` |
| `axtree.rematch_observed` | 1 | `harness/tools/browser_tools/axtree_state.py:892` |
| `axtree.snapshot` | 1 | `harness/tools/browser_tools/axtree_state.py:579` |
| `axtree.target_resolution_fallback` | 1 | `harness/tools/browser_tools/axtree_state.py:842` |
| `batch_source.derived` | 1 | `harness/tools/lead_tools.py:1948` |
| `batch_source.materialized` | 1 | `harness/task_control/cohorts.py:599` |
| `batch_source.not_derived` | 1 | `harness/tools/lead_tools.py:1961` |
| `browser.bootstrap` | 1 | `agent_harness.py:2028` |
| `browser.call.arguments_prepared` | 1 | `harness/tools/browser_tools/capability.py:455` |
| `browser.call.captcha_auto_solved` | 1 | `harness/tools/browser_tools/capability.py:504` |
| `browser.call.contract_violation` | 1 | `harness/tools/browser_tools/capability.py:317` |
| `browser.call.cross_task_memory_rejected` | 1 | `harness/tools/browser_tools/capability.py:328` |
| `browser.call.dialog_rejected` | 1 | `harness/tools/browser_tools/capability.py:403` |
| `browser.call.dom_get_img_output_normalized` | 1 | `harness/tools/browser_tools/validation.py:555` |
| `browser.call.fleet_auth_gated` | 3 | `harness/tools/browser_tools/capability.py:372`, `harness/tools/browser_tools/capability.py:488`, `harness/tools/browser_tools/capability.py:516` |
| `browser.call.fleet_binding_rejected` | 1 | `harness/tools/browser_tools/capability.py:343` |
| `browser.call.internal` | 1 | `harness/tools/browser_tools/capability.py:1320` |
| `browser.call.lifecycle_gated` | 1 | `harness/tools/browser_tools/capability.py:386` |
| `browser.call.navigation_context_rejected` | 1 | `harness/tools/browser_tools/capability.py:232` |
| `browser.call.page_binding_rejected` | 1 | `harness/tools/browser_tools/capability.py:356` |
| `browser.call.params_error` | 2 | `harness/tools/browser_tools/capability.py:173`, `harness/tools/browser_tools/capability.py:413` |
| `browser.call.purpose_added` | 1 | `harness/tools/browser_tools/capability.py:446` |
| `browser.call.rejected` | 1 | `harness/tools/browser_tools/capability.py:192` |
| `browser.call.result` | 2 | `harness/tools/browser_tools/capability.py:1042`, `harness/tools/browser_tools/capability.py:1351` |
| `browser.call.schema_rejected` | 1 | `harness/tools/browser_tools/capability.py:469` |
| `browser.call.screenshot_output_normalized` | 2 | `harness/tools/browser_tools/capability.py:244`, `harness/tools/browser_tools/capability.py:1107` |
| `browser.call.screenshot_rejected` | 1 | `harness/tools/browser_tools/capability.py:396` |
| `browser.call.select_replay_blocked` | 1 | `harness/tools/browser_tools/navigate.py:2262` |
| `browser.call.stale_axtree_target` | 2 | `harness/tools/browser_tools/capability.py:431`, `harness/tools/browser_tools/capability.py:1145` |
| `browser.tool.routing_rejected` | 1 | `harness/tools/browser_tools/dispatch.py:793` |
| `browser.transport.fatal` | 1 | `harness/tools/browser_tools/capability.py:811` |
| `challenge.navigation_cleared` | 1 | `harness/tools/browser_tools/navigate.py:1118` |
| `collect_items.result` | 2 | `harness/tools/browser_tools/composites/collect_items.py:766`, `harness/tools/browser_tools/composites/collect_items.py:1280` |
| `completion_receipt.persisted` | 1 | `harness/results/completion_receipt.py:600` |
| `content_completeness.artifact_region_credit` | 1 | `harness/tools/browser_tools/record_extraction.py:164` |
| `content_completeness.observed` | 1 | `harness/tools/browser_tools/navigate.py:992` |
| `context.compacted` | 1 | `harness/compaction.py:670` |
| `context.compaction_requested` | 4 | `agent_harness.py:2795`, `agent_harness.py:5139`, `agent_harness.py:5191`, `agent_harness.py:5748` |
| `context.compaction_skipped` | 4 | `harness/compaction.py:498`, `harness/compaction.py:523`, `harness/compaction.py:602`, `harness/compaction.py:653` |
| `context.snapshot.failed` | 2 | `agent_harness.py:1931`, `agent_harness.py:5635` |
| `context.snapshot.saved` | 1 | `harness/utils.py:902` |
| `dismiss_overlay.result` | 1 | `harness/tools/browser_tools/visual.py:58` |
| `download.event_dropped` | 1 | `harness/tools/browser_tools/downloads.py:884` |
| `download.event_observed` | 1 | `harness/tools/browser_tools/downloads.py:906` |
| `download.operation_reused` | 1 | `harness/tools/browser_tools/capability.py:561` |
| `download.timeout_reconciled` | 1 | `harness/tools/browser_tools/capability.py:598` |
| `event` ⚠动态 | 17 | `agent_harness.py:2121`, `agent_harness.py:3613`, `harness/fleet/runtime.py:1296`, `harness/observation/event_observer.py:331` 等 17 处 |
| `event_name` ⚠动态 | 1 | `harness/tools/browser_tools/navigate.py:964` |
| `event_observer.error` | 1 | `harness/observation/event_observer.py:132` |
| `event_type` ⚠动态 | 5 | `agent_harness.py:1126`, `harness/evidence/extraction_artifacts.py:121`, `harness/tools/browser_tools/visual.py:612`, `harness/utils.py:739` 等 5 处 |
| `extension_event` ⚠动态 | 1 | `agent_harness.py:1805` |
| `fast_path.replan_checkpoint` | 1 | `harness/task_control/replan.py:520` |
| `fast_path.replan_checkpoint_business_contract_unavailable` | 1 | `harness/task_control/replan.py:629` |
| `fast_path.replan_checkpoint_contract_degraded` | 1 | `harness/task_control/replan.py:336` |
| `fast_path.replan_checkpoint_invalidated` | 1 | `harness/task_control/replan.py:183` |
| `fast_path.replan_checkpoint_predecessor_mismatch` | 2 | `harness/task_control/replan.py:293`, `harness/task_control/replan.py:309` |
| `fast_path.replan_checkpoint_progress_mismatch` | 1 | `harness/task_control/replan.py:387` |
| `fleet_click_gate.disabled` | 1 | `harness/spawner/spawner_core.py:177` |
| `harness.config` | 1 | `agent_harness.py:4835` |
| `hitl.auto_request_pause` | 1 | `harness/tools/browser_tools/hitl.py:1251` |
| `hitl.pause_snapshot.captured` | 1 | `harness/tools/browser_tools/hitl.py:1369` |
| `hitl.pause_snapshot.failed` | 1 | `harness/tools/browser_tools/hitl.py:1343` |
| `hitl.post_resume.confirmation_input_failed` | 1 | `harness/tools/browser_tools/hitl.py:523` |
| `hitl.post_resume.confirmation_non_tty` | 1 | `harness/tools/browser_tools/hitl.py:506` |
| `hitl.post_resume.raw_call` | 1 | `harness/tools/browser_tools/hitl.py:695` |
| `hitl.refused` | 1 | `harness/tools/browser_tools/hitl.py:1117` |
| `hitl.wait.branch_error` | 1 | `harness/hitl.py:710` |
| `hitl.wait.page_settled_after_hitl` | 2 | `harness/hitl.py:795`, `harness/hitl.py:989` |
| `hitl.wait.resumed` | 2 | `harness/hitl.py:810`, `harness/hitl.py:1005` |
| `hitl.wait.settlement_check` | 2 | `harness/hitl.py:829`, `harness/hitl.py:920` |
| `hitl.wait.stale_pause_deadlock` | 2 | `harness/hitl.py:768`, `harness/hitl.py:960` |
| `hitl.wait.start` | 1 | `harness/hitl.py:640` |
| `hitl.wait.timeout` | 1 | `harness/hitl.py:734` |
| `lead.artifact_supersession` | 1 | `harness/tools/lead_tools.py:2956` |
| `lead.cancelled` | 1 | `agent_harness.py:5528` |
| `lead.completion_receipt` | 2 | `agent_harness.py:5593`, `harness/tools/lead_tools.py:3135` |
| `lead.completion_receipt_failed` | 1 | `agent_harness.py:5604` |
| `lead.empty_model_response` | 1 | `agent_harness.py:5328` |
| `lead.error` | 1 | `agent_harness.py:5554` |
| `lead.final` | 1 | `agent_harness.py:5658` |
| `lead.interrupted` | 1 | `agent_harness.py:5641` |
| `lead.model` | 1 | `agent_harness.py:5278` |
| `lead.model_connection_error` | 1 | `agent_harness.py:5165` |
| `lead.model_degenerate_response` | 1 | `agent_harness.py:5079` |
| `lead.model_protocol_error` | 1 | `agent_harness.py:5212` |
| `lead.model_rate_limited` | 1 | `agent_harness.py:5542` |
| `lead.model_timeout` | 1 | `agent_harness.py:5113` |
| `lead.numeric_reconciliation` | 1 | `harness/tools/lead_tools.py:3203` |
| `lead.step.start` | 1 | `agent_harness.py:5033` |
| `lead.step_cap.reminder` | 1 | `agent_harness.py:5724` |
| `lead.tool.error` | 1 | `harness/tools/lead_tools.py:1477` |
| `lead.tool.params_error` | 1 | `harness/tools/parsers.py:104` |
| `lead.tool.result` | 1 | `agent_harness.py:5438` |
| `loop_guard.observed` | 1 | `harness/tools/loop_guard.py:139` |
| `loop_guard.spend_limit` | 1 | `harness/tools/loop_guard.py:111` |
| `loop_nudge.detected` | 1 | `agent_harness.py:1695` |
| `memory.bootstrap` | 1 | `agent_harness.py:2104` |
| `memory.bootstrap.foreign_context` | 1 | `agent_harness.py:2082` |
| `memory.bootstrap.get_failed` | 1 | `agent_harness.py:2071` |
| `memory.bootstrap.skipped` | 1 | `agent_harness.py:2061` |
| `memory.bootstrap.unsupported_contract` | 1 | `agent_harness.py:2056` |
| `memory.heartbeat` | 1 | `agent_harness.py:2188` |
| `memory.heartbeat.failed` | 1 | `agent_harness.py:2198` |
| `memory.heartbeat.stop_failed` | 1 | `agent_harness.py:2215` |
| `memory.terminal_checkpoint.failed` | 3 | `harness/spawner/spawner_worker.py:360`, `harness/spawner/spawner_worker.py:374`, `harness/spawner/spawner_worker.py:384` |
| `microloop.telemetry` | 1 | `harness/tools/browser_tools/auto_intercept.py:51` |
| `pacing.phase.wait_completed` | 1 | `harness/spawner/spawner_core.py:1415` |
| `pacing.phase.wait_started` | 1 | `harness/spawner/spawner_core.py:1413` |
| `pacing.row.wait_completed` | 1 | `harness/pacing.py:105` |
| `pacing.row.wait_started` | 1 | `harness/pacing.py:102` |
| `page.lifecycle.after_action` | 1 | `harness/tools/browser_tools/dispatch.py:316` |
| `page.lifecycle.settlement_wait` | 1 | `harness/tools/browser_tools/dispatch.py:195` |
| `page.lifecycle.timeout_resync` | 1 | `harness/tools/browser_tools/dispatch.py:215` |
| `page_stats.detected` | 1 | `agent_harness.py:1672` |
| `plan_validator.error_deduplicated` | 1 | `agent_harness.py:3567` |
| `plan_validator.mechanical_invalid` | 2 | `agent_harness.py:3423`, `agent_harness.py:3496` |
| `plan_validator.operational_continuation` | 1 | `agent_harness.py:3531` |
| `progress.history_navigation_unverified` | 1 | `harness/tools/browser_tools/navigate.py:1211` |
| `progress.mandatory_recovery_credit_used` | 1 | `harness/tools/browser_tools/progress_obs.py:358` |
| `progress.observed` | 1 | `harness/tools/browser_tools/progress_obs.py:405` |
| `progress.repair_advanced` | 1 | `harness/tools/browser_tools/progress_obs.py:438` |
| `progress.snapshot` | 1 | `harness/tools/browser_tools/progress_obs.py:446` |
| `progress.unrecorded_rows_observed` | 1 | `harness/tools/browser_tools/progress_obs.py:137` |
| `repair.visual_evidence_abandoned` | 1 | `harness/tools/browser_tools/record_extraction.py:316` |
| `repair.visual_evidence_satisfied` | 1 | `harness/tools/browser_tools/visual.py:349` |
| `repair.visual_page_rejected` | 1 | `harness/tools/browser_tools/visual.py:424` |
| `resume.instruction.audit_failed` | 1 | `harness/tools/lead_tools.py:1728` |
| `resume.instruction.reviewed` | 2 | `agent_harness.py:4283`, `harness/tools/lead_tools.py:1736` |
| `resume.projection_built` | 1 | `main.py:2297` |
| `resume.started` | 1 | `main.py:2279` |
| `row_ledger.error` | 1 | `harness/spawner/spawner_worker.py:236` |
| `row_ledger.recorded` | 1 | `harness/spawner/spawner_worker.py:241` |
| `runtime.evaluate.batch_boundary_rejected` | 1 | `agent_harness.py:1607` |
| `runtime.evaluate.escalation_authorized` | 1 | `harness/tools/browser_tools/capability.py:276` |
| `runtime.evaluate.escalation_rejected` | 1 | `harness/tools/browser_tools/capability.py:267` |
| `runtime.evaluate.main_fallback_authorized` | 1 | `harness/tools/browser_tools/capability.py:664` |
| `runtime.evaluate.rejected` | 2 | `harness/tools/browser_tools/capability.py:258`, `harness/tools/browser_tools/capability.py:1125` |
| `runtime.evaluate.trusted_collection_template` | 1 | `harness/tools/browser_tools/runtime_eval.py:82` |
| `runtime.evaluate.world_evidence_degraded` | 1 | `harness/tools/browser_tools/capability.py:707` |
| `schema.bootstrap.cached` | 3 | `agent_harness.py:4547`, `agent_harness.py:4567`, `agent_harness.py:4593` |
| `schema.bootstrap.done` | 1 | `agent_harness.py:4650` |
| `schema.bootstrap.failed` | 3 | `agent_harness.py:4500`, `agent_harness.py:4629`, `agent_harness.py:4668` |
| `schema.bootstrap.lock_timeout` | 1 | `agent_harness.py:4579` |
| `schema.bootstrap.timing` | 1 | `agent_harness.py:4677` |
| `schema.bundle.loaded` | 1 | `harness/schema_loader.py:171` |
| `schema.bundle.reused` | 1 | `harness/spawner/spawner_worker.py:1295` |
| `schema.describeAction.error` | 1 | `harness/schema_loader.py:136` |
| `schema.describeAction.stale_catalog` | 1 | `harness/schema_loader.py:146` |
| `semantic_index.error` | 1 | `harness/observation/semantic_index.py:221` |
| `semantic_index.frame_graph` | 1 | `harness/observation/semantic_index.py:229` |
| `semantic_index.selector_candidates` | 1 | `harness/observation/semantic_index.py:262` |
| `semantic_index.subtree` | 1 | `harness/observation/semantic_index.py:287` |
| `semantic_terminal.counterevidence` | 1 | `harness/spawner/spawner_worker.py:853` |
| `semantic_tree.diagnostic_bypass` | 1 | `harness/tools/browser_tools/progress_obs.py:367` |
| `semantic_tree.shadow_dom_defaulted` | 1 | `harness/tools/browser_tools/capability.py:209` |
| `skill.autoheal.error` | 1 | `harness/spawner/spawner_worker.py:307` |
| `skill.context.error` | 1 | `harness/spawner/spawner_worker.py:502` |
| `skill.contract.enrich_error` | 1 | `harness/skill/contract.py:445` |
| `skill.contract.enriched` | 1 | `harness/skill/contract.py:438` |
| `skill.fast_path.error` | 1 | `harness/spawner/spawner_worker.py:171` |
| `skill.fast_path.repair_fallback` | 1 | `harness/tools/browser_tools/record_extraction.py:314` |
| `skill.forced` | 1 | `harness/skill/contract.py:348` |
| `skill.forced.ranked` | 1 | `harness/skill/contract.py:331` |
| `skill.forced.unknown` | 1 | `harness/skill/contract.py:297` |
| `skill.forced.{}` ⚠动态 | 1 | `harness/skill/contract.py:318` |
| `skill.guidance.recorded` | 1 | `harness/skill/guidance.py:545` |
| `skill.guidance.signal_error` | 1 | `harness/spawner/spawner_worker.py:336` |
| `skill.guidance.stage_mismatch` | 1 | `harness/skill/guidance.py:526` |
| `skill.registry.load_failed` | 1 | `harness/spawner/spawner_worker.py:127` |
| `skill.selected_workflow.executed` | 1 | `harness/tools/browser_tools/dispatch.py:1135` |
| `skill.selection.error` | 1 | `harness/skill/contract.py:581` |
| `skill.selection.required` | 1 | `harness/skill/contract.py:568` |
| `snapshot_diff.detected` | 1 | `agent_harness.py:1680` |
| `spawner.browser.failure_cleanup.cancel_suppressed` | 1 | `harness/spawner/spawner_worker.py:411` |
| `spawner.browser.failure_cleanup.failed` | 1 | `harness/spawner/spawner_worker.py:417` |
| `spawner.browser.result` | 2 | `harness/spawner/spawner_worker.py:1055`, `harness/spawner/spawner_worker.py:1260` |
| `spawner.browser.spawn` | 1 | `harness/spawner/spawner_core.py:2018` |
| `spawner.browser.start_rejected` | 2 | `harness/spawner/spawner_core.py:1392`, `harness/spawner/spawner_core.py:1425` |
| `spawner.browser_context.persist_failed` | 2 | `harness/spawner/spawner_core.py:1111`, `harness/spawner/spawner_registry.py:814` |
| `spawner.browser_context.persist_skipped` | 2 | `harness/spawner/spawner_core.py:808`, `harness/spawner/spawner_core.py:819` |
| `spawner.browser_context.persisted` | 1 | `harness/spawner/spawner_core.py:985` |
| `spawner.fleet.assigned` | 1 | `harness/spawner/spawner_slots.py:1796` |
| `spawner.fleet.assignment_rejected` | 1 | `harness/spawner/spawner_core.py:1915` |
| `spawner.fleet.cap_blocked` | 1 | `harness/spawner/spawner_slots.py:254` |
| `spawner.fleet.cap_released` | 1 | `harness/spawner/spawner_slots.py:244` |
| `spawner.fleet.cap_reuse` | 1 | `harness/spawner/spawner_slots.py:290` |
| `spawner.fleet.inventory_retired` | 1 | `harness/spawner/spawner_slots.py:237` |
| `spawner.fleet.notification_relay_attached` | 1 | `harness/spawner/spawner_slots.py:2166` |
| `spawner.fleet.readiness_failed` | 1 | `harness/spawner/spawner_slots.py:1982` |
| `spawner.fleet.readiness_ready` | 1 | `harness/spawner/spawner_slots.py:1929` |
| `spawner.fleet.readiness_started` | 1 | `harness/spawner/spawner_slots.py:1854` |
| `spawner.fleet.worker_isolation_applied` | 1 | `harness/spawner/spawner_slots.py:105` |
| `spawner.fleet.worker_isolation_skipped` | 1 | `harness/spawner/spawner_slots.py:94` |
| `spawner.resume_browser_hint.ignored` | 3 | `harness/spawner/spawner_core.py:773`, `harness/spawner/spawner_slots.py:1552`, `harness/spawner/spawner_slots.py:1579` |
| `spawner.resume_browser_hint.page_probe_failed` | 1 | `harness/spawner/spawner_core.py:1629` |
| `spawner.resume_browser_hint.used` | 1 | `harness/spawner/spawner_slots.py:1561` |
| `spawner.similar_task_reuse.matched` | 1 | `harness/spawner/spawner_slots.py:1695` |
| `spawner.similar_task_reuse.miss` | 1 | `harness/spawner/spawner_slots.py:1619` |
| `spawner.similar_task_reuse.readiness_fallback` | 1 | `harness/spawner/spawner_slots.py:1672` |
| `spawner.similar_task_reuse.rejected` | 1 | `harness/spawner/spawner_slots.py:1683` |
| `spawner.slot.acquire_exhausted` | 1 | `harness/spawner/spawner_core.py:1451` |
| `spawner.slot.acquire_failed` | 1 | `harness/spawner/spawner_core.py:1958` |
| `spawner.slot.bootstrap_timing` | 1 | `harness/spawner/spawner_slots.py:938` |
| `spawner.slot.created` | 1 | `harness/spawner/spawner_slots.py:955` |
| `spawner.slot.page_quarantine_cleared` | 1 | `harness/spawner/spawner_registry.py:629` |
| `spawner.slot.page_quarantine_retired` | 1 | `harness/spawner/spawner_registry.py:588` |
| `spawner.slot.page_quarantined` | 1 | `harness/spawner/spawner_registry.py:502` |
| `spawner.slot.recovered` | 1 | `harness/spawner/spawner_slots.py:1074` |
| `spawner.slot.recovery_deferred` | 1 | `harness/spawner/spawner_slots.py:993` |
| `spawner.slot.recovery_failed` | 1 | `harness/spawner/spawner_slots.py:1051` |
| `spawner.slot.reserved` | 1 | `harness/spawner/spawner_slots.py:866` |
| `spawner.slot.retired` | 1 | `harness/spawner/spawner_slots.py:1105` |
| `spawner.slot.start_cancelled` | 1 | `harness/spawner/spawner_core.py:1864` |
| `spawner.slot.sync_warning` | 1 | `harness/spawner/spawner_registry.py:265` |
| `spawner.task_session_binding.candidate_recorded` | 1 | `harness/spawner/spawner_core.py:601` |
| `spawner.task_session_binding.expired` | 1 | `harness/spawner/spawner_core.py:273` |
| `spawner.task_session_binding.handler_failed` | 1 | `harness/tools/browser_tools/hitl.py:1589` |
| `spawner.task_session_binding.load_ambiguous` | 1 | `harness/spawner/spawner_core.py:349` |
| `spawner.task_session_binding.load_failed` | 1 | `harness/spawner/spawner_core.py:294` |
| `spawner.task_session_binding.operator_reset` | 1 | `harness/spawner/spawner_core.py:555` |
| `spawner.task_session_binding.write_failed` | 1 | `harness/spawner/spawner_core.py:617` |
| `storage.download_registration_failed` | 1 | `harness/tools/browser_tools/downloads.py:276` |
| `storage.dual_verify` | 1 | `main.py:1592` |
| `storage.external_file_unregistered` | 1 | `agent_harness.py:2686` |
| `storage.revision_conflict` | 1 | `main.py:1589` |
| `strategy_attempts.appended` | 1 | `harness/strategy_telemetry.py:83` |
| `strategy_attempts.write_failed` | 1 | `harness/strategy_telemetry.py:78` |
| `task_phase.blocked_by_dependency` | 2 | `harness/task_control/phase_lifecycle.py:1112`, `harness/task_control/phase_lifecycle.py:1238` |
| `task_phase.exhausted` | 1 | `harness/task_control/phase_lifecycle.py:1208` |
| `task_plan.accepted` | 2 | `harness/task_control/plan_validation.py:1985`, `harness/task_control/plan_validation.py:2063` |
| `task_plan.accepted_with_warnings` | 1 | `agent_harness.py:4178` |
| `task_plan.rejected` | 18 | `agent_harness.py:3796`, `agent_harness.py:3814`, `agent_harness.py:3872`, `agent_harness.py:3903` 等 18 处 |
| `task_plan.review_unavailable` | 1 | `agent_harness.py:4006` |
| `task_plan.validate.degraded` | 1 | `agent_harness.py:3832` |
| `task_plan.validate.warning` | 1 | `agent_harness.py:3823` |
| `task_plan.versioned` | 1 | `harness/task_control/plan_validation.py:2066` |
| `task_state.initialized` | 1 | `harness/task_control/plan_validation.py:2206` |
| `task_state.resume_prepared` | 1 | `harness/task_control/phase_lifecycle.py:852` |
| `tool.direct_capability_wrapped` | 1 | `harness/tools/browser_tools/capability.py:118` |
| `tool.error` | 2 | `harness/tools/browser_tools/capability.py:133`, `harness/tools/browser_tools/dispatch.py:782` |
| `tool.final_answer` | 1 | `harness/tools/browser_tools/dispatch.py:1274` |
| `tool.record_extraction.rejected` | 1 | `harness/tools/browser_tools/record_extraction.py:105` |
| `tool_batch.deferred` | 1 | `agent_harness.py:1748` |
| `tool_result.model_visible` | 1 | `agent_harness.py:690` |
| `tool_result.offloaded` | 1 | `harness/offload.py:555` |
| `tool_result.preserve_failed` | 1 | `harness/offload.py:429` |
| `vl.captcha_autosolve.failed` | 1 | `harness/tools/browser_tools/hitl.py:765` |
| `vl.captcha_autosolve.result` | 1 | `harness/tools/browser_tools/captcha_autosolve.py:887` |
| `vl.captcha_autosolve.screenshot_attempt_failed` | 1 | `harness/tools/browser_tools/captcha_autosolve.py:237` |
| `vl.captcha_autosolve.screenshot_retry_recovered` | 1 | `harness/tools/browser_tools/captcha_autosolve.py:230` |
| `vl.captcha_autosolve.viewport_exhausted` | 1 | `harness/tools/browser_tools/captcha_autosolve.py:434` |
| `vl.captcha_autosolve.viewport_fallback` | 1 | `harness/tools/browser_tools/captcha_autosolve.py:424` |
| `vl.captcha_screenshot.retain_failed` | 1 | `harness/tools/browser_tools/captcha_autosolve.py:280` |
| `vl.captcha_screenshot.retained` | 1 | `harness/tools/browser_tools/captcha_autosolve.py:287` |
| `vl.captcha_solve` | 1 | `harness/tools/browser_tools/captcha_autosolve.py:775` |
| `vl.locate.promotion` | 1 | `harness/tools/browser_tools/visual.py:1540` |
| `vl.locate.promotion_error` | 1 | `harness/tools/browser_tools/visual.py:1606` |
| `vl.reality_check` | 1 | `harness/tools/browser_tools/visual.py:1291` |
| `vl.reality_check.capture_unavailable` | 1 | `harness/tools/browser_tools/visual.py:1184` |
| `vl.reality_check.error` | 1 | `harness/tools/browser_tools/visual.py:1304` |
| `vl.reality_check.persist_failed` | 1 | `harness/tools/browser_tools/visual.py:1264` |
| `vl.visual_recovery_hint` | 1 | `harness/tools/browser_tools/visual.py:798` |
| `vl.visual_verify` | 1 | `harness/tools/browser_tools/visual.py:618` |
| `workflow.auth_fence.before` | 1 | `harness/tools/browser_tools/bindings.py:624` |
| `workflow.auth_generation_changed` | 2 | `harness/tools/browser_tools/bindings.py:629`, `harness/tools/browser_tools/bindings.py:739` |
| `workflow.execute.rejected` | 1 | `harness/tools/browser_tools/capability.py:308` |
| `workflow.execute.runtime_disabled` | 1 | `harness/tools/browser_tools/capability.py:289` |
| `workflow.row_quarantined` | 1 | `harness/tools/browser_tools/bindings.py:740` |
| `{}.model_input_moderation_folded` ⚠动态 | 1 | `agent_harness.py:458` |
| `{}.{}` ⚠动态 | 1 | `harness/utils.py:787` |

## 5. trace type 全表

| trace type | 产生点 | 位置 |
|---|---:|---|
| `browser_call` | 2 | `harness/tools/browser_tools/capability.py:1054`, `harness/tools/browser_tools/capability.py:1356` |
| `browser_call_params_error` | 2 | `harness/tools/browser_tools/capability.py:182`, `harness/tools/browser_tools/capability.py:419` |
| `browser_call_rejected` | 1 | `harness/tools/browser_tools/capability.py:198` |
| `browser_call_schema_rejected` | 1 | `harness/tools/browser_tools/capability.py:477` |
| `captcha_auto_solved` | 1 | `harness/tools/browser_tools/capability.py:505` |
| `contract_violation` | 2 | `harness/tools/browser_tools/capability.py:323`, `harness/tools/browser_tools/dispatch.py:805` |
| `cross_task_memory_guard` | 1 | `harness/tools/browser_tools/capability.py:331` |
| `dialog_guard` | 1 | `harness/tools/browser_tools/capability.py:404` |
| `execute_selected_skill` | 1 | `harness/tools/browser_tools/dispatch.py:1138` |
| `final_answer` | 1 | `harness/tools/browser_tools/dispatch.py:1275` |
| `fleet_auth_gate` | 3 | `harness/tools/browser_tools/capability.py:373`, `harness/tools/browser_tools/capability.py:489`, `harness/tools/browser_tools/capability.py:517` |
| `fleet_binding_guard` | 1 | `harness/tools/browser_tools/capability.py:344` |
| `lead_tool_after_call_exception` | 1 | `harness/tools/lead_tools.py:1460` |
| `lead_tool_arguments_rejected` | 1 | `harness/tools/lead_tools.py:1428` |
| `lead_tool_exception` | 1 | `harness/tools/lead_tools.py:1441` |
| `loop_guard` | 1 | `harness/tools/browser_tools/dispatch.py:763` |
| `loop_nudge` | 1 | `agent_harness.py:1696` |
| `loop_observation` | 1 | `harness/tools/loop_guard.py:142` |
| `model` | 1 | `agent_harness.py:1407` |
| `navigation_context_rejected` | 1 | `harness/tools/browser_tools/capability.py:236` |
| `page_binding_guard` | 2 | `harness/tools/browser_tools/capability.py:357`, `harness/tools/browser_tools/dispatch.py:794` |
| `page_lifecycle_gate` | 1 | `harness/tools/browser_tools/capability.py:387` |
| `page_stats` | 1 | `agent_harness.py:1673` |
| `progress_observation` | 2 | `harness/tools/browser_tools/progress_obs.py:140`, `harness/tools/browser_tools/progress_obs.py:408` |
| `root` | 1 | `harness/diagnostics/judge_trace.py:56` |
| `runtime_batch_boundary_rejected` | 1 | `agent_harness.py:1636` |
| `runtime_escalation_rejected` | 1 | `harness/tools/browser_tools/capability.py:268` |
| `runtime_policy_rejected` | 1 | `harness/tools/browser_tools/capability.py:259` |
| `screenshot_guard` | 1 | `harness/tools/browser_tools/capability.py:397` |
| `skill_fast_path` | 1 | `harness/skill/dispatch.py:2172` |
| `snapshot_diff` | 1 | `agent_harness.py:1681` |
| `stale_axtree_target` | 2 | `harness/tools/browser_tools/capability.py:432`, `harness/tools/browser_tools/capability.py:1146` |
| `tool_after_call_exception` | 1 | `harness/tools/browser_tools/dispatch.py:639` |
| `tool_arguments_rejected` | 1 | `harness/tools/browser_tools/dispatch.py:619` |
| `tool_error` | 2 | `harness/tools/browser_tools/capability.py:134`, `harness/tools/browser_tools/dispatch.py:783` |
| `tool_exception` | 1 | `harness/tools/browser_tools/dispatch.py:480` |
| `trace_entry` | 1 | `harness/tools/browser_tools/dispatch.py:821` |
| `workflow_policy_rejected` | 1 | `harness/tools/browser_tools/capability.py:309` |
| `workflow_runtime_disabled` | 1 | `harness/tools/browser_tools/capability.py:290` |

## 6. 消费端

机械匹配，可能含误报；用于证明某条产生链是否真的有读者。

### `read_events(` — 读取持久化 run event（4 处）

| 位置 | 代码 |
|---|---|
| `harness/storage/dual_store.py:490` | `return self.primary.read_events(` |
| `harness/storage/dual_store.py:1006` | `rows = store.read_events(task_id=task_id, after_event_id=cursor, limit=1000)` |
| `harness/storage/dual_store.py:1035` | `rows = store.read_events(task_id=task_id, after_event_id=cursor, limit=page)` |
| `harness/storage/sqlite_store.py:376` | `return dao.read_events(` |

### `.trace` — 读取 agent.trace 列表（46 处）

| 位置 | 代码 |
|---|---|
| `agent_harness.py:1043` | `self.trace: List[JsonDict] = []` |
| `agent_harness.py:1058` | `runtime, logger, self.trace, actor_type="browser",` |
| `agent_harness.py:1407` | `self.trace.append({` |
| `agent_harness.py:1636` | `self.trace.append({` |
| `agent_harness.py:1673` | `self.trace.append({` |
| `agent_harness.py:1681` | `self.trace.append({` |
| `agent_harness.py:1696` | `self.trace.append({` |
| `agent_harness.py:2871` | `for item in self.trace` |
| `agent_harness.py:3012` | `for item in (self.trace or []):` |
| `harness/spawner/spawner_worker.py:769` | `trace_path = self._write_worker_trace(worker_id, harness.trace)` |
| `harness/spawner/spawner_worker.py:770` | `trace_summary = self._summarize_worker_trace(harness.trace)` |
| `harness/spawner/spawner_worker.py:837` | `harness.trace,` |
| `harness/tools/browser_tools/capability.py:134` | `agent.trace.append({"type": "tool_error", "result": result})` |
| `harness/tools/browser_tools/capability.py:182` | `agent.trace.append({"type": "browser_call_params_error", "result": result})` |
| `harness/tools/browser_tools/capability.py:198` | `agent.trace.append({"type": "browser_call_rejected", "result": result})` |
| `harness/tools/browser_tools/capability.py:236` | `agent.trace.append({` |
| `harness/tools/browser_tools/capability.py:259` | `agent.trace.append({"type": "runtime_policy_rejected", "result": policy_error})` |
| `harness/tools/browser_tools/capability.py:268` | `agent.trace.append({` |
| `harness/tools/browser_tools/capability.py:290` | `agent.trace.append({` |
| `harness/tools/browser_tools/capability.py:309` | `agent.trace.append({"type": "workflow_policy_rejected", "result": workflow_error})` |
| `harness/tools/browser_tools/capability.py:323` | `agent.trace.append({"type": "contract_violation", "result": contract_result})` |
| `harness/tools/browser_tools/capability.py:331` | `agent.trace.append({` |
| `harness/tools/browser_tools/capability.py:344` | `agent.trace.append({` |
| `harness/tools/browser_tools/capability.py:357` | `agent.trace.append({` |
| `harness/tools/browser_tools/capability.py:373` | `agent.trace.append({` |
| `harness/tools/browser_tools/capability.py:387` | `agent.trace.append({` |
| `harness/tools/browser_tools/capability.py:397` | `agent.trace.append({"type": "screenshot_guard", "result": screenshot_guard})` |
| `harness/tools/browser_tools/capability.py:404` | `agent.trace.append({"type": "dialog_guard", "result": dialog_guard})` |
| `harness/tools/browser_tools/capability.py:419` | `agent.trace.append({"type": "browser_call_params_error", "result": target_param_guard})` |
| `harness/tools/browser_tools/capability.py:432` | `agent.trace.append({"type": "stale_axtree_target", "result": stale_target})` |
| `harness/tools/browser_tools/capability.py:477` | `agent.trace.append({"type": "browser_call_schema_rejected", "result": result})` |
| `harness/tools/browser_tools/capability.py:489` | `agent.trace.append({` |
| `harness/tools/browser_tools/capability.py:505` | `agent.trace.append({` |
| `harness/tools/browser_tools/capability.py:517` | `agent.trace.append({` |
| `harness/tools/browser_tools/capability.py:1054` | `agent.trace.append({` |
| `harness/tools/browser_tools/capability.py:1146` | `agent.trace.append({"type": "stale_axtree_target", "result": stale_target})` |
| `harness/tools/browser_tools/capability.py:1356` | `agent.trace.append({` |
| `harness/tools/browser_tools/dispatch.py:763` | `agent.trace.append({"type": "loop_guard", "result": guard_result})` |
| `harness/tools/browser_tools/dispatch.py:783` | `agent.trace.append({"type": "tool_error", "result": result})` |
| `harness/tools/browser_tools/dispatch.py:794` | `agent.trace.append({` |
| … | 另有 6 处 |

### `worker_trace_events` — 读取 worker trace 表（6 处）

| 位置 | 代码 |
|---|---|
| `harness/storage/dao.py:623` | `"SELECT COALESCE(MAX(sequence_no), 0) FROM worker_trace_events"` |
| `harness/storage/dao.py:629` | `"INSERT INTO worker_trace_events("` |
| `harness/storage/dao.py:657` | `sql = "SELECT * FROM worker_trace_events WHERE task_id = ?"` |
| `harness/storage/virtual_fs.py:13` | `traces/<worker>.jsonl     -> worker_trace_events` |
| `harness/storage/virtual_fs.py:123` | `" FROM worker_trace_events WHERE task_id = ?"` |
| `harness/storage/virtual_fs.py:275` | `"SELECT trace_event_id, trace_json FROM worker_trace_events"` |

### `on_event` — 事件回调（Console/transport）（19 处）

| 位置 | 代码 |
|---|---|
| `abcp_client.py:308` | `on_event: Optional[EventCallback] = None,` |
| `abcp_client.py:311` | `self.on_event = on_event` |
| `abcp_client.py:700` | `if not self.on_event:` |
| `abcp_client.py:706` | `self.on_event(event_type, sanitize(payload, table))` |
| `agent_harness.py:1800` | `extension_event = (` |
| `agent_harness.py:1805` | `self._write_agent_event(extension_event, {` |
| `agent_harness.py:4476` | `async with ABCPClient(browser_config, on_event=event_logger) as browser:` |
| `harness/fast_path.py:226` | `if (candidate := _successful_collection_event(event)) is not None` |
| `harness/fleet/runtime.py:1706` | `name, event_payload = _notification_event(message)` |
| `harness/spawner/spawner_slots.py:892` | `client = _sp().ABCPClient(self.runtime.browser, on_event=event_logger)` |
| `harness/spawner/spawner_slots.py:1027` | `client = _sp().ABCPClient(self.runtime.browser, on_event=event_logger)` |
| `harness/spawner/spawner_slots.py:1166` | `slot.client.on_event = slot.idle_event_logger` |
| `harness/spawner/spawner_worker.py:475` | `slot.client.on_event = event_logger` |
| `harness/utils.py:498` | `on_event: Optional[EventSink] = None,` |
| `harness/utils.py:511` | `self.on_event = on_event` |
| `harness/utils.py:575` | `self.on_event(event_type, payload) if self.on_event else None` |
| `harness/utils.py:789` | `return on_event` |
| `main.py:2196` | `on_event=ConsoleProgressReporter(),` |
| `main.py:2327` | `on_event=ConsoleProgressReporter(),` |

### `iter_events(` — 遍历事件流（1 处）

| 位置 | 代码 |
|---|---|
| `harness/diagnostics/selector_audit.py:251` | `for event in _iter_events(run_jsonl_path):` |

