"""Bounded, task-owned file search batches. Cursors contain values, never handles.

A batch pauses all scanning before it returns. Continuing is an explicit caller
choice; an exhausted budget is not evidence that a file or value does not exist.
"""
from __future__ import annotations

from collections import OrderedDict
import copy
import json
import os
from pathlib import Path
import pickle
import re
import secrets
import stat
import threading
import time

from harness.storage.base import glob_matches
from harness.utils import json_size_bytes, truncate_utf8_text

CURSOR_TTL_SECONDS = 300
MAX_CURSORS = 32
MAX_CURSOR_BYTES = 8 * 1024 * 1024
MAX_TOTAL_CURSOR_BYTES = 64 * 1024 * 1024
MAX_LINE_BYTES = 1024 * 1024
READ_CHUNK_BYTES = 64 * 1024
SAMPLE_LIMIT = 12
_lock = threading.Lock()
_cursors = OrderedDict()
_retired = OrderedDict()


def _task_key(logger):
    return (str(getattr(logger, "task_id", "")), str(Path(logger.task_dir).resolve()))


def _retire(token, reason):
    _retired[token] = (time.monotonic(), reason)
    while len(_retired) > MAX_CURSORS * 8:
        _retired.popitem(last=False)


def _purge():
    now = time.monotonic()
    for token, entry in list(_cursors.items()):
        if now - entry["at"] >= CURSOR_TTL_SECONDS:
            _cursors.pop(token)
            _retire(token, "cursor_expired")
    for token, (at, _) in list(_retired.items()):
        if now - at >= CURSOR_TTL_SECONDS:
            _retired.pop(token)


def _take(token, owner, query):
    with _lock:
        _purge()
        entry = _cursors.get(token)
        if entry is None:
            return None, _retired.get(token, (None, "cursor_invalid"))[1]
        if entry["owner"] != owner:
            return None, "cursor_owner_mismatch"
        if entry["query"] != query:
            return None, "cursor_query_mismatch"
        _cursors.pop(token)
        _retire(token, "cursor_consumed")
        return entry["state"], None


def _put(owner, query, state):
    size = len(pickle.dumps(state, protocol=5))
    if size > MAX_CURSOR_BYTES:
        return None
    with _lock:
        _purge()
        while _cursors and (len(_cursors) >= MAX_CURSORS or
                sum(item["size"] for item in _cursors.values()) + size > MAX_TOTAL_CURSOR_BYTES):
            old, _ = _cursors.popitem(last=False)
            _retire(old, "cursor_evicted")
        token = secrets.token_urlsafe(24)
        _cursors[token] = {"owner": owner, "query": query, "state": state,
                           "size": size, "at": time.monotonic()}
        return token


def close_searches(logger):
    """Release paused value-only scans owned by a closing task."""
    owner = _task_key(logger)
    with _lock:
        for token, entry in list(_cursors.items()):
            if entry["owner"] == owner:
                _cursors.pop(token)
                _retire(token, "cursor_closed")


def close_search_cursor(logger, cursor):
    owner = _task_key(logger)
    with _lock:
        _purge()
        entry = _cursors.get(cursor)
        if entry is None or entry["owner"] != owner:
            return False
        _cursors.pop(cursor)
        _retire(cursor, "cursor_closed")
        return True


def _fingerprint(value):
    return (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns)


def _directory_identity(value):
    return (value.st_dev, value.st_ino, value.st_mtime_ns)


def _plan(pattern):
    """Conservative pruning only; the shared matcher still decides every hit."""
    parts = pattern.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        return [], None, None
    depth = None if "**" in pattern else len(parts)
    magic = lambda part: any(char in part for char in "*?[")
    if not any(magic(part) for part in parts):
        return [], depth, parts
    prefix = []
    for part in parts[:-1]:
        if magic(part):
            break
        prefix.append(part)
    return prefix, depth, None


def _issue(state, code, *, path=None, message=None, skipped=False):
    key = "skipped" if skipped else "errors"
    state[key + "Count"] += 1
    counts = state[key + "ByCode"]
    counts[code] = counts.get(code, 0) + 1
    if len(state[key]) < SAMPLE_LIMIT:
        sample = {"code": code}
        if path is not None:
            sample["path"] = str(path)
        if message:
            sample["message"] = str(message)[:300]
        state[key].append(sample)


def _safe_path(access, root, path, state):
    """Do not disclose excluded paths or traverse directory symlinks."""
    from harness.tools.path_authorization import allowed
    try:
        relative = path.relative_to(root)
        current = root
        for part in (None, *relative.parts):
            if part is not None:
                current = current / part
            if current.is_symlink():
                _issue(state, "symlink_not_followed", skipped=True)
                return False
        resolved = path.resolve()
        if not resolved.is_relative_to(root) or not allowed(access, resolved, "read"):
            _issue(state, "permission_excluded", skipped=True)
            return False
        return True
    except (OSError, ValueError) as exc:
        _issue(state, "path_unavailable", message=getattr(exc, "strerror", None))
        return False


class Batch:
    def __init__(self, state, *, max_scan_entries, timeout_ms, cancelled, started):
        self.state = state
        self.limit = max_scan_entries
        self.deadline = started + timeout_ms / 1000
        self.cancelled = cancelled
        self.start = dict(state["counters"])
        self.units = 0
        self.reason = None
        self.directories = OrderedDict()
        self.virtual = None
        self.file_stream = None

    def check(self):
        if self.cancelled and self.cancelled():
            self.reason = "cancelled"
        elif time.monotonic() >= self.deadline:
            self.reason = "time_budget"
        elif self.units >= self.limit:
            self.reason = "entry_budget"
        return self.reason is None

    def close_directory(self, path):
        item = self.directories.pop(path, None)
        if item:
            try:
                item[1].close()
            finally:
                os.close(item[0])

    def close_virtual(self):
        if self.virtual:
            close = getattr(self.virtual[1], "close", None)
            self.virtual = None
            if close:
                close()

    def close_file(self):
        if self.file_stream:
            self.file_stream[1].close()
            self.file_stream = None

    def close(self):
        self.close_file()
        for path in list(self.directories):
            self.close_directory(path)
        self.close_virtual()

    def charge(self, key, count=1):
        self.state["counters"][key] = self.state["counters"].get(key, 0) + count


def _new_state(root, pattern, virtual_files, db_authoritative):
    prefix, depth, exact = _plan(pattern)
    return {"stack": [{"path": str(root.joinpath(*prefix)), "offset": 0, "identity": None}]
            if exact is None else [], "exact": str(root.joinpath(*exact)) if exact else None,
            "maxDepth": depth, "file": None, "pendingHit": None, "phase": "physical",
            "virtualFiles": virtual_files, "virtualIndex": 0, "virtualLine": 0,
            "virtualHits": set(), "dbAuthoritative": db_authoritative,
            "errors": [], "errorsCount": 0, "errorsByCode": {},
            "skipped": [], "skippedCount": 0, "skippedByCode": {},
            "counters": {}, "limitations": []}


def _next_physical(access, root, state, batch):
    """Keep iterators only within this batch; resume with value-only offsets."""
    if state["exact"] is not None:
        if not batch.check():
            return None
        candidate = Path(state["exact"])
        state["exact"] = None
        batch.units += 1
        batch.charge("entriesVisited")
        if _safe_path(access, root, candidate, state):
            return candidate
    while state["stack"] and batch.check():
        frame = state["stack"][-1]
        directory = Path(frame["path"])
        if not _safe_path(access, root, directory, state):
            batch.close_directory(str(directory))
            state["stack"].pop()
            continue
        try:
            active = batch.directories.get(str(directory))
            if active is None:
                identity = _directory_identity(directory.stat())
                if frame["identity"] is not None and frame["identity"] != identity:
                    _issue(state, "directory_changed", path=directory)
                    state["stack"].pop()
                    continue
                frame["identity"] = identity
                fd = os.open(directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0))
                try:
                    if _directory_identity(os.fstat(fd)) != identity:
                        _issue(state, "directory_changed", path=directory)
                        state["stack"].pop()
                        os.close(fd)
                        continue
                    items = os.scandir(fd)
                except BaseException:
                    os.close(fd)
                    raise
                active = [fd, items, 0]
                batch.directories[str(directory)] = active
                batch.charge("directoryOpens")
                # Deep paths cannot retain unbounded descriptors within a batch.
                while len(batch.directories) > 32:
                    batch.close_directory(next(iter(batch.directories)))
            try:
                entry = next(active[1])
            except StopIteration:
                if _directory_identity(os.fstat(active[0])) != frame["identity"]:
                    _issue(state, "directory_changed", path=directory)
                batch.close_directory(str(directory))
                state["stack"].pop()
                continue
            active[2] += 1
            if active[2] <= frame["offset"]:
                batch.charge("resumeEntriesSkipped")
                continue
            frame["offset"] = active[2]
            batch.units += 1
            batch.charge("entriesVisited")
            candidate = directory / entry.name
            if entry.is_symlink():
                _issue(state, "symlink_not_followed", skipped=True)
                continue
            if not _safe_path(access, root, candidate, state):
                continue
            relative = candidate.relative_to(root)
            if entry.is_dir(follow_symlinks=False):
                if state["maxDepth"] is None or len(relative.parts) < state["maxDepth"]:
                    state["stack"].append({"path": str(candidate), "offset": 0, "identity": None})
                continue
            if entry.is_file(follow_symlinks=False):
                return candidate
        except OSError as exc:
            _issue(state, "directory_unreadable", path=directory, message=exc.strerror)
            batch.close_directory(str(directory))
            if state["stack"] and state["stack"][-1] is frame:
                state["stack"].pop()
    return None


def _base(path, root, size, storage="file", approximate=False):
    value = {"path": str(path), "relativePath": str(path.relative_to(root)),
             "byteSize": size, "storage": storage}
    if approximate:
        value["byteSizeApproximate"] = True
    return value


def _line_hit(base, line, number, regex, event_type, max_bytes_per_hit):
    if event_type:
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            return None
        if not isinstance(event, dict) or event.get("type") != event_type:
            return None
    match = regex.search(line) if regex else None
    if regex is not None and match is None:
        return None
    start = max(0, match.start() - min(80, max_bytes_per_hit // 8)) if match else 0
    snippet, truncated = truncate_utf8_text(line[start:].rstrip("\r\n"), max_bytes_per_hit)
    return {**base, "line": number, "bytes": len(line.encode("utf-8")),
            "snippet": snippet, "snippetTruncated": bool(start or truncated),
            "snippetStartColumn": start + 1, "matchColumn": match.start() + 1 if match else None,
            "readHint": {"line_offset": max(0, number - 3), "line_limit": 5}}


def _physical_line(access, root, state, batch, regex, event_type, max_bytes_per_hit):
    current = state["file"]
    path = Path(current["base"]["path"])
    if not _safe_path(access, root, path, state):
        batch.close_file()
        state["file"] = None
        return None
    try:
        if batch.file_stream is None:
            fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            stream = os.fdopen(fd, "rb")
            batch.file_stream = [str(path), stream]
            if _fingerprint(os.fstat(stream.fileno())) != current["identity"]:
                _issue(state, "file_changed", path=path)
                batch.close_file()
                state["file"] = None
                return None
            batch.charge("fileOpens")
            stream.seek(current["offset"])
        stream = batch.file_stream[1]
        while batch.check():
            if not current["chunk"] and not current["eof"]:
                data = stream.read(READ_CHUNK_BYTES)
                current["offset"] += len(data)
                current["chunk"] = data
                current["eof"] = not data
                batch.units += 1
                batch.charge("bytesRead", len(data))
            data = current["chunk"]
            if current["skipLF"] and data:
                if data.startswith(b"\n"):
                    data = data[1:]
                current["skipLF"] = False
            # Match text-mode universal newlines while keeping reads bounded.
            cr, lf = data.find(b"\r"), data.find(b"\n")
            positions = [position for position in (cr, lf) if position >= 0]
            position = min(positions) if positions else None
            if position is not None:
                segment = data[:position]
                current["skipLF"] = data[position:position + 1] == b"\r"
                current["chunk"] = data[position + 1:]
            else:
                segment = data
                current["chunk"] = b""
            if segment and not current["discard"]:
                current["buffer"] += segment
                if len(current["buffer"]) > MAX_LINE_BYTES:
                    current["buffer"] = b""
                    current["discard"] = True
                    _issue(state, "line_too_large", path=path,
                           message=f"Line {current['line']} exceeds {MAX_LINE_BYTES} bytes; content not searched.")
            complete_line = position is not None or current["eof"]
            if not complete_line:
                continue
            at_end = current["eof"] and position is None
            if at_end and not current["buffer"] and not current["discard"]:
                if _fingerprint(os.fstat(stream.fileno())) != current["identity"]:
                    _issue(state, "file_changed", path=path)
                batch.close_file()
                state["file"] = None
                return None
            line = current["buffer"].decode("utf-8", errors="replace") + ("\n" if position is not None else "")
            number, discarded = current["line"], current["discard"]
            current.update(buffer=b"", discard=False, line=number + 1)
            batch.units += 1
            batch.charge("linesRead")
            if at_end:
                if _fingerprint(os.fstat(stream.fileno())) != current["identity"]:
                    _issue(state, "file_changed", path=path)
                batch.close_file()
                state["file"] = None
            if not discarded:
                hit = _line_hit(current["base"], line, number, regex, event_type, max_bytes_per_hit)
                if hit:
                    state["lastHitIdentity"] = current["identity"]
                    return hit
            if at_end:
                return None
    except OSError as exc:
        _issue(state, "file_unreadable", path=path, message=exc.strerror)
        batch.close_file()
        state["file"] = None
    return None


def _virtual_hit(logger, access, root, state, batch, regex, event_type, max_bytes_per_hit):
    from harness.storage.virtual_fs import virtual_fs_for
    view = virtual_fs_for(logger)
    task_root = Path(logger.task_dir).resolve()
    while state["virtualIndex"] < len(state["virtualFiles"]) and batch.check():
        logical, relative, size, approximate = state["virtualFiles"][state["virtualIndex"]]
        path = task_root / logical
        if relative in state["virtualHits"] or not _safe_path(access, root, path, state):
            batch.close_virtual()
            state["virtualIndex"] += 1
            state["virtualLine"] = 0
            continue
        base = _base(path, root, size, "sqlite", approximate)
        if regex is None and not event_type:
            state["virtualIndex"] += 1
            batch.units += 1
            batch.charge("virtualFilesVisited")
            return base
        if view is None:
            _issue(state, "virtual_view_unavailable", path=path)
            state["virtualIndex"] = len(state["virtualFiles"])
            break
        # Full-resource codec checks are authoritative and deliberately propagate.
        if batch.virtual is None:
            batch.charge("virtualDecodeCalls")
            lines = view.iter_lines(logical)
            if lines is None:
                _issue(state, "virtual_file_unavailable", path=path)
                state["virtualIndex"] += 1
                state["virtualLine"] = 0
                continue
            batch.virtual = [logical, iter(lines), 0]
        try:
            line = next(batch.virtual[1])
        except StopIteration:
            batch.close_virtual()
            state["virtualIndex"] += 1
            state["virtualLine"] = 0
            continue
        batch.virtual[2] += 1
        number = batch.virtual[2]
        if number <= state["virtualLine"]:
            batch.charge("virtualResumeLinesSkipped")
            continue
        batch.units += 1
        state["virtualLine"] = number
        batch.charge("linesRead")
        if len(line) > MAX_LINE_BYTES or len(line.encode("utf-8")) > MAX_LINE_BYTES:
            _issue(state, "line_too_large", path=path, message=f"Virtual line {number} not searched.")
            continue
        hit = _line_hit(base, line, number, regex, event_type, max_bytes_per_hit)
        if hit:
            return hit
    return None


def _position(state):
    file = state.get("file") or {}
    return (tuple((frame["path"], frame["offset"]) for frame in state["stack"]), state["exact"],
            state["phase"], file.get("offset"), file.get("line"), len(file.get("buffer", b"")),
            len(file.get("chunk", b"")), state["virtualIndex"], state["virtualLine"],
            bool(state.get("pendingHit")))


def _visible_issues(access, root, samples):
    from harness.tools.path_authorization import allowed
    result = copy.deepcopy(samples)
    for sample in result:
        if "path" in sample:
            try:
                path = Path(sample["path"]).resolve()
                visible = path.is_relative_to(root) and allowed(access, path, "read")
            except (OSError, ValueError):
                visible = False
            if not visible:
                sample.pop("path", None)
    return result


def search(logger, *, agent=None, path=".", glob_pattern="**/*", pattern=None, event_type=None,
           max_results=20, max_bytes_per_hit=2000, max_total_bytes=20000, cursor=None,
           max_scan_entries=5000, timeout_ms=2000, cancelled=None):
    from harness.tools.path_authorization import resolve_authorized_path
    from harness.storage.virtual_fs import db_authoritative_for, virtual_fs_for
    started = time.monotonic()
    access = agent or logger
    try:
        root = resolve_authorized_path(access, path, mode="read")
        glob_pattern = glob_pattern or "**/*"
        event_type = str(event_type).strip() if event_type is not None else None
        if event_type and event_type.lower() in {"null", "none"}:
            event_type = None
        event_type = event_type or None
        pattern = pattern or None
        regex = re.compile(pattern) if pattern else None
        # Compile through the shared matcher before any filesystem traversal.
        glob_matches(glob_pattern, "")
        max_results = max(1, min(int(max_results or 20), 100))
        max_bytes_per_hit = max(200, min(int(max_bytes_per_hit or 2000), 20000))
        max_total_bytes = max(max_bytes_per_hit, min(int(max_total_bytes or 20000), 200000))
        max_scan_entries = max(1, min(int(max_scan_entries), 100000))
        timeout_ms = max(1, min(int(timeout_ms), 30000))
        if cursor is not None and (not isinstance(cursor, str) or not cursor):
            raise ValueError("cursor must be a nonempty string or null")
    except (OSError, ValueError, TypeError, re.error) as exc:
        return {"status": "failed", "code": "invalid_search_request", "error": str(exc)}
    owner = _task_key(logger)
    query = (str(root), glob_pattern, pattern, event_type)
    if cursor:
        state, error = _take(cursor, owner, query)
        if error:
            return {"status": "failed", "code": error,
                    "error": "Continue with the same task, path, glob, pattern and event_type; use the latest nextCursor once. Expired or closed searches require a new query.",
                    "nextCursor": None}
    else:
        task_root = Path(logger.task_dir).resolve()
        in_task = root.is_relative_to(task_root)
        view = virtual_fs_for(logger) if in_task else None
        virtual_files = []
        # Listing is metadata-only; no text resource is decoded for a glob-only query.
        for logical, size, approximate in view.list_files() if view is not None else []:
            candidate = task_root / logical
            if candidate.is_relative_to(root) and candidate != root:
                relative = str(candidate.relative_to(root))
                if glob_matches(glob_pattern, relative):
                    virtual_files.append((logical, relative, size, approximate))
        db_authoritative = bool(in_task and db_authoritative_for(logger))
        state = _new_state(root, glob_pattern, virtual_files, db_authoritative)
        if db_authoritative and not root.exists() and (root == task_root or virtual_files):
            state["stack"] = []
            state["exact"] = None
    if state["virtualFiles"]:
        state["limitations"] = ["Virtual metadata listing and resource decoding may be non-interruptible; resumed text is a live view."]
    batch = Batch(state, max_scan_entries=max_scan_entries, timeout_ms=timeout_ms, cancelled=cancelled, started=started)
    before_position = _position(state)
    results, total_bytes = [], 0
    virtual_paths = {item[1] for item in state["virtualFiles"]}
    try:
        while batch.check():
            hit = state.pop("pendingHit", None)
            hit_identity = state.pop("pendingHitIdentity", None)
            if hit is not None:
                # Permission may have changed since the previous batch.
                if not _safe_path(access, root, Path(hit["path"]), state):
                    hit = None
                elif hit_identity is not None:
                    try:
                        unchanged = _fingerprint(Path(hit["path"]).stat(follow_symlinks=False)) == hit_identity
                    except OSError:
                        unchanged = False
                    if not unchanged:
                        _issue(state, "file_changed", path=hit["path"], message="Pending search hit changed before delivery.")
                        hit = None
            if hit is None and state["phase"] == "physical":
                if state["file"] is not None:
                    hit = _physical_line(access, root, state, batch, regex, event_type, max_bytes_per_hit)
                    hit_identity = state.pop("lastHitIdentity", None)
                    if hit is None and not batch.check():
                        break
                    if hit is None and state["file"] is not None:
                        continue
                if hit is None:
                    candidate = _next_physical(access, root, state, batch)
                    if candidate is None:
                        if not batch.check():
                            break
                        state["phase"] = "virtual"
                        continue
                    relative = str(candidate.relative_to(root))
                    if not glob_matches(glob_pattern, relative) or (state["dbAuthoritative"] and relative in virtual_paths):
                        continue
                    try:
                        facts = candidate.stat(follow_symlinks=False)
                        if not stat.S_ISREG(facts.st_mode):
                            continue
                        base = _base(candidate, root, facts.st_size)
                        batch.charge("filesMatched")
                        if regex is None and not event_type:
                            hit = base
                            hit_identity = _fingerprint(facts)
                        else:
                            state["file"] = {"base": base, "identity": _fingerprint(facts), "offset": 0,
                                             "line": 1, "buffer": b"", "discard": False,
                                             "chunk": b"", "eof": False, "skipLF": False}
                            continue
                    except OSError as exc:
                        _issue(state, "file_unavailable", path=candidate, message=exc.strerror)
                        continue
            if hit is None and state["phase"] == "virtual":
                hit = _virtual_hit(logger, access, root, state, batch, regex, event_type, max_bytes_per_hit)
                if hit is None:
                    if batch.check():
                        batch.reason = "exhausted"
                    break
            if hit is None:
                continue
            cost = json_size_bytes(hit)
            if cost > max_total_bytes:
                # A metadata-rich hit must remain recoverable even with a small output budget.
                state["pendingHit"] = hit
                state["pendingHitIdentity"] = hit_identity
                batch.reason = "output_limit"
                break
            if total_bytes + cost > max_total_bytes:
                state["pendingHit"] = hit
                state["pendingHitIdentity"] = hit_identity
                batch.reason = "output_limit"
                break
            results.append(hit)
            total_bytes += cost
            if hit["storage"] == "file" and hit["relativePath"] in virtual_paths:
                state["virtualHits"].add(hit["relativePath"])
            if len(results) >= max_results:
                batch.reason = "result_limit"
                break
    finally:
        batch.close()
    reason = batch.reason or "exhausted"
    progress = bool(results) or before_position != _position(state)
    stalled = bool(cursor and not progress and reason == "time_budget")
    if stalled:
        reason = "resume_budget"
    next_cursor = None
    if reason not in {"exhausted", "cancelled"}:
        next_cursor = _put(owner, query, state)
        if next_cursor is None:
            _issue(state, "cursor_state_limit", message="Paused scan exceeded bounded cursor storage; narrow the query.")
            reason = "cursor_state_limit"
    minimum_output = json_size_bytes(state["pendingHit"]) if reason == "output_limit" and not results else None
    recovery = ("Increase timeout_ms for this cursor, or start a narrower query." if stalled else
                f"Continue this cursor with max_total_bytes at least {minimum_output}." if minimum_output else None)
    coverage = {"scanComplete": reason == "exhausted" and not state["errorsCount"] and not state["skippedCount"],
                "traversalComplete": reason == "exhausted", "stopReason": reason,
                "progressMade": progress, "resumeRequiresLargerBudget": stalled,
                "recovery": recovery, "minimumOutputBytes": minimum_output,
                "scope": "authorized paths; symlinks are not followed", "consistency": "live, not a filesystem snapshot",
                "errors": _visible_issues(access, root, state["errors"]), "errorCount": state["errorsCount"],
                "errorsTruncated": state["errorsCount"] > len(state["errors"]), "errorsByCode": dict(state["errorsByCode"]),
                "skipped": _visible_issues(access, root, state["skipped"]), "skippedCount": state["skippedCount"],
                "skippedTruncated": state["skippedCount"] > len(state["skipped"]), "skippedByCode": dict(state["skippedByCode"]),
                "counters": dict(state["counters"]),
                "batchCounters": {key: value - batch.start.get(key, 0) for key, value in state["counters"].items()},
                "limitations": list(state["limitations"]),
                "continuation": "Directory offsets are reopened without live handles; large single directories may require repeated prefix enumeration. Changed directories are reported as incomplete."}
    return {"status": "cancelled" if reason == "cancelled" else "done", "root": str(root),
            "glob": glob_pattern, "pattern": pattern, "eventType": event_type,
            "count": len(results), "truncated": not coverage["scanComplete"], "maxTotalBytes": max_total_bytes,
            "results": results, "nextCursor": next_cursor, "coverage": coverage}
