"""Compact pointers to immutable evidence; never reinterpret missing facts as success."""
import copy
import json


def page(value, *, reference, offset=0, max_chars=16000):
    text = json.dumps(value, ensure_ascii=False)
    start, limit = max(0, offset), max(1, min(max_chars, 50000))
    return {"reference": reference, "content": text[start:start + limit],
        "nextOffset": min(len(text), start + limit), "hasMore": start + limit < len(text)}


def evidence_summary(value):
    result = copy.deepcopy(value)
    # Raw binding contains the entire inline JS package plus all business inputs.
    # Identity and raw receipt remain; this never changes script/semantic outcomes.
    for workflow in result.get("workflows", []):
        for execution in workflow.get("currentExecutions", []):
            binding = execution.get("binding") or {}
            execution["binding"] = {k: binding[k] for k in ("path", "pageId", "fleetId") if k in binding}
    for execution in result.get("unattributedExecutions", []):
        execution.pop("binding", None)
    return result


def execution_delta(result):
    """Only this dispatch's effects; full state remains available through state reads."""
    result = copy.deepcopy(result)
    projection = result.pop("execution", None)
    if projection:
        result["currentState"] = {k: projection.get(k) for k in
            ("revision", "status", "browserBinding", "pendingInput")}
        result["checkpoints"] = {sid: {k: r.get(k) for k in
            ("status", "reason", "sourceHash", "inputHash", "receiptRef")}
            for sid, r in (projection.get("steps") or {}).items()}
    return result
