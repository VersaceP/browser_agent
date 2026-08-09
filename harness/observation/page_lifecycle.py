"""Event-driven per-page loading and re-perception state.

Notification callbacks call only the synchronous ``observe_event`` method.  It
never performs I/O, which keeps it safe inside ABCPClient's websocket reader.
The browser-tool dispatcher owns all waits and the one-shot Page.getState
fallback.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Dict, Optional

from harness.utils import JsonDict


LOADING_STATUSES = frozenset({"loading", "navigating", "startedloading"})
FAILED_STATUSES = frozenset({"failed", "loadfailed", "error"})


@dataclass
class PageLifecycleState:
    page_id: str
    status: str = "unknown"
    generation: int = 0
    requires_state_resync: bool = False
    requires_ax_refresh: bool = False
    last_event: str = ""
    # asyncio.Event binds to the current loop on Python 3.9. Lifecycle state
    # may be created synchronously during registration/tests, so allocate it
    # lazily inside wait_for_settlement where a running loop is guaranteed.
    settled_event: Optional[asyncio.Event] = None

    def __post_init__(self) -> None:
        if self.status == "settled" and self.settled_event is not None:
            self.settled_event.set()


class PageLifecycleTracker:
    """Small state machine keyed by pageId.

    ``requires_state_resync`` and ``requires_ax_refresh`` deliberately survive a
    Page.loaded event.  Loading settlement and DOM re-perception are separate
    obligations: after navigate/recovered the page must settle, then Page.getState
    and DOM.getAXTree must refresh the model-visible handles.
    """

    def __init__(self) -> None:
        self._pages: Dict[str, PageLifecycleState] = {}

    def state(self, page_id: Any) -> Optional[PageLifecycleState]:
        key = str(page_id or "").strip()
        return self._pages.get(key) if key else None

    def ensure(self, page_id: Any) -> Optional[PageLifecycleState]:
        key = str(page_id or "").strip()
        if not key:
            return None
        state = self._pages.get(key)
        if state is None:
            state = PageLifecycleState(page_id=key)
            self._pages[key] = state
        return state

    def before_action(self, method: str, page_id: Any) -> None:
        state = self.ensure(page_id)
        if state is None:
            return
        if method in {"Page.navigate", "Page.reload", "Page.go"}:
            self._mark_loading(state, method)
            state.requires_state_resync = True
            state.requires_ax_refresh = True
        elif method in {"File.download", "Download.cancel", "Download.pause", "Download.resume"}:
            state.requires_state_resync = True

    def observe_event(self, name: str, payload: Any) -> Optional[PageLifecycleState]:
        payload = payload if isinstance(payload, dict) else {}
        page_id = str(
            payload.get("pageId")
            or payload.get("pageID")
            or payload.get("targetPageId")
            or ""
        ).strip()
        state = self.ensure(page_id)
        if state is None:
            return None
        state.last_event = str(name or "")
        if name == "Page.startedLoading":
            self._mark_loading(state, name)
        elif name == "Page.loaded":
            state.status = "settled"
            self._set_settled_event(state)
        elif name == "Page.loadFailed":
            state.status = "failed"
            self._set_settled_event(state)
        elif name == "Page.crashed":
            state.status = "crashed"
            state.requires_state_resync = True
            state.requires_ax_refresh = True
            self._set_settled_event(state)
        elif name in {"Page.navigate", "Page.recovered"}:
            state.generation += 1
            state.status = "loading" if name == "Page.navigate" else "unknown"
            state.requires_state_resync = True
            state.requires_ax_refresh = True
            self._clear_settled_event(state)
        elif name in {"Page.dialogClosed", "File.chooserClosed"}:
            state.status = "unknown"
            state.requires_state_resync = True
            self._clear_settled_event(state)
        return state

    def observe_state_response(self, page_id: Any, response: Any) -> None:
        state = self.ensure(page_id)
        if state is None:
            return
        body = response if isinstance(response, dict) else {}
        nested = body.get("response") if isinstance(body.get("response"), dict) else body
        data = nested.get("data") if isinstance(nested.get("data"), dict) else None
        if (
            not isinstance(data, dict)
            or not data
            or bool(body.get("error"))
            or bool(nested.get("error"))
            or body.get("tool_was_executed") is False
            or nested.get("tool_was_executed") is False
        ):
            # A render-recovery advisory and any malformed/failed getState
            # response must never discharge the resynchronization obligation.
            state.status = "failed"
            state.requires_state_resync = True
            state.last_event = "Page.getState.failed"
            self._clear_settled_event(state)
            return
        status = str(data.get("status") or "").strip().lower().replace("_", "")
        state.requires_state_resync = False
        if status in LOADING_STATUSES:
            state.status = "loading"
            self._clear_settled_event(state)
        elif status in FAILED_STATUSES:
            state.status = "failed"
            self._set_settled_event(state)
        elif status == "crashed":
            state.status = "crashed"
            state.requires_ax_refresh = True
            self._set_settled_event(state)
        else:
            # Page.getState is the documented one-shot resynchronization when a
            # settlement event was missed.  A successful response without an
            # explicit loading status is treated as the synchronized snapshot.
            state.status = "settled"
            self._set_settled_event(state)

    def observe_ax_refresh(self, page_id: Any) -> None:
        state = self.ensure(page_id)
        if state is not None:
            state.requires_ax_refresh = False

    def invalidate_ax_refresh(self, page_id: Any) -> None:
        """Restore the AX refresh obligation after a stale result was isolated.

        A successful DOM.getAXTree response normally clears the obligation in
        ``observe_ax_refresh``. Callers that subsequently prove that response
        belonged to an older navigation generation must be able to roll back
        that optimistic transition without manufacturing another navigation.
        """
        state = self.ensure(page_id)
        if state is not None:
            state.requires_ax_refresh = True

    async def wait_for_settlement(self, page_id: Any, timeout_seconds: float) -> str:
        state = self.ensure(page_id)
        if state is None:
            return "unknown"
        if state.status != "loading":
            return state.status
        if state.settled_event is None:
            state.settled_event = asyncio.Event()
        try:
            await asyncio.wait_for(
                state.settled_event.wait(), timeout=max(0.0, float(timeout_seconds))
            )
        except asyncio.TimeoutError:
            return "timeout"
        return state.status

    def receipt(self, page_id: Any) -> JsonDict:
        state = self.state(page_id)
        if state is None:
            return {"pageId": str(page_id or "") or None, "status": "unknown"}
        return {
            "pageId": state.page_id,
            "status": state.status,
            "generation": state.generation,
            "lastEvent": state.last_event or None,
            "requiresStateResync": state.requires_state_resync,
            "requiresAXTreeRefresh": state.requires_ax_refresh,
        }

    @staticmethod
    def _mark_loading(state: PageLifecycleState, event: str) -> None:
        state.generation += 1
        state.status = "loading"
        state.last_event = event
        PageLifecycleTracker._clear_settled_event(state)

    @staticmethod
    def _set_settled_event(state: PageLifecycleState) -> None:
        if state.settled_event is not None:
            state.settled_event.set()

    @staticmethod
    def _clear_settled_event(state: PageLifecycleState) -> None:
        if state.settled_event is not None:
            state.settled_event.clear()
