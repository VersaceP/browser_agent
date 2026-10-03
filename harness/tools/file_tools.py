"""Bounded local file operations for Browser workers.

The browser agent may create delivery directories, write text/JSON, copy
existing task files, and inspect file metadata.  This is deliberately not a
shell: it cannot execute code, delete/move files, or mutate harness control
state.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from harness.utils import JsonDict, open_artifact_binary
from harness.context.offload import store_offloaded


_TASK_OUTPUT_DIRS = frozenset({"observations", "deliverables", "scratchpad"})
_FORBIDDEN_COMPONENTS = frozenset({
    ".git", ".ssh", ".gnupg", ".env", "credentials", "node_modules",
})
_MAX_OPERATIONS = 100
_MAX_TEXT_BYTES = 2_000_000


def _inside(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _nearest_existing(path: Path) -> Optional[Path]:
    current = path
    while True:
        if current.exists() or current.is_symlink():
            return current
        if current == current.parent:
            return None
        current = current.parent


def _canonicalize_destination(path: Path) -> Tuple[Optional[Path], Optional[str]]:
    """Resolve the existing parent while preserving the final path component.

    macOS exposes temporary paths through both /var and /private/var.  Resolving
    an existing parent normalizes that alias before containment checks.  The
    destination itself is deliberately not resolved so a final symlink remains
    visible to the caller and can be rejected.
    """
    existing_parent = _nearest_existing(path.parent)
    if existing_parent is None:
        return None, "no existing ancestor for destination"
    try:
        suffix = path.relative_to(existing_parent)
        resolved_parent = existing_parent.resolve(strict=True)
    except (OSError, ValueError) as exc:
        return None, f"cannot resolve destination: {exc}"
    return resolved_parent.joinpath(*suffix.parts), None


def _workspace_root(task_dir: Path) -> Optional[Path]:
    parent = task_dir.parent
    if parent.name == "worktree":
        return parent.parent.resolve()
    return None


def _input_path(agent: Any, raw_path: str, base: Any = None) -> Path:
    task_dir = Path(agent.logger.task_dir).resolve()
    desktop = (Path.home() / "Desktop").resolve()
    candidate = Path(raw_path).expanduser()
    base_name = str(base or "task").strip().lower()
    if not candidate.is_absolute():
        parts = candidate.parts
        if base_name == "desktop":
            candidate = desktop / candidate
        elif parts and parts[0].lower() == "desktop":
            candidate = desktop.joinpath(*parts[1:])
        else:
            candidate = task_dir / candidate
    return Path(os.path.abspath(str(candidate)))


def _destination_path(
    agent: Any, raw_path: Any, *, base: Any = None,
) -> Tuple[Optional[Path], Optional[str]]:
    if not isinstance(raw_path, str) or not raw_path.strip():
        return None, "path must be a non-empty string"
    if _input_path(agent, raw_path, base).is_symlink():
        return None, "symlink destinations are not allowed"
    from harness.tools.path_authorization import resolve_authorized_path
    try:
        candidate = resolve_authorized_path(agent, raw_path, mode="write", base=base)
    except (OSError, ValueError) as exc:
        return None, str(exc)
    task_dir = Path(agent.logger.task_dir).resolve()
    desktop = (Path.home() / "Desktop").resolve()
    if ".." in Path(raw_path).parts:
        return None, "parent traversal is not allowed"
    candidate, canonical_error = _canonicalize_destination(candidate)
    if canonical_error or candidate is None:
        return None, canonical_error
    if any(part.lower() in _FORBIDDEN_COMPONENTS or part.startswith(".") for part in candidate.parts):
        return None, "hidden or sensitive path components are not allowed"

    if _inside(candidate, task_dir):
        relative = candidate.relative_to(task_dir)
        if not relative.parts or relative.parts[0] not in _TASK_OUTPUT_DIRS:
            return None, (
                "task-worktree writes are limited to observations/, deliverables/, or scratchpad/"
            )
        if getattr(agent, "standalone_browser_mode", False):
            control = task_dir / "scratchpad"
            versions = control / "todo_versions"
            if (candidate in {control / "todo.md", control / "review-latest.json",
                              control / "review-session.json", versions}
                    or _inside(candidate, versions)):
                return None, "standalone Browser control file; use update_task_todo for the checklist"
        allowed_root = task_dir / relative.parts[0]
        # DB-only tasks may have no physical directory yet. Anchor inspection
        # at the existing ancestor; the output-root containment check below
        # still constrains the destination to this task's allowed directory.
        security_root = _nearest_existing(task_dir) or task_dir
    else:
        # Use the approved external write root, not the destination itself, so
        # one directory approval covers all files below it.
        from harness.tools.path_authorization import authorized_root_for
        allowed_root = authorized_root_for(agent, candidate, mode="write") or candidate
        security_root = _nearest_existing(allowed_root.parent) or allowed_root.parent

    existing = _nearest_existing(candidate.parent)
    if existing is None:
        return None, "no existing ancestor for destination"
    try:
        resolved_existing = existing.resolve(strict=True)
        resolved_allowed = allowed_root.resolve(strict=False)
        resolved_security_root = security_root.resolve(strict=True)
    except OSError as exc:
        return None, f"cannot resolve destination: {exc}"
    if not _inside(resolved_existing, resolved_security_root) and resolved_existing != resolved_security_root:
        return None, "destination resolves outside its allowed root"
    if not _inside(candidate, resolved_allowed) and candidate != resolved_allowed:
        return None, "destination is outside its allowed output directory"
    if candidate.is_symlink():
        return None, "symlink destinations are not allowed"
    return candidate, None


def _stat_path(
    agent: Any, raw_path: Any, *, base: Any = None,
) -> Tuple[Optional[Path], Optional[str]]:
    """Resolve a readable task/Desktop file or directory without mutation."""
    if not isinstance(raw_path, str) or not raw_path.strip():
        return None, "path must be a non-empty string"
    if ".." in Path(raw_path).parts:
        return None, "parent traversal is not allowed"
    from harness.tools.path_authorization import resolve_authorized_path
    try:
        candidate = resolve_authorized_path(agent, raw_path, mode="read", base=base)
    except (OSError, ValueError) as exc:
        return None, str(exc)
    task_dir = Path(agent.logger.task_dir).resolve()
    desktop = (Path.home() / "Desktop").resolve()
    try:
        resolved = candidate.resolve(strict=False)
    except OSError as exc:
        return None, f"path cannot be resolved: {exc}"
    if any(
        part.lower() in _FORBIDDEN_COMPONENTS or part.startswith(".")
        for part in resolved.parts
    ):
        return None, "hidden or sensitive path components are not allowed"
    if _inside(resolved, task_dir):
        from harness.storage.virtual_fs import virtual_fs_for
        view = virtual_fs_for(agent.logger)
        logical = str(resolved.relative_to(task_dir))
        if not resolved.exists() and not (view is not None and
                (resolved == task_dir or view.exists(logical) or
                 any(name.startswith(logical + "/") for name, _, _ in view.list_files()))):
            return None, "path cannot be resolved: path does not exist"
        return resolved, None
    if not resolved.exists():
        return None, "path cannot be resolved: path does not exist"
    if _inside(resolved, desktop):
        workspace = _workspace_root(task_dir)
        if workspace is not None and _inside(resolved, workspace):
            return None, "browser file tools cannot inspect workspace source or control files"
        return resolved, None
    if resolved.is_file():
        registered = {
            str(Path(path).expanduser().resolve(strict=False))
            for path in (getattr(agent, "artifacts", []) or [])
            if str(path).strip()
        }
        if str(resolved) in registered:
            return resolved, None
    return resolved, None


def _source_path(agent: Any, raw_path: Any) -> Tuple[Optional[Path], Optional[str]]:
    if not isinstance(raw_path, str) or not raw_path.strip():
        return None, "source must be a non-empty string"
    from harness.tools.path_authorization import resolve_authorized_path
    try:
        candidate = resolve_authorized_path(agent, raw_path, mode="read")
    except (OSError, ValueError) as exc:
        return None, str(exc)
    task_dir = Path(agent.logger.task_dir).resolve()
    try:
        resolved = candidate.resolve(strict=False)
    except OSError as exc:
        return None, f"source cannot be resolved: {exc}"
    from harness.storage.virtual_fs import virtual_fs_for
    view = virtual_fs_for(agent.logger) if _inside(resolved, task_dir) else None
    virtual_file = view is not None and view.exists(str(resolved.relative_to(task_dir)))
    if not resolved.is_file() and not virtual_file:
        return None, "source must be a regular file"
    if _inside(resolved, task_dir):
        return resolved, None
    registered = {
        str(Path(path).expanduser().resolve(strict=False))
        for path in (getattr(agent, "artifacts", []) or [])
        if str(path).strip()
    }
    return resolved, None


def _file_facts(path: Path, *, operation: str, source: Optional[Path] = None,
                logger: Any = None) -> JsonDict:
    with open_artifact_binary(logger, path) as handle:
        data = handle.read()
    result: JsonDict = {
        "path": str(path.resolve()),
        "byteSize": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
        "operation": operation,
    }
    if source is not None:
        result["sourcePath"] = str(source.resolve())
    return result


def _stat_facts(path: Path, *, logger: Any = None) -> JsonDict:
    from harness.storage.virtual_fs import db_authoritative_for, virtual_fs_for
    if logger is not None and db_authoritative_for(logger) and path.is_relative_to(logger.task_dir.resolve()):
        view = virtual_fs_for(logger)
        logical = str(path.relative_to(logger.task_dir.resolve()))
        if view is not None and view.exists(logical):
            return {**_file_facts(path, operation="stat", logger=logger), "storage": "sqlite"}
        if not path.exists():
            return {"path": str(path), "kind": "directory", "byteSize": None,
                    "operation": "stat", "storage": "sqlite"}
    if path.is_file():
        return _file_facts(path, operation="stat")
    stat = path.stat()
    return {
        "path": str(path.resolve()),
        "kind": "directory" if path.is_dir() else "other",
        "byteSize": int(stat.st_size),
        "modifiedNs": int(stat.st_mtime_ns),
        "operation": "stat",
    }


def _register_file(agent: Any, facts: JsonDict) -> None:
    path = str(facts["path"])
    artifacts = getattr(agent, "artifacts", None)
    if isinstance(artifacts, list) and path not in artifacts:
        artifacts.append(path)
    register = getattr(agent, "_register_external_file", None)
    if callable(register):
        register("local_fs_batch", path)


def local_fs_batch(agent: Any, operations: Any) -> JsonDict:
    if not isinstance(operations, list) or not operations:
        return {"status": "failed", "error": "operations must be a non-empty list"}
    if len(operations) > _MAX_OPERATIONS:
        return {"status": "failed", "error": f"at most {_MAX_OPERATIONS} operations are allowed"}

    results: List[JsonDict] = []
    files: List[JsonDict] = []
    for index, raw in enumerate(operations):
        if not isinstance(raw, dict):
            results.append({"index": index, "status": "failed", "error": "operation must be an object"})
            continue
        op = str(raw.get("op") or "").strip()
        try:
            if op == "mkdir":
                target, error = _destination_path(
                    agent, raw.get("path"), base=raw.get("base"),
                )
                if error or target is None:
                    raise ValueError(error)
                target.mkdir(parents=True, exist_ok=True)
                if not target.resolve(strict=True).is_dir():
                    raise ValueError("created path is not a directory")
                results.append({"index": index, "op": op, "status": "done", "path": str(target.resolve())})
                continue

            if op == "list":
                from harness.tools.local_fs import local_fs_list
                listing = local_fs_list(agent, raw.get("path"), base=raw.get("base"),
                                        recursive=bool(raw.get("recursive", False)))
                results.append({"index": index, "op": op, **listing})
                continue

            if op == "stat":
                source, error = _stat_path(
                    agent, raw.get("path"), base=raw.get("base"),
                )
                if error or source is None:
                    raise ValueError(error)
                facts = _stat_facts(source, logger=agent.logger)
                results.append({"index": index, "op": op, "status": "done", **facts})
                continue

            target, error = _destination_path(
                agent, raw.get("path"), base=raw.get("base"),
            )
            if error or target is None:
                raise ValueError(error)
            overwrite = bool(raw.get("overwrite", False))
            from harness.utils import task_file_exists
            if (target.exists() or task_file_exists(agent.logger, str(target))) and not overwrite:
                raise FileExistsError("destination exists; set overwrite=true to replace it")
            source = None
            if op == "copy":
                source, source_error = _source_path(agent, raw.get("source"))
                if source_error or source is None:
                    raise ValueError(source_error)
                if source == target:
                    raise shutil.SameFileError("source and destination are the same file")
            target.parent.mkdir(parents=True, exist_ok=True)
            if op == "write_text":
                content = raw.get("content")
                if not isinstance(content, str):
                    raise ValueError("content must be a string")
                encoded = content.encode("utf-8")
                if len(encoded) > _MAX_TEXT_BYTES:
                    raise ValueError(f"content exceeds {_MAX_TEXT_BYTES} UTF-8 bytes")
                target.write_bytes(encoded)
                facts = _file_facts(target, operation=op)
            elif op == "write_json":
                content = raw.get("content")
                if isinstance(content, str):
                    content = json.loads(content)
                encoded = (json.dumps(content, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
                if len(encoded) > _MAX_TEXT_BYTES:
                    raise ValueError(f"JSON exceeds {_MAX_TEXT_BYTES} UTF-8 bytes")
                target.write_bytes(encoded)
                facts = _file_facts(target, operation=op)
            elif op == "copy":
                with open_artifact_binary(agent.logger, source) as input_file:
                    with target.open("wb") as output_file:
                        shutil.copyfileobj(input_file, output_file)
                    if getattr(input_file, "name", None) == str(source):
                        shutil.copystat(source, target)
                facts = _file_facts(target, operation=op, source=source)
            else:
                raise ValueError("op must be mkdir, write_text, write_json, copy, or stat")
            _register_file(agent, facts)
            files.append(facts)
            results.append({"index": index, "op": op, "status": "done", **facts})
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
            results.append({"index": index, "op": op, "status": "failed", "error": str(exc)[:500]})

    task_dir = Path(agent.logger.task_dir).resolve()
    manifest_dir = task_dir / "artifacts" / "file_manifests"
    manifest_path = manifest_dir / f"file-batch-{uuid.uuid4().hex[:8]}.json"
    manifest: JsonDict = {
        "protocol": "browser-file-manifest-v1",
        "workerId": getattr(agent, "worker_id", None),
        "phaseId": (getattr(agent, "worker_contract", {}) or {}).get("phase_id"),
        "files": files,
        "results": results,
    }
    store_offloaded(
        agent.logger,
        manifest_path,
        resource_type="file_manifest",
        content=manifest,
        media_type="application/json",
    )
    manifest_receipt = {
        "protocol": manifest["protocol"],
        "manifestPath": str(manifest_path.resolve()),
        "fileCount": len(files),
        "failedCount": sum(1 for item in results if item.get("status") == "failed"),
    }
    manifests = getattr(agent, "file_manifests", None)
    if not isinstance(manifests, list):
        manifests = []
        setattr(agent, "file_manifests", manifests)
    manifests.append(manifest_receipt)
    evidence = getattr(agent, "file_action_evidence", None)
    if isinstance(evidence, list):
        evidence.append({
            "method": "local_fs_batch",
            "params": {"operationCount": len(operations)},
            "response": {"status": "done", "files": files, "manifestPath": str(manifest_path)},
        })
    failed = manifest_receipt["failedCount"]
    return {
        "status": "done" if failed == 0 else "partial",
        "operationCount": len(results),
        "failedCount": failed,
        "files": files,
        "manifestPath": str(manifest_path.resolve()),
        "results": results,
    }
