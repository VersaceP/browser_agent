"""
harness.local_fs - Read-only task worktree search/read tools.
"""

import json
import re
from typing import List, Optional

from runtime_config import DEFAULT_LOCAL_FS_READ_BYTES
from harness.utils import (
    JsonDict,
    RunLogger,
    json_size_bytes,
    optional_int,
    resolve_task_file,
    truncate_utf8_text,
)


def local_fs_search(
    logger: RunLogger,
    *,
    glob_pattern: str,
    pattern: Optional[str] = None,
    event_type: Optional[str] = None,
    max_results: int = 20,
    max_bytes_per_hit: int = 2000,
    max_total_bytes: int = 20000,
) -> JsonDict:
    root = logger.task_dir.resolve()
    glob_pattern = glob_pattern or "**/*"
    # Strict tool schemas force callers to always send event_type; models often
    # emit the STRING "null" for "not needed", which would otherwise silently
    # filter out every line of non-JSONL files (each line fails json.loads).
    if event_type is not None:
        event_type = str(event_type).strip()
        if event_type.lower() in {"", "null", "none"}:
            event_type = None
    max_results = max(1, min(optional_int(max_results, 20) or 20, 100))
    max_bytes_per_hit = max(200, min(optional_int(max_bytes_per_hit, 2000) or 2000, 20000))
    max_total_bytes = max(
        max_bytes_per_hit,
        min(optional_int(max_total_bytes, 20000) or 20000, 200000),
    )
    try:
        candidates = sorted(root.glob(glob_pattern))
    except ValueError as exc:
        return {"status": "failed", "error": f"invalid glob: {exc}"}

    regex = None
    if pattern:
        try:
            regex = re.compile(pattern)
        except re.error as exc:
            return {"status": "failed", "error": f"invalid grep pattern: {exc}"}

    results: List[JsonDict] = []
    total_bytes = 0
    truncated = False
    for path in candidates:
        if len(results) >= max_results:
            truncated = True
            break
        try:
            resolved = path.resolve()
            resolved.relative_to(root)
        except (OSError, ValueError):
            continue
        if not resolved.is_file():
            continue
        rel = str(resolved.relative_to(root))
        size = resolved.stat().st_size
        if regex is None and not event_type:
            hit = {
                "path": str(resolved),
                "relativePath": rel,
                "byteSize": size,
            }
            hit_bytes = json_size_bytes(hit)
            if total_bytes + hit_bytes > max_total_bytes:
                truncated = True
                break
            total_bytes += hit_bytes
            results.append(hit)
            continue
        try:
            with resolved.open("r", encoding="utf-8", errors="replace") as handle:
                for line_no, line in enumerate(handle, start=1):
                    if event_type:
                        try:
                            event = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        if not isinstance(event, dict) or event.get("type") != event_type:
                            continue
                    if regex is None or regex.search(line):
                        excerpt = line.strip()
                        line_bytes = len(line.encode("utf-8"))
                        excerpt, was_truncated = truncate_utf8_text(
                            excerpt,
                            max_bytes_per_hit,
                        )
                        hit = {
                            "path": str(resolved),
                            "relativePath": rel,
                            "byteSize": size,
                            "line": line_no,
                            "bytes": line_bytes,
                            "snippet": excerpt,
                            "snippetTruncated": was_truncated,
                        }
                        hit_bytes = json_size_bytes(hit)
                        if total_bytes + hit_bytes > max_total_bytes:
                            truncated = True
                            return {
                                "status": "done",
                                "root": str(root),
                                "glob": glob_pattern,
                                "pattern": pattern,
                                "eventType": event_type,
                                "count": len(results),
                                "truncated": truncated,
                                "maxTotalBytes": max_total_bytes,
                                "results": results,
                            }
                        total_bytes += hit_bytes
                        results.append(hit)
                        if len(results) >= max_results:
                            truncated = True
                            break
            if len(results) >= max_results:
                truncated = True
                break
        except OSError as exc:
            hit = {
                "path": str(resolved),
                "relativePath": rel,
                "error": str(exc),
            }
            hit_bytes = json_size_bytes(hit)
            if total_bytes + hit_bytes > max_total_bytes:
                truncated = True
                break
            total_bytes += hit_bytes
            results.append(hit)
    return {
        "status": "done",
        "root": str(root),
        "glob": glob_pattern,
        "pattern": pattern,
        "eventType": event_type,
        "count": len(results),
        "truncated": truncated,
        "maxTotalBytes": max_total_bytes,
        "results": results,
    }


def local_fs_read(
    logger: RunLogger,
    *,
    path: str,
    line_offset: int = 0,
    line_limit: int = 200,
    max_bytes: int = DEFAULT_LOCAL_FS_READ_BYTES,
) -> JsonDict:
    resolved, error = resolve_task_file(logger, path)
    if error or resolved is None:
        return {"status": "failed", "error": error}
    if not resolved.is_file():
        return {"status": "failed", "error": "path is not a file", "path": str(resolved)}

    max_bytes = max(1, min(optional_int(max_bytes, DEFAULT_LOCAL_FS_READ_BYTES) or DEFAULT_LOCAL_FS_READ_BYTES, 200000))
    try:
        size = resolved.stat().st_size
        line_offset = max(0, optional_int(line_offset, 0) or 0)
        line_limit = max(1, min(optional_int(line_limit, 200) or 200, 5000))
        lines: List[str] = []
        total_bytes = 0
        truncated = False
        next_line_offset: Optional[int] = None
        with resolved.open("r", encoding="utf-8", errors="replace") as handle:
            for index, line in enumerate(handle):
                if index < line_offset:
                    continue
                if len(lines) >= line_limit:
                    next_line_offset = index
                    truncated = True
                    break
                line_bytes = len(line.encode("utf-8"))
                if total_bytes + line_bytes > max_bytes:
                    remaining = max_bytes - total_bytes
                    if remaining > 0:
                        partial, _ = truncate_utf8_text(line, remaining)
                        lines.append(partial)
                        total_bytes = max_bytes
                    next_line_offset = index
                    truncated = True
                    break
                lines.append(line.rstrip("\n"))
                total_bytes += line_bytes
        return {
            "status": "done",
            "path": str(resolved),
            "relativePath": str(resolved.relative_to(logger.task_dir.resolve())),
            "byteSize": size,
            "lineOffset": line_offset,
            "lineLimit": line_limit,
            "linesRead": len(lines),
            "maxBytes": max_bytes,
            "truncated": truncated,
            "nextLineOffset": next_line_offset,
            "content": "\n".join(lines),
        }
    except OSError as exc:
        return {"status": "failed", "path": str(resolved), "error": str(exc)}
