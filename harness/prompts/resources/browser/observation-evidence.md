---
id: browser.observation-evidence
audience: browser
version: "2026-09-15"
description: Interpret AXTree flags, truncated observations and current-epoch evidence without redundant full-page reads.
sources:
  - harness/tools/browser_tools/axtree_state.py
  - harness/observation/page_lifecycle.py
related_tools:
  - browser_call
  - find_in_axtree
related_methods:
  - DOM.getAXTree
  - DOM.getSemanticTree
  - DOM.getText
  - DOM.getAttribute
topics:
  - AXTree flags
  - snapshot
  - canonical id
  - observation freshness
aliases:
  - AX 节点标记
  - 观察时效
  - 重复读取
---
# Observation and evidence

Read AXTree lines as `depth [id] role "label" [flags...] #|~ @x,y,w,h (+N omitted)`. `#` marks a preferred actionable target and `~` is only a secondary locatable candidate that needs extra DOM or visual evidence. An unmarked canonical id is semantic structure, not a normal Input target; `[hidden]` is diagnostic and must not be operated. `@x,y,w,h` is the element's viewport rect (absent on unpositioned nodes) — use it for spatial reasoning (relative position, overlap, on/off-screen), not for deriving click coordinates; act through the canonical id or a selector, never coordinates read off the rect. Depth is the node's depth in the unfiltered tree, so gaps like 0→3 are normal and consecutive lines are NOT contiguous siblings.

ALL flags share ONE bracket group in a fixed order — `[checked enabled]`, `[enabled collapsed single popup]`, `[off]` — never a separate group per flag. Generic AX state comes first and is explicit in BOTH directions: `checked`/`unchecked`/`mixed`, `enabled`/`disabled` (plus `inert` for native inertness), `selected`/`unselected`, `expanded`/`collapsed`, `multi`/`single`, and `popup` only when true. A state that is ABSENT means AX does not expose it for that node — it does not mean the negative, which is why the negative forms exist. Layout flags come last: `hidden`, `off` (out of view), `blocked` (occluded), `scroll` (scrollable container), `sticky`, `clip`, `zN` (stacking order). Layout flags are SPARSE evidence: a missing `blocked`/`hidden` does not prove the target is clear, so never read their absence as a clearance check. Prefer `#` targets showing no `hidden`/`blocked`; treat `blocked` as occlusion (dismiss the blocker first) and `scroll` as the container to scroll in nested-scroll flows. `[off]` is not a problem to solve — a locator-based Input Action reveals such a target by itself, so do not pre-scroll it. Flags never change the `#`/`~` confidence, and they never substitute for `DOM.inspectSelect`: `popup`/`expanded`/`multi` are generic AX hints and carry no `controlKind`, `selectionMode`, or option values.

## Match the read to the unresolved question

- Known current target and field: use a targeted native read. Batch independent
  targets when supported and inspect each item result.
- Need a label/id in a current AX snapshot: use find_in_axtree; do not request
  the entire page tree again merely because its full text was offloaded.
- Snapshot invalidated or page/epoch differs: refresh the required live state
  and AX snapshot. A local file read cannot refresh browser identity.
- Need a historical value or prior attempt evidence: read the relevant bounded
  artifact slice. Label it historical and do not reuse its ids as current.

A successful read is not an instruction to take another observation. Ask what
fact the next read would establish and whether an existing current receipt
already establishes it. New evidence, incomplete coverage or a changed page can
justify another read; repeated unchanged reads do not establish absence.

An AX node being visible does not prove WebCross can resolve it for an action.
Inspect the action's failure and current target evidence. Do not label a
resolver/platform failure as missing business content or work around it with
an undocumented execution path.
