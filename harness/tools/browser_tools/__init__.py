"""
harness.tools.browser_tools - BrowserAgent tool schemas and dispatch factory.
"""

import asyncio
import base64
import copy
import hashlib
import re
import sys
import time
import uuid
from functools import lru_cache
from typing import Any, Awaitable, Callable, Dict, List, Optional, Set, Tuple

import json
from pathlib import Path
from urllib.parse import urlparse

from abcp_client import ABCPTransportError
from harness.challenge_detector import (
    HIGH_CONFIDENCE_CHALLENGE_KEYWORDS,
    ChallengeTracker,
    detect_structural_challenge,
    detect_structural_challenge_from_lines,
    extract_page_id,
    is_lingering_loading_title,
)
from harness.content_completeness import (
    ContentCompletenessTracker,
)
from harness.diagnostics.error_classification import attach_error_classification
from harness.extraction_artifacts import (
    field_names_from_specs,
    save_extraction_artifact,
    validate_extraction_rows,
)
from harness.artifact_evidence import detect_blocker_data_rows
from harness.call_outcome import (
    action_runtime_info,
    auto_hitl_is_actionable,
    classify_call_outcome,
    evaluate_grant,
    page_state_evidence_ok,
    replay_forbidden,
)
from harness.fleet_runtime import FleetClickGateTimeout
from harness.hitl import wait_for_hitl_resume
from harness.lifecycle import LifecycleContext, lifecycle_for
from harness.local_fs import local_fs_read, local_fs_search
from harness.observation.overlay_actions import (
    compute_backdrop_point,
    backdrop_point_is_safe,
    find_close_control,
    is_sensitive_method,
    is_sensitive_target,
    normalized_point_to_css,
    visible_layers_occluded,
    vl_dismiss_target_is_safe,
)
from harness.observation.overlay_detector import (
    detect_overlay_from_result,
    title_looks_like_auth_page,
)
from harness.observation.semantic_index import discover_selector_candidates
from harness.observation.page_lifecycle import (
    AUTOMATION_UNAVAILABLE_FAILURE,
    PageLifecycleTracker,
)
from harness.observation.event_observer import unwrap_notification
from harness.observation.verifiers import (
    build_collection_oracle,
    build_read_only_oracle,
    collect_rows,
    probe_occluder,
    probe_viewport_metrics,
    SemanticLocator,
    verify_field_value,
    verify_overlay_gone,
)
from harness.offload import offload_large_tool_result
from harness.progress import NO_ARTIFACT_DIAGNOSTIC_TOOLS, extraction_artifact_count
from harness.pacing import wait_between_rows
from harness.render_recovery import build_render_recovery_runner
from harness.screenshot_policy import normalize_screenshot_output_params
from harness.runtime_evaluation import (
    MAIN_WORLD_REQUIRED_PREFIX,
    RuntimeEvaluationService,
    runtime_last_resort_evidence,
)
from harness.task_control import (
    phase_prior_artifact_paths,
    validate_worker_artifacts,
)
from harness.task_types import resolve_task_type_fail_closed
from harness.tool_policy import (
    disabled_reason_for_method,
    hidden_harness_tools_for_task_type,
    mask_params,
)
from harness.tools.loop_guard import check_tool_call_loop
from harness.tools.parsers import (
    attach_method_schema,
    ensure_required_purpose,
    parse_browser_call_params,
    parse_direct_capability_params,
)
from harness.tools.registry import ToolContext, ToolRegistry
from harness.utils import JsonDict, exception_payload, optional_int, trim_large_strings
from harness.workflow_runtime import (
    workflow_execution_disabled_result,
    workflow_execution_enabled,
)
from .schemas import EVAL_JS_REASON_KINDS, _browser_input_schemas
from .axtree_state import (
    AXTREE_INVALIDATING_METHODS,
    _apply_recovered_target,
    AXTREE_ID_RE,
    _axtree_ids_from_params,
    _axtree_ids_from_value,
    _axtree_lines_from_value,
    _axtree_nodes_from_lines,
    _axtree_seen_ids,
    _axtree_seen_signature,
    _browser_side_rematch_mode,
    _check_stale_axtree_target,
    _invalidate_axtree_snapshot,
    _observe_axtree_state_after,
    _precompute_axtree_snapshot,
    _record_axtree_history,
)
from harness.vl import visual_verify_image
from harness.workflow_policy import validate_workflow_params



SCREENSHOT_MISUSE_RE = re.compile(
    r"\b("
    r"identify|selector|selectors|read|text|understand|layout|structure|"
    r"card|cards|extract|view\s+the\s+current\s+page|figure\s+out"
    r")\b",
    re.I,
)
SCREENSHOT_ALLOWED_PURPOSE_RE = re.compile(
    r"\b("
    r"visual_verify|visual verification|human audit|human review|audit evidence|"
    r"before navigation|after navigation|before/after|before-and-after|"
    r"visual evidence|evidence screenshot"
    r")\b",
    re.I,
)


def _prepare_runtime_evaluation(
    agent: Any,
    params: JsonDict,
    policy: Optional[JsonDict],
    *,
    origin: str,
) -> Tuple[Optional[Any], Optional[JsonDict]]:
    """Single policy boundary shared by model and harness Runtime callers."""
    return RuntimeEvaluationService(
        getattr(agent, "method_schemas", {})
    ).prepare(params, policy, origin=origin)


# Which explicit provenance declarations each method accepts. A landing page
# opened by the SITE has no ABCP opener relation the harness may trust, and
# guessing one from Page.list ordering is the same weak attribution this
# architecture removed from the click gate. The model states the link instead,
# on the first call it makes against the page it claimed.
_NAVIGATION_CONTEXT_KINDS = {
    "Page.create": ("route_recovery_new_page",),
    "Page.getState": ("route_recovery_claimed_page",),
}


def _prepare_navigation_context(
    agent: Any,
    method: str,
    raw: Any,
) -> Tuple[JsonDict, Optional[JsonDict]]:
    """Validate model-supplied Page.create provenance without forwarding it.

    ABCP Page.create exposes no opener/source relation.  This sideband is
    accepted only when the named source page belongs to the worker and the
    completeness tracker has already classified it as an unresolved recovery
    candidate.  That makes the exemption causal and fail-closed rather than a
    "most recently used page" guess.
    """
    if raw is None:
        return {}, None
    if not isinstance(raw, dict):
        return {}, {
            "status": "invalid_navigation_context",
            "error": "navigation_context must be an object",
            "tool_was_executed": False,
        }
    kind = str(raw.get("kind") or "").strip()
    source_page_id = str(raw.get("sourcePageId") or "").strip()
    allowed_kinds = _NAVIGATION_CONTEXT_KINDS.get(method, ())
    if not allowed_kinds:
        return {}, {
            "status": "invalid_navigation_context",
            "error": (
                "navigation_context is supported only for Page.create and"
                " Page.getState"
            ),
            "tool_was_executed": False,
        }
    if kind not in allowed_kinds or not source_page_id:
        return {}, {
            "status": "invalid_navigation_context",
            "error": (
                f"navigation_context on {method} requires kind in"
                f" {sorted(allowed_kinds)} and a non-empty sourcePageId"
            ),
            "tool_was_executed": False,
        }
    allowed = getattr(agent, "allowed_page_ids", set())
    if source_page_id not in allowed:
        return {}, {
            "status": "invalid_navigation_context",
            "error": "navigation_context.sourcePageId is not owned by this worker",
            "sourcePageId": source_page_id,
            "tool_was_executed": False,
        }
    tracker = getattr(agent, "content_completeness_tracker", None)
    if (
        tracker is None
        or not getattr(tracker, "enabled", False)
        or not hasattr(tracker, "can_designate_recovery_source")
        or not tracker.can_designate_recovery_source(source_page_id)
    ):
        return {}, {
            "status": "invalid_navigation_context",
            "error": (
                "sourcePageId is not a tracker-confirmed unresolved"
                " route-recovery source"
            ),
            "sourcePageId": source_page_id,
            "tool_was_executed": False,
            "next_instruction": (
                "Use Page.getState plus DOM evidence on the source page first."
                " Supply navigation_context only after contentCompleteness"
                " reports materialization_required or route_recovery_required."
            ),
        }
    return {
        "kind": kind,
        "sourcePageId": source_page_id,
    }, None


def _lifecycle_page_id(agent: Any, params: Any) -> str:
    if isinstance(params, dict) and params.get("pageId"):
        return str(params.get("pageId") or "").strip()
    return str(getattr(agent, "axtree_page_id", "") or "").strip()


async def _page_lifecycle_guard_before(
    agent: Any,
    method: str,
    params: JsonDict,
) -> Optional[JsonDict]:
    """Event-driven pre-call gate.

    DOM probes wait for settlement.  A missed event triggers exactly one
    Page.getState call.  Re-perception obligations are then exposed as explicit
    guards so the model cannot continue with stale DOM handles.
    """
    tracker = getattr(agent, "page_lifecycle", None)
    if not isinstance(tracker, PageLifecycleTracker):
        return None
    page_id = _lifecycle_page_id(agent, params)
    state = tracker.state(page_id)
    if state is None:
        return None

    is_dom_probe = method.startswith("DOM.")
    if is_dom_probe and state.status == "loading":
        raw_timeout = getattr(
            agent.runtime.harness, "page_settlement_timeout_seconds", 15.0
        )
        try:
            timeout = max(0.0, float(raw_timeout))
        except (TypeError, ValueError):
            timeout = 15.0
        settled = await tracker.wait_for_settlement(page_id, timeout)
        agent.logger.write("page.lifecycle.settlement_wait", {
            "pageId": page_id,
            "timeoutSeconds": timeout,
            "outcome": settled,
        })
        if settled == "timeout":
            runner = getattr(agent, "render_recovery_runner", None)
            if runner is None:
                runner = build_render_recovery_runner(
                    browser=agent.browser,
                    logger=agent.logger,
                    capability_methods=agent.capability_methods,
                    recent_recoveries=agent._render_recovery_recent,
                )
                agent.render_recovery_runner = runner
            try:
                response, _recovery = await runner.call("Page.getState", {
                    "pageId": page_id,
                    "purpose": "One-shot resynchronization after settlement event timeout",
                })
                tracker.observe_state_response(page_id, response)
                agent.logger.write("page.lifecycle.timeout_resync", {
                    **tracker.receipt(page_id),
                    "performed": True,
                })
            except Exception as exc:  # the original DOM call remains blocked
                return {
                    "status": "page_settlement_unknown",
                    "tool_was_executed": False,
                    "pageLifecycle": tracker.receipt(page_id),
                    "error": str(exc),
                    "next_instruction": (
                        "The Page.loaded settlement event timed out and the one-shot"
                        " Page.getState resynchronization failed. Do not poll; inspect"
                        " the failure or recover the page."
                    ),
                }
            state = tracker.state(page_id)

    state = tracker.state(page_id)
    if state is None:
        return None
    if state.status == "loading" and is_dom_probe:
        return {
            "status": "page_still_loading",
            "tool_was_executed": False,
            "pageLifecycle": tracker.receipt(page_id),
            "next_instruction": (
                "DOM probes remain paused. Wait for a lifecycle event; do not poll"
                " Page.getState."
            ),
        }
    lifecycle_recovery_methods = {
        "Page.getState",
        "Page.navigate",
        "Page.reload",
        "Page.go",
        "Page.close",
    }
    # Download controls are mutually composable (pause -> resume/cancel). They
    # may dirty page state for later DOM work, but must not deadlock each other
    # behind that deferred resynchronization obligation.
    is_file_control = method == "File.download" or method.startswith("Download.")
    if (
        state.requires_state_resync
        and method not in lifecycle_recovery_methods
        and not is_file_control
    ):
        return {
            "status": "page_state_resync_required",
            "tool_was_executed": False,
            "pageLifecycle": tracker.receipt(page_id),
            "next_instruction": (
                "Call Page.getState once before continuing after navigation,"
                " recovery, dialog/chooser close, or a download state change."
            ),
        }
    if (
        state.requires_ax_refresh
        and method not in {*lifecycle_recovery_methods, "DOM.getAXTree"}
        and not is_file_control
    ):
        return {
            "status": "page_axtree_refresh_required",
            "tool_was_executed": False,
            "pageLifecycle": tracker.receipt(page_id),
            "next_instruction": (
                "Call DOM.getAXTree before continuing; navigation/recovery"
                " invalidated all prior DOM targets."
            ),
        }
    return None


def _page_lifecycle_before_action(agent: Any, method: str, params: JsonDict) -> None:
    tracker = getattr(agent, "page_lifecycle", None)
    if isinstance(tracker, PageLifecycleTracker):
        tracker.before_action(method, _lifecycle_page_id(agent, params))


def _page_lifecycle_after_action(
    agent: Any,
    method: str,
    params: JsonDict,
    response: Any,
) -> None:
    tracker = getattr(agent, "page_lifecycle", None)
    if not isinstance(tracker, PageLifecycleTracker):
        return
    page_id = _lifecycle_page_id(agent, params)
    if method == "Page.getState":
        tracker.observe_state_response(page_id, response)
    elif method == "DOM.getAXTree" and not _invoke_result_failed({"response": response}):
        tracker.observe_ax_refresh(page_id)
    if (
        method in {
            "Page.navigate", "Page.getState", "DOM.getAXTree",
            "File.download", "File.handleChooser", "Workflow.execute",
        }
        or method.startswith("Download.")
    ):
        agent.logger.write("page.lifecycle.after_action", {
            "method": method,
            **tracker.receipt(page_id),
        })

BrowserToolDispatcher = Callable[
    [JsonDict, int],
    Awaitable[Tuple[JsonDict, bool]],
]

BROWSER_TOOLS = ToolRegistry("browser_agent")


def _browser_schema_for(tool_name: str) -> Callable[[Optional[Any]], JsonDict]:
    def factory(capability_methods: Optional[Any] = None) -> JsonDict:
        schema = _browser_input_schemas_cached(
            _capability_methods_key(capability_methods)
        ).get(tool_name)
        if schema is not None:
            return copy.deepcopy(schema)
        raise KeyError(f"BrowserAgent tool schema not found: {tool_name}")

    return factory


def _capability_methods_key(capability_methods: Optional[Any]) -> Tuple[str, ...]:
    if isinstance(capability_methods, set):
        values = capability_methods
    else:
        values = set(capability_methods or [])
    return tuple(sorted(str(item) for item in values if str(item).strip()))


def _allowed_tool_hint(agent: Any) -> JsonDict:
    capability_methods = sorted(
        str(item)
        for item in getattr(agent, "capability_methods", set())
        if str(item).strip()
    )
    return {
        "allowed_tools": BROWSER_TOOLS.names(),
        "allowed_capability_methods": capability_methods[:50],
        "capability_method_count": len(capability_methods),
    }


@lru_cache(maxsize=32)
def _browser_input_schemas_cached(capability_methods: Tuple[str, ...]) -> Dict[str, JsonDict]:
    return _browser_input_schemas(capability_methods)


def build_browser_tool_dispatcher(agent: Any) -> BrowserToolDispatcher:
    async def dispatch(tool_call: JsonDict, step: int) -> Tuple[JsonDict, bool]:
        lifecycle = lifecycle_for(agent)
        effective_call = lifecycle.tool_pre_call(
            LifecycleContext(
                actor="browser_agent",
                step=step,
                metadata={"agent_id": getattr(getattr(agent, "runtime", None), "agent_id", "")},
            ),
            tool_call,
        )
        result, should_stop = await execute_browser_tool(agent, effective_call, step)
        result = lifecycle.tool_post_call(
            LifecycleContext(
                actor="browser_agent",
                step=step,
                metadata={"agent_id": getattr(getattr(agent, "runtime", None), "agent_id", "")},
            ),
            effective_call,
            result,
        )
        result = await _maybe_reality_check(agent, effective_call, result, step)
        return result, should_stop

    return dispatch


async def execute_browser_tool(agent: Any, tool_call: JsonDict, step: int) -> Tuple[JsonDict, bool]:
    name = str(tool_call.get("name") or "")
    raw_tool_input = tool_call.get("input") or {}
    tool_input = (
        raw_tool_input
        if isinstance(raw_tool_input, dict)
        else {"value": raw_tool_input}
    )
    action = BROWSER_TOOLS.get(name)
    ctx = ToolContext(agent=agent, tool_call=tool_call, tool_input=tool_input, step=step)

    if action is not None and action.terminal:
        result = await action.handler(ctx)
        # A terminal handler may soft-reject its call (tool_was_executed False)
        # to bounce it back to the model with guidance instead of terminating —
        # e.g. final_answer declaring target_absent without any visual reality
        # check on record. The rejection carries next_instruction; the loop
        # continues so the model can comply and re-finalize.
        should_stop = not (
            isinstance(result, dict)
            and result.get("tool_was_executed") is False
        )
        return result, should_stop

    progress_gate = _call_extraction_progress_gate(agent, name, tool_input)
    if progress_gate is not None:
        agent.trace.append({"type": "progress_gate", "result": progress_gate})
        return progress_gate, False

    # Loop guard: short-circuit if the model is hammering the same tool with
    # the same args. final_answer is exempted above so a deliberate retry of
    # the terminal call doesn't trip the guard.
    if action is None or action.loop_guard:
        short_circuit = check_tool_call_loop(
            agent,
            name=name,
            tool_input=tool_input,
            step=step,
        )
        if short_circuit is not None:
            guard_result, should_stop = short_circuit
            agent.trace.append({"type": "loop_guard", "result": guard_result})
            return guard_result, should_stop

    # browser_call carries the page_create terminal hard-stop in its second
    # return value (page_create_should_stop). Its registered handler can only
    # return a JsonDict, so dispatching through it would drop should_stop to a
    # hard-coded False and let the worker keep hammering a dead browser. Route
    # it straight to the capability executor here (after the loop guard) so the
    # hard-stop propagates, mirroring the direct-capability-name path below.
    if name == "browser_call":
        return await _execute_browser_capability_tool(agent, name, tool_input, step)

    if action is None:
        if name in getattr(agent, "capability_methods", set()):
            return await _execute_browser_capability_tool(agent, name, tool_input, step)
        result = {
            "error": f"Unknown harness tool: {name}",
            **_allowed_tool_hint(agent),
        }
        agent.logger.write("tool.error", result)
        agent.trace.append({"type": "tool_error", "result": result})
        return result, False

    fleet_guard, _fleet_receipt = _apply_fleet_binding(
        agent, name, tool_input
    )
    routing_guard = fleet_guard or _check_page_binding(
        agent, name, tool_input
    )
    if routing_guard is not None:
        agent.logger.write("browser.tool.routing_rejected", routing_guard)
        agent.trace.append({
            "type": "page_binding_guard",
            "method": name,
            "params": tool_input,
            "result": routing_guard,
        })
        return routing_guard, False

    if action.contract_check:
        contract_result = _check_worker_contract(agent, name)
        if contract_result is not None:
            agent.trace.append({"type": "contract_violation", "result": contract_result})
            return contract_result, False

    if action.progress_check:
        progress_result = _check_progress_before(agent, name, tool_input, step)
        if progress_result is not None:
            agent.trace.append({"type": "progress_intervention", "result": progress_result})
            return progress_result, False

    result = await action.handler(ctx)
    if action.trace_type:
        _observe_progress_after(agent, name, result)
        trace_entry: JsonDict = {"type": action.trace_type, "result": result}
        if name == "collect_items":
            from harness.fast_path import trace_params_for_fast_path

            stable_params = trace_params_for_fast_path(name, tool_input)
            if stable_params:
                trace_entry["params"] = stable_params
        agent.trace.append(trace_entry)
    return result, False


@BROWSER_TOOLS.register(
    name="browser_call",
    description=(
        "Invoke a single ABCP Browser atomic capability and return the browser observation/data."
        " Derive params from live feedback: previous response.data handles, current"
        " DOM.getAXTree ids, DOM.getText/DOM.getAttribute evidence, worker_contract,"
        " or cited record_extraction artifacts."
    ),
    input_schema=_browser_schema_for("browser_call"),
    strict=False,
    trace_type="",
)
async def _browser_call(ctx: ToolContext) -> JsonDict:
    result, _should_stop = await _execute_browser_capability_tool(
        ctx.agent,
        "browser_call",
        ctx.tool_input,
        ctx.step,
    )
    return result


@BROWSER_TOOLS.register(
    name="execute_selected_skill",
    description=(
        "Execute the currently selected workflow skill's frozen recipe without"
        " copying, reading, or reconstructing workflow.json steps. Accepts one"
        " variables object or multiple rows; batch rows run strictly serially"
        " on the supplied warm page. This tool only runs the selected recipe and"
        " returns structured rows; persist accepted data with record_extraction."
    ),
    input_schema=_browser_schema_for("execute_selected_skill"),
    contract_check=True,
)
async def _browser_execute_selected_skill(ctx: ToolContext) -> JsonDict:
    if not workflow_execution_enabled(ctx.agent):
        return workflow_execution_disabled_result(source="execute_selected_skill")
    from harness.skill.contract import skill_selection_declined
    from harness.skill.dispatch import (
        _align_row_fields_to_expected,
        auth_fence_outcome,
        _expected_fields_of,
        _provenance_evidence_requirements,
        _run_with_transient_retry,
        build_extraction_row,
        page_binding_mismatch,
        required_filled,
    )
    from harness.skill.pause import classify_run_for_hitl
    from harness.skill.registry import SkillRegistry
    from harness.skill.workflow import check_success_contract

    agent = ctx.agent
    contract = getattr(agent, "worker_contract", None)
    contract = contract if isinstance(contract, dict) else {}
    repair_manifest = contract.get("_repair_manifest")
    if (
        isinstance(repair_manifest, dict)
        and not str(repair_manifest.get("disabledReason") or "").strip()
    ):
        return {
            "status": "rejected",
            "error": (
                "A field-level repair manifest is active. Preserve its trusted"
                " baseline and patch only the listed fields; do not re-run the"
                " full selected workflow."
            ),
            "tool_was_executed": False,
        }
    if bool(getattr(agent, "_selected_skill_workflow_attempted", False)):
        return {
            "status": "rejected",
            "error": (
                "The selected frozen workflow already ran in this worker."
                " Follow the existing failure/partial handoff and re-observe"
                " only the unresolved target instead of replaying the recipe."
            ),
            "tool_was_executed": False,
        }
    if skill_selection_declined(contract):
        return {
            "status": "rejected",
            "error": "The worker contract explicitly declined skill execution.",
            "tool_was_executed": False,
        }
    skill_id = str(contract.get("skill_id") or "").strip()
    if not skill_id:
        selection = contract.get("skill_selection")
        if isinstance(selection, dict) and selection.get("use_skill") is not False:
            skill_id = str(selection.get("skill_id") or "").strip()
    registry = SkillRegistry.load()
    skill = registry.get(skill_id) if skill_id else None
    if skill is None or not skill.has_workflow:
        return {
            "status": "rejected",
            "error": "No selected workflow skill is available for this worker.",
            "tool_was_executed": False,
        }

    single = ctx.tool_input.get("variables")
    single = single if isinstance(single, dict) else {}
    batch = ctx.tool_input.get("rows")
    batch = [row for row in batch if isinstance(row, dict)] if isinstance(batch, list) else []
    if single and batch:
        return {
            "status": "invalid_input",
            "error": "Provide variables or rows, not both.",
            "tool_was_executed": False,
        }
    page_id = str(ctx.tool_input.get("pageId") or "").strip()
    fleet_id = str(ctx.tool_input.get("fleetId") or "").strip()
    if not page_id:
        return {
            "status": "invalid_input",
            "error": "pageId must be a live page handle from Page.getState/Page.list.",
            "tool_was_executed": False,
        }

    evidence_fields = set(
        _provenance_evidence_requirements(contract.get("validators")).values()
    )
    passthrough = {
        str(value)
        for value in (skill.row_contract.get("passthrough_variables") or [])
        if str(value).strip()
    }
    allowed = set(skill.variable_template) | passthrough | evidence_fields
    base_input = contract.get("skill_variables")
    base_input = base_input if isinstance(base_input, dict) else {}
    inputs = batch if batch else [single]
    effective_inputs: List[JsonDict] = []
    for row in inputs:
        effective = dict(base_input)
        effective.update(row)
        unknown = sorted(str(key) for key in effective if str(key) not in allowed)
        if unknown:
            return {
                "status": "invalid_input",
                "error": "Input contains fields outside the selected skill row contract.",
                "unknownFields": unknown,
                "allowedFields": sorted(allowed),
                "tool_was_executed": False,
            }
        effective_inputs.append(effective)

    output_rows: List[JsonDict] = []
    runs: List[JsonDict] = []
    for index, row_input in enumerate(effective_inputs):
        variables = {
            key: row_input.get(key, default)
            for key, default in skill.variable_template.items()
        }
        if not required_filled(skill, variables):
            result = {
                "status": "partial" if output_rows else "invalid_input",
                "skill": skill.skill_id,
                "completedRows": len(output_rows),
                "failedRow": index,
                "error": "A workflow-referenced variable is empty.",
                "rows": output_rows,
                "tool_was_executed": bool(output_rows),
            }
            return result
        run_id = f"skill-tool-{skill.skill_id}-{uuid.uuid4().hex[:8]}"
        run_result, observed_signal = await _run_with_transient_retry(
            agent,
            skill,
            run_id=run_id,
            page_id=page_id,
            fleet_id=fleet_id,
            variables=variables,
            event_prefix="skill.selected_workflow",
        )
        auth_fence = auth_fence_outcome(run_result)
        if auth_fence is not None:
            result = {
                "status": "partial" if output_rows else "workflow_auth_fenced",
                "skill": skill.skill_id,
                "completedRows": len(output_rows),
                "failedRow": index,
                "rows": output_rows,
                "runs": runs,
                "authFence": auth_fence,
                "next_instruction": (
                    "The shared authentication generation changed or its"
                    " barrier closed. Preserve completed rows, re-perceive the"
                    " current page, and retry only the failed row."
                ),
            }
            _record_selected_skill_tool_trace(agent, result)
            return result
        hitl = classify_run_for_hitl(run_result, observed_signal)
        verdict = check_success_contract(skill, run_result)
        mismatch = page_binding_mismatch(skill, run_result, variables)
        run_summary: JsonDict = {
            "index": index,
            "runId": run_id,
            "succeeded": bool(run_result.get("succeeded")),
            "failedChecks": verdict.get("failed_checks") or [],
        }
        if hitl is not None:
            run_summary["hitl"] = hitl
        if mismatch is not None:
            run_summary["pageBinding"] = mismatch
        runs.append(run_summary)
        if (
            not run_result.get("succeeded")
            or not verdict.get("ok")
            or hitl is not None
            or mismatch is not None
        ):
            result = {
                "status": "partial" if output_rows else "workflow_failed",
                "skill": skill.skill_id,
                "completedRows": len(output_rows),
                "failedRow": index,
                "rows": output_rows,
                "runs": runs,
                "next_instruction": (
                    "Use the returned completed rows as observed data. Re-observe"
                    " only the failed row/fields, then persist one final artifact."
                ),
            }
            _record_selected_skill_tool_trace(agent, result)
            return result
        built = build_extraction_row(
            skill,
            run_result,
            input_variables=dict(row_input),
        )
        output_rows.append(_align_row_fields_to_expected(
            built, _expected_fields_of(contract),
        ))
        await wait_between_rows(
            agent,
            contract,
            completed_index=index,
            total_rows=len(effective_inputs),
            source="execute_selected_skill",
        )

    result = {
        "status": "done",
        "skill": skill.skill_id,
        "completedRows": len(output_rows),
        "rows": output_rows,
        "runs": runs,
        "next_instruction": (
            "Review the structured rows against the worker contract, add any"
            " evidence the frozen workflow cannot produce, then call"
            " record_extraction. Do not re-run the same workflow manually."
        ),
    }
    _record_selected_skill_tool_trace(agent, result)
    return result


@BROWSER_TOOLS.register(
    name="execute_browser_workflow",
    description=(
        "Execute a temporary browser-only ABCP workflow after recursive harness"
        " validation. Use it only when the complete action sequence and simple"
        " data dependencies are known in advance. It cannot call harness-local"
        " tools or Runtime.evaluate, and navigation must be followed by"
        " Page.loaded, Page.getState, and DOM.getAXTree."
    ),
    input_schema=_browser_schema_for("execute_browser_workflow"),
    contract_check=True,
)
async def _browser_execute_browser_workflow(ctx: ToolContext) -> JsonDict:
    if not workflow_execution_enabled(ctx.agent):
        return workflow_execution_disabled_result(source="execute_browser_workflow")
    params = {
        "description": str(ctx.tool_input.get("description") or "Temporary browser workflow"),
        "variables": dict(ctx.tool_input.get("variables") or {}),
        "steps": list(ctx.tool_input.get("steps") or []),
        "timeout": int(ctx.tool_input.get("timeout") or 600000),
        "stepTimeout": int(ctx.tool_input.get("stepTimeout") or 30000),
    }
    page_id = str(ctx.tool_input.get("pageId") or "").strip()
    fleet_id = str(ctx.tool_input.get("fleetId") or "").strip()
    if page_id:
        params["pageId"] = page_id
    if fleet_id:
        params["fleetId"] = fleet_id
    if isinstance(ctx.tool_input.get("errorConfig"), dict):
        params["errorConfig"] = dict(ctx.tool_input.get("errorConfig") or {})
    result, _should_stop = await _execute_browser_capability_tool(
        ctx.agent,
        "browser_call",
        {
            "method": "Workflow.execute",
            "params": params,
            "reason": params["description"],
        },
        ctx.step,
    )
    return result


def _record_selected_skill_tool_trace(agent: Any, result: JsonDict) -> None:
    summary = {
        "skill": result.get("skill"),
        "status": result.get("status"),
        "completedRows": result.get("completedRows"),
        "failedRow": result.get("failedRow"),
        "runIds": [
            run.get("runId")
            for run in (result.get("runs") or [])
            if isinstance(run, dict)
        ],
    }
    logger = getattr(agent, "logger", None)
    if logger is not None and hasattr(logger, "write"):
        logger.write("skill.selected_workflow.executed", summary)
    trace = getattr(agent, "trace", None)
    if isinstance(trace, list):
        trace.append({"type": "execute_selected_skill", "result": summary})


@BROWSER_TOOLS.register(
    name="navigate_verified",
    description=(
        "Navigate to a URL once, follow redirects, and report the actual"
        " URL/title. Exactly one Page.navigate is dispatched per call: an unmet"
        " expectation returns navigation_arrived_expectation_mismatch with the"
        " page that did arrive, never a second request."
    ),
    input_schema=_browser_schema_for("navigate_verified"),
    contract_check=True,
    progress_check=True,
    trace_type="navigate_verified",
)
async def _browser_navigate_verified(ctx: ToolContext) -> JsonDict:
    result = await _navigate_verified(ctx.agent, ctx.tool_input, ctx.step)
    if result.get("status") not in {
        "done",
        # Nothing was dispatched and no page was touched, so there is no new
        # page state for challenge adjudication to read.
        "expectation_pattern_invalid",
        "blocked_by_challenge",
        "hitl_required",
        "hitl_timeout",
        "page_settled_after_hitl",
        "stale_pause_deadlock",
    }:
        result = await _maybe_auto_hitl_for_challenge(
            ctx.agent,
            "navigate_verified",
            {"pageId": ctx.tool_input.get("pageId")},
            result,
            ctx.step,
        )
    return result


@BROWSER_TOOLS.register(
    name="dismiss_overlay",
    description=(
        "Dismiss an overlay/modal/cookie-banner blocking a target action. Runs"
        " the dismiss ladder internally (find close control -> click -> verify"
        " -> Escape -> verify) and reports"
        " a structured result. Auth/login and paywall overlays are never"
        " auto-dismissed (returns status=blocked). Optionally retries the"
        " original action after the overlay is gone, but never a consequential"
        " one (submit/pay/login -> status=dismissed_pending_action). Coordinate"
        " backdrop/VL clicks are unavailable until ABCP exposes an independent"
        " native point hit-test."
    ),
    input_schema=_browser_schema_for("dismiss_overlay"),
    contract_check=True,
    trace_type="dismiss_overlay",
)
async def _browser_dismiss_overlay(ctx: ToolContext) -> JsonDict:
    return await _dismiss_overlay(ctx.agent, ctx.tool_input, ctx.step)


@BROWSER_TOOLS.register(
    name="collect_items",
    description=(
        "Collect one single-level homogeneous list/card/row collection that"
        " grows through ONE scroll container or ONE load-more control, without"
        " burning a model step per round. Harvests rows every round and dedups"
        " by a stable key, so lazy-loaded and virtualized rows can be retained."
        " On an unknown site, first probe DOM/SemanticTree to identify the"
        " repeated-item selector and the actual scroll container/load-more"
        " control; do not guess them."
        " Use this only when the collection cannot be read from one DOM snapshot;"
        " otherwise enumerate canonical ids and batch DOM.getText/DOM.getAttribute."
        " Persists through record_extraction"
        " only after target_reached or mechanically evidenced exhaustion; stalled"
        " or blocked partial rows are not persisted. When content completeness is"
        " declared, pass an explicit regionId or a matching collectionField."
        " Use a freshly created tab (a reused tab can cap some sites'"
        " lazy-loader). Nested lists, multiple scroll layers, next-page"
        " pagination, filter/search/sort, and dependent per-row expansion are"
        " outside this preset's complete coverage; decompose/probe them in the"
        " BrowserAgent slow path."
    ),
    input_schema=_browser_schema_for("collect_items"),
    contract_check=True,
    trace_type="collect_items",
)
async def _browser_collect_items(ctx: ToolContext) -> JsonDict:
    # collect_items needs the declared min_records before it starts its bounded
    # loop.  Do not rely on an earlier model-facing DOM call to have initialized
    # the tracker incidentally.
    _ensure_content_completeness_tracker(ctx.agent)
    result = await _collect_items(ctx.agent, ctx.tool_input, ctx.step)
    return _observe_content_completeness_after(
        ctx.agent,
        "collect_items",
        ctx.tool_input,
        result,
        ctx.step,
    )


@BROWSER_TOOLS.register(
    name="fill_field_verified",
    description=(
        "Type a value into a form field and verify it was actually accepted by"
        " reading the field's live value back (handles React/controlled inputs"
        " where the DOM attribute lags). On mismatch it clears harder and"
        " retries once; if the field can't be uniquely located it yields"
        " (ambiguous/field_not_found) instead of claiming success. Recovers from"
        " an occluding overlay. Never submits the form — do that as a separate"
        " verified action."
    ),
    input_schema=_browser_schema_for("fill_field_verified"),
    contract_check=True,
    trace_type="fill_field_verified",
)
async def _browser_fill_field_verified(ctx: ToolContext) -> JsonDict:
    return await _fill_field_verified(ctx.agent, ctx.tool_input, ctx.step)


@BROWSER_TOOLS.register(
    name="visual_verify",
    description=(
        "Take a screenshot and ask the configured VL model to verify an"
        " action/page-state outcome. Use only for visual arbitration after"
        " click/navigation uncertainty, validator failure, overlays, CAPTCHA,"
        " or layout mismatch. Do not use for bulk data extraction."
    ),
    input_schema=_browser_schema_for("visual_verify"),
    contract_check=True,
    progress_check=True,
    trace_type="visual_verify",
)
async def _browser_visual_verify(ctx: ToolContext) -> JsonDict:
    return await _visual_verify(ctx.agent, ctx.tool_input, ctx.step)


@BROWSER_TOOLS.register(
    name="final_answer",
    description=(
        "Terminate orchestration and return the structured result to LeadAgent."
        " The `status` field is restricted to the whitelist below; other terminal states"
        " (hitl_*, page_crashed, browser_api_contract_error, context_limit_exceeded,"
        " step_budget_exhausted) are detected and set by the harness — do not self-report them."
    ),
    input_schema=_browser_schema_for("final_answer"),
    terminal=True,
    loop_guard=False,
    trace_type="",
)
async def _browser_final_answer(ctx: ToolContext) -> JsonDict:
    answer = str(ctx.tool_input.get("answer", "")).strip()
    rejection = _final_answer_content_completeness_rejection(
        ctx.agent,
        answer,
        status=str(ctx.tool_input.get("status") or "done"),
    )
    if rejection is not None:
        ctx.agent.logger.write(
            "final_answer.content_completeness_rejected", rejection
        )
        ctx.agent.trace.append({
            "type": "final_answer_rejected",
            "result": rejection,
        })
        return rejection
    rejection = _final_answer_reality_check_rejection(ctx.agent, answer)
    if rejection is not None:
        ctx.agent.logger.write("final_answer.reality_check_rejected", {
            "status": ctx.tool_input.get("status"),
        })
        ctx.agent.trace.append({
            "type": "final_answer_rejected",
            "result": rejection,
        })
        return rejection
    result = {
        "status": ctx.tool_input.get("status", "done"),
        "answer": answer,
        "artifacts": ctx.agent.artifacts,
    }
    reason = ctx.tool_input.get("reason")
    if isinstance(reason, str) and reason.strip():
        result["reason"] = reason.strip()[:200]
    ctx.agent.logger.write("tool.final_answer", result)
    ctx.agent.trace.append({"type": "final_answer", "result": result})
    return result


@BROWSER_TOOLS.register(
    name="record_extraction",
    description=(
        "Persist structured data already observed in the browser (product URLs/titles, form fields,"
        " list rows, etc.) to an artifact that LeadAgent can reuse."
        " Fields that never went through record_extraction must not appear"
        " in the final_answer's `data`."
        " `name` identifies the dataset; `rows` must be a list[dict] backed by actual observations"
        " and should preserve provenance for critical fields."
    ),
    input_schema=_browser_schema_for("record_extraction"),
    strict=False,
    trace_type="record_extraction",
)
async def _browser_record_extraction(ctx: ToolContext) -> JsonDict:
    result = _record_extraction(ctx.agent, ctx.tool_input)
    if _record_extraction_persisted(result):
        ctx.agent.pending_unrecorded_extraction = None
    return result


@BROWSER_TOOLS.register(
    name="find_in_axtree",
    description=(
        "Search the current DOM.getAXTree snapshot by role/name/text and return"
        " complete canonical AXTree ids with line context. Use this instead of"
        " grepping offloaded AXTree text when locating an element in a large"
        " accessibility tree. Matches include layout `flags`"
        " (hidden/off/blocked/scroll/sticky/clip/zN) and the `rect` viewport"
        " box when the line carries them — avoid hidden/blocked targets; use"
        " `rect` for spatial reasoning only, not for deriving click coordinates"
        " (act on the id). It is read-only and requires a fresh current"
        " DOM.getAXTree snapshot."
    ),
    input_schema=_browser_schema_for("find_in_axtree"),
    contract_check=True,
    trace_type="find_in_axtree",
)
async def _browser_find_in_axtree(ctx: ToolContext) -> JsonDict:
    return _find_in_axtree(ctx.agent, ctx.tool_input)


@BROWSER_TOOLS.register(
    name="local_fs_search",
    description="Read-only search across files inside the current task worktree; supports glob, JSONL event-type filtering, and per-hit / total output caps.",
    input_schema=_browser_schema_for("local_fs_search"),
    contract_check=True,
    progress_check=True,
    trace_type="local_fs_search",
)
async def _browser_local_fs_search(ctx: ToolContext) -> JsonDict:
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


@BROWSER_TOOLS.register(
    name="local_fs_read",
    description="Read-only line-range read of a file inside the current task worktree; well suited to JSONL traces and AXTree lines.txt offload files.",
    input_schema=_browser_schema_for("local_fs_read"),
    contract_check=True,
    progress_check=True,
    trace_type="local_fs_read",
)
async def _browser_local_fs_read(ctx: ToolContext) -> JsonDict:
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


def _apply_fleet_binding(
    agent: Any,
    method: str,
    params: JsonDict,
) -> Tuple[Optional[JsonDict], JsonDict]:
    """Enforce the coordinator-issued fleet binding on model-initiated calls.

    Internal harness plumbing does not pass through this function.  This makes
    Fleet.create coordinator-owned while still allowing the fast path and other
    deterministic harness code to use its explicit assignment.
    """

    if not _fleet_reuse_enabled(agent):
        return None, {}

    assigned_fleet_id = str(
        getattr(agent, "assigned_fleet_id", "") or ""
    ).strip()
    allowed = {
        str(item).strip()
        for item in (getattr(agent, "allowed_fleet_ids", set()) or set())
        if str(item).strip()
    }
    if assigned_fleet_id:
        allowed.add(assigned_fleet_id)
    assignment_reason = str(
        getattr(agent, "fleet_assignment_reason", "") or ""
    ).strip()

    receipt = {
        "assignedFleetId": assigned_fleet_id,
        "assignmentReason": assignment_reason,
        "fleetInjected": False,
    }
    pinned_page_id = str(
        getattr(agent, "pinned_page_id", "") or ""
    ).strip()
    if pinned_page_id and method == "Page.create":
        return {
            "status": "pinned_browser_context_violation",
            "error": (
                "Page.create cannot replace the user-pinned existing page"
                f" {pinned_page_id!r}."
            ),
            "assignedFleetId": assigned_fleet_id,
            "pinnedPageId": pinned_page_id,
            "tool_was_executed": False,
            "next_instruction": (
                "Use the pinned pageId from slot_context. If that page is no"
                " longer usable, report pinned_page_unavailable to LeadAgent;"
                " do not create a substitute page."
            ),
        }, receipt
    if (
        pinned_page_id
        and method == "Page.close"
        and str(params.get("pageId") or "").strip() == pinned_page_id
    ):
        return {
            "status": "pinned_browser_context_violation",
            "error": (
                "Page.close cannot close the user-pinned existing page"
                f" {pinned_page_id!r}."
            ),
            "assignedFleetId": assigned_fleet_id,
            "pinnedPageId": pinned_page_id,
            "tool_was_executed": False,
            "next_instruction": (
                "Leave the pinned page open and continue on that page, or"
                " report pinned_page_unavailable if it cannot be used."
            ),
        }, receipt
    if method == "Fleet.create":
        return {
            "status": "fleet_create_coordinator_owned",
            "error": (
                "Fleet.create is coordinator-owned while fleet reuse is enabled;"
                " the worker must create pages inside its assigned fleet."
            ),
            "assignedFleetId": assigned_fleet_id,
            "assignmentReason": assignment_reason,
            "tool_was_executed": False,
            "next_instruction": (
                "Call Page.create with the assignedFleetId. If true session"
                " isolation is required, declare needs_isolated_session before"
                " spawning the worker."
            ),
        }, receipt
    if method == "Fleet.close":
        return {
            "status": "fleet_close_coordinator_owned",
            "error": (
                "Fleet.close is disabled for workers while fleet reuse is"
                " enabled because close clears ownership and makes the fleet"
                " claimable by another agent that knows its fleetId."
            ),
            "assignedFleetId": assigned_fleet_id,
            "assignmentReason": assignment_reason,
            "tool_was_executed": False,
            "next_instruction": (
                "Close task pages with Page.close when appropriate. Fleet"
                " ownership transfer and retention are Dispatcher lifecycle"
                " responsibilities."
            ),
        }, receipt

    requested_fleet_id = str(params.get("fleetId") or "").strip()
    if requested_fleet_id:
        if not assigned_fleet_id:
            return {
                "status": "fleet_assignment_required",
                "error": "No coordinator fleet assignment is attached to this worker.",
                "requestedFleetId": requested_fleet_id,
                "tool_was_executed": False,
            }, receipt
        if requested_fleet_id not in allowed:
            return {
                "status": "fleet_binding_violation",
                "error": (
                    f"fleetId {requested_fleet_id!r} is outside this worker's"
                    " coordinator-issued binding."
                ),
                "assignedFleetId": assigned_fleet_id,
                "allowedFleetIds": sorted(allowed),
                "tool_was_executed": False,
                "next_instruction": (
                    "Use assignedFleetId from slot_context; never fabricate or"
                    " substitute fleet identifiers."
                ),
            }, receipt
    elif method in {"Page.create", "Page.list"}:
        if not assigned_fleet_id:
            return {
                "status": "fleet_assignment_required",
                "error": (
                    f"{method} requires a coordinator-issued fleetId;"
                    " fleetless Dispatcher selection is intentionally disabled."
                ),
                "tool_was_executed": False,
            }, receipt
        params["fleetId"] = assigned_fleet_id
        receipt["fleetInjected"] = True

    if method not in {"Page.create", "Page.list"} and not method.startswith("Fleet."):
        return None, {}
    return None, receipt


def _fleet_reuse_enabled(agent: Any) -> bool:
    runtime = getattr(agent, "runtime", None)
    harness_config = getattr(runtime, "harness", None)
    if harness_config is None or not hasattr(harness_config, "fleet_reuse_enabled"):
        # Compatibility for direct helper users and lightweight test doubles.
        # Real RuntimeConfig always carries the explicit flag (default: true).
        return False
    return bool(getattr(harness_config, "fleet_reuse_enabled", True))


def _check_page_binding(
    agent: Any,
    method: str,
    params: JsonDict,
) -> Optional[JsonDict]:
    """Reject model-visible page handles outside the worker delegation."""

    if not _fleet_reuse_enabled(agent) or not isinstance(params, dict):
        return None
    # Page.list stays readable for every assignment: a worker that cannot see
    # the Fleet cannot tell "my action did nothing" from "my result opened in a
    # tab I am not allowed to look at". Visibility and usability are separate
    # concerns — the pageId binding check below still governs what may be
    # operated, and _filter_page_list_response marks which rows are delegated.
    page_id = str(params.get("pageId") or "").strip()
    if not page_id:
        return None
    allowed_pages = {
        str(item).strip()
        for item in (getattr(agent, "allowed_page_ids", set()) or set())
        if str(item).strip()
    }
    page_fleets = getattr(agent, "page_fleet_ids", None)
    page_fleets = page_fleets if isinstance(page_fleets, dict) else {}
    allowed_fleets = {
        str(item).strip()
        for item in (getattr(agent, "allowed_fleet_ids", set()) or set())
        if str(item).strip()
    }
    assigned_fleet = str(getattr(agent, "assigned_fleet_id", "") or "").strip()
    if assigned_fleet:
        allowed_fleets.add(assigned_fleet)
    page_fleet = str(page_fleets.get(page_id) or "").strip()
    if page_id in allowed_pages and not (
        page_fleet and page_fleet not in allowed_fleets
    ):
        return None
    if _page_is_claimable(agent, page_id):
        # This is admission only. The shared PageLeaseManager performs the
        # authoritative atomic claim immediately before transport dispatch;
        # mutating allowed_page_ids here would recreate a check-then-act race.
        return None
    manager = getattr(agent, "page_lease_manager", None)
    worker_id = str(getattr(agent, "worker_id", "") or "").strip()
    owner = (
        str(manager.owner_for(page_id) or "")
        if manager is not None and hasattr(manager, "owner_for")
        else ""
    )
    quarantined = bool(
        manager is not None
        and hasattr(manager, "page_is_quarantined")
        and manager.page_is_quarantined(page_id)
    )
    return {
        "status": (
            "page_quarantined"
            if quarantined
            else "page_busy"
            if owner and owner != worker_id
            else "page_binding_violation"
        ),
        "error": (
            f"pageId {page_id!r} is outside this worker's Fleet or is held by"
            " another worker."
        ),
        "pageId": page_id,
        "pageFleetId": page_fleet,
        "assignedFleetId": assigned_fleet,
        "ownerWorkerId": owner or None,
        "quarantined": quarantined,
        "tool_was_executed": False,
        "next_instruction": (
            "Call Page.list to see this Fleet's pages; rows with"
            " claimable=true can be used directly. Quarantined rows must not"
            " be used. Otherwise create your own"
            " page with Page.create."
        ),
    }


def _page_is_claimable(agent: Any, page_id: str) -> bool:
    """Whether an undelegated page may be taken over on first use.

    Two conditions, both plain facts rather than inferences: the page belongs
    to a Fleet this worker was assigned, and no other live worker holds it.
    Cross-worker interference is the risk worth guarding; a stray site popup is
    only a wasted step the model corrects on its own.
    """

    if not page_id:
        return False
    fleet_pages = getattr(agent, "fleet_page_fleet_ids", None)
    if not isinstance(fleet_pages, dict):
        return False
    page_fleet = str(fleet_pages.get(page_id) or "").strip()
    if not page_fleet:
        return False
    allowed_fleets = {
        str(item).strip()
        for item in (getattr(agent, "allowed_fleet_ids", set()) or set())
        if str(item).strip()
    }
    assigned = str(getattr(agent, "assigned_fleet_id", "") or "").strip()
    if assigned:
        allowed_fleets.add(assigned)
    if page_fleet not in allowed_fleets:
        return False
    manager = getattr(agent, "page_lease_manager", None)
    worker_id = str(getattr(agent, "worker_id", "") or "").strip()
    if manager is not None and hasattr(manager, "owner_for"):
        if (
            hasattr(manager, "page_is_quarantined")
            and manager.page_is_quarantined(page_id)
        ):
            return False
        owner = str(manager.owner_for(page_id) or "")
        return not owner or owner == worker_id
    # Lightweight helper users have no concurrent worker runtime. Production
    # workers always receive the shared manager from BrowserAgentSpawner.
    return True


def _observe_page_binding_after(
    agent: Any,
    method: str,
    params: JsonDict,
    result: JsonDict,
) -> None:
    """Register only pages proven to belong to the assigned fleet."""

    if not _fleet_reuse_enabled(agent) or not isinstance(result, dict):
        return
    response = result.get("response")
    if result.get("error") or (
        isinstance(response, dict) and response.get("error")
    ):
        return
    allowed_pages = getattr(agent, "allowed_page_ids", None)
    if not isinstance(allowed_pages, set):
        allowed_pages = set()
        agent.allowed_page_ids = allowed_pages
    page_fleets = getattr(agent, "page_fleet_ids", None)
    if not isinstance(page_fleets, dict):
        page_fleets = {}
        agent.page_fleet_ids = page_fleets
    allowed_fleets = {
        str(item).strip()
        for item in (getattr(agent, "allowed_fleet_ids", set()) or set())
        if str(item).strip()
    }
    assigned_fleet = str(getattr(agent, "assigned_fleet_id", "") or "").strip()
    if assigned_fleet:
        allowed_fleets.add(assigned_fleet)

    addressed_page_id = str(params.get("pageId") or "").strip()
    manager = getattr(agent, "page_lease_manager", None)
    worker_id = str(getattr(agent, "worker_id", "") or "").strip()
    if (
        addressed_page_id
        and method != "Page.close"
        and manager is not None
        and hasattr(manager, "owner_for")
        and str(manager.owner_for(addressed_page_id) or "") == worker_id
    ):
        fleet_id = str(
            getattr(agent, "fleet_page_fleet_ids", {}).get(addressed_page_id)
            or assigned_fleet
            or ""
        ).strip()
        if fleet_id in allowed_fleets:
            allowed_pages.add(addressed_page_id)
            page_fleets[addressed_page_id] = fleet_id

    if method in {"Page.create", "Page.list"}:
        inherited_fleet = str(params.get("fleetId") or assigned_fleet).strip()
        for page in _pages_from_value(result):
            page_id = str(page.get("pageId") or page.get("page_id") or "").strip()
            row_fleet_id = str(
                page.get("fleetId") or page.get("fleet_id") or ""
            ).strip()
            fleet_id = (
                row_fleet_id
                if method == "Page.list"
                else row_fleet_id or inherited_fleet
            )
            if method == "Page.list" and page_id not in allowed_pages:
                continue
            if page_id and fleet_id in allowed_fleets:
                allowed_pages.add(page_id)
                page_fleets[page_id] = fleet_id
    elif method == "Page.close":
        page_id = str(params.get("pageId") or "").strip()
        if page_id:
            allowed_pages.discard(page_id)
            page_fleets.pop(page_id, None)
            fleet_pages = getattr(agent, "fleet_page_fleet_ids", None)
            if isinstance(fleet_pages, dict):
                fleet_pages.pop(page_id, None)


def _shown_page_inventory_rows(value: Any) -> List[JsonDict]:
    """Return the page identities actually present in a Page.list response.

    This is a harness-private evidence sidecar, not an authorization decision.
    Keep it available even when Fleet reuse is disabled: in that mode the raw
    response is shown unchanged, so those rows still count as pages the model
    has seen and may discharge the inventory-change notification.
    """
    rows: List[JsonDict] = []
    seen = set()
    for page in _pages_from_value(value):
        page_id = str(page.get("pageId") or page.get("page_id") or "").strip()
        fleet_id = str(
            page.get("fleetId") or page.get("fleet_id") or ""
        ).strip()
        key = (fleet_id, page_id)
        if not page_id or not fleet_id or key in seen:
            continue
        seen.add(key)
        rows.append({"pageId": page_id, "fleetId": fleet_id})
    return rows


def _filter_page_list_response(
    agent: Any,
    response: Any,
) -> Tuple[Any, JsonDict]:
    """Annotate Page.list rows with delegation and claimability.

    Hiding non-delegated rows made a worker unable to observe that its own
    submit had opened a result tab, which reads as "the action did nothing" and
    drives pointless retries. Every row in the assigned Fleet is therefore
    returned, tagged ``delegated`` (already this worker's) and ``claimable``
    (usable on first touch because no other worker holds it).

    This listing is also what teaches the binding guard which pages exist and
    where: a page can only be claimed after the worker has seen it here, which
    keeps discovery an explicit model act rather than an inference.

    Rows outside the assigned Fleet remain hidden — that is a tenancy boundary,
    not a usability one.
    """

    if not _fleet_reuse_enabled(agent):
        return response, {
            "_shownInventoryPages": _shown_page_inventory_rows(response),
        }
    allowed_pages = {
        str(item).strip()
        for item in (getattr(agent, "allowed_page_ids", set()) or set())
        if str(item).strip()
    }
    allowed_fleets = {
        str(item).strip()
        for item in (getattr(agent, "allowed_fleet_ids", set()) or set())
        if str(item).strip()
    }
    assigned_fleet = str(getattr(agent, "assigned_fleet_id", "") or "").strip()
    if assigned_fleet:
        allowed_fleets.add(assigned_fleet)
    manager = getattr(agent, "page_lease_manager", None)
    worker_id = str(getattr(agent, "worker_id", "") or "").strip()
    shown_inventory_pages: List[JsonDict] = []
    # Remember where each visible page lives so the binding guard can decide
    # claimability later without re-listing.
    fleet_pages = getattr(agent, "fleet_page_fleet_ids", None)
    if not isinstance(fleet_pages, dict):
        fleet_pages = {}
        agent.fleet_page_fleet_ids = fleet_pages
    hidden_count = 0
    delegated_count = 0
    claimable_count = 0
    held_count = 0
    quarantined_count = 0

    def filtered(value: Any) -> Any:
        nonlocal hidden_count, delegated_count, claimable_count
        nonlocal held_count, quarantined_count
        if isinstance(value, list):
            is_page_list = any(
                isinstance(item, dict)
                and bool(str(item.get("pageId") or item.get("page_id") or "").strip())
                for item in value
            )
            if is_page_list:
                kept: List[Any] = []
                for item in value:
                    if not isinstance(item, dict):
                        continue
                    page_id = str(
                        item.get("pageId") or item.get("page_id") or ""
                    ).strip()
                    row_fleet = str(
                        item.get("fleetId") or item.get("fleet_id") or ""
                    ).strip()
                    if not page_id or not row_fleet:
                        hidden_count += 1
                        continue
                    if allowed_fleets and row_fleet not in allowed_fleets:
                        hidden_count += 1
                        continue
                    fleet_pages[page_id] = row_fleet
                    if manager is not None and hasattr(manager, "observe_inventory"):
                        manager.observe_inventory(
                            row_fleet,
                            [page_id],
                        )
                    shown_inventory_pages.append({
                        "pageId": page_id,
                        "fleetId": row_fleet,
                    })
                    owner = (
                        str(manager.owner_for(page_id) or "")
                        if manager is not None and hasattr(manager, "owner_for")
                        else ""
                    )
                    delegated = page_id in allowed_pages or bool(
                        worker_id and owner == worker_id
                    )
                    quarantined = bool(
                        manager is not None
                        and hasattr(manager, "page_is_quarantined")
                        and manager.page_is_quarantined(page_id)
                    )
                    if manager is not None and hasattr(manager, "owner_for"):
                        claimable = not delegated and not owner and not quarantined
                    else:
                        claimable = not delegated and not quarantined
                    row = filtered(item)
                    if isinstance(row, dict):
                        row["delegated"] = delegated
                        row["claimable"] = claimable
                        row["leasedByMe"] = bool(worker_id and owner == worker_id)
                        row["busy"] = bool(owner and owner != worker_id)
                        row["quarantined"] = quarantined
                    if quarantined:
                        quarantined_count += 1
                    elif delegated:
                        delegated_count += 1
                    elif claimable:
                        claimable_count += 1
                    else:
                        held_count += 1
                    kept.append(row)
                return kept
            return [filtered(item) for item in value]
        if isinstance(value, dict):
            return {key: filtered(item) for key, item in value.items()}
        return value

    sanitized = filtered(copy.deepcopy(response))
    receipt: JsonDict = {
        "pageListFiltered": True,
        "delegatedPageCount": delegated_count,
        "claimablePageCount": claimable_count,
        "heldByOtherWorkerCount": held_count,
        "quarantinedPageCount": quarantined_count,
        "hiddenPageCount": hidden_count,
        # Harness-private sidecar. The caller removes it before merging the
        # public receipt and defers discharge until all result post-processing
        # has completed successfully.
        "_shownInventoryPages": shown_inventory_pages,
    }
    if claimable_count:
        receipt["next_instruction"] = (
            "Rows with claimable=true are free: address one by its pageId and"
            " it becomes yours on first use. If an action of yours looked like"
            " it did nothing, its result most likely rendered in one of these"
            " tabs rather than in your current page. Rows with busy=true are"
            " held by another worker; rows with quarantined=true are unusable."
        )
    return sanitized, receipt


# Methods a worker may still issue while re-perception is pending. The two
# reads are the exit condition itself. Hitl.requestPause is here because the
# ownerless-barrier whitelist below (`resolverRequired`) already allows it: a
# worker that decides it needs a human must be able to say so. Rejecting it
# left browser-003 in task 48b4d7d7 unable to escalate — it could read the
# CAPTCHA page and nothing else, and died as `blocked_content_suppression`
# with "Hitl.requestPause also blocked by fleet_reperception_required gate".
_REPERCEPTION_ALLOWED_METHODS = {
    "Page.getState",
    "DOM.getAXTree",
    "Hitl.requestPause",
}


async def _fleet_auth_barrier_before_call(
    agent: Any,
    method: str,
    params: JsonDict,
    *,
    emit_workflow_telemetry: bool = False,
) -> Optional[JsonDict]:
    barrier = getattr(agent, "fleet_auth_barrier", None)
    fleet_id = str(getattr(agent, "assigned_fleet_id", "") or "").strip()
    worker_id = str(getattr(agent, "worker_id", "") or "").strip()
    if barrier is None or not fleet_id or not worker_id:
        return None
    if method == "Workflow.execute":
        receipt = await barrier.workflow_fence_before(
            fleet_id,
            worker_id,
            seen_generation=int(
                getattr(agent, "fleet_barrier_generation", 0) or 0
            ),
        )
        if emit_workflow_telemetry:
            payload = {
                **dict(receipt),
                "source": "raw_workflow",
                "method": method,
                "runId": None,
                "workerId": worker_id,
            }
            logger = getattr(agent, "logger", None)
            if logger is not None and hasattr(logger, "write"):
                logger.write("workflow.auth_fence.before", payload)
                if (
                    not receipt.get("allowed")
                    and receipt.get("generationChanged")
                ):
                    logger.write(
                        "workflow.auth_generation_changed",
                        payload,
                    )
        if receipt.get("allowed"):
            return None
        return {
            **receipt,
            "next_instruction": (
                "Do not start or trust an opaque workflow while shared"
                " authentication is changing. After the barrier opens, call"
                " Page.getState and DOM.getAXTree, then retry the same row."
            ),
        }
    receipt = await barrier.before_call(
        fleet_id,
        worker_id,
        seen_generation=int(
            getattr(agent, "fleet_barrier_generation", 0) or 0
        ),
    )
    if not receipt.get("allowed"):
        if receipt.get("resolverRequired") and method in {
            "Page.getState",
            "Page.create",
            "DOM.getAXTree",
            "Hitl.requestPause",
        }:
            # An ownerless but still-closed gate permits page-scoped diagnosis.
            # Page.create and Hitl.requestPause proceed to explicit atomic
            # claims below; Page.list remains delegation-scoped and no
            # arbitrary business call becomes resolver.
            return None
        return receipt
    if receipt.get("generationChanged"):
        generation = int(receipt.get("generation") or 0)
        # Latch one target generation. ``seen_generation`` intentionally stays
        # unchanged until both observations complete, so before_call will keep
        # reporting generationChanged in the meantime.  Resetting the flags on
        # every such call makes Page.getState and DOM.getAXTree erase each
        # other's progress forever.
        if (
            not getattr(agent, "fleet_reperception_pending", False)
            or int(
                getattr(agent, "fleet_reperception_generation", -1) or -1
            )
            != generation
        ):
            agent.fleet_reperception_generation = generation
            agent.fleet_reperception_pending = True
            agent.fleet_reperception_state_seen = False
            agent.fleet_reperception_tree_seen = False
            agent.axtree_invalidated = True
    if not getattr(agent, "fleet_reperception_pending", False):
        return None
    if method not in _REPERCEPTION_ALLOWED_METHODS:
        return {
            "status": "fleet_reperception_required",
            "reasonKind": "fleet_reperception_required",
            "fleetId": fleet_id,
            "generation": receipt.get("generation"),
            "tool_was_executed": False,
            "retryable": True,
            "next_instruction": (
                "The shared authentication state changed. Call Page.getState"
                " and then DOM.getAXTree for this page before any other action."
            ),
        }
    return None


def _workflow_auth_started_generation(agent: Any, method: str) -> Optional[int]:
    if method != "Workflow.execute":
        return None
    barrier = getattr(agent, "fleet_auth_barrier", None)
    fleet_id = str(getattr(agent, "assigned_fleet_id", "") or "").strip()
    worker_id = str(getattr(agent, "worker_id", "") or "").strip()
    if barrier is None or not fleet_id or not worker_id:
        return None
    return int(barrier.generation(fleet_id))


async def _quarantine_workflow_result_after_auth_change(
    agent: Any,
    method: str,
    result: JsonDict,
    *,
    started_generation: Optional[int],
    emit_telemetry: bool,
) -> JsonDict:
    if method != "Workflow.execute" or started_generation is None:
        return result
    barrier = getattr(agent, "fleet_auth_barrier", None)
    fleet_id = str(getattr(agent, "assigned_fleet_id", "") or "").strip()
    if barrier is None or not fleet_id:
        return result
    receipt = await barrier.workflow_fence_after(
        fleet_id,
        started_generation=int(started_generation),
    )
    if receipt.get("valid"):
        return result
    logger = getattr(agent, "logger", None)
    payload = {
        **receipt,
        "source": "raw_workflow",
        "method": method,
        "runId": None,
        "workerId": str(getattr(agent, "worker_id", "") or ""),
    }
    if emit_telemetry and logger is not None and hasattr(logger, "write"):
        if receipt.get("generationChanged"):
            logger.write("workflow.auth_generation_changed", payload)
        logger.write("workflow.row_quarantined", payload)
    return {
        "method": method,
        "params": result.get("params") if isinstance(result, dict) else {},
        "status": "workflow_row_quarantined",
        "error": (
            "Workflow result was isolated because the shared authentication"
            " barrier or generation changed while it was in flight."
        ),
        "authFence": receipt,
        "tool_was_executed": True,
        "retryable": True,
        "next_instruction": (
            "Call Page.getState and DOM.getAXTree after the barrier opens, then"
            " retry only this row. Do not persist variables from this run."
        ),
    }


def _fleet_auth_barrier_after_call(
    agent: Any,
    method: str,
    result: JsonDict,
) -> None:
    if not getattr(agent, "fleet_reperception_pending", False):
        return
    # The exit condition is "this worker re-read the page", which is a fact
    # about the CALL, not about the page's health. `_invoke_result_failed`
    # answers a different question: it also fails on `response.data.error`,
    # which for Page.getState is the page's own last-navigation error. On a
    # risk-controlled page that field is permanent, so the gate whose exit
    # requires reading the page could never be opened by reading the page.
    if not classify_call_outcome(result).succeeded:
        return
    if method == "Page.getState":
        agent.fleet_reperception_state_seen = True
    elif method == "DOM.getAXTree":
        agent.fleet_reperception_tree_seen = True
    if not (
        getattr(agent, "fleet_reperception_state_seen", False)
        and getattr(agent, "fleet_reperception_tree_seen", False)
    ):
        return
    generation = int(
        getattr(agent, "fleet_reperception_generation", 0) or 0
    )
    agent.fleet_barrier_generation = generation
    agent.fleet_reperception_pending = False


async def _claim_fleet_auth_barrier_for_hitl(
    agent: Any,
    method: str,
    params: JsonDict,
) -> Optional[JsonDict]:
    """Admit, then atomically select the one worker allowed to enter HITL.

    Every `Hitl.requestPause` — the model's own call and the harness's
    auto-adjudicated one — passes through here, so this is where attendance and
    the cumulative pause budget are ENFORCED and ACCOUNTED. Putting them only in
    the auto path left the manual call as a hole wide enough to drive the whole
    mechanism through: a model handed an `hitl_unattended` verdict could reach
    the same 900-second wait by calling the tool itself.
    """

    if method != "Hitl.requestPause":
        return None
    page_id = str(params.get("pageId") or "").strip()
    admission = _hitl_admission(agent, page_id)
    if admission is not None:
        return await _refuse_hitl(agent, admission, page_id, method)
    barrier = getattr(agent, "fleet_auth_barrier", None)
    fleet_id = str(getattr(agent, "assigned_fleet_id", "") or "").strip()
    worker_id = str(getattr(agent, "worker_id", "") or "").strip()
    if barrier is None or not fleet_id or not worker_id:
        _count_hitl_pause_round(agent, page_id)
        return None
    claim = await barrier.claim(
        fleet_id,
        worker_id,
        str(params.get("reason") or params.get("purpose") or "manual HITL"),
    )
    if claim.get("claimed"):
        # Counted only once the pause is actually going to be dispatched: a
        # worker turned away at the gate never bothered a human.
        _count_hitl_pause_round(agent, page_id)
        return None
    return {
        "status": "fleet_auth_gated",
        "reasonKind": "fleet_auth_gated",
        "fleetId": fleet_id,
        "resolverWorkerId": claim.get("resolverWorkerId"),
        "generation": claim.get("generation"),
        "tool_was_executed": False,
        "retryable": True,
        "next_instruction": (
            "Another worker owns authentication recovery for this fleet. "
            "Do not request HITL or act on the shared cookie jar until it finishes."
        ),
    }


async def _claim_ownerless_fleet_auth_barrier_for_page_create(
    agent: Any,
    method: str,
    params: JsonDict,
) -> Tuple[Optional[JsonDict], bool]:
    """Select one resolver before Page.create crosses an ownerless gate.

    Returns ``(guard, takeover_claimed)``. Open fleets do not need a claim;
    an existing resolver may continue; a competing worker remains gated.
    """

    if method != "Page.create":
        return None, False
    barrier = getattr(agent, "fleet_auth_barrier", None)
    fleet_id = str(getattr(agent, "assigned_fleet_id", "") or "").strip()
    worker_id = str(getattr(agent, "worker_id", "") or "").strip()
    if barrier is None or not fleet_id or not worker_id:
        return None, False
    claim = await barrier.claim_ownerless(
        fleet_id,
        worker_id,
        "Create or recover a page for ownerless authentication recovery",
    )
    if not claim.get("required"):
        return None, False
    if claim.get("claimed"):
        takeover = bool(claim.get("takeover"))
        if takeover:
            logger = getattr(agent, "logger", None)
            if logger is not None:
                logger.write(
                    "auth_fleet.resolver_claimed_for_page_create",
                    {
                        "fleetId": fleet_id,
                        "workerId": worker_id,
                        "generation": claim.get("generation"),
                    },
                )
        return None, takeover
    return {
        "status": "fleet_auth_gated",
        "reasonKind": "fleet_auth_gated",
        "fleetId": fleet_id,
        "resolverWorkerId": claim.get("resolverWorkerId"),
        "generation": claim.get("generation"),
        "tool_was_executed": False,
        "retryable": True,
        "next_instruction": (
            "Another worker atomically claimed ownerless authentication recovery. "
            "Do not create a page or act on the shared cookie jar until it finishes."
        ),
    }, False


async def _relinquish_fleet_auth_resolver_after_failed_pause(
    agent: Any,
    method: str,
    *,
    pause_succeeded: bool,
) -> JsonDict:
    if method != "Hitl.requestPause" or pause_succeeded:
        return {}
    barrier = getattr(agent, "fleet_auth_barrier", None)
    fleet_id = str(getattr(agent, "assigned_fleet_id", "") or "").strip()
    worker_id = str(getattr(agent, "worker_id", "") or "").strip()
    if barrier is None or not fleet_id or not worker_id:
        return {}
    receipt = await barrier.relinquish(
        fleet_id,
        worker_id,
        reason="Hitl.requestPause failed before the human wait began",
    )
    if receipt.get("relinquished"):
        logger = getattr(agent, "logger", None)
        if logger is not None:
            logger.write(
                "auth_fleet.resolver_relinquished",
                {"fleetId": fleet_id, "workerId": worker_id, **receipt},
            )
    return receipt


async def _relinquish_fleet_auth_resolver_after_failed_recovery_page_create(
    agent: Any,
    method: str,
    *,
    takeover_claimed: bool,
    call_succeeded: bool,
) -> JsonDict:
    """Release a failed Page.create takeover without opening the gate."""

    if method != "Page.create" or not takeover_claimed or call_succeeded:
        return {}
    barrier = getattr(agent, "fleet_auth_barrier", None)
    fleet_id = str(getattr(agent, "assigned_fleet_id", "") or "").strip()
    worker_id = str(getattr(agent, "worker_id", "") or "").strip()
    if barrier is None or not fleet_id or not worker_id:
        return {}
    receipt = await barrier.relinquish(
        fleet_id,
        worker_id,
        reason="Recovery Page.create failed before a challenge page was available",
    )
    if receipt.get("relinquished"):
        logger = getattr(agent, "logger", None)
        if logger is not None:
            logger.write(
                "auth_fleet.resolver_relinquished_after_page_create",
                {"fleetId": fleet_id, "workerId": worker_id, **receipt},
            )
    return receipt


async def _execute_browser_capability_tool(
    agent: Any,
    tool_name: str,
    tool_input: JsonDict,
    step: int,
) -> Tuple[JsonDict, bool]:
    direct_method = str(tool_name or "").strip()
    if tool_name == "browser_call":
        method = str(tool_input.get("method", "")).strip()
        params, params_error = parse_browser_call_params(tool_input)
        reason = str(tool_input.get("reason") or "").strip()
    elif direct_method in agent.capability_methods:
        method = direct_method
        params, params_error = parse_direct_capability_params(tool_input)
        reason = str(
            tool_input.get("reason")
            or tool_input.get("purpose")
            or f"direct capability tool call: {method}"
        ).strip()
        agent.logger.write(
            "tool.direct_capability_wrapped",
            {
                "tool": method,
                "params": agent._trim_for_log(params),
            },
        )
    else:
        result = {
            "error": f"Unknown harness tool: {tool_name}",
            **_allowed_tool_hint(agent),
        }
        agent.logger.write("tool.error", result)
        agent.trace.append({"type": "tool_error", "result": result})
        return result, False

    runtime_policy = (
        tool_input.get("runtime_policy")
        if isinstance(tool_input, dict) else None
    )
    raw_navigation_context = (
        tool_input.get("navigation_context")
        if tool_name == "browser_call" and isinstance(tool_input, dict)
        else None
    )
    raw_content_binding = (
        tool_input.get("content_binding")
        if tool_name == "browser_call" and isinstance(tool_input, dict)
        else None
    )
    navigation_context: JsonDict = {}
    runtime_receipt: JsonDict = {}
    runtime_json_expression = ""

    if params_error:
        result = {
            "method": method,
            "error": params_error,
            "expected": "params must be a JSON object, e.g. {\"pageId\":\"...\"}; pass {} when there are no params",
        }
        attach_error_classification(result, method=method)
        attach_method_schema(result, method, agent.method_schemas)
        agent.logger.write("browser.call.params_error", result)
        _observe_progress_after(agent, method or "browser_call.params_error", result)
        progress_result = _check_progress_before(
            agent,
            method or "browser_call",
            params if isinstance(params, dict) else tool_input,
            step,
            charge_diagnostic=False,
        )
        if progress_result is not None:
            agent.trace.append({"type": "progress_intervention", "result": progress_result})
            return progress_result, False
        agent.trace.append({"type": "browser_call_params_error", "result": result})
        return result, False

    if agent.capability_methods and method not in agent.capability_methods:
        result = {
            "error": f"ABCP capability not found: {method}",
            "known_methods": sorted(agent.capability_methods),
        }
        attach_error_classification(result, method=method)
        attach_method_schema(result, method, agent.method_schemas)
        agent.logger.write("browser.call.rejected", result)
        _observe_progress_after(agent, method or "browser_call_rejected", result)
        progress_result = _check_progress_before(
            agent, method or "browser_call", params, step,
            charge_diagnostic=False,
        )
        if progress_result is not None:
            agent.trace.append({"type": "progress_intervention", "result": progress_result})
            return progress_result, False
        agent.trace.append({"type": "browser_call_rejected", "result": result})
        return result, False

    params, shadow_dom_defaulted = _default_semantic_tree_shadow_dom(
        method,
        params,
        getattr(agent, "method_schemas", {}),
    )
    if shadow_dom_defaulted:
        agent.logger.write(
            "semantic_tree.shadow_dom_defaulted",
            {
                "method": method,
                "pageId": str(params.get("pageId") or ""),
                "includeShadowDom": True,
            },
        )

    navigation_context, navigation_context_error = _prepare_navigation_context(
        agent,
        method,
        raw_navigation_context,
    )
    if navigation_context_error is not None:
        attach_method_schema(
            navigation_context_error,
            method,
            agent.method_schemas,
        )
        agent.logger.write(
            "browser.call.navigation_context_rejected",
            navigation_context_error,
        )
        agent.trace.append({
            "type": "navigation_context_rejected",
            "result": navigation_context_error,
        })
        return navigation_context_error, False

    params, screenshot_output_receipt = _normalize_screenshot_output(method, params)
    if screenshot_output_receipt is not None:
        agent.logger.write(
            "browser.call.screenshot_output_normalized",
            {"method": method, **screenshot_output_receipt},
        )

    if method == "Runtime.evaluate":
        prepared, policy_error = _prepare_runtime_evaluation(
            agent,
            params,
            runtime_policy,
            origin="model_browser_call" if tool_name == "browser_call" else "model_direct_capability",
        )
        if policy_error is not None:
            attach_method_schema(policy_error, method, agent.method_schemas)
            agent.logger.write("runtime.evaluate.rejected", policy_error)
            agent.trace.append({"type": "runtime_policy_rejected", "result": policy_error})
            return policy_error, False
        escalation, escalation_error = runtime_last_resort_evidence(
            agent,
            page_id=str(params.get("pageId") or ""),
        )
        if escalation_error is not None:
            attach_method_schema(escalation_error, method, agent.method_schemas)
            agent.logger.write("runtime.evaluate.escalation_rejected", escalation_error)
            agent.trace.append({
                "type": "runtime_escalation_rejected",
                "result": escalation_error,
            })
            return escalation_error, False
        params = dict(prepared.params)
        runtime_receipt = dict(prepared.receipt)
        runtime_receipt["lastResortEvidence"] = escalation
        agent.logger.write("runtime.evaluate.escalation_authorized", escalation)
        if runtime_receipt.get("resultMode") == "json":
            runtime_json_expression = _build_runtime_json_expression(
                str(params.get("expression") or "")
            )
            params["expression"] = runtime_json_expression
            params["returnByValue"] = True

    if method == "Workflow.execute":
        if not workflow_execution_enabled(agent):
            disabled = workflow_execution_disabled_result(
                source="browser_call.Workflow.execute"
            )
            agent.logger.write("workflow.execute.runtime_disabled", disabled)
            agent.trace.append({
                "type": "workflow_runtime_disabled",
                "result": disabled,
            })
            return disabled, False
        contract = getattr(agent, "worker_contract", None)
        task_type = resolve_task_type_fail_closed(
            contract.get("task_type") if isinstance(contract, dict) else None
        )
        normalized_workflow, workflow_error = validate_workflow_params(
            params,
            capability_methods=getattr(agent, "capability_methods", set()),
            task_type=task_type,
            allow_runtime=False,
            enforce_lifecycle=True,
        )
        if workflow_error is not None:
            attach_method_schema(workflow_error, method, agent.method_schemas)
            agent.logger.write("workflow.execute.rejected", workflow_error)
            agent.trace.append({"type": "workflow_policy_rejected", "result": workflow_error})
            return workflow_error, False
        params = dict(normalized_workflow)

    contract_result = _check_worker_contract(agent, method)
    if contract_result is not None:
        attach_error_classification(contract_result, method=method)
        attach_method_schema(contract_result, method, agent.method_schemas)
        agent.logger.write("browser.call.contract_violation", contract_result)
        _observe_progress_after(agent, method or "browser_call.contract_violation", contract_result)
        progress_result = _check_progress_before(
            agent, method or "browser_call", params, step,
            charge_diagnostic=False,
        )
        if progress_result is not None:
            agent.trace.append({"type": "progress_intervention", "result": progress_result})
            return progress_result, False
        agent.trace.append({"type": "contract_violation", "result": contract_result})
        return contract_result, False

    memory_scope_guard = _check_cross_task_memory_scope(agent, method, params)
    if memory_scope_guard is not None:
        agent.logger.write(
            "browser.call.cross_task_memory_rejected", memory_scope_guard
        )
        agent.trace.append({
            "type": "cross_task_memory_guard",
            "result": memory_scope_guard,
        })
        return memory_scope_guard, False

    fleet_binding_guard, fleet_binding_receipt = _apply_fleet_binding(
        agent, method, params
    )
    if fleet_binding_guard is not None:
        attach_error_classification(fleet_binding_guard, method=method)
        attach_method_schema(fleet_binding_guard, method, agent.method_schemas)
        agent.logger.write("browser.call.fleet_binding_rejected", fleet_binding_guard)
        agent.trace.append({
            "type": "fleet_binding_guard",
            "method": method,
            "params": params,
            "result": fleet_binding_guard,
        })
        return fleet_binding_guard, False

    page_binding_guard = _check_page_binding(agent, method, params)
    if page_binding_guard is not None:
        attach_error_classification(page_binding_guard, method=method)
        attach_method_schema(page_binding_guard, method, agent.method_schemas)
        agent.logger.write("browser.call.page_binding_rejected", page_binding_guard)
        agent.trace.append({
            "type": "page_binding_guard",
            "method": method,
            "params": params,
            "result": page_binding_guard,
        })
        return page_binding_guard, False

    auth_barrier_guard = await _fleet_auth_barrier_before_call(
        agent,
        method,
        params,
        emit_workflow_telemetry=True,
    )
    if auth_barrier_guard is not None:
        agent.logger.write("browser.call.fleet_auth_gated", auth_barrier_guard)
        agent.trace.append({
            "type": "fleet_auth_gate",
            "method": method,
            "params": params,
            "result": auth_barrier_guard,
        })
        return auth_barrier_guard, False
    workflow_auth_started_generation = _workflow_auth_started_generation(
        agent, method
    )

    lifecycle_guard = await _page_lifecycle_guard_before(agent, method, params)
    if lifecycle_guard is not None:
        agent.logger.write("browser.call.lifecycle_gated", lifecycle_guard)
        agent.trace.append({
            "type": "page_lifecycle_gate",
            "method": method,
            "result": lifecycle_guard,
        })
        return lifecycle_guard, False

    screenshot_guard = _check_screenshot_misuse(method, params, reason)
    if screenshot_guard is not None:
        agent.logger.write("browser.call.screenshot_rejected", screenshot_guard)
        agent.trace.append({"type": "screenshot_guard", "result": screenshot_guard})
        return screenshot_guard, False

    target_param_guard = _check_target_param_requirements(
        method, params, getattr(agent, "method_schemas", {})
    )
    if target_param_guard is not None:
        attach_error_classification(target_param_guard, method=method)
        attach_method_schema(target_param_guard, method, agent.method_schemas)
        agent.logger.write("browser.call.params_error", target_param_guard)
        _observe_progress_after(agent, method or "browser_call.params_error", target_param_guard)
        progress_result = _check_progress_before(
            agent, method or "browser_call", params, step,
            charge_diagnostic=False,
        )
        if progress_result is not None:
            agent.trace.append({"type": "progress_intervention", "result": progress_result})
            return progress_result, False
        agent.trace.append({"type": "browser_call_params_error", "result": target_param_guard})
        return target_param_guard, False

    stale_target = _check_stale_axtree_target(
        agent,
        method,
        params,
        # Model-initiated calls only bypass the guard in the explicit "on"
        # mode; composite tools opt in per-call regardless ("composite_only").
        allow_rematch=_browser_side_rematch_mode(agent) == "on",
    )
    if stale_target is not None:
        agent.logger.write("browser.call.stale_axtree_target", stale_target)
        agent.trace.append({"type": "stale_axtree_target", "result": stale_target})
        return stale_target, False

    progress_result = _check_progress_before(agent, method, params, step)
    if progress_result is not None:
        agent.trace.append({"type": "progress_intervention", "result": progress_result})
        return progress_result, False

    if ensure_required_purpose(
        agent.methods_requiring_purpose,
        method,
        params,
        reason,
        purpose_hints=agent.purpose_hints,
    ):
        agent.logger.write(
            "browser.call.purpose_added",
            {
                "method": method,
                "purpose": params.get("purpose"),
            },
        )
    _ensure_hitl_request_reason(method, params, reason)
    page_create_claim_guard, page_create_takeover_claimed = (
        await _claim_ownerless_fleet_auth_barrier_for_page_create(
            agent, method, params
        )
    )
    if page_create_claim_guard is not None:
        agent.logger.write("browser.call.fleet_auth_gated", page_create_claim_guard)
        agent.trace.append({
            "type": "fleet_auth_gate",
            "method": method,
            "params": params,
            "result": page_create_claim_guard,
        })
        return page_create_claim_guard, False
    # A bounded VL solve runs before the pause is issued. It claims the fleet auth
    # barrier itself (inside the autosolver) so concurrent same-fleet workers can
    # never drive the same challenge; on success it verifies and releases the
    # barrier, on failure it keeps ownership and the pause below inherits it.
    captcha_short_circuit = await _maybe_autosolve_before_model_pause(
        agent, method, params, step
    )
    if captcha_short_circuit is not None:
        agent.logger.write("browser.call.captcha_auto_solved", captcha_short_circuit)
        agent.trace.append({
            "type": "captcha_auto_solved",
            "method": method,
            "params": params,
            "result": captcha_short_circuit,
        })
        return captcha_short_circuit, False
    hitl_claim_guard = await _claim_fleet_auth_barrier_for_hitl(
        agent, method, params
    )
    if hitl_claim_guard is not None:
        agent.logger.write("browser.call.fleet_auth_gated", hitl_claim_guard)
        agent.trace.append({
            "type": "fleet_auth_gate",
            "method": method,
            "params": params,
            "result": hitl_claim_guard,
        })
        return hitl_claim_guard, False

    page_create_should_stop = False
    hitl_pause_succeeded = False
    page_list_shown: Optional[List[JsonDict]] = None
    try:
        runner = getattr(agent, "render_recovery_runner", None)
        if runner is None:
            runner = build_render_recovery_runner(
                browser=agent.browser,
                logger=agent.logger,
                capability_methods=agent.capability_methods,
                recent_recoveries=agent._render_recovery_recent,
            )
            agent.render_recovery_runner = runner
        # Sample the event serial + held page before the call so post-action
        # invalidation can detect a same-page DOM.axTreeUpdated that landed
        # mid-call (race fix) without letting a cross-page event suppress it.
        event_serial_before = int(getattr(agent, "axtree_event_serial", 0) or 0)
        page_before = str(getattr(agent, "axtree_page_id", "") or "")
        if method == "Hitl.requestPause":
            await _capture_hitl_pause_snapshot(
                agent,
                runner,
                str(params.get("pageId") or ""),
                step,
            )
        _page_lifecycle_before_action(agent, method, params)
        reused_download = (
            _reusable_download_response(agent, params)
            if method == "Download.start" else None
        )
        if method == "Download.start" and reused_download is None:
            reused_download = await _refresh_active_download_response(
                agent, runner, params,
            )
        download_timeout_error: Optional[ABCPTransportError] = None
        if reused_download is not None:
            response, _recovery = reused_download, None
            agent.logger.write(
                "download.operation_reused",
                reused_download.get("downloadReconciliation") or {},
            )
        else:
            try:
                response, _recovery = await runner.call(method, params)
            except ABCPTransportError as exc:
                # JSON-RPC action timeouts may happen after Electron has begun
                # a download.  Contain this one method locally so reconciliation
                # runs before the generic transport handler discards the call.
                if method != "Download.start" or not _download_start_timed_out(exc):
                    raise
                download_timeout_error = exc
                response, _recovery = {}, None

        if method == "Download.list":
            known_downloads = _download_receipt_store(agent)
            for download_record in _download_records(response):
                if _download_operation_key(download_record) in known_downloads:
                    _remember_download_record(agent, download_record)
        elif method == "Download.start" and (
            download_timeout_error is not None
            or _download_start_timed_out(response)
        ):
            reconciliation = await _reconcile_download_start_timeout(
                agent=agent,
                runner=runner,
                params=params,
                timeout_error=download_timeout_error,
            )
            agent.logger.write(
                "download.timeout_reconciled",
                {
                    "url": str(params.get("url") or ""),
                    "savePath": str(params.get("savePath") or ""),
                    **reconciliation,
                },
            )
            if reconciliation.get("classification") in {"completed", "active"}:
                receipt = reconciliation.get("receipt") or {}
                response = {
                    "observation": (
                        "Download.start timed out, but Download.list proved"
                        " that the operation exists. Do not retry it."
                    ),
                    "data": {
                        "success": True,
                        "downloadId": receipt.get("downloadId"),
                        "state": receipt.get("state"),
                        "savePath": receipt.get("savePath"),
                        "url": receipt.get("url"),
                        "reconciledAfterTimeout": True,
                    },
                    "downloadReconciliation": reconciliation,
                }
            elif reconciliation.get("classification") == "failed":
                response = {
                    "error": "The reconciled download reached a terminal failed state.",
                    "downloadReconciliation": reconciliation,
                    "suggested_prompt": (
                        "The exact prior operation is proven failed, so one"
                        " bounded retry is allowed after checking page readiness."
                    ),
                }
            else:
                response = {
                    "error": str(download_timeout_error or "Download.start timed out"),
                    "downloadReconciliation": reconciliation,
                    "suggested_prompt": (
                        "This Download.start timed out and the exact requested"
                        " URL/savePath operation could not be verified. The"
                        " redirect may already have saved a file to the browser's"
                        " default download directory. Do not resend the same URL."
                        " If the page exposes a final direct file URL, retry that"
                        " direct URL once with the required savePath."
                    ),
                }
        elif method == "Download.start" and isinstance(response, dict):
            data = response.get("data")
            if isinstance(data, dict) and (
                data.get("downloadId") or data.get("id")
            ):
                _remember_download_record(agent, {
                    **params,
                    **data,
                })
        if method == "Runtime.evaluate" and runtime_receipt:
            runtime_receipt["attempts"] = [
                _runtime_attempt_receipt(response, "isolated")
            ]
            if (
                _invoke_result_failed({"method": method, "response": response})
                and runtime_receipt.get("mainFallbackAuthorized") is True
                and _runtime_main_fallback_signaled(response)
            ):
                main_params = {**params, "world": "main"}
                agent.logger.write(
                    "runtime.evaluate.main_fallback_authorized",
                    {
                        "pageId": str(params.get("pageId") or ""),
                        "reasonKind": runtime_receipt.get("reasonKind"),
                        "signal": MAIN_WORLD_REQUIRED_PREFIX,
                    },
                )
                response, _recovery = await runner.call(method, main_params)
                runtime_receipt["attempts"].append(
                    _runtime_attempt_receipt(response, "main")
                )

            final_attempt = runtime_receipt["attempts"][-1]
            runtime_receipt["executedWorld"] = final_attempt.get("executedWorld")
            expected_world = str(final_attempt.get("requestedWorld") or "")
            runtime_receipt["dispatchedWorld"] = expected_world
            runtime_receipt["worldEvidenceStrength"] = final_attempt.get(
                "evidenceStrength", "strong"
            )
            if not _invoke_result_failed({"method": method, "response": response}):
                metadata_supplied = _runtime_response_world_metadata_supplied(response)
                if metadata_supplied and not _runtime_response_world_verified(
                    response, expected_world
                ):
                    response = {
                        "error": (
                            "Runtime.evaluate completed with invalid or mismatched"
                            " platform world evidence"
                            f" (expected {expected_world})"
                        )
                    }
                    final_attempt["status"] = "failed"
                    final_attempt["failureKind"] = "world_evidence_mismatch"
                    final_attempt["evidence"] = "platform_response_invalid"
                    final_attempt["evidenceStrength"] = "invalid"
                    final_attempt["error"] = str(response["error"])
                    runtime_receipt["worldEvidenceStrength"] = "invalid"
                elif not metadata_supplied:
                    # Compatibility path for deployed panels predating the
                    # runtimeEvaluation response envelope.  This proves only
                    # which strict world the harness dispatched; it deliberately
                    # does not claim which world the platform executed.
                    agent.logger.write(
                        "runtime.evaluate.world_evidence_degraded",
                        {
                            "pageId": str(params.get("pageId") or ""),
                            "reasonKind": runtime_receipt.get("reasonKind"),
                            "dispatchedWorld": expected_world,
                            "evidence": "harness_dispatched_world",
                            "resultAccepted": True,
                        },
                    )
        _page_lifecycle_after_action(agent, method, params, response)
        response = agent._capture_artifacts(method, response)
        structural_challenge = detect_structural_challenge(method, response)
        record_file_action = getattr(agent, "_capture_file_action", None)
        if callable(record_file_action):
            record_file_action(method, params, response)
        response = _annotate_dom_batch_response(method, response)
        page_list_receipt: JsonDict = {}
        if method == "Page.list":
            response, page_list_receipt = _filter_page_list_response(
                agent, response
            )
            shown_sidecar = page_list_receipt.pop("_shownInventoryPages", None)
            if isinstance(shown_sidecar, list):
                page_list_shown = [
                    dict(row) for row in shown_sidecar if isinstance(row, dict)
                ]
        axtree_snapshot = _precompute_axtree_snapshot(method, params, response)
        response = agent._offload_response(method, params, response, step)

        hitl_pause_succeeded = (
            method == "Hitl.requestPause" and _hitl_pause_succeeded(response)
        )
        if hitl_pause_succeeded:
            response = await _enrich_pause_with_wait(agent, params, response, step)

        result = {
            "method": method,
            "params": params,
            "response": response,
        }
        if structural_challenge:
            result["structuralChallenge"] = structural_challenge
        if runtime_receipt:
            result["runtimePolicy"] = runtime_receipt
            if runtime_receipt.get("resultMode") == "json":
                payload = _runtime_any_json_payload(result)
                if isinstance(payload, dict) and payload.get("error"):
                    result["runtimeJSONError"] = {
                        "error": str(payload.get("error")),
                        "stack": str(payload.get("stack") or "")[:1000],
                    }
                elif isinstance(payload, dict) and "value" in payload:
                    _attach_runtime_json_value(
                        agent,
                        result,
                        payload.get("value"),
                        runtime_receipt,
                        step=step,
                    )
                else:
                    result["runtimeJSONError"] = {
                        "error": "Runtime.evaluate did not return a JSON envelope"
                    }
        if page_list_receipt:
            result.update(page_list_receipt)
        if isinstance(response, dict) and response.get("error"):
            attach_method_schema(result, method, agent.method_schemas)
    except FleetClickGateTimeout as exc:
        result = {
            "method": method,
            "params": params,
            "error": str(exc),
            **exc.receipt,
        }
        attach_method_schema(result, method, agent.method_schemas)
    except ABCPTransportError as exc:
        result = {
            "method": method,
            "params": params,
            "error": str(exc),
            **_transport_error_metadata(method, exc),
        }
        attach_method_schema(result, method, agent.method_schemas)

    if runtime_receipt and "runtimePolicy" not in result:
        result["runtimePolicy"] = runtime_receipt

    result = await _quarantine_workflow_result_after_auth_change(
        agent,
        method,
        result,
        started_generation=workflow_auth_started_generation,
        emit_telemetry=True,
    )
    relinquished = await _relinquish_fleet_auth_resolver_after_failed_pause(
        agent,
        method,
        pause_succeeded=hitl_pause_succeeded,
    )
    if relinquished:
        result["fleetAuthBarrier"] = relinquished

    lost_fleet_result = _assigned_fleet_lost_result(
        agent, method, params, result
    )
    if lost_fleet_result is not None:
        result = lost_fleet_result
        page_create_should_stop = True
    elif _is_page_create_32005_failure(method, result):
        result, page_create_should_stop = await _recover_page_create_32005(
            agent,
            params,
            result,
        )
    page_create_relinquished = (
        await _relinquish_fleet_auth_resolver_after_failed_recovery_page_create(
            agent,
            method,
            takeover_claimed=page_create_takeover_claimed,
            call_succeeded=not _invoke_result_failed(result),
        )
    )
    if page_create_relinquished:
        result["fleetAuthBarrier"] = page_create_relinquished
    _observe_page_binding_after(agent, method, params, result)
    if fleet_binding_receipt and (
        method in {"Page.create", "Page.list"} or method.startswith("Fleet.")
    ):
        result.update(fleet_binding_receipt)
    attach_error_classification(result, method=method)
    result = _apply_select_failure_guidance(agent, method, params, result)
    if method == "Runtime.evaluate" and _invoke_result_failed(result):
        attempts = list(runtime_receipt.get("attempts") or [])
        attempted_main = any(
            item.get("requestedWorld") == "main"
            for item in attempts if isinstance(item, dict)
        )
        signaled = any(
            MAIN_WORLD_REQUIRED_PREFIX in str(item.get("error") or "")
            for item in attempts if isinstance(item, dict)
        )
        classification = (
            "runtime_execution_world_unverified"
            if attempts and attempts[-1].get("failureKind") == "world_evidence_mismatch"
            else "runtime_main_evaluation_failed" if attempted_main
            else "runtime_isolated_context_blocked"
            if signaled
            else "runtime_isolated_evaluation_failed"
        )
        result["status"] = "blocked"
        result["runtimeBlocker"] = {
            "classification": classification,
            "attempts": attempts,
            "error": _runtime_evaluation_error_text(result)[:2000],
            "final": True,
        }
        result["next_instruction"] = (
            "The guarded Runtime evaluation exhausted its authorized strict"
            " world attempts or received invalid/mismatched platform world"
            " evidence. Do not"
            " request main directly or repeat Runtime.evaluate; report this blocker."
        )
    _fleet_auth_barrier_after_call(agent, method, result)
    result = _attach_navigation_check(result, method=method, params=params)
    result = _attach_runtime_strategy_hints(result, method=method)
    if not page_create_should_stop:
        result = await _maybe_auto_hitl_for_challenge(agent, method, params, result, step)
    result = _attach_normalized_handles(result)
    result = _settle_page_inventory_signal(
        agent,
        method,
        params,
        result,
        page_list_shown=page_list_shown,
    )
    content_observation_params = params
    if navigation_context:
        content_observation_params = {
            **params,
            "_harnessNavigationContext": navigation_context,
        }
    result = _observe_content_completeness_after(
        agent,
        method,
        content_observation_params,
        result,
        step,
        content_binding=raw_content_binding,
    )
    if navigation_context:
        source_page_id = str(navigation_context.get("sourcePageId") or "")
        kind = str(navigation_context.get("kind") or "")
        tracker = getattr(agent, "content_completeness_tracker", None)
        source_state = (
            tracker.pages.get(source_page_id)
            if tracker is not None and hasattr(tracker, "pages")
            else None
        )
        # Each declaration kind is recorded differently, so `accepted` has to
        # ask the mechanism that actually consumed it. Probing Page.create's
        # pending map for every kind reported a successful claimed-page binding
        # as rejected, inviting the model to replay a declaration that had
        # already been consumed.
        if kind == "route_recovery_claimed_page":
            accepted = bool(
                tracker is not None
                and getattr(tracker, "last_declaration_accepted", False)
            )
        else:
            accepted = bool(
                tracker is not None
                and str(
                    getattr(
                        tracker,
                        "pending_explicit_recovery_sources",
                        {},
                    ).get(
                        str(
                            _response_data(result).get("pageId")
                            or _response_data(result).get("id")
                            or ""
                        ),
                        "",
                    )
                    or ""
                ) == source_page_id
            )
        result["navigationContext"] = {
            **navigation_context,
            "accepted": accepted,
            "sourceExemptionPendingTargetEvidence": True,
            "forwardedToABCP": False,
        }
        if not accepted:
            result["navigationContext"]["next_instruction"] = (
                "The declaration was not recorded. Do not replay it: check"
                " that the call succeeded and that sourcePageId is a listing"
                " click the harness reported as unresolved."
            )
    _observe_navigation_progress_after(agent, method, params, result)
    # Record THIS call's AXTree snapshot first (precomputed_snapshot is the
    # pre-auto-intercept tree). Auto-intercept runs AFTER: its dismiss_overlay
    # mutates the page and its own internal calls invalidate/refresh the snapshot,
    # so the last word on agent.axtree_* reflects the post-dismiss page. If
    # auto-intercept ran before this, the stale precomputed DOM.getAXTree snapshot
    # would be written back as clean even though the page just changed.
    _observe_axtree_state_after(
        agent,
        method,
        params,
        result,
        precomputed_snapshot=axtree_snapshot if "axtree_snapshot" in locals() else None,
        event_serial_before=event_serial_before if "event_serial_before" in locals() else None,
        page_before=page_before if "page_before" in locals() else None,
    )
    # Phase 7.2: optionally auto-run the dismiss_overlay micro-loop (gated by
    # config auto_intercept). dismiss_overlay uses _invoke_browser_method (not
    # this model path), so there is no recursion; its internal clicks/re-inspects
    # leave agent.axtree_* either invalidated or refreshed to the post-dismiss
    # tree — never a stale snapshot marked clean.
    if not page_create_should_stop:
        result = await _maybe_auto_intercept_overlay(agent, method, params, result, step)
    # VL Role D: if the call still failed with a visual/occlusion/challenge/locator
    # error after deterministic recovery, auto-route it to the VL arbiter and attach
    # a recovery recommendation (resolvedId / hitl / dismiss / reperceive). Gated by
    # vl.arbiter_enabled; non-visual failures and disabled VL are no-ops.
    if not page_create_should_stop:
        result = await _maybe_vl_arbitrate(agent, method, params, result, step)
    agent.logger.write("browser.call.result", agent._trim_for_log(result))
    model_result = agent._clean_for_model(result)
    model_result = offload_large_tool_result(
        logger=agent.logger,
        tool_name=method or str(tool_name or "browser_call"),
        result=model_result,
        step=step,
        prefix=agent.runtime.agent_id,
        threshold_bytes=agent.runtime.harness.tool_result_offload_threshold_bytes,
    )
    _observe_progress_after(agent, method, model_result)
    agent.trace.append({
        "type": "browser_call",
        "method": method,
        "params": params,
        "result": agent._clean_for_model(model_result),
    })
    return model_result, page_create_should_stop


_TRUSTED_COLLECTION_RUNTIME_TOKEN = object()


async def _invoke_browser_method(
    agent: Any,
    method: str,
    params: JsonDict,
    step: int,
    *,
    count_progress: bool = True,
    read_only_eval: bool = False,
    allow_rematch: bool = False,
    internal: bool = False,
    redact_params: Optional[Set[str]] = None,
    runtime_policy: Optional[JsonDict] = None,
    lifecycle_cleanup_bypass: bool = False,
    _trusted_collection_runtime_token: Any = None,
) -> JsonDict:
    # internal=True marks a harness plumbing call: it must not enter the observation chain —
    # no challenge adjudication, diagnostics, progress, or model-facing trace —
    # only a compact audit log. Such calls also never count as progress.
    if internal:
        count_progress = False
    params, screenshot_output_receipt = _normalize_screenshot_output(method, params)
    if screenshot_output_receipt is not None:
        logger = getattr(agent, "logger", None)
        if logger is not None:
            logger.write(
                "browser.call.screenshot_output_normalized",
                {"method": method, **screenshot_output_receipt},
            )
    runtime_receipt: JsonDict = {}
    if method == "Runtime.evaluate":
        trusted_collection_call = (
            _trusted_collection_runtime_token is _TRUSTED_COLLECTION_RUNTIME_TOKEN
            and internal
            and read_only_eval
        )
        if not trusted_collection_call:
            error = RuntimeEvaluationService._error(
                "runtime_internal_path_forbidden",
                "Harness-internal Runtime.evaluate paths are disabled; only the model-facing browser_call boundary or the registered collect_items templates may authorize execution.",
            )
            logger = getattr(agent, "logger", None)
            if logger is not None:
                logger.write("runtime.evaluate.rejected", error)
            return error
    # redact_params: the browser still receives the real values, but these keys
    # are masked everywhere the call surfaces (result/log/trace/model_result,
    # and the render-recovery logs/advisory), so secrets (e.g. Input.type text
    # for a password) never hit logs or trace.
    def _shown_params(p: JsonDict) -> JsonDict:
        return mask_params(p, redact_params)
    # Composite tools opt in with allow_rematch=True so previously-seen stale
    # ids pass through to the browser-side rematch while never-seen ids and
    # page mismatches are still blocked. Default (False) preserves the legacy
    # behavior of internal calls: no stale guard at this layer. The model
    # path keeps its own guard in _execute_browser_capability_tool.
    if allow_rematch:
        stale_target = _check_stale_axtree_target(
            agent, method, params, allow_rematch=True
        )
        if stale_target is not None:
            logger = getattr(agent, "logger", None)
            if logger is not None:
                logger.write("browser.call.stale_axtree_target", stale_target)
            agent.trace.append({"type": "stale_axtree_target", "result": stale_target})
            return stale_target
    auth_barrier_guard = await _fleet_auth_barrier_before_call(
        agent, method, params
    )
    if auth_barrier_guard is not None:
        return auth_barrier_guard
    workflow_auth_started_generation = _workflow_auth_started_generation(
        agent, method
    )
    if lifecycle_cleanup_bypass and not internal:
        return {
            "status": "rejected",
            "policy_violation": "lifecycle_cleanup_bypass_requires_internal",
            "tool_was_executed": False,
        }
    if not lifecycle_cleanup_bypass:
        lifecycle_guard = await _page_lifecycle_guard_before(agent, method, params)
        if lifecycle_guard is not None:
            return lifecycle_guard
    _ensure_hitl_request_reason(method, params, str(params.get("purpose") or ""))
    page_create_claim_guard, page_create_takeover_claimed = (
        await _claim_ownerless_fleet_auth_barrier_for_page_create(
            agent, method, params
        )
    )
    if page_create_claim_guard is not None:
        return page_create_claim_guard
    hitl_claim_guard = await _claim_fleet_auth_barrier_for_hitl(
        agent, method, params
    )
    if hitl_claim_guard is not None:
        return hitl_claim_guard
    hitl_pause_succeeded = False
    try:
        runner = getattr(agent, "render_recovery_runner", None)
        if runner is None:
            runner = build_render_recovery_runner(
                browser=agent.browser,
                logger=agent.logger,
                capability_methods=agent.capability_methods,
                recent_recoveries=agent._render_recovery_recent,
            )
            agent.render_recovery_runner = runner
        # Only forward redact_params when set, so runners that predate the kwarg
        # (test fakes) keep working for the common non-redacted path.
        runner_kwargs = {"redact_params": redact_params} if redact_params else {}
        # Sample the event serial + held page before the call so post-action
        # invalidation can detect a same-page DOM.axTreeUpdated that landed
        # mid-call (race fix) without letting a cross-page event suppress it.
        event_serial_before = int(getattr(agent, "axtree_event_serial", 0) or 0)
        page_before = str(getattr(agent, "axtree_page_id", "") or "")
        # No page-open intent is armed here on purpose. This is the internal
        # dispatch used by composites and harness machinery (focus clicks,
        # overlay dismissal, load-more), and it has no settlement tail — an
        # intent armed here would outlive its action and let an unrelated site
        # popup claim it. Adoption is a model-facing grant, so only the
        # model's own dispatch path arms one.
        if method == "Hitl.requestPause":
            await _capture_hitl_pause_snapshot(
                agent,
                runner,
                str(params.get("pageId") or ""),
                step,
            )
        _page_lifecycle_before_action(agent, method, params)
        response, _recovery = await runner.call(method, params, **runner_kwargs)
        _page_lifecycle_after_action(agent, method, params, response)
        response = agent._capture_artifacts(method, response)
        structural_challenge = detect_structural_challenge(method, response)
        record_file_action = getattr(agent, "_capture_file_action", None)
        if callable(record_file_action):
            record_file_action(method, params, response)
        axtree_snapshot = _precompute_axtree_snapshot(method, params, response)
        response = agent._offload_response(method, params, response, step)
        hitl_pause_succeeded = (
            method == "Hitl.requestPause" and _hitl_pause_succeeded(response)
        )
        if hitl_pause_succeeded:
            response = await _enrich_pause_with_wait(agent, params, response, step)
        result = {
            "method": method,
            "params": _shown_params(params),
            "response": response,
        }
        if structural_challenge:
            result["structuralChallenge"] = structural_challenge
        if runtime_receipt:
            result["runtimePolicy"] = runtime_receipt
        if isinstance(response, dict) and response.get("error"):
            attach_method_schema(result, method, agent.method_schemas)
    except FleetClickGateTimeout as exc:
        result = {
            "method": method,
            "params": _shown_params(params),
            "error": str(exc),
            **exc.receipt,
        }
        attach_method_schema(result, method, agent.method_schemas)
    except ABCPTransportError as exc:
        result = {
            "method": method,
            "params": _shown_params(params),
            "error": str(exc),
            **_transport_error_metadata(method, exc),
        }
        attach_method_schema(result, method, agent.method_schemas)

    result = await _quarantine_workflow_result_after_auth_change(
        agent,
        method,
        result,
        started_generation=workflow_auth_started_generation,
        emit_telemetry=False,
    )
    relinquished = await _relinquish_fleet_auth_resolver_after_failed_pause(
        agent,
        method,
        pause_succeeded=hitl_pause_succeeded,
    )
    if relinquished:
        result["fleetAuthBarrier"] = relinquished

    if _is_page_create_32005_failure(method, result):
        result, _page_create_should_stop = await _recover_page_create_32005(
            agent,
            params,
            result,
        )
        if "params" in result:
            result["params"] = _shown_params(params)
    page_create_relinquished = (
        await _relinquish_fleet_auth_resolver_after_failed_recovery_page_create(
            agent,
            method,
            takeover_claimed=page_create_takeover_claimed,
            call_succeeded=not _invoke_result_failed(result),
        )
    )
    if page_create_relinquished:
        result["fleetAuthBarrier"] = page_create_relinquished
    attach_error_classification(result, method=method)
    result = _apply_select_failure_guidance(agent, method, params, result)
    _fleet_auth_barrier_after_call(agent, method, result)
    result = _attach_navigation_check(result, method=method, params=params)
    result = _attach_runtime_strategy_hints(result, method=method)
    if not internal:
        result = await _maybe_auto_hitl_for_challenge(agent, method, params, result, step)
    result = _attach_normalized_handles(result)
    result = _settle_page_inventory_signal(agent, method, params, result)
    _observe_axtree_state_after(
        agent,
        method,
        params,
        result,
        precomputed_snapshot=axtree_snapshot if "axtree_snapshot" in locals() else None,
        read_only_eval=read_only_eval,
        event_serial_before=event_serial_before if "event_serial_before" in locals() else None,
        page_before=page_before if "page_before" in locals() else None,
    )
    if internal:
        agent.logger.write("browser.call.internal", {"method": method})
        if (
            method == "Runtime.evaluate"
            and _trusted_collection_runtime_token is _TRUSTED_COLLECTION_RUNTIME_TOKEN
        ):
            # The fixed collection result can legitimately contain hundreds of
            # rows.  Model-facing cleanup truncates long strings, which would
            # corrupt the JSON envelope before the in-process collector parses
            # it.  This raw return never reaches the model or trace; the trusted
            # helper immediately decodes it and only collect_items' bounded
            # digest is exposed.
            return result
        return agent._clean_for_model(result)
    agent.diagnostics.observe_browser_call(method, params, result)
    agent.logger.write("browser.call.result", agent._trim_for_log(result))
    model_result = agent._clean_for_model(result)
    if count_progress:
        _observe_progress_after(agent, method, model_result)
    agent.trace.append({
        "type": "browser_call",
        "step": step,
        "method": method,
        "params": _shown_params(params),
        "result": agent._clean_for_model(model_result),
    })
    return model_result


def _find_in_axtree(agent: Any, tool_input: JsonDict) -> JsonDict:
    page_id = str(tool_input.get("pageId") or "").strip()
    current_page_id = str(getattr(agent, "axtree_page_id", "") or "")
    if page_id and current_page_id and page_id != current_page_id:
        return {
            "status": "needs_fresh_axtree",
            "reason": "axtree_page_mismatch",
            "pageId": page_id,
            "currentAXTreePageId": current_page_id,
            "next_instruction": "Call DOM.getAXTree for this page, then retry find_in_axtree.",
        }
    if bool(getattr(agent, "axtree_invalidated", True)):
        return {
            "status": "needs_fresh_axtree",
            "reason": "axtree_snapshot_invalidated",
            "pageId": page_id or current_page_id or None,
            "next_instruction": "Call DOM.getAXTree to refresh the AXTree before searching it.",
        }

    nodes = list(getattr(agent, "axtree_nodes", []) or [])
    if not nodes:
        lines = list(getattr(agent, "axtree_lines", []) or [])
        nodes = _axtree_nodes_from_lines(lines)
    if not nodes:
        return {
            "status": "needs_fresh_axtree",
            "reason": "no_current_axtree_nodes",
            "pageId": page_id or current_page_id or None,
            "next_instruction": "Call DOM.getAXTree first; find_in_axtree searches the current AXTree snapshot.",
        }

    role = str(tool_input.get("role") or "").strip().lower()
    query = str(
        tool_input.get("name")
        if tool_input.get("name") is not None
        else tool_input.get("text") or ""
    ).strip()
    match_mode = str(tool_input.get("match") or "contains").strip().lower()
    if match_mode not in {"exact", "contains", "regex"}:
        match_mode = "contains"
    case_sensitive = bool(tool_input.get("case_sensitive", False))
    interactive_only = bool(tool_input.get("interactive_only", False))
    max_results = max(1, min(optional_int(tool_input.get("max_results"), 10) or 10, 50))

    if match_mode == "regex":
        flags = 0 if case_sensitive else re.I
        try:
            query_re = re.compile(query, flags)
        except re.error as exc:
            return {"status": "failed", "error": f"invalid name/text regex: {exc}"}
    else:
        query_re = None

    def text_matches(value: str) -> bool:
        if not query:
            return True
        candidate = value if case_sensitive else value.lower()
        needle = query if case_sensitive else query.lower()
        if match_mode == "exact":
            return candidate == needle
        if match_mode == "regex" and query_re is not None:
            return bool(query_re.search(value))
        return needle in candidate

    lines = list(getattr(agent, "axtree_lines", []) or [])
    current_ids = set(getattr(agent, "axtree_ids", set()) or set())
    matches: List[JsonDict] = []
    for node in nodes:
        if role and str(node.get("role") or "").lower() != role:
            continue
        if interactive_only and not bool(node.get("interactive")):
            continue
        name = str(node.get("name") or "")
        raw_line = str(node.get("line") or "")
        if not text_matches(name or raw_line):
            continue
        node_id = str(node.get("id") or "")
        if current_ids and node_id not in current_ids:
            continue
        line_number = optional_int(node.get("lineNumber"), 0) or 0
        context = ""
        if lines and line_number > 0:
            start = max(0, line_number - 2)
            end = min(len(lines), line_number + 1)
            context = "\n".join(lines[start:end])
        entry: JsonDict = {
            "id": node_id,
            "role": node.get("role") or "",
            "name": name,
            "interactive": bool(node.get("interactive")),
            "lineNumber": line_number or None,
            "line": raw_line,
            "context": context,
        }
        node_flags = node.get("flags") or []
        if node_flags:
            entry["flags"] = list(node_flags)
        node_rect = node.get("rect")
        if isinstance(node_rect, dict):
            entry["rect"] = node_rect
        matches.append(entry)
        if len(matches) >= max_results:
            break

    return {
        "status": "done",
        "pageId": page_id or current_page_id or None,
        "currentAXTreePageId": current_page_id or None,
        "axtreeEpoch": int(getattr(agent, "axtree_epoch", 0) or 0),
        "count": len(matches),
        "matches": matches,
        "next_instruction": (
            "Use a returned full id with DOM.getText/DOM.getAttribute/Input.*."
            if matches
            else "No matching node exists in the current AXTree snapshot; refresh or change query."
        ),
    }


_URL_DEFAULT_PORTS = {"http": 80, "https": 443, "ws": 80, "wss": 443}

# One Page.navigate reaches the site as one real request. Bounding the redirect
# read loop keeps verification cheap without ever re-issuing that request.
NAVIGATE_VERIFIED_DEFAULT_STATE_CHECKS = 5
NAVIGATE_VERIFIED_MAX_STATE_CHECKS = 10
NAVIGATE_VERIFIED_STATE_RECHECK_SECONDS = 0.5
_NAVIGATION_IN_FLIGHT_STATUSES = {"loading", "navigating", "pending"}
_NAVIGATION_FAILED_STATUSES = {"failed", "loadfailed", "load_failed", "crashed"}


def _normalize_url_for_equivalence(raw: str) -> str:
    """Canonicalize only the URL differences no server can distinguish.

    Scheme/host case and an explicit default port are erased, and an empty path
    becomes "/" so `https://x.com` and `https://x.com/` compare equal. Path,
    query (including its order) and fragment stay byte-exact: a redirect that
    rewrites the path or appends tracking parameters must still read as a
    mismatch, because it means the caller did not land where it asked to.
    """
    text = (raw or "").strip()
    if not text:
        return ""
    try:
        parsed = urlparse(text)
    except ValueError:
        return text
    if not parsed.scheme or not parsed.netloc:
        return text
    try:
        host = (parsed.hostname or "").lower()
        port = parsed.port
    except ValueError:
        # A malformed port makes the authority unparseable; comparing the raw
        # text is wrong-but-honest, whereas guessing an authority is not.
        return text
    if not host:
        return text
    scheme = parsed.scheme.lower()
    userinfo = ""
    if parsed.username:
        userinfo = parsed.username
        if parsed.password:
            userinfo = f"{userinfo}:{parsed.password}"
        userinfo = f"{userinfo}@"
    netloc = f"{userinfo}{host}"
    if port is not None and port != _URL_DEFAULT_PORTS.get(scheme):
        netloc = f"{netloc}:{port}"
    rebuilt = f"{scheme}://{netloc}{parsed.path or '/'}"
    if parsed.query:
        rebuilt = f"{rebuilt}?{parsed.query}"
    if parsed.fragment:
        rebuilt = f"{rebuilt}#{parsed.fragment}"
    return rebuilt


def _make_url_matcher(
    url_re: Any,
    target_url: str,
) -> Callable[[str], bool]:
    """Return the URL acceptance test for one navigate_verified call.

    A caller-supplied regex is used verbatim. Without one the harness compares
    normalized URLs instead of synthesizing a regex: an unanchored `re.escape`
    pattern would accept `https://phish.example/?next=<target>`, and an anchored
    one rejects a bare trailing-slash difference the browser always adds.
    """
    if url_re is not None:
        return lambda actual: bool(url_re.search(actual or ""))
    expected = _normalize_url_for_equivalence(target_url)
    return lambda actual: _normalize_url_for_equivalence(actual) == expected


def _possible_double_escape(pattern: str, actual_url: str) -> Optional[JsonDict]:
    """Flag a caller pattern that fails ONLY because it looks over-escaped.

    All three conditions must hold together, so a legitimately escaped pattern
    that simply does not describe this page is never flagged: the original does
    not match, dropping one escaping layer still compiles, and the de-escaped
    form does match. The pattern is reported, never rewritten or applied — a
    syntactically valid regex belongs to its caller.
    """
    if not pattern or "\\\\" not in pattern or not actual_url:
        return None
    try:
        if re.compile(pattern).search(actual_url):
            return None
    except re.error:
        return None
    candidate = pattern.replace("\\\\", "\\")
    if candidate == pattern:
        return None
    try:
        candidate_re = re.compile(candidate)
    except re.error:
        return None
    if not candidate_re.search(actual_url):
        return None
    return {
        "code": "possible_double_escape",
        "expectedUrlPattern": pattern[:200],
        "deEscapedCandidate": candidate[:200],
        "detail": (
            "expectedUrlPattern fails only because it appears to carry an extra"
            " escaping layer. The harness did not rewrite or apply the"
            " candidate. This is a note about how to write the pattern next"
            " time — it is NOT a reason to re-navigate to this URL, which has"
            " already loaded."
        ),
    }


async def _navigate_verified(agent: Any, tool_input: JsonDict, step: int) -> JsonDict:
    """Navigate once and report what was actually observed.

    The audit fields are merged onto whatever terminal receipt the
    implementation returns, so `navigateDispatchCount` is present and truthful
    on EVERY branch — including the early input rejections, HITL handoffs, and
    AX-refresh failures that each build their own dict.
    """
    audit: JsonDict = {"navigateDispatchCount": 0}
    result = await _navigate_verified_impl(agent, tool_input, step, audit)
    if isinstance(result, dict):
        result.update(audit)
    return result


async def _navigate_verified_impl(
    agent: Any,
    tool_input: JsonDict,
    step: int,
    audit: JsonDict,
) -> JsonDict:
    page_id = str(tool_input.get("pageId") or "").strip()
    url = str(tool_input.get("url") or "").strip()
    expected_url_pattern = str(tool_input.get("expectedUrlPattern") or "").strip()
    expected_title_pattern = str(tool_input.get("expectedTitlePattern") or "").strip()
    timeout_seconds = max(1.0, min(float(tool_input.get("timeoutSeconds") or 20.0), 120.0))
    # `maxRetries` used to multiply Page.navigate dispatches, so a caller
    # expectation that could never match spent N real requests on a page that
    # had already arrived. It now only bounds read-side redirect settlement,
    # under its new name; the legacy key keeps working and says so in the receipt.
    legacy_retries = optional_int(tool_input.get("maxRetries"), None)
    max_state_checks = optional_int(
        tool_input.get("maxStateChecks"),
        legacy_retries if legacy_retries is not None else NAVIGATE_VERIFIED_DEFAULT_STATE_CHECKS,
    )
    max_state_checks = max(
        1,
        min(
            max_state_checks or NAVIGATE_VERIFIED_DEFAULT_STATE_CHECKS,
            NAVIGATE_VERIFIED_MAX_STATE_CHECKS,
        ),
    )
    if not page_id:
        return {"status": "failed", "error": "pageId is required"}
    if not url:
        return {"status": "failed", "error": "url is required"}

    # Compile before dispatching. An expectation that cannot compile can never
    # be satisfied, so navigating first would spend a real request on a call
    # that is already doomed.
    url_re = None
    if expected_url_pattern:
        try:
            url_re = re.compile(expected_url_pattern)
        except re.error as exc:
            return _navigate_pattern_invalid_result(
                page_id=page_id,
                field="expectedUrlPattern",
                pattern=expected_url_pattern,
                error=str(exc),
            )
    title_re = None
    if expected_title_pattern:
        try:
            title_re = re.compile(expected_title_pattern)
        except re.error as exc:
            return _navigate_pattern_invalid_result(
                page_id=page_id,
                field="expectedTitlePattern",
                pattern=expected_title_pattern,
                error=str(exc),
            )

    url_matches = _make_url_matcher(url_re, url)
    expectation_mode = "caller_regex" if url_re is not None else "normalized_url_equality"
    attempts: List[JsonDict] = []
    state_resync_count = 0
    last_challenge_summary: JsonDict = {}
    audit["urlExpectationMode"] = expectation_mode
    audit["maxStateChecks"] = max_state_checks
    if legacy_retries is not None and tool_input.get("maxStateChecks") is None:
        audit["maxRetriesInterpretedAs"] = "state_checks"

    # Exactly one Page.navigate per call, unconditionally. A failed expectation
    # is not a failed navigation, and this composite must never hide a second
    # request from the model that authorized one.
    attempt = 1
    deadline = time.monotonic() + timeout_seconds
    nav = await _invoke_browser_method(
        agent,
        "Page.navigate",
        {
            "pageId": page_id,
            "url": url,
            "purpose": "Navigate and verify URL",
        },
        step,
        count_progress=False,
    )
    # The count is what actually reached transport, not what this composite
    # intended. A pre-dispatch guard answers `tool_was_executed=False` without
    # the panel ever seeing the call, and reporting 1 there would contradict
    # the `navigation_not_dispatched` status sitting beside it.
    if nav.get("tool_was_executed") is not False:
        audit["navigateDispatchCount"] = 1
    if _result_has_auto_hitl(nav):
        return _navigate_hitl_result(page_id, attempt, nav)
    if _invoke_result_failed(nav):
        return await _navigate_dispatch_failure_result(
            agent,
            page_id=page_id,
            url=url,
            nav=nav,
            step=step,
        )
    last_challenge_summary = _page_challenge_summary(agent, page_id)
    tracker = getattr(agent, "page_lifecycle", None)
    settlement = "unknown"
    if isinstance(tracker, PageLifecycleTracker):
        settlement = await tracker.wait_for_settlement(
            page_id,
            max(0.0, deadline - time.monotonic()),
        )
    redirect_settlements = 0
    state_checks_used = 0
    state_read_failed = False
    last_state: JsonDict = {}
    while True:
        # ONE budget for every Page.getState this settlement loop issues,
        # whichever path asked for it. Two separate counters let a redirect
        # keep granting reads that the recheck budget had already refused.
        if state_checks_used >= max_state_checks:
            break
        # Register the fresh settlement waiter before Page.getState so a
        # redirect that starts/finishes during the RPC cannot fall through
        # the gap. This is event-driven redirect tolerance, not polling.
        remaining = max(0.0, deadline - time.monotonic())
        redirect_waiter = None
        if state_checks_used + 1 < max_state_checks and remaining > 0:
            redirect_waiter = _fresh_page_settlement_task(
                agent, page_id, remaining
            )
        state_result = await _invoke_browser_method(
            agent,
            "Page.getState",
            {
                "pageId": page_id,
                "purpose": "Synchronize state once after navigation settlement",
            },
            step,
            count_progress=False,
        )
        state_resync_count += 1
        state_checks_used += 1
        if _result_has_auto_hitl(state_result):
            await _cancel_waiter(redirect_waiter)
            return _navigate_hitl_result(page_id, attempt, state_result)
        # A failed read yields an empty snapshot, which looks exactly like "the
        # page is at about:blank with no title". Remember that the state is
        # unknown so the terminal branch cannot report it as an arrival.
        state_outcome = classify_call_outcome(state_result)
        state_read_failed = not (
            state_outcome.succeeded
            and page_state_evidence_ok(page_id, state_result)
        )
        last_state = _navigation_state_snapshot(
            _response_data(state_result),
            url_matches=url_matches,
            title_re=title_re,
            settlement=settlement,
            redirect_settlements=redirect_settlements,
        )
        current_url = str(last_state.get("url") or "")
        title = str(last_state.get("title") or "")
        status = str(last_state.get("status") or "")
        title_is_lingering = bool(last_state.get("titleLingering"))
        url_ok = bool(last_state.get("urlOk"))
        title_ok = bool(last_state.get("titleOk"))
        last_challenge_summary = _page_challenge_summary(agent, page_id)
        # A matching URL/title is not arrival on a tab that is still fetching
        # or that reported a failed load. The harness's own doctrine forbids
        # DOM probes before settlement, so `done` in either state would
        # contradict the instruction the model is given.
        if (
            url_ok
            and title_ok
            and not title_is_lingering
            and not state_read_failed
            and status not in _NAVIGATION_IN_FLIGHT_STATUSES
            and status not in _NAVIGATION_FAILED_STATUSES
        ):
            await _cancel_waiter(redirect_waiter)
            # Page.navigate invalidates DOM identity. Refresh the AXTree before
            # returning so callers cannot inherit a clean-looking stale cache.
            # AX refresh failure is not navigation failure: retry only the
            # perception leg, never Page.navigate, after URL/title are proven.
            tree_result, tree_attempts, ax_state_resyncs, ax_latest_state = (
                await _refresh_axtree_after_verified_navigation(
                    agent,
                    page_id=page_id,
                    step=step,
                    deadline=deadline,
                    url_matches=url_matches,
                    title_re=title_re,
                )
            )
            state_resync_count += ax_state_resyncs
            if _result_has_auto_hitl(tree_result):
                return _navigate_hitl_result(page_id, attempt, tree_result)
            if isinstance(ax_latest_state, dict):
                if tree_result.get("status") == "navigation_redirected_during_ax_refresh":
                    return {
                        "status": "navigation_redirected_during_ax_refresh",
                        "error": (
                            "page URL/title changed after navigation was"
                            " verified and before AX refresh completed"
                        ),
                        "pageId": page_id,
                        "url": ax_latest_state.get("url"),
                        "title": ax_latest_state.get("title"),
                        "pageStatus": ax_latest_state.get("status"),
                        "attempt": attempt,
                        "navigationVerified": False,
                        "previousVerifiedState": last_state,
                        "currentState": ax_latest_state,
                        "stateResyncCount": state_resync_count,
                        "redirectSettlementCount": redirect_settlements,
                        "axtreeRefreshed": False,
                        "axtreeRefreshAttempts": len(tree_attempts),
                        "axtreeRefreshResults": tree_attempts,
                        "suspectedChallenge": (
                            _page_challenge_summary(agent, page_id) or None
                        ),
                        "next_instruction": (
                            "Do not report the earlier navigation as verified"
                            " and do not guess the new page's meaning. Inspect"
                            " the reported current URL/title and recover or"
                            " re-verify from the current page state."
                        ),
                    }
                last_state = ax_latest_state
                current_url = str(last_state.get("url") or "")
                title = str(last_state.get("title") or "")
                status = str(last_state.get("status") or "")
            if tree_result.get("status") == "navigation_state_resync_failed_during_ax":
                return {
                    "status": "navigation_verified_state_resync_failed",
                    "error": (
                        "navigation URL/title were verified, but page state"
                        " resynchronization failed during AX refresh"
                    ),
                    "pageId": page_id,
                    "url": current_url,
                    "title": title,
                    "pageStatus": status,
                    "attempt": attempt,
                    "navigationVerified": True,
                    "state": last_state,
                    "stateResyncCount": state_resync_count,
                    "axtreeRefreshed": bool(
                        tree_result.get("axtreeRefreshed")
                    ),
                    "axtreeRefreshAttempts": len(tree_attempts),
                    "axtreeRefreshResults": tree_attempts,
                    "next_instruction": (
                        "Do NOT call navigate_verified again for this"
                        " navigation. Complete the required Page.getState"
                        " resynchronization on this page before issuing"
                        " dependent page actions."
                    ),
                }
            if _invoke_result_failed(tree_result):
                attempt_receipt = {
                    "attempt": attempt,
                    "lastState": last_state,
                    "axtreeRefreshAttempts": len(tree_attempts),
                    "axtreeRefreshResults": tree_attempts,
                }
                attempts.append(attempt_receipt)
                last_challenge_summary = _page_challenge_summary(agent, page_id)
                if _challenge_score(last_challenge_summary) >= 80:
                    return _navigate_challenge_blocked_result(
                        page_id=page_id,
                        attempt=attempt,
                        last_state=last_state,
                        attempts=attempts,
                        state_resync_count=state_resync_count,
                        challenge_summary=last_challenge_summary,
                        expected_url_pattern=expected_url_pattern,
                        expected_title_pattern=expected_title_pattern,
                        trigger="verified_navigation_ax_refresh_failed_with_challenge",
                    )
                return {
                    "status": "navigation_verified_ax_refresh_failed",
                    "error": (
                        "navigation URL/title were verified, but the fresh"
                        " AXTree could not be obtained"
                    ),
                    "pageId": page_id,
                    "url": current_url,
                    "title": title,
                    "pageStatus": status,
                    "attempt": attempt,
                    "navigationVerified": True,
                    "navigateResult": _strip_challenge_fields(nav),
                    "state": last_state,
                    "stateResyncCount": state_resync_count,
                    "redirectSettlementCount": redirect_settlements,
                    "axtreeRefreshed": False,
                    "axtreeRefreshAttempts": len(tree_attempts),
                    "axtreeRefreshResults": tree_attempts,
                    "next_instruction": (
                        "Do NOT call navigate_verified again: the target URL"
                        " and title are already verified. Recover the current"
                        " renderer/page if needed, then retry DOM.getAXTree on"
                        " this pageId."
                    ),
                }
            _clear_navigation_challenge_state(agent, page_id)
            return {
                "status": "done",
                "pageId": page_id,
                "url": current_url,
                "title": title,
                "pageStatus": status,
                "attempt": attempt,
                "navigationCommitted": True,
                "navigateResult": _strip_challenge_fields(nav),
                "state": last_state,
                "stateResyncCount": state_resync_count,
                "redirectSettlementCount": redirect_settlements,
                "axtreeRefreshed": True,
                "axtreeRefreshAttempts": len(tree_attempts),
                "axtreeRefreshResults": tree_attempts,
            }
        settlement_event = (
            await redirect_waiter if redirect_waiter is not None else None
        )
        if settlement_event is not None:
            redirect_settlements += 1
            settlement = str(settlement_event.get("event") or "redirect_settled")
            continue
        # No settlement event arrived, but a page that is still loading or still
        # showing an interstitial title has not finished arriving. Re-read its
        # state instead of declaring a mismatch: Page.getState never touches the
        # site, unlike the Page.navigate replay this loop used to fall back on.
        if (
            state_checks_used < max_state_checks
            and time.monotonic() < deadline
            and (
                state_read_failed
                or title_is_lingering
                or status in _NAVIGATION_IN_FLIGHT_STATUSES
            )
        ):
            settlement = "state_recheck"
            await asyncio.sleep(NAVIGATE_VERIFIED_STATE_RECHECK_SECONDS)
            continue
        break
    attempts.append({"attempt": attempt, "lastState": last_state})

    if _challenge_score(last_challenge_summary) >= 80:
        return _navigate_challenge_blocked_result(
            page_id=page_id,
            attempt=attempt,
            last_state=attempts[-1].get("lastState", {}) if attempts else {},
            attempts=attempts,
            state_resync_count=state_resync_count,
            challenge_summary=last_challenge_summary,
            expected_url_pattern=expected_url_pattern,
            expected_title_pattern=expected_title_pattern,
            trigger="navigation_verification_exhausted_with_challenge",
        )

    # Verification did not pass. "The page arrived but your pattern was wrong"
    # is only ONE of the reasons that can happen, and it is the only one that
    # licenses the model to keep working from this page. Claiming it when the
    # state was unreadable, still loading, or reported a load failure would put
    # a fact in the receipt that the harness never observed.
    actual_url = str(last_state.get("url") or "")
    actual_title = str(last_state.get("title") or "")
    page_status = str(last_state.get("status") or "")
    lifecycle_state = (
        tracker.state(page_id)
        if isinstance(tracker, PageLifecycleTracker)
        else None
    )
    lifecycle_status = (
        str(getattr(lifecycle_state, "status", "") or "")
        if lifecycle_state is not None
        else ""
    )
    common: JsonDict = {
        "tool_was_executed": True,
        "pageId": page_id,
        "requestedUrl": url,
        "actualUrl": actual_url,
        "actualTitle": actual_title,
        "pageStatus": page_status,
        "lifecycleStatus": lifecycle_status or None,
        "expectedUrlPattern": expected_url_pattern or None,
        "expectedTitlePattern": expected_title_pattern or None,
        "attempts": attempts,
        "stateResyncCount": state_resync_count,
        "suspectedChallenge": last_challenge_summary or None,
    }

    if state_read_failed:
        return {
            **common,
            "status": "navigation_outcome_unknown",
            "navigationCommitted": None,
            "reason": "state_unreadable",
            "error": "Page.getState did not return a readable state",
            "next_instruction": (
                "The navigation was dispatched but the page state could not be"
                " read, so where the page landed is unknown. Do NOT call"
                " navigate_verified again for this navigation, and do not treat"
                " actualUrl as observed: recover the page or re-read its state"
                " with Page.getState first."
            ),
        }

    if lifecycle_status in {"failed", "crashed"} or page_status in _NAVIGATION_FAILED_STATUSES:
        return {
            **common,
            "status": "navigation_load_failed",
            "navigationCommitted": False,
            "error": f"page reported a failed load (status={page_status or lifecycle_status})",
            "next_instruction": (
                "The browser received the navigation and the page failed to"
                " load. Inspect the failure before deciding whether a retry is"
                " warranted; this composite will not re-dispatch it for you."
            ),
        }

    if bool(last_state.get("titleLingering")) or page_status in _NAVIGATION_IN_FLIGHT_STATUSES:
        return {
            **common,
            "status": "navigation_settlement_incomplete",
            "navigationCommitted": True,
            "titleLingering": bool(last_state.get("titleLingering")),
            "next_instruction": (
                "The navigation committed but the page had not finished"
                " settling when the read budget ran out. Do NOT call"
                " navigate_verified again for this navigation — that would"
                " re-request the URL. Call Page.getState once to see whether it"
                " settled, and do not treat actualTitle as final until it has."
            ),
        }

    result: JsonDict = {
        **common,
        "status": "navigation_arrived_expectation_mismatch",
        "navigationCommitted": True,
        "urlOk": bool(last_state.get("urlOk")),
        "titleOk": bool(last_state.get("titleOk")),
        "titleLingering": False,
        "next_instruction": (
            "The browser reached actualUrl/actualTitle; only the expectation"
            " failed. Do NOT call navigate_verified again for this navigation:"
            " the page is already here, so continue read-only with"
            " Page.getState/DOM.getAXTree. Apply any corrected expectation only"
            " to a future, genuinely different navigation."
        ),
    }
    suspect = _possible_double_escape(expected_url_pattern, actual_url)
    if suspect:
        result["expectationPatternSuspect"] = suspect
    return result


NAVIGATE_VERIFIED_AX_REFRESH_MAX_ATTEMPTS = 3


def _navigation_state_snapshot(
    data: Any,
    *,
    url_matches: Callable[[str], bool],
    title_re: Any,
    settlement: str,
    redirect_settlements: int,
) -> JsonDict:
    data = data if isinstance(data, dict) else {}
    current_url = str(data.get("url") or "")
    title = str(data.get("title") or "")
    return {
        "url": current_url,
        "title": title,
        "status": str(data.get("status") or ""),
        "urlOk": bool(url_matches(current_url)),
        "titleOk": True if title_re is None else bool(title_re.search(title)),
        "titleLingering": is_lingering_loading_title(title),
        "settlement": settlement,
        "redirectSettlements": redirect_settlements,
    }


async def _refresh_axtree_after_verified_navigation(
    agent: Any,
    *,
    page_id: str,
    step: int,
    deadline: float,
    url_matches: Callable[[str], bool],
    title_re: Any,
) -> Tuple[JsonDict, List[JsonDict], int, Optional[JsonDict]]:
    """Refresh post-navigation DOM identity without replaying navigation.

    ``Page.navigate`` may already have committed even when AX collection hits a
    transient renderer/lifecycle failure. Replaying it can duplicate side
    effects and restart loading. Keep this recovery leg bounded by the original
    navigation attempt deadline and retry only state synchronization/AX.
    """
    attempts: List[JsonDict] = []
    state_resync_count = 0
    latest_state: Optional[JsonDict] = None
    last_result: JsonDict = {
        "status": "axtree_refresh_deadline_exhausted",
        "tool_was_executed": False,
    }
    force_next_ax = False
    for ax_attempt in range(1, NAVIGATE_VERIFIED_AX_REFRESH_MAX_ATTEMPTS + 1):
        # The first AX refresh is a required consistency check after navigation,
        # even when Page.navigate/settlement consumed the nominal deadline. Only
        # tolerance retries (attempts 2-3) are suppressed after budget expiry.
        if ax_attempt > 1 and time.monotonic() >= deadline and not force_next_ax:
            break
        force_next_ax = False
        tracker = getattr(agent, "page_lifecycle", None)
        lifecycle_before = (
            tracker.state(page_id)
            if isinstance(tracker, PageLifecycleTracker)
            else None
        )
        generation_before = (
            lifecycle_before.generation if lifecycle_before is not None else None
        )
        tree_result = await _invoke_browser_method(
            agent,
            "DOM.getAXTree",
            {
                "pageId": page_id,
                "purpose": (
                    "Refresh DOM identity after verified navigation"
                    f" (AX attempt {ax_attempt})"
                ),
            },
            step,
            count_progress=False,
        )
        last_result = tree_result
        attempt_receipt: JsonDict = {"attempt": ax_attempt, "result": tree_result}
        attempts.append(attempt_receipt)
        if _result_has_auto_hitl(tree_result):
            return tree_result, attempts, state_resync_count, latest_state

        # A redirect/recovery can begin between the verified Page.getState and
        # the AX RPC. Discharge only the newly raised state-resync obligation;
        # never convert it into another Page.navigate attempt.
        lifecycle_state = (
            tracker.state(page_id)
            if isinstance(tracker, PageLifecycleTracker)
            else None
        )
        generation_changed = bool(
            lifecycle_state is not None
            and generation_before is not None
            and lifecycle_state.generation != generation_before
        )
        crashed = bool(
            lifecycle_state is not None
            and (
                lifecycle_state.status == "crashed"
                or lifecycle_state.last_event == "Page.crashed"
            )
        )
        identity_invalidated = bool(generation_changed or crashed)
        state_resync_required = bool(
            lifecycle_state is not None
            and lifecycle_state.requires_state_resync
        )
        tree_failed = _invoke_result_failed(tree_result)
        if not tree_failed and not identity_invalidated and not state_resync_required:
            return tree_result, attempts, state_resync_count, latest_state
        if identity_invalidated:
            # Even a successful AX response is stale when navigation generation
            # changed (or the renderer crashed) during the RPC. Quarantine it
            # and require a new AX after state synchronization; never combine
            # old-tree evidence with the new page's URL/title.
            quarantine_reason = (
                "page_generation_changed_during_ax"
                if generation_changed
                else "page_crashed_during_ax"
            )
            attempt_receipt["quarantined"] = quarantine_reason
            if isinstance(tracker, PageLifecycleTracker):
                tracker.invalidate_ax_refresh(page_id)
            _invalidate_axtree_snapshot(
                agent,
                "navigate_verified.ax_identity_invalidated",
                {"pageId": page_id},
            )
            last_result = {
                "status": "axtree_refresh_invalidated_by_navigation",
                "tool_was_executed": False,
            }
        if state_resync_required:
            state_result = await _invoke_browser_method(
                agent,
                "Page.getState",
                {
                    "pageId": page_id,
                    "purpose": (
                        "Synchronize state after post-navigation AX refresh failure"
                    ),
                },
                step,
                count_progress=False,
            )
            state_resync_count += 1
            if _result_has_auto_hitl(state_result):
                return state_result, attempts, state_resync_count, latest_state
            state_outcome = classify_call_outcome(state_result)
            if not (
                state_outcome.succeeded
                and page_state_evidence_ok(page_id, state_result)
            ):
                return (
                    {
                        "status": "navigation_state_resync_failed_during_ax",
                        "tool_was_executed": False,
                        "axtreeRefreshed": bool(
                            not tree_failed and not identity_invalidated
                        ),
                    },
                    attempts,
                    state_resync_count,
                    latest_state,
                )
            latest_state = _navigation_state_snapshot(
                _response_data(state_result),
                url_matches=url_matches,
                title_re=title_re,
                settlement="ax_refresh_state_resync",
                redirect_settlements=0,
            )
            if (
                not latest_state.get("urlOk")
                or not latest_state.get("titleOk")
                or latest_state.get("titleLingering")
            ):
                if not identity_invalidated:
                    attempt_receipt["quarantined"] = (
                        "page_state_mismatch_during_ax"
                    )
                    if isinstance(tracker, PageLifecycleTracker):
                        tracker.invalidate_ax_refresh(page_id)
                    _invalidate_axtree_snapshot(
                        agent,
                        "navigate_verified.ax_state_mismatch",
                        {"pageId": page_id},
                    )
                return (
                    {
                        "status": "navigation_redirected_during_ax_refresh",
                        "tool_was_executed": False,
                    },
                    attempts,
                    state_resync_count,
                    latest_state,
                )
            if not tree_failed and not identity_invalidated:
                # Dialog/chooser/download events require state synchronization
                # but do not invalidate DOM identity. Keep the successful AX and
                # return without an unnecessary replacement AX RPC.
                return tree_result, attempts, state_resync_count, latest_state
            # The preceding AX failed or belongs to the previous lifecycle
            # generation. Its replacement is a mandatory consistency check, not
            # a tolerance retry, so it gets one bounded attempt past deadline.
            force_next_ax = True
    return last_result, attempts, state_resync_count, latest_state


def _fresh_page_settlement_task(
    agent: Any,
    page_id: str,
    timeout_seconds: float,
) -> Optional["asyncio.Task[Optional[JsonDict]]"]:
    waiter = getattr(getattr(agent, "browser", None), "wait_for_notification", None)
    if not callable(waiter):
        return None

    def predicate(message: JsonDict) -> bool:
        event = unwrap_notification(message)
        if event is None or str(event.get("event") or "") not in {
            "Page.loaded", "Page.loadFailed", "Page.crashed",
        }:
            return False
        payload = event.get("payload")
        return bool(
            isinstance(payload, dict)
            and str(payload.get("pageId") or "") == page_id
        )

    async def wait() -> Optional[JsonDict]:
        try:
            message = await waiter(predicate, timeout=max(0.0, timeout_seconds))
        except TypeError:
            message = await waiter(predicate, max(0.0, timeout_seconds))
        return unwrap_notification(message)

    return asyncio.create_task(wait())


async def _cancel_waiter(waiter: Optional["asyncio.Task[Any]"]) -> None:
    if waiter is None:
        return
    if not waiter.done():
        waiter.cancel()
    try:
        await waiter
    except asyncio.CancelledError:
        pass


def _page_challenge_summary(agent: Any, page_id: str) -> JsonDict:
    tracker = getattr(agent, "challenge_tracker", None)
    state = tracker.get_state(page_id) if tracker is not None and page_id else None
    return state.to_summary() if state is not None else {}


def _ensure_content_completeness_tracker(
    agent: Any,
) -> Optional[ContentCompletenessTracker]:
    """Install the worker's normalized completeness contract when needed."""
    contract = getattr(agent, "worker_contract", None)
    config = (
        contract.get("content_completeness")
        if isinstance(contract, dict) else None
    )
    config_source = (
        str(contract.get("content_completeness_source") or "explicit")
        if isinstance(contract, dict) else "explicit"
    )
    tracker = getattr(agent, "content_completeness_tracker", None)
    if tracker is None or (not tracker.enabled and bool(config)):
        tracker = ContentCompletenessTracker(
            config,
            config_source=config_source,
        )
        agent.content_completeness_tracker = tracker
    return tracker


def _observe_content_completeness_after(
    agent: Any,
    method: str,
    params: JsonDict,
    result: JsonDict,
    step: int,
    *,
    content_binding: Any = None,
) -> JsonDict:
    contract = getattr(agent, "worker_contract", None)
    tracker = _ensure_content_completeness_tracker(agent)
    if tracker is None or not tracker.enabled:
        return result
    if hasattr(tracker, "observe_auth_generation"):
        tracker.observe_auth_generation(
            getattr(agent, "fleet_barrier_generation", 0)
        )
    upstream_blocker = _content_completeness_upstream_blocker(
        agent,
        method,
        params,
        result,
    )
    summary = tracker.observe(
        method=method,
        params=params,
        result=result,
        step=step,
        upstream_blocker=upstream_blocker,
    )
    binding_receipt = tracker.observe_content_binding(
        method=method,
        params=params,
        result=result,
        binding=content_binding,
    ) if isinstance(content_binding, dict) else None
    if isinstance(binding_receipt, dict) and binding_receipt.get("status") in {
        "accepted", "unchanged",
    }:
        binding_page_id = str(
            params.get("pageId") if isinstance(params, dict) else ""
        )
        binding_state = getattr(tracker, "pages", {}).get(binding_page_id)
        if binding_state is not None:
            summary = binding_state.summary()
    logger = getattr(agent, "logger", None)
    if logger is not None and hasattr(logger, "write"):
        phase_id = str(contract.get("phase_id") or "") if isinstance(contract, dict) else ""
        for telemetry in tracker.drain_telemetry_events():
            event_name = str(telemetry.pop("event", "") or "")
            if not event_name:
                continue
            payload = {"phaseId": phase_id or None, **telemetry}
            for key in ("sourceUrl", "targetUrl"):
                raw_url = str(payload.get(key) or "")
                if not raw_url:
                    continue
                try:
                    parsed = urlparse(raw_url)
                    payload[key] = (
                        f"{parsed.scheme}://{parsed.netloc}{parsed.path}"
                        if parsed.scheme and parsed.netloc else parsed.path
                    )
                except ValueError:
                    payload[key] = raw_url.split("?", 1)[0]
            logger.write(event_name, payload)
    if not isinstance(summary, dict):
        return result
    enriched = dict(result)
    enriched["contentCompleteness"] = summary
    if isinstance(binding_receipt, dict):
        enriched["contentBinding"] = binding_receipt
        if binding_receipt.get("status") == "rejected":
            existing_next_step = str(enriched.get("next_step") or "").strip()
            enriched["next_step"] = " ".join(value for value in (
                existing_next_step,
                "Use content_binding.regionId from the declared"
                " content_completeness expected regions, or omit the binding.",
            ) if value)
    page_id = str(summary.get("pageId") or "")
    recovery_receipt = (
        tracker.recovery_receipt(page_id)
        if hasattr(tracker, "recovery_receipt") else None
    )
    if isinstance(recovery_receipt, dict):
        enriched["routeRecovery"] = recovery_receipt
    route_preference = (
        tracker.route_preference_for_page(page_id)
        if hasattr(tracker, "route_preference_for_page") else None
    )
    if isinstance(route_preference, dict):
        enriched["routePreference"] = route_preference
    binding_instruction = str(
        summary.get("collectionBindingNextInstruction") or ""
    ).strip()
    recovery_instruction = str(
        (recovery_receipt or {}).get("next_instruction")
        if isinstance(recovery_receipt, dict) else ""
    ).strip()
    decision_instruction = str(
        summary.get("decisionNextInstruction") or ""
    ).strip()
    if binding_instruction or recovery_instruction or decision_instruction:
        existing_next_step = str(enriched.get("next_step") or "").strip()
        enriched["next_step"] = " ".join(
            value for value in (
                existing_next_step,
                binding_instruction,
                recovery_instruction,
                decision_instruction,
            ) if value
        )
    if logger is not None and hasattr(logger, "write"):
        logger.write("content_completeness.observed", summary)
        if (
            summary.get("decision") != "inconclusive"
            or summary.get("contentState") != "absent"
        ):
            logger.write("content_completeness.decision", summary)
    return enriched


def _content_completeness_upstream_blocker(
    agent: Any,
    method: str,
    params: JsonDict,
    result: JsonDict,
) -> str:
    """Return an existing higher-priority page classification, if any.

    Content completeness must not reinterpret authentication, challenge,
    lifecycle, navigation, or infrastructure failures as route-sensitive
    suppression.  Vocabulary remains owned by the dedicated detectors; this
    adapter consumes their structured receipts only.
    """
    if _invoke_result_failed(result):
        classification = (
            result.get("errorClassification")
            if isinstance(result.get("errorClassification"), dict) else {}
        )
        kind = str(classification.get("type") or "browser_call_failed").strip()
        return f"error:{kind}"

    page_id = extract_page_id(params, result)
    data = _response_data(result)
    hitl = data.get("hitl") if isinstance(data.get("hitl"), dict) else {}
    if hitl.get("isPaused") is True or isinstance(result.get("pausedState"), dict):
        return "hitl_paused"

    if method == "collect_items" and str(result.get("collectionState") or "") == "blocked":
        overlay_receipt = (
            result.get("overlayEncountered")
            if isinstance(result.get("overlayEncountered"), dict) else {}
        )
        overlay_subtype = str(overlay_receipt.get("subtype") or "").strip()
        if overlay_subtype:
            return f"overlay:{overlay_subtype}"
        stop_reason = str(result.get("stopReason") or "").strip()
        if stop_reason in {"overlay_blocked", "overlay_unresolved"}:
            return f"overlay:{stop_reason.removeprefix('overlay_')}"

    lifecycle = getattr(agent, "page_lifecycle", None)
    lifecycle_state = (
        lifecycle.state(page_id)
        if lifecycle is not None and page_id and hasattr(lifecycle, "state")
        else None
    )
    lifecycle_status = str(getattr(lifecycle_state, "status", "") or "").lower()
    if lifecycle_status in {"loading", "failed", "crashed"}:
        return f"lifecycle:{lifecycle_status}"

    status = str(data.get("status") or "").strip().lower().replace("_", "")
    if status in {"loading", "navigating", "startedloading"}:
        return "lifecycle:loading"
    if status in {"failed", "loadfailed", "error", "crashed"}:
        return f"lifecycle:{status}"

    navigation_check = (
        result.get("navigationCheck")
        if isinstance(result.get("navigationCheck"), dict) else {}
    )
    navigation_status = str(navigation_check.get("status") or "")
    if navigation_status == "challenge_pending":
        return "challenge:navigation"
    if navigation_status == "off_target":
        return "navigation:off_target"

    if isinstance(result.get("structuralChallenge"), dict):
        return "challenge:structural"
    auto_hitl = result.get("autoHitl")
    if isinstance(auto_hitl, dict) and _auto_hitl_is_actionable(auto_hitl):
        return "challenge:hitl"
    challenge = _page_challenge_summary(agent, page_id)
    tracker = getattr(agent, "challenge_tracker", None)
    threshold = int(getattr(tracker, "threshold", 70) or 70)
    if (
        challenge.get("structuralChallenge")
        or challenge.get("highConfidenceHit")
        or _challenge_score(challenge) >= threshold
    ):
        return "challenge:detected"

    overlay = detect_overlay_from_result(result)
    subtype = str((overlay or {}).get("subtype") or "")
    if subtype in {"auth_prompt", "paywall"}:
        return f"overlay:{subtype}"
    # DOM responses do not always repeat the document title.  Reuse the title
    # most recently recorded by the completeness tracker, but classify it via
    # the dedicated auth detector rather than adding auth vocabulary here.
    content_tracker = getattr(agent, "content_completeness_tracker", None)
    content_state = (
        content_tracker.pages.get(page_id)
        if content_tracker is not None
        and isinstance(getattr(content_tracker, "pages", None), dict)
        and page_id
        else None
    )
    remembered_title = str(getattr(content_state, "title", "") or "")
    if title_looks_like_auth_page(remembered_title):
        return "overlay:auth_prompt"
    return ""


def _challenge_score(summary: JsonDict) -> int:
    try:
        return int(summary.get("suspicionScore") or 0)
    except (TypeError, ValueError):
        return 0


def _clear_navigation_challenge_state(agent: Any, page_id: str) -> None:
    tracker = getattr(agent, "challenge_tracker", None)
    if tracker is not None and page_id:
        tracker.clear_page(page_id)
    logger = getattr(agent, "logger", None)
    if logger is not None:
        logger.write("challenge.navigation_cleared", {"pageId": page_id})


def _notify_navigation_success(
    agent: Any,
    page_id: str,
    *,
    navigation_kind: str = "verified",
) -> Optional[JsonDict]:
    progress = getattr(agent, "progress", None)
    if progress is None or not hasattr(progress, "notify_navigation_success"):
        return None
    result = progress.notify_navigation_success(
        page_id,
        navigation_kind=navigation_kind,
    )
    logger = getattr(agent, "logger", None)
    if logger is not None:
        event = (
            "progress.history_navigation_credit_exhausted"
            if result.get("status") == "history_navigation_credit_exhausted"
            else "progress.navigation_success"
        )
        logger.write(event, result)
    return result


def _observe_navigation_progress_after(
    agent: Any,
    method: str,
    params: JsonDict,
    result: JsonDict,
) -> None:
    page_id = str(params.get("pageId") or "").strip()
    pending = getattr(agent, "navigation_progress_pending_pages", None)
    if not isinstance(pending, dict):
        pending = {}
        agent.navigation_progress_pending_pages = pending
    last_urls = getattr(agent, "navigation_progress_last_urls", None)
    if not isinstance(last_urls, dict):
        last_urls = {}
        agent.navigation_progress_last_urls = last_urls
    # Only the explicit history-return primitive earns a reset on this raw
    # browser-call path. Page.reload is same-route retry and raw Page.navigate
    # is not URL/title verified; either could otherwise loop with Page.getState
    # to replenish the no-artifact and heavy-diagnostic budgets indefinitely.
    # navigate_verified has its own verified reset in _observe_progress_after.
    if method == "Page.go":
        pending.pop(page_id, None)
        if page_id and not _invoke_result_failed(result):
            pending[page_id] = str(last_urls.get(page_id) or "")
        return
    if method in {"Page.navigate", "Page.reload"}:
        pending.pop(page_id, None)
        return
    if method == "Page.getState" and page_id in pending:
        current_url = str(
            _response_data(result).get("url")
            or _response_data(result).get("currentUrl")
            or ""
        ).strip()
        previous_url = str(pending.pop(page_id, "") or "").strip()
        if not _invoke_result_failed(result):
            if current_url:
                last_urls[page_id] = current_url
            if previous_url and current_url and current_url != previous_url:
                progress_receipt = _notify_navigation_success(
                    agent,
                    page_id,
                    navigation_kind="history",
                )
                if isinstance(progress_receipt, dict):
                    result["progressNavigation"] = progress_receipt
            else:
                result["progressNavigation"] = {
                    "status": "history_navigation_unverified",
                    "pageId": page_id,
                    "navigationKind": "history",
                    "previousUrl": previous_url or None,
                    "currentUrl": current_url or None,
                    "creditApplied": False,
                }
                logger = getattr(agent, "logger", None)
                if logger is not None:
                    logger.write(
                        "progress.history_navigation_unverified",
                        {
                            "pageId": page_id,
                            "previousUrl": previous_url or None,
                            "currentUrl": current_url or None,
                            "creditApplied": False,
                            "reason": (
                                "missing_pre_navigation_url"
                                if not previous_url
                                else "missing_post_navigation_url"
                                if not current_url
                                else "url_unchanged"
                            ),
                        },
                    )
        return
    if method == "Page.getState" and page_id and not _invoke_result_failed(result):
        current_url = str(
            _response_data(result).get("url")
            or _response_data(result).get("currentUrl")
            or ""
        ).strip()
        if current_url:
            last_urls[page_id] = current_url


def _strip_challenge_fields(value: Any) -> Any:
    if isinstance(value, dict):
        challenge_keys = {"suspected_challenge", "challengeAdjudication", "autoHitl"}
        stripped_keys = set(challenge_keys)
        if any(key in value for key in challenge_keys):
            stripped_keys.add("next_instruction")
        return {
            key: _strip_challenge_fields(item)
            for key, item in value.items()
            if key not in stripped_keys
        }
    if isinstance(value, list):
        return [_strip_challenge_fields(item) for item in value]
    return value


def _page_inventory_is_discoverable(agent: Any, page_id: str) -> bool:
    """Whether an unseen page is worth telling this worker to go look for.

    A page another live worker already holds is not a discovery opportunity, so
    signalling it would be pure noise. Ownership is checked here rather than
    when the event arrived because the lease is recorded only after the
    creating RPC returns — at event time every page still looks unowned.
    """
    manager = getattr(agent, "page_lease_manager", None)
    if manager is None or not hasattr(manager, "owner_for"):
        return True
    worker_id = str(getattr(agent, "worker_id", "") or "").strip()
    owner = str(manager.owner_for(page_id) or "").strip()
    return not owner or owner == worker_id


def _settle_page_inventory_signal(
    agent: Any,
    method: str,
    params: JsonDict,
    result: JsonDict,
    *,
    page_list_shown: Optional[List[JsonDict]] = None,
) -> JsonDict:
    """Discharge pages the worker now knows about, then attach the change bit.

    Discharge runs BEFORE the receipt is built so a call that itself reveals a
    page never carries a signal about that page: Page.create names the tab it
    just made, Page.list shows the model every row, Page.close removes one.
    """
    signal = getattr(agent, "page_inventory_signal", None)
    if signal is None or not isinstance(result, dict):
        return result

    if method == "Page.create":
        # The response names the tab this worker just made. Without this the
        # worker would be told to go find its own page: the Page.open event
        # always lands BEFORE the response that identifies it.
        for page_id in _result_page_ids_for_inventory(result.get("response")):
            grant = evaluate_grant(
                kind="inventory_discharge_page_create",
                method=method,
                result=result,
                page_id=page_id,
            )
            if grant.allowed:
                signal.discharge([page_id])
    elif method == "Page.close":
        page_id = str(params.get("pageId") or "").strip()
        grant = evaluate_grant(
            kind="inventory_discharge_page_close",
            method=method,
            result=result,
            page_id=page_id,
        )
        if grant.allowed:
            signal.discharge([page_id])
    elif method == "Page.list" and page_list_shown is not None:
        grant = evaluate_grant(
            kind="inventory_discharge_page_list",
            method=method,
            result=result,
        )
        if grant.allowed:
            for row in page_list_shown:
                if not isinstance(row, dict):
                    continue
                signal.discharge(
                    [row.get("pageId")],
                    fleet_id=row.get("fleetId"),
                )

    fleet_id = str(getattr(agent, "assigned_fleet_id", "") or "").strip()
    if not fleet_id:
        return result
    receipt = signal.receipt(
        fleet_id,
        is_discoverable=lambda page_id: _page_inventory_is_discoverable(
            agent, page_id
        ),
    )
    if receipt:
        result["pageInventoryChanged"] = True
        result["pageInventoryInstruction"] = receipt["next_instruction"]
    return result


def _result_page_ids_for_inventory(response: Any) -> List[str]:
    data = response.get("data") if isinstance(response, dict) else None
    if isinstance(data, dict):
        page_id = str(data.get("pageId") or "").strip()
        return [page_id] if page_id else []
    return []


def _navigate_pattern_invalid_result(
    *,
    page_id: str,
    field: str,
    pattern: str,
    error: str,
) -> JsonDict:
    """Reject an uncompilable expectation BEFORE spending a real navigation."""
    return {
        "status": "expectation_pattern_invalid",
        "tool_was_executed": False,
        "navigationCommitted": False,
        "pageId": page_id,
        "field": field,
        "pattern": pattern[:200],
        "error": f"{field} is not a valid regular expression: {error}"[:300],
        "next_instruction": (
            f"No navigation was dispatched. Fix {field} — or omit it, which"
            " accepts the requested URL itself (expectedUrlPattern) or skips"
            " the title check (expectedTitlePattern) — then call"
            " navigate_verified again."
        ),
    }


def _nested_response_error(result: Any) -> str:
    """Return the browser-side error text carried inside `response`."""
    if not isinstance(result, dict):
        return ""
    response = result.get("response")
    if not isinstance(response, dict):
        return ""
    for candidate in (
        response.get("error"),
        (response.get("data") or {}).get("error")
        if isinstance(response.get("data"), dict)
        else None,
    ):
        if isinstance(candidate, dict):
            text = str(candidate.get("message") or candidate.get("error") or "")
            if text:
                return text
        elif candidate:
            return str(candidate)
    return ""


async def _read_page_state_once(
    agent: Any,
    page_id: str,
    step: int,
) -> JsonDict:
    """One read-only Page.getState, reported as observation or as unreadable."""
    state_result = await _invoke_browser_method(
        agent,
        "Page.getState",
        {
            "pageId": page_id,
            "purpose": "Observe page state after a failed navigation",
        },
        step,
        count_progress=False,
    )
    outcome = classify_call_outcome(state_result)
    if outcome.interrupted:
        # A challenge/HITL pause is a terminal state, not an unreadable page.
        # Flattening it here hid the whole hitl_wait payload and let the model
        # keep acting on a page the platform had paused.
        return {
            "observedState": "hitl_interrupted",
            "autoHitl": outcome.auto_hitl,
            "next_instruction": (
                "The page entered human-intervention handling while its state"
                " was being read. Inspect autoHitl.hitl_wait and stop acting on"
                " this page until it reports resumed."
            ),
        }
    if not outcome.succeeded or not page_state_evidence_ok(page_id, state_result):
        return {
            "observedState": "unreadable",
            "observedStateError": (
                outcome.error or "Page.getState returned no usable page state"
            ),
        }
    data = _response_data(state_result) or {}
    return {
        "observedState": "read",
        "observedUrl": str(data.get("url") or ""),
        "observedTitle": str(data.get("title") or ""),
        "observedPageStatus": str(data.get("status") or ""),
    }


async def _navigate_dispatch_failure_result(
    agent: Any,
    *,
    page_id: str,
    url: str,
    nav: JsonDict,
    step: int = 0,
) -> JsonDict:
    """Classify a failed Page.navigate by what the harness actually OBSERVED.

    Only two facts are ever available first-hand, and only they may be stated:

    * A pre-dispatch guard answered ``tool_was_executed=False``. The call never
      reached the panel, so the page is provably untouched.
    * The lifecycle tracker received ``Page.loadFailed`` for this page. The
      navigation was attempted and provably did not arrive.

    Everything else — transport exceptions, ``-32005``, a dead renderer, a
    precondition rejection, any Chrome ``net::ERR_*`` string — leaves the commit
    position genuinely unknown. Earlier revisions tried to rank those by
    parsing the error text, which meant guessing browser semantics: ERR_ABORTED
    is raised when another navigation supersedes this one, and
    ERR_BLOCKED_BY_CLIENT fires before the request leaves. Neither proves the
    page stayed put. They now share one status, with the distinction kept as
    non-load-bearing diagnostics, because the model's next move is identical in
    every case: read the page state before deciding anything.
    """
    classification = nav.get("errorClassification")
    # A transport exception lands at the top level; a browser-side failure is
    # nested in the response.
    error_text = str(nav.get("error") or _nested_response_error(nav) or "")[:300]

    if nav.get("tool_was_executed") is False:
        return {
            "status": "navigation_not_dispatched",
            "tool_was_executed": False,
            "navigationCommitted": False,
            "pageId": page_id,
            "requestedUrl": url,
            "guardStatus": str(nav.get("status") or "") or None,
            "error": error_text or None,
            "errorClassification": classification,
            "navigateResult": _strip_challenge_fields(nav),
            "next_instruction": (
                "A harness guard refused the call before it reached the"
                " browser, so the page is untouched. Read guardStatus, clear"
                " that condition, then decide whether to navigate."
            ),
        }

    challenge = _page_challenge_summary(agent, page_id)
    # Snapshot the lifecycle BEFORE reading: Page.getState feeds the tracker and
    # would overwrite the Page.loadFailed this branch exists to detect.
    lifecycle_state = (
        agent.page_lifecycle.state(page_id)
        if isinstance(getattr(agent, "page_lifecycle", None), PageLifecycleTracker)
        else None
    )
    lifecycle_reported_failure = (
        str(getattr(lifecycle_state, "status", "") or "") == "failed"
    )
    # The request was dispatched and failed, so where the page sits is a
    # question only the page can answer. Read it ONCE here rather than telling
    # the model to: Page.getState issues no network request, and a receipt that
    # merely says "go look" leaves the model to act on a state nobody observed.
    observed = await _read_page_state_once(agent, page_id, step)
    if observed.get("observedState") == "hitl_interrupted":
        # The read itself hit the human-intervention path. That outranks any
        # navigation classification: the model must handle the pause, not the
        # failed navigate.
        return {
            "status": "navigation_interrupted_by_hitl",
            "tool_was_executed": True,
            "navigationCommitted": None,
            "pageId": page_id,
            "requestedUrl": url,
            "error": error_text,
            "errorClassification": classification,
            **observed,
        }
    common: JsonDict = {
        "pageId": page_id,
        "requestedUrl": url,
        "error": error_text,
        "errorClassification": classification,
        "navigateResult": _strip_challenge_fields(nav),
        "suspectedChallenge": challenge or None,
        **observed,
    }

    if lifecycle_reported_failure:
        # ABCP states WHY the page is unusable in `failure.kind`; carry it
        # instead of leaving the caller to re-derive it from prose. An
        # `automation-unavailable` page is not a navigation the browser lost —
        # re-navigating cannot fix it.
        failure_kind = str(getattr(lifecycle_state, "failure_kind", "") or "")
        automation_unavailable = failure_kind == AUTOMATION_UNAVAILABLE_FAILURE
        return {
            **common,
            "status": "navigation_load_failed",
            "tool_was_executed": True,
            "navigationCommitted": False,
            "pageFailure": {
                "kind": failure_kind,
                "message": str(getattr(lifecycle_state, "failure_message", "") or "") or None,
                "retryableByNavigation": not automation_unavailable,
            } if failure_kind else None,
            "next_instruction": (
                "The browser reported Page.loadFailed for this navigation."
                " observedUrl/observedTitle are where the page actually sits."
                + (
                    " pageFailure.kind=automation-unavailable: the document may"
                    " be fine while automation cannot attach, so navigating"
                    " again will not change it — report the blocker instead."
                    if automation_unavailable else
                    " Decide from those whether a fresh navigation is warranted;"
                    " this composite will not re-dispatch it for you."
                )
            ),
        }

    error_type = (
        str(classification.get("type") or "")
        if isinstance(classification, dict)
        else ""
    )
    if error_type in {"page_crashed", "render_lost"}:
        reason = "page_unavailable"
    elif nav.get("error"):
        reason = "transport_error"
    else:
        reason = "browser_action_failed"
    return {
        **common,
        "status": "navigation_outcome_unknown",
        "tool_was_executed": True,
        "navigationCommitted": None,
        "reason": reason,
        "next_instruction": (
            "Page.navigate failed without proving where the page ended up, so"
            " the harness read the page for you: observedUrl/observedTitle are"
            " its actual state. Decide from those; do NOT call"
            " navigate_verified again for this navigation."
        ),
    }



def _navigate_challenge_blocked_result(
    *,
    page_id: str,
    attempt: int,
    last_state: JsonDict,
    attempts: List[JsonDict],
    state_resync_count: int,
    challenge_summary: JsonDict,
    expected_url_pattern: str,
    expected_title_pattern: str,
    trigger: str,
) -> JsonDict:
    return {
        "status": "blocked_by_challenge",
        "pageId": page_id,
        "attempt": attempt,
        "lastState": last_state,
        "attempts": attempts,
        "stateResyncCount": state_resync_count,
        "expectedUrlPattern": expected_url_pattern,
        "expectedTitlePattern": expected_title_pattern or None,
        "suspectedChallenge": challenge_summary or None,
        "trigger": trigger,
        "next_instruction": (
            "Navigation appears blocked by an anti-bot or challenge page after"
            " bounded verification. Do not keep polling Page.getState; call"
            " final_answer with status=\"blocked_by_challenge\", request HITL"
            " if the workflow supports it, or let LeadAgent pivot strategy."
        ),
    }


def _result_has_auto_hitl(result: Any) -> bool:
    return isinstance(result, dict) and isinstance(result.get("autoHitl"), dict)


def _auto_hitl_is_actionable(auto: Any) -> bool:
    """True only when an autoHitl entry represents a REAL pause request — i.e.
    `Hitl.requestPause` actually ran. A skipped / not-executed adjudication is a
    no-op: the page was never paused, so a composite loop must NOT abort on it.

    Post-97f105e the harness only writes result['autoHitl'] when it truly requests
    HITL (skipped/cooldown/stale verdicts go to `suspected_challenge.adjudication`
    instead), so in practice every autoHitl is actionable. This guard keeps
    `_loop_interrupt_from_result` honest against a future short-circuit that could
    attach a `tool_was_executed: False` / `status: "skipped*"` autoHitl.

    The rule itself lives in harness.call_outcome so the shared verdict and this
    loop guard cannot drift apart; a second, weaker copy of it treated every
    skipped adjudication as a pause."""
    return auto_hitl_is_actionable(auto)


def _navigate_hitl_result(page_id: str, attempt: int, result: JsonDict) -> JsonDict:
    wait = {}
    auto_hitl = result.get("autoHitl")
    if isinstance(auto_hitl, dict):
        response = auto_hitl.get("response")
        if isinstance(response, dict) and isinstance(response.get("hitl_wait"), dict):
            wait = response.get("hitl_wait") or {}
    if wait.get("status") in {"timeout", "page_settled_after_hitl", "stale_pause_deadlock"}:
        status = str(wait.get("status"))
    else:
        status = "hitl_required"
    next_instruction = (
        "The page appears to be past the challenge, but ABCP still reports it"
        " paused. Do not keep polling; call final_answer with"
        " status=\"page_settled_after_hitl\" and surface that the ABCP control"
        " channel has not released the paused page yet."
        if status == "page_settled_after_hitl" else
        "The page is in a stale HITL pause deadlock. Do not request HITL again;"
        " continue from a fresh page/fleet or report the platform blocker."
        if status == "stale_pause_deadlock" else
        "Human intervention was requested for a suspected challenge. Do not"
        " keep polling this page while it is paused; inspect autoHitl.hitl_wait."
    )
    return {
        "status": status,
        "pageId": page_id,
        "attempt": attempt,
        "autoHitl": auto_hitl,
        "triggerResult": result,
        "next_instruction": next_instruction,
    }


def _loop_interrupt_summary(
    status: str,
    *,
    autoHitl: Optional[JsonDict] = None,
    pausedState: Optional[JsonDict] = None,
) -> JsonDict:
    """Summary a composite loop returns when a HITL/challenge interrupt aborts it.

    For the blocked statuses needsHuman=True tells the LLM that resuming/retrying
    is futile until a human clears the page. The `hitl_resumed` status is
    different: a human ALREADY resolved the challenge mid-loop, so needsHuman is
    False — but the loop still STOPS (loopInterrupted) because the page may have
    changed under the human (navigation, closed dialogs, altered form state) and
    the loop's local assumptions / target ids are no longer trustworthy. The
    model must re-observe and re-issue rather than the loop blindly continuing."""
    instructions = {
        "hitl_required": (
            "A human verification (e.g. Cloudflare/CAPTCHA) blocked this page and"
            " the loop paused for HITL. Do NOT resume the loop or retry browser"
            " actions; wait for the human resume event or report the blocker to"
            " LeadAgent."
        ),
        "timeout": (
            "Human intervention was requested for a challenge but did not complete"
            " in time. Do NOT resume the loop or retry; report the blocker or hand"
            " off to LeadAgent."
        ),
        "page_settled_after_hitl": (
            "The page looks past the challenge but ABCP still reports it paused."
            " Do NOT resume the loop; surface that the control channel has not"
            " released the page."
        ),
        "stale_pause_deadlock": (
            "The page is in a stale HITL pause deadlock. Do NOT request HITL again"
            " or resume the loop; continue from a fresh page/fleet or report the"
            " platform blocker."
        ),
        "hitl_resumed": (
            "A human resolved a challenge (e.g. Cloudflare) mid-loop, so the page"
            " may have changed (navigation, closed dialogs, altered form state)."
            " The loop stopped WITHOUT acting on possibly-stale state. Re-observe"
            " with Page.getState + DOM.getAXTree, then re-issue the action/tool"
            " with fresh ids if it is still valid. Any partial results are included."
        ),
    }
    needs_human = status != "hitl_resumed"
    if status == "hitl_resumed":
        resume = "reobserve_then_reissue"
    elif status in {"hitl_required", "timeout"}:
        resume = "wait_for_human"
    else:
        resume = "fresh_page_or_report"
    summary: JsonDict = {
        "status": status,
        "loopInterrupted": True,
        "needsHuman": needs_human,
        "resumeRecommendation": resume,
        "next_instruction": instructions.get(status, instructions["hitl_required"]),
    }
    # Layer 2 discipline: surface only a compact digest to the model. The full
    # autoHitl payload (pause request, VL adjudication, nested response) is
    # verbose and already in the run log via browser.call.result; the model only
    # needs the wait status + where/why.
    if autoHitl is not None:
        summary["hitlDigest"] = _hitl_digest(autoHitl)
    if pausedState is not None:
        summary["pausedState"] = pausedState
    return summary


def _hitl_digest(auto_hitl: Any) -> JsonDict:
    """Compact, model-facing digest of an autoHitl payload."""
    if not isinstance(auto_hitl, dict):
        return {}
    response = auto_hitl.get("response") if isinstance(auto_hitl.get("response"), dict) else {}
    wait = response.get("hitl_wait") if isinstance(response.get("hitl_wait"), dict) else {}
    suspected = (
        auto_hitl.get("suspected_challenge")
        if isinstance(auto_hitl.get("suspected_challenge"), dict) else {}
    )
    recovery = wait.get("postHitlRecovery") if isinstance(wait.get("postHitlRecovery"), dict) else {}
    digest = {
        "hitlWaitStatus": wait.get("status"),
        "pageId": auto_hitl.get("pageId") or wait.get("pageId") or response.get("pageId"),
        "reason": auto_hitl.get("reason") or suspected.get("reason") or suspected.get("adjudication"),
        "postHitlRecoveryStatus": recovery.get("status"),
        "screenshotPath": auto_hitl.get("screenshotPath") or suspected.get("screenshotPath"),
    }
    return {key: value for key, value in digest.items() if value is not None}


def _loop_interrupt_from_result(result: Any) -> Optional[JsonDict]:
    """Detect a HITL/challenge interrupt on a composite-loop internal browser
    call. Composite tools run with the model OUT of the loop, so when a call
    triggers auto-HITL (Cloudflare/CAPTCHA) or hits an already-paused page, the
    loop must STOP and surface a human-needed summary rather than keep
    clicking/scrolling or degrade to a generic stagnant/failed reason.

    Returns a summary to return immediately, or None when there is no interrupt
    and the loop may continue. NOTE: a `resumed` wait is NOT None — a human
    touched the page mid-loop, so the loop stops with a non-terminal
    `hitl_resumed` summary (needsHuman=False) for the model to re-observe; the
    loop must not keep acting on possibly-stale local state. The pause+wait happen
    synchronously inside the triggering _invoke_browser_method call, so the
    outcome is on THAT result."""
    if not isinstance(result, dict):
        return None
    auto = result.get("autoHitl")
    if isinstance(auto, dict) and _auto_hitl_is_actionable(auto):
        wait: JsonDict = {}
        response = auto.get("response") if isinstance(auto, dict) else None
        if isinstance(response, dict) and isinstance(response.get("hitl_wait"), dict):
            wait = response.get("hitl_wait") or {}
        status = str(wait.get("status") or "")
        if status == "resumed":
            # A human cleared the challenge, but the page may have changed under
            # them: stop and make the model re-observe rather than continue on
            # stale ids/assumptions.
            return _loop_interrupt_summary(
                "hitl_resumed", autoHitl=auto if isinstance(auto, dict) else None
            )
        terminal = (
            status
            if status in {"timeout", "page_settled_after_hitl", "stale_pause_deadlock"}
            else "hitl_required"
        )
        return _loop_interrupt_summary(
            terminal, autoHitl=auto if isinstance(auto, dict) else None
        )
    paused_state = result.get("pausedState")
    if isinstance(paused_state, dict) or _result_has_paused_error(result):
        return _loop_interrupt_summary(
            "hitl_required",
            pausedState=paused_state if isinstance(paused_state, dict) else None,
        )
    return None


def _invoke_result_failed(result: Any) -> bool:
    """True when an _invoke_browser_method result represents a failed ACTION.

    Browser-side action errors surface in response.error / response.data.error
    (top-level `error` is only set on transport exceptions), so a check that
    only reads result["error"] would report a failed retry as succeeded.

    NOT interchangeable with `classify_call_outcome`, and the difference is
    `response.data.error`:

    * this predicate answers "did the ACTION achieve its page effect", and for
      an action method a page-level error means it did not — retry paths and
      recovery ladders want that reading;
    * `classify_call_outcome` answers "did the CALL execute and come back",
      and deliberately ignores `data.error` because for a read like
      Page.getState that field is the PAGE's last-navigation error, permanent
      on a risk-controlled page. Anything that GRANTS state — re-perception
      credit, recovery credit, content binding, inventory baselines — must use
      the verdict, not this. Task 48b4d7d7 deadlocked for 84 minutes because a
      gate whose exit condition was "re-read the page" used this predicate.

    Two general failure predicates in one tree is the shape that caused that
    bug. Collapsing the ~20 call sites onto the verdict is tracked separately;
    until then, choose by the question you are asking."""
    if not isinstance(result, dict):
        return False
    if result.get("tool_was_executed") is False:
        return True
    if result.get("error"):
        return True
    if result.get("status") == "stale_element_reference":
        return True
    response = result.get("response")
    if isinstance(response, dict):
        if response.get("error"):
            return True
        data = response.get("data")
        if isinstance(data, dict) and data.get("error"):
            return True
    classification = result.get("errorClassification")
    if isinstance(classification, dict) and classification.get("type"):
        return True
    return False


def _transport_error_metadata(
    method: str,
    exc: ABCPTransportError,
) -> JsonDict:
    """Keep machine-readable RPC failure data where recovery needs it.

    ``rpcData`` is surfaced only for the select API pair. Other actions may
    carry typed or otherwise sensitive values in provider diagnostics; their
    numeric code/method remain useful without copying that opaque payload into
    the model-facing result.
    """

    metadata: JsonDict = {}
    local_receipt = getattr(exc, "receipt", None)
    if isinstance(local_receipt, dict):
        for key in (
            "status",
            "reasonKind",
            "pageId",
            "fleetId",
            "workerId",
            "ownerWorkerId",
            "methodKind",
            "retryable",
            "quarantined",
            "tool_was_executed",
            "next_instruction",
        ):
            if key in local_receipt:
                metadata[key] = local_receipt.get(key)
    rpc_code = getattr(exc, "rpc_code", None)
    rpc_method = str(getattr(exc, "rpc_method", "") or "")
    if rpc_code is not None:
        metadata["rpcCode"] = rpc_code
    if rpc_method:
        metadata["rpcMethod"] = rpc_method
    rpc_data = getattr(exc, "rpc_data", None)
    if method in {"DOM.inspectSelect", "Input.select"} and rpc_data is not None:
        metadata["rpcData"] = trim_large_strings(rpc_data, 4000)
    runtime = action_runtime_info(rpc_data)
    if runtime:
        # Four bounded scalars, no provider payload: whether the failure landed
        # before or after dispatch is the one fact a retry decision needs, and
        # inferring it from prose is guessing at something the platform states.
        metadata["actionRuntime"] = runtime
    return metadata


_SELECT_FAILURE_GUIDANCE: Dict[str, Tuple[int, str]] = {
    "select-option-stale": (
        1,
        "Call DOM.inspectSelect again, copy only fields returned for the requested"
        " option, then retry Input.select once, preferring its exact value or"
        " label when present and using option id only as fallback. Do not open"
        " or operate the popup manually or reuse an arbitrary AXTree option id.",
    ),
    "select-option-not-found": (
        1,
        "Call DOM.inspectSelect again with an appropriate query/maxOptions and"
        " inspect its loadMore/truncated state. Retry once only with an exact"
        " option descriptor returned by that inspection.",
    ),
    "select-option-disabled": (
        0,
        "The requested option is disabled. Stop retrying and report that it is"
        " unavailable; do not silently choose a different option.",
    ),
    "select-popup-lost": (
        0,
        "ABCP lost the select popup while executing the atomic Input.select"
        " action. Do not repeat the call, reload the page, or operate the popup"
        " manually; report the platform failure with this receipt.",
    ),
    "select-navigation-stalled": (
        0,
        "ABCP could not advance the cascading selection. Do not repeat the same"
        " path or replace it with manual popup clicks; report the platform"
        " failure with the DOM.inspectSelect path used.",
    ),
}


def _apply_select_failure_guidance(
    agent: Any,
    method: str,
    params: JsonDict,
    result: JsonDict,
) -> JsonDict:
    """Attach code-specific, mechanically bounded Input.select recovery."""

    if not isinstance(result, dict):
        return result
    if method == "DOM.inspectSelect":
        classification = result.get("errorClassification")
        error_code = (
            str(classification.get("errorCode") or "")
            if isinstance(classification, dict)
            else ""
        )
        if error_code == "select-control-not-visible":
            result["next_instruction"] = (
                "Refresh DOM.getAXTree and target only a currently visible"
                " select-like control. Do not retry the same hidden container"
                " selector or construct an Input.select request from hidden"
                " option rows."
            )
            result["selectRecovery"] = {
                "errorCode": error_code,
                "retryAllowed": False,
            }
        elif error_code == "select-control-unsupported":
            result["next_instruction"] = (
                "This element is not an ABCP-supported select-like control. Do"
                " not call Input.select for it. If it is an ordinary visible"
                " category/list browser, use fresh DOM.getAXTree targets and"
                " one verified Input.click per visible level; this is a"
                " non-select UI fallback, not manual popup management."
            )
            result["selectRecovery"] = {
                "errorCode": error_code,
                "retryAllowed": False,
            }
        return result
    if method != "Input.select":
        return result
    target = str(params.get("selector") or params.get("id") or "<unknown>")
    page_id = str(params.get("pageId") or "")
    ledger = getattr(agent, "_select_failure_ledger", None)
    if not _invoke_result_failed(result):
        if isinstance(ledger, dict):
            for key in list(ledger):
                if key[:2] == (page_id, target):
                    ledger.pop(key, None)
        return result
    classification = result.get("errorClassification")
    error_code = (
        str(classification.get("errorCode") or "")
        if isinstance(classification, dict)
        else ""
    )
    guidance = _SELECT_FAILURE_GUIDANCE.get(error_code)
    if guidance is None:
        return result
    max_retries, instruction = guidance
    if not isinstance(ledger, dict):
        ledger = {}
        setattr(agent, "_select_failure_ledger", ledger)
    key = (page_id, target, error_code)
    failures = int(ledger.get(key) or 0) + 1
    ledger[key] = failures
    retry_allowed = failures <= max_retries
    if max_retries and not retry_allowed:
        instruction = (
            "The one permitted recovery retry for this select/control/error has"
            " already failed. Stop retrying and report an ABCP select contract"
            " failure with the inspect and select receipts."
        )
    result["selectRecovery"] = {
        "errorCode": error_code,
        "failureCount": failures,
        "maxRetries": max_retries,
        "retryAllowed": retry_allowed,
        "controlTarget": target,
    }
    result["next_instruction"] = instruction
    return result


def _download_operation_key(params: Any) -> str:
    if not isinstance(params, dict):
        return ""
    url = str(params.get("url") or "").strip()
    save_path = str(params.get("savePath") or "").strip()
    return json.dumps([url, save_path], ensure_ascii=False) if url and save_path else ""


def _download_records(value: Any) -> List[JsonDict]:
    records: List[JsonDict] = []
    seen: Set[str] = set()

    def visit(item: Any, depth: int = 0) -> None:
        if depth > 6:
            return
        if isinstance(item, dict):
            url = str(item.get("url") or "").strip()
            save_path = str(item.get("savePath") or "").strip()
            state = str(item.get("state") or "").strip()
            if url and save_path and state:
                identity = str(item.get("id") or item.get("downloadId") or "")
                dedupe = identity or json.dumps(
                    [url, save_path, state, item.get("startedAt")],
                    ensure_ascii=False,
                )
                if dedupe not in seen:
                    seen.add(dedupe)
                    records.append(dict(item))
            for nested in item.values():
                if isinstance(nested, (dict, list)):
                    visit(nested, depth + 1)
        elif isinstance(item, list):
            for nested in item:
                visit(nested, depth + 1)

    visit(value)
    return records


def _download_receipt_store(agent: Any) -> Dict[str, JsonDict]:
    store = getattr(agent, "download_operation_receipts", None)
    if not isinstance(store, dict):
        store = {}
        agent.download_operation_receipts = store
    return store


DOWNLOAD_TIMEOUT_RECONCILIATION_DELAY_SECONDS = 4.0


def _remember_download_record(agent: Any, record: JsonDict) -> JsonDict:
    key = str(record.get("operationKey") or "") or _download_operation_key(record)
    receipt = {
        "downloadId": str(record.get("id") or record.get("downloadId") or ""),
        "url": str(record.get("url") or ""),
        "savePath": str(record.get("savePath") or ""),
        "state": str(record.get("state") or ""),
        "totalBytes": int(record.get("totalBytes") or 0),
        "receivedBytes": int(record.get("receivedBytes") or 0),
        "source": "Download.list",
    }
    if key:
        _download_receipt_store(agent)[key] = receipt
    return receipt


def _remember_unverified_download_timeout(
    agent: Any,
    params: JsonDict,
    *,
    rpc_code: Optional[int],
) -> JsonDict:
    """Remember an uncertain side effect without laundering it as success."""
    key = _download_operation_key(params)
    receipt = {
        "downloadId": "",
        "url": str(params.get("url") or ""),
        "savePath": str(params.get("savePath") or ""),
        "state": "timeout_unverified",
        "totalBytes": 0,
        "receivedBytes": 0,
        "source": "Download.start_timeout",
        "rpcCode": rpc_code,
        "possibleSideEffect": True,
    }
    if key:
        _download_receipt_store(agent)[key] = receipt
    return receipt


def _reusable_download_response(agent: Any, params: JsonDict) -> Optional[JsonDict]:
    key = _download_operation_key(params)
    store = _download_receipt_store(agent)
    receipt = store.get(key) if key else None
    requested_url = str(params.get("url") or "").strip()
    # An uncertain redirect side effect is URL-scoped, not path-scoped: merely
    # changing savePath must not let the model re-dispatch the same URL and
    # create another file in the browser's default download directory.
    unverified = next(
        (
            item for item in store.values()
            if isinstance(item, dict)
            and str(item.get("state") or "") == "timeout_unverified"
            and str(item.get("url") or "").strip() == requested_url
        ),
        None,
    )
    if (
        isinstance(unverified, dict)
        and str((receipt or {}).get("state") or "") != "completed"
    ):
        receipt = unverified
    # Active receipts are observations from an earlier instant.  Reusing them
    # forever can make a stalled/failed operation impossible to retry; callers
    # must refresh those by downloadId through Download.list first.
    if not isinstance(receipt, dict):
        return None
    state = str(receipt.get("state") or "")
    if state == "timeout_unverified":
        return {
            "error": "A prior Download.start for this exact URL/savePath timed out with an unverified side effect.",
            "downloadReconciliation": {
                "classification": "timeout_unverified",
                "receipt": dict(receipt),
            },
            "suggested_prompt": (
                "Do not resend the same URL. The redirected file may already"
                " exist in the browser's default download directory. Obtain"
                " the final direct file URL before one bounded retry."
            ),
        }
    if state != "completed":
        return None
    return {
        "observation": "Reused an existing reconciled download operation.",
        "data": {
            "success": True,
            "downloadId": receipt.get("downloadId"),
            "state": receipt.get("state"),
            "savePath": receipt.get("savePath"),
            "url": receipt.get("url"),
            "reused": True,
        },
        "downloadReconciliation": {
            "classification": "already_started",
            "receipt": dict(receipt),
        },
    }


async def _refresh_active_download_response(
    agent: Any,
    runner: Any,
    params: JsonDict,
) -> Optional[JsonDict]:
    """Refresh an old active receipt before deciding whether to retry."""
    key = _download_operation_key(params)
    receipt = _download_receipt_store(agent).get(key) if key else None
    if not isinstance(receipt, dict) or str(receipt.get("state") or "") not in {
        "downloading", "paused",
    }:
        return None
    download_id = str(receipt.get("downloadId") or "").strip()
    fleet_id = str(getattr(agent, "assigned_fleet_id", "") or "").strip()
    if not download_id or not fleet_id:
        if key:
            _download_receipt_store(agent).pop(key, None)
        return None
    try:
        listed, _recovery = await runner.call(
            "Download.list",
            {
                "fleetId": fleet_id,
                "downloadId": download_id,
                "limit": 1,
                "purpose": "Refresh an existing download before retrying it",
            },
        )
    except ABCPTransportError:
        # A failed refresh does not prove the old operation is gone.  Surface
        # uncertainty rather than dispatching a duplicate side effect.
        return {
            "error": "Existing download state could not be refreshed.",
            "downloadReconciliation": {
                "classification": "active_unverified",
                "receipt": dict(receipt),
            },
            "suggested_prompt": (
                "Do not retry this Download.start until Download.list can"
                " confirm the prior operation's terminal state."
            ),
        }
    records = [
        row for row in _download_records(listed)
        if str(row.get("id") or row.get("downloadId") or "") == download_id
    ]
    if len(records) != 1:
        if key:
            _download_receipt_store(agent).pop(key, None)
        return None
    refreshed = _remember_download_record(
        agent,
        {**records[0], "operationKey": key},
    )
    state = str(refreshed.get("state") or "")
    if state not in {"downloading", "paused", "completed"}:
        if key:
            _download_receipt_store(agent).pop(key, None)
        return None
    return {
        "observation": "Refreshed and reused an existing download operation.",
        "data": {
            "success": True,
            "downloadId": refreshed.get("downloadId"),
            "state": state,
            "savePath": refreshed.get("savePath"),
            "url": refreshed.get("url"),
            "reused": True,
        },
        "downloadReconciliation": {
            "classification": "already_started",
            "receipt": dict(refreshed),
        },
    }


def _download_start_timed_out(response: Any) -> bool:
    if isinstance(response, ABCPTransportError):
        return getattr(response, "rpc_code", None) == -32014
    if not isinstance(response, dict):
        return False

    candidates: List[Any] = [response]
    nested = response.get("response")
    if isinstance(nested, dict):
        candidates.append(nested)
    for candidate in candidates:
        error = candidate.get("error") if isinstance(candidate, dict) else None
        if isinstance(error, dict) and error.get("code") == -32014:
            return True
    return False


def _classify_download_reconciliation(
    *,
    params: JsonDict,
    list_response: Any,
) -> JsonDict:
    url = str(params.get("url") or "").strip()
    save_path = str(params.get("savePath") or "").strip()
    matches = [
        row for row in _download_records(list_response)
        if str(row.get("url") or "").strip() == url
        and str(row.get("savePath") or "").strip() == save_path
    ]
    if len(matches) > 1:
        return {"classification": "ambiguous", "matches": matches}
    if not matches:
        return {"classification": "not_observed", "matches": []}
    record = matches[0]
    state = str(record.get("state") or "")
    classification = (
        "completed" if state == "completed"
        else "active" if state in {"downloading", "paused"}
        else "failed" if state in {"failed", "cancelled"}
        else "ambiguous"
    )
    return {"classification": classification, "matches": [record]}


async def _reconcile_download_start_timeout(
    *,
    agent: Any,
    runner: Any,
    params: JsonDict,
    timeout_error: Optional[ABCPTransportError] = None,
) -> JsonDict:
    """Reconcile a possibly-side-effecting timeout without blind retry.

    Download records are created asynchronously by Electron's will-download
    hook and can appear a few seconds after the RPC timeout.  Only an exact
    requested URL/path match is authoritative here.  Redirected orphan records
    are deliberately not claimed by time proximity because concurrent workers
    (or a human) may download in the same Fleet.
    """
    fleet_id = str(getattr(agent, "assigned_fleet_id", "") or "").strip()
    rpc_code = getattr(timeout_error, "rpc_code", None)
    if not fleet_id:
        receipt = _remember_unverified_download_timeout(
            agent, params, rpc_code=rpc_code,
        )
        return {
            "classification": "timeout_unverified",
            "matches": [],
            "reason": "assigned_fleet_id_unavailable",
            "receipt": receipt,
        }

    last_result: JsonDict = {
        "classification": "not_observed",
        "matches": [],
    }
    observations: List[JsonDict] = []
    for check_index in range(2):
        if check_index:
            await asyncio.sleep(DOWNLOAD_TIMEOUT_RECONCILIATION_DELAY_SECONDS)
        try:
            list_response, _list_recovery = await runner.call(
                "Download.list",
                {
                    "fleetId": fleet_id,
                    "limit": 100,
                    "purpose": (
                        "Reconcile whether a timed-out Download.start already"
                        " produced the exact requested browser-side operation"
                    ),
                },
            )
        except ABCPTransportError as exc:
            observations.append({
                "check": check_index + 1,
                "classification": "list_failed",
                "error": str(exc),
            })
            last_result = {
                "classification": "ambiguous",
                "matches": [],
                "reason": "download_list_failed",
                "error": str(exc),
            }
            continue
        last_result = _classify_download_reconciliation(
            params=params,
            list_response=list_response,
        )
        observations.append({
            "check": check_index + 1,
            "classification": last_result.get("classification"),
            "matchCount": len(last_result.get("matches") or []),
        })
        if last_result.get("classification") in {
            "completed", "active", "failed", "ambiguous",
        }:
            break

    last_result = dict(last_result)
    last_result["checks"] = observations
    matches = last_result.get("matches") or []
    if len(matches) == 1 and isinstance(matches[0], dict):
        record = {**matches[0], "operationKey": _download_operation_key(params)}
        last_result["receipt"] = _remember_download_record(agent, record)
    elif last_result.get("classification") in {"not_observed", "ambiguous"}:
        last_result["classification"] = "timeout_unverified"
        last_result["reason"] = (
            last_result.get("reason") or "exact_operation_not_observed"
        )
        last_result["receipt"] = _remember_unverified_download_timeout(
            agent, params, rpc_code=rpc_code,
        )
    return last_result


def _result_occlusion_blocked(result: Any) -> bool:
    """True when an action failed specifically because an overlay occluded the
    target. Distinct from generic failure: an occluded load-more is recoverable
    (dismiss the overlay and retry), not exhaustion."""
    if not isinstance(result, dict):
        return False
    classification = result.get("errorClassification")
    return isinstance(classification, dict) and classification.get("type") == "occlusion_blocked"


def _layers_from_result(result: JsonDict) -> List[JsonDict]:
    data = _response_data(result)
    layers = data.get("layers")
    return [layer for layer in layers if isinstance(layer, dict)] if isinstance(layers, list) else []


def _viewport_from_layers(layers: List[JsonDict]) -> JsonDict:
    for layer in layers:
        if layer.get("isMainFrame"):
            bounds = layer.get("viewportBounds")
            if isinstance(bounds, dict):
                return bounds
    for layer in layers:
        bounds = layer.get("viewportBounds")
        if isinstance(bounds, dict):
            return bounds
    return {}


def _log_dismiss_overlay(
    agent: Any,
    page_id: str,
    status: str,
    overlay: Optional[JsonDict],
    attempts: List[JsonDict],
) -> None:
    logger = getattr(agent, "logger", None)
    if logger is not None:
        logger.write(
            "dismiss_overlay.result",
            {
                "pageId": page_id,
                "status": status,
                "subtype": (overlay or {}).get("subtype"),
                "attemptCount": len(attempts),
                "attempts": attempts,
            },
        )


def _repair_identity_text(value: Any) -> str:
    return str(value).strip() if value is not None else ""


def _repair_visual_target_signature(identity: Any, field: Any) -> str:
    identity_field = (
        str(identity.get("field") or "").strip()
        if isinstance(identity, dict) else ""
    )
    identity_value = (
        _repair_identity_text(identity.get("value"))
        if isinstance(identity, dict) else ""
    )
    return json.dumps(
        [identity_field, identity_value, str(field or "").strip()],
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _normalized_repair_page(url: Any) -> Tuple[str, str]:
    """Normalize a repair evidence URL to its stable host/path destination."""
    raw = str(url or "").strip()
    try:
        parsed = urlparse(raw)
    except ValueError:
        return "", raw.rstrip("/")
    host = parsed.netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    path = (parsed.path or "/").rstrip("/") or "/"
    return host, path


def _repair_page_binding(raw: Any) -> Optional[JsonDict]:
    if not isinstance(raw, dict):
        return None
    field = str(raw.get("field") or "").strip()
    url = str(raw.get("url") or "").strip()
    host, _ = _normalized_repair_page(url)
    if not field or not host:
        return None
    return {"field": field, "url": url}


def _validated_repair_visual_targets(
    agent: Any,
    raw_targets: Any,
) -> Tuple[List[JsonDict], Optional[JsonDict]]:
    if raw_targets in (None, []):
        return [], None
    if not isinstance(raw_targets, list):
        return [], {
            "status": "rejected",
            "error": "visual_verify.repair_targets must be an array",
            "tool_was_executed": False,
        }
    contract = getattr(agent, "worker_contract", None)
    manifest = (
        contract.get("_repair_manifest") if isinstance(contract, dict) else None
    )
    repairs = manifest.get("repairs") if isinstance(manifest, dict) else None
    if not isinstance(repairs, list) or not repairs:
        return [], {
            "status": "rejected",
            "error": "repair_targets require an active repair manifest",
            "tool_was_executed": False,
        }
    allowed: Dict[Tuple[str, str], Set[str]] = {}
    identity_values: Dict[Tuple[str, str], Any] = {}
    page_bindings: Dict[Tuple[str, str], JsonDict] = {}
    for item in repairs:
        identity = item.get("identity") if isinstance(item, dict) else None
        identity_field = (
            str(identity.get("field") or "").strip()
            if isinstance(identity, dict) else ""
        )
        identity_value = (
            _repair_identity_text(identity.get("value"))
            if isinstance(identity, dict) else ""
        )
        fields = item.get("fields") if isinstance(item, dict) else None
        if identity_field and identity_value and isinstance(fields, list):
            key = (identity_field, identity_value)
            allowed[key] = {
                str(field).strip() for field in fields if str(field).strip()
            }
            identity_values[key] = identity.get("value")
            page_binding = _repair_page_binding(item.get("pageBinding"))
            if page_binding is not None:
                page_bindings[key] = page_binding

    normalized: List[JsonDict] = []
    seen_signatures: Set[str] = set()
    for index, raw_target in enumerate(raw_targets):
        identity = raw_target.get("identity") if isinstance(raw_target, dict) else None
        identity_field = (
            str(identity.get("field") or "").strip()
            if isinstance(identity, dict) else ""
        )
        identity_value = (
            _repair_identity_text(identity.get("value"))
            if isinstance(identity, dict) else ""
        )
        fields = raw_target.get("fields") if isinstance(raw_target, dict) else None
        target_fields = sorted({
            str(field).strip() for field in fields if str(field).strip()
        }) if isinstance(fields, list) else []
        key = (identity_field, identity_value)
        if (
            key not in allowed
            or not target_fields
            or any(field not in allowed[key] for field in target_fields)
        ):
            return [], {
                "status": "rejected",
                "error": (
                    f"visual_verify.repair_targets[{index}] must match one"
                    " manifest identity and its repair fields"
                ),
                "tool_was_executed": False,
            }
        fresh_fields = []
        for field in target_fields:
            signature = _repair_visual_target_signature(identity, field)
            if signature in seen_signatures:
                continue
            seen_signatures.add(signature)
            fresh_fields.append(field)
        if fresh_fields:
            target = {
                "identity": {
                    "field": identity_field,
                    "value": identity_values[key],
                },
                "fields": fresh_fields,
            }
            if key in page_bindings:
                target["pageBinding"] = dict(page_bindings[key])
            normalized.append(target)
    return normalized, None


async def _verify_repair_visual_page(
    agent: Any,
    page_id: str,
    targets: List[JsonDict],
    step: int,
) -> Tuple[JsonDict, Optional[JsonDict]]:
    target_bindings = [
        _repair_page_binding(target.get("pageBinding")) for target in targets
    ]
    bindings = [binding for binding in target_bindings if binding is not None]
    if not bindings:
        return {"status": "unavailable"}, None
    if len(bindings) != len(targets):
        return {"status": "mixed_bindings"}, {
            "status": "rejected",
            "error": (
                "repair_targets mix page-bound and unbound rows; verify them"
                " in separate visual_verify calls"
            ),
            "tool_was_executed": False,
        }

    destinations = {
        _normalized_repair_page(binding["url"]) for binding in bindings
    }
    if len(destinations) != 1:
        return {"status": "conflicting_targets"}, {
            "status": "rejected",
            "error": (
                "repair_targets resolve to different pages; verify each page"
                " in a separate visual_verify call"
            ),
            "tool_was_executed": False,
        }

    expected_urls = sorted({binding["url"] for binding in bindings})
    state = await _invoke_browser_method(
        agent,
        "Page.getState",
        {
            "pageId": page_id,
            "purpose": "Bind repair absence evidence to its expected baseline page",
        },
        step,
    )
    data = _response_data(state)
    current_url = str(data.get("url") or data.get("currentUrl") or "").strip()
    binding_result = {
        "status": "unverified",
        "expectedUrls": expected_urls,
        "currentUrl": current_url,
    }
    if not current_url:
        return binding_result, {
            "status": "repair_visual_page_unverified",
            "error": "Page.getState did not return a URL for repair evidence",
            "expectedPageUrls": expected_urls,
            "tool_was_executed": True,
            "next_instruction": (
                "Re-establish the target page and retry visual_verify; repair"
                " absence evidence cannot be attached without a current URL."
            ),
        }
    if _normalized_repair_page(current_url) not in destinations:
        binding_result["status"] = "mismatch"
        return binding_result, {
            "status": "repair_visual_wrong_page",
            "error": "visual repair evidence was requested on the wrong page",
            "expectedPageUrls": expected_urls,
            "currentUrl": current_url,
            "tool_was_executed": True,
            "next_instruction": (
                "Navigate or switch to the manifest-bound target page, confirm"
                " it with Page.getState, then retry visual_verify."
            ),
        }
    binding_result["status"] = "matched"
    return binding_result, None


def _record_repair_visual_evidence(
    agent: Any,
    targets: List[JsonDict],
    result: JsonDict,
    *,
    question: str,
) -> List[JsonDict]:
    if (
        not targets
        or str(result.get("status") or "") != "done"
        or str(result.get("verdict") or "").strip().lower() != "absent"
    ):
        return []
    has_page_binding = any(
        _repair_page_binding(target.get("pageBinding")) is not None
        for target in targets
    )
    page_binding = result.get("repairPageBinding")
    if has_page_binding and (
        not isinstance(page_binding, dict)
        or page_binding.get("status") != "matched"
    ):
        return []
    contract = getattr(agent, "worker_contract", None)
    manifest = (
        contract.get("_repair_manifest") if isinstance(contract, dict) else None
    )
    if not isinstance(manifest, dict):
        return []
    satisfied = manifest.get("visualEvidenceSatisfied")
    if not isinstance(satisfied, dict):
        satisfied = {}
        manifest["visualEvidenceSatisfied"] = satisfied
    recorded: List[JsonDict] = []
    for target in targets:
        identity = target.get("identity")
        for field in target.get("fields") or []:
            signature = _repair_visual_target_signature(identity, field)
            evidence = {
                "identity": dict(identity) if isinstance(identity, dict) else {},
                "field": str(field),
                "signature": signature,
                "screenshotPath": str(result.get("screenshotPath") or ""),
                "verdict": "absent",
                "question": question[:500],
            }
            if isinstance(page_binding, dict):
                evidence["pageBinding"] = dict(page_binding)
            satisfied[signature] = evidence
            recorded.append(evidence)
    if recorded:
        pending = manifest.get("visualEvidencePending")
        recorded_signatures = {item["signature"] for item in recorded}
        if isinstance(pending, list):
            remaining = [
                item for item in pending
                if isinstance(item, dict)
                and str(item.get("signature") or "") not in recorded_signatures
            ]
            if remaining:
                manifest["visualEvidencePending"] = remaining
            else:
                manifest.pop("visualEvidencePending", None)
        agent.logger.write("repair.visual_evidence_satisfied", {
            "targets": recorded,
        })
    return recorded


async def _visual_verify(agent: Any, tool_input: JsonDict, step: int) -> JsonDict:
    vl_config = getattr(agent.runtime.harness, "vl", None)
    if vl_config is None or not getattr(vl_config, "enabled", False):
        return {
            "status": "disabled",
            "reason": "vl.enabled is false or vl config is missing",
        }
    raw_max_checks = optional_int(
        getattr(vl_config, "max_checks_per_worker", 2),
        2,
    )
    max_checks = max(0, raw_max_checks if raw_max_checks is not None else 2)
    page_id = str(tool_input.get("pageId") or "").strip()
    if not page_id:
        return {"status": "failed", "error": "pageId is required"}
    selector = str(tool_input.get("selector") or "").strip()
    element_id = str(tool_input.get("id") or "").strip()
    requested_mode = str(tool_input.get("mode") or "action_outcome").strip()
    mode = requested_mode
    question = str(tool_input.get("question") or "").strip()
    repair_targets, repair_target_error = _validated_repair_visual_targets(
        agent, tool_input.get("repair_targets"),
    )
    if repair_target_error is not None:
        return repair_target_error
    # Target-bound repair evidence is a machine-enforced completion gate, so an
    # earlier overlay/layout check must not exhaust its budget. It uses the
    # separate forced counter and remains bounded by the worker's step limit.
    force_check = bool(tool_input.get("_force", False)) or bool(repair_targets)
    if not force_check and getattr(agent, "vl_check_count", 0) >= max_checks:
        return {
            "status": "rejected",
            "reason": "vl_check_limit_reached",
            "maxChecksPerWorker": max_checks,
            "next_instruction": (
                "Do not keep using screenshots. Use DOM/Runtime evidence or"
                " finalize with the blocker."
            ),
        }
    if repair_targets:
        mode = "repair_absence"
        question = (
            f"{question}\nRepair evidence targets: "
            f"{json.dumps(repair_targets, ensure_ascii=False, default=str)}. "
            "Determine whether the expected content for these exact fields is"
            " absent on the current page."
        ).strip()
    expected = tool_input.get("expected")
    if not isinstance(expected, dict):
        expected = {}
    elif repair_targets:
        expected = dict(expected)
    if repair_targets:
        expected["repair_targets"] = repair_targets
    full_page = bool(tool_input.get("fullPage", False))

    repair_page_binding: JsonDict = {"status": "not_applicable"}
    if repair_targets:
        repair_page_binding, page_binding_error = await _verify_repair_visual_page(
            agent, page_id, repair_targets, step,
        )
        if page_binding_error is not None:
            agent.logger.write("repair.visual_page_rejected", {
                **page_binding_error,
                "pageId": page_id,
                "repairTargets": repair_targets,
            })
            return page_binding_error

    screenshot_params: JsonDict = {
        "pageId": page_id,
        "fullPage": full_page,
        "options": {"format": "file"},
        "purpose": f"Visual verification for {mode or 'action_outcome'}",
    }
    if selector:
        screenshot_params["selector"] = selector
    if element_id:
        screenshot_params["id"] = element_id
    stale_target = _check_stale_axtree_target(
        agent,
        "Page.screenshot",
        screenshot_params,
    )
    if stale_target is not None:
        return stale_target

    screenshot_scope = (
        "element" if (selector or element_id)
        else ("fullpage" if full_page else "viewport")
    )
    before_artifacts = set(str(path) for path in getattr(agent, "artifacts", []))
    screenshot = await _invoke_browser_method(
        agent,
        "Page.screenshot",
        screenshot_params,
        step,
    )
    image_path = _screenshot_saved_path(screenshot)
    if not image_path:
        after_artifacts = [
            str(path) for path in getattr(agent, "artifacts", [])
            if str(path) not in before_artifacts
        ]
        image_path = after_artifacts[-1] if after_artifacts else ""
    if not image_path and (selector or element_id or full_page):
        # skillsGuide §5: if element capture fails, do not repeat it — resync
        # once with Page.getState, then fall back to a viewport screenshot. The
        # verdict consumer sees screenshotScope so it knows the crop widened.
        #
        # `full_page` takes the same road for a different reason: the platform
        # serves it from CDP `Page.captureScreenshot{captureBeyondViewport}`,
        # which fails on a page taller than the compositor will surface. That
        # is not an edge case — across two live canaries 113 of 114 full-page
        # captures failed, and the single success was a freshly loaded page
        # before the worker expanded anything. So the capture worked only
        # while there was nothing worth looking at, and failed on exactly the
        # content-heavy pages a stuck worker needs to see. A viewport shot is
        # bounded by construction, and the reality check scrolls its region
        # into view first, so the narrower frame is also the better-aimed one.
        await _invoke_browser_method(
            agent,
            "Page.getState",
            {
                "pageId": page_id,
                "purpose": "Resync page state after element screenshot failed before viewport fallback",
            },
            step,
        )
        fallback_params: JsonDict = {
            "pageId": page_id,
            "fullPage": False,
            "options": {"format": "file"},
            "purpose": (
                "Viewport fallback after full-page screenshot failure"
                if full_page and not (selector or element_id)
                else "Viewport fallback after element screenshot failure"
            ),
        }
        before_artifacts = set(str(path) for path in getattr(agent, "artifacts", []))
        screenshot = await _invoke_browser_method(
            agent,
            "Page.screenshot",
            fallback_params,
            step,
        )
        image_path = _screenshot_saved_path(screenshot)
        if not image_path:
            after_artifacts = [
                str(path) for path in getattr(agent, "artifacts", [])
                if str(path) not in before_artifacts
            ]
            image_path = after_artifacts[-1] if after_artifacts else ""
        if image_path:
            screenshot_scope = "viewport_fallback"
    if not image_path:
        return {
            "status": "failed",
            "error": "screenshot did not produce a saved image path",
            "screenshot": agent._trim_for_model(screenshot),
        }

    if force_check:
        agent.vl_force_check_count = getattr(agent, "vl_force_check_count", 0) + 1
    else:
        agent.vl_check_count = getattr(agent, "vl_check_count", 0) + 1
    verdict = await visual_verify_image(
        config=vl_config,
        image_path=image_path,
        expected=expected,
        mode=mode,
        question=question,
    )
    # VL Role A: promote a located pixel back to a durable canonical id (bbox→id),
    # so the agent acts on a stable handle instead of raw coordinates. Gated by
    # vl.visual_locate_enabled; best-effort (any failure leaves the raw point).
    if (
        mode == "visual_locate"
        and isinstance(verdict, dict)
        and verdict.get("verdict") == "located"
        and verdict.get("point")
        and bool(getattr(vl_config, "visual_locate_enabled", False))
    ):
        verdict = await _promote_visual_locate(
            agent, page_id, image_path, verdict, step,
            expected_text=" ".join(
                part for part in (question, str(expected.get("target") or ""))
                if part
            ),
        )
    vl_check_count = getattr(agent, "vl_check_count", 0)
    vl_force_check_count = getattr(agent, "vl_force_check_count", 0)
    result = {
        **verdict,
        "mode": mode,
        "screenshotPath": image_path,
        "screenshotScope": screenshot_scope,
        "selector": selector or None,
        "id": element_id or None,
        "vlCheckCount": vl_check_count,
        "vlForceCheckCount": vl_force_check_count,
        "maxChecksPerWorker": max_checks,
        "forced": force_check,
        "usage_boundary": (
            "visual_verify is evidence for action/state verification only;"
            " do not use it as final structured extraction."
        ),
    }
    if repair_targets:
        result["repairPageBinding"] = repair_page_binding
        if requested_mode != mode:
            result["requestedMode"] = requested_mode
    repair_evidence = _record_repair_visual_evidence(
        agent,
        repair_targets,
        result,
        question=question,
    )
    if repair_targets:
        result["repairTargets"] = repair_targets
    if repair_evidence:
        result["repairEvidenceSatisfied"] = repair_evidence
    elif repair_targets and result.get("status") == "done":
        verdict_name = str(result.get("verdict") or "uncertain")
        if verdict_name == "present":
            result["status"] = "repair_visual_contradiction"
            result["next_instruction"] = (
                "The visual check found the target content present. Do not mark"
                " it confirmed_absent; extract the visible value and submit a"
                " non-empty repair patch instead."
            )
            event_type = "repair.visual_evidence_contradicted"
        else:
            result["status"] = "repair_visual_inconclusive"
            result["next_instruction"] = (
                "The screenshot did not prove absence. Reframe or expand the"
                " relevant page region and retry, or leave the repair unresolved."
            )
            event_type = "repair.visual_evidence_inconclusive"
        agent.logger.write(event_type, {
            "pageId": page_id,
            "verdict": verdict_name,
            "repairTargets": repair_targets,
            "screenshotPath": image_path,
        })
    agent.logger.write(
        "vl.visual_verify",
        {
            key: value
            for key, value in result.items()
            if key not in {"visible_evidence"}
        },
    )
    return result


def _arbiter_error_text(result: JsonDict) -> str:
    """Pull a failure string from a browser_call result (else '')."""
    if not isinstance(result, dict):
        return ""
    if result.get("error"):
        return str(result["error"])
    response = result.get("response")
    if isinstance(response, dict):
        if response.get("error"):
            return str(response["error"])
        obs = response.get("observation")
        if isinstance(obs, str) and "fail" in obs.lower():
            return obs
    return ""


def _arbiter_next_instruction(rec: JsonDict) -> str:
    action = rec.get("action")
    if action == "retry_by_id":
        return (f"VL arbiter located the target and promoted it to durable id"
                f" {rec.get('id')!r}. Retry the failed action targeting that id"
                f" (a durable handle — not coordinates).")
    if action == "hitl":
        return (f"VL arbiter assessment: {rec.get('reason', 'needs human/challenge handling')}."
                f" Take the HITL/challenge path instead of retrying blindly.")
    if action == "dismiss":
        label = rec.get("label")
        return (f"VL arbiter found a safe dismiss control{(' (' + str(label) + ')') if label else ''}."
                f" Dismiss the overlay, then retry the action.")
    if action == "coordinate":
        return ("VL arbiter located the target but no AXTree node covers it; if safe and"
                " not consequential, use one coordinate action at cssPoint (never persist coordinates).")
    if action == "reperceive":
        return "VL arbiter suggests re-perceiving: refresh Page.getState + DOM.getAXTree before retrying."
    return ""


async def _maybe_vl_arbitrate(
    agent: Any,
    method: str,
    params: JsonDict,
    result: JsonDict,
    step: int,
) -> JsonDict:
    """Role D auto-trigger: on a visually-related failure, route to the VL arbiter
    and attach a recovery recommendation. Best-effort + gated (vl.arbiter_enabled);
    bounded per worker by max_checks_per_worker. Never raises into the call path."""
    if not isinstance(result, dict):
        return result
    vl_config = getattr(getattr(getattr(agent, "runtime", None), "harness", None), "vl", None)
    if (vl_config is None or not getattr(vl_config, "enabled", False)
            or not getattr(vl_config, "arbiter_enabled", False)):
        return result
    error_text = _arbiter_error_text(result)
    if not error_text:
        return result
    classification = ""
    cl = result.get("errorClassification")
    if isinstance(cl, dict):
        classification = str(cl.get("type") or "")
    page_id = str((params or {}).get("pageId") or "")
    browser = getattr(agent, "browser", None)
    if not page_id or browser is None:
        return result
    # bound the number of arbiter VL calls per worker
    max_checks = optional_int(getattr(vl_config, "max_checks_per_worker", 2), 2) or 2
    if getattr(agent, "vl_arbiter_count", 0) >= max_checks:
        return result
    try:
        from harness.vl.arbiter import arbitrate, is_visual_failure
        if not is_visual_failure(classification, error_text):
            return result
        agent.vl_arbiter_count = getattr(agent, "vl_arbiter_count", 0) + 1
        rec = await arbitrate(
            browser, page_id, classification_type=classification, error_text=error_text,
            target_description=str((params or {}).get("purpose") or ""),
            vl_config=vl_config, logger=getattr(agent, "logger", None),
        )
    except Exception as exc:  # arbitration must never break the call path
        logger = getattr(agent, "logger", None)
        if logger is not None:
            logger.write("vl.arbiter.error", {"method": method, "error": str(exc)})
        return result
    if not isinstance(rec, dict) or rec.get("action") in (None, "none"):
        return result
    out = {**result, "vlArbiter": rec}
    instruction = _arbiter_next_instruction(rec)
    if instruction:
        out["next_instruction"] = instruction
    return out


def _reality_check_region(tool_input: JsonDict) -> JsonDict:
    """The region the failing tool was actually working on.

    Read off the caller's own params rather than any list of known page
    structures: a container/selector/id the worker passed IS its declaration of
    where it expected the content, and it is the only region the harness can
    honestly name. No locator means no region, and the check stays page-scoped.
    """
    region: JsonDict = {}
    container = tool_input.get("container")
    if isinstance(container, dict):
        for key in ("id", "selector"):
            value = str(container.get(key) or "").strip()
            if value:
                region[key] = value
    for source, key in (
        ("containerId", "id"),
        ("containerSelector", "selector"),
        ("id", "id"),
        ("selector", "selector"),
    ):
        if key in region:
            continue
        value = str(tool_input.get(source) or "").strip()
        if value:
            region[key] = value
    # A human-readable hint for the prompt, never a decision: the model cannot
    # see a selector, so whatever the caller wrote about what it was looking
    # for describes the region better. `name` is last because it is ambiguous
    # across tools (an accessible-name query in find_in_axtree, an artifact
    # name in record_extraction) — useful as a hint, wrong as a source of truth.
    for key in ("purpose", "query", "text", "name"):
        description = str(tool_input.get(key) or "").strip()
        if description:
            region["description"] = description[:200]
            break
    return region


def _region_hint_text(region: JsonDict) -> str:
    """Human-readable region for the VL prompt, preferring the worker's own
    words over a selector the model cannot see anyway."""
    description = str(region.get("description") or "").strip()
    if description:
        return description
    selector = str(region.get("selector") or "").strip()
    if selector:
        return f"the page section matching {selector}"
    return ""


async def _scroll_region_into_view(
    agent: Any,
    page_id: str,
    region: JsonDict,
    step: int,
) -> JsonDict:
    """Bring the region into the root viewport before capturing it.

    Uses Input.scroll target mode, whose receipt answers the one question a
    screenshot cannot: was the thing we are about to ask about actually in
    frame. A failure here is not fatal — it just leaves the capture unproven,
    which downgrades what the verdict may be used for rather than blocking it.
    """
    locator = {
        key: region[key] for key in ("id", "selector") if region.get(key)
    }
    if not locator:
        return {}
    return await _invoke_browser_method(
        agent,
        "Input.scroll",
        {
            "pageId": page_id,
            "target": locator,
            "purpose": "reality check: bring the region into view before capture",
        },
        step,
    )


def _reality_check_summary(row: JsonDict) -> JsonDict:
    """What the worker sees of the check.

    Carries the standing fields (`evidenceGrade`, `mayTerminate`, whether the
    region was provably in frame) alongside the observation, so a model reading
    only the tool result — never the persisted artifact — still sees that this
    is an assertion and what it may be used for.
    """
    summary: JsonDict = {
        "verdict": row.get("verdict"),
        "observation": row.get("observation"),
        "screenshotPath": row.get("screenshotPath"),
        "targetShortfallStreak": row.get("targetShortfallStreak"),
        "evidenceGrade": row.get("evidenceGrade"),
        "mayTerminate": row.get("mayTerminate"),
        "claimScope": row.get("claimScope"),
    }
    for key in ("rowKey", "verdictClass", "claimedClass", "overrideReason",
                "regionInCapture", "itemCount", "armedBy",
                "turnsSinceArtifactProgress"):
        if key in row:
            summary[key] = row[key]
    return summary


def _page_reality_check_instruction(evidence_path: str) -> str:
    """Instruction for the page-scoped fallback (no assigned row matched this
    URL — a listing page, or a contract carrying no row keys).

    The verdict is free-form here, so the worker does the comparing. What the
    harness must still say is what the verdict is WORTH: the old wording told
    the worker to declare target_absent citing this artifact, which contradicts
    the mayTerminate=False the same artifact records and walks straight into
    the spawner's visual-evidence-only rejection.
    """
    return (
        "A visual reality check ran because perception kept falling short of"
        " the task target. It is an advisory model reading of one screenshot,"
        " not a measurement, and it cannot close anything on its own: if it"
        " shows the content somewhere on the page, adjust your perception"
        " (scroll/selector) and go read it; if it agrees the content is not"
        " there, that is a reason to verify mechanically — materialize the"
        " region, enumerate it to exhaustion, calibrate your selector against"
        " a page where it does match — not a reason to stop. When you do"
        " report a blocker, cite what you actually observed alongside"
        f" {evidence_path or 'the reality-check artifact'}; a citation naming"
        " only this artifact is rejected."
    )


def _reality_check_instruction(
    *,
    reconciled: Optional[JsonDict],
    grading: Optional[JsonDict],
    capture: JsonDict,
    evidence_path: str,
) -> str:
    """What the worker should do with this verdict, given its standing.

    Deliberately asymmetric. "There is content here" always redirects work and
    is stated as an instruction. Everything else is reported as an observation
    that does not close anything, because an advisory model claim that ends a
    row is the failure this whole path exists to prevent.
    """
    from harness.vl.capture_geometry import (
        CAPTURE_DISPROVEN,
        CLASS_AUTH_OVERLAY,
        CLASS_CONTENT_PRESENT,
        CLASS_EXPLICIT_EMPTY,
        CLASS_REGION_NOT_IN_CAPTURE,
    )

    if not reconciled or not grading:
        return _page_reality_check_instruction(evidence_path)
    resolved = str(reconciled.get("class") or "")
    citation = evidence_path or "the reality-check artifact"
    if resolved == CLASS_CONTENT_PRESENT:
        return (
            "A visual check reports that the region DOES hold content. Do not"
            " declare absence for it. Re-read that region — refresh"
            " DOM.getAXTree, then extract from the container the check"
            " describes."
        )
    if resolved == CLASS_REGION_NOT_IN_CAPTURE:
        detail = (
            " The scroll receipt confirms the region was not in the captured"
            " frame, so this says nothing about whether the content exists."
            if str(capture.get("state") or "") == CAPTURE_DISPROVEN else
            " This says only that the region was not visible in this capture."
        )
        return (
            "The visual check could not see the region." + detail
            + " Materialize it first (open the tab/accordion that owns it,"
            " scroll it into view, or wait for it to load) and observe again."
            " Do not report absence from this."
        )
    if resolved == CLASS_AUTH_OVERLAY:
        return (
            "The visual check reports a login/paywall overlay over this page."
            " That is a fact about THIS page epoch, not about the content"
            " behind it and not about any other item: run the safe dismiss"
            " ladder, re-navigate, and re-observe before recording a blocker,"
            f" citing {citation}."
        )
    if resolved == CLASS_EXPLICIT_EMPTY:
        if not grading.get("directsWork"):
            return (
                "The visual check read an explicit empty state in the region."
                " This is an advisory model observation, not proof: it does not"
                " by itself satisfy confirmed_absent. To record the field as"
                " absent you still owe the mechanical obligations — the region"
                " materialized in this navigation epoch, the overlay clear, the"
                " selector calibrated against a peer that HAS content, and the"
                f" page's own empty-state text captured. Cite {citation}"
                " alongside them, never instead of them."
            )
        return (
            "The visual check read an explicit empty state in the region. Use"
            " it to corroborate a confirmed_absent declaration, and still"
            " discharge the mechanical obligations (region materialized this"
            " epoch, overlay clear, selector calibrated, empty-state text"
            f" captured), citing {citation}."
        )
    return (
        "The visual check was inconclusive about the region. It is not evidence"
        " of absence. Observe again after materializing the region, or record"
        " the outstanding obligations rather than a verdict."
    )


# How many captures may fail before this worker stops arming the check.
# Keeping the streak armed across a failure is right for a transient one and
# ruinous for a systematic one: live run d32a810d burned 53 scroll+screenshot
# round-trips retrying a capture that could never succeed on those pages. Two
# attempts distinguish the two cases; a third only pays for the diagnosis
# twice.
REALITY_CHECK_CAPTURE_FAILURE_LIMIT = 2


async def _maybe_reality_check(
    agent: Any,
    tool_call: JsonDict,
    result: JsonDict,
    step: int,
) -> JsonDict:
    """Layer-2 visual reality check: after a target-shortfall streak (tools
    keep yielding nothing OR yielding rows that never satisfy the phase
    contract — mis-attributed rows look productive while missing the target),
    auto-run a full-page screenshot + VL against a claim synthesized from the
    worker contract, persist the observation through record_extraction (so
    its savedPath is ledger-valid evidence for target_absent claims), and
    attach the verdict to the tool result. Task-type agnostic — the trigger
    is the streak, not any validator kind. Best-effort + gated; never raises
    into the path."""
    if not isinstance(result, dict):
        return result
    vl_config = getattr(
        getattr(getattr(agent, "runtime", None), "harness", None), "vl", None
    )
    if (
        vl_config is None
        or not getattr(vl_config, "enabled", False)
        or not getattr(vl_config, "reality_check_enabled", True)
    ):
        return result
    try:
        from harness.vl.reality_check import (
            artifact_stall_turns,
            build_reality_check_row,
            classify_target_yield,
            stall_armed,
            synthesize_claim,
        )
        name = str(tool_call.get("name") or "")
        tool_input = tool_call.get("input") or {}
        if not isinstance(tool_input, dict):
            tool_input = {}
        threshold = max(
            1,
            optional_int(
                getattr(vl_config, "reality_check_shortfall_threshold", 3), 3
            ) or 3,
        )
        stall_threshold = optional_int(
            getattr(vl_config, "reality_check_stall_turns", 15), 15
        )
        stall_threshold = 15 if stall_threshold is None else stall_threshold

        yield_state = classify_target_yield(name, result)
        if yield_state is False:
            agent.target_shortfall_streak = 0
            return result
        if yield_state is True:
            agent.target_shortfall_streak = (
                getattr(agent, "target_shortfall_streak", 0) + 1
            )

        # Two independent ways to be stuck, and the second one has no yield to
        # count: a worker looping on DOM.getAXTree / DOM.getSemanticTree /
        # local_fs_read produces nothing the shortfall streak can see, so
        # before this it could spend its whole budget with the streak at 0 and
        # the check never armed (observed live in task e3173b5b).
        armed_by = ""
        if getattr(agent, "target_shortfall_streak", 0) >= threshold:
            armed_by = "target_shortfall"
        elif stall_armed(agent, stall_threshold):
            armed_by = "artifact_stall"
        if not armed_by:
            return result
        if getattr(agent, "reality_check_count", 0) >= 1:
            return result
        if (
            getattr(agent, "reality_check_capture_failures", 0)
            >= REALITY_CHECK_CAPTURE_FAILURE_LIMIT
        ):
            # Perception is unavailable on this worker, not merely unhelpful.
            # Re-arming would keep spending the step budget on a capture that
            # has already proven it cannot land.
            return result
        page_id = str(tool_input.get("pageId") or "").strip()
        if not page_id:
            # The stall trigger fires on tools that carry no pageId at all
            # (local_fs_read, and any call made after the page moved on). The
            # last AXTree page is the surface the worker was actually reading,
            # so the check still has something to look at instead of being
            # dropped exactly when the worker is most lost.
            page_id = str(getattr(agent, "axtree_page_id", "") or "").strip()
        if not page_id:
            urls = getattr(agent, "page_urls", None)
            if isinstance(urls, dict) and urls:
                page_id = str(next(reversed(list(urls))) or "").strip()
        if not page_id:
            return result
        from harness.vl.capture_geometry import (
            evidence_grade,
            reconcile_region_verdict,
            region_in_capture,
            scroll_coverage,
        )
        from harness.vl.reality_check import (
            assigned_row_keys,
            build_row_scoped_claim,
            resolve_current_row,
        )

        contract = getattr(agent, "worker_contract", None)
        page_url = str(getattr(agent, "page_urls", {}).get(page_id) or "")
        row_key = resolve_current_row(
            page_url, assigned_row_keys(contract, getattr(agent, "phase", None)),
        ) or ""
        region = _reality_check_region(tool_input)
        # Scope the question to the item this page actually is. Asking a detail
        # page whether the whole cohort's expectation is met invites a truthful
        # "no" that means nothing about the field the worker is missing — the
        # 5324506f defect.
        if row_key:
            claim = build_row_scoped_claim(
                worker_contract=contract,
                row_key=row_key,
                region_hint=_region_hint_text(region),
            )
            mode = "region_reality"
        else:
            claim = synthesize_claim(contract)
            mode = "page_state"

        # Put the region in frame first, and keep the receipt: `targetVisible`
        # is the only mechanical answer to "was it in the picture?".
        scroll_result = (
            await _scroll_region_into_view(agent, page_id, region, step)
            if region else {}
        )
        coverage = scroll_coverage(scroll_result)
        # An element-bound crop is self-evidencing; without a locator the
        # full-page shot is the widest honest coverage available.
        capture_request: JsonDict = {
            "pageId": page_id,
            "mode": mode,
            "question": claim,
            "_force": True,
        }
        if region.get("id"):
            capture_request["id"] = region["id"]
        elif region.get("selector"):
            capture_request["selector"] = region["selector"]
        else:
            capture_request["fullPage"] = True
        verdict = await _visual_verify(agent, capture_request, step)
        if not isinstance(verdict, dict) or verdict.get("status") in {
            "disabled",
            "failed",
            "rejected",
        }:
            # Do NOT consume the per-worker budget on a failed capture — the
            # streak stays armed so a later shortfall can retry. But count the
            # failures: after REALITY_CHECK_CAPTURE_FAILURE_LIMIT the gate above
            # stops arming, because a capture that cannot land will not start
            # landing on the fifty-third try.
            failures = getattr(agent, "reality_check_capture_failures", 0) + 1
            agent.reality_check_capture_failures = failures
            logger = getattr(agent, "logger", None)
            if logger is not None and hasattr(logger, "write"):
                # Without this event the run log cannot distinguish "the check
                # never armed" from "the check armed and was blind" — and the
                # second is a far more serious statement about the run. It is
                # what actually happened in d32a810d, where the log showed
                # nothing at all.
                logger.write("vl.reality_check.capture_unavailable", {
                    "triggerTool": name,
                    "armedBy": armed_by,
                    "pageId": page_id,
                    "captureScope": (
                        "element" if (region.get("id") or region.get("selector"))
                        else "fullPage"
                    ),
                    "status": str(
                        (verdict or {}).get("status") or "no_verdict"
                    ) if isinstance(verdict, dict) else "no_verdict",
                    "error": str(
                        (verdict or {}).get("error") or ""
                    )[:300] if isinstance(verdict, dict) else "",
                    "consecutiveFailures": failures,
                    "armingDisabled": (
                        failures >= REALITY_CHECK_CAPTURE_FAILURE_LIMIT
                    ),
                })
            return result
        # A capture landed: the worker's perception is working, so an earlier
        # transient failure must not count toward the circuit breaker.
        agent.reality_check_capture_failures = 0
        capture = region_in_capture(
            region_declared=bool(region.get("id") or region.get("selector")),
            screenshot_scope=str(verdict.get("screenshotScope") or ""),
            coverage=coverage,
        )
        reconciled = reconcile_region_verdict(
            verdict.get("classification") or verdict.get("verdict"), capture,
        )
        grading = evidence_grade(
            evidence_mode=getattr(
                vl_config, "reality_check_evidence_mode", "advisory",
            ),
            resolved_class=reconciled.get("class"),
            capture=capture,
        )
        # The class taxonomy only exists in region_reality mode. On the
        # page-scoped fallback the verdict is free-form, so no class is
        # asserted and the worker does its own comparing.
        row_reconciled = reconciled if mode == "region_reality" else None
        row_grading = grading if mode == "region_reality" else None
        row = build_reality_check_row(
            claim=claim,
            verdict=verdict,
            trigger_tool=name,
            shortfall_streak=getattr(agent, "target_shortfall_streak", 0),
            armed_by=armed_by,
            stall_turns=artifact_stall_turns(agent),
            page_id=page_id,
            page_url=page_url,
            row_key=row_key,
            region=region,
            capture=capture,
            coverage=coverage,
            reconciled=row_reconciled,
            grading=row_grading,
        )
        record = _record_extraction(agent, {
            "name": "vl_reality_check",
            "rows": [row],
            "schema": {"source": "vl_reality_check"},
            "description": (
                "Automatic visual reality check triggered by a"
                " target-shortfall perception streak"
            ),
        })
        # The check ran: consume the budget either way. Re-arming on a
        # persist failure would burn an unbounded _force VL call per further
        # shortfall while the worker never sees the verdict.
        agent.reality_check_count = getattr(agent, "reality_check_count", 0) + 1
        if not str(record.get("savedPath") or "").strip():
            # VL succeeded but the evidence did not persist: hand the verdict
            # to the worker anyway (the observation is still real) and tell
            # it to persist its own copy — the layer-3 pass and the B3 gate
            # need a ledger entry to verify.
            agent.target_shortfall_streak = 0
            logger = getattr(agent, "logger", None)
            if logger is not None and hasattr(logger, "write"):
                logger.write("vl.reality_check.persist_failed", {
                    "triggerTool": name,
                    "recordStatus": str(record.get("status") or ""),
                })
            out = {**result, "realityCheck": {
                **_reality_check_summary(row),
                "evidencePersisted": False,
            }}
            out["next_instruction"] = (
                "A visual reality check ran but its evidence artifact failed"
                " to persist. The observation above is still valid: persist"
                " it yourself via record_extraction and cite that savedPath"
                " in evidenceArtifacts before declaring"
                " target_absent/instruction_infeasible. "
            ) + _reality_check_instruction(
                reconciled=row_reconciled,
                grading=row_grading,
                capture=capture,
                evidence_path="",
            )
            return out
        reality: JsonDict = {
            **_reality_check_summary(row),
            "evidenceSavedPath": str(record.get("savedPath") or ""),
        }
        logger = getattr(agent, "logger", None)
        if logger is not None and hasattr(logger, "write"):
            logger.write("vl.reality_check", {**reality, "triggerTool": name})
        agent.target_shortfall_streak = 0
        out = {**result, "realityCheck": reality}
        out["next_instruction"] = _reality_check_instruction(
            reconciled=row_reconciled,
            grading=row_grading,
            capture=capture,
            evidence_path=reality["evidenceSavedPath"],
        )
        return out
    except Exception as exc:  # reality check must never break the call path
        logger = getattr(agent, "logger", None)
        if logger is not None and hasattr(logger, "write"):
            logger.write("vl.reality_check.error", {"error": str(exc)[:300]})
        return result


def _final_answer_reality_check_rejection(
    agent: Any,
    answer: str,
) -> Optional[JsonDict]:
    """Layer-3 gate: a final_answer that declares target_absent /
    instruction_infeasible with NO visual reality check on record is bounced
    back once, telling the worker to verify visually and cite the persisted
    observation. One bounce only — the spawner-side evidence gate remains the
    last line of defense, so a second attempt always goes through."""
    vl_config = getattr(
        getattr(getattr(agent, "runtime", None), "harness", None), "vl", None
    )
    if (
        vl_config is None
        or not getattr(vl_config, "enabled", False)
        or not getattr(vl_config, "reality_check_enabled", True)
    ):
        return None
    if getattr(agent, "final_answer_reality_nudged", False):
        return None
    try:
        from harness.vl.reality_check import (
            cites_ledger_evidence,
            semantic_terminal_claimed,
        )
        if not semantic_terminal_claimed(answer):
            return None
        # "Any VL check happened" is too weak a pass — an overlay/CAPTCHA
        # check says nothing about the target's absence. Accept only:
        # (a) the layer-2 auto reality check ran (its target-specific
        #     observation was handed back to the worker), or
        # (b) the worker did its own visual check AND cites ledger-backed
        #     evidence in the answer (so the B3 gate can verify it).
        if getattr(agent, "reality_check_count", 0) > 0:
            return None
        vl_checks = (
            (getattr(agent, "vl_check_count", 0) or 0)
            + (getattr(agent, "vl_force_check_count", 0) or 0)
        )
        if vl_checks > 0:
            ledger = [
                *list(getattr(agent, "artifacts", []) or []),
                *list(getattr(agent, "extraction_attempt_artifacts", []) or []),
            ]
            if cites_ledger_evidence(answer, ledger):
                return None
    except Exception:
        return None
    agent.final_answer_reality_nudged = True
    return {
        "status": "rejected_needs_reality_check",
        "tool_was_executed": False,
        "next_instruction": (
            "You are declaring the target absent/infeasible, but no visual"
            " reality check ran this session — DOM probing alone is not"
            " sufficient evidence of absence. Scroll to the relevant region,"
            " call visual_verify with a claim describing the expected content,"
            " persist the observation via record_extraction, cite its"
            " savedPath in evidenceArtifacts, then call final_answer again."
            " If visual verification is impossible (e.g. page dead), re-issue"
            " this final_answer unchanged and it will be accepted."
        ),
    }


def _final_answer_content_completeness_rejection(
    agent: Any,
    answer: str,
    *,
    status: str = "",
) -> Optional[JsonDict]:
    """Reject semantic absence or false success while content is incomplete."""
    tracker = getattr(agent, "content_completeness_tracker", None)
    veto = tracker.terminal_veto() if tracker is not None else None
    if not isinstance(veto, dict):
        return None
    payload: JsonDict = {}
    try:
        parsed = json.loads(str(answer or ""))
        payload = parsed if isinstance(parsed, dict) else {}
    except Exception:
        payload = {}
    claimed_statuses = {
        str(status or "").strip().casefold(),
        str(payload.get("status") or "").strip().casefold(),
        str(payload.get("outcome") or "").strip().casefold(),
    }
    claims_success = bool(
        claimed_statuses
        & {"done", "success", "completed", "complete", "validated_done"}
    )
    claims_semantic_terminal = False
    try:
        blockers = payload.get("blockers") if isinstance(payload, dict) else None
        for blocker in blockers if isinstance(blockers, list) else []:
            if not isinstance(blocker, dict):
                continue
            raw = blocker.get("classification")
            category = (
                str(raw.get("category") or "").strip()
                if isinstance(raw, dict)
                else str(
                    raw or blocker.get("category") or blocker.get("type") or ""
                ).strip()
            )
            if category in {"target_absent", "instruction_infeasible"}:
                claims_semantic_terminal = True
                break
    except Exception:
        claims_semantic_terminal = False
    if not (claims_success or claims_semantic_terminal):
        return None
    return {
        "status": "rejected_content_incomplete",
        "classification": veto,
        "claimedSuccess": claims_success,
        "tool_was_executed": False,
        "next_instruction": str(veto.get("next_instruction") or ""),
    }


async def _promote_visual_locate(
    agent: Any,
    page_id: str,
    image_path: str,
    verdict: JsonDict,
    step: int,
    *,
    expected_text: str = "",
) -> JsonDict:
    """Reverse-look-up the VL `point` to a canonical AXTree id via bbox containment
    (the AXTree bbox space == screenshot px space). Attaches `resolvedId` (durable)
    or `cssPoint` (coords fallback for a genuine AXTree blind spot). Best-effort."""
    try:
        from harness.vl.locate import (
            _screenshot_dims,
            apply_promotion_guard,
            promote_locate,
        )

        shot_w, shot_h = await _screenshot_dims(image_path)
        ax = await _invoke_browser_method(
            agent, "DOM.getAXTree",
            {"pageId": page_id, "purpose": "promote the VL pixel to a canonical id"},
            step,
        )
        lines = (_response_data(ax) or {}).get("lines") or []
        # Avoid hidden Runtime.evaluate probes. AXTree rectangles and the
        # standard screenshot path use the same CSS-pixel coordinate contract;
        # promotion is guarded by label/role matching before any action.
        dpr = 1.0
        promo = promote_locate(lines, verdict["point"], shot_w=shot_w, shot_h=shot_h, dpr=dpr)
        promo = apply_promotion_guard(
            promo, vl_label=verdict.get("control_label"),
            expected_text=expected_text, dpr=dpr,
            logger=getattr(agent, "logger", None),
            page_id=page_id,
        )
        out = {**verdict, "promotion": promo}
        if promo.get("resolved"):
            out["resolvedId"] = promo.get("id")
            out["resolvedLabel"] = promo.get("label")
            out["next_instruction"] = (
                f"VL located the target and it was promoted to durable id"
                f" {promo.get('id')!r}. Act on that id (Input.click/DOM.getText with"
                f" id), NOT raw coordinates."
            )
        elif promo.get("promotionGuard"):
            out["cssPoint"] = promo.get("cssPoint")
            out["next_instruction"] = (
                "VL located the target but the bbox promotion failed a sanity"
                f" check ({promo['promotionGuard'].get('reason')}) and was demoted."
                " If safe and not consequential, use a single coordinate action at"
                " cssPoint; coordinates must never be persisted into a skill."
            )
        else:
            out["cssPoint"] = promo.get("cssPoint")
            out["next_instruction"] = (
                "VL located the target but no AXTree node covers it (blind spot)."
                " If safe and not consequential, use a single coordinate action at"
                " cssPoint; coordinates must never be persisted into a skill."
            )
        return out
    except Exception as exc:  # promotion is best-effort; keep the raw verdict
        return {**verdict, "promotion_error": str(exc)}


def _screenshot_saved_path(result: JsonDict) -> Optional[str]:
    data = _response_data(result)
    if not data:
        data = _raw_response_data(result)
    for key in ("savedPath", "path", "filePath"):
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            return value
    if str(data.get("encoding") or "").lower() == "file":
        value = data.get("data")
        if isinstance(value, str) and value.strip():
            return value
    response = result.get("response") if isinstance(result, dict) else None
    if isinstance(response, dict):
        data = response.get("data")
        if isinstance(data, dict):
            for key in ("savedPath", "path", "filePath"):
                value = data.get(key)
                if isinstance(value, str) and value.strip():
                    return value
            if str(data.get("encoding") or "").lower() == "file":
                value = data.get("data")
                if isinstance(value, str) and value.strip():
                    return value
    return None


def _runtime_any_json_payload(result: JsonDict) -> Optional[Any]:
    values: List[Any] = []
    response = result.get("response") if isinstance(result, dict) else None
    data = response.get("data") if isinstance(response, dict) else None
    if isinstance(data, dict):
        values.extend([
            # Runtime.evaluate now returns a platform evidence envelope:
            # {value, runtimeEvaluation:{requestedWorld,executedWorld,...}}.
            # Unwrap value before considering legacy direct-object payloads.
            data.get("value"),
            data.get("result"),
            data.get("returnValue"),
            data,
        ])
    elif data is not None:
        values.append(data)
    for value in list(values):
        if isinstance(value, dict):
            values.extend([
                value.get("value"),
                value.get("result"),
                value.get("returnValue"),
            ])
    for value in values:
        if isinstance(value, str):
            try:
                return json.loads(value)
            except json.JSONDecodeError:
                continue
        if isinstance(value, (dict, list)):
            return value
    return None


async def _invoke_trusted_collection_template(
    agent: Any,
    *,
    template_id: str,
    bindings: JsonDict,
    page_id: str,
    step: int,
) -> Any:
    """Execute one registered, read-only ``collect_items`` template.

    This is the only harness-internal Runtime exception.  The caller cannot
    supply JavaScript: the verifier registry renders a fixed source template
    from JSON-encoded bindings, and this function hard-codes strict isolated
    execution. It intentionally does not require the model-facing platform
    world-evidence envelope; do not route model-authored scripts through this
    compatibility path. The payload is returned as JSON directly; the former
    document.title side channel is not restored.
    """
    from harness.observation.verifiers import render_trusted_collection_template

    try:
        rendered = render_trusted_collection_template(template_id, dict(bindings))
    except (TypeError, ValueError) as exc:
        return {
            "_oracle_error": str(exc),
            "_oracle_error_code": "trusted_collection_template_invalid",
        }
    expression = f"JSON.stringify(({rendered}))"
    digest = hashlib.sha256(expression.encode("utf-8")).hexdigest()
    logger = getattr(agent, "logger", None)
    if logger is not None:
        logger.write(
            "runtime.evaluate.trusted_collection_template",
            {
                "templateId": template_id,
                "expressionSha256": digest,
                "pageId": page_id,
                "bindingNames": sorted(bindings),
            },
        )
    result = await _invoke_browser_method(
        agent,
        "Runtime.evaluate",
        {
            "pageId": page_id,
            "expression": expression,
            "world": "isolated",
            "purpose": f"collect_items fixed read-only template: {template_id}",
        },
        step,
        count_progress=False,
        read_only_eval=True,
        internal=True,
        _trusted_collection_runtime_token=_TRUSTED_COLLECTION_RUNTIME_TOKEN,
    )
    if _invoke_result_failed(result):
        error_text = str(
            result.get("error")
            or ((result.get("response") or {}).get("error") if isinstance(result.get("response"), dict) else "")
            or "trusted collection template execution failed"
        )[:500]
        normalized = error_text.casefold()
        error_code = (
            "stealth_probe_unavailable"
            if "stealthprobe is unavailable" in normalized
            else "stealth_probe_timeout"
            if "stealthprobe" in normalized and "timed out" in normalized
            else "trusted_collection_runtime_failed"
        )
        return {
            "_oracle_error": error_text,
            "_oracle_error_code": error_code,
        }
    payload = _runtime_any_json_payload(result)
    if payload is None:
        return {
            "_oracle_error": "trusted collection template returned no JSON payload",
            "_oracle_error_code": "trusted_collection_payload_invalid",
        }
    return payload


def _build_runtime_json_expression(expression: str) -> str:
    expression_json = json.dumps(expression)
    return f"""
(async () => {{
  const __abcpExpression = {expression_json};
  const __abcpValue = (0, eval)("(" + __abcpExpression + ")");
  const __abcpResolved = (
    __abcpValue && typeof __abcpValue.then === "function"
  ) ? await __abcpValue : __abcpValue;
  return JSON.stringify({{ value: __abcpResolved }});
}})()
"""


def _runtime_evaluation_error_text(result: JsonDict) -> str:
    if not isinstance(result, dict):
        return "Runtime.evaluate failed"
    if result.get("error"):
        return str(result.get("error"))
    response = result.get("response")
    if isinstance(response, dict):
        if response.get("error"):
            return str(response.get("error"))
        data = response.get("data")
        if isinstance(data, dict) and data.get("error"):
            return str(data.get("error"))
    return "Runtime.evaluate failed without an error message"


def _runtime_execution_metadata(response: Any) -> JsonDict:
    """Read platform-issued world evidence from a Runtime.evaluate response."""
    if not isinstance(response, dict):
        return {}
    data = response.get("data")
    if not isinstance(data, dict):
        nested = response.get("response")
        data = nested.get("data") if isinstance(nested, dict) else None
    metadata = data.get("runtimeEvaluation") if isinstance(data, dict) else None
    return dict(metadata) if isinstance(metadata, dict) else {}


def _runtime_response_world_metadata_supplied(response: Any) -> bool:
    """Whether the platform attempted to supply its world-evidence envelope.

    Presence is kept separate from validity: a legacy response with no field may
    use degraded harness dispatch evidence, while a present but malformed field
    must fail closed instead of being mistaken for legacy compatibility.
    """
    if not isinstance(response, dict):
        return False
    data = response.get("data")
    if not isinstance(data, dict):
        nested = response.get("response")
        data = nested.get("data") if isinstance(nested, dict) else None
    return isinstance(data, dict) and "runtimeEvaluation" in data


def _runtime_attempt_receipt(response: Any, requested_world: str) -> JsonDict:
    metadata = _runtime_execution_metadata(response)
    metadata_supplied = _runtime_response_world_metadata_supplied(response)
    failed = _invoke_result_failed(
        {"method": "Runtime.evaluate", "response": response}
        if isinstance(response, dict) and "response" not in response
        else response
    )
    receipt = {
        "requestedWorld": requested_world,
        "executedWorld": str(metadata.get("executedWorld") or "") or None,
        "status": "failed" if failed else "done",
        "evidence": (
            "platform_response"
            if metadata
            else "platform_response_invalid"
            if metadata_supplied
            else "harness_dispatched_world"
        ),
        **(
            {"fallbackReason": str(metadata.get("fallbackReason"))}
            if metadata.get("fallbackReason") else {}
        ),
        **(
            {"error": _runtime_evaluation_error_text({"response": response})[:500]}
            if failed else {}
        ),
    }
    if not metadata_supplied:
        receipt["dispatchedWorld"] = requested_world
        receipt["evidenceStrength"] = "degraded"
    elif not metadata:
        receipt["evidenceStrength"] = "invalid"
    return receipt


def _runtime_response_world_verified(response: Any, expected_world: str) -> bool:
    metadata = _runtime_execution_metadata(response)
    return (
        str(metadata.get("requestedWorld") or "") == expected_world
        and str(metadata.get("executedWorld") or "") == expected_world
    )


def _runtime_main_fallback_signaled(response: Any) -> bool:
    return MAIN_WORLD_REQUIRED_PREFIX in _runtime_evaluation_error_text(
        {"response": response}
    )


def _rows_from_eval_value(value: Any) -> Optional[List[JsonDict]]:
    if isinstance(value, list) and all(isinstance(item, dict) for item in value):
        return value
    if isinstance(value, dict):
        rows = value.get("rows")
        if isinstance(rows, list) and all(isinstance(item, dict) for item in rows):
            return rows
    return None


def _attach_runtime_json_value(
    agent: Any,
    result: JsonDict,
    value: Any,
    runtime_receipt: JsonDict,
    *,
    step: int,
) -> None:
    """Attach JSON-mode Runtime output and preserve extraction guarantees.

    Keep the raw value available for diagnostics, but make a non-row
    ``recordName`` contract failure explicit and apply the unrecorded-row gate.
    """
    result["runtimeValue"] = value
    result["runtimeValueType"] = type(value).__name__
    record_name = str(runtime_receipt.get("recordName") or "").strip()
    rows = _rows_from_eval_value(value)
    if record_name:
        if rows is None:
            message = (
                "record_name was provided, but Runtime.evaluate returned neither"
                " a list of objects nor an object with rows=[...]"
            )
            result["runtimeJSONError"] = {
                "code": "runtime_record_value_not_rows",
                "error": message,
            }
            result["recordExtraction"] = {
                "status": "failed",
                "error": message,
                "tool_was_executed": False,
            }
            return
        record_result = _record_extraction(
            agent,
            {
                "name": record_name,
                "rows": rows,
                "schema": {"source": "Runtime.evaluate"},
                "description": "Rows extracted by Runtime.evaluate",
            },
        )
        result["recordExtraction"] = record_result
        if _record_extraction_persisted(record_result):
            agent.pending_unrecorded_extraction = None
        return
    if rows:
        agent.pending_unrecorded_extraction = {
            "source": "Runtime.evaluate",
            "step": step,
            "rowCount": len(rows),
            "turns": 0,
        }


def _response_data(result: JsonDict) -> JsonDict:
    response = result.get("response") if isinstance(result, dict) else None
    if not isinstance(response, dict):
        return {}
    data = response.get("data")
    return data if isinstance(data, dict) else {}


def _raw_response_data(response: Any) -> JsonDict:
    if not isinstance(response, dict):
        return {}
    data = response.get("data")
    return data if isinstance(data, dict) else {}


def _page_create_error_text(result: Any) -> str:
    parts: List[str] = []

    def visit(value: Any, depth: int = 0) -> None:
        if depth > 5:
            return
        if isinstance(value, str):
            parts.append(value)
        elif isinstance(value, (int, float, bool)):
            parts.append(str(value))
        elif isinstance(value, dict):
            for key in ("error", "message", "code", "data", "observation"):
                if key in value:
                    visit(value.get(key), depth + 1)
            response = value.get("response")
            if isinstance(response, dict):
                visit(response, depth + 1)
        elif isinstance(value, list):
            for item in value[:10]:
                visit(item, depth + 1)

    visit(result)
    return " ".join(parts)


def _is_page_create_32005_failure(method: str, result: Any) -> bool:
    if method != "Page.create":
        return False
    text = _page_create_error_text(result).lower()
    return "-32005" in text and "page.create" in text


FLEET_LOSS_ERROR_CODES = frozenset({
    "FLEET_ARCHIVED",
    "FLEET_NOT_AVAILABLE",
    "FLEET_OWNERSHIP_MISMATCH",
    "FLEET_OWNER_MISMATCH",
})


def _fleet_loss_signal(result: Any) -> str:
    """Prefer Dispatcher structured codes, retaining one compatibility fallback."""

    signals: Set[str] = set()

    def visit(value: Any, depth: int = 0) -> None:
        if depth > 6:
            return
        if isinstance(value, dict):
            for key, nested in value.items():
                if key in {"code", "errorCode", "reasonCode", "reasonKind"}:
                    if isinstance(nested, str):
                        signals.add(nested.strip().upper())
                if key != "methodSchema":
                    visit(nested, depth + 1)
        elif isinstance(value, list):
            for nested in value[:20]:
                visit(nested, depth + 1)

    visit(result)
    structured = sorted(signals.intersection(FLEET_LOSS_ERROR_CODES))
    if structured:
        return structured[0]
    lowered = _page_create_error_text(result).lower()
    if any(marker in lowered for marker in (
        "is archived",
        "has been archived",
        "fleet archived",
        "owned by another agent",
        "not available for this agent",
    )):
        return "LEGACY_ERROR_TEXT"
    return ""


def _assigned_fleet_lost_result(
    agent: Any,
    method: str,
    params: JsonDict,
    result: JsonDict,
) -> Optional[JsonDict]:
    if not _fleet_reuse_enabled(agent):
        return None
    if method != "Page.create":
        return None
    error_text = _page_create_error_text(result)
    loss_signal = _fleet_loss_signal(result)
    if not loss_signal:
        return None
    session_key = str(getattr(agent, "fleet_session_key", "") or "").strip()
    fleet_id = str(
        params.get("fleetId") or getattr(agent, "assigned_fleet_id", "") or ""
    ).strip()
    status = "session_fleet_lost" if session_key else "fleet_assignment_lost"
    next_instruction = (
        "Treat this authenticated session as stale and follow the"
        " auth-interrupt/login recovery flow; do not retry the same binding."
        if session_key
        else "Stop this worker and request a fresh coordinator assignment."
    )
    lost_handler = getattr(agent, "auth_session_lost_handler", None)
    if session_key and callable(lost_handler):
        try:
            lost_handler({
                "sessionKey": session_key,
                "fleetId": fleet_id,
                "sessionGeneration": int(
                    getattr(agent, "fleet_session_generation", 0) or 0
                ),
                "reason": error_text[:500],
            })
        except Exception as exc:  # recovery bookkeeping must not mask evidence
            logger = getattr(agent, "logger", None)
            if logger is not None:
                logger.write(
                    "auth_fleet.lost_handler_failed",
                    {"sessionKey": session_key, "error": str(exc)[:300]},
                )
    answer = {
        "outcome": "blocked",
        "data": {},
        "evidence": [{
            "method": method,
            "fleetId": fleet_id,
            "error": error_text[:500],
        }],
        "blockers": [{
            "classification": status,
            "message": next_instruction,
        }],
        "next_steps": [next_instruction],
    }
    return {
        **result,
        "status": status,
        "terminal": True,
        "sessionKey": session_key,
        "assignedFleetId": fleet_id,
        "fleetLossSignal": loss_signal,
        "errorClassification": {
            "type": status,
            "suggested_action": "auth_interrupt" if session_key else "respawn_worker",
            "method": method,
        },
        "answer": json.dumps(answer, ensure_ascii=False),
        "next_instruction": next_instruction,
    }


def _pages_from_value(value: Any) -> List[JsonDict]:
    pages: List[JsonDict] = []

    def visit(item: Any, inherited_fleet_id: str = "") -> None:
        if isinstance(item, dict):
            fleet_id = str(item.get("fleetId") or inherited_fleet_id or "")
            page_id = item.get("pageId") or item.get("page_id")
            if isinstance(page_id, str) and page_id.strip():
                page = dict(item)
                if fleet_id and not page.get("fleetId"):
                    page["fleetId"] = fleet_id
                pages.append(page)
            for key, nested in item.items():
                if key == "methodSchema":
                    continue
                visit(nested, fleet_id)
        elif isinstance(item, list):
            for nested in item:
                visit(nested, inherited_fleet_id)

    visit(value)
    dedup: Dict[str, JsonDict] = {}
    for page in pages:
        page_id = str(page.get("pageId") or page.get("page_id") or "").strip()
        if page_id:
            dedup[page_id] = page
    return list(dedup.values())


async def _page_create_probe_call(agent: Any, method: str, params: JsonDict) -> JsonDict:
    try:
        runner = getattr(agent, "render_recovery_runner", None)
        if runner is not None:
            response, _recovery = await runner.call(method, params)
        else:
            response = await agent.browser.call(method, params)
        return {"ok": True, "method": method, "params": params, "response": response}
    except Exception as exc:  # noqa: BLE001 - diagnostic probe must record all failures.
        return {"ok": False, "method": method, "params": params, "error": str(exc)}


def _page_state_is_usable(response: Any) -> bool:
    if not isinstance(response, dict) or response.get("error"):
        return False
    data = _raw_response_data(response)
    status = str(data.get("status") or "").strip().lower()
    if status in {"closed", "crashed", "stale", "quarantined", "paused"}:
        return False
    hitl = data.get("hitl")
    if isinstance(hitl, dict) and hitl.get("isPaused") is True:
        return False
    return True


def _page_create_infrastructure_classification() -> JsonDict:
    return {
        "category": "blocked_infrastructure",
        "type": "browser_unavailable_or_no_page",
        "method": "Page.create",
        "hint": (
            "Page.create failed with -32005 and Fleet/Page probing found no"
            " usable existing page. Reconnect or rebuild the Browser Client"
            " before retrying this worker."
        ),
    }


def _page_create_terminal_answer(
    *,
    original_error: str,
    probe: JsonDict,
) -> str:
    classification = _page_create_infrastructure_classification()
    payload = {
        "outcome": "blocked",
        "data": {},
        "evidence": [
            {
                "method": "Page.create",
                "error": original_error[:500],
                "probeClassification": "browser_unavailable_or_no_page",
                "checkedPageCount": len(probe.get("checkedPages") or []),
            }
        ],
        "blockers": [
            {
                "classification": classification,
                "message": classification["hint"],
                "method": "Page.create",
            }
        ],
        "next_steps": [
            "Reconnect or restart the Browser Client/playground backend, then retry the worker.",
            "If Fleet.list/Page.list shows reusable pages later, prefer reusing one instead of creating a new page.",
        ],
    }
    return json.dumps(payload, ensure_ascii=False, default=str)


async def _recover_page_create_32005(
    agent: Any,
    params: JsonDict,
    result: JsonDict,
) -> Tuple[JsonDict, bool]:
    original_error = _page_create_error_text(result)
    assigned_fleet_id = str(
        params.get("fleetId")
        or getattr(agent, "assigned_fleet_id", "")
        or ""
    ).strip()
    probe: JsonDict = {
        "trigger": "Page.create_-32005",
        "originalError": original_error[:500],
        "fleetList": None,
        "pageLists": [],
        "checkedPages": [],
        "classification": "unknown",
    }
    page_candidates: List[JsonDict] = []
    if _fleet_reuse_enabled(agent):
        # A coordinator-managed fresh worker must never turn a create failure
        # into implicit adoption of another worker's page. Probe only explicit
        # local bindings plus pages authoritatively leased to this worker (the
        # latter covers direct skill/fast-path calls that bypass tool post-hooks).
        page_fleets = getattr(agent, "page_fleet_ids", None)
        page_fleets = page_fleets if isinstance(page_fleets, dict) else {}
        candidate_page_fleets = {
            str(page_id or "").strip(): str(fleet_id or "").strip()
            for page_id, fleet_id in page_fleets.items()
            if str(page_id or "").strip()
        }
        manager = getattr(agent, "page_lease_manager", None)
        worker_id = str(getattr(agent, "worker_id", "") or "").strip()
        if (
            manager is not None
            and hasattr(manager, "page_fleets_for_worker")
            and worker_id
        ):
            candidate_page_fleets.update(
                manager.page_fleets_for_worker(worker_id)
            )
        allowed_page_ids = {
            str(page_id or "").strip()
            for page_id in (getattr(agent, "allowed_page_ids", set()) or set())
            if str(page_id or "").strip()
        }
        allowed_page_ids.update(
            page_id
            for page_id in candidate_page_fleets
            if (
                manager is not None
                and hasattr(manager, "owner_for")
                and str(manager.owner_for(page_id) or "") == worker_id
            )
        )
        for page_id in sorted(allowed_page_ids):
            page_id = str(page_id or "").strip()
            if not page_id:
                continue
            candidate_fleet_id = str(
                candidate_page_fleets.get(page_id) or ""
            ).strip()
            if not candidate_fleet_id or (
                assigned_fleet_id
                and candidate_fleet_id != assigned_fleet_id
            ):
                continue
            page_candidates.append({
                "pageId": page_id,
                "fleetId": candidate_fleet_id,
            })
        probe["fleetList"] = {
            "skipped": True,
            "reason": "coordinator_page_delegation_only",
        }
    else:
        fleet_list = await _page_create_probe_call(agent, "Fleet.list", {})
        probe["fleetList"] = fleet_list
        page_candidates.extend(_pages_from_value(fleet_list.get("response")))
        fleets = _raw_response_data(fleet_list.get("response")).get("fleets")
        if isinstance(fleets, list):
            for fleet in fleets:
                if not isinstance(fleet, dict):
                    continue
                fleet_id = str(fleet.get("fleetId") or "").strip()
                if not fleet_id or (
                    assigned_fleet_id and fleet_id != assigned_fleet_id
                ):
                    continue
                listed = await _page_create_probe_call(
                    agent,
                    "Page.list",
                    {"fleetId": fleet_id},
                )
                probe["pageLists"].append(listed)
                for page in _pages_from_value(listed.get("response")):
                    page.setdefault("fleetId", fleet_id)
                    page_candidates.append(page)

        page_candidates.extend(
            _pages_from_value(getattr(agent, "preloaded_registration", None))
        )
    deduped: Dict[str, JsonDict] = {}
    for page in page_candidates:
        page_id = str(page.get("pageId") or page.get("page_id") or "").strip()
        fleet_id = str(page.get("fleetId") or "").strip()
        if (
            page_id
            and (not assigned_fleet_id or fleet_id == assigned_fleet_id)
        ):
            deduped[page_id] = page

    for page in list(deduped.values())[:5]:
        page_id = str(page.get("pageId") or page.get("page_id") or "").strip()
        if not page_id:
            continue
        state = await _page_create_probe_call(
            agent,
            "Page.getState",
            {
                "pageId": page_id,
                "purpose": "verify existing page after Page.create -32005",
            },
        )
        state_data = _raw_response_data(state.get("response"))
        candidate_fleet_id = str(page.get("fleetId") or "")
        checked = {
            "pageId": page_id,
            "fleetId": candidate_fleet_id,
            "ok": (
                bool(state.get("ok"))
                and _page_state_is_usable(state.get("response"))
            ),
            "status": state_data.get("status"),
            "title": state_data.get("title"),
            "url": state_data.get("url"),
            "error": state.get("error"),
        }
        probe["checkedPages"].append(checked)
        if checked["ok"]:
            probe["classification"] = "create_failed_but_existing_page_usable"
            response = {
                "observation": (
                    "Page.create failed with -32005, but an existing usable"
                    f" page was found and reused: pageId=\"{page_id}\""
                    f" fleetId=\"{checked['fleetId']}\"."
                ),
                "data": {
                    "pageId": page_id,
                    "fleetId": checked["fleetId"],
                    "reusedExistingPage": True,
                    "pageCreateOriginalError": original_error[:500],
                },
            }
            recovered = {
                "method": "Page.create",
                "params": params,
                "response": response,
                "pageCreateRecovery": probe,
                "next_instruction": (
                    "Continue with the reused pageId. Call Page.getState and"
                    " DOM.getAXTree before targeting page elements."
                ),
            }
            return recovered, False

    probe["classification"] = "browser_unavailable_or_no_page"
    classification = _page_create_infrastructure_classification()
    terminal = {
        "method": "Page.create",
        "params": params,
        "status": "incomplete",
        "terminal": True,
        "error": (
            "Page.create failed with -32005 and no usable existing page was"
            " found via Fleet.list/Page.list/Page.getState."
        ),
        "classification": classification,
        "errorClassification": {
            "type": "browser_unavailable_or_no_page",
            "suggested_action": "abort_worker_reconnect_browser_then_retry",
            "method": "Page.create",
        },
        "pageCreateRecovery": probe,
        "answer": _page_create_terminal_answer(
            original_error=original_error,
            probe=probe,
        ),
        "next_instruction": (
            "Stop this worker. The browser backend has no usable page after"
            " Page.create -32005; LeadAgent should retry only after the Browser"
            " Client/playground backend is connected or rebuilt."
        ),
    }
    return terminal, True


def _attach_navigation_check(result: JsonDict, *, method: str, params: JsonDict) -> JsonDict:
    if method != "Page.navigate" or not isinstance(result, dict):
        return result
    target_url = str(params.get("url") or "").strip()
    if not target_url:
        return result
    data = _response_data(result)
    current_url = str(data.get("url") or "").strip()
    title = str(data.get("title") or "").strip()
    status = "unknown"
    hint = "Call Page.getState after the reactive load event to verify final URL before extraction."
    if current_url:
        status = "arrived" if _urls_same_destination(target_url, current_url) else "off_target"
    if _looks_like_challenge_title(title):
        status = "challenge_pending"
        hint = (
            "Navigation is on a challenge/interstitial surface. Do not extract target data yet;"
            " wait for settlement, request HITL if confirmed, then verify the final URL."
        )
    elif status == "off_target":
        hint = (
            "Navigation did not report the requested destination. Re-check Page.getState,"
            " then re-navigate or report the redirect/blocker before extracting."
        )
    enriched = dict(result)
    enriched["navigationCheck"] = {
        "status": status,
        "targetUrl": target_url,
        "currentUrl": current_url,
        "title": title,
        "hint": hint,
    }
    return enriched


def _urls_same_destination(expected: str, current: str) -> bool:
    try:
        expected_parts = urlparse(expected)
        current_parts = urlparse(current)
    except ValueError:
        return expected.rstrip("/") == current.rstrip("/")
    if expected_parts.netloc and current_parts.netloc:
        if expected_parts.netloc.lower() != current_parts.netloc.lower():
            return False
    expected_path = (expected_parts.path or "/").rstrip("/") or "/"
    current_path = (current_parts.path or "/").rstrip("/") or "/"
    if expected_path != current_path:
        return False
    if expected_parts.query and expected_parts.query != current_parts.query:
        return False
    if expected_parts.fragment and expected_parts.fragment != current_parts.fragment:
        return False
    return True


def _looks_like_challenge_title(title: str) -> bool:
    lowered = str(title or "").strip().lower()
    return lowered in {"just a moment...", "just a moment", "checking your browser..."}


def _attach_runtime_strategy_hints(result: JsonDict, *, method: str) -> JsonDict:
    if not isinstance(result, dict):
        return result
    classification = result.get("errorClassification")
    if not isinstance(classification, dict):
        return result
    if classification.get("type") != "occlusion_blocked":
        return result
    enriched = dict(result)
    blocked_target = ""
    params = result.get("params") if isinstance(result.get("params"), dict) else {}
    for key in ("id", "nodeId", "targetId", "selector"):
        value = params.get(key)
        if isinstance(value, str) and value.strip():
            blocked_target = value.strip()
            break
    enriched["runtimeStrategy"] = {
        "id": "browser_action.overlay.dismiss_overlay",
        "trigger": "occlusion_blocked",
        "method": method,
        "preferredTool": "dismiss_overlay",
        "call": {
            "tool": "dismiss_overlay",
            "pageId": params.get("pageId") or "",
            "targetId": blocked_target,
            # Only Input.click is auto-retried after dismissal; for any other
            # blocked method the tool returns dismissed_pending_action.
            "targetMethod": method if method == "Input.click" else "",
        },
        "safetyBoundary": (
            "dismiss_overlay never auto-clicks login/payment/provider buttons"
            " and never auto-retries consequential targets."
        ),
    }
    existing = str(enriched.get("next_instruction") or "").strip()
    overlay_instruction = (
        "Occlusion blocked this action. Call the dismiss_overlay tool with this"
        " pageId (and targetId=the blocked element id to auto-retry a safe"
        " action); it runs the close -> Escape -> verified-backdrop ladder"
        " internally and verifies the overlay is gone. Do not hand-run the"
        " ladder step by step."
    )
    enriched["next_instruction"] = (
        f"{existing} {overlay_instruction}".strip()
        if existing
        else overlay_instruction
    )
    return enriched


# Cap auto-intercept runs per page so a recurring/unclearable overlay cannot make
# the harness loop dismiss_overlay (and the VL arbiter) indefinitely. Once hit,
# the action falls back to the suggest-only hint for the model to decide.
AUTO_INTERCEPT_MAX_PER_PAGE = 3


def _auto_intercept_mode(agent: Any) -> str:
    harness = getattr(getattr(agent, "runtime", None), "harness", None)
    mode = str(getattr(harness, "auto_intercept", "p0p1") or "p0p1")
    return mode if mode in {"off", "suggest", "p0", "p0p1"} else "p0p1"


def _blocked_target_id(params: Any) -> str:
    if not isinstance(params, dict):
        return ""
    for key in ("id", "nodeId", "targetId", "selector"):
        value = params.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _record_microloop_telemetry(
    agent: Any,
    loop: str,
    outcome: str,
    detail: Optional[JsonDict] = None,
) -> None:
    """Per-loop micro-loop telemetry. Granularity (one row per loop invocation
    with trigger/outcome) does not fit strategy_telemetry's worker-result rows,
    so this is a dedicated in-memory aggregate + an auditable log event."""
    agg = getattr(agent, "_microloop_telemetry", None)
    if not isinstance(agg, dict):
        agg = {}
        agent._microloop_telemetry = agg
    bucket = agg.setdefault(loop, {})
    bucket["attempts"] = int(bucket.get("attempts", 0)) + 1
    bucket[outcome] = int(bucket.get(outcome, 0)) + 1
    logger = getattr(agent, "logger", None)
    if logger is not None:
        logger.write(
            "microloop.telemetry",
            {"loop": loop, "outcome": outcome, **(detail or {})},
        )


async def _maybe_auto_intercept_overlay(
    agent: Any,
    method: str,
    params: JsonDict,
    result: JsonDict,
    step: int,
) -> JsonDict:
    """Phase 7.2 auto-intercept. When an action is overlay-blocked and config
    permits, run dismiss_overlay automatically (saving the model a step) instead
    of only suggesting it, then fold an honest digest into the result.

    Triggers, by escalating config mode:
      p0  -> P0: errorClassification == occlusion_blocked on this result
      p0p1 -> also P1: an AXTree layer reports occlusionState == occluded
    P2 (text soft-detect) and P3 (observation keywords) are never auto-run:
    soft text has false positives, so they keep the suggest-only hint.

    Auth/paywall login/provider/payment controls are still never auto-clicked;
    dismiss_overlay runs only its safe rungs there and returns `policy_refused`
    when they do not clear it, and the original error/hint is preserved."""
    if not isinstance(result, dict):
        return result
    mode = _auto_intercept_mode(agent)
    if mode in {"off", "suggest"}:
        return result

    p0 = _result_occlusion_blocked(result)
    p1 = False
    if mode == "p0p1" and not p0:
        p1 = bool(visible_layers_occluded(_layers_from_result(result)))
    if not (p0 or p1):
        return result

    page_id = str(params.get("pageId") or "").strip() if isinstance(params, dict) else ""
    if not page_id:
        return result

    counts = getattr(agent, "_auto_intercept_counts", None)
    if not isinstance(counts, dict):
        counts = {}
        agent._auto_intercept_counts = counts
    if int(counts.get(page_id, 0)) >= AUTO_INTERCEPT_MAX_PER_PAGE:
        _record_microloop_telemetry(
            agent, "auto_intercept", "capped", {"pageId": page_id}
        )
        enriched = dict(result)
        enriched["autoIntercept"] = {
            "trigger": "occlusion_blocked" if p0 else "occluded_layers",
            "mode": mode,
            "skipped": "per_page_cap_reached",
            "cap": AUTO_INTERCEPT_MAX_PER_PAGE,
        }
        return enriched
    counts[page_id] = int(counts.get(page_id, 0)) + 1

    trigger = "occlusion_blocked" if p0 else "occluded_layers"
    blocked_target = _blocked_target_id(params)
    # Only Input.click is auto-retry-safe; dismiss_overlay re-checks the target's
    # sensitivity before any retry and returns dismissed_pending_action otherwise.
    # A click whose failure reports `sideEffectStarted` is NOT auto-retry-safe no
    # matter how safe the target looks: input dispatch had already begun, so the
    # click may have landed under the overlay and the retry would be a second
    # one. Clear the overlay anyway — that is useful and side-effect-free — but
    # hand back an unretried action for the caller to judge.
    side_effect_started = replay_forbidden(result)
    target_method = (
        method if method == "Input.click" and not side_effect_started else ""
    )
    dismiss = await _dismiss_overlay(
        agent,
        {"pageId": page_id, "targetId": blocked_target, "targetMethod": target_method},
        step,
    )
    dismiss_status = str(dismiss.get("status") or "")
    resolved = dismiss_status == "dismissed_and_retried"
    cleared = dismiss_status in {"dismissed", "dismissed_and_retried", "dismissed_pending_action"}
    # The dismiss interacted with the page (clicks/Escape) or could not clear it;
    # either way any snapshot recorded for THIS call (e.g. a DOM.getAXTree tree
    # written by _observe_axtree_state_after just before this) is now stale.
    # Invalidate so the next action re-fetches rather than trusting a pre-dismiss
    # tree. Only a receipt that dispatched nothing at all leaves the snapshot
    # valid: an auth/paywall refusal now still runs the safe rungs, so
    # `policy_refused` normally DID mutate the page. ("blocked" is the legacy
    # zero-attempt shape.)
    dismissed_nothing = (
        dismiss_status == "blocked"
        or str(dismiss.get("dismissOutcome") or "") == "not_attempted"
    )
    if not dismissed_nothing:
        _invalidate_axtree_snapshot(
            agent, "auto_intercept", params if isinstance(params, dict) else {}
        )
    # If the model's own call was DOM.getAXTree and we cleared the overlay, the
    # lines it would read are the PRE-dismiss tree. Re-fetch a fresh tree (no
    # model step), which both replaces those lines below and re-establishes a
    # clean current snapshot, so the model sees the post-dismiss page map and its
    # next action does not trip the stale guard on an obsolete id.
    tree_refreshed = False
    fresh_lines: List[Any] = []
    fresh_data: JsonDict = {}
    if cleared and method == "DOM.getAXTree":
        fresh = await _invoke_browser_method(
            agent,
            "DOM.getAXTree",
            {"pageId": page_id, "purpose": "auto_intercept: refresh tree after overlay cleared"},
            step,
            count_progress=False,
        )
        candidate_data = _response_data(fresh)
        fresh_data = candidate_data if isinstance(candidate_data, dict) else {}
        fresh_lines = list(getattr(agent, "axtree_lines", []) or [])
        tree_refreshed = bool(fresh_lines)
    outcome = (
        "resolved" if resolved
        else "cleared" if cleared
        else "blocked" if dismiss_status in {"policy_refused", "blocked"}
        else "failed"
    )
    _record_microloop_telemetry(
        agent,
        "auto_intercept",
        outcome,
        {"pageId": page_id, "trigger": trigger, "dismissStatus": dismiss_status},
    )

    enriched = dict(result)
    # Replace the stale pre-dismiss tree the model would otherwise read with the
    # freshly re-fetched post-dismiss tree. Swap the WHOLE data block (so
    # layers/nodeCount/truncated no longer contradict the refreshed lines — the
    # P1 trigger was a stale layers.occlusionState), then overlay the raw,
    # never-offloaded lines/nodes from the agent snapshot.
    if tree_refreshed:
        response = enriched.get("response")
        if isinstance(response, dict) and isinstance(response.get("data"), dict):
            if fresh_data:
                new_data = dict(fresh_data)
            else:
                new_data = dict(response["data"])
            new_data["lines"] = fresh_lines
            new_data["nodes"] = list(getattr(agent, "axtree_nodes", []) or [])
            response["data"] = new_data
    enriched["autoIntercept"] = {
        "trigger": trigger,
        "mode": mode,
        "dismissStatus": dismiss_status,
        "resolved": resolved,
        "cleared": cleared,
        "retried": bool(dismiss.get("retried")),
        "treeRefreshed": tree_refreshed,
        "overlay": dismiss.get("overlay"),
        "vlArbiter": dismiss.get("vlArbiter"),
        **(
            {"replayForbidden": True, "retrySuppressed": "side_effect_started"}
            if side_effect_started else {}
        ),
    }
    stale_tree_note = ""
    if cleared and method == "DOM.getAXTree" and not tree_refreshed:
        # Could not refresh: be explicit that the returned map is pre-dismiss.
        stale_tree_note = (
            " NOTE: response.data.lines is the PRE-dismiss tree and is now stale;"
            " call DOM.getAXTree again before using any element id from it."
        )
    if resolved:
        instruction = (
            "Occlusion auto-intercept: the overlay was dismissed and your original"
            " action was retried successfully. Continue — do NOT re-issue it."
        )
    elif cleared:
        if method == "DOM.getAXTree" and tree_refreshed:
            instruction = (
                "Occlusion auto-intercept: the overlay was dismissed and"
                " response.data.lines was refreshed to the post-dismiss tree. Use"
                " these ids."
            )
        elif side_effect_started:
            instruction = (
                "Occlusion auto-intercept: the overlay was dismissed, but your"
                " action was NOT retried because the platform reported that"
                " input dispatch had already started — it may have taken effect"
                " under the overlay. Read the page (Page.getState plus a fresh"
                " DOM.getAXTree, or the field/row you were changing) and decide"
                " from what you see; do not re-issue it blind."
            ) + stale_tree_note
        else:
            instruction = (
                "Occlusion auto-intercept: the overlay was dismissed but your action"
                " was not auto-retried (not auto-retry-safe or a consequential"
                " target). Re-issue the action if it is still needed."
            ) + stale_tree_note
    else:
        # blocked (auth/paywall) or failed: keep the original suggest hint intent.
        instruction = (
            "Occlusion auto-intercept ran dismiss_overlay but could not clear the"
            f" overlay (status={dismiss_status or 'unknown'}). It may be an"
            " auth/paywall wall (never auto-clicked); request HITL or report a"
            " blocker."
        )
    existing = str(enriched.get("next_instruction") or "").strip()
    enriched["next_instruction"] = f"{existing} {instruction}".strip() if existing else instruction
    return enriched


def _non_empty_param(params: JsonDict, key: str) -> bool:
    value = params.get(key)
    if isinstance(value, str):
        return bool(value.strip())
    return value is not None


def _non_negative_numeric_param(params: JsonDict, key: str) -> bool:
    value = params.get(key)
    if isinstance(value, (int, float)):
        return value >= 0
    if isinstance(value, str) and value.strip():
        try:
            return float(value) >= 0
        except ValueError:
            return False
    return False


def _check_select_param_requirements(
    method: str,
    params: JsonDict,
) -> Optional[JsonDict]:
    """Fail early on malformed Input.select selection envelopes.

    The live schema now requires EXACTLY ONE locator per item — id, value,
    label, or path — with `path` exclusive and every path segment following the
    same rule. An earlier revision of this guard deliberately accepted several
    coexisting fields because the schema of the day allowed it; that is now the
    opposite of the contract, and combining them is rejected by the platform.

    Multiple direct choices mean "this is the final selection set", not "append
    one more", and are only valid on a confirmed multi-select control — which
    the harness cannot know before dispatch, so that stays the platform's call.
    """

    if method != "Input.select":
        return None
    selections = params.get("selections")
    if not isinstance(selections, list) or not selections:
        return {
            "method": method,
            "params": params,
            "status": "invalid_params",
            "error": "Input.select requires a non-empty params.selections array.",
            "invalidParam": "selections",
            "missingAnyOf": [["selections"]],
            "tool_was_executed": False,
            "next_instruction": (
                "Call DOM.inspectSelect when choices are unknown, then pass"
                " selections as an array even for one choice. Copy only the"
                " id/value/label or complete path fields returned for the"
                " intended option, preferring exact value or label when present;"
                " Input.select manages the popup atomically."
            ),
        }

    canonical_id = re.compile(r"^\d+:\d+:\d+$")

    def invalid(path: str, detail: str) -> JsonDict:
        return {
            "method": method,
            "params": params,
            "status": "invalid_params",
            "error": detail,
            "invalidParam": path,
            "tool_was_executed": False,
            "next_instruction": (
                "Every selections item must carry EXACTLY ONE locator: id,"
                " exact value, exact label, or path. path is exclusive, and"
                " each path segment follows the same one-locator rule. Copy"
                " only option descriptor fields returned by DOM.inspectSelect;"
                " do not synthesize identifiers or operate the popup manually."
            ),
        }

    def present_locators(choice: JsonDict, *, allow_path: bool) -> List[str]:
        names = ["id", "value", "label"] + (["path"] if allow_path else [])
        present: List[str] = []
        for name in names:
            value = choice.get(name)
            if value is None:
                continue
            if isinstance(value, str) and not value.strip() and name != "value":
                # An empty value IS a legitimate option value; an empty
                # id/label is just an unfilled field.
                continue
            present.append(name)
        return present

    def validate_choice(choice: Any, path: str, *, allow_path: bool) -> Optional[JsonDict]:
        if not isinstance(choice, dict):
            return invalid(path, f"Input.select {path} must be an object.")
        raw_id = choice.get("id")
        if raw_id is not None and (
            not isinstance(raw_id, str) or canonical_id.fullmatch(raw_id.strip()) is None
        ):
            return invalid(f"{path}.id", f"Input.select {path}.id is not a canonical option id.")
        if not allow_path and choice.get("path") is not None:
            return invalid(f"{path}.path", "Nested Input.select cascade paths are not supported.")
        locators = present_locators(choice, allow_path=allow_path)
        if not locators:
            return invalid(
                path,
                f"Input.select {path} requires exactly one of id, value, label"
                + (", or path." if allow_path else "."),
            )
        if len(locators) > 1:
            return invalid(
                path,
                f"Input.select {path} carries {len(locators)} locators"
                f" ({', '.join(locators)}); the schema accepts exactly one.",
            )
        cascade = choice.get("path")
        if cascade is not None:
            if not isinstance(cascade, list) or len(cascade) < 2:
                return invalid(
                    f"{path}.path",
                    f"Input.select {path}.path must contain at least two ordered choices.",
                )
            for index, step in enumerate(cascade):
                error = validate_choice(step, f"{path}.path[{index}]", allow_path=False)
                if error is not None:
                    return error
        return None

    for index, selection in enumerate(selections):
        error = validate_choice(selection, f"selections[{index}]", allow_path=True)
        if error is not None:
            return error
    return None


def _check_nested_id_format(method: str, params: JsonDict) -> Optional[JsonDict]:
    """Same canonical-id check for locators that do not sit at the top level.

    Input.scroll's `target`/`container` and Input.drag's destination carry ids
    the describeAction schema describes inline, so the top-level `params.id`
    lookup below finds no spec and validates nothing. A truncated id there
    still reaches the browser as an opaque -32602.
    """
    for path, locator, key in (
        ("target", params.get("target"), "id"),
        ("container", params.get("container"), "id"),
        ("", params, "toId"),
    ):
        if not isinstance(locator, dict):
            continue
        raw = locator.get(key)
        if not isinstance(raw, str) or not raw.strip():
            continue
        if AXTREE_ID_RE.match(raw.strip()):
            continue
        param_path = f"{path}.{key}" if path else key
        return {
            "method": method,
            "params": params,
            "status": "invalid_params",
            "error": (
                f"{method} params.{param_path} is not a valid canonical element"
                " id (expected frameId:axNodeId:domNodeId)."
            ),
            "tool_was_executed": False,
            "invalidParam": param_path,
            "next_instruction": (
                "Re-read the active page with DOM.getAXTree and copy a current"
                " canonical id verbatim, or drop the id and locate by selector."
            ),
        }
    return None


def _check_id_param_format(
    method: str,
    params: JsonDict,
    method_schemas: Optional[dict],
) -> Optional[JsonDict]:
    """Validate a supplied canonical element `id` against the describeAction
    schema pattern. A truncated/fabricated id (e.g. "2:5367" where the schema
    requires "^\\d+:\\d+:\\d+$") is caught here with an actionable error
    instead of reaching the browser and returning a raw -32602 Invalid params.
    Only fires when an `id` is actually supplied; a missing id is handled by the
    selector/id presence check. Returns None when no pattern is available
    (nothing to validate against) so this never over-rejects."""
    if not isinstance(params, dict) or not isinstance(method_schemas, dict):
        return None
    nested = _check_nested_id_format(method, params)
    if nested is not None:
        return nested
    raw_id = params.get("id")
    if not isinstance(raw_id, str) or not raw_id.strip():
        return None
    schema = method_schemas.get(method)
    if not isinstance(schema, dict):
        return None
    spec_params = schema.get("params")
    if not isinstance(spec_params, dict):
        return None
    id_spec = spec_params.get("id")
    if not isinstance(id_spec, dict):
        return None
    pattern = id_spec.get("pattern")
    if not isinstance(pattern, str) or not pattern.strip():
        return None
    try:
        matched = re.search(pattern, raw_id.strip()) is not None
    except re.error:
        return None
    if matched:
        return None
    return {
        "method": method,
        "params": params,
        "status": "invalid_params",
        "error": (
            f"{method} params.id is not a valid canonical element id"
            f" (schema pattern: {pattern})."
        ),
        "tool_was_executed": False,
        "invalidParam": "id",
        "pattern": pattern,
        "next_instruction": (
            "Re-read the active page with DOM.getAXTree and copy a current"
            " canonical id verbatim. Do not truncate ids, reuse stale ids from"
            " a prior page/navigation, or fabricate one. The id must match the"
            f" schema pattern: {pattern}."
        ),
    }


# Platform cap on one DOM.getImg batch (schema: targets maxItems).
DOM_GET_IMG_MAX_TARGETS = 32


_SCROLL_MODE_INSTRUCTION = (
    "Input.scroll has three modes and no top-level locator. Target mode:"
    " target={id?,selector?} (plus optional container) with amount as the"
    " per-step cap and NO direction — the browser derives it and success means"
    " targetVisible=true. Container mode: container={id?,selector?} with"
    " direction and amount, for a container that is already visible. Viewport"
    " mode: neither locator, just direction and amount. Read layers[].delta for"
    " the real movement, and do not repeat the same direction after"
    " completedReason=boundary-reached."
)


def _check_scroll_param_requirements(
    method: str,
    params: JsonDict,
) -> Optional[JsonDict]:
    """Reject Input.scroll shapes the platform's three-mode union will refuse.

    The union is strict, so a flat `id`/`selector` — the pre-frame-graph shape
    and the one most models reach for — matches no variant and comes back as a
    bare -32602 with nothing to act on. Catching it here costs one round trip
    less and says which mode was meant.
    """
    if method != "Input.scroll":
        return None

    def invalid(detail: str, invalid_param: str) -> JsonDict:
        return {
            "method": method,
            "params": params,
            "status": "invalid_params",
            "error": detail,
            "invalidParam": invalid_param,
            "tool_was_executed": False,
            "next_instruction": _SCROLL_MODE_INSTRUCTION,
        }

    for key in ("id", "selector", "nodeId", "targetId"):
        if _non_empty_param(params, key):
            return invalid(
                f"Input.scroll does not accept a top-level {key}; put the"
                " locator in target={id?,selector?} or container={id?,selector?}.",
                key,
            )

    target = params.get("target")
    container = params.get("container")
    for key, locator in (("target", target), ("container", container)):
        if locator is None:
            continue
        if not isinstance(locator, dict):
            return invalid(f"Input.scroll params.{key} must be an object.", key)
        if not (_non_empty_param(locator, "id") or _non_empty_param(locator, "selector")):
            return invalid(
                f"Input.scroll params.{key} requires id or selector.", key
            )

    amount = params.get("amount")
    numeric_amount = (
        float(amount)
        if isinstance(amount, (int, float)) and not isinstance(amount, bool)
        else None
    )
    if target is not None:
        if _non_empty_param(params, "direction"):
            return invalid(
                "Input.scroll target mode derives its own direction; drop"
                " params.direction or switch to container/viewport mode.",
                "direction",
            )
        if numeric_amount is not None and numeric_amount <= 0:
            return invalid(
                "Input.scroll target mode needs a positive amount (the cap on"
                " each smooth-scroll step). amount=0 reads state and is valid"
                " only for container or viewport mode.",
                "amount",
            )
    if numeric_amount is not None and numeric_amount < 0:
        return invalid("Input.scroll amount must not be negative.", "amount")
    return None


def _check_target_param_requirements(
    method: str,
    params: JsonDict,
    method_schemas: Optional[dict] = None,
) -> Optional[JsonDict]:
    if not isinstance(params, dict):
        return None
    scroll_error = _check_scroll_param_requirements(method, params)
    if scroll_error is not None:
        return scroll_error
    has_selector_or_id = _non_empty_param(params, "selector") or _non_empty_param(params, "id")
    batch_methods = {"DOM.getText", "DOM.getAttribute", "DOM.getImg"}
    raw_targets = params.get("targets")
    has_batch_targets = isinstance(raw_targets, list) and bool(raw_targets)
    if method in batch_methods and has_batch_targets:
        schema = method_schemas.get(method) if isinstance(method_schemas, dict) else None
        schema_params = schema.get("params") if isinstance(schema, dict) else None
        if isinstance(schema_params, dict) and "targets" not in schema_params:
            return {
                "method": method,
                "params": params,
                "status": "capability_not_supported",
                "error": f"The connected ABCP schema for {method} does not expose params.targets.",
                "tool_was_executed": False,
                "next_instruction": (
                    "Use the single-target selector/id shape for this server version,"
                    " or upgrade ABCP before using native batch reads."
                ),
            }
        for index, target in enumerate(raw_targets):
            if not isinstance(target, dict) or not (
                _non_empty_param(target, "selector") or _non_empty_param(target, "id")
            ):
                return {
                    "method": method,
                    "params": params,
                    "status": "invalid_params",
                    "error": f"{method} params.targets[{index}] requires selector or id.",
                    "invalidParam": f"targets[{index}]",
                    "tool_was_executed": False,
                }
            id_error = _check_id_param_format(method, target, method_schemas)
            if id_error is not None:
                id_error["invalidParam"] = f"targets[{index}].id"
                return id_error
        if method == "DOM.getImg":
            if len(raw_targets) > DOM_GET_IMG_MAX_TARGETS:
                return {
                    "method": method,
                    "params": params,
                    "status": "invalid_params",
                    "error": (
                        f"DOM.getImg accepts at most {DOM_GET_IMG_MAX_TARGETS}"
                        f" targets per call; {len(raw_targets)} were supplied."
                    ),
                    "invalidParam": "targets",
                    "tool_was_executed": False,
                    "next_instruction": (
                        "Split the export into batches of"
                        f" {DOM_GET_IMG_MAX_TARGETS} or fewer targets, keeping"
                        " each batch on one page, and read every batch's"
                        " response.data.items independently."
                    ),
                }
            options = params.get("options")
            path = options.get("path") if isinstance(options, dict) else None
            if not isinstance(path, str) or not path.strip():
                return {
                    "method": method,
                    "params": params,
                    "status": "invalid_params",
                    "error": "DOM.getImg requires params.options.path as an output directory.",
                    "invalidParam": "options.path",
                    "tool_was_executed": False,
                }
        return None
    if method == "DOM.getImg" and not has_batch_targets:
        return {
            "method": method,
            "params": params,
            "status": "invalid_params",
            "error": "DOM.getImg requires a non-empty params.targets array.",
            "tool_was_executed": False,
            "missingAnyOf": [["targets"]],
        }
    if method in {
        "DOM.getText",
        "DOM.getAttribute",
        "DOM.inspectSelect",
        "Input.select",
        "Input.type",
    } and not has_selector_or_id:
        return {
            "method": method,
            "params": params,
            "status": "invalid_params",
            "error": f"{method} requires either params.selector or params.id.",
            "tool_was_executed": False,
            "missingAnyOf": [["selector"], ["id"]],
            "next_instruction": (
                "Use DOM.getAXTree to locate a canonical AX id, or provide a"
                " concrete CSS selector. Do not call this method with only"
                " pageId/purpose or without a target element."
            ),
        }
    select_error = _check_select_param_requirements(method, params)
    if select_error is not None:
        return select_error
    if method == "Input.click" and not has_selector_or_id:
        has_coordinates = (
            _non_negative_numeric_param(params, "x")
            and _non_negative_numeric_param(params, "y")
        )
        if not has_coordinates:
            return {
                "method": method,
                "params": params,
                "status": "invalid_params",
                "error": (
                    "Input.click requires selector/id or both non-negative x and y"
                    " coordinates."
                ),
                "tool_was_executed": False,
                "missingAnyOf": [["selector"], ["id"], ["x", "y"]],
                "next_instruction": (
                    "Prefer a current DOM.getAXTree id for Input.click. Use x/y"
                    " only for a verified coordinate fallback such as a backdrop"
                    " click."
                ),
            }
    # Canonical id format guard: catch a malformed id here (clear, actionable
    # error) rather than letting it reach the browser as a -32602 Invalid params.
    id_format_error = _check_id_param_format(method, params, method_schemas)
    if id_format_error is not None:
        return id_format_error
    return None


def _annotate_dom_batch_response(method: str, response: Any) -> Any:
    """Add a compact receipt without changing the native ordered item envelope."""

    if method not in {"DOM.getText", "DOM.getAttribute", "DOM.getImg"}:
        return response
    if not isinstance(response, dict):
        return response
    data = response.get("data")
    items = data.get("items") if isinstance(data, dict) else None
    if not isinstance(items, list):
        return response
    succeeded = sum(
        1 for item in items
        if isinstance(item, dict)
        and item.get("error") is None
        and isinstance(item.get("info"), dict)
    )
    failed = len(items) - succeeded
    # Only the outer envelope and data mapping change. Keep the potentially
    # large native item payload shared instead of recursively copying it.
    copied = dict(response)
    copied_data = dict(data)
    copied["data"] = copied_data
    copied_data["batchSummary"] = {
        "total": len(items),
        "succeeded": succeeded,
        "failed": failed,
        "partialFailure": bool(succeeded and failed),
        "targetOrderPreserved": True,
    }
    return copied


def _check_screenshot_misuse(
    method: str,
    params: JsonDict,
    reason: str = "",
) -> Optional[JsonDict]:
    if method != "Page.screenshot":
        return None
    text = " ".join(
        str(value or "")
        for value in (
            reason,
            params.get("purpose") if isinstance(params, dict) else "",
        )
    )
    if SCREENSHOT_ALLOWED_PURPOSE_RE.search(text):
        return None
    if not SCREENSHOT_MISUSE_RE.search(text):
        return None
    return {
        "status": "rejected",
        "reason": "page_screenshot_not_model_visible",
        "method": method,
        "tool_was_executed": False,
        "next_instruction": (
            "Page.screenshot returns only a savedPath; the model cannot inspect"
            " that image from this tool result. Use DOM.getAXTree,"
            " DOM.getText, DOM.getAttribute, or"
            " visual_verify for bounded visual arbitration."
        ),
    }


def _default_semantic_tree_shadow_dom(
    method: str,
    params: JsonDict,
    method_schemas: Any,
) -> Tuple[JsonDict, bool]:
    """Include shadow content unless the caller explicitly opts out.

    An omitted flag makes a rendered custom-element host look like an empty
    subtree, which led workers to classify tall v-detail-* hosts as a platform
    limitation and skip exportable images. This default is applied only when
    the connected schema advertises the parameter, so older ABCP versions do
    not receive an invented argument. Explicit false remains an escape hatch
    for a deliberately light diagnostic.
    """
    if method != "DOM.getSemanticTree" or "includeShadowDom" in params:
        return params, False
    schema = (
        method_schemas.get(method)
        if isinstance(method_schemas, dict) else None
    )
    schema_params = schema.get("params") if isinstance(schema, dict) else None
    if not isinstance(schema_params, dict) or "includeShadowDom" not in schema_params:
        return params, False
    normalized = dict(params)
    normalized["includeShadowDom"] = True
    return normalized, True


def _normalize_screenshot_output(
    method: str,
    params: JsonDict,
) -> Tuple[JsonDict, Optional[JsonDict]]:
    """Force Page.screenshot to return a file handle, never image bytes.

    Image payload stripping happens only after the WebSocket response arrives,
    which is too late for a large full-page base64 frame.  ABCP owns the output
    path; model-provided path/quality/encoding options are intentionally not
    forwarded because Page.screenshot is a savedPath-only harness primitive.
    """
    return normalize_screenshot_output_params(method, params)


def _attach_normalized_handles(result: JsonDict) -> JsonDict:
    if not isinstance(result, dict):
        return result
    data = _response_data(result)
    handles = {
        key: str(data.get(key))
        for key in ("fleetId", "pageId", "downloadId", "bookmarkId")
        if data.get(key) is not None and str(data.get(key)).strip()
    }
    if handles:
        result = dict(result)
        result["normalizedHandles"] = handles
    return result


async def _maybe_auto_hitl_for_challenge(
    agent: Any,
    method: str,
    params: JsonDict,
    result: JsonDict,
    step: int,
) -> JsonDict:
    if method == "Hitl.requestPause":
        return result
    if getattr(agent, "challenge_adjudicating", False):
        return result
    if _result_has_paused_error(result):
        enriched = dict(result)
        enriched["pausedState"] = {
            "type": "hitl_paused_state",
            "pageId": extract_page_id(params, result),
            "triggerMethod": method,
        }
        enriched["next_instruction"] = (
            "This is an existing HITL paused-state error, not a newly detected"
            " page challenge. Do not call Hitl.requestPause again for this"
            " page. Wait for an explicit HITL resume event, or let LeadAgent"
            " restart with a fresh page if the pause is stale."
        )
        return enriched
    tracker = getattr(agent, "challenge_tracker", None)
    if tracker is None:
        tracker = ChallengeTracker()
        agent.challenge_tracker = tracker
    tracker.cleanup_stale(step)
    page_id = extract_page_id(params, result)
    if not page_id:
        return result
    state = tracker.feed(method=method, params=params, result=result, step=step)
    if state is None:
        return result
    cooldown_until = float(getattr(agent, "hitl_no_repause_until", 0.0) or 0.0)
    if cooldown_until > time.monotonic():
        enriched = dict(result)
        enriched["suspected_challenge"] = {
            **state.to_summary(),
            "adjudication": "cooldown",
            "cooldownMs": int((cooldown_until - time.monotonic()) * 1000),
        }
        enriched["next_instruction"] = (
            "Recent HITL resume is still settling. Re-check Page.getState/DOM.getAXTree"
            " and verify the final URL before requesting another pause."
        )
        return enriched
    guard_ms = _post_hitl_repause_guard_ms(agent, page_id)
    if guard_ms > 0 and tracker.should_adjudicate(page_id, step):
        enriched = dict(result)
        enriched["suspected_challenge"] = {
            **state.to_summary(),
            "adjudication": "post_hitl_recheck",
            "guardMs": guard_ms,
        }
        enriched["next_instruction"] = (
            "This page resumed from HITL recently. Do not request another"
            " automatic pause for the same page yet; first re-check Page.getState,"
            " refresh DOM.getAXTree, and verify the active page contains target"
            " content. If it is still blocked, report the blocker to LeadAgent."
        )
        return enriched
    if not tracker.should_adjudicate(page_id, step):
        enriched = dict(result)
        enriched["suspected_challenge"] = {
            **state.to_summary(),
            "adjudication": "not_ready",
        }
        return enriched
    return await _adjudicate_and_maybe_hitl(agent, page_id, method, result, step)


def _result_has_paused_error(value: Any, *, depth: int = 0) -> bool:
    if depth > 5:
        return False
    if isinstance(value, dict):
        for key, item in value.items():
            key_text = str(key).lower()
            if key_text in {
                "error",
                "message",
                "reason",
                "status",
                "observation",
                "suggested_prompt",
            } and _result_has_paused_error(item, depth=depth + 1):
                return True
            if isinstance(item, (dict, list)) and _result_has_paused_error(item, depth=depth + 1):
                return True
        return False
    if isinstance(value, list):
        return any(_result_has_paused_error(item, depth=depth + 1) for item in value)
    text = str(value or "").lower()
    return "err_page_paused" in text or "paused for human intervention" in text


def _post_hitl_repause_guard_ms(agent: Any, page_id: str) -> int:
    guards = getattr(agent, "hitl_post_resume_guards", None)
    if not isinstance(guards, dict):
        return 0
    now = time.monotonic()
    until = float(guards.get(str(page_id)) or 0.0)
    if until <= now:
        guards.pop(str(page_id), None)
        return 0
    return int((until - now) * 1000)


def _record_post_hitl_repause_guard(agent: Any, page_id: str, seconds: float) -> None:
    seconds = max(0.0, float(seconds or 0.0))
    if seconds <= 0:
        return
    guards = getattr(agent, "hitl_post_resume_guards", None)
    if not isinstance(guards, dict):
        guards = {}
        agent.hitl_post_resume_guards = guards
    guards[str(page_id)] = time.monotonic() + seconds


async def _post_hitl_recovery_loop(
    agent: Any,
    page_id: str,
    wait_result: JsonDict,
    step: int,
) -> JsonDict:
    vl_config = getattr(agent.runtime.harness, "vl", None)
    vl_enabled = bool(vl_config is not None and getattr(vl_config, "enabled", False))
    structural_receipts = getattr(agent, "hitl_structural_challenges", None)
    structural_expected = (
        structural_receipts.get(str(page_id))
        if isinstance(structural_receipts, dict)
        else None
    )
    if not vl_enabled and not isinstance(structural_expected, dict):
        return wait_result

    max_rounds = max(
        1,
        int(
            getattr(
                agent.runtime.harness,
                "hitl_post_resume_confirm_max_rounds",
                3,
            )
            or 1
        ),
    )
    current_wait = dict(wait_result)
    rounds: List[JsonDict] = []
    for round_index in range(max_rounds):
        if current_wait.get("status") != "resumed":
            recovery = current_wait.get("postHitlRecovery")
            if not isinstance(recovery, dict):
                wait_status = str(current_wait.get("status") or "not_resumed")
                precise_statuses = {
                    "browser_error_after_hitl",
                    "still_challenge_after_hitl",
                    "timeout",
                    "page_settled_after_hitl",
                    "stale_pause_deadlock",
                    "hitl_waiting",
                }
                recovery_status = (
                    wait_status if wait_status in precise_statuses else "not_resumed"
                )
                recovery = {"status": recovery_status}
            recovery["rounds"] = rounds
            current_wait["postHitlRecovery"] = {
                **recovery,
            }
            return current_wait

        if isinstance(structural_expected, dict):
            structural_check = await _post_hitl_structural_challenge_check(
                agent, page_id, step, round_index + 1
            )
            if structural_check.get("status") == "challenge_present":
                round_record = {
                    "round": round_index + 1,
                    "structural": structural_check,
                }
                rounds.append(round_record)
                if round_index >= max_rounds - 1:
                    return {
                        **current_wait,
                        "status": "still_challenge_after_hitl",
                        "postHitlRecovery": {
                            "status": "max_rounds_reached",
                            "verificationMode": "structural_axtree",
                            "rounds": rounds,
                        },
                    }
                next_wait = await _repause_for_structural_challenge(
                    agent,
                    page_id,
                    step,
                    round_index + 1,
                    structural_check,
                )
                round_record["retryWait"] = {
                    key: value
                    for key, value in next_wait.items()
                    if key in {"status", "via", "elapsedMs", "reason", "error"}
                }
                current_wait = next_wait
                continue
            if structural_check.get("status") == "check_failed":
                rounds.append({
                    "round": round_index + 1,
                    "structural": structural_check,
                })
                if not vl_enabled:
                    return {
                        **current_wait,
                        "status": "browser_error_after_hitl",
                        "postHitlRecovery": {
                            "status": "structural_check_failed",
                            "rounds": rounds,
                        },
                    }
            elif not vl_enabled:
                current_wait["postHitlRecovery"] = {
                    "status": "recovered_by_structural_axtree",
                    "rounds": rounds + [{
                        "round": round_index + 1,
                        "structural": structural_check,
                    }],
                }
                return current_wait

        if not vl_enabled:
            return current_wait

        vl_result = await _post_hitl_recovery_vl_check(
            agent,
            page_id,
            step,
            round_index + 1,
        )
        round_record: JsonDict = {
            "round": round_index + 1,
            "vl": _compact_vl_for_wait(vl_result),
        }
        rounds.append(round_record)
        verdict = str(vl_result.get("verdict") or "uncertain")
        recovery = str(vl_result.get("recommended_recovery") or "")
        if verdict == "normal_loading" or recovery == "continue":
            current_wait["postHitlRecovery"] = {
                "status": "recovered_by_vl",
                "rounds": rounds,
            }
            return current_wait
        if verdict != "confirmed_challenge":
            current_wait["postHitlRecovery"] = {
                "status": "uncertain_vl",
                "rounds": rounds,
            }
            return current_wait

        decision = await _prompt_post_hitl_confirmation(
            agent,
            {
                "pageId": page_id,
                "round": round_index + 1,
                "maxRounds": max_rounds,
                "vl": vl_result,
            },
        )
        round_record["humanDecision"] = decision
        if decision == "yes":
            current_wait["postHitlRecovery"] = {
                "status": "human_override_recovered",
                "humanOverride": True,
                "rounds": rounds,
            }
            return current_wait
        if decision == "error":
            return {
                **current_wait,
                "status": "browser_error_after_hitl",
                "postHitlRecovery": {
                    "status": "browser_error_after_hitl",
                    "rounds": rounds,
                },
            }

        if round_index >= max_rounds - 1:
            return {
                **current_wait,
                "status": "still_challenge_after_hitl",
                "postHitlRecovery": {
                    "status": "max_rounds_reached",
                    "rounds": rounds,
                },
            }

        next_wait = await _refresh_and_wait_for_post_hitl_retry(
            agent,
            page_id,
            step,
            round_index + 1,
        )
        round_record["retryWait"] = {
            key: value
            for key, value in next_wait.items()
            if key in {"status", "via", "elapsedMs", "reason", "error"}
        }
        current_wait = next_wait

    return current_wait


async def _post_hitl_structural_challenge_check(
    agent: Any,
    page_id: str,
    step: int,
    round_index: int,
) -> JsonDict:
    tree = await _post_hitl_raw_browser_call(
        agent,
        "DOM.getAXTree",
        {
            "pageId": page_id,
            "purpose": (
                "Verify that the embedded CAPTCHA/verification frame is gone"
                f" after HITL round {round_index}."
            ),
        },
        step,
        capture_axtree_text=True,
    )
    raw_text = str(tree.pop("_authAXTreeText", "") or "")
    if _invoke_result_failed(tree) or not raw_text:
        return {
            "status": "check_failed",
            "round": round_index,
            "reason": "fresh_axtree_unavailable",
        }
    evidence = detect_structural_challenge_from_lines(
        raw_text.splitlines(), source_method="DOM.getAXTree"
    )
    if evidence:
        return {
            "status": "challenge_present",
            "round": round_index,
            "evidence": evidence,
        }
    return {
        "status": "challenge_cleared",
        "round": round_index,
        "freshAXTree": True,
    }


async def _repause_for_structural_challenge(
    agent: Any,
    page_id: str,
    step: int,
    round_index: int,
    structural_check: JsonDict,
) -> JsonDict:
    state_call = await _post_hitl_raw_browser_call(
        agent,
        "Page.getState",
        {
            "pageId": page_id,
            "purpose": "Preserve current detail state before repeating HITL for a remaining embedded challenge.",
        },
        step,
    )
    state_data = _response_data(state_call)
    pause_call = await _post_hitl_raw_browser_call(
        agent,
        "Hitl.requestPause",
        {
            "pageId": page_id,
            "purpose": (
                "The embedded verification frame and control remain after"
                " HITL; pause the same detail page again without navigation."
            ),
            "reason": "请继续完成当前详情页中仍存在的验证码/滑块验证。",
        },
        step,
    )
    response = pause_call.get("response") if isinstance(pause_call, dict) else None
    if not _hitl_pause_succeeded(response):
        return {
            "status": "browser_error_after_hitl",
            "error": "Hitl.requestPause failed for remaining structural challenge",
            "structural": structural_check,
        }
    harness_cfg = agent.runtime.harness
    return await wait_for_hitl_resume(
        browser=agent.browser,
        page_id=str(page_id),
        timeout_seconds=getattr(harness_cfg, "hitl_wait_timeout_seconds", 900.0),
        poll_interval_seconds=getattr(harness_cfg, "hitl_poll_interval_seconds", 2.0),
        diagnostics=getattr(agent, "diagnostics", None),
        logger=agent.logger,
        challenge_verifier=_make_hitl_challenge_verifier(agent, str(page_id), step),
        pause_snapshot={
            "url": str(state_data.get("url") or ""),
            "title": str(state_data.get("title") or ""),
            "round": round_index,
        },
    )


async def _post_hitl_recovery_vl_check(
    agent: Any,
    page_id: str,
    step: int,
    round_index: int,
) -> JsonDict:
    agent.challenge_adjudicating = True
    try:
        return await _visual_verify(
            agent,
            {
                "pageId": page_id,
                "selector": "",
                "id": "",
                "fullPage": False,
                "mode": "challenge_detection",
                "_force": True,
                "question": (
                    "After the user handled HITL, has this browser page"
                    " recovered from CAPTCHA/security verification and returned"
                    " to normal website content, or is it still blocked by a"
                    " challenge?"
                ),
                "expected": {
                    "pageId": page_id,
                    "postHitlRecoveryRound": round_index,
                },
            },
            step,
        )
    finally:
        agent.challenge_adjudicating = False


def _compact_vl_for_wait(vl_result: JsonDict) -> JsonDict:
    return {
        key: value
        for key, value in vl_result.items()
        if key in {
            "status",
            "verdict",
            "confidence",
            "visible_evidence",
            "recommended_recovery",
            "reason",
            "screenshotPath",
            "mode",
        }
    }


async def _prompt_post_hitl_confirmation(agent: Any, payload: JsonDict) -> str:
    handler = getattr(agent, "post_hitl_confirmation_handler", None)
    if callable(handler):
        value = handler(payload)
        if hasattr(value, "__await__"):
            value = await value
        return _normalize_post_hitl_confirmation(value)

    vl = payload.get("vl") if isinstance(payload.get("vl"), dict) else {}
    lines = [
        "",
        "[ABCP HITL] VL still sees a challenge after user intervention.",
        f"  pageId: {payload.get('pageId')}",
        f"  round: {payload.get('round')}/{payload.get('maxRounds')}",
        f"  verdict: {vl.get('verdict')} confidence={vl.get('confidence')}",
        f"  reason: {vl.get('reason')}",
        f"  screenshot: {vl.get('screenshotPath')}",
        "  Choose: yes = browser page is actually recovered; no = refresh and keep handling HITL; error = browser/pageId is wrong or broken.",
    ]
    if not sys.stdin or not sys.stdin.isatty():
        logger = getattr(agent, "logger", None)
        if logger is not None and hasattr(logger, "write"):
            logger.write(
                "hitl.post_resume.confirmation_non_tty",
                {
                    "pageId": payload.get("pageId"),
                    "round": payload.get("round"),
                    "maxRounds": payload.get("maxRounds"),
                    "decision": "error",
                    "reason": "stdin is not interactive",
                },
            )
        return "error"
    print("\n".join(lines), flush=True)
    try:
        value = await asyncio.to_thread(input, "Post-HITL confirmation [yes/no/error]: ")
    except (EOFError, KeyboardInterrupt):
        logger = getattr(agent, "logger", None)
        if logger is not None and hasattr(logger, "write"):
            logger.write(
                "hitl.post_resume.confirmation_input_failed",
                {
                    "pageId": payload.get("pageId"),
                    "round": payload.get("round"),
                    "decision": "error",
                },
            )
        value = "error"
    return _normalize_post_hitl_confirmation(value)


def _normalize_post_hitl_confirmation(value: Any) -> str:
    normalized = str(value or "").strip().lower()
    if normalized in {"yes", "y", "true", "ok", "continue", "normal"}:
        return "yes"
    if normalized in {"no", "n", "false", "retry", "refresh"}:
        return "no"
    if normalized in {"error", "err", "browser_error", "broken", "abort", "stop"}:
        return "error"
    return "error"


async def _refresh_and_wait_for_post_hitl_retry(
    agent: Any,
    page_id: str,
    step: int,
    round_index: int,
) -> JsonDict:
    state_call = await _post_hitl_raw_browser_call(
        agent,
        "Page.getState",
        {
            "pageId": page_id,
            "purpose": "post-HITL terminal confirmation requested refresh; read current URL before retry",
        },
        step,
    )
    current_url = str(_response_data(state_call).get("url") or "").strip()
    if not current_url:
        return {
            "status": "browser_error_after_hitl",
            "error": "Page.getState did not return a URL for post-HITL refresh",
            "state": state_call,
        }

    navigate_call = await _post_hitl_raw_browser_call(
        agent,
        "Page.navigate",
        {
            "pageId": page_id,
            "url": current_url,
            "purpose": "refresh page after human confirmed the HITL challenge is still visible",
        },
        step,
    )
    if navigate_call.get("error"):
        return {
            "status": "browser_error_after_hitl",
            "error": "Page.navigate failed during post-HITL retry",
            "navigate": navigate_call,
        }

    pause_call = await _post_hitl_raw_browser_call(
        agent,
        "Hitl.requestPause",
        {
            "pageId": page_id,
            "purpose": (
                "Post-HITL confirmation reported the page still shows a challenge;"
                " pause again so the user can continue handling it."
            ),
        },
        step,
    )
    response = pause_call.get("response") if isinstance(pause_call, dict) else None
    if not _hitl_pause_succeeded(response):
        return {
            "status": "browser_error_after_hitl",
            "error": "Hitl.requestPause failed during post-HITL retry",
            "pause": pause_call,
        }

    harness_cfg = agent.runtime.harness
    retry_snapshot = {
        "url": str(_response_data(navigate_call).get("url") or current_url or ""),
        "title": str(_response_data(navigate_call).get("title") or ""),
    }
    wait_result = await wait_for_hitl_resume(
        browser=agent.browser,
        page_id=str(page_id),
        timeout_seconds=getattr(harness_cfg, "hitl_wait_timeout_seconds", 900.0),
        poll_interval_seconds=getattr(harness_cfg, "hitl_poll_interval_seconds", 2.0),
        diagnostics=getattr(agent, "diagnostics", None),
        logger=agent.logger,
        challenge_verifier=_make_hitl_challenge_verifier(agent, str(page_id), step),
        pause_snapshot=retry_snapshot,
    )
    wait_result = dict(wait_result)
    wait_result["postHitlRetry"] = {
        "round": round_index,
        "refreshedUrl": current_url,
        "navigate": {
            "status": _response_data(navigate_call).get("status"),
            "url": _response_data(navigate_call).get("url"),
            "title": _response_data(navigate_call).get("title"),
        },
    }
    return wait_result


async def _post_hitl_raw_browser_call(
    agent: Any,
    method: str,
    params: JsonDict,
    step: int,
    *,
    capture_axtree_text: bool = False,
) -> JsonDict:
    private_axtree_text = ""
    try:
        _ensure_hitl_request_reason(method, params, str(params.get("purpose") or ""))
        runner = getattr(agent, "render_recovery_runner", None)
        if runner is None:
            runner = build_render_recovery_runner(
                browser=agent.browser,
                logger=agent.logger,
                capability_methods=agent.capability_methods,
                recent_recoveries=agent._render_recovery_recent,
            )
            agent.render_recovery_runner = runner
        response, _recovery = await runner.call(method, params)
        response = agent._capture_artifacts(method, response)
        record_file_action = getattr(agent, "_capture_file_action", None)
        if callable(record_file_action):
            record_file_action(method, params, response)
        if capture_axtree_text and method == "DOM.getAXTree":
            private_axtree_text = "\n".join(_axtree_lines_from_value(response))
        response = agent._offload_response(method, params, response, step)
        result = {"method": method, "params": params, "response": response}
    except FleetClickGateTimeout as exc:
        result = {
            "method": method,
            "params": params,
            "status": "fleet_click_gated",
            "error": str(exc),
            **exc.receipt,
        }
        attach_method_schema(
            result, method, getattr(agent, "method_schemas", {})
        )
    except ABCPTransportError as exc:
        result = {
            "method": method,
            "params": params,
            "status": "browser_error_after_hitl",
            "error": str(exc),
            **_transport_error_metadata(method, exc),
        }
        attach_method_schema(result, method, getattr(agent, "method_schemas", {}))
    except Exception as exc:
        result = {
            "method": method,
            "params": params,
            "status": "browser_error_after_hitl",
            **exception_payload(exc),
        }

    attach_error_classification(result, method=method)
    result = _apply_select_failure_guidance(agent, method, params, result)
    result = _attach_normalized_handles(result)
    logger = getattr(agent, "logger", None)
    if logger is not None and hasattr(logger, "write"):
        trim_for_log = getattr(agent, "_trim_for_log", lambda value: value)
        logger.write("hitl.post_resume.raw_call", trim_for_log(result))
    if private_axtree_text:
        # Ephemeral proof input for AuthFleetLedger. Attach only after logging;
        # callers must not persist or expose the raw accessibility text.
        result["_authAXTreeText"] = private_axtree_text
    return result


def _clear_challenge_state_after_recovery(agent: Any, page_id: str, *, event: str) -> None:
    """Shared bookkeeping for "this page is no longer challenged".

    Used by the HITL resume path and by a successful VL auto-solve: drop the
    accumulated suspicion, forget the structural receipt, and hold a short
    re-pause guard so residual challenge wording in the next tool result cannot
    trigger a second pause before the worker has re-perceived the page.
    """
    harness_cfg = getattr(getattr(agent, "runtime", None), "harness", None)
    cooldown_seconds = float(
        getattr(harness_cfg, "hitl_no_repause_cooldown_seconds", 8.0) or 0.0
    )
    agent.hitl_no_repause_until = time.monotonic() + max(0.0, cooldown_seconds)
    guard_seconds = float(
        getattr(harness_cfg, "hitl_post_resume_guard_seconds", 30.0) or 0.0
    )
    _record_post_hitl_repause_guard(
        agent, str(page_id), max(cooldown_seconds, guard_seconds)
    )
    tracker = getattr(agent, "challenge_tracker", None)
    if tracker is not None:
        tracker.clear_page(str(page_id))
    structural_receipts = getattr(agent, "hitl_structural_challenges", None)
    if isinstance(structural_receipts, dict):
        structural_receipts.pop(str(page_id), None)
    logger = getattr(agent, "logger", None)
    if logger is not None and hasattr(logger, "write"):
        logger.write(event, {"pageId": str(page_id)})


async def _maybe_autosolve_before_hitl(
    agent: Any,
    page_id: str,
    step: int,
    *,
    trigger: str,
    vl_only_detection: bool,
    reason: str,
) -> JsonDict:
    """Bounded VL attempt to clear a detected challenge BEFORE asking a human.

    Returns {} when the role is switched off, so a disabled deployment keeps the
    exact pre-existing straight-to-HITL behavior. Never raises: the human path
    must stay reachable no matter how the solve fails.
    """
    from harness.tools.browser_tools.captcha_autosolve import (
        autosolve_enabled,
        maybe_autosolve_captcha,
    )

    if not autosolve_enabled(agent):
        return {}
    try:
        return await maybe_autosolve_captcha(
            agent,
            str(page_id),
            step,
            trigger=trigger,
            vl_only_detection=vl_only_detection,
            reason=reason,
        )
    except Exception as exc:
        logger = getattr(agent, "logger", None)
        if logger is not None and hasattr(logger, "write"):
            logger.write("vl.captcha_autosolve.failed", {
                "pageId": str(page_id),
                "trigger": trigger,
                "errorType": type(exc).__name__,
                "error": str(exc)[:300],
            })
        return {
            "status": "error",
            "attempted": False,
            "trigger": trigger,
            "errorType": type(exc).__name__,
            "reason": str(exc)[:300],
        }


def _autosolve_cleared(receipt: Any) -> bool:
    from harness.tools.browser_tools.captcha_autosolve import CLEARED_STATUSES

    return bool(
        isinstance(receipt, dict)
        and receipt.get("attempted")
        and str(receipt.get("status") or "") in CLEARED_STATUSES
    )


def _autosolve_cleared_result(
    agent: Any,
    enriched: JsonDict,
    page_id: str,
    step: int,
    solve: JsonDict,
    *,
    pause_skipped: bool = False,
) -> JsonDict:
    """Build the "solved without a human" result and reset the page's challenge
    bookkeeping so the next observation starts from a clean slate."""
    out = dict(enriched)
    suspected = dict(out.get("suspected_challenge") or {})
    suspected["adjudication"] = "auto_solved_by_vl"
    out["suspected_challenge"] = suspected
    out["captchaAutoSolve"] = solve
    _clear_challenge_state_after_recovery(
        agent, page_id, event="challenge.autosolve_cleared"
    )
    out["next_instruction"] = (
        (
            "Your Hitl.requestPause was NOT executed: the harness cleared this"
            " challenge automatically with a bounded VL solve first, so no human"
            " was interrupted and no pause is pending."
            if pause_skipped
            else "The harness cleared this challenge automatically with a bounded"
            " VL solve; no human pause was requested and no Hitl.* call is pending."
        )
        + " Treat the page as unverified until you re-perceive it: refresh"
        " Page.getState and DOM.getAXTree, confirm the target content is"
        " actually present, then continue the original action. If the challenge"
        " is still there, report it — this page will not be auto-solved again."
    )
    return out


def _reason_with_autosolve(reason: str, solve: Any) -> str:
    """Tell the human why automation gave up, in the pause reason they read."""
    from harness.tools.browser_tools.captcha_autosolve import solve_summary

    summary = solve_summary(solve)
    return f"{reason} ({summary})" if summary else reason


def _model_pause_challenge_evidence(agent: Any, params: JsonDict) -> Optional[JsonDict]:
    """Decide whether a model-issued pause is CAPTCHA-shaped enough to try solving.

    Login walls, SMS/QR/2FA and payment confirmations are human-only by nature —
    spending a screenshot plus a VL round-trip on them would only make the person
    wait longer. The evidence is reused, never re-invented: the page's own
    accumulated challenge state, or the model's stated reason matching the shared
    high-confidence challenge vocabulary. Returns None when it is not worth trying.
    """
    page_id = str(params.get("pageId") or "").strip()
    tracker = getattr(agent, "challenge_tracker", None)
    state = tracker.get_state(page_id) if (tracker is not None and page_id) else None
    if state is not None and state.structural_challenge:
        return {"source": "structural_challenge", "vlOnly": False}
    if state is not None and state.high_confidence_hit:
        return {"source": "high_confidence_signal", "vlOnly": True}
    haystack = " ".join(
        str(params.get(key) or "") for key in ("reason", "purpose")
    ).lower()
    if any(keyword in haystack for keyword in HIGH_CONFIDENCE_CHALLENGE_KEYWORDS):
        return {"source": "model_pause_reason", "vlOnly": True}
    return None


async def _maybe_autosolve_before_model_pause(
    agent: Any,
    method: str,
    params: JsonDict,
    step: int,
) -> Optional[JsonDict]:
    """Intercept a model-issued Hitl.requestPause for a visual challenge.

    Returns a short-circuit result when the challenge was solved (the pause is
    never issued), else None so the pause proceeds exactly as before — with the
    solve attempt appended to the reason the human reads.
    """
    if method != "Hitl.requestPause" or not isinstance(params, dict):
        return None
    from harness.tools.browser_tools.captcha_autosolve import autosolve_enabled

    if not autosolve_enabled(agent):
        return None
    page_id = str(params.get("pageId") or "").strip()
    if not page_id:
        return None
    evidence = _model_pause_challenge_evidence(agent, params)
    if evidence is None:
        return None
    reason = str(params.get("reason") or params.get("purpose") or "")
    solve = await _maybe_autosolve_before_hitl(
        agent,
        page_id,
        step,
        trigger="model_request_pause",
        vl_only_detection=bool(evidence.get("vlOnly", True)),
        reason=reason,
    )
    if isinstance(solve, dict):
        solve = {**solve, "detectionEvidence": evidence}
    if not _autosolve_cleared(solve):
        # The pause the model asked for still happens; the human just gets to
        # see that automation already tried and how it failed.
        enriched_reason = _reason_with_autosolve(reason, solve)
        if enriched_reason != reason:
            params["reason"] = enriched_reason
        return None
    return _autosolve_cleared_result(
        agent,
        {
            "method": method,
            "status": "captcha_auto_solved",
            "tool_was_executed": False,
            "pageId": page_id,
        },
        page_id,
        step,
        solve,
        pause_skipped=True,
    )


async def _adjudicate_and_maybe_hitl(
    agent: Any,
    page_id: str,
    trigger_method: str,
    result: JsonDict,
    step: int,
) -> JsonDict:
    tracker = getattr(agent, "challenge_tracker", None)
    state = tracker.get_state(page_id) if tracker is not None else None
    summary = state.to_summary() if state is not None else {"pageId": page_id}
    vl_config = getattr(agent.runtime.harness, "vl", None)
    vl_enabled = bool(vl_config is not None and getattr(vl_config, "enabled", False))
    enriched = copy.deepcopy(result)
    enriched["suspected_challenge"] = {
        **summary,
        "adjudication": "pending",
        "triggerMethod": trigger_method,
    }

    # A challenge-labelled embedded root plus an actionable verification
    # control is stronger than a whole-page visual verdict.  Small iframes can
    # be visually inconspicuous while still blocking one business subrequest;
    # do not let VL "normal_loading" suppress deterministic AX evidence.
    if state is not None and state.structural_challenge:
        evidence = state.structural_evidence or {}
        controls = (
            evidence.get("controls")
            if isinstance(evidence.get("controls"), list)
            else []
        )
        control_labels = [
            str(control.get("label") or control.get("role") or "").strip()
            for control in controls
            if isinstance(control, dict)
        ]
        control_summary = ", ".join(label for label in control_labels if label)[:160]
        enriched["suspected_challenge"]["adjudication"] = "structural_confirmed"
        challenge_reason = (
            "Embedded verification frame detected: "
            f"{evidence.get('rootLabel') or 'challenge'}"
            + (f"; control: {control_summary}" if control_summary else "")
        )
        solve = await _maybe_autosolve_before_hitl(
            agent,
            page_id,
            step,
            trigger="structural_challenge",
            vl_only_detection=False,
            reason=challenge_reason,
        )
        if _autosolve_cleared(solve):
            return _autosolve_cleared_result(agent, enriched, page_id, step, solve)
        if solve:
            enriched["captchaAutoSolve"] = solve
        enriched["autoHitl"] = await _request_hitl_for_challenge(
            agent,
            page_id,
            trigger_method,
            step,
            reason=_reason_with_autosolve(challenge_reason, solve),
            trigger_result=result,
        )
        enriched["next_instruction"] = (
            "A cross-frame AXTree challenge and an actionable verification"
            " control were detected. The harness requested HITL without"
            " allowing a whole-page VL verdict to override that evidence."
            " After resume, follow autoHitl.resumeCheckpoint and revalidate"
            " the original business content."
        )
        return enriched

    if vl_enabled:
        agent.challenge_adjudicating = True
        try:
            vl_result = await _visual_verify(
                agent,
                {
                    "pageId": page_id,
                    "selector": "",
                    "id": "",
                    "fullPage": False,
                    "mode": "challenge_detection",
                    "question": (
                        "Is this page blocked by CAPTCHA, Cloudflare/security"
                        " verification, or another challenge requiring human"
                        " action?"
                    ),
                    "expected": {
                        "pageId": page_id,
                        "triggerMethod": trigger_method,
                        "suspectedChallenge": summary,
                    },
                },
                step,
            )
        finally:
            agent.challenge_adjudicating = False
        verdict = str(vl_result.get("verdict") or "uncertain")
        if tracker is not None:
            tracker.record_vl_verdict(page_id, step, verdict)
        enriched["challengeAdjudication"] = vl_result
        if verdict == "confirmed_challenge":
            challenge_reason = str(vl_result.get("reason") or "VL confirmed challenge")
            solve = await _maybe_autosolve_before_hitl(
                agent,
                page_id,
                step,
                trigger="vl_confirmed_challenge",
                vl_only_detection=True,
                reason=challenge_reason,
            )
            if _autosolve_cleared(solve):
                return _autosolve_cleared_result(agent, enriched, page_id, step, solve)
            if solve:
                enriched["captchaAutoSolve"] = solve
            enriched["autoHitl"] = await _request_hitl_for_challenge(
                agent,
                page_id,
                trigger_method,
                step,
                reason=_reason_with_autosolve(challenge_reason, solve),
                trigger_result=result,
            )
            enriched["next_instruction"] = (
                "The visual adjudicator confirmed a challenge and the harness"
                " requested human intervention. Inspect autoHitl.hitl_wait."
            )
        elif verdict == "normal_loading":
            enriched["next_instruction"] = (
                "Visual adjudicator classified the page as normal loading. Continue"
                " without requesting HITL during the cooldown window."
            )
        elif verdict == "unrelated_block":
            enriched["next_instruction"] = (
                "Visual adjudicator found an unrelated block. Record the blocker or"
                " let LeadAgent pivot strategy."
            )
        else:
            enriched["next_instruction"] = (
                "Challenge suspicion remains uncertain after visual adjudication."
                " Observe once more or hand the blocker to LeadAgent."
            )
        return enriched

    if state is not None and state.high_confidence_hit:
        enriched["autoHitl"] = await _request_hitl_for_challenge(
            agent,
            page_id,
            trigger_method,
            step,
            reason="High-confidence CAPTCHA/challenge keyword with VL disabled",
            trigger_result=result,
        )
        enriched["next_instruction"] = (
            "VL is disabled, but a high-confidence challenge keyword was found."
            " The harness requested human intervention."
        )
        return enriched

    enriched["suspected_challenge"]["adjudication"] = "vl_unavailable"
    enriched["suspected_challenge"]["vl_unavailable_reason"] = (
        "vl.enabled=false in config"
    )
    enriched["next_instruction"] = (
        "This page is suspected to be blocked by a challenge, but visual"
        " adjudication is unavailable and no high-confidence CAPTCHA keyword"
        " was found. Do not poll indefinitely; report the blocker or let"
        " LeadAgent decide."
    )
    return enriched


def _hitl_pause_rounds(agent: Any) -> Dict[str, int]:
    """Pause rounds spent, keyed by pageId; the "" key is the worker total."""
    rounds = getattr(agent, "hitl_pause_rounds", None)
    if not isinstance(rounds, dict):
        rounds = {}
        agent.hitl_pause_rounds = rounds
    return rounds


def _count_hitl_pause_round(agent: Any, page_id: str) -> Dict[str, int]:
    """Charge one pause round. Called from exactly one place.

    `_claim_fleet_auth_barrier_for_hitl` is the single choke point every
    dispatched Hitl.requestPause crosses, so counting there — and only there —
    keeps the auto path (which claims the barrier itself first, then dispatches
    through the same guard) from being charged twice for one pause.
    """
    rounds = _hitl_pause_rounds(agent)
    key = str(page_id or "")
    if key:
        rounds[key] = int(rounds.get(key, 0)) + 1
    rounds[""] = int(rounds.get("", 0)) + 1
    return rounds


async def _refuse_hitl(
    agent: Any,
    admission: JsonDict,
    page_id: str,
    trigger_method: str,
) -> JsonDict:
    """Hand back a refusal, releasing the lease without opening the gate."""
    released = await _release_fleet_auth_after_hitl_refusal(
        agent, f"HITL refused: {admission['reasonKind']}"
    )
    if released:
        admission = {**admission, "fleetAuthBarrier": released}
    logger = getattr(agent, "logger", None)
    if logger is not None and hasattr(logger, "write"):
        logger.write("hitl.refused", {
            "pageId": page_id,
            "triggerMethod": trigger_method,
            "reasonKind": admission["reasonKind"],
            "budgetScope": admission.get("budgetScope"),
            "pauseRoundsUsed": admission.get("pauseRoundsUsed"),
        })
    return admission


def _hitl_admission(agent: Any, page_id: str) -> Optional[JsonDict]:
    """Decide whether asking a human is still worth doing.

    Returns None to proceed, or a terminal receipt to hand back instead. Both
    refusals are returned BEFORE the fleet auth barrier is claimed: a pause
    nobody will answer must not also shut the gate on every sibling worker.
    """
    harness_cfg = getattr(getattr(agent, "runtime", None), "harness", None)
    attendance = str(
        getattr(harness_cfg, "hitl_attendance", "attended") or "attended"
    ).strip().lower()
    if attendance == "unattended":
        return {
            "status": "hitl_unattended",
            "reasonKind": "hitl_unattended",
            "tool_was_executed": False,
            "retryable": False,
            "pageId": page_id,
            "next_instruction": (
                "This deployment is configured as unattended (hitl_attendance)."
                " No human will resolve this challenge. Do not pause, retry, or"
                " navigate around it: report it as a blocker and finish."
            ),
        }
    rounds = _hitl_pause_rounds(agent)
    per_page = max(0, int(getattr(harness_cfg, "hitl_max_pause_rounds_per_page", 3) or 0))
    per_worker = max(0, int(getattr(harness_cfg, "hitl_max_pause_rounds_per_worker", 3) or 0))
    page_used = int(rounds.get(str(page_id), 0))
    worker_used = int(rounds.get("", 0))
    if per_page and page_used >= per_page:
        scope, used, budget = "page", page_used, per_page
    elif per_worker and worker_used >= per_worker:
        scope, used, budget = "worker", worker_used, per_worker
    else:
        return None
    return {
        "status": "hitl_budget_exhausted",
        "reasonKind": "hitl_budget_exhausted",
        "tool_was_executed": False,
        "retryable": False,
        "pageId": page_id,
        "budgetScope": scope,
        "pauseRoundsUsed": used,
        "pauseRoundsBudget": budget,
        "next_instruction": (
            f"The cumulative HITL budget for this {scope} is spent"
            f" ({used}/{budget} pauses). A human did not resolve the challenge"
            " in the earlier rounds and re-pausing holds the fleet gate shut"
            " for every sibling worker. Report this as a blocker and finish."
        ),
    }


async def _release_fleet_auth_after_hitl_refusal(agent: Any, reason: str) -> JsonDict:
    """Hand the gate back when this worker will not be asking a human."""
    barrier = getattr(agent, "fleet_auth_barrier", None)
    fleet_id = str(getattr(agent, "assigned_fleet_id", "") or "").strip()
    worker_id = str(getattr(agent, "worker_id", "") or "").strip()
    if barrier is None or not fleet_id or not worker_id:
        return {}
    try:
        return await barrier.relinquish(fleet_id, worker_id, reason=reason) or {}
    except Exception:  # noqa: BLE001 - a refusal must never raise
        return {}


async def _request_hitl_for_challenge(
    agent: Any,
    page_id: str,
    trigger_method: str,
    step: int,
    *,
    reason: str,
    trigger_result: Optional[JsonDict] = None,
) -> JsonDict:
    # Checked here as well as at the dispatch guard, because this path claims
    # the barrier BEFORE dispatching: a pause nobody will answer must not shut
    # the gate on every sibling worker first and be refused afterwards.
    # Releasing the lease is not the same as opening the gate — the fleet is
    # still challenged, so waiters keep getting a terminal verdict rather than
    # a pass onto a cookie jar that is still under risk control. The round is
    # NOT charged here; the dispatch guard owns accounting.
    admission = _hitl_admission(agent, page_id)
    if admission is not None:
        return await _refuse_hitl(agent, admission, page_id, trigger_method)
    structural_evidence = (
        trigger_result.get("structuralChallenge")
        if isinstance(trigger_result, dict)
        and isinstance(trigger_result.get("structuralChallenge"), dict)
        else None
    )
    barrier = getattr(agent, "fleet_auth_barrier", None)
    fleet_id = str(getattr(agent, "assigned_fleet_id", "") or "").strip()
    worker_id = str(getattr(agent, "worker_id", "") or "").strip()
    barrier_claim: JsonDict = {}
    if barrier is not None and fleet_id and worker_id:
        barrier_claim = await barrier.claim(fleet_id, worker_id, reason)
        if not barrier_claim.get("claimed"):
            return {
                "status": "fleet_auth_gated",
                "reasonKind": "fleet_auth_gated",
                "fleetId": fleet_id,
                "resolverWorkerId": barrier_claim.get("resolverWorkerId"),
                "tool_was_executed": False,
                "retryable": True,
            }
    if isinstance(structural_evidence, dict):
        receipts = getattr(agent, "hitl_structural_challenges", None)
        if not isinstance(receipts, dict):
            receipts = {}
            agent.hitl_structural_challenges = receipts
        receipts[str(page_id)] = dict(structural_evidence)
    # Capture the pre-pause surface here so every auto-HITL path (VL-confirmed,
    # VL-disabled high-confidence, future callers) records the snapshot that the
    # verified-settlement title gate compares against.
    trigger_data = _response_data(trigger_result) if trigger_result else {}
    snapshot = {
        "url": str(trigger_data.get("url") or ""),
        "title": str(trigger_data.get("title") or ""),
    }
    if snapshot["url"] or snapshot["title"]:
        snapshots = getattr(agent, "hitl_pause_snapshots", None)
        if not isinstance(snapshots, dict):
            snapshots = {}
            agent.hitl_pause_snapshots = snapshots
        snapshots[str(page_id)] = snapshot
    rounds = _hitl_pause_rounds(agent)
    agent.logger.write(
        "hitl.auto_request_pause",
        {
            "pageId": page_id,
            "triggerMethod": trigger_method,
            "reason": reason,
            "structuralEvidence": structural_evidence,
            "pauseSnapshot": snapshot,
            "authBarrier": barrier_claim or None,
            # Charged by the dispatch guard below, so this reports the rounds
            # already spent before this one.
            "pauseRoundsBefore": int(rounds.get(str(page_id), 0)),
            "workerPauseRoundsBefore": int(rounds.get("", 0)),
        },
    )
    agent.challenge_adjudicating = True
    try:
        pause_result = await _invoke_browser_method(
            agent,
            "Hitl.requestPause",
            {
                "pageId": page_id,
                "purpose": (
                    "Anti-bot verification or CAPTCHA-like challenge was"
                    " detected; pause for user intervention."
                ),
                "reason": reason,
            },
            step,
        )
    finally:
        agent.challenge_adjudicating = False
    if isinstance(pause_result, dict):
        pause_result = dict(pause_result)
        pause_result["resumeCheckpoint"] = {
            "pageId": page_id,
            "triggerMethod": trigger_method,
            "challengeReason": reason,
            "structuralEvidence": structural_evidence,
            "requiredSequence": [
                "Page.getState",
                "DOM.getAXTree",
                "retry_original_materialization_if_needed",
                "DOM.getSemanticTree",
                "validate_requested_record_count",
            ],
            "successCondition": (
                "The challenge frame is absent and the original task-required"
                " content or requested record count is materialized."
            ),
            "doNotAccept": [
                "normal page title alone",
                "drawer shell alone",
                "loading skeleton",
                "preview rows outside the target subtree",
            ],
        }
    return pause_result

PROGRESS_GATE_MAX_BLOCKS = 2
PROGRESS_GATE_RECOVERY_TOOLS = frozenset({
    *NO_ARTIFACT_DIAGNOSTIC_TOOLS,
    "local_fs_read",
    "local_fs_search",
})


def _record_extraction_persisted(result: JsonDict) -> bool:
    return bool(isinstance(result, dict) and str(result.get("savedPath") or "").strip())


def _gate_subject_tool(next_tool: str, tool_input: JsonDict) -> str:
    if str(next_tool or "") == "browser_call":
        method = str(tool_input.get("method") or "").strip()
        if method:
            return method
    return str(next_tool or "").strip()


def _call_extraction_progress_gate(
    agent: Any,
    next_tool: str,
    tool_input: JsonDict,
) -> Optional[JsonDict]:
    try:
        return _check_extraction_progress_gate(agent, next_tool, tool_input)
    except TypeError as exc:
        # Some tests/plugins monkeypatch the internal gate using the old
        # two-argument signature. Keep that compatibility while the real helper
        # accepts tool_input so browser_call can be classified by method.
        if "positional" not in str(exc) and "argument" not in str(exc):
            raise
        return _check_extraction_progress_gate(agent, next_tool)


def _check_extraction_progress_gate(
    agent: Any,
    next_tool: str,
    tool_input: Optional[JsonDict] = None,
) -> Optional[JsonDict]:
    if next_tool == "record_extraction":
        return None
    subject_tool = _gate_subject_tool(next_tool, tool_input or {})
    if subject_tool in PROGRESS_GATE_RECOVERY_TOOLS:
        pending = getattr(agent, "pending_unrecorded_extraction", None)
        if isinstance(pending, dict):
            pending["recoveryBypassCount"] = (
                optional_int(pending.get("recoveryBypassCount"), 0) or 0
            ) + 1
            agent.pending_unrecorded_extraction = pending
        return None
    pending = getattr(agent, "pending_unrecorded_extraction", None)
    if not isinstance(pending, dict):
        return None
    turns = optional_int(pending.get("turns"), 0) or 0
    if turns < 1:
        pending["turns"] = turns + 1
        agent.pending_unrecorded_extraction = pending
        return None
    gate_blocks = optional_int(pending.get("gateBlocks"), 0) or 0
    if gate_blocks >= PROGRESS_GATE_MAX_BLOCKS:
        downgraded = {
            "status": "progress_gate_downgraded",
            "reason": "unrecorded_structured_rows_gate_limit",
            "rowCount": pending.get("rowCount"),
            "source": pending.get("source"),
            "tool": subject_tool,
            "gateBlocks": gate_blocks,
            "tool_was_executed": True,
            "next_instruction": (
                "The unrecorded-rows gate reached its bounded limit and was"
                " downgraded so recovery tools can continue. Persist trustworthy"
                " rows when possible; otherwise gather evidence and finalize with"
                " a blocker or target_absent/instruction_infeasible classification."
            ),
        }
        agent.pending_unrecorded_extraction = None
        if hasattr(agent, "trace") and isinstance(agent.trace, list):
            agent.trace.append({"type": "progress_gate_downgraded", "result": downgraded})
        logger = getattr(agent, "logger", None)
        if logger is not None and hasattr(logger, "write"):
            logger.write("progress_gate.downgraded", downgraded)
        return None
    pending["gateBlocks"] = gate_blocks + 1
    agent.pending_unrecorded_extraction = pending
    return {
        "status": "progress_gate",
        "reason": "unrecorded_structured_rows",
        "rowCount": pending.get("rowCount"),
        "source": pending.get("source"),
        "tool": subject_tool,
        "gateBlocks": pending.get("gateBlocks"),
        "tool_was_executed": False,
        "next_instruction": (
            "You already extracted structured rows but did not persist them."
            " Call record_extraction now if the rows are relevant, or use recovery"
            " tools such as DOM.getAXTree/DOM.getText/DOM.getAttribute/Input.scroll to"
            " gather missing evidence, or call final_answer with a blocker if"
            " they are not trustworthy."
        ),
    }


def _check_cross_task_memory_scope(
    agent: Any,
    method: str,
    params: JsonDict,
) -> Optional[JsonDict]:
    """Block Memory.get/save against another task's scope.

    Task-scope memories carry a previous task's objective/steps; reading
    them contaminates the current worker's premise (2cb616: "scroll to
    rank 50, extract 11 rows" restored as established knowledge), and
    writing them corrupts the other task's record. Registration already
    strips foreign entries; this guard closes the direct-query path.
    Non-task scopes (auth fleet, fleet ids) are untouched.
    """
    if method not in {"Memory.get", "Memory.save"}:
        return None
    scope = str((params or {}).get("scope") or "").strip()
    if not scope:
        return None
    parts = scope.split(":")
    if len(parts) < 3 or parts[-1] != "task":
        return None
    scope_task_id = parts[-2]
    # Only gate scopes whose middle segment looks like a harness task id
    # (long hex) — custom scopes keep working.
    if not re.fullmatch(r"[0-9a-f]{16,}", scope_task_id):
        return None
    task_dir = getattr(getattr(agent, "logger", None), "task_dir", None)
    current_task_id = str(getattr(task_dir, "name", "") or "")
    if not current_task_id or scope_task_id == current_task_id:
        return None
    return {
        "status": "rejected",
        "method": method,
        "error": (
            f"{method} targets another task's memory scope: {scope}"
        ),
        "tool_was_executed": False,
        "next_instruction": (
            "Memory from other tasks is historical context, not instructions"
            " for the current task. Use your own task scope"
            f" (…:{current_task_id}:task) and derive the objective from the"
            " user_task and worker contract only."
        ),
    }


def _check_worker_contract(agent: Any, method_or_tool: str) -> Optional[JsonDict]:
    contract = getattr(agent, "worker_contract", None)
    if not isinstance(contract, dict) or not contract:
        contract = {}

    forbidden = {
        str(item).strip()
        for item in contract.get("forbidden_methods", [])
        if str(item).strip()
    }
    if any(_method_pattern_matches(pattern, method_or_tool) for pattern in forbidden):
        return {
            "status": "contract_violation",
            "method": method_or_tool,
            "error": f"{method_or_tool} is forbidden by worker_contract",
            "next_instruction": "Choose an allowed method or finalize with a blocker.",
        }

    resolved_contract_task_type = resolve_task_type_fail_closed(
        contract.get("task_type")
    )
    disabled_reason = ""
    if "." in str(method_or_tool):
        disabled_reason = disabled_reason_for_method(
            method_or_tool,
            resolved_contract_task_type,
        )
    if disabled_reason:
        return {
            "status": "contract_violation",
            "method": method_or_tool,
            "error": disabled_reason,
            "task_type": resolved_contract_task_type,
            "classification": {
                "category": "blocked_cross_task_type_required",
                "hint": (
                    "This phase needs a method outside its task_type policy;"
                    " LeadAgent should replan a phase with the appropriate task_type."
                ),
                "method": method_or_tool,
                "task_type": resolved_contract_task_type,
            },
            "next_instruction": (
                "Use a method allowed by the task_type policy, or finalize with"
                " a blocker if this task really requires the disabled domain."
                " In final_answer, report blocked_cross_task_type_required so"
                " LeadAgent can emit a new phase with the appropriate task_type."
            ),
        }

    max_attempts = contract.get("max_surface_attempts")
    if isinstance(max_attempts, dict):
        limit = optional_int(max_attempts.get(method_or_tool))
        if limit is not None and limit >= 0:
            attempts = getattr(agent, "surface_attempts", None)
            if not isinstance(attempts, dict):
                attempts = {}
                agent.surface_attempts = attempts
            current = optional_int(attempts.get(method_or_tool), 0) or 0
            if current >= limit:
                return {
                    "status": "contract_violation",
                    "method": method_or_tool,
                    "error": (
                        f"{method_or_tool} exceeded max_surface_attempts={limit}"
                    ),
                    "next_instruction": (
                        "Switch strategy, record the blocker, or call final_answer."
                    ),
                }
            attempts[method_or_tool] = current + 1

    return None


def _method_pattern_matches(pattern: str, method: str) -> bool:
    if pattern == method:
        return True
    if pattern.endswith(".*"):
        return method.startswith(pattern[:-1])
    return False


def _is_own_artifact_read(agent: Any, tool_name: str, path_hint: Any) -> bool:
    """True only for local_fs_read of a file THIS RUN persisted via
    record_extraction (exact path match against the attempt ledger). Reading
    one's own needs_fix artifact to figure out what to fix is analysis of the
    ledger, not offload spinning — task 9d5655d3 got gated mid-self-diagnosis."""
    if tool_name != "local_fs_read":
        return False
    path = str(path_hint or "")
    if not path or "/artifacts/extractions/" not in path:
        return False
    attempts = {
        str(item)
        for item in (getattr(agent, "extraction_attempt_artifacts", None) or [])
    }
    return path in attempts


def _check_progress_before(
    agent: Any,
    tool_name: str,
    tool_input: Optional[JsonDict] = None,
    step: Optional[int] = None,
    *,
    charge_diagnostic: bool = True,
) -> Optional[JsonDict]:
    progress = getattr(agent, "progress", None)
    if progress is None:
        return None
    limit = optional_int(
        getattr(agent.runtime.harness, "progress_local_fs_without_extraction_limit", 5),
        5,
    ) or 5
    raw_no_artifact_limit = optional_int(
        getattr(agent.runtime.harness, "progress_no_artifact_limit", 8),
        8,
    )
    no_artifact_limit = (
        raw_no_artifact_limit
        if raw_no_artifact_limit is not None
        else 8
    )
    contract = getattr(agent, "worker_contract", {}) or {}
    requires_artifact = bool(
        contract.get("must_record_extraction")
        or contract.get("expected_artifact")
        or contract.get("validators")
    )
    page_id = str((tool_input or {}).get("pageId") or "")
    mandatory_recovery_generation: Optional[int] = None
    lifecycle = getattr(agent, "page_lifecycle", None)
    if isinstance(lifecycle, PageLifecycleTracker) and page_id:
        lifecycle_state = lifecycle.state(page_id)
        if lifecycle_state is not None and (
            (
                tool_name == "Page.getState"
                and lifecycle_state.requires_state_resync
            )
            or (
                tool_name == "DOM.getAXTree"
                and not lifecycle_state.requires_state_resync
                and lifecycle_state.requires_ax_refresh
            )
        ):
            mandatory_recovery_generation = lifecycle_state.generation
    result = progress.before_tool(
        tool_name=tool_name,
        artifact_count=extraction_artifact_count(getattr(agent, "artifacts", [])),
        local_fs_limit=limit,
        no_artifact_limit=no_artifact_limit,
        requires_artifact=requires_artifact,
        own_artifact_read=_is_own_artifact_read(
            agent, tool_name, (tool_input or {}).get("path"),
        ),
        step=step,
        page_id=page_id,
        charge_heavy_diagnostic=charge_diagnostic,
        mandatory_recovery_generation=mandatory_recovery_generation,
    )
    mandatory_allowance = getattr(
        progress, "last_mandatory_recovery_allowance", None
    )
    if isinstance(mandatory_allowance, dict):
        agent.logger.write(
            "progress.mandatory_recovery_credit_used",
            dict(mandatory_allowance),
        )
    allowance = (
        progress.consume_diagnostic_allowance()
        if hasattr(progress, "consume_diagnostic_allowance") else None
    )
    if isinstance(allowance, dict) and tool_name == "DOM.getSemanticTree":
        agent.logger.write("semantic_tree.diagnostic_bypass", allowance)
    if result is not None:
        # Saves that carried schemaWarnings were persisted but deliberately NOT
        # credited to the artifact ledger ("trust the ledger, not the claim").
        # Surface them on the intervention so neither the model nor a human
        # reading the log mistakes "uncredited save" for "never extracted
        # anything" — task 9d5655d3's diagnosis stalled on that ambiguity.
        attempted = [
            str(path)
            for path in (getattr(agent, "extraction_attempt_artifacts", None) or [])
        ]
        credited = {
            str(path) for path in (getattr(agent, "artifacts", None) or [])
        }
        uncredited = [path for path in attempted if path not in credited]
        if uncredited:
            result["uncreditedArtifacts"] = {
                "count": len(uncredited),
                "paths": uncredited[-3:],
                "note": (
                    "saved with schema warnings, so not counted as extraction"
                    " progress; fix the row keys/values and re-record"
                ),
            }
        agent.logger.write("progress.intervention", result)
    return result


def _observe_progress_after(agent: Any, tool_name: str, result: Optional[JsonDict] = None) -> None:
    progress = getattr(agent, "progress", None)
    if progress is None:
        return
    result_path = (
        result.get("path") if isinstance(result, dict) else None
    )
    progress.after_tool(
        tool_name=tool_name,
        artifact_count=extraction_artifact_count(getattr(agent, "artifacts", [])),
        result=result,
        own_artifact_read=_is_own_artifact_read(agent, tool_name, result_path),
    )
    repair_merge = (
        result.get("repairMerge") if isinstance(result, dict) else None
    )
    applied_repairs = (
        repair_merge.get("applied")
        if isinstance(repair_merge, dict) else None
    )
    if applied_repairs and hasattr(progress, "notify_repair_progress"):
        repair_progress = progress.notify_repair_progress(applied_repairs)
        if repair_progress.get("newFieldCount"):
            agent.logger.write("progress.repair_advanced", repair_progress)
    if (
        tool_name == "navigate_verified"
        and isinstance(result, dict)
        and result.get("status") == "done"
        and hasattr(progress, "notify_navigation_success")
    ):
        progress.notify_navigation_success(str(result.get("pageId") or ""))
    agent.logger.write("progress.snapshot", progress.to_log_payload())


def _record_extraction(
    agent: Any,
    tool_input: JsonDict,
) -> JsonDict:
    """Persist a structured extraction artifact."""
    return _record_extraction_persist(agent, tool_input)


def _record_extraction_persist(
    agent: Any,
    tool_input: JsonDict,
) -> JsonDict:
    """Persist a structured extraction artifact for LeadAgent consumption.

    Returns a stub describing the saved file.

    The contract is intentionally simple: name + rows (list of dicts) +
    optional schema. The agent must populate `rows` from observed evidence
    (e.g. extracted hrefs from a Runtime.evaluate result). Downstream
    consumers must read from the saved artifact rather than rely on the
    agent's narrative summary.
    """
    raw_name = str(tool_input.get("name") or "").strip()
    raw_rows = tool_input.get("rows")
    raw_schema = tool_input.get("schema")
    description = str(tool_input.get("description") or "").strip()

    if not raw_name:
        return {"status": "rejected", "error": "name required"}
    rows, error = validate_extraction_rows(raw_rows)
    if error is not None:
        return error
    rows = rows or []

    rows, repair_merge, repair_error = _merge_repair_patch_rows(
        agent,
        artifact_name=raw_name,
        patch_rows=rows,
        repair_resolutions=tool_input.get("repair_resolutions"),
    )
    if repair_error is not None:
        repair_error.setdefault(
            "next_instruction",
            (
                "This worker is in field-repair mode. Submit only manifest target"
                " rows, each with the exact identity field/value shown in the"
                " handoff plus at least one requested repair field; do not resend"
                " trusted rows or fields."
            ),
        )
        return repair_error
    if repair_merge:
        description = description or (
            "Field-level slow-path repair merged into trusted fast-path baseline"
        )

    contract = getattr(agent, "worker_contract", None)
    expected = (
        contract.get("expected_artifact")
        if isinstance(contract, dict)
        and isinstance(contract.get("expected_artifact"), dict)
        else {}
    )
    blocker_failures = detect_blocker_data_rows(rows, expected)
    if blocker_failures:
        result = {
            "status": "rejected",
            "error": (
                "blocker or challenge explanation cannot be stored in a"
                " declared business data field"
            ),
            "failures": blocker_failures,
            "next_instruction": (
                "Keep observed business values in the declared data fields."
                " Report authentication/challenge state through HITL and the"
                " worker blocker/status channel; do not pad rows with failure"
                " notes or structured blocker tokens."
            ),
        }
        agent.logger.write("tool.record_extraction.rejected", {
            "name": raw_name,
            "rowCount": len(rows),
            "reason": "blocker_as_business_data",
            "failures": blocker_failures,
        })
        return result

    schema_warnings = [
        *_record_extraction_schema_warnings(agent, rows),
        *_record_extraction_content_warnings(rows),
    ]
    result = save_extraction_artifact(
        logger=agent.logger,
        runtime=agent.runtime,
        artifacts=None if schema_warnings else agent.artifacts,
        name=raw_name,
        rows=rows,
        schema=raw_schema,
        description=description,
        schema_warnings=schema_warnings,
        event_type="tool.record_extraction",
    )
    attempts = getattr(agent, "extraction_attempt_artifacts", None)
    if isinstance(attempts, list):
        saved_path = str(result.get("savedPath") or "")
        if saved_path and saved_path not in attempts:
            attempts.append(saved_path)
    if repair_merge:
        result["repairMerge"] = repair_merge
        contract = getattr(agent, "worker_contract", None)
        manifest = (
            contract.get("_repair_manifest")
            if isinstance(contract, dict)
            and isinstance(contract.get("_repair_manifest"), dict)
            else None
        )
        if manifest is not None and result.get("savedPath"):
            # Subsequent patch saves build on the latest merged rows, so a
            # worker can repair several targets serially without resending old
            # patches or copying the full baseline through the LLM context.
            manifest["workingArtifact"] = str(result["savedPath"])
    validation = _validate_recorded_extraction(agent, str(result.get("savedPath") or ""))
    if validation:
        contract_validation = trim_large_strings(validation, 3000)
        result["artifactValidation"] = contract_validation
        # Name this boundary explicitly: spawner may later compose a separate
        # content-completeness veto, but phase credit must consume only the
        # artifact/row contract result or it would depend circularly on itself.
        result["contractValidation"] = contract_validation
        tracker = _ensure_content_completeness_tracker(agent)
        if tracker is not None and tracker.enabled:
            if validation.get("status") == "done":
                credit = tracker.observe_contract_validated_artifact(
                    rows=rows,
                    artifact_name=raw_name,
                    saved_path=str(result.get("savedPath") or ""),
                )
                result["contentRegionCredit"] = credit
                agent.logger.write(
                    "content_completeness.artifact_region_credit",
                    credit,
                )
            else:
                tracker.observe_failed_artifact_attempt()
        if validation.get("status") == "failed":
            failures = [
                failure for failure in (validation.get("failures") or [])
                if isinstance(failure, dict)
            ]
            blocking = [
                failure for failure in failures
                if not _is_advisory_record_failure(failure)
            ]
            if blocking:
                result["status"] = "needs_fix"
                result["next_instruction"] = (
                    "record_extraction saved the rows but the current worker_contract"
                    " validators failed. Fix the row keys, artifact name, or values"
                    " shown in artifactValidation before final_answer."
                )
            elif result.get("status") == "done":
                result["validationPending"] = sorted({
                    str(failure.get("type") or "") for failure in failures
                })
                result["next_instruction"] = (
                    "Rows saved. Phase validation is not satisfied yet:"
                    " keep collecting until the expected row count is reached,"
                    " and include sourceTool/sourceSelectorOrAxId/pageUrl plus"
                    " the canonical <field>EvidenceText keys (e.g. rankEvidenceText)"
                    " before final_answer."
                )
    repair_resolutions = (
        repair_merge.get("resolutions")
        if isinstance(repair_merge, dict)
        and isinstance(repair_merge.get("resolutions"), list)
        else []
    )
    if repair_resolutions:
        contract = getattr(agent, "worker_contract", None)
        manifest = (
            contract.get("_repair_manifest")
            if isinstance(contract, dict) else None
        )
        satisfied = (
            manifest.get("visualEvidenceSatisfied")
            if isinstance(manifest, dict) else None
        )
        satisfied_signatures = (
            set(satisfied) if isinstance(satisfied, dict) else set()
        )
        pending_by_signature = {
            str(item.get("signature")): dict(item)
            for item in (
                manifest.get("visualEvidencePending")
                if isinstance(manifest, dict)
                and isinstance(manifest.get("visualEvidencePending"), list)
                else []
            )
            if isinstance(item, dict) and str(item.get("signature") or "")
        }
        visual_checks_enabled = _repair_visual_checks_enabled(agent)
        for item in repair_resolutions:
            identity = item.get("identity") if isinstance(item, dict) else None
            field = item.get("field") if isinstance(item, dict) else None
            signature = _repair_visual_target_signature(identity, field)
            outcome = str(item.get("outcome") or "") if isinstance(item, dict) else ""
            if outcome == "confirmed_absent" and visual_checks_enabled:
                if signature not in satisfied_signatures:
                    pending_by_signature[signature] = {**item, "signature": signature}
                continue
            pending_by_signature.pop(signature, None)
            # Evidence for a prior absence claim must not automatically satisfy
            # a later claim after the field was observed or supplied with a value.
            if isinstance(satisfied, dict):
                satisfied.pop(signature, None)
                satisfied_signatures.discard(signature)
        unresolved_absent = list(pending_by_signature.values())
        if isinstance(manifest, dict):
            if unresolved_absent:
                manifest["visualEvidencePending"] = unresolved_absent
            else:
                manifest.pop("visualEvidencePending", None)
        if unresolved_absent:
            pending = {
                str(item) for item in (result.get("validationPending") or [])
                if str(item).strip()
            }
            pending.add("absence_visual_evidence")
            result["validationPending"] = sorted(pending)
            result["repairEvidencePending"] = unresolved_absent
            visual_instruction = (
                "Repair values marked confirmed_absent are merged, but target-"
                "bound visual evidence is still pending. Keep/reuse the relevant"
                " live page and call visual_verify with repair_targets matching"
                " the listed identity/field targets before final_answer;"
                " Page.screenshot and unrelated visual checks do not count. Cite"
                " this merged savedPath plus the visual evidence in final_answer."
                " Do not re-submit or re-scrape already merged fields."
            )
            prior_instruction = str(result.get("next_instruction") or "").strip()
            result["next_instruction"] = (
                f"{prior_instruction} {visual_instruction}".strip()
            )
    return result


def _merge_repair_patch_rows(
    agent: Any,
    *,
    artifact_name: str,
    patch_rows: List[JsonDict],
    repair_resolutions: Any = None,
) -> Tuple[List[JsonDict], JsonDict, Optional[JsonDict]]:
    """Merge model-supplied patch rows into an internal fast-path baseline.

    The manifest is injected by the spawner, never accepted from the model.
    Only named repair fields and their evidence metadata may change; every
    other baseline field is preserved byte-for-byte.
    """
    contract = getattr(agent, "worker_contract", None)
    manifest = (
        contract.get("_repair_manifest")
        if isinstance(contract, dict)
        and isinstance(contract.get("_repair_manifest"), dict)
        else None
    )
    if manifest is None or str(manifest.get("artifactName") or "") != artifact_name:
        return patch_rows, {}, None
    if manifest.get("disabledReason"):
        # A previous structural failure deliberately abandoned merge mode. The
        # worker may now record one complete replacement artifact normally.
        return patch_rows, {}, None

    def fallback(reason: str, detail: str) -> Tuple[List[JsonDict], JsonDict, JsonDict]:
        """Disable an unusable internal manifest so the next save can recover."""
        manifest["disabledReason"] = reason
        abandoned_visual = manifest.pop("visualEvidencePending", None)
        if isinstance(abandoned_visual, list) and abandoned_visual:
            manifest["visualEvidenceAbandoned"] = [
                dict(item) for item in abandoned_visual if isinstance(item, dict)
            ]
        payload = {
            "artifactName": artifact_name,
            "reason": reason,
            "detail": detail[:500],
            "baselineArtifact": str(manifest.get("baselineArtifact") or ""),
        }
        logger = getattr(agent, "logger", None)
        if logger is not None and hasattr(logger, "write"):
            logger.write("skill.fast_path.repair_fallback", payload)
            if isinstance(abandoned_visual, list) and abandoned_visual:
                logger.write("repair.visual_evidence_abandoned", {
                    "reason": reason,
                    "targets": manifest.get("visualEvidenceAbandoned") or [],
                })
        return patch_rows, {}, {
            "status": "repair_fallback_required",
            "error": detail,
            "tool_was_executed": False,
            "next_instruction": (
                "The trusted repair baseline is unavailable or inconsistent, so"
                " field-patch mode has been disabled. Re-record ONE COMPLETE"
                " artifact under the expected name with every expected row and"
                " field; the normal phase validators will check it."
            ),
        }

    if str(manifest.get("version") or "") != "repair_manifest.v1":
        return fallback(
            "invalid_manifest_version",
            "invalid internal repair manifest version",
        )

    raw_path = str(
        manifest.get("workingArtifact") or manifest.get("baselineArtifact") or ""
    ).strip()
    try:
        path = Path(raw_path).expanduser().resolve()
        root = (agent.logger.task_dir / "artifacts" / "extractions").resolve()
    except Exception:
        return fallback("invalid_baseline_path", "repair baseline path is invalid")
    if not raw_path or (path != root and root not in path.parents):
        return fallback(
            "baseline_outside_task",
            "repair baseline must be an extraction artifact in this task",
        )
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return fallback(
            "baseline_unreadable",
            f"repair baseline could not be read: {str(exc)[:300]}",
        )
    raw_baseline_rows = payload.get("rows") if isinstance(payload, dict) else None
    if not isinstance(raw_baseline_rows, list) or not all(
        isinstance(row, dict) for row in raw_baseline_rows
    ):
        return fallback(
            "baseline_rows_invalid",
            "repair baseline has no valid rows array",
        )
    baseline_rows = [dict(row) for row in raw_baseline_rows]
    expected_count = manifest.get("rowCount")
    if isinstance(expected_count, int) and len(baseline_rows) != expected_count:
        return fallback(
            "baseline_row_count_changed",
            "repair baseline row count changed unexpectedly",
        )

    repairs = manifest.get("repairs")
    if not isinstance(repairs, list) or not repairs:
        return fallback("manifest_targets_missing", "repair manifest has no targets")

    targets: Dict[Tuple[str, str], JsonDict] = {}
    for item in repairs:
        identity = item.get("identity") if isinstance(item, dict) else None
        field = str(identity.get("field") or "") if isinstance(identity, dict) else ""
        value = identity.get("value") if isinstance(identity, dict) else None
        fields = item.get("fields") if isinstance(item, dict) else None
        if not field or not isinstance(fields, list) or not fields:
            return fallback(
                "manifest_target_invalid",
                "repair manifest contains an invalid target",
            )
        key = (field, str(value).strip() if value is not None else "")
        row_indexes = [
            index for index, row in enumerate(baseline_rows)
            if (
                str(row.get(field)).strip()
                if row.get(field) is not None else ""
            ) == key[1]
        ]
        if not key[1] or len(row_indexes) != 1 or key in targets:
            return fallback(
                "baseline_identity_mismatch",
                "repair target identity is not unique in the baseline",
            )
        targets[key] = {
            "rowIndex": row_indexes[0],
            "fields": {str(name) for name in fields if str(name).strip()},
        }

    resolutions: Dict[Tuple[Tuple[str, str], str], JsonDict] = {}
    if repair_resolutions is not None:
        if not isinstance(repair_resolutions, list):
            return patch_rows, {}, {
                "status": "rejected",
                "error": "repair_resolutions must be an array in repair mode",
            }
        for index, raw_resolution in enumerate(repair_resolutions):
            if not isinstance(raw_resolution, dict):
                return patch_rows, {}, {
                    "status": "rejected",
                    "error": f"repair_resolutions[{index}] must be an object",
                }
            identity = raw_resolution.get("identity")
            identity_field = (
                str(identity.get("field") or "").strip()
                if isinstance(identity, dict) else ""
            )
            identity_value = (
                str(identity.get("value")).strip()
                if isinstance(identity, dict) and identity.get("value") is not None
                else ""
            )
            field = str(raw_resolution.get("field") or "").strip()
            outcome = str(raw_resolution.get("outcome") or "").strip()
            identity_key = (identity_field, identity_value)
            target = targets.get(identity_key)
            if (
                target is None
                or not field
                or field not in target["fields"]
            ):
                return patch_rows, {}, {
                    "status": "rejected",
                    "error": (
                        f"repair_resolutions[{index}] does not identify one"
                        " manifest target field"
                    ),
                }
            if outcome not in {
                "value_found", "observed_empty", "confirmed_absent", "unresolved",
            }:
                return patch_rows, {}, {
                    "status": "rejected",
                    "error": f"repair_resolutions[{index}].outcome is invalid",
                }
            resolution_key = (identity_key, field)
            if resolution_key in resolutions:
                return patch_rows, {}, {
                    "status": "rejected",
                    "error": "duplicate repair resolution for one target field",
                }
            resolutions[resolution_key] = {
                "outcome": outcome,
                "evidenceArtifacts": [
                    str(path).strip()
                    for path in (raw_resolution.get("evidenceArtifacts") or [])
                    if str(path).strip()
                ] if isinstance(raw_resolution.get("evidenceArtifacts"), list) else [],
                "note": str(raw_resolution.get("note") or "").strip()[:500],
            }

    applied: List[JsonDict] = []
    ignored_fields: List[JsonDict] = []
    resolution_results: List[JsonDict] = []
    confirmed_absent: List[JsonDict] = []
    seen_targets: set[Tuple[str, str]] = set()
    shared_metadata = {"pageUrl", "sourceTool", "sourceSelectorOrAxId"}
    for patch_index, patch in enumerate(patch_rows):
        matching = [
            (key, target) for key, target in targets.items()
            if (
                str(patch.get(key[0])).strip()
                if patch.get(key[0]) is not None else ""
            ) == key[1]
        ]
        if len(matching) != 1:
            return patch_rows, {}, {
                "status": "rejected",
                "error": (
                    f"repair patch row {patch_index} must contain exactly one"
                    " manifest identity field/value"
                ),
            }
        key, target = matching[0]
        if key in seen_targets:
            return patch_rows, {}, {
                "status": "rejected",
                "error": "duplicate repair patch row for one target",
            }
        seen_targets.add(key)
        repair_fields = target["fields"]
        provided = sorted(field for field in repair_fields if field in patch)
        if not provided:
            return patch_rows, {}, {
                "status": "rejected",
                "error": (
                    f"repair patch row {patch_index} contains none of its"
                    f" requested fields: {sorted(repair_fields)}"
                ),
            }

        for field in provided:
            value_is_empty = _repair_value_is_empty(patch.get(field))
            resolution = resolutions.get((key, field))
            if value_is_empty and resolution is None:
                return patch_rows, {}, {
                    "status": "rejected",
                    "error": (
                        f"empty repair field {field!r} requires a matching"
                        " repair_resolutions entry with outcome observed_empty"
                        " or confirmed_absent"
                    ),
                }
            if resolution is None:
                outcome = "value_found"
                resolution = {"outcome": outcome, "evidenceArtifacts": [], "note": ""}
            else:
                outcome = str(resolution.get("outcome") or "")
            if outcome == "unresolved":
                return patch_rows, {}, {
                    "status": "rejected",
                    "error": (
                        f"repair field {field!r} is unresolved and cannot be"
                        " persisted as a completed patch"
                    ),
                }
            if value_is_empty and outcome == "value_found":
                return patch_rows, {}, {
                    "status": "rejected",
                    "error": f"empty repair field {field!r} cannot be value_found",
                }
            if not value_is_empty and outcome in {"observed_empty", "confirmed_absent"}:
                return patch_rows, {}, {
                    "status": "rejected",
                    "error": (
                        f"non-empty repair field {field!r} conflicts with"
                        f" outcome {outcome}"
                    ),
                }
            if (
                outcome in {"observed_empty", "confirmed_absent"}
                and _repair_field_requires_nonempty(agent, field)
            ):
                return patch_rows, {}, {
                    "status": "repair_contract_conflict",
                    "error": (
                        f"repair field {field!r} is constrained by field_nonempty"
                        f" and cannot resolve as {outcome}"
                    ),
                    "field": field,
                    "validator": "field_nonempty",
                    "outcome": outcome,
                    "tool_was_executed": False,
                    "next_instruction": (
                        "This is a deterministic contract conflict, not an"
                        " extraction retry. Do not submit the same empty patch"
                        " again; report the blocker so LeadAgent can revise the"
                        " contract or accept a partial result."
                    ),
                }
            if (
                outcome in {"observed_empty", "confirmed_absent"}
                and not _repair_resolution_has_source_evidence(
                    agent, patch, field, resolution,
                )
            ):
                return patch_rows, {}, {
                    "status": "rejected",
                    "error": (
                        f"empty repair field {field!r} requires source evidence"
                        f" for outcome {outcome}"
                    ),
                }
            resolution_result = {
                "identity": {"field": key[0], "value": patch.get(key[0])},
                "field": field,
                "outcome": outcome,
            }
            resolution_results.append(resolution_result)
            if outcome == "confirmed_absent":
                confirmed_absent.append(resolution_result)

        destination = baseline_rows[target["rowIndex"]]
        allowed_evidence = {
            evidence_name
            for field in repair_fields
            for evidence_name in (f"{field}EvidenceText", f"{field}Evidence")
        }
        allowed = repair_fields | allowed_evidence | shared_metadata | {key[0]}
        ignored = sorted(str(field) for field in patch.keys() if field not in allowed)
        if ignored:
            ignored_fields.append({"patchRow": patch_index, "fields": ignored})
        for field in allowed:
            if field in patch and field != key[0]:
                destination[field] = patch[field]
        applied.append({
            "patchRow": patch_index,
            "baselineRow": target["rowIndex"],
            "identity": {"field": key[0], "value": patch.get(key[0])},
            "fields": provided,
        })

    if not applied:
        return patch_rows, {}, {
            "status": "rejected",
            "error": "repair patch did not update any manifest field",
        }
    info: JsonDict = {
        "baselineArtifact": str(manifest.get("baselineArtifact") or raw_path),
        "workingArtifact": raw_path,
        "applied": applied,
        "preservedRowCount": len(baseline_rows),
    }
    if ignored_fields:
        info["ignoredFields"] = ignored_fields
    if resolution_results:
        info["resolutions"] = resolution_results
    if confirmed_absent:
        info["confirmedAbsent"] = confirmed_absent
    return baseline_rows, info, None


def _repair_value_is_empty(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        return not value.strip()
    if isinstance(value, (list, tuple, dict, set)):
        return len(value) == 0
    return False


def _repair_field_requires_nonempty(agent: Any, field: str) -> bool:
    contract = getattr(agent, "worker_contract", None)
    if not isinstance(contract, dict):
        return False
    validators = contract.get("validators")
    for validator in validators if isinstance(validators, list) else []:
        if not isinstance(validator, dict):
            continue
        if str(validator.get("type") or "") != "field_nonempty":
            continue
        fields = validator.get("fields")
        if isinstance(fields, list) and field in {str(item) for item in fields}:
            return True
        if str(validator.get("field") or "").strip() == field:
            return True
    expected = contract.get("expected_artifact")
    specs = expected.get("fields") if isinstance(expected, dict) else None
    for spec in specs if isinstance(specs, list) else []:
        if not isinstance(spec, dict):
            continue
        name = str(spec.get("name") or spec.get("field") or spec.get("key") or "")
        if name != field:
            continue
        if spec.get("allow_empty") is True or spec.get("optional_empty") is True:
            return False
        return bool(spec.get("nonempty") or spec.get("required_nonempty"))
    return False


def _repair_resolution_has_source_evidence(
    agent: Any,
    patch: JsonDict,
    field: str,
    resolution: JsonDict,
) -> bool:
    if any(
        str(patch.get(name) or "").strip()
        for name in (f"{field}EvidenceText", f"{field}Evidence")
    ):
        return True
    source_tool = str(patch.get("sourceTool") or "").strip()
    source_locator = str(
        patch.get("sourceSelectorOrAxId") or patch.get("pageUrl") or ""
    ).strip()
    if source_tool and source_locator:
        return True
    ledger = {
        str(path).strip()
        for path in [
            *list(getattr(agent, "artifacts", []) or []),
            *list(getattr(agent, "extraction_attempt_artifacts", []) or []),
        ]
        if str(path).strip()
    }
    return any(
        str(path).strip() in ledger
        for path in (resolution.get("evidenceArtifacts") or [])
    )


def _repair_visual_checks_enabled(agent: Any) -> bool:
    vl_config = getattr(
        getattr(getattr(agent, "runtime", None), "harness", None), "vl", None,
    )
    return bool(
        vl_config is not None
        and getattr(vl_config, "enabled", False)
        and getattr(vl_config, "reality_check_enabled", True)
    )


def _is_advisory_record_failure(failure: JsonDict) -> bool:
    """Failures an in-progress worker resolves by continuing (row-count
    shortfall) or enriching rows on a later save (provenance) — not signals
    that the just-saved rows are wrong."""
    failure_type = str(failure.get("type") or "")
    if failure_type in {"min_rows", "field_provenance"}:
        return True
    if failure_type == "exact_rows":
        expected = failure.get("expected")
        actual = failure.get("actual")
        if isinstance(expected, (int, float)) and isinstance(actual, (int, float)):
            return actual < expected
    return False


def _validate_recorded_extraction(agent: Any, saved_path: str) -> JsonDict:
    contract = getattr(agent, "worker_contract", None)
    if not isinstance(contract, dict) or not saved_path:
        return {}
    phase_id = str(contract.get("phase_id") or "")
    try:
        prior_artifacts = phase_prior_artifact_paths(
            agent.logger,
            phase_id=phase_id,
            exclude_worker_id=getattr(agent, "worker_id", None),
        )
        return validate_worker_artifacts(
            contract=contract,
            artifacts=list(getattr(agent, "artifacts", []) or []),
            attempt_artifacts=[saved_path],
            prior_artifacts=prior_artifacts,
            file_evidence=list(getattr(agent, "file_action_evidence", []) or []),
            task_dir=agent.logger.task_dir,
        )
    except Exception as exc:
        return {
            "status": "skipped",
            "reason": "record_extraction_validation_error",
            "error": str(exc)[:500],
        }


def _record_extraction_schema_warnings(agent: Any, rows: List[JsonDict]) -> List[JsonDict]:
    contract = getattr(agent, "worker_contract", None)
    if not isinstance(contract, dict):
        return []
    expected = contract.get("expected_artifact")
    if not isinstance(expected, dict):
        return []
    fields = expected.get("required_fields")
    if not isinstance(fields, list) or not fields:
        fields = expected.get("fields")
    expected_fields = field_names_from_specs(fields)
    if not expected_fields or not rows:
        return []

    warnings: List[JsonDict] = []
    expected_set = set(expected_fields)
    for index, row in enumerate(rows[:20]):
        keys = set(str(key) for key in row.keys())
        missing = sorted(expected_set - keys)
        if missing:
            warnings.append({
                "type": "expected_fields_missing",
                "row": index,
                "missing": missing,
                "expectedFields": expected_fields,
            })
    return warnings


PLACEHOLDER_VALUE_RE = re.compile(
    r"^\s*(?:<\s*)?(?:placeholder|sample|example|todo|tbd|n/?a)(?:\s*>)?\s*$",
    re.I,
)
PLACEHOLDER_URL_RE = re.compile(r"/(?:placeholder|sample|example)(?:[/?#]|$)", re.I)


def _record_extraction_content_warnings(rows: List[JsonDict]) -> List[JsonDict]:
    warnings: List[JsonDict] = []
    for index, row in enumerate(rows[:20]):
        placeholder_fields: List[JsonDict] = []
        if _row_reports_placeholder(row):
            placeholder_fields.append({
                "field": "placeholderDetected",
                "reason": "row_self_reported_placeholder",
            })
        for field, value in row.items():
            if not isinstance(value, str):
                continue
            text = value.strip()
            if not text or len(text) > 500:
                continue
            if PLACEHOLDER_VALUE_RE.search(text) or PLACEHOLDER_URL_RE.search(text):
                placeholder_fields.append({
                    "field": str(field),
                    "value": text[:120],
                    "reason": "placeholder_like_value",
                })
        if placeholder_fields:
            warnings.append({
                "type": "placeholder_like_extraction_value",
                "row": index,
                "fields": placeholder_fields[:5],
            })
    return warnings


def _row_reports_placeholder(row: JsonDict) -> bool:
    for key in (
        "placeholderDetected",
        "placeholder_detected",
        "isPlaceholder",
        "is_placeholder",
        "dataPlaceholder",
        "data_placeholder",
    ):
        value = row.get(key)
        if value is True:
            return True
        if isinstance(value, str) and value.strip().lower() in {"true", "yes", "1"}:
            return True
    return False


def _hitl_pause_succeeded(response: Any) -> bool:
    if not isinstance(response, dict):
        return False
    if response.get("error"):
        return False
    obs = str(response.get("observation") or "").lower()
    if "paused for human intervention" in obs:
        return True
    data = response.get("data") if isinstance(response.get("data"), dict) else None
    if data is not None and data.get("paused") is True:
        return True
    return False


async def _capture_hitl_pause_snapshot(
    agent: Any,
    runner: Any,
    page_id: str,
    step: int,
) -> None:
    page_id = str(page_id or "").strip()
    if not page_id:
        return
    try:
        response, _recovery = await runner.call(
            "Page.getState",
            {
                "pageId": page_id,
                "purpose": "Capture URL/title before HITL pause for resume verification.",
            },
        )
    except Exception as exc:
        logger = getattr(agent, "logger", None)
        if logger is not None:
            logger.write(
                "hitl.pause_snapshot.failed",
                {
                    "pageId": page_id,
                    "step": step,
                    "errorType": type(exc).__name__,
                    "error": str(exc)[:300],
                },
            )
        return
    data = response.get("data") if isinstance(response, dict) else None
    if not isinstance(data, dict):
        return
    snapshot = {
        "url": str(data.get("url") or data.get("currentUrl") or "").strip(),
        "title": str(data.get("title") or "").strip(),
    }
    if not (snapshot["url"] or snapshot["title"]):
        return
    snapshots = getattr(agent, "hitl_pause_snapshots", None)
    if not isinstance(snapshots, dict):
        snapshots = {}
        agent.hitl_pause_snapshots = snapshots
    snapshots[page_id] = snapshot
    logger = getattr(agent, "logger", None)
    if logger is not None:
        logger.write(
            "hitl.pause_snapshot.captured",
            {"pageId": page_id, "step": step, **snapshot},
        )


def _ensure_hitl_request_reason(method: str, params: JsonDict, reason: str = "") -> None:
    if method != "Hitl.requestPause" or not isinstance(params, dict):
        return
    if str(params.get("reason") or "").strip():
        return
    purpose = str(params.get("purpose") or "").strip()
    fallback = str(reason or "").strip()
    text = purpose or fallback
    if text:
        params["reason"] = text


def _make_hitl_challenge_verifier(agent: Any, page_id: str, step: int):
    """Build the verified-settlement adjudicator for wait_for_hitl_resume.

    Called by the HITL wait loop when lifecycle events suggest the human may
    have finished the challenge. Returns the VL verdict dict; any failure mode
    (VL disabled, screenshot blocked while paused, budget) degrades to
    verdict="unavailable" so the wait falls back to title evidence.
    """
    async def verify(evidence: JsonDict) -> JsonDict:
        agent.challenge_adjudicating = True
        try:
            vl_result = await _visual_verify(
                agent,
                {
                    "pageId": page_id,
                    "selector": "",
                    "id": "",
                    "fullPage": False,
                    "mode": "challenge_detection",
                    "question": (
                        "This page was paused for a human to handle a"
                        " CAPTCHA/Cloudflare-style challenge and has since"
                        " emitted load/title events. Is a challenge still"
                        " visible, or is this the normal target page now?"
                    ),
                    "expected": {
                        "pageId": page_id,
                        "settlementEvidence": evidence,
                    },
                    "_force": True,
                },
                step,
            )
        except Exception as exc:
            return {
                "verdict": "unavailable",
                "errorType": type(exc).__name__,
                "error": str(exc)[:300],
            }
        finally:
            agent.challenge_adjudicating = False
        if not isinstance(vl_result, dict):
            return {"verdict": "unavailable"}
        status = str(vl_result.get("status") or "").strip().lower()
        if status in {"disabled", "rejected", "failed", "error"}:
            return {
                "verdict": "unavailable",
                "status": status,
                "reason": str(
                    vl_result.get("reason") or vl_result.get("error") or ""
                )[:300],
            }
        return vl_result

    return verify


def _hitl_pause_snapshot(agent: Any, page_id: str) -> Optional[JsonDict]:
    snapshots = getattr(agent, "hitl_pause_snapshots", None)
    if not isinstance(snapshots, dict):
        return None
    snapshot = snapshots.get(str(page_id))
    return snapshot if isinstance(snapshot, dict) else None


async def _verify_and_open_fleet_auth_barrier(
    agent: Any,
    page_id: str,
    step: int,
) -> JsonDict:
    barrier = getattr(agent, "fleet_auth_barrier", None)
    fleet_id = str(getattr(agent, "assigned_fleet_id", "") or "").strip()
    worker_id = str(getattr(agent, "worker_id", "") or "").strip()
    if barrier is None or not fleet_id or not worker_id:
        return {"enabled": False}
    state = await _post_hitl_raw_browser_call(
        agent,
        "Page.getState",
        {
            "pageId": page_id,
            "purpose": "Verify shared fleet state before opening the authentication barrier.",
        },
        step,
    )
    tree = await _post_hitl_raw_browser_call(
        agent,
        "DOM.getAXTree",
        {
            "pageId": page_id,
            "purpose": "Refresh page perception before opening the shared authentication barrier.",
        },
        step,
        capture_axtree_text=True,
    )
    if _invoke_result_failed(state) or _invoke_result_failed(tree):
        return {
            "enabled": True,
            "opened": False,
            "reason": "clearance_perception_failed",
        }
    state_data = _response_data(state)
    hitl = state_data.get("hitl") if isinstance(state_data.get("hitl"), dict) else {}
    if hitl.get("isPaused") is True:
        return {
            "enabled": True,
            "opened": False,
            "reason": "page_still_paused",
        }
    resolved = await barrier.resolve(fleet_id, worker_id)
    if resolved.get("resolved"):
        agent.fleet_reperception_pending = True
        agent.fleet_reperception_state_seen = True
        agent.fleet_reperception_tree_seen = True
        agent.fleet_barrier_generation = int(resolved.get("generation") or 0)
        agent.fleet_reperception_pending = False
    callback = getattr(agent, "auth_session_verified_handler", None)
    ledger_receipt: JsonDict = {}
    if resolved.get("resolved") and callable(callback):
        try:
            contract = getattr(agent, "worker_contract", None)
            verification_contract = (
                contract.get("auth_verification")
                if isinstance(contract, dict)
                else None
            )
            value = callback(
                {
                    "fleetId": fleet_id,
                    "pageId": page_id,
                    "url": state_data.get("url"),
                    "title": state_data.get("title"),
                    "sessionKey": getattr(agent, "fleet_session_key", ""),
                    "verificationContract": verification_contract,
                    # The ledger uses this only for an in-memory marker match;
                    # the raw tree is never persisted or included in receipts.
                    "axTreeText": str(
                        tree.get("_authAXTreeText")
                        or "\n".join(_axtree_lines_from_value(tree))
                    ),
                    "evidence": {
                        "pageStateObserved": True,
                        "axTreeObserved": True,
                        "hitlPaused": False,
                    },
                }
            )
            if hasattr(value, "__await__"):
                value = await value
            if isinstance(value, dict):
                ledger_receipt = value
        except Exception as exc:
            # Clearing a live fleet barrier and persisting a durable reuse
            # claim are separate operations.  A ledger conflict/write failure
            # must be visible, but must not crash the resolver or strand peers.
            ledger_receipt = {
                "recorded": False,
                "reason": "auth_ledger_handler_failed",
                "errorType": type(exc).__name__,
                "error": str(exc)[:300],
            }
            logger = getattr(agent, "logger", None)
            if logger is not None:
                logger.write("auth_fleet.ledger_handler_failed", ledger_receipt)
    return {
        "enabled": True,
        "opened": bool(resolved.get("resolved")),
        "generation": resolved.get("generation"),
        "ledger": ledger_receipt or None,
        "reason": resolved.get("reason"),
    }


def _hitl_resumed_suggested_prompt(wait_result: Any) -> str:
    recovery = (
        wait_result.get("postHitlRecovery")
        if isinstance(wait_result, dict) else None
    )
    rounds = recovery.get("rounds") if isinstance(recovery, dict) else None
    structural_cleared = any(
        isinstance(item, dict)
        and isinstance(item.get("structural"), dict)
        and item["structural"].get("status") == "challenge_cleared"
        for item in (rounds if isinstance(rounds, list) else [])
    )
    if structural_cleared:
        return (
            "Page has resumed from HITL and a fresh AXTree no longer shows the"
            " blocking structural challenge. Re-check Page.getState and"
            " DOM.getAXTree, then resume the original business checkpoint. If"
            " target content is still a skeleton, retry its reveal/materialize"
            " action once and verify with DOM.getSemanticTree plus the requested"
            " record count; do not finalize from page title or drawer shell alone."
        )
    return (
        "Page has resumed from HITL. Re-check Page.getState and DOM.getAXTree"
        " before resuming the original business checkpoint; this resume receipt"
        " does not by itself prove that every prior challenge surface or target"
        " skeleton has disappeared. Retry the original reveal/materialize action"
        " when needed and verify the requested content with DOM.getSemanticTree."
    )


async def _enrich_pause_with_wait(
    agent: Any,
    params: JsonDict,
    response: JsonDict,
    step: int,
) -> JsonDict:
    """When Hitl.requestPause succeeds, harness takes over the wait so the
    model doesn't burn steps polling broken APIs. The pause response is
    extended with a `hitl_wait` field describing the outcome.
    """
    page_id = params.get("pageId") if isinstance(params, dict) else None
    if not page_id:
        return response
    diagnostics = getattr(agent, "diagnostics", None)
    harness_cfg = agent.runtime.harness
    wait_result = await wait_for_hitl_resume(
        browser=agent.browser,
        page_id=str(page_id),
        timeout_seconds=getattr(harness_cfg, "hitl_wait_timeout_seconds", 900.0),
        poll_interval_seconds=getattr(harness_cfg, "hitl_poll_interval_seconds", 2.0),
        diagnostics=diagnostics,
        logger=agent.logger,
        challenge_verifier=_make_hitl_challenge_verifier(agent, str(page_id), step),
        pause_snapshot=_hitl_pause_snapshot(agent, str(page_id)),
    )
    if wait_result.get("status") == "resumed":
        wait_result = await _post_hitl_recovery_loop(
            agent,
            str(page_id),
            wait_result,
            step,
        )
    if wait_result.get("status") == "resumed":
        wait_result = dict(wait_result)
        wait_result["fleetAuthBarrier"] = await _verify_and_open_fleet_auth_barrier(
            agent,
            str(page_id),
            step,
        )
    enriched = dict(response)
    enriched["hitl_wait"] = wait_result
    if wait_result.get("status") == "resumed":
        _clear_challenge_state_after_recovery(
            agent, str(page_id), event="challenge.hitl_resume_cleared"
        )
        enriched["suggested_prompt"] = _hitl_resumed_suggested_prompt(wait_result)
    elif wait_result.get("status") in {
        "still_challenge_after_hitl",
        "browser_error_after_hitl",
        "stale_pause_deadlock",
    }:
        if wait_result.get("status") == "stale_pause_deadlock":
            enriched["suggested_prompt"] = (
                "HITL pause is deadlocked: Hitl.resolvePause is blocked by"
                " ERR_PAGE_PAUSED. Do NOT call Hitl.requestPause again for this"
                " page; report status=stale_pause_deadlock and let LeadAgent"
                " continue from a fresh page/fleet."
            )
        else:
            enriched["suggested_prompt"] = (
                "Post-HITL recovery did not confirm a usable page. Do NOT call"
                " more browser tools or Hitl.* methods in this worker; call"
                " final_answer(status=\"incomplete\") and report hitl_wait.status,"
                " postHitlRecovery evidence, screenshotPath, and pageId to LeadAgent."
            )
    elif wait_result.get("status") == "page_settled_after_hitl":
        enriched["suggested_prompt"] = (
            "The page appears to be past the challenge, but ABCP still reports"
            " it as paused for human intervention. Do NOT call browser tools;"
            " report status=page_settled_after_hitl via final_answer and"
            " surface that platform auto-recovery has not released the paused"
            " page yet."
        )
    else:
        enriched["suggested_prompt"] = (
            "HITL wait timed out — page is still paused. Do NOT call other"
            " Hitl.* methods; report status=incomplete via final_answer."
        )
    return enriched


from .composites.dismiss_overlay import (
    DISMISS_OVERLAY_MAX_ATTEMPTS,
    DISMISS_OVERLAY_MAX_DURATION_MS,
    _dismiss_overlay,
    _maybe_retry_original_action,
    _vl_overlay_arbiter,
)
from .composites.collect_items import (
    COLLECT_ITEMS_DEFAULT_FIELDS,
    COLLECT_ITEMS_HARVEST_LIMIT,
    COLLECT_ITEMS_MAX_DURATION_MS,
    COLLECT_ITEMS_MAX_ROUNDS,
    COLLECT_ITEMS_MAX_WINDOWS,
    COLLECT_ITEMS_SETTLE_MS,
    COLLECT_ITEMS_STABILITY_THRESHOLD,
    _collect_dedup_key,
    _collect_interrupt_result,
    _collect_items,
    _collect_items_materialize,
    _collect_overlay_recovery,
    _collect_overlay_stop_reason,
)
from .composites.fill_field_verified import (
    FILL_FIELD_STOPWORDS,
    _axtree_node_name,
    _fill_field_action,
    _fill_field_keywords,
    _fill_field_verified,
)

def build_browser_agent_tool_specs(
    capability_methods: Set[str],
    task_type: Any = "web_scrape",
    *,
    workflow_enabled: bool = False,
) -> List[JsonDict]:
    hidden = hidden_harness_tools_for_task_type(task_type)
    # A live capability does not authorize Harness execution by itself. Both
    # the control-plane master switch and the ABCP capability must be present.
    workflow_visible = bool(
        workflow_enabled and "Workflow.execute" in capability_methods
    )
    return [
        spec
        for spec in BROWSER_TOOLS.tool_specs(capability_methods)
        if spec.get("name") not in hidden
        and (
            workflow_visible
            or spec.get("name") not in {
                "execute_selected_skill",
                "execute_browser_workflow",
            }
        )
    ]
