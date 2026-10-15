"""Small orchestration contract; browser programs remain WebCross JS/TS.

This module checks references, state and protocol structure. The author/model
chooses business predicates and decision boundaries; there are no site rules.
"""
from __future__ import annotations

import copy
import hashlib
import json
import re
from pathlib import Path, PurePosixPath
from typing import Any

MISSING = object()


def step_identity(root, step, bindings, results, browser_binding):
    """Identity of the contract accepted now, distinct from executed code."""
    page = {**browser_binding, **bind(step.get("binding", {}), bindings, results)}
    value = {"contractHash": digest(step), "pageId": page.get("pageId"), "fleetId": page.get("fleetId")}
    if step["kind"] == "workflow":
        value.update(sourceHash=digest(read_source(root, step["path"])),
                     inputHash=digest(bind(step.get("input", {}), bindings, results)))
    return value


def plan_schema() -> dict:
    """Public consumer contract used by both Coding and execution tools."""
    ref = {"description": "JSON value, {$binding:'/pointer'} or {$result:{step:'earlier-id',pointer:'/output/path'}}"}
    return {"type": "object", "properties": {
        "schemaVersion": {"type": "integer", "enum": [1]},
        "steps": {"type": "array", "minItems": 1, "items": {"type": "object", "properties": {
            "id": {"type": "string", "minLength": 1},
            "kind": {"type": "string", "enum": ["workflow", "browser", "decision"]},
            "path": {"type": "string", "description": "workflow.json or workflows/*.json"},
            "input": {"type": "object", "additionalProperties": ref},
            "binding": {"type": "object", "properties": {"pageId": ref}, "additionalProperties": False},
            "instruction": {"type": "string"}, "purpose": {"type": "string"},
            "metadata": {"type": "object"},
            "continueWhen": {"type": "array", "items": {"type": "object", "properties": {
                "pointer": {"type": "string"}, "equals": {}}, "required": ["pointer", "equals"],
                "additionalProperties": False}},
        }, "required": ["id", "kind"], "additionalProperties": False}},
        "metadata": {"type": "object"}, "note": {"type": "string"}, "source": {"type": "string"},
    }, "required": ["schemaVersion", "steps"], "additionalProperties": False}


def digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                    separators=(",", ":")).encode()).hexdigest()


def relative_path(value: str) -> str:
    if (not isinstance(value, str) or not value or "\\" in value
            or PurePosixPath(value).is_absolute()
            or any(p in {"", ".", ".."} or p.startswith(".") for p in value.split("/"))):
        raise ValueError("Resource path must stay inside the Skill")
    return value


def read_source(root: Path, path: str) -> dict:
    target = root / relative_path(path)
    if target.is_symlink() or not target.resolve().is_relative_to(root.resolve()):
        raise ValueError("Skill resource escapes its bound directory")
    document = json.loads(target.read_text())
    if not isinstance(document, dict):
        raise ValueError("Skill source must be a JSON object")
    return document


def pointer(value: Any, path: str, default: Any = MISSING) -> Any:
    if not isinstance(path, str) or (path and not path.startswith("/")):
        raise ValueError("Use an RFC 6901 JSON pointer")
    parts = path.split("/")[1:] if path else []
    if any(re.search(r"~(?![01])", raw) for raw in parts):
        raise ValueError("Invalid JSON pointer escape")
    try:
        for raw in parts:
            key = raw.replace("~1", "/").replace("~0", "~")
            if isinstance(value, list):
                if not re.fullmatch(r"0|[1-9][0-9]*", key):
                    raise ValueError("Array pointer requires a nonnegative canonical index")
                value = value[int(key)]
            else:
                value = value[key]
        return value
    except (KeyError, IndexError, TypeError, ValueError):
        if default is MISSING:
            raise ValueError(f"Input/result reference is missing: {path}")
        return default


def bind(value: Any, bindings: dict, results: dict) -> Any:
    """References are whole objects, never interpolation or executable text."""
    if isinstance(value, dict):
        if set(value) == {"$binding"}:
            return copy.deepcopy(pointer(bindings, value["$binding"]))
        if set(value) == {"$result"}:
            ref = value["$result"]
            if not isinstance(ref, dict) or set(ref) != {"step", "pointer"}:
                raise ValueError("$result requires step and pointer")
            if ref["step"] not in results:
                raise ValueError("Referenced step has no recorded output")
            return copy.deepcopy(pointer(results[ref["step"]], ref["pointer"]))
        return {k: bind(v, bindings, results) for k, v in value.items()}
    if isinstance(value, list):
        return [bind(v, bindings, results) for v in value]
    return copy.deepcopy(value)


def validate_plan(document: Any, root: Path | None = None) -> dict:
    if (not isinstance(document, dict) or document.get("schemaVersion") != 1
            or not isinstance(document.get("steps"), list) or not document["steps"]):
        raise ValueError("Orchestration requires schemaVersion=1 and nonempty steps")
    seen = set()
    for step in document["steps"]:
        if not isinstance(step, dict):
            raise ValueError("Each orchestration step must be an object")
        unknown = set(step) - set(plan_schema()["properties"]["steps"]["items"]["properties"])
        if unknown:
            raise ValueError(f"Unsupported step fields: {sorted(unknown)}; use input for business values and binding.pageId for execution identity")
        sid = step.get("id")
        if not isinstance(sid, str) or not sid or sid in seen:
            raise ValueError("Step identities must be nonempty and unique")
        kind = step.get("kind")
        if kind not in {"workflow", "browser", "decision"}:
            raise ValueError("Step kind must be workflow, browser or decision")
        if kind == "workflow":
            path = relative_path(step.get("path"))
            if path != "workflow.json" and not path.startswith("workflows/"):
                raise ValueError("Workflow must be a formal Skill entry")
            if not isinstance(step.get("input", {}), dict):
                raise ValueError("Workflow input must be an object")
            if root is not None:
                source = read_source(root, path)
                if not isinstance(source.get("entry"), str) or not isinstance(source.get("files"), dict):
                    raise ValueError("Workflow must be a WebCross JS/TS source package")
            predicates = step.get("continueWhen", [])
            binding = step.get("binding", {})
            if not isinstance(binding, dict) or set(binding) - {"pageId"}:
                raise ValueError("Step binding supports only pageId; Fleet remains task-owned")
            if not isinstance(predicates, list):
                raise ValueError("continueWhen must be an array")
            for predicate in predicates:
                if (not isinstance(predicate, dict) or "equals" not in predicate
                        or not isinstance(predicate.get("pointer"), str)):
                    raise ValueError("Continuation predicate requires pointer and equals")
                pointer({}, predicate["pointer"], default=None)
        elif not isinstance(step.get("instruction"), str) or not step["instruction"].strip():
            raise ValueError("Browser/decision step requires an instruction")
        # Validate backward result references without guessing their business meaning.
        def refs(value):
            if isinstance(value, dict):
                if set(value) == {"$binding"}:
                    pointer({}, value["$binding"], default=None)
                if set(value) == {"$result"}:
                    ref = value["$result"]
                    if (not isinstance(ref, dict) or set(ref) != {"step", "pointer"}
                            or not isinstance(ref["step"], str) or ref["step"] not in seen):
                        raise ValueError("Result references must name an earlier step")
                    pointer({}, ref["pointer"], default=None)
                for v in value.values(): refs(v)
            elif isinstance(value, list):
                for v in value: refs(v)
        refs(step.get("input", {}))
        refs(step.get("binding", {}))
        seen.add(sid)
    return copy.deepcopy(document)


def continuation_facts(step: dict, receipt: dict) -> dict:
    predicates = step.get("continueWhen", [])
    facts = []
    for predicate in predicates:
        sentinel = object()
        actual = pointer(receipt, predicate["pointer"], sentinel)
        present = actual is not sentinel
        # JSON boolean and number must not compare equal through Python coercion.
        expected = predicate["equals"]
        matches = present and digest(actual) == digest(expected)
        facts.append({"pointer": predicate["pointer"], "present": present,
                      "actual": actual if present else None, "expected": expected,
                      "matches": matches})
    return {"declared": bool(predicates), "matched": bool(predicates) and all(f["matches"] for f in facts),
            "checks": facts}
