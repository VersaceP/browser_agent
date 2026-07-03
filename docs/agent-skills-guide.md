# ABCP Agent Skills Guide

This guide is the fallback operating manual when the ABCP server does not
provide `System.skillsDoc`. Treat live capability schemas from
`System.describeAction` and harness tool feedback as the source of truth.

## Core Loop

Every browser action returns feedback. `observation` and `data` are facts.
`suggested_prompt` is recovery or next-step guidance, not proof. Use all three,
but verify against the task contract and method schema before acting.

Never repeat a failed call with identical params. Change parameters, switch
tools, re-perceive current state, or finalize with a blocker.

## Handles

Never invent handles. Use `pageId`, `fleetId`, `downloadId`, `bookmarkId`, and
similar identifiers from the previous action's `response.data`. Keep the current
`pageId` explicit in every Page, DOM, and Input call.

## Calling Tools

Call browser capabilities through `browser_call` with `params` as a JSON object.
Use `{}` for no params. If a tool fails, inspect the attached
`methodSchema.params`; cached schemas live under
`global_schema_cache/schemas/<Method>.json`, not in the task worktree.

Methods with `requiresPurpose: true` can receive an auto-filled `purpose` from
your `browser_call.reason`. Always provide a clear, goal-advancing reason
instead of placeholders like `"click"` or `"type"`. For example:

```
browser_call({
  method: "Input.type",
  params: { pageId, selector: "#username", text: "tom" },
  reason: "Enter the username required by the login step."
})
```

Never guess undocumented params.

## Dynamic Parameters

Treat parameters as live data, not constants. Derive `fleetId`, `pageId`,
`downloadId`, `bookmarkId`, `folderId`, canonical AXTree ids, URLs, selectors,
and critical field values from previous tool feedback, the current task input,
or a `record_extraction` artifact. Do not copy handles, ids, or example
selectors from this guide or from old traces.

Use this source order:

- Handles come from the previous action's `response.data`, for example
  `Fleet.create -> fleetId -> Page.create`, then
  `Page.create -> pageId -> Page/DOM/Input` calls. The ABCP demo uses this
  pattern throughout `abcp browser/packages/demo/src/scenarios/basic/index.ts`.
- Data-tool handles follow the same rule: `Bookmark.createFolder -> folderId`,
  `Bookmark.add/list -> bookmarkId`, and `Download.list -> downloadId`, as shown
  in `abcp browser/packages/demo/src/scenarios/data/index.ts`.
- Interaction targets come from the latest `DOM.getAXTree` or a verified
  selector/attribute. In `/Users/versace/Downloads/index.ts`, the TAAFT
  integration scenario reads `context.axTree.data.lines`, finds the line for a
  link such as `Pros & Cons`, extracts the canonical id from the live AXTree, and
  passes that id into `Input.click`. If the id is absent, refresh perception or
  report a blocker; do not invent a replacement.
- After reveal actions, re-run `DOM.getAXTree` and derive the next text
  selectors from the refreshed state. The same TAAFT scenario clicks section
  anchors, refreshes AXTree, then reads verified sections such as
  `#pros-and-cons`, `#rw_cont`, and `#faq` with `DOM.getText`.
- Element screenshots can crop by CSS selector or by a live AXTree id. The
  `element-screenshot` demo shows both shapes, but `$exampleId` is a placeholder;
  replace it with a canonical id from the current `DOM.getAXTree` result.

If a needed source result was offloaded, inspect `savedPath` with local_fs tools
before deriving the next param. If a sensitive field such as rank, order, price,
score, status, count, or date matters, derive it from page text/attributes or a
recorded artifact; never substitute loop index, visual position, or expected
range as the value.

## Page Lifecycle

Use Page capabilities for creation, navigation, dialogs, screenshots, and page
state. The harness receives browser events automatically and surfaces state
changes through tool results. Follow these behavioral rules:

- After `Page.navigate` or render recovery/recovered feedback: call
  `Page.getState`, then refresh `DOM.getAXTree` before targeting. Treat both as
  DOM-invalidating events.
- After `Page.create` or `Page.switchTo` (including multi-page workflows):
  call `Page.getState` to confirm the active page, then `DOM.getAXTree`.
- On page identity changes (opens, closes, popups): refresh handles with
  `Page.list` or `Page.getState`; stop using closed or stale `pageId`s.
- After dialog or file-chooser closure: call `Page.getState` before continuing,
  because resolution may trigger loading or UI changes.
- Before critical or destructive actions (clicks, submits, deletes): call
  `Page.getState` once to confirm no loading, crash, HITL, dialog, file chooser,
  or viewport shift.
- On `Page.loadFailed`: inspect failure details before retrying.
- On `Page.crashed`: discard stale targets and resync or recreate the page.

Avoid tight `Page.getState` loops that merely hope for change. Act on returned
feedback, tool results, or a fresh semantic perception after a state-changing
Input action.

## Semantic Perception

Use `DOM.getAXTree` as the primary perception tool. It exposes text, roles,
states, controls, and canonical element ids. Use `DOM.getSemanticTree` only for
local diagnostics when AXTree is insufficient and you need tag hierarchy,
complete local bounds, Shadow DOM, or selector debugging; it is heavy (~3.65x
AXTree) and its results are offloaded, so prefer AXTree + focused
`DOM.getText`/`DOM.getAttribute` for routine perception. Both
`DOM.getAXTree` and `DOM.getSemanticTree` return canonical ids of the form
`frameId:axNodeId:domNodeId` (three segments) — copy them verbatim and never
truncate to two segments.

Selector priority: canonical AXTree id > semantic attributes (`aria-label`,
`name`) > stable CSS selector. Avoid dynamic hash classes.

AXTree ids are physical anchors returned by the live page. Use the actual id
returned by the current AXTree with the parameter accepted by the live
`methodSchema` (`id` when the schema supports it, otherwise the supported
selector form). Do not copy example ids from traces or documentation.

## Text and Attributes

Use `DOM.getText` for final visible text. Use `DOM.getAttribute` for non-text
values such as `href`, `src`, `id`, `aria-*`, `data-*`, and `value`. Do not use
screenshots or JavaScript for text/attributes that DOM tools can read.

After text extraction, reject empty or obvious placeholder-only content in any
language. Examples include loading prompts, sign-in prompts, first-comment
prompts, and empty submission forms.

## Interaction

Use Input capabilities for user-like actions. `Input.click`, `Input.type`,
`Input.drag`, `Input.press`, and `Input.scroll` handle focus, scrolling,
and stabilization automatically. Occlusion cleanup may run through
`dismiss_overlay`, but only `Input.click` is eligible for automatic retry of the
original action on the safe path. Prefer canonical AXTree ids. Use stable
selectors as fallback and raw coordinates only when no semantic target exists.
Do not add manual scroll/wait before standard interactions; manually scroll only
nested scroll containers or lazy-loading flows.

After click, type, press, or scroll, verify effects through the cheapest
reliable signal: read `ActionFeedback`; if navigation/loading/page-identity
changes may occur, wait or resync via `Page.getState`; otherwise use
`DOM.getText`, `DOM.getAttribute`, or refreshed `DOM.getAXTree` for in-page
text, attributes, or feedback.

## Structured Output

Persist reusable data with `record_extraction`. Row keys must match the
expected artifact fields exactly. Critical fields should include provenance such
as `sourceTool`, `sourceSelectorOrAxId` or selector, `pageUrl`, and observed
evidence text/attribute when applicable.

Only data persisted through `record_extraction` counts as verified handoff data.
Final answers should reference extraction `savedPath` values instead of inlining
large row sets.

## JavaScript Fallback

`eval_js_json` is a last resort for computed geometry, cross-node
relationships, shadow DOM traversal, cross-frame aggregation, non-DOM state, or
legacy cases with no DOM equivalent. Provide a valid reason kind, a concrete
explanation, and a DOM cross-check plan.

Do not use JavaScript for ordinary visible text or attributes that
`DOM.getText`/`DOM.getAttribute` can read.

## Offload

Large DOM, text, attribute, screenshot, and tool results are auto-offloaded
under the task worktree. In-context payloads keep `savedPath`, `outline`,
`format`, and `query_with`. Follow `query_with` with `local_fs_search` or
`local_fs_read` to inspect offloaded evidence.

## HITL and Visual Checks

After `Hitl.requestPause` succeeds, the harness owns wait/resolve/confirmation.
Do not call `Hitl.*` again. Continue only after `hitl_wait.status == "resumed"`;
for `timeout`, `stale_pause_deadlock`, `still_challenge_after_hitl`,
`browser_error_after_hitl`, or `page_settled_after_hitl`, finalize with a
blocker.
If HITL result or tool feedback includes human-provided input, incorporate it
into task context and update `Memory` if constraints or milestones changed.

Use screenshots and `visual_verify` for visual ambiguity: CAPTCHA, overlays,
canvas/image-heavy UI, layout mismatch, or DOM/visual disagreement. Do not use
VL for bulk data extraction or DOM-readable text.

## Recovery

After an action failure:

1. Do not submit the identical failing action again.
2. Read failure `ActionFeedback`; prioritize `suggested_prompt`.
3. Call `Page.getState` to rule out loading, crash, HITL, dialog, or file chooser.
4. If the target may be stale, hidden, disabled, or blocked, refresh with
   `DOM.getAXTree`.
5. If auto-scroll reports out-of-bounds or invisibility, query and scroll the
   nearest scrollable parent container.
6. If repeated attempts fail, change strategy or finalize with a blocker.
