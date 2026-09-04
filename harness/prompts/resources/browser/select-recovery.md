---
id: browser.select-recovery
audience: browser
version: "2026-09-04"
description: Interpret DOM.inspectSelect and Input.select receipts, including replay safety and popup/option recovery.
sources:
  - harness/tools/browser_tools/capability.py
  - harness/tools/browser_tools/dispatch.py
emitter_sources:
  - harness/diagnostics/error_classification.py
related_tools:
  - browser_call
  - visual_verify
related_methods:
  - DOM.inspectSelect
  - Input.select
error_codes:
  - select-capability-unavailable
  - select-popup-not-found
  - select-popup-ambiguous
  - select-popup-not-ready
  - select-option-not-in-current-window
  - select-option-label-ambiguous
  - select-option-disabled
  - select-options-incomplete
  - select-target-not-select
  - select-selection-mode-unknown
  - select-state-restore-failed
  - select-final-state-unproven
topics:
  - select
  - dropdown
  - popup
  - option enumeration
  - combobox
  - replay safety
  - unsupported control
  - selection verification
aliases:
  - 下拉框
  - 下拉选择
  - 下拉菜单
  - 选项弹窗
  - 找不到选项
  - 选不中
  - 不是下拉框
  - 多级分类
  - 选完没生效
---
# Select recovery

Use this guide when a native or custom select needs interpretation beyond the
live method schema, or when an `Input.select` / `DOM.inspectSelect` receipt is
not self-explanatory.

Start with `DOM.inspectSelect` when choices are unknown. It returns the actual
control id/kind, selection mode and the currently observed choices. A custom
control inspection may drive its menu with real key presses, so previously
captured element ids can already be stale; refresh the AX tree before acting on
another target. Copy the returned option id, exact label or explicit value to
`Input.select` without converting it. Native selects require their value.

An Input.select success on a custom control proves recorded keyboard activity,
not necessarily a final committed value. If later work depends on that choice,
inspect again or read the control value.

Do not replay a failed select merely by changing the requested option. A failed
receipt does not prove whether keys or a state change were already sent. Read
`selectRecovery`, `selectGuard`, `tool_was_executed` and the current page
evidence. A successful inspect can clear the replay block, but only
`selectRecovery.retryAllowed=true` permits the one corrected selection the
receipt describes.

For `select-popup-not-found`, `select-popup-ambiguous`, or
`select-popup-not-ready`, re-read AX evidence, then use a page-wide semantic
tree to relate aria-controls/aria-owns/aria-activedescendant to a portal menu,
listbox or option. Use visual locate only when the target is visibly on screen
and structured evidence cannot name it; visual locate identifies a target but
does not authorize an action. Re-observe after any action.

For `select-option-not-in-current-window`, `select-option-label-ambiguous`, or
`select-option-disabled`, the platform enumerated the menu but the requested
option did not match. Re-inspect and use a field that inspection returned. When
the walk was incomplete, continue it from the `startOption` the platform named
rather than starting the enumeration over. For `select-options-incomplete`,
enumeration failed: inspect again before using the menu-binding path. A bare
`-32005` is an inspection failure, not a retry permit; do not infer a selector
from its text and assume the UI may have re-rendered.

`select-capability-unavailable` is a different family. A required capability,
page surface or frame was unavailable, so the control was neither inspected nor
operated. Re-observe page and control state and continue only once it is
available. Visual locate is deliberately not offered here: when the surface
itself is missing there is nothing trustworthy to photograph, and a screenshot
that succeeds anyway is of a different surface than the one that failed.

## Reading the receipt fields

`selectGuard.selectionBlockPolicyApplies` is POLICY - it says this Action is
subject to the pre-dispatch block. `selectGuard.blockedNow` is STATE - whether
it is blocked at this moment, written after the ledger update. They were one
field once, and a successful selection reported itself blocked as a result;
read them separately.

While the block stands, no selection on that control is dispatched, whichever
option it names, so naming a different option does not help. It is lifted by a
successful `DOM.inspectSelect` on that control, whose receipt carries
`selectReplayBlockCleared`. Even then, only `selectRecovery.retryAllowed=true`
permits the single corrected selection the receipt describes.

A `contractDrift` field means a code arrived on an Action this harness declares
cannot raise it. It is advisory: the recovery in this guide is still the right
one to follow, and `contractDriftDetail` names the mismatch to report.

## When the control is not a select at all

`select-target-not-select` is a verdict about the element, not a failure to
recover from - but two different situations reach it, so settle which one
first. Refresh the AX tree and check whether the real select control is a
different node you simply did not target; the platform's own suggested_prompt
reads that way, and if it is true, target that node instead.

If the page genuinely has no supported select here, the verdict stands: only
native `<select>`, Ant Design and Element controls have adapters, so this
element is ordinary UI and the select Actions will keep refusing it however
the request is reshaped. Drive it as ordinary UI: enumerate fresh AX targets
and take one verified step per visible level. A visible multi-column category
or list browser is ordinary UI, not a broken select. If a level is visible but
no structured surface names it, visual locate can name it, and the id it
returns is acted on with whichever ordinary `Input.*`/`DOM.*` method that
level needs.

`select-selection-mode-unknown` is neither of those, and it is not terminal on
arrival. The platform could not confirm whether the control takes one option
or a final set of options. Do not send another `Input.select` and do not guess
the mode: inspect the control again and read the mode off that observation.
Only when a fresh inspection still cannot confirm it is this an ABCP select
contract failure to report with the receipt.

## After a selection that did not prove itself

Two codes arrive when the selection itself already happened. Neither is a
failure to retry, and treating them as one is how a successful selection gets
undone.

`select-state-restore-failed` means the selection was made but the menu could
not be restored to a known state. Inspect the control again before continuing.
Do not assume the menu is closed - and do not assume it is open either, which
is the half that sends an option click into a surface that is no longer
there.

`select-final-state-unproven` means the keyboard operation recorded the choice
but the final control state could not be proven. Read the control's own value
first - inspect it, or `DOM.getAttribute` - BEFORE issuing any correction. A
corrective selection against an unknown state can undo a selection that in
fact succeeded.
