"""
harness.observation.event_observer - Layer-0 browser event observer.

Subscribes to the ABCP notification stream and turns platform events into
harness-internal state updates. The decisive Phase-3 facts (probe_axtree_events.py,
reports/axtree_events_probe_*.json, confirmed live 2026-06-14):

- Events arrive WRAPPED: {jsonrpc, method:"System.notification",
  params:{type, data:{event, category, severity, payload, suggested_prompt}}}.
  The real event name is at params.data.event; its payload at params.data.payload.
- `DOM.axTreeUpdated` fires "after a target became stale" (the browser-side
  auto-rematch path) and carries the FULL AXTree:
  payload = {pageId, lines, layers, nodeCount, truncated, truncatedReason}
  (same shape as a DOM.getAXTree response). recommendedResponse:
  "Use the supplied lines and layers to continue; do not reuse stale element ids."

Discipline (v4 §6 Layer 0): these updates NEVER enter the model context. They
only refresh agent.axtree_* bookkeeping and write to the run log for audit. The
callback runs inside the websocket read loop, so it must be fast, non-blocking,
and exception-safe — a bad event must never kill the reader.
"""

from __future__ import annotations

from typing import Any, Callable, Optional

from harness.utils import JsonDict


def unwrap_notification(message: Any) -> Optional[JsonDict]:
    """Return the inner event dict {event, payload, ...} from a wrapped
    System.notification, or None if this message is not an event notification."""
    if not isinstance(message, dict):
        return None
    if str(message.get("method") or "") != "System.notification":
        return None
    params = message.get("params")
    if not isinstance(params, dict):
        return None
    data = params.get("data")
    if isinstance(data, dict) and data.get("event"):
        return data
    return None


class BrowserEventObserver:
    """Passive Layer-0 observer bound to one BrowserAgent + ABCP client."""

    def __init__(self, agent: Any):
        self.agent = agent
        self._unsubscribe: Optional[Callable[[], None]] = None
        self.event_counts: dict[str, int] = {}

    def attach(self, client: Any) -> None:
        if self._unsubscribe is not None:
            return
        subscribe = getattr(client, "subscribe_notifications", None)
        if callable(subscribe):
            self._unsubscribe = subscribe(self.handle_message)

    def detach(self) -> None:
        if self._unsubscribe is not None:
            try:
                self._unsubscribe()
            finally:
                self._unsubscribe = None

    # The subscribed callback. Must not raise into the read loop.
    def handle_message(self, message: Any) -> None:
        try:
            event = unwrap_notification(message)
            if event is None:
                return
            name = str(event.get("event") or "")
            self.event_counts[name] = self.event_counts.get(name, 0) + 1
            if name == "DOM.axTreeUpdated":
                self._handle_axtree_updated(event.get("payload"))
        except Exception as exc:  # noqa: BLE001 - never break the reader
            logger = getattr(self.agent, "logger", None)
            if logger is not None:
                logger.write("event_observer.error", {"error": str(exc)[:300]})

    def _handle_axtree_updated(self, payload: Any) -> None:
        """The browser refreshed the AX tree after a stale target. Rebuild the
        agent's id snapshot from the supplied full tree so the next action does
        not need a manual DOM.getAXTree and stale-guard passes against fresh ids.
        Layer 0: state + log only, never model context."""
        agent = self.agent
        if not isinstance(payload, dict):
            self._mark_invalidated("axtree_event_no_payload")
            return
        # Lazy import keeps this observer free of the browser tool dispatcher
        # while still sharing the AXTree parser/history code.
        from harness.tools.browser_tools.axtree_state import (
            _axtree_lines_from_value,
            _axtree_nodes_from_lines,
            _record_axtree_history,
        )

        page_id = str(payload.get("pageId") or "")
        current_page = str(getattr(agent, "axtree_page_id", "") or "")
        # Only refresh the snapshot the agent actually holds. An event for a
        # different page is logged but must not overwrite the active snapshot.
        if page_id and current_page and page_id != current_page:
            self._log("axtree.event.other_page", {"eventPageId": page_id,
                                                   "currentPageId": current_page})
            return

        raw_lines = payload.get("lines")
        lines = raw_lines if isinstance(raw_lines, list) else _axtree_lines_from_value(payload)
        nodes = _axtree_nodes_from_lines(lines)
        ids = {str(n.get("id")) for n in nodes if n.get("id")}
        if not ids:
            self._mark_invalidated("axtree_event_no_ids")
            return

        agent.axtree_epoch = int(getattr(agent, "axtree_epoch", 0) or 0) + 1
        agent.axtree_ids = ids
        agent.axtree_page_id = page_id or current_page
        agent.axtree_invalidated = False
        agent.axtree_lines = list(lines)
        agent.axtree_nodes = nodes
        # Bump a dedicated serial ONLY on a successful same-page apply. An action
        # that triggered this event (Input.click/type on a stale target) will,
        # once its response returns, run the pessimistic per-method invalidation.
        # If the notification reached us first (legal websocket ordering), that
        # invalidation would wipe this fresh snapshot. The action samples this
        # serial before its runner.call and skips invalidation when it advanced.
        # Record the page too, so suppression is page-scoped: an event bootstrapped
        # onto an empty baseline (the cross-page check above only rejects when
        # axtree_page_id is already set) must not suppress a different page's action.
        agent.axtree_event_serial = int(getattr(agent, "axtree_event_serial", 0) or 0) + 1
        agent.axtree_event_page_id = agent.axtree_page_id
        _record_axtree_history(agent, agent.axtree_page_id, nodes, ids)
        self._log("axtree.event.updated", {
            "pageId": agent.axtree_page_id or None,
            "epoch": agent.axtree_epoch,
            "serial": agent.axtree_event_serial,
            "idCount": len(ids),
            "source": "DOM.axTreeUpdated",
        })

    def _mark_invalidated(self, reason: str) -> None:
        # An axtree-update event we can't apply means the cached snapshot is no
        # longer trustworthy; fall back to the pessimistic path.
        self.agent.axtree_invalidated = True
        self._log("axtree.event.invalidated", {"reason": reason})

    def _log(self, event: str, payload: JsonDict) -> None:
        logger = getattr(self.agent, "logger", None)
        if logger is not None:
            logger.write(event, payload)
