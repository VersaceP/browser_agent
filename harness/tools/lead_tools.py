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
from harness.fleet_coordinator import VALID_PAGE_POLICIES, VALID_REUSE_SCOPES
from harness.lifecycle import LifecycleContext, lifecycle_for
from harness.local_fs import local_fs_read, local_fs_search
from harness.strategy_bank import render_strategy_guidance
from harness.task_control import VALIDATOR_TYPES, mark_phase_exhausted_if_needed
from harness.task_types import (
    VALID_TASK_TYPES,
    normalize_task_type,
    task_type_choices_for_error,
)
from harness.tools.loop_guard import check_tool_call_loop
from harness.tools.registry import ToolContext, ToolRegistry
from harness.utils import (
    JsonDict,
    contains_affirmative_semantic_marker,
    contains_semantic_marker,
    optional_int,
)


LeadToolDispatcher = Callable[[JsonDict], Awaitable[Tuple[JsonDict, bool]]]

LEAD_TOOLS = ToolRegistry("lead_agent")


def _nullable(type_name: str) -> JsonDict:
    return {"type": [type_name, "null"]}


def _auth_verification_schema() -> JsonDict:
    return {
        "type": "object",
        "description": (
            "Optional pre-HITL proof contract for durable session reuse. Both"
            " the protected URL and an authenticated UI marker must match;"
            " without this contract HITL may clear the current barrier but the"
            " fleet is not persisted as a verified login session."
        ),
        "properties": {
            "protected_url_prefixes": {
                "type": "array",
                "minItems": 1,
                "items": {"type": "string"},
            },
            "authenticated_markers": {
                "type": "array",
                "minItems": 1,
                "description": (
                    "Stable visible AX nodes that prove authentication. Match"
                    " is exact; ordinary page text and hidden/blocked nodes do"
                    " not count."
                ),
                "items": {
                    "type": "object",
                    "properties": {
                        "role": {
                            "type": "string",
                            "pattern": "^[A-Za-z][A-Za-z0-9_-]*$",
                        },
                        "name": {"type": "string", "minLength": 3},
                        "match": {"type": "string", "enum": ["exact"]},
                    },
                    "required": ["role", "name"],
                    "additionalProperties": False,
                },
            },
        },
        "required": ["protected_url_prefixes", "authenticated_markers"],
        "additionalProperties": False,
    }


def _validator_item_schema() -> JsonDict:
    """Typed schema for one plan validator.

    The type enum is generated from VALIDATOR_TYPES (single source of truth)
    so the model sees the exact canonical names UP FRONT — task 9d5655d3
    burned two plan rejections learning them from error messages because the
    old schema was an opaque `additionalProperties: true` object.
    """
    return {
        "type": "object",
        "properties": {
            "type": {
                "type": "string",
                "enum": sorted(VALIDATOR_TYPES),
            },
            "field": {
                "type": "string",
                "description": "Target field for single-field validators (range/url_pattern/field_pattern).",
            },
            "fields": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Target fields for required_fields/field_nonempty/unique.",
            },
            "count": {
                "type": "integer",
                "minimum": 1,
                "description": "Row count for exact_rows (aliases value/exact accepted).",
            },
            "value": {"type": "integer", "minimum": 1},
            "min": {"type": "number"},
            "max": {"type": "number"},
            "pattern": {
                "type": "string",
                "description": "Regex for url_pattern/field_pattern.",
            },
            "values": {
                "type": "array",
                "items": {},
                "description": (
                    "Exact allowed value set for set_equals; use this for"
                    " non-contiguous targets such as ranks [38, 40]."
                ),
            },
        },
        "required": ["type"],
        "additionalProperties": True,
    }


def _expected_artifact_schema() -> JsonDict:
    # `type: [...]` is already used by this tool surface (_nullable). Avoid
    # introducing oneOf here: several Anthropic-compatible gateways implement
    # only a conservative JSON-schema subset even though native providers accept
    # oneOf. Runtime normalization still validates object field specs fully.
    field_items: JsonDict = {
        "type": ["string", "object"],
        "description": (
            "A field name string, or an object field spec using name/field/key"
            " plus optional type/allow_empty/nonempty metadata."
        ),
    }
    field_list: JsonDict = {"type": "array", "items": field_items}
    return {
        "type": "object",
        "description": (
            "Structured output contract. Declare name, fields and row-count"
            " constraints here; equivalent explicit validators are accepted"
            " but normalized/deduplicated by the harness."
        ),
        "properties": {
            "name": {"type": "string"},
            "fields": field_list,
            "required_fields": field_list,
            "exact_rows": {"type": "integer", "minimum": 1},
            "min_rows": {"type": "integer", "minimum": 1},
            "max_rows": {"type": "integer", "minimum": 1},
            "count_range": {
                "type": "array",
                "items": {"type": "integer"},
                "minItems": 2,
                "maxItems": 2,
            },
            "nonempty_fields": field_list,
            "field_nonempty": field_list,
            "provenance_required": field_list,
        },
        "propertyNames": {"minLength": 1},
        "additionalProperties": True,
    }


def _emit_task_plan_schema(_: Any = None) -> JsonDict:
    return {
        "type": "object",
        "properties": {
            "plan": {
                "type": "object",
                "description": (
                    "Plan object with goal, task_type, and a phases array."
                    " Each phase needs id, type='browser_worker', objective,"
                    " worker_task, stage_hint, stage_hint_reason,"
                    " expected_artifact, validators, and max_attempts."
                    " Scheduling: depends_on OMITTED = the phase implicitly"
                    " depends on ALL phases listed before it (strict serial"
                    " order); depends_on=[] = independent, startable"
                    " immediately; depends_on=[ids] = exactly those phases"
                    " must be validated_done first. Phases whose dependencies"
                    " are satisfied can be spawned in parallel."
                ),
                "properties": {
                    "goal": {"type": "string"},
                    "task_type": {
                        "type": "string",
                        # Canonical names only — legacy aliases are accepted at
                        # runtime with a warning receipt, same policy as the
                        # validator type enum.
                        "enum": sorted(VALID_TASK_TYPES),
                    },
                    "phases": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "id": {"type": "string"},
                                "type": {"type": "string"},
                                "task_type": {
                                    "type": "string",
                                    "enum": sorted(VALID_TASK_TYPES),
                                    "description": (
                                        "Optional per-phase override of the"
                                        " plan task_type (e.g. one"
                                        " file_download phase inside a"
                                        " web_scrape plan). Omit to inherit"
                                        " the plan task_type."
                                    ),
                                },
                                "objective": {"type": "string"},
                                "worker_task": {"type": "string"},
                                "stage_hint": {"type": "string"},
                                "stage_hint_reason": {"type": "string"},
                                "expected_artifact": {
                                    **_expected_artifact_schema(),
                                },
                                "depends_on": {
                                    "type": "array",
                                    "items": {"type": "string"},
                                    "description": (
                                        "Phase ids that must be validated_done"
                                        " before this phase can start. OMIT for"
                                        " strict serial order (implicitly"
                                        " depends on all prior phases); [] for"
                                        " an independent phase; list only the"
                                        " true data dependencies (e.g. every"
                                        " detail phase depends only on the"
                                        " collection phase) so independent"
                                        " phases can run in parallel."
                                    ),
                                },
                                "validators": {
                                    "type": "array",
                                    "description": (
                                        "MUST be an array of typed objects"
                                        " (never a dict keyed by validator"
                                        " name)."
                                    ),
                                    "items": _validator_item_schema(),
                                },
                                "worker_contract": {
                                    "type": "object",
                                    "properties": {
                                        "reuse_scope": {
                                            "type": "string",
                                            "enum": sorted(VALID_REUSE_SCOPES),
                                        },
                                        "session_key": {"type": "string"},
                                        "page_policy": {
                                            "type": "string",
                                            "enum": sorted(VALID_PAGE_POLICIES),
                                        },
                                        "needs_isolated_session": {"type": "boolean"},
                                        "auth_verification": _auth_verification_schema(),
                                    },
                                    "additionalProperties": True,
                                },
                                "max_attempts": {"type": "integer"},
                            },
                            "required": [
                                "id",
                                "objective",
                                "worker_task",
                                "stage_hint",
                                "stage_hint_reason",
                                "expected_artifact",
                                "validators",
                            ],
                            "additionalProperties": True,
                        },
                    },
                },
                "required": ["goal", "task_type", "phases"],
                "additionalProperties": True,
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
            "reuse_scope": {
                "type": ["string", "null"],
                "enum": [*sorted(VALID_REUSE_SCOPES), None],
                "description": (
                    "Fleet/page reuse boundary. Omit or use connection for a"
                    " fresh page in the slot's assigned fleet; fleet keeps the"
                    " same fleet/session with a fresh page; page explicitly"
                    " exposes prior pages for a related continuation."
                ),
            },
            "session_key": {
                **_nullable("string"),
                "description": (
                    "Stable harness session-affinity key for related phases."
                    " First use creates a fresh fleet; later uses bind only to"
                    " that exact fleet and fail terminally if it is lost. It is"
                    " not an account credential and must not contain secrets."
                ),
            },
            "page_policy": {
                "type": ["string", "null"],
                "enum": [*sorted(VALID_PAGE_POLICIES), None],
                "description": (
                    "Use new for a fresh page in assignedFleetId. existing is"
                    " valid only with reuse_scope=page."
                ),
            },
            "worker_contract": {
                "type": "object",
                "additionalProperties": True,
                "properties": {
                    "task_type": {
                        "type": "string",
                        "enum": sorted(VALID_TASK_TYPES),
                        "description": (
                            "Optional task_type override for this worker;"
                            " method access follows its policy. Omit to use"
                            " the phase/plan task_type."
                        ),
                    },
                    "needs_isolated_session": {
                        "type": "boolean",
                        "description": (
                            "Request coordinator creation of a distinct fleet"
                            " because cookies/storage/proxy identity must not be"
                            " shared with the slot default. The resulting fleet"
                            " never becomes the generic slot default."
                        ),
                    },
                    "auth_verification": _auth_verification_schema(),
                },
                "description": (
                    "Contract override; pass {} when the phase contract is enough."
                    " The harness merges it with the"
                    " phase's expected_artifact, validators, allowed_methods,"
                    " forbidden_methods, max_surface_attempts, and stop_condition."
                    " Optional: set skill_id (a known reusable skill) +"
                    " skill_variables (its required inputs, e.g. detailUrl) to run"
                    " that skill's fast path. If spawn_browser_agent returns"
                    " skill_selection_required, read candidate skillMarkdown and"
                    " retry with skill_id+skill_variables, or decline with"
                    " skill_selection={\"use_skill\":false,\"reason\":\"...\"}."
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
        " The coordinator assigns a fleet before execution; normal workers"
        " start a fresh page in that fleet. Use reuse_scope/session_key for"
        " cookie/session affinity (a new key starts a fresh fleet), and"
        " reuse_scope=page plus"
        " reuse_from_worker_id or preferred_slot_id only when prior pages must"
        " be exposed."
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
    # The override contract must reach the pre-check: a spawn that genuinely
    # changes the objective via worker_contract would otherwise be rejected
    # against the raw phase's exhausted fingerprint before ever reaching the
    # spawner (which already receives the effective contract).
    raw_contract = tool_input.get("worker_contract")
    # Runtime twin of the plan-time task_type check: execute_lead_tool does no
    # local JSON-schema validation, so the spawn schema's enum only constrains
    # a well-behaved provider — a gateway that ignores schemas (the recurring
    # failure class here) can still send anything. tool_policy fail-opens on
    # unknown task_type (dict lookup → no disabled domains), so an unchecked
    # override typo like 'scraping' would re-enable Download/File on a
    # web_scrape phase. Reject loud before the contract is built.
    if isinstance(raw_contract, dict):
        raw_task_type = str(raw_contract.get("task_type") or "").strip()
        if raw_task_type:
            canonical_task_type = normalize_task_type(raw_task_type)
            if canonical_task_type not in VALID_TASK_TYPES:
                return {
                    "status": "invalid_worker_contract",
                    "error": (
                        "worker_contract.task_type must be one of"
                        f" {task_type_choices_for_error()}; got {raw_task_type!r}"
                    ),
                    "tool_was_executed": False,
                    "next_instruction": (
                        "Retry spawn_browser_agent with a canonical task_type,"
                        " or omit worker_contract.task_type to inherit the"
                        " phase/plan task_type. Never invent task_type names:"
                        " an unknown value would bypass method policy."
                    ),
                }
            raw_contract["task_type"] = canonical_task_type
    phase, rejection = agent.resolve_phase_for_spawn_with_rejection(
        str(phase_id) if isinstance(phase_id, str) and phase_id.strip() else None,
        worker_contract=raw_contract if isinstance(raw_contract, dict) else None,
    )
    if phase is None:
        # Pass the structured rejection through verbatim: it carries the real
        # status (dependency_not_ready / blocked_by_dependency /
        # phase_already_running / objective_exhausted / ...) plus a
        # next_instruction. Task 2ed5a466 collapsed these into a generic
        # "phase not found" and the Lead blind-retried a dependency-gated
        # phase twice.
        if rejection is not None:
            return rejection
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
    auth_gate_guidance = _auth_gate_probe_guidance(phase, worker_contract)
    collection_guidance = _collection_contract_guidance(phase, worker_contract)
    if auth_gate_guidance:
        base_context = f"{base_context}\n\n{auth_gate_guidance}".strip()
    if collection_guidance:
        base_context = f"{base_context}\n\n{collection_guidance}".strip()
    if strategy_guidance:
        base_context = f"{base_context}\n\n{strategy_guidance}".strip()
    # Skill selection is a LeadAgent decision gate. Soft recall returns candidate
    # SKILL.md content first; the LeadAgent must retry with explicit skill_id or
    # an explicit decline. The worker fast path then takes the explicit path.
    try:
        spawner = getattr(agent, "spawner", None)
        runtime = getattr(spawner, "runtime", None)
        harness_cfg = getattr(runtime, "harness", None)
        registry = spawner._get_skill_registry() if spawner is not None and hasattr(spawner, "_get_skill_registry") else None
        if registry is not None and getattr(harness_cfg, "skill_fast_path_enabled", True):
            from harness.skill.contract import (
                apply_forced_skill,
                build_skill_selection_request,
                enrich_worker_contract_with_skill,
            )
            from harness.skill.guidance import default_guidance_health
            from harness.skill.health import default_health
            # Operator override wins first: a configured forced_skill_id stamps
            # skill_id (clearing any Lead decline), so selection is skipped and the
            # worker runs that skill wherever its variables are derivable.
            selection_mode = str(getattr(harness_cfg, "skill_selection_mode", "manual") or "manual")
            forced = apply_forced_skill(
                worker_contract,
                registry=registry,
                forced_skill_id=str(getattr(harness_cfg, "forced_skill_id", "") or ""),
                phase=phase,
                logger=agent.logger,
                workflow_health=default_health(),
                guidance_health=default_guidance_health(),
            )
            if not forced:
                # manual mode: the Lead is never interrupted with a selection
                # request — only the user's /skill choice engages a skill.
                selection_request = build_skill_selection_request(
                    worker_contract,
                    registry=registry,
                    phase=phase,
                    task=base_task,
                    context=base_context,
                    logger=agent.logger,
                    mode=selection_mode,
                )
                if selection_request is not None:
                    return selection_request
            enrich_worker_contract_with_skill(
                worker_contract, registry=registry, phase=phase,
                task=base_task, context=base_context, logger=agent.logger,
                mode=selection_mode,
            )
    except Exception:  # never break spawning
        pass
    return await agent.spawner.spawn_browser_agent(
        task=base_task,
        context=base_context,
        name=tool_input.get("name") or None,
        max_steps=tool_input.get("max_steps"),
        result_contract=str(tool_input.get("result_contract") or ""),
        phase_id=str(phase.get("id") or ""),
        worker_contract=worker_contract,
        phase=phase,
        task_plan=getattr(agent, "task_plan", None),
        preferred_slot_id=tool_input.get("preferred_slot_id"),
        reuse_from_worker_id=tool_input.get("reuse_from_worker_id"),
        reuse_scope=tool_input.get("reuse_scope"),
        session_key=tool_input.get("session_key"),
        page_policy=tool_input.get("page_policy"),
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


_AUTH_GATE_FIELD_NAMES = {
    "auth_required",
    "authentication_required",
    "login_required",
    "signin_required",
    "sign_in_required",
    "requires_login",
    "requires_auth",
    "auth_method",
    "authentication_method",
    "login_method",
    "auth_surface",
    "login_surface",
    "auth_evidence",
    "login_evidence",
    "next_phase_requires_hitl",
}

_AUTH_GATE_MARKERS = (
    "auth",
    "authentication",
    "authenticate",
    "login",
    "log in",
    "logged in",
    "sign in",
    "signin",
    "sso",
    "oauth",
    "credential",
    "password",
    "captcha",
    "human verification",
    "identity verification",
    "phone verification",
    "security verification",
    "verification code",
    "sms verification",
    "2fa",
    "mfa",
    "paywall",
    "subscribe",
    "subscription",
    "hitl",
    "登录",
    "登陆",
    "认证",
    "手机号",
    "验证码",
    "人机",
    "扫码",
    "微信",
    "付费墙",
)

_AUTH_GATE_PROBE_MARKERS = (
    "probe",
    "explore",
    "assess",
    "identify",
    "detect",
    "discover",
    "check auth",
    "check authentication",
    "check login",
    "check sign-in",
    "check signin",
    "check gate",
    "check paywall",
    "inspect",
    "understand",
    "requirements",
    "page state",
    "visible",
    "门禁",
    "探测",
    "探索",
    "识别",
    "判断",
    "确认",
    "可见",
)

_AUTH_GATE_EXECUTION_MARKERS = (
    "handle login",
    "handle auth",
    "handle authentication",
    "complete login",
    "complete auth",
    "complete authentication",
    "perform login",
    "perform auth",
    "perform authentication",
    "requestpause",
    "request pause",
    "request hitl",
    "hitl.requestpause",
    "after login",
    "after authentication",
    "post-login",
    "post login",
    "post-auth",
    "post auth",
    "verify login",
    "verify auth",
    "verify authentication",
    "verify authenticated",
    "verify by page.getstate",
    "login verification",
    "auth verification",
    "authentication verification",
    "login status",
    "login_status",
    "form accessible",
    "form_accessible",
    "完成登录",
    "完成认证",
    "处理登录",
    "处理认证",
    "请求 hitl",
    "人工登录",
    "登录后",
    "认证后",
    "验证登录",
    "验证认证",
)

_POST_AUTH_TARGET_MARKERS = (
    "form",
    "field",
    "section",
    "fill",
    "submit",
    "row",
    "item",
    "detail",
    "download",
    "collect",
    "extract",
    "list",
    "表单",
    "字段",
    "版块",
    "部分",
    "填写",
    "提交",
    "采集",
    "下载",
    "详情",
)


def _auth_gate_probe_guidance(phase: JsonDict, worker_contract: JsonDict) -> str:
    """Inject guardrails only for an explicitly diagnostic gate probe.

    The trigger is intentionally semantic rather than site-specific: it catches
    phases whose final deliverable is gate diagnosis, while skipping business
    phases and phases whose job explicitly includes requesting HITL/login.
    Unpredicted gates in ordinary work are handled by the BrowserAgent's global
    runtime-auth interrupt SOP, not by ending this worker and spawning another.
    """
    expected = (
        worker_contract.get("expected_artifact")
        if isinstance(worker_contract.get("expected_artifact"), dict)
        else phase.get("expected_artifact")
        if isinstance(phase.get("expected_artifact"), dict)
        else {}
    )
    fields = _expected_field_names(expected)
    normalized_fields = {
        str(item or "").strip().lower()
        for item in fields
        if str(item or "").strip()
    }
    has_gate_fields = bool(normalized_fields & _AUTH_GATE_FIELD_NAMES)

    validators = worker_contract.get("validators")
    if not isinstance(validators, list):
        validators = phase.get("validators")

    parts = [
        phase.get("id"),
        phase.get("objective"),
        phase.get("worker_task"),
        phase.get("stage_hint"),
        phase.get("stage_hint_reason"),
        phase.get("context"),
        worker_contract.get("task_type"),
        worker_contract.get("objective"),
        worker_contract.get("stage_hint"),
        worker_contract.get("stage_hint_reason"),
        *fields,
    ]
    for structured in (expected, validators):
        if structured:
            try:
                parts.append(json.dumps(structured, ensure_ascii=False, default=str))
            except TypeError:
                parts.append(str(structured))
    text = " ".join(str(item or "") for item in parts)
    if not any(
        contains_semantic_marker(text, marker)
        for marker in _AUTH_GATE_MARKERS
    ):
        return ""

    is_probe = has_gate_fields or any(
        contains_semantic_marker(text, marker)
        for marker in _AUTH_GATE_PROBE_MARKERS
    )
    if not is_probe:
        return ""

    if any(
        contains_affirmative_semantic_marker(text, marker)
        for marker in _AUTH_GATE_EXECUTION_MARKERS
    ):
        return ""

    target_terms = [
        marker
        for marker in _POST_AUTH_TARGET_MARKERS
        if contains_semantic_marker(text, marker)
    ][:8]
    target_note = ""
    if target_terms:
        target_note = (
            "- Treat target-content terms in this phase as post-auth scope if a"
            f" gate is present: {target_terms!r}. Mark them behind_auth or"
            " unknown; do not spend steps discovering them before auth.\n"
        )

    return (
        "<auth_gate_probe_guidance>\n"
        "- The phase contract explicitly makes gate diagnosis the final deliverable; this is the narrow probe-only exception to the runtime-auth interrupt rule.\n"
        "- Stop as soon as login/auth/SSO/OAuth/QR/phone verification/CAPTCHA/HITL/paywall is confirmed with Page.getState and DOM.getAXTree evidence.\n"
        "- Report only gate facts: auth_required/login_required, auth_surface, auth_method/options/providers, current URL/title, evidence text/source, and whether the next phase needs HITL.\n"
        "- Because the user asked only for diagnosis, do not call Hitl.requestPause, dismiss auth/paywall overlays, click provider/login/submit buttons, fill credentials, direct-navigate around the gate, or inspect post-auth form/list/detail/download fields.\n"
        f"{target_note}"
        "- If the protected target is not visible before auth, return it as behind_auth/unknown and finish this diagnostic contract; do not assume or request a follow-up login/HITL phase.\n"
        "</auth_gate_probe_guidance>"
    )


def _collection_contract_guidance(phase: JsonDict, worker_contract: JsonDict) -> str:
    """Inject rank/window collection guardrails derived from the contract."""
    stage = str(
        worker_contract.get("stage_hint")
        or phase.get("stage_hint")
        or ""
    ).strip()
    if stage != "collection":
        return ""
    expected = (
        worker_contract.get("expected_artifact")
        if isinstance(worker_contract.get("expected_artifact"), dict)
        else {}
    )
    fields = _expected_field_names(expected)
    if not fields:
        return ""
    field_set = set(fields)
    rank_like = {"rank", "position", "index"} & field_set
    link_like = {"detailUrl", "url", "href", "productUrl"} & field_set
    name_like = {"productName", "name", "title"} & field_set
    if not (rank_like and link_like and name_like):
        return ""
    exact_rows = _exact_rows_from_contract(worker_contract)
    exact_text = (
        f"exactly {exact_rows} rows"
        if exact_rows is not None
        else "the requested row count"
    )
    return (
        "<collection_contract_guidance>\n"
        f"- The final record_extraction must satisfy fields {fields!r} and produce {exact_text}; do not treat a broad link harvest as final target data.\n"
        "- Use collect_items for repeated list/card candidates when useful, but first verify the selector represents the product/card sequence, not all /ai/ links, featured links, nav links, or comment links.\n"
        "- If collect_items returns many more rows than expected or recordExtraction.status=needs_fix, treat the selector as too broad or the row schema as wrong. Narrow the repeated card selector or transform/slice trusted DOM-order rows before final_answer.\n"
        "- For rank/position tasks, derive rank from the verified DOM/visual order and persist canonical provenance fields such as rankEvidenceText, sourceTool, sourceSelectorOrAxId, and pageUrl.\n"
        "</collection_contract_guidance>"
    )


def _expected_field_names(expected: JsonDict) -> List[str]:
    raw_fields = expected.get("required_fields")
    if not isinstance(raw_fields, list) or not raw_fields:
        raw_fields = expected.get("fields")
    if not isinstance(raw_fields, list):
        return []
    out: List[str] = []
    seen = set()
    for item in raw_fields:
        value = (
            item.get("name") or item.get("field") or item.get("key")
            if isinstance(item, dict)
            else item
        )
        text = str(value or "").strip()
        if text and text not in seen:
            out.append(text)
            seen.add(text)
    return out


def _exact_rows_from_contract(worker_contract: JsonDict) -> Optional[int]:
    expected = (
        worker_contract.get("expected_artifact")
        if isinstance(worker_contract.get("expected_artifact"), dict)
        else {}
    )
    value = optional_int(expected.get("exact_rows"))
    if value is not None and value > 0:
        return value
    validators = worker_contract.get("validators")
    if not isinstance(validators, list):
        return None
    for validator in validators:
        if not isinstance(validator, dict):
            continue
        if str(validator.get("type") or "") != "exact_rows":
            continue
        value = optional_int(validator.get("value"))
        if value is not None and value > 0:
            return value
    return None


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
