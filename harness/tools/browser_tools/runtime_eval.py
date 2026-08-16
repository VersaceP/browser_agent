"""
harness.tools.browser_tools.runtime_eval - Runtime.evaluate helpers and trusted collection templates.
"""

import hashlib
from typing import Any
from typing import List
from typing import Optional
import json
from harness.runtime_evaluation import MAIN_WORLD_REQUIRED_PREFIX
from harness.utils import JsonDict

def _bt():
    import harness.tools.browser_tools as bt

    return bt

def _runtime_any_json_payload(result: JsonDict) -> Optional[Any]:
    values: List[Any] = []
    response = result.get("response") if isinstance(result, dict) else None
    data = response.get("data") if isinstance(response, dict) else None
    if isinstance(data, dict):
        values.extend([
            # Runtime.evaluate now returns a platform evidence envelope:
            # {value, runtimeEvaluation:{requestedWorld,executedWorld,...}}.
            # Unwrap value before considering legacy direct-object payloads.
            data.get("value"),
            data.get("result"),
            data.get("returnValue"),
            data,
        ])
    elif data is not None:
        values.append(data)
    for value in list(values):
        if isinstance(value, dict):
            values.extend([
                value.get("value"),
                value.get("result"),
                value.get("returnValue"),
            ])
    for value in values:
        if isinstance(value, str):
            try:
                return json.loads(value)
            except json.JSONDecodeError:
                continue
        if isinstance(value, (dict, list)):
            return value
    return None

async def _invoke_trusted_collection_template(
    agent: Any,
    *,
    template_id: str,
    bindings: JsonDict,
    page_id: str,
    step: int,
) -> Any:
    """Execute one registered, read-only ``collect_items`` template.

    This is the only harness-internal Runtime exception.  The caller cannot
    supply JavaScript: the verifier registry renders a fixed source template
    from JSON-encoded bindings, and this function hard-codes strict isolated
    execution. It intentionally does not require the model-facing platform
    world-evidence envelope; do not route model-authored scripts through this
    compatibility path. The payload is returned as JSON directly; the former
    document.title side channel is not restored.
    """
    from harness.observation.verifiers import render_trusted_collection_template

    try:
        rendered = render_trusted_collection_template(template_id, dict(bindings))
    except (TypeError, ValueError) as exc:
        return {
            "_oracle_error": str(exc),
            "_oracle_error_code": "trusted_collection_template_invalid",
        }
    expression = f"JSON.stringify(({rendered}))"
    digest = hashlib.sha256(expression.encode("utf-8")).hexdigest()
    logger = getattr(agent, "logger", None)
    if logger is not None:
        logger.write(
            "runtime.evaluate.trusted_collection_template",
            {
                "templateId": template_id,
                "expressionSha256": digest,
                "pageId": page_id,
                "bindingNames": sorted(bindings),
            },
        )
    result = await _bt()._invoke_browser_method(
        agent,
        "Runtime.evaluate",
        {
            "pageId": page_id,
            "expression": expression,
            "world": "isolated",
            "purpose": f"collect_items fixed read-only template: {template_id}",
        },
        step,
        count_progress=False,
        read_only_eval=True,
        internal=True,
        _trusted_collection_runtime_token=_bt()._TRUSTED_COLLECTION_RUNTIME_TOKEN,
    )
    if _bt()._invoke_result_failed(result):
        error_text = str(
            result.get("error")
            or ((result.get("response") or {}).get("error") if isinstance(result.get("response"), dict) else "")
            or "trusted collection template execution failed"
        )[:500]
        normalized = error_text.casefold()
        error_code = (
            "stealth_probe_unavailable"
            if "stealthprobe is unavailable" in normalized
            else "stealth_probe_timeout"
            if "stealthprobe" in normalized and "timed out" in normalized
            else "trusted_collection_runtime_failed"
        )
        return {
            "_oracle_error": error_text,
            "_oracle_error_code": error_code,
        }
    payload = _runtime_any_json_payload(result)
    if payload is None:
        return {
            "_oracle_error": "trusted collection template returned no JSON payload",
            "_oracle_error_code": "trusted_collection_payload_invalid",
        }
    return payload

def _build_runtime_json_expression(expression: str) -> str:
    expression_json = json.dumps(expression)
    return f"""
(async () => {{
  const __abcpExpression = {expression_json};
  const __abcpValue = (0, eval)("(" + __abcpExpression + ")");
  const __abcpResolved = (
    __abcpValue && typeof __abcpValue.then === "function"
  ) ? await __abcpValue : __abcpValue;
  return JSON.stringify({{ value: __abcpResolved }});
}})()
"""

def _runtime_evaluation_error_text(result: JsonDict) -> str:
    if not isinstance(result, dict):
        return "Runtime.evaluate failed"
    if result.get("error"):
        return str(result.get("error"))
    response = result.get("response")
    if isinstance(response, dict):
        if response.get("error"):
            return str(response.get("error"))
        data = response.get("data")
        if isinstance(data, dict) and data.get("error"):
            return str(data.get("error"))
    return "Runtime.evaluate failed without an error message"

def _runtime_execution_metadata(response: Any) -> JsonDict:
    """Read platform-issued world evidence from a Runtime.evaluate response."""
    if not isinstance(response, dict):
        return {}
    data = response.get("data")
    if not isinstance(data, dict):
        nested = response.get("response")
        data = nested.get("data") if isinstance(nested, dict) else None
    metadata = data.get("runtimeEvaluation") if isinstance(data, dict) else None
    return dict(metadata) if isinstance(metadata, dict) else {}

def _runtime_response_world_metadata_supplied(response: Any) -> bool:
    """Whether the platform attempted to supply its world-evidence envelope.

    Presence is kept separate from validity: a legacy response with no field may
    use degraded harness dispatch evidence, while a present but malformed field
    must fail closed instead of being mistaken for legacy compatibility.
    """
    if not isinstance(response, dict):
        return False
    data = response.get("data")
    if not isinstance(data, dict):
        nested = response.get("response")
        data = nested.get("data") if isinstance(nested, dict) else None
    return isinstance(data, dict) and "runtimeEvaluation" in data

def _runtime_attempt_receipt(response: Any, requested_world: str) -> JsonDict:
    metadata = _runtime_execution_metadata(response)
    metadata_supplied = _runtime_response_world_metadata_supplied(response)
    failed = _bt()._invoke_result_failed(
        {"method": "Runtime.evaluate", "response": response}
        if isinstance(response, dict) and "response" not in response
        else response
    )
    receipt = {
        "requestedWorld": requested_world,
        "executedWorld": str(metadata.get("executedWorld") or "") or None,
        "status": "failed" if failed else "done",
        "evidence": (
            "platform_response"
            if metadata
            else "platform_response_invalid"
            if metadata_supplied
            else "harness_dispatched_world"
        ),
        **(
            {"fallbackReason": str(metadata.get("fallbackReason"))}
            if metadata.get("fallbackReason") else {}
        ),
        **(
            {"error": _runtime_evaluation_error_text({"response": response})[:500]}
            if failed else {}
        ),
    }
    if not metadata_supplied:
        receipt["dispatchedWorld"] = requested_world
        receipt["evidenceStrength"] = "degraded"
    elif not metadata:
        receipt["evidenceStrength"] = "invalid"
    return receipt

def _runtime_response_world_verified(response: Any, expected_world: str) -> bool:
    metadata = _runtime_execution_metadata(response)
    return (
        str(metadata.get("requestedWorld") or "") == expected_world
        and str(metadata.get("executedWorld") or "") == expected_world
    )

def _runtime_main_fallback_signaled(response: Any) -> bool:
    return MAIN_WORLD_REQUIRED_PREFIX in _runtime_evaluation_error_text(
        {"response": response}
    )

def _rows_from_eval_value(value: Any) -> Optional[List[JsonDict]]:
    if isinstance(value, list) and all(isinstance(item, dict) for item in value):
        return value
    if isinstance(value, dict):
        rows = value.get("rows")
        if isinstance(rows, list) and all(isinstance(item, dict) for item in rows):
            return rows
    return None

def _attach_runtime_json_value(
    agent: Any,
    result: JsonDict,
    value: Any,
    runtime_receipt: JsonDict,
    *,
    step: int,
) -> None:
    """Attach JSON-mode Runtime output and preserve extraction guarantees.

    Keep the raw value available for diagnostics, but make a non-row
    ``recordName`` contract failure explicit and apply the unrecorded-row gate.
    """
    result["runtimeValue"] = value
    result["runtimeValueType"] = type(value).__name__
    record_name = str(runtime_receipt.get("recordName") or "").strip()
    rows = _rows_from_eval_value(value)
    if record_name:
        if rows is None:
            message = (
                "record_name was provided, but Runtime.evaluate returned neither"
                " a list of objects nor an object with rows=[...]"
            )
            result["runtimeJSONError"] = {
                "code": "runtime_record_value_not_rows",
                "error": message,
            }
            result["recordExtraction"] = {
                "status": "failed",
                "error": message,
                "tool_was_executed": False,
            }
            return
        record_result = _bt()._record_extraction(
            agent,
            {
                "name": record_name,
                "rows": rows,
                "schema": {"source": "Runtime.evaluate"},
                "description": "Rows extracted by Runtime.evaluate",
            },
        )
        result["recordExtraction"] = record_result
        if _bt()._record_extraction_persisted(record_result):
            agent.pending_unrecorded_extraction = None
        return
    if rows:
        agent.pending_unrecorded_extraction = {
            "source": "Runtime.evaluate",
            "step": step,
            "rowCount": len(rows),
            "turns": 0,
        }
