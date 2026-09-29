---
id: browser.workflow-segments
audience: browser
version: "2026-09-27"
description: Author bounded Workflow segments with observation references, deterministic transforms, event synchronization and failure recovery.
sources:
  - abcp-platform/resources/skills/webcross-browser/references/workflow-orchestration.md
  - harness/tools/browser_tools/schemas.py
  - harness/workflow/workflow_policy.py
related_tools:
  - execute_browser_workflow
  - execute_saved_browser_workflow
  - browser_call
related_methods:
  - Workflow.execute
  - Workflow.getStatus
---

# Segments

Use Workflow only when its execution tool is exposed and enabled for this worker.
Choose a bounded sequence whose actions, conditions and stopping rules are known.
A segment may observe the page and mechanically select a target. End it when the
next decision needs model judgment, a screenshot, a Harness-only tool, artifact
expiry recovery or failure diagnosis. Runtime.evaluate and record_extraction are
not available inside a Harness Workflow segment.

## References and transforms

After DOM.getAXTree, the complete `$cache.observation` or `$last` reference reads
leased artifact text. `$cache.observation.diff` reads a computed diff; a diff that
was not computed remains status metadata. `$cache.observation.detail[nodeId]`
reads an available leased detail artifact for that literal node id. The path
`$cache.observation.artifact.path` is metadata, not content. A lease does not make
old node ids current: after navigation, settle and read a fresh observation.

Keep the two DOM.getAXTree return shapes separate. A standalone browser_call is
hydrated by the Harness: a bounded query can appear as `response.data.records`
after the Harness reads and parses its artifact. Inside Workflow.execute, the
Action returns the platform's raw data: a bounded query has `mode: "detail"`,
`artifact`, `checkedAt` and `summary`, but no `records` array. Do not write
`extract: {"name": "records"}` or `$last.records` after a workflow DOM.getAXTree
step. Action `extract` reads fields of that raw data (for example
`summary.succeeded` or `artifact.path`). To inspect detail values in the same
segment, pass the complete `$cache.observation` or `$last` to transform: the
Workflow host reads the leased text artifact. Match only lines whose target,
index and `ok` status establish the intended result; check failed or missing
targets before acting. If model judgment is needed, return the Action result
and interpret it after the segment.

References occupy the complete string value; embedded references are not string
interpolation. Use template for string construction. The roots are `$context`,
`$vars.NAME`, `$last`, `$cache`, and `$store`; there is no `$steps[N]`.
`$context` holds read-only execution data. `$vars` supports nested object fields
and numeric array indices, with literal variable keys taking precedence.
`$last` is the latest successful Action, readEvents or waitEvent result;
transform writes its output variable without replacing `$last` or `$store`.
`extract` paths address Action data directly: `url`, not `data.url`.
Missing references fail unless checked with exists/notExists.

## Read a known target inside a workflow

This bounded query illustrates the raw result boundary. Replace the placeholder
with an id from the current page observation. `summary.succeeded` is a raw
Action result field; the detail line comes from the leased artifact text.
The resulting line is evidence to inspect, not proof that a requested value
has the expected meaning.

```json
[
  {"type":"action","action":"DOM.getAXTree","purpose":"Read the current state of a known control","params":{"query":{"view":"state","targets":[{"id":"«current-node-id»"}]}},"extract":{"succeeded":"summary.succeeded"}},
  {"type":"transform","input":"$cache.observation","ops":[{"op":"find","pattern":"detail index=0 ok=true","mode":"contains"}],"output":"successfulDetailLines"}
]
```

`find` returns every matching line/item as an array, or [] on a miss. Check
`$vars.matches.length` equals 1 before selecting jsonpath index `0` to extract an
id. regex and template preserve array shape. A regex miss returns an empty
string, so exists alone is not a valid id guard. jsonpath uses dot paths and
numeric indices, not full JSONPath. reg parses the first matching observation
record and does not prove uniqueness. querySelector operates on a supplied
simplified tree, not the live DOM or raw artifact text. Use only operations
exposed by the current schema; some versions also expose join and decode.

## Step types

Write explicit type for every step and purpose for every Action. The Harness
also accepts action shorthand where its schema permits it.

| type | purpose |
| --- | --- |
| action | Run an Action with params, purpose, extract and onError |
| readEvents | Read replayable events from the latest Action window |
| waitEvent | Wait for events after that window, with bounded timeout |
| transform | Apply deterministic operations and write an output variable |
| store | set, merge, append or delete a path relative to the execution's store |
| if | Evaluate condition and run then or else |
| loop | Repeat body subject to condition, maxIterations and total deadline |

Keep onError at stop. There is no retry step setting. Continue only when an
explicit recovery branch handles the failure under the applicable policy.
Variables and store hold JSON-compatible values. Store is execution-scoped;
return accepted rows to the Agent for record_extraction. Every loop must make
observable progress and recheck its continuation condition.

Step shapes use exact field names, not a family of synonyms: a loop step is
`{"type":"loop","maxIterations":N,"condition":{...},"body":[...]}` — never
`steps` or `stopWhen`. A conditional step is
`{"type":"if","condition":{...},"then":[...],"else":[...]}` — the type is
`"if"`, never `"condition"`. A transform step is
`{"type":"transform","input":"$reference","ops":[...],"output":"name"}`
— never `expression`, `inputs` or `outputs`. `extract` is a field on an action
step, not a step type.

## Runtime binding

Do not write `pageId` or `fleetId` in action params. The runtime binding
supplies the target; a placeholder string ("$pageId", "{{pageId}}") is sent
as a literal value and fails, and a guessed UUID passes validation but fails
at runtime. Omit the field entirely. Object arguments keep their object shape:
Input.scroll's `target` and `container` are `{"id":"n_..."}` objects, never
a bare node-id string.

## Observe, select one target, act

This example assumes a settled page and a previously chosen target label. The
pattern must match the observed role/name and artifact format for the task.
Zero or multiple matches return evidence without clicking. Even a succeeded
Workflow can take this no-action branch; inspect its store before claiming success.

```json
[
  {"type":"action","action":"DOM.getAXTree","purpose":"Read current controls"},
  {"type":"transform","input":"$cache.observation","ops":[{"op":"find","pattern":"button \"Continue\"","mode":"contains"}],"output":"matches"},
  {"type":"if","condition":{"path":"$vars.matches.length","operator":"equals","value":1},
   "then":[
     {"type":"transform","input":"$vars.matches","ops":[{"op":"jsonpath","path":"0"},{"op":"regex","pattern":"\\[(n_[A-Za-z0-9_-]+)\\]","group":1}],"output":"targetId"},
     {"type":"if","condition":{"path":"$vars.targetId","operator":"matches","value":"^n_[A-Za-z0-9_-]+$"},
      "then":[{"type":"action","action":"Input.click","params":{"id":"$vars.targetId"},"purpose":"Activate the uniquely matched control"}],
      "else":[{"type":"store","op":"set","path":"selection.status","value":"invalid-id"}]}
   ],
   "else":[{"type":"store","op":"set","path":"selection.matches","value":"$vars.matches"}]}
]
```

## Event synchronization

A terminal event can arrive during or after an Action. First readEvents for its
Action window. Only waitEvent when no terminal event was found. Handle success,
failure and timeout explicitly. A timeout is normal data with events: [] and
timedOut: true; it does not prove readiness. Use the current schema's focus names;
schema support does not guarantee an event will occur for this operation.
Page.go can report navigationStarted=false with no load event. Follow the live
Harness lifecycle policy, synchronize Page.getState and refresh AXTree before
using new document ids. If the policy rejects an authored synchronization shape,
inspect its receipt and return to Agent settlement rather than bypassing the gate.

The following steps illustrate event-window handling after an Action. They are
not a standalone navigation or readiness assertion:

```json
[
  {"type":"readEvents","focus":["Page.loaded","Page.loadFailed"],"extract":{"navigationEvents":"events"}},
  {"type":"if","condition":{"path":"$vars.navigationEvents.0.event","operator":"notExists"},
   "then":[{"type":"waitEvent","focus":["Page.loaded","Page.loadFailed"],"timeout":15000,"extract":{"navigationEvents":"events","navigationTimedOut":"timedOut"}}]},
  {"type":"store","op":"set","path":"navigation.events","value":"$vars.navigationEvents"}
]
```

## Failure and continuation

Inspect failedStepPath, failedErrorCode, completedSteps, variablesAtFailure,
storeAtFailure and executionTrace.pageEvents in the Harness failure receipt.
Keep collected rows and resume only the remaining work. Do not mechanically
slice at failedStepPath: branches and loops carry state. stepPath identifies a
static DSL location; stepRunId distinguishes executions such as loop iterations.

Dispatch is not outcome proof. An Action may have taken effect even if a later
step failed or a receipt is missing. Verify the affected resource before retrying
and obey replayForbidden and task permissions. A failed segment does not undo
prior actions. Choose a new bounded segment or single calls for diagnosis.

Consume final execution results for full values. Workflow.progress and
Workflow.getStatus expose summaries such as variableKeys and resultCount, not
full variable values. Store is not shared between executions; carry required
state explicitly into a continuation.

## Reuse

Reuse returned workflowDefinition.definitionRef and definitionHash through
execute_saved_browser_workflow, patching only the intended changes. Read the
receipt's lastAttempt status, issues and errors before reuse: a definition is
saved before execution and may never have passed validation or reached the
platform. Correct invalid parameter paths; do not replay an unchanged invalid
definition. A page_state_resync_required receipt requires Page.getState, not a
new template. Carry lastAttempt with ref/hash when handing a definition to
another worker. Definition identity does not authorize replay of already
dispatched side effects.
The Harness execution tool accepts its own wrapper; direct platform
Workflow.execute uses workflow plus binding. Workshop export is a separate
format with hostname availability; exclude execution ids, credentials, cookies
and machine-specific paths from reusable Workshop documents.
