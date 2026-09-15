---
id: lead.artifact-validation
audience: lead
version: "2026-09-15"
description: Distinguish artifact shape repair, missing evidence, placeholder data, and durable contract changes after worker validation.
sources:
  - harness/evidence/artifact_evidence.py
  - harness/evidence/extraction_artifacts.py
  - harness/task_control/artifact_validation.py
  - harness/task_control/validators.py
  - harness/tools/lead_tools.py
emitter_sources:
  - harness/evidence/artifact_evidence.py
  - harness/results/row_ledger.py
  - harness/task_control/artifact_validation.py
related_tools:
  - lead_save_artifact
  - spawn_browser_agent
  - repair_task_plan
related_methods: []
error_codes:
  - is_placeholder
  - absence_declaration_missing
  - contract_invalid
topics:
  - artifact validation
  - placeholder
  - evidence
  - repair
  - schema
  - contract invalid
aliases:
  - 产物校验
  - 占位数据
  - 证据不足
  - 空值
---
# Artifact validation and repair

Use this guide after a worker result, artifact validator receipt, or
lead_save_artifact decision needs more than the short core contract.

record_extraction artifacts are the trusted handoff representation. Raw worker
prose, an artifact path, statusCategory and a row count do not prove the
requested fields, identities or validators passed. Keep counterevidence and
worker claims separate from validation facts.

When rows are trustworthy but their shape is wrong, use lead_save_artifact to
reshape from trusted extraction artifacts. Do not re-scrape merely to rename a
field. A validation_failed phase is not complete and cannot advance a
dependency until a replacement passes validation.

A `contract_invalid` receipt is a proved conflict between the declared schema
and a validator, so retrying the unchanged phase cannot help. Replan with a
compatible contract and reuse trustworthy rows where possible: `range` checks
a numeric scalar, while `array_length` checks the number of items in a declared
array. When the request caps a collection rather than requiring a fixed count
(for example, “at most N” or “however many exist”), only `max: N` is a real
constraint: the page's ceiling is not a floor the worker can meet. A lower
bound turns “the page has fewer” into a permanent failure that no retry can
clear. Use a lower bound or exact count only when the user explicitly requires
it; interpret an unqualified “first N” from the surrounding request rather
than assuming it means either a cap or a fixed delivery count.

For placeholder data, wrong values, missing rank/range evidence or off-target
rows, keep the same phase and try a changed falsifiable experiment if task
type, artifact contract and topology remain sound. Replan only when a durable
contract must change. A collection_contract_replan_required result means the
worker could not change an immutable nested shape; replan that explicit shape
instead of asking the worker to flatten, sample, or respawn unchanged.

Quantity and identity are independent. exact_rows proves cardinality, while a
named cohort also needs its declared identity/set and uniqueness evidence.
Never turn a missing value into a failure narrative in a data field or relax a
validator merely to accept a partial artifact.

For complete plan-authoring calls and validator-parameter repairs, read
`lead.plan-contracts`. In particular, `path_pattern` is a file-validator
parameter; `field_pattern` checks text and does not verify disk delivery.

## Required keys, non-empty values and verified absence

A requested output key must be present. Whether its value may be empty is a
separate semantic decision from the original goal. Require a non-empty value
when the user requires that concrete value. Use `empty:"with_evidence"` and
`allow_empty_with_outcome:["confirmed_absent"]` only when verified absence is
acceptable for that goal; do not invent permission to omit a requested value.
Neither "every scalar must be nonempty" nor "possibly missing means allow
empty" is a universal rule. Preserve the approved policy during execution;
a contradictory policy requires a reviewed repair, not silent relaxation.
The page's ceiling is not a floor the worker can meet; interpret an unqualified
“first N” from the original request. Entity coverage and nested-array size are
separate: missing optional values must not silently remove requested entities.
Absence evidence must satisfy the actual validator contract, including the
field's declared outcome and observation evidence. An empty value alone is not
proof of absence. See lead.plan-contracts for the field-shape example.

## Revalidation and semantic judgments

Use revalidate_phase_artifacts to inspect an existing failed phase before
spawning another worker just to resubmit its rows. Accept with a reason only
after comparing the original goal with the persisted evidence. The tool changes
no raw attempt history, budget, plan identity or browser state. Missing actual
files and invalid references remain failures. A conditional absence declaration
is a worker judgment, not proof produced by the harness; no fixed flags or
mandatory visual call apply. Domain affiliation is evidence for review, not a
mechanical rejection. Do not rebuild it using URL regexes.
