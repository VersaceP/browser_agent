---
id: browser.runtime-evaluate
audience: browser
version: "2026-09-03"
description: Runtime.evaluate admission rules, isolated-world execution, and the narrow main-world fallback.
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
  - runtime_policy_rejected
  - runtime_escalation_rejected
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

Use Runtime.evaluate only as a read-only last resort after the current page
epoch includes both a structural read and a targeted native read. Page.getState
is neither. Structured DOM/Page/Input methods remain the preferred contract.

Supply the complete runtime_policy required by the live schema: intent, effect,
valid reason_kind, why structured tools are insufficient, and a cross-check
plan. Request the isolated world and never ask for `main` or `auto`, mutation,
permission bypass, or a substitute for Input/File operations.

The Harness may make a strict main-world retry only for the documented
`ABCP_MAIN_WORLD_REQUIRED:<global>` signal and only for an allowed
non-DOM-state policy. Do not attempt to manufacture that retry yourself. Treat
the returned world metadata and failure classification as the authority for
what happened.

JSON mode needs a serialisable expression or invoked IIFE. When the computed
value is already the extraction rows, use runtime_policy.record_name so the
Harness can retain the result through the normal evidence path.
