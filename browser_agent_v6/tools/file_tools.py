"""File IO tools — read_file / write_file / list_files / publish_artifact.

No manifest.json — file is the source of truth (lesson from task_1777979388).
publish_artifact simply writes to shared/<name> without any side bookkeeping.
"""
import json
from pathlib import Path
from typing import Any, Dict, Tuple

from .base import ToolSpec, ToolContext
from sandbox.path_guard import PathGuard, PathEscapeError


# ──────────────────────────────────
# read_file: 4-tier progressive read — 10K → 20K → 50K → Haiku summary
# ──────────────────────────────────
# Design: demand-loaded reads; above 50K we delegate to a cheap summarizer to
# preserve information density without blowing main context.
#   Tier 1: max_chars <= 10000  → head sample, validate schema
#   Tier 2: max_chars <= 20000  → progressive expansion (more records)
#   Tier 3: max_chars <= 50000  → progressive max for raw content shown to LLM
#   Tier 4: max_chars >  50000  → summarizer (Haiku-class) compresses full file to
#                                  <= 50000 chars structured summary
#                                  (head/tail 3 + schema + field-fill rates + anomalies)
#
# Co-operates with the supersede mechanism: each successful read makes
# execution_loop replace prior same-path tool_result content with a short stub,
# so the main context only retains the latest read. We therefore do NOT do
# "redundant" detection — if the LLM wants to re-read it's legitimate (it can no
# longer see the prior content); the system honestly re-reads and supersedes.
#
# history: only provides prior tool_use_id list for supersede + hard-cap counter.
#   key: (session_id, agent_type, abs_path) -> [{"tool_use_id", ...}]
# Summary cache: avoid wasting Haiku tokens on repeated summaries of the same file.
#   key: (session_id, agent_type, abs_path, mtime) -> {"summary", "model", "original_size"}
_READ_FILE_CALLS: Dict[Tuple[str, str, str], list] = {}
_SUMMARY_CACHE: Dict[Tuple[str, str, str, float], dict] = {}
_READ_FILE_TIER_BOUNDARIES = (10_000, 20_000, 50_000)
_READ_FILE_SUMMARY_THRESHOLD = 50_000
_READ_FILE_SUMMARY_TARGET = 50_000
_READ_FILE_HARD_CAP = 6        # 4 progressive + 2 buffer; true-loop safety net


def clear_read_history(session_id: str = "") -> None:
    """Clear read_file history + summary cache (in-place; dict references stay stable).

    No arg  -> clear everything (test helper)
    session_id -> clear only that session's entries (called by spawner.clear_session)
    """
    if not session_id:
        _READ_FILE_CALLS.clear()
        _SUMMARY_CACHE.clear()
        return
    for k in [k for k in _READ_FILE_CALLS if k[0] == session_id]:
        del _READ_FILE_CALLS[k]
    for k in [k for k in _SUMMARY_CACHE if k[0] == session_id]:
        del _SUMMARY_CACHE[k]


def _make_guard(ctx: ToolContext) -> PathGuard:
    """Build the path guard for the current agent. Allowed: worktree (rw) + shared (rw)."""
    return PathGuard(
        worktree=ctx.worktree,
        shared_dirs=[ctx.shared_dir] if ctx.shared_dir else [],
    )


def _normalize_path(raw: str, ctx: ToolContext) -> str:
    """Remap an LLM-written 'shared/foo.json' to the absolute path of the session share dir.

    From the LLM's perspective, 'shared/' is the conventional cross-agent channel.
    Physically, 'shared/' is not inside each agent's worktree but a sibling directory
    under the session root. We remap once here so the LLM doesn't need to learn that.
    """
    if ctx.shared_dir and (raw.startswith("shared/") or raw == "shared"):
        from pathlib import Path
        rel = raw[len("shared"):].lstrip("/\\")
        return str(Path(ctx.shared_dir) / rel) if rel else ctx.shared_dir
    return raw


# ──────────────────────────────────
# read_file
# ──────────────────────────────────

def _read_file_handler(tool_input: dict, ctx: ToolContext) -> dict:
    guard = _make_guard(ctx)
    raw = _normalize_path(tool_input["path"], ctx)
    path = guard.resolve(raw, mode="read")
    if not path.exists():
        raise FileNotFoundError(f"{tool_input['path']} → {path}")
    if path.is_dir():
        raise IsADirectoryError(f"{tool_input['path']} is a directory; use list_files")

    max_chars = int(tool_input.get("max_chars", _READ_FILE_TIER_BOUNDARIES[0]))
    key = (ctx.session_id or "", ctx.agent_type or "", str(path))
    history = _READ_FILE_CALLS.setdefault(key, [])

    # ── Single hard limit: > 6 calls per (session, agent, path) is treated as a loop ──
    if len(history) >= _READ_FILE_HARD_CAP:
        return {
            "path": str(path),
            "throttled": True,
            "calls_so_far": len(history),
            "hard_cap": _READ_FILE_HARD_CAP,
            "msg": (
                f"STOP: read_file for the same path has reached the hard cap of "
                f"{_READ_FILE_HARD_CAP} calls. Decide based on what you have already "
                f"read and end_turn."
            ),
        }

    file_stat = path.stat()
    file_size = file_stat.st_size
    file_mtime = file_stat.st_mtime
    text = path.read_text(encoding="utf-8")
    actual_size = len(text)

    # ── Tier 4: max_chars > 50K and the file is large too → summarizer (with cache) ──
    use_summary = (
        max_chars > _READ_FILE_SUMMARY_THRESHOLD
        and actual_size > _READ_FILE_SUMMARY_THRESHOLD
        and ctx.summarizer is not None
    )

    if use_summary:
        cache_key = (ctx.session_id or "", ctx.agent_type or "", str(path), file_mtime)

        # Cache hit → return cached summary, no Haiku re-call
        cached = _SUMMARY_CACHE.get(cache_key)
        if cached:
            _mark_supersede(ctx, history)
            history.append({
                "tool_use_id": ctx.tool_use_id,
                "max_chars": max_chars,
                "returned_chars": cached["summary_size"],
                "truncated": False,
                "summarized": True,
            })
            return {
                "path": str(path),
                "size_bytes": file_size,
                "summarized": True,
                "cached": True,
                "summary": cached["summary"],
                "summary_size_chars": cached["summary_size"],
                "original_size_chars": cached["original_size"],
                "summarizer_model": cached["model"],
                "calls_so_far": len(history),
                "msg": "Summary cache hit for this session; returned cached summary (zero summarizer cost).",
            }

        # First summary: invoke Haiku and write to cache
        try:
            summary_payload = _summarize_file_content(
                text, str(path), ctx.summarizer, target_chars=_READ_FILE_SUMMARY_TARGET,
            )
            summary_text = summary_payload.get("summary", "")
            _SUMMARY_CACHE[cache_key] = {
                "summary": summary_text,
                "summary_size": len(summary_text),
                "original_size": actual_size,
                "model": summary_payload.get("model", "?"),
            }
            _mark_supersede(ctx, history)
            history.append({
                "tool_use_id": ctx.tool_use_id,
                "max_chars": max_chars,
                "returned_chars": len(summary_text),
                "truncated": False,
                "summarized": True,
            })
            return {
                "path": str(path),
                "size_bytes": file_size,
                "summarized": True,
                "cached": False,
                "summary": summary_text,
                "summary_size_chars": len(summary_text),
                "original_size_chars": actual_size,
                "summarizer_model": summary_payload.get("model"),
                "calls_so_far": len(history),
                "msg": (
                    f"File is {actual_size} chars (> {_READ_FILE_SUMMARY_THRESHOLD} threshold); "
                    f"summarizer compressed it ({actual_size} -> {len(summary_text)} chars). "
                    f"Summary contains head/tail/schema/field-fill rates/anomalies. "
                    f"Subsequent same-file reads in this session hit cache (no summarizer re-call)."
                ),
            }
        except Exception as e:
            # Summary failed → fall back to truncated 50K raw (don't crash)
            text = text[:_READ_FILE_SUMMARY_TARGET]
            _mark_supersede(ctx, history)
            history.append({
                "tool_use_id": ctx.tool_use_id,
                "max_chars": max_chars,
                "returned_chars": len(text),
                "truncated": True,
                "summarized": False,
            })
            return {
                "path": str(path),
                "size_bytes": file_size,
                "content": text,
                "truncated": True,
                "summarizer_failed": str(e)[:200],
                "calls_so_far": len(history),
                "msg": (
                    f"summarizer call failed ({type(e).__name__}); fell back to "
                    f"truncated {_READ_FILE_SUMMARY_TARGET} chars of raw content."
                ),
            }

    # Tier 1-3: normal truncated read
    truncated = False
    if actual_size > max_chars:
        text = text[:max_chars]
        truncated = True

    # Successful read → all prior same-path tool_use_ids should be superseded
    # (the new read covers/replaces the old content)
    _mark_supersede(ctx, history)
    history.append({
        "tool_use_id": ctx.tool_use_id,
        "max_chars": max_chars,
        "returned_chars": len(text),
        "truncated": truncated,
        "summarized": False,
    })

    # Next-step hint for the LLM
    if truncated:
        if max_chars < _READ_FILE_TIER_BOUNDARIES[1]:
            next_hint = f"truncated=true -> if you need more, re-call with max_chars=20000 (tier 2)"
        elif max_chars < _READ_FILE_TIER_BOUNDARIES[2]:
            next_hint = f"truncated=true -> if you need more, re-call with max_chars=50000 (tier 3)"
        else:
            next_hint = (
                f"truncated=true and tier-3 max ({_READ_FILE_TIER_BOUNDARIES[2]}) is reached. "
                f"For the full file, re-call with max_chars > {_READ_FILE_SUMMARY_THRESHOLD}; "
                f"the system will return a structured summary (head/tail/schema/stats/anomalies)."
            )
    else:
        next_hint = "truncated=false -> full file is in context. Next same-path read will be superseded."

    return {
        "path": str(path),
        "size_bytes": file_size,
        "content": text,
        "truncated": truncated,
        "calls_so_far": len(history),
        "next_step_hint": next_hint,
    }


# ──────────────────────────────────
# Supersede tagging: cause this successful read_file call to mark prior same-path
# tool_results as superseded (their content gets replaced with a short stub by
# execution_loop).
# ──────────────────────────────────
# The price of progressive reads (10K → 20K → 50K → summary) would otherwise be
# 4 accumulated copies in main context. Fix: at each successful read, attach the
# prior tool_use_ids to ctx.extra. execution_loop, before appending the new
# tool_result, scans messages and replaces those ids' tool_result content with a
# brief stub. The LLM only ever sees the latest read.

def _mark_supersede(ctx: ToolContext, history: list) -> None:
    """Collect prior tool_use_ids from history into ctx.extra for execution_loop.

    The old tool_result contents will be replaced with a short stub by the loop.
    No history (this is the first call) → nothing to supersede; do nothing.
    """
    prior_ids = [h.get("tool_use_id") for h in history if h.get("tool_use_id")]
    if not prior_ids or ctx.extra is None:
        return
    # Accumulate (multiple read_file calls in one turn both need handling)
    existing = ctx.extra.setdefault("supersede_tool_use_ids", [])
    for tid in prior_ids:
        if tid not in existing:
            existing.append(tid)


# ──────────────────────────────────
# Summarizer — fallback for large read_file content
# ──────────────────────────────────

_SUMMARY_SYSTEM = """You are a data-validation summarization assistant. Given a data
file (usually a JSON array or plain text), produce a structured summary whose length
is strictly under the user-specified target_chars.

Requirements:
1. **Keep head 3 records** (full, showing schema detail)
2. **Keep tail 3 records** (full, confirming consistency to the end)
3. **Sample 3-5 middle records** (so the verifier sees data diversity)
4. **Explicit schema**: list field names + inferred types + nullability
5. **Statistics**: total record count, per-field fill rate (non-null ratio)
6. **Anomalies**: list any odd-looking fields, fields with dense nulls, format inconsistencies
7. **One-line summary**: whether data looks complete and whether anything obvious is wrong

**Important**: output must be valid JSON, starting with `{` and ending with `}`. Do
NOT wrap it in a markdown code fence.
"""

_SUMMARY_USER_TPL = """Summarize the following file content (original {original_size} chars,
target summary <= {target_chars} chars).

---FILE START---
{content}
---FILE END---

Emit JSON summary with these fields: schema / total_records / head_3 / tail_3 /
middle_samples / field_fill_rates / anomalies / summary."""


def _summarize_file_content(text: str, path_str: str, summarizer, target_chars: int) -> dict:
    """Synchronously invoke the summarizer (Haiku-class) to produce a structured summary.

    `summarizer` is a BaseLLMProvider instance whose generate_response is async. We
    call it from a sync handler that's already running inside asyncio.to_thread, so
    we can't use asyncio.run directly (could collide with the outer loop). Fix:
    spin up a fresh event loop for this single call.
    """
    import asyncio as _asyncio

    user_msg = _SUMMARY_USER_TPL.format(
        original_size=len(text),
        target_chars=target_chars,
        content=text,
    )

    # Run on a fresh loop to avoid conflict with the outer to_thread loop
    def _call_in_new_loop():
        loop = _asyncio.new_event_loop()
        try:
            return loop.run_until_complete(summarizer.generate_response(
                system_prompt=_SUMMARY_SYSTEM,
                messages=[{"role": "user", "content": user_msg}],
                tools=[],
            ))
        finally:
            loop.close()

    text_out, _tool_uses, _stop, _usage = _call_in_new_loop()

    # Force-truncate to target_chars (in case the summarizer didn't honor the limit)
    summary_text = (text_out or "").strip()
    if len(summary_text) > target_chars:
        summary_text = summary_text[:target_chars] + "\n... [summary truncated to target_chars]"

    return {
        "summary": summary_text,
        "model": getattr(getattr(summarizer, "config", None), "model_id", "?"),
    }


READ_FILE = ToolSpec(
    name="read_file",
    description=(
        "Read a text file from worktree or shared/. Default max_chars=10000.\n"
        "\n"
        "FOUR-TIER PROGRESSIVE STRATEGY:\n"
        "  Tier 1: max_chars=10000   ← head sample, validate schema (start here)\n"
        "  Tier 2: max_chars=20000   ← if truncated and need more\n"
        "  Tier 3: max_chars=50000   ← progressive max for raw content\n"
        "  Tier 4: max_chars > 50000 ← system invokes summarizer (Haiku-class) and returns\n"
        "          {summarized:true, cached?, summary, schema, head_3, tail_3,\n"
        "           field_fill_rates, anomalies}, summary <= 50000 chars\n"
        "          (subsequent same-file calls hit cache: cached=true, no Haiku re-call)\n"
        "\n"
        "AUTOMATIC SUPERSEDE: each successful read_file makes prior tool_results for the SAME\n"
        "path collapse to a short stub. So progressive reads (10K→20K→50K→summary) keep only\n"
        "the latest content in your context — you don't pay for accumulated copies.\n"
        "\n"
        "HARD CAP: 6 calls per (session, agent, path). On call 7 you get {throttled:true}\n"
        "— treat it as a definitive 'STOP, decide now'."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Relative to worktree, or 'shared/...' for shared dir"},
            "max_chars": {"type": "integer", "default": 10000},
        },
        "required": ["path"],
    },
    handler=_read_file_handler,
    readonly=True,
)


# ──────────────────────────────────
# write_file
# ──────────────────────────────────

def _write_file_handler(tool_input: dict, ctx: ToolContext) -> dict:
    guard = _make_guard(ctx)
    raw = _normalize_path(tool_input["path"], ctx)
    path = guard.resolve(raw, mode="write")
    path.parent.mkdir(parents=True, exist_ok=True)
    content = tool_input["content"]
    path.write_text(content, encoding="utf-8")
    return {"path": str(path), "size_bytes": len(content.encode("utf-8"))}


WRITE_FILE = ToolSpec(
    name="write_file",
    description=(
        "Write text to worktree or shared/. Creates parent dirs. "
        "For deliverables (the 'output' declared in plan), use publish_artifact instead — "
        "it writes to shared/ and is the contract handoff point."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "content": {"type": "string"},
        },
        "required": ["path", "content"],
    },
    handler=_write_file_handler,
)


# ──────────────────────────────────
# list_files
# ──────────────────────────────────

def _list_files_handler(tool_input: dict, ctx: ToolContext) -> dict:
    guard = _make_guard(ctx)
    raw = _normalize_path(tool_input.get("path", "."), ctx)
    base = guard.resolve(raw, mode="read")
    if not base.exists():
        return {"path": str(base), "files": []}
    recursive = bool(tool_input.get("recursive", False))
    pattern = "**/*" if recursive else "*"

    entries = []
    for p in sorted(base.glob(pattern)):
        if p.is_file():
            entries.append({
                "path": str(p.relative_to(base)),
                "size": p.stat().st_size,
                "type": "file",
            })
        elif p.is_dir():
            entries.append({"path": str(p.relative_to(base)) + "/", "type": "dir"})
    return {"path": str(base), "count": len(entries), "files": entries}


LIST_FILES = ToolSpec(
    name="list_files",
    description="List files in worktree (or specific subdir). Set recursive=true for deep walk.",
    input_schema={
        "type": "object",
        "properties": {
            "path": {"type": "string", "default": ".", "description": "Subdirectory (default = worktree root)"},
            "recursive": {"type": "boolean", "default": False},
        },
    },
    handler=_list_files_handler,
    readonly=True,
)


# ──────────────────────────────────
# publish_artifact — write to shared/<name>; no manifest
# ──────────────────────────────────

def _publish_artifact_handler(tool_input: dict, ctx: ToolContext) -> dict:
    if not ctx.shared_dir:
        raise RuntimeError("ctx.shared_dir is not set — cross-agent delivery requires shared/")

    name = tool_input["name"]
    if "/" in name or "\\" in name or name.startswith("."):
        raise ValueError(f"name must be a simple filename (no path separators or leading '.'): {name!r}")

    target = Path(ctx.shared_dir) / name
    target.parent.mkdir(parents=True, exist_ok=True)

    # Two sources: content (inline) / spill_path (copy from a worktree file)
    if "content" in tool_input:
        body = tool_input["content"]
        if isinstance(body, (dict, list)):
            target.write_text(json.dumps(body, ensure_ascii=False, indent=2), encoding="utf-8")
        else:
            target.write_text(str(body), encoding="utf-8")
    elif "spill_path" in tool_input:
        guard = _make_guard(ctx)
        raw = _normalize_path(tool_input["spill_path"], ctx)
        src = guard.resolve(raw, mode="read")
        if not src.exists():
            raise FileNotFoundError(f"spill_path does not exist: {src}")
        if src.is_dir():
            raise IsADirectoryError(f"spill_path is a directory, cannot publish: {src}")
        # Don't interpret content; copy raw bytes
        target.write_bytes(src.read_bytes())
    else:
        raise ValueError("must provide either 'content' or 'spill_path'")

    size = target.stat().st_size
    return {
        "name": name,
        "path": str(target),
        "size_bytes": size,
        "description": tool_input.get("description", ""),
    }


PUBLISH_ARTIFACT = ToolSpec(
    name="publish_artifact",
    description=(
        "Publish a deliverable to shared/<name> (the cross-agent data channel). "
        "Source: either 'content' (inline str/dict/list) or 'spill_path' (copy from worktree file). "
        "name MUST be a simple basename (no /, no .. — e.g. 'ai_tools.json' not 'shared/ai_tools.json'). "
        "Same-name republish overwrites — fine for incremental batch publishing."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "Output basename (e.g. 'products.json')"},
            "content": {"description": "Inline content (str / dict / list). Mutually exclusive with spill_path."},
            "spill_path": {"type": "string", "description": "Worktree-relative file to copy as the artifact. Mutually exclusive with content."},
            "description": {"type": "string", "description": "Optional human-readable note"},
        },
        "required": ["name"],
    },
    handler=_publish_artifact_handler,
)


ALL_FILE_TOOLS = [READ_FILE, WRITE_FILE, LIST_FILES, PUBLISH_ARTIFACT]
