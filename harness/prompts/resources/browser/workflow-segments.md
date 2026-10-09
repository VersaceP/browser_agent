---
id: browser.workflow-segments
audience: browser
version: "2026-10-07"
description: Run reusable WebCross JS/TS packages with independent input, scoped capabilities and source/call recovery.
sources:
  - global_schema_cache/workflow_guide.md
  - harness/workflow/script_runtime.py
related_tools:
  - execute_published_skill_workflow
  - read_selected_skill_resource
  - execute_browser_workflow
  - execute_saved_browser_workflow
related_methods:
  - Workflow.execute
  - Workflow.getStatus
  - Workflow.pause
  - Workflow.resume
  - Workflow.cancel
---

# Workflow authoring guide

## Harness execution receipts

Dispatch is not outcome proof. A `succeeded` receipt means the script returned;
interpret its output and observed page results against the user's goal.
`execute_browser_workflow` also saves a versioned definition. Reuse its exact
`definitionRef/definitionHash` with `execute_saved_browser_workflow`, changing
`input` rather than source for ordinary parameter changes. For a selected Skill,
prefer `execute_published_skill_workflow` on the pinned formal entry.

## When to use Workflow

Use Workflow when the flow, conditions, and stopping rules are already clear, such as batch page reads, observation processing, or fixed operations.

When new page content requires a decision, let the Agent read the result and decide the next step.

## Write and run immediately

Provide source directly in a `Workflow.execute` request. The platform validates, compiles in memory, and runs the script.

This example constructs a request to read the selected page. Set `pageId` to the page chosen for this execution.

```js
const source = `
export default async function main({ actions }) {
  const page = await actions.Page.getState({
    purpose: "读取当前页面信息"
  });

  return {
    title: page.title,
    url: page.url
  };
}
`;

const params = {
  workflow: {
    schemaVersion: 1,
    name: "读取当前页面",
    entry: "main.js",
    files: {
      "main.js": source,
    },
  },
  binding: {
    pageId,
  },
};
```

Pass `params` to `Workflow.execute`.

| Parameter  | Meaning                                              |
| ---------- | ---------------------------------------------------- |
| `workflow` | Name, entry module, and source files                 |
| `input`    | Business input passed to the entry; defaults to `{}` |
| `binding`  | Page or Fleet used for this execution                |

Compilation errors include available source locations. Correct the source and submit again.

## Entry and business logic

Default-export the entry function. Its first argument provides platform capabilities; its second argument receives business input.

| Capability | Use                                                 |
| ---------- | --------------------------------------------------- |
| `actions`  | Call platform Actions and receive business data     |
| `context`  | Read the immutable execution-start snapshot         |
| `waitFor`  | Wait for events, optionally with a trigger function |
| `readText` | Read observation artifact text                      |
| `sleep`    | Wait for a duration in milliseconds                 |

`context` contains `workflowId`, `taskId`, and available page, Fleet, URL, and hostname information. Call `Page.getState` for current page state.

Use native JavaScript or TypeScript variables, conditions, loops, functions, classes, exceptions, and data transformations. Pass required capabilities to helper modules.

Provide `purpose` for proxied Actions. Explicit page and Fleet arguments take precedence over execution bindings.

## Modules, events, and observations

Import package modules through relative paths. Reference TypeScript modules with the corresponding `.js` suffix.

`main.ts`:

```ts
import { summarizeText } from "./helpers.js";

interface Input {
  url: string;
}

export default async function main(
  { actions, context, waitFor, readText }: WorkflowApi,
  input: Input,
) {
  const pageId = context.pageId;

  if (!pageId) {
    throw new Error("需要绑定页面");
  }

  if (typeof input?.url !== "string") {
    throw new Error("需要提供目标地址");
  }

  const url = input.url;
  const { event } = await waitFor(
    {
      events: ["Page.loaded", "Page.loadFailed"],
      pageId,
      timeoutMs: 30_000,
      match(event) {
        if (event.event === "Page.loadFailed") {
          return true;
        }

        return event.payload.url === url;
      },
    },
    () =>
      actions.Page.navigate({
        pageId,
        url,
        purpose: "打开目标页面",
      }),
  );

  if (event.event === "Page.loadFailed") {
    throw new Error("页面加载失败");
  }

  const observation = await actions.DOM.getAXTree({
    pageId,
    purpose: "读取页面结构",
  });
  const text = await readText(observation.artifact);

  return {
    pageId,
    url,
    version: observation.version,
    freshness: observation.freshness,
    completeness: observation.completeness,
    ...summarizeText(text),
  };
}
```

`helpers.ts`:

```ts
export function summarizeText(text: string) {
  const lines = text
    .split(/\r?\n/)
    .map((line) => line.trim())
    .filter((line) => line.length > 0);

  return {
    lineCount: lines.length,
    preview: lines.slice(0, 5).join("\n"),
  };
}
```

Provide this `input` through `Workflow.execute`:

```json
{
  "url": "https://example.com/"
}
```

`waitFor(options, trigger)` subscribes before invoking the trigger and can receive an event that arrives before the Action returns. Filter by page scope and business conditions; keep related operations on one page sequential.

Use `freshness` and `completeness` to assess whether the observation meets the next operation's needs. `DOM.getDetails` accepts `version` and `nodeIds` for saved-version reads or `query` for live reads. Read its returned `details` directly.

## Concurrency and return values

Use `Promise.all` for independent pages. Organize larger batches with at most 8 platform requests per batch. Same-page Actions follow platform scheduling.

Await every platform operation before returning. Handle allowed individual failures with `try/catch` and return them as business data.

Return JSON data, preferably an object organized by business meaning. No return value produces `output: null`. Convert dates, collections, and other objects to strings, arrays, or plain objects first.

Output is limited to 4 MiB. Save larger reports through file Actions and return a summary with file information. Workshop file exports use `output.file` for the file Action's result.

## Results and controls

Read the execution result's `status` first:

- `succeeded`: read `output`.
- `failed`: read `error` and the call records.
- `cancelled`: inspect call records to determine effects already produced.

`results` contains platform call status, timing, and available source locations. Values omitted for size include `valueOmittedReason`.

Use `Workflow.getStatus` to inspect an execution and `Workflow.pause`, `Workflow.resume`, or `Workflow.cancel` to control it. Pause takes effect at an asynchronous job boundary; resume continues the live environment. Manual retry starts a new execution from the entry.

## Save and import into Workshop

A local project contains `workflow.json`, the editor type file, and scripts under `src`.

`workflow.json`:

```json
{
  "schemaVersion": 1,
  "name": "读取当前页面",
  "entry": "src/main.js",
  "timeoutMs": 60000,
  "availability": {
    "mode": "hosts",
    "hostnamePatterns": ["(^|\\.)example\\.com$"]
  }
}
```

Export editor types and package the source:

```sh
webcross workflow types --out ./my-workflow/webcross.d.ts
webcross workflow pack ./my-workflow --out ./read-page.workshop.json
```

The type file supplies editor completion and type checking. Packaging collects source and metadata into a `webcross-workshop` document.

Availability patterns match normalized hostnames. Use `mode: "all-hosts"` for all HTTP and HTTPS hosts.

Workshop passes `{}` to the entry. For workflows requiring business input, use `Workflow.execute` with `input`.

## Harness execution scope and selected Skill

In standalone /browser, the explicitly selected Skill supplies the starting
orchestration. Read its formal Workflow packages and input definitions through
read_selected_skill_resource. Bind actual task values, then prefer applicable
execute_published_skill_workflow entries. Its variables argument is passed as
execution input, separately from the immutable source and page/Fleet binding.
Normal supported value changes reuse source. Model judgment decides applicability,
missing data and recovery; record evidence when a template gap requires deviation.
Parameter examples support input binding. Development probes are not business execution entries.

The current source language replaces steps/transform/store and implicit $vars,
$last, $cache references. Rebuild legacy files against the actual task and current
SDK; changing variables cannot translate their language. A fresh read or page
re-render does not automatically require another model turn: native JS can read,
locate and act when the choice is already determined. End a segment at a new
semantic decision, permission handoff, unavailable capability or uncertain effect.
Evaluate boundaries by model round trips and recovery cost, not a fixed step count.

The Harness passes a scoped API to unmodified business source. Every call checks
allowed methods and bound page/Fleet, including aliases and computed calls.
Ownership discovery/changes, Network.readApi, Runtime.evaluate and local output
remain independent tools. For File.handleChooser, canonical already-authorized
file paths must appear in execution input or individual file grants. A source
string cannot grant access. Read or authorize missing files before retrying.
This Harness currently scopes each execution to one page/Fleet, even though the
native platform can schedule several pages. Keep same-page operations sequential.

Read full observation text with readText; DOM.getDetails returns details directly.
waitFor subscribes before trigger and timeout throws; neither an event nor file
assignment alone proves a website accepted the upload. Await every platform call
and return compact JSON evidence, coverage and remaining work. The JS environment
does not supply Node, Shell, Python or browser Runtime.evaluate.

The client waits for the source package's declared deadline plus bounded cleanup.
If transport delivery fails, inspect identity and actual effects. getStatus reports
completedCalls/activeCalls. cancelled and unknown are not success. Retry starts
from the entry: never mechanically slice at an error source line or replay uncertain
writes. Large intermediate call values are preserved with task-resource references;
read them only when needed to diagnose or verify.

The trusted capability module initializes before business modules. SDK globals
and their prototypes are reserved: scripts use their own helpers instead of
replacing built-ins. This protects permission and JSON encoding invariants.
Compiler errors and business exceptions map to original source; a proxied Action
call's native source location may name the trusted capability module. Keep that
location and the action/code/ledger; do not relabel it as a business source line.
