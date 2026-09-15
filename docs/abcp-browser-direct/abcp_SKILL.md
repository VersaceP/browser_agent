---
name: abcp-browser-direct
description: Directly operate ABCP Browser from Codex through the local WebSocket RPC client, bypassing the Python agent harness. Use when the user asks Codex to drive ABCP Browser itself, inspect or interact with web pages through ABCP methods, debug ABCP actions/events/workflows, or complete browser tasks without `main.py`, `agent_harness.py`, `browser_call`, `spawn_browser_agent`, or harness workers.
---

# ABCP Browser Direct

## Overview

Use this skill to make Codex the browser agent. Drive ABCP Browser directly with `abcp_client.py` and ABCP RPC methods, not through the repository's lead/worker harness.

Keep the harness boundary strict: do not run `main.py`, `agent_harness.py`, `spawn_browser_agent`, `wait_browser_agents`, or harness `browser_call` unless the user explicitly asks for harness behavior.

## Sources Of Truth

- Prefer live `System.getCapabilities` and `System.describeAction` results over docs or cached schemas.
- Use `abcp_client.py` as the direct WebSocket client.
- Read only the `browser` section of `config.json`; do not copy, print, or persist model API keys.
- Use `global_schema_cache/schemas/<Method>.json` only as an offline fallback when ABCP is unavailable.
- Use `docs/agent-skills-guide.md` and `abcp browser/resources/agent-skills.md` as SOP fallbacks if the server does not return `skillsDoc`.
- Read `abcp browser/resources/workflow-orchestration.md` only when using `Workflow.execute`.

## Direct Connection

Default local development uses `ws://127.0.0.1:61168/ws`. The repo's `config.json` may set `request_shape: "jsonrpc"`; preserve that value.

If Dispatcher TLS/JWT is enabled, the server expects the JWT in the WebSocket URL query as `?token=<JWT>`. The helper script appends this query token from `jwt_token` or `jwt_token_env` without printing it.

For quick one-off RPC probes:

```bash
python3 docs/abcp-browser-direct/scripts/abcp_rpc.py call System.getCapabilities --params '{"guide": "omit"}'
python3 docs/abcp-browser-direct/scripts/abcp_rpc.py call Page.create --params '{"url": "https://example.com"}'
```

For multi-step browser work, write or run a small Python driver that keeps one `ABCPClient` connection open:

```python
import asyncio
import json
from abcp_client import ABCPClient, ABCPClientConfig

async def main():
    raw = json.load(open("config.json", "r", encoding="utf-8"))
    cfg = ABCPClientConfig.from_dict(raw.get("browser", {}))
    agent_id = raw.get("browser", {}).get("agent_id", "codex-abcp-direct")

    async with ABCPClient(cfg) as browser:
        await browser.call("System.register", {"agentId": agent_id})
        await browser.call("System.getCapabilities", {"guide": "omit"})
        page = await browser.call("Page.create", {"url": "https://example.com"})
        page_id = page.get("data", page).get("pageId")
        await browser.call("Page.getState", {"pageId": page_id, "purpose": "Confirm page state before reading content"})
        tree = await browser.call("DOM.getAXTree", {"pageId": page_id, "purpose": "Locate readable content and actionable targets"})
        print(json.dumps(tree, ensure_ascii=False, indent=2))

asyncio.run(main())
```

## Startup Sequence

1. Connect with `ABCPClient`.
2. Call `System.register({"agentId": stable_agent_id})` on the same connection before browser work. This is the robust first RPC for local `ws://` connections because the current Dispatcher auth middleware always allows `System.register`.
3. Call `System.getCapabilities({"guide": "omit"})`; treat returned capabilities and SOP text as authoritative.
4. If an action schema is unclear, call `System.describeAction({"method": "Domain.action"})`.
5. Create or recover a page with `Page.create` or `Page.list`; record `fleetId` and `pageId` from `response.data`.
6. Call `Page.getState` before DOM reads or physical input.
7. Use `DOM.getAXTree` as the default perception tool, then target actions with live canonical ids where possible.

When reuse or login state matters, pass an observed `fleetId` explicitly to
`Page.create`. Omitting it delegates selection to the Dispatcher, whose
origin-based selection policy does not guarantee the intended reusable fleet
and may create another one. Use fleetless `Page.create` only when any selected
or newly created fleet is acceptable.

## Action Rules

- Never invent handles. Derive `fleetId`, `pageId`, `downloadId`, bookmark ids, and AXTree ids from live responses.
- Every proxied/state-changing action must include a concrete `purpose` in `params`.
- Do not use placeholder purposes such as `click`, `type`, `continue`, or `do task`.
- Events are not actions. Do not call `Page.loaded`, `Hitl.resumed`, or other event names as methods.
- Prefer target order: live AXTree canonical id, stable semantic attribute, stable CSS selector, coordinates as last resort. Avoid dynamic hash classes. When you hold both an id and a stable selector for the same element, pass both in one locator: ABCP resolves the id first and falls back to the selector in the same dispatch, and `resolvedBy` in the receipt says which answered (anything but `id` means that id was stale). Never invent a selector to complete the pair, and never re-issue the action with the other locator — that is a second real action.
- Read AXTree lines as `depth [id] role "label" [flags...] #|~ @x,y,w,h (+N omitted)`: `#` marks a preferred actionable target, `~` a secondary candidate needing extra evidence, and `@x,y,w,h` is a viewport rect (use it for spatial reasoning only — relative position, overlap, on/off-screen — not for deriving click coordinates; act through the canonical id or a selector). ALL flags share ONE bracket group in a fixed order, not a group per flag: generic AX state first (`checked`/`unchecked`/`mixed`, `enabled`/`disabled`, `inert`, `selected`/`unselected`, `expanded`/`collapsed`, `multi`/`single`, `popup` only when true), then layout (`hidden`, `off`, `blocked`, `scroll`, `sticky`, `clip`, `zN`) — so a real line reads `[checked enabled]` or `[enabled collapsed single popup]`. An absent AX state means AX does not expose it, not the negative; layout flags are sparse, so a missing `blocked`/`hidden` does not prove the target is clear. Prefer `#` targets without `hidden`/`blocked`. `popup`/`expanded`/`multi` are generic hints and never substitute for `DOM.inspectSelect`.
- Do not use screenshots to read ordinary text or form values. Use screenshots only for canvas, graphical state, CAPTCHA, layout overlap, or human-audit proof. For visual checks, crop to the element when it can be located (confirm the current `Page.screenshot` element-targeting parameter via `System.describeAction`, then pass `pageId` and a stable selector; omit `options.path` for automatic saving). If element capture fails, do not repeat it; call `Page.getState`, then fall back to a viewport screenshot only if needed.
- Prefer one native `DOM.getText` or `DOM.getAttribute` call with `targets:[...]` for related reads when the live schema exposes it. Consume ordered `response.data.items` independently; partial item failure is not whole-call failure.
- Use `DOM.getImg` for visual assets when the capability exists: `<img>`, `<picture>`, SVG `<image>`, inline SVG, `<canvas>`, and other visual nodes captured by screenshot fallback. Its selector pierces nested author Shadow DOM. It takes up to 32 batched `targets` (each may carry `id` and `selector`) plus `options.path`; successful items return `info.savedPath`, with `info.method` separating `native-image` from a `fallback-screenshot` (still a real artifact). Point at the visual node itself — a container that merely wraps an image exports as a screenshot of the container.
- `Input.scroll` is a three-mode union with no top-level locator, and EVERY branch requires a locator — the old root-viewport mode is gone, so a locator-less call is rejected `invalid-params`. The branches are `target:{id?,selector?}` (no `direction`/`edge`/`axis`; success is `targetVisible=true`), `container:{id?,selector?}` with `direction`/`amount`, and `container` with `edge:start|end` plus `axis:vertical|horizontal`. Root-viewport and coordinate scrolling is `Page.wheel{pageId,x,y,scrollX?,scrollY?}` or `Page.wheel{...,edge,axis}`; positive `scrollX`/`scrollY` mean right/down. The two Actions name their movement receipt DIFFERENTLY: `Input.scroll` returns `totalDelta` plus `actualDistance`/`requestedDistance` and per-surface `layers[].delta`, while `Page.wheel` returns `observedDelta` against `requestedDelta` and its `layers[]` carry NO `delta`. A zero request on either reads state without moving: `completedReason="state-read"` with the current `position`. Do not repeat a direction after `completedReason="boundary-reached"`.
- `Input.select` takes the select control locator (`id` and/or `selector` - at least one, both allowed, id resolved first) plus a `selections` array of per-option `{value|id|label}` objects, copied verbatim from a fresh `DOM.inspectSelect`. Preserve the field semantics the inspection returned: a native `<select>` needs `value`; a custom select matches on `id`, exact `label`, or an explicitly returned `value` - never convert between them. Read this connection's live `System.describeAction` schema rather than assuming a shape from another build, and hold the entries to exactly the item constraints it states - no more, no less. `DOM.inspectSelect` returns `controlId`, `controlKind`, `selectionMode` and the `options` observed so far. For a custom control the platform can open the menu and walk it with real key presses, so a successful inspect may already have moved the page: treat element ids taken before it as suspect and re-read `DOM.getAXTree`. When the response's `suggested_prompt` asks to keep exploring, repeat the SAME Action with the `startOption` it names (`Input.select` keeps the same `selections`). A custom `Input.select` result reports the choices recorded during its keyboard operation, not a proven final state - verify independently when later behavior depends on it. Only native `<select>`, Ant Design and Element controls have adapters, and `select-target-not-select` reaches you from two different situations - settle which one first. Refresh `DOM.getAXTree` and check whether the real select control is a different node you simply did not target, which is what the platform's own `suggested_prompt` asks; if it is, target that node. Only once the page genuinely has no supported select here is this ordinary UI, traversed with fresh AXTree targets plus one verified `Input.click` per visible level. `select-capability-unavailable` is not that verdict - a capability, page surface, or frame was unavailable - so re-read `Page.getState` and `DOM.getAXTree` and continue once the control is observable again. Never replay a failed select Action: the receipt does not say whether a side effect started, so treat the control as possibly already moved. A direct connection has no platform-side replay block, so that restraint is yours to keep - re-inspect before any further selection on that control, and never send a second `Input.select` merely because the first named the wrong option. Recover by family. For menu-binding failures (`select-popup-not-found`, `select-popup-ambiguous`, `select-popup-not-ready`) go `DOM.getAXTree` -> a page-wide `DOM.getSemanticTree` relating `aria-controls`/`aria-owns`/`aria-activedescendant` to a listbox/menu/option surface, which for a custom control often sits in a portal outside the control's own subtree -> only when the target is visibly on screen and no structured surface can name it, one `Page.screenshot`, which is the graphical-state case screenshots are for - but the receipt returns a saved `path` (or inline `data`), never rendered pixels, so open that image in a viewer that actually puts it in front of you, and if you cannot display it say so instead of reasoning about pixels you never saw; a screenshot you did see confirms only that the menu is rendered and roughly where it sits -> keep hunting for a structured name for what you saw and act through that id or selector; never click a coordinate estimated from the image -> re-observe. For option failures (`select-option-not-in-current-window`, `select-option-label-ambiguous`, `select-option-disabled`) the platform DID enumerate the menu and the request did not match it - a failure receipt carries no option list - so re-inspect and use a field that inspection returned, or continue from the named `startOption`, rather than reading it off a screenshot. `select-options-incomplete` is the opposite case: enumeration itself failed, so inspect again first and fall back to the menu-binding ladder when the menu is visibly on screen and still cannot be enumerated. Option ids are valid only for the menu generation that returned them.
- `Runtime.evaluate` accepts arbitrary page JavaScript, including DOM and page-state writes. `world` is a required parameter in the current schema: `auto` lets ABCP select the execution path, while `isolated` and `main` are strict. Re-observe page state before relying on an effect from a prior script.

## Page And Event Loop

After `Page.startedLoading`, pause DOM probes until `Page.loaded` or another
settlement event. If no event arrives before timeout, call `Page.getState`
once; do not poll.

After navigation, page recovery, popup/page identity changes, dialog closure, file chooser closure, or HITL resume:

1. Stop using old DOM targets.
2. Call `Page.getState`.
3. Refresh `DOM.getAXTree`.
4. Select new live ids before input.

Subscribe to notifications or use `wait_for_notification` when waiting for lifecycle events. A typical event notification arrives as `System.notification` with `params.type == "event"` and `params.data.event`. Discover events with `System.listEvents`; for unfamiliar events call `System.describeEvent({"event": ...})` for meaning, severity, payload, and recommended response.

Before critical clicks, submits, deletes, downloads, or credential entry, call `Page.getState` once to rule out loading, crash, HITL, dialogs, file choosers, or page switches.

## Failure Recovery

After any failed action:

1. Do not repeat the identical call with identical params.
2. Read `observation`, `suggested_prompt`, and error data.
3. Follow `suggested_prompt` unless it conflicts with the user goal or live schema.
4. Call `Page.getState`.
5. If the target may be stale, hidden, disabled, offscreen, or blocked, refresh `DOM.getAXTree`.
6. If params were rejected, call `System.describeAction` for that method.
7. Retry only with new evidence, changed params, or a different strategy.

If ABCP is unavailable, report the connection blocker clearly. Do not fall back to Playwright, CDP, browser screenshots, or generic web tools unless the user explicitly changes the task.

## Workflow.execute

Use `Workflow.execute` only for stable subflows whose sequence is known before execution: navigate, wait, inspect, bounded loops, simple branching, or extraction into variables.

Avoid workflow for open-ended browsing, CAPTCHA/HITL resolution, visual judgment, or decisions that require fresh reasoning after each page change. If a workflow fails, the -32005 error body carries only `failedStepPath`. `Workflow.getStatus` needs a `workflowId` the error body does not include, and returns variable NAMES without values. The complete record is the `Workflow.progress` notification stream.
