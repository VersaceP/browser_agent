"""Immutable, task-scoped Workflow definitions for short reuse requests."""

from __future__ import annotations

import copy
import hashlib
import json
from typing import Any, Dict, List, Optional, Tuple

from harness.tool_policy import (
    SENSITIVE_URL_QUERY_KEYS,
    collect_sensitive_replacements,
    redact_values,
)
from harness.utils import JsonDict, storage_for_logger


WORKFLOW_DEFINITION_PROTOCOL = "workflow-definition-v2"
WORKFLOW_DEFINITION_TYPE = "workflow_definition"
MAX_PATCH_OPERATIONS = 64


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        default=str,
    )


def _envelope_hash(envelope: JsonDict) -> str:
    """Bind executable content and every rebinding constraint to one identity."""
    return hashlib.sha256(_canonical_json({
        key: envelope[key] for key in (
            "protocol", "definition", "executable",
            "requiresSensitiveRebinding", "requiredVariableNames",
        )
    }).encode("utf-8")).hexdigest()


def _stored_definition(
    definition: JsonDict,
    *,
    required_variable_names: Optional[List[str]] = None,
) -> Tuple[JsonDict, bool]:
    payload = {
        "description": str(definition.get("description") or ""),
        "variables": copy.deepcopy(definition.get("variables") or {}),
        "steps": copy.deepcopy(definition.get("steps") or []),
    }
    replacements = collect_sensitive_replacements(
        payload, set(SENSITIVE_URL_QUERY_KEYS),
    )
    scrubbed = redact_values(payload, replacements) if replacements else payload
    sensitive_variable_names = sorted({
        *(
            str(name) for name, value in payload["variables"].items()
            if scrubbed["variables"].get(name) != value
        ),
        *(
            str(name) for name in (required_variable_names or [])
            if str(name).strip()
        ),
    })
    sensitive_outside_variables = bool(
        scrubbed["description"] != payload["description"]
        or scrubbed["steps"] != payload["steps"]
    )
    if sensitive_variable_names and not sensitive_outside_variables:
        # Keep only the variable names in the reusable definition. The actual
        # values must be supplied again through the normal authorized tool call.
        for name in sensitive_variable_names:
            scrubbed["variables"].pop(name, None)
    sensitive = sensitive_outside_variables
    envelope = {
        "protocol": WORKFLOW_DEFINITION_PROTOCOL,
        "definition": scrubbed,
        "executable": not sensitive,
        "requiresSensitiveRebinding": bool(
            sensitive or sensitive_variable_names
        ),
        "requiredVariableNames": sensitive_variable_names,
    }
    envelope["definitionHash"] = _envelope_hash(envelope)
    return envelope, sensitive


def save_workflow_definition(
    logger: Any,
    definition: JsonDict,
    *,
    required_variable_names: Optional[List[str]] = None,
) -> JsonDict:
    """Save before dispatch; credential-bearing copies remain non-executable."""
    envelope, sensitive = _stored_definition(
        definition, required_variable_names=required_variable_names,
    )
    digest = str(envelope["definitionHash"])
    storage, task_id = storage_for_logger(logger)
    stored = storage.save_resource(
        task_id=task_id,
        run_id=str(getattr(logger, "run_id", "") or ""),
        resource_type=WORKFLOW_DEFINITION_TYPE,
        logical_path=f"workflow_definitions/{digest}.json",
        content=envelope,
        media_type="application/json",
        metadata={
            "protocol": WORKFLOW_DEFINITION_PROTOCOL,
            "definitionHash": digest,
            "executable": not sensitive,
            "requiresSensitiveRebinding": bool(
                sensitive or envelope.get("requiredVariableNames")
            ),
        },
    )
    return {
        "definitionRef": stored.get("saved_path"),
        "definitionHash": digest,
        "definitionBytes": len(_canonical_json(definition).encode("utf-8")),
        "executable": not sensitive,
        "requiresSensitiveRebinding": bool(
            sensitive or envelope.get("requiredVariableNames")
        ),
        "requiredVariableNames": list(envelope.get("requiredVariableNames") or []),
    }


def _resource_json(record: Any) -> Optional[JsonDict]:
    if not isinstance(record, dict):
        return None
    value = record.get("content_json")
    if isinstance(value, dict):
        return value
    text = record.get("content_text")
    if isinstance(text, str):
        try:
            loaded = json.loads(text)
        except (TypeError, ValueError):
            return None
        return loaded if isinstance(loaded, dict) else None
    return None


def load_workflow_definition(
    logger: Any, *, definition_ref: str, expected_hash: str,
) -> Tuple[Optional[JsonDict], Optional[JsonDict]]:
    storage, task_id = storage_for_logger(logger)
    try:
        record = storage.read_resource(
            current_task_id=task_id, resource_uri=str(definition_ref or ""),
        )
    except Exception as exc:
        return None, {
            "status": "workflow_definition_unavailable",
            "error": str(exc)[:500],
        }
    envelope = _resource_json(record)
    if not isinstance(envelope, dict):
        return None, {"status": "workflow_definition_not_found"}
    if envelope.get("protocol") != WORKFLOW_DEFINITION_PROTOCOL:
        return None, {"status": "workflow_definition_protocol_mismatch"}
    actual_hash = str(envelope.get("definitionHash") or "")
    if not expected_hash or actual_hash != str(expected_hash):
        return None, {
            "status": "workflow_definition_hash_conflict",
            "expectedHash": str(expected_hash or ""),
            "actualHash": actual_hash,
        }
    if envelope.get("executable") is not True:
        return None, {
            "status": "workflow_definition_sensitive_rebinding_required",
            "definitionHash": actual_hash,
        }
    definition = envelope.get("definition")
    if not isinstance(definition, dict):
        return None, {"status": "workflow_definition_corrupt"}
    try:
        verified_hash = _envelope_hash(envelope)
    except (KeyError, TypeError, ValueError):
        return None, {"status": "workflow_definition_corrupt"}
    if verified_hash != actual_hash:
        return None, {"status": "workflow_definition_content_mismatch"}
    loaded = copy.deepcopy(definition)
    required_variable_names = list(envelope.get("requiredVariableNames") or [])
    if required_variable_names:
        loaded["_requiredVariableNames"] = required_variable_names
    return loaded, None


def _pointer_parts(path: Any) -> Tuple[Optional[List[str]], Optional[str]]:
    raw = str(path or "")
    if not raw.startswith("/") or raw == "/":
        return None, "path must be a non-root RFC 6901 JSON Pointer"
    parts: List[str] = []
    for encoded in raw[1:].split("/"):
        decoded = ""
        index = 0
        while index < len(encoded):
            if encoded[index] != "~":
                decoded += encoded[index]
                index += 1
                continue
            if index + 1 >= len(encoded) or encoded[index + 1] not in {"0", "1"}:
                return None, "path has an invalid RFC 6901 escape"
            decoded += "~" if encoded[index + 1] == "0" else "/"
            index += 2
        parts.append(decoded)
    if not parts or parts[0] not in {"description", "variables", "steps"}:
        return None, "patch may change only description, variables, or steps"
    return parts, None


def apply_workflow_definition_patch(
    definition: JsonDict, operations: Any,
) -> Tuple[Optional[JsonDict], List[str]]:
    if not isinstance(operations, list):
        return None, ["operations must be an array"]
    if len(operations) > MAX_PATCH_OPERATIONS:
        return None, [f"operations exceeds {MAX_PATCH_OPERATIONS}"]
    updated: Any = copy.deepcopy(definition)
    errors: List[str] = []
    for op_index, operation in enumerate(operations):
        where = f"operations[{op_index}]"
        if not isinstance(operation, dict):
            errors.append(f"{where} must be an object")
            continue
        kind = str(operation.get("op") or "")
        if kind not in {"add", "set", "remove"}:
            errors.append(f"{where}.op must be add, set, or remove")
            continue
        parts, error = _pointer_parts(operation.get("path"))
        if error or not parts:
            errors.append(f"{where}: {error}")
            continue
        parent: Any = updated
        for part in parts[:-1]:
            if isinstance(parent, dict) and part in parent:
                parent = parent[part]
            elif isinstance(parent, list) and part.isdigit() and int(part) < len(parent):
                parent = parent[int(part)]
            else:
                errors.append(f"{where}.path parent does not exist")
                parent = None
                break
        if parent is None:
            continue
        leaf = parts[-1]
        if isinstance(parent, dict):
            exists = leaf in parent
            if kind == "add" and exists:
                errors.append(f"{where}.path already exists")
            elif kind in {"set", "remove"} and not exists:
                errors.append(f"{where}.path does not exist")
            elif kind == "remove":
                del parent[leaf]
            else:
                parent[leaf] = copy.deepcopy(operation.get("value"))
        elif isinstance(parent, list):
            if kind == "add" and leaf == "-":
                parent.append(copy.deepcopy(operation.get("value")))
                continue
            if not leaf.isdigit():
                errors.append(f"{where}.path list index is invalid")
                continue
            position = int(leaf)
            if kind == "add":
                if position > len(parent):
                    errors.append(f"{where}.path list index is out of range")
                else:
                    parent.insert(position, copy.deepcopy(operation.get("value")))
            elif position >= len(parent):
                errors.append(f"{where}.path list index is out of range")
            elif kind == "remove":
                parent.pop(position)
            else:
                parent[position] = copy.deepcopy(operation.get("value"))
        else:
            errors.append(f"{where}.path parent is not a container")
    return (updated if not errors and isinstance(updated, dict) else None), errors
