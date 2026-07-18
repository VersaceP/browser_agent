"""
harness.tools.parsers - Tool argument parsers shared by browser and lead tools.
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
    Reads from the describeAction cache: schemas already arrive as
    structured dicts with method/description/params/requiresPurpose."""
    schema = method_schemas.get(method)
    if not isinstance(schema, dict):
        return None
    return trim_large_strings(
        {
            "method": schema.get("method", method),
            "description": schema.get("description", ""),
            "params": schema.get("params", {}),
            "requiresPurpose": schema.get("requiresPurpose", False),
            "purposeHint": schema.get("purposeHint"),
        },
        6000,
    )


def attach_method_schema(
    result: JsonDict,
    method: str,
    method_schemas: dict[str, JsonDict],
) -> None:
    schema = method_schema_summary(method_schemas, method)
    if schema:
        result["methodSchema"] = schema


def lead_tool_parse_error(
    agent: Any,
    tool_name: str,
    field: str,
    error: str,
    expected: str,
) -> JsonDict:
    result = {
        "status": "failed",
        "tool": tool_name,
        "field": field,
        "error": error,
        "expected": expected,
    }
    agent.logger.write("lead.tool.params_error", result)
    return result


def parse_json_text(
    agent: Any,
    tool_name: str,
    field: str,
    raw_value: Any,
    expected_type: Any,
    expected: str,
) -> Tuple[Any, Optional[JsonDict]]:
    if not isinstance(raw_value, str):
        return None, lead_tool_parse_error(
            agent,
            tool_name,
            field,
            f"{field} must be a string",
            expected,
        )
    try:
        parsed = json.loads(raw_value)
    except json.JSONDecodeError as exc:
        return None, lead_tool_parse_error(
            agent,
            tool_name,
            field,
            f"{field} is not valid JSON: {exc}",
            expected,
        )
    if not isinstance(parsed, expected_type):
        return None, lead_tool_parse_error(
            agent,
            tool_name,
            field,
            f"{field} parsed to the wrong type",
            expected,
        )
    return parsed, None


def parse_json_arg(
    agent: Any,
    tool_name: str,
    tool_input: JsonDict,
    field: str,
    expected_type: Any,
    expected: str,
) -> Tuple[Any, Optional[JsonDict]]:
    return parse_json_text(
        agent,
        tool_name,
        field,
        tool_input.get(field),
        expected_type,
        expected,
    )


def parse_plan_steps_arg(
    agent: Any,
    tool_name: str,
    tool_input: JsonDict,
) -> Tuple[List[JsonDict], Optional[JsonDict]]:
    raw_steps, error = parse_json_arg(
        agent,
        tool_name,
        tool_input,
        "steps_json",
        list,
        (
            "steps_json must be a JSON array string; each step contains "
            "method, params, and optionally save_as"
        ),
    )
    if error:
        return [], error

    normalized_steps: List[JsonDict] = []
    for index, step in enumerate(raw_steps, start=1):
        if not isinstance(step, dict):
            return [], lead_tool_parse_error(
                agent,
                tool_name,
                f"steps_json[{index}]",
                "step must be a JSON object",
                'step shape: {"method":"Page.navigate","params":{},"save_as":"..."}',
            )
        method = step.get("method")
        if not isinstance(method, str) or not method.strip():
            return [], lead_tool_parse_error(
                agent,
                tool_name,
                f"steps_json[{index}].method",
                "step.method must be a non-empty string",
                "An ABCP method string, e.g. Page.navigate",
            )
        if "params" not in step:
            return [], lead_tool_parse_error(
                agent,
                tool_name,
                f"steps_json[{index}].params",
                "step.params is required",
                "Every step.params must be a JSON object; pass {} when there are no params",
            )
        params = step.get("params")
        if not isinstance(params, dict):
            return [], lead_tool_parse_error(
                agent,
                tool_name,
                f"steps_json[{index}].params",
                "step.params must be a JSON object",
                "Every step.params must be a JSON object; pass {} when there are no params",
            )

        normalized_step: JsonDict = {
            "method": method.strip(),
            "params": params,
        }
        save_as = step.get("save_as")
        if save_as is not None:
            if not isinstance(save_as, str):
                return [], lead_tool_parse_error(
                    agent,
                    tool_name,
                    f"steps_json[{index}].save_as",
                    "step.save_as must be a string or null",
                    "Variable name string to save under, or null to use the default step name",
                )
            normalized_step["save_as"] = save_as
        normalized_steps.append(normalized_step)
    return normalized_steps, None
