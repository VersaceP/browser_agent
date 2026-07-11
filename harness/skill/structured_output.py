"""Pure helpers for multi-row output from ordinary workflow skills.

``Workflow.execute`` variables are scalar, so a normal workflow can return a
collection without introducing another skill execution mode: its final
``Runtime.evaluate`` serializes rows with ``JSON.stringify`` and extracts that
string into a declared workflow variable.  The optional declaration lives in
``workflow.json``::

    {
      "structured_output": {
        "version": 1,
        "transport": "json_variable",
        "variable": "structuredRowsJson",
        "fields": ["rank", "productName", "productUrl"],
        "rank": {"field": "rank", "source": "dom_order", "base": 1},
        "window": {"source": "phase_validator", "field": "rank"}
      }
    }

The JSON string may encode either a row array or ``{"rows": [...]}``.  This
module only decodes and normalizes it; callers remain responsible for field
alignment, provenance enrichment, persistence, success-contract checks, and
the authoritative phase validators.
"""
from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from typing import Any, Dict, List, Optional, Tuple


StructuredRowsResult = Tuple[List[Dict[str, Any]], List[str]]

_VARIABLE_NAME_RE = re.compile(r"^[A-Za-z_$][A-Za-z0-9_$]*$")
_RANK_SOURCES = {"payload", "dom_order"}
_WINDOW_SOURCES = {"phase_validator"}


def structured_output_contract(skill: Any) -> Optional[Dict[str, Any]]:
    """Return a shallow copy of ``skill.workflow.structured_output``.

    ``None`` means the workflow has no structured-output declaration.  Registry
    code that needs to distinguish missing from a malformed non-object value
    should validate ``skill.workflow.get("structured_output")`` directly.
    """

    workflow = getattr(skill, "workflow", None)
    workflow = workflow if isinstance(workflow, Mapping) else {}
    raw = workflow.get("structured_output")
    return dict(raw) if isinstance(raw, Mapping) else None


def validate_structured_output_contract(raw: Any) -> Tuple[bool, List[str]]:
    """Validate an optional version-1 ``json_variable`` declaration.

    ``None`` is valid because structured output is optional for legacy/scalar
    workflows.  A present declaration is fail-closed: unsupported transports,
    rank semantics, and window semantics are reported rather than guessed.

    Supported rank modes are ``payload`` (rows already contain integer ranks)
    and ``dom_order`` (assign ``base + row_index``).  A
    ``phase_validator`` window requires the runtime caller to pass an inclusive
    or exclusive ``rank_window`` to :func:`structured_output_rows`.
    """

    if raw is None:
        return True, []
    if not isinstance(raw, Mapping):
        return False, ["structured_output must be an object"]

    failures: List[str] = []
    if raw.get("version") != 1:
        failures.append("structured_output.version must be 1")
    if raw.get("transport") != "json_variable":
        failures.append("structured_output.transport must be json_variable")

    variable = raw.get("variable")
    if not isinstance(variable, str) or not _VARIABLE_NAME_RE.fullmatch(
        variable.strip()
    ):
        failures.append(
            "structured_output.variable must be a simple workflow variable name"
        )

    fields = _validate_fields(raw.get("fields"), failures)

    rank = raw.get("rank")
    rank_field = ""
    if rank is not None:
        if not isinstance(rank, Mapping):
            failures.append("structured_output.rank must be an object")
        else:
            rank_field = str(rank.get("field") or "").strip()
            if not rank_field:
                failures.append("structured_output.rank.field must not be blank")
            elif rank_field not in fields:
                failures.append("structured_output.rank.field must appear in fields")
            source = str(rank.get("source") or "").strip()
            if source not in _RANK_SOURCES:
                failures.append(
                    "structured_output.rank.source must be payload or dom_order"
                )
            base = rank.get("base")
            if source == "dom_order":
                if not _is_int(base):
                    failures.append(
                        "structured_output.rank.base must be an integer for dom_order"
                    )
            elif base is not None:
                failures.append(
                    "structured_output.rank.base is only valid for dom_order"
                )

    window = raw.get("window")
    if window is not None:
        if not isinstance(window, Mapping):
            failures.append("structured_output.window must be an object")
        else:
            source = str(window.get("source") or "").strip()
            if source not in _WINDOW_SOURCES:
                failures.append(
                    "structured_output.window.source must be phase_validator"
                )
            window_field = str(window.get("field") or "").strip()
            if not window_field:
                failures.append("structured_output.window.field must not be blank")
            elif window_field not in fields:
                failures.append("structured_output.window.field must appear in fields")
            if rank is None:
                failures.append("structured_output.window requires rank")
            elif rank_field and window_field and window_field != rank_field:
                failures.append(
                    "structured_output.window.field must equal structured_output.rank.field"
                )
            inclusive = window.get("inclusive", True)
            if not isinstance(inclusive, bool):
                failures.append("structured_output.window.inclusive must be boolean")

    return not failures, failures


def validate_structured_output_workflow(
    raw: Any,
    workflow: Any,
) -> Tuple[bool, List[str]]:
    """Validate the contract plus its workflow-output cross-reference.

    The one-argument validator intentionally has no registry dependency.  Skill
    loading/recheck, which also has ``workflow.json``, should use this stronger
    helper so a typo cannot survive until the first live run.
    """

    valid, failures = validate_structured_output_contract(raw)
    failures = list(failures)
    if raw is None or not valid:
        return not failures, failures
    if not isinstance(workflow, Mapping):
        return False, failures + ["structured_output workflow must be an object"]
    variable = str(raw["variable"])
    produced = _workflow_output_variables(workflow.get("steps"))
    if variable not in produced:
        failures.append(
            f"structured_output.variable is not produced by a workflow step: {variable}"
        )
    return not failures, failures


def parse_structured_output_json(value: Any) -> Tuple[Any, List[str]]:
    """Decode one scalar Workflow variable containing structured-output JSON.

    Only strings are accepted.  Accepting a Python list/dict here would conceal
    a violation of the observed ``Workflow.execute`` scalar-variable contract
    and make dry-run behavior differ from live execution.
    """

    if not isinstance(value, str):
        return None, ["structured_output workflow variable must be a JSON string"]
    if not value.strip():
        return None, ["structured_output workflow variable must not be blank"]
    try:
        return json.loads(value), []
    except (TypeError, ValueError) as exc:
        return None, [f"structured_output workflow variable is not valid JSON: {exc}"]


def postprocess_structured_output_rows(
    payload: Any,
    raw_contract: Any,
    *,
    rank_window: Any = None,
) -> StructuredRowsResult:
    """Validate/copy rows, apply rank semantics, then apply a rank window.

    ``payload`` must already be decoded JSON and may be a list or an object with
    a ``rows`` list.  The function is pure: input mappings are never mutated.

    ``rank_window`` accepts ``(minimum, maximum)`` or
    ``{"min": minimum, "max": maximum}``.  It is required only when the
    contract declares ``window.source=phase_validator``.
    """

    valid, failures = validate_structured_output_contract(raw_contract)
    if not valid:
        return [], list(failures)
    if raw_contract is None:
        return [], ["structured_output is not configured"]
    contract = dict(raw_contract)

    candidate = payload
    if isinstance(payload, Mapping):
        candidate = payload.get("rows")
    if not isinstance(candidate, list):
        return [], ["structured_output JSON must be an array or an object with rows"]

    rows: List[Dict[str, Any]] = []
    for index, item in enumerate(candidate):
        if not isinstance(item, Mapping):
            failures.append(f"structured_output.rows[{index}] must be an object")
            continue
        rows.append(dict(item))
    if failures:
        return [], failures

    rank = contract.get("rank")
    rank = dict(rank) if isinstance(rank, Mapping) else None
    if rank:
        rank_field = str(rank["field"])
        if rank.get("source") == "dom_order":
            base = int(rank["base"])
            for index, row in enumerate(rows):
                row[rank_field] = base + index
        else:
            for index, row in enumerate(rows):
                value = _rank_int(row.get(rank_field))
                if value is None:
                    failures.append(
                        f"structured_output.rows[{index}].{rank_field} must be an integer"
                    )
                else:
                    row[rank_field] = value

    required_fields = [str(field) for field in contract.get("fields") or []]
    for index, row in enumerate(rows):
        for field in required_fields:
            if field not in row:
                failures.append(f"structured_output.rows[{index}] missing field: {field}")
                continue
            value = row.get(field)
            if value is None or (isinstance(value, str) and not value.strip()):
                failures.append(
                    f"structured_output.rows[{index}] has empty field: {field}"
                )
    if failures:
        return [], failures

    window = contract.get("window")
    if isinstance(window, Mapping):
        bounds, bounds_error = _normalize_rank_window(rank_window)
        if bounds_error:
            return [], [bounds_error]
        minimum, maximum = bounds
        field = str(window["field"])
        inclusive = bool(window.get("inclusive", True))
        selected: List[Dict[str, Any]] = []
        for row in rows:
            value = _rank_int(row.get(field))
            if value is None:
                # Normally caught above by rank validation; retain this guard so
                # future contract extensions cannot make windowing unsafe.
                return [], [f"structured_output window field {field} is not an integer"]
            within = (
                minimum <= value <= maximum
                if inclusive
                else minimum < value < maximum
            )
            if within:
                selected.append(row)
        rows = selected

    return rows, []


def structured_output_rows(
    skill: Any,
    run_result: Any,
    *,
    rank_window: Any = None,
) -> StructuredRowsResult:
    """Extract structured rows from a normalized workflow ``run_result``.

    The expected input is the dictionary returned by
    :func:`harness.skill.workflow.run_skill_workflow`.  Call this only after the
    workflow succeeds; failure snapshots may contain partial variables and
    must not be accepted as target data.
    """

    workflow = getattr(skill, "workflow", None)
    workflow = workflow if isinstance(workflow, Mapping) else {}
    raw = workflow.get("structured_output")
    valid, failures = validate_structured_output_workflow(raw, workflow)
    if not valid:
        return [], failures
    if raw is None:
        return [], ["structured_output is not configured"]
    if not isinstance(run_result, Mapping):
        return [], ["structured_output run_result must be an object"]
    if not run_result.get("succeeded"):
        return [], ["structured_output cannot be read from a failed workflow run"]

    variables = run_result.get("variables")
    if not isinstance(variables, Mapping):
        return [], ["structured_output run_result.variables must be an object"]
    variable_name = str(raw["variable"])
    if variable_name not in variables:
        return [], [f"structured_output variable is missing: {variable_name}"]

    payload, parse_failures = parse_structured_output_json(variables.get(variable_name))
    if parse_failures:
        return [], parse_failures
    return postprocess_structured_output_rows(
        payload,
        raw,
        rank_window=rank_window,
    )


def _validate_fields(value: Any, failures: List[str]) -> List[str]:
    if not isinstance(value, list):
        failures.append("structured_output.fields must be an array")
        return []
    fields = [str(item).strip() for item in value]
    if not fields:
        failures.append("structured_output.fields must not be empty")
    if any(not field for field in fields):
        failures.append("structured_output.fields cannot contain blank names")
    fields = [field for field in fields if field]
    if len(fields) != len(set(fields)):
        failures.append("structured_output.fields cannot contain duplicates")
    return fields


def _workflow_output_variables(steps: Any) -> set[str]:
    """Collect extract/transform outputs from nested Workflow steps."""

    outputs: set[str] = set()
    for step in steps if isinstance(steps, list) else []:
        if not isinstance(step, Mapping):
            continue
        extract = step.get("extract")
        if isinstance(extract, Mapping):
            outputs.update(str(name) for name in extract)
        output = step.get("output")
        if isinstance(output, str) and output.strip():
            outputs.add(output.strip())
        for branch in ("then", "else", "body"):
            outputs.update(_workflow_output_variables(step.get(branch)))
    return outputs


def _normalize_rank_window(value: Any) -> Tuple[Tuple[int, int], Optional[str]]:
    minimum: Any
    maximum: Any
    if isinstance(value, Mapping):
        minimum, maximum = value.get("min"), value.get("max")
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        if len(value) != 2:
            return (0, 0), "structured_output rank_window must contain min and max"
        minimum, maximum = value[0], value[1]
    else:
        return (0, 0), (
            "structured_output rank_window is required by the phase_validator window"
        )
    low, high = _rank_int(minimum), _rank_int(maximum)
    if low is None or high is None:
        return (0, 0), "structured_output rank_window min/max must be integers"
    if low > high:
        return (0, 0), "structured_output rank_window min must not exceed max"
    return (low, high), None


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _rank_int(value: Any) -> Optional[int]:
    if _is_int(value):
        return int(value)
    if isinstance(value, str) and re.fullmatch(r"[+-]?\d+", value.strip()):
        return int(value.strip())
    return None


__all__ = [
    "parse_structured_output_json",
    "postprocess_structured_output_rows",
    "structured_output_contract",
    "structured_output_rows",
    "validate_structured_output_contract",
    "validate_structured_output_workflow",
]
