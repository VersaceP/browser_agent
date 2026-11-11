"""Explicit material handoff, independent of business-input preparation.

Manifests contain caller declarations, not semantic verification or path grants.
Every consumer inspects current files using the existing task authorization.
"""
from __future__ import annotations

import asyncio
import copy
import hashlib
import json
from pathlib import Path
from typing import Any

from harness.workflow_mode import preflight
from harness.workflow_mode.evidence import evidence_reference, pointer, resolve_reference
from harness.workflow_mode.input_reference import (
    manifest_files, manifest_reference, material_manifest, resolve_files,
)


_SOURCES = ("files", "files_ref", "files_manifest", "manifest_ref")
_UNSET = object()


async def inspect(session: Any, arguments: dict[str, Any]) -> dict[str, Any]:
    """Resolve explicit declarations and collect fresh, authorized file facts."""
    source = None
    previous_files = None
    previous_ref = None
    if sum(name in arguments for name in _SOURCES) > 1:
        raise ValueError("Provide only one of files, files_ref, files_manifest or manifest_ref")

    def read_files_receipt(reference):
        nonlocal previous_files, previous_ref
        value = session.store.read_ref(reference)
        previous_files = value.get("files") if isinstance(value, dict) else None
        previous_ref = reference
        return value

    def read_manifest(location):
        nonlocal source
        try:
            value, source = preflight.read_material_manifest(session, location)
        except OSError as exc:
            raise ValueError(f"Material manifest read failed ({type(exc).__name__}): {exc}") from exc
        return value

    if "manifest_ref" in arguments:
        reference, selected = evidence_reference(arguments["manifest_ref"])
        canonical = resolve_reference(reference, session.known_refs())
        if canonical not in session.known_refs():
            raise PermissionError("Material manifest evidence must belong to this task")
        files = manifest_files(pointer(session.store.read_ref(canonical), selected))
        source = {"kind": "receipt", "ref": canonical, "pointer": selected}
    else:
        files = resolve_files(arguments, known_refs=session.known_refs(),
                              read=read_files_receipt, read_manifest=read_manifest)
    facts = await asyncio.to_thread(preflight.file_facts, session, files)
    changes = preflight.compare_file_facts(previous_files, facts, previous_ref)
    portable_entries = [{**item, "path": fact.get("canonicalPath", item["path"])}
                        for item, fact in zip(files, facts)]
    portable = (material_manifest(portable_entries)
                if all(Path(item["path"]).is_absolute() for item in portable_entries) else None)
    return {"inputFiles": files, "files": facts, "fileFactChanges": changes,
            "materialManifest": portable, "manifestImport": source,
            "materialManifestStatus": "available" if portable is not None else "unconfirmed",
            "manifestPortabilityNote": None if portable is not None else
                "A relative declared path lacks an authorized canonical path; no portable manifest is available"}


def _encoded_manifest(portable: dict[str, Any]) -> bytes:
    return (json.dumps(portable, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _write_ack(session: Any, operation: dict[str, Any], response: Any, index: int,
               encoded: bytes) -> tuple[dict[str, Any] | None, str | None]:
    """Bind a completed write to its exact operation path and encoded content."""
    from harness.tools.path_authorization import canonical
    rows = response.get("results", []) if isinstance(response, dict) else []
    if not isinstance(rows, list):
        return None, "Write acknowledgment results is not an array"
    candidates = [row for row in rows if isinstance(row, dict)
                  and type(row.get("index")) is int and row["index"] == index]
    if len(candidates) > 1:
        return None, "Multiple write acknowledgments conflict for one manifest operation"
    if not candidates or candidates[0].get("status") != "done" or candidates[0].get("op") != "write_json":
        return None, None
    try:
        expected = str(canonical(session, operation["path"], base=operation.get("base")))
    except (OSError, ValueError) as exc:
        return None, f"Cannot bind the write acknowledgment to the destination: {exc}"
    digest = hashlib.sha256(encoded).hexdigest()
    row = candidates[0]
    if (row.get("path") == expected and row.get("sha256") == digest
            and type(row.get("byteSize")) is int and row["byteSize"] == len(encoded)):
        return row, None
    return None, "Write acknowledgment does not bind this manifest's path, SHA-256 and byte size"


async def export_manifest(session: Any, portable: dict[str, Any] | None,
                          output: Any = _UNSET) -> dict[str, Any] | None:
    """Use existing local write policy; an uncertain acknowledgment stays uncertain."""
    if output is _UNSET:
        return None
    from harness.workflow_mode.tools import compact, run_local_file_tool
    if (not isinstance(output, dict) or set(output) - {"path", "overwrite"}
            or not isinstance(output.get("path"), str) or not output["path"]
            or ("overwrite" in output and not isinstance(output["overwrite"], bool))):
        raise ValueError("manifest_output needs path and optional overwrite boolean")
    request = {"operations": [{"op": "write_json", "path": output["path"],
                "content": portable, "overwrite": output.get("overwrite", False)}]}
    encoded = _encoded_manifest(portable) if portable is not None else None
    try:
        if portable is None:
            response = {"status": "not_started", "error": "Cannot export a portable manifest while a declared path lacks a confirmed absolute path"}
        elif len(encoded) > preflight._MAX_MANIFEST_BYTES:
            response = {"status": "not_started", "error": "Portable manifest exceeds the existing 1 MiB import limit; no non-reimportable manifest was written"}
        else:
            response = await run_local_file_tool(session, "local_fs_batch", request)
    except Exception as exc:
        error = {"type": type(exc).__name__, "message": str(exc)}
        for name in ("code", "details", "action", "actionCode", "observation", "suggested_prompt"):
            if hasattr(exc, name):
                error[name] = json.loads(json.dumps(getattr(exc, name), ensure_ascii=False, default=str))
        response = {"status": "failed", "error": error}
    ref = session.store.receipt(session.reload(), "workflow.material_manifest.export",
                                {"tool": "local_fs_batch", "input": request, "result": response})
    written, issue = ((None, None) if encoded is None else
                     _write_ack(session, request["operations"][0], response, 0, encoded))
    value = {"status": "saved" if written else "unconfirmed", "receiptRef": ref,
             "pointer": "/result", **({key: written[key] for key in
                 ("path", "sha256", "byteSize")} if written else {})}
    if not written:
        value.update(result=compact(response, 1500), note=issue or
                     "No verified save acknowledgment; reconcile the destination before another write")
    return value


def register(session: Any, value: dict[str, Any], ref: str) -> dict[str, Any]:
    """Expose a small locator; never promote a declaration to an access grant."""
    files = value["inputFiles"]
    locator = {"receiptRef": ref, "pointer": "/materialManifest",
        "portableStatus": value["materialManifestStatus"], "note": value["manifestPortabilityNote"],
        "factsPointer": "/files", "fileCount": len(files), "files_ref": manifest_reference(files, ref),
        "importedFrom": value.get("manifestImport"), "export": value.get("manifestExport"),
        "freshness": "preparation-time file facts; validation rechecks permissions and current facts"}
    session.state["materialManifest"] = locator
    return locator


def register_and_save(session: Any, value: dict[str, Any], ref: str) -> dict[str, Any]:
    """Register completed preparation without turning saved IO into a failed tool.

    The receipt must already be durable. A missing registration acknowledgment
    does not invalidate that receipt, its file reference or confirmed exports.
    """
    previous = (copy.deepcopy(session.state["materialManifest"])
                if "materialManifest" in session.state else _UNSET)
    try:
        register(session, value, ref)
        session.save()
    except Exception as exc:
        if previous is _UNSET:
            session.state.pop("materialManifest", None)
        else:
            session.state["materialManifest"] = previous
        return {"status": "registration_failed", "updated": False, "error": str(exc),
                "completedPrepareReceipt": ref,
                "note": "Preparation receipt and confirmed exports remain available. Registration save was not confirmed; reconcile it without repeating confirmed writes."}
    return {"status": "registered", "updated": True}


def register_write_candidates(session: Any, arguments: dict[str, Any], response: Any,
                              receipt_ref: str) -> list[dict[str, Any]]:
    """Index only explicit protocol objects with a fully matching write receipt.

    No file reads, scans, semantic role inference or permission changes occur.
    A candidate is offered for explicit consumption, never selected automatically.
    """
    candidates = []
    for index, operation in enumerate(arguments.get("operations", [])):
        if not isinstance(operation, dict) or operation.get("op") != "write_json":
            continue
        content = operation.get("content")
        if not isinstance(content, dict):
            continue
        try:
            files = manifest_files(content)
            written, _ = _write_ack(session, operation, response, index, _encoded_manifest(content))
        except (OSError, ValueError, KeyError, TypeError):
            continue
        if not written:
            continue
        item = {"files_manifest": {"path": written["path"], "sha256": written["sha256"]},
                "fileCount": len(files), "byteSize": written["byteSize"],
                "declarationEvidence": {"ref": receipt_ref, "pointer": f"/input/operations/{index}/content"},
                "writeEvidence": {"ref": receipt_ref, "pointer": f"/result/results/{response['results'].index(written)}"},
                "status": "declared_saved", "freshness": "No source-file facts or permissions are inherited"}
        candidates.append(item)
    if candidates:
        session.state.setdefault("materialManifestCandidates", []).extend(copy.deepcopy(candidates))
    return candidates


def projection(session: Any) -> dict[str, Any] | None:
    candidates = session.state.get("materialManifestCandidates") or []
    if not candidates:
        return None
    return {"candidates": candidates[-8:], "candidateCount": len(candidates),
            "next": "Choose an applicable candidate explicitly with prepare_materials or validate_input(files_manifest). Saved declarations grant no access and prove no semantic coverage."}


def prepare_tool(session: Any):
    from harness.workflow_mode.tools import A, B, S, compact, result, schema, tool

    async def run(arguments):
        if not any(name in arguments for name in _SOURCES):
            raise ValueError("Provide explicit files, files_ref, files_manifest or manifest_ref")
        value = await inspect(session, arguments)
        value["manifestExport"] = (await export_manifest(session, value["materialManifest"],
                                                         arguments["manifest_output"])
                                    if "manifest_output" in arguments else None)
        issues = [row for row in value["files"] if row.get("status") != "observed"
                  or (row.get("image") or {}).get("status") == "unconfirmed"]
        value["status"] = "observed" if not issues else "unconfirmed"
        value["declarationScope"] = "role/source are caller declarations, not verified semantic identity or access grants; source files must be listed separately to inspect them"
        ref = session.store.receipt(session.reload(), "workflow.materials.prepared", value)
        registration = register_and_save(session, value, ref)
        return result({"status": value["status"], "fileCount": len(value["files"]),
            "fileIssueCount": len(issues), "fileIssues": compact(issues, 6000),
            "files_ref": manifest_reference(value["inputFiles"], ref), "manifestImport": value["manifestImport"],
            "manifestExport": value["manifestExport"], "receiptRef": ref,
            **({"materialRegistration": registration} if not registration["updated"] else {}),
            "evidenceLocations": {key: {"ref": ref, "pointer": "/" + key} for key in
                ("inputFiles", "files", "materialManifest", "fileFactChanges")},
            "next": "Read the declared metadata/source document and only images needed for unresolved semantic choices. Pass files_ref explicitly to validate_input with the prepared business input; validation rechecks current facts and permissions. Exported manifest path/hash can be handed to another task, without grants or cached facts."},
            error=value["status"] != "observed")

    files = A(schema({"path": S, "kind": {"type": "string", "enum": ["file", "image"]},
                      "role": S, "source": S}, ("path",)))
    return tool("prepare_materials", "Prepare an explicit material handoff without business input or browser actions. "
        "Provide one of files, this task's files_ref, files_manifest(path, optional sha256), or manifest_ref "
        "to a schemaVersion:1 manifest object already in an owned receipt (including copied Browser exports). "
        "No directory discovery or role inference. Metadata documents must be their own file entries. "
        "Every source file is checked against current read permission and fresh file facts. Optional manifest_output "
        "exports the same portable declarations using existing local write policy. Neither references nor source declarations grant access.",
        schema({"files": files, "files_ref": schema({"ref": S, "sha256": S}, ("ref", "sha256")),
                "files_manifest": schema({"path": S, "sha256": S}, ("path",)),
                "manifest_ref": schema({"ref": S, "pointer": S}, ("ref",)),
                "manifest_output": schema({"path": S, "overwrite": B}, ("path",))}), run)
