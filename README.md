# ABCP Agent Harness

[中文文档](README.zh-CN.md)

ABCP Agent Harness connects LLM tool calling to ABCP Browser's WebSocket capabilities. Instead of driving CDP, Playwright, screenshots, or hand-written selectors directly, the agent calls ABCP methods such as `Page.navigate`, `DOM.getAXTree`, and `Input.click`, then decides the next step from browser observations.

## Requirements

- Python 3.9 or newer for Browser; Skill mode and Builder require Python 3.12 or newer.
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
Configure `worker` and optionally `skill_orchestrator`, then check that `browser.ws_url` points
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

`worker` and any explicit `skill_orchestrator` connection are configured in full.
Without `skill_orchestrator`, Skill and Workflow owners reuse `worker`.
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
    "worker_max_steps": 30,
    "max_browser_agents": 3,
    "max_task_fleets": 3,
    "worktree_dir": "worktree"
  }
}
```

- `worker_max_steps`: decision rounds for a delegated Browser worker. Standalone `/browser` has no step limit.
- `max_browser_agents` / `max_task_fleets`: concurrent workers and browser instances per task.
- `worktree_dir`: root directory for the SQLite database and deliverable files.

HITL questions have no cumulative pause quota or automatic human-wait timeout.
Answers and live guidance have no fixed character cap. Old `hitl_wait_timeout_seconds`
and `hitl_max_pause_rounds_per_page/worker` overrides are accepted but ignored;
Question identity, explicit attendance, cancellation and permission checks remain.

Standalone `/browser` maintains a persistent TODO and triggers independent evidence
review on checklist changes, explicit review requests and completion submissions.
Completion requires final review. No checklist or review tuning is needed for setup.
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

For an interactive run, choose the orchestration entry before entering the
task: type `/browser` for one direct BrowserAgent, `/workflow` for reusable program development,
or `/skill` for independent Skill execution.
The prompt confirms the choice for this run;
it does not modify `config.json`.

CLI help, progress messages and control responses are English.
Task text and raw evidence can remain in their original language.

`/skill` lists published Skills and drafts, then uses a short `[skill]>` prompt.
Execute with `/<Skill-name-or-ID> [@Fleet-ID] [task]` or select with `--skill <name>`.
Names allow a unique case-insensitive partial match; multiple matches display all
names and IDs for exact selection. Its Orchestrator binds inputs and the Harness
runs decided JS/TS Workflows, returning at judgment or recovery points. Browser
handles bounded recovery. Recorded defects can be addressed in a Builder through
`/resume <builder-task-id> Improve using @<execution-task-id>`. Reuse starts no
background replay or Coding repair loop. Completion requires independent goal review.
Shared Python/Shell execution is deferred.
An optional `skill_orchestrator` model section overrides the default connection
from `worker`.

`/workflow [@<Fleet ID> task]` lets the main agent write and execute task-local
files directly. The same author validates the source on an independent page with
separate trial checkpoints. Browser can handle bounded semantic operations or
recovery. Business delivery and program validation are reviewed separately.
`/skill-create @<workflow-task-id> request` imports those exact source files and
execution evidence into a Builder draft. After user publication, `/skill` reuses
the programs with new business inputs. See the [implementation plan](docs/tau-skill-builder-agent-loop-plan.md)
for development, recovery and verification boundaries.
The Skill command surface is `/<Skill> [@Fleet-ID] [task]`,
`/skill-create [Skill-name-or-ID] @<task-id> [request]`, `/skill-publish <name-or-ID>`,
`/skill-delete <name-or-ID>` and `/resume <task-id> [instruction]`. Source and resume
task IDs are required. `/skill-create` creates a new Skill when the name is omitted,
and updates the named published Skill when supplied. Publish/delete resolve names identically; multiple draft or
deletion versions require an explicit interactive selection. Content hashes and
publication conflicts are checked internally. `/resume` restores the recorded execution mode and also continues Builder
conversations. `/skill`, `/browser` and `/workflow` remain mode navigation.
See the [Skill command guide](skills/README.md) for examples and ambiguity behavior.

Browser, Workflow and Skill share a default 60-second automatic reconnect window
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

Browser, Skill and Workflow store task records in SQLite. DB mode creates no task
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

`/browser` starts BrowserTaskRunner and its BrowserAgent. `/skill` starts the
independent Skill Orchestrator; `/workflow` starts Workflow development. They
share browser tools, leases, permissions, storage, evidence and recovery.

Lead mode has been removed. Move a former `lead` model connection to
`skill_orchestrator` to preserve the Skill/Workflow model choice, remove
`plan_validator`, `claim_extractor` and Lead-only harness settings, and choose
`browser`, `skill` or `workflow`. Historical Lead records remain readable, but
those tasks cannot resume through a removed runtime.
