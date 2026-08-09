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

- In the harness, `fleetId` comes from `<slot_context>.assignedFleetId`; pass it
  explicitly to `Page.create`, then derive `pageId` from that response for
  Page/DOM/Input calls. The browser-tool boundary injects the same assignment
  when `Page.create` omits it and rejects a different or fabricated fleet id.
  Direct ABCP clients must derive handles from live responses and must not
  assume that fleetless `Page.create` selects the intended reusable fleet.
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

- On `Page.startedLoading`, or after navigation/download/state changes, pause
  DOM probes until `Page.loaded` or another settlement event. If no event
  arrives before the timeout, call `Page.getState` once to resynchronize; do
  not poll.
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

Read AXTree lines as `depth [id] role "label" flags # @x,y,w,h`: `#` marks a
preferred actionable target, `@x,y,w,h` is the element's viewport rect (absent
on unpositioned nodes), and flags such as `hidden`, `off`, `blocked`, `scroll`,
`sticky`, `clip`, or `zN` describe compact layout state. Use `rect` for spatial
reasoning only (relative position, overlap, on/off-screen), not for deriving
click coordinates — act through the canonical id or a selector. Prefer `#`
targets without `hidden`/`blocked`; treat `blocked` as occlusion (dismiss the
blocker first) and `scroll` as a scrollable container. (The pixel space of
`@x,y,w,h` is a pending live probe — see the maintainer note below.)

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

When the live `methodSchema` exposes `targets`, combine related reads from one
page into one native call. Both methods return ordered
`response.data.items`. Read every item separately: `DOM.getText` successes use
`item.info.textContent`; `DOM.getAttribute` successes use
`item.info.attributes`, where a missing attribute is `null` and an empty value
is `""`; failures use `item.error`. A failed item does not invalidate its
successful siblings. Fall back to single-target reads on older servers whose
schema does not expose `targets`.

Use `DOM.getImg` only for actual `<img>` assets and only when the live
capability exists. It requires a `targets` batch and `options.path` output
directory. Prefer `imageFormat:"auto"`; successful items return
`item.info.savedPath`. Preserve `fallback-screenshot` receipts, and treat
`not-img-element` as an item error rather than converting arbitrary elements
into screenshots.

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

The only model-facing JavaScript path is
`browser_call({method:"Runtime.evaluate", ...})`. It is a trace-gated last resort for
computed geometry, cross-node relationships, shadow DOM traversal, cross-frame
aggregation, non-DOM state, or legacy cases with no DOM equivalent. Supply the
harness-only `runtime_policy` with intent, effect, a valid `reason_kind`,
`why_structured_tools_insufficient`, `cross_check_plan`, and `result_mode`. The
current page epoch must contain actual attempts of every available structured
alternative (`Page.getState`, AX/Semantic tree, batched text, and batched
attributes); prose claims do not satisfy this gate.

The live Runtime schema must advertise strict `isolated` and `main` worlds, while
every model-authored call must explicitly request `world="isolated"`. Direct
`main`, `auto`, implicit/legacy worlds, and all state-changing scripts are
rejected. Only `non_dom_state` may authorize one harness-controlled strict main
retry: throw `ReferenceError("ABCP_MAIN_WORLD_REQUIRED:<global>")` when the
required page global is absent in isolated world. Ordinary JavaScript errors,
timeouts, and empty successful results never authorize main. With
`result_mode="json"`, pass a JSON-serializable value expression or an invoked
IIFE, never a function body containing top-level `return` or an uninvoked
function value.

Do not use JavaScript for ordinary visible text or attributes that
`DOM.getText`/`DOM.getAttribute` can read. Never use it to bypass permissions,
casually mutate page state, or replace form interactions — form entry goes
through Input-level tools. The harness expression scanner is a conservative
defense-in-depth heuristic, not a JavaScript parser or security sandbox;
ABCP's structured interaction, file, permission, and platform boundaries remain
authoritative.

Frozen skills and harness-internal helpers may not execute Runtime scripts.
They must use native Page/DOM/Input operations so a workflow cannot bypass the
model-facing last-resort evidence gate.

## File Operations

Keep `file_download` and `file_upload` as distinct task types because their
allowed ABCP domains differ. Download phases should validate the browser
completion receipt (`download_completed`) separately from on-disk existence,
size, extension, and optional digest (`file_integrity`). Upload phases should
validate chooser selection (`upload_selected`) separately from a page-observed
success state (`upload_confirmed`). Image exports use `image_exported` plus
`file_integrity` when file contents matter.

## Offload

Large DOM, text, attribute, screenshot, and tool results are auto-offloaded
under the task worktree. In-context payloads keep `savedPath`, `outline`,
`format`, and `query_with`. Follow `query_with` with `local_fs_search` or
`local_fs_read` to inspect offloaded evidence.

## HITL and Visual Checks

Treat login walls, QR/SMS/2FA prompts, CAPTCHAs, and human-verification
challenges as runtime interrupts of the worker that encounters them, not as a
reason to end that worker and spawn a separate auth-probe or HITL worker. A
generic sign-in header is not decisive. When `Page.getState` plus
`DOM.getAXTree` show an authentication/verification surface, concrete auth
controls or methods, and protected content that is blocked or inaccessible,
call `Hitl.requestPause` immediately. Do not add repeated offload reads, a
gate-only artifact, screenshots, or visual verification unless the DOM evidence
is ambiguous, contradictory, or primarily graphical. After resume, synchronize
state, refresh the AXTree, verify access, and continue the original worker task.

After `Hitl.requestPause` succeeds, the harness owns wait/resolve/confirmation.
Do not call `Hitl.*` again. Continue only after `hitl_wait.status == "resumed"`;
for `timeout`, `stale_pause_deadlock`, `still_challenge_after_hitl`,
`browser_error_after_hitl`, or `page_settled_after_hitl`, finalize with a
blocker.
If HITL result or tool feedback includes human-provided input, incorporate it
into task context and update `Memory` if constraints or milestones changed.

Use screenshots and `visual_verify` for visual ambiguity: CAPTCHA, overlays,
canvas/image-heavy UI, layout mismatch, or DOM/visual disagreement. Do not use
VL for bulk data extraction or DOM-readable text. When the element can be
located, crop strictly to the component (selector or canonical id,
`fullPage=false`) instead of viewport/fullpage capture; if element capture
fails, do not repeat it — resync with `Page.getState` and fall back to a
viewport screenshot only if still needed.

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

## Maintainer Note: Pending Live Probes (2026-07-07)

The upstream skillsGuide update documents richer AXTree lines
(`flags # @x,y,w,h`). Items that need a live panel probe before stronger
automation is built on them:

1. **bbox coordinate space** — the new guide says `@x,y,w,h` is CSS pixels; the
   2026-06-27 probe found the bbox space equal to the `Page.screenshot` pixel
   space (2560×1600 on a DPR-2 machine). If the panel switched to CSS px while
   screenshots stay physical px, `harness/vl/locate.py` bbox→id promotion needs
   a DPR conversion. Probe: compare one element's AXTree bbox,
   `getBoundingClientRect`, and screenshot dimensions.
2. **Per-flag semantics** — `off`/`clip`/`zN` meanings are undocumented; pin
   them down before find_in_axtree filtering or auto_intercept uses them.
3. **getSemanticTree shadow-root traversal** — the new guide lists Shadow DOM
   as a valid use, but the 2026-06 probe found `includeShadowDOM` had no
   effect (abcp-panel-quirks #8). Retest on the current build.
