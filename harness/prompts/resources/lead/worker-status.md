---
id: lead.worker-status
audience: lead
version: "2026-09-05"
description: Interpret worker terminal statuses, validation state, and handoffs before continuing, replanning, or finalising.
sources:
  - harness/results/worker_result.py
  - harness/task_control/phase_lifecycle.py
  - harness/tools/lead_tools.py
emitter_sources:
  - harness/spawner/spawner_classification.py
  - harness/task_control/phase_lifecycle.py
  - harness/constants.py
  - harness/task_control/artifact_validation.py
related_tools:
  - wait_browser_agents
  - list_browser_agents
  - final_answer
related_methods: []
error_codes:
  - blocked_cross_task_type_required
  - context_limit_exceeded
  - page_crashed
  - step_budget_exhausted
  - blocked_by_dependency
  - blocked_infrastructure
  - blocked_user_action_required
  - validated_done
  - target_absent
  - objective_exhausted
  - phase_exhausted
  - instruction_infeasible
  - browser_api_contract_error
  - collection_contract_replan_required
topics:
  - worker status
  - terminal status
  - handoff
  - replan trigger
aliases:
  - worker 状态
  - 终态
  - 失败原因
  - 要不要重规划
---
# Worker status interpretation

Raw worker status is never completion proof. Reuse only artifacts whose
validation state and receipts support the fields and identities required by the
phase contract.

For `partial`, carry forward only validated rows and continue the uncovered
obligations without changing the durable contract. For `step_budget_exhausted`,
inspect result levels and extraction artifacts before choosing a narrow
continuation or a changed experiment. `context_limit_exceeded` needs a smaller
task boundary or result contract, not a verbatim retry.

For `page_crashed`, recreate a page only in the same allowed Fleet/session when
routing allows it; distinguish loss of required unsaved page-local state from
ordinary renderer recovery. HITL and session-continuity outcomes require the
structured routing instruction, not a fresh Fleet escape. A
`blocked_cross_task_type_required` result needs a new phase with the correct
task type. `collection_contract_replan_required` must change the immutable
artifact shape - replan expected_artifact.fields with the nested array
expectedShape the receipt reports, because the worker cannot repair its own
contract; respawning it unchanged repeats the same refusal.
`browser_api_contract_error` is a platform-side contract problem: switch method
or report it, rather than retrying the same call. `failed`, `cancelled` and
`unknown` carry no verdict at all - read error and diagnostics, and be
conservative before scaling anything up.
