# ABCP Workflow Orchestration Guide

Use `Workflow.execute` for stable subflows where the next steps are known: action sequences, waits, simple branching, bounded loops, event handling, or value extraction. Do not use it for open-ended browsing, visual judgment, CAPTCHA/login/HITL waits, or decisions that require fresh semantic reasoning after every page change.

Inside this harness, model-authored workflows must use
`execute_browser_workflow` (or the equivalently gated raw capability path).
The harness validates action policy, task type, focus events, loop bounds,
timeouts, and lifecycle sequencing before forwarding. It forbids nested
workflows and `Runtime.evaluate` in ephemeral/model-authored workflows.

`System.describeAction` can explain a callable action's params, but it does not teach workflow-only step types (`if`, `loop`, `waitEvent`, `readEvents`, `store`, `transform`) or runtime path/cache behavior. Use this guide for workflow structure; use `System.describeAction` for each action's real params.

## 1. Use Workflow When

- The sequence can be written before execution.
- Failure of a required step should stop the whole subflow.
- `pageId` or `fleetId` should be reused across many action steps.
- Conditions are simple comparisons over `$vars`, `$last`, or `$cache`.
- Loops have a clear max count and the body changes the condition.

Avoid workflow when the agent must inspect unknown content, choose new strategies, use screenshots for judgment, or wait for unpredictable human input.

## 2. Call Shape

Pass `pageId` and `fleetId` at top level when available. Workflow copies them into variables, then action steps auto-inject missing `params.pageId` / `params.fleetId`.

```json
{
  "pageId": "00000000-0000-0000-0000-000000000000",
  "fleetId": "11111111-1111-1111-1111-111111111111",
  "description": "Short intent",
  "variables": { "name": "value" },
  "timeout": 60000,
  "steps": []
}
```

Timeouts are milliseconds. `Workflow.execute` cannot be nested inside workflow steps.

`Workflow.execute` declares exactly these six params. Its action schema is NOT
`.strict()`, so anything else is accepted, forwarded, and dropped without a
word. Two such fields lived here for a long time:

- `stepTimeout` — a live probe sent `stepTimeout: 1000` against a step that then
  ran 5005 ms uninterrupted. There is no per-step budget; the only real one is
  `waitEvent.timeout`.
- `errorConfig` — the identifier appears nowhere in `packages/workflow`.

This asymmetry is worth remembering: a phantom field on a STEP is rejected
loudly with -32602, because every step-union member is `.strict()`. A phantom
field at the TOP LEVEL is dropped in silence.

## 3. Path Rules

Any string starting with `$` is resolved recursively inside params, arrays, objects, conditions, and transform inputs.

- `$foo` or `$vars.foo`: workflow variable `foo`. Variables are strings; `$vars.a.b` means variable key `a.b`, not nested object access.
- `$last.x.y`: nested value from the last successful action result. ABCP action results are usually already unwrapped; do not assume `$last.data`.
- `$cache.axTree.lines`: cached result from `DOM.getAXTree`.
- `$cache.semanticTree`: cached result from `DOM.getSemanticTree`.

Use a path as the whole string value: `{ "id": "$vars.submitId" }`. Do not embed paths inside larger strings like `"button-$id"`; use a `transform` template op instead.

Event payloads are not read through any `$listen` root — there is no such root.
Save them with the event step's `extract`, then use `$vars.someName`.

The roots are exactly `$last`, `$cache`, `$store` and `$vars.NAME`
(`utils/pathResolver.ts:37-62`). **There is no `$steps[N]`**, so a step that
reads an earlier step's result has to sit directly after it, or that result has
to have been extracted into a variable.

An unresolvable reference **throws** `workflow-reference-not-found` and
terminates the workflow; it is not left as a literal `$...` string. A variable
that exists but holds an empty string is a different case entirely — it
resolves fine, and an empty element id travels on until an action rejects it.

## 4. Step Types

**`action`**: runs one ABCP action. Omit `type` when the step has `action`; it defaults to action. Params must match the real action schema. `purpose` is sent to proxied/state-changing actions; if omitted, workflow sends `[Workflow] <ActionName>`. `extract` maps result paths to string variables.

**`if`**: evaluates a condition or group, then runs `then` or optional `else`. Operators: `exists`, `notExists`, `equals`, `notEquals`, `contains`, `notContains`, `matches`, `gt`, `gte`, `lt`, `lte`. Groups use `and` or `or`.

**`loop`**: repeats while its condition is true. The body must change page state or a variable used by the condition; otherwise the loop only stops at `maxIterations`.

**`waitEvent`**: waits for one of `focus` **after** the preceding Action finishes — the engine advances its cursor past that Action's own event window. Optional `timeout` (default 30000). There is no `onTimeout` and no `filter`. Save event data through `extract`.

**`readEvents`**: reads `focus` from the preceding Action's own window and returns immediately.

**`store`**: `op` is `set`, `merge`, `append` or `delete` at a dot `path`. `append` accumulates across loop iterations; the whole store returns with the result.

Focus events (22; the whitelist lives in `harness/workflow_policy.LISTENABLE_EVENTS`): `Page.open`, `Page.close`, `Page.loaded`, `Page.startedLoading`, `Page.loadFailed`, `Page.crashed`, `Page.recovered`, `Page.navigate`, `Page.titleUpdated`, `Page.switchTo`, `Page.dialogOpened`, `Page.dialogClosed`, `File.chooserOpened`, `File.chooserClosed`, `File.operationCompleted`, `File.operationFailed`, `Download.waiting`, `Download.started`, `Download.progressed`, `Download.stateChanged`, `Hitl.paused`, `Hitl.resumed`.

`DOM.axTreeUpdated` is deliberately absent: it appears in the platform's event catalogue but was not observed being emitted under navigation, an explicit tree read, or a wheel scroll. Since a wait that times out is not a failure, waiting on it silently burns the full timeout. `Hitl.humanInput` and `Hitl.resumeEvent` do not exist in the catalogue at all — the HITL resume event is `Hitl.resumed`.

**`transform`**: reads one `$` input, applies ops, writes a variable. Ops: `find` (FIRST matching line/item — no uniqueness check), `regex` (capture group), `jsonpath` (nested property path, not full JSONPath), `template` (`{input}` plus `{varName}`), `querySelector` (against a simplified semantic tree, with an optional match `index`). Transform `input` must be a `$` reference — the platform enforces `^\$.*`. A `find` or `regex` miss resolves to an **empty string, not an error**, so the failure surfaces at whichever later step chokes on it; guard an extracted element id with `matches` on its shape rather than `exists`, because an empty string exists.

## 5. Step Input Shapes

Use these compact JSON shapes; optional fields are marked with `?`.

- **Action**: `{ "action": "Domain.action", "id"?: "...", "params"?: {}, "purpose"?: "...", "extract"?: { "var": "result.path" }, "onError"?: "stop|continue" }`
  - **No `timeout` and no `maxRetries`.** `workflowActionFields` declares neither, and the step union is `.strict()`, so either one rejects the whole workflow with -32602. This cost two model turns on run `8208ed49` before the model dropped the field on its own. The only per-step bound is `waitEvent.timeout`; the only overall budget is the top-level `timeout`.
  - `onError` has no `retry`, and there is no retry setting to move it to. Re-observe and submit a new segment instead: the step failed against a page you have not looked at since.
- **If**: `{ "type": "if", "condition": Cond|Group, "then": Step[], "else"?: Step[] }`
- **Loop**: `{ "type": "loop", "maxIterations": 10, "condition": Cond|Group, "body": Step[] }`
- **WaitEvent**: `{ "type": "waitEvent", "id"?: "...", "focus": ["Page.loaded"], "pageId"?: "...", "fleetId"?: "...", "taskId"?: "...", "timeout"?: 15000, "extract"?: { "var": "events" } }`
  - The step type is `waitEvent`, **not** `listen`: `listen` is not a member of the dispatcher's step union and is rejected with -32602. Events are named in the `focus` array, not a single `event` field, and there is no `onTimeout` — a timeout returns `timedOut: true` and the workflow continues.
  - The wait is gap-safe: an event emitted before the wait begins is replayed from the cursor rather than missed, so no pre-arming is needed.
- **Store**: `{ "type": "store", "op": "set|merge|append|delete", "path": "rows", "value"?: any }`
  - `append` accumulates across loop iterations; the whole store returns with the result alongside `storeRevision`.
- **ReadEvents**: `{ "type": "readEvents", "id"?: "...", "focus": ["Page.loaded"], "pageId"?: "...", "fleetId"?: "...", "taskId"?: "...", "extract"?: { "var": "events" } }`
- **Transform**: `{ "type": "transform", "id"?: "...", "input": "$last.lines", "ops": Op[], "output": "varName" }`
- **Cond**: `{ "path": "$vars.name", "operator": "exists|notExists|equals|notEquals|contains|notContains|matches|gt|gte|lt|lte", "value"?: "x" }`
- **Group**: `{ "operator": "and|or", "conditions": [Cond|Group] }`
- **Op**: `{ "op": "find", "pattern": "...", "mode"?: "contains|regex" }` or `{ "op": "regex", "pattern": "...", "group"?: 1 }` or `{ "op": "jsonpath", "path": "$.x.y" }` or `{ "op": "template", "template": "{input}" }`

## 6. Defaults And Omissions

- If a step has `action` and omits `type`, workflow treats it as an action step.
- If top-level `pageId` or `fleetId` is provided, workflow stores them as variables and injects missing `params.pageId` / `params.fleetId` into action steps.
- If an action step omits `purpose`, workflow sends `[Workflow] <ActionName>`.
- If an action step omits local `onError`, it defaults to `stop`. There is no top-level default to inherit from.
- Action steps have NO `timeout` field. The harness advertised one; it is not in the platform's `workflowActionFields`, and a step carrying it rejects the whole workflow with -32602.
- Top-level `timeout` defaults to `600000` and is the only workflow-wide budget.
- If a `waitEvent` step omits `timeout`, it defaults to `30000`.
- A `waitEvent` timeout is NOT a failure: the step returns `{events: [], timedOut: true}` with `status: "success"` and the workflow continues. Nothing forces a later step to read `timedOut`, so a mandatory wait needs an explicit assertion after it.

## 7. Runtime Behavior

After each action, workflow auto-extracts top-level or nested `data` keys ending in `Id`, plus `scope`, into variables. `DOM.getAXTree` also writes one sample element id to `$vars.exampleId`; treat it as a convenience sample, not a task target.

Perception cache is written only by successful action steps. After `DOM.getAXTree`, workflow stores that result in `$cache.axTree`; after `DOM.getSemanticTree`, it stores that result in `$cache.semanticTree`. `$cache.lastResult` is updated after a successful action step and after `waitEvent`/`readEvents`, but NOT after `if`, `loop`, or `transform` — so a `transform` reading `$last` sees whatever the step before it produced, and a chain of `observe → transform → act → observe → transform` resolves the way you would want.

`$cache.axTree` and `$cache.semanticTree` remain available to later `if`, `loop`, `transform`, and action param resolution until one of these events arrives: `Page.navigate`, `Page.loaded`, `Page.crashed`, `Page.recovered`. On those events, workflow clears both perception caches automatically. Treat `Page.navigate` and `Page.recovered` as DOM-invalidating. After settlement, call `Page.getState` and then `DOM.getAXTree` before using element ids or cached tree lines. After `Page.dialogClosed` or `File.chooserClosed`, call `Page.getState` because resolving the surface may trigger loading or other UI changes.

For optional steps, set `onError: "continue"` locally; the default is `stop`.

Validation happens before execution starts. Workflow rejects nested `Workflow.execute` steps and rejects any `waitEvent`/`readEvents` focus entry outside the whitelist.

Condition comparisons coerce values by operator. `equals` / `notEquals` compare stringified values, `contains` / `notContains` search inside strings or array items, `matches` treats `value` as a regex pattern, and `gt` / `gte` / `lt` / `lte` compare numeric coercions. A condition `value` may itself be a `$...` path.

Common compact patterns: scroll loops should set an initial variable such as `targetFound:"false"`, scroll, refresh `DOM.getAXTree`, transform a search result, then set `targetFound` with a `template` op when found. Dialog flows should click only when a native dialog is predictable, then `waitEvent` on `Page.dialogOpened`, `extract` fields such as `message`, and call `Page.handleDialog` with its real params. If later steps need event data, `extract` it into `$vars` at the event step — `$last` points at the immediately preceding step only.

## 8. Compact Templates

Navigate, wait for load, inspect AX tree:

```json
{
  "pageId": "00000000-0000-0000-0000-000000000000",
  "description": "Open page and inspect accessibility tree",
  "steps": [
    { "action": "Page.navigate", "params": { "url": "https://example.com" }, "purpose": "Open target page" },
    { "type": "waitEvent", "focus": ["Page.loaded"], "timeout": 15000 },
    { "action": "Page.getState", "purpose": "Confirm settled page identity" },
    { "action": "DOM.getAXTree", "purpose": "Read accessible page structure" }
  ]
}
```

Extract a target id from AX lines, then click if found:

```json
{
  "steps": [
    {
      "type": "transform",
      "input": "$cache.axTree.lines",
      "ops": [
        { "op": "find", "pattern": "Submit", "mode": "contains" },
        { "op": "regex", "pattern": "\\[([0-9a-fA-F-]+:\\d+:\\d+)\\]", "group": 1 }
      ],
      "output": "submitId"
    },
    {
      "type": "if",
      "condition": { "path": "$vars.submitId", "operator": "exists" },
      "then": [
        { "action": "Input.click", "params": { "id": "$vars.submitId" }, "purpose": "Click the Submit control" }
      ]
    }
  ]
}
```
