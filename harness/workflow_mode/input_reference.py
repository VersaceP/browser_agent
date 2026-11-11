"""Immutable business-input reuse; every execution still checks its current scope."""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
from typing import Any, Callable

from harness.workflow_mode.evidence import pointer, resolve_reference, short_reference


def input_hash(value: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


def resolve_input(arguments: dict[str, Any], *, known_refs: set[str],
                  read: Callable[[str], Any]) -> dict[str, Any]:
    """One source; overrides replace whole top-level values, including null."""
    if ("input" in arguments) == ("input_ref" in arguments):
        raise ValueError("Provide exactly one of input or input_ref")
    if "input_ref" not in arguments:
        if "input_overrides" in arguments or "input_patch" in arguments:
            raise ValueError("input_overrides/input_patch requires input_ref")
        value = arguments["input"]
    else:
        reference = arguments["input_ref"]
        if (not isinstance(reference, dict) or set(reference) != {"ref", "sha256"}
                or not isinstance(reference["ref"], str) or not isinstance(reference["sha256"], str)):
            raise ValueError("input_ref requires ref and sha256 strings")
        canonical = resolve_reference(reference["ref"], known_refs)
        if canonical not in known_refs:
            raise PermissionError("Input reference is not a receipt owned by this task")
        value = pointer(read(canonical), "/input")
        if not isinstance(value, dict) or input_hash(value) != reference["sha256"]:
            raise ValueError("Referenced input does not match its SHA-256")
        overrides = arguments.get("input_overrides", {})
        if not isinstance(overrides, dict):
            raise ValueError("input_overrides must contain complete top-level replacement values")
        if "input_patch" in arguments and "input_overrides" in arguments:
            raise ValueError("Use input_patch or input_overrides, not both")
        value = apply_patch(value, arguments["input_patch"]) if "input_patch" in arguments else {**value, **overrides}
    if not isinstance(value, dict):
        raise ValueError("Business input must be an object")
    return bind_input(value, arguments.get('input_bindings', []), known_refs=known_refs, read=read)


def bind_input(value, bindings, *, known_refs, read):
    """Copy exact owned facts into explicit input addresses, without model echo."""
    if not isinstance(bindings, list):
        raise ValueError('input_bindings must be an array')
    output = copy.deepcopy(value)
    for binding in bindings:
        if not isinstance(binding, dict) or set(binding)-{'path','ref','pointer','offset','limit'}:
            raise ValueError('Invalid input binding')
        ref = resolve_reference(binding['ref'], known_refs)
        if ref not in known_refs:
            raise PermissionError('Input binding requires an owned receipt')
        data = pointer(read(ref), binding['pointer'])
        if 'offset' in binding or 'limit' in binding:
            offset, limit = binding.get('offset', 0), binding.get('limit')
            if (not isinstance(data, list) or type(offset) is not int or offset < 0
                    or limit is not None and (type(limit) is not int or limit < 0)):
                raise ValueError('Input slices require an array and nonnegative integer bounds')
            if offset > len(data) or limit is not None and offset+limit > len(data):
                raise ValueError('Input slice exceeds recorded evidence coverage')
            data = data[offset:None if limit is None else offset+limit]
        output = apply_patch(output, [{'op': 'add', 'path': binding['path'], 'value': data}])
    return output


def reference_for(receipt: dict[str, Any], ref: str) -> dict[str, str] | None:
    value = receipt.get("input")
    return {"ref": short_reference(ref) or ref, "sha256": input_hash(value)} if isinstance(value, dict) else None


def manifest_hash(files: list[dict[str, Any]]) -> str:
    return hashlib.sha256(json.dumps(files, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def material_manifest(files: list[dict[str, Any]]) -> dict[str, Any]:
    """Portable declarations only: never carries task grants or cached file facts."""
    return {"schemaVersion": 1, "files": copy.deepcopy(files)}


def manifest_files(value: Any) -> list[dict[str, Any]]:
    if (not isinstance(value, dict) or set(value) != {"schemaVersion", "files"}
            or type(value["schemaVersion"]) is not int or value["schemaVersion"] != 1):
        raise ValueError("Material manifest must contain schemaVersion:1 and files")
    files = _clean_files(value["files"])
    if any(not Path(item["path"]).is_absolute() for item in files):
        raise ValueError("Portable material manifest file paths must be absolute; no path base is inferred")
    return files


def _clean_files(files: Any) -> list[dict[str, Any]]:
    if not isinstance(files, list):
        raise ValueError("files must be an explicit array of input roles")
    clean = []
    for item in files:
        if (not isinstance(item, dict) or set(item) - {"path", "kind", "role", "source"}
                or not isinstance(item.get("path"), str) or not item["path"]
                or item.get("kind", "file") not in {"file", "image"}):
            raise ValueError("Each input file needs path, optional kind:file|image, role and source")
        row = {"path": item["path"], "kind": item.get("kind", "file")}
        for key, limit in (("role", 256), ("source", 2048)):
            if key in item:
                if not isinstance(item[key], str) or not item[key].strip() or len(item[key]) > limit:
                    raise ValueError(f"File {key} must be a nonempty string of at most {limit} characters")
                row[key] = item[key]
        clean.append(row)
    return copy.deepcopy(clean)


def resolve_files(arguments: dict[str, Any], *, known_refs: set[str],
                  read: Callable[[str], Any],
                  read_manifest: Callable[[dict[str, Any]], Any] | None = None) -> list[dict[str, Any]]:
    """Reuse explicit declarations; callers reacquire current permissions/facts."""
    if sum(key in arguments for key in ("files", "files_ref", "files_manifest")) > 1:
        raise ValueError("Provide only one of files, files_ref or files_manifest")
    if "files_manifest" in arguments:
        source = arguments["files_manifest"]
        if (not isinstance(source, dict) or set(source) - {"path", "sha256"}
                or not isinstance(source.get("path"), str) or not source["path"]
                or ("sha256" in source and not isinstance(source["sha256"], str))):
            raise ValueError("files_manifest needs a path and optional sha256 string")
        if read_manifest is None:
            raise ValueError("Authorized manifest reader is unavailable")
        return manifest_files(read_manifest(source))
    if "files_ref" in arguments:
        reference = arguments["files_ref"]
        if (not isinstance(reference, dict) or set(reference) != {"ref", "sha256"}
                or not isinstance(reference["ref"], str) or not isinstance(reference["sha256"], str)):
            raise ValueError("files_ref requires ref and sha256 strings")
        canonical = resolve_reference(reference["ref"], known_refs)
        if canonical not in known_refs:
            raise PermissionError("File manifest is not a receipt of this task")
        files = pointer(read(canonical), "/inputFiles")
        if not isinstance(files, list) or manifest_hash(files) != reference["sha256"]:
            raise ValueError("Referenced file manifest does not match its SHA-256")
    else:
        files = arguments.get("files", [])
    return _clean_files(files)


def manifest_reference(files: list[dict[str, Any]], ref: str) -> dict[str, str]:
    return {"ref": short_reference(ref) or ref, "sha256": manifest_hash(files)}


def apply_patch(value: dict[str, Any], operations: Any) -> dict[str, Any]:
    """Explicit RFC 6901 add/replace/remove edits of an owned JSON input copy.

    Addresses select data only. Neither values nor declarations grant file or
    browser access. No wildcard, guessed business key or deep-merge behavior.
    """
    import re
    if not isinstance(operations, list):
        raise ValueError('input_patch must be an array')
    result = copy.deepcopy(value)
    for operation in operations:
        if not isinstance(operation, dict) or operation.get('op') not in {'add', 'replace', 'remove'}:
            raise ValueError('input_patch supports add, replace and remove')
        op, path = operation['op'], operation.get('path')
        if not isinstance(path, str) or not path.startswith('/') or re.search(r'~(?![01])', path):
            raise ValueError('input_patch paths must be non-root RFC 6901 pointers')
        if op != 'remove' and 'value' not in operation:
            raise ValueError('add/replace requires value (null is a value)')
        parts = [p.replace('~1', '/').replace('~0', '~') for p in path[1:].split('/')]
        parent = result
        for part in parts[:-1]:
            if isinstance(parent, list):
                if not re.fullmatch(r'0|[1-9][0-9]*', part) or int(part) >= len(parent):
                    raise ValueError('input_patch array address is absent')
                parent = parent[int(part)]
            elif isinstance(parent, dict) and part in parent:
                parent = parent[part]
            else:
                raise ValueError('input_patch parent is absent')
        key = parts[-1]
        if isinstance(parent, list):
            if key == '-' and op == 'add':
                parent.append(copy.deepcopy(operation['value']))
                continue
            if not re.fullmatch(r'0|[1-9][0-9]*', key):
                raise ValueError('input_patch array index is invalid')
            index = int(key)
            if op == 'add' and index <= len(parent):
                parent.insert(index, copy.deepcopy(operation['value']))
            elif index >= len(parent):
                raise ValueError('input_patch target is absent')
            elif op == 'remove':
                parent.pop(index)
            else:
                parent[index] = copy.deepcopy(operation['value'])
        elif isinstance(parent, dict):
            if op != 'add' and key not in parent:
                raise ValueError('input_patch target is absent')
            if op == 'remove':
                del parent[key]
            else:
                parent[key] = copy.deepcopy(operation['value'])
        else:
            raise ValueError('input_patch parent must be an object or array')
    return result
