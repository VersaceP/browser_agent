---
id: browser.workflow-segments
audience: browser
version: "2026-11-04"
description: Scoped Workflow execution, saved Workshop snapshots and evidence-based handoff.
sources:
  - harness/workflow/script_runtime.py
  - harness/workflow/workshop.py
related_tools:
  - execute_browser_workflow
  - execute_saved_browser_workflow
related_methods:
  - Workflow.execute
  - Workflow.list
  - Workflow.getGuide
  - Workflow.getStatus
  - Workflow.pause
  - Workflow.resume
  - Workflow.cancel
---

# Workflow execution in Harness

Read the live Workflow.getGuide for SDK details and System.describeAction for
current Action contracts. This guide adds Harness permissions and reuse semantics;
it does not copy the platform guide or grant its broader capabilities.

## Execute and reuse

Use a deterministic segment when its inputs, target-selection rules and stopping
conditions are known. Return facts at the next new
semantic decision. Contiguous deterministic stages can share one invocation.

```js
const params = {
  workflow: {
    schemaVersion: 1,
    name: "Read the bound page",
    entry: "main.js",
    files: {
      "main.js": "export default async function main({actions}, input) { return actions.Page.getState({purpose: input.purpose}); }",
    },
  },
  input: { purpose: "Read current page identity" },
  binding: { pageId },
};
```

execute_browser_workflow takes workflow, input, pageId and fleetId. It stores
unmodified business source before wrapping it with per-call page, Action and file
permissions. Reuse the exact definitionRef/definitionHash with
execute_saved_browser_workflow and input overrides instead of resending source.
These are task-local definitions, distinct from a published package or workshopId.

For a saved Workshop record, discover it with Workflow.list. Call browser_call
with method Workflow.execute and params {workshopId, input, binding}; supply
exactly one of workflow or workshopId. Harness reads the complete record once,
records its revision/source hash, saves a task-local definition and executes the
scoped inline snapshot. Later remote changes do not change that execution. Reuse
the returned definitionRef/definitionHash to keep using the same snapshot.
Enabled and hostname rules govern menus, not explicit execution authorization.
Workflow.upsert/remove require host-issued author authority for the affected
record; ordinary Browser and Workflow completion have none.

## Observe only what the next decision needs

Reuse receipts that establish the required identity and postcondition. An input
Action returning does not necessarily prove application persistence. When facts
are missing, read known targets in bounded batches; expand structural observation
only for the relevant gap. Within a Workflow, use readText on observation artifacts
and read DOM.getDetails details directly. Partial observations cannot establish
absence or uniqueness across unobserved candidates. Preserve coverage and failed
attribute reads as unknown; a batch limit is not a complete option list.

A selection receipt must match the requested values and identity, not merely be
nonempty. Do not classify a value as invalid just because it is empty, zero or has
a familiar placeholder label; use the explicit task and declared contract.

## Wait, reconcile and hand off

api.waitFor(options, trigger) subscribes before invoking the trigger. Match the
actual page/task and terminal condition. A timeout throws; it means the condition
was not confirmed in that interval, not that a write did not happen. If no useful
event exists, trigger once and use bounded state observations. Stable signatures
and zero pending requests alone do not prove business success.

Reading loading state and then subscribing can miss a completed load between the
two calls. Subscribe before the relevant transition when possible; otherwise
reconcile with bounded current state. Correlate new-page identity to its opener;
a discovered page does not acquire permission until the host admits its binding.

Dispatch is not outcome proof. Read execution status, output, error and call records together. succeeded means
the script returned; a caught Action failure remains in workflowExecution.callFacts.
failed/cancelled do not undo earlier actions. Preserve original failures and
reconcile unknown effects before retrying or switching to Browser. A transport
timeout is not platform cancellation. Recover retained results without replaying
writes merely to recover diagnostics.

When a package needs Browser completion, carry the original goal, confirmed and
remaining work, unknown effects, constraints, package version and evidence. Finish
the authorized task and report package defects separately. Repair belongs to the
author flow; completion does not verify or modify the package.
