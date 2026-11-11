"""Verify paged WebCross results without replaying their browser operations."""
from __future__ import annotations

import base64
import binascii
import hashlib
import json
import re
from typing import Any

PAGE_BYTES = 512 * 1024
MAX_RESULT_BYTES = 256 * 1024 * 1024


async def read_retained_result(client: Any, workflow_id: str, reference: dict | None = None) -> tuple[dict, dict]:
    """Read immutable pages, validating identity, offsets and digest before exposing JSON."""
    cursor = None
    body = bytearray()
    pages = 0
    expected = dict(reference) if isinstance(reference, dict) else None
    while True:
        params = {"workflowId": workflow_id, "maxBytes": PAGE_BYTES}
        if cursor is not None:
            params["cursor"] = cursor
        response = await client.call("Workflow.readResult", params)
        page = response.get("data", response) if isinstance(response, dict) else None
        if not isinstance(page, dict) or page.get("encoding") != "base64":
            raise ValueError("Invalid retained workflow result page")
        ref = page.get("reference")
        if (not isinstance(ref, dict) or ref.get("workflowId") != workflow_id
                or type(ref.get("bytes")) is not int or not 0 < ref["bytes"] <= MAX_RESULT_BYTES
                or not isinstance(ref.get("sha256"), str)
                or not re.fullmatch(r"[a-f0-9]{64}", ref["sha256"])
                or not isinstance(ref.get("taskId"), str)):
            raise ValueError("Invalid retained workflow result identity or size")
        if expected is None:
            expected = ref
        if ref != expected or type(page.get("offset")) is not int or page["offset"] != len(body):
            raise ValueError("Retained workflow result changed or has a discontinuous byte range")
        encoded = page.get("data")
        if not isinstance(encoded, str) or len(encoded) > 4 * ((PAGE_BYTES + 2) // 3):
            raise ValueError("Retained workflow result page exceeds the requested budget")
        try:
            chunk = base64.b64decode(encoded, validate=True)
        except (ValueError, binascii.Error) as exc:
            raise ValueError("Invalid base64 in retained workflow result") from exc
        if len(chunk) != min(PAGE_BYTES, expected["bytes"] - len(body)):
            raise ValueError("Retained workflow result page has an invalid length")
        body.extend(chunk)
        pages += 1
        next_cursor = page.get("nextCursor")
        if len(body) == expected["bytes"]:
            if next_cursor is not None:
                raise ValueError("Retained workflow result has a cursor after its last byte")
            break
        if not isinstance(next_cursor, str) or not next_cursor or len(next_cursor) > 200 or next_cursor == cursor:
            raise ValueError("Retained workflow result is incomplete or its cursor did not advance")
        cursor = next_cursor
    if hashlib.sha256(body).hexdigest() != expected["sha256"]:
        raise ValueError("Retained workflow result digest mismatch")
    result = json.loads(body)
    if (not isinstance(result, dict) or result.get("workflowId") != workflow_id
            or result.get("taskId") != expected["taskId"]
            or result.get("status") not in {"succeeded", "failed", "cancelled"}
            or not isinstance(result.get("results"), list)
            or len(result["results"]) != expected.get("callCount")
            or "output" not in result):
        raise ValueError("Retained workflow result does not match its reference")
    omitted = sum(isinstance(call, dict) and call.get("valueOmittedReason") is not None for call in result["results"])
    if omitted != expected.get("omittedValueCount"):
        raise ValueError("Retained workflow result has inconsistent evidence completeness")
    return result, {"reference": expected, "pages": pages, "verified": True}
