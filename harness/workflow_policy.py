"""Recursive harness policy for ABCP Workflow.execute."""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

from harness.tool_policy import disabled_reason_for_method
from harness.screenshot_policy import normalize_screenshot_output_params
from harness.workflow_schema_source import workflow_contract


JsonDict = Dict[str, Any]
# Every name here must be an event the platform actually publishes. This set
# used to admit `Hitl.humanInput` and `Hitl.resumeEvent` — neither exists in the
# event catalog — while the real `Hitl.resumed` was reachable only through an
# `allow_legacy_listen_events` back door, so the contract was exactly inverted.
#
# Verified 2026-09-11 against a live `System.listEvents` (eventCatalogRevision
# event:9e1e48fc): the catalog advertises 26 agent-visible events. Being in the
# catalog is NOT proof the deployment emits it, so this set is narrower.
#
# Deliberately NOT waitable:
#   - Fleet.ready / Fleet.stopped: fleet lifecycle, never the result of a page
#     action inside a workflow, so waiting on one can only burn the timeout.
#   - Workflow.progress: a workflow waiting on its own progress stream.
#   - DOM.axTreeUpdated: advertised by System.listEvents but NEVER OBSERVED.
#     A live probe ran two full navigations plus DOM.getAXTree reads and saw
#     only Page.startedLoading / navigate / titleUpdated / loaded / open — not
#     one axTreeUpdated. Three workflow shapes (navigate→waitEvent,
#     navigate→readEvents→waitEvent, wheel→readEvents→waitEvent) all timed out
#     empty after 4-6s. Because a waitEvent timeout is NOT a failure, a step
#     waiting on it silently burns its whole timeout (default 30s) and then
#     continues with empty events. It was briefly admitted here on catalog
#     evidence alone; that was wrong.
#
# `Action.started/succeeded/failed` are not in the agent-visible catalog at all
# (control-plane only), so no workflow step can wait for them.
# See docs/workflow-execute-live-contract.md.
LISTENABLE_EVENTS = frozenset({
    "Page.open", "Page.close", "Page.loaded", "Page.startedLoading",
    "Page.loadFailed", "Page.crashed", "Page.recovered", "Page.navigate",
    "Page.titleUpdated", "Page.switchTo", "Page.dialogOpened", "Page.dialogClosed",
    "File.chooserOpened", "File.chooserClosed",
    "File.operationCompleted", "File.operationFailed",
    "Download.waiting", "Download.started", "Download.progressed",
    "Download.stateChanged",
    "Hitl.paused", "Hitl.resumed",
})

# The platform's discriminated union of step types (workflow/src/types/schemas.ts).
# `listen` is NOT one of them: it is a harness-only legacy spelling that the
# dispatcher rejects with -32602. It is still ACCEPTED as input here and
# rewritten to `waitEvent` during normalization, so stored skills keep working.
PLATFORM_EVENT_STEP_TYPE = "waitEvent"
# Only these are rewritten by _normalize_wait_events; `readEvents` is a distinct
# platform step with different semantics and must never be folded into them.
_EVENT_STEP_TYPES = frozenset({"listen", PLATFORM_EVENT_STEP_TYPE})
# Both halves of the event contract can settle a navigation:
#   readEvents  reads what was emitted DURING the preceding Action's window;
#   waitEvent   waits for what comes AFTER it — the engine advances waitCursor
#               to the Action window's end, so waitEvent cannot see the window.
# Which one settles a given page is a property of that page: a slow load emits
# Page.loaded after the navigate Action returns (live probe: waitEvent caught it
# in 527ms on first visit, 3ms on a cached revisit, while readEvents came back
# empty), but an Action that completes after the event has already fired needs
# readEvents. Accept either, and accept them in sequence; do not force a shape.
_SETTLEMENT_STEP_TYPES = frozenset({"readEvents"}) | _EVENT_STEP_TYPES
# The platform's action step accepts only these. There is no retry setting to
# move `retry` to: `errorConfig` appears nowhere in packages/workflow.
_STEP_ON_ERROR_VALUES = frozenset({"stop", "continue"})
# workflow/src/types/schemas.ts workflowStoreStepSchema
_STORE_OPS = frozenset({"set", "merge", "append", "delete"})
# Params the harness used to invent for Workflow.execute. The action schema is
# not strict, so these were accepted, forwarded and dropped in silence — the
# model was told it had a per-step bound and a retry policy, and had neither.
_INVENTED_EXECUTE_PARAMS = frozenset({"stepTimeout", "errorConfig"})


def _contract():
    """The platform contract, re-read whenever this run's bootstrap rewrote it.

    Resolved lazily and never at import: `_bootstrap_schema_cache` refreshes
    `global_schema_cache/schemas` after this module is already loaded, so facts
    frozen at import time would describe the previous catalog revision for the
    rest of the process.
    """
    return workflow_contract()


def event_step_focus(step: Any) -> List[str]:
    """Event names a step waits for, across the legacy and platform spellings.

    Legacy `listen` carries one `event`; platform `waitEvent` carries a `focus`
    array. Reading both here keeps every caller from re-deriving it.
    """
    if not isinstance(step, dict):
        return []
    focus = step.get("focus")
    if isinstance(focus, list):
        return [str(item) for item in focus if str(item or "").strip()]
    event = str(step.get("event") or "").strip()
    return [event] if event else []


def is_event_step(step: Any) -> bool:
    return (
        isinstance(step, dict)
        and str(step.get("type") or "").strip() in _EVENT_STEP_TYPES
    )


def harden_navigation_lifecycle(steps: Any) -> List[JsonDict]:
    """Return copied steps with required perception after settlement events.

    Trace distillation already inserts the settlement listen. This helper is
    intentionally mechanical and idempotent so online autoheal candidates
    satisfy the same execution policy as authored workflows. A successful
    Page.loaded settlement needs state + AX refresh; Page.loadFailed needs only
    Page.getState so the failure can be classified without probing a dead DOM.
    """
    source = steps if isinstance(steps, list) else []
    hardened: List[JsonDict] = []
    for index, raw in enumerate(source):
        if not isinstance(raw, dict):
            continue
        step = dict(raw)
        for key in ("then", "else", "body"):
            if isinstance(step.get(key), list):
                step[key] = harden_navigation_lifecycle(step[key])
        hardened.append(step)
        if str(step.get("type") or "").strip() not in _SETTLEMENT_STEP_TYPES:
            continue
        focus = event_step_focus(step)
        event = focus[0] if len(focus) == 1 else ""
        next_step = source[index + 1] if index + 1 < len(source) else None
        if event == "Page.loadFailed":
            if not (
                isinstance(next_step, dict)
                and next_step.get("action") == "Page.getState"
            ):
                hardened.append({
                    "action": "Page.getState",
                    "purpose": (
                        "Classify state after distilled navigation load failure"
                    ),
                })
            continue
        if event != "Page.loaded":
            continue
        next_next = source[index + 2] if index + 2 < len(source) else None
        if not (
            isinstance(next_step, dict)
            and next_step.get("action") == "Page.getState"
            and isinstance(next_next, dict)
            and next_next.get("action") == "DOM.getAXTree"
        ):
            hardened.extend([
                {
                    "action": "Page.getState",
                    "purpose": "Synchronize state after distilled navigation settlement",
                },
                {
                    "action": "DOM.getAXTree",
                    "purpose": "Refresh DOM identity after distilled navigation",
                },
            ])
    return hardened


def validate_workflow_params(
    params: Any,
    *,
    capability_methods: Optional[Iterable[str]],
    task_type: str,
    allow_runtime: bool = False,
    enforce_lifecycle: bool = True,
    max_steps: int = 100,
    max_loop_iterations: int = 50,
) -> Tuple[Optional[JsonDict], Optional[JsonDict]]:
    if not isinstance(params, dict):
        return None, _error(["Workflow params must be an object."])
    steps = params.get("steps")
    if not isinstance(steps, list) or not steps:
        return None, _error(["Workflow steps must be a non-empty array."])
    errors: List[str] = []
    known: Set[str] = {
        str(item) for item in (capability_methods or []) if str(item).strip()
    }
    timeout = _number(params.get("timeout"), 600000)
    if timeout < 1000 or timeout > 600000:
        errors.append("timeout must be between 1000 and 600000 ms")

    count = [0]
    _validate_sequence(
        steps,
        path="steps",
        errors=errors,
        known=known,
        task_type=task_type,
        allow_runtime=allow_runtime,
        enforce_lifecycle=enforce_lifecycle,
        count=count,
        max_loop_iterations=max_loop_iterations,
    )
    if count[0] > max_steps:
        errors.append(f"workflow contains {count[0]} steps; maximum is {max_steps}")
    if errors:
        return None, _error(errors)
    normalized = dict(params)
    normalized["steps"] = _normalize_wait_events(
        _normalize_screenshot_outputs(steps)
    )
    normalized["timeout"] = int(timeout)
    # Workflow.execute declares only description/steps/variables/pageId/
    # fleetId/timeout and its action schema is not strict, so anything else
    # travels and is dropped without a word. `stepTimeout` was such a field:
    # a live probe (2026-09-11) sent stepTimeout=1000 against a step that ran
    # 5005 ms uninterrupted. Drop the invented params here rather than let a
    # stored skill keep believing they bound anything.
    for phantom in _contract().phantom_execute_params(_INVENTED_EXECUTE_PARAMS):
        normalized.pop(phantom, None)
    return normalized, None


def _normalize_screenshot_outputs(steps: List[Any]) -> List[Any]:
    """Return copied workflow steps with path-only screenshot output.

    Workflow.execute runs child actions inside ABCP, so ordinary browser-call
    interception cannot rewrite a nested Page.screenshot before transport.  A
    full-page base64 result can exceed the WebSocket frame limit before the
    harness gets a chance to offload it.  Normalize recursively at workflow
    admission and let ABCP choose the output path.
    """
    normalized: List[Any] = []
    for raw in steps:
        if not isinstance(raw, dict):
            normalized.append(raw)
            continue
        step = dict(raw)
        for branch in ("then", "else", "body"):
            nested = step.get(branch)
            if isinstance(nested, list):
                step[branch] = _normalize_screenshot_outputs(nested)
        if str(step.get("action") or "").strip() == "Page.screenshot":
            raw_params = step.get("params")
            action_params, _receipt = normalize_screenshot_output_params(
                "Page.screenshot",
                raw_params,
            )
            step["params"] = action_params
        normalized.append(step)
    return normalized


def _normalize_wait_events(steps: List[Any]) -> List[Any]:
    """Return copied steps with every event wait in the platform's spelling.

    The harness has always written `{"type": "listen", "event": "Page.loaded"}`,
    but the dispatcher's step union has no `listen` member — it rejects the whole
    workflow with -32602. Live probe 2026-09-11 also showed that unknown FIELDS
    are silently ignored rather than rejected, so a half-translated step
    (`waitEvent` still carrying `event`) waits for *any* event instead of
    failing loudly. Both halves are therefore rewritten here, at the single
    point every workflow passes through before transport.

    `onTimeout` has no platform counterpart: a `waitEvent` that times out
    returns `timedOut: true` and the workflow continues, which is exactly the
    legacy `onTimeout: "continue"` behavior. A legacy `"stop"` cannot be
    honored, so it is dropped rather than silently ignored downstream — callers
    that need a hard stop must assert on the extracted events in a later step.
    """
    normalized: List[Any] = []
    for raw in steps:
        if not isinstance(raw, dict):
            normalized.append(raw)
            continue
        step = dict(raw)
        for branch in ("then", "else", "body"):
            nested = step.get(branch)
            if isinstance(nested, list):
                step[branch] = _normalize_wait_events(nested)
        if is_event_step(step):
            focus = event_step_focus(step)
            step["type"] = PLATFORM_EVENT_STEP_TYPE
            step.pop("event", None)
            step.pop("onTimeout", None)
            step.pop("filter", None)
            if focus:
                step["focus"] = focus
        normalized.append(step)
    return normalized


def _validate_sequence(
    steps: List[Any],
    *,
    path: str,
    errors: List[str],
    known: Set[str],
    task_type: str,
    allow_runtime: bool,
    enforce_lifecycle: bool,
    count: List[int],
    max_loop_iterations: int,
) -> None:
    obligation = ""
    for index, raw in enumerate(steps):
        step_path = f"{path}[{index}]"
        if not isinstance(raw, dict):
            errors.append(f"{step_path} must be an object")
            continue
        count[0] += 1
        action = str(raw.get("action") or "").strip()
        step_type = str(raw.get("type") or ("action" if action else "")).strip()

        if enforce_lifecycle and obligation:
            if obligation == "settlement" and not (
                step_type in _SETTLEMENT_STEP_TYPES
                and set(event_step_focus(raw)) & {"Page.loaded", "Page.loadFailed"}
            ):
                errors.append(
                    f"{step_path} must settle navigation with a waitEvent or"
                    " readEvents step focused on Page.loaded or Page.loadFailed"
                )
            elif obligation == "state" and action != "Page.getState":
                errors.append(f"{step_path} must call Page.getState after settlement/recovery")
            elif obligation == "axtree" and action != "DOM.getAXTree":
                errors.append(f"{step_path} must call DOM.getAXTree after Page.getState")
            elif obligation == "state_only" and action != "Page.getState":
                errors.append(
                    f"{step_path} must call Page.getState after load failure"
                    " or dialog/chooser close"
                )

        on_error = str(raw.get("onError") or "").strip()
        if on_error and on_error not in _STEP_ON_ERROR_VALUES:
            # The platform's step schema is strict, so a step carrying "retry"
            # rejects the whole workflow with -32602. There is no retry config
            # to move it to either: `errorConfig` appears nowhere in
            # abcp-platform/packages/workflow. Retrying is the model's job, in
            # a follow-up segment that re-observes first.
            errors.append(
                f"{step_path}.onError must be 'stop' or 'continue'"
                f" (got {on_error!r}; the platform has no retry config —"
                " re-observe and submit a new segment instead)"
            )

        if action:
            if action == "Workflow.execute":
                errors.append(f"{step_path}: nested Workflow.execute is forbidden")
            if action == "Runtime.evaluate" and not allow_runtime:
                errors.append(f"{step_path}: Runtime.evaluate is forbidden in model-authored workflows")
            if known and action not in known:
                errors.append(f"{step_path}: unknown ABCP action {action!r}")
            disabled = disabled_reason_for_method(action, task_type)
            if disabled:
                errors.append(f"{step_path}: {disabled}")
            if enforce_lifecycle:
                if action in {"Page.navigate", "Page.reload", "Page.go"}:
                    obligation = "settlement"
                elif obligation == "state" and action == "Page.getState":
                    obligation = "axtree"
                elif obligation == "axtree" and action == "DOM.getAXTree":
                    obligation = ""
                elif obligation == "state_only" and action == "Page.getState":
                    obligation = ""
        elif step_type in _SETTLEMENT_STEP_TYPES:
            focus = event_step_focus(raw)
            if not focus:
                errors.append(
                    f"{step_path}: a {step_type} step must name at least one event"
                    " in focus"
                )
            for event in focus:
                if event not in LISTENABLE_EVENTS:
                    errors.append(f"{step_path}: event {event!r} is not listenable")
            if enforce_lifecycle:
                waited = set(focus)
                if obligation == "settlement" and "Page.loaded" in waited:
                    obligation = "state"
                elif obligation == "settlement" and "Page.loadFailed" in waited:
                    obligation = "state_only"
                elif waited & {"Page.recovered", "Page.navigate"}:
                    obligation = "state"
                elif waited & {"Page.dialogClosed", "File.chooserClosed"}:
                    obligation = "state_only"
        elif step_type == "if":
            _validate_condition(raw.get("condition"), f"{step_path}.condition", errors)
            for branch in ("then", "else"):
                nested = raw.get(branch)
                if nested is None and branch == "else":
                    continue
                if not isinstance(nested, list):
                    errors.append(f"{step_path}.{branch} must be an array")
                    continue
                _validate_sequence(
                    nested, path=f"{step_path}.{branch}", errors=errors,
                    known=known, task_type=task_type, allow_runtime=allow_runtime,
                    enforce_lifecycle=enforce_lifecycle, count=count,
                    max_loop_iterations=max_loop_iterations,
                )
        elif step_type == "loop":
            _validate_condition(raw.get("condition"), f"{step_path}.condition", errors)
            iterations = int(_number(raw.get("maxIterations"), 0))
            if iterations < 1 or iterations > max_loop_iterations:
                errors.append(
                    f"{step_path}.maxIterations must be between 1 and {max_loop_iterations}"
                )
            body = raw.get("body")
            if not isinstance(body, list):
                errors.append(f"{step_path}.body must be an array")
            else:
                _validate_sequence(
                    body, path=f"{step_path}.body", errors=errors,
                    known=known, task_type=task_type, allow_runtime=allow_runtime,
                    enforce_lifecycle=enforce_lifecycle, count=count,
                    max_loop_iterations=max_loop_iterations,
                )
        elif step_type == "store":
            if not str(raw.get("path") or "").strip():
                errors.append(f"{step_path}.path must be a non-empty store path")
            op = str(raw.get("op") or "").strip()
            if op not in _STORE_OPS:
                errors.append(
                    f"{step_path}.op must be one of {sorted(_STORE_OPS)}"
                )
            elif op != "delete" and "value" not in raw:
                errors.append(f"{step_path}.value is required for op {op!r}")
        elif step_type != "transform":
            errors.append(f"{step_path}: unsupported workflow step type {step_type!r}")

    if enforce_lifecycle and obligation:
        errors.append(f"{path} ends with unresolved lifecycle obligation {obligation!r}")


def _validate_condition(condition: Any, path: str, errors: List[str]) -> None:
    """Reject conditions the platform's strict union cannot parse.

    workflowConditionOrGroupSchema is a union of two strict objects — a leaf
    ({path, operator, value?}) or a group ({operator, conditions}). A bare
    string was offered by the harness schema for a long time and has no
    platform counterpart, so it rejects the whole workflow with -32602.
    """
    if isinstance(condition, str):
        errors.append(
            f"{path} must be a structured condition object, not a string"
            " (use {\"path\": ..., \"operator\": ..., \"value\": ...})"
        )
        return
    if not isinstance(condition, dict):
        errors.append(f"{path} must be a condition object or condition group")
        return
    operators = _contract().condition_operators
    operator = str(condition.get("operator") or "").strip()
    if operator in operators["group"]:
        nested = condition.get("conditions")
        if not isinstance(nested, list) or not nested:
            errors.append(f"{path}.conditions must be a non-empty array")
            return
        for index, item in enumerate(nested):
            _validate_condition(item, f"{path}.conditions[{index}]", errors)
        return
    if not str(condition.get("path") or "").strip():
        errors.append(f"{path}.path must be a non-empty workflow reference")
    if operator not in operators["leaf"]:
        errors.append(
            f"{path}.operator must be one of {sorted(operators['leaf'])}"
        )


def _number(value: Any, default: float) -> float:
    try:
        return float(default if value is None else value)
    except (TypeError, ValueError):
        return float(default)


def _error(errors: List[str]) -> JsonDict:
    return {
        "status": "rejected",
        "policy_violation": "workflow_policy_rejected",
        "errors": errors,
        "tool_was_executed": False,
        "next_instruction": (
            "Use only task-type-allowed ABCP actions. After navigation wait for"
            " Page.loaded or Page.loadFailed with a waitEvent step. After"
            " Page.loaded call"
            " Page.getState and DOM.getAXTree; after Page.loadFailed call"
            " Page.getState only."
        ),
    }
