---
id: browser.observation-evidence
audience: browser
version: "2026-09-27"
description: Read the unified page view, its change lists and bounded queries; interpret node flags and ids without redundant full-page reads.
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
topics:
  - AXTree flags
  - page view
  - change list
  - bounded query
  - node id
  - observation freshness
  - watch
aliases:
  - AX 节点标记
  - 观察时效
  - 重复读取
  - 增量
  - 节点监控
---
# Observation and evidence

## One read, three shapes

`DOM.getAXTree` publishes immutable artifact files on the browser host. For a
standalone `browser_call`, the Harness reads and verifies them and puts the
content in the receipt. Inside `Workflow.execute`, the Action returns the raw
artifact reference and summary; a complete `$cache.observation` or `$last`
reference lets the Workflow host read its leased text for `transform`. There
is no inline `records` field to extract inside the workflow. Never open a host
`artifact.path` yourself.

- **Full view** — `lines`: the first read of a page, or whenever continuity is
  lost (navigation, a gap in the version chain, a view the change list cannot
  describe compactly). Large views are offloaded to a file and indexed for
  find_in_axtree.
- **Change list** — `changes` (with `changesSince`): a later read of a page you
  already hold. Only the difference since your version is shown; the complete
  view still goes to disk and to find_in_axtree. A change list is not an
  inventory: unlisted nodes are unchanged, and `= … (context)` lines are
  anchors, not a list of what is visible. A `-` removal means the node left the
  observation, not that business data was deleted. `changesFolded` counts pure
  geometry (nodes scrolled into or out of view, bounds, focus moves).
- **Unchanged** — `lines.delivery: "unchanged"`: nothing changed since your
  version. It does not show that an earlier action succeeded; query the
  specific unresolved values instead of rereading the full view.

Choose the observation by the next decision, not a fixed full-read cycle:

- Discover targets or restore context after navigation or lost continuity: a
  full read.
- Known targets: a bounded query, `query: {view, targets, …}` — `state` for
  current values and control state, `text` for displayed text and selections,
  `attributes` for attributes (`"*"` or a list), `dom` for local structure with
  an appropriate `maxDepth`. Up to 64 targets per query, answered in target
  order as Harness-hydrated `records` for standalone calls. `mode: "detail"`
  is the RESULT mode, not an input. Query
  results do not replace the page view or fill gaps in its change chain.
- A label or id in the view you hold: find_in_axtree.
- Something that changes only after you act: `await_node_change` (below).

Version changes require fresh evidence for the next decision, not necessarily
a full read; targeted queries give current data independently of full-view
changes. Do not infer ordering from version strings. `freshness: pending`,
`pendingChanges: true` or `completeness: partial` means the view may lag or
miss a frame: it never proves absence. Resolve only the relevant uncertainty
and stop once the outcome is established.

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
  short and the receipt's `details` lists nodes whose complete values exist;
  query that node with `text` or `attributes` when you need the full value.
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
