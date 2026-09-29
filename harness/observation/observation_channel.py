"""Per-page observation chains, model-facing change views, and node watches.

Every hydrated DOM.getAXTree full view - from any client, model-initiated or
harness-internal - is recorded here, keyed by page. Three things follow.

1. Version chain. WebCross computes each diff against the page's previously
   issued view, whoever asked for it, and never names that base. The platform
   numbers versions per document with a sequence that grows by exactly one for
   every new content version, and creates versions only when a view is read.
   So a computed diff whose sequence is exactly one past the previous recorded
   view is provably relative to it; anything else is a gap, and a gap is never
   papered over.

2. Model view. A worker that already holds a complete view of the page gets
   the chained change records since the version it last received instead of
   the whole page again, rendered compactly. Any gap, reset, document change or
   oversized change list falls back to the full view.

3. Watches. A worker may watch specific nodes. Full AX views compare semantic
   state, while exact detail queries compare values only with earlier queries
   on the same surface. A first detail query without a comparable prior value
   reports the current sample without claiming a change. Geometry, scrolling
   and focus are ignored. A background poller probes the nodes while watches
   are active; `wait` blocks on their event channel.

Everything here is process-local and in memory. It never talks to the browser
itself: the poller calls a read function supplied by the tool layer.
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import time
import uuid
from collections import OrderedDict, deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Deque, Dict, Iterable, List, Optional, Set, Tuple

from harness.observation.axtree_format import AXTREE_LINE_RE, parse_axtree_line
from harness.observation.page_observation import parse_change_line
from harness.utils import JsonDict


MAX_TRANSITIONS_PER_PAGE = 64
MAX_WATCHES_PER_CONSUMER = 8
MAX_NODES_PER_WATCH = 16
MAX_EVENTS_PER_WATCH = 64
DEFAULT_WATCH_TTL_SECONDS = 120
POLL_FAILURE_LIMIT = 3
# A change view larger than this share of the full view saves little and is
# harder to read than the view itself.
MAX_CHANGE_VIEW_RATIO = 0.5

# Flags that describe where a node sits, or where focus happens to be, rather
# than what it is. Watches ignore them: a scroll would otherwise fire every
# watch on the page, and every click elsewhere moves focus.
_TRANSIENT_FLAGS = frozenset({"off", "scroll", "focused"})
_FOCUS_ONLY_VALUES = frozenset({"absent", '{"focused":true}', '{"focused":false}'})


def version_sequence(version: Any) -> Optional[int]:
    """The base36 content sequence at the end of a `v_<epoch>_<seq>` version."""
    if not isinstance(version, str) or not version.startswith("v_"):
        return None
    try:
        return int(version.rsplit("_", 1)[-1], 36)
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Change rendering
# ---------------------------------------------------------------------------

def _label(role: Any, name: Any) -> str:
    role_text = str(role or "?")
    name_text = str(name or "")
    if len(name_text) > 60:
        name_text = name_text[:57] + "…"
    return f'{role_text} {json.dumps(name_text, ensure_ascii=False)}' if name_text else role_text


def _json_node_line(value: Any) -> str:
    """A compact node line from a diff record's JSON node value."""
    if not isinstance(value, dict):
        return json.dumps(value, ensure_ascii=False)[:200]
    parts = [_label(value.get("role") or value.get("tag") or value.get("kind"), value.get("name"))]
    flags: List[str] = []
    if value.get("targetable"):
        flags.append("targetable")
    if value.get("interaction") in {"actionable", "candidate"}:
        flags.append(str(value["interaction"]))
    state = value.get("state") if isinstance(value.get("state"), dict) else {}
    for key in ("checked", "selected", "expanded", "disabled", "focused", "required", "invalid"):
        item = state.get(key)
        if item is True:
            flags.append(key)
        elif key in {"checked", "selected", "expanded"} and item in (False, "mixed"):
            flags.append(f"{key}={str(item).lower()}")
    if flags:
        parts.append(f"[{','.join(flags)}]")
    bounds = value.get("bounds")
    if isinstance(bounds, dict):
        parts.append("@{x},{y},{width},{height}".format(**{
            key: bounds.get(key, 0) for key in ("x", "y", "width", "height")
        }))
    text = value.get("text")
    if isinstance(text, str) and text and text != value.get("name"):
        parts.append(f"text={json.dumps(text[:80], ensure_ascii=False)}")
    if value.get("visibility") in {"↓", "∅"}:
        parts.append(f"vis={value['visibility']}")
    if "value" in state:
        parts.append(f"value={json.dumps(state['value'], ensure_ascii=False)[:80]}")
    if value.get("parent"):
        parts.append(f"parent={value['parent']}")
    return " ".join(parts)


def _record_value(line: str) -> Any:
    marker = " value="
    index = line.find(marker)
    if index < 0:
        return None
    try:
        return json.loads(line[index + len(marker):])
    except json.JSONDecodeError:
        return None


def _labels_for(ids: Set[str], lines: Iterable[str]) -> Dict[str, str]:
    labels: Dict[str, str] = {}
    if not ids:
        return labels
    for line in lines:
        head = AXTREE_LINE_RE.match(line)
        if head is None or head.group("id") not in ids:
            continue
        name = head.group("name")
        labels[head.group("id")] = _label(head.group("role"), json.loads(name) if name else "")
        if len(labels) == len(ids):
            break
    return labels


def _is_focus_only(record: str, path: str) -> bool:
    if path == "/state/focused":
        return True
    if path != "/state":
        return False
    values = record.split(" set /state ", 1)[-1].split(" -> ")
    return len(values) == 2 and all(value.strip() in _FOCUS_ONLY_VALUES for value in values)


def render_changes(records: List[str], current_lines: List[str]) -> JsonDict:
    """Compact, model-facing NET change view of chained diff records.

    Returns ``{"changes": [...], "folded": {...}, "reset": bool}``. Records
    from several consecutive diffs are reduced to their net effect: a node
    added and removed again within the chain disappears, a field set several
    times shows only its first value and its last, splices on one list are
    summed, and a context anchor appears once. Additions become node lines,
    removals and anchors become labels, and the updates of one node collapse
    into one line prefixed with that node's current label. Pure geometry -
    visibility flips from layout or scrolling, bounds, focus moves, surface
    bookkeeping - is folded into counts.
    """
    folded = {"cameIntoView": 0, "leftView": 0, "geometry": 0, "focus": 0, "surfaces": 0}
    reset = False
    order: List[Tuple[str, str]] = []  # (kind, key) in first-seen order
    seen: Set[Tuple[str, str]] = set()
    added: Dict[str, str] = {}
    removed: Dict[str, str] = {}
    context: Dict[str, str] = {}
    sets: Dict[str, Dict[str, List[str]]] = {}
    splices: Dict[str, Dict[str, List[List[str]]]] = {}
    changed: Dict[str, List[str]] = {}

    def note(kind: str, key: str) -> None:
        if (kind, key) not in seen:
            seen.add((kind, key))
            order.append((kind, key))

    for record in records:
        if record.startswith("! reset"):
            reset = True
            continue
        parsed = parse_change_line(record)
        if parsed is None:
            continue
        op, entity, node_id = parsed["op"], parsed["entity"], parsed["id"]
        if entity == "surface":
            folded["surfaces"] += 1
            continue
        verb, path = parsed.get("verb"), parsed.get("path", "")
        if op == "~" and verb == "set" and path == "/visibility":
            if record.rstrip().endswith("-> absent"):
                folded["cameIntoView"] += 1
            else:
                folded["leftView"] += 1
            continue
        if op == "~" and verb == "set" and (path.startswith("/bounds") or path.startswith("/scroll")):
            folded["geometry"] += 1
            continue
        if op == "~" and verb == "set" and _is_focus_only(record, path):
            folded["focus"] += 1
            continue
        key = f"{entity}:{node_id}"
        if op == "+":
            if key in removed:
                # Removed earlier in the chain and back again: keep both facts.
                note("removed", key)
            added[key] = f"+ {entity} [{node_id}] {_json_node_line(_record_value(record))}"
            note("added", key)
        elif op == "-":
            value = _record_value(record)
            label = _label(value.get("role"), value.get("name")) if isinstance(value, dict) else ""
            if key in added:
                # Appeared and vanished within the chain: no net change.
                del added[key]
                sets.pop(key, None)
                splices.pop(key, None)
                changed.pop(key, None)
                continue
            removed[key] = f"- {entity} [{node_id}] {label}".rstrip()
            note("removed", key)
        elif op == "=":
            value = _record_value(record)
            label = _label(value.get("role"), value.get("name")) if isinstance(value, dict) else ""
            context.setdefault(key, f"= {entity} [{node_id}] {label} (context)".rstrip())
            note("context", key)
        else:
            detail = record.split(f"[{node_id}] ", 1)[-1]
            note("update", key)
            if verb == "set":
                values = detail[len("set ") + len(path):].strip().split(" -> ", 1)
                before, after = (values + [""])[:2] if len(values) == 2 else ("?", values[0])
                entry = sets.setdefault(key, {}).setdefault(path, [before, after])
                entry[1] = after
            elif verb == "splice":
                try:
                    removed_items = json.loads(detail.split(" removed=", 1)[1].split(" inserted=", 1)[0])
                    inserted_items = json.loads(detail.split(" inserted=", 1)[1])
                except (IndexError, json.JSONDecodeError):
                    changed.setdefault(key, []).append(path)
                else:
                    # Net by member: an item inserted and removed again within
                    # the chain (or the reverse) cancels out.
                    net_removed, net_inserted = splices.setdefault(key, {}).setdefault(path, [[], []])
                    for item in map(str, removed_items):
                        if item in net_inserted:
                            net_inserted.remove(item)
                        else:
                            net_removed.append(item)
                    for item in map(str, inserted_items):
                        if item in net_removed:
                            net_removed.remove(item)
                        else:
                            net_inserted.append(item)
            else:
                paths = changed.setdefault(key, [])
                if path not in paths:
                    paths.append(path)

    update_ids = {key.split(":", 1)[1] for kind, key in order if kind == "update" and key.startswith("node:")}
    labels = _labels_for(update_ids, current_lines)
    lines_out: List[str] = []
    for kind, key in order:
        if kind == "added" and key in added:
            lines_out.append(added[key])
        elif kind == "removed" and key in removed:
            lines_out.append(removed[key])
        elif kind == "context" and key in context:
            if any(key in store for store in (sets, splices, changed, added, removed)):
                continue  # the node's own change already anchors it
            lines_out.append(context.pop(key))
        elif kind == "update":
            parts = [
                f"{path} {before} -> {after}"
                for path, (before, after) in sets.get(key, {}).items() if before != after
            ]
            parts += [
                f"{path} -{len(net_removed)} +{len(net_inserted)}"
                for path, (net_removed, net_inserted) in splices.get(key, {}).items()
                if net_removed or net_inserted
            ]
            parts += [f"changed {path}" for path in changed.get(key, [])]
            if not parts:
                continue
            entity, node_id = key.split(":", 1)
            label = labels.get(node_id, "")
            prefix = f"~ {entity} [{node_id}]" + (f" {label}" if label else "")
            lines_out.append(f"{prefix}: " + "; ".join(parts))
    return {"changes": lines_out, "folded": {k: v for k, v in folded.items() if v}, "reset": reset}


# ---------------------------------------------------------------------------
# Watch comparison
# ---------------------------------------------------------------------------

def _semantic_signature(node: JsonDict) -> JsonDict:
    return {
        "role": node.get("role"),
        "name": node.get("name"),
        "flags": sorted(flag for flag in node.get("flags") or [] if flag not in _TRANSIENT_FLAGS),
        "state": node.get("state") or {},
        "text": node.get("text"),
        "description": node.get("description"),
        "ariaValueText": node.get("ariaValueText"),
        "attrs": node.get("attrs") or {},
    }


def _signature_delta(before: JsonDict, after: JsonDict) -> JsonDict:
    delta: JsonDict = {}
    for key in ("role", "name", "text", "description", "ariaValueText"):
        if before.get(key) != after.get(key):
            delta[key] = [before.get(key), after.get(key)]
    added = sorted(set(after["flags"]) - set(before["flags"]))
    removed = sorted(set(before["flags"]) - set(after["flags"]))
    if added or removed:
        delta["flags"] = {k: v for k, v in (("added", added), ("removed", removed)) if v}
    for key in ("state", "attrs"):
        old, new = before.get(key) or {}, after.get(key) or {}
        changed = {
            item: [old.get(item), new.get(item)]
            for item in sorted(set(old) | set(new)) if old.get(item) != new.get(item)
        }
        if changed:
            delta[key] = changed
    return delta


def _watched_snapshot(lines: List[str], node_ids: Set[str], scope: str) -> Dict[str, Optional[JsonDict]]:
    """Semantic state of each watched node (None when absent from the view)."""
    found: Dict[str, Optional[JsonDict]] = {node_id: None for node_id in node_ids}
    index = 0
    while index < len(lines):
        head = AXTREE_LINE_RE.match(lines[index])
        if head is None or head.group("id") not in node_ids:
            index += 1
            continue
        node = parse_axtree_line(lines[index]) or {}
        entry: JsonDict = {"self": _semantic_signature(node)}
        if scope == "subtree":
            depth = int(head.group("depth"))
            descendants: Dict[str, JsonDict] = {}
            cursor = index + 1
            while cursor < len(lines):
                child = AXTREE_LINE_RE.match(lines[cursor])
                if child is not None:
                    if int(child.group("depth")) <= depth:
                        break
                    parsed = parse_axtree_line(lines[cursor]) or {}
                    descendants[child.group("id")] = _semantic_signature(parsed)
                cursor += 1
            entry["descendants"] = descendants
        found[head.group("id")] = entry
        index += 1
    return found


def _bounded_query_sample(value: JsonDict) -> JsonDict:
    raw = value.get("value")
    rendered = raw if isinstance(raw, str) else json.dumps(raw, ensure_ascii=False, sort_keys=True, default=str)
    return {
        "ok": value.get("ok") is True,
        "value": rendered[:500],
        "valueTruncated": len(rendered) > 500,
        "valueSha256": hashlib.sha256(rendered.encode("utf-8")).hexdigest(),
        **({"error": str(value["error"])[:200]} if value.get("error") else {}),
    }


def _compare_entry(before: Optional[JsonDict], after: Optional[JsonDict]) -> Optional[JsonDict]:
    if before is None and after is None:
        return None
    if before is None:
        return {"change": "appeared", "label": _label(after["self"]["role"], after["self"]["name"])}
    if after is None:
        return {"change": "removed", "label": _label(before["self"]["role"], before["self"]["name"])}
    event: JsonDict = {}
    delta = _signature_delta(before["self"], after["self"])
    if delta:
        event["delta"] = delta
    if "descendants" in after:
        old = before.get("descendants") or {}
        new = after["descendants"]
        added = [key for key in new if key not in old]
        removed = [key for key in old if key not in new]
        updated = [key for key in new if key in old and new[key] != old[key]]
        if added or removed or updated:
            event["subtree"] = {
                "added": len(added), "removed": len(removed), "updated": len(updated),
                "samples": [
                    f"+ {key} {_label(new[key]['role'], new[key]['name'])}" for key in added[:3]
                ] + [
                    f"- {key} {_label(old[key]['role'], old[key]['name'])}" for key in removed[:3]
                ] + [
                    f"~ {key} {_label(new[key]['role'], new[key]['name'])}" for key in updated[:3]
                ],
            }
    if not event:
        return None
    event["change"] = "updated"
    event["label"] = _label(after["self"]["role"], after["self"]["name"])
    return event


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------

@dataclass
class _Transition:
    base: str
    target: str
    records: Optional[List[str]]


@dataclass
class _PageState:
    epoch: str
    version: str
    sequence: Optional[int]
    lines: List[str]
    transitions: Deque[_Transition] = field(
        default_factory=lambda: deque(maxlen=MAX_TRANSITIONS_PER_PAGE)
    )


ReadFn = Callable[[], Awaitable[Any]]
BusyFn = Callable[[], bool]


@dataclass
class Watch:
    watch_id: str
    consumer: str
    page_id: str
    node_ids: List[str]
    scope: str
    epoch: str
    created_at: float
    expires_at: float
    labels: Dict[str, str]
    baseline: Dict[str, Optional[JsonDict]]
    detail_baseline: Dict[Tuple[str, str], Any] = field(default_factory=dict)
    sample_count: int = 0
    first_sample_at: str = ""
    last_sample_at: str = ""
    last_samples: Dict[str, JsonDict] = field(default_factory=dict)
    probe_error_count: int = 0
    read: Optional[ReadFn] = None
    busy: Optional[BusyFn] = None
    events: Deque[JsonDict] = field(default_factory=lambda: deque(maxlen=MAX_EVENTS_PER_WATCH))
    dropped: int = 0
    closed_reason: Optional[str] = None
    version: str = ""
    signal: Optional[asyncio.Event] = None

    def describe(self) -> JsonDict:
        return {
            "watchId": self.watch_id,
            "pageId": self.page_id,
            "nodes": [{"id": node_id, "label": self.labels.get(node_id, "")} for node_id in self.node_ids],
            "scope": self.scope,
            "sampleCount": self.sample_count,
            "firstSampleAt": self.first_sample_at or None,
            "lastSampleAt": self.last_sample_at or None,
            "lastSamples": self.last_samples,
            "probeErrorCount": self.probe_error_count,
            "status": "closed" if self.closed_reason else "active",
            **({"closedReason": self.closed_reason} if self.closed_reason else {}),
            "expiresInSeconds": max(0, int(self.expires_at - time.monotonic())),
        }


class ObservationChannel:
    def __init__(self) -> None:
        self._pages: Dict[str, _PageState] = {}
        self._cursors: Dict[Tuple[str, str], Tuple[str, str]] = {}
        self._watches: Dict[str, Watch] = {}
        self._pollers: Dict[str, asyncio.Task] = {}
        # Recent exact queries are comparable with later queries on the same
        # surface. A full AX line is not a substitute for rendered text.
        self._details: Dict[str, OrderedDict[Tuple[str, str], Any]] = {}

    # -- chain -------------------------------------------------------------

    def record_full(self, page_id: str, data: JsonDict) -> None:
        """Record one hydrated full view (called for every client's read)."""
        observation = data.get("observation") if isinstance(data, dict) else None
        lines = data.get("lines") if isinstance(data, dict) else None
        if not page_id or not isinstance(observation, dict) or not isinstance(lines, list):
            return
        version = str(observation.get("version") or "")
        epoch = str(observation.get("documentEpoch") or "")
        if not version or not epoch:
            return
        sequence = version_sequence(version)
        state = self._pages.get(page_id)
        if state is None or state.epoch != epoch:
            if state is not None:
                self._close_page_watches(page_id, "document_changed")
            self._details.pop(page_id, None)
            self._pages[page_id] = _PageState(epoch, version, sequence, lines)
            self._evaluate_watches(page_id)
            return
        if version != state.version:
            diff = data.get("diff") if isinstance(data.get("diff"), dict) else {}
            records = diff.get("records") if diff.get("status") == "computed" else None
            continuous = (
                sequence is not None and state.sequence is not None
                and sequence == state.sequence + 1
            )
            state.transitions.append(_Transition(
                state.version, version,
                list(records) if continuous and isinstance(records, list) else None,
            ))
            state.version, state.sequence = version, sequence
        state.lines = lines
        self._evaluate_watches(page_id)

    def forget_page(self, page_id: str, reason: str = "page_closed") -> None:
        self._pages.pop(page_id, None)
        self._details.pop(page_id, None)
        for key in [key for key in self._cursors if key[1] == page_id]:
            del self._cursors[key]
        self._close_page_watches(page_id, reason)

    def _chain_records(self, page_id: str, base: str) -> Optional[List[str]]:
        state = self._pages.get(page_id)
        if state is None:
            return None
        if base == state.version:
            return []
        collected: List[List[str]] = []
        expected_target = state.version
        for transition in reversed(state.transitions):
            if transition.target != expected_target or transition.records is None:
                return None
            collected.append(transition.records)
            if transition.base == base:
                return [record for records in reversed(collected) for record in records]
            expected_target = transition.base
        return None

    # -- model view --------------------------------------------------------

    def model_view(self, consumer: str, page_id: str, data: JsonDict) -> JsonDict:
        """Decide what a worker sees for a full view it just received.

        Returns ``{"delivery": "full"}``, ``{"delivery": "unchanged", ...}`` or
        ``{"delivery": "changes", "since": v, "changes": [...], ...}`` and
        advances the worker's cursor to the delivered version.
        """
        observation = data.get("observation") if isinstance(data, dict) else None
        state = self._pages.get(page_id)
        if not consumer or not isinstance(observation, dict) or state is None:
            return {"delivery": "full"}
        version = str(observation.get("version") or "")
        epoch = str(observation.get("documentEpoch") or "")
        cursor = self._cursors.get((consumer, page_id))
        if version and epoch:
            self._cursors[(consumer, page_id)] = (epoch, version)
        if version != state.version or epoch != state.epoch:
            # A later read (a watch poll) already moved the chain on; this
            # response is not the head, so it cannot anchor a change view.
            return {"delivery": "full", "reason": "superseded_by_newer_read"}
        if cursor is None or cursor[0] != epoch:
            return {"delivery": "full"}
        if cursor[1] == version:
            return {"delivery": "unchanged", "version": version}
        records = self._chain_records(page_id, cursor[1])
        if records is None:
            return {"delivery": "full", "reason": "version_chain_gap"}
        rendered = render_changes(records, data.get("lines") or [])
        if rendered["reset"]:
            return {"delivery": "full", "reason": "platform_reset"}
        size = sum(len(line) for line in rendered["changes"])
        full_size = sum(len(line) for line in data.get("lines") or []) or 1
        if size > full_size * MAX_CHANGE_VIEW_RATIO:
            return {"delivery": "full", "reason": "change_view_too_large"}
        return {
            "delivery": "changes",
            "since": cursor[1],
            "version": version,
            "changes": rendered["changes"],
            **({"folded": rendered["folded"]} if rendered["folded"] else {}),
        }

    def reset_consumer_view(self, consumer: str, page_id: str) -> None:
        """Forget what a worker holds, so its next read is shown in full."""
        self._cursors.pop((consumer, page_id), None)

    # -- watches -----------------------------------------------------------

    def current_lines(self, page_id: str) -> Optional[List[str]]:
        state = self._pages.get(page_id)
        return state.lines if state is not None else None

    def preferred_probe_surface(self, page_id: str, node_ids: List[str], scope: str) -> str:
        """Use a prior exact query first when the worker already has one."""
        cache = self._details.get(page_id, {})
        choices = ("text:rendered", "dom:1") if scope == "subtree" else ("state", "text:rendered")
        for surface in choices:
            if any((node_id, surface) in cache for node_id in node_ids):
                return surface
        return "text:rendered"

    def open_watch(
        self,
        *,
        consumer: str,
        page_id: str,
        node_ids: List[str],
        scope: str,
        ttl_seconds: float,
        read: Optional[ReadFn],
        busy: Optional[BusyFn],
        poll_interval_seconds: float,
    ) -> JsonDict:
        state = self._pages.get(page_id)
        if state is None:
            return {"status": "error", "reason": "no_current_observation",
                    "next_instruction": "Read DOM.getAXTree for this page first; watches start from a view you hold."}
        active = [w for w in self._watches.values() if w.consumer == consumer and not w.closed_reason]
        if len(active) >= MAX_WATCHES_PER_CONSUMER:
            return {"status": "error", "reason": "too_many_watches",
                    "activeWatchIds": [w.watch_id for w in active],
                    "next_instruction": "Close a watch with unwatch_nodes before opening another."}
        unique = list(dict.fromkeys(node_ids))[:MAX_NODES_PER_WATCH]
        baseline = _watched_snapshot(state.lines, set(unique), scope)
        missing = [node_id for node_id in unique if baseline.get(node_id) is None]
        if missing:
            return {"status": "error", "reason": "node_not_in_current_view", "missingIds": missing,
                    "next_instruction": "Watch only ids from the latest DOM.getAXTree view of this page."}
        now = time.monotonic()
        watch = Watch(
            watch_id="w_" + uuid.uuid4().hex[:12],
            consumer=consumer,
            page_id=page_id,
            node_ids=unique,
            scope=scope,
            epoch=state.epoch,
            created_at=now,
            expires_at=now + ttl_seconds,
            labels={
                node_id: _label(entry["self"]["role"], entry["self"]["name"])
                for node_id, entry in baseline.items() if entry is not None
            },
            baseline=baseline,
            detail_baseline={
                key: copy.deepcopy(value)
                for key, value in self._details.get(page_id, {}).items()
                if key[0] in unique
            },
            read=read,
            busy=busy,
            version=state.version,
        )
        self._watches[watch.watch_id] = watch
        self._ensure_poller(page_id, poll_interval_seconds)
        return {"status": "active", **watch.describe(), "baselineVersion": state.version}

    def record_detail(self, page_id: str, surface: str, records: List[Any]) -> None:
        """Compare exact query values without pretending they are full AX nodes."""
        if page_id not in self._pages or not surface:
            return
        cache = self._details.setdefault(page_id, OrderedDict())
        for record in records:
            if not isinstance(record, dict):
                continue
            target = record.get("target") if isinstance(record.get("target"), dict) else {}
            node_id = str(target.get("resolvedId") or target.get("requestedId") or "")
            if not node_id:
                continue
            value = {"ok": record.get("ok") is True,
                     "value": copy.deepcopy(record.get("value")),
                     "error": record.get("error") if record.get("ok") is False else None}
            key = (node_id, surface)
            for watch in self._watches.values():
                if watch.page_id != page_id or watch.closed_reason or node_id not in watch.node_ids:
                    continue
                previous = watch.detail_baseline.get(key)
                sampled_at = datetime.now(timezone.utc).isoformat()
                watch.sample_count += 1
                watch.first_sample_at = watch.first_sample_at or sampled_at
                watch.last_sample_at = sampled_at
                sample = _bounded_query_sample(value)
                watch.last_samples[f"{surface}:{node_id}"] = sample
                if previous is None:
                    self._push(watch, {"watchId": watch.watch_id, "nodeId": node_id,
                                       "change": "current_sample", "surface": surface,
                                       "sample": sample,
                                       "baselineStatus": "no_comparable_prior_query",
                                       "sampledAt": sampled_at})
                elif previous != value:
                    self._push(watch, {"watchId": watch.watch_id, "nodeId": node_id,
                                       "change": "detail_changed", "surface": surface,
                                       "before": _bounded_query_sample(previous),
                                       "after": sample, "sampledAt": sampled_at})
                watch.detail_baseline[key] = copy.deepcopy(value)
            cache[key] = value
            cache.move_to_end(key)
            while len(cache) > 256:
                cache.popitem(last=False)

    def record_probe_error(self, page_id: str, node_ids: List[str]) -> None:
        for watch in self._watches.values():
            if watch.page_id == page_id and not watch.closed_reason and set(node_ids) & set(watch.node_ids):
                watch.probe_error_count += 1

    def close_watches(
        self,
        consumer: str,
        *,
        watch_id: Optional[str] = None,
        page_id: Optional[str] = None,
        reason: str = "closed_by_agent",
    ) -> List[JsonDict]:
        closed: List[JsonDict] = []
        for watch in list(self._watches.values()):
            if watch.consumer != consumer:
                continue
            if watch_id and watch.watch_id != watch_id:
                continue
            if page_id and watch.page_id != page_id:
                continue
            pending = self._drain_watch(watch)
            if not watch.closed_reason:
                watch.closed_reason = reason
            self._watches.pop(watch.watch_id, None)
            closed.append({**watch.describe(), "undeliveredEvents": pending})
            if watch.signal is not None:
                watch.signal.set()
        return closed

    def close_consumer(self, consumer: str) -> None:
        self.close_watches(consumer, reason="agent_finished")
        for key in [key for key in self._cursors if key[0] == consumer]:
            del self._cursors[key]

    def active_watches(self, consumer: str) -> List[Watch]:
        return [w for w in self._watches.values() if w.consumer == consumer and not w.closed_reason]

    def drain_events(self, consumer: str, *, limit: int = 20) -> List[JsonDict]:
        """Pop queued events (and final close notices) for one worker."""
        self._expire()
        events: List[JsonDict] = []
        for watch in list(self._watches.values()):
            if watch.consumer != consumer:
                continue
            events.extend(self._drain_watch(watch, limit=max(0, limit - len(events))))
            if watch.closed_reason and not watch.events:
                self._watches.pop(watch.watch_id, None)
        return events

    async def wait(self, consumer: str, watch_id: Optional[str], timeout_seconds: float) -> JsonDict:
        deadline = time.monotonic() + max(0.0, timeout_seconds)
        while True:
            events = self._drain_selected(consumer, watch_id)
            if events:
                return {
                    "status": (
                        "observed" if all(event.get("change") == "current_sample" for event in events)
                        else "changed"
                    ),
                    "events": events,
                }
            targets = [
                w for w in self._watches.values()
                if w.consumer == consumer and (not watch_id or w.watch_id == watch_id)
            ]
            if not targets:
                return {"status": "no_active_watch",
                        "next_instruction": "Open a watch with watch_nodes first."}
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return {"status": "timeout", "watches": [w.describe() for w in targets]}
            signal = asyncio.Event()
            for watch in targets:
                watch.signal = signal
            try:
                await asyncio.wait_for(signal.wait(), timeout=min(remaining, 1.0))
            except asyncio.TimeoutError:
                pass

    def _drain_selected(self, consumer: str, watch_id: Optional[str]) -> List[JsonDict]:
        self._expire()
        events: List[JsonDict] = []
        for watch in list(self._watches.values()):
            if watch.consumer != consumer or (watch_id and watch.watch_id != watch_id):
                continue
            events.extend(self._drain_watch(watch))
            if watch.closed_reason and not watch.events:
                self._watches.pop(watch.watch_id, None)
        return events

    def _drain_watch(self, watch: Watch, limit: int = MAX_EVENTS_PER_WATCH) -> List[JsonDict]:
        events: List[JsonDict] = []
        while watch.events and len(events) < limit:
            events.append(watch.events.popleft())
        if watch.dropped and not watch.events:
            events.append({"watchId": watch.watch_id, "change": "events_dropped", "count": watch.dropped})
            watch.dropped = 0
        return events

    def _push(self, watch: Watch, event: JsonDict) -> None:
        if len(watch.events) == watch.events.maxlen:
            watch.dropped += 1
        watch.events.append(event)
        if watch.signal is not None:
            watch.signal.set()

    def _close_page_watches(self, page_id: str, reason: str) -> None:
        for watch in self._watches.values():
            if watch.page_id == page_id and not watch.closed_reason:
                watch.closed_reason = reason
                self._push(watch, {"watchId": watch.watch_id, "change": "watch_closed", "reason": reason})

    def _expire(self) -> None:
        now = time.monotonic()
        for watch in self._watches.values():
            if not watch.closed_reason and now >= watch.expires_at:
                watch.closed_reason = "expired"
                self._push(watch, {"watchId": watch.watch_id, "change": "watch_closed", "reason": "expired"})

    def _evaluate_watches(self, page_id: str) -> None:
        state = self._pages.get(page_id)
        if state is None:
            return
        for watch in self._watches.values():
            if watch.page_id != page_id or watch.closed_reason or watch.version == state.version:
                continue
            current = _watched_snapshot(state.lines, set(watch.node_ids), watch.scope)
            for node_id in watch.node_ids:
                event = _compare_entry(watch.baseline.get(node_id), current.get(node_id))
                if event is not None:
                    self._push(watch, {
                        "watchId": watch.watch_id,
                        "nodeId": node_id,
                        "version": state.version,
                        **event,
                    })
            watch.baseline = current
            watch.version = state.version

    # -- poller ------------------------------------------------------------

    def _ensure_poller(self, page_id: str, interval: float) -> None:
        task = self._pollers.get(page_id)
        if task is not None and not task.done():
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._pollers[page_id] = loop.create_task(self._poll(page_id, interval))

    async def _poll(self, page_id: str, interval: float) -> None:
        failures = 0
        try:
            while True:
                await asyncio.sleep(interval)
                self._expire()
                watches = [
                    w for w in self._watches.values()
                    if w.page_id == page_id and not w.closed_reason and w.read is not None
                ]
                if not watches:
                    return
                owner = watches[0]
                if owner.busy is not None and owner.busy():
                    # The worker is mid-tool on its own page: do not queue a
                    # read in front of its action.
                    continue
                try:
                    ok = await owner.read()
                except asyncio.CancelledError:
                    raise
                except Exception:  # noqa: BLE001 - a failed poll is counted, not fatal
                    ok = False
                failures = 0 if ok else failures + 1
                if failures >= POLL_FAILURE_LIMIT:
                    self._close_page_watches(page_id, "page_unavailable")
                    return
        finally:
            if self._pollers.get(page_id) is asyncio.current_task():
                self._pollers.pop(page_id, None)


_CHANNEL = ObservationChannel()


def observation_channel() -> ObservationChannel:
    return _CHANNEL


def record_hydrated_response(params: Any, response: Any) -> None:
    """Record a hydrated DOM.getAXTree response on the process channel."""
    if not isinstance(response, dict):
        return
    data = response.get("data")
    if not isinstance(data, dict):
        return
    page_id = str((params or {}).get("pageId") or data.get("pageId") or "") if isinstance(params, dict) else str(data.get("pageId") or "")
    if data.get("mode") == "full" and "lines" in data:
        _CHANNEL.record_full(page_id, data)
    elif data.get("mode") == "detail" and isinstance(data.get("records"), list):
        query = params.get("query") if isinstance(params, dict) and isinstance(params.get("query"), dict) else {}
        view = str(query.get("view") or "")
        surface = (
            f"text:{query.get('textMode') or 'semantic'}" if view == "text" else
            f"dom:{query.get('maxDepth', 0)}" if view == "dom" else view
        )
        _CHANNEL.record_detail(page_id, surface, data["records"])
