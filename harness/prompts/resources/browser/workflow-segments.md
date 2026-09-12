---
id: browser.workflow-segments
audience: browser
version: "2026-09-11"
description: Write one execute_browser_workflow segment instead of a run of single calls, and decide what to do when a segment fails.
sources:
  - harness/tools/browser_tools/dispatch.py
  - harness/tools/browser_tools/schemas.py
  - harness/workflow_policy.py
  - harness/observation/exec_observer.py
related_tools:
  - execute_browser_workflow
  - browser_call
related_methods:
  - Workflow.execute
  - Workflow.getStatus
---

# Segments

A segment is the run of actions from where you are now to the next point where
you genuinely have to look before deciding. Submit it as one
`execute_browser_workflow` call instead of one `browser_call` per action.

The end of a segment is where you get control back, so there is no mid-workflow
"ask the model" step and none is needed: if you cannot predict what comes next,
that is where the segment ends.

## Step types

Only these exist. They are the platform's own names — anything else is rejected
before the workflow starts.

| type | what it does |
| --- | --- |
| `action` (default; `type` may be omitted) | one ABCP action, with `params`, `purpose`, `extract`, `onError` |
| `waitEvent` | wait for `focus: [...]` AFTER the preceding Action, with a `timeout` |
| `readEvents` | read `focus: [...]` from the preceding Action's OWN window; returns immediately |
| `store` | `op` `set` / `merge` / `append` / `delete` at `path` |
| `if` | `condition` + `then` / `else` |
| `loop` | `maxIterations` + `condition` + `body` |
| `transform` | `find` / `regex` / `jsonpath` / `template` / `querySelector` over `input` → `output` |

`onError` is `stop` (default) or `continue` — **never `retry`**. There is no
retry setting anywhere in the workflow language: a step that failed did so
against a page you have not re-observed, so retrying it blind is the wrong move
anyway. Read the failure receipt, re-observe, submit a new segment.

## A filled-form segment

Everything here is decided in advance, so it is one segment. It ends right after
the results land, because what to do with them depends on what they are.

```json
[
  {"action": "Input.type", "params": {"pageId": "$vars.pageId", "id": "$vars.keywordId", "text": "$vars.keyword"},
   "purpose": "Enter the search keyword", "onError": "stop"},
  {"action": "Input.click", "params": {"pageId": "$vars.pageId", "id": "$vars.regionId"},
   "purpose": "Open the region control", "onError": "stop"},
  {"action": "DOM.getAttribute", "params": {"pageId": "$vars.pageId", "id": "$vars.keywordId", "name": "value"},
   "purpose": "Read the keyword field back", "onError": "stop", "extract": {"keywordEcho": "value"}},
  {"action": "Input.click", "params": {"pageId": "$vars.pageId", "id": "$vars.submitId"},
   "purpose": "Submit the search", "onError": "stop"},
  {"type": "waitEvent", "focus": ["Page.loaded", "Page.loadFailed"], "timeout": 20000},
  {"action": "Page.getState", "params": {"pageId": "$vars.pageId"},
   "purpose": "Confirm where the submit landed", "onError": "stop", "extract": {"landedUrl": "url"}},
  {"action": "DOM.getAXTree", "params": {"pageId": "$vars.pageId"},
   "purpose": "Refresh element identity for the results page", "onError": "stop"}
]
```

`keywordEcho` comes back in the receipt: compare it to what you asked for. The
workflow cannot judge whether a value is the right one — it only reads it back
for you.

## Driving a control you cannot address yet

A menu's options do not exist until you open it. Nothing written before that
click can address one: not a canonical id you have never seen, and not a CSS
selector either, because CSS cannot match on visible text. The observation is
not optional.

What *is* optional is paying a model turn for it. Put the observation inside the
segment: **act, read, search the reading, act on what you found.**

References resolve against `$last` — the immediately preceding step's result —
plus `$cache`, `$store` and `$vars.NAME`. There is no `$steps[N]`, so a
`transform` that searches a reading has to sit directly after it.

```json
[
  {"action": "Input.click", "params": {"pageId": "$vars.pageId", "id": "«the control you already located»"},
   "purpose": "Open the control", "onError": "stop"},
  {"action": "DOM.getAXTree", "params": {"pageId": "$vars.pageId"},
   "purpose": "Read what the click revealed", "onError": "stop"},
  {"type": "transform", "input": "$last.lines", "output": "targetId",
   "ops": [
     {"op": "find", "mode": "regex", "pattern": "«a pattern that matches exactly one line»"},
     {"op": "regex", "pattern": "\\[([0-9]+:[0-9]+:[0-9]+)\\]", "group": 1}
   ]},
  {"action": "Input.click", "params": {"pageId": "$vars.pageId", "id": "$vars.targetId"},
   "purpose": "Act on the match", "onError": "stop"},
  {"action": "DOM.getAXTree", "params": {"pageId": "$vars.pageId"},
   "purpose": "Verify the control now reads as chosen", "onError": "stop"}
]
```

This is framework-agnostic on purpose: the search runs over the tree you just
captured and matches on what a person would read. It never touches a class name
or a DOM path, so it behaves the same on any front end that renders its labels.

Repeat the middle three steps to walk a chain — a cascading picker, a date
picker drilling year → month → day — all in one call.

### Making the pattern match exactly one line

A bare label is rarely unique in a page map. The same text appears on the
control, on the control's own label, and in any heading that mentions it. Each
AXTree line carries more than the name: a role, the full quoted accessible
name, and a bracketed state. Use `mode: "regex"` and require all three.

Read the role off the tree you are holding. Do not assume which role a control
uses — a checkbox is not always a `checkbox`, and a chooser is not always a
`combobox`. Look at how *this* page rendered it, then pin that.

Anchoring the name with its quotes is what separates a control from a heading
that merely mentions it.

### Two ways this fails quietly

`find` reports neither problem:

- **No match** → it yields an empty string, which flows on until some later step
  chokes on it. The failure then names that later step, not the bad pattern. The
  receipt's `variablesAtFailure` is where you see which variable came back empty
  — that is the one whose pattern was wrong.
- **Several matches** → it takes the first one, silently. The segment reports
  success having acted on the wrong element.

So: **end every such segment with a read that shows the effect**, and check it
in the receipt. A click on the wrong element and a click on the right one are
indistinguishable until you look at the result.

Do not wrap the acting step in an `if` that skips when the search came back
empty. A skipped step makes the segment succeed having done nothing, which is
worse than a failure — let the empty value fail the step, and read the receipt.
Where you do need a guard, test the shape rather than presence:
`{"path": "$vars.targetId", "operator": "matches", "value": "^[0-9]+:[0-9]+:[0-9]+$"}`.
`exists` is no use here: an empty string exists.

Anything you must not lose, `extract` into a variable or `store` it **before**
the step that might fail. A failed segment hands back variables; it does not
hand back the store.

### Where this stops

It works whenever the decision can be made from a page map. When the decision
needs eyes — a canvas, an image, a layout question, anything you would want a
screenshot for — the segment ends at the screenshot. Look, then submit the next
segment. There is no way to bring a visual judgement inside a workflow.

## Collecting rows

`store` with `op: "append"` accumulates across loop iterations, and the whole
store returns with the result.

```json
{"type": "loop", "maxIterations": 20,
 "condition": {"path": "$vars.nextUrl", "operator": "exists"},
 "body": [
   {"action": "DOM.getText", "params": {"pageId": "$vars.pageId", "selector": "$vars.rowSelector"},
    "purpose": "Read the current row", "onError": "stop", "extract": {"rowText": "text"}},
   {"type": "store", "op": "append", "path": "rows", "value": "$vars.rowText"}
 ]}
```

Persist what you accept with `record_extraction`; the workflow only fills
variables and the store.

## Events: two halves, and a trap

`waitEvent` starts looking **after** the preceding Action finishes — the engine
moves its cursor past that Action's own event window. `readEvents` reads exactly
that window, and returns immediately.

A real navigation fires `Page.loaded` after `Page.navigate` returns, so
`waitEvent` settles it (measured: 527ms on a first visit, 3ms on a cached
revisit, while `readEvents` came back empty). Reach for `readEvents` when the
event may already have fired inside the Action — not as a mandatory prefix.

**The trap**: a `waitEvent` timeout is not a failure. It returns
`{"events": [], "timedOut": true}` and the segment continues. Waiting on an
event the page never emits costs the entire timeout and yields nothing to act
on. Only the names in the step schema's `focus` enum are accepted — that enum
is the list of events this deployment was actually observed emitting.

## When a segment fails

The receipt carries the state as of the failure, rebuilt from the platform's own
progress stream:

- `failedStepPath` — which step, e.g. `steps[4]` or `steps[2].then[1]`
- `failedErrorCode` — e.g. `target-not-found`
- `completedSteps` — every step that did succeed, with timings
- `variablesAtFailure` — the variable VALUES at that moment
- `executionTrace.pageEvents` — what the page did during the run

What the receipt does **not** carry yet: the workflow `store`'s contents and the
per-step `result` data. If a segment appended 7 rows and failed on the 8th, the
receipt proves the store changed but cannot hand those 7 rows back. Until that
gap closes, extract anything you must keep into workflow VARIABLES (which do
come back) rather than relying on the store alone across a possible failure.

Choose from what it says:

| choice | when it applies | check first |
| --- | --- | --- |
| rerun the whole segment | read-only work whose starting point still holds | nothing irreversible already ran |
| rerun with remaining inputs | the segment is parameterized over rows/pages | which inputs are still outstanding |
| continuation segment | you can express what is left as its own segment | the variables it needs are in `variablesAtFailure` |
| drop back to single calls | the rest still needs exploring | keep the rows already collected |

Two rules that outrank convenience:

1. **Do not slice at `failedStepPath` mechanically.** A step inside a loop or a
   branch carries iteration state, branch conditions and variable setup that a
   bare tail would silently lose.
2. **Dispatched is dispatched.** Anything the failed segment already sent
   happened. A submit that failed on the step AFTER it still submitted — verify
   from the page before repeating it, and never re-run an irreversible action to
   "make sure".

## Sizing

Short segments cost one model turn to resume. A long segment on a page you have
not verified costs a wrong path executed to completion. When unsure, cut it
shorter — the receipt tells you what the page actually did, and the next segment
starts from fact instead of assumption.
