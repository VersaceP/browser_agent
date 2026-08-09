# ABCP Agent Harness

[中文文档](README.zh-CN.md)

ABCP Agent Harness connects LLM tool calling to ABCP Browser's WebSocket capabilities. Instead of driving CDP, Playwright, screenshots, or hand-written selectors directly, the agent calls ABCP methods such as `Page.navigate`, `DOM.getAXTree`, and `Input.click`, then decides the next step from browser observations.

## Requirements

- Python 3.9 or newer.
- An ABCP Browser service reachable over WebSocket.
- An OpenAI-compatible or Anthropic API key.

## Quick Start

Install Python dependencies:

```bash
python -m pip install -r requirements.txt
```

Start or point to your ABCP Browser service. The default config expects:

```text
ws://localhost:9300/ws
```

Set the model API key expected by your `config.json`:

```bash
export OPENAI_API_KEY="your-openai-key"
```

Run a task:

```bash
python main.py --task "Open https://example.com and summarize the page title and main text."
```

The CLI prints the final answer, task id, task directory, and run log path. Runtime logs and artifacts are written under:

```text
worktree/<task_id>/
```

## Configuration

The CLI reads `config.json` by default. Use `--config` to load another file:

```bash
python main.py --config ./my-config.json --task "Check the current fleet list."
```

### Model

The top-level `provider` field is required. Set it explicitly to `openai` or
`anthropic`; the harness does not infer the wire protocol from `model_id` or
`base_url`.

OpenAI-compatible example:

```json
{
  "provider": "openai",
  "model_id": "gpt-4.1",
  "api_key_env": "OPENAI_API_KEY",
  "base_url_env": "OPENAI_BASE_URL",
  "extra_params": {
    "temperature": 0.2,
    "max_tokens": 4096
  }
}
```

Anthropic example:

```json
{
  "provider": "anthropic",
  "model_id": "claude-sonnet-4-20250514",
  "api_key_env": "ANTHROPIC_AUTH_TOKEN",
  "base_url_env": "ANTHROPIC_BASE_URL",
  "extra_params": {
    "temperature": 0.2,
    "max_tokens": 4096
  }
}
```

`cache_control_mode` controls explicit prompt-cache markers:

- `auto` (default): enable markers for Anthropic-style providers and known-good OpenAI-compatible base URLs.
- `on`: force markers and retry once without markers if the provider rejects them.
- `off`: never send markers.

```json
{
  "extra_params": {
    "cache_control_mode": "auto",
    "temperature": 0.2,
    "max_tokens": 4096
  }
}
```

The legacy `enable_cache_control` boolean is still accepted when `cache_control_mode` is not set.

### Browser

Default browser request shape is `flat`:

```json
{
  "browser": {
    "agent_id": "abcp-agent",
    "ws_url": "ws://localhost:9300/ws",
    "jwt_token_env": "ABCP_JWT_TOKEN",
    "request_shape": "flat"
  }
}
```

If your ABCP service expects JSON-RPC requests:

```json
{
  "browser": {
    "request_shape": "jsonrpc"
  }
}
```

### Harness

Common harness options:

```json
{
  "harness": {
    "lead_max_steps": 20,
    "worker_max_steps": 30,
    "max_browser_agent_instances": 3,
    "max_browser_agents": 3,
    "fleet_reuse_enabled": true,
    "same_fleet_multiworker_enabled": false,
    "max_task_fleets": 3,
    "fleet_auth_barrier_enabled": true,
    "fleet_auth_barrier_wait_seconds": 120,
    "auth_fleet_ledger_path": ".auth_fleet_ledger.json",
    "fleet_slot_reconnect_attempts": 2,
    "fleet_slot_reconnect_backoff_seconds": 0.25,
    "fleet_slot_manual_reset_after_failures": 3,
    "hitl_poll_interval_seconds": 2,
    "hitl_wait_timeout_seconds": 600,
    "worktree_dir": "worktree",
    "context_file": null
  }
}
```

- `lead_max_steps`: maximum LeadAgent decision rounds.
- `worker_max_steps`: maximum BrowserAgent rounds.
- `max_browser_agent_instances`: maximum live BrowserAgent slots kept in the reusable pool. Idle slots keep their ABCP connection and page registry.
- `max_browser_agents`: maximum concurrently running browser workers. Effective browser-slot concurrency is still bounded by `max_browser_agent_instances`.
- `fleet_reuse_enabled`: deterministically assign each worker a fleet and force `Page.create` into it. Generic work may reuse an eligible slot fleet; a new `session_key` or isolated worker gets a fresh fleet, while `worker_contract.fleet_id` selects an existing Fleet by full UUID or unique prefix and never creates a replacement. Named/isolated fleets never become the generic slot default. Lost named sessions fail with `session_fleet_lost` instead of silently rebinding. Model-initiated `Fleet.create`/`Fleet.close` and out-of-assignment fleet ids fail closed; explicit page continuations may receive prior page candidates.
- `same_fleet_multiworker_enabled`: opt-in canary for sharing one task/session fleet across parallel slots while keeping separate pages. It defaults to `false`; when enabled, the owner socket remains authoritative, notifications are relayed to delegates, and equal-page calls are serialized.
- `max_task_fleets`: ceiling on how many distinct fleets (browser instances) one task may occupy; `0` disables it. The harness never closes a fleet, so one it opens holds its budget slot until the platform stops reporting it — a fleet that disappears from the owner inventory releases its slot again. Counted over fleets bound to this task's workers, never over the Agent-global `Fleet.list`. An explicitly selected fleet (`--fleet-id` pin, `worker_contract.fleet_id`, a bound `session_key`, `reuse_from_worker_id`) is always honored and never blocked, but it does consume budget. At the ceiling a fleetless worker reuses one of the task's existing fleets, preferring one no running worker holds, and deployment-default `worker_session_isolation_enabled` yields to the cap. Two cases cannot be served that way and get a `task_fleet_limit_reached` receipt instead: a spawn demanding a separate identity (a phase-declared `needs_isolated_session`, or a new `session_key`), and a ceiling where every task fleet is bound to a named session, since a logged-in cookie jar is never lent to a generic worker. Waiting does not clear either one — the harness closes no fleets and a session binding outlives its worker — so the receipt tells the Lead to continue on an existing fleet, release a session binding through auth recovery, or raise the ceiling. Before refusing, the cap re-reads the authoritative `Fleet.list` once. The dispatcher answers that from the whole fleets table with no per-connection scoping, so one successful read both finds a fleet another slot created seconds ago and retires any fleet the platform has dropped — whichever slot owned it — handing its budget back. A failed read is never treated as proof of disappearance.
- `fleet_auth_barrier_enabled`: make login/CAPTCHA resolution fleet-wide and fail closed for non-resolver workers. `fleet_auth_barrier_wait_seconds` controls the bounded wait.
- `auth_fleet_ledger_path`: persistent, non-secret verified session index, relative to `worktree_dir` unless absolute. Reclaimed fleets are quarantined until ledger reconciliation restores their restrictions.
- `fleet_slot_reconnect_attempts`: bounded same-`agentId` reconnect attempts per recovery cycle. Transport loss never proves that the fleet is lost.
- `fleet_slot_reconnect_backoff_seconds`: base delay between those reconnect attempts. Failed browser mutations are never replayed.
- `fleet_slot_manual_reset_after_failures`: recovery cycles before spawn returns `session_manual_reset_required`. The binding remains fail-closed until a host/operator explicitly resets it with the reported fleet id and generation.
- `hitl_poll_interval_seconds`: polling interval after `Hitl.requestPause`.
- `hitl_wait_timeout_seconds`: maximum wait time for human intervention.
- `worktree_dir`: root directory for run logs and artifacts.
- `context_file`: optional static prompt context file. Keep it stable during a task, or it can reduce prompt-cache reuse.

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

Each run creates a task directory:

```text
worktree/<task_id>/
  run.jsonl
  artifacts/
```

`run.jsonl` is JSON Lines. Important event types include:

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

`harness.context_file` is disabled by default. If enabled, its contents are injected into the static system prompt and its sha256 is recorded in usage diagnostics. Use it only for stable context. Fast-changing context should be appended dynamically by the caller instead.

## Lead Agent Tools

`LeadAgent` does not operate the browser directly. It plans, dispatches, and summarizes work through these tools:

- `spawn_browser_agent`: start an isolated BrowserAgent.
- `wait_browser_agents`: wait for one or more browser workers.
- `list_browser_agents`: inspect active workers.
- `lead_save_artifact`: persist LeadAgent-reshaped rows from trusted extraction evidence.
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
  -> emit_task_plan: split by task_type and phase
  -> spawn_browser_agent: collect the first pending phase with exact expected fields
  -> validate extraction artifacts and resultLevels
  -> lead_save_artifact: reshape trusted rows only when validation is schema_mismatch
  -> replan or spawn one focused continuation when evidence is missing/wrong
  -> final_answer: summarize successes, failures, and blocked items
```
