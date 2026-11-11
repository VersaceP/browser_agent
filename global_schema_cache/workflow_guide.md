# Workflow authoring guide

## When to use Workflow

Use Workflow when the flow, conditions, and stopping rules are already clear, such as batch page reads, observation processing, or fixed operations.

When new page content requires a decision, let the Agent read the result and decide the next step.

## Before authoring

Use the current Workflow.execute contract for source-package, input,
binding, and result fields. Discover the Action and event contracts needed
by the script before composing it.

Use this guide for the script entry, SDK, event waiting, concurrency,
return values, and packaging. Apply the Browser Skill's task, evidence,
and recovery rules.

## Entry and business logic

Default-export the entry function. Its first argument provides platform capabilities; its second argument receives business input.

| Capability | Use                                                 |
| ---------- | --------------------------------------------------- |
| `actions`  | Call platform Actions and receive business data     |
| `context`  | Read the immutable execution-start snapshot         |
| `waitFor`  | Wait for events, optionally with a trigger function |
| `readText` | Read observation artifact text                      |
| `sleep`    | Wait for a duration in milliseconds                 |

`context` contains `workflowId`, `taskId`, and available page, Fleet, URL, and hostname information.

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
    throw new Error("Bind a page before running this workflow.");
  }

  if (typeof input?.url !== "string") {
    throw new Error("Provide a target URL.");
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
        purpose: "Open the target page.",
      }),
  );

  if (event.event === "Page.loadFailed") {
    throw new Error("The page failed to load.");
  }

  const observation = await actions.DOM.getAXTree({
    pageId,
    purpose: "Inspect the page structure.",
  });
  if (observation.freshness === "unavailable") {
    return { ...observation, url };
  }
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

Use readText to read an observation artifact returned to the workflow.
Interpret each Action result using its current contract, and choose the
evidence needed for the next decision according to the Browser Skill.

## Concurrency and return values

Use `Promise.all` for independent pages. Organize larger batches with at most 8 platform requests per batch. Same-page Actions follow platform scheduling.

Await every platform operation before returning. Handle allowed individual failures with `try/catch` and return them as business data.

Return JSON data, preferably an object organized by business meaning. No return value produces `output: null`. Convert dates, collections, and other objects to strings, arrays, or plain objects first.

Output is limited to 4 MiB. Save larger reports through file Actions and return a summary with file information. Workshop file exports use `output.file` for the file Action's result.

## Save and import into Workshop

Use `Workflow.list` to discover existing saved scripts before authoring a replacement. `query` matches partial titles and descriptions without case sensitivity; an exact `workshopId` lookup returns the complete manifest. Listings include disabled records and current folder IDs.

Use `Workflow.upsert` with a complete Workshop manifest to save a script directly. Omit `workshopId` to create; include an existing ID to replace its document. Omitted folder and enablement settings are preserved on update. Same-folder name conflicts require an explicit record ID or a different name. `Workflow.remove` deletes one saved record.

Execute a saved script with `Workflow.execute` and `workshopId`, plus optional `input` and `binding`. Do not also supply `workflow`. Explicit execution is independent of menu enablement and hostname rules. The returned `workflowId` identifies the execution. Its Task retry plan captures source independently of later saved-record changes.

A local project contains `workflow.json`, the editor type file, and scripts under `src`.

`workflow.json`:

```json
{
  "schemaVersion": 1,
  "name": "Read current page",
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
