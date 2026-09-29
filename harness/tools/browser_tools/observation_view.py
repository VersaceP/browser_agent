"""Model-facing page-observation views and the node-watch tools.

Two worker-facing behaviours sit on the process observation channel
(harness.observation.observation_channel):

* After a worker has received one complete DOM.getAXTree view of a page, a
  later read shows only the changes since the version that worker holds. The
  full view still goes to disk and to find_in_axtree; only what enters the
  model context shrinks. Any break in the version chain shows the full view.

* await_node_change: one call registers a watch on named nodes, blocks until
  they change, and closes itself. A poller probes those nodes while the watch
  is active and reads the whole page only when the probe moves. In background
  mode the call returns at once and changes ride the worker's later tool
  results as `watchEvents`; the harness also closes watches on navigation,
  page loss, expiry, and when the worker finishes.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Dict, List, Optional

from harness.context.offload import page_view_blob_path, write_offloaded_blob
from harness.observation.axtree_format import AX_NODE_ID_RE
from harness.observation.observation_channel import (
    DEFAULT_WATCH_TTL_SECONDS,
    MAX_NODES_PER_WATCH,
    observation_channel,
)
from harness.utils import JsonDict, task_subdir


WAIT_DEFAULT_SECONDS = 15.0
WAIT_MAX_SECONDS = 120.0


def _consumer_id(agent: Any) -> str:
    runtime = getattr(agent, "runtime", None)
    agent_id = str(getattr(runtime, "agent_id", "") or "")
    return agent_id or f"agent-{id(agent)}"


def _harness_config(agent: Any) -> Any:
    return getattr(getattr(agent, "runtime", None), "harness", None)


def _diff_view_enabled(agent: Any) -> bool:
    return bool(getattr(_harness_config(agent), "observation_diff_view", True))


def _store_full_view_quietly(agent: Any, params: JsonDict, lines: List[str], step: int) -> JsonDict:
    """Persist the complete view without putting any of it in the model context."""
    logger = getattr(agent, "logger", None)
    if logger is None:
        return {"lineCount": len(lines)}
    path = page_view_blob_path(
        task_subdir(logger, "observations"),
        _consumer_id(agent),
        str(params.get("pageId") or "no-page"),
    )
    _format, query_with, _outline, facts = write_offloaded_blob(logger, path, "lines", lines)
    return {
        "_offloaded": True,
        "format": "text_lines",
        "query_with": query_with,
        **{key: facts[key] for key in ("savedPath", "lineCount", "sameContentAs") if key in facts},
    }


def _full_view_evidence(method: str, response: Any) -> Optional[JsonDict]:
    """The full view as received, kept before the model projection replaces `lines`."""
    if method != "DOM.getAXTree" or not isinstance(response, dict):
        return None
    data = response.get("data")
    if not isinstance(data, dict) or not isinstance(data.get("lines"), list):
        return None
    return {**response, "data": dict(data)}


def _project_observation_for_model(
    agent: Any,
    method: str,
    params: JsonDict,
    response: Any,
    step: int,
) -> Any:
    """Show a worker the changes since its last view instead of the page again."""
    if method != "DOM.getAXTree" or not isinstance(response, dict):
        return response
    data = response.get("data")
    if not isinstance(data, dict) or data.get("mode") != "full" or not isinstance(data.get("lines"), list):
        return response
    diff = data.get("diff")
    if isinstance(diff, dict):
        # Raw platform records are for the harness; the model gets the
        # rendered view below, or the full view.
        diff.pop("records", None)
    if not _diff_view_enabled(agent):
        return response
    page_id = str(params.get("pageId") or data.get("pageId") or "")
    view = observation_channel().model_view(_consumer_id(agent), page_id, data)
    delivery = view.get("delivery")
    version = (data.get("observation") or {}).get("version")
    if delivery == "changes":
        data["lines"] = {
            **_store_full_view_quietly(agent, params, data["lines"], step),
            "delivery": "changes",
            "note": (
                "Only the changes since the version you already hold are shown"
                " in `changes`; the complete current view is on disk and in"
                " find_in_axtree."
            ),
        }
        data["changes"] = view["changes"]
        data["changesSince"] = view["since"]
        if view.get("folded"):
            data["changesFolded"] = view["folded"]
    elif delivery == "unchanged":
        data["lines"] = {
            "delivery": "unchanged",
            "version": version,
            "note": (
                "The page view is identical to the version you already hold."
                " This does not show that an earlier action succeeded; query"
                " the specific values still unresolved instead of rereading"
                " the full view."
            ),
        }
    else:
        data["observationView"] = {
            "delivery": "full",
            **({"reason": view["reason"]} if view.get("reason") else {}),
        }
    return response


# ---------------------------------------------------------------------------
# Watch tools
# ---------------------------------------------------------------------------

def _tool_in_flight(agent: Any) -> bool:
    return int(getattr(agent, "_observation_tool_depth", 0) or 0) > 0


def _audit_watch_evidence(evidence: Any) -> JsonDict:
    """Persist probe identity and digests without a second page-text copy."""
    audit = dict(evidence) if isinstance(evidence, dict) else {}
    audit["lastSamples"] = {
        key: {field: value for field, value in sample.items() if field != "value"}
        for key, sample in (audit.get("lastSamples") or {}).items()
        if isinstance(sample, dict)
    }
    return audit


def _make_reader(agent: Any, page_id: str, node_ids: List[str], scope: str):
    """Probe exact text plus structure/state, with occasional full reads.

    A watch used to re-read the whole page every interval: on a 400KB page a
    30-second wait cost twenty full captures. The watch names at most sixteen
    nodes, which a bounded query answers. Query values themselves generate
    comparable watch events; a continuing probe change also refreshes the
    full AX view for the model's page-version chain.
    """
    # Rendered text can change without a corresponding full AX label change.
    # Alternate it with the original structural/state probe so existing watch
    # coverage remains available at the same request rate.
    preferred = observation_channel().preferred_probe_surface(page_id, node_ids, scope)
    probe_number = 0 if preferred == "text:rendered" else 1
    probe_count = 0
    last_digest: Dict[str, str] = {}

    async def read() -> bool:
        nonlocal probe_number, probe_count
        browser = getattr(agent, "browser", None)
        if browser is None:
            observation_channel().record_probe_error(page_id, node_ids)
            return False

        async def full_read() -> bool:
            # Straight to the client: the hydrated view is recorded on the
            # channel there, and the worker's own AX snapshot is deliberately
            # left alone - a poll racing the worker's action must not
            # re-validate a snapshot that action invalidated.
            try:
                response = await browser.call("DOM.getAXTree", {
                    "pageId": page_id,
                    "purpose": "watch: re-read the page to check watched nodes",
                })
            except Exception:  # noqa: BLE001 - the poller records availability failures
                observation_channel().record_probe_error(page_id, node_ids)
                return False
            data = response.get("data") if isinstance(response, dict) else None
            ok = isinstance(data, dict) and isinstance(data.get("lines"), list)
            if not ok:
                observation_channel().record_probe_error(page_id, node_ids)
            return ok

        text_probe = probe_number % 2 == 0
        probe_number += 1
        probe_count += 1
        surface = "text:rendered" if text_probe else "dom:1" if scope == "subtree" else "state"
        probe_query = {
            "targets": [{"id": node_id} for node_id in node_ids],
            "view": "text" if text_probe else "dom" if scope == "subtree" else "state",
            **({"textMode": "rendered"} if text_probe else {}),
            **({"maxDepth": 1} if not text_probe and scope == "subtree" else {}),
        }

        try:
            probe = await browser.call("DOM.getAXTree", {
                "pageId": page_id,
                "purpose": "watch: probe the watched nodes for a change",
                "query": probe_query,
            })
        except Exception:  # noqa: BLE001 - fall back to the full read
            observation_channel().record_probe_error(page_id, node_ids)
            return await full_read()
        data = probe.get("data") if isinstance(probe, dict) else None
        records = data.get("records") if isinstance(data, dict) else None
        if not isinstance(records, list):
            observation_channel().record_probe_error(page_id, node_ids)
            return await full_read()
        relevant = []
        for item in records:
            if not isinstance(item, dict):
                continue
            target = item.get("target") if isinstance(item.get("target"), dict) else {}
            relevant.append({"ok": item.get("ok"),
                             "target": target.get("resolvedId") or target.get("requestedId"),
                             "value": item.get("value"), "error": item.get("error")})
        digest = hashlib.sha256(
            json.dumps(relevant, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
        ).hexdigest()
        # A bounded subtree query can omit deeper descendants. Refresh the
        # full AX view at a low fixed cadence even if the query stays stable.
        periodic_full_read = probe_count % 8 == 0
        if last_digest.get(surface) == digest:
            return await full_read() if periodic_full_read else True
        first_probe = surface not in last_digest
        last_digest[surface] = digest
        # The observation channel has already compared this exact query against
        # the pre-watch query, or reported an explicit current sample when the
        # surfaces had no compatible prior value.
        return await full_read() if periodic_full_read or not first_probe else True

    return read


async def _resolve_watch_targets(agent: Any, page_id: str, selector: str) -> JsonDict:
    """Turn a selector into node ids with one platform query, not a model turn."""
    browser = getattr(agent, "browser", None)
    if browser is None:
        return {"ids": [], "reason": "no_browser"}
    try:
        response = await browser.call("DOM.getAXTree", {
            "pageId": page_id,
            "purpose": "await_node_change: resolve the watched selector",
            "query": {
                "targets": [{"selector": selector}],
                "view": "dom",
                "maxDepth": 0,
            },
        })
    except Exception:  # noqa: BLE001 - an unresolvable selector is a tool answer
        return {"ids": [], "reason": "selector_query_failed"}
    data = response.get("data") if isinstance(response, dict) else None
    records = data.get("records") if isinstance(data, dict) else None
    ids: List[str] = []
    for record in records or []:
        if not isinstance(record, dict) or not record.get("ok"):
            continue
        target = record.get("target") if isinstance(record.get("target"), dict) else {}
        node_id = str(target.get("resolvedId") or "")
        if AX_NODE_ID_RE.match(node_id):
            ids.append(node_id)
    return {"ids": ids[:MAX_NODES_PER_WATCH], "reason": "" if ids else "selector_matched_nothing"}


async def _await_node_change(agent: Any, tool_input: JsonDict) -> JsonDict:
    """Register, wait and close in one call - see the tool description."""
    page_id = str(tool_input.get("pageId") or "").strip()
    watch_id = str(tool_input.get("watchId") or "").strip() or None

    if bool(tool_input.get("close")):
        if not watch_id and not page_id:
            return {
                "status": "error",
                "reason": "no_watch_selected",
                "next_instruction": "Pass watchId or pageId to close a background watch.",
            }
        closed = observation_channel().close_watches(
            _consumer_id(agent), watch_id=watch_id, page_id=page_id or None,
        )
        _log(agent, "observation.watch_closed", {
            "watchIds": [item.get("watchId") for item in closed],
            "reason": "closed_by_agent",
        })
        return {"status": "closed" if closed else "no_matching_watch", "closed": closed}

    raw_ids = tool_input.get("nodeIds")
    node_ids = [str(item).strip() for item in raw_ids] if isinstance(raw_ids, list) else []
    selector = str(tool_input.get("selector") or "").strip()
    resolved_from = "ids"
    if not node_ids and selector and page_id:
        resolution = await _resolve_watch_targets(agent, page_id, selector)
        node_ids = list(resolution["ids"])
        resolved_from = "selector"
        if not node_ids:
            return {
                "status": "error",
                "reason": resolution["reason"],
                "selector": selector,
                "next_instruction": (
                    "No current node matches that selector. Read the page or"
                    " pass node ids from your latest view."
                ),
            }
    bad = [node_id for node_id in node_ids if not AX_NODE_ID_RE.match(node_id)]
    if not page_id or not node_ids or bad:
        return {
            "status": "error",
            "reason": "invalid_watch_request",
            **({"invalidIds": bad} if bad else {}),
            "next_instruction": (
                f"Pass pageId with a selector, or 1-{MAX_NODES_PER_WATCH} node ids"
                " from the latest view of that page."
            ),
        }

    background = bool(tool_input.get("background"))
    timeout = tool_input.get("timeoutSeconds")
    timeout_seconds = (
        max(0.0, min(WAIT_MAX_SECONDS, float(timeout)))
        if isinstance(timeout, (int, float)) else WAIT_DEFAULT_SECONDS
    )
    config = _harness_config(agent)
    max_ttl = float(getattr(config, "observation_watch_max_ttl_seconds", 600) or 600)
    # A blocking call outlives its own wait by nothing: the ttl only has to
    # cover the wait itself, and the call closes the watch on its way out.
    ttl_seconds = (
        min(max_ttl, float(DEFAULT_WATCH_TTL_SECONDS)) if background
        else max(10.0, min(max_ttl, timeout_seconds + 10.0))
    )
    interval = float(getattr(config, "observation_watch_poll_interval_ms", 1500) or 1500) / 1000.0
    scope = "subtree" if tool_input.get("scope") == "subtree" else "node"
    opened = observation_channel().open_watch(
        consumer=_consumer_id(agent),
        page_id=page_id,
        node_ids=node_ids,
        scope=scope,
        ttl_seconds=ttl_seconds,
        read=_make_reader(agent, page_id, node_ids, scope),
        busy=lambda: _tool_in_flight(agent),
        poll_interval_seconds=interval,
    )
    _log(agent, "observation.watch_opened", {
        "pageId": page_id, "resolvedFrom": resolved_from, "background": background,
        **{key: opened.get(key) for key in ("status", "watchId", "reason") if opened.get(key)},
    })
    if opened.get("status") != "active":
        return opened

    opened_id = str(opened.get("watchId") or "")
    if background:
        opened["resolvedFrom"] = resolved_from
        opened["watchedIds"] = node_ids
        opened["next_instruction"] = (
            "Changes arrive as `watchEvents` on your later tool results. Close"
            " it with close=true and this watchId once you no longer need it."
        )
        return opened

    result = await observation_channel().wait(
        _consumer_id(agent), opened_id, timeout_seconds,
    )
    current_watch = next((watch for watch in observation_channel().active_watches(_consumer_id(agent))
                          if watch.watch_id == opened_id), None)
    if current_watch is not None:
        result["observationEvidence"] = {
            key: current_watch.describe()[key]
            for key in ("sampleCount", "firstSampleAt", "lastSampleAt", "lastSamples", "probeErrorCount")
        }
    observation_channel().close_watches(_consumer_id(agent), watch_id=opened_id)
    _log(agent, "observation.watch_wait", {
        "watchId": opened_id, "status": result.get("status"),
        "eventCount": len(result.get("events") or []),
        "evidence": _audit_watch_evidence(result.get("observationEvidence")),
    })
    result["watchId"] = opened_id
    result["watchedIds"] = node_ids
    result["resolvedFrom"] = resolved_from
    result["closed"] = True
    if result.get("status") == "timeout":
        result["next_instruction"] = (
            f"No change was observed on these query surfaces in {timeout_seconds:g}s."
            " This does not prove the page stayed unchanged or that an action"
            " failed. Inspect the last samples and verify the relevant current"
            " value or request outcome before deciding what to do next."
        )
    return result


def _attach_watch_events(agent: Any, result: Any, tool_name: str) -> None:
    """Deliver queued watch events on a worker's tool result."""
    if not isinstance(result, dict) or tool_name == "await_node_change":
        return
    events = observation_channel().drain_events(_consumer_id(agent))
    if events:
        result["watchEvents"] = events
        _log(agent, "observation.watch_events_delivered", {
            "tool": tool_name, "eventCount": len(events),
        })


def _close_agent_watches(agent: Any) -> None:
    observation_channel().close_consumer(_consumer_id(agent))


def _log(agent: Any, event: str, payload: JsonDict) -> None:
    logger = getattr(agent, "logger", None)
    if logger is not None and hasattr(logger, "write"):
        logger.write(event, payload)
