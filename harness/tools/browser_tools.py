"""
harness.tools.browser_tools - BrowserAgent tool schemas and dispatch factory.
"""

import asyncio
import base64
import copy
import re
from typing import Any, Awaitable, Callable, List, Optional, Set, Tuple

import json
import uuid
from pathlib import Path

from abcp_client import ABCPTransportError
from harness.challenge_detector import (
    ChallengeTracker,
    extract_page_id,
    is_lingering_loading_title,
)
from harness.hitl import wait_for_hitl_resume
from harness.local_fs import local_fs_jsonpath, local_fs_read, local_fs_search
from harness.offload import offload_large_tool_result
from harness.progress import extraction_artifact_count
from harness.render_recovery import build_render_recovery_runner
from harness.tool_policy import disabled_reason_for_method
from harness.tools.loop_guard import check_tool_call_loop
from harness.tools.parsers import (
    attach_method_schema,
    ensure_required_purpose,
    parse_browser_call_params,
    parse_direct_capability_params,
)
from harness.utils import JsonDict, optional_int
from harness.vl import visual_verify_image


BrowserToolDispatcher = Callable[
    [JsonDict, int],
    Awaitable[Tuple[JsonDict, bool]],
]


def build_browser_tool_dispatcher(agent: Any) -> BrowserToolDispatcher:
    async def dispatch(tool_call: JsonDict, step: int) -> Tuple[JsonDict, bool]:
        return await execute_browser_tool(agent, tool_call, step)

    return dispatch


async def execute_browser_tool(agent: Any, tool_call: JsonDict, step: int) -> Tuple[JsonDict, bool]:
    name = tool_call.get("name")
    raw_tool_input = tool_call.get("input") or {}
    tool_input = (
        raw_tool_input
        if isinstance(raw_tool_input, dict)
        else {"value": raw_tool_input}
    )

    if name == "final_answer":
        answer = str(tool_input.get("answer", "")).strip()
        result = {
            "status": tool_input.get("status", "done"),
            "answer": answer,
            "artifacts": agent.artifacts,
        }
        reason = tool_input.get("reason")
        if isinstance(reason, str) and reason.strip():
            result["reason"] = reason.strip()[:200]
        agent.logger.write("tool.final_answer", result)
        agent.trace.append({"type": "final_answer", "result": result})
        return result, True

    progress_gate = _check_extraction_progress_gate(agent, str(name or ""))
    if progress_gate is not None:
        agent.trace.append({"type": "progress_gate", "result": progress_gate})
        return progress_gate, False

    # Loop guard: short-circuit if the model is hammering the same tool with
    # the same args. final_answer is exempted above so a deliberate retry of
    # the terminal call doesn't trip the guard.
    short_circuit = check_tool_call_loop(
        agent,
        name=str(name or ""),
        tool_input=tool_input,
        step=step,
    )
    if short_circuit is not None:
        guard_result, should_stop = short_circuit
        agent.trace.append({"type": "loop_guard", "result": guard_result})
        return guard_result, should_stop

    if name == "local_fs_search":
        contract_result = _check_worker_contract(agent, "local_fs_search")
        if contract_result is not None:
            agent.trace.append({"type": "contract_violation", "result": contract_result})
            return contract_result, False
        progress_result = _check_progress_before(agent, "local_fs_search")
        if progress_result is not None:
            agent.trace.append({"type": "progress_intervention", "result": progress_result})
            return progress_result, False
        result = local_fs_search(
            agent.logger,
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
        _observe_progress_after(agent, "local_fs_search", result)
        agent.trace.append({"type": "local_fs_search", "result": result})
        return result, False

    if name == "local_fs_read":
        contract_result = _check_worker_contract(agent, "local_fs_read")
        if contract_result is not None:
            agent.trace.append({"type": "contract_violation", "result": contract_result})
            return contract_result, False
        progress_result = _check_progress_before(agent, "local_fs_read")
        if progress_result is not None:
            agent.trace.append({"type": "progress_intervention", "result": progress_result})
            return progress_result, False
        result = local_fs_read(
            agent.logger,
            path=str(tool_input.get("path") or ""),
            line_offset=optional_int(tool_input.get("line_offset"), 0) or 0,
            line_limit=optional_int(tool_input.get("line_limit"), 200) or 200,
            max_bytes=min(
                optional_int(
                    tool_input.get("max_bytes"),
                    agent.runtime.harness.local_fs_max_read_bytes,
                ) or agent.runtime.harness.local_fs_max_read_bytes,
                agent.runtime.harness.local_fs_max_read_bytes,
            ),
        )
        _observe_progress_after(agent, "local_fs_read", result)
        agent.trace.append({"type": "local_fs_read", "result": result})
        return result, False

    if name == "local_fs_jsonpath":
        contract_result = _check_worker_contract(agent, "local_fs_jsonpath")
        if contract_result is not None:
            agent.trace.append({"type": "contract_violation", "result": contract_result})
            return contract_result, False
        progress_result = _check_progress_before(agent, "local_fs_jsonpath")
        if progress_result is not None:
            agent.trace.append({"type": "progress_intervention", "result": progress_result})
            return progress_result, False
        result = local_fs_jsonpath(
            agent.logger,
            path=str(tool_input.get("path") or ""),
            expr=str(tool_input.get("expr") or "$"),
            mode=str(tool_input.get("mode") or "auto"),
            max_nodes=optional_int(tool_input.get("max_nodes"), 50) or 50,
            max_bytes_per_node=(
                optional_int(tool_input.get("max_bytes_per_node"), 1000) or 1000
            ),
        )
        _observe_progress_after(agent, "local_fs_jsonpath", result)
        agent.trace.append({"type": "local_fs_jsonpath", "result": result})
        return result, False

    if name == "record_extraction":
        result = _record_extraction(agent, tool_input)
        if result.get("status") == "done":
            agent.pending_unrecorded_extraction = None
        _observe_progress_after(agent, "record_extraction", result)
        agent.trace.append({"type": "record_extraction", "result": result})
        return result, False

    if name == "extract_dom_records":
        contract_result = _check_worker_contract(agent, "extract_dom_records")
        if contract_result is not None:
            agent.trace.append({"type": "contract_violation", "result": contract_result})
            return contract_result, False
        result = await _extract_dom_records(agent, tool_input, step)
        _observe_progress_after(agent, "extract_dom_records", result)
        agent.trace.append({"type": "extract_dom_records", "result": result})
        return result, False

    if name == "eval_js_json":
        contract_result = _check_worker_contract(agent, "eval_js_json")
        if contract_result is not None:
            agent.trace.append({"type": "contract_violation", "result": contract_result})
            return contract_result, False
        result = await _eval_js_json_tool(agent, tool_input, step)
        result = await _maybe_auto_hitl_for_challenge(
            agent,
            "eval_js_json",
            {"pageId": tool_input.get("pageId")},
            result,
            step,
        )
        _observe_progress_after(agent, "eval_js_json", result)
        agent.trace.append({"type": "eval_js_json", "result": result})
        return result, False

    if name == "navigate_verified":
        contract_result = _check_worker_contract(agent, "navigate_verified")
        if contract_result is not None:
            agent.trace.append({"type": "contract_violation", "result": contract_result})
            return contract_result, False
        progress_result = _check_progress_before(agent, "navigate_verified")
        if progress_result is not None:
            agent.trace.append({"type": "progress_intervention", "result": progress_result})
            return progress_result, False
        result = await _navigate_verified(agent, tool_input, step)
        if result.get("status") not in {
            "done",
            "blocked_by_challenge",
            "hitl_required",
            "hitl_timeout",
            "page_settled_after_hitl",
        }:
            result = await _maybe_auto_hitl_for_challenge(
                agent,
                "navigate_verified",
                {"pageId": tool_input.get("pageId")},
                result,
                step,
            )
        _observe_progress_after(agent, "navigate_verified", result)
        agent.trace.append({"type": "navigate_verified", "result": result})
        return result, False

    if name == "visual_verify":
        contract_result = _check_worker_contract(agent, "visual_verify")
        if contract_result is not None:
            agent.trace.append({"type": "contract_violation", "result": contract_result})
            return contract_result, False
        progress_result = _check_progress_before(agent, "visual_verify")
        if progress_result is not None:
            agent.trace.append({"type": "progress_intervention", "result": progress_result})
            return progress_result, False
        result = await _visual_verify(agent, tool_input, step)
        _observe_progress_after(agent, "visual_verify", result)
        agent.trace.append({"type": "visual_verify", "result": result})
        return result, False

    direct_method = str(name or "").strip()
    if name == "browser_call":
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
            "error": f"Unknown harness tool: {name}",
            "allowed_tools": [
                "browser_call",
                "extract_dom_records",
                "eval_js_json",
                "navigate_verified",
                "visual_verify",
                "final_answer",
                "local_fs_search",
                "local_fs_read",
                "local_fs_jsonpath",
            ],
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
        attach_method_schema(contract_result, method, agent.method_schemas)
        agent.logger.write("browser.call.contract_violation", contract_result)
        _observe_progress_after(agent, method or "browser_call.contract_violation", contract_result)
        progress_result = _check_progress_before(agent, method or "browser_call")
        if progress_result is not None:
            agent.trace.append({"type": "progress_intervention", "result": progress_result})
            return progress_result, False
        agent.trace.append({"type": "contract_violation", "result": contract_result})
        return contract_result, False

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

    result = await _maybe_auto_hitl_for_challenge(agent, method, params, result, step)
    agent.logger.write("browser.call.result", agent._trim_for_log(result))
    model_result = agent._clean_for_model(result)
    model_result = offload_large_tool_result(
        logger=agent.logger,
        tool_name=method or str(name or "browser_call"),
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

    result = await _maybe_auto_hitl_for_challenge(agent, method, params, result, step)
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
    if not page_id:
        return {"status": "failed", "error": "pageId is required"}
    if not expression:
        return {"status": "failed", "error": "expression is required"}

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
    if wait.get("status") in {"timeout", "page_settled_after_hitl"}:
        status = str(wait.get("status"))
    else:
        status = "hitl_required"
    next_instruction = (
        "The page appears to be past the challenge, but ABCP still reports it"
        " paused. Do not keep polling; call final_answer with"
        " status=\"page_settled_after_hitl\" and surface that the ABCP control"
        " channel has not released the paused page yet."
        if status == "page_settled_after_hitl" else
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
    if getattr(agent, "vl_check_count", 0) >= max_checks:
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

    agent.vl_check_count = getattr(agent, "vl_check_count", 0) + 1
    verdict = await visual_verify_image(
        config=vl_config,
        image_path=image_path,
        expected=expected,
        mode=mode,
        question=question,
    )
    result = {
        **verdict,
        "mode": mode,
        "screenshotPath": image_path,
        "selector": selector or None,
        "id": element_id or None,
        "vlCheckCount": agent.vl_check_count,
        "maxChecksPerWorker": max_checks,
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
(() => {{
  try {{
    const __abcpExpression = {expression_json};
    const __abcpValue = (0, eval)("(" + __abcpExpression + ")");
    return JSON.stringify({{ value: __abcpValue }});
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
) -> Optional[Any]:
    prefix = "__ABCP_JSON__"
    setup = f"""
(() => {{
  try {{
    const text = String(({json_string_expression}) ?? "null");
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
    ready = await _invoke_browser_method(
        agent,
        "Page.getState",
        {"pageId": page_id, "purpose": "Read JSON side-channel ready marker"},
        step,
    )
    title = str(_response_data(ready).get("title") or "")
    if not title.startswith(f"{prefix}|READY|"):
        return None
    try:
        total_len = int(title.rsplit("|", 1)[-1])
    except ValueError:
        return None

    chunks: List[str] = []
    while sum(len(chunk) for chunk in chunks) < total_len:
        if len(chunks) >= max_chunks:
            return None
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
            return None
        chunks.append(parts[3])

    try:
        text = base64.b64decode("".join(chunks).encode("ascii")).decode("utf-8")
        return json.loads(text)
    except (ValueError, UnicodeDecodeError):
        return None


def _response_data(result: JsonDict) -> JsonDict:
    response = result.get("response") if isinstance(result, dict) else None
    if not isinstance(response, dict):
        return {}
    data = response.get("data")
    return data if isinstance(data, dict) else {}


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
    if not tracker.should_adjudicate(page_id, step):
        enriched = dict(result)
        enriched["suspected_challenge"] = {
            **state.to_summary(),
            "adjudication": "not_ready",
        }
        return enriched
    return await _adjudicate_and_maybe_hitl(agent, page_id, method, result, step)


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
) -> JsonDict:
    agent.logger.write(
        "hitl.auto_request_pause",
        {
            "pageId": page_id,
            "triggerMethod": trigger_method,
            "reason": reason,
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
            "next_instruction": (
                "Use a method allowed by the task_type policy, or finalize with"
                " a blocker if this task really requires the disabled domain."
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
    """Persist a structured extraction artifact for downstream SkillAgent /
    LeadAgent consumption. Returns a stub describing the saved file.

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
    if not isinstance(raw_rows, list):
        return {
            "status": "rejected",
            "error": "rows must be a JSON array of records (dicts)",
        }

    rows: list = []
    for index, raw in enumerate(raw_rows):
        if not isinstance(raw, dict):
            return {
                "status": "rejected",
                "error": (
                    f"rows[{index}] must be a JSON object; got "
                    f"{type(raw).__name__}"
                ),
            }
        rows.append(raw)

    safe_name = (
        "".join(c if c.isalnum() or c in "-_." else "-" for c in raw_name)[:80]
        or "extraction"
    )
    task_dir = Path(
        getattr(agent.logger, "task_dir", "")
        or agent.runtime.harness.runs_dir
        or "."
    )
    out_dir = task_dir / "artifacts" / "extractions"
    out_dir.mkdir(parents=True, exist_ok=True)
    unique = uuid.uuid4().hex[:8]
    file_path = out_dir / f"{safe_name}-{unique}.json"

    payload = {
        "name": raw_name,
        "description": description or None,
        "schema": raw_schema if isinstance(raw_schema, (dict, list)) else None,
        "rowCount": len(rows),
        "rows": rows,
    }
    file_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    absolute_path = str(file_path.resolve())
    if absolute_path not in agent.artifacts:
        agent.artifacts.append(absolute_path)
    agent.logger.write(
        "tool.record_extraction",
        {
            "name": raw_name,
            "rowCount": len(rows),
            "savedPath": absolute_path,
        },
    )
    return {
        "status": "done",
        "name": raw_name,
        "rowCount": len(rows),
        "savedPath": absolute_path,
        "next_step": (
            "Pass this savedPath to LeadAgent so it can include it in"
            " run_skill_agent.evidence_artifacts."
        ),
    }


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
    )
    enriched = dict(response)
    enriched["hitl_wait"] = wait_result
    if wait_result.get("status") == "resumed":
        enriched["suggested_prompt"] = (
            "Page has resumed from HITL. Re-check page state before issuing"
            " new actions; the user may have navigated."
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


def build_browser_agent_tool_specs(capability_methods: Set[str]) -> List[JsonDict]:
    method_schema: JsonDict = {
        "type": "string",
        "description": "ABCP capability method, e.g. Fleet.create, Page.navigate, DOM.getAXTree.",
    }
    if capability_methods:
        method_schema["enum"] = sorted(capability_methods)

    return [
        {
            "name": "browser_call",
            "description": "Invoke a single ABCP Browser atomic capability and return the browser observation/data.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "method": method_schema,
                    "params": {
                        "type": "object",
                        "description": "JSON object of params for the ABCP method; pass {} when there are none.",
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
        },
        {
            "name": "extract_dom_records",
            "description": (
                "Extract a repeated DOM collection into structured rows without hand-written JS."
                " Use this for lists, tables, cards, and link collections instead of parsing"
                " large AXTree text. Internally wraps Runtime.evaluate in an explicit return IIFE."
            ),
            "input_schema": {
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
            "strict": True,
        },
        {
            "name": "eval_js_json",
            "description": (
                "Evaluate a JavaScript expression and force a JSON return through a"
                " harness wrapper. Use this instead of raw Runtime.evaluate when you"
                " need structured data back. For statement bodies, pass an IIFE"
                " expression such as (() => { const rows = []; return rows; })()."
            ),
            "input_schema": {
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
                },
                "required": [
                    "pageId",
                    "expression",
                    "record_name",
                    "description",
                ],
                "additionalProperties": False,
            },
            "strict": True,
        },
        {
            "name": "navigate_verified",
            "description": (
                "Navigate to a URL, poll Page.getState, and verify actual URL/title."
                " Prefer this over raw Page.navigate when URL correctness matters."
            ),
            "input_schema": {
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
            "strict": True,
        },
        {
            "name": "visual_verify",
            "description": (
                "Take a screenshot and ask the configured VL model to verify an"
                " action/page-state outcome. Use only for visual arbitration after"
                " click/navigation uncertainty, validator failure, overlays, CAPTCHA,"
                " or layout mismatch. Do not use for bulk data extraction."
            ),
            "input_schema": {
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
            "strict": True,
        },
        {
            "name": "final_answer",
            "description": (
                "Terminate orchestration and return the structured result to LeadAgent."
                " The `status` field is restricted to the whitelist below; other terminal states"
                " (hitl_*, page_crashed, browser_api_contract_error, context_limit_exceeded,"
                " step_budget_exhausted) are detected and set by the harness — do not self-report them."
            ),
            "input_schema": {
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
                        "description": "JSON string containing outcome, data, evidence, next_steps.",
                    },
                    "reason": {
                        "type": "string",
                        "description": "Optional; brief justification (≤ 200 chars) for non-done statuses.",
                    },
                },
                "required": ["status", "answer"],
                "additionalProperties": False,
            },
            "strict": True,
        },
        {
            "name": "record_extraction",
            "description": (
                "Persist structured data already observed in the browser (product URLs/titles, form fields,"
                " list rows, etc.) to an artifact that LeadAgent / SkillAgent can reuse."
                " This is the input side of the SkillAgent evidence contract: fields that never went through"
                " record_extraction must not appear in the final_answer's `data`."
                " `name` identifies the dataset; `rows` must be a list[dict] backed by actual observations."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "Short dataset name, e.g. \"trending-week-products\".",
                    },
                    "rows": {
                        "type": "array",
                        "items": {"type": "object", "additionalProperties": True},
                        "description": "Structured rows; every row must be a JSON object.",
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
        },
        {
            "name": "local_fs_search",
            "description": "Read-only search across files inside the current task worktree; supports glob, JSONL event-type filtering, and per-hit / total output caps.",
            "input_schema": {
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
            "strict": True,
        },
        {
            "name": "local_fs_read",
            "description": "Read-only line-range read of a file inside the current task worktree; well suited to JSONL traces and AXTree lines.txt offload files.",
            "input_schema": {
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
            "strict": True,
        },
        {
            "name": "local_fs_jsonpath",
            "description": "Read-only read of a JSON/JSONL file inside the current task worktree, with a JSONPath subset for node extraction.",
            "input_schema": {
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
            "strict": True,
        },
    ]
