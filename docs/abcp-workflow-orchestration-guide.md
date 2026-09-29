# Workflow authoring in the Harness

The platform contract is documented in
[Workflow Orchestration](../abcp-platform/resources/skills/webcross-browser/references/workflow-orchestration.md).
The model-facing Harness rules and validated step examples live in
[workflow-segments](../harness/prompts/resources/browser/workflow-segments.md).
Use the current capability schema when a deployment differs from these references.
Historical probes are retained separately in
[workflow-execute-live-contract](workflow-execute-live-contract.md).

## Execution formats

The Harness's execute_browser_workflow tool accepts description, steps, variables,
pageId, fleetId and timeout. Its wire adapter builds the platform's workflow and
binding objects. Do not send the Harness wrapper directly as the current platform
Workflow.execute contract. Saved skills and Workshop exports are separate formats;
do not rename their fields by copying an API example.

Write explicit step type and Action purpose, bound loops and deadlines, and keep
onError at stop. A Workflow segment can observe and act when all decisions are
mechanical. Model interpretation, screenshots, Harness tools and failure recovery
return control to the Agent. Runtime.evaluate is excluded by Harness Workflow policy.

## Observation and variables

- A standalone `browser_call` to `DOM.getAXTree` is hydrated by the Harness:
  a bounded query may appear in the model result as `response.data.records`.
  A `DOM.getAXTree` Action inside `Workflow.execute` returns the platform's raw
  data, which has `artifact` and `summary` for a detail query, but no `records`.
  `extract: {"x":"records"}` therefore fails with
  `workflow-reference-not-found`. Extract raw metadata such as
  `summary.succeeded`, or use the complete observation reference with transform
  to read and search the leased artifact text. The bounded-query example in
  [workflow-segments](../harness/prompts/resources/browser/workflow-segments.md)
  uses both supported forms.
- Complete `$cache.observation` or `$last` reads leased observation artifact text.
  `$last` must currently hold a page-observation result for this behavior.
- `.diff` reads a computed diff artifact; when no diff was computed it remains
  metadata. `.detail[nodeId]` reads the available detail artifact for that literal id.
- `.artifact.path` returns path metadata, not artifact content.
- `$last` updates after successful Actions and event steps; transform only writes its
  output variable. Use `$cache.observation` to retain the latest observation reference.
- `$context` holds read-only execution data. `$vars` supports JSON values and nested
  fields/array indices; literal variable keys take precedence. `$store` is local to
  one execution. References occupy the complete string, not an embedded substring.
- Extract paths address Action data directly (`url`, not `data.url`). Missing references
  fail unless tested with exists/notExists. There is no `$steps[N]` root.

find returns all matching lines/items as an array, or [] on a miss. Require
`$vars.matches.length` equals 1 before jsonpath index `0` and scalar id extraction.
regex and template preserve array shape. An empty regex capture is not a valid id;
exists alone does not guard it. reg returns the first parsed observation record and
does not establish uniqueness. querySelector searches a supplied simplified tree,
not the live DOM or raw artifact text. Check the exposed schema for available ops.

## Events and lifecycle

Read the Action event window with readEvents before waiting for future events.
Wait only if no terminal event was observed, and handle load failure and timeout.
A waitEvent timeout succeeds with events: [] and timedOut: true, so it is not a
readiness assertion. A no-op Page.go need not emit a load event. Follow the current
Harness lifecycle gate, synchronize Page.getState and refresh observation before
using ids from a new document. Schema-supported events are not guaranteed to occur.

## Results and recovery

Consume final execution results for variables, store and ordered step results.
Progress/status summaries contain variableKeys and resultCount rather than full
values. Harness failure receipts expose failedStepPath, failedErrorCode,
completedSteps, variablesAtFailure and storeAtFailure. stepRunId distinguishes loop
iterations sharing the same static stepPath.

Preserve partial results. Do not splice a failed loop or branch mechanically, and do
not replay an uncertain side effect. Inspect affected resources and respect
replayForbidden. Persist accepted rows with record_extraction outside Workflow.
Reuse a saved definition through its returned reference/hash while checking each
execution's already completed effects.
