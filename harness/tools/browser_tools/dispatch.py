"""
harness.tools.browser_tools.dispatch - Tool registry, dispatcher and model-facing tool handlers.
"""

import copy
import re
import uuid
from functools import lru_cache
from typing import Any
from typing import Awaitable
from typing import Callable
from typing import Dict
from typing import List
from typing import Optional
from typing import Set
from typing import Tuple
from harness.lifecycle import LifecycleContext
from harness.lifecycle import lifecycle_for
from harness.local_fs import local_fs_read
from harness.local_fs import local_fs_search
from harness.observation.page_lifecycle import PageLifecycleTracker
from harness.pacing import wait_between_rows
from harness.observation.render_recovery import build_render_recovery_runner
from harness.runtime_evaluation import RuntimeEvaluationService
from harness.tool_policy import hidden_harness_tools_for_task_type
from harness.tools.registry import ToolContext
from harness.tools.registry import ToolRegistry
from harness.utils import JsonDict
from harness.utils import optional_int
from harness.workflow_runtime import workflow_execution_disabled_result
from harness.workflow_runtime import workflow_execution_enabled
from .schemas import _browser_input_schemas
from .composites.fill_field_verified import _fill_field_verified

def _bt():
    import harness.tools.browser_tools as bt

    return bt

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
    elif method == "DOM.getAXTree" and not _bt()._invoke_result_failed({"response": response}):
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
    result, should_stop = await _bt()._execute_browser_tool_impl(agent, tool_call, step)
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
    result, _should_stop = await _bt()._execute_browser_capability_tool(
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
    result = _bt()._record_extraction(ctx.agent, ctx.tool_input)
    if _bt()._record_extraction_persisted(result):
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
    return _bt()._find_in_axtree(ctx.agent, ctx.tool_input)

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
