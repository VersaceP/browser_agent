---
id: lead.cohort-checkpoints
audience: lead
version: "2026-09-15"
description: Plan cohort dependencies, confidence checkpoints, continuations, and remediation without weakening the accepted contract.
sources:
  - harness/task_control/plan_validation.py
  - harness/task_control/cohorts.py
  - harness/tools/lead_tools.py
emitter_sources:
  - harness/task_control/replan.py
  - harness/tools/lead_tools.py
related_tools:
  - emit_task_plan
  - repair_task_plan
  - spawn_browser_agent
related_methods: []
error_codes:
  - replan_checkpoint_required
  - mechanical_invalid
topics:
  - cohort
  - checkpoint
  - dependency
  - batch
  - continuation
aliases:
  - 分组
  - 检查点
  - 依赖
  - 批次
---
# Cohorts and checkpoints

Use this guide when a phase uses batches, a cohort source, a confidence
checkpoint, validation/bulk progression, or a continuation after partial work.

depends_on carries data dependencies. Omitting it means serial dependency on
all earlier phases; an empty list declares independence; a list declares the
exact producers required. Do not create dependencies merely because a phase is
listed later.

The confidence ladder is conditional, not a template. Use a one-row probe only
when concrete evidence identifies a shared unknown route and the expected
duplicate-work cost justifies delaying independent targets. The probe must
contribute a requested deliverable, not be throwaway exploration. A checkpoint determines the next
required role. A continuation that proves a reusable candidate may advance to
validation; a bulk trace that loses proof must downgrade to continuation.
Create bulk only when the checkpoint requires it and the rows are independent.

An active checkpoint binds its successor to the same cohort and validated
predecessor. Retain that predecessor in a replacement plan, cite it in
depends_on, preserve or strengthen all non-slice validators, and bind exactly
one successor. Remaining or failed rows inside that cohort use a
checkpoint-bound continuation. Remediation is only for an explicit failed-row
set outside an active checkpoint; it cannot bind a checkpoint.
