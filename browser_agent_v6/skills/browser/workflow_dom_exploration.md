---
name: workflow_dom_exploration
description: How to discover selectors on an unknown page using dom_classes / dom_outline / dom_query, plus selector-failure protocol and defensive JS rules. Read when you don't know what selectors to use on a new page.
---

# DOM exploration playbook

When you land on an unknown page and need to find selectors for repeating
items (cards, rows, list entries) or specific elements (buttons, headings),
use these three tools — in this order:

## 1. dom_classes — find the iteration anchor

`dom_classes(min_count=10)` returns class names appearing ≥ N times. **A class
appearing 50 times is almost certainly the row / card class** for the list.

```
classes = dom_classes(min_count=10)
print(classes[:8])
# Look for entries like {"class_name":"product-card","count":50,"tag_sample":"li", ...}
# That's your iteration anchor.
```

If `dom_classes` returns nothing useful (page is React/SPA with hashed class
names), fall back to step 2.

## 2. dom_outline — fall back when classes are useless

`dom_outline(max_items=50)` samples candidate interactive/text elements in
DOM order. Use to find a specific button / form / nav element by its text or
attributes.

```
outline = dom_outline(max_items=50)
# Look for repeating tag patterns or data-* attributes — those generalize
# even when classes don't.
```

## 3. dom_query — VERIFY before committing to a batch loop

`dom_query(selector, max_items=2)` returns the actual matched elements with
their `text / href / src / value / dataset / inner_html_size`. **Always run
this BEFORE writing a batch loop or run_browser_python loop** — a 0-result
query means the selector is wrong, and you'll save yourself N failed
iterations.

```
probe = dom_query('.your_card_class', max_items=2)
print(probe)
# Confirm href / text / dataset look right before committing.
```

## Selector failure protocol — DO NOT skip

If a selector returns `null` / `None` / empty list, OR if `js()` raises
`'NoneType has no attribute textContent'` (selector matched 0 elements):

| failure # | what to do |
|---|---|
| 1st | Call `dom_outline()`, look at the printed candidates, pick the most likely tag/class for what you want, retry |
| 2nd | Call `dom_query('your_selector_guess', max_items=3)` to confirm structure before writing batch loops |
| 3rd | `screenshot('debug.png')` and either trigger the vision-first recovery (`read_skill('workflow_vision_first')`) OR report to lead — stop guessing |

**Do NOT** call `extract_text` repeatedly with random selectors hoping one
will hit. **Do NOT** write batch loops on a selector you have not verified
with `dom_query` first.

## Defensive JS — REQUIRED inside `js()` expressions

WRONG (will throw NoneType / TypeError when selector misses):
```js
return document.querySelector('.x').textContent          // crashes
return [...document.querySelectorAll('.row')].map(r => r.querySelector('.price').textContent)  // crashes if any row lacks .price
```

RIGHT — always optional-chain and nullish-coalesce:
```js
return document.querySelector('.x')?.textContent ?? null
return [...document.querySelectorAll('.row')].map(r => ({
  price: r.querySelector('.price')?.textContent?.trim() ?? null,
  name:  r.querySelector('.name')?.textContent?.trim()  ?? null
}))
```

## Inside run_browser_python — the helper inventory

You have these functions in scope (no import needed):
```
goto(url), click(sel), fill(sel,text), extract_text, screenshot, scroll,
page_info(), current_url(), current_title(), wait_for_element(sel),
js(expr), list_tabs, switch_tab, new_tab, close_tab, sleep,
accept_dialog, dismiss_dialog,
dom_outline(max_items=30), dom_classes(min_count=3), dom_query(selector, max_items=20),
save_artifact(name, data), read_artifact(name), list_artifacts()
```

Plus stdlib: `json, re, math, time, datetime, pathlib, Path, pd, np`
And exception classes: `NavigationTimeoutError, ElementNotFoundError,
JSResultTooLargeError, PendingDialogError, HumanInterventionRequired,
BlockedImportError`

NEVER `import requests / httpx / urllib` — sandbox-blocked. ALL network
access goes through `goto / click / js` helpers.
