---
name: workflow_batch_recovery
description: How to recover when batch_browser_actions stops mid-iteration. stop_reason classification, recovery actions, and resume protocol. Read when batch returned an error or partial-completion result.
---

# batch_browser_actions failure recovery

`batch_browser_actions` stops on the first error and returns a structured
`stop_reason`. Read it, recover with the matching SINGLE-STEP tool, then
resume with `start_from=<failed_at>`. Partial results are auto-merged on
resume — you do NOT lose iter 0..N-1 work.

## stop_reason → recovery action map

| stop_reason | what happened | recovery action | resume call |
|---|---|---|---|
| `interaction_required` | A native dialog blocked the page | `dismiss_dialog()` or `accept_dialog()` (single-step) | `batch_browser_actions(start_from=<failed_at>, ...same steps)` |
| `human_required` | Captcha / Cloudflare / login needed | `wait_user("captcha — solve and press Enter")` | `batch_browser_actions(start_from=<failed_at>, ...same steps)` |
| `selector_failed` | A step's CSS / xpath matched nothing | Use `dom_query` to re-verify, then refine the recipe | `batch_browser_actions(start_from=<failed_at>, ...corrected steps)` |
| `transient` | Network blip / timeout / page reload | wait briefly | `batch_browser_actions(start_from=<failed_at>, ...same steps)` |
| `user_rejected` | User rejected iter 1 in `require_first_approval` mode | Fix steps / selectors based on user feedback | `batch_browser_actions(...new steps)` — start FRESH, no `start_from` |
| `configuration_error` | Bad iteration payload (e.g. non-dict iter) | Fix the `iterations` array | `batch_browser_actions(...corrected iterations)` |

## What you get back on failure

```
{
  "ok": true,
  "completed": false,
  "iterations_total": N,
  "succeeded_total": K,           # K iterations succeeded before the failure
  "failed_at": K,                 # the exact iteration index that failed
  "stop_reason": "...",
  "exception": "...",
  "message": "...",
  "failed_tool": "...",           # which step's tool errored
  "failed_step_idx": ...,         # index within steps array
  "suggested_action": "...",      # human-readable next step
  "partial_saved_to": "<path>",   # spill file with K records
  "schema": [...],                # field names from successful records
  "sample_first": {...},
  "sample_last": {...}
}
```

## Resume mechanics

The `partial_saved_to` file is written automatically. When you re-call with
`start_from=<failed_at>`, the tool:
1. Reads `partial_saved_to`, keeps the first `start_from` entries.
2. Continues from iter `<failed_at>`.
3. Merges new results into the same file.
4. On full success, calls `publish_artifact` once with the full merged file
   (if you set `publish_to`).

Pass the same `partial_filename` across calls if you customized it.

## Anti-patterns

✗ Re-issuing batch WITHOUT `start_from` after a transient failure → re-runs
   the K already-succeeded iterations.
✗ Refining steps after `selector_failed` but forgetting `start_from` →
   loses the K records before the failure.
✗ Passing `partial_filename` mismatched between calls → silent loss of
   partial progress.

## When to give up vs keep recovering

- After 2 consecutive `human_required` (Cloudflare / captcha) on the same
  iteration → escalate to lead, don't keep retrying yourself.
- After `selector_failed` and a refined recipe still fails → switch to
  `read_skill('workflow_vision_first')` to verify the section actually exists
  before assuming your selectors are wrong.
- If `succeeded_total` ≥ 80% of `iterations_total` and the rest are flaky →
  publish the partial as-is rather than burning more attempts.
