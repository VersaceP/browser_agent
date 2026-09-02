---
name: abcp-browser
description: Operate the ABCP local browser control platform through CLI, MCP, or WebSocket. Use this skill for browser navigation, DOM or accessibility-tree inspection, input, events, downloads, HITL, workflows, and recovery whenever an ABCP connection is available.
compatibility: Requires a running ABCP User and an available CLI, MCP, or WebSocket connection.
---

# ABCP Browser

Reliable browser automation follows live feedback, confirms tool contracts dynamically, synchronizes state through events, prefers DOM and accessibility-tree data, and diagnoses failures before retrying.

Operate ABCP through the connection selected by the user. Discover live contracts before invoking business operations, process events by cursor, and verify observed state before continuing.

## Fleet Scope

Prefer a task-matched Fleet. Record its returned `fleetId` in the current task context or Workflow variables and reuse it for this task. Create a new Fleet only for isolation, a clean session, or when no compatible Fleet exists.

## Connection Quick Reference

Use only the column for the active connection. Names are intentionally transport-specific. The table maps equivalent operations but does not define their arguments; use the live schemas exposed by the current connection.

| Task | CLI | MCP | WebSocket |
| --- | --- | --- | --- |
| Initialize catalogs | `abcp actions list`, then `abcp System.listEvents` | `tools/list` → `system_register` → `system_get_capabilities` → `system_list_events` | `System.register` → `System.getCapabilities` → `System.listEvents` |
| Refresh the Action catalog | `abcp actions list` | `system_get_capabilities`, then refresh `tools/list` | `System.getCapabilities` |
| Refresh the event catalog | `abcp System.listEvents` | `system_list_events` | `System.listEvents` |
| Describe an Action | `abcp actions describe <Action.name>` | `system_describe_action` | `System.describeAction` |
| Describe an event | `abcp System.describeEvent` | `system_describe_event` | `System.describeEvent` |
| Read event history | `abcp events read` | `abcp_read_events` | `events.read` |
| Watch live events | `abcp events watch` | subscribe to `abcp://events` | receive `System.notification`; use `events.watch` for a filtered subscription |
| Invoke an Action | `abcp <Action.name>` | use the exact underscore tool name returned by `tools/list` | use the canonical `<Domain>.<action>` method |

### CLI Profile and Output

Pair a User invitation through stdin with `abcp pair`; it contains the local socket and writes the CLI Profile. The CLI registers automatically before normal commands.

To select a runtime explicitly, pass its current `dispatcher-host.json` with `--runtime <path>` and use a Profile from the same Dispatcher.

The CLI pairing Profile identifies and authenticates the Agent connection. It is not a browser fingerprint Profile. Profile selection follows this order: `--profile`, `ABCP_PROFILE`, then `~/.abcp/profiles/default.json`. Treat the Profile as a private credential and do not print, copy, or record its contents.

CLI output defaults to `--output ndjson`. Each stdout line is an independent record whose `type` is `session.ready`, `result`, `event`, or `error`. Do not parse the complete stdout stream as one JSON document. A connected one-shot Action or event command normally emits `session.ready` followed by `result`; a watch command continues with `event` records. Use `--output human` only for interactive human-readable output.

`abcp events read` reads from the committed cursor unless another cursor is selected. After successfully processing a returned batch, commit its safe cursor with `abcp events checkpoint`. Continue while `hasMore` is true. After reconnecting or restarting, resume from the committed cursor.

### Registration Gate

Transport authentication establishes the connection but does not complete Agent registration. On a new process-local identity, complete `System.register` before invoking business Actions, reading or watching events, or using the full Action and event directories. MCP initially exposes only `system_register`; after registration, wait for `tools/list_changed` and call `tools/list` again. A Profile-backed identity may retain registration across connections in the same User process; after a process restart or Profile revocation, register again.

### MCP

Use the exact underscore tool names and schemas returned by `tools/list`. Reuse the same MCP session while it remains valid.

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
- Proxied Actions require a specific `purpose` explaining how the operation advances the user's goal. Follow the exact `purpose` requirement and hint returned by that Action's contract.
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

`Hitl.paused` blocks state-changing operations until `Hitl.resumed`; after resuming, refresh the affected state before continuing. A `pageId` remains the page identity across navigation; element IDs and geometry can become stale after navigation, recovery, or DOM replacement and must be refreshed.

`Page.go` reports that history navigation was started; it does not mean the destination document has loaded. Wait for `Page.loaded` or `Page.loadFailed` before querying the page or reusing targets.

`Download.remove` does not cancel active downloads automatically; confirm that the record is terminal before removing it.

When an Input Action returns native dialog information, pass its `dialog.id` to `Page.handleDialog`. If no Action result identifies the dialog and multiple dialogs are pending, use the latest `Page.dialogOpened` event's `dialog.id`. After resolving a dialog, re-observe the page. Never echo or record prompt input text.

## 4. DOM, Input, and Select Interaction

Prefer `DOM.getAXTree` as the page map and target source. Use targets in this order:

1. The canonical `id` returned by `DOM.getAXTree`.
2. A stable, semantic `selector`.
3. Current-viewport coordinates.

Use the canonical AXTree `id` as the preferred target when executing page Actions.

When the current Action schema accepts both `id` and `selector`, they may be supplied together:

- use `id` as the primary target;
- use `selector` as corroboration or as a fallback when the ID is stale;
- if the two references resolve to different targets, stop and refresh page state;
- if the schema makes them mutually exclusive, follow that schema instead of guessing.

For page elements targeted by an ID or selector, do not scroll them into view in advance. Use the standard Input Action directly and avoid unnecessary waits, scrolling, or repeated DOM reads.

Do not interact with targets that are hidden, zero-sized, fully transparent, invisible, disabled, or covered by another element. After the target has been brought into view, pause and request confirmation when its effective opacity is below `0.2`. If visibility, opacity, or coverage cannot be determined reliably, treat the state as uncertain and do not force the interaction.

Use explicit scrolling only to discover targets, trigger infinite scroll or lazy loading, operate a nested scroll container, satisfy a user request to change scroll position, or support coordinate-based interaction. Coordinates do not provide semantic retargeting, automatic scrolling, or stale-target recovery, so use them only as a last resort and confirm that they are inside the current viewport and usable.

`Input.scroll` uses mutually exclusive schema branches. Select the exact branch from the live Action Schema. A declared container must be the target's real scrolling or clipping ancestor in the same document.

If an Input Action reports that a target cannot be used, inspect the current page and decide whether another element must be completed or dismissed before retrying. Re-observe before retrying.

For select-like controls:

- call `DOM.inspectSelect` when the choices or current selection mode are unknown;
- preserve the returned field semantics: native selects require `value`; custom selects match `id`, exact `label`, or an explicitly returned `value` without converting between fields;
- only when Select feedback requests continued exploration, repeat the same Action and pass back its returned `startOption` (keep the same `selections` for `Input.select`);
- treat custom `Input.select.selected` as the choices recorded during its keyboard operation, and inspect again when later page behavior makes the state uncertain;
- after a failure or uncertain result, inspect the current state before continuing and do not automatically replay an input that may have changed the page.

For file-upload controls:

- after `Input.click` or `Input.press` activates an upload control, call `File.handleChooser` directly with a current upload target reference; do not wait for chooser events or repeat the input;
- refresh the target after stale-id recovery. Directory uploads require human intervention.

For `Input.drag`, element-to-element dragging requires source and destination to be in the same document. For an iframe, provide current element IDs from that iframe for both endpoints; cross-frame dragging is unsupported.

After navigation, scrolling, overlays, animations, focus changes, or node replacement, refresh page state and targets instead of reusing stale IDs or coordinates.

Use real Input Actions for interaction. Do not bypass focus, visibility, or coverage checks through script injection, direct DOM mutation, or equivalent methods.

Treat DOM and accessibility-tree results as authoritative for text and target state. Use screenshots only for visual states that structured DOM data cannot represent, not for reading ordinary text, form values, or state attributes.

## 5. Risk and Data Boundaries

- Before delete, submit, send, download, payment, or another irreversible operation, confirm the target, scope, final parameters, and current page state.
- Perform search, filtering, pagination, and form submission through real page interaction. Do not concatenate URL parameters to bypass page operations or carry sensitive or bulk data; a complete URL explicitly provided by the user is allowed only when it contains no credentials or sensitive data.
- Do not use script injection, repeated navigation, or bulk URL parameters to transfer data.
- Do not put credentials in URLs, prompts, or logs. Handle sensitive input only through the current Action contract and platform security boundary.
- Bound loops, pagination, and batch operations, and verify results incrementally.
- Do not use forced interaction to bypass coverage checks. Pause when the target, the reason it is covered, or the intended recipient is uncertain.

## 6. Recovery

After an Action fails, times out, or leaves the result uncertain:

1. Do not immediately repeat an operation that may have side effects.
2. Read the Action feedback and inspect current page and execution state.
3. If event delivery may be incomplete, recover from the committed event cursor.
4. If the connection, page, or target may have changed, refresh the page and DOM/AX targets.
5. Retry only after confirming that the previous operation did not succeed and that the retry will not duplicate a side effect.

Failure responses expose a stable Action error code, a concise observation, and a suggested prompt. Use the code and fresh state to choose recovery; do not expect the failure to declare whether a side effect started or whether an automatic retry is safe.

## Workflow Reference

Before constructing or modifying a multi-step ABCP Workflow, read `references/workflow-orchestration.md`.
