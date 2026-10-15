"""Immutable, indexed development sources and evidence-preserving Builder import."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from harness.skill_builder.catalog import inspect_skill
from harness.skill_runtime.plan import relative_path
from harness.storage.sqlite_store import build_resource_uri


def snapshot_source(logger, directory: Path) -> dict:
    info = inspect_skill(directory)
    root = logger.task_dir / "workflow" / "versions" / info["hash"]
    if root.resolve() != logger.task_dir.resolve() / "workflow" / "versions" / info["hash"]:
        raise ValueError("Workflow snapshot cannot traverse symbolic links")
    root.mkdir(parents=True, exist_ok=True)
    files = []
    for item in info["files"]:
        body = (directory / item["path"]).read_bytes()
        if hashlib.sha256(body).hexdigest() != item["sha256"]:
            raise ValueError("Development file changed while snapshotting")
        target = root / relative_path(item["path"])
        if target.exists() and target.read_bytes() != body:
            raise ValueError("Immutable Workflow snapshot changed")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(body)
        row = logger.storage.save_resource(task_id=logger.task_id, run_id=logger.run_id,
            resource_type="workflow_source", logical_path=target.relative_to(logger.task_dir).as_posix(),
            content=body.decode("utf-8"), media_type="text/plain",
            metadata={"sourceHash": info["hash"], "sha256": item["sha256"]})
        files.append({**item, "reference": row["saved_path"]})
    if inspect_skill(root)["hash"] != info["hash"]:
        raise ValueError("Development snapshot is inconsistent")
    return {"hash": info["hash"], "path": root.relative_to(logger.task_dir).as_posix(), "files": files}


def read_source_json(source, path):
    rows = [r for r in source.resources(path_glob=path, limit=100)
            if r.get("logical_path") == path and r.get("is_current", True)]
    if not rows:
        return None
    row = max(rows, key=lambda r: (r.get("resource_version", 0), r.get("created_at", "")))
    ref = build_resource_uri(source.task_id, row["resource_id"])
    record = source.read_resource(ref)
    value = record.get("content_json")
    return json.loads(value) if isinstance(value, str) else value


def import_workflow_package(builder) -> dict | None:
    package = (read_source_json(builder.source, "workflow/package.json")
               or read_source_json(builder.source, "skill/package.json"))
    if not package:
        return None
    if package.get("taskId") != builder.source.task_id or package.get("schemaVersion") != 1:
        raise ValueError("Workflow package does not match its source task")
    if any(builder.work_dir.iterdir()):
        raise ValueError("Workflow import requires an empty draft; existing edits must be preserved")
    source = package["source"]
    # Load and verify every byte before creating the draft. A log string or an
    # external mutable path cannot substitute for an indexed source snapshot.
    contents = {}
    for item in source["files"]:
        path = relative_path(item["path"])
        record = builder.source.read_resource(item["reference"])
        text = record.get("content_text") if record else None
        if not isinstance(text, str) or hashlib.sha256(text.encode()).hexdigest() != item["sha256"]:
            raise ValueError(f"Workflow snapshot unavailable or changed: {path}")
        contents[path] = text
    for path, text in contents.items():
        builder._write_file(path, text)
    if inspect_skill(builder.work_dir)["hash"] != source["hash"]:
        raise ValueError("Imported Workflow hash differs from executed source package")
    row = builder.storage.save_resource(task_id=builder.logger.task_id, run_id=builder.logger.run_id,
        resource_type="workflow_import", logical_path="coding/workflow-import.json", content=package)
    builder.logger.write("skill_builder.workflow.imported", {
        "sourceTaskId": builder.source.task_id, "sourceHash": source["hash"],
        "evidenceRef": row["saved_path"], "status": package.get("status")})
    return package
