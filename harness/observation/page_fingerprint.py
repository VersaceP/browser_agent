"""
harness.observation.page_fingerprint - Shared AXTree/Page state fingerprints.

The BrowserAgent uses this module for three related signals:
- loop_nudge: detect repeated actions while the page is effectively stagnant
- page_stats: emit a compact page summary only when it is useful
- snapshot_diff: passively log semantic/physical page changes after AXTree reads
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter, deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Deque, List, Optional, Tuple

from harness.observation.overlay_detector import detect_overlay_from_result
from harness.utils import JsonDict


AXTREE_ID_RE = re.compile(r"\b\d+:-?\d+:-?\d+\b")
# Accepts both the legacy indent-based line format and the current
# depth-prefixed one (`3 [3:426:426] link "TAAFT" # @10,0,106,94`).
AXTREE_LINE_RE = re.compile(
    r"^(?:\d+\s+)?\s*\[(?P<id>\d+:-?\d+:-?\d+)\]\s+"
    r"(?P<role>[\w-]+)(?:\s+\"(?P<name>[^\"]*)\")?"
)
ACTIONABLE_ROLES = {
    "button",
    "checkbox",
    "combobox",
    "link",
    "listbox",
    "menuitem",
    "option",
    "radio",
    "radiobutton",
    "searchbox",
    "slider",
    "spinbutton",
    "switch",
    "tab",
    "textbox",
    "treeitem",
}
TEXT_FILE_CACHE_MAX = 32
SNAPSHOT_HISTORY_MAX = 5
_TEXT_FILE_CACHE: dict[Tuple[str, int, int], str] = {}


@dataclass
class PageFingerprint:
    source: str
    page_id: str = ""
    url: str = ""
    title: str = ""
    physical_ids: set[str] = field(default_factory=set)
    semantic_counts: Counter[str] = field(default_factory=Counter)
    role_counts: Counter[str] = field(default_factory=Counter)
    total_nodes: int = 0

    @property
    def physical_count(self) -> int:
        return len(self.physical_ids)

    @property
    def physical_hash(self) -> str:
        return stable_hash(sorted(self.physical_ids)) if self.physical_ids else ""

    @property
    def semantic_count(self) -> int:
        return len(self.semantic_counts)

    @property
    def semantic_total(self) -> int:
        return sum(self.semantic_counts.values())

    @property
    def actionable_count(self) -> int:
        return sum(int(self.role_counts.get(role, 0)) for role in ACTIONABLE_ROLES)

    @property
    def semantic_hash(self) -> str:
        if not self.semantic_counts:
            return ""
        return stable_hash(sorted(self.semantic_counts.items()))

    def key(self) -> Tuple[str, str, str, int, str, int, str]:
        return (
            self.page_id,
            self.url,
            self.title,
            self.physical_count,
            self.physical_hash,
            self.semantic_count,
            self.semantic_hash,
        )

    def stagnation_key(self) -> Tuple[str, str, str]:
        if self.semantic_count and self.semantic_hash:
            return ("semantic", self.page_id, self.semantic_hash)
        if self.physical_count and self.physical_hash:
            return ("physical", self.page_id, self.physical_hash)
        return ("location", self.page_id, stable_hash([self.url, self.title]))

    def to_log_payload(self) -> JsonDict:
        return {
            "source": self.source,
            "pageId": self.page_id or None,
            "url": self.url or None,
            "title": self.title or None,
            "totalNodes": self.total_nodes,
            "physicalCount": self.physical_count,
            "physicalHash": self.physical_hash,
            "actionableCount": self.actionable_count,
            "namedActionableCount": self.semantic_total,
            "semanticCount": self.semantic_count,
            "semanticHash": self.semantic_hash,
            "roleCounts": dict(self.role_counts),
        }


class PageObservationTracker:
    def __init__(self) -> None:
        self.axtree_snapshots: Deque[Tuple[int, PageFingerprint]] = deque(
            maxlen=SNAPSHOT_HISTORY_MAX
        )
        self.latest_state_by_page_id: dict[str, Tuple[str, str]] = {}
        self.pending_page_stats: Optional[JsonDict] = None
        self.last_emitted_stats_key: Tuple[str, str, str, str] = ("", "", "", "")
        self.last_emitted_total_nodes = 0

    def observe_result(
        self,
        tool_call: JsonDict,
        result: JsonDict,
        *,
        step: int,
        agent: Any = None,
    ) -> JsonDict:
        fingerprint = page_fingerprint_from_result(tool_call, result, agent=agent)
        if fingerprint is None:
            return {}

        self._merge_known_page_state(fingerprint)
        if fingerprint.url or fingerprint.title:
            self.latest_state_by_page_id[fingerprint.page_id] = (
                fingerprint.url,
                fingerprint.title,
            )

        out: JsonDict = {"fingerprint": fingerprint}
        if fingerprint.source != "DOM.getAXTree":
            return out

        previous = self.axtree_snapshots[-1] if self.axtree_snapshots else None
        self.axtree_snapshots.append((step, fingerprint))
        overlay = (
            detect_overlay_from_result(result)
            if fingerprint.source == "DOM.getAXTree" else None
        )
        page_stats = self._maybe_page_stats(step, fingerprint, overlay=overlay)
        if page_stats is not None:
            self.pending_page_stats = page_stats
            out["pageStats"] = page_stats
        if previous is not None:
            from_step, previous_fp = previous
            out["snapshotDiff"] = snapshot_diff(
                previous_fp,
                fingerprint,
                from_step=from_step,
                to_step=step,
            )
        return out

    def consume_page_stats(self) -> Optional[JsonDict]:
        page_stats = self.pending_page_stats
        self.pending_page_stats = None
        return page_stats

    def _merge_known_page_state(self, fingerprint: PageFingerprint) -> None:
        if not fingerprint.page_id:
            return
        known = self.latest_state_by_page_id.get(fingerprint.page_id)
        if not known:
            return
        known_url, known_title = known
        if not fingerprint.url:
            fingerprint.url = known_url
        if not fingerprint.title:
            fingerprint.title = known_title

    def _maybe_page_stats(
        self,
        step: int,
        fingerprint: PageFingerprint,
        *,
        overlay: Optional[JsonDict] = None,
    ) -> Optional[JsonDict]:
        key = (
            fingerprint.page_id,
            fingerprint.url,
            _actionable_bucket(fingerprint.actionable_count),
            _node_bucket(fingerprint.total_nodes),
        )
        should_emit = key != self.last_emitted_stats_key or overlay is not None
        if not should_emit and self.last_emitted_total_nodes:
            delta = abs(fingerprint.total_nodes - self.last_emitted_total_nodes)
            if delta >= 30:
                should_emit = (
                    delta / max(1, self.last_emitted_total_nodes)
                ) >= 0.30
        if not should_emit:
            return None

        self.last_emitted_stats_key = key
        self.last_emitted_total_nodes = fingerprint.total_nodes
        hint = page_stats_hint(fingerprint)
        if overlay and overlay.get("hint"):
            hint = (
                f"{hint} {overlay.get('hint')}"
                if hint else str(overlay.get("hint"))
            )
        payload: JsonDict = {
            "type": "page_stats",
            "step": step,
            "pageId": fingerprint.page_id or None,
            "url": fingerprint.url or None,
            "title": fingerprint.title or None,
            "nodes": fingerprint.total_nodes,
            "physicalIds": fingerprint.physical_count,
            "actionable": fingerprint.actionable_count,
            "namedActionable": fingerprint.semantic_total,
            "semanticItems": fingerprint.semantic_count,
            "links": int(fingerprint.role_counts.get("link", 0)),
            "buttons": int(fingerprint.role_counts.get("button", 0)),
            "textboxes": int(fingerprint.role_counts.get("textbox", 0))
            + int(fingerprint.role_counts.get("searchbox", 0)),
        }
        if hint:
            payload["hint"] = hint
        if overlay:
            payload["overlay"] = overlay
        return payload


def page_fingerprint_from_result(
    tool_call: JsonDict,
    result: JsonDict,
    *,
    agent: Any = None,
) -> Optional[PageFingerprint]:
    name = str(tool_call.get("name") or "")
    raw_input = tool_call.get("input") if isinstance(tool_call.get("input"), dict) else {}
    method = str(raw_input.get("method") or "") if name == "browser_call" else name
    if method not in {"DOM.getAXTree", "Page.getState", "Page.navigate", "navigate_verified"}:
        return None
    params = raw_input.get("params") if isinstance(raw_input.get("params"), dict) else raw_input
    page_id = str(params.get("pageId") or "")
    data = _response_data(result)
    if not page_id:
        page_id = str(data.get("pageId") or "")
    url = str(data.get("url") or data.get("currentUrl") or "")
    title = str(data.get("title") or "")

    ids: set[str] = set()
    role_counts: Counter[str] = Counter()
    semantic_counts: Counter[str] = Counter()
    total_nodes = optional_int(data.get("nodeCount"), 0) or 0
    _collect_from_any(
        result,
        ids=ids,
        role_counts=role_counts,
        semantic_counts=semantic_counts,
        limit_ids=5000,
        limit_semantic=1000,
    )
    if not ids:
        agent_ids = getattr(agent, "axtree_ids", None)
        if isinstance(agent_ids, set):
            ids = {str(item) for item in agent_ids if str(item).strip()}
    if not total_nodes and role_counts:
        total_nodes = sum(role_counts.values())

    return PageFingerprint(
        source=method,
        page_id=page_id,
        url=url,
        title=title,
        physical_ids=ids,
        semantic_counts=semantic_counts,
        role_counts=role_counts,
        total_nodes=total_nodes,
    )


def snapshot_diff(
    previous: PageFingerprint,
    current: PageFingerprint,
    *,
    from_step: int,
    to_step: int,
) -> JsonDict:
    semantic_added = current.semantic_counts - previous.semantic_counts
    semantic_removed = previous.semantic_counts - current.semantic_counts
    physical_added = current.physical_ids - previous.physical_ids
    physical_removed = previous.physical_ids - current.physical_ids
    cross_page = (
        bool(previous.page_id and current.page_id and previous.page_id != current.page_id)
        or bool(previous.url and current.url and previous.url != current.url)
    )
    total_delta = current.total_nodes - previous.total_nodes
    return {
        "type": "snapshot_diff",
        "fromStep": from_step,
        "toStep": to_step,
        "fromPageId": previous.page_id or None,
        "toPageId": current.page_id or None,
        "crossPageDiff": cross_page,
        "semanticAdded": sum(semantic_added.values()),
        "semanticRemoved": sum(semantic_removed.values()),
        "physicalAdded": len(physical_added),
        "physicalRemoved": len(physical_removed),
        "totalNodeDelta": total_delta,
        "semanticChanged": bool(semantic_added or semantic_removed),
        "physicalChanged": bool(physical_added or physical_removed),
    }


def page_stats_hint(fingerprint: PageFingerprint) -> str:
    if fingerprint.actionable_count > 100:
        return (
            "Large interactive page; use the current AXTree/offload evidence to"
            " narrow targets before interacting."
        )
    if fingerprint.actionable_count < 5 and fingerprint.total_nodes < 30:
        return (
            "Page appears sparse or empty; wait/resync before assuming content"
            " is unavailable."
        )
    return ""


def _actionable_bucket(count: int) -> str:
    if count < 5:
        return "sparse"
    if count > 100:
        return "large"
    return "normal"


def _node_bucket(count: int) -> str:
    if count < 30:
        return "sparse"
    if count > 1000:
        return "large"
    return "normal"


def render_page_stats_for_prompt(page_stats: JsonDict) -> str:
    parts = [
        "<page_stats>",
        (
            f"pageId={page_stats.get('pageId') or ''} "
            f"title={json.dumps(page_stats.get('title') or '', ensure_ascii=False)} "
            f"url={json.dumps(page_stats.get('url') or '', ensure_ascii=False)}"
        ),
        (
            f"nodes={page_stats.get('nodes') or 0} "
            f"actionable={page_stats.get('actionable') or 0} "
            f"links={page_stats.get('links') or 0} "
            f"buttons={page_stats.get('buttons') or 0} "
            f"textboxes={page_stats.get('textboxes') or 0} "
            f"semanticItems={page_stats.get('semanticItems') or 0}"
        ),
    ]
    hint = str(page_stats.get("hint") or "")
    if hint:
        parts.append(f"hint={json.dumps(hint, ensure_ascii=False)}")
    overlay = page_stats.get("overlay")
    if isinstance(overlay, dict):
        parts.append(
            "overlay="
            + json.dumps(
                {
                    "type": overlay.get("type"),
                    "subtype": overlay.get("subtype"),
                    "confidence": overlay.get("confidence"),
                    "dismissibleSignal": overlay.get("dismissibleSignal"),
                    "evidence": overlay.get("evidence") or [],
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        parts.append(
            "runtimeStrategy="
            + json.dumps(
                {
                    "id": "browser_action.overlay.dismiss_ladder",
                    "trigger": "page_stats.overlay",
                    "next": (
                        "For dismissible business overlays, refresh DOM.getAXTree,"
                        " try a visible close/dismiss control if present, then"
                        " Escape, then"
                        " verified backdrop click only if the click target is"
                        " outside modal content. Do not click login/provider or"
                        " payment buttons automatically."
                    ),
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
    parts.append("</page_stats>")
    return "\n".join(parts)


def _collect_from_any(
    value: Any,
    *,
    ids: set[str],
    role_counts: Counter[str],
    semantic_counts: Counter[str],
    limit_ids: int,
    limit_semantic: int,
) -> None:
    def visit(item: Any) -> None:
        if isinstance(item, dict):
            path = item.get("savedPath")
            parsed_text_lines_file = False
            if isinstance(path, str) and item.get("format") == "text_lines":
                text = _read_text_file_cached(path)
                if text is not None:
                    parsed_text_lines_file = True
                    _collect_from_text(
                        text,
                        ids=ids,
                        role_counts=role_counts,
                        semantic_counts=semantic_counts,
                        limit_ids=limit_ids,
                        limit_semantic=limit_semantic,
                    )
            for key, nested in item.items():
                if parsed_text_lines_file and str(key) in {"outline", "summary", "preview"}:
                    continue
                visit(nested)
        elif isinstance(item, list):
            for nested in item:
                visit(nested)
        elif isinstance(item, str):
            if len(item) < 1_000_000:
                for match in AXTREE_ID_RE.findall(item):
                    if len(ids) >= limit_ids:
                        break
                    ids.add(match)
            if "\n" in item or AXTREE_LINE_RE.search(item):
                _collect_from_text(
                    item,
                    ids=ids,
                    role_counts=role_counts,
                    semantic_counts=semantic_counts,
                    limit_ids=limit_ids,
                    limit_semantic=limit_semantic,
                )

    visit(value)


def _collect_from_text(
    text: str,
    *,
    ids: set[str],
    role_counts: Counter[str],
    semantic_counts: Counter[str],
    limit_ids: int,
    limit_semantic: int,
) -> None:
    for line in text.splitlines():
        match = AXTREE_LINE_RE.match(line)
        if not match:
            continue
        ax_id = str(match.group("id") or "")
        if ax_id and len(ids) < limit_ids:
            ids.add(ax_id)
        role = normalize_role(match.group("role") or "")
        if not role:
            continue
        role_counts[role] += 1
        if role not in ACTIONABLE_ROLES:
            continue
        if sum(semantic_counts.values()) >= limit_semantic:
            continue
        name = normalize_accessible_name(match.group("name") or "")
        if not name:
            continue
        semantic_counts[f"{role}:{name}"] += 1


def _read_text_file_cached(path_text: str) -> Optional[str]:
    try:
        path = Path(path_text).resolve(strict=False)
        if not path.exists() or not path.is_file():
            return None
        stat = path.stat()
        key = (str(path), int(stat.st_mtime_ns), int(stat.st_size))
        cached = _TEXT_FILE_CACHE.get(key)
        if cached is not None:
            return cached
        text = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return None

    for old_key in list(_TEXT_FILE_CACHE.keys()):
        if old_key[0] == key[0] and old_key != key:
            _TEXT_FILE_CACHE.pop(old_key, None)
    _TEXT_FILE_CACHE[key] = text
    while len(_TEXT_FILE_CACHE) > TEXT_FILE_CACHE_MAX:
        _TEXT_FILE_CACHE.pop(next(iter(_TEXT_FILE_CACHE)))
    return text


def _response_data(result: Any) -> JsonDict:
    if not isinstance(result, dict):
        return {}
    response = result.get("response") if isinstance(result.get("response"), dict) else {}
    data = response.get("data") if isinstance(response.get("data"), dict) else {}
    return data


def normalize_role(value: str) -> str:
    return str(value or "").replace("_", "").replace("-", "").lower()


def normalize_accessible_name(value: str) -> str:
    return " ".join(str(value or "").lower().split())


def optional_int(value: Any, default: int = 0) -> int:
    try:
        if value is None:
            return default
        return int(value)
    except (TypeError, ValueError):
        return default


def stable_hash(value: Any) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        default=str,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]
