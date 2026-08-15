"""
harness.local_fs - Read-only task worktree search/read tools.
"""

import json
import re
from typing import List, Optional

from runtime_config import DEFAULT_LOCAL_FS_READ_BYTES
from harness.storage.base import glob_matches
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
    # The same matcher the storage backends and the virtual view use, rather
    # than Path.glob. They agreed with each other and disagreed with this
    # branch, so the identical search returned different files in file mode
    # and db mode - the switch is meant to be invisible.
    try:
        candidates = sorted(
            path for path in root.rglob("*")
            if glob_matches(glob_pattern, str(path.relative_to(root)))
        )
    except (OSError, ValueError) as exc:
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
                "storage": "file",
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
                            "storage": "file",
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
                "storage": "file",
                "error": str(exc),
            }
            hit_bytes = json_size_bytes(hit)
            if total_bytes + hit_bytes > max_total_bytes:
                truncated = True
                break
            total_bytes += hit_bytes
            results.append(hit)

    if not truncated and len(results) < max_results:
        # Whatever the database holds that never became a file. Files win: in
        # dual mode both sides exist and the on-disk copy is authoritative, and
        # a legacy worktree has files but no rows at all.
        seen = {str(hit.get("relativePath") or "") for hit in results}
        results, total_bytes, truncated = _search_virtual_files(
            logger,
            already_seen=seen,
            results=results,
            regex=regex,
            event_type=event_type,
            max_results=max_results,
            max_bytes_per_hit=max_bytes_per_hit,
            max_total_bytes=max_total_bytes,
            total_bytes=total_bytes,
            glob_pattern=glob_pattern,
        )
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


def _search_virtual_files(
    logger: RunLogger,
    *,
    already_seen,
    results: List[JsonDict],
    regex,
    event_type: Optional[str],
    max_results: int,
    max_bytes_per_hit: int,
    max_total_bytes: int,
    total_bytes: int,
    glob_pattern: str,
):
    """Scan database-backed paths under the same budget as the file scan."""

    from harness.storage.virtual_fs import virtual_fs_for

    view = virtual_fs_for(logger)
    if view is None:
        return results, total_bytes, False

    root = logger.task_dir.resolve()
    truncated = False
    for logical_path, size, approximate in view.match_files(glob_pattern):
        if logical_path in already_seen:
            continue
        if len(results) >= max_results:
            return results, total_bytes, True
        base: JsonDict = {
            "path": str(root / logical_path),
            "relativePath": logical_path,
            "byteSize": size,
            "storage": "sqlite",
        }
        if approximate:
            base["byteSizeApproximate"] = True
        if regex is None and not event_type:
            # A glob-only query needs no content, and reading it is not free:
            # iter_lines decompresses and splits the whole resource, so naming
            # 46 observation files used to cost decompressing 37 MB to throw
            # it away. The listing already restricts this loop to paths that
            # have text, so skipping the read admits nothing extra.
            hit_bytes = json_size_bytes(base)
            if total_bytes + hit_bytes > max_total_bytes:
                return results, total_bytes, True
            total_bytes += hit_bytes
            results.append(base)
            continue
        lines = view.iter_lines(logical_path)
        if lines is None:
            continue
        for line_no, line in enumerate(lines, start=1):
            if event_type:
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(event, dict) or event.get("type") != event_type:
                    continue
            if regex is not None and not regex.search(line):
                continue
            excerpt, was_truncated = truncate_utf8_text(line.strip(), max_bytes_per_hit)
            hit = dict(base)
            hit.update({
                "line": line_no,
                "bytes": len(line.encode("utf-8")),
                "snippet": excerpt,
                "snippetTruncated": was_truncated,
            })
            hit_bytes = json_size_bytes(hit)
            if total_bytes + hit_bytes > max_total_bytes:
                return results, total_bytes, True
            total_bytes += hit_bytes
            results.append(hit)
            if len(results) >= max_results:
                return results, total_bytes, True
    return results, total_bytes, truncated


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
        # No file here. Either the backend keeps this content in the database,
        # or the path really is wrong - the virtual read distinguishes the two
        # and returns the same shape so callers cannot tell where it came from.
        virtual = _read_virtual_file(
            logger,
            resolved,
            line_offset=line_offset,
            line_limit=line_limit,
            max_bytes=max_bytes,
        )
        if virtual is not None:
            return virtual
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
        scanned = 0
        with resolved.open("r", encoding="utf-8", errors="replace") as handle:
            for index, line in enumerate(handle):
                scanned = index + 1
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
            # The loop above stops at the page boundary, so `scanned` is where
            # this page ended and not how big the file is. Finish walking the
            # handle to report the real total: knowing only `nextLineOffset`,
            # a model knows more exists but never how much, so it pages
            # blindly — task a608b5e7 spent a whole turn reading one line, and
            # seven turns walking one file.
            total_lines = scanned + sum(1 for _ in handle)
        return {
            "status": "done",
            "path": str(resolved),
            "relativePath": str(resolved.relative_to(logger.task_dir.resolve())),
            # Same shape as the database-backed read, field for field: a
            # caller must not be able to tell which branch answered, and
            # bytesRead in particular is a business field, not a diagnostic.
            "byteSize": size,
            "byteSizeApproximate": False,
            "bytesRead": total_bytes,
            "storage": "file",
            "lineOffset": line_offset,
            "lineLimit": line_limit,
            "linesRead": len(lines),
            "linesScanned": scanned,
            "totalLines": total_lines,
            "maxBytes": max_bytes,
            "truncated": truncated,
            "nextLineOffset": next_line_offset,
            "content": "\n".join(lines),
        }
    except OSError as exc:
        return {"status": "failed", "path": str(resolved), "error": str(exc)}


def _read_virtual_file(
    logger: RunLogger,
    resolved,
    *,
    line_offset: int,
    line_limit: int,
    max_bytes: int,
) -> Optional[JsonDict]:
    """Serve a database-backed path through the file-read contract.

    Mirrors the on-disk reader's paging and byte budget exactly, including
    ``nextLineOffset``, so a model paging through run.jsonl behaves the same
    whether the bytes came from a file or from run_events.
    """

    from harness.storage.virtual_fs import virtual_fs_for

    view = virtual_fs_for(logger)
    if view is None:
        return None
    root = logger.task_dir.resolve()
    try:
        relative = str(resolved.relative_to(root))
    except ValueError:
        return None
    lines_iter = view.iter_lines(relative)
    if lines_iter is None:
        return None
    # The whole file's size, matching what the on-disk branch reports from
    # stat(). Reporting the slice actually returned made byteSize mean two
    # different things depending on where the bytes lived.
    known_size = view.size_of(relative)

    max_bytes = max(1, min(optional_int(max_bytes, DEFAULT_LOCAL_FS_READ_BYTES) or DEFAULT_LOCAL_FS_READ_BYTES, 200000))
    line_offset = max(0, optional_int(line_offset, 0) or 0)
    line_limit = max(1, min(optional_int(line_limit, 200) or 200, 5000))

    lines: List[str] = []
    total_bytes = 0
    truncated = False
    next_line_offset: Optional[int] = None
    scanned = 0
    for index, line in enumerate(lines_iter):
        scanned = index + 1
        if index < line_offset:
            continue
        if len(lines) >= line_limit:
            next_line_offset = index
            truncated = True
            break
        # The view yields lines with their terminator, exactly as iterating a
        # file handle does, so the budget charges the same bytes on both paths
        # - including the fact that a resource's last line has no newline and
        # a JSONL's does.
        line_bytes = len(line.encode("utf-8"))
        if total_bytes + line_bytes > max_bytes:
            remaining = max_bytes - total_bytes
            if remaining > 0:
                partial, _ = truncate_utf8_text(line.rstrip("\n"), remaining)
                lines.append(partial)
                total_bytes = max_bytes
            next_line_offset = index
            truncated = True
            break
        lines.append(line.rstrip("\n"))
        total_bytes += line_bytes
    total_lines = scanned + sum(1 for _ in lines_iter)
    return {
        "status": "done",
        "path": str(resolved),
        "relativePath": relative,
        "byteSize": known_size[0] if known_size else total_bytes,
        "byteSizeApproximate": bool(known_size[1]) if known_size else False,
        "bytesRead": total_bytes,
        "storage": "sqlite",
        "lineOffset": line_offset,
        "lineLimit": line_limit,
        "linesRead": len(lines),
        "linesScanned": scanned,
        "totalLines": total_lines,
        "maxBytes": max_bytes,
        "truncated": truncated,
        "nextLineOffset": next_line_offset,
        "content": "\n".join(lines),
    }
