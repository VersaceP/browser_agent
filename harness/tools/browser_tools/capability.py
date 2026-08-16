"""
harness.tools.browser_tools.capability - ABCP capability execution core (browser_call dispatch layer).
"""

import re
from typing import Any
from typing import List
from typing import Optional
from typing import Set
from typing import Tuple
from abcp_client import ABCPTransportError
from harness.observation.challenge_detector import detect_structural_challenge
from harness.diagnostics.error_classification import attach_error_classification
from harness.fleet.runtime import FleetClickGateTimeout
from harness.offload import offload_large_tool_result
from harness.observation.render_recovery import build_render_recovery_runner
from harness.runtime_evaluation import MAIN_WORLD_REQUIRED_PREFIX
from harness.runtime_evaluation import RuntimeEvaluationService
from harness.runtime_evaluation import runtime_last_resort_evidence
from harness.task_types import resolve_task_type_fail_closed
from harness.tool_policy import mask_params
from harness.tools.parsers import attach_method_schema
from harness.tools.parsers import ensure_required_purpose
from harness.tools.parsers import parse_browser_call_params
from harness.tools.parsers import parse_direct_capability_params
from harness.utils import JsonDict
from harness.utils import optional_int
from harness.workflow_runtime import workflow_execution_disabled_result
from harness.workflow_runtime import workflow_execution_enabled
from .axtree_state import _axtree_nodes_from_lines
from .axtree_state import _browser_side_rematch_mode
from .axtree_state import _check_stale_axtree_target
from .axtree_state import _observe_axtree_state_after
from .axtree_state import _precompute_axtree_snapshot
from harness.workflow_policy import validate_workflow_params

def _bt():
    import harness.tools.browser_tools as bt

    return bt

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
            **_bt()._allowed_tool_hint(agent),
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
        _bt()._observe_progress_after(agent, method or "browser_call.params_error", result)
        _bt()._observe_progress_before(
            agent,
            method or "browser_call",
            params if isinstance(params, dict) else tool_input,
            step,
            charge_diagnostic=False,
        )
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
        _bt()._observe_progress_after(agent, method or "browser_call_rejected", result)
        _bt()._observe_progress_before(
            agent, method or "browser_call", params, step,
            charge_diagnostic=False,
        )
        agent.trace.append({"type": "browser_call_rejected", "result": result})
        return result, False

    params, shadow_dom_defaulted = _bt()._default_semantic_tree_shadow_dom(
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
    params, image_output_receipt = _bt()._normalize_dom_get_img_output(
        agent, method, params,
    )

    navigation_context, navigation_context_error = _bt()._prepare_navigation_context(
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

    params, screenshot_output_receipt = _bt()._normalize_screenshot_output(method, params)
    if screenshot_output_receipt is not None:
        agent.logger.write(
            "browser.call.screenshot_output_normalized",
            {"method": method, **screenshot_output_receipt},
        )

    if method == "Runtime.evaluate":
        prepared, policy_error = _bt()._prepare_runtime_evaluation(
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
            runtime_json_expression = _bt()._build_runtime_json_expression(
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

    contract_result = _bt()._check_worker_contract(agent, method)
    if contract_result is not None:
        attach_error_classification(contract_result, method=method)
        attach_method_schema(contract_result, method, agent.method_schemas)
        agent.logger.write("browser.call.contract_violation", contract_result)
        _bt()._observe_progress_after(agent, method or "browser_call.contract_violation", contract_result)
        _bt()._observe_progress_before(
            agent, method or "browser_call", params, step,
            charge_diagnostic=False,
        )
        agent.trace.append({"type": "contract_violation", "result": contract_result})
        return contract_result, False

    memory_scope_guard = _bt()._check_cross_task_memory_scope(agent, method, params)
    if memory_scope_guard is not None:
        agent.logger.write(
            "browser.call.cross_task_memory_rejected", memory_scope_guard
        )
        agent.trace.append({
            "type": "cross_task_memory_guard",
            "result": memory_scope_guard,
        })
        return memory_scope_guard, False

    fleet_binding_guard, fleet_binding_receipt = _bt()._apply_fleet_binding(
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

    page_binding_guard = _bt()._check_page_binding(agent, method, params)
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

    auth_barrier_guard = await _bt()._fleet_auth_barrier_before_call(
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
    workflow_auth_started_generation = _bt()._workflow_auth_started_generation(
        agent, method
    )

    lifecycle_guard = await _bt()._page_lifecycle_guard_before(agent, method, params)
    if lifecycle_guard is not None:
        agent.logger.write("browser.call.lifecycle_gated", lifecycle_guard)
        agent.trace.append({
            "type": "page_lifecycle_gate",
            "method": method,
            "result": lifecycle_guard,
        })
        return lifecycle_guard, False

    screenshot_guard = _bt()._check_screenshot_misuse(method, params, reason)
    if screenshot_guard is not None:
        agent.logger.write("browser.call.screenshot_rejected", screenshot_guard)
        agent.trace.append({"type": "screenshot_guard", "result": screenshot_guard})
        return screenshot_guard, False

    target_param_guard = _bt()._check_target_param_requirements(
        method, params, getattr(agent, "method_schemas", {})
    )
    if target_param_guard is not None:
        attach_error_classification(target_param_guard, method=method)
        attach_method_schema(target_param_guard, method, agent.method_schemas)
        agent.logger.write("browser.call.params_error", target_param_guard)
        _bt()._observe_progress_after(agent, method or "browser_call.params_error", target_param_guard)
        _bt()._observe_progress_before(
            agent, method or "browser_call", params, step,
            charge_diagnostic=False,
        )
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

    _bt()._observe_progress_before(agent, method, params, step)

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
    _bt()._ensure_hitl_request_reason(method, params, reason)
    page_create_claim_guard, page_create_takeover_claimed = (
        await _bt()._claim_ownerless_fleet_auth_barrier_for_page_create(
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
    captcha_short_circuit = await _bt()._maybe_autosolve_before_model_pause(
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
    hitl_claim_guard = await _bt()._claim_fleet_auth_barrier_for_hitl(
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
            await _bt()._capture_hitl_pause_snapshot(
                agent,
                runner,
                str(params.get("pageId") or ""),
                step,
            )
        _bt()._page_lifecycle_before_action(agent, method, params)
        reused_download = (
            _bt()._reusable_download_response(agent, params)
            if method == "Download.start" else None
        )
        if method == "Download.start" and reused_download is None:
            reused_download = await _bt()._refresh_active_download_response(
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
                if method != "Download.start" or not _bt()._download_start_timed_out(exc):
                    raise
                download_timeout_error = exc
                response, _recovery = {}, None

        if method == "Download.list":
            known_downloads = _bt()._download_receipt_store(agent)
            for download_record in _bt()._download_records(response):
                if _bt()._download_operation_key(download_record) in known_downloads:
                    _bt()._remember_download_record(agent, download_record)
        elif method == "Download.start" and (
            download_timeout_error is not None
            or _bt()._download_start_timed_out(response)
        ):
            reconciliation = await _bt()._reconcile_download_start_timeout(
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
                _bt()._remember_download_record(agent, {
                    **params,
                    **data,
                })
        if method == "Runtime.evaluate" and runtime_receipt:
            runtime_receipt["attempts"] = [
                _bt()._runtime_attempt_receipt(response, "isolated")
            ]
            if (
                _bt()._invoke_result_failed({"method": method, "response": response})
                and runtime_receipt.get("mainFallbackAuthorized") is True
                and _bt()._runtime_main_fallback_signaled(response)
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
                    _bt()._runtime_attempt_receipt(response, "main")
                )

            final_attempt = runtime_receipt["attempts"][-1]
            runtime_receipt["executedWorld"] = final_attempt.get("executedWorld")
            expected_world = str(final_attempt.get("requestedWorld") or "")
            runtime_receipt["dispatchedWorld"] = expected_world
            runtime_receipt["worldEvidenceStrength"] = final_attempt.get(
                "evidenceStrength", "strong"
            )
            if not _bt()._invoke_result_failed({"method": method, "response": response}):
                metadata_supplied = _bt()._runtime_response_world_metadata_supplied(response)
                if metadata_supplied and not _bt()._runtime_response_world_verified(
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
        _bt()._page_lifecycle_after_action(agent, method, params, response)
        response = agent._capture_artifacts(method, response)
        structural_challenge = detect_structural_challenge(method, response)
        record_file_action = getattr(agent, "_capture_file_action", None)
        if callable(record_file_action):
            record_file_action(method, params, response)
        response = _bt()._annotate_dom_batch_response(method, response)
        page_list_receipt: JsonDict = {}
        if method == "Page.list":
            response, page_list_receipt = _bt()._filter_page_list_response(
                agent, response
            )
            shown_sidecar = page_list_receipt.pop("_shownInventoryPages", None)
            if isinstance(shown_sidecar, list):
                page_list_shown = [
                    dict(row) for row in shown_sidecar if isinstance(row, dict)
                ]
        axtree_snapshot = _precompute_axtree_snapshot(method, params, response)
        response = agent._offload_response(method, params, response, step)
        _bt()._annotate_axtree_offload(response, axtree_snapshot)

        hitl_pause_succeeded = (
            method == "Hitl.requestPause" and _bt()._hitl_pause_succeeded(response)
        )
        if hitl_pause_succeeded:
            response = await _bt()._enrich_pause_with_wait(agent, params, response, step)

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
                payload = _bt()._runtime_any_json_payload(result)
                if isinstance(payload, dict) and payload.get("error"):
                    result["runtimeJSONError"] = {
                        "error": str(payload.get("error")),
                        "stack": str(payload.get("stack") or "")[:1000],
                    }
                elif isinstance(payload, dict) and "value" in payload:
                    _bt()._attach_runtime_json_value(
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
            **_bt()._transport_error_metadata(method, exc),
        }
        attach_method_schema(result, method, agent.method_schemas)

    if image_output_receipt is not None:
        result["normalizedFields"] = ["params.options.path"]
        result["outputNormalization"] = image_output_receipt

    if runtime_receipt and "runtimePolicy" not in result:
        result["runtimePolicy"] = runtime_receipt

    result = await _bt()._quarantine_workflow_result_after_auth_change(
        agent,
        method,
        result,
        started_generation=workflow_auth_started_generation,
        emit_telemetry=True,
    )
    relinquished = await _bt()._relinquish_fleet_auth_resolver_after_failed_pause(
        agent,
        method,
        pause_succeeded=hitl_pause_succeeded,
    )
    if relinquished:
        result["fleetAuthBarrier"] = relinquished

    lost_fleet_result = _bt()._assigned_fleet_lost_result(
        agent, method, params, result
    )
    if lost_fleet_result is not None:
        result = lost_fleet_result
        page_create_should_stop = True
    elif _bt()._is_page_create_32005_failure(method, result):
        result, page_create_should_stop = await _bt()._recover_page_create_32005(
            agent,
            params,
            result,
        )
    page_create_relinquished = (
        await _bt()._relinquish_fleet_auth_resolver_after_failed_recovery_page_create(
            agent,
            method,
            takeover_claimed=page_create_takeover_claimed,
            call_succeeded=not _bt()._invoke_result_failed(result),
        )
    )
    if page_create_relinquished:
        result["fleetAuthBarrier"] = page_create_relinquished
    _bt()._observe_page_binding_after(agent, method, params, result)
    if fleet_binding_receipt and (
        method in {"Page.create", "Page.list"} or method.startswith("Fleet.")
    ):
        result.update(fleet_binding_receipt)
    attach_error_classification(result, method=method)
    result = _bt()._apply_select_failure_guidance(agent, method, params, result)
    if method == "Runtime.evaluate" and _bt()._invoke_result_failed(result):
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
            "error": _bt()._runtime_evaluation_error_text(result)[:2000],
            "final": True,
        }
        result["next_instruction"] = (
            "The guarded Runtime evaluation exhausted its authorized strict"
            " world attempts or received invalid/mismatched platform world"
            " evidence. Do not"
            " request main directly or repeat Runtime.evaluate; report this blocker."
        )
    _bt()._fleet_auth_barrier_after_call(agent, method, result)
    result = _bt()._attach_navigation_check(result, method=method, params=params)
    result = _bt()._attach_runtime_strategy_hints(result, method=method)
    if not page_create_should_stop:
        result = await _bt()._maybe_auto_hitl_for_challenge(agent, method, params, result, step)
    result = _bt()._attach_normalized_handles(result)
    result = _bt()._settle_page_inventory_signal(
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
    result = _bt()._observe_content_completeness_after(
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
                            _bt()._response_data(result).get("pageId")
                            or _bt()._response_data(result).get("id")
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
    _bt()._observe_navigation_progress_after(agent, method, params, result)
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
        result = await _bt()._maybe_auto_intercept_overlay(agent, method, params, result, step)
    # VL Role D: if the call still failed with a visual/occlusion/challenge/locator
    # error after deterministic recovery, auto-route it to the VL arbiter and attach
    # a recovery recommendation (resolvedId / hitl / dismiss / reperceive). Gated by
    # vl.arbiter_enabled; non-visual failures and disabled VL are no-ops.
    if not page_create_should_stop:
        result = await _bt()._maybe_vl_arbitrate(agent, method, params, result, step)
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
    _bt()._observe_progress_after(agent, method, model_result)
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
    params, screenshot_output_receipt = _bt()._normalize_screenshot_output(method, params)
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
    auth_barrier_guard = await _bt()._fleet_auth_barrier_before_call(
        agent, method, params
    )
    if auth_barrier_guard is not None:
        return auth_barrier_guard
    workflow_auth_started_generation = _bt()._workflow_auth_started_generation(
        agent, method
    )
    if lifecycle_cleanup_bypass and not internal:
        return {
            "status": "rejected",
            "policy_violation": "lifecycle_cleanup_bypass_requires_internal",
            "tool_was_executed": False,
        }
    if not lifecycle_cleanup_bypass:
        lifecycle_guard = await _bt()._page_lifecycle_guard_before(agent, method, params)
        if lifecycle_guard is not None:
            return lifecycle_guard
    _bt()._ensure_hitl_request_reason(method, params, str(params.get("purpose") or ""))
    page_create_claim_guard, page_create_takeover_claimed = (
        await _bt()._claim_ownerless_fleet_auth_barrier_for_page_create(
            agent, method, params
        )
    )
    if page_create_claim_guard is not None:
        return page_create_claim_guard
    hitl_claim_guard = await _bt()._claim_fleet_auth_barrier_for_hitl(
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
            await _bt()._capture_hitl_pause_snapshot(
                agent,
                runner,
                str(params.get("pageId") or ""),
                step,
            )
        _bt()._page_lifecycle_before_action(agent, method, params)
        response, _recovery = await runner.call(method, params, **runner_kwargs)
        _bt()._page_lifecycle_after_action(agent, method, params, response)
        response = agent._capture_artifacts(method, response)
        structural_challenge = detect_structural_challenge(method, response)
        record_file_action = getattr(agent, "_capture_file_action", None)
        if callable(record_file_action):
            record_file_action(method, params, response)
        axtree_snapshot = _precompute_axtree_snapshot(method, params, response)
        response = agent._offload_response(method, params, response, step)
        _bt()._annotate_axtree_offload(response, axtree_snapshot)
        hitl_pause_succeeded = (
            method == "Hitl.requestPause" and _bt()._hitl_pause_succeeded(response)
        )
        if hitl_pause_succeeded:
            response = await _bt()._enrich_pause_with_wait(agent, params, response, step)
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
            **_bt()._transport_error_metadata(method, exc),
        }
        attach_method_schema(result, method, agent.method_schemas)

    result = await _bt()._quarantine_workflow_result_after_auth_change(
        agent,
        method,
        result,
        started_generation=workflow_auth_started_generation,
        emit_telemetry=False,
    )
    relinquished = await _bt()._relinquish_fleet_auth_resolver_after_failed_pause(
        agent,
        method,
        pause_succeeded=hitl_pause_succeeded,
    )
    if relinquished:
        result["fleetAuthBarrier"] = relinquished

    if _bt()._is_page_create_32005_failure(method, result):
        result, _page_create_should_stop = await _bt()._recover_page_create_32005(
            agent,
            params,
            result,
        )
        if "params" in result:
            result["params"] = _shown_params(params)
    page_create_relinquished = (
        await _bt()._relinquish_fleet_auth_resolver_after_failed_recovery_page_create(
            agent,
            method,
            takeover_claimed=page_create_takeover_claimed,
            call_succeeded=not _bt()._invoke_result_failed(result),
        )
    )
    if page_create_relinquished:
        result["fleetAuthBarrier"] = page_create_relinquished
    attach_error_classification(result, method=method)
    result = _bt()._apply_select_failure_guidance(agent, method, params, result)
    _bt()._fleet_auth_barrier_after_call(agent, method, result)
    result = _bt()._attach_navigation_check(result, method=method, params=params)
    result = _bt()._attach_runtime_strategy_hints(result, method=method)
    if not internal:
        result = await _bt()._maybe_auto_hitl_for_challenge(agent, method, params, result, step)
    result = _bt()._attach_normalized_handles(result)
    result = _bt()._settle_page_inventory_signal(agent, method, params, result)
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
        _bt()._observe_progress_after(agent, method, model_result)
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
