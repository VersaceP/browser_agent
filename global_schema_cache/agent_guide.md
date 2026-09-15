---
name: webcross-browser
description: Operate the WebCross local browser control platform through the webcross CLI, ABCP MCP, or WebSocket. Use this skill for browser navigation, DOM or accessibility-tree inspection, input, events, downloads, HITL, workflows, and recovery whenever an ABCP protocol connection is available.
compatibility: Requires a running WebCross User and an available webcross CLI, MCP, or WebSocket connection.
---

# WebCross Browser

Reliable browser automation follows live feedback, confirms tool contracts dynamically, synchronizes state through events, prefers DOM and accessibility-tree data, and diagnoses failures before retrying.

Operate WebCross through the connection selected by the user. Discover live ABCP contracts before invoking business operations, process events by cursor, and verify observed state before continuing.

## Fleet Scope

Prefer a task-matched Fleet. Record its returned `fleetId` in the current task context or Workflow variables and reuse it for this task. Create a new Fleet only for isolation, a clean session, or when no compatible Fleet exists.

## Connection Quick Reference

Use only the column for the active connection. Names are intentionally transport-specific. The table maps equivalent operations but does not define their arguments; use the live schemas exposed by the current connection.

| Task | CLI | MCP | WebSocket |
| --- | --- | --- | --- |
| Initialize catalogs | `webcross actions list`, then `webcross System.listEvents` | `tools/list` → `system_register` → `system_get_capabilities` → `system_list_events` | `System.register` → `System.getCapabilities` → `System.listEvents` |
| Refresh the Action catalog | `webcross actions list` | `system_get_capabilities`, then refresh `tools/list` | `System.getCapabilities` |
| Refresh the event catalog | `webcross System.listEvents` | `system_list_events` | `System.listEvents` |
| Describe an Action | `webcross actions describe <Action.name>` | `system_describe_action` | `System.describeAction` |
| Describe an event | `webcross System.describeEvent` | `system_describe_event` | `System.describeEvent` |
| Read event history | `webcross events read` | `abcp_read_events` | `events.read` |
| Watch live events | `webcross events watch` | subscribe to `abcp://events` | receive `System.notification`; use `events.watch` for a filtered subscription |
| Invoke an Action | `webcross <Action.name>` | use the exact underscore tool name returned by `tools/list` | use the canonical `<Domain>.<action>` method |

### CLI Identity and Output

- Unauthenticated use: omit `--agent-id` and `--profile` for the first call. Save `agentId` and `eventCursor` from `session.ready.data`, then reuse `--agent-id <agentId>` on later calls. Omitting `--agent-id` starts a new identity. `agentId` identifies the same Agent across calls and event reads; it is not a credential and does not grant access. It is a global option, not part of `--params`, and cannot be combined with `--profile` or `ABCP_PROFILE`.
- Paired use: pass the User's one-time invitation on stdin to `webcross pair`, then use the paired Profile. If `--runtime <path>` is provided, it must point to the current `dispatcher-host.json`, and the Profile must belong to the same Dispatcher. A paired Profile is a CLI credential, not a browser fingerprint Profile; never print, copy, or record it. If the Dispatcher requires authentication, use a paired Profile.
- Output: default to `--output ndjson`. Treat each stdout line as an independent record and dispatch by `type`: `session.ready`, `result`, `event`, or `error`. A one-shot command normally emits `session.ready` followed by `result`; a failed command may emit `error` after `session.ready`. `events watch` continues emitting `event` records. Use `--output human` only for interactive human use.
- Event recovery: `events read` starts after `session.ready.eventCursor` unless `--from <cursor>` is supplied. After successfully processing a batch, save its `nextCursor`; continue while `hasMore` is `true`. `events watch` does not save cursors, and `events checkpoint` is unavailable. After a disconnect or restart, call `events read` with the saved cursor. If the saved cursor is rejected as invalid for the current runtime, do not overwrite it; use the new `session.ready.eventCursor` as the current baseline. Events before that baseline cannot be replayed from the current runtime.

### Registration Gate

Transport authentication establishes the connection but does not complete Agent registration. On a new process-local identity, complete `System.register` before invoking business Actions, reading or watching events, or using the full Action and event directories. MCP initially exposes only `system_register`; after registration, wait for `tools/list_changed` and call `tools/list` again. A Profile-backed identity may retain registration across connections in the same User process; after a process restart or Profile revocation, register again.

### MCP

Use the exact underscore tool names and schemas returned by `tools/list`. Reuse the same MCP session while it remains valid. To release this MCP session, call the exact `system_disconnect` tool once. After the acknowledgement, send no further requests.

Keep executable CLI/MCP snippets copyable: use fenced code blocks, preserve raw URLs and ordinary characters such as `_`, and escape only what the target syntax requires; validate JSON before sending.

Treat updates to `abcp://events` as notifications that newer events may exist. They do not prove that any event has been consumed. Recover the durable event sequence with `abcp_read_events` from the saved cursor.

### WebSocket

Use canonical dot-separated Action methods such as `System.describeAction`. Event transport operations use the lowercase names `events.read`, `events.watch`, and `events.unwatch`.

Pushed events arrive through `System.notification`. Use `events.read` to recover any gap from the saved cursor after reconnecting or when delivery is uncertain.

## 1. Action Feedback

Every Action returns `ActionFeedback`. On both success and failure, read:

- `observation`: the platform's observed current state.
- `suggested_prompt`: the platform's recommended next step or recovery advice.

Base the next operation on the feedback and current page state, not on an expected result. After a failure, timeout, partial completion, or uncertain result, do not immediately repeat the Action.

## 2. Tools and Contracts

- Discover callable operations through the current transport before invoking them. Treat the discovered names and schemas as authoritative for the session.
- When the Action or event directory changes, refresh the corresponding directory and read the current descriptions again before invoking or relying on those operations.
- `System.getCapabilities` returns compact Action summaries only. Use the current transport's Action-description operation for the complete input, result, output, and failure Schema of an unfamiliar Action.
- Use the current transport's event-list operation to discover Agent-visible events. Describe an unfamiliar event before relying on its payload or recommended response.
- Never guess operation names, parameter names, parameter types, event payloads, or resource state.
- When the current Action contract requires `purpose`, provide a non-empty value explaining how the call advances the user's goal, and follow the returned `purposeHint`.
- Treat the current Action schema as the source of truth for parameter shapes, required fields, defaults, and result fields. This guide does not redefine those details.

## 3. Events and Page Lifecycle

The event delivery method depends on the current transport: a connection may push events proactively, or the Agent may read them from a cursor through a dedicated event-reading operation. Do not treat subscription as a universal prerequisite.

Manage event cursors as follows:

1. Save the last cursor whose events were processed successfully.
2. Advance the local cursor only after a pushed event has been handled successfully.
3. Treat a reported latest visible cursor only as a hint that newer events may exist; it is not a consumed cursor.
4. When replaying, read from the saved cursor and continue through every page while `hasMore` is true.
5. Persist `nextCursor` only after the current batch has been processed successfully.
6. After disconnects, reconnects, restarts, resource updates, or uncertain state, resume from the saved cursor.
7. Live delivery and replay may contain the same event. Use cursors to avoid repeating side effects.

Event names are notifications, not Actions. Do not try to call names such as `Page.loaded` or `Hitl.resumed`.

`Page.open` means that a page is registered and visible to the Agent; it starts in `lifecycle="loading"`. Do not run DOM or Input Actions yet. Wait for `Page.loaded`, or poll `Page.getState` until `status="ready"`.

`Page.loaded` means the current main document is ready for DOM/Input automation. Fetch a new `DOM.getAXTree` only when new element targets are needed; do not perform an extra state read first. `Page.startedLoading` invalidates current DOM/Input targets. `Page.loadFailed` and `Page.crashed` require inspection with `Page.getState` before recovery.

`Hitl.paused` is page-scoped. Keep the current connection and event listener open, stop ordinary automation for that page, and wait for the matching `Hitl.resumed`; if delivery is uncertain, replay from the saved cursor without disconnecting. Re-observe the page before continuing. A `pageId` remains the page identity across navigation; element IDs and geometry can become stale after navigation, recovery, or DOM replacement and must be refreshed.

`Page.go` reports that history navigation was started; it does not mean the destination document has loaded. Wait for `Page.loaded` or `Page.loadFailed` before querying the page or reusing targets.

`Download.remove` does not cancel active downloads automatically; confirm that the record is terminal before removing it.

When an Input Action returns page dialog information, pass its `dialog.id` to `Page.handleDialog`. If no Action result identifies the dialog and multiple dialogs are pending, use the latest `Page.dialogOpened` event's `dialog.id`. After resolving a dialog, re-observe the page. Never echo or record prompt input text.

## 4. DOM, Input, and Select Interaction

### Result confirmation

For confirmation, choose the smallest structured Action that directly exposes the fact:

- use `DOM.getText` for rendered text or application messages;
- use `DOM.getAttribute` for HTML or ARIA attributes and attribute-backed state;
- use `DOM.getSemanticTree` for DOM structure, relationships, visibility, frame context, or scroll state when the target or structure is already known;
- use `DOM.getAXTree` to discover unknown targets, obtain a full accessibility map, or inspect accessibility-only interaction evidence;
- use `Page.screenshot` only for visual facts unavailable from structured data.

Do not call `DOM.getAXTree` solely to confirm text, attributes, or known DOM structure. Use only the Actions needed for the current fact. After a page change or uncertain result, follow the single re-observation rule in the recovery section.

Use `Page.screenshot` only for visual properties that structured data cannot represent. Capture the smallest useful scope: element (`id`/`selector`) first, then region (`x`, `y`, `width`, `height`), and viewport or full-page only when broader context is required. Do not use screenshots to read ordinary text, form values, or attributes. If structured-tree retrieval fails, refresh page state and targets before using a screenshot.

Prefer `DOM.getAXTree` as the page map and target-discovery source. Use targets in this order:

1. A current canonical `id` marked with `#` by `DOM.getAXTree` or returned by targeted `DOM.getSemanticTree`.
2. A stable, semantic `selector`.
3. Current-viewport coordinates through `Page.click`.

Use `#` targets as the preferred operation surface. A `~` target requires
additional DOM or visual evidence. `[hidden]` is diagnostic and must not be
operated.
An ID without current interaction evidence provides semantic context rather than a normal Input target until a current DOM observation confirms it.

Each AXTree line uses `depth [id] role "label" [flags...] #|~ @rect`. The
optional flags group keeps a fixed order. Applicable semantic states use explicit
state order: checked state, interaction state, selection state, expansion state,
selection mode, popup, then layout. Values use explicit positive and negative
forms where applicable: `checked`/`unchecked`, `selected`/`unselected`,
`expanded`/`collapsed`, and `multi`/`single`. An absent semantic state means AX
did not expose that state for the node. Interactive nodes may also expose
`enabled` or `disabled`; native `inert` is reported separately as `inert`, and
`popup` is emitted only when true. Layout flags remain sparse evidence:
`hidden off blocked scroll sticky clip zN`; a missing layout flag does not prove
false. These flags describe state and layout but never change the target
confidence expressed by `#` or `~`.

If `DOM.getAXTree` succeeds with `truncatedReason: "partial-frame"`, the
returned nodes are usable for the available frames but one or more child
frames could not be observed. Read `Page.getState`, wait for the page to
settle, and call `DOM.getAXTree` again before relying on the missing frame.
`snapshot_unavailable` is a failed page-map request, not a usable truncation.

`DOM.getSemanticTree` reports one `visibility` state. `visible` means the node
has a positive visible region in its frame but does not prove that the page can
receive the interaction; `not-rendered` cannot be an Input target.

When the current Action schema accepts both `id` and `selector`, they may be supplied together:

- use `id` as the primary target;
- use `selector` as corroboration or as a fallback when the ID is stale;
- if the two references resolve to different targets, stop and refresh page state;
- if the schema makes them mutually exclusive, follow that schema instead of guessing.

For an element target identified by an `id` or `selector`, a locator-based Input
Action may bring the target into view before interaction, including `[off]` and
`out-of-view` targets. Do not pre-scroll a known interaction target.

Do not interact with targets that are hidden, zero-sized, fully transparent, invisible, disabled, or covered by another element. After the target has been brought into view, pause and request confirmation when its effective opacity is below `0.2`. If visibility, opacity, or coverage cannot be determined reliably, treat the state as uncertain and do not force the interaction.

Use explicit scrolling only to discover targets, trigger infinite scroll or lazy loading, operate a nested scroll container, satisfy a user request to change scroll position, or support coordinate-based interaction.

### Scrolling decision

- For deterministic movement of a known scroll container, use `Input.scroll` with `container` and follow the current Action Schema for the exact form.
- Use `Page.wheel` for the root viewport, coordinate-selected scrolling, an unknown scroll owner, nested or iframe propagation, or real wheel-event semantics.
- Before calling `Page.wheel`, read its current Action Schema and choose the delta/state or boundary form; do not infer parameter combinations from this guide.
- Use the `Input.scroll` target form only when bringing the element into view is itself the goal. For a known interaction target, call its locator-based Input Action directly.

For select-like controls:

- If the options or selection mode are unknown, call `DOM.inspectSelect` first.
- Use `Input.select` with the returned semantics: native selects require `{ value }`; supported custom selects accept exactly one returned `{ id }`, `{ label }`, or explicit `{ value }`. Do not convert fields.
- Follow Select feedback for `startOption` and recovery. Re-observe after custom selection or uncertainty; do not blindly retry.

Read `references/select.md` for custom-select and recovery details.

AXTree flags are generic state hints and cannot replace the control kind,
selection mode, and options returned by `DOM.inspectSelect`; use that result
before calling `Input.select`, and inspect again when it is stale.

For file-upload controls:

- when the target is known, call `File.handleChooser` directly with the current `pageId`, `files`, and the actual file-input `id` or `selector`; do not click first or wait for chooser events;
- if input is required to activate a wrapper, do it once, then follow the feedback to `File.handleChooser` without repeating the input; do not reuse a label or wrapper ID, and refresh the known upload structure with `DOM.getSemanticTree` or discover a replacement with `DOM.getAXTree` only when needed;
- directory uploads (`uploadFolder`/`openDirectory`) and save choosers require human intervention; re-observe page state after a successful injection.

For `Input.drag`, element-to-element dragging requires source and destination to be in the same document. For an iframe, provide current element IDs from that iframe for both endpoints; cross-frame dragging is unsupported.

Use real Input Actions for interaction. Do not bypass focus, visibility, or coverage checks through script injection, direct DOM mutation, or equivalent methods.

## 5. Risk and Data Boundaries

- Before delete, submit, send, download, payment, or another irreversible operation, confirm the target, scope, final parameters, and current page state.
- Perform search, filtering, pagination, and form submission through real page interaction. Do not concatenate URL parameters to bypass page operations or carry sensitive or bulk data; a complete URL explicitly provided by the user is allowed only when it contains no credentials or sensitive data.
- Do not use script injection, repeated navigation, or bulk URL parameters to transfer data.
- Do not put credentials in URLs, prompts, or logs. Handle sensitive input only through the current Action contract and platform security boundary.
- Bound loops, pagination, and batch operations, and verify results incrementally.
- Do not use forced interaction to bypass coverage checks. Pause when the target, the reason it is covered, or the intended recipient is uncertain.

## 6. Recovery

When an Action fails, times out, or leaves the result uncertain, do not retry immediately. First check whether the connection, page lifecycle, event delivery, or DOM/AX target changed:

- wait until the page is ready and resolve any blocking HITL or dialog state;
- recover the committed event cursor if delivery may be incomplete;
- refresh page state and the smallest relevant DOM observation when the page or target may be stale; use `DOM.getAXTree` only when a replacement target must be discovered.

Use the Action feedback and stable error code to determine whether the operation succeeded. Retry only after confirming that it did not succeed and that retrying will not duplicate a side effect. The feedback does not prove whether a side effect started or whether retrying is safe.

## Workflow Reference

Before constructing or modifying a multi-step WebCross Workflow over the ABCP protocol, read `references/workflow-orchestration.md`.
