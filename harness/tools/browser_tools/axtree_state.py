"""AXTree cache, stale-id guard, and browser-side rematch bookkeeping."""

import re
from typing import Any, Dict, List, Optional, Set, Tuple

from harness.utils import JsonDict


def _response_data(result: JsonDict) -> JsonDict:
    response = result.get("response") if isinstance(result, dict) else None
    if not isinstance(response, dict):
        return {}
    data = response.get("data")
    return data if isinstance(data, dict) else {}


AXTREE_ID_RE = re.compile(r"^\d+:-?\d+:-?\d+$")
AXTREE_ID_TOKEN_RE = re.compile(r"\[(\d+:-?\d+:-?\d+)\]")
AXTREE_ID_ANYWHERE_RE = re.compile(r"\b\d+:-?\d+:-?\d+\b")
# Two live line formats: legacy indent-based (`  [30:553:553] link "TAAFT" #`)
# and the current depth-prefixed one (`3 [3:426:426] link "TAAFT" # @10,0,106,94`)
# where the leading integer is the node's depth in the ORIGINAL (unfiltered) AX
# tree — folding noise nodes keeps original depth values, so gaps like 0→3 are
# normal and consecutive depths are NOT contiguous.
AXTREE_LINE_RE = re.compile(
    r"^(?:(?P<depth>\d+)\s+)?(?P<indent>\s*)\[(?P<id>\d+:-?\d+:-?\d+)\]\s+"
    r"(?P<role>[^\s\"]+)(?:\s+\"(?P<name>.*?)\")?(?P<rest>.*)$"
)

# Compact layout tokens the panel appends after the accessible name, before the
# `#` actionable marker / `@x,y,w,h` viewport rect (px space pending live probe;
# see abcp-panel-quirks #11). Whitelisted so unknown future tokens are ignored;
# `zN` encodes stacking order.
AXTREE_KNOWN_FLAGS = frozenset({"hidden", "off", "blocked", "scroll", "sticky", "clip"})
AXTREE_Z_FLAG_RE = re.compile(r"^z-?\d+$")
AXTREE_RECT_RE = re.compile(r"@(-?\d+),(-?\d+),(\d+),(\d+)")


def _axtree_flags_and_rect(rest: str) -> Tuple[List[str], Optional[JsonDict]]:
    """Parse layout flags and the viewport rect out of a line's tail (the text
    after the quoted accessible name). Legacy lines without flags/rect yield
    ([], None)."""
    match = AXTREE_RECT_RE.search(rest)
    rect: Optional[JsonDict] = None
    if match:
        x, y, w, h = (int(group) for group in match.groups())
        rect = {"x": x, "y": y, "w": w, "h": h}
    head = rest[: match.start()] if match else rest
    flags = [
        token
        for token in head.replace("#", " ").split()
        if token in AXTREE_KNOWN_FLAGS or AXTREE_Z_FLAG_RE.match(token)
    ]
    return flags, rect

AXTREE_INVALIDATING_METHODS = {
    "Page.create",
    "Page.navigate",
    "Page.reload",
    "Page.go",
    "Page.recovered",
    "Page.close",
    "Page.switchTo",
    "Page.handleDialog",
    "File.handleChooser",
    "Runtime.evaluate",
    "Input.click",
    "Input.select",
    "Input.type",
    "Input.press",
    "Input.scroll",
    "Input.drag",
    "Hitl.requestPause",
}


def _precompute_axtree_snapshot(
    method: str,
    params: JsonDict,
    response: Any,
) -> Optional[JsonDict]:
    if method != "DOM.getAXTree":
        return None
    page_id = ""
    if isinstance(params, dict):
        page_id = str(params.get("pageId") or "")
    lines = _axtree_lines_from_value(response)
    nodes = _axtree_nodes_from_lines(lines)
    ids = _axtree_ids_from_value(response)
    if nodes:
        ids.update(str(node.get("id") or "") for node in nodes if node.get("id"))
    return {
        "ids": ids,
        "lines": lines,
        "nodes": nodes,
        "pageId": page_id,
    }


def _axtree_lines_from_value(value: Any, *, limit: int = 10000) -> List[str]:
    lines: List[str] = []

    def append_line(line: str) -> None:
        if len(lines) >= limit:
            return
        text = line.rstrip("\n")
        if AXTREE_LINE_RE.match(text):
            lines.append(text)

    def visit(item: Any) -> None:
        if len(lines) >= limit:
            return
        if isinstance(item, dict):
            for nested in item.values():
                visit(nested)
        elif isinstance(item, list):
            for nested in item:
                visit(nested)
                if len(lines) >= limit:
                    return
        elif isinstance(item, str):
            if "\n" in item:
                for line in item.splitlines():
                    append_line(line)
                    if len(lines) >= limit:
                        return
            else:
                append_line(item)

    visit(value)
    return lines


def _axtree_nodes_from_lines(lines: List[str]) -> List[JsonDict]:
    nodes: List[JsonDict] = []
    for index, line in enumerate(lines, start=1):
        match = AXTREE_LINE_RE.match(line)
        if not match:
            continue
        rest = str(match.group("rest") or "")
        name = str(match.group("name") or "")
        depth_prefix = match.group("depth")
        flags, rect = _axtree_flags_and_rect(rest)
        nodes.append({
            "id": match.group("id"),
            "role": match.group("role"),
            "name": name,
            "interactive": "#" in rest,
            "flags": flags,
            "rect": rect,
            "line": line,
            "lineNumber": index,
            "depth": (
                int(depth_prefix)
                if depth_prefix is not None
                else len(str(match.group("indent") or "")) // 2
            ),
        })
    return nodes


def _browser_side_rematch_mode(agent: Any) -> str:
    runtime = getattr(agent, "runtime", None)
    harness = getattr(runtime, "harness", None)
    mode = str(getattr(harness, "browser_side_rematch", "composite_only") or "")
    return mode if mode in {"off", "composite_only", "on"} else "composite_only"


def _check_stale_axtree_target(
    agent: Any,
    method: str,
    params: JsonDict,
    *,
    allow_rematch: bool = False,
) -> Optional[JsonDict]:
    if method == "DOM.getAXTree" or not isinstance(params, dict):
        return None
    target_ids = _axtree_ids_from_params(params)
    if not target_ids:
        return None

    current_ids = set(getattr(agent, "axtree_ids", set()) or set())
    epoch = int(getattr(agent, "axtree_epoch", 0) or 0)
    invalidated = bool(getattr(agent, "axtree_invalidated", True))
    current_page_id = str(getattr(agent, "axtree_page_id", "") or "")
    page_id = str(params.get("pageId") or "")
    missing = sorted(target_ids - current_ids)

    if allow_rematch and _browser_side_rematch_mode(agent) != "off":
        # Pass stale-but-previously-seen ids through so the browser-side
        # auto-rematch can fire. Page mismatches and never-seen ids stay
        # blocked; the rematch outcome is validated in
        # _apply_recovered_target and by the caller's verifier.
        page_mismatch = bool(
            page_id and current_page_id and page_id != current_page_id
        )
        seen = _axtree_seen_ids(agent, page_id or current_page_id)
        if not page_mismatch and target_ids and target_ids <= seen:
            logger = getattr(agent, "logger", None)
            if logger is not None:
                logger.write(
                    "axtree.stale_guard.rematch_passthrough",
                    {
                        "method": method,
                        "targetIds": sorted(target_ids),
                        "pageId": page_id or current_page_id or None,
                        "epoch": epoch,
                    },
                )
            return None

    if not current_ids or invalidated:
        reason = "axtree_snapshot_invalidated" if invalidated else "no_current_axtree_snapshot"
    elif page_id and current_page_id and page_id != current_page_id:
        reason = "axtree_page_mismatch"
    elif missing:
        reason = "axtree_id_not_in_current_snapshot"
    else:
        return None

    return {
        "status": "stale_element_reference",
        "reason": reason,
        "method": method,
        "pageId": page_id or None,
        "currentAXTreePageId": current_page_id or None,
        "axTreeEpoch": epoch,
        "targetIds": sorted(target_ids),
        "missingIds": missing,
        "tool_was_executed": False,
        "next_instruction": (
            "DOM changed or the target id is not from the current AXTree epoch."
            " Call Page.getState if lifecycle is uncertain, then DOM.getAXTree"
            " for this page and derive a fresh id/selector before retrying."
        ),
    }


def _axtree_ids_from_params(params: JsonDict) -> Set[str]:
    ids: Set[str] = set()

    def collect(value: Any) -> None:
        if not isinstance(value, dict):
            return
        for key in ("id", "nodeId", "targetId", "selector"):
            candidate = value.get(key)
            if isinstance(candidate, str) and AXTREE_ID_RE.match(candidate.strip()):
                ids.add(candidate.strip())

    collect(params)
    targets = params.get("targets")
    if isinstance(targets, list):
        for target in targets:
            collect(target)
    return ids


def _observe_axtree_state_after(
    agent: Any,
    method: str,
    params: JsonDict,
    result: JsonDict,
    *,
    precomputed_snapshot: Optional[JsonDict] = None,
    read_only_eval: bool = False,
    event_serial_before: Optional[int] = None,
    page_before: Optional[str] = None,
) -> None:
    if method == "DOM.getAXTree":
        snapshot = precomputed_snapshot if isinstance(precomputed_snapshot, dict) else {}
        raw_ids = snapshot.get("ids")
        ids = {str(item) for item in raw_ids if str(item).strip()} if isinstance(raw_ids, set) else set()
        if not ids:
            ids = _axtree_ids_from_value(result)
        page_id = ""
        if isinstance(params, dict):
            page_id = str(params.get("pageId") or "")
        if not page_id:
            page_id = str(snapshot.get("pageId") or "")
        if not page_id:
            page_id = str(_response_data(result).get("pageId") or "")
        if ids:
            agent.axtree_epoch = int(getattr(agent, "axtree_epoch", 0) or 0) + 1
            agent.axtree_ids = ids
            agent.axtree_page_id = page_id
            agent.axtree_invalidated = False
            agent.axtree_lines = list(snapshot.get("lines") or _axtree_lines_from_value(result))
            agent.axtree_nodes = list(snapshot.get("nodes") or _axtree_nodes_from_lines(agent.axtree_lines))
            _record_axtree_history(agent, page_id, agent.axtree_nodes, ids)
            logger = getattr(agent, "logger", None)
            if logger is not None:
                logger.write(
                    "axtree.snapshot",
                    {
                        "pageId": page_id or None,
                        "epoch": agent.axtree_epoch,
                        "idCount": len(ids),
                    },
                )
        return

    recovered = _recovered_target_from_result(result)
    if recovered is not None:
        _apply_recovered_target(agent, method, params, result, recovered)

    if method in AXTREE_INVALIDATING_METHODS and not (
        method == "Runtime.evaluate" and read_only_eval
    ):
        # Message-ordering race: if a same-page DOM.axTreeUpdated arrived DURING
        # this call (notification dispatched before the action response — a legal
        # websocket order), BrowserEventObserver already rebuilt a fresh full
        # snapshot and bumped axtree_event_serial. That snapshot is newer than any
        # mutation this method caused (the browser emitted it because of this very
        # action), so the pessimistic per-method invalidation must not clobber it.
        # Three gates:
        #  - serial advanced: an event actually applied during this call
        #  - clean: a later inconsistent recoveredTarget may have re-dirtied it
        #  - same page: the event refreshed the page we held before the call. The
        #    observer accepts ANY page's event onto an empty baseline, so without
        #    this gate a bootstrapped other-page snapshot could suppress this
        #    page's invalidation (page_before == event page guards against it).
        if event_serial_before is not None:
            event_serial_now = int(getattr(agent, "axtree_event_serial", 0) or 0)
            event_page = str(getattr(agent, "axtree_event_page_id", "") or "")
            held_page = str(page_before or "")
            same_page = bool(held_page and event_page and held_page == event_page)
            if (
                event_serial_now > int(event_serial_before)
                and same_page
                and not getattr(agent, "axtree_invalidated", False)
            ):
                logger = getattr(agent, "logger", None)
                if logger is not None:
                    logger.write(
                        "axtree.invalidation_superseded_by_event",
                        {
                            "method": method,
                            "pageId": event_page,
                            "epoch": int(getattr(agent, "axtree_epoch", 0) or 0),
                            "eventSerial": event_serial_now,
                        },
                    )
                return
        _invalidate_axtree_snapshot(agent, method, params)


AXTREE_SEEN_EPOCH_LIMIT = 3


def _record_axtree_history(
    agent: Any,
    page_id: str,
    nodes: Optional[List[JsonDict]],
    ids: Set[str],
) -> None:
    """Keep a bounded per-page history of recent snapshot ids with their
    role/name signatures. The stale guard uses it to distinguish "id from a
    recent snapshot of this page" (eligible for browser-side rematch) from
    "id we never saw" (always blocked), and rematch validation uses the
    signatures even after the live snapshot has been invalidated."""
    if not page_id:
        return
    signatures: Dict[str, JsonDict] = {}
    for node in nodes or []:
        node_id = str(node.get("id") or "")
        if node_id:
            signatures[node_id] = {
                "role": str(node.get("role") or ""),
                "name": str(node.get("name") or ""),
            }
    for node_id in ids:
        signatures.setdefault(str(node_id), {})
    history = getattr(agent, "axtree_seen_history", None)
    if not isinstance(history, dict):
        history = {}
        agent.axtree_seen_history = history
    epochs = history.setdefault(page_id, [])
    epochs.append(signatures)
    if len(epochs) > AXTREE_SEEN_EPOCH_LIMIT:
        del epochs[: len(epochs) - AXTREE_SEEN_EPOCH_LIMIT]


def _axtree_seen_ids(agent: Any, page_id: str) -> Set[str]:
    history = getattr(agent, "axtree_seen_history", None)
    if not isinstance(history, dict) or not page_id:
        return set()
    seen: Set[str] = set()
    for signatures in history.get(page_id, []):
        seen.update(signatures.keys())
    return seen


def _axtree_seen_signature(
    agent: Any,
    node_id: str,
    page_id: str,
) -> Optional[JsonDict]:
    """Most recent role/name signature recorded for node_id on `page_id`.

    Scoped to one page: canonical ids (frameId:axNodeId:domNodeId) can repeat
    across pages, so a global lookup would validate a rematch against an
    unrelated page's node. Without a page_id we cannot prove scope, so the
    lookup fails conservatively (rejecting the rematch)."""
    history = getattr(agent, "axtree_seen_history", None)
    if not isinstance(history, dict) or not page_id:
        return None
    for signatures in reversed(history.get(page_id, [])):
        signature = signatures.get(node_id)
        if signature:
            return signature
    return None


def _recovered_target_from_result(result: JsonDict) -> Optional[JsonDict]:
    data = _response_data(result)
    recovered = data.get("recoveredTarget")
    return recovered if isinstance(recovered, dict) else None


def _apply_recovered_target(
    agent: Any,
    method: str,
    params: JsonDict,
    result: JsonDict,
    recovered: JsonDict,
) -> None:
    """Record a browser-side stale-id rematch and surface a compact digest.

    A consistent rematch updates the id bookkeeping (current set + seen
    history); an inconsistent one only marks the snapshot dirty. Either way
    the result carries `axtreeRematch` so callers (composite tools, model)
    see what the browser did without consuming raw events."""
    previous_id = str(recovered.get("previousId") or recovered.get("oldId") or "")
    current_id = str(recovered.get("currentId") or recovered.get("newId") or "")
    if not previous_id or not current_id:
        return
    page_id = ""
    if isinstance(params, dict):
        page_id = str(params.get("pageId") or "")
    if not page_id:
        page_id = str(getattr(agent, "axtree_page_id", "") or "")
    consistent = _validate_rematch(agent, previous_id, page_id, recovered)
    digest = {
        "method": method,
        "previousId": previous_id,
        "currentId": current_id,
        "consistent": consistent,
    }
    if consistent:
        ids = set(getattr(agent, "axtree_ids", set()) or set())
        ids.discard(previous_id)
        ids.add(current_id)
        agent.axtree_ids = ids
        signature = _axtree_seen_signature(agent, previous_id, page_id) or {}
        if page_id:
            history = getattr(agent, "axtree_seen_history", None)
            if isinstance(history, dict) and history.get(page_id):
                history[page_id][-1][current_id] = dict(signature)
    else:
        agent.axtree_invalidated = True
    logger = getattr(agent, "logger", None)
    if logger is not None:
        logger.write("axtree.rematch_observed", digest)
    if isinstance(result, dict):
        result["axtreeRematch"] = digest


def _validate_rematch(
    agent: Any,
    previous_id: str,
    page_id: str,
    recovered: JsonDict,
) -> bool:
    """Accept a browser-side rematch only when the new node's role (and name,
    when the original had one) matches the original target's recorded
    signature on the same page. Missing evidence on either side fails
    conservatively."""
    original = _axtree_seen_signature(agent, previous_id, page_id)
    if not original:
        return False
    new_role = str(
        recovered.get("role") or recovered.get("currentRole") or ""
    ).strip().lower()
    new_name = str(
        recovered.get("name") or recovered.get("currentName") or ""
    ).strip().lower()
    original_role = str(original.get("role") or "").strip().lower()
    original_name = str(original.get("name") or "").strip().lower()
    if not new_role or not original_role or new_role != original_role:
        return False
    if original_name and new_name != original_name:
        return False
    return True


def _invalidate_axtree_snapshot(agent: Any, method: str, params: JsonDict) -> None:
    agent.axtree_invalidated = True
    agent.axtree_lines = []
    agent.axtree_nodes = []
    logger = getattr(agent, "logger", None)
    if logger is not None:
        logger.write(
            "axtree.invalidated",
            {
                "method": method,
                "pageId": str(params.get("pageId") or "") if isinstance(params, dict) else "",
                "epoch": int(getattr(agent, "axtree_epoch", 0) or 0),
            },
        )


def _axtree_ids_from_value(value: Any, *, limit: int = 5000) -> Set[str]:
    ids: Set[str] = set()

    def visit(item: Any) -> None:
        if len(ids) >= limit:
            return
        if isinstance(item, dict):
            for key, nested in item.items():
                key_text = str(key)
                if key_text in {"id", "nodeId", "axNodeId", "domNodeId"}:
                    if isinstance(nested, str) and AXTREE_ID_RE.match(nested.strip()):
                        ids.add(nested.strip())
                visit(nested)
        elif isinstance(item, list):
            for nested in item:
                visit(nested)
        elif isinstance(item, str):
            for match in AXTREE_ID_TOKEN_RE.findall(item):
                ids.add(match)
                if len(ids) >= limit:
                    return
            if len(item) < 2000:
                for match in AXTREE_ID_ANYWHERE_RE.findall(item):
                    ids.add(match)
                    if len(ids) >= limit:
                        return

    visit(value)
    return ids
