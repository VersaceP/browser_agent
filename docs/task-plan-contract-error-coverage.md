# Task plan contract and error coverage

This inventory records where a Lead learns each task-plan rule. It is organized
by universal contract domains rather than incidents, sites, or business fields.
The mechanical layer may reject malformed protocol, invalid authorization,
broken identity/dependency bindings, invalid arithmetic, and internally
contradictory declarations. Whether a valid declaration satisfies the user's
business goal remains an independent semantic-review decision.

| Contract domain | Authoring disclosure | Mechanical receipt | Structured repair | Status |
|---|---|---|---|---|
| Plan root, phase IDs and supported phase type | planning prompt + emit schema | exact phase/index error | complete re-emit for structural edits | covered |
| Per-phase `task_type` capability boundary | planning prompt + enum/description | phase path and allowed values | `set` when the property exists | covered |
| `stage_hint` dispatch classification | planning prompt + schema | allowed values; row-wise generic warning/rejection | semantic candidates, then `set` | covered |
| Compact/shared output contracts | planning prompt + emit schema | source declaration path and affected phases | bounded object `add`/`set` | covered |
| Empty-value policy and evidence-backed absence | planning prompt + field schema | exact source field path and legal example | add missing field outcome list | covered |
| Row count versus per-row array length | planning prompt + schema | validator/type compatibility errors | `set` existing bound or complete re-emit | covered |
| Required fields, non-empty fields and provenance | emit schema + validator schema | field and validator location | `set` existing declarations | covered |
| Form `requiredControls` receipt contract | planning prompt + emit schema | phase-specific explanation | typed alternative repair options | covered |
| Direct input versus artifact-derived input | planning prompt + emit schema | missing source/identity and mutual-exclusion errors | complete re-emit when structure changes | covered |
| Dependencies and artifact producer binding | planning prompt + emit schema | missing/unknown/cyclic producer errors | complete re-emit when phase arrays change | covered |
| Dispatch waves and pacing | planning prompt + schemas | numeric/order/invariant errors | `set` existing values | covered |
| Cohorts, checkpoints and replan immutability | schema + on-demand guides | authoritative IDs and immutable-prefix errors | dedicated extend/replan path | covered |
| ABCP method capability and authorization | task-type descriptions + capability digest | disabled/unknown method facts per phase | change declared task type or objective | covered |
| Replan reason and operator approval identity | planning prompt + receipts | stable error code and candidate hash | dedicated approval/replan tools | covered |
| Repeated equivalent invalid candidates | first rejection receipt | remaining-submission budget and terminal reason | candidate repair or materially changed plan | covered |

## Disclosure policy

The first Lead call receives only plan-authoring rules and plan-related tools.
Execution, worker recovery, artifact repair, and final-answer policy are loaded
after an exact plan candidate is approved. Detailed recovery rules remain in
the guide index and are loaded on demand. A mechanical rejection must carry the
rule-specific path and example needed to repair that candidate; the Lead should
not search the entire guide corpus to understand a local schema error.

## Repair protocol

`repair_task_plan` uses a candidate hash and bounded RFC 6901 paths. `add` may
create one missing property only when its parent object already exists; `set`
replaces an existing value; `remove` deletes an existing object property.
Array insertion, deletion, or reordering and missing parent containers require
a complete revised plan. Every repaired candidate is compiled and validated
again, so the repair operation itself cannot bypass plan invariants or semantic
review.

## Regression expectations

- Every example in the planning prompt and tool schema must compile.
- Shared-contract errors identify their single source declaration and list all
  affected phases.
- Reviewer collection facts reflect the validators that will actually execute,
  including `minItems > 0` and evidence-backed empty-array exceptions.
- Before approval, spawn/wait/artifact tools are absent. `final_answer` remains
  available so an operator cancellation can terminate cleanly. After approval,
  the full execution tool set is present.
- Usage events identify the effective role-specific provider and model rather
  than the top-level fallback model.
