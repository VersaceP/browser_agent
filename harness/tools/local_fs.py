"""
harness.tools.local_fs - Read-only task worktree search/read tools.
"""

import json
from typing import List, Optional

from runtime_config import DEFAULT_LOCAL_FS_READ_BYTES
from harness.utils import (
    JsonDict,
    RunLogger,
    optional_int,
    resolve_task_file,
    truncate_utf8_text,
)


def local_fs_search(
    logger: RunLogger,
    *,
    agent=None,
    path: str = ".",
    glob_pattern: str,
    pattern: Optional[str] = None,
    event_type: Optional[str] = None,
    max_results: int = 20,
    max_bytes_per_hit: int = 2000,
    max_total_bytes: int = 20000,
    cursor: Optional[str] = None,
    max_scan_entries: int = 5000,
    timeout_ms: int = 2000,
    cancelled=None,
) -> JsonDict:
    """Return one bounded search batch; continue only with its task-owned cursor."""
    from harness.tools.local_search import search
    return search(logger, agent=agent, path=path, glob_pattern=glob_pattern,
                  pattern=pattern, event_type=event_type, max_results=max_results,
                  max_bytes_per_hit=max_bytes_per_hit, max_total_bytes=max_total_bytes,
                  cursor=cursor, max_scan_entries=max_scan_entries, timeout_ms=timeout_ms,
                  cancelled=cancelled)


def _walk_authorized(access, root, *, recursive):
    from harness.tools.path_authorization import allowed
    pending = [root]
    while pending:
        directory = pending.pop()
        for item in directory.iterdir():
            # Do not follow directory symlinks during discovery. Explicit reads
            # resolve symlinks and check their actual target independently.
            if item.is_symlink() or not allowed(access, item.resolve(), "read"):
                continue
            yield item
            if recursive and item.is_dir():
                pending.append(item)


def local_fs_list(agent, path, *, base=None, recursive=False):
    """List authorized physical outputs and authoritative DB task paths."""
    from harness.tools.path_authorization import allowed, resolve_authorized_path
    try:
        root = resolve_authorized_path(agent, path, mode="read", base=base)
        from harness.storage.virtual_fs import db_authoritative_for, virtual_fs_for

        logger = getattr(agent, "logger", agent)
        task_root = logger.task_dir.resolve()
        virtual_root = root.is_relative_to(task_root) and db_authoritative_for(logger)
        view = virtual_fs_for(logger) if virtual_root else None
        virtual_files = [(task_root / name, size, approximate)
                         for name, size, approximate in (view.list_files() if view is not None else [])
                         if (task_root / name).is_relative_to(root) and task_root / name != root]
        if not root.is_dir() and not (virtual_root and not root.exists()
                                      and (root == task_root or virtual_files)):
            return {"status": "failed", "error": "path is not a directory", "path": str(root)}
        iterator = _walk_authorized(agent, root, recursive=recursive) if root.is_dir() else ()
        entries = {}
        for item in sorted(iterator, key=lambda p: str(p)):
            entries[str(item)] = {"name": item.name, "path": str(item),
                            "kind": "directory" if item.is_dir() else "file",
                            "byteSize": item.stat().st_size if item.is_file() else None}
        for item, size, approximate in virtual_files:
            if item.resolve() != item or not allowed(agent, item, "read"):
                continue
            relative = item.relative_to(root)
            for depth in range(1, len(relative.parts)):
                directory = root.joinpath(*relative.parts[:depth])
                if not recursive and depth > 1:
                    break
                if allowed(agent, directory, "read"):
                    entries[str(directory)] = {"name": directory.name, "path": str(directory),
                        "kind": "directory", "byteSize": None, "storage": "database"}
            if recursive or len(relative.parts) == 1:
                entries[str(item)] = {"name": item.name, "path": str(item), "kind": "file",
                    "byteSize": size, "sizeApproximate": approximate, "storage": "database"}
        return {"status": "done", "path": str(root),
                "entries": [entries[key] for key in sorted(entries)]}
    except (OSError, ValueError) as exc:
        return {"status": "failed", "error": str(exc)}


def local_fs_read(
    logger: RunLogger,
    *,
    path: str,
    agent=None,
    line_offset: int = 0,
    line_limit: int = 200,
    max_bytes: int = DEFAULT_LOCAL_FS_READ_BYTES,
) -> JsonDict:
    try:
        from harness.tools.path_authorization import resolve_authorized_path
        resolved = resolve_authorized_path(agent or logger, path, mode="read")
        error = None
    except (OSError, ValueError) as exc:
        resolved, error = None, str(exc)
    if error or resolved is None:
        return {"status": "failed", "error": error}
    from harness.storage.virtual_fs import db_authoritative_for

    if resolved.is_relative_to(logger.task_dir.resolve()) and db_authoritative_for(logger):
        virtual = _read_virtual_file(
            logger,
            resolved,
            line_offset=line_offset,
            line_limit=line_limit,
            max_bytes=max_bytes,
        )
        if virtual is not None:
            return virtual
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
            "relativePath": str(resolved.relative_to(logger.task_dir.resolve())) if resolved.is_relative_to(logger.task_dir.resolve()) else str(resolved),
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
