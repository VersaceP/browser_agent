---
id: browser.page-lifecycle
audience: browser
version: "2026-09-15"
description: Reconcile navigation, page lifecycle, dialog, page inventory and stale-handle receipts before another browser action.
sources:
  - abcp-platform/resources/skills/webcross-browser/SKILL.md
  - harness/observation/page_lifecycle.py
  - harness/tools/browser_tools/capability.py
  - harness/tools/browser_tools/navigate.py
emitter_sources:
  - harness/tools/browser_tools/axtree_state.py
  - harness/tools/browser_tools/dispatch.py
  - harness/tools/browser_tools/navigate.py
related_tools:
  - browser_call
related_methods:
  - Page.getState
  - Page.list
  - Page.go
  - Page.navigate
  - Page.handleDialog
  - File.handleChooser
  - Download.remove
  - DOM.getAXTree
error_codes:
  - page_changed_during_read
  - page_still_loading
  - page_state_resync_required
  - page_axtree_refresh_required
  - page_settlement_unknown
  - stale_element_reference
  - axtree_page_mismatch
  - axtree_snapshot_invalidated
  - axtree_id_never_seen_on_page
  - axtree_id_not_in_current_snapshot
  - navigation_not_dispatched
  - expectation_pattern_invalid
topics:
  - page lifecycle
  - navigation
  - stale handle
  - page inventory
  - dialog
  - resync
aliases:
  - 页面加载
  - 导航
  - 陈旧目标
  - 元素失效
  - 重新感知
  - 新标签页
---
# Page lifecycle and stale handles

Use this guide when a navigation, click, page inventory, dialog or lifecycle
receipt leaves uncertainty about whether a page is ready or an old target is
still valid.

A real document load requires settlement before DOM/Input; dialog, readiness
and identity gates also apply. After Page.startedLoading or a
receipt with navigationStarted=true, wait for Page.loaded/Page.loadFailed. If
settlement times out, call Page.getState once rather than polling. If Page.go
reports navigationStarted=false, no history move was dispatched: keep the
existing page identity and do not wait for a load that cannot arrive.

Page.navigate, reload, a navigating Page.go, recovered feedback, Page.create,
Page.switchTo, Page.close, Runtime.evaluate, HITL transitions and Input actions
can invalidate epoch-bound AX/DOM ids and geometry. PageId itself remains the
same through navigation and stops being usable only after close, authoritative
replacement, or an authoritative inventory that no longer contains it.

Call Page.list once only when a receipt says pageInventoryChanged or a
navigation-like click did not visibly move the source page. Never re-click
first. Claim the destination in the assigned Fleet, pass the prescribed
navigation_context on its first Page.getState, and then refresh state/AX
evidence. A click gate's short no-navigation observation is not proof of
failure or absence of a popup.

Dialogs are stateful. When the Input action that opened the dialog returns
`dialog.id`, copy that id directly into `Page.handleDialog`. If it did not,
`Page.getState` carries harness-tracked `pendingDialogs`; select the intended id
from that current list when more than one is pending. Refresh state after
handling one. Never retain `Page.handleDialog.userInput` in reasoning,
artifacts, or output.

## File choosers and downloads are not document loads

After `Input.click`, `Input.press`, or `Page.click` activates an upload control,
call `File.handleChooser` with a current target. Do not wait for a chooser event
and do not repeat the activating action. If the target was stale, refresh page
state and DOM targets, then make one chooser call with the fresh target. A
directory chooser requires HITL.

`Download.remove` is record cleanup, not cancellation and not file deletion.
Before removing a record, inspect current download evidence: only completed,
failed, or cancelled records are terminal. Cancel an active record and observe
that terminal state first.

## Refresh identities when the next action uses them

After navigation or recovery, first settle loading and synchronize Page.getState.
Refresh DOM.getAXTree when deriving canonical AX ids or querying find_in_axtree.
Selector-only reads, DOM.getText, DOM.getAttribute and root DOM.getSemanticTree
can run on a settled page without a preceding AXTree. They do not make old AX
ids current. Target identity, ownership and actual readiness checks still apply.

Workflow has the same distinction: navigation settlement followed by
Page.getState is sufficient for a text/selector read or to end the segment.
When later steps use AX ids, derive them from a current tree; a tree call does
not make a hard-coded old id fresh. Workflow action execution and live handle
resolution remain WebCross's responsibility.

If the page's lifecycle generation moved while that tree was being read, the
receipt comes back as `page_changed_during_read` with `stableEvidence: false`.
The call did run, so do not replay it blindly; the tree simply describes a
document you were not asking about. Re-observe the settled page instead of
recording it.

## No dispatch, load failure and snapshot freshness

navigation_not_dispatched means that navigation request was not sent.
navigation_load_failed means a sent load failed; it does not prove that the
previous URL/document survived unchanged. Reconcile current state before
choosing recovery. An expectation mismatch is an arrived page with mismatched
expectations, not a reason to repeat the navigation.

Follow actual freshness receipts: Page.go with navigationStarted=false and a
policy-verified read-only Runtime.evaluate do not themselves invalidate AX.
Harness may accept a newer same-page AX event instead of invalidating that
snapshot. This is not permission to assume an event happened; use the accepted
current snapshot and refresh whenever the lifecycle/AX gate requires it.
Download progress changes alone do not require a page refresh. Dialog,
chooser, navigation and recovery receipts retain their own requirements.
