"""
harness.tools.browser_tools - BrowserAgent tool schemas and dispatch factory.
"""

import asyncio
import base64
import copy
import re
import sys
import time
from functools import lru_cache
from typing import Any, Awaitable, Callable, Dict, List, Optional, Set, Tuple

import json
from pathlib import Path
from urllib.parse import urlparse

from abcp_client import ABCPTransportError
from harness.challenge_detector import (
    ChallengeTracker,
    extract_page_id,
    is_lingering_loading_title,
)
from harness.diagnostics.error_classification import attach_error_classification
from harness.extraction_artifacts import (
    field_names_from_specs,
    save_extraction_artifact,
    validate_extraction_rows,
)
from harness.hitl import wait_for_hitl_resume
from harness.lifecycle import LifecycleContext, lifecycle_for
from harness.local_fs import local_fs_jsonpath, local_fs_read, local_fs_search
from harness.offload import offload_large_tool_result
from harness.progress import extraction_artifact_count
from harness.render_recovery import build_render_recovery_runner
from harness.task_control import phase_prior_artifact_paths, validate_worker_artifacts
from harness.tool_policy import disabled_reason_for_method
from harness.tools.loop_guard import check_tool_call_loop
from harness.tools.parsers import (
    attach_method_schema,
    ensure_required_purpose,
    parse_browser_call_params,
    parse_direct_capability_params,
)
from harness.tools.registry import ToolContext, ToolRegistry
from harness.utils import JsonDict, exception_payload, optional_int, trim_large_strings
from harness.vl import visual_verify_image


EVAL_JS_REASON_KINDS = {
    "computed_geometry",
    "cross_node_relationship",
    "shadow_dom_traversal",
    "cross_frame_aggregation",
    "non_dom_state",
    "legacy_no_dom_equivalent",
}

AXTREE_ID_RE = re.compile(r"^\d+:-?\d+:-?\d+$")
AXTREE_ID_TOKEN_RE = re.compile(r"\[(\d+:-?\d+:-?\d+)\]")
AXTREE_ID_ANYWHERE_RE = re.compile(r"\b\d+:-?\d+:-?\d+\b")
AXTREE_LINE_RE = re.compile(
    r"^(?P<indent>\s*)\[(?P<id>\d+:-?\d+:-?\d+)\]\s+"
    r"(?P<role>[^\s\"]+)(?:\s+\"(?P<name>.*?)\")?(?P<rest>.*)$"
)

AXTREE_INVALIDATING_METHODS = {
    "Page.create",
    "Page.navigate",
    "Page.recovered",
    "Page.close",
    "Page.switchTo",
    "Page.handleDialog",
    "File.handleChooser",
    "Runtime.evaluate",
    "Input.click",
    "Input.type",
    "Input.press",
    "Input.scroll",
    "Input.drag",
    "Hitl.requestPause",
}

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
        return result, True

    progress_gate = _check_extraction_progress_gate(agent, name)
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

    if action.contract_check:
        contract_result = _check_worker_contract(agent, name)
        if contract_result is not None:
            agent.trace.append({"type": "contract_violation", "result": contract_result})
            return contract_result, False

    if action.progress_check:
        progress_result = _check_progress_before(agent, name)
        if progress_result is not None:
            agent.trace.append({"type": "progress_intervention", "result": progress_result})
            return progress_result, False

    result = await action.handler(ctx)
    if action.trace_type:
        _observe_progress_after(agent, name, result)
        agent.trace.append({"type": action.trace_type, "result": result})
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
    name="extract_dom_records",
    description=(
        "Extract a repeated DOM collection into structured rows without hand-written JS."
        " Use this for lists, tables, cards, and link collections instead of parsing"
        " large AXTree text. Internally wraps Runtime.evaluate in an explicit return IIFE."
    ),
    input_schema=_browser_schema_for("extract_dom_records"),
    contract_check=True,
    trace_type="extract_dom_records",
)
async def _browser_extract_dom_records(ctx: ToolContext) -> JsonDict:
    return await _extract_dom_records(ctx.agent, ctx.tool_input, ctx.step)


@BROWSER_TOOLS.register(
    name="eval_js_json",
    description=(
        "Evaluate a JavaScript expression and force a JSON return through a"
        " harness wrapper. Last-resort fallback for structured data only"
        " when DOM.getAXTree + DOM.getText/DOM.getAttribute cannot express"
        " the needed relationship. For statement bodies, pass an IIFE"
        " expression such as (() => { const rows = []; return rows; })()."
    ),
    input_schema=_browser_schema_for("eval_js_json"),
    contract_check=True,
    trace_type="eval_js_json",
)
async def _browser_eval_js_json(ctx: ToolContext) -> JsonDict:
    result = await _eval_js_json_tool(ctx.agent, ctx.tool_input, ctx.step)
    return await _maybe_auto_hitl_for_challenge(
        ctx.agent,
        "eval_js_json",
        {"pageId": ctx.tool_input.get("pageId")},
        result,
        ctx.step,
    )


@BROWSER_TOOLS.register(
    name="navigate_verified",
    description=(
        "Navigate to a URL, poll Page.getState, and verify actual URL/title."
        " Prefer this over raw Page.navigate when URL correctness matters."
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
    if result.get("status") == "done":
        ctx.agent.pending_unrecorded_extraction = None
    return result


@BROWSER_TOOLS.register(
    name="find_in_axtree",
    description=(
        "Search the current DOM.getAXTree snapshot by role/name/text and return"
        " complete canonical AXTree ids with line context. Use this instead of"
        " grepping offloaded AXTree text when locating an element in a large"
        " accessibility tree. It is read-only and requires a fresh current"
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


@BROWSER_TOOLS.register(
    name="local_fs_jsonpath",
    description="Read-only read of a JSON/JSONL file inside the current task worktree, with a JSONPath subset for node extraction.",
    input_schema=_browser_schema_for("local_fs_jsonpath"),
    contract_check=True,
    progress_check=True,
    trace_type="local_fs_jsonpath",
)
async def _browser_local_fs_jsonpath(ctx: ToolContext) -> JsonDict:
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
        progress_result = _check_progress_before(agent, method or "browser_call")
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
        progress_result = _check_progress_before(agent, method or "browser_call")
        if progress_result is not None:
            agent.trace.append({"type": "progress_intervention", "result": progress_result})
            return progress_result, False
        agent.trace.append({"type": "browser_call_rejected", "result": result})
        return result, False

    contract_result = _check_worker_contract(agent, method)
    if contract_result is not None:
        attach_error_classification(contract_result, method=method)
        attach_method_schema(contract_result, method, agent.method_schemas)
        agent.logger.write("browser.call.contract_violation", contract_result)
        _observe_progress_after(agent, method or "browser_call.contract_violation", contract_result)
        progress_result = _check_progress_before(agent, method or "browser_call")
        if progress_result is not None:
            agent.trace.append({"type": "progress_intervention", "result": progress_result})
            return progress_result, False
        agent.trace.append({"type": "contract_violation", "result": contract_result})
        return contract_result, False

    screenshot_guard = _check_screenshot_misuse(method, params, reason)
    if screenshot_guard is not None:
        agent.logger.write("browser.call.screenshot_rejected", screenshot_guard)
        agent.trace.append({"type": "screenshot_guard", "result": screenshot_guard})
        return screenshot_guard, False

    target_param_guard = _check_target_param_requirements(method, params)
    if target_param_guard is not None:
        attach_error_classification(target_param_guard, method=method)
        attach_method_schema(target_param_guard, method, agent.method_schemas)
        agent.logger.write("browser.call.params_error", target_param_guard)
        _observe_progress_after(agent, method or "browser_call.params_error", target_param_guard)
        progress_result = _check_progress_before(agent, method or "browser_call")
        if progress_result is not None:
            agent.trace.append({"type": "progress_intervention", "result": progress_result})
            return progress_result, False
        agent.trace.append({"type": "browser_call_params_error", "result": target_param_guard})
        return target_param_guard, False

    stale_target = _check_stale_axtree_target(agent, method, params)
    if stale_target is not None:
        agent.logger.write("browser.call.stale_axtree_target", stale_target)
        agent.trace.append({"type": "stale_axtree_target", "result": stale_target})
        return stale_target, False

    progress_result = _check_progress_before(agent, method)
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
        response, _recovery = await runner.call(method, params)
        response = agent._capture_artifacts(method, response)
        axtree_snapshot = _precompute_axtree_snapshot(method, params, response)
        response = agent._offload_response(method, params, response, step)

        if method == "Hitl.requestPause" and _hitl_pause_succeeded(response):
            response = await _enrich_pause_with_wait(agent, params, response, step)

        result = {
            "method": method,
            "params": params,
            "response": response,
        }
        if isinstance(response, dict) and response.get("error"):
            attach_method_schema(result, method, agent.method_schemas)
    except ABCPTransportError as exc:
        result = {
            "method": method,
            "params": params,
            "error": str(exc),
        }
        attach_method_schema(result, method, agent.method_schemas)

    attach_error_classification(result, method=method)
    result = _attach_navigation_check(result, method=method, params=params)
    result = _attach_runtime_strategy_hints(result, method=method)
    result = await _maybe_auto_hitl_for_challenge(agent, method, params, result, step)
    result = _attach_normalized_handles(result)
    _observe_axtree_state_after(
        agent,
        method,
        params,
        result,
        precomputed_snapshot=axtree_snapshot if "axtree_snapshot" in locals() else None,
    )
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
    return model_result, False


async def _invoke_browser_method(
    agent: Any,
    method: str,
    params: JsonDict,
    step: int,
    *,
    count_progress: bool = True,
) -> JsonDict:
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
        axtree_snapshot = _precompute_axtree_snapshot(method, params, response)
        response = agent._offload_response(method, params, response, step)
        if method == "Hitl.requestPause" and _hitl_pause_succeeded(response):
            response = await _enrich_pause_with_wait(agent, params, response, step)
        result = {
            "method": method,
            "params": params,
            "response": response,
        }
        if isinstance(response, dict) and response.get("error"):
            attach_method_schema(result, method, agent.method_schemas)
    except ABCPTransportError as exc:
        result = {"method": method, "params": params, "error": str(exc)}
        attach_method_schema(result, method, agent.method_schemas)

    attach_error_classification(result, method=method)
    result = _attach_navigation_check(result, method=method, params=params)
    result = _attach_runtime_strategy_hints(result, method=method)
    result = await _maybe_auto_hitl_for_challenge(agent, method, params, result, step)
    result = _attach_normalized_handles(result)
    _observe_axtree_state_after(
        agent,
        method,
        params,
        result,
        precomputed_snapshot=axtree_snapshot if "axtree_snapshot" in locals() else None,
    )
    agent.diagnostics.observe_browser_call(method, params, result)
    agent.logger.write("browser.call.result", agent._trim_for_log(result))
    model_result = agent._clean_for_model(result)
    if count_progress:
        _observe_progress_after(agent, method, model_result)
    agent.trace.append({
        "type": "browser_call",
        "step": step,
        "method": method,
        "params": params,
        "result": agent._clean_for_model(model_result),
    })
    return model_result


async def _extract_dom_records(agent: Any, tool_input: JsonDict, step: int) -> JsonDict:
    page_id = str(tool_input.get("pageId") or "").strip()
    selector = str(tool_input.get("selector") or "").strip()
    if not page_id:
        return {"status": "failed", "error": "pageId is required"}
    if not selector:
        return {"status": "failed", "error": "selector is required"}

    fields = tool_input.get("fields")
    if not isinstance(fields, dict) or not fields:
        fields = {
            "text": "text",
            "href": "href",
            "imgAlt": "imgAlt",
            "visible": "visible",
            "ancestorText": "ancestorText",
        }
    visible_only = bool(tool_input.get("visibleOnly", True))
    include_rect = bool(tool_input.get("includeRect", True))
    include_ancestor_text = bool(tool_input.get("includeAncestorText", True))
    limit = max(1, min(optional_int(tool_input.get("limit"), 200) or 200, 1000))
    record_name = str(tool_input.get("record_name") or "").strip()

    expression = _build_extract_dom_records_expression(
        selector=selector,
        fields=fields,
        visible_only=visible_only,
        include_rect=include_rect,
        include_ancestor_text=include_ancestor_text,
        limit=limit,
    )
    purpose = f"Extract structured DOM records for selector {selector!r}"
    eval_result = await _invoke_browser_method(
        agent,
        "Runtime.evaluate",
        {
            "pageId": page_id,
            "expression": expression,
            "returnByValue": True,
            "purpose": purpose,
        },
        step,
    )

    payload = _runtime_json_payload(eval_result)
    if payload is None:
        payload = await _eval_json_via_title(agent, page_id, expression, step, purpose)
    if not isinstance(payload, dict):
        return {
            "status": "failed",
            "error": "Runtime.evaluate did not return a JSON object",
            "runtimeResult": agent._trim_for_model(eval_result),
        }
    if payload.get("error"):
        return {
            "status": "failed",
            "error": str(payload.get("error")),
            "stack": str(payload.get("stack") or "")[:1000],
        }

    rows = payload.get("rows") if isinstance(payload.get("rows"), list) else []
    rows = [row for row in rows if isinstance(row, dict)]
    result: JsonDict = {
        "status": "done",
        "selector": selector,
        "rowCount": len(rows),
        "rows": rows,
        "truncated": bool(payload.get("truncated")),
        "next_step": (
            "If these rows are target data, call record_extraction now."
            if not record_name
            else "Rows were automatically persisted via record_extraction."
        ),
    }
    if record_name:
        record_result = _record_extraction(
            agent,
            {
                "name": record_name,
                "rows": rows,
                "schema": {
                    "source": "extract_dom_records",
                    "selector": selector,
                    "fields": fields,
                },
                "description": f"Rows extracted from DOM selector {selector!r}",
            },
        )
        result["recordExtraction"] = record_result
        if record_result.get("status") == "done":
            agent.pending_unrecorded_extraction = None
    elif rows:
        agent.pending_unrecorded_extraction = {
            "source": "extract_dom_records",
            "step": step,
            "rowCount": len(rows),
            "turns": 0,
        }
    return result


async def _eval_js_json_tool(agent: Any, tool_input: JsonDict, step: int) -> JsonDict:
    page_id = str(tool_input.get("pageId") or "").strip()
    expression = str(tool_input.get("expression") or "").strip()
    record_name = str(tool_input.get("record_name") or "").strip()
    description = str(tool_input.get("description") or "").strip()
    why_dom_primitives_insufficient = str(
        tool_input.get("why_dom_primitives_insufficient") or ""
    ).strip()
    reason_kind = str(tool_input.get("reason_kind") or "").strip()
    cross_check_plan = str(tool_input.get("cross_check_plan") or "").strip()
    if not page_id:
        return {"status": "failed", "error": "pageId is required"}
    if not expression:
        return {"status": "failed", "error": "expression is required"}

    policy_warnings = _eval_js_policy_warnings(
        record_name=record_name,
        reason_kind=reason_kind,
        why_dom_primitives_insufficient=why_dom_primitives_insufficient,
        cross_check_plan=cross_check_plan,
    )
    if record_name and policy_warnings:
        return {
            "status": "rejected",
            "policy_violation": "eval_js_json_requires_justification_for_target_data",
            "policyWarnings": policy_warnings,
            "next_instruction": (
                "Use native DOM/Page/Input tools, or provide a valid reason_kind,"
                " a concrete why_dom_primitives_insufficient, and a cross_check_plan."
            ),
        }

    wrapped_expression = _build_eval_js_json_expression(expression)
    purpose = "Evaluate JavaScript expression and return JSON via harness wrapper"
    eval_result = await _invoke_browser_method(
        agent,
        "Runtime.evaluate",
        {
            "pageId": page_id,
            "expression": wrapped_expression,
            "returnByValue": True,
            "purpose": purpose,
        },
        step,
    )

    payload = _runtime_any_json_payload(eval_result)
    if payload is None:
        payload = await _eval_json_via_title(
            agent,
            page_id,
            wrapped_expression,
            step,
            purpose,
        )
    if not isinstance(payload, dict):
        return {
            "status": "failed",
            "error": "Runtime.evaluate did not return a JSON object",
            "runtimeResult": agent._trim_for_model(eval_result),
        }
    if payload.get("error"):
        return {
            "status": "failed",
            "error": str(payload.get("error")),
            "stack": str(payload.get("stack") or "")[:1000],
        }

    value = payload.get("value")
    rows = _rows_from_eval_value(value)
    result: JsonDict = {
        "status": "done",
        "value": value,
        "valueType": type(value).__name__,
        "next_step": (
            "If this value is target data, call record_extraction now."
            if not record_name
            else "Rows were automatically persisted via record_extraction."
        ),
    }
    if policy_warnings:
        result["policyWarnings"] = policy_warnings
    if record_name:
        if rows is None:
            return {
                **result,
                "status": "failed",
                "error": (
                    "record_name was provided, but evaluated value was not a"
                    " list of objects or an object with rows=[...]"
                ),
            }
        record_result = _record_extraction(
            agent,
            {
                "name": record_name,
                "rows": rows,
                "schema": {"source": "eval_js_json"},
                "description": description or "Rows extracted by eval_js_json",
            },
        )
        result["recordExtraction"] = record_result
        if record_result.get("status") == "done":
            agent.pending_unrecorded_extraction = None
    elif rows:
        agent.pending_unrecorded_extraction = {
            "source": "eval_js_json",
            "step": step,
            "rowCount": len(rows),
            "turns": 0,
        }
    return result


def _eval_js_policy_warnings(
    *,
    record_name: str,
    reason_kind: str,
    why_dom_primitives_insufficient: str,
    cross_check_plan: str,
) -> List[JsonDict]:
    warnings: List[JsonDict] = []
    if reason_kind not in EVAL_JS_REASON_KINDS:
        warnings.append({
            "type": "eval_js_json_invalid_reason_kind",
            "reason_kind": reason_kind,
            "allowed": sorted(EVAL_JS_REASON_KINDS),
        })
    if len(why_dom_primitives_insufficient.strip()) < 30:
        warnings.append({
            "type": "eval_js_json_without_sufficient_dom_reason",
            "message": (
                "State why DOM.getAXTree + DOM.getText/DOM.getAttribute cannot"
                " solve this extraction."
            ),
        })
    if len(cross_check_plan.strip()) < 20:
        warnings.append({
            "type": "eval_js_json_without_cross_check_plan",
            "message": (
                "State how at least one target field will be cross-checked with"
                " DOM.getText or DOM.getAttribute before record_extraction."
            ),
        })
    if warnings and not record_name:
        for warning in warnings:
            warning["severity"] = "warning"
    return warnings


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
        matches.append({
            "id": node_id,
            "role": node.get("role") or "",
            "name": name,
            "interactive": bool(node.get("interactive")),
            "lineNumber": line_number or None,
            "line": raw_line,
            "context": context,
        })
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


async def _navigate_verified(agent: Any, tool_input: JsonDict, step: int) -> JsonDict:
    page_id = str(tool_input.get("pageId") or "").strip()
    url = str(tool_input.get("url") or "").strip()
    expected_url_pattern = str(tool_input.get("expectedUrlPattern") or "").strip()
    expected_title_pattern = str(tool_input.get("expectedTitlePattern") or "").strip()
    timeout_seconds = max(1.0, min(float(tool_input.get("timeoutSeconds") or 20.0), 120.0))
    poll_interval = max(0.25, min(float(tool_input.get("pollIntervalSeconds") or 1.0), 5.0))
    max_retries = max(1, min(optional_int(tool_input.get("maxRetries"), 1) or 1, 3))
    if not page_id:
        return {"status": "failed", "error": "pageId is required"}
    if not url:
        return {"status": "failed", "error": "url is required"}

    pattern = expected_url_pattern or f"^{re.escape(url)}$"
    title_re = re.compile(expected_title_pattern) if expected_title_pattern else None
    url_re = re.compile(pattern)
    attempts: List[JsonDict] = []
    internal_poll_count = 0
    last_challenge_summary: JsonDict = {}
    challenge_poll_limit = _navigate_challenge_poll_limit(
        timeout_seconds,
        poll_interval,
        max_retries,
    )

    for attempt in range(1, max_retries + 1):
        nav = await _invoke_browser_method(
            agent,
            "Page.navigate",
            {
                "pageId": page_id,
                "url": url,
                "purpose": f"Navigate and verify URL for attempt {attempt}",
            },
            step,
            count_progress=False,
        )
        if _result_has_auto_hitl(nav):
            return _navigate_hitl_result(page_id, attempt, nav)
        last_challenge_summary = _page_challenge_summary(agent, page_id)
        deadline = asyncio.get_running_loop().time() + timeout_seconds
        last_state: JsonDict = {}
        while True:
            state_result = await _invoke_browser_method(
                agent,
                "Page.getState",
                {
                    "pageId": page_id,
                    "purpose": "Verify navigation reached the expected page",
                },
                step,
                count_progress=False,
            )
            internal_poll_count += 1
            if _result_has_auto_hitl(state_result):
                return _navigate_hitl_result(page_id, attempt, state_result)
            data = _response_data(state_result)
            current_url = str(data.get("url") or "")
            title = str(data.get("title") or "")
            status = str(data.get("status") or "")
            title_is_lingering = is_lingering_loading_title(title)
            url_ok = bool(url_re.search(current_url))
            title_ok = True if title_re is None else bool(title_re.search(title))
            last_state = {
                "url": current_url,
                "title": title,
                "status": status,
                "urlOk": url_ok,
                "titleOk": title_ok,
                "titleLingering": title_is_lingering,
            }
            last_challenge_summary = _page_challenge_summary(agent, page_id)
            if url_ok and title_ok and not title_is_lingering:
                _clear_navigation_challenge_state(agent, page_id)
                return {
                    "status": "done",
                    "pageId": page_id,
                    "url": current_url,
                    "title": title,
                    "pageStatus": status,
                    "attempt": attempt,
                    "navigateResult": _strip_challenge_fields(nav),
                    "state": last_state,
                    "internalPollCount": internal_poll_count,
                }
            if _should_block_navigation_for_challenge(
                agent,
                page_id,
                challenge_poll_limit=challenge_poll_limit,
                internal_poll_count=internal_poll_count,
                title_is_lingering=title_is_lingering,
            ):
                return _navigate_challenge_blocked_result(
                    page_id=page_id,
                    attempt=attempt,
                    last_state=last_state,
                    attempts=attempts,
                    internal_poll_count=internal_poll_count,
                    challenge_summary=last_challenge_summary,
                    expected_url_pattern=pattern,
                    expected_title_pattern=expected_title_pattern,
                    trigger="bounded_lingering_challenge",
                )
            if asyncio.get_running_loop().time() >= deadline:
                break
            await asyncio.sleep(poll_interval)
        attempts.append({"attempt": attempt, "lastState": last_state})

    if _challenge_score(last_challenge_summary) >= 80:
        return _navigate_challenge_blocked_result(
            page_id=page_id,
            attempt=max_retries,
            last_state=attempts[-1].get("lastState", {}) if attempts else {},
            attempts=attempts,
            internal_poll_count=internal_poll_count,
            challenge_summary=last_challenge_summary,
            expected_url_pattern=pattern,
            expected_title_pattern=expected_title_pattern,
            trigger="navigation_verification_exhausted_with_challenge",
        )

    return {
        "status": "failed",
        "error": "navigation verification failed",
        "expectedUrlPattern": pattern,
        "expectedTitlePattern": expected_title_pattern or None,
        "attempts": attempts,
        "internalPollCount": internal_poll_count,
        "suspectedChallenge": last_challenge_summary or None,
        "next_instruction": (
            "Do not assume navigation succeeded. Reuse the reported actual URL/title,"
            " retry with a corrected expected pattern, or open a fresh page."
        ),
    }


def _navigate_challenge_poll_limit(
    timeout_seconds: float,
    poll_interval: float,
    max_retries: int,
) -> int:
    budget_polls = int((timeout_seconds * max_retries) / max(poll_interval, 0.25))
    return max(8, min(30, budget_polls))


def _page_challenge_summary(agent: Any, page_id: str) -> JsonDict:
    tracker = getattr(agent, "challenge_tracker", None)
    state = tracker.get_state(page_id) if tracker is not None and page_id else None
    return state.to_summary() if state is not None else {}


def _challenge_score(summary: JsonDict) -> int:
    try:
        return int(summary.get("suspicionScore") or 0)
    except (TypeError, ValueError):
        return 0


def _vl_unavailable(agent: Any) -> bool:
    vl_config = getattr(getattr(agent.runtime, "harness", None), "vl", None)
    return not bool(vl_config is not None and getattr(vl_config, "enabled", False))


def _should_block_navigation_for_challenge(
    agent: Any,
    page_id: str,
    *,
    challenge_poll_limit: int,
    internal_poll_count: int,
    title_is_lingering: bool,
) -> bool:
    if not title_is_lingering or not _vl_unavailable(agent):
        return False
    tracker = getattr(agent, "challenge_tracker", None)
    state = tracker.get_state(page_id) if tracker is not None and page_id else None
    if state is None:
        return False
    return (
        state.suspicion_score >= 80
        and internal_poll_count >= challenge_poll_limit
        and state.lingering_title_count >= challenge_poll_limit
    )


def _clear_navigation_challenge_state(agent: Any, page_id: str) -> None:
    tracker = getattr(agent, "challenge_tracker", None)
    if tracker is not None and page_id:
        tracker.clear_page(page_id)
    logger = getattr(agent, "logger", None)
    if logger is not None:
        logger.write("challenge.navigation_cleared", {"pageId": page_id})


def _notify_navigation_success(agent: Any, page_id: str) -> None:
    progress = getattr(agent, "progress", None)
    if progress is None or not hasattr(progress, "notify_navigation_success"):
        return
    result = progress.notify_navigation_success(page_id)
    logger = getattr(agent, "logger", None)
    if logger is not None:
        logger.write("progress.navigation_success", result)


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


def _navigate_challenge_blocked_result(
    *,
    page_id: str,
    attempt: int,
    last_state: JsonDict,
    attempts: List[JsonDict],
    internal_poll_count: int,
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
        "internalPollCount": internal_poll_count,
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
    force_check = bool(tool_input.get("_force", False))
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

    page_id = str(tool_input.get("pageId") or "").strip()
    if not page_id:
        return {"status": "failed", "error": "pageId is required"}
    selector = str(tool_input.get("selector") or "").strip()
    element_id = str(tool_input.get("id") or "").strip()
    mode = str(tool_input.get("mode") or "action_outcome").strip()
    question = str(tool_input.get("question") or "").strip()
    expected = tool_input.get("expected")
    if not isinstance(expected, dict):
        expected = {}
    full_page = bool(tool_input.get("fullPage", False))

    screenshot_params: JsonDict = {
        "pageId": page_id,
        "fullPage": full_page,
        "options": {"format": "base64"},
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
    vl_check_count = getattr(agent, "vl_check_count", 0)
    vl_force_check_count = getattr(agent, "vl_force_check_count", 0)
    result = {
        **verdict,
        "mode": mode,
        "screenshotPath": image_path,
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
    agent.logger.write(
        "vl.visual_verify",
        {
            key: value
            for key, value in result.items()
            if key not in {"visible_evidence"}
        },
    )
    return result


def _screenshot_saved_path(result: JsonDict) -> Optional[str]:
    data = _response_data(result)
    for key in ("savedPath", "path", "filePath"):
        value = data.get(key)
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
    return None


def _build_extract_dom_records_expression(
    *,
    selector: str,
    fields: JsonDict,
    visible_only: bool,
    include_rect: bool,
    include_ancestor_text: bool,
    limit: int,
) -> str:
    selector_json = json.dumps(selector)
    fields_json = json.dumps(fields, ensure_ascii=False)
    visible_json = "true" if visible_only else "false"
    rect_json = "true" if include_rect else "false"
    ancestor_json = "true" if include_ancestor_text else "false"
    return f"""
(() => {{
  try {{
    const selector = {selector_json};
    const fieldSpecs = {fields_json};
    const limit = {int(limit)};
    const visibleOnly = {visible_json};
    const includeRect = {rect_json};
    const includeAncestorText = {ancestor_json};
    const norm = (value, max = 1000) => String(value ?? "")
      .replace(/\\s+/g, " ")
      .trim()
      .slice(0, max);
    const rectOf = (el) => {{
      const r = el.getBoundingClientRect();
      return {{
        x: Math.round(r.x), y: Math.round(r.y),
        top: Math.round(r.top), left: Math.round(r.left),
        width: Math.round(r.width), height: Math.round(r.height),
        pageX: Math.round(r.left + window.scrollX),
        pageY: Math.round(r.top + window.scrollY)
      }};
    }};
    const isVisible = (el) => {{
      const r = el.getBoundingClientRect();
      const s = window.getComputedStyle(el);
      return !!(r.width && r.height)
        && s.visibility !== "hidden"
        && s.display !== "none"
        && Number(s.opacity || "1") > 0;
    }};
    const ancestorText = (el) => {{
      let node = el.parentElement;
      for (let depth = 0; node && depth < 4; depth++, node = node.parentElement) {{
        if (node === document.body || node === document.documentElement) break;
        const text = norm(node.innerText || node.textContent || "", 1500);
        if (text && text !== norm(el.innerText || el.textContent || "", 1500)) {{
          return text;
        }}
      }}
      return "";
    }};
    const read = (el, spec) => {{
      spec = String(spec || "text");
      if (spec === "text" || spec === "textContent") return norm(el.innerText || el.textContent || "");
      if (spec === "href") return el.href || (el.closest && el.closest("a[href]") ? el.closest("a[href]").href : "");
      if (spec === "src") return el.src || "";
      if (spec === "imgAlt") {{
        const img = el.matches && el.matches("img") ? el : el.querySelector && el.querySelector("img");
        return img ? norm(img.getAttribute("alt") || img.alt || "") : "";
      }}
      if (spec === "visible") return isVisible(el);
      if (spec === "rect" || spec === "boundingRect") return rectOf(el);
      if (spec === "ancestorText") return ancestorText(el);
      if (spec === "tag") return el.tagName ? el.tagName.toLowerCase() : "";
      if (spec === "id") return el.id || "";
      if (spec === "class") return el.className || "";
      if (spec === "ariaLabel") return el.getAttribute("aria-label") || "";
      if (spec === "role") return el.getAttribute("role") || "";
      if (spec.startsWith("attr:")) return el.getAttribute(spec.slice(5)) || "";
      return norm(el.innerText || el.textContent || "");
    }};
    const rows = [];
    const nodes = Array.from(document.querySelectorAll(selector));
    for (let domOrder = 0; domOrder < nodes.length && rows.length < limit; domOrder++) {{
      const el = nodes[domOrder];
      const visible = isVisible(el);
      if (visibleOnly && !visible) continue;
      const row = {{ domOrder, visible }};
      for (const [name, spec] of Object.entries(fieldSpecs || {{}})) {{
        row[name] = read(el, spec);
      }}
      if (includeRect) row.boundingRect = rectOf(el);
      if (includeAncestorText && row.ancestorText === undefined) row.ancestorText = ancestorText(el);
      rows.push(row);
    }}
    return JSON.stringify({{
      rows,
      rowCount: rows.length,
      matchedCount: nodes.length,
      truncated: nodes.length > rows.length
    }});
  }} catch (err) {{
    return JSON.stringify({{
      error: String(err && err.message || err),
      stack: String(err && err.stack || "")
    }});
  }}
}})()
"""


def _runtime_json_payload(result: JsonDict) -> Optional[Any]:
    values: List[Any] = []
    response = result.get("response") if isinstance(result, dict) else None
    data = response.get("data") if isinstance(response, dict) else None
    if isinstance(data, dict):
        if "rows" in data or "error" in data:
            values.append(data)
        values.extend([
            data.get("result"),
            data.get("value"),
            data.get("returnValue"),
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
        if isinstance(value, dict) and ("rows" in value or "error" in value):
            return value
        if isinstance(value, list):
            return value
    return None


def _runtime_any_json_payload(result: JsonDict) -> Optional[Any]:
    values: List[Any] = []
    response = result.get("response") if isinstance(result, dict) else None
    data = response.get("data") if isinstance(response, dict) else None
    if isinstance(data, dict):
        values.extend([
            data,
            data.get("result"),
            data.get("value"),
            data.get("returnValue"),
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


def _build_eval_js_json_expression(expression: str) -> str:
    expression_json = json.dumps(expression)
    return f"""
(async () => {{
  try {{
    const __abcpExpression = {expression_json};
    const __abcpValue = (0, eval)("(" + __abcpExpression + ")");
    const __abcpResolved = (
      __abcpValue && typeof __abcpValue.then === "function"
    ) ? await __abcpValue : __abcpValue;
    return JSON.stringify({{ value: __abcpResolved }});
  }} catch (err) {{
    return JSON.stringify({{
      error: String(err && err.message || err),
      stack: String(err && err.stack || "")
    }});
  }}
}})()
"""


def _rows_from_eval_value(value: Any) -> Optional[List[JsonDict]]:
    candidate = None
    if isinstance(value, list):
        candidate = value
    elif isinstance(value, dict) and isinstance(value.get("rows"), list):
        candidate = value.get("rows")
    if not isinstance(candidate, list):
        return None
    rows = [item for item in candidate if isinstance(item, dict)]
    return rows if len(rows) == len(candidate) else None


async def _eval_json_via_title(
    agent: Any,
    page_id: str,
    json_string_expression: str,
    step: int,
    purpose: str,
    *,
    chunk_chars: int = 700,
    max_chunks: int = 300,
    ready_timeout_seconds: float = 30.0,
    poll_interval_seconds: float = 0.25,
) -> Optional[Any]:
    prefix = "__ABCP_JSON__"
    setup = f"""
(async () => {{
  try {{
    document.title = "{prefix}|PENDING|0";
    const __abcpJsonText = await ({json_string_expression});
    const text = String(__abcpJsonText ?? "null");
    const bytes = new TextEncoder().encode(text);
    let binary = "";
    const step = 0x8000;
    for (let i = 0; i < bytes.length; i += step) {{
      binary += String.fromCharCode.apply(null, bytes.subarray(i, i + step));
    }}
    window.__abcpJsonB64 = btoa(binary);
    window.__abcpJsonOffset = 0;
    document.title = "{prefix}|READY|" + String(window.__abcpJsonB64.length);
    return document.title;
  }} catch (err) {{
    document.title = "{prefix}|ERROR|" + String(err && err.message || err).slice(0, 500);
    return document.title;
  }}
}})()
"""
    await _invoke_browser_method(
        agent,
        "Runtime.evaluate",
        {
            "pageId": page_id,
            "expression": setup,
            "returnByValue": True,
            "purpose": f"{purpose}; initialize JSON title side-channel",
        },
        step,
    )
    title = ""
    deadline = asyncio.get_running_loop().time() + max(1.0, ready_timeout_seconds)
    error_title = ""
    while asyncio.get_running_loop().time() < deadline:
        ready = await _invoke_browser_method(
            agent,
            "Page.getState",
            {"pageId": page_id, "purpose": "Read JSON side-channel ready marker"},
            step,
        )
        title = str(_response_data(ready).get("title") or "")
        if title.startswith(f"{prefix}|READY|"):
            break
        if title.startswith(f"{prefix}|ERROR|"):
            error_title = title
            break
        await asyncio.sleep(max(0.05, poll_interval_seconds))
    if error_title:
        return {
            "error": error_title.split("|", 2)[-1] if "|ERROR|" in error_title else "unknown title side-channel error"
        }
    if not title.startswith(f"{prefix}|READY|"):
        return {
            "error": (
                "Timed out waiting for eval_js_json title side-channel READY marker"
                f" after {ready_timeout_seconds:.1f}s"
            )
        }
    try:
        total_len = int(title.rsplit("|", 1)[-1])
    except ValueError:
        return {
            "error": f"Invalid eval_js_json READY marker length: {title[:200]}"
        }

    chunks: List[str] = []
    while sum(len(chunk) for chunk in chunks) < total_len:
        if len(chunks) >= max_chunks:
            return {
                "error": (
                    "eval_js_json title side-channel exceeded max_chunks="
                    f"{max_chunks} before reading {total_len} base64 chars"
                )
            }
        chunk_expr = f"""
(() => {{
  const text = String(window.__abcpJsonB64 || "bnVsbA==");
  const start = Number(window.__abcpJsonOffset || 0);
  const chunk = text.slice(start, start + {int(chunk_chars)});
  window.__abcpJsonOffset = start + chunk.length;
  document.title = "{prefix}|CHUNK|" + String(start) + "|" + chunk;
  return document.title;
}})()
"""
        await _invoke_browser_method(
            agent,
            "Runtime.evaluate",
            {
                "pageId": page_id,
                "expression": chunk_expr,
                "returnByValue": True,
                "purpose": "Emit JSON title side-channel chunk",
            },
            step,
        )
        state = await _invoke_browser_method(
            agent,
            "Page.getState",
            {"pageId": page_id, "purpose": "Read JSON title side-channel chunk"},
            step,
        )
        title = str(_response_data(state).get("title") or "")
        parts = title.split("|", 3)
        if len(parts) != 4 or parts[0] != prefix or parts[1] != "CHUNK":
            return {
                "error": f"Invalid eval_js_json CHUNK marker: {title[:200]}"
            }
        chunks.append(parts[3])

    try:
        text = base64.b64decode("".join(chunks).encode("ascii")).decode("utf-8")
        return json.loads(text)
    except (ValueError, UnicodeDecodeError) as exc:
        return {
            "error": f"Failed to decode eval_js_json title side-channel payload: {exc}"
        }


def _response_data(result: JsonDict) -> JsonDict:
    response = result.get("response") if isinstance(result, dict) else None
    if not isinstance(response, dict):
        return {}
    data = response.get("data")
    return data if isinstance(data, dict) else {}


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
    enriched["runtimeStrategy"] = {
        "id": "browser_action.overlay.dismiss_ladder",
        "trigger": "occlusion_blocked",
        "method": method,
        "safeSequence": [
            "refresh DOM.getAXTree and inspect page_stats.overlay",
            "use a visible close/dismiss control if present",
            "try Escape for a dismissible business overlay",
            "try a verified backdrop click outside the modal content",
            "refresh DOM.getAXTree before retrying the blocked action once",
        ],
        "safetyBoundary": (
            "Do not click login, payment, provider sign-in, or other sensitive"
            " submit buttons automatically."
        ),
    }
    existing = str(enriched.get("next_instruction") or "").strip()
    overlay_instruction = (
        "Occlusion blocked this action. Apply browser_action.overlay.dismiss_ladder:"
        " refresh DOM.getAXTree, dismiss only non-sensitive overlay surfaces"
        " (visible close/dismiss, Escape, or verified backdrop), then refresh DOM"
        " and retry the original action once with a fresh id/selector."
    )
    enriched["next_instruction"] = (
        f"{existing} {overlay_instruction}".strip()
        if existing
        else overlay_instruction
    )
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


def _check_target_param_requirements(method: str, params: JsonDict) -> Optional[JsonDict]:
    if not isinstance(params, dict):
        return None
    has_selector_or_id = _non_empty_param(params, "selector") or _non_empty_param(params, "id")
    if method in {"DOM.getText", "DOM.getAttribute", "Input.type"} and not has_selector_or_id:
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
    return None


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
            " DOM.getText, DOM.getAttribute, extract_dom_records, or"
            " visual_verify for bounded visual arbitration."
        ),
    }


def _precompute_axtree_snapshot(
    method: str,
    params: JsonDict,
    response: Any,
) -> Optional[JsonDict]:
    if method != "DOM.getAXTree":
        return None
    page_id = ""
    if isinstance(params, dict):
        page_id = str(params.get("pageId") or "")
    lines = _axtree_lines_from_value(response)
    nodes = _axtree_nodes_from_lines(lines)
    ids = _axtree_ids_from_value(response)
    if nodes:
        ids.update(str(node.get("id") or "") for node in nodes if node.get("id"))
    return {
        "ids": ids,
        "lines": lines,
        "nodes": nodes,
        "pageId": page_id,
    }


def _axtree_lines_from_value(value: Any, *, limit: int = 10000) -> List[str]:
    lines: List[str] = []

    def append_line(line: str) -> None:
        if len(lines) >= limit:
            return
        text = line.rstrip("\n")
        if AXTREE_LINE_RE.match(text):
            lines.append(text)

    def visit(item: Any) -> None:
        if len(lines) >= limit:
            return
        if isinstance(item, dict):
            for nested in item.values():
                visit(nested)
        elif isinstance(item, list):
            for nested in item:
                visit(nested)
                if len(lines) >= limit:
                    return
        elif isinstance(item, str):
            if "\n" in item:
                for line in item.splitlines():
                    append_line(line)
                    if len(lines) >= limit:
                        return
            else:
                append_line(item)

    visit(value)
    return lines


def _axtree_nodes_from_lines(lines: List[str]) -> List[JsonDict]:
    nodes: List[JsonDict] = []
    for index, line in enumerate(lines, start=1):
        match = AXTREE_LINE_RE.match(line)
        if not match:
            continue
        rest = str(match.group("rest") or "")
        name = str(match.group("name") or "")
        nodes.append({
            "id": match.group("id"),
            "role": match.group("role"),
            "name": name,
            "interactive": "#" in rest,
            "line": line,
            "lineNumber": index,
            "depth": len(str(match.group("indent") or "")) // 2,
        })
    return nodes


def _check_stale_axtree_target(
    agent: Any,
    method: str,
    params: JsonDict,
) -> Optional[JsonDict]:
    if method == "DOM.getAXTree" or not isinstance(params, dict):
        return None
    target_ids = _axtree_ids_from_params(params)
    if not target_ids:
        return None

    current_ids = set(getattr(agent, "axtree_ids", set()) or set())
    epoch = int(getattr(agent, "axtree_epoch", 0) or 0)
    invalidated = bool(getattr(agent, "axtree_invalidated", True))
    current_page_id = str(getattr(agent, "axtree_page_id", "") or "")
    page_id = str(params.get("pageId") or "")
    missing = sorted(target_ids - current_ids)
    if not current_ids or invalidated:
        reason = "axtree_snapshot_invalidated" if invalidated else "no_current_axtree_snapshot"
    elif page_id and current_page_id and page_id != current_page_id:
        reason = "axtree_page_mismatch"
    elif missing:
        reason = "axtree_id_not_in_current_snapshot"
    else:
        return None

    return {
        "status": "stale_element_reference",
        "reason": reason,
        "method": method,
        "pageId": page_id or None,
        "currentAXTreePageId": current_page_id or None,
        "axTreeEpoch": epoch,
        "targetIds": sorted(target_ids),
        "missingIds": missing,
        "tool_was_executed": False,
        "next_instruction": (
            "DOM changed or the target id is not from the current AXTree epoch."
            " Call Page.getState if lifecycle is uncertain, then DOM.getAXTree"
            " for this page and derive a fresh id/selector before retrying."
        ),
    }


def _axtree_ids_from_params(params: JsonDict) -> Set[str]:
    ids: Set[str] = set()
    for key in ("id", "nodeId", "targetId", "selector"):
        value = params.get(key)
        if isinstance(value, str) and AXTREE_ID_RE.match(value.strip()):
            ids.add(value.strip())
    return ids


def _observe_axtree_state_after(
    agent: Any,
    method: str,
    params: JsonDict,
    result: JsonDict,
    *,
    precomputed_snapshot: Optional[JsonDict] = None,
) -> None:
    if method == "DOM.getAXTree":
        snapshot = precomputed_snapshot if isinstance(precomputed_snapshot, dict) else {}
        raw_ids = snapshot.get("ids")
        ids = {str(item) for item in raw_ids if str(item).strip()} if isinstance(raw_ids, set) else set()
        if not ids:
            ids = _axtree_ids_from_value(result)
        page_id = ""
        if isinstance(params, dict):
            page_id = str(params.get("pageId") or "")
        if not page_id:
            page_id = str(snapshot.get("pageId") or "")
        if not page_id:
            page_id = str(_response_data(result).get("pageId") or "")
        if ids:
            agent.axtree_epoch = int(getattr(agent, "axtree_epoch", 0) or 0) + 1
            agent.axtree_ids = ids
            agent.axtree_page_id = page_id
            agent.axtree_invalidated = False
            agent.axtree_lines = list(snapshot.get("lines") or _axtree_lines_from_value(result))
            agent.axtree_nodes = list(snapshot.get("nodes") or _axtree_nodes_from_lines(agent.axtree_lines))
            logger = getattr(agent, "logger", None)
            if logger is not None:
                logger.write(
                    "axtree.snapshot",
                    {
                        "pageId": page_id or None,
                        "epoch": agent.axtree_epoch,
                        "idCount": len(ids),
                    },
                )
        return

    if method in AXTREE_INVALIDATING_METHODS:
        _invalidate_axtree_snapshot(agent, method, params)


def _invalidate_axtree_snapshot(agent: Any, method: str, params: JsonDict) -> None:
    agent.axtree_invalidated = True
    agent.axtree_lines = []
    agent.axtree_nodes = []
    logger = getattr(agent, "logger", None)
    if logger is not None:
        logger.write(
            "axtree.invalidated",
            {
                "method": method,
                "pageId": str(params.get("pageId") or "") if isinstance(params, dict) else "",
                "epoch": int(getattr(agent, "axtree_epoch", 0) or 0),
            },
        )


def _axtree_ids_from_value(value: Any, *, limit: int = 5000) -> Set[str]:
    ids: Set[str] = set()

    def visit(item: Any) -> None:
        if len(ids) >= limit:
            return
        if isinstance(item, dict):
            for key, nested in item.items():
                key_text = str(key)
                if key_text in {"id", "nodeId", "axNodeId", "domNodeId"}:
                    if isinstance(nested, str) and AXTREE_ID_RE.match(nested.strip()):
                        ids.add(nested.strip())
                visit(nested)
        elif isinstance(item, list):
            for nested in item:
                visit(nested)
        elif isinstance(item, str):
            for match in AXTREE_ID_TOKEN_RE.findall(item):
                ids.add(match)
                if len(ids) >= limit:
                    return
            if len(item) < 2000:
                for match in AXTREE_ID_ANYWHERE_RE.findall(item):
                    ids.add(match)
                    if len(ids) >= limit:
                        return

    visit(value)
    return ids


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
    if vl_config is None or not getattr(vl_config, "enabled", False):
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
        timeout_seconds=getattr(harness_cfg, "hitl_wait_timeout_seconds", 1200.0),
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
) -> JsonDict:
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
        response = agent._offload_response(method, params, response, step)
        result = {"method": method, "params": params, "response": response}
    except ABCPTransportError as exc:
        result = {
            "method": method,
            "params": params,
            "status": "browser_error_after_hitl",
            "error": str(exc),
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
    result = _attach_normalized_handles(result)
    logger = getattr(agent, "logger", None)
    if logger is not None and hasattr(logger, "write"):
        trim_for_log = getattr(agent, "_trim_for_log", lambda value: value)
        logger.write("hitl.post_resume.raw_call", trim_for_log(result))
    return result


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
            enriched["autoHitl"] = await _request_hitl_for_challenge(
                agent,
                page_id,
                trigger_method,
                step,
                reason=str(vl_result.get("reason") or "VL confirmed challenge"),
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


async def _request_hitl_for_challenge(
    agent: Any,
    page_id: str,
    trigger_method: str,
    step: int,
    *,
    reason: str,
    trigger_result: Optional[JsonDict] = None,
) -> JsonDict:
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
    agent.logger.write(
        "hitl.auto_request_pause",
        {
            "pageId": page_id,
            "triggerMethod": trigger_method,
            "reason": reason,
            "pauseSnapshot": snapshot,
        },
    )
    agent.challenge_adjudicating = True
    try:
        return await _invoke_browser_method(
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

def _check_extraction_progress_gate(agent: Any, next_tool: str) -> Optional[JsonDict]:
    if next_tool == "record_extraction":
        return None
    pending = getattr(agent, "pending_unrecorded_extraction", None)
    if not isinstance(pending, dict):
        return None
    turns = optional_int(pending.get("turns"), 0) or 0
    if turns < 1:
        pending["turns"] = turns + 1
        agent.pending_unrecorded_extraction = pending
        return None
    return {
        "status": "progress_gate",
        "reason": "unrecorded_structured_rows",
        "rowCount": pending.get("rowCount"),
        "source": pending.get("source"),
        "tool_was_executed": False,
        "next_instruction": (
            "You already extracted structured rows but did not persist them."
            " Call record_extraction now if the rows are relevant, rerun"
            " extract_dom_records with record_name set, or call final_answer"
            " with a blocker if they are not trustworthy."
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

    disabled_reason = ""
    if "." in str(method_or_tool):
        disabled_reason = disabled_reason_for_method(
            method_or_tool,
            contract.get("task_type"),
        )
    if disabled_reason:
        return {
            "status": "contract_violation",
            "method": method_or_tool,
            "error": disabled_reason,
            "task_type": contract.get("task_type") or "general",
            "classification": {
                "category": "blocked_cross_task_type_required",
                "hint": (
                    "This phase needs a method outside its task_type policy;"
                    " LeadAgent should replan a phase with the appropriate task_type."
                ),
                "method": method_or_tool,
                "task_type": contract.get("task_type") or "general",
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


def _check_progress_before(agent: Any, tool_name: str) -> Optional[JsonDict]:
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
    result = progress.before_tool(
        tool_name=tool_name,
        artifact_count=extraction_artifact_count(getattr(agent, "artifacts", [])),
        local_fs_limit=limit,
        no_artifact_limit=no_artifact_limit,
        requires_artifact=requires_artifact,
    )
    if result is not None:
        agent.logger.write("progress.intervention", result)
    return result


def _observe_progress_after(agent: Any, tool_name: str, result: Optional[JsonDict] = None) -> None:
    progress = getattr(agent, "progress", None)
    if progress is None:
        return
    progress.after_tool(
        tool_name=tool_name,
        artifact_count=extraction_artifact_count(getattr(agent, "artifacts", [])),
        result=result,
    )
    if (
        tool_name == "navigate_verified"
        and isinstance(result, dict)
        and result.get("status") == "done"
        and hasattr(progress, "notify_navigation_success")
    ):
        progress.notify_navigation_success(str(result.get("pageId") or ""))
    agent.logger.write("progress.snapshot", progress.to_log_payload())


def _record_extraction(agent: Any, tool_input: JsonDict) -> JsonDict:
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
    validation = _validate_recorded_extraction(agent, str(result.get("savedPath") or ""))
    if validation:
        result["artifactValidation"] = trim_large_strings(validation, 3000)
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
    return result


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
        timeout_seconds=getattr(harness_cfg, "hitl_wait_timeout_seconds", 1200.0),
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
    enriched = dict(response)
    enriched["hitl_wait"] = wait_result
    if wait_result.get("status") == "resumed":
        cooldown_seconds = float(
            getattr(harness_cfg, "hitl_no_repause_cooldown_seconds", 8.0) or 0.0
        )
        agent.hitl_no_repause_until = time.monotonic() + max(0.0, cooldown_seconds)
        guard_seconds = float(
            getattr(harness_cfg, "hitl_post_resume_guard_seconds", 30.0) or 0.0
        )
        _record_post_hitl_repause_guard(
            agent,
            str(page_id),
            max(cooldown_seconds, guard_seconds),
        )
        tracker = getattr(agent, "challenge_tracker", None)
        if tracker is not None:
            tracker.clear_page(str(page_id))
            logger = getattr(agent, "logger", None)
            if logger is not None and hasattr(logger, "write"):
                logger.write("challenge.hitl_resume_cleared", {"pageId": str(page_id)})
        enriched["suggested_prompt"] = (
            "Page has resumed from HITL. Re-check page state before issuing"
            " new actions; the user may have navigated."
        )
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


def _browser_input_schemas(capability_methods: Tuple[str, ...]) -> Dict[str, JsonDict]:
    method_schema: JsonDict = {
        "type": "string",
        "description": "ABCP capability method, e.g. Fleet.create, Page.navigate, DOM.getAXTree.",
    }
    if capability_methods:
        method_schema["enum"] = list(capability_methods)

    return {
        "browser_call": {
            "type": "object",
            "properties": {
                "method": method_schema,
                "params": {
                    "type": "object",
                    "description": (
                        "JSON object of params for the ABCP method; pass {} when there are none."
                        " Do not invent handles, copy placeholder ids, reuse stale AXTree ids,"
                        " or encode assumed page order as factual params."
                    ),
                    "additionalProperties": True,
                },
                "reason": {
                    "type": "string",
                    "description": "Short reason for this call (used in logs and as fallback for the `purpose` field).",
                },
            },
            "required": ["method", "params", "reason"],
            "additionalProperties": False,
        },
        "extract_dom_records": {
            "type": "object",
            "properties": {
                "pageId": {"type": "string"},
                "selector": {
                    "type": "string",
                    "description": "CSS selector for repeated elements, e.g. a[href], article, .card.",
                },
                "fields": {
                    "type": "object",
                    "additionalProperties": {"type": "string"},
                    "description": (
                        "Map output field names to built-in extractors: text, href,"
                        " imgAlt, visible, rect, boundingRect, ancestorText, tag, id,"
                        " class, ariaLabel, role, attr:<name>, src."
                    ),
                },
                "visibleOnly": {"type": "boolean"},
                "includeRect": {"type": "boolean"},
                "includeAncestorText": {"type": "boolean"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 1000},
                "record_name": {
                    "type": "string",
                    "description": (
                        "If non-empty, automatically persist the rows via record_extraction"
                        " under this dataset name. Pass \"\" to inspect rows first."
                    ),
                },
            },
            "required": [
                "pageId",
                "selector",
                "fields",
                "visibleOnly",
                "includeRect",
                "includeAncestorText",
                "limit",
                "record_name",
            ],
            "additionalProperties": False,
        },
        "eval_js_json": {
            "type": "object",
            "properties": {
                "pageId": {"type": "string"},
                "expression": {
                    "type": "string",
                    "description": (
                        "JavaScript expression whose value is JSON-serializable."
                        " The harness wraps it and returns JSON.stringify({value})."
                    ),
                },
                "record_name": {
                    "type": "string",
                    "description": (
                        "If the value is rows or {rows:[...]}, persist it via"
                        " record_extraction under this name. Pass \"\" to inspect."
                    ),
                },
                "description": {
                    "type": "string",
                    "description": "Optional artifact description when record_name is set.",
                },
                "why_dom_primitives_insufficient": {
                    "type": "string",
                    "description": (
                        "Explain why DOM.getAXTree plus DOM.getText/"
                        "DOM.getAttribute cannot satisfy this extraction."
                        " Required because eval_js_json is a last-resort fallback."
                    ),
                },
                "reason_kind": {
                    "type": "string",
                    "enum": sorted(EVAL_JS_REASON_KINDS),
                    "description": (
                        "Why native DOM primitives are insufficient."
                        " Required for eval_js_json."
                    ),
                },
                "cross_check_plan": {
                    "type": "string",
                    "description": (
                        "How at least one target field will be checked with"
                        " DOM.getText or DOM.getAttribute before handoff."
                    ),
                },
            },
            "required": [
                "pageId",
                "expression",
                "record_name",
                "description",
                "why_dom_primitives_insufficient",
                "reason_kind",
                "cross_check_plan",
            ],
            "additionalProperties": False,
        },
        "navigate_verified": {
            "type": "object",
            "properties": {
                "pageId": {"type": "string"},
                "url": {"type": "string"},
                "expectedUrlPattern": {
                    "type": "string",
                    "description": "Regex that must match the final URL; pass \"\" to require exact target URL.",
                },
                "expectedTitlePattern": {
                    "type": "string",
                    "description": "Optional regex for final title; pass \"\" to skip title check.",
                },
                "timeoutSeconds": {"type": "number"},
                "pollIntervalSeconds": {"type": "number"},
                "maxRetries": {"type": "integer", "minimum": 1, "maximum": 3},
            },
            "required": [
                "pageId",
                "url",
                "expectedUrlPattern",
                "expectedTitlePattern",
                "timeoutSeconds",
                "pollIntervalSeconds",
                "maxRetries",
            ],
            "additionalProperties": False,
        },
        "visual_verify": {
            "type": "object",
            "properties": {
                "pageId": {"type": "string"},
                "selector": {
                    "type": "string",
                    "description": "Optional CSS selector to crop; pass \"\" for viewport/fullPage.",
                },
                "id": {
                    "type": "string",
                    "description": "Optional canonical AXTree id to crop; pass \"\" if not used.",
                },
                "fullPage": {
                    "type": "boolean",
                    "description": "Whether to capture full page. Prefer false/cropped screenshots.",
                },
                "mode": {
                    "type": "string",
                    "description": "action_outcome | validator_failure | overlay_check | captcha_check | layout_check",
                },
                "question": {
                    "type": "string",
                    "description": "Short visual question for the verifier.",
                },
                "expected": {
                    "type": "object",
                    "additionalProperties": True,
                    "description": "Expected visible state, e.g. {\"target\":\"JobBuddy\",\"state\":\"product detail page\"}.",
                },
            },
            "required": [
                "pageId",
                "selector",
                "id",
                "fullPage",
                "mode",
                "question",
                "expected",
            ],
            "additionalProperties": False,
        },
        "final_answer": {
            "type": "object",
            "properties": {
                "status": {
                    "type": "string",
                    "enum": [
                        "done",
                        "incomplete",
                        "partial",
                        "extraction_inconclusive",
                    ],
                    "description": (
                        "done = task complete; partial = some trustworthy results but not all targets reached;"
                        " extraction_inconclusive = extraction kept failing and no trustworthy result is available;"
                        " incomplete = any other inability to proceed."
                    ),
                },
                "answer": {
                    "type": "string",
                    "description": (
                        "JSON string containing outcome, data, evidence,"
                        " blockers, and next_steps. Large row sets must stay"
                        " in record_extraction artifacts referenced by savedPath."
                    ),
                },
                "reason": {
                    "type": "string",
                    "description": "Optional; brief justification (≤ 200 chars) for non-done statuses.",
                },
            },
            "required": ["status", "answer"],
            "additionalProperties": False,
        },
        "record_extraction": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Short dataset name, e.g. \"trending-week-products\".",
                },
                "rows": {
                    "type": "array",
                    "items": {"type": "object", "additionalProperties": True},
                    "description": (
                        "Structured rows; every row must be a JSON object. Use exact expected_artifact"
                        " field names. For sensitive fields include pageUrl, sourceTool,"
                        " sourceSelectorOrAxId, and the canonical <field>EvidenceText"
                        " key, e.g. rankEvidenceText. Legacy evidence/<field>Evidence"
                        " aliases may validate but should not be preferred."
                    ),
                },
                "schema": {
                    "type": "object",
                    "description": "Optional; documents the source/meaning of fields in `rows`. Not enforced.",
                    "additionalProperties": True,
                },
                "description": {
                    "type": "string",
                    "description": "Optional; which page / selector this data was extracted from.",
                },
            },
            "required": ["name", "rows"],
            "additionalProperties": False,
        },
        "find_in_axtree": {
            "type": "object",
            "properties": {
                "pageId": {
                    "type": "string",
                    "description": "Page id whose current DOM.getAXTree snapshot should be searched.",
                },
                "role": {
                    "type": "string",
                    "description": "Optional AX role filter, e.g. link, button, textbox. Pass \"\" for any role.",
                },
                "name": {
                    "type": "string",
                    "description": "Accessible name/text to locate. Pass \"\" to list by role only.",
                },
                "text": {
                    "type": "string",
                    "description": "Alias/fallback for name; pass \"\" unless name is empty.",
                },
                "match": {
                    "type": "string",
                    "enum": ["exact", "contains", "regex"],
                    "description": "How to match name/text.",
                },
                "case_sensitive": {"type": "boolean"},
                "interactive_only": {
                    "type": "boolean",
                    "description": "When true, only return AXTree lines marked interactable/focusable with #.",
                },
                "max_results": {"type": "integer", "minimum": 1, "maximum": 50},
            },
            "required": [
                "pageId",
                "role",
                "name",
                "text",
                "match",
                "case_sensitive",
                "interactive_only",
                "max_results",
            ],
            "additionalProperties": False,
        },
        "local_fs_search": {
            "type": "object",
            "properties": {
                "pattern": {
                    "type": "string",
                    "description": "Regex grep; pass an empty string to list matches by glob / event_type only.",
                },
                "glob": {
                    "type": "string",
                    "description": "Glob relative to the current task worktree, e.g. observations/*.json or **/*.json.",
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
        },
        "local_fs_read": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "line_offset": {"type": "integer", "minimum": 0},
                "line_limit": {"type": "integer", "minimum": 1, "maximum": 5000},
                "max_bytes": {"type": "integer", "minimum": 1000, "maximum": 200000},
            },
            "required": ["path", "line_offset", "line_limit", "max_bytes"],
            "additionalProperties": False,
        },
        "local_fs_jsonpath": {
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
        },
    }


def build_browser_agent_tool_specs(capability_methods: Set[str]) -> List[JsonDict]:
    return BROWSER_TOOLS.tool_specs(capability_methods)
