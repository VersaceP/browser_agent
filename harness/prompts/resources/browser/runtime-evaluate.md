---
id: browser.runtime-evaluate
audience: browser
version: "2026-10-23"
description: Runtime.evaluate world selection, audit receipts, and direct JSON extraction recording.
sources:
  - harness/tools/runtime_evaluation.py
  - harness/tools/browser_tools/runtime_eval.py
  - harness/tools/browser_tools/capability.py
emitter_sources:
  - harness/tools/browser_tools/capability.py
related_tools:
  - browser_call
  - record_extraction
related_methods:
  - Runtime.evaluate
error_codes:
  - policy_violation
  - runtime_evaluation_rejected
topics:
  - runtime evaluate
  - javascript
  - isolated world
  - main world
  - admission
aliases:
  - 执行脚本
  - 注入脚本
  - 主世界
  - 隔离世界
---
# Runtime.evaluate

Runtime.evaluate is a bounded read-only fallback when current structured DOM/AX
and targeted native observations cannot express the missing fact. Use native
Actions for interaction. Do not mutate the page, synthesize input, invoke
page-owned business APIs, or use evaluation to conceal platform failures.
The platform's ability to execute JavaScript is not permission to use every
possible expression. Record the observation gap and preserve target identity.

## Choosing the execution world

Follow the live schema and choose the world from the expression's read dependencies,
not the site name or component library.

- Use `isolated` for shared DOM and standard Web API reads that need no page-owned
  JavaScript objects. Its global environment is separate; that alone does not make
  an expression read-only.
- Use `main` only for an authorized read that requires page-owned object identity.
- Use `auto` only where the advertised schema and caller scope allow it and either
  world satisfies the read; a fallback may repeat the expression. Workflow packages
  declare allowed worlds and do not accept `auto`.

After the call, read `runtimePolicy.requestedWorld`, `executedWorld`,
`fallbackReason`, and `worldEvidenceStrength` when present. If world evidence
is degraded or absent, do not infer the executed world from the returned value;
re-observe the page state before taking a dependent action.

These behavioral requirements are judged from the goal and evidence. The Harness
does not classify JavaScript effect with source-pattern rules or enforce a fixed
number of prior reads. It records the requested and platform-reported
execution world, then invalidates stale page targeting state after every model
Runtime.evaluate call. Re-observe any page state that a subsequent action
depends on.

## Recording structured captures

For data intended as an extraction artifact, set
`browser_call.runtime_policy={"record_name":"<dataset name>"}` in the capture
call. The expression returns a row-object array or `{rows:[...]}` directly,
without `JSON.stringify`. The Harness enables JSON decoding and passes the
returned rows unchanged through `record_extraction`; nested arrays, objects,
nulls and source text are preserved. Decide selectors and field mapping in the
expression rather than copying the resulting dataset into another model call.

Inspect `recordExtraction.status`, validation feedback and `savedPath`. Reuse
that path for inspection and delivery. Large recorded results
also expose the complete source receipt through `_truncation.savedPath`; read
it when source details are needed. Recording confirms persistence, not source
coverage, field meaning or task completion. A failed record is reported explicitly;
its returned data remains available for diagnosis.

Omit `record_name` for diagnostic calls. `result_mode="json"` alone requests JSON
decoding without persistence; existing calls using both options remain supported.
These options stay inside the Harness and do not grant JavaScript permissions
or choose the execution world.
