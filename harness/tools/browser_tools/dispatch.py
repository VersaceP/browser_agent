"""
harness.tools.browser_tools.dispatch - Tool registry, dispatcher and model-facing tool handlers.
"""

import asyncio
import copy
import hashlib
import json
import re
import uuid
from functools import lru_cache
from pathlib import Path
from typing import Any
from typing import Awaitable
from typing import Callable
from typing import Dict
from typing import List
from typing import Optional
from typing import Set
from typing import Tuple
from abcp_client import ABCPTransportError
from harness.diagnostics.error_classification import attach_error_classification
from harness.runtime.lifecycle import LifecycleContext
from harness.runtime.lifecycle import lifecycle_for
from harness.tools.local_fs import local_fs_read
from harness.tools.local_fs import local_fs_search
from harness.tools.local_fs import local_fs_list
from harness.tools.file_tools import local_fs_batch
from harness.tools.path_authorization import authorize_tool_call
from harness.prompts import read_harness_guide
from harness.prompts import search_harness_guides
from harness.observation.page_lifecycle import PageLifecycleTracker
from harness.planning.pacing import wait_between_rows
from harness.observation.browser_call import build_browser_call_runner
from harness.tools.runtime_evaluation import RuntimeEvaluationService
from harness.results.call_outcome import replay_forbidden
from harness.workflow.workflow_schema_source import contract_stamp
from harness.tools.tool_policy import collect_sensitive_replacements
from harness.tools.tool_policy import redact_values
from harness.tools.tool_policy import sensitive_browser_method_params
from harness.tools.argument_pipeline import SchemaIssue
from harness.tools.argument_pipeline import apply_registered_tool_defaults
from harness.tools.argument_pipeline import prepare_model_tool_call
from harness.tools.argument_pipeline import tool_argument_error
from harness.tools.argument_pipeline import validate_registered_tool_call
from harness.tools.registry import ToolContext
from harness.tools.registry import ToolRegistry
from harness.utils import JsonDict
from harness.utils import optional_int
from harness.workflow.workflow_runtime import workflow_execution_disabled_result
from harness.workflow.workflow_runtime import workflow_execution_enabled
from .schemas import _browser_input_schemas

def _bt():
    import harness.tools.browser_tools as bt

    return bt

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

    DOM probes wait for known loading to settle. An ambiguous navigation commit
    or missed settlement event triggers one Page.getState call. Re-perception
    obligations still prevent use of stale DOM handles.
    """
    tracker = getattr(agent, "page_lifecycle", None)
    if not isinstance(tracker, PageLifecycleTracker):
        return None
    if method == "Workflow.execute":
        # WebCross owns the sequential action execution, including waitEvent
        # and Page.getState recovery inside the document. An outer one-action
        # resync obligation must not prevent those recovery steps from running.
        # Capability/schema, task binding, Fleet ownership and HITL admission
        # still run independently in the capability dispatcher.
        return None
    page_id = _lifecycle_page_id(agent, params)
    state = tracker.state(page_id)
    if state is None:
        return None

    is_dom_probe = method.startswith("DOM.")
    settled = None
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
    navigation_unknown = (is_dom_probe and state.status == "unknown"
                          and state.requires_state_resync and state.requires_ax_refresh)
    if settled == "timeout" or navigation_unknown:
        runner = getattr(agent, "browser_call_runner", None)
        if runner is None:
            runner = build_browser_call_runner(
                browser=agent.browser,
                logger=agent.logger,
                capability_methods=agent.capability_methods,
            )
            agent.browser_call_runner = runner
        observed_state = (state.generation, state.status)
        try:
            response = await runner.call("Page.getState", {
                "pageId": page_id,
                "purpose": ("Confirm current state after a navigation commit"
                            if navigation_unknown else
                            "One-shot resynchronization after settlement event timeout"),
            })
            current = tracker.state(page_id)
            response_applied = (current.generation, current.status) == observed_state
            if response_applied:
                tracker.observe_state_response(page_id, response)
            agent.logger.write("page.lifecycle.navigation_resync" if navigation_unknown
                               else "page.lifecycle.timeout_resync", {
                **tracker.receipt(page_id), "performed": True,
                "responseApplied": response_applied,
            })
        except Exception as exc:  # the original DOM call remains blocked
            return {
                "status": "page_settlement_unknown",
                "tool_was_executed": False,
                "pageLifecycle": tracker.receipt(page_id),
                "error": str(exc),
                "next_instruction": (
                    "The one-shot Page.getState resynchronization failed."
                    " Do not poll; inspect the failure or recover the page."
                ),
            }

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
    # Download controls are mutually composable (control pause -> resume /
    # cancel). They may dirty page state for later DOM work, but must not
    # deadlock each other behind that deferred resynchronization obligation.
    is_file_control = method.startswith("Download.") or method in {
        "File.download", "File.handleChooser",
    }
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
    if state.requires_ax_refresh:
        from harness.tools.browser_tools.axtree_state import _axtree_ids_from_params
        if method != "DOM.getAXTree" and _axtree_ids_from_params(params):
            return {"status": "page_axtree_refresh_required", "tool_was_executed": False,
                    "pageLifecycle": tracker.receipt(page_id),
                    "next_instruction": "Refresh the AX evidence before using handles from the previous document."}
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
    tracker.observe_navigation_response(method, page_id, response)
    if method == "Page.getState":
        tracker.observe_state_response(page_id, response)
    elif method == "DOM.getAXTree" and not _bt()._invoke_result_failed({"response": response}):
        tracker.observe_ax_refresh(page_id)
    if (
        method in {
            "Page.navigate", "Page.getState", "DOM.getAXTree",
            "Download.start", "File.handleChooser", "Workflow.execute",
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
            _capability_methods_key(capability_methods), contract_stamp(),
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


def _browser_method_from_tool_call(agent: Any, tool_call: Any) -> str:
    """Return the ABCP method a model tool call intended to execute, if any."""
    if not isinstance(tool_call, dict):
        return ""
    name = str(tool_call.get("name") or "").strip()
    raw_input = tool_call.get("input")
    tool_input = raw_input if isinstance(raw_input, dict) else {}
    if name == "browser_call":
        return str(tool_input.get("method") or "").strip()
    if name in getattr(agent, "capability_methods", set()):
        return name
    return ""


def _sensitive_transport_metadata(metadata: JsonDict) -> JsonDict:
    """Drop string-bearing transport fields when the call had secret input.

    The normal browser-call boundary redacts a transport exception before it
    escapes. This dispatcher fallback exists precisely for paths where that
    guarantee may have been bypassed, so it must not forward public prose or a
    receipt's arbitrary strings merely because they have the expected shape.
    Error code and dispatch facts remain useful and cannot contain a supplied
    value under the public contract.
    """
    safe: JsonDict = {}
    for key in (
        "exceptionType",
        "transportCode",
        "connectionFatal",
        "requestSent",
        "rpcCode",
        "requestId",
        "tool_was_executed",
        "retryable",
        "quarantined",
    ):
        if key in metadata:
            safe[key] = metadata[key]
    rpc_data = metadata.get("rpcData")
    error = rpc_data.get("error") if isinstance(rpc_data, dict) else None
    code = error.get("code") if isinstance(error, dict) else None
    if isinstance(code, str) and code.strip():
        safe["rpcData"] = {"error": {"code": code}}
    return safe


def _tool_exception_result(
    agent: Any,
    tool_call: Any,
    exc: Exception,
) -> JsonDict:
    """Return a missed tool exception to the model without claiming no-op.

    This is the dispatcher-level safety net, after individual tools have had
    the chance to return their richer, domain-specific failures.  The exception
    may have been raised at any point in a browser call, so omitting
    ``tool_was_executed`` is deliberate: only a first-hand pre-dispatch receipt
    can prove that an action did not happen.  The model can continue its loop,
    but must re-observe before replaying an action whose outcome is unknown.
    """
    method = _browser_method_from_tool_call(agent, tool_call)
    raw_input = tool_call.get("input") if isinstance(tool_call, dict) else {}
    tool_name = str(tool_call.get("name") or "") if isinstance(tool_call, dict) else ""
    declared_sensitive_params = sensitive_browser_method_params(method)
    if declared_sensitive_params:
        safe_message = (
            "Exception message withheld because this call carried sensitive input."
        )
    else:
        secrets = collect_sensitive_replacements(raw_input)
        message = str(exc).strip() or "exception raised without a message"
        safe_message = redact_values(message, secrets) if secrets else message
    result: JsonDict = {
        "isError": True,
        "status": "tool_exception",
        "error": safe_message[:1200],
        "exceptionType": type(exc).__name__,
        "tool": tool_name,
    }
    if method:
        result["method"] = method

    if isinstance(exc, ABCPTransportError):
        # Individual capability handlers normally return this themselves.  If
        # one misses it, retain the public failure envelope and its typed
        # replay facts instead of flattening it into a generic exception.
        transport_metadata = _bt()._transport_error_metadata(method, exc)
        receipt_status = transport_metadata.pop("status", None)
        if receipt_status is not None:
            result["transportReceiptStatus"] = receipt_status
        if declared_sensitive_params:
            transport_metadata = _sensitive_transport_metadata(transport_metadata)
        result.update(transport_metadata)
        attach_error_classification(result, method=method)
        result["replayForbidden"] = replay_forbidden(result)
        event = "browser.tool.transport_exception"
    else:
        result["error"] = f"{type(exc).__name__}: {result['error']}"
        result["replayForbidden"] = True
        result["errorClassification"] = {
            "type": "unexpected_tool_exception",
            "exceptionType": type(exc).__name__,
            "method": method,
            "replayForbidden": True,
            "suggested_action": (
                "reobserve_state_before_retrying_or_reporting_tool_exception"
            ),
        }
        event = "browser.tool.exception"

    logger = getattr(agent, "logger", None)
    write = getattr(logger, "write", None)
    if callable(write):
        try:
            trim_for_log = getattr(agent, "_trim_for_log", None)
            log_result = trim_for_log(result) if callable(trim_for_log) else result
            write(event, log_result)
        except Exception:
            # The fallback itself must not fail because observability failed.
            pass
    trace = getattr(agent, "trace", None)
    if isinstance(trace, list):
        trace.append({"type": "tool_exception", "result": copy.deepcopy(result)})
    return result


@lru_cache(maxsize=32)
def _browser_input_schemas_cached(
    capability_methods: Tuple[str, ...], stamp: str = "",
) -> Dict[str, JsonDict]:
    # `stamp` is unused on purpose: it is part of the cache KEY. The workflow
    # tool's schema is derived from the platform contract, which this run's
    # schema bootstrap rewrites after import. Keyed on capability methods
    # alone, this cache would keep handing back a tool schema built against the
    # previous catalog revision for the life of the process.
    del stamp
    return _browser_input_schemas(capability_methods)

def build_browser_tool_dispatcher(agent: Any) -> BrowserToolDispatcher:
    async def dispatch(tool_call: JsonDict, step: int) -> Tuple[JsonDict, bool]:
        lifecycle = lifecycle_for(agent)
        context = LifecycleContext(
            actor="browser_agent",
            step=step,
            metadata={"agent_id": getattr(getattr(agent, "runtime", None), "agent_id", "")},
        )
        prepared_call, prepared_fields, preparation_error = prepare_model_tool_call(
            tool_call
        )
        if preparation_error is not None or prepared_call is None:
            result = tool_argument_error(
                tool_call,
                [SchemaIssue((), "type", preparation_error or "invalid tool call")],
                stage="prepare_arguments",
            )
            _record_tool_argument_rejection(agent, result)
            return result, False
        prepared_call, defaulted_fields = apply_registered_tool_defaults(
            BROWSER_TOOLS,
            prepared_call,
            schema_context=getattr(agent, "capability_methods", set()),
        )
        prepared_fields.extend(defaulted_fields)
        issues = validate_registered_tool_call(
            BROWSER_TOOLS,
            prepared_call,
            schema_context=getattr(agent, "capability_methods", set()),
        )
        if issues:
            result = tool_argument_error(
                prepared_call,
                issues,
                stage="validate_tool_arguments",
                normalized_fields=prepared_fields,
            )
            _record_tool_argument_rejection(agent, result)
            return result, False
        effective_call = prepared_call
        post_call_attempted = False
        try:
            # Middleware is intentionally between the two schema checks.  It
            # may add trusted context, but cannot smuggle an invalid model call
            # into execution by changing the arguments after validation.
            effective_call = lifecycle.tool_pre_call(context, prepared_call)
            effective_call, defaulted_fields = apply_registered_tool_defaults(
                BROWSER_TOOLS,
                effective_call,
                schema_context=getattr(agent, "capability_methods", set()),
            )
            prepared_fields.extend(defaulted_fields)
            issues = validate_registered_tool_call(
                BROWSER_TOOLS,
                effective_call,
                schema_context=getattr(agent, "capability_methods", set()),
            )
            if issues:
                result = tool_argument_error(
                    effective_call,
                    issues,
                    stage="validate_after_before_tool_call",
                    normalized_fields=prepared_fields,
                )
                _record_tool_argument_rejection(agent, result)
                return result, False
            authorization_error = await authorize_tool_call(agent, effective_call)
            if authorization_error is not None:
                if authorization_error.get("status") == "needs_human":
                    agent.diagnostics.local_path_authorization_pending = dict(authorization_error)
                    # Finish this attempt so the spawner wakes the Lead rather
                    # than letting the model retry an unanswered consent prompt.
                    return {
                        **authorization_error,
                        "status": "hitl_required",
                        "answer": (
                            "Local file authorization is required in the task terminal: "
                            f"{authorization_error.get('mode')} {authorization_error.get('path')}. "
                            "No file operation was executed. Resume after explicit user consent; "
                            "browser HITL resume does not grant file permissions."
                        ),
                    }, True
                if authorization_error.get("code") == "external_path_access_denied":
                    return {**authorization_error, "status": "incomplete",
                            "answer": "User denied local file access. The requested tool and remaining calls in this batch were not executed. Do not retry through another tool."}, True
                return authorization_error, False
            result, should_stop = await execute_browser_tool(
                agent, effective_call, step
            )
            post_call_attempted = True
            try:
                result = lifecycle.tool_post_call(context, effective_call, result)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # The tool already ran.  Keep its receipt and terminal signal;
                # after-call middleware is observational and cannot turn a
                # completed action into an uncertain execution.
                _record_tool_after_call_exception(agent, effective_call, exc)
        except asyncio.CancelledError:
            raise
        except ABCPTransportError as exc:
            # A dead transport is a slot lifecycle failure.  It must reach the
            # spawner, which owns client teardown and reconnection, rather than
            # becoming a tool error that invites another browser call.
            if (
                bool(getattr(exc, "connection_fatal", False))
                or bool(
                    getattr(exc, "requires_spawn_acquisition_cooldown", False)
                )
            ):
                raise
            result, should_stop = _tool_exception_result(agent, effective_call, exc), False
        except Exception as exc:
            result, should_stop = _tool_exception_result(agent, effective_call, exc), False
        if not post_call_attempted:
            try:
                # A handler exception is still a ToolResultMessage. Let the
                # after hook observe that receipt, while avoiding a second
                # invocation if the hook itself was the source of the error.
                result = lifecycle.tool_post_call(context, effective_call, result)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # The fallback result still describes the earlier failure.  A
                # second exception in an observational hook must not replace
                # it with a misleading execution failure.
                _record_tool_after_call_exception(agent, effective_call, exc)
        if _contains_truncated_receipt(result):
            result.setdefault(
                "truncationNotice",
                (
                    "This receipt is truncated. It proves only the returned"
                    " matches were observed; it does not prove an unreturned"
                    " item or control is absent. Query the fuller observation"
                    " surface separately when absence matters."
                ),
            )
        result = await _bt()._maybe_reality_check(agent, effective_call, result, step)
        return result, should_stop

    return dispatch


def _record_tool_argument_rejection(agent: Any, result: JsonDict) -> None:
    logger = getattr(agent, "logger", None)
    write = getattr(logger, "write", None)
    if callable(write):
        write("tool.arguments.rejected", result)
    trace = getattr(agent, "trace", None)
    if isinstance(trace, list):
        trace.append({"type": "tool_arguments_rejected", "result": copy.deepcopy(result)})


def _record_tool_after_call_exception(
    agent: Any,
    tool_call: Any,
    exc: Exception,
) -> None:
    """Record middleware failure without replacing an existing tool receipt."""
    name = str(tool_call.get("name") or "unknown") if isinstance(tool_call, dict) else "unknown"
    receipt = {"tool": name, "exceptionType": type(exc).__name__}
    logger = getattr(agent, "logger", None)
    write = getattr(logger, "write", None)
    if callable(write):
        try:
            write("browser.tool.after_call_exception", receipt)
        except Exception:
            pass
    trace = getattr(agent, "trace", None)
    if isinstance(trace, list):
        trace.append({"type": "tool_after_call_exception", "result": receipt})

def _contains_truncated_receipt(value: Any) -> bool:
    if isinstance(value, dict):
        if value.get("truncated") is True:
            return True
        return any(_contains_truncated_receipt(item) for item in value.values())
    if isinstance(value, list):
        return any(_contains_truncated_receipt(item) for item in value)
    return False

async def execute_browser_tool(agent: Any, tool_call: JsonDict, step: int) -> Tuple[JsonDict, bool]:
    """Execute one worker tool and attach non-blocking progress observations.

    ProgressAccountant still computes exactly the same arithmetic facts, but
    production execution no longer treats its interpretation as permission to
    run the tool.  The facts are attached to both the model result and the
    corresponding trace receipt so replay/audit sees the same evidence.
    """
    agent._pending_progress_observations = []
    agent._pending_loop_observations = []
    trace_start = len(getattr(agent, "trace", []) or [])
    # Hold off pollers during ordinary tools, but not during their consumer's
    # wait: otherwise a blocking await_node_change prevents the very reads
    # it waits for.
    # A concurrent ordinary tool still owns its own depth contribution.
    blocks_observation = str(tool_call.get("name") or "") != "await_node_change"
    if blocks_observation:
        agent._observation_tool_depth = int(getattr(agent, "_observation_tool_depth", 0) or 0) + 1
    try:
        result, should_stop = await _bt()._execute_browser_tool_impl(agent, tool_call, step)
    finally:
        if blocks_observation:
            agent._observation_tool_depth -= 1
    _bt()._attach_watch_events(agent, result, str(tool_call.get("name") or ""))
    if should_stop:
        _bt()._close_agent_watches(agent)
    observations = list(
        getattr(agent, "_pending_progress_observations", None) or []
    )
    loop_observations = list(
        getattr(agent, "_pending_loop_observations", None) or []
    )
    if observations and isinstance(result, dict):
        result["progressObservations"] = observations
        result["progressObservationNotice"] = (
            "These are attributed arithmetic observations from the progress"
            " accountant, recorded before dispatch. They did not decide"
            " whether the call runs, and they do not report whether it ran:"
            " read the receipt beside them for the execution outcome."
        )
        # Capability calls clean/offload their model result and then append a
        # separate trace copy.  Mutating ``result`` above therefore does not
        # update that receipt.  Attach the same facts to the last real action
        # emitted by this invocation so transcript replay sees exactly what the
        # model saw; the standalone observation entry keeps provenance.
        trace = getattr(agent, "trace", None)
        if isinstance(trace, list):
            for entry in reversed(trace[trace_start:]):
                if not isinstance(entry, dict):
                    continue
                if entry.get("type") == "progress_observation":
                    continue
                trace_result = entry.get("result")
                if not isinstance(trace_result, dict):
                    continue
                trace_result["progressObservations"] = observations
                trace_result["progressObservationNotice"] = result[
                    "progressObservationNotice"
                ]
                break
    if loop_observations and isinstance(result, dict):
        result["loopObservations"] = loop_observations
        result["loopObservationNotice"] = (
            "These are attributed duplicate-call facts, recorded before"
            " dispatch. They did not decide whether the call runs, and they do"
            " not report whether it ran: interpret repetition using the"
            " receipt beside them and the current goal."
        )
        trace = getattr(agent, "trace", None)
        if isinstance(trace, list):
            for entry in reversed(trace[trace_start:]):
                if not isinstance(entry, dict):
                    continue
                if entry.get("type") in {"progress_observation", "loop_observation"}:
                    continue
                trace_result = entry.get("result")
                if not isinstance(trace_result, dict):
                    continue
                trace_result["loopObservations"] = loop_observations
                trace_result["loopObservationNotice"] = result[
                    "loopObservationNotice"
                ]
                break
    return result, should_stop

async def _execute_browser_tool_impl(
    agent: Any,
    tool_call: JsonDict,
    step: int,
) -> Tuple[JsonDict, bool]:
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

    _bt()._observe_unrecorded_extraction_before(agent, name, tool_input, step)

    # Loop guard: short-circuit if the model is hammering the same tool with
    # the same args. final_answer is exempted above so a deliberate retry of
    # the terminal call doesn't trip the guard.
    if action is None or action.loop_guard:
        short_circuit = _bt().check_tool_call_loop(
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
        return await _bt()._execute_browser_capability_tool(agent, name, tool_input, step)

    if action is None:
        if name in getattr(agent, "capability_methods", set()):
            return await _bt()._execute_browser_capability_tool(agent, name, tool_input, step)
        result = {
            "error": f"Unknown harness tool: {name}",
            **_allowed_tool_hint(agent),
        }
        agent.logger.write("tool.error", result)
        agent.trace.append({"type": "tool_error", "result": result})
        return result, False

    fleet_guard, _fleet_receipt = _bt()._apply_fleet_binding(
        agent, name, tool_input
    )
    routing_guard = fleet_guard or _bt()._check_page_binding(
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
        contract_result = _bt()._check_worker_contract(agent, name)
        if contract_result is not None:
            agent.trace.append({"type": "contract_violation", "result": contract_result})
            return contract_result, False

    if action.progress_check:
        _bt()._observe_progress_before(agent, name, tool_input, step)

    result = await action.handler(ctx)
    if action.trace_type:
        _bt()._observe_progress_after(agent, name, result)
        trace_entry: JsonDict = {"type": action.trace_type, "result": result}
        if name == "collect_items":
            from harness.planning.fast_path import trace_params_for_fast_path

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
        " DOM.getAXTree node ids and query records, worker_contract,"
        " or cited record_extraction artifacts."
        " For Runtime.evaluate data capture, runtime_policy.record_name persists"
        " returned rows directly and returns recordExtraction.savedPath."
    ),
    input_schema=_browser_schema_for("browser_call"),
    strict=False,
    trace_type="",
)
async def _browser_call(ctx: ToolContext) -> JsonDict:
    result, _should_stop = await _bt()._execute_browser_capability_tool(
        ctx.agent,
        "browser_call",
        ctx.tool_input,
        ctx.step,
    )
    return result

def _workflow_definition_outcome(ctx: ToolContext, receipt: JsonDict, result: JsonDict) -> None:
    """A stored definition is not evidence of successful execution.

    Capability results may expose a platform response envelope rather than a
    top-level status. Project the actual Workflow receipt so reuse sees
    ``succeeded``/``failed`` and the workflow id instead of ``unknown``.
    """
    execution = result.get("workflowExecution") if isinstance(result, dict) else None
    execution = execution if isinstance(execution, dict) else {}
    response = result.get("response") if isinstance(result, dict) else None
    response = response if isinstance(response, dict) else {}
    data = response.get("data") if isinstance(response.get("data"), dict) else {}
    details = result.get("rpcData") if isinstance(result, dict) else None
    details = details if isinstance(details, dict) else {}
    detail_data = details.get("details") if isinstance(details.get("details"), dict) else details
    status = (
        execution.get("status")
        or result.get("status")
        or data.get("status")
        or detail_data.get("status")
        or ("failed" if result.get("error") else "unknown")
    )
    executed = execution.get("tool_was_executed")
    if executed is None:
        executed = result.get("tool_was_executed")
    if executed is None:
        executed = bool(data.get("workflowId") or detail_data.get("workflowId"))
    outcome = {
        "status": status,
        "workflowId": execution.get("workflowId") or data.get("workflowId") or detail_data.get("workflowId"),
        "tool_was_executed": executed,
        "issues": result.get("issues", []),
        "errors": result.get("errors", []),
        "failedStepPath": execution.get("failedStepPath") or result.get("failedStepPath") or detail_data.get("failedStepPath"),
        "failedErrorCode": execution.get("failedErrorCode") or result.get("failedErrorCode") or detail_data.get("failedActionCode"),
        "next_instruction": result.get("next_instruction") or response.get("suggested_prompt"),
    }
    receipt["lastAttempt"] = outcome
    receipt["reuseGuidance"] = (
        "Stored does not mean validated or succeeded. Correct reported issues/errors "
        "before retrying invalid parameters; for page-state gates synchronize the "
        "page first. Do not replay completed side effects."
    )
    ctx.agent.trace.append({
        "type": "workflow_definition_outcome",
        "result": {"workflowDefinition": dict(receipt)},
    })


def _record_workflow_definition_before_dispatch(ctx: ToolContext, receipt: JsonDict) -> None:
    """Keep a small recovery reference even when execution raises or is cancelled."""
    record = {
        "type": "workflow_definition_execution",
        "step": ctx.step,
        "toolCallId": ctx.tool_call.get("id"),
        "result": {"workflowDefinition": dict(receipt), "executionOutcome": "unknown"},
    }
    ctx.agent.logger.write("workflow.definition.prepared", record)
    trace = getattr(ctx.agent, "trace", None)
    if not isinstance(trace, list):
        trace = []
        ctx.agent.trace = trace
    trace.append(record)


@BROWSER_TOOLS.register(
    name="execute_published_skill_workflow",
    description=(
        "Execute one immutable Workflow JSON file from the exact Skill version"
        " explicitly selected by the user for this task. The live page and"
        " variables are supplied separately; the file is never reconstructed"
        " from model text. Returns actual WebCross and Harness receipts."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Relative Workflow JSON path inside selected Skill."},
            "pageId": {"type": "string"},
            "fleetId": {"type": "string"},
            "variables": {"type": "object", "additionalProperties": True},
        },
        "required": ["path", "pageId", "fleetId", "variables"],
        "additionalProperties": False,
    },
    contract_check=True,
    trace_type="skill_workflow_invocation",
)
async def _browser_execute_published_skill_workflow(ctx: ToolContext) -> JsonDict:
    if not workflow_execution_enabled(ctx.agent):
        return workflow_execution_disabled_result(source="execute_published_skill_workflow")
    config = getattr(getattr(ctx.agent, "runtime", None), "harness", None)
    skill_id = str(getattr(config, "forced_skill_id", "") or "").strip()
    skill_hash = str(getattr(config, "forced_skill_hash", "") or "").strip()
    if not skill_id or not skill_hash:
        return {"status": "permission_denied", "tool_was_executed": False,
                "error": "此任务没有用户明确选择且绑定版本的 Skill"}
    from harness.skill_builder.catalog import SkillCatalog
    from harness.storage.factory import resolve_sqlite_path
    from harness.workflow.workflow_projection import workflow_execution_facts

    worktree = str(getattr(config, "worktree_dir", "worktree"))
    catalog = SkillCatalog(
        Path(__file__).resolve().parents[3] / "skills",
        resolve_sqlite_path(getattr(config, "storage_sqlite_path", "harness.db"), worktree),
    )
    invocation = None
    invocation_open = False
    try:
        snapshot = catalog.version_path(skill_id, skill_hash)
        if snapshot is None:
            return {"status": "skill_version_unavailable", "tool_was_executed": False,
                    "skill": skill_id, "hash": skill_hash}
        relative = Path(str(ctx.tool_input.get("path") or ""))
        if (relative.is_absolute() or not relative.parts or ".." in relative.parts
                or any(part.startswith(".") for part in relative.parts)
                or relative.suffix != ".json"):
            return {"status": "invalid_skill_path", "tool_was_executed": False}
        path = snapshot / relative
        if not path.is_file() or path.is_symlink() or not path.resolve().is_relative_to(snapshot.resolve()):
            return {"status": "skill_workflow_missing", "tool_was_executed": False,
                    "path": relative.as_posix()}
        workflow = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(workflow, dict):
            return {"status": "invalid_skill_workflow", "tool_was_executed": False}
        variables = ctx.tool_input.get("variables")
        if not isinstance(variables, dict):
            return {"status": "invalid_variables", "tool_was_executed": False}
        workflow = dict(workflow)
        workflow["initialVariables"] = {**(workflow.get("initialVariables") or {}), **variables}
        page_id = str(ctx.tool_input.get("pageId") or "")
        fleet_id = str(ctx.tool_input.get("fleetId") or "")
        input_hash = hashlib.sha256(json.dumps(
            {"workflow": relative.as_posix(), "variables": variables,
             "pageId": page_id, "fleetId": fleet_id},
            sort_keys=True, default=str,
        ).encode()).hexdigest()
        invocation = catalog.begin_invocation(
            task_id=ctx.agent.logger.task_id, run_id=ctx.agent.logger.run_id,
            skill_id=skill_id, content_hash=skill_hash, input_hash=input_hash,
        )
        invocation_open = True
        ctx.agent.logger.write("skill.invocation.started", {
            **invocation, "workflowPath": relative.as_posix(), "pageId": page_id,
            "fleetId": fleet_id, "inputHash": input_hash,
        })
        result, _should_stop = await _bt()._execute_browser_capability_tool(
            ctx.agent, "browser_call", {
                "method": "Workflow.execute",
                "params": {"workflow": workflow,
                           "binding": {"pageId": page_id, "fleetId": fleet_id}},
                "reason": f"Execute selected Skill {skill_id} Workflow {relative.as_posix()}",
            }, ctx.step, published_skill_workflow=True,
        )
        facts = workflow_execution_facts(result)
        status = str(facts.get("status") or "unknown")
        ctx.agent.logger.write("skill.invocation.result", {
            "invocationId": invocation["invocationId"], "facts": facts,
        })
        catalog.finish_invocation(invocation["invocationId"], status=status,
                                  result_ref="skill.invocation.result")
        invocation_open = False
        result["skillInvocation"] = {**invocation, "workflowPath": relative.as_posix(),
                                     "status": status}
        return result
    except BaseException:
        if invocation is not None and invocation_open:
            try:
                catalog.finish_invocation(invocation["invocationId"], status="unknown")
            except Exception:
                pass  # Preserve the original execution or persistence failure.
        raise
    finally:
        catalog.close()


@BROWSER_TOOLS.register(
    name="execute_browser_workflow",
    description=(
        "Execute a browser-only ABCP workflow after recursive harness validation."
        " A complete definition is saved before dispatch and its receipt returns"
        " definitionRef/definitionHash; later calls may reuse that pair with"
        " optional local operations instead of copying the full steps. Use it"
        " when the upcoming actions and target-selection rules are decided;"
        " target ids may be discovered inside the segment. A Workflow"
        " segment can read a leased page observation: after DOM.getAXTree, use"
        " the complete `$cache.observation` or `$last` reference with transform"
        " to search artifact text and extract a current id."
        " `$cache.observation.artifact.path` is metadata only. It cannot call"
        " Harness-local tools or Runtime.evaluate. After navigation, read"
        " terminal load events, wait only if needed, handle failure/timeout,"
        " and synchronize Page.getState before DOM/Input. End the segment at the"
        " next point that needs model judgment, a screenshot, a Harness-only"
        " tool, an expired artifact, or recovery from a failed workflow."
    ),
    input_schema=_browser_schema_for("execute_browser_workflow"),
    contract_check=True,
    trace_type="workflow_definition_execution",
)
async def _browser_execute_browser_workflow(ctx: ToolContext) -> JsonDict:
    if not workflow_execution_enabled(ctx.agent):
        return workflow_execution_disabled_result(source="execute_browser_workflow")
    from harness.workflow.workflow_definitions import save_workflow_definition

    workflow_definition = {
        "description": str(
            ctx.tool_input.get("description") or "Temporary browser workflow"
        ),
        "variables": dict(ctx.tool_input.get("variables") or {}),
        "steps": list(ctx.tool_input.get("steps") or []),
    }
    try:
        definition_receipt = save_workflow_definition(
            ctx.agent.logger, workflow_definition,
        )
    except Exception as exc:
        return {
            "status": "workflow_definition_store_failed",
            "tool_was_executed": False,
            "error": str(exc)[:500],
        }
    definition_receipt.update({"reused": False, "patchBytes": 0})
    params = {
        "description": str(
            workflow_definition.get("description") or "Temporary browser workflow"
        ),
        "variables": dict(workflow_definition.get("variables") or {}),
        "steps": list(workflow_definition.get("steps") or []),
        "timeout": int(ctx.tool_input.get("timeout") or 600000),
    }
    page_id = str(ctx.tool_input.get("pageId") or "").strip()
    fleet_id = str(ctx.tool_input.get("fleetId") or "").strip()
    if page_id:
        params["pageId"] = page_id
    if fleet_id:
        params["fleetId"] = fleet_id
    _record_workflow_definition_before_dispatch(ctx, definition_receipt)
    result, _should_stop = await _bt()._execute_browser_capability_tool(
        ctx.agent,
        "browser_call",
        {
            "method": "Workflow.execute",
            "params": params,
            "reason": params["description"],
        },
        ctx.step,
    )
    if isinstance(result, dict):
        _workflow_definition_outcome(ctx, definition_receipt, result)
        result["workflowDefinition"] = definition_receipt
        ctx.agent.logger.write("workflow.definition.used", {
            **definition_receipt,
            "workflowStatus": definition_receipt["lastAttempt"]["status"],
        })
    return result


@BROWSER_TOOLS.register(
    name="execute_saved_browser_workflow",
    description=(
        "Execute a task-scoped immutable Workflow definition returned by"
        " execute_browser_workflow. Supply its exact definitionRef/hash and"
        " optional add/set/remove operations. The harness rebuilds the ordinary"
        " Workflow.execute request and applies the same validation, Fleet/page,"
        " authorization, and execution gates."
    ),
    input_schema=_browser_schema_for("execute_saved_browser_workflow"),
    contract_check=True,
    trace_type="workflow_definition_execution",
)
async def _browser_execute_saved_browser_workflow(ctx: ToolContext) -> JsonDict:
    if not workflow_execution_enabled(ctx.agent):
        return workflow_execution_disabled_result(
            source="execute_saved_browser_workflow",
        )
    from harness.workflow.workflow_definitions import (
        apply_workflow_definition_patch,
        load_workflow_definition,
        save_workflow_definition,
    )

    definition_ref = str(ctx.tool_input.get("definitionRef") or "").strip()
    definition_hash = str(ctx.tool_input.get("definitionHash") or "").strip()
    operations = ctx.tool_input.get("operations") or []
    previous_attempt = None
    for entry in reversed(getattr(ctx.agent, "trace", [])):
        previous = (entry.get("result") or {}).get("workflowDefinition") or {}
        if previous.get("definitionHash") == definition_hash and previous.get("lastAttempt"):
            previous_attempt = previous["lastAttempt"]
            break
    if (previous_attempt and previous_attempt.get("status") == "invalid_params"
            and not operations and not ctx.tool_input.get("variables")):
        return {
            "status": "workflow_definition_correction_required",
            "isError": True,
            "tool_was_executed": False,
            "definitionRef": definition_ref,
            "definitionHash": definition_hash,
            "lastAttempt": previous_attempt,
            "next_instruction": "Correct the reported parameter paths using operations or send a corrected full definition. An unchanged saved definition repeats the failure.",
        }
    loaded, load_error = load_workflow_definition(
        ctx.agent.logger,
        definition_ref=definition_ref,
        expected_hash=definition_hash,
    )
    if load_error is not None or not isinstance(loaded, dict):
        return {
            **(load_error or {"status": "workflow_definition_unavailable"}),
            "tool_was_executed": False,
            "next_instruction": (
                "Use a current definitionRef/hash from this task, or send the"
                " complete Workflow definition again."
            ),
        }
    required_variables = [
        str(value) for value in loaded.pop("_requiredVariableNames", [])
        if str(value).strip()
    ]
    overrides = dict(ctx.tool_input.get("variables") or {})
    missing_variables = [
        name for name in required_variables if name not in overrides
    ]
    if missing_variables:
        return {
            "status": "workflow_definition_sensitive_rebinding_required",
            "tool_was_executed": False,
            "missingVariableNames": missing_variables,
        }
    workflow_definition, patch_errors = apply_workflow_definition_patch(
        loaded, operations,
    )
    if patch_errors or not isinstance(workflow_definition, dict):
        return {
            "status": "workflow_definition_patch_invalid",
            "tool_was_executed": False,
            "errors": patch_errors,
        }
    if operations:
        try:
            definition_receipt = save_workflow_definition(
                ctx.agent.logger,
                workflow_definition,
                required_variable_names=required_variables,
            )
        except Exception as exc:
            return {
                "status": "workflow_definition_store_failed",
                "tool_was_executed": False,
                "error": str(exc)[:500],
            }
    else:
        definition_receipt = {
            "definitionRef": definition_ref,
            "definitionHash": definition_hash,
            "definitionBytes": len(json.dumps(
                workflow_definition, ensure_ascii=False, default=str,
            ).encode("utf-8")),
            "executable": True,
            "requiresSensitiveRebinding": bool(required_variables),
            "requiredVariableNames": required_variables,
        }
    definition_receipt.update({
        "reused": True,
        "patchBytes": len(json.dumps(
            operations, ensure_ascii=False, default=str,
        ).encode("utf-8")),
    })
    variables = dict(workflow_definition.get("variables") or {})
    variables.update(overrides)
    params = {
        "description": str(
            workflow_definition.get("description") or "Saved browser workflow"
        ),
        "variables": variables,
        "steps": list(workflow_definition.get("steps") or []),
        "timeout": int(ctx.tool_input.get("timeout") or 600000),
        "pageId": str(ctx.tool_input.get("pageId") or "").strip(),
        "fleetId": str(ctx.tool_input.get("fleetId") or "").strip(),
    }
    _record_workflow_definition_before_dispatch(ctx, definition_receipt)
    result, _should_stop = await _bt()._execute_browser_capability_tool(
        ctx.agent,
        "browser_call",
        {
            "method": "Workflow.execute",
            "params": params,
            "reason": params["description"],
        },
        ctx.step,
    )
    if isinstance(result, dict):
        if previous_attempt:
            definition_receipt["previousAttempt"] = previous_attempt
        _workflow_definition_outcome(ctx, definition_receipt, result)
        result["workflowDefinition"] = definition_receipt
        ctx.agent.logger.write("workflow.definition.used", {
            **definition_receipt,
            "workflowStatus": definition_receipt["lastAttempt"]["status"],
        })
    return result

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
    result = await _bt()._navigate_verified(ctx.agent, ctx.tool_input, ctx.step)
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
        result = await _bt()._maybe_auto_hitl_for_challenge(
            ctx.agent,
            "navigate_verified",
            {"pageId": ctx.tool_input.get("pageId")},
            result,
            ctx.step,
        )
    # navigate_verified dispatches Page.navigate through its composite path,
    # which bypasses capability.py's normal post-call observer. Feed the
    # verified terminal facts into the same observer as Page.navigate so DOM
    # evidence from the previous document is invalidated and receives the
    # next navigation epoch. The synthetic response is private to this adapter
    # and removed before the model sees the composite receipt.
    if int(result.get("navigateDispatchCount") or 0) > 0:
        observed = dict(result)
        observed["response"] = {"data": {
            "pageId": str(result.get("pageId") or ctx.tool_input.get("pageId") or ""),
            "url": str(result.get("url") or result.get("actualUrl") or ""),
            "title": str(result.get("title") or result.get("actualTitle") or ""),
            "status": str(result.get("pageStatus") or ""),
        }}
        result = _bt()._observe_content_completeness_after(
            ctx.agent,
            "Page.navigate",
            ctx.tool_input,
            observed,
            ctx.step,
        )
        result.pop("response", None)
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
    return await _bt()._dismiss_overlay(ctx.agent, ctx.tool_input, ctx.step)

@BROWSER_TOOLS.register(
    name="collect_items",
    description=(
        "Collect one single-level homogeneous list/card/row collection that"
        " grows through ONE scroll container or ONE load-more control, without"
        " burning a model step per round. Harvests rows every round and dedups"
        " by a stable key, so lazy-loaded and virtualized rows can be retained."
        " On an unknown site, first read DOM.getAXTree (plus a `dom` query when"
        " needed) to identify the"
        " repeated-item selector and the actual scroll container/load-more"
        " control; do not guess them."
        " Use this only when the collection cannot be read from one DOM snapshot;"
        " otherwise enumerate node ids and read them with one batched"
        " DOM.getAXTree `text`/`attributes` query."
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
    _bt()._ensure_content_completeness_tracker(ctx.agent)
    result = await _bt()._collect_items(ctx.agent, ctx.tool_input, ctx.step)
    return _bt()._observe_content_completeness_after(
        ctx.agent,
        "collect_items",
        ctx.tool_input,
        result,
        ctx.step,
    )

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
    return await _bt()._visual_verify(ctx.agent, ctx.tool_input, ctx.step)

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
    status = str(ctx.tool_input.get("status") or "done")
    if (
        bool(getattr(ctx.agent, "standalone_browser_mode", False))
        and status == "partial"
    ):
        checkpoint = {
            "status": "continuing", "answer": answer,
            "continuation": ctx.tool_input.get("continuation"),
        }
        ctx.agent.logger.write("browser.task_checkpoint", checkpoint)
        ctx.agent.trace.append({"type": "browser_task_checkpoint", "result": checkpoint})
        return {
            **checkpoint, "tool_was_executed": False,
            "next_instruction": (
                "Progress was saved. This standalone Browser task is still"
                " active. Re-observe the current page as needed and continue"
                " the original user goal. Use final_answer(status='done')"
                " only after verifying completion, or status='incomplete'"
                " with a concrete blocker when no useful action remains."
            ),
        }
    result = {
        "status": status,
        "answer": answer,
        "artifacts": ctx.agent.artifacts,
    }
    reason = ctx.tool_input.get("reason")
    if isinstance(reason, str) and reason.strip():
        result["reason"] = reason.strip()[:200]
    continuation = ctx.tool_input.get("continuation")
    if isinstance(continuation, dict):
        status = str(result.get("status") or "")
        if status == "done":
            result["continuationRejected"] = {
                "reason": "done_status_cannot_request_continuation",
            }
            setattr(ctx.agent, "continuation_decision", None)
        else:
            normalized = {
                "protocol": "browser-continuation-v1",
                "action": str(continuation.get("action") or ""),
                "reason": str(continuation.get("reason") or "")[:500],
                "remainingObjective": str(
                    continuation.get("remainingObjective") or ""
                )[:2000],
                "evidenceRefs": [
                    str(value)[:1000]
                    for value in (continuation.get("evidenceRefs") or [])[:20]
                    if isinstance(value, str) and value.strip()
                ],
                "workflowRef": (
                    str(continuation.get("workflowRef"))[:1000]
                    if continuation.get("workflowRef") else None
                ),
            }
            result["continuation"] = normalized
            setattr(ctx.agent, "continuation_decision", normalized)
    else:
        setattr(ctx.agent, "continuation_decision", None)
    ctx.agent.logger.write("tool.final_answer", result)
    ctx.agent.trace.append({"type": "final_answer", "result": result})
    return result


@BROWSER_TOOLS.register(
    name="request_step_extension",
    description=(
        "Near the current BrowserAgent step limit, request one bounded"
        " continuation only when you believe the current phase can be"
        " completed within the requested extra steps. List the concrete"
        " remaining actions and the evidence supporting that estimate. The"
        " harness may deny the request because of timing, loop, failure, HITL,"
        " routing, or hard-limit guards. An estimate above the configured"
        " maximum is denied outright and locks this run against any further"
        " request, so report the honest number: when the remaining work does"
        " not fit, skip this tool and spend the remaining steps persisting"
        " rows and stating the remaining range in final_answer. Call this tool"
        " alone and read its receipt before taking another action."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "estimated_steps": {
                "type": "integer",
                "minimum": 1,
                "maximum": 50,
                "description": (
                    "Extra model turns needed to finish this phase — turns,"
                    " not individual actions, since one turn may carry several"
                    " tool calls. The configured harness maximum is"
                    " authoritative and exceeding it is denied, never trimmed"
                    " to fit."
                ),
            },
            "remaining_actions": {
                "type": "array",
                "minItems": 1,
                "maxItems": 8,
                "items": {"type": "string"},
                "description": "Concrete bounded actions still required.",
            },
            "evidence": {
                "type": "string",
                "description": (
                    "Brief current-page or artifact evidence supporting the"
                    " estimate; do not restate the full task."
                ),
            },
        },
        "required": ["estimated_steps", "remaining_actions", "evidence"],
        "additionalProperties": False,
    },
    loop_guard=False,
    trace_type="step_extension_request",
)
async def _browser_request_step_extension(ctx: ToolContext) -> JsonDict:
    return ctx.agent.request_step_extension(ctx.tool_input, step=ctx.step)

@BROWSER_TOOLS.register(
    name="record_extraction",
    description=(
        "Persist and validate structured data already observed in the browser (product URLs/titles, form fields,"
        " list rows, etc.) to an artifact that LeadAgent can reuse."
        " Fields that never went through record_extraction must not appear"
        " in the final_answer's `data`."
        " `name` identifies the dataset; `rows` must be a list[dict] backed by actual observations"
        " and should preserve provenance for critical fields. Inspect the returned"
        " validation status; savedPath alone is not proof the contract passed."
        " When capturing structured rows with Runtime.evaluate, set browser_call's"
        " runtime_policy.record_name instead of regenerating those rows here."
    ),
    input_schema=_browser_schema_for("record_extraction"),
    strict=False,
    trace_type="record_extraction",
)
async def _browser_record_extraction(ctx: ToolContext) -> JsonDict:
    result = _bt()._record_extraction(ctx.agent, ctx.tool_input)
    if _bt()._record_extraction_persisted(result):
        ctx.agent.pending_unrecorded_extraction = None
    return result

@BROWSER_TOOLS.register(
    name="find_in_axtree",
    description=(
        "Search the current DOM.getAXTree snapshot by role/name/text and return"
        " node ids (n_…) with line context. Use this instead of grepping"
        " offloaded page text when locating an element in a large page view;"
        " it also covers the full view saved behind a change list. Matches"
        " include the line's `flags` and its `rect` viewport box (CSS pixels)"
        " when present. `flags` carries target evidence (targetable,"
        " actionable, candidate, ignored), explicit state"
        " (checked/unchecked/mixed, selected/unselected, expanded/collapsed,"
        " disabled, focused, required, invalid, valueRedacted) and layout"
        " (hidden = not rendered, off = out of view, scroll) — never target a"
        " hidden node; flags are sparse, so their ABSENCE proves nothing, and"
        " the page view does not report occlusion. Use `rect` for spatial"
        " reasoning only, not for deriving click coordinates (act on the id)."
        " It is read-only and requires a current DOM.getAXTree snapshot."
    ),
    input_schema=_browser_schema_for("find_in_axtree"),
    contract_check=True,
    trace_type="find_in_axtree",
)
async def _browser_find_in_axtree(ctx: ToolContext) -> JsonDict:
    return _bt()._find_in_axtree(ctx.agent, ctx.tool_input)

@BROWSER_TOOLS.register(
    name="await_node_change",
    description=(
        "Wait for named page content to change, in one call: it registers the"
        " watch, blocks until a change or timeoutSeconds (default 15, max 120),"
        " and closes itself. Target up to 16 nodes by id, or pass a selector"
        " and the harness resolves it, so no preparatory read is needed."
        " scope=node follows the targets themselves (a value, a state, an"
        " accessible name, the node leaving the tree); scope=subtree also"
        " follows their descendants, for a region that grows. USE IT when you"
        " have acted and the result appears later: lazy content after a"
        " scroll, a control that enables after validation, a list that refills"
        " after a filter, a status that settles. DO NOT use it to wait for a"
        " document to load (that is Page.getState and the page events), and do"
        " not use it where the answer is already on the page - read it. It is"
        " the harness reading the page for you: prefer it over a Runtime.evaluate"
        " poll of your own for anything the page view describes, and over"
        " re-reading the page yourself in a loop. status=timeout means nothing"
        " changed in that window, which is not proof that nothing will."
        " background=true instead returns at once for a wait longer than one"
        " call can hold; its changes ride your later tool results and you close"
        " it with close=true and its watchId."
    ),
    input_schema=_browser_schema_for("await_node_change"),
    contract_check=True,
    trace_type="await_node_change",
)
async def _browser_await_node_change(ctx: ToolContext) -> JsonDict:
    return await _bt()._await_node_change(ctx.agent, ctx.tool_input)

@BROWSER_TOOLS.register(
    name="local_fs_search",
    description="Read-only search under an authorized directory; external roots require terminal confirmation. If multiple sibling directories or the full tree are needed, request the common parent explicitly first; a parent READ grant covers descendants, while a child grant covers neither siblings nor the parent. Supports glob, JSONL event-type filtering, and per-hit / total output caps.",
    input_schema=_browser_schema_for("local_fs_search"),
    contract_check=True,
    progress_check=True,
    trace_type="local_fs_search",
)
async def _browser_local_fs_search(ctx: ToolContext) -> JsonDict:
    tool_input = ctx.tool_input
    return local_fs_search(
        ctx.agent.logger,
        agent=ctx.agent,
        path=str(tool_input.get("path") or "."),
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
    description="Read-only line-range read under an authorized task or user directory; external roots require terminal confirmation. Request only the needed child, or explicitly request its common parent first when several sibling paths are required; READ grants are task-scoped and do not cover WRITE.",
    input_schema=_browser_schema_for("local_fs_read"),
    contract_check=True,
    progress_check=True,
    trace_type="local_fs_read",
)
async def _browser_local_fs_read(ctx: ToolContext) -> JsonDict:
    tool_input = ctx.tool_input
    return local_fs_read(
        ctx.agent.logger,
        agent=ctx.agent,
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
    name="update_task_todo",
    description=(
        "Write the standalone Browser task's full Markdown checklist to its"
        " task scratchpad. Replace the whole checklist when progress changes;"
        " this note is not a user deliverable."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "content": {"type": "string", "minLength": 1},
            "ready_for_final_review": {"type": "boolean", "default": False,
                "description": "Set true when the next step is final_answer. Saves this update for that final review; it does not approve completion."},
        },
        "required": ["content"], "additionalProperties": False,
    },
    trace_type="browser_todo_update",
)
async def _browser_update_task_todo(ctx: ToolContext) -> JsonDict:
    from harness.agents.browser.review.task import write_todo
    result = write_todo(ctx.agent, ctx.tool_input.get("content"))
    if result.get("status") == "done":
        result["readyForFinalReview"] = ctx.tool_input.get("ready_for_final_review") is True
    return result


@BROWSER_TOOLS.register(
    name="request_goal_review",
    description=(
        "Ask the independent reviewer to inspect a concrete concern about the"
        " standalone Browser task. This does not modify the page or the goal."
    ),
    input_schema={
        "type": "object",
        "properties": {"question": {"type": "string", "minLength": 1}},
        "required": ["question"], "additionalProperties": False,
    },
    loop_guard=False,
    trace_type="goal_review_request",
)
async def _browser_request_goal_review(ctx: ToolContext) -> JsonDict:
    if not getattr(ctx.agent, "standalone_browser_mode", False):
        return {"status": "rejected", "reason": "standalone_browser_only",
                "tool_was_executed": False}
    return {"status": "requested", "question": ctx.tool_input["question"][:2000],
            "tool_was_executed": True}


@BROWSER_TOOLS.register(
    name="local_fs_batch",
    description=(
        "Batch bounded local file operations: list, create directories, write UTF-8 text/JSON,"
        " stat/hash files, or copy files while preserving their source. External material and"
        " delivery roots require terminal approval. Request the common parent explicitly when"
        " multiple sibling paths are needed; same-mode parent grants cover descendants, while"
        " child grants do not cover siblings or the parent. READ and WRITE approvals are separate;"
        " protected application/source paths are denied."
        " It cannot delete/move files, execute code, or modify protected Harness control files."
    ),
    input_schema=_browser_schema_for("local_fs_batch"),
    contract_check=True,
    progress_check=True,
    trace_type="local_fs_batch",
)
async def _browser_local_fs_batch(ctx: ToolContext) -> JsonDict:
    return local_fs_batch(ctx.agent, ctx.tool_input.get("operations"))


@BROWSER_TOOLS.register(
    name="read_harness_guide",
    description=(
        "Read a paged, versioned Harness operating guide listed in "
        "<available_harness_guides>. Use it when its topic is useful for "
        "reasoning about a complex tool receipt or recovery path."
    ),
    input_schema=_browser_schema_for("read_harness_guide"),
    trace_type="read_harness_guide",
)
async def _browser_read_harness_guide(ctx: ToolContext) -> JsonDict:
    return read_harness_guide(
        guide_id=str(ctx.tool_input.get("guide_id") or ""),
        audience="browser",
        line_offset=optional_int(ctx.tool_input.get("line_offset"), 0) or 0,
        line_limit=optional_int(ctx.tool_input.get("line_limit"), 200) or 200,
    )


@BROWSER_TOOLS.register(
    name="search_harness_guides",
    description=(
        "Find candidate Harness operating guides by an error/reason code from "
        "a receipt, a method name, or a phrase in any language. Returns ids and "
        "why each matched; read one with read_harness_guide when it helps."
    ),
    input_schema=_browser_schema_for("search_harness_guides"),
    trace_type="search_harness_guides",
)
async def _browser_search_harness_guides(ctx: ToolContext) -> JsonDict:
    return search_harness_guides(
        query=str(ctx.tool_input.get("query") or ""),
        audience="browser",
        limit=optional_int(ctx.tool_input.get("limit"), 5) or 5,
    )

def build_browser_agent_tool_specs(
    capability_methods: Set[str],
    *,
    workflow_enabled: bool = False,
    selected_skill_available: bool = False,
    step_extension_enabled: bool = False,
    multimodal_enabled: bool = False,
    standalone_review_enabled: bool = False,
) -> List[JsonDict]:
    # A live capability does not authorize Harness execution by itself. Both
    # the control-plane master switch and the ABCP capability must be present.
    workflow_visible = bool(
        workflow_enabled and "Workflow.execute" in capability_methods
    )
    return [
        spec
        for spec in BROWSER_TOOLS.tool_specs(capability_methods)
        if (
            step_extension_enabled
            or spec.get("name") != "request_step_extension"
        )
        and (standalone_review_enabled or spec.get("name") not in {
            "request_goal_review", "update_task_todo",
        })
        and (
            workflow_visible
            or spec.get("name") not in {
                "execute_published_skill_workflow",
                "execute_browser_workflow",
                "execute_saved_browser_workflow",
            }
        )
        and (selected_skill_available or spec.get("name") != "execute_published_skill_workflow")
        # A multimodal BrowserAgent receives a bounded Page.screenshot image
        # attachment in the very next model request. Do not offer the old
        # second-model visual tool alongside it: that adds a network/model
        # round trip without adding a distinct observation surface.
        and (
            not multimodal_enabled
            or spec.get("name") != "visual_verify"
        )
    ]
