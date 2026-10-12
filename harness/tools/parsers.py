"""
harness.tools.parsers - Browser tool argument parsers and schema annotations.
"""

import json
from typing import Any, List, Optional, Set, Tuple

from harness.utils import JsonDict, trim_large_strings


def parse_browser_call_params(tool_input: JsonDict) -> Tuple[JsonDict, Optional[str]]:
    if "params" not in tool_input:
        return {}, "browser_call.params is required; pass {} when there are no params"
    params = tool_input.get("params")
    if not isinstance(params, dict):
        return {}, "browser_call.params must be a JSON object"
    return params, None


def parse_direct_capability_params(tool_input: JsonDict) -> Tuple[JsonDict, Optional[str]]:
    if not isinstance(tool_input, dict):
        return {}, "direct capability tool input must be a JSON object"
    if "params" in tool_input:
        params = tool_input.get("params")
        if not isinstance(params, dict):
            return {}, "direct capability params must be a JSON object"
        return params, None
    return {
        key: value
        for key, value in tool_input.items()
        if key not in {"method", "reason", "runtime_policy"}
    }, None


def ensure_required_purpose(
    methods_requiring_purpose: Set[str],
    method: str,
    params: JsonDict,
    reason: str,
    purpose_hints: Optional[dict[str, str]] = None,
) -> bool:
    if method not in methods_requiring_purpose:
        return False
    purpose = params.get("purpose")
    if isinstance(purpose, str) and purpose.strip():
        return False
    if reason:
        params["purpose"] = reason
    elif purpose_hints and purpose_hints.get(method):
        params["purpose"] = purpose_hints[method]
    else:
        params["purpose"] = f"Invoke {method} to advance the current browser task"
    return True


def method_schema_summary(
    method_schemas: dict[str, JsonDict],
    method: str,
) -> Optional[JsonDict]:
    """Return a compact schema summary for tool_result error annotations.
    Reads from the describeAction cache, whose entries carry method,
    description, requiresPurpose and the input/result/output/failure
    schemas."""
    schema = method_schemas.get(method)
    if not isinstance(schema, dict):
        return None
    summary: JsonDict = {
        "method": schema.get("method", method),
        "description": schema.get("description", ""),
        "requiresPurpose": schema.get("requiresPurpose", False),
        "purposeHint": schema.get("purposeHint"),
    }
    # `inputSchema` is what a caller fixes a rejected call against; it may be a
    # union, so it is surfaced whole rather than flattened here.
    if isinstance(schema.get("inputSchema"), dict):
        summary["inputSchema"] = schema.get("inputSchema")
    return trim_large_strings(summary, 6000)


def attach_method_schema(
    result: JsonDict,
    method: str,
    method_schemas: dict[str, JsonDict],
) -> None:
    schema = method_schema_summary(method_schemas, method)
    if schema:
        result["methodSchema"] = schema
