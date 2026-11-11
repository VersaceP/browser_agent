"""Project a Workflow.execute receipt down to what the model actually needs.

A segment that drives a control by observing inside itself — click, read the
tree, match a label, click the match — consumes its own intermediate
observations.  They are the workflow's working memory, not an answer, yet the
platform returns every one of them and the whole envelope travels into model
context.

Measured on run ``8208ed49`` (2026-09-11, 16 segments):

===============================  =========  ====================
part of the receipt                  bytes  share
===============================  =========  ====================
``data.results`` step payloads      413927  79% of all receipts
one segment's ``data``               42644  99% of that receipt
its nested AXTree ``lines``          41653  98% of that ``data``
===============================  =========  ====================

Field offload cannot reach any of it.  ``offload_large_response_fields`` keys on
the method name (``Workflow.execute`` is not a ``DOM.*``) and only scans
``response["data"][field]``, two levels above where a nested tree lives.  Nor is
raising the whitelist enough: the 13 trees measured 27076-45858 bytes each, so
at the 50000-byte threshold **none** of them would offload.  Lowering the
threshold to catch them would also catch every direct ``DOM.getAXTree``, which
was deliberately raised to 50000.

So the fix is not to move the intermediate trees to disk and hand back a
reference — it is to not return them at all.  What the model needs from a
finished segment is: which steps ran, what each returned *if it was small*, the
variables and store the segment produced, and the final observation it ended on.
The complete untouched payload is written to the observations directory, so
nothing is lost and an audit or a recovery can still read it.
"""

from __future__ import annotations

import copy
import uuid
from typing import Any, Dict, List, Optional

from harness.context.offload import store_offloaded_payload
from harness.utils import (
    JsonDict,
    RunLogger,
    count_json_nodes,
    json_size_bytes,
    outline_value,
    safe_path_component,
    task_subdir,
)

#: Step results at or below this size ride along inline. A click receipt is
#: ~120 bytes and genuinely useful ("did the click resolve by id or selector");
#: an AX tree is two orders of magnitude larger and is what this module exists
#: to remove.
DEFAULT_INLINE_RESULT_BYTES = 4000


def workflow_execution_facts(result: Any) -> JsonDict:
    """Extract stable execution facts before a large receipt is offloaded."""
    if not isinstance(result, dict):
        return {"status": "unknown", "workflowId": None, "tool_was_executed": False}
    captured = result.get("workflowExecution")
    if isinstance(captured, dict):
        return copy.deepcopy(captured)
    response = result.get("response")
    response = response if isinstance(response, dict) else {}
    data = response.get("data")
    data = data if isinstance(data, dict) else {}
    if "output" in data:
        from harness.workflow.script_runtime import script_result_facts
        return script_result_facts(result)
    rpc_data = result.get("rpcData")
    rpc_data = rpc_data if isinstance(rpc_data, dict) else {}
    details = rpc_data.get("details")
    details = details if isinstance(details, dict) else rpc_data
    if str(details.get("code") or "").startswith("workflow-") or "source" in details:
        return {"status": "failed", "workflowId": details.get("workflowId"),
                "tool_was_executed": bool(details.get("taskId")),
                "failedErrorCode": details.get("code"), "source": details.get("source")}
    if str(result.get("transportCode") or "").startswith("ABCP_TRANSPORT_"):
        trace = result.get("executionTrace") or {}
        return {"status": "unknown", "workflowId": trace.get("workflowId"),
                "tool_was_executed": result.get("requestSent"),
                "failedErrorCode": result.get("transportCode"), "deliveryConfirmed": False}
    workflow_id = data.get("workflowId") or details.get("workflowId")
    executed = result.get("tool_was_executed")
    if executed is None:
        executed = bool(workflow_id)
    return {
        "status": data.get("status") or details.get("status") or result.get("status")
                  or ("failed" if result.get("error") else "unknown"),
        "workflowId": workflow_id,
        "tool_was_executed": bool(executed),
        "failedStepPath": data.get("failedStepPath") or result.get("failedStepPath")
                          or details.get("failedStepPath"),
        "failedErrorCode": data.get("failedErrorCode") or data.get("failedActionCode")
                           or result.get("failedErrorCode") or details.get("failedActionCode"),
    }


def capture_workflow_result(logger, result: dict) -> None:
    """Persist control data before the model copy can truncate its output."""
    if result.get("workflowResultRef"):
        return
    facts = workflow_execution_facts(result)
    response = result.get("response") or {}
    data = response.get("data") if isinstance(response, dict) else None
    control = {"execution": facts, "outputPresent": isinstance(data, dict) and "output" in data,
               "output": data.get("output") if isinstance(data, dict) else None,
               "error": data.get("error") if isinstance(data, dict) else result.get("error")}
    if logger is not None and hasattr(logger, "storage"):
        row = logger.storage.save_resource(task_id=logger.task_id, run_id=logger.run_id,
            resource_type="workflow_execution", logical_path=f"workflow/receipts/{uuid.uuid4().hex}.json",
            content=control)
        result["workflowResultRef"] = row["saved_path"]
    result["workflowExecution"] = facts


def workflow_execution_result(result: dict, logger=None) -> dict:
    """Consumer view; indexed execution data takes precedence over display."""
    reference = result.get("workflowResultRef")
    if reference and logger is not None:
        import json
        row = logger.storage.read_resource(current_task_id=logger.task_id, resource_uri=reference)
        if not row:
            raise ValueError("Workflow control receipt is unavailable; reconcile execution before retry")
        value = row.get("content_json")
        value = json.loads(value) if isinstance(value, str) else value
        if not isinstance(value, dict) or not isinstance(value.get("execution"), dict):
            raise ValueError("Invalid Workflow control receipt")
        return value
    response = result.get("response") or {}
    data = response.get("data") if isinstance(response, dict) else None
    return {"execution": workflow_execution_facts(result),
            "outputPresent": isinstance(data, dict) and "output" in data,
            "output": data.get("output") if isinstance(data, dict) else None}


def project_workflow_receipt(
    *,
    logger: Optional[RunLogger],
    result: Any,
    step: Any = None,
    prefix: str = "",
    inline_result_bytes: int = DEFAULT_INLINE_RESULT_BYTES,
) -> Optional[JsonDict]:
    """Shrink ``result`` in place; return a receipt of what was projected.

    Returns ``None`` when the result is not a Workflow.execute envelope or has
    nothing worth projecting, so the caller can leave the payload untouched.
    """
    if not isinstance(result, dict):
        return None
    response = result.get("response")
    if not isinstance(response, dict):
        return None
    data = response.get("data")
    if not isinstance(data, dict):
        return None
    steps = data.get("results")
    if not isinstance(steps, list) or not steps:
        return None
    if "output" in data:
        from harness.workflow.call_facts import call_facts
        data["callFacts"] = call_facts(data)
        candidates = {i for i, call in enumerate(steps)
                      if isinstance(call, dict) and "value" in call
                      and json_size_bytes(call["value"]) > inline_result_bytes}
        if not candidates and len(steps) <= 64:
            return None
        saved = _persist_full_payload(logger, data, step, prefix)
        if not saved.get("savedPath"):
            return {"projected": False, "projectionSkipped": "full-payload-not-persisted"}
        projected = copy.deepcopy(steps)
        for i in candidates:
            projected[i].pop("value", None)
            projected[i]["valueReference"] = {"resource": saved["savedPath"], "index": i, "field": "value"}
        if len(projected) > 64:
            indices = sorted({*range(8), *range(len(projected) - 32, len(projected)),
                              *[i for i, c in enumerate(projected) if c.get("status") in {"failed", "unknown", "cancelled"}][:24]})
            projected = [projected[i] for i in indices]
        data["results"] = projected
        data["callRecordsReference"] = {"resource": saved["savedPath"], "total": len(steps),
                                         "omitted": len(steps) - len(projected)}
        return {"projected": True, "stepsTotal": len(steps), "stepResultsProjected": len(candidates),
                **saved}

    # Two phases, and the order matters. Scanning first means a segment with
    # nothing to project leaves no trace at all: the receipt is returned
    # untouched, no observation file is written, and no telemetry claims a
    # projection happened. Persisting first (the original shape) made every
    # ordinary click->getAXTree segment write a file and report
    # `projected: true` with `stepResultsProjected: 0` — 15 times in run
    # 8208ed49, whose segments are all exactly that shape.
    last_index = len(steps) - 1
    candidates = {
        index
        for index, entry in enumerate(steps)
        if isinstance(entry, dict)
        and index != last_index
        and entry.get("result") is not None
        and json_size_bytes(entry["result"]) > inline_result_bytes
    }
    if not candidates:
        return None

    original_bytes = json_size_bytes(data)
    saved_facts = _persist_full_payload(logger, data, step, prefix)
    if not saved_facts.get("savedPath"):
        # Fail OPEN. Projection is an optimisation, and an optimisation that
        # cannot write its escape hatch has to decline rather than proceed:
        # a full disk, a permission error or an unserialisable payload would
        # otherwise drop the intermediate results with nowhere to read them
        # back from, while the receipt still claimed the payload was on disk.
        return {
            "projected": False,
            "projectionSkipped": "full-payload-not-persisted",
            "stepsTotal": len(steps),
            "stepResultsProjected": 0,
            "candidates": len(candidates),
            "originalBytes": original_bytes,
        }

    projected: List[Any] = []
    projected_count = 0
    for index, entry in enumerate(steps):
        if not isinstance(entry, dict):
            projected.append(entry)
            continue
        shrunk, was_projected = _project_step(
            entry,
            is_last=index == last_index,
            inline_result_bytes=inline_result_bytes,
            saved_facts=saved_facts,
        )
        projected.append(shrunk)
        projected_count += int(was_projected)

    data["results"] = projected
    projected_bytes = json_size_bytes(data)
    receipt: JsonDict = {
        "projected": True,
        "stepsTotal": len(steps),
        "stepResultsProjected": projected_count,
        "originalBytes": original_bytes,
        "projectedBytes": projected_bytes,
        "inlineResultBytes": inline_result_bytes,
        "note": (
            "Intermediate step results were consumed by this segment and are"
            " not returned. Variables, store and the final step's result are"
            " complete. The untouched payload is on disk."
        ),
    }
    if saved_facts:
        receipt.update(saved_facts)
        receipt["query_with"] = "local_fs_read"
    result["resultProjection"] = receipt
    return receipt


def _project_step(
    entry: JsonDict,
    *,
    is_last: bool,
    inline_result_bytes: int,
    saved_facts: JsonDict,
) -> tuple:
    shrunk = dict(entry)
    # The per-step `step` echo repeats the authored params, which already
    # travel whole in the top-level `params` of this very tool result. Keep the
    # identity fields so a failure path can name the step, drop the copy.
    descriptor = shrunk.get("step")
    if isinstance(descriptor, dict):
        shrunk["step"] = {
            key: descriptor[key]
            for key in ("id", "type", "action", "purpose", "output", "op")
            if key in descriptor
        }

    payload = shrunk.get("result")
    if payload is None:
        return shrunk, False
    size = json_size_bytes(payload)
    if is_last or size <= inline_result_bytes:
        return shrunk, False

    stub: JsonDict = {
        "_projected": True,
        "reason": (
            "intermediate step result consumed inside the segment"
        ),
        "byteSize": size,
        "outline": outline_value(payload),
        "nodeCount": count_json_nodes(payload),
    }
    if isinstance(payload, dict):
        stub["fields"] = {
            key: json_size_bytes(value) for key, value in payload.items()
        }
    if saved_facts.get("savedPath"):
        stub["savedPath"] = saved_facts["savedPath"]
        stub["stepPath"] = shrunk.get("stepPath")
        stub["query_with"] = "local_fs_read"
    shrunk["result"] = stub
    return shrunk, True


def _persist_full_payload(
    logger: Optional[RunLogger], data: JsonDict, step: Any, prefix: str,
) -> JsonDict:
    """Write the untouched workflow payload and return how to page it back."""
    if logger is None:
        return {}
    try:
        observations = task_subdir(logger, "observations")
        parts = [
            part
            for part in (
                safe_path_component(prefix, "agent") if prefix else "",
                f"workflow-execute-step{step or 0}",
                safe_path_component(
                    str(data.get("workflowId") or "")[:8] or "run", "workflow"
                ),
                uuid.uuid4().hex[:8],
            )
            if part
        ]
        path = observations / ("-".join(parts) + ".json")
        return store_offloaded_payload(
            logger,
            path,
            resource_type="observation",
            content=copy.deepcopy(data),
        )
    except Exception:  # pragma: no cover - persistence must not break a call
        return {}
