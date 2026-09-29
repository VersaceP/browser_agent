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
can make the held page view and geometry stale. Node ids die with their
document: a navigation, reload, recovery or new page retires them, while an
Input action on the same document leaves them resolvable. PageId itself remains the
same through navigation and stops being usable only after close, authoritative
replacement, or an authoritative inventory that no longer contains it.

At task entry, honor pinned or explicitly delegated pages. Otherwise, unless
the task explicitly requires a new page, call Page.list in assignedFleetId
before creating one. Prefer a page matching the task's site and purpose only
when claimable=true, busy=false and quarantined=false. Claim with Page.switchTo,
then verify Page.getState and fresh DOM evidence; a matching URL does not prove
login or task state. Create a page if no suitable candidate exists. Do not
navigate an unrelated existing page or inherit another worker's observations.

During execution, call Page.list once when a receipt says pageInventoryChanged
or a navigation-like click did not visibly move the source page. Never re-click
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

## Human decisions when the task no longer determines the next action

Preserve the original target identity, ranking/list evidence, source URL and
actual destination before escalating. Once a settled destination is an advert or
promotion instead of the requested detail, call Hitl.requestPause with that page
and the evidence. Offer skipping this target or supplying a new task based on the
landing page. Do not re-search, re-page, replace the target with the same rank in
a new list, or keep retrying the advertising link. A tracking URL alone does not
prove an advertising landing page; inspect the actual destination first.

Use the same human-decision boundary when fresh evidence leaves a business choice
that the task and prior authorization do not resolve:

- Target validity changed: the original target no longer exists, is unavailable,
  or has materially changed so the requested operation no longer applies. First
  use stable identifiers and available evidence to recover the same target. If
  continuation requires substitution or a changed goal, ask to skip, designate a
  replacement, or revise the task; never silently substitute a different target.
- Target identity is ambiguous: multiple candidates satisfy the description and
  existing context cannot uniquely identify the intended one. First use stable
  identifiers and available context to disambiguate. If different choices would
  affect the result, present their distinguishing facts and ask which to use.
- Material conditions changed: cost, scope, timing, access, obligations or other
  conditions differ from the authorized task. Present the change and its effect
  before taking an action that commits the user to it.
- Existing user content conflicts with the requested write, or continuing would
  overwrite/delete it without authorization: ask to retain, replace or revise.
- A paywall, new terms, identity verification or missing user-only information
  requires a decision or human interaction: explain the exact requirement.
- A required deliverable is unavailable and only a materially different format,
  scope or lower-quality substitute is possible: ask to accept that change or skip.
- Submission outcome remains uncertain after read-only reconciliation and retry
  could duplicate an external effect or persistent change: explain the evidence and ask for
  a decision before replaying the effect.

Use the existing HITL mechanism; keep the affected branch paused until an explicit
resolution arrives. Record a skip as user-directed, not successful extraction or
confirmed_absent. New requirements must be handed to the Lead to update the plan
and contract before resuming. Existing task authorization resolves choices already
made by the user; do not ask again. Stale DOM, schema errors, page-state sync and
bounded recoverable navigation failures are technical recovery, not user decisions.
READ and WRITE path authorization remain separate permissions.

## File choosers and downloads are not document loads

For a known file input, call `File.handleChooser` with its current target;
there is no required preliminary click. If a wrapper must be activated to
expose the input, use one `Input.click`, `Input.press`, or `Page.click` and follow
its file-upload feedback without repeating that action. Do not wait for a
chooser event. If the target was stale, refresh page state and DOM targets.
File assignment does not confirm that the page accepted or uploaded the file;
use the result-bearing page state and, when needed, a permitted
`Network.readApi` read. A directory chooser requires HITL.

`Download.remove` is record cleanup, not cancellation and not file deletion.
Before removing a record, inspect current download evidence: only completed,
failed, or cancelled records are terminal. Cancel an active record and observe
that terminal state first.

## Refresh identities when the next action uses them

After navigation or recovery, first settle loading and synchronize Page.getState.
Refresh DOM.getAXTree when deriving node ids or querying find_in_axtree.
Selector-targeted actions and `DOM.getAXTree` queries by selector can run on a
settled page without a preceding full read. They do not make old ids current. Target identity, ownership and actual readiness checks still apply.

Workflow has the same distinction: navigation settlement followed by
Page.getState is sufficient for a selector-targeted step or to end the segment.
Workflow action execution and live handle resolution remain WebCross's responsibility.
A workflow can read leased observation content with `$cache.observation` or `$last`
and use `transform`; the artifact path field itself is metadata only.

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
