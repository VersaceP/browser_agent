"""Finite local data operations for published Workflow runs.

The Agent supplies data paths and a format, never code, argv or an interpreter.
The existing process runner still enforces OS scopes, stop, timeout and receipts.
"""
from __future__ import annotations

import shutil
from pathlib import Path

from harness.workflow_mode.evidence import evidence_locations
from harness.workflow_mode.local_execution import _FIXED_OPERATION
from harness.workflow_mode.tools import A, B, I, S, compact, result, schema, tool

FORMATS = {
    "jpg": ["-frames:v", "1", "-c:v", "mjpeg", "-f", "image2"],
    "png": ["-frames:v", "1", "-c:v", "png", "-f", "image2"],
    "webp": ["-frames:v", "1", "-c:v", "libwebp", "-f", "image2"],
    "mp4": ["-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-f", "mp4"],
    "mp3": ["-vn", "-c:a", "libmp3lame", "-f", "mp3"],
    "wav": ["-vn", "-c:a", "pcm_s16le", "-f", "wav"],
}


def compile_operation(item: dict, scratch: Path, timeout: int, purpose: str) -> dict:
    """Translate a closed data schema to host-owned arguments."""
    if set(item) - {"operation", "source", "destination", "format", "overwrite"}:
        raise ValueError("Local operations accept data fields only, not commands or code")
    operation = item.get("operation")
    if operation not in {"media_info", "convert_media"}:
        raise ValueError("operation must be media_info or convert_media")
    source = Path(item.get("source") or "")
    if not source.is_absolute():
        raise ValueError("source must be an absolute local path")
    if "\x00" in str(source):
        raise ValueError("source contains NUL")
    name = "ffprobe" if operation == "media_info" else "ffmpeg"
    executable = shutil.which(name, path="/opt/homebrew/bin:/usr/local/bin:/usr/bin")
    if not executable:
        raise FileNotFoundError(f"{name} is unavailable; no automatic installation or command fallback")
    base = [executable, "-v", "error", "-protocol_whitelist", "file,pipe"]
    writes = []
    if operation == "media_info":
        if any(key in item for key in ("destination", "format", "overwrite")):
            raise ValueError("media_info has no output or conversion options")
        argv = [*base, "-show_format", "-show_streams", "-of", "json", "-i", str(source)]
    else:
        target = Path(item.get("destination") or "")
        if not target.is_absolute() or "\x00" in str(target):
            raise ValueError("destination must be an absolute local path")
        if source.resolve() == target.resolve():
            raise ValueError("conversion preserves its source; use a different destination")
        fmt = item.get("format")
        if fmt not in FORMATS:
            raise ValueError(f"format must be one of {sorted(FORMATS)}")
        if not isinstance(item.get("overwrite", False), bool):
            raise ValueError("overwrite must be boolean")
        argv = [*base, "-nostdin", "-y" if item.get("overwrite") else "-n",
                "-i", str(source), *FORMATS[fmt], str(target)]
        writes = [str(target)]
    return {"argv": argv, "cwd": str(scratch), "read_paths": [str(source)],
            "write_paths": writes, "timeout_seconds": timeout, "purpose": purpose}


def operation_tool(session):
    async def run(arguments, *, signal=None):
        items = arguments["items"]
        if not items:
            raise ValueError("Give at least one local operation")
        if not str(arguments["purpose"]).strip():
            raise ValueError("purpose is required")
        timeout = arguments.get("timeout_seconds", 120)
        if not 1 <= timeout <= 3600:
            raise ValueError("timeout_seconds must be 1..3600 per operation")
        scratch = Path(session.logger.task_dir).resolve() / "scratchpad" / "local-operations"
        scratch.mkdir(parents=True, exist_ok=True)
        # Preflight every independent item before running any of them.
        prepared, findings = [], []
        for index, item in enumerate(items):
            try:
                prepared.append((index, compile_operation(item, scratch, timeout, arguments["purpose"])))
            except (ValueError, OSError) as exc:
                findings.append({"index": index, "status": "rejected", "error": str(exc)})
        if findings:
            findings.extend({"index": index, "status": "not_started", "tool_was_executed": False,
                             "reason": "Batch preflight found invalid operations"}
                            for index, _ in prepared)
            findings.sort(key=lambda item: item["index"])
        else:
            for index, command in prepared:
                if session._stop_flag() or (signal is not None and signal.is_cancelled()):
                    findings.extend({"index": i, "status": "cancelled", "tool_was_executed": False}
                                    for i, _ in prepared if i >= index)
                    break
                outcome = await session.local_runner.run(command, signal=signal, _authority=_FIXED_OPERATION)
                findings.append({"index": index, **outcome})
        status = "completed" if findings and all(f["status"] == "completed" for f in findings) else "incomplete"
        saved = {"tool": "run_local", "input": arguments, "status": status, "results": findings}
        ref = session.store.receipt(session.reload(), "workflow.local_operations.result", saved)
        session.save()
        return result({"status": status, "results": compact(findings, 8000), "receiptRef": ref,
                       "evidenceLocations": evidence_locations(ref, saved, {
                           "results": "/results", "input": "/input"}),
                       "next": "Verify actual output files. A process exit code alone is not delivery."},
                      error=status != "completed")

    item = schema({"operation": {"type": "string", "enum": ["media_info", "convert_media"]},
                   "source": S, "destination": S,
                   "format": {"type": "string", "enum": sorted(FORMATS)}, "overwrite": B},
                  ("operation", "source"))
    return tool("run_local", "Run fixed local data operations in one batch: inspect media with ffprobe, "
                "or convert media with ffmpeg. Supply local paths and format only; no scripts, commands, "
                "filters, network or business automation. Sources are preserved. Create destination "
                "directories with local_fs_batch. Existing task path authorization, OS sandbox, receipts, "
                "per-operation timeout, cancellation and task stop apply. Missing capabilities belong "
                "to workflow-edit, not an improvised script.",
                schema({"items": A(item), "timeout_seconds": I, "purpose": S}, ("items", "purpose")),
                run, cancellable=True)
