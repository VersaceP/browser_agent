# ABCP Agent Harness

[中文文档](README.zh-CN.md)

ABCP Agent Harness connects LLM tool calling to ABCP Browser's WebSocket capabilities. Instead of driving CDP, Playwright, screenshots, or hand-written selectors directly, the agent calls ABCP methods such as `Page.navigate`, `DOM.getAXTree`, and `Input.click`, then decides the next step from browser observations.

## Requirements

- Python 3.9 or newer for Browser/Lead; Skill Builder requires Python 3.12 or newer.
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
Existing configurations remain supported. Fill in the model connections for
`lead`, `worker` and `plan_validator`, then check that `browser.ws_url` points
to the running browser. See the [model configuration guide](docs/provider-model-configuration.md).

When using `api_key_env`, set the named variable in the launch terminal. For example,
the template's validator credentials use:

```bash
export PLAN_VALIDATOR_API_KEY="your-validator-key"
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

`lead`, `worker` and `plan_validator` are each configured in full and inherit
nothing; the top level holds no model fields (loading fails if it does), and the
plan validator must stay enabled. `provider` names the service and `api` the
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
    "lead_max_steps": 20,
    "worker_max_steps": 30,
    "max_browser_agents": 3,
    "max_task_fleets": 3,
    "hitl_wait_timeout_seconds": 600,
    "worktree_dir": "worktree"
  }
}
```

- `lead_max_steps` / `worker_max_steps`: decision rounds for Lead and its delegated workers. Standalone `/browser` has neither of these step limits.
- `max_browser_agents` / `max_task_fleets`: concurrent workers and browser instances per task.
- `hitl_wait_timeout_seconds`: maximum wait for human assistance.
- `worktree_dir`: root directory for the SQLite database and deliverable files.

Standalone `/browser` maintains a persistent TODO and triggers independent evidence
review on checklist changes, explicit review requests and completion submissions.
Completion requires final review. No checklist or review tuning is needed for setup.
See [advanced Harness configuration](docs/harness-advanced-configuration.md#english-reference)
for deployment policies, project instructions and detailed options.

## Running Tasks

Run a task through the LeadAgent orchestrator:

```bash
python main.py --task "Open https://example.com and summarize the page."
```

Read the task from stdin:

```bash
echo "Open https://example.com and summarize the page." | python main.py
```

Override agent id or step count:

```bash
python main.py --agent-id demo-agent --max-steps 20 --task "Check the current fleet list."
```

For an interactive run, choose the orchestration entry before entering the
task: type `/browser` for one direct BrowserAgent, or `/lead` for planning,
parallel work, and aggregation. The prompt confirms the choice for this run;
it does not modify `config.json`.

Resume a task at phase granularity, optionally adding a user instruction under
the same task ID:

```bash
python main.py --resume worktree/<task_id> --task "Additional instruction"
```

At the interactive prompt, the equivalent form is
`/resume <task-directory> [additional instruction]`; omit the instruction for recovery of the original
task alone. The task manifest keeps the original request, while the amendment
is recorded as attributed user input. Browser mode continues an open phase or
creates a new continuation after a terminal phase without replaying its old
side effects. An amendment saved before a startup failure remains available to
the next plain resume. An existing task cannot be rebound to a different
explicit `@Fleet` reference through an amendment. Lead mode evaluates the new instruction and routes new or revised
assignments through review and any required approval. Validated phases and
their active artifacts are preserved; an unfinished phase requires fresh
verification before repeating its actions. A phase that was live
when the process stopped requires confirmation before it is replayed. For
unattended use, pass `--resume-retry-interrupted` explicitly. Live Fleet/page
handles are reused only as best-effort task-owned hints and are revalidated
against the browser inventory; stale hints fall back to normal routing.

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

Both Browser and Lead store task records in SQLite. DB mode creates no task
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

- `lead.model` / `agent.model`: model text and tool calls.
- `browser.call.result`: ABCP method call result.
- `llm.usage`: per-call token and prompt-cache metrics.
- `llm.usage_summary`: task-level token and prompt-cache summary.
- `lead.final` / `agent.final`: final answer.

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

## Lead Agent Tools

`LeadAgent` does not operate the browser directly. It plans, dispatches, and summarizes work through these tools:

- `spawn_browser_agent`: start an isolated BrowserAgent.
- `wait_browser_agents`: wait for one or more browser workers.
- `list_browser_agents`: inspect active workers.
- `lead_save_artifact`: persist LeadAgent-reshaped rows from trusted extraction evidence.
- `read_harness_guide`: load a paged, versioned operating guide from the prompt's compact guide index when a detailed receipt/recovery rule is relevant.
- `final_answer`: finish the LeadAgent run.

LeadAgent should use BrowserAgent phases. BrowserAgent's `browser_call` uses:

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

## Typical Orchestration Flow

```text
LeadAgent receives task
  -> spawn_browser_agent: submit one reviewed assignment
  -> wait_browser_agents: return worker evidence and remaining work
  -> validate extraction artifacts and resultLevels
  -> lead_save_artifact: reshape trusted rows only when validation is schema_mismatch
  -> revise the assignment or continue its phase when evidence is missing/wrong
  -> final_answer: summarize successes, failures, and blocked items
```
