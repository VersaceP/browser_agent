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
from harness.observation.overlay_detector import detect_overlay_from_result
from harness.observation.semantic_index import discover_selector_candidates
from harness.observation.verifiers import (
    build_read_only_oracle,
    collect_rows,
    probe_occluder,
    probe_viewport_metrics,
    SemanticLocator,
    verify_field_value,
    verify_overlay_gone,
)
from harness.offload import offload_large_tool_result
from harness.progress import extraction_artifact_count
from harness.render_recovery import build_render_recovery_runner
from harness.task_control import phase_prior_artifact_paths, validate_worker_artifacts
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
from .schemas import EVAL_JS_REASON_KINDS, _browser_input_schemas
from .axtree_state import (
    AXTREE_INVALIDATING_METHODS,
    _apply_recovered_target,
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
        " large AXTree text. PREFER this over eval_js_json whenever the extraction is"
        " expressible as one selector + field specs — it adds matchedCount diagnostics"
        " and record_name persistence for free."
        " Internally wraps Runtime.evaluate in an explicit return IIFE."
        " Field specs: text, href, src, imgAlt, ariaLabel, role, rect, ancestorText,"
        " or attr:<name>. The src spec auto-resolves lazy images (falls back to"
        " data-src/data-original/srcset and absolutizes the URL) so blank 1x1"
        " placeholders are skipped; for images not yet in the DOM, scroll or use"
        " collect_items first to mount them."
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
        " the needed relationship. If the data is a flat repeated collection"
        " reachable by ONE CSS selector, use extract_dom_records instead;"
        " reserve this for cross-node/cross-section logic (heading-scoped"
        " aggregation, computed relations). For statement bodies, pass an IIFE"
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
    name="dismiss_overlay",
    description=(
        "Dismiss an overlay/modal/cookie-banner blocking a target action. Runs"
        " the dismiss ladder internally (find close control -> click -> verify"
        " -> Escape -> verify -> verified backdrop click -> verify) and reports"
        " a structured result. Auth/login and paywall overlays are never"
        " auto-dismissed (returns status=blocked). Optionally retries the"
        " original action after the overlay is gone, but never a consequential"
        " one (submit/pay/login -> status=dismissed_pending_action)."
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
        "Collect a repeated list/card/row collection that grows by scrolling or"
        " a load-more button, without burning a model step per round. Harvests"
        " rows every round and dedups by a stable key, so lazy-loaded AND"
        " virtualized lists (rows recycled out of the DOM) are fully captured."
        " Stops on target count, stagnation, or the round budget, then persists"
        " the rows via record_extraction. Use a freshly created tab (a reused"
        " tab can cap some sites' lazy-loader). Not for filter/search/sort,"
        " which change the data set."
    ),
    input_schema=_browser_schema_for("collect_items"),
    contract_check=True,
    trace_type="collect_items",
)
async def _browser_collect_items(ctx: ToolContext) -> JsonDict:
    return await _collect_items(ctx.agent, ctx.tool_input, ctx.step)


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

    target_param_guard = _check_target_param_requirements(
        method, params, getattr(agent, "method_schemas", {})
    )
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

    page_create_should_stop = False
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

    if _is_page_create_32005_failure(method, result):
        result, page_create_should_stop = await _recover_page_create_32005(
            agent,
            params,
            result,
        )
    attach_error_classification(result, method=method)
    result = _attach_navigation_check(result, method=method, params=params)
    result = _attach_runtime_strategy_hints(result, method=method)
    if not page_create_should_stop:
        result = await _maybe_auto_hitl_for_challenge(agent, method, params, result, step)
    result = _attach_normalized_handles(result)
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
) -> JsonDict:
    # internal=True marks a harness plumbing call (e.g. the title side-channel's
    # PENDING/READY/CHUNK markers): it must not enter the observation chain —
    # no challenge adjudication, diagnostics, progress, or model-facing trace —
    # only a compact audit log. Such calls also never count as progress.
    if internal:
        count_progress = False
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
        # Only forward redact_params when set, so runners that predate the kwarg
        # (test fakes) keep working for the common non-redacted path.
        runner_kwargs = {"redact_params": redact_params} if redact_params else {}
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
        response, _recovery = await runner.call(method, params, **runner_kwargs)
        response = agent._capture_artifacts(method, response)
        axtree_snapshot = _precompute_axtree_snapshot(method, params, response)
        response = agent._offload_response(method, params, response, step)
        if method == "Hitl.requestPause" and _hitl_pause_succeeded(response):
            response = await _enrich_pause_with_wait(agent, params, response, step)
        result = {
            "method": method,
            "params": _shown_params(params),
            "response": response,
        }
        if isinstance(response, dict) and response.get("error"):
            attach_method_schema(result, method, agent.method_schemas)
    except ABCPTransportError as exc:
        result = {"method": method, "params": _shown_params(params), "error": str(exc)}
        attach_method_schema(result, method, agent.method_schemas)

    if _is_page_create_32005_failure(method, result):
        result, _page_create_should_stop = await _recover_page_create_32005(
            agent,
            params,
            result,
        )
        if "params" in result:
            result["params"] = _shown_params(params)
    attach_error_classification(result, method=method)
    result = _attach_navigation_check(result, method=method, params=params)
    result = _attach_runtime_strategy_hints(result, method=method)
    if not internal:
        result = await _maybe_auto_hitl_for_challenge(agent, method, params, result, step)
    result = _attach_normalized_handles(result)
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
    matched_count = int(payload.get("matchedCount") or 0)
    result: JsonDict = {
        "status": "done",
        "selector": selector,
        # matchedCount = selector hits BEFORE visibleOnly/limit filtering; rowCount
        # = rows returned. Exposing both lets the model tell "selector wrong /
        # content absent" (matchedCount 0) apart from "matched but filtered out"
        # (matchedCount > 0, rowCount 0 -> usually not-yet-visible lazy content).
        "matchedCount": matched_count,
        "rowCount": len(rows),
        "rows": rows,
        # filteredCount = scanned nodes dropped by visibleOnly; truncated STRICTLY
        # means the scan stopped early because `limit` was reached (filtered-out
        # nodes never set it, so lazyHint and truncated can't contradict).
        "filteredCount": int(payload.get("filteredCount") or 0),
        "truncated": bool(payload.get("truncated")),
        "next_step": (
            "If these rows are target data, call record_extraction now."
            if not record_name
            else "Rows were automatically persisted via record_extraction."
        ),
    }
    if matched_count > 0 and not rows:
        result["lazyHint"] = (
            f"Selector matched {matched_count} node(s) but 0 rows passed the filter"
            " (likely visibleOnly hiding not-yet-visible lazy content). Scroll the"
            " section into view or use collect_items, then re-extract before"
            " concluding the content is absent."
        )
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
    # Advisory routing only (never blocks): a flat selector+map extraction is
    # exactly what extract_dom_records expresses declaratively.
    if _looks_like_flat_collection_js(expression):
        result["routingHint"] = (
            "This expression is a flat querySelectorAll+map extraction;"
            " prefer extract_dom_records (selector + field specs) next time —"
            " it adds matchedCount diagnostics, lazy-src resolution, and"
            " record_name persistence."
        )
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


_FLAT_COLLECTION_MAP_RE = re.compile(r"\.map\s*\(|forEach\s*\(")
# Cross-node markers extract_dom_records cannot express — their presence means
# the free-form JS is justified. Note: r"querySelector\s*\(" does NOT match
# "querySelectorAll(" ("querySelector" is followed by "All", not "(").
_CROSS_NODE_JS_RE = re.compile(
    r"\bclosest\s*\(|\.parentElement\b|\.parentNode\b"
    r"|\.next(?:Element)?Sibling\b|\.previous(?:Element)?Sibling\b"
    r"|\.children\b|\.childNodes\b"
    r"|\.(?:first|last)(?:Element)?Child\b"
    r"|querySelector\s*\("
)


def _looks_like_flat_collection_js(expression: str) -> bool:
    """True when the JS is a single querySelectorAll + map/forEach projection —
    the shape extract_dom_records covers declaratively. Advisory only."""
    expr = str(expression or "")
    if expr.count("querySelectorAll") != 1:
        return False
    if not _FLAT_COLLECTION_MAP_RE.search(expr):
        return False
    return not _CROSS_NODE_JS_RE.search(expr)


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


def _auto_hitl_is_actionable(auto: Any) -> bool:
    """True only when an autoHitl entry represents a REAL pause request — i.e.
    `Hitl.requestPause` actually ran. A skipped / not-executed adjudication is a
    no-op: the page was never paused, so a composite loop must NOT abort on it.

    Post-97f105e the harness only writes result['autoHitl'] when it truly requests
    HITL (skipped/cooldown/stale verdicts go to `suspected_challenge.adjudication`
    instead), so in practice every autoHitl is actionable. This guard keeps
    `_loop_interrupt_from_result` honest against a future short-circuit that could
    attach a `tool_was_executed: False` / `status: "skipped*"` autoHitl."""
    if not isinstance(auto, dict):
        return False
    if auto.get("tool_was_executed") is False:
        return False
    if str(auto.get("status") or "").lower().startswith("skipped"):
        return False
    return True


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
    """True when an _invoke_browser_method result represents a failed action.

    Browser-side action errors surface in response.error / response.data.error
    (top-level `error` is only set on transport exceptions), so a check that
    only reads result["error"] would report a failed retry as succeeded."""
    if not isinstance(result, dict):
        return False
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
        dpr_resp = await _invoke_browser_method(
            agent, "Runtime.evaluate",
            {"pageId": page_id, "returnByValue": True,
             "expression": "return {dpr: window.devicePixelRatio || 1};",
             "purpose": "read devicePixelRatio to map screenshot px to CSS px"},
            step,
        )
        dpr = float((_response_data(dpr_resp) or {}).get("dpr") or 1.0) or 1.0
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
      if (spec === "src") {{
        // Site-agnostic lazy-image resolver: many sites (1688/taobao/amazon...)
        // keep the real URL in data-src/srcset until the <img> scrolls into view
        // and only set a 1x1/blank placeholder on .src. Fall back to the common
        // lazy attributes when .src is empty or a placeholder, then absolutize.
        const isPh = (u) => !u
          || /^data:image\\/(gif|svg)/i.test(u)
          || /(blank|placeholder|spacer|loading|transparent|grey|gray|1x1|s\\.gif)\\.(gif|png|svg|webp)/i.test(u);
        let u = el.currentSrc || el.src || "";
        if (isPh(u)) {{
          u = el.getAttribute("data-src") || el.getAttribute("data-lazy-src")
            || el.getAttribute("data-original") || el.getAttribute("data-ks-lazyload")
            || el.getAttribute("data-url") || el.getAttribute("data-image") || u;
        }}
        if (isPh(u)) {{
          const ss = el.getAttribute("srcset") || el.getAttribute("data-srcset") || "";
          if (ss) {{ const first = ss.split(",")[0].trim().split(/\\s+/)[0]; if (first) u = first; }}
        }}
        try {{ if (u && !/^(https?:|data:|\\/\\/)/i.test(u)) u = new URL(u, location.href).href; }} catch (e) {{}}
        if (/^\\/\\//.test(u)) u = location.protocol + u;
        return u || "";
      }}
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
    let filteredCount = 0;
    let stoppedByLimit = false;
    for (let domOrder = 0; domOrder < nodes.length; domOrder++) {{
      if (rows.length >= limit) {{ stoppedByLimit = true; break; }}
      const el = nodes[domOrder];
      const visible = isVisible(el);
      if (visibleOnly && !visible) {{ filteredCount++; continue; }}
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
      filteredCount,
      stoppedByLimit,
      truncated: stoppedByLimit
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
    read_only_eval: bool = False,
    internal: bool = False,
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
        read_only_eval=read_only_eval,
        internal=internal,
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
            read_only_eval=read_only_eval,
            internal=internal,
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
            read_only_eval=read_only_eval,
            internal=internal,
        )
        state = await _invoke_browser_method(
            agent,
            "Page.getState",
            {"pageId": page_id, "purpose": "Read JSON title side-channel chunk"},
            step,
            read_only_eval=read_only_eval,
            internal=internal,
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
            for nested in item.values():
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
    probe: JsonDict = {
        "trigger": "Page.create_-32005",
        "originalError": original_error[:500],
        "fleetList": None,
        "pageLists": [],
        "checkedPages": [],
        "classification": "unknown",
    }
    page_candidates: List[JsonDict] = []

    fleet_list = await _page_create_probe_call(agent, "Fleet.list", {})
    probe["fleetList"] = fleet_list
    page_candidates.extend(_pages_from_value(fleet_list.get("response")))
    fleets = _raw_response_data(fleet_list.get("response")).get("fleets")
    if isinstance(fleets, list):
        for fleet in fleets:
            if not isinstance(fleet, dict):
                continue
            fleet_id = str(fleet.get("fleetId") or "").strip()
            if not fleet_id:
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

    page_candidates.extend(_pages_from_value(getattr(agent, "preloaded_registration", None)))
    deduped: Dict[str, JsonDict] = {}
    for page in page_candidates:
        page_id = str(page.get("pageId") or page.get("page_id") or "").strip()
        if page_id:
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
        checked = {
            "pageId": page_id,
            "fleetId": str(page.get("fleetId") or state_data.get("fleetId") or ""),
            "ok": bool(state.get("ok")) and _page_state_is_usable(state.get("response")),
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

    Auth/paywall overlays are still never auto-clicked: dismiss_overlay returns
    `blocked` for those, and the original error/hint is preserved."""
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
    target_method = method if method == "Input.click" else ""
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
    # tree. "blocked" = auth/paywall: dismiss made no page mutation, so the
    # snapshot stays valid.
    if dismiss_status != "blocked":
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
        else "blocked" if dismiss_status == "blocked"
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


def _check_target_param_requirements(
    method: str,
    params: JsonDict,
    method_schemas: Optional[dict] = None,
) -> Optional[JsonDict]:
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
    # Canonical id format guard: catch a malformed id here (clear, actionable
    # error) rather than letting it reach the browser as a -32602 Invalid params.
    id_format_error = _check_id_param_format(method, params, method_schemas)
    if id_format_error is not None:
        return id_format_error
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


from .composites.dismiss_overlay import (
    DISMISS_OVERLAY_MAX_ATTEMPTS,
    DISMISS_OVERLAY_MAX_DURATION_MS,
    _dismiss_overlay,
    _maybe_retry_original_action,
    _try_backdrop_click,
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
    task_type: Any = "general",
) -> List[JsonDict]:
    hidden = hidden_harness_tools_for_task_type(task_type)
    return [
        spec
        for spec in BROWSER_TOOLS.tool_specs(capability_methods)
        if spec.get("name") not in hidden
    ]
