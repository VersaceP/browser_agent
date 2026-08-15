# Semantic status consumer audit

This is the required pre-migration consumer inventory for the v4.1 semantic
decision changes. The governing boundary is: receipts and arithmetic remain
code-owned; page meaning and business completion remain model judgments with
provenance and counterevidence.

## Ledger ownership

- `task_state.json` is the current task's authoritative coordination ledger.
  `state.artifacts` and each phase's `validated_artifacts` contain only final,
  validated deliveries. Phase `attempts` contain raw status, validation,
  attemptDigest, partial artifact paths, and the attributed handoff.
- `strategy_attempts.jsonl` is append-only audit telemetry. The task-local file
  mirrors the relevant run, while the workspace-root file is a cross-run audit
  stream. Neither is read for scheduling, validation, completion, strategy
  ranking, or causal attribution. New entries omit `statusCategory`; historical
  lines are not rewritten.

## Phase lifecycle

- `SEMANTIC_TERMINAL_CLASSIFICATIONS` is empty. Worker classifications such as
  `target_absent`, `instruction_infeasible`, and content suppression remain in
  the attempt digest as claims; they no longer choose a terminal phase state.
- `TERMINAL_PHASE_STATUSES` retains validated completion and hard orchestration
  or HITL receipts. Dependency blocking consumes this set, so unverified page
  semantics no longer freeze downstream phases.
- `RETRYABLE_PHASE_FAILURE_STATUSES` covers mechanical failure outcomes.
  `partial` is deliberately continuable and excluded from the declared
  `max_attempts` count.
- `REPLAN_RESET_STATUSES` retains mechanical phase/dependency reset behavior.
  The `objective_exhausted` rejection path is removed; objective counters remain
  observations available in state and handoff evidence.

## Repeated-attempt and progress consumers

- The repeated-phase signature is retained in `attemptDigest.failureSignature`
  as evidence. The spawn rejection and `phase_locked_must_finalize` escalation
  are removed.
- Existing progress and loop interceptors remain unchanged pending the
  replay-only shadow A/B decision. Their eventual removal is not assumed by
  this migration.

## HITL consumers

- Model-allowed soft statuses are limited to ordinary outcome claims.
- `blocked_by_challenge` and HITL lifecycle statuses require diagnostics-owned
  pause/wait/resume/session receipts. Without such a receipt, a model report is
  normalized to `incomplete` and remains visible as an unverified claim.
- Structural DOM/AX/VL challenge observations may motivate a pause request but
  do not themselves become a hard terminal receipt.

## Content completeness consumers

- Marker matches, missing-region lists, scroll/navigation/action receipts, and
  page summaries remain observable through the tracker and worker trace.
- Artifact validation no longer calls a content-completeness veto.
- Worker semantic classifications are no longer rewritten by completeness
  tracking; BrowserAgent final-answer dispatch no longer rejects on that
  semantic decision. `unresolved_observation()` — the renamed `terminal_veto()`
  — has been deleted along with `recovery_receipt()` and
  `route_preference_for_page()`: production had no caller for any of them.
  Model-facing paths use the fact-only projection and never receive
  action-oriented labels.
- A worker's `target_absent` / `instruction_infeasible` claim keeps the category
  the worker declared. Where this run's receipts say something against it
  (never scrolled, no exhaustion proof, cited artifacts absent from the ledger,
  visual-check-only evidence), those facts ride along as
  `classification.counterevidence`; the harness neither rewrites the category
  nor prescribes the next move.
- Model-facing tool results and handoffs strip tracker `decision`, route policy,
  and next-instruction labels. They retain only attributed observation facts.

## Final completion and Reviewer

- A required artifact phase reaches `validated_done` only from raw worker
  `status=done` plus artifact validation `status=done`.
- A partial artifact remains in the phase attempt and handoff evidence. It is
  intentionally excluded from the global/phase validated artifact ledgers, so
  completion, numeric reconciliation, and `batch_source` cannot silently treat
  an unfinished collection as trusted upstream data.
- Lead final `done` performs only a terminal consistency check against each
  required phase's latest raw attempt. It is not a page-completeness proof and
  does not reject legitimate no-artifact operations.
- Reviewer receives the same attributed worker handoffs as Lead continuation.
  Tactical changes and declared resource-budget changes bypass full semantic
  review when goal, topology, capability boundary, and deliverable are stable.
