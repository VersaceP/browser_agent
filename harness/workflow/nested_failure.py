"""Project received public nested Action failures; never replay or synthesize advice."""
from __future__ import annotations

from typing import Any

_MAX_TEXT = 1000
_MAX_NESTED_DEPTH = 4


def _text(value: Any) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    return value[:_MAX_TEXT]


def public_failure(value: Any, *, _depth: int = 0) -> dict:
    """Extract only public leaf feedback from common RPC wrapper shapes.

    The source object is never mutated. Wrapper traversal is bounded because
    this function runs on model-facing failure data.
    """
    if not isinstance(value, dict) or _depth > _MAX_NESTED_DEPTH:
        return {}
    out = {}
    for key in ("observation", "suggested_prompt"):
        text = _text(value.get(key))
        if text is not None:
            out[key] = text
    error = value.get("error")
    if isinstance(error, dict):
        projected = {}
        for key in ("code", "message"):
            text = _text(error.get(key))
            if text is not None:
                projected[key] = text
        if projected:
            out["error"] = projected
    if out:
        return out
    for key in ("rpcData", "response", "data", "result", "nestedActionFailure"):
        nested = public_failure(value.get(key), _depth=_depth + 1)
        if nested:
            return nested
    return {}


def attach_nested_action_failure(result: dict, details: dict, trace: dict) -> None:
    """Only match the failed leaf; successful/other-step feedback is not recovery advice."""
    path = result.get("failedStepPath")
    code = result.get("failedErrorCode")
    if not path:
        return
    out = {"stepPath": path}
    if code:
        out["error"] = {"code": code}
    sources = []

    def merge(feedback: dict, label: str) -> None:
        if not feedback:
            return
        error = feedback.pop("error", {})
        if error:
            out.setdefault("error", {}).update(error)
        out.update(feedback)
        sources.append(label)

    terminal = trace.get("failure")
    terminal_rows = [dict(terminal, status="error")] if isinstance(terminal, dict) else []
    # Trace first, RPC last: the final response is authoritative when present.
    for label, rows in (("progress", trace.get("steps")),
                        ("progress.failure", terminal_rows),
                        ("rpc.results", details.get("results"))):
        if not isinstance(rows, list):
            continue
        matches = [r for r in rows if isinstance(r, dict)
                   and r.get("stepPath") == path and r.get("status") in ("error", "failed")]
        if not matches:
            continue
        row = matches[-1]  # repeated loop locations: latest failed execution
        for key in ("action", "stepRunId"):
            if isinstance(row.get(key), str):
                out[key] = row[key]
        for value in (row, row.get("result"), row.get("nestedActionFailure")):
            feedback = public_failure(value)
            merge(feedback, label)
    nested = details.get("nestedActionFailure")
    if isinstance(nested, dict) and nested.get("stepPath") == path:
        feedback = public_failure(nested)
        merge(feedback, "rpc.nestedActionFailure")
        if isinstance(nested.get("action"), str):
            out["action"] = nested["action"]
    missing = [key for key in ("observation", "suggested_prompt") if key not in out]
    if not out.get("error", {}).get("message"):
        missing.append("error.message")
    out["feedbackAvailability"] = "partial" if sources and missing else "available" if sources else "unavailable"
    out["missingFields"] = missing
    out["sources"] = sorted(set(sources))
    result["nestedActionFailure"] = out
