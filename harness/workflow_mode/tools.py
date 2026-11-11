"""Agent tools for Workflow task, author and coordinator sessions.

Each tool returns JSON facts with a receipt reference when it touched the
platform. Errors are returned as tool errors; they never terminate the loop.
"""
from __future__ import annotations

import asyncio
import base64
import copy
import hashlib
import json
import mimetypes
from pathlib import Path
from typing import Any, Awaitable, Callable

from harness.workflow_mode.evidence import (child_pointer, evidence_locations, evidence_reference,
                                           pointer, read_evidence, value_index, short_reference)

from harness._vendor.tau_agent import AgentTool
from harness._vendor.tau_agent.messages import ImageContent, TextContent
from harness._vendor.tau_agent.tools import AgentToolResult

S = {"type": "string"}
O = {"type": "object"}
I = {"type": "integer"}
B = {"type": "boolean"}
A = lambda items=S: {"type": "array", "items": items}
_MAX_INLINE = 6000
_MAX_IMAGE = 4 * 1024 * 1024


def schema(properties: dict[str, Any], required: tuple[str, ...] = ()) -> dict[str, Any]:
    return {"type": "object", "properties": properties, "required": list(required),
            "additionalProperties": False}


def result(value: Any, *, error: bool = False, terminate: bool = False,
           images: list[ImageContent] | None = None) -> AgentToolResult:
    if isinstance(value, dict):
        alias = short_reference(value.get("receiptRef") or value.get("ref"))
        if alias:
            value = {**value, "shortRef": alias}
    text = json.dumps(value, ensure_ascii=False, default=str)
    return AgentToolResult(content=[TextContent(text=text), *(images or [])],
                           details={"is_error": error}, terminate=terminate)


def compact(value: Any, limit: int = _MAX_INLINE) -> Any:
    """Bounded model view; full payloads stay in receipts."""
    text = json.dumps(value, ensure_ascii=False, default=str)
    if len(text) <= limit:
        return value
    if isinstance(value, dict):
        scalars = {k: v for k, v in value.items() if isinstance(v, (str, int, float, bool)) and len(str(v)) < 300}
        return {"truncated": True, "chars": len(text), "keys": sorted(value)[:80], "scalars": scalars,
                "hint": "Read the full value with read_receipt(ref, pointer)."}
    if isinstance(value, list):
        return {"truncated": True, "chars": len(text), "length": len(value),
                "first": compact(value[0], limit // 3) if value else None,
                "hint": "Read saved items with read_receipt(ref, pointer, offset, limit), or search."}
    return {"truncated": True, "chars": len(text), "preview": text[:limit]}


def observation_view(value: dict[str, Any], limit: int = 16000) -> dict[str, Any]:
    """Keep receipt addresses outside truncation; serve already hydrated evidence."""
    view = {**value, "data": compact(value.get("data"), limit)}
    data = value.get("data")
    if isinstance(data, dict) and isinstance(view["data"], dict) and view["data"].get("truncated"):
        view["dataIndex"] = {key: value_index(item, child_pointer("/response/data", key))
                             for key, item in data.items()}
    if isinstance(data, dict) and ("lines" in data or "records" in data):
        # The client verified and copied the artifact before its host path expired.
        # Keep the platform's original guidance in the receipt, not in this view.
        view["observation"] = "Page evidence is saved in this task's receipt."
        view["suggested_prompt"] = (
            "Use evidenceLocations with read_receipt. Page arrays with offset/limit, "
            "or search literal terms; no host artifact path is needed. Search absence "
            "only covers the selected saved data, not unobserved page content.")
        if isinstance(data.get("lines"), list):
            view["preview"] = read_evidence(data["lines"], "/response/data/lines",
                                           limit=8, max_chars=2400)
    return view


def image_content(path: Path, *, max_bytes: int = _MAX_IMAGE) -> ImageContent:
    body = path.read_bytes()
    if len(body) > max_bytes:
        raise ValueError(f"Image is {len(body)} bytes; limit {max_bytes}")
    mime = mimetypes.guess_type(path.name)[0] or "image/png"
    if not mime.startswith("image/"):
        raise ValueError("Not an image file")
    return ImageContent(data=base64.b64encode(body).decode(), mime_type=mime)


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def receipt_tool(session) -> AgentTool:
    return tool("read_receipt",
        "Read owned saved evidence using returned shortRef or canonical ref and exact pointer. Arrays/objects support offset/limit "
        "(entries); strings use offset (characters). search is a list of literal terms (OR, "
        "case-insensitive); matches retain pointers and only cover saved evidence. "
        "pointer/pointers are receipt-root addresses. For package summary pointers relative to output, "
        "pass ref=summaryEvidenceScope and relative_pointer, or relative_pointers for a batch. "
        "An explicit {ref,pointer} scope is required; modes cannot be mixed. Returned addresses are receipt-root.",
        schema({"ref": {"anyOf": [S, schema({"ref": S, "pointer": S}, ("ref",))]},
                "pointer": S, "pointers": A(), "relative_pointer": S, "relative_pointers": A(),
                "max_chars": I, "offset": I,
                "limit": I, "search": A()}, ("ref",)), session._read_receipt)


def _check(value: Any, rule: dict[str, Any], path: str) -> None:
    kinds = rule.get("type")
    if kinds is not None:
        allowed = kinds if isinstance(kinds, list) else [kinds]
        match = {"object": lambda x: isinstance(x, dict), "array": lambda x: isinstance(x, list),
                 "string": lambda x: isinstance(x, str),
                 "integer": lambda x: isinstance(x, int) and not isinstance(x, bool),
                 "number": lambda x: isinstance(x, (int, float)) and not isinstance(x, bool),
                 "boolean": lambda x: isinstance(x, bool), "null": lambda x: x is None}
        if not any(match[k](value) for k in allowed):
            raise ValueError(f"{path}: expected {allowed}")
    if "enum" in rule and value not in rule["enum"]:
        raise ValueError(f"{path}: must be one of {rule['enum']}")
    if isinstance(value, dict):
        props = rule.get("properties") or {}
        missing = set(rule.get("required") or []) - value.keys()
        if missing:
            raise ValueError(f"{path}: missing {sorted(missing)}")
        if rule.get("additionalProperties") is False:
            unknown = value.keys() - props.keys()
            if unknown:
                raise ValueError(f"{path}: unknown {sorted(unknown)}")
        for key, child in value.items():
            if key in props:
                _check(child, props[key], f"{path}.{key}")
    if isinstance(value, list) and isinstance(rule.get("items"), dict):
        for index, child in enumerate(value):
            _check(child, rule["items"], f"{path}[{index}]")


class ToolResultError(ValueError):
    """Preserve a failed dependency's structured feedback at the tool boundary."""

    def __init__(self, value: dict[str, Any]):
        self.value = value
        super().__init__(str(value.get("error") or value.get("status") or "Tool dependency failed"))


def tool(name: str, description: str, parameters: dict[str, Any],
         handler: Callable[..., Awaitable[AgentToolResult]], *, cancellable: bool = False) -> AgentTool:
    async def execute(call_id, arguments, signal=None, on_update=None):
        if signal is not None and signal.is_cancelled():
            return result({"status": "cancelled"}, error=True)
        try:
            return await handler(dict(arguments), signal=signal) if cancellable else await handler(dict(arguments))
        except ToolResultError as exc:
            return result(exc.value, error=True)
        except (ValueError, KeyError, PermissionError, FileNotFoundError, IndexError) as exc:
            return result({"status": "rejected", "error": f"{type(exc).__name__}: {exc}"}, error=True)

    def prepare(value: object) -> dict[str, Any]:
        if not isinstance(value, dict):
            raise ValueError("arguments must be an object")
        _check(value, parameters, "arguments")
        return dict(value)

    return AgentTool(name=name, label=name, description=description, parameters=parameters,
                     execute_fn=execute, prepare_arguments=prepare, execution_mode="sequential")


async def run_local_file_tool(session: Any, name: str, arguments: dict[str, Any], *, signal=None) -> dict[str, Any]:
    """Reuse the Browser handlers and the task-scoped path authorization."""
    from harness.tools.browser_tools.dispatch import BROWSER_TOOLS
    from harness.tools.path_authorization import authorize_tool_call
    from harness.tools.registry import ToolContext
    call = {"name": name, "input": arguments}
    with session.usage.waiting_for_human():
        denied = await authorize_tool_call(session, call)
    if denied is not None:
        return denied
    action = BROWSER_TOOLS.get(name)
    def cancelled():
        stop = getattr(session, "_stop_flag", None)
        return bool((signal is not None and signal.is_cancelled()) or (callable(stop) and stop()))
    return await action.handler(ToolContext(agent=session, tool_call=call, tool_input=arguments,
                                           cancelled=cancelled))


def local_file_tools(session: Any) -> list[AgentTool]:
    from harness.tools.browser_tools.dispatch import BROWSER_TOOLS
    tools = []
    for name in ("local_fs_authorize", "local_fs_search", "local_fs_read", "local_fs_batch"):
        spec = BROWSER_TOOLS.get(name).to_tool_spec()

        async def handler(arguments, _name=name, *, signal=None):
            value = await run_local_file_tool(session, _name, arguments, signal=signal)
            saved = {"tool": _name, "input": arguments, "result": value}
            ref = session.store.receipt(session.reload(), "workflow.local_file.result",
                                        saved)
            notes = None
            handoff = None
            previous_notes = session.state.get("workNotes")
            previous_candidates = (copy.deepcopy(session.state.get("materialManifestCandidates"))
                                   if _name == "local_fs_batch" else None)
            if _name == "local_fs_batch" and isinstance(value, dict):
                from harness.workflow_mode import work_notes
                session.state.setdefault("fileManifests", []).append({
                    "receiptRef": ref, "manifestPath": value.get("manifestPath"),
                    "fileCount": len(value.get("files") or []), "failedCount": value.get("failedCount")})
                try:
                    notes = work_notes.register_write(session, arguments, value, ref)
                except Exception as exc:
                    if previous_notes is None:
                        session.state.pop("workNotes", None)
                    else:
                        session.state["workNotes"] = previous_notes
                    notes = {"status": "registration_failed", "updated": False,
                             "error": str(exc), "completedWriteReceipt": ref}
                from harness.workflow_mode import materials
                try:
                    candidates = materials.register_write_candidates(session, arguments, value, ref)
                    if candidates:
                        handoff = {"status": "registered", "candidates": candidates}
                except Exception as exc:
                    if previous_candidates is None:
                        session.state.pop("materialManifestCandidates", None)
                    else:
                        session.state["materialManifestCandidates"] = previous_candidates
                    handoff = {"status": "registration_failed", "error": str(exc),
                               "completedWriteReceipt": ref}
            try:
                session.save()
            except Exception as exc:
                notes_updated = bool(notes and notes.get("updated"))
                if not notes_updated and handoff is None:
                    raise
                if notes_updated:
                    if previous_notes is None:
                        session.state.pop("workNotes", None)
                    else:
                        session.state["workNotes"] = previous_notes
                    notes = {"status": "registration_failed", "updated": False,
                             "error": str(exc), "completedWriteReceipt": ref}
                if handoff is not None:
                    if previous_candidates is None:
                        session.state.pop("materialManifestCandidates", None)
                    else:
                        session.state["materialManifestCandidates"] = previous_candidates
                    handoff = {"status": "registration_failed", "error": str(exc),
                               "completedWriteReceipt": ref}
            if notes is not None:
                notes = {**notes, "current": work_notes.projection(session)}
            failed = isinstance(value, dict) and value.get("status") in {"failed", "partial", "needs_human", "cancelled"}
            view = compact(value, 20000)
            envelope = {**view} if isinstance(view, dict) else {"result": view}
            if _name == "local_fs_search" and isinstance(value, dict):
                # Continuation and coverage remain usable even when matches need a receipt read.
                envelope = {**value, "results": compact(value.get("results", []), 16000)}
            return result({**envelope, "receiptRef": ref,
                            **({"workNotes": notes} if notes is not None else {}),
                            **({"materialHandoff": handoff} if handoff is not None else {}),
                           "evidenceLocations": evidence_locations(ref, saved, {
                               "result": "/result", "results": "/result/results",
                               "content": "/result/content", "files": "/result/files",
                               "input": "/input"})}, error=failed)
        tools.append(tool(name, spec["description"], spec["input_schema"], handler,
                          cancellable=name == "local_fs_search"))

    async def view_image(arguments):
        from harness.tools.path_authorization import resolve_authorized_path
        raw = arguments["path"]
        with session.usage.waiting_for_human():
            denied = await run_local_file_tool(session, "local_fs_authorize", {"paths": [raw], "mode": "read"})
        if denied.get("status") != "authorized":
            return result(denied, error=True)
        path = resolve_authorized_path(session, raw, mode="read")
        content = image_content(Path(path))
        return result({"path": str(path), "bytes": Path(path).stat().st_size}, images=[content])

    tools.append(tool("view_image", "View one authorized local image (product materials, saved screenshots). "
                      "Use it to decide image-to-variant mappings yourself; report uncertainty instead of guessing.",
                      schema({"path": S}, ("path",)), view_image))

    if session.profile != "author":
        from harness.workflow_mode.local_operations import operation_tool
        tools.append(operation_tool(session))
        return tools

    async def run_local(arguments, *, signal=None):
        value = await session.local_runner.run(arguments, signal=signal)
        return result(value, error=value.get("status") != "completed")

    tools.append(tool("run_local", "Run a local program/script directly in this Workflow context, without "
        "a coding delegate. argv is an argument array, not shell text; python/python3 uses the Harness "
        "interpreter. cwd is read-authorized; declare additional read_paths and write_paths explicitly. "
        "macOS enforces these scopes (plus system/runtime libraries) in an OS sandbox; network and task "
        "control/package writes are denied. No unsandboxed fallback. A WRITE scope does not grant READ. "
        "Use local_fs_batch to create/write scripts and inspect/hash outputs; exit code alone is not "
        "proof of delivery. Supports task stop, cancellation and timeout; all child processes end when "
        "the call ends. Full stdout/stderr and scope stay in a receipt; large output is bounded.",
        schema({"argv": A(), "cwd": S, "read_paths": A(), "write_paths": A(),
                "stdin": S, "timeout_seconds": I, "purpose": S},
               ("argv", "cwd", "read_paths", "write_paths", "purpose")), run_local, cancellable=True))
    return tools
