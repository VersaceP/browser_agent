"""
harness.tools.lead_tools - LeadAgent tool schemas and dispatch factory.
"""

import json
from pathlib import Path
from typing import Any, Awaitable, Callable, List, Optional, Tuple

from harness.extraction_artifacts import (
    save_extraction_artifact,
    validate_extraction_rows,
)
from harness.lifecycle import LifecycleContext, lifecycle_for
from harness.local_fs import local_fs_jsonpath, local_fs_read, local_fs_search
from harness.strategy_bank import render_strategy_guidance
from harness.task_control import mark_phase_exhausted_if_needed
from harness.tools.loop_guard import check_tool_call_loop
from harness.tools.registry import ToolContext, ToolRegistry
from harness.utils import JsonDict, optional_int


LeadToolDispatcher = Callable[[JsonDict], Awaitable[Tuple[JsonDict, bool]]]

LEAD_TOOLS = ToolRegistry("lead_agent")


def _nullable(type_name: str) -> JsonDict:
    return {"type": [type_name, "null"]}


def _emit_task_plan_schema(_: Any = None) -> JsonDict:
    return {
        "type": "object",
        "properties": {
            "plan": {
                "type": "object",
                "additionalProperties": True,
                "description": (
                    "Plan object with goal, task_type, and a linear phases array."
                    " Each phase needs id, type='browser_worker', objective,"
                    " worker_task, stage_hint, stage_hint_reason,"
                    " expected_artifact, validators, and max_attempts."
                ),
            },
        },
        "required": ["plan"],
        "additionalProperties": False,
    }


def _spawn_browser_agent_schema(_: Any = None) -> JsonDict:
    return {
        "type": "object",
        "properties": {
            "name": {
                **_nullable("string"),
                "description": "BrowserAgent name; pass null to auto-name.",
            },
            "phase_id": {
                **_nullable("string"),
                "description": "The task_plan phase id this worker executes. Pass null to use the next pending phase.",
            },
            "task": {"type": "string"},
            "context": {
                "type": "string",
                "description": (
                    "Subtask context; pass an empty string when none. Include artifact paths"
                    " or prior result fields that the worker may use as dynamic-param sources."
                ),
            },
            "result_contract": {
                "type": "string",
                "description": "Structure / fields you expect the BrowserAgent to put in `answer`; pass an empty string when there are no extra requirements.",
            },
            "max_steps": {
                **_nullable("integer"),
                "description": "Override the worker's max step count; pass null to use the default.",
            },
            "preferred_slot_id": {
                **_nullable("string"),
                "description": (
                    "Optional idle BrowserAgent slotId for an explicit related"
                    " continuation. Passing it allows reusable page candidates"
                    " from that slot to be exposed to the worker."
                ),
            },
            "reuse_from_worker_id": {
                **_nullable("string"),
                "description": (
                    "Optional previous workerId whose idle slot should be reused"
                    " for an explicit related continuation. Passing it allows"
                    " reusable page candidates from that slot to be exposed."
                ),
            },
            "worker_contract": {
                "type": "object",
                "additionalProperties": True,
                "description": (
                    "Contract override; pass {} when the phase contract is enough."
                    " The harness merges it with the"
                    " phase's expected_artifact, validators, allowed_methods,"
                    " forbidden_methods, max_surface_attempts, and stop_condition."
                ),
            },
        },
        "required": [
            "name",
            "phase_id",
            "task",
            "context",
            "result_contract",
            "max_steps",
            "worker_contract",
        ],
        "additionalProperties": False,
    }


def _wait_browser_agents_schema(_: Any = None) -> JsonDict:
    return {
        "type": "object",
        "properties": {
            "worker_ids": {
                "type": ["array", "null"],
                "items": {"type": "string"},
                "description": "List of workerIds to wait for; pass null to wait for every spawned agent.",
            },
            "mode": {
                "type": "string",
                "enum": ["all", "first"],
            },
            "timeout_seconds": {
                **_nullable("number"),
                "description": "Wait timeout in seconds; pass null for no limit.",
            },
        },
        "required": ["worker_ids", "mode", "timeout_seconds"],
        "additionalProperties": False,
    }


def _list_browser_agents_schema(_: Any = None) -> JsonDict:
    return {"type": "object", "properties": {}, "required": [], "additionalProperties": False}


def _local_fs_search_schema(_: Any = None) -> JsonDict:
    return {
        "type": "object",
        "properties": {
            "pattern": {
                "type": "string",
                "description": "Regex grep; pass an empty string to list matches by glob / event_type only.",
            },
            "glob": {
                "type": "string",
                "description": "Glob relative to the current task worktree, e.g. traces/*.jsonl or observations/*.json.",
            },
            "event_type": {
                "type": ["string", "null"],
                "description": "JSONL-only: restrict the search to lines whose `event` matches this string; pass null when not needed.",
            },
            "max_results": {"type": "integer", "minimum": 1, "maximum": 100},
            "max_bytes_per_hit": {"type": "integer", "minimum": 200, "maximum": 20000},
            "max_total_bytes": {"type": "integer", "minimum": 1000, "maximum": 200000},
        },
        "required": [
            "pattern",
            "glob",
            "event_type",
            "max_results",
            "max_bytes_per_hit",
            "max_total_bytes",
        ],
        "additionalProperties": False,
    }


def _local_fs_read_schema(_: Any = None) -> JsonDict:
    return {
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "line_offset": {"type": "integer", "minimum": 0},
            "line_limit": {"type": "integer", "minimum": 1, "maximum": 5000},
            "max_bytes": {"type": "integer", "minimum": 1000, "maximum": 200000},
        },
        "required": ["path", "line_offset", "line_limit", "max_bytes"],
        "additionalProperties": False,
    }


def _local_fs_jsonpath_schema(_: Any = None) -> JsonDict:
    return {
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "expr": {
                "type": "string",
                "description": "Supports $.a.b[0], [*], .*, ..field, ..*.",
            },
            "mode": {
                "type": "string",
                "enum": ["auto", "json", "jsonl"],
            },
            "max_nodes": {"type": "integer", "minimum": 1, "maximum": 500},
            "max_bytes_per_node": {"type": "integer", "minimum": 100, "maximum": 50000},
        },
        "required": [
            "path",
            "expr",
            "mode",
            "max_nodes",
            "max_bytes_per_node",
        ],
        "additionalProperties": False,
    }


def _lead_save_artifact_schema(_: Any = None) -> JsonDict:
    return {
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": "Short dataset name, matching expected_artifact.name when applicable.",
            },
            "rows": {
                "type": "array",
                "items": {"type": "object", "additionalProperties": True},
                "description": "Structured rows using the exact expected field names.",
            },
            "schema": {
                "type": "object",
                "additionalProperties": True,
                "description": "Optional schema/field description for the saved rows.",
            },
            "description": {
                "type": "string",
                "description": "Why this artifact was saved and which evidence it came from.",
            },
            "source_artifacts": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Extraction artifact paths used as evidence for this reshape.",
            },
        },
        "required": ["name", "rows", "schema", "description", "source_artifacts"],
        "additionalProperties": False,
    }


def _final_answer_schema(_: Any = None) -> JsonDict:
    return {
        "type": "object",
        "properties": {
            "status": {
                "type": "string",
                "enum": ["done", "blocked", "failed"],
            },
            "answer": {"type": "string"},
        },
        "required": ["status", "answer"],
        "additionalProperties": False,
    }


def build_lead_tool_dispatcher(agent: Any) -> LeadToolDispatcher:
    async def dispatch(tool_call: JsonDict) -> Tuple[JsonDict, bool]:
        step = getattr(agent, "_current_step", 0)
        lifecycle = lifecycle_for(agent)
        effective_call = lifecycle.tool_pre_call(
            LifecycleContext(actor="lead_agent", step=step),
            tool_call,
        )
        result, should_stop = await execute_lead_tool(agent, effective_call)
        result = lifecycle.tool_post_call(
            LifecycleContext(actor="lead_agent", step=step),
            effective_call,
            result,
        )
        return result, should_stop

    return dispatch


async def execute_lead_tool(agent: Any, tool_call: JsonDict) -> Tuple[JsonDict, bool]:
    name = str(tool_call.get("name") or "")
    raw_tool_input = tool_call.get("input") or {}
    tool_input = raw_tool_input if isinstance(raw_tool_input, dict) else {"value": raw_tool_input}
    action = LEAD_TOOLS.get(name)
    if action is None:
        result = {
            "status": "failed",
            "error": f"Unknown LeadAgent tool: {name}",
        }
        agent.logger.write("lead.tool.error", result)
        return result, False

    if action.loop_guard:
        short_circuit = check_tool_call_loop(
            agent,
            name=name,
            tool_input=tool_input,
            step=getattr(agent, "_current_step", 0),
        )
        if short_circuit is not None:
            return short_circuit

    result = await action.handler(
        ToolContext(
            agent=agent,
            tool_call=tool_call,
            tool_input=tool_input,
            step=getattr(agent, "_current_step", 0),
        )
    )
    return result, action.terminal


@LEAD_TOOLS.register(
    name="emit_task_plan",
    description=(
        "Submit the structured v1 task plan before spawning any worker."
        " The harness validates and persists task_plan.json and task_state.json."
    ),
    input_schema=_emit_task_plan_schema,
    loop_guard=False,
)
async def _lead_emit_task_plan(ctx: ToolContext) -> JsonDict:
    return ctx.agent.accept_task_plan(ctx.tool_input.get("plan"))


@LEAD_TOOLS.register(
    name="spawn_browser_agent",
    description=(
        "Asynchronously run a BrowserAgent worker in a pooled browser slot."
        " Normal workers reuse only the slot connection and must start from a"
        " fresh page. For related continuation work, pass reuse_from_worker_id"
        " or preferred_slot_id when you know which prior slot/page set should"
        " continue."
        " Keep BrowserAgent slots scarce; when related work only needs additional"
        " pages/tabs, put that into one worker as serial Page.create and"
        " Page.switchTo work instead of fan-out."
        " The task/context should state which fields to collect and how to derive dynamic"
        " params from live feedback (response.data handles, DOM.getAXTree ids,"
        " DOM.getText/DOM.getAttribute evidence, or cited record_extraction artifacts),"
        " not hard-code stale pageIds, AXTree ids, selectors, or assumed positions."
    ),
    input_schema=_spawn_browser_agent_schema,
)
async def _lead_spawn_browser_agent(ctx: ToolContext) -> JsonDict:
    agent = ctx.agent
    tool_input = ctx.tool_input
    if getattr(agent, "task_plan", None) is None:
        return {
            "status": "plan_required",
            "error": "LeadAgent must call emit_task_plan successfully before spawning BrowserAgents.",
            "next_instruction": "Emit a valid task_plan with phases, expected_artifact, and validators.",
        }
    exhausted = mark_phase_exhausted_if_needed(agent.task_plan, agent.logger)
    phase_id = tool_input.get("phase_id")
    phase = agent.resolve_phase_for_spawn(
        str(phase_id) if isinstance(phase_id, str) and phase_id.strip() else None
    )
    if phase is None:
        exhausted_match = _matching_exhaustion(exhausted, phase_id)
        if exhausted_match is not None:
            return {
                "status": "phase_exhausted",
                "phaseId": exhausted_match.get("phaseId"),
                "attempts": exhausted_match.get("attempts"),
                "max_attempts": exhausted_match.get("max_attempts"),
                "last_failure": exhausted_match.get("last_failure"),
                "classification": exhausted_match.get("classification"),
                "next_instruction": (
                    "Emit a revised task_plan with replan_reason that changes"
                    " strategy, contract, task_type, decomposition, or stop with"
                    " final_answer if no recovery path remains."
                ),
            }
        return {
            "status": "failed",
            "error": f"phase not found or no pending phase: {phase_id}",
        }
    raw_contract = tool_input.get("worker_contract")
    worker_contract = agent.build_worker_contract(
        phase,
        raw_contract if isinstance(raw_contract, dict) else None,
    )
    strategies = (
        agent.strategies_for_phase(phase)
        if hasattr(agent, "strategies_for_phase")
        else []
    )
    strategy_guidance = render_strategy_guidance(strategies)
    worker_contract["strategy_ids"] = [
        str(item.get("id"))
        for item in strategies
        if isinstance(item, dict) and str(item.get("id") or "").strip()
    ]
    _merge_strategy_avoid_tools(worker_contract, strategies)
    base_task = str(tool_input.get("task") or phase.get("worker_task") or "")
    base_context = str(tool_input.get("context") or phase.get("context") or "")
    if strategy_guidance:
        base_context = f"{base_context}\n\n{strategy_guidance}".strip()
    return await agent.spawner.spawn_browser_agent(
        task=base_task,
        context=base_context,
        name=tool_input.get("name") or None,
        max_steps=tool_input.get("max_steps"),
        result_contract=str(tool_input.get("result_contract") or ""),
        phase_id=str(phase.get("id") or ""),
        worker_contract=worker_contract,
        phase=phase,
        preferred_slot_id=tool_input.get("preferred_slot_id"),
        reuse_from_worker_id=tool_input.get("reuse_from_worker_id"),
    )


def _merge_strategy_avoid_tools(worker_contract: JsonDict, strategies: List[JsonDict]) -> None:
    forbidden = [
        str(item).strip()
        for item in worker_contract.get("forbidden_methods", [])
        if str(item).strip()
    ]
    allowed = {
        str(item).strip()
        for item in worker_contract.get("allowed_methods", [])
        if str(item).strip()
    }
    seen = set(forbidden)
    for strategy in strategies:
        if not isinstance(strategy, dict):
            continue
        for method in strategy.get("avoid_tools") or []:
            text = str(method).strip()
            if not text or text in seen or text in allowed:
                continue
            forbidden.append(text)
            seen.add(text)
    if forbidden:
        worker_contract["forbidden_methods"] = forbidden


@LEAD_TOOLS.register(
    name="wait_browser_agents",
    description="Wait for spawned BrowserAgents to complete; wait for all of them or the first one to finish.",
    input_schema=_wait_browser_agents_schema,
)
async def _lead_wait_browser_agents(ctx: ToolContext) -> JsonDict:
    return await ctx.agent.spawner.wait_browser_agents(
        worker_ids=ctx.tool_input.get("worker_ids"),
        mode=ctx.tool_input.get("mode", "all"),
        timeout_seconds=ctx.tool_input.get("timeout_seconds"),
    )


@LEAD_TOOLS.register(
    name="list_browser_agents",
    description=(
        "Inspect workers and the pooled BrowserAgent slots. Use idle slotId or"
        " a previous workerId only when spawning explicit related continuation"
        " work that should see reusable page candidates."
    ),
    input_schema=_list_browser_agents_schema,
)
async def _lead_list_browser_agents(ctx: ToolContext) -> JsonDict:
    return ctx.agent.spawner.list_browser_agents()


@LEAD_TOOLS.register(
    name="local_fs_search",
    description="Read-only search across files inside the current task worktree; supports glob, JSONL event-type filtering, and per-hit / total output caps.",
    input_schema=_local_fs_search_schema,
)
async def _lead_local_fs_search(ctx: ToolContext) -> JsonDict:
    tool_input = ctx.tool_input
    return local_fs_search(
        ctx.agent.logger,
        glob_pattern=str(tool_input.get("glob") or "**/*"),
        pattern=(
            str(tool_input.get("pattern"))
            if tool_input.get("pattern") is not None else None
        ),
        event_type=(
            str(tool_input.get("event_type"))
            if tool_input.get("event_type") is not None else None
        ),
        max_results=optional_int(tool_input.get("max_results"), 20) or 20,
        max_bytes_per_hit=(
            optional_int(tool_input.get("max_bytes_per_hit"), 2000) or 2000
        ),
        max_total_bytes=(
            optional_int(tool_input.get("max_total_bytes"), 20000) or 20000
        ),
    )


@LEAD_TOOLS.register(
    name="local_fs_read",
    description="Read-only line-range read of a file inside the current task worktree; well suited to JSONL traces and AXTree lines.txt offload files.",
    input_schema=_local_fs_read_schema,
)
async def _lead_local_fs_read(ctx: ToolContext) -> JsonDict:
    tool_input = ctx.tool_input
    return local_fs_read(
        ctx.agent.logger,
        path=str(tool_input.get("path") or ""),
        line_offset=optional_int(tool_input.get("line_offset"), 0) or 0,
        line_limit=optional_int(tool_input.get("line_limit"), 200) or 200,
        max_bytes=min(
            optional_int(
                tool_input.get("max_bytes"),
                ctx.agent.runtime.harness.local_fs_max_read_bytes,
            ) or ctx.agent.runtime.harness.local_fs_max_read_bytes,
            ctx.agent.runtime.harness.local_fs_max_read_bytes,
        ),
    )


@LEAD_TOOLS.register(
    name="local_fs_jsonpath",
    description="Read-only read of a JSON/JSONL file inside the current task worktree, with a JSONPath subset for node extraction.",
    input_schema=_local_fs_jsonpath_schema,
)
async def _lead_local_fs_jsonpath(ctx: ToolContext) -> JsonDict:
    tool_input = ctx.tool_input
    return local_fs_jsonpath(
        ctx.agent.logger,
        path=str(tool_input.get("path") or ""),
        expr=str(tool_input.get("expr") or "$"),
        mode=str(tool_input.get("mode") or "auto"),
        max_nodes=optional_int(tool_input.get("max_nodes"), 50) or 50,
        max_bytes_per_node=(
            optional_int(tool_input.get("max_bytes_per_node"), 1000) or 1000
        ),
    )


@LEAD_TOOLS.register(
    name="lead_save_artifact",
    description=(
        "Persist structured rows prepared by LeadAgent as a standard"
        " extraction artifact under the current task worktree. Use this after"
        " schema_mismatch reshape from trusted evidence instead of re-scraping."
    ),
    input_schema=_lead_save_artifact_schema,
)
async def _lead_save_artifact(ctx: ToolContext) -> JsonDict:
    agent = ctx.agent
    tool_input = ctx.tool_input
    raw_name = str(tool_input.get("name") or "").strip()
    if not raw_name:
        return {"status": "rejected", "error": "name required"}
    rows, error = validate_extraction_rows(tool_input.get("rows"))
    if error is not None:
        return error
    raw_sources = tool_input.get("source_artifacts") or []
    if isinstance(raw_sources, str):
        raw_sources = [raw_sources]
    source_artifacts, source_error = _validate_lead_save_sources(agent, raw_sources)
    if source_error is not None:
        return source_error
    return save_extraction_artifact(
        logger=agent.logger,
        runtime=agent.runtime,
        artifacts=None,
        name=raw_name,
        rows=rows or [],
        schema=tool_input.get("schema"),
        description=str(tool_input.get("description") or ""),
        source_artifacts=source_artifacts,
        event_type="tool.lead_save_artifact",
    )


def _validate_lead_save_sources(agent: Any, raw_sources: Any) -> Tuple[List[str], Optional[JsonDict]]:
    if not isinstance(raw_sources, list):
        return [], {
            "status": "rejected",
            "error": "source_artifacts must be a non-empty array of extraction artifact paths",
        }
    source_texts = [
        str(path).strip()
        for path in raw_sources
        if isinstance(path, str) and str(path).strip()
    ]
    if not source_texts:
        return [], {
            "status": "rejected",
            "error": "lead_save_artifact requires at least one source extraction artifact",
            "next_instruction": (
                "Read a worker record_extraction or extractionAttemptArtifacts path"
                " first; do not create reshaped rows without cited source evidence."
            ),
        }

    task_dir = Path(getattr(getattr(agent, "logger", None), "task_dir", "") or ".")
    try:
        task_root = task_dir.resolve(strict=False)
    except (OSError, ValueError):
        task_root = task_dir.absolute()

    validated: List[str] = []
    for source in source_texts:
        try:
            path = Path(source).expanduser().resolve(strict=False)
        except (OSError, ValueError) as exc:
            return [], {
                "status": "rejected",
                "error": f"invalid source_artifact path: {source}",
                "details": str(exc),
            }
        try:
            path.relative_to(task_root)
        except ValueError:
            return [], {
                "status": "rejected",
                "error": "source_artifact must stay inside the current task worktree",
                "sourceArtifact": str(path),
                "taskDir": str(task_root),
            }
        normalized = str(path).replace("\\", "/")
        if "/artifacts/extractions/" not in normalized:
            return [], {
                "status": "rejected",
                "error": "source_artifact must be an extraction artifact path",
                "sourceArtifact": str(path),
            }
        if not path.exists() or not path.is_file():
            return [], {
                "status": "rejected",
                "error": "source_artifact does not exist",
                "sourceArtifact": str(path),
            }
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            return [], {
                "status": "rejected",
                "error": "source_artifact must be readable JSON",
                "sourceArtifact": str(path),
                "details": str(exc),
            }
        if not isinstance(payload, dict) or not isinstance(payload.get("rows"), list):
            return [], {
                "status": "rejected",
                "error": "source_artifact must contain a rows array",
                "sourceArtifact": str(path),
            }
        text = str(path)
        if text not in validated:
            validated.append(text)
    return validated, None


@LEAD_TOOLS.register(
    name="final_answer",
    description="Terminate LeadAgent orchestration and return the final result.",
    input_schema=_final_answer_schema,
    terminal=True,
    loop_guard=False,
)
async def _lead_final_answer(ctx: ToolContext) -> JsonDict:
    return {
        "status": ctx.tool_input.get("status", "done"),
        "answer": str(ctx.tool_input.get("answer", "")).strip(),
        "trigger": "lead_decided",
    }


def _matching_exhaustion(exhausted: List[JsonDict], phase_id: Any) -> Any:
    if not exhausted:
        return None
    wanted = str(phase_id or "").strip()
    if wanted:
        for item in exhausted:
            if str(item.get("phaseId") or "") == wanted:
                return item
        return None
    return exhausted[-1]


def build_lead_agent_tool_specs() -> List[JsonDict]:
    return LEAD_TOOLS.tool_specs()
