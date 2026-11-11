---
id: browser.observation-evidence
audience: browser
version: "2026-10-08"
description: Read full page views, bounded live queries and saved details; interpret node flags and ids without redundant full-page reads.
sources:
  - harness/observation/axtree_format.py
  - harness/observation/page_observation.py
  - harness/observation/observation_channel.py
  - harness/tools/browser_tools/observation_view.py
  - harness/tools/browser_tools/axtree_state.py
related_tools:
  - browser_call
  - find_in_axtree
  - await_node_change
related_methods:
  - DOM.getAXTree
  - DOM.getDetails
topics:
  - AXTree flags
  - page view
  - bounded query
  - node id
  - observation freshness
  - watch
aliases:
  - AX 节点标记
  - 观察时效
  - 重复读取
  - 节点监控
---
# Observation and evidence

## Full views and details

DOM.getAXTree returns a complete page-view artifact. DOM.getDetails has two
mutually exclusive forms: query for bounded live facts, or version + nodeIds
for complete saved values from that observation. source=snapshot is historical
and does not refresh the page, validate current targets, or feed live watches.
A saved node without complete detail returns an individual detail-not-found;
that does not mean the node or business value is absent from the page.

For standalone browser_call, the Harness verifies full artifacts and exposes
inline DOM.getDetails as response.data.details once; older artifact receipts
use records. Check each ok/error independently.
Inside a JS/TS Workflow, read a full artifact with api.readText and inspect
DOM.getDetails.details directly. A saved-version read never refreshes live state.
Never open a host artifact.path through generic file tools.

Changed full versions deliver complete views, with large views offloaded and
indexed for find_in_axtree. There are no platform diff/change-chain records.
An immutable version already delivered to the worker may be abbreviated as
lines.delivery=unchanged; this does not confirm an action's outcome.

Choose the read by the unresolved fact:

- Discover targets or restore context after navigation: DOM.getAXTree.
- Known targets: DOM.getDetails query.view=state for current values, text for
  rendered text, attributes for requested attributes, or dom for bounded
  structure. Use the live schema's declared defaults and limits.
- Complete truncated values from a saved view: DOM.getDetails with its version
  and nodeIds. Do not combine this with query or call it current live evidence.
- A label or id in the current cached full view: find_in_axtree.
- A delayed result after acting: await_node_change, which uses live queries.

Versions are opaque. pendingChanges=true, freshness=pending or
completeness=partial means evidence may lag or miss a frame; it cannot prove
absence. Resolve the relevant uncertainty and stop once the outcome is known.

Harness axTreeCache reports the local full view's search status: current,
invalidated or unavailable. find_in_axtree only searches that cache. A bounded
live query does not refresh it; invalidation does not retire known document ids
or invalidate current live-query facts. Choose a live query for known targets
or a full read for discovery according to the unresolved question.

find_in_axtree adds viewParentId and scrollAncestorIds, with deduplicated facts
in relatedNodes. These follow the current view's depth and explicit scroll
markers; they are not DOM-parent or business-row assertions. A depth gap leaves
the direct parent unknown, and embedded documents have separate ancestry.
Compare the relevant container's geometry and scroll facts before choosing a
scroll target. A local match count or filtered/rendered subset is not a total;
retain observed row/control identities when checking coverage across regions.

## Node lines

```
depth [n_<16 hex>] role "name" description="…" [flag,…,ev{…}] @x,y,w,h text="…" vis=↓|∅ state{…} scroll{…} attrs{…} rel{…} component=c_… truncated{…}
```

Only depth, id and role are always present; the rest appear in that order when
they apply. Every free-text value is a JSON string. Depth is the node's depth in
this view's tree; each embedded frame is its own document rooted at a
`rootWebArea` and its `@x,y,w,h` boxes are frame-local.

- `@x,y,w,h` is the node's box in VIEWPORT CSS pixels (it follows scrolling).
  Use it for spatial reasoning — relative position, overlap, on/off-screen —
  never to derive click coordinates; act through the id or a selector.
- Flags: `targetable` (the platform can locate it), `actionable` (preferred
  target, marker `#`) or `candidate` (secondary, marker `~`, needs supporting
  evidence), `ignored`, `ev{…}` (why the platform thinks it is interactive).
  State is explicit in both directions: `checked`/`checked=false`/
  `checked=mixed`, `selected`/`selected=false`, `expanded`/`expanded=false`,
  plus `disabled`, `focused`, `required`, `invalid`, `valueRedacted`. An ABSENT
  state means it does not apply to that node, not the negative. find_in_axtree
  reports the negatives as `unchecked`, `unselected`, `collapsed`.
- `vis=∅` (reported as `hidden`): not rendered — never an Input target.
  `vis=↓` (`off`): out of view or clipped — a locator-based Input Action reveals
  such a target itself, so do not pre-scroll it; it is not proof that scrolling
  can reveal it either. `scroll{…}` marks a scrollable container.
- The page view does NOT report occlusion. A missing flag never proves a
  target is clear; a covered target surfaces as the action's occlusion failure.
- `state{value=…}` is an editable control's current runtime value; `attrs{…}`
  is the DOM attribute and may differ. `rel{…}` lists ARIA relations, `parent`
  and `children` the structure. `component=` groups the parts of a composite
  control; flags never substitute for `DOM.inspectSelect`.
- Page-view text is capped at 50 characters. `truncated{…}` names the fields cut
  short; use DOM.getDetails with version + nodeIds for needed saved values,
  or a live query for their current values.
  `valueRedacted` means the complete value is unavailable.

## Node ids

Ids are opaque `n_…` tokens: never parse, shorten or construct one. An id stays
valid for the life of its document — an Input action does not retire it, a
navigation does. After an action the view's CONTENT is stale while a known id
still resolves, or fails with a public stale-target code; if a selector backs it
the platform may answer with `selector-fallback` and a new id. Ids from a file
read are historical and never become current by being read.

## Waiting for a change

`await_node_change(pageId, nodeIds | selector, scope, timeoutSeconds)` is one
call: it registers the watch, blocks until a comparable query changes, a
current sample is available, or the timeout expires, and closes itself. Give
up to 16 ids from your latest view, or a selector the harness
resolves for you — a selector needs no preparatory read. `scope: "node"`
reports name, text, value, checked/expanded/disabled, attributes and removal;
`scope: "subtree"` also reports descendants added, removed or changed.
Geometry, scrolling and focus are ignored. While the watch is active the
harness alternates bounded rendered-text and state/structure queries. It
compares each query only with earlier results from the same surface. When no
earlier comparable query exists, `status: "observed"` supplies the current
sample without claiming a change. Verify the value against your goal; if the
action is still pending, start another watch or inspect a relevant request.
The whole page is re-read only after a continuing probe changes.

Use it when you have acted and the result appears later: content that loads
after a scroll, a control that enables after validation, a list that refills
after a filter, a status that settles on its own. Prefer it over polling with
your own JavaScript, and over re-reading the page in a loop. Do not use it to
wait for a document to load — that is `Page.getState` and the page events —
and do not use it when the answer is already in the view you hold.

`status: "timeout"` means the watch did not observe a change on its queried
surfaces. It is not proof that the page stayed unchanged or an action failed:
inspect `observationEvidence` and read the exact current value when needed.
`background: true` returns at once instead of
blocking, for a wait longer than one call can hold; its changes ride your later
tool results as `watchEvents`, and you close it with `close: true` and its
`watchId`. A watch also closes itself on navigation (`document_changed`), page
loss (`page_unavailable`), expiry (`expired`) or when you finish, and reports
it as a `watch_closed` event.

## Match the read to the unresolved question

A successful read is not an instruction to take another observation. Ask what
fact the next read would establish and whether an existing current receipt
already establishes it. New evidence, incomplete coverage or a changed page can
justify another read; repeated unchanged reads do not establish absence.

A node being visible does not prove WebCross can resolve it for an action.
Inspect the action's failure and current target evidence. Do not label a
resolver/platform failure as missing business content or work around it with an
undocumented execution path.
