# ABCP Agent Skills Guide

Reliable browser automation follows live feedback, confirms tool contracts dynamically, synchronizes state through events, prefers DOM and accessibility-tree data, and diagnoses failures before retrying.

Use the connection provided by the current transport and read this guide before using ABCP capabilities. The guide is one shared document: MCP exposes it through its guide resource, CLI exposes it with `abcp guide`, and a native WebSocket Agent requests it with `System.getCapabilities({"guide":"content"})` and reads `agentGuide.value`.

## 1. Action Feedback

Every Action returns `ActionFeedback`. On both success and failure, read:

- `observation`: the platform's observed current state.
- `suggested_prompt`: the platform's recommended next step or recovery advice.

Base the next operation on the feedback and current page state, not on an expected result. After a failure, timeout, partial completion, or uncertain result, do not immediately repeat the Action.

## 2. Tools and Contracts

- Discover callable operations through the current transport before invoking them. Treat the discovered names and schemas as authoritative for the session.
- Register first, then retrieve the capability directory. Cache `catalogRevision`, `guideRevision`, and every Action's `actionRevision`; refresh the catalog when `catalogRevision` changes, describe an Action again when its `actionRevision` changes, and reload this guide when `guideRevision` changes.
- `System.getCapabilities` returns compact Action summaries only. Use `System.describeAction` for the complete input, result, output, and failure Schema of an unfamiliar Action; do not infer a union branch from a flattened field list.
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

`Page.loaded` means the current main document has rendered enough for AX perception and is ready for DOM/Input automation. Fetch a new `DOM.getAXTree` only when new element targets are needed; do not perform an extra state synchronization first. `Page.startedLoading` invalidates current DOM/Input targets. `Page.loadFailed` and `Page.crashed` require inspection with `Page.getState` before recovery.

`Hitl.paused` blocks state-changing operations until `Hitl.resumed`; after resuming, refresh the affected state before continuing. A `pageId` remains the page identity across navigation; element IDs and geometry can become stale after navigation, recovery, or DOM replacement and must be refreshed.

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

For page elements targeted by an ID or selector, do not scroll them into view in advance. Standard Input Actions handle the required scrolling, layout stabilization, and interaction checks. Do not add unnecessary waits, scrolling, or repeated DOM reads before a standard Input Action.

Do not interact with targets that are hidden, zero-sized, fully transparent, invisible, disabled, occluded, or not confirmed by hit testing. After the target has been brought into view, pause and request confirmation when its effective opacity is below `0.2`. If visibility, opacity, or occlusion cannot be determined reliably, treat the state as uncertain and do not force the interaction.

Use explicit scrolling only to discover targets, trigger infinite scroll or lazy loading, operate a nested scroll container, satisfy a user request to change scroll position, or support coordinate-based interaction. Coordinates do not provide semantic retargeting, automatic scrolling, or stale-target recovery, so use them only as a last resort and confirm that they are inside the current viewport and pass hit testing.

`Input.scroll` uses mutually exclusive fields rather than a `mode` parameter. Provide `target: { id?, selector? }` to reveal an element through Blink's native scrolling algorithm; do not combine target requests with `direction` or `amount`. Add `container: { id?, selector? }` to establish a hard boundary: it must be the target's real scrolling or clipping ancestor in the same document, and ancestors outside it will not be moved. Provide only `container` with optional `direction` and `amount` to send one wheel transaction to an already visible scrollable container. Provide neither locator to send one wheel transaction to the root viewport. `direction` defaults to `down`, `amount` defaults to `300`, and `amount=0` is valid only for container or viewport requests and reads state without wheel input. Target reveal returns a visibility proof and net movement for affected ancestors. Wheel requests return requested and actual distance, final position, extent, and one of `distance-reached`, `boundary-reached`, or `partial-progress`; the platform does not send compensating wheel events. In container-only mode the container is not revealed implicitly, so reveal the container as a target first if necessary.

Target-resolution and observation failures occur before wheel dispatch. A dispatch, acknowledgement, or settle failure may occur after the page has already changed; inspect the current page and scroll state before deciding whether to retry. When a click reports `target-occluded`, `no-clickable-point`, or `hit-test-unavailable`, inspect the structure covering the target and decide whether that overlay or control must be interacted with, completed, or dismissed; re-observe before retrying.

For select-like controls:

- use `DOM.inspectSelect` when the available choices are unknown. It reports only the currently published option window and may keep a complex popup open for further Agent exploration;
- `Input.select` accepts exactly one of `nativeValues`, `optionIds`, or `optionLabels`. `nativeValues` is only for native HTML selects; the other two are only for options currently published by an open custom popup;
- use `optionIds` for exact current option identities or `optionLabels` for exact, normalized, unique labels. Do not use search text, prefixes, `includes`, old `selections`, `path`, or `selectionMode` fields;
- after `Input.type`, `Input.scroll`, `Input.click`, or any other popup mutation, call `DOM.inspectSelect` or `DOM.getSemanticTree` again. All previous option IDs are stale after a mutation;
- complex search, remote loading, virtual scrolling, and pagination are Agent-composed workflows. `Input.select` never searches, scrolls, paginates, or loads remote options;
- do not interpret an `unknown` capability state as proof that a capability is unsupported; use the Action description, feedback, and current page state to decide;
- when a selection may change dependent fields, page content, or navigation, refresh DOM or AX state to verify the result.

`Input.select` sets the requested final selection set. Treat the returned `selected` collection and explicit `proof` as the completion proof. `DOM.inspectSelect` is read-only for the option values; it restores a popup that it opened when the current option set is complete, and otherwise leaves the popup open with an explicit current-window coverage.

For `Input.drag`, element-to-element dragging requires source and destination to be in the same document. To drag inside an iframe, provide canonical IDs for both endpoints from that iframe. Cross-frame endpoints are unsupported, and an iframe source cannot use coordinate or relative (`dx`/`dy`) destinations because their frame ownership is ambiguous.

After navigation, scrolling, overlays, animations, focus changes, or node replacement, refresh page state and targets instead of reusing stale IDs or coordinates.

Use real Input Actions for interaction. Do not bypass focus, visibility, hit testing, or occlusion checks through script injection, direct DOM mutation, or equivalent methods.

Treat DOM and accessibility-tree results as authoritative for text and target state. Use screenshots only for visual states that structured DOM data cannot represent, not for reading ordinary text, form values, or state attributes.

## 5. Risk and Data Boundaries

- Before delete, submit, send, download, payment, or another irreversible operation, confirm the target, scope, final parameters, and current page state.
- Perform search, filtering, pagination, and form submission through real page interaction. Do not concatenate URL parameters to bypass page operations or carry sensitive or bulk data; a complete URL explicitly provided by the user is allowed only when it contains no credentials or sensitive data.
- Do not use script injection, repeated navigation, or bulk URL parameters to transfer data.
- Do not put credentials in URLs, prompts, or logs. Handle sensitive input only through the current Action contract and platform security boundary.
- Bound loops, pagination, and batch operations, and verify results incrementally.
- Do not use forced interaction to bypass occlusion checks. Pause when the target, the reason for occlusion, or the intended recipient is uncertain.

## 6. Recovery

After an Action fails, times out, or leaves the result uncertain:

1. Do not immediately repeat an operation that may have side effects.
2. Read the Action feedback and inspect current page and execution state.
3. If the connection, page, or target may have changed, recover event state and refresh the DOM/AX targets.
4. Retry only after confirming that the previous operation did not succeed and that the retry will not duplicate a side effect.

Failure responses expose a stable Action error code, a concise observation, and a suggested prompt. Use the code and fresh state to choose recovery; do not expect the failure to declare whether a side effect started or whether an automatic retry is safe.
