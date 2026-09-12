"""Shared preparation and validation for model-authored tool arguments.

The provider's ``strict`` flag is useful guidance for a model API, but it is
not an execution boundary: compatible gateways may ignore it and lifecycle
middleware can still alter a call.  This module is the local execution
boundary.  It deliberately performs only lossless preparation (copying
schema-declared defaults); it never guesses handles, URLs, or user data.
"""

from __future__ import annotations

import copy
import json
import re
import uuid
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple
from urllib.parse import urlparse

from harness.tools.registry import ToolRegistry
from harness.utils import JsonDict


@dataclass(frozen=True)
class SchemaIssue:
    path: Tuple[str, ...]
    keyword: str
    message: str

    def as_dict(self) -> JsonDict:
        return {
            "path": _display_path(self.path),
            "keyword": self.keyword,
            "message": self.message,
        }


def prepare_model_tool_call(tool_call: Any) -> Tuple[Optional[JsonDict], List[str], Optional[str]]:
    """Copy a model tool call and turn a missing/null input into an object.

    Providers normally guarantee an object, but normalising this one harmless
    shape here keeps every downstream dispatcher on the same contract.  Other
    type repairs are intentionally forbidden: coercing ``"3"`` to ``3`` hides
    model errors and can change the meaning of a tool call.
    """
    if not isinstance(tool_call, dict):
        return None, [], "tool call must be an object"
    prepared = dict(tool_call)
    normalized: List[str] = []
    if "input" not in prepared or prepared.get("input") is None:
        prepared["input"] = {}
        normalized.append("input")
    return prepared, normalized, None


def validate_registered_tool_call(
    registry: ToolRegistry,
    tool_call: Any,
    *,
    schema_context: Optional[Any] = None,
) -> List[SchemaIssue]:
    """Validate a registered harness tool's input schema.

    Unknown names deliberately return no issue so the existing dispatcher can
    retain its richer unknown-tool/capability hint.  Direct ABCP method names
    are checked later against their method schema.
    """
    if not isinstance(tool_call, dict):
        return [SchemaIssue((), "type", "tool call must be an object")]
    name = str(tool_call.get("name") or "").strip()
    action = registry.get(name)
    if action is None:
        return []
    value = tool_call.get("input")
    if not isinstance(value, dict):
        return [SchemaIssue(("input",), "type", "tool input must be an object")]
    schema = action.input_schema(schema_context) if callable(action.input_schema) else action.input_schema
    return validate_schema(value, schema)


def apply_registered_tool_defaults(
    registry: ToolRegistry,
    tool_call: Any,
    *,
    schema_context: Optional[Any] = None,
) -> Tuple[Any, List[str]]:
    """Apply required defaults declared by a registered harness tool schema."""
    if not isinstance(tool_call, dict):
        return tool_call, []
    name = str(tool_call.get("name") or "").strip()
    action = registry.get(name)
    value = tool_call.get("input")
    if action is None or not isinstance(value, dict):
        return tool_call, []
    schema = action.input_schema(schema_context) if callable(action.input_schema) else action.input_schema
    prepared_input, defaulted = apply_required_schema_defaults(value, schema)
    if not defaulted:
        return tool_call, []
    prepared_call = dict(tool_call)
    prepared_call["input"] = prepared_input
    return prepared_call, defaulted


def capability_input_schema(method_schemas: Any, method: str) -> Optional[JsonDict]:
    if not isinstance(method_schemas, dict):
        return None
    descriptor = method_schemas.get(method)
    if not isinstance(descriptor, dict):
        return None
    schema = descriptor.get("inputSchema")
    return schema if isinstance(schema, dict) else None


def apply_required_schema_defaults(value: JsonDict, schema: Optional[JsonDict]) -> Tuple[JsonDict, List[str]]:
    """Apply only defaults for schema-required fields, recursively.

    Some live ABCP schemas mark server-defaulted fields as required.  Applying
    those declared defaults before validation preserves the contract without
    inventing a business value for an optional field.
    """
    prepared = copy.deepcopy(value)
    applied: List[str] = []
    if isinstance(schema, dict):
        _apply_defaults(prepared, schema, schema, (), applied)
    return prepared, applied


def validate_schema(value: Any, schema: Any) -> List[SchemaIssue]:
    """Validate the JSON-Schema vocabulary used by ABCP descriptors.

    This is intentionally dependency-free because the harness's supported
    runtime only declares transport/model dependencies.  It implements the
    descriptor vocabulary present in ``inputSchema``: references, composition,
    scalar constraints, objects, arrays, and the UUID/URI formats used by the
    protocol.  A bounded issue list keeps bad model output from bloating the
    next context window.
    """
    if not isinstance(schema, dict):
        return []
    issues: List[SchemaIssue] = []
    _validate(value, schema, schema, (), issues)
    return issues[:12]


def tool_argument_error(
    tool_call: Any,
    issues: Sequence[SchemaIssue],
    *,
    stage: str,
    normalized_fields: Iterable[str] = (),
    method: str = "",
) -> JsonDict:
    name = str(tool_call.get("name") or "") if isinstance(tool_call, dict) else ""
    result: JsonDict = {
        "isError": True,
        "status": "invalid_tool_arguments",
        "stage": stage,
        "tool": name or "unknown",
        "error": "Tool arguments do not satisfy the declared schema.",
        "issues": [issue.as_dict() for issue in issues],
        "tool_was_executed": False,
    }
    fields = list(normalized_fields)
    if fields:
        result["preparedFields"] = fields
    if method:
        result["method"] = method
    return result


def capability_argument_error(
    tool_name: str,
    method: str,
    issues: Sequence[SchemaIssue],
    *,
    normalized_fields: Iterable[str] = (),
) -> JsonDict:
    result = tool_argument_error(
        {"name": tool_name},
        issues,
        stage="validate_capability_arguments",
        normalized_fields=normalized_fields,
        method=method,
    )
    result["status"] = "invalid_params"
    result["error"] = "Browser method parameters do not satisfy the live method schema."
    return result


def _apply_defaults(
    value: Any,
    schema: Any,
    root: JsonDict,
    path: Tuple[str, ...],
    applied: List[str],
) -> None:
    if not isinstance(schema, dict):
        return
    resolved = _resolve_ref(schema, root)
    if resolved is not schema:
        _apply_defaults(value, resolved, root, path, applied)
    if isinstance(value, list):
        _apply_item_defaults(value, schema, root, path, applied)
        return
    if not isinstance(value, dict):
        return

    all_of = schema.get("allOf")
    if isinstance(all_of, list):
        for child in all_of:
            _apply_defaults(value, child, root, path, applied)

    condition = schema.get("if")
    if isinstance(condition, dict):
        branch = schema.get("then") if _matches_schema(value, condition, root) else schema.get("else")
        if branch is not None:
            _apply_defaults(value, branch, root, path, applied)

    any_of = schema.get("anyOf")
    if isinstance(any_of, list):
        _apply_composed_defaults(
            value, any_of, root, path, applied, require_unique_match=False
        )
    one_of = schema.get("oneOf")
    if isinstance(one_of, list):
        _apply_composed_defaults(
            value, one_of, root, path, applied, require_unique_match=True
        )

    properties = schema.get("properties")
    required = set(schema.get("required") or [])
    if not isinstance(properties, dict):
        return
    for key, child_schema in properties.items():
        if key not in value and key in required and isinstance(child_schema, dict) and "default" in child_schema:
            value[key] = copy.deepcopy(child_schema["default"])
            applied.append(_display_path(path + (str(key),)))
        if key in value:
            _apply_defaults(value[key], child_schema, root, path + (str(key),), applied)


def _apply_item_defaults(
    value: List[Any],
    schema: JsonDict,
    root: JsonDict,
    path: Tuple[str, ...],
    applied: List[str],
) -> None:
    """Descend into array elements so item shapes can supply their defaults.

    The recursion used to stop at the array: only object properties were ever
    visited, so a field an item shape declares as required-with-a-default was
    never filled and the element failed validation the platform would have
    accepted.  Run a686e03f step 29: every step of an
    ``execute_browser_workflow`` segment omitted ``onError``, which the live
    ``Workflow.execute`` step union lists in ``required`` while carrying a
    ``default`` -- the Zod ``.default()`` artifact already documented in
    ``workflow_schema_source._effective_required``.  All four steps were
    rejected here as ``anyOf`` misfits, and 16KB of schema error went back to
    the model for a call the platform would have run.  This is not specific to
    workflows: it applies wherever a live schema defaults a required field
    inside an array.
    """
    items = schema.get("items")
    prefix = schema.get("prefixItems")
    consumed = 0
    if isinstance(prefix, list):
        for index, child_schema in enumerate(prefix[: len(value)]):
            _apply_defaults(
                value[index], child_schema, root, path + (str(index),), applied
            )
        consumed = len(prefix)
    elif isinstance(items, list):
        # Draft-04/07 tuple form: ``items`` is the positional list itself.
        for index, child_schema in enumerate(items[: len(value)]):
            _apply_defaults(
                value[index], child_schema, root, path + (str(index),), applied
            )
        consumed = len(items)
        items = schema.get("additionalItems")
    if not isinstance(items, dict):
        return
    for index in range(consumed, len(value)):
        _apply_defaults(value[index], items, root, path + (str(index),), applied)


def _apply_composed_defaults(
    value: JsonDict,
    branches: Sequence[Any],
    root: JsonDict,
    path: Tuple[str, ...],
    applied: List[str],
    *,
    require_unique_match: bool,
) -> None:
    """Apply defaults from a composed shape without selecting it by guesswork.

    A required default can make an otherwise valid branch complete.  For an
    ``anyOf`` with more than one valid branch, only defaults with the same value
    in every viable branch are safe to apply.  A ``oneOf`` is safe only when
    exactly one branch is viable after its own declared defaults.
    """
    candidates: List[JsonDict] = []
    for branch in branches:
        candidate = copy.deepcopy(value)
        branch_applied: List[str] = []
        _apply_defaults(candidate, branch, root, path, branch_applied)
        if _matches_schema(candidate, branch, root):
            candidates.append(candidate)
    if not candidates or (require_unique_match and len(candidates) != 1):
        return
    if require_unique_match or len(candidates) == 1:
        _apply_missing_defaults(value, [candidates[0]], path, applied)
        return
    _apply_missing_defaults(value, candidates, path, applied)


def _apply_missing_defaults(
    value: JsonDict,
    candidates: Sequence[JsonDict],
    path: Tuple[str, ...],
    applied: List[str],
) -> None:
    """Copy missing values only when every candidate supplies the same value."""
    if not candidates or not all(isinstance(candidate, dict) for candidate in candidates):
        return
    first = candidates[0]
    for key, candidate_value in first.items():
        candidate_values = [candidate.get(key) for candidate in candidates]
        if key not in value:
            if not all(key in candidate for candidate in candidates):
                continue
            if not all(_json_equal(candidate_value, item) for item in candidate_values[1:]):
                continue
            value[key] = copy.deepcopy(candidate_value)
            applied.append(_display_path(path + (str(key),)))
        elif isinstance(value[key], dict) and all(
            isinstance(item, dict) for item in candidate_values
        ):
            _apply_missing_defaults(
                value[key], candidate_values, path + (str(key),), applied
            )


def _validate(
    value: Any,
    schema: Any,
    root: JsonDict,
    path: Tuple[str, ...],
    issues: List[SchemaIssue],
) -> None:
    if len(issues) >= 12 or schema is True or schema is None:
        return
    if schema is False:
        _issue(issues, path, "falseSchema", "value is not allowed")
        return
    if not isinstance(schema, dict):
        return

    resolved = _resolve_ref(schema, root)
    if resolved is not schema:
        _validate(value, resolved, root, path, issues)
        if "$ref" in schema:
            schema = {key: item for key, item in schema.items() if key != "$ref"}

    if "const" in schema and not _json_equal(value, schema["const"]):
        _issue(issues, path, "const", "must equal the declared constant")
    if isinstance(schema.get("enum"), list) and not any(_json_equal(value, item) for item in schema["enum"]):
        _issue(issues, path, "enum", "must be one of the declared values")

    expected_type = schema.get("type")
    if expected_type is not None:
        types = expected_type if isinstance(expected_type, list) else [expected_type]
        if not any(_matches_type(value, item) for item in types):
            names = ", ".join(str(item) for item in types)
            _issue(issues, path, "type", f"must be of type {names}")
            return

    all_of = schema.get("allOf")
    if isinstance(all_of, list):
        for child in all_of:
            _validate(value, child, root, path, issues)

    any_of = schema.get("anyOf")
    if isinstance(any_of, list) and not _matches_any(value, any_of, root):
        _append_closest_branch_issues(issues, value, any_of, root, path, "anyOf")
    one_of = schema.get("oneOf")
    if isinstance(one_of, list):
        matches = sum(1 for child in one_of if _matches_schema(value, child, root))
        if matches == 0:
            _append_closest_branch_issues(issues, value, one_of, root, path, "oneOf")
        elif matches != 1:
            _issue(issues, path, "oneOf", "must satisfy exactly one allowed shape")
    not_schema = schema.get("not")
    if isinstance(not_schema, dict) and _matches_schema(value, not_schema, root):
        _issue(issues, path, "not", "matches a forbidden shape")
    condition = schema.get("if")
    if isinstance(condition, dict):
        branch = schema.get("then") if _matches_schema(value, condition, root) else schema.get("else")
        if branch is not None:
            _validate(value, branch, root, path, issues)

    if isinstance(value, str):
        _validate_string(value, schema, path, issues)
    if _is_number(value):
        _validate_number(value, schema, path, issues)
    if isinstance(value, dict):
        _validate_object(value, schema, root, path, issues)
    if isinstance(value, list):
        _validate_array(value, schema, root, path, issues)


def _validate_string(value: str, schema: JsonDict, path: Tuple[str, ...], issues: List[SchemaIssue]) -> None:
    minimum = schema.get("minLength")
    maximum = schema.get("maxLength")
    if isinstance(minimum, int) and len(value) < minimum:
        _issue(issues, path, "minLength", f"must be at least {minimum} characters")
    if isinstance(maximum, int) and len(value) > maximum:
        _issue(issues, path, "maxLength", f"must be at most {maximum} characters")
    pattern = schema.get("pattern")
    if isinstance(pattern, str):
        try:
            matched = re.search(pattern, value) is not None
        except re.error:
            matched = True  # an invalid server descriptor must not reject a model call
        if not matched:
            _issue(issues, path, "pattern", "does not match the required pattern")
    fmt = schema.get("format")
    if isinstance(fmt, str) and not _matches_format(value, fmt):
        _issue(issues, path, "format", f"must match format {fmt}")


def _validate_number(value: Any, schema: JsonDict, path: Tuple[str, ...], issues: List[SchemaIssue]) -> None:
    minimum = schema.get("minimum")
    maximum = schema.get("maximum")
    exclusive_minimum = schema.get("exclusiveMinimum")
    exclusive_maximum = schema.get("exclusiveMaximum")
    if _is_number(minimum) and value < minimum:
        _issue(issues, path, "minimum", f"must be at least {minimum}")
    if _is_number(maximum) and value > maximum:
        _issue(issues, path, "maximum", f"must be at most {maximum}")
    if _is_number(exclusive_minimum) and value <= exclusive_minimum:
        _issue(issues, path, "exclusiveMinimum", f"must be greater than {exclusive_minimum}")
    if _is_number(exclusive_maximum) and value >= exclusive_maximum:
        _issue(issues, path, "exclusiveMaximum", f"must be less than {exclusive_maximum}")


def _validate_object(value: JsonDict, schema: JsonDict, root: JsonDict, path: Tuple[str, ...], issues: List[SchemaIssue]) -> None:
    required = schema.get("required")
    if isinstance(required, list):
        for key in required:
            if isinstance(key, str) and key not in value:
                _issue(issues, path + (key,), "required", "is required")
    properties = schema.get("properties") if isinstance(schema.get("properties"), dict) else {}
    property_names = schema.get("propertyNames")
    additional = schema.get("additionalProperties", True)
    for key, item in value.items():
        child_path = path + (str(key),)
        if isinstance(property_names, dict):
            _validate(str(key), property_names, root, child_path, issues)
        if key in properties:
            _validate(item, properties[key], root, child_path, issues)
        elif additional is False:
            _issue(issues, child_path, "additionalProperties", "is not an allowed property")
        elif isinstance(additional, dict):
            _validate(item, additional, root, child_path, issues)


def _validate_array(value: List[Any], schema: JsonDict, root: JsonDict, path: Tuple[str, ...], issues: List[SchemaIssue]) -> None:
    minimum = schema.get("minItems")
    maximum = schema.get("maxItems")
    if isinstance(minimum, int) and len(value) < minimum:
        _issue(issues, path, "minItems", f"must contain at least {minimum} items")
    if isinstance(maximum, int) and len(value) > maximum:
        _issue(issues, path, "maxItems", f"must contain at most {maximum} items")
    if schema.get("uniqueItems") is True:
        seen = set()
        for item in value:
            encoded = json.dumps(item, sort_keys=True, ensure_ascii=False, default=str)
            if encoded in seen:
                _issue(issues, path, "uniqueItems", "must not contain duplicate items")
                break
            seen.add(encoded)
    items = schema.get("items")
    if isinstance(items, dict):
        for index, item in enumerate(value):
            _validate(item, items, root, path + (str(index),), issues)
    elif isinstance(items, list):
        for index, item in enumerate(value):
            if index < len(items):
                _validate(item, items[index], root, path + (str(index),), issues)


def _matches_schema(value: Any, schema: Any, root: JsonDict) -> bool:
    return not _collect_issues(value, schema, root)


def _matches_any(value: Any, schemas: Sequence[Any], root: JsonDict) -> bool:
    return any(_matches_schema(value, child, root) for child in schemas)


def _append_closest_branch_issues(
    issues: List[SchemaIssue],
    value: Any,
    branches: Sequence[Any],
    root: JsonDict,
    path: Tuple[str, ...],
    keyword: str,
) -> None:
    """Expose the least-failing composition branch as actionable feedback."""
    candidates: List[Tuple[int, int, int, List[SchemaIssue]]] = []
    for index, branch in enumerate(branches):
        branch_issues: List[SchemaIssue] = []
        _validate(value, branch, root, path, branch_issues)
        visible_issues = [
            issue
            for issue in branch_issues
            if not _is_defaulted_required_issue(issue, branch, root, path)
        ]
        # A bad supplied value identifies the intended shape better than a
        # wholly absent required field.  Prefer it before considering the
        # total issue count; branch index is only a deterministic tie-breaker.
        required_count = sum(
            issue.keyword == "required" for issue in visible_issues
        )
        candidates.append((required_count, len(visible_issues), index, visible_issues))
    if not candidates:
        _issue(issues, path, keyword, "must satisfy an allowed shape")
        return
    _, _, _, closest = min(candidates, key=lambda item: item[:3])
    if not closest:
        _issue(issues, path, keyword, "must satisfy an allowed shape")
        return
    for issue in closest:
        if len(issues) >= 12:
            return
        if issue not in issues:
            issues.append(issue)


def _is_defaulted_required_issue(
    issue: SchemaIssue,
    branch: Any,
    root: JsonDict,
    branch_path: Tuple[str, ...],
) -> bool:
    """Hide a missing required field the preparation stage will supply.

    This applies only to a direct property of the composed branch.  Deeper
    paths can have conditional meaning, so keeping them visible is safer than
    inferring a default through an unselected nested shape.
    """
    if (
        issue.keyword != "required"
        or issue.path[:len(branch_path)] != branch_path
        or len(issue.path) != len(branch_path) + 1
    ):
        return False
    property_name = issue.path[-1]
    return _branch_property_has_default(branch, property_name, root)


def _branch_property_has_default(
    schema: Any,
    property_name: str,
    root: JsonDict,
) -> bool:
    if not isinstance(schema, dict):
        return False
    resolved = _resolve_ref(schema, root)
    if resolved is not schema and _branch_property_has_default(
        resolved, property_name, root
    ):
        return True
    properties = schema.get("properties")
    property_schema = properties.get(property_name) if isinstance(properties, dict) else None
    if isinstance(property_schema, dict) and "default" in property_schema:
        return True
    all_of = schema.get("allOf")
    return isinstance(all_of, list) and any(
        _branch_property_has_default(child, property_name, root)
        for child in all_of
    )


def _collect_issues(value: Any, schema: Any, root: JsonDict) -> List[SchemaIssue]:
    found: List[SchemaIssue] = []
    _validate(value, schema, root, (), found)
    return found


def _resolve_ref(schema: JsonDict, root: JsonDict) -> JsonDict:
    reference = schema.get("$ref")
    if not isinstance(reference, str) or not reference.startswith("#/"):
        return schema
    current: Any = root
    for token in reference[2:].split("/"):
        if not isinstance(current, dict):
            return schema
        current = current.get(token.replace("~1", "/").replace("~0", "~"))
    return current if isinstance(current, dict) else schema


def _matches_type(value: Any, name: Any) -> bool:
    if name == "object":
        return isinstance(value, dict)
    if name == "array":
        return isinstance(value, list)
    if name == "string":
        return isinstance(value, str)
    if name == "boolean":
        return isinstance(value, bool)
    if name == "null":
        return value is None
    if name == "number":
        return _is_number(value)
    if name == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    return True


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _json_equal(left: Any, right: Any) -> bool:
    return type(left) is type(right) and left == right


def _matches_format(value: str, fmt: str) -> bool:
    if fmt == "uuid":
        try:
            uuid.UUID(value)
            return True
        except (ValueError, AttributeError):
            return False
    if fmt in {"uri", "uri-reference"}:
        parsed = urlparse(value)
        return bool(parsed.scheme) if fmt == "uri" else bool(value)
    return True


def _issue(issues: List[SchemaIssue], path: Tuple[str, ...], keyword: str, message: str) -> None:
    if len(issues) < 12:
        issues.append(SchemaIssue(path, keyword, message))


def _display_path(path: Tuple[str, ...]) -> str:
    return ".".join(path) if path else "input"
