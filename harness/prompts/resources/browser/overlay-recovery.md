---
id: browser.overlay-recovery
audience: browser
version: "2026-09-09"
description: Resolve a page-covering modal, popup, cookie banner, or mask without treating an AXTree miss as clearance.
sources:
  - harness/observation/overlay_detector.py
  - harness/observation/overlay_actions.py
  - harness/tools/browser_tools/auto_intercept.py
  - harness/tools/browser_tools/composites/dismiss_overlay.py
emitter_sources:
  - harness/diagnostics/error_classification.py
  - harness/tools/browser_tools/auto_intercept.py
related_tools:
  - dismiss_overlay
  - browser_call
  - visual_verify
related_methods:
  - DOM.getAXTree
  - Input.click
  - Input.press
  - Page.getState
error_codes:
  - occluded
  - target-occluded
topics:
  - overlay
  - modal
  - popup
  - mask
  - occlusion
  - dismiss
  - conflicting evidence
aliases:
  - 遮罩
  - 弹窗
  - 模态框
  - 页面被挡住
  - 元素被遮挡
  - 关闭弹窗
  - 红包弹窗
---
# Overlay recovery

Use this guide for a page-covering modal, popup, consent banner, promotional
mask, or an `occluded` / `target-occluded` receipt.

## Treat conflicting observations honestly

A visual/reality check can observe a cover that the AXTree does not name as a
dialog or even as an actionable node. A later AXTree miss means only that the
surface was not observed through that tree; it does not disprove the visual
observation. Keep the conflict open rather than calling the page clear, and do
not operate a control behind the reported cover.

This does not require closing every visible surface. First decide from the
user's goal whether the surface itself is the intended UI. If the task needs a
page control behind a routine business overlay, clear the overlay before using
that control. If the surface is an authentication, payment, provider, or other
consequential flow, do not turn its primary action into a dismissal attempt.

## Decide whether the cover requires human action

Before choosing dismissal, connect the observed surface to the failed action.
An ordinary sign-in link or a nonblocking embedded login panel is not enough
for HITL. A current authentication/verification surface with concrete login
controls, together with evidence that it blocks the intended target, is enough:
call `Hitl.requestPause` in the current worker with the page id and the human
action needed. Do not first try the dismissal ladder or another equivalent
button behind that cover.

Reuse current action receipts and DOM/AX evidence. Repeating `Page.getState`
and tree retrieval is not required to reconfirm established facts. If the
relationship between the cover and target is ambiguous, inspect that specific
uncertainty; when structured evidence cannot explain the cover, use a narrow
`visual_verify` with `mode="overlay_check"`. Visual diagnosis of a cover does
not require exhausting alternative click targets or visual-location recovery.
Page readiness, no native dialogs, and readable background content do not
establish that the target is unblocked. Reassess after a real page-state change.

## Use the bounded dismissal tool for an eligible routine overlay

Refresh `DOM.getAXTree` once when current targets may be stale. Then call
`dismiss_overlay` rather than reproducing close, Escape, backdrop, or visual
location steps yourself.

- If a normal `Input.click` was rejected as occluded, copy that target's
  current `targetId` and `targetMethod` into the tool. Its result may prove the
  original click already ran; do not issue it again when `retried=true`.
- If the cover is known before an underlying action is attempted, pass the
  page id and empty `targetId` / `targetMethod`. This asks only for dismissal;
  it does not authorize a follow-up action.
- `dismissed_pending_action` means the original action was not auto-retry-safe.
  Re-observe and decide whether it still advances the user goal before
  dispatching it once.
- `failed`, `blocked`, or `policy_refused` do not make the page clear. Read the
  receipt, refresh the page evidence, and choose a task-consistent next action.
  Login, provider, paywall, or payment surfaces may need HITL or an explicit
  blocker; do not click through them.

The tool's safe ladder uses only an eligible close control, Escape, a proven
backdrop, and then bounded visual recovery. It does not make a generic
"Accept", sign-in, payment, or provider button safe to press.

## Verify before resuming

After a dismissal result, refresh `DOM.getAXTree` and targets. When the original
evidence was visual and the AXTree still cannot describe the cover, use one
narrow `visual_verify` call with `mode="overlay_check"` to ask whether the
same page-covering surface remains. `Page.screenshot` is only a saved-path
capture in this harness, so use `visual_verify` for screenshot-based visual
evidence. Never estimate click coordinates from a screenshot.

If the same mask remains after a failed or refused dismissal in the same page
state, do not repeat the identical dismissal blindly. Re-observe first. A mask
that reappears after confirmed clearance, or a different cover after a page
change, is new evidence and can justify another bounded recovery.
