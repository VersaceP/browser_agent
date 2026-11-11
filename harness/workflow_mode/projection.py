"""First views of saved errors and checks; complete evidence keeps its address."""
from __future__ import annotations

import json
from typing import Any

from harness.workflow_mode.evidence import evidence_locations, read_evidence


def summary_evidence_scope(receipt: dict[str, Any], ref: str) -> dict[str, str] | None:
    """Bind a package's output-relative summary addresses to saved evidence.

    Keep the package summary verbatim. Its business strings are not scanned or
    rewritten. The reader explicitly composes its pointers with this scope.
    """
    return evidence_locations(ref, receipt, {"output": "/response/data/output"}).get("output")


def _bounded_view(value: Any, address: str, budget: int) -> dict[str, Any]:
    view = read_evidence(value, address, max_chars=budget)
    if len(json.dumps(view, ensure_ascii=False)) <= budget:
        return view
    # Generic evidence indices can contain every object key. Keep this first
    # view bounded even when a single error contains thousands of fields.
    return {"truncated": True, "pointer": address, "type": type(value).__name__,
            "totalChars": len(json.dumps(value, ensure_ascii=False)),
            "next": "Read this saved pointer with offset/limit for complete evidence."}


def preflight_view(checks: dict[str, Any], ref: str) -> dict[str, Any]:
    """Keep non-pass claims visible instead of truncating a large passing report."""
    items = checks.get("items") or []
    selected = [(i, item) for i, item in enumerate(items)
                if isinstance(item, dict) and item.get("status") != "pass"]
    rows, used = [], 0
    for index, item in selected:
        if rows and used >= 8000:
            break
        address = f"/preflight/items/{index}"
        row = {"evidence": {"ref": ref, "pointer": address},
               **_bounded_view(item, address, max(512, 8000 - used))}
        if row.get("truncated"):
            row.update({k: item[k] for k in ("key", "status")
                        if isinstance(item.get(k), str) and len(item[k]) <= 256})
        rows.append(row)
        used += len(json.dumps(row, ensure_ascii=False))
    view = {"status": checks.get("status"), "counts": checks.get("counts"),
            "nonPassing": {"items": rows, "total": len(selected), "shown": len(rows),
                           "hasMore": len(rows) < len(selected)},
            "evidence": {"ref": ref, "pointer": "/preflight"},
            "interpretation": checks.get("interpretation")}
    if "error" in checks:
        view["error"] = _bounded_view(checks["error"], "/preflight/error", 2000)
    return view


def failure_evidence(receipt: dict[str, Any], ref: str) -> dict[str, Any]:
    """Group identical errors; differing details never collapse into one example."""
    response = receipt.get("response") or {}
    data = response.get("data") if isinstance(response, dict) else None
    calls = data.get("results") if isinstance(data, dict) else None
    variants = {}
    for index, call in enumerate(calls if isinstance(calls, list) else []):
        if not isinstance(call, dict) or call.get("status") != "failed":
            continue
        error = call.get("error")
        action = call.get("action") or call.get("kind")
        key = json.dumps([action, error], ensure_ascii=False, sort_keys=True)
        if key not in variants:
            variants[key] = {"action": action, "count": 0, "firstCallId": call.get("callId"),
                "pointer": f"/response/data/results/{index}" + ("/error" if "error" in call else ""),
                "error": error if "error" in call else call}
        variants[key]["count"] += 1
    rows, used = [], 0
    for item in variants.values():
        if rows and used >= 8000:
            break
        row = {"action": item["action"], "count": item["count"], "firstCallId": item["firstCallId"],
               "evidence": {"ref": ref, "pointer": item["pointer"]},
               "error": _bounded_view(item["error"], item["pointer"], max(512, 8000 - used))}
        rows.append(row)
        used += len(json.dumps(row, ensure_ascii=False))
    view = {"variants": rows, "totalVariants": len(variants), "hasMore": len(rows) < len(variants),
            "interpretation": "Recorded failures include handled ones; they do not decide business completion."}
    if isinstance(calls, list):
        view["calls"] = {"ref": ref, "pointer": "/response/data/results"}
    return view


def checkpoint_view(checkpoint: dict[str, Any], ref: str) -> dict[str, Any]:
    """Point to a saved checkpoint instead of echoing prior receipts and inputs."""
    return {key: checkpoint.get(key) for key in
            ("status", "conclusion", "taskId", "acceptanceRevision", "executions")} | {
                "evidence": {"ref": ref, "pointer": ""},
                "note": "Full checkpoint is saved. Interpret the new answer against the original goal."}
