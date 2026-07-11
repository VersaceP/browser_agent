"""
harness.offload - Artifact capture and large result offload helpers.
"""

import base64
import copy
import json
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, List, Optional, Tuple

from harness.constants import (
    DEFAULT_OFFLOAD_THRESHOLD_BYTES,
    DEFAULT_TOOL_RESULT_OFFLOAD_THRESHOLD_BYTES,
    GENERIC_TOOL_RESULT_KEEP_FIELD_BYTES,
    GENERIC_TOOL_RESULT_KEEP_KEYS,
    GENERIC_TOOL_RESULT_RESPONSE_KEEP_KEYS,
    OFFLOAD_FIELDS,
    OFFLOAD_FIELDS_AS_TEXT,
    OFFLOAD_METHODS,
    SCREENSHOT_METHODS,
)
from harness.utils import (
    JsonDict,
    RunLogger,
    count_json_nodes,
    extract_offloaded_paths,
    json_size_bytes,
    outline_value,
    safe_path_component,
    task_subdir,
)


def write_offloaded_blob(path: Path, field: str, blob: Any) -> Tuple[str, str, Any]:
    if field in OFFLOAD_FIELDS_AS_TEXT:
        if isinstance(blob, list):
            content = "\n".join(str(item) for item in blob)
        else:
            content = str(blob)
        path.write_text(content, encoding="utf-8")
        return "text_lines", "local_fs_search", outline_value(content)

    path.write_text(
        json.dumps(blob, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    return "json_tree", "local_fs_read", outline_value(blob)


def outline_large_field(value: Any, max_bytes: int = GENERIC_TOOL_RESULT_KEEP_FIELD_BYTES) -> Any:
    if json_size_bytes(value) <= max_bytes:
        return value
    return outline_value(value)


SUGGESTED_PROMPT_SUCCESS_MAX_CHARS = 200


def compact_model_facing_tool_result(value: Any) -> Any:
    """Drop low-value successful-result chatter before it reaches the model."""
    if isinstance(value, dict):
        compacted: JsonDict = {}
        for key, item in value.items():
            if key == "suspected_challenge" and _empty_challenge_summary(item):
                continue
            if key == "suggested_prompt" and not _keep_suggested_prompt(value):
                # The skillsGuide says to read suggested_prompt on success too,
                # so keep it — but truncated: it is platform advice, not harness
                # truth, and full-length success chatter is token noise.
                trimmed = _trim_success_suggested_prompt(item)
                if trimmed:
                    compacted[key] = trimmed
                continue
            compacted[key] = compact_model_facing_tool_result(item)
        return compacted
    if isinstance(value, list):
        return [compact_model_facing_tool_result(item) for item in value]
    return value


def _empty_challenge_summary(value: Any) -> bool:
    if not isinstance(value, dict):
        return False
    adjudication = str(value.get("adjudication") or "")
    if adjudication and adjudication != "not_ready":
        return False
    signals = value.get("signals")
    if isinstance(signals, list) and signals:
        return False
    try:
        score = float(value.get("suspicionScore") or 0)
    except (TypeError, ValueError):
        score = 0.0
    return score <= 0 and not bool(value.get("highConfidenceHit"))


def _trim_success_suggested_prompt(item: Any) -> str:
    text = str(item or "").strip()
    if len(text) <= SUGGESTED_PROMPT_SUCCESS_MAX_CHARS:
        return text
    return text[: SUGGESTED_PROMPT_SUCCESS_MAX_CHARS - 1].rstrip() + "…"


def _keep_suggested_prompt(container: JsonDict) -> bool:
    for key in (
        "error",
        "errorClassification",
        "warning",
        "warnings",
        "blocked",
        "blocker",
        "blockers",
        "missingAnyOf",
        "pausedState",
        "autoHitl",
        "challengeAdjudication",
        "hitl_wait",
    ):
        value = container.get(key)
        if value not in (None, "", [], {}):
            return True
    challenge = container.get("suspected_challenge")
    if isinstance(challenge, dict) and not _empty_challenge_summary(challenge):
        return True
    status = str(container.get("status") or "").lower()
    if status and status not in {
        "ok",
        "done",
        "success",
        "succeeded",
        "running",
        "loaded",
        "ready",
        "resumed",
    }:
        return True
    observation = str(container.get("observation") or "").lower()
    return any(
        marker in observation
        for marker in (
            "error",
            "failed",
            "blocked",
            "warning",
            "paused",
            "captcha",
            "challenge",
            "timeout",
            "stale",
        )
    )


def offload_large_tool_result(
    *,
    logger: RunLogger,
    tool_name: str,
    result: Any,
    step: Optional[int],
    prefix: str = "",
    threshold_bytes: int = DEFAULT_TOOL_RESULT_OFFLOAD_THRESHOLD_BYTES,
) -> Any:
    result = compact_model_facing_tool_result(result)
    byte_size = json_size_bytes(result)
    if byte_size <= threshold_bytes:
        return result

    tool_results_dir = task_subdir(logger, "tool_results")
    safe_tool = safe_path_component(tool_name.replace(".", "-"), "tool")
    safe_prefix = safe_path_component(prefix, "agent") if prefix else ""
    filename_parts = [
        part for part in (
            safe_prefix,
            f"step{step or 0}",
            safe_tool,
            uuid.uuid4().hex[:8],
        ) if part
    ]
    path = tool_results_dir / ("-".join(filename_parts) + ".json")
    path.write_text(
        json.dumps(result, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )

    stub: JsonDict = {
        "_offloaded": True,
        "savedPath": str(path.resolve()),
        "format": "json_response",
        "query_with": "local_fs_read",
        "originalBytes": byte_size,
        "byteSize": byte_size,
        "outline": outline_value(result),
    }
    if isinstance(result, dict):
        for key in GENERIC_TOOL_RESULT_KEEP_KEYS:
            if key in result:
                stub[key] = outline_large_field(result[key])
        response = result.get("response")
        if isinstance(response, dict):
            response_stub: JsonDict = {}
            for key in GENERIC_TOOL_RESULT_RESPONSE_KEEP_KEYS:
                if key in response:
                    response_stub[key] = outline_large_field(response[key])
            if response_stub:
                stub["response"] = response_stub
        nested_offloaded = extract_offloaded_paths(result)
        if nested_offloaded:
            stub["nestedOffloadedFiles"] = nested_offloaded[:100]
    try:
        relative_path = str(path.resolve().relative_to(logger.task_dir.resolve()))
    except ValueError:
        relative_path = str(path.resolve())
    logger.write(
        "tool_result.offloaded",
        {
            "tool": tool_name,
            "step": step,
            "prefix": prefix,
            "savedPath": str(path.resolve()),
            "relativePath": relative_path,
            "byteSize": byte_size,
            "queryWith": "local_fs_read",
        },
    )
    return stub


def offload_large_response_fields(
    *,
    logger: RunLogger,
    method: str,
    params: JsonDict,
    response: Any,
    step: Optional[int],
    prefix: str = "",
    threshold_bytes: int = DEFAULT_OFFLOAD_THRESHOLD_BYTES,
) -> Any:
    if method not in OFFLOAD_METHODS or not isinstance(response, dict):
        return response
    data = response.get("data")
    if not isinstance(data, dict):
        if data is None:
            return response
        byte_size = json_size_bytes(data)
        if byte_size <= threshold_bytes:
            return response
        copied = copy.deepcopy(response)
        observations_dir = task_subdir(logger, "observations")
        safe_method = safe_path_component(method.replace(".", "-"), "method")
        safe_prefix = safe_path_component(prefix, "agent") if prefix else ""
        filename_parts = [part for part in (
            safe_prefix,
            f"{safe_method}-step{step or 0}",
            "data",
            uuid.uuid4().hex[:8],
        ) if part]
        path = observations_dir / ("-".join(filename_parts) + ".json")
        path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
        copied["data"] = {
            "_offloaded": True,
            "savedPath": str(path.resolve()),
            "format": "json_tree",
            "query_with": "local_fs_read",
            "originalBytes": byte_size,
            "byteSize": byte_size,
            "nodeCount": count_json_nodes(data),
            "outline": outline_value(data),
            "summary": outline_value(data),
        }
        return copied

    copied: Optional[JsonDict] = None
    page_id = params.get("pageId") or params.get("page_id") or "no-page"
    observations_dir = task_subdir(logger, "observations")
    for field in sorted(OFFLOAD_FIELDS):
        blob = data.get(field)
        if blob is None:
            continue
        byte_size = json_size_bytes(blob)
        field_threshold = (
            min(threshold_bytes, GENERIC_TOOL_RESULT_KEEP_FIELD_BYTES)
            if field == "layers"
            else threshold_bytes
        )
        if byte_size <= field_threshold:
            continue
        if copied is None:
            copied = copy.deepcopy(response)
            data = copied.get("data", {})
        safe_method = safe_path_component(method.replace(".", "-"), "method")
        safe_page = safe_path_component(page_id, "page")
        safe_prefix = safe_path_component(prefix, "agent") if prefix else ""
        suffix = "txt" if field in OFFLOAD_FIELDS_AS_TEXT else "json"
        filename_parts = [part for part in (
            safe_prefix,
            f"{safe_method}-step{step or 0}",
            safe_page,
            safe_path_component(field, "field"),
            uuid.uuid4().hex[:8],
        ) if part]
        path = observations_dir / ("-".join(filename_parts) + f".{suffix}")
        output_format, query_with, outline = write_offloaded_blob(path, field, blob)
        node_count = (
            data.get("nodeCount")
            if isinstance(data, dict) and data.get("nodeCount") is not None
            else count_json_nodes(blob)
        )
        data[field] = {
            "_offloaded": True,
            "savedPath": str(path.resolve()),
            "format": output_format,
            "query_with": query_with,
            "originalBytes": byte_size,
            "byteSize": byte_size,
            "nodeCount": node_count,
            "outline": outline,
            "summary": outline,
        }
    return copied if copied is not None else response


def strip_image_payload(
    *,
    logger: RunLogger,
    method: str,
    response: JsonDict,
    artifacts: List[str],
    prefix: str = "",
) -> JsonDict:
    if method not in SCREENSHOT_METHODS:
        return response

    copied = copy.deepcopy(response)
    payload = copied.get("data")
    if not isinstance(payload, dict):
        return copied

    image_b64 = payload.get("data")
    if not isinstance(image_b64, str) or not image_b64:
        return copied

    artifacts_dir = logger.artifacts_dir
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    suffix = safe_path_component(payload.get("format") or "png", "png")
    filename_parts = [
        safe_path_component(prefix, "image") if prefix else "",
        safe_path_component(method.replace(".", "-"), "screenshot"),
        datetime.now().strftime("%Y%m%d-%H%M%S"),
        uuid.uuid4().hex[:6],
    ]
    filename = "-".join(part for part in filename_parts if part) + f".{suffix}"
    path = artifacts_dir / filename

    try:
        raw = base64.b64decode(image_b64)
        path.write_bytes(raw)
        artifact_path = str(path.resolve())
        artifacts.append(artifact_path)
        payload["savedPath"] = artifact_path
        payload["byteSize"] = len(raw)
    except (OSError, ValueError) as exc:
        payload["saveError"] = str(exc)

    payload.pop("data", None)
    payload["dataOmitted"] = True
    payload["omissionReason"] = (
        "image payload stored on disk; use visual_verify for bounded VL checks"
        " when configured"
    )
    return copied
