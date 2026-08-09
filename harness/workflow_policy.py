"""Recursive harness policy for ABCP Workflow.execute."""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

from harness.tool_policy import disabled_reason_for_method
from harness.screenshot_policy import normalize_screenshot_output_params


JsonDict = Dict[str, Any]
LISTENABLE_EVENTS = frozenset({
    "Page.open", "Page.close", "Page.loaded", "Page.startedLoading",
    "Page.loadFailed", "Page.crashed", "Page.recovered", "Page.navigate",
    "Page.titleUpdated", "Page.dialogOpened", "Page.dialogClosed",
    "File.chooserOpened", "File.chooserClosed", "Hitl.humanInput",
    "Hitl.resumeEvent",
})


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
        if step.get("type") != "listen":
            continue
        event = str(step.get("event") or "")
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
    allow_legacy_listen_events: bool = False,
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
    step_timeout = _number(params.get("stepTimeout"), 30000)
    if timeout < 1000 or timeout > 600000:
        errors.append("timeout must be between 1000 and 600000 ms")
    if step_timeout < 1000 or step_timeout > 60000:
        errors.append("stepTimeout must be between 1000 and 60000 ms")

    count = [0]
    _validate_sequence(
        steps,
        path="steps",
        errors=errors,
        known=known,
        task_type=task_type,
        allow_runtime=allow_runtime,
        enforce_lifecycle=enforce_lifecycle,
        allow_legacy_listen_events=allow_legacy_listen_events,
        count=count,
        max_loop_iterations=max_loop_iterations,
    )
    if count[0] > max_steps:
        errors.append(f"workflow contains {count[0]} steps; maximum is {max_steps}")
    if errors:
        return None, _error(errors)
    normalized = dict(params)
    normalized["steps"] = _normalize_screenshot_outputs(steps)
    normalized["timeout"] = int(timeout)
    normalized["stepTimeout"] = int(step_timeout)
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


def _validate_sequence(
    steps: List[Any],
    *,
    path: str,
    errors: List[str],
    known: Set[str],
    task_type: str,
    allow_runtime: bool,
    enforce_lifecycle: bool,
    allow_legacy_listen_events: bool,
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
                step_type == "listen"
                and str(raw.get("event") or "") in {"Page.loaded", "Page.loadFailed"}
            ):
                errors.append(
                    f"{step_path} must listen for Page.loaded or Page.loadFailed"
                    " immediately after navigation"
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
        elif step_type == "listen":
            event = str(raw.get("event") or "")
            if event not in LISTENABLE_EVENTS and not (
                allow_legacy_listen_events and event == "Hitl.resumed"
            ):
                errors.append(f"{step_path}: event {event!r} is not listenable")
            if enforce_lifecycle:
                if obligation == "settlement" and event == "Page.loaded":
                    obligation = "state"
                elif obligation == "settlement" and event == "Page.loadFailed":
                    obligation = "state_only"
                elif event in {"Page.recovered", "Page.navigate"}:
                    obligation = "state"
                elif event in {"Page.dialogClosed", "File.chooserClosed"}:
                    obligation = "state_only"
        elif step_type == "if":
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
                    allow_legacy_listen_events=allow_legacy_listen_events,
                    max_loop_iterations=max_loop_iterations,
                )
        elif step_type == "loop":
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
                    allow_legacy_listen_events=allow_legacy_listen_events,
                    max_loop_iterations=max_loop_iterations,
                )
        elif step_type != "transform":
            errors.append(f"{step_path}: unsupported workflow step type {step_type!r}")

    if enforce_lifecycle and obligation:
        errors.append(f"{path} ends with unresolved lifecycle obligation {obligation!r}")


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
            "Use only task-type-allowed ABCP actions. After navigation listen for"
            " Page.loaded or Page.loadFailed. After Page.loaded call"
            " Page.getState and DOM.getAXTree; after Page.loadFailed call"
            " Page.getState only."
        ),
    }
