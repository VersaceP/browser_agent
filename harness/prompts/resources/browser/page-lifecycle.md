---
id: browser.page-lifecycle
audience: browser
version: "2026-09-04"
description: Reconcile navigation, page lifecycle, dialog, page inventory and stale-handle receipts before another browser action.
sources:
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

Only a real document load blocks DOM/Input. After Page.startedLoading or a
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

Dialogs are stateful. Page.getState carries harness-tracked pending dialog ids;
select the intended id when more than one is pending and refresh state after
handling one. Never retain Page.handleDialog.userInput in reasoning, artifacts
or output.

## The one AX-refresh exemption

After navigation or recovery, `page_axtree_refresh_required` blocks work
because every element id taken before that point may now name something else.
One call is exempt: a `DOM.getSemanticTree` with neither `id` nor `selector`.
That is the schema's document-root request, so it holds no prior element for
the navigation to have invalidated. It runs only on a settled page whose state
resync is already done.

The exemption buys one page-wide look at structure and nothing more. The tree
it returns is a live observation, not stale content - but its ids were never
admitted as the harness's current AX snapshot, so the read discharges nothing
and authorizes no target. The receipt says exactly that in
`axRefreshBypass.doesNotClearAXRefresh`, and `DOM.getAXTree` is still required
before any call that names a target -
`Input.*`, a targeted `DOM.getText`/`getAttribute`, a scoped
`DOM.getSemanticTree`, or `find_in_axtree`, which searches the AX snapshot you
have not refreshed.

If the page's lifecycle generation moved while that tree was being read, the
receipt comes back as `page_changed_during_read` with `stableEvidence: false`.
The call did run, so do not replay it blindly; the tree simply describes a
document you were not asking about. Re-observe the settled page instead of
recording it.
