"""Wire form of Workflow.execute: the harness's flat params -> WorkflowDefinitionV1.

Inside the harness a workflow request stays the flat shape every policy,
fence and observer already reads: ``{description, steps, variables, timeout,
pageId, fleetId}``. WebCross 0.9.3 accepts only a versioned document plus a
runtime binding, in a strict schema that rejects any other top-level key::

    {"workflow": {"schemaVersion": 1, "name", "description"?, "timeoutMs",
                  "initialVariables", "steps"},
     "binding": {"pageId"?, "fleetId"?}}

and every Action step must say ``type: "action"`` and carry a ``purpose``.
`to_platform_execute_params` is the single translation, applied where the
request leaves the process, so no caller has to know the wire form.
"""

from __future__ import annotations

import copy
from typing import Any, Dict, List

from harness.utils import JsonDict


WORKFLOW_SCHEMA_VERSION = 1
_NAME_LIMIT = 200
_DESCRIPTION_LIMIT = 2_000
_NESTED_STEP_KEYS = ("then", "else", "body")


def _platform_step(step: Any) -> Any:
    if not isinstance(step, dict):
        return step
    out = copy.deepcopy(step)
    if "type" not in out and "action" in out:
        out["type"] = "action"
    if out.get("type") == "action" and not str(out.get("purpose") or "").strip():
        # The platform requires an audit purpose on every Action step; a
        # recipe written before that rule states its intent through the step
        # id or the Action itself.
        subject = str(out.get("id") or out.get("action") or "workflow step")
        out["purpose"] = f"Workflow step: {subject}"
    for key in _NESTED_STEP_KEYS:
        nested = out.get(key)
        if isinstance(nested, list):
            out[key] = [_platform_step(item) for item in nested]
    return out


def platform_steps(steps: Any) -> List[Any]:
    return [_platform_step(step) for step in steps] if isinstance(steps, list) else []


def to_platform_execute_params(params: Any) -> Any:
    """Translate flat harness params; params already in wire form pass through."""
    if not isinstance(params, dict) or "workflow" in params:
        return params
    description = str(params.get("description") or "").strip()
    workflow: JsonDict = {
        "schemaVersion": WORKFLOW_SCHEMA_VERSION,
        "name": (description or "Harness workflow")[:_NAME_LIMIT],
        "initialVariables": copy.deepcopy(params.get("variables") or {}),
        "steps": platform_steps(params.get("steps")),
    }
    if description:
        workflow["description"] = description[:_DESCRIPTION_LIMIT]
    timeout = params.get("timeout")
    if isinstance(timeout, (int, float)) and not isinstance(timeout, bool):
        workflow["timeoutMs"] = int(timeout)
    wire: Dict[str, Any] = {"workflow": workflow}
    binding = {
        key: params[key] for key in ("pageId", "fleetId")
        if isinstance(params.get(key), str) and params[key].strip()
    }
    if binding:
        wire["binding"] = binding
    return wire
