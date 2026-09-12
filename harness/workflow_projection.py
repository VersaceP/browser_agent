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

from harness.offload import store_offloaded_payload
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
