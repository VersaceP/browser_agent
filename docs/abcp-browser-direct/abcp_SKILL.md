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

Default local development uses `ws://localhost:9300/ws`. The repo's `config.json` may set `request_shape: "jsonrpc"`; preserve that value.

If Dispatcher TLS/JWT is enabled, the server expects the JWT in the WebSocket URL query as `?token=<JWT>`. The helper script appends this query token from `jwt_token` or `jwt_token_env` without printing it.

For quick one-off RPC probes:

```bash
python3 docs/abcp-browser-direct/scripts/abcp_rpc.py call System.getCapabilities --params '{"skillFile": false}'
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
        await browser.call("System.getCapabilities", {"skillFile": False})
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
3. Call `System.getCapabilities({"skillFile": false})`; treat returned capabilities and SOP text as authoritative.
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
- Prefer target order: live AXTree canonical id, stable semantic attribute, stable CSS selector, coordinates as last resort. Avoid dynamic hash classes.
- Read AXTree lines as `depth [id] role "label" flags # @x,y,w,h`: `#` marks a preferred actionable target, `@x,y,w,h` is a viewport rect (use it for spatial reasoning only — relative position, overlap, on/off-screen — not for deriving click coordinates; act through the canonical id or a selector), and flags such as `hidden`, `off`, `blocked`, `scroll`, `sticky`, `clip`, or `zN` describe compact layout state. Prefer `#` targets without `hidden`/`blocked`.
- Do not use screenshots to read ordinary text or form values. Use screenshots only for canvas, graphical state, CAPTCHA, layout overlap, or human-audit proof. For visual checks, crop to the element when it can be located (confirm the current `Page.screenshot` element-targeting parameter via `System.describeAction`, then pass `pageId` and a stable selector; omit `options.path` for automatic saving). If element capture fails, do not repeat it; call `Page.getState`, then fall back to a viewport screenshot only if needed.
- Use `Runtime.evaluate` only when DOM/attribute tools cannot access the required data or relationship. Never use it to bypass permissions, casually mutate page state, or replace form interactions.

## Page And Event Loop

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

Avoid workflow for open-ended browsing, CAPTCHA/HITL resolution, visual judgment, or decisions that require fresh reasoning after each page change. If a workflow fails, use the stable `runId` with `Workflow.getStatus` to inspect `failedStepPath`, error, variables, and step results.
