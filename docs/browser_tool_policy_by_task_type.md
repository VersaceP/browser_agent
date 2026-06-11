# Browser Tool Policy By Task Type

This document defines the v1 BrowserAgent tool policy. The policy is owned by
the harness, not by LLM-authored `worker_contract.allowed_methods`.

## Policy Model

- Harness tools are available by default: `final_answer`,
  `record_extraction`, `local_fs_search`, `local_fs_read`, `local_fs_jsonpath`,
  `extract_dom_records`, `eval_js_json`, `navigate_verified`, `visual_verify`.
- ABCP atomic methods are visible/usable according to `task_type` policy.
- `worker_contract.forbidden_methods` always wins.
- Globally forbidden methods are never exposed or executed:
  `DOM.getSemanticTree`, `Hitl.getTaskSummary`, `Hitl.resumeEvent`.
- `strategy_bank.json` does not grant or deny permissions. It recommends a
  strategy, preferred tools, and recovery hints inside the permitted task type
  surface.

## web_search

Use for finding or checking information on webpages without modifying browser
state beyond normal navigation.

Default ABCP surface:

- `System.getCapabilities`, `System.describeAction`
- `Page.create`, `Page.navigate`, `Page.getState`, `Page.close`,
  `Page.screenshot`
- `Input.scroll`, `Input.click`
- `DOM.getAXTree`, `DOM.getText`
- `Runtime.evaluate`
- `Hitl.requestPause`

Disabled domains:

- `Bookmark.*`, `Download.*`, `File.*`, `History.*`, `Memory.*`

## web_scrape

Use for list/detail extraction.

Default ABCP surface:

- `System.getCapabilities`, `System.describeAction`
- `Page.create`, `Page.navigate`, `Page.getState`, `Page.close`,
  `Page.screenshot`
- `Input.scroll`, `Input.click`
- `DOM.getAXTree`, `DOM.getText`, `DOM.getAttribute`
- `Runtime.evaluate`
- `Hitl.requestPause`

Disabled domains:

- `Bookmark.*`, `Download.*`, `File.*`, `History.*`, `Memory.*`

Guidance:

- Prefer `extract_dom_records` for repeated cards, tables, links, and rows.
- Prefer `eval_js_json` over raw `Runtime.evaluate` when structured JS data
  must be returned.
- Use `DOM.getAXTree` for anchors and interaction targets, not as the final
  bulk extraction source.

## form_fill

Use for filling forms, login pages, and submit workflows.

Default ABCP surface:

- `System.getCapabilities`, `System.describeAction`
- `Page.create`, `Page.navigate`, `Page.getState`, `Page.close`,
  `Page.screenshot`, `Page.handleDialog`
- `Input.click`, `Input.type`, `Input.press`, `Input.scroll`
- `DOM.getAXTree`, `DOM.getText`, `DOM.getAttribute`
- `File.handleChooser`
- `Runtime.evaluate`
- `Hitl.requestPause`

Disabled domains:

- `Bookmark.*`, `Download.*`, `History.*`

## download_file

Use when the user explicitly asks to download or inspect a downloaded file.

Default ABCP surface:

- `System.getCapabilities`, `System.describeAction`
- `Page.create`, `Page.navigate`, `Page.getState`, `Page.close`
- `Input.click`, `Input.scroll`
- `DOM.getAXTree`, `DOM.getText`
- `File.download`
- `Download.list`, `Download.getStatus`, `Download.openFolder`
- `Hitl.requestPause`

Disabled domains:

- `Bookmark.*`, `History.*`, `Memory.*`

## browser_state_management

Use only when the user explicitly asks to manage bookmarks, history, downloads,
cookies, or browser state.

Default ABCP surface:

- Relevant `Bookmark.*`, `History.*`, `Download.*`, `Network.*`, `Memory.*`
  methods as required by the user request.
- Normal page navigation and inspection methods remain available.

## Anti-Bot / HITL Escalation

For `web_search`, `web_scrape`, and `form_fill`, `Hitl.requestPause` is part of
the default surface. If `Page.getState`, `DOM.getAXTree`, `Runtime.evaluate`, or
`eval_js_json` repeatedly returns challenge signals such as `cloudflare`,
`captcha`, `turnstile`, or `security verification`, the harness should request
HITL automatically and wait for page recovery.
