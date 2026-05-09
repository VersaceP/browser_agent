"""Lead-only tools: spawn_agent / spawn_agents_parallel / submit_plan.

vs v5, simplified:
- Removed default approval gate — submit_plan is no longer a spawn prerequisite;
  only when spawner.require_plan_approval is True do we trigger a terminal y/n.
  Once approved within a session, subsequent revisions don't re-prompt.
- Removed _approved_plan_sessions module-level dict — state lives in
  spawner.session_states[session_id]
- Real validation is delegated to publish_artifact itself (opens file to check)

Plan format (compatible with v5):
    - [ ] [worker] Scrape 50 product details
          output: shared/products.json
          schema: array of {name, slug, ...}

    - [ ] [verification] Validate data completeness
          output: shared/validation.txt

    Or single-line compact form:
    - [ ] [worker] Scrape 50 → output: shared/products.json
"""
import asyncio
import os
import re
import sys
from typing import Any, Dict, List, Optional

from .base import ToolSpec, ToolContext
from core.session_state import Contract


# ──────────────────────────────────
# Plan parsing — minimal regex version
# ──────────────────────────────────

# Line-start checklist item: - [ ] [agent_type] description
_CHECKLIST_RE = re.compile(r"^-\s*\[[ xX]\]\s*\[(\w+)\]\s*(.+?)(?:\s*[→\-]>?\s*output:\s*(\S+))?\s*$")
# Indented `output:` line
_OUTPUT_LINE_RE = re.compile(r"^\s+output:\s*(\S+)\s*$", re.IGNORECASE)
# Indented `schema:` line
_SCHEMA_LINE_RE = re.compile(r"^\s+schema:\s*(.+?)\s*$", re.IGNORECASE)


def parse_plan(plan_md: str) -> List[Dict[str, Any]]:
    """Parse plan markdown, return [{agent_type, description, output, schema}, ...]"""
    contracts = []
    current = None

    for raw in plan_md.splitlines():
        m = _CHECKLIST_RE.match(raw)
        if m:
            agent_type, description, inline_out = m.groups()
            current = {
                "agent_type": agent_type.lower(),
                "description": description.strip(),
                "output": inline_out,
                "schema": None,
            }
            contracts.append(current)
            continue
        if current is None:
            continue
        m = _OUTPUT_LINE_RE.match(raw)
        if m:
            current["output"] = m.group(1)
            continue
        m = _SCHEMA_LINE_RE.match(raw)
        if m:
            current["schema"] = m.group(1)
            continue

    return contracts


def _basename_no_ext(p: str) -> str:
    """shared/foo.json → foo"""
    base = os.path.basename(p)
    
    return os.path.splitext(base)[0]


def find_contract_for_task(
    contracts: List[Contract],
    agent_type: str,
    task: str,
) -> Optional[Contract]:
    """Find the contract best matching (agent_type, task) in plan contracts.

    Simple strategy: same agent_type + max word overlap between task and description.
    """
    candidates = [c for c in contracts if c.agent_type == agent_type and c.output_path]
    if not candidates:
        return None
    if len(candidates) == 1:
        return candidates[0]

    task_words = set(re.findall(r"\w+", task.lower()))
    scored = []
    for c in candidates:
        desc_words = set(re.findall(r"\w+", c.description.lower()))
        scored.append((len(task_words & desc_words), c))
    scored.sort(key=lambda x: -x[0])
    return scored[0][1] if scored[0][0] > 0 else candidates[0]


def format_contract_block(contract: Contract) -> str:
    """Generate the delivery-contract block appended to the child agent's task."""
    parts = ["", "━━━ DELIVERY CONTRACT ━━━", f"final_output: {contract.output_path}"]
    if contract.schema:
        parts.append(f"schema: {contract.schema}")
    parts.append(
        "\nThe contract MUST be fulfilled via publish_artifact("
        f"name='{contract.name}'). "
        "Same-name republish overwrites — publish incrementally for batch jobs."
    )
    return "\n".join(parts)


# ──────────────────────────────────
# submit_plan
# ──────────────────────────────────

def _ask_user_approval(plan_md: str, summary_lines: str) -> bool:
    """Synchronous terminal y/n prompt. Runs in to_thread so it doesn't block the event loop.

    EOF / non-tty environments are treated as rejection — caller must explicitly
    enable stdin to use --require-plan-approval.
    """
    if not (sys.stdin and sys.stdin.isatty()):
        print("\nWARN: require_plan_approval=True but stdin is not a tty; cannot prompt -> treating as reject")
        return False

    print("\n" + "═" * 60)
    print("  PLAN APPROVAL — please review")
    print("═" * 60)
    print(plan_md)
    print("─" * 60)
    print("Parsed contracts:")
    print(summary_lines)
    print("─" * 60)
    sys.stdout.flush()
    while True:
        try:
            ans = input("approve? [y/n]: ").strip().lower()
        except EOFError:
            return False
        if ans in ("y", "yes"):
            return True
        if ans in ("n", "no"):
            return False
        print("  please enter y or n")


def _submit_plan_handler(tool_input: dict, ctx: ToolContext) -> dict:
    if not ctx.spawner:
        raise RuntimeError("ToolContext.spawner is not injected")

    plan_md = tool_input["plan"]
    raw_contracts = parse_plan(plan_md)

    if not raw_contracts:
        return {
            "ok": False,
            "msg": "No checklist items found. Use format: '- [ ] [agent_type] description\\n      output: shared/<name>.<ext>'",
        }

    # Check 1: browser/worker/coding steps must declare output
    missing_output = [c for c in raw_contracts if c["agent_type"] in ("worker", "browser", "coding") and not c.get("output")]
    if missing_output:
        return {
            "ok": False,
            "msg": f"{len(missing_output)} step(s) lack 'output: shared/...' declaration: "
                   + ", ".join(c["description"][:40] for c in missing_output),
        }

    # Check 2: output must start with shared/
    bad_output = [c for c in raw_contracts if c.get("output") and not c["output"].startswith("shared/")]
    if bad_output:
        return {
            "ok": False,
            "msg": f"output paths must start with 'shared/': "
                   + ", ".join(c["output"] for c in bad_output),
        }

    # Check 3: contract names (basename without extension) must be unique
    # — parallel-write isolation: each worker publishes to its own shared/<unique_name>
    name_to_steps: Dict[str, List[str]] = {}
    for c in raw_contracts:
        if c.get("output"):
            n = _basename_no_ext(c["output"])
            name_to_steps.setdefault(n, []).append(c["description"][:60])
    duplicates = {n: descs for n, descs in name_to_steps.items() if len(descs) > 1}
    if duplicates:
        return {
            "ok": False,
            "msg": (
                f"contract output names must be unique (parallel-write isolation): "
                + "; ".join(f"{n} ← [{', '.join(descs)}]" for n, descs in duplicates.items())
            ),
        }

    # Convert all to Contract objects and attach to session state
    state = ctx.spawner.get_session_state(ctx.session_id)
    state.contracts.clear()
    state.has_verification_step = False
    for c in raw_contracts:
        if not c.get("output"):
            continue
        contract = Contract(
            name=_basename_no_ext(c["output"]),
            output_path=c["output"],
            schema=c.get("schema") or "",
            agent_type=c["agent_type"],
            description=c["description"],
        )
        state.add_contract(contract)
    state.plan_md = plan_md

    summary = "\n".join(
        f"  {i+1}. [{c.agent_type}] → {c.output_path}"
        for i, c in enumerate(state.contracts.values())
    )

    # Approval gate — only when user opts in; skip if already approved this session
    if ctx.spawner.require_plan_approval and not state.plan_approved:
        approved = _ask_user_approval(plan_md, summary)
        state.plan_approved = approved
        if not approved:
            # Rejected: roll back contracts
            state.contracts.clear()
            state.has_verification_step = False
            state.plan_md = ""
            return {
                "ok": False,
                "approved": False,
                "msg": "User rejected the plan. Revise and resubmit.",
            }
    else:
        # Approval not required → auto-approve so spawn is not blocked
        state.plan_approved = True

    return {
        "ok": True,
        "approved": True,
        "registered": len(state.contracts),
        "session_id": ctx.session_id,
        "has_verification_step": state.has_verification_step,
        "summary": summary,
        "msg": (
            f"Plan registered with {len(state.contracts)} contract(s). "
            + ("Verification step declared — Lead must spawn it before end_turn. "
               if state.has_verification_step else "")
            + "Subsequent spawn_agent calls matching plan steps will get delivery contracts auto-injected."
        ),
    }


SUBMIT_PLAN = ToolSpec(
    name="submit_plan",
    description=(
        "Register a structured execution plan. Format: markdown checklist where each item is "
        "`- [ ] [agent_type] description\\n      output: shared/<name>.<ext>`. "
        "Optional: `      schema: <description>`. "
        "Each contract output basename MUST be unique within the plan (parallel-write isolation). "
        "Once registered, spawn_agent calls matching the plan's steps will get delivery "
        "contracts auto-injected at the end of their task. "
        "If a [verification] step is declared, Lead MUST spawn it before end_turn — "
        "the system will inject a reminder otherwise. "
        "Use this for any multi-step (3+) job to make delivery contracts explicit."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "plan": {
                "type": "string",
                "description": "Markdown checklist (see format above)",
            },
        },
        "required": ["plan"],
    },
    handler=_submit_plan_handler,
)


# ──────────────────────────────────
# spawn_agent
# ──────────────────────────────────

def _check_approval_gate(ctx: ToolContext) -> Optional[dict]:
    """If require_plan_approval is on but plan is unregistered/unapproved, reject spawn."""
    if not ctx.spawner.require_plan_approval:
        return None
    state = ctx.spawner.session_states.get(ctx.session_id)
    if state and state.plan_approved:
        return None
    return {
        "spawned": False,
        "error": (
            "plan_approval_required: spawner.require_plan_approval=True. "
            "Call submit_plan() first and wait for user approval."
        ),
    }


async def _spawn_agent_handler(tool_input: dict, ctx: ToolContext) -> dict:
    """async handler — base.dispatch awaits automatically."""
    if not ctx.spawner:
        raise RuntimeError("ToolContext.spawner is not injected")

    blocked = _check_approval_gate(ctx)
    if blocked:
        return blocked

    agent_type = tool_input["agent_type"]
    task = tool_input["task"]
    max_turns = int(tool_input.get("max_turns", 0))

    # Inject delivery contract (if plan is registered and matches)
    state = ctx.spawner.session_states.get(ctx.session_id)
    matched: Optional[Contract] = None
    if state:
        matched = find_contract_for_task(list(state.contracts.values()), agent_type, task)

    effective_task = task
    if matched:
        effective_task = task + format_contract_block(matched)

    # Verification reuses the parent worktree (read-only sibling cross-check)
    parent_wt = ctx.worktree if agent_type == "verification" else None

    result = await ctx.spawner.spawn(
        agent_type=agent_type,
        task=effective_task,
        session_id=ctx.session_id,    # same session, shared/ available
        max_turns=max_turns if max_turns > 0 else None,
        parent_worktree=parent_wt,
    )

    # Compact result for the LLM — don't push the full events/context back into context
    return {
        "agent_type": result["agent_type"],
        "success": result["success"],
        "turns_used": result["turns_used"],
        "stop_reason": result["stop_reason"],
        "final_text": (result["final_text"] or "")[:1500],
        "worktree": result["worktree"],
        "contract_injected": bool(matched),
        "contract_name": matched.name if matched else None,
    }


SPAWN_AGENT = ToolSpec(
    name="spawn_agent",
    description=(
        "Spawn a child agent to execute a subtask. "
        "Available agent_types: 'worker' (general execution: browser + code), "
        "'verification' (read-only adversarial check). "
        "Returns {success, turns_used, final_text, worktree}. "
        "If submit_plan was called earlier and the spawned agent_type+task matches a plan step, "
        "the delivery contract is automatically appended to the task — the child agent must publish_artifact accordingly."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "agent_type": {"type": "string", "enum": ["worker", "verification"]},
            "task": {
                "type": "string",
                "description": "Specific subtask description. Be concrete: what data, from where, in what format.",
            },
            "max_turns": {
                "type": "integer",
                "default": 0,
                "description": "Override agent's default max_turns. 0 = use agent default (worker=50, verification=25).",
            },
        },
        "required": ["agent_type", "task"],
    },
    handler=_spawn_agent_handler,
    long_running=True,
)


# ──────────────────────────────────
# spawn_agents_parallel
# ──────────────────────────────────

async def _spawn_parallel_handler(tool_input: dict, ctx: ToolContext) -> dict:
    if not ctx.spawner:
        raise RuntimeError("ToolContext.spawner is not injected")

    blocked = _check_approval_gate(ctx)
    if blocked:
        return blocked

    tasks = tool_input["tasks"]
    if not isinstance(tasks, list) or len(tasks) == 0:
        raise ValueError("tasks must be a non-empty array")
    if len(tasks) > 10:
        raise ValueError(f"max 10 parallel tasks, got {len(tasks)}")

    state = ctx.spawner.session_states.get(ctx.session_id)
    contracts = list(state.contracts.values()) if state else []

    async def _one(t: dict) -> dict:
        agent_type = t["agent_type"]
        task = t["task"]
        max_turns = int(t.get("max_turns", 0))
        matched = find_contract_for_task(contracts, agent_type, task)
        effective_task = task + format_contract_block(matched) if matched else task
        parent_wt = ctx.worktree if agent_type == "verification" else None
        result = await ctx.spawner.spawn(
            agent_type=agent_type,
            task=effective_task,
            session_id=ctx.session_id,
            max_turns=max_turns if max_turns > 0 else None,
            parent_worktree=parent_wt,
        )
        return {
            "agent_type": result["agent_type"],
            "success": result["success"],
            "turns_used": result["turns_used"],
            "stop_reason": result["stop_reason"],
            "final_text": (result["final_text"] or "")[:800],
            "contract_injected": bool(matched),
            "contract_name": matched.name if matched else None,
        }

    # Concurrent execution — note: browser-using workers are still serialized via _browser_lock.
    # True parallelism only applies between code-only / verification jobs.
    results = await asyncio.gather(*[_one(t) for t in tasks], return_exceptions=True)
    out = []
    for i, r in enumerate(results):
        if isinstance(r, Exception):
            out.append({
                "agent_type": tasks[i]["agent_type"],
                "success": False,
                "error": str(r),
            })
        else:
            out.append(r)

    return {
        "spawned": len(tasks),
        "results": out,
        "success_count": sum(1 for r in out if r.get("success")),
    }


SPAWN_AGENTS_PARALLEL = ToolSpec(
    name="spawn_agents_parallel",
    description=(
        "Spawn multiple child agents concurrently. Use when subtasks are independent. "
        "NOTE: in this version, browser-using workers are auto-serialized via a browser_lock — "
        "they won't actually run concurrently. Real parallelism applies to code-only or "
        "verification jobs (or different sites once multi-browser is supported). "
        "Returns {spawned, results, success_count}."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "tasks": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "agent_type": {"type": "string", "enum": ["worker", "verification"]},
                        "task": {"type": "string"},
                        "max_turns": {"type": "integer", "default": 0},
                    },
                    "required": ["agent_type", "task"],
                },
                "minItems": 1,
                "maxItems": 10,
            },
        },
        "required": ["tasks"],
    },
    handler=_spawn_parallel_handler,
    long_running=True,
)


# ──────────────────────────────────
# Clear session plan (called from main.py's /reset)
# ──────────────────────────────────

def clear_plan_for_session(session_id: str, spawner: Optional[Any] = None) -> None:
    """Backward-compat shim — actual state lives in spawner.session_states.

    Noop when spawner is None (the /reset path now calls spawner.clear_session directly).
    """
    if spawner is not None:
        spawner.clear_session(session_id)


ALL_LEAD_TOOLS = [SUBMIT_PLAN, SPAWN_AGENT, SPAWN_AGENTS_PARALLEL]
