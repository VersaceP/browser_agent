# ABCP Agent Harness

[中文文档](README.zh-CN.md)

ABCP Agent Harness connects LLM tool calling to ABCP Browser's WebSocket capabilities. Instead of driving CDP, Playwright, screenshots, or hand-written selectors directly, the agent calls ABCP methods such as `Page.navigate`, `DOM.getAXTree`, and `Input.click`, then decides the next step from browser observations.

## Requirements

- Python 3.9 or newer for Browser; Workflow mode is tested on Python 3.13.
- An ABCP Browser service reachable over WebSocket.
- An OpenAI-compatible or Anthropic API key.

## Quick Start

Install Python dependencies:

```bash
python -m pip install -r requirements.txt
```

Start or point to your ABCP Browser service. The default config expects:

```text
ws://127.0.0.1:61168/ws
```

For a new setup, copy [`config example.json`](config%20example.json) to `config.json`.
Configure `worker` and optionally `workflow_agent`, then check that `browser.ws_url` points
to the running browser. See the [model configuration guide](docs/provider-model-configuration.md).

When using `api_key_env`, set the named variable in the launch terminal. For example,
set your worker credentials, for example:

```bash
export WORKER_API_KEY="your-worker-key"
```

Run a task:

```bash
python main.py --task "Open https://example.com and summarize the page title and main text."
```

The CLI prints the final answer and task id. Run logs, TODOs and review evidence
are stored in SQLite by default:

```text
worktree/harness.db
```

Use the actual returned file paths for deliverables.

## Configuration

The CLI reads `config.json` by default. Use `--config` to load another file:

```bash
python main.py --config ./my-config.json --task "Check the current fleet list."
```

### Model

Use the [provider and model configuration guide](docs/provider-model-configuration.md)
for all supported services, protocol endpoints, credentials, per-role connections,
thinking, output budgets, caching, examples and connection checks.

`worker` and any explicit `workflow_agent` connection are configured in full.
Without `workflow_agent`, Workflow agents reuse `worker`.
The top level holds no model fields (loading fails if it does). `provider` names the service and `api` the
request protocol; named services require an explicit `api`. Auxiliary models
have separate configuration rules. Keep configuration instructions in that guide
so examples do not drift.

### Browser

Default browser request shape is `flat`:

```json
{
  "browser": {
    "agent_id": "abcp-agent",
    "ws_url": "ws://127.0.0.1:61168/ws",
    "jwt_token_env": "ABCP_JWT_TOKEN",
    "request_shape": "flat"
  }
}
```

`agent_id` is a harness-local routing and logging identifier. WebCross assigns
protocol identity to the WebSocket connection; configuring this value neither
authenticates nor restores a WebCross session.

If your ABCP service expects JSON-RPC requests:

```json
{
  "browser": {
    "request_shape": "jsonrpc"
  }
}
```

### Harness (optional)

You may omit `harness`. Internal thresholds, reuse and recovery use defaults
from `runtime_config.py`; existing explicit overrides remain supported.
Add these common options only when needed:

```json
{
  "harness": {
    "reviewer_enabled": true,
    "worker_max_steps": 30,
    "max_browser_agents": 3,
    "max_task_fleets": 3,
    "worktree_dir": "worktree"
  }
}
```

- `local_access_auto_approve`: defaults to `true`. Local reads/writes and the Workflow Fleet confirmation are granted without asking and logged. The WebCross installation and data/runtime directories, sensitive names (`.ssh`, `.env`, `.git`, `credentials`), the Harness, other tasks and `protected_local_roots` stay denied. Set `false` to confirm each scope in the terminal.
- `reviewer_enabled`: independent semantic review for Browser tasks; defaults to `true`. Set `false` to let the task owner decide completion directly. Permission, protocol and program validation still apply; results record that review was skipped.
- `worker_max_steps`: decision rounds for a delegated Browser worker. Standalone `/browser` has no step limit.
- `max_browser_agents` / `max_task_fleets`: concurrent workers and browser instances per task.
- `worktree_dir`: root directory for the SQLite database and deliverable files.

HITL questions have no cumulative pause quota or automatic human-wait timeout.
Answers and live guidance have no fixed character cap. Old `hitl_wait_timeout_seconds`
and `hitl_max_pause_rounds_per_page/worker` overrides are accepted but ignored;
Question identity, explicit attendance, cancellation and permission checks remain.

Standalone `/browser` maintains a persistent TODO for stage progress, evidence and
next execution steps. Saves do not trigger review. With review enabled, the Browser
requests an initial plan check through `request_goal_review` once needed sources
are inspected, then requests targeted checks for material concerns during execution.
Completion submissions trigger independent final evidence review.
By default, completion requires final review. `reviewer_enabled: false` keeps the
TODO and disables progress, explicit and final review. Restart the Harness after
changing configuration; the setting also applies to resumed tasks.
See [advanced Harness configuration](docs/harness-advanced-configuration.md#english-reference)
for deployment policies, project instructions and detailed options.

## Running Tasks

Run a standalone Browser task (the default mode):

```bash
python main.py --task "Open https://example.com and summarize the page."
```

Read the task from stdin:

```bash
echo "Open https://example.com and summarize the page." | python main.py
```

Override agent id or step count:

```bash
python main.py --agent-id demo-agent --task "Check the current fleet list."
```

For an interactive run, type `/browser` for one direct BrowserAgent, or enter a
`/workflow` command. The choice applies to this run; it does not modify `config.json`.

CLI help, progress messages and control responses are English.
Task text and raw evidence can remain in their original language.

### Workflow mode

Workflow mode turns a user goal into a reusable WebCross Workshop package and
runs it with new business input. The package executes operations that are already
explicit and returns result facts; the agent judges what the facts mean; the
Harness keeps every execution bound to the right goal, version, page and
permissions.

```text
/workflow                                list WebCross Workshop workflows
/workflow <name|workshop-id> [task]       inspect or run a selected remote version
/workflow-drafts                         list local author workspaces
/workflow-create <request>               create a local draft
/workflow-edit <draft|workshop-id|name> <request> edit a local draft or check out remote source
/workflow-rename <draft> <name>           rename locally; publish to sync remotely
/workflow-delete <draft> [--yes]          delete local working files, retaining history
/workflow-publish <draft> [--workshop <id>] pack and upload/update WebCross Workshop
/workflow-unpublish <name|workshop-id>    remove remotely, retaining a local draft and source history
/workflow-stop [task-id|batch-id|all]      stop owned tasks and their WebCross executions
/resume <task-id> [instruction]           resume the recorded task/version
```

WebCross Workshop is the only executable marketplace. `/workflow` resolves the
remote identity and complete manifest, then pins a scoped execution snapshot;
it never silently substitutes an unpublished local draft. Local author files live
under `workflows/<slug>/`, with `draft.json`, `history/versions/<hash>/`, unified
diffs and `history/changes.jsonl`. These records support authoring and recovery,
not a second published catalog. `/workflow-publish` uses the official CLI packer
and the WebCross `Workflow.upsert` contract; `/workflow-unpublish` uses
`Workflow.remove`. The current CLI accepts literal JSON parameters only, so large
manifests travel through the configured native connection rather than shell argv.
Readback verifies upload/removal; uncertain writes are not automatically replayed.
The remote revision/hash is reviewed before an update, but the platform has no
conditional-update/CAS parameter, so readback does not eliminate concurrent writes.

Browser can execute an existing Workshop with `browser_call` → `Workflow.execute`
and `{workshopId, input, binding}`: Harness resolves the full manifest, pins its
revision and source hash, and executes a scoped inline snapshot. The returned
`definitionRef/definitionHash` reuses that same task-local snapshot. Browser/run
agents cannot upsert or remove remote records. Each task pins the exact version it started with. Each run
task binds one page; the package acts only on that page, and a new tab is bound
by the agent after the Harness checks its origin. Acceptance points come from the
original goal; the Harness checks references and identity; the agent judges.
Single items enter the run Agent directly. Independent items can be dispatched as
separate tasks. Runs can delegate read-only observation, and use supported package
recovery; business completion through Browser is unavailable. If no supported recovery
remains, report partial/blocked results and exact remaining identities. Package repair
belongs to `/workflow-edit`. Historical completion receipts remain readable; those
Browser child tasks cannot resume. Standalone Browser and author exploration retain
their existing scope.
Workflow records four token classes, model rounds, tool/platform I/O and net time
(observed human waits excluded; child execution included), without token/time limits or budget-extension prompts. Historical usage
is retained on resume; legacy `harness.workflow_budget` configuration is ignored. See the [plan](docs/tau-skill-builder-agent-loop-plan.md).

Local drafts retain one author task and its context. `/workflow-edit` and
`/resume` share that task; remote workflows without a local draft are checked out
first. Deleting a local working copy preserves history and does not remove the
remote record. Unpublishing retains the exact remote source and local work.
Historical runs resume their pinned snapshots; cleared legacy author branches
must be continued through an active draft rather than reconstructed from a newer
remote version. See [Workshop migration](docs/workshop-only-20261110.md).

`/workflow-create` first establishes intended reuse scenarios, asking about unclear
business scope before implementation. New drafts include `coverage.json` for controls,
prerequisites, options, dependencies and representative scenarios, including optional
branches unused by the first sample. Discovery, documentation, implementation and
verification stay distinct. Dynamic choices retain retrieval/refresh conditions;
runtime logic belongs in `src/`, since the authoring inventory is not embedded in
the exported Workshop. Legacy packages without an inventory remain supported.

Authoring puts evidenced deterministic requirements in the package, batches
local checks, and keeps normal actions and readbacks within one invocation. New
drafts include a pure `src/preflight.js` checker. `validate_input` checks complete
input and explicit material files together, retaining unknowns and full receipts
without site-specific business gates. See the [authoring contract](docs/workflow-authoring-contract.md).

Browser and Workflow share a default 60-second automatic reconnect window
after Dispatcher transport loss. Connection identity, event subscriptions and task
context survive recovery. Sent calls with lost replies retain an unknown outcome;
business actions are never replayed automatically. Workflow checkpoints preserve
the interruption and query the original `workflowId` when available; status does
not reconstruct missing execution output. A timeout or identity/protocol rejection
preserves the task for `/resume <task_id>`. This window does not shorten Workflow
execution or human HITL waiting. `browser.reconnect_timeout_seconds` accepts 0–60
seconds (default 60; 0 disables automatic waiting).

Resume a task at phase granularity, optionally adding a user instruction under
the same task ID:

```bash
python main.py --resume <task_id> --task "Additional instruction"
```

At the interactive prompt, the equivalent form is
`/resume <task_id> [additional instruction]`; the full ID also accepts an `@` prefix,
and legacy task directory paths remain supported. Omit the instruction for recovery of the original
task alone. The task manifest keeps the original request, while the amendment
is recorded as attributed user input. Browser mode continues an open phase or
creates a new continuation after a terminal phase without replaying its old
side effects. An amendment saved before a startup failure remains available to
the next plain resume. An existing task cannot be rebound to a different
explicit `@Fleet` reference through an amendment. Validated phases and
their active artifacts are preserved; an unfinished phase requires fresh
verification before repeating its actions. A phase that was live
when the process stopped requires confirmation before it is replayed. For
unattended use, pass `--resume-retry-interrupted` explicitly. Live Fleet/page
handles are reused only as best-effort task-owned hints and are revalidated
against the browser inventory; stale hints fall back to normal routing.
When a fatal transport blocker was saved, the first browser dispatch probes the
connection, capabilities and Fleet inventory again. A successful probe updates
the blocker; a failed probe preserves its reason and task identity. The probe
does not replay the interrupted browser operation.

DB mode resumes from SQLite task records without requiring a physical task
directory. Missing or malformed plan/state records fail closed and never
silently create an empty task. File mode still requires the original directory.

## Tests and Acceptance

Use pytest as the authoritative full-suite runner:

```bash
conda run --no-capture-output -n agent python -m pytest tests/ -q
```

The suite contains both `unittest.TestCase` methods and module-level
`def test_*` functions. `python -m unittest discover` does not collect the
module-level pytest functions, so it is suitable for targeted diagnostics but
must not be reported as a complete repository regression run. When reporting
acceptance results, include the exact command together with pytest's passed,
skipped, and subtest counts.

## Logs and Artifacts

Browser and Workflow store task records in SQLite. DB mode creates no task
or log subdirectories in advance. Locks live in a shared control directory;
task directories are created only for actual downloads, screenshots, local
intermediate files, or deliverables:

```text
worktree/harness.db
worktree/.run-locks/<task_id>/owner.json  # Exists only while running
worktree/<task_id>/...                  # Actual files, created as needed
```

The task's events are SQLite rows, available through the virtual `run.jsonl`
view for task-scoped readers. Important event types include:

- `agent.model`: model text and tool calls.
- `browser.call.result`: ABCP method call result.
- `llm.usage`: per-call token and prompt-cache metrics.
- `llm.usage_summary`: task-level token and prompt-cache summary.
- `agent.final`: final answer.

Screenshot-like responses are saved to `artifacts/`; large base64 payloads are omitted from model context.

## Prompt Cache Observability

The harness records per-call cache metrics returned by the provider:

- `cache_read`
- `cache_creation`
- `uncached_input`
- `output`
- `cache_read_rate`
- `cache_reuse_rate`
- `cache_diagnostics.marker_count`
- `cache_diagnostics.marker_positions`
- `cache_diagnostics.cache_control_signature`
- `cache_diagnostics.cache_control`

`estimated_cost_usd` is currently reserved as `null`; model pricing can be added later through configuration.

The static prompt-context fields are disabled by default. Their rendered XML is hashed as part of the full prompt fingerprint recorded in usage diagnostics; the guide manifest is included in that fingerprint too. Keep these fields stable within a task to preserve prefix-cache reuse. Do not place current date, cwd, or task-specific observations here.

## Browser Tools

BrowserAgent uses `browser_call` to invoke the declared platform capabilities:

```json
{
  "method": "Page.navigate",
  "params": {
    "pageId": "...",
    "url": "https://example.com"
  },
  "reason": "Navigate to the target page"
}
```

## Runtime Entries

`/browser` starts BrowserTaskRunner and its BrowserAgent. `/workflow*` commands
start Workflow task, author or coordinator agents on the shared Tau loop
(`harness/workflow_mode/`). They share storage, permissions, steering and
transport recovery.

Lead and Skill modes have been removed. Rename a `skill_orchestrator` model
section to `workflow_agent`; `harness.agent_mode` accepts `browser` or `workflow`.
Historical Lead and Skill records remain readable, but those tasks cannot resume
through a removed runtime.
