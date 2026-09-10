---
id: browser.runtime-evaluate
audience: browser
version: "2026-09-08"
description: Runtime.evaluate world selection, audit receipts, and legacy JSON extraction compatibility.
sources:
  - harness/runtime_evaluation.py
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

Runtime.evaluate accepts model-authored JavaScript, including page-state writes.
Its parameters and execution world follow the live ABCP schema. Use `isolated`,
`main`, or `auto` when the advertised schema permits it; `auto` is resolved by
ABCP.

## Choosing the execution world

Choose `world` for each expression from the user's goal, current page evidence,
and the JavaScript dependencies. This is the model's decision: do not choose by
site name, component library, or a remembered default.

- Use `isolated` when the expression needs the shared DOM or standard Web APIs,
  but does not need JavaScript objects created by the page. Its JavaScript
  global environment is separate from the page's. DOM writes still affect the
  page and must be treated as state changes.
- Use `main` when the expression must read or write page-owned globals,
  framework runtime state, page-defined APIs, or values whose identity must be
  the page's own. `main` executes in the page's JavaScript world: mutations of
  `window`, prototypes, event handlers, or shared singletons persist and are
  visible to the page's later code. Keep that scope deliberate and verify the
  resulting page state.
- Use `auto` only when either execution world satisfies the goal and a possible
  platform retry is safe. It is not the default for an unknown dependency. A
  retry can run the expression more than once, so a write must be idempotent or
  otherwise safe to repeat before choosing `auto`.

After the call, read `runtimePolicy.requestedWorld`, `executedWorld`,
`fallbackReason`, and `worldEvidenceStrength` when present. If world evidence
is degraded or absent, do not infer the executed world from the returned value;
re-observe the page state before taking a dependent action.

The Harness does not inspect the expression, require prior structured reads, or
classify an expression's effect. It records the requested and platform-reported
execution world, then invalidates stale page targeting state after every model
Runtime.evaluate call. Re-observe any page state that a subsequent action
depends on.

`runtime_policy` is optional legacy metadata and is not authorization data. Its
`result_mode="json"` and `record_name` fields retain the existing JSON extraction
envelope for older callers; new calls can omit it.
