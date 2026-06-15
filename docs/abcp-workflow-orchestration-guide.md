# ABCP Workflow Orchestration Guide

Use `Workflow.execute` for stable subflows where the next steps are known: action sequences, waits, simple branching, bounded loops, event handling, or value extraction. Do not use it for open-ended browsing, visual judgment, CAPTCHA/login/HITL waits, or decisions that require fresh semantic reasoning after every page change.

`System.describeAction` can explain a callable action's params, but it does not teach workflow-only step types (`if`, `loop`, `listen`, `transform`) or runtime path/cache behavior. Use this guide for workflow structure; use `System.describeAction` for each action's real params.

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
  "stepTimeout": 15000,
  "errorConfig": { "onError": "stop", "maxRetries": 1 },
  "steps": []
}
```

Timeouts are milliseconds. `Workflow.execute` cannot be nested inside workflow steps.

## 3. Path Rules

Any string starting with `$` is resolved recursively inside params, arrays, objects, conditions, and transform inputs.

- `$foo` or `$vars.foo`: workflow variable `foo`. Variables are strings; `$vars.a.b` means variable key `a.b`, not nested object access.
- `$last.x.y`: nested value from the last successful action result. ABCP action results are usually already unwrapped; do not assume `$last.data`.
- `$cache.axTree.lines`: cached result from `DOM.getAXTree`.
- `$cache.semanticTree`: cached result from `DOM.getSemanticTree`.

Use a path as the whole string value: `{ "id": "$vars.submitId" }`. Do not embed paths inside larger strings like `"button-$id"`; use a `transform` template op instead.

Listen payloads are not read through `$listen`; save them with listen `extract`, then use `$vars.someName`.

## 4. Step Types

**`action`**: runs one ABCP action. Omit `type` when the step has `action`; it defaults to action. Params must match the real action schema. `purpose` is sent to proxied/state-changing actions; if omitted, workflow sends `[Workflow] <ActionName>`. `extract` maps result paths to string variables.

**`if`**: evaluates a condition or group, then runs `then` or optional `else`. Operators: `exists`, `notExists`, `equals`, `notEquals`, `contains`, `notContains`, `matches`, `gt`, `gte`, `lt`, `lte`. Groups use `and` or `or`.

**`loop`**: repeats while its condition is true. The body must change page state or a variable used by the condition; otherwise the loop only stops at `maxIterations`.

**`listen`**: waits for a whitelisted event. `onTimeout` is `stop` or `continue`. `filter` keys are dot paths into the event payload and filter values are literal exact matches; do not use `$vars` in filter values unless you already wrote the concrete value. Save event data through `extract`.

Listenable events: `Page.open`, `Page.close`, `Page.loaded`, `Page.startedLoading`, `Page.loadFailed`, `Page.crashed`, `Page.recovered`, `Page.navigate`, `Page.titleUpdated`, `Page.dialogOpened`, `Page.dialogClosed`, `File.chooserOpened`, `File.chooserClosed`, `Hitl.humanInput`, `Hitl.resumeEvent`.

**`transform`**: reads one `$` input, applies ops, writes a string variable. Ops: `find` (first matching line/item), `regex` (capture group), `jsonpath` (nested property path, not full JSONPath), `template` (`{input}` plus `{varName}`). Transform `input` must resolve from a `$` path. `find` or `regex` misses resolve to an empty string, not an error.

## 5. Step Input Shapes

Use these compact JSON shapes; optional fields are marked with `?`.

- **Action**: `{ "action": "Domain.action", "params"?: {}, "purpose"?: "...", "extract"?: { "var": "result.path" }, "onError"?: "stop|continue|retry", "timeout"?: 1000 }`
- **If**: `{ "type": "if", "condition": Cond|Group, "then": Step[], "else"?: Step[] }`
- **Loop**: `{ "type": "loop", "maxIterations": 10, "condition": Cond|Group, "body": Step[] }`
- **Listen**: `{ "type": "listen", "event": "Page.loaded", "filter"?: { "payload.path": "literal" }, "timeout"?: 15000, "onTimeout"?: "stop|continue", "extract"?: { "var": "payload.path" } }`
- **Transform**: `{ "type": "transform", "input": "$cache.axTree.lines", "ops": Op[], "output": "varName" }`
- **Cond**: `{ "path": "$vars.name", "operator": "exists|notExists|equals|notEquals|contains|notContains|matches|gt|gte|lt|lte", "value"?: "x" }`
- **Group**: `{ "operator": "and|or", "conditions": [Cond|Group] }`
- **Op**: `{ "op": "find", "pattern": "...", "mode"?: "contains|regex" }` or `{ "op": "regex", "pattern": "...", "group"?: 1 }` or `{ "op": "jsonpath", "path": "$.x.y" }` or `{ "op": "template", "template": "{input}" }`

## 6. Defaults And Omissions

- If a step has `action` and omits `type`, workflow treats it as an action step.
- If top-level `pageId` or `fleetId` is provided, workflow stores them as variables and injects missing `params.pageId` / `params.fleetId` into action steps.
- If an action step omits `purpose`, workflow sends `[Workflow] <ActionName>`.
- If an action step omits local `onError`, it inherits top-level `errorConfig.onError`, which itself defaults to `stop`.
- If an action step omits local `timeout`, it uses top-level `stepTimeout`, which defaults to `30000`.
- Top-level `timeout` defaults to `600000`.
- If a `listen` step omits `timeout`, it defaults to `30000`; it does not inherit top-level `stepTimeout`.
- `listen.mode` defaults to `blocking`.
- `listen.onTimeout` defaults to `stop`.
- If a step explicitly sets `onError:"retry"`, workflow retries that step up to 2 times; if retry is inherited from top-level `errorConfig`, workflow uses `errorConfig.maxRetries`.

## 7. Runtime Behavior

After each action, workflow auto-extracts top-level or nested `data` keys ending in `Id`, plus `scope`, into variables. `DOM.getAXTree` also writes one sample element id to `$vars.exampleId`; treat it as a convenience sample, not a task target.

Perception cache is written only by successful action steps. After `DOM.getAXTree`, workflow stores that result in `$cache.axTree`; after `DOM.getSemanticTree`, it stores that result in `$cache.semanticTree`. `$cache.lastResult` is updated only after a successful action step, not after `listen`, `if`, `loop`, or `transform`.

`$cache.axTree` and `$cache.semanticTree` remain available to later `if`, `loop`, `transform`, and action param resolution until one of these events arrives: `Page.navigate`, `Page.loaded`, `Page.crashed`, `Page.recovered`. On those events, workflow clears both perception caches automatically. After a navigation, reload, crash, or recovery, call `DOM.getAXTree` again before using old element ids or old cached tree lines.

For optional or flaky action steps, set `onError:"continue"` or `onError:"retry"` locally. Global `errorConfig` applies when a step has no local `onError`.

Validation happens before execution starts. Workflow rejects nested `Workflow.execute` steps and rejects any `listen.event` that is not in the whitelist.

Condition comparisons coerce values by operator. `equals` / `notEquals` compare stringified values, `contains` / `notContains` search inside strings or array items, `matches` treats `value` as a regex pattern, and `gt` / `gte` / `lt` / `lte` compare numeric coercions. A condition `value` may itself be a `$...` path.

Common compact patterns: scroll loops should set an initial variable such as `targetFound:"false"`, scroll, refresh `DOM.getAXTree`, transform a search result, then set `targetFound` with a `template` op when found. Dialog flows should click only when a native dialog is predictable, then `listen` for `Page.dialogOpened`, `extract` fields such as `message`, and call `Page.handleDialog` with its real params. If later steps need data from `listen`, extract it into `$vars`; do not rely on `$last` for listen payloads.

## 8. Compact Templates

Navigate, wait for load, inspect AX tree:

```json
{
  "pageId": "00000000-0000-0000-0000-000000000000",
  "description": "Open page and inspect accessibility tree",
  "steps": [
    { "action": "Page.navigate", "params": { "url": "https://example.com" }, "purpose": "Open target page" },
    { "type": "listen", "event": "Page.loaded", "timeout": 15000, "onTimeout": "stop" },
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
