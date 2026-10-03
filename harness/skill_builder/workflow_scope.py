"""Permission admission for child actions executed inside WebCross Workflow.

WebCross owns DSL syntax and execution. Harness inspects only Action names
because nested calls bypass per-tool file, network, and target authorization.
"""

from __future__ import annotations

from typing import Any, Iterable

from harness.tools.tool_policy import disabled_reason_for_method


def workflow_action_scope_error(
    workflow: dict[str, Any], *, capability_methods: Iterable[str] = (),
    page_id: str = "", fleet_id: str = "",
) -> str | None:
    methods = set(capability_methods)

    def scan(steps: Any) -> str | None:
        if not isinstance(steps, list):
            return None  # DSL shape belongs to the platform compiler.
        for step in steps:
            if not isinstance(step, dict):
                continue
            method = step.get("action")
            if isinstance(method, str) and method:
                reason = disabled_reason_for_method(method)
                if reason:
                    return reason
                if methods and method not in methods:
                    return f"Nested Action {method} is not in the live capability catalog"
                if not method.startswith(("Page.", "DOM.", "Input.")):
                    return f"Nested Action {method} needs an independently authorized tool call"
                if method in {"Page.create", "Page.list", "Page.switchTo"}:
                    return f"Nested Action {method} changes or discovers page ownership"
                params = step.get("params")
                if isinstance(params, dict):
                    if "pageId" in params and params["pageId"] != page_id:
                        return f"Nested Action {method} names a page outside the bound page"
                    if "fleetId" in params and params["fleetId"] != fleet_id:
                        return f"Nested Action {method} names a Fleet outside the bound Fleet"
            for key in ("then", "else", "body"):
                reason = scan(step.get(key))
                if reason:
                    return reason
        return None

    return scan(workflow.get("steps"))
