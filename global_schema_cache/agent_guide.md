---
name: webcross-browser
description: Operate the WebCross local browser control platform through the webcross CLI, ABCP MCP, or WebSocket. Use this skill for browser navigation, unified page observation, input, events, downloads, HITL, and workflows whenever an ABCP protocol connection is available.
compatibility: Requires a running WebCross User and an available webcross CLI, MCP, or WebSocket connection.
---

# WebCross Browser

Operate WebCross through the user's selected connection and live Action contracts. Use current evidence to choose targets, verify task outcomes, and recover from uncertainty.

## Fleet Scope

Use a task-matched Fleet when one is available. For an ordinary new page, if no suitable Fleet is available, call `Page.create` without `fleetId`; it will select or create the page's Fleet. Call `Fleet.create` only when you need an isolated session, a clean browser instance, or explicit Fleet settings.

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
- Paired use: run `webcross pair` with the pairing information provided by the User. For subsequent CLI calls, select the resulting paired credential with `--profile <path>`. Treat it as sensitive; never print, copy, or record it.
- Output: default to `--output ndjson`. Treat each stdout line as an independent record and dispatch by `type`: `session.ready`, `result`, `event`, or `error`. A one-shot command normally emits `session.ready` followed by `result`; a failed command may emit `error` after `session.ready`. `events watch` continues emitting `event` records. Use `--output human` only for interactive human use.
- Event recovery: follow the cursor procedure in Events and Page Lifecycle. `events read` starts after `session.ready.eventCursor` unless `--from <cursor>` supplies the saved cursor. `events watch` does not save cursors, and `events checkpoint` is unavailable. If the saved cursor is rejected as invalid for the current runtime, retain it separately and use the new `session.ready.eventCursor` as the current baseline. Events before that baseline cannot be replayed from the current runtime.

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

On success and failure, read the Action result, `observation`, and `suggested_prompt`. Distinguish execution acknowledgement from confirmed outcomes, and use each result only within the scope established by the Action contract.

## 2. Tools and Contracts

- Discover callable Actions and Agent-visible events through the active transport. Refresh the affected catalog when it changes.
- Use live schemas for names, arguments, defaults, results, and failures. `System.getCapabilities` provides summaries; describe an unfamiliar Action or event before using it.
- When required, provide a non-empty `purpose` explaining how the call advances the user's goal, and follow `purposeHint`.

### Network request interception

For `Network.setInterception`, `urlPattern` matches the complete request URL. Use an exact URL, or use `*` to match any sequence of characters; all other characters are literal, and matching is case-sensitive. This is not a regular expression: use `*acceptance=token*`, not `.*acceptance=token.*`. `patternsSet` counts patterns accepted for installation, not requests that matched. Confirm the relevant request outcome before relying on a rule.

## 3. Events and Page Lifecycle

The event delivery method depends on the current transport: a connection may push events proactively, or the Agent may read them from a cursor through a dedicated event-reading operation. Do not treat subscription as a universal prerequisite.

Manage event cursors as follows:

1. Save the cursor of the last successfully processed event. A latest-visible cursor or notification is not a consumed cursor.
2. Replay from the saved cursor, continue while `hasMore` is true, and persist `nextCursor` only after processing that batch successfully.
3. Resume from the saved cursor after disconnects, restarts, resource updates, or uncertain delivery. If the cursor is rejected, use the active transport's recovery procedure.
4. Live delivery and replay can overlap. Use cursors to avoid repeating side effects.

Event names are notifications, not Actions. Do not try to call names such as `Page.loaded` or `Hitl.resumed`.

`Page.open` means that a page is registered and visible to the Agent; it starts in `lifecycle="loading"`. Do not run DOM or Input Actions yet. Wait for `Page.loaded`, or poll `Page.getState` until `status="ready"`.

`Page.loaded` means the page exposes a usable main-frame accessibility structure and is ready for DOM/Input actions. Call `DOM.getAXTree` when you need the current page view; page loading does not provide that view automatically. `Page.startedLoading` invalidates current DOM/Input targets. `Page.loadFailed` and `Page.crashed` require inspection with `Page.getState` before recovery.

`Hitl.paused` is page-scoped. Keep the current connection open, stop ordinary automation for that page, and wait for the matching `Hitl.resumed` through the active transport's event listener or cursor-based reads. If delivery is uncertain, replay from the saved cursor without disconnecting. Follow the Human intervention rule before resuming.

`Page.go` reports that history navigation was started; it does not mean the destination document has loaded. Wait for `Page.loaded` or `Page.loadFailed` before querying the page or reusing targets.

`Download.remove` does not cancel active downloads automatically; confirm that the record is terminal before removing it.

When an Input Action returns page dialog information, pass its `dialog.id` to `Page.handleDialog`. If no Action result identifies the dialog and multiple dialogs are pending, use the latest `Page.dialogOpened` event's `dialog.id`. After resolving a dialog, re-observe the page. Never echo or record prompt input text.

### Human intervention

Call `Hitl.requestPause` when a CAPTCHA blocks progress or sign-in, verification, or approval requires the user. Reuse an existing authorized session when possible. For other blockers, request help after two evidence-based recovery attempts make no progress on the same step; never repeat an action with an uncertain effect to reach this limit.

Give a concise `reason` describing the required human action, without credentials or verification codes. After a confirmed pause, follow the HITL waiting rule. Check the current HITL state before retrying an uncertain request; if handoff is unavailable, report the blocker. Do not call `Hitl.resolvePause` merely to bypass waiting or repeat a request the user declined.

`Hitl.resumed` means the pause ended, not that the task succeeded; system lifecycle changes can also end it. Check `Page.getState`, wait for readiness, and verify the required outcome with fresh evidence. Stop waiting if the page closes.

## 4. Observation and Interaction

### Verify outcomes

Before acting, identify the outcome required by the user's goal and where evidence of it should appear. Verify the result-bearing control, collection, or region, which may differ from the interaction target.

- Interpret fields in the context of the node's role, component, and interaction stage. Focus, highlight, selection, expansion, current value, and business completion are distinct.
- Use sufficient direct evidence, including outcomes explicitly confirmed by the Action contract. Refresh evidence when the next decision depends on changed targets or state.
- A missing or redacted field is unavailable evidence. `false` and an empty string are actual values; they establish failure only when their meaning on the relevant object contradicts the required outcome.
- Do not transfer state between same-named semantic and rendered nodes. Establish identity or relationships from current structure and explicit evidence.
- For search or filtering, verify the resulting content or applied-filter state required by the task. For saving or uploading, verify the relevant completion result. A click, closed popup, or injected file alone is insufficient.
- Stop checking once the required outcome is established. If it is pending, wait for relevant events or use bounded observation. If evidence is missing or contradictory, inspect related state and keep the outcome unconfirmed until resolved. An unchanged field alone is not a reason to repeat the action.

### Choose and read observations

Choose observations by the next decision, not a fixed full-read cycle. For custom-dropdown result confirmation, follow the Select-like controls rules below first. Read the selected artifact before using its facts or IDs.

- Full: call `DOM.getAXTree` without `query` to discover targets or restore context after navigation or lost continuity. Read `artifact.path` to establish the baseline.
- Diff: with a usable full baseline and all intervening changes, prefer `diff.artifact.path` when `diff.status="computed"`. Apply it to that baseline; omitted nodes and limited context are not a complete inventory. A removal means leaving the observation, not deletion of business data.
- Query: use bounded queries for known targets: `state` for current values, `text` for displayed selections, `attributes` for attributes, or `dom` for local structure. Specify targets and an appropriate DOM depth. Read the returned `artifact.path`; `mode="detail"` is a result mode, not an input parameter.
- Detail references: read the returned `details` reference only for a needed omitted or truncated field. It belongs to that observation and does not refresh live state. Page-view text is limited to 50 Unicode characters; `valueRedacted` means the complete value is unavailable.

`unchanged` has no diff artifact: do not reread full by default or infer success. Choose another observation using the rules above and the custom-dropdown rules below. If diff is unavailable, read full when rebuilding context is necessary. Query results neither replace the full baseline nor fill gaps in its diff chain.

Version changes require fresh evidence, not necessarily full reads. Do not infer ordering from version strings; targeted queries provide current data independently of full-view changes. Re-observe expired artifacts. Pending or partial observations do not prove absence.

Use `state.value` for an editable control's current value; `attributes.value` may differ. Use `parent` and `children` for structure.

Use `Page.screenshot` for visual facts and for custom-dropdown result confirmation as described below. Start with the smallest relevant region; use a larger capture only when necessary. For other ordinary text, values, or attributes, prefer structured observation.

### Targets and interaction

Use a current `id`, otherwise a unique current `selector`, and coordinates only when a locator cannot reliably address the target. If both `id` and `selector` are supplied, `id` takes precedence.

Node IDs and `targetable` identify locatable nodes; they do not guarantee suitability for an operation. Use current state, interaction evidence, and geometry to choose a target. Hidden semantic nodes can provide state but are not pointer targets. `vis=↓` can indicate an offscreen or clipped node, not necessarily a target that scrolling can reveal. For a pointer failure with public code `target-has-no-interaction-area`, re-observe the page and choose the visible rendered node representing the intended control or drag endpoint; do not retry the same target ID unchanged. `target-not-visible` indicates a visibility, scrolling, reveal, or frame-exposure problem and should be handled through the current viewport and page state. Follow Recovery before retrying a failed Action.

A `pageId` remains the page identity across navigation. After navigation or document replacement, discard old element IDs and geometry. After recovery or other structure changes, reuse targets only when current evidence establishes their identity and usability.

Use real Input Actions. Do not bypass focus, visibility, or coverage checks through script injection, direct DOM mutation, or forced interaction.

### Expandable controls

After opening or updating associated content, use fresh evidence to identify its region, exposed expansion state, and usable targets. Follow explicit relationships and current structure; the region may be outside the control's subtree. Use a bounded `query.view="dom"` for a known region when needed, or full to discover unknown targets. Do not add a query when current evidence is sufficient. If content is not ready, use bounded re-observation rather than fixed delays.

### Scrolling

For a known interaction target, call its locator-based Action directly. Scroll explicitly to discover content, trigger lazy loading, handle nested scrolling, satisfy the user's request, or prepare coordinate interaction.

- Use `Input.scroll` with `container` for deterministic movement of a known container. Use its target form when bringing an element into view is itself the goal.
- Use `Page.wheel` for the root viewport, coordinate-selected or unknown scroll owners, nested or iframe propagation, or real wheel-event semantics. Read its current Schema for the delta/state or boundary form.

### Select-like controls

For supported native controls, use `DOM.inspectSelect` to read the current `type`; a `combobox` role alone does not establish a native `<select>`. For native `<select>`, pass exact `{ value }` entries from inspection in `selections`, respecting selection mode and disabled options; for date, datetime-local, time, month, or week, pass `value` in HTML format and respect the current constraints; for color, use #RRGGBB. Set the value with `Input.select`; never supply both value forms. The confirmed result establishes the control value, not downstream business effects.

For custom dropdowns, apply the `Expandable controls` rule and prefer pointer interaction. Click a known usable option directly; scroll the actual option list only to discover more candidates, then refresh targets. Searchable controls may narrow candidates through typing. Follow the shared scrolling and pointer-target rules. Do not use native Select Actions as a fallback.

Use `Input.press` for keyboard selection only when the pointer path is unavailable and the focused control supports it. Navigate incrementally, inspect the active candidate, and press Enter only when it selects the intended option rather than submitting the form.

After selecting an option in a custom dropdown, confirm the selection as follows:

- If current evidence already establishes the intended selection, continue without another check.
- Otherwise, identify where the selected result is shown. Prefer the owning control’s displayed selection or selected-item collection over the option list.
- If the result is shown outside the popup, use `Page.screenshot` to capture the smallest visible component region containing it, then inspect the image. Do not read HTML, DOM, or AX values solely to reconfirm a selection clearly established by the image.
- If the result is shown inside the popup, do not use a screenshot for confirmation. Use an available current diff if it meets the baseline and continuity requirements above and already provides sufficient evidence; otherwise use a bounded `DOM.getAXTree` query to read the relevant selection state.
- If the screenshot is unavailable or inconclusive, use a bounded query of the result-bearing node. Resolve only the missing or conflicting evidence; do not repeat unchanged reads or reopen the popup solely to reconfirm an established result.

Search text, focus, highlight, the active option, and popup closure alone do not prove selection. Follow Verify outcomes and Recovery if evidence remains insufficient or contradictory. Refresh dependent controls before continuing a cascading selection. Confirming the selection does not confirm form submission or downstream business effects.

### Files and dragging

For a known file input, call `File.handleChooser` directly with the current `pageId`, `files`, and the actual input `id` or `selector`; do not click first or wait for chooser events. If activating a wrapper is necessary, do it once and follow the feedback without repeating the input. Do not substitute the wrapper or label ID for the file input; refresh its identity when needed.

Directory uploads (`uploadFolder`/`openDirectory`) and save choosers follow the Human intervention rule. File injection confirms only that stage; verify subsequent upload completion when the user's goal requires it.

For `Input.drag`, source and destination must be in the same document. Within an iframe, use current IDs from that iframe for both endpoints; cross-frame dragging is unsupported.

## 5. Risk and Data Boundaries

- Before delete, submit, send, download, payment, or another consequential operation, verify the target, scope, final parameters, current state, and authorization already provided by the user.
- Perform search, filtering, pagination, and submission through real page interaction. Do not construct URL parameters to bypass these operations or carry sensitive or bulk data. A complete URL explicitly provided by the user is allowed only when it contains no credentials or sensitive data.
- Do not transfer data through script injection, repeated navigation, or bulk URL parameters. Keep credentials out of URLs, prompts, and logs; use the current Action contract and platform security boundary for sensitive input.
- Bound loops, pagination, and batches, and verify results incrementally. Pause affected actions when the target, obstruction, or intended recipient is uncertain.

## 6. Recovery

After failure, timeout, partial completion, or an uncertain result, establish what already happened before deciding what remains.

Use `Page.getState` when readiness, loading, renderer availability, dialogs, or HITL state is unclear. Once page interaction is permitted, inspect the smallest relevant state needed to resolve the uncertainty. After navigation or document replacement, wait for readiness and restore full context.

Follow the Human intervention rule when recovery makes no progress. Do not retry blindly. Repeat an action only when evidence establishes that its intended effect did not occur, repetition is safe, and the current contract permits it. If the effect may already have occurred, inspect the outcome or report uncertainty instead of duplicating it.

## Workflow authoring

Read `references/workflow-orchestration.md` before creating, changing, executing, or publishing a multi-step Workflow.

Use a `Workflow.execute` request for immediate execution. Use a `webcross-workshop` document for a reusable or importable workflow.
