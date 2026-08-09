"""
harness.observation.page_inventory - fleet page-inventory change signal.

A worker never sees browser events, so a tab the site opens after a submit is
invisible to it: `Page.getState` on the opener keeps returning the unchanged
page and the model concludes the action failed. Task a294ed5d lost a whole run
to that reading — the results tab was open, correct, and four seconds late.

This is the one Layer-0 exception, and it is deliberately one bit wide. What
reaches the model context is ``pageInventoryChanged: true`` plus an instruction
to call ``Page.list``. No pageId, URL, title, opener, or timing crosses that
line, so nothing here can be mistaken for causal attribution: it says only
"the set of pages in your fleet is not what you last saw".

The pageIds ARE kept harness-side, because the alternative does not work. A
``Page.open`` event for the worker's own ``Page.create`` can arrive BEFORE the
RPC response that names the page, so "is this page mine?" is unanswerable at
event time — ownership is recorded only after the response lands. Remembering
the id and discharging it when the response, a ``Page.list``, or a
``Page.close`` accounts for it is what keeps the worker from being nagged about
tabs it opened itself. Discharge is sticky in both directions: the platform
promises no ordering between the event and the response, and only one of the
two orders has been observed, which is not the same as a guarantee.
"""

from __future__ import annotations

from collections import OrderedDict
from typing import Any, Dict, Iterable, Optional, Set

from harness.utils import JsonDict


DISCHARGED_TOMBSTONE_LIMIT = 512

NEXT_INSTRUCTION = (
    "The set of pages in your fleet changed since you last looked. Call"
    " Page.list once before retrying an action or navigating manually: a"
    " submit or click may have opened its result in a tab you do not hold yet."
)


class PageInventorySignal:
    """Per-worker record of fleet pages the model has not been shown."""

    def __init__(self) -> None:
        self._pending: Dict[str, Set[str]] = {}
        # Pages already accounted for. Discharge has to be STICKY: the platform
        # guarantees no ordering between Page.open and the Page.create response
        # that names the page, and observed runs only prove one of the two
        # orders. If the response lands first, a bare "remove from pending"
        # would let the late event re-arm the signal for the worker's own tab.
        # Bounded FIFO per fleet: a tombstone only has to outlive the window
        # between an event and the response that names the same page, so an
        # unbounded set would be a slow leak for a long-lived worker that opens
        # many tabs.
        self._discharged: Dict[str, "OrderedDict[str, None]"] = {}
        self._generation: int = 0

    @property
    def generation(self) -> int:
        return self._generation

    def observe_opened(self, fleet_id: Any, page_id: Any) -> bool:
        """Record a page that appeared in a fleet. Returns True if it is new."""
        fleet = str(fleet_id or "").strip()
        page = str(page_id or "").strip()
        if not fleet or not page:
            return False
        if page in self._discharged.get(fleet, ()) or page in self._discharged.get("", ()):
            return False
        bucket = self._pending.setdefault(fleet, set())
        if page in bucket:
            return False
        bucket.add(page)
        self._generation += 1
        return True

    def observe_closed(self, fleet_id: Any, page_id: Any) -> None:
        """A closed page is no longer a discovery opportunity."""
        self.discharge([page_id], fleet_id=fleet_id)

    def discharge(
        self,
        page_ids: Iterable[Any],
        *,
        fleet_id: Any = None,
    ) -> None:
        """Account for pages the worker now demonstrably knows about.

        Called when a ``Page.create`` response names the page it made, when a
        ``Page.list`` shows the row to the model, and when a page is closed.
        """
        wanted = {
            str(item).strip()
            for item in (page_ids or ())
            if str(item or "").strip()
        }
        if not wanted:
            return
        fleet = str(fleet_id or "").strip()
        fleets = [fleet] if fleet else list(self._pending)
        if not fleet:
            # Discharging by id alone (a Page.create response names the page but
            # the caller may not pass its fleet) must still be remembered, so
            # keep a fleet-agnostic record too.
            fleets.append("")
        for key in fleets:
            bucket = self._pending.get(key)
            if bucket:
                bucket -= wanted
            tombstones = self._discharged.setdefault(key, OrderedDict())
            for page in wanted:
                tombstones.pop(page, None)
                tombstones[page] = None
            while len(tombstones) > DISCHARGED_TOMBSTONE_LIMIT:
                tombstones.popitem(last=False)

    def pending_for(self, fleet_id: Any) -> Set[str]:
        return set(self._pending.get(str(fleet_id or "").strip()) or set())

    def receipt(
        self,
        fleet_id: Any,
        *,
        is_discoverable: Optional[Any] = None,
    ) -> Optional[JsonDict]:
        """The model-visible bit, or None when there is nothing to report.

        ``is_discoverable`` filters out pages another worker already holds:
        those are not a discovery opportunity for this worker, so nagging it to
        list them would be pure noise. It is applied HERE rather than at event
        time because ownership is not yet recorded when the event arrives.
        """
        pending = self.pending_for(fleet_id)
        if not pending:
            return None
        if callable(is_discoverable):
            pending = {page for page in pending if is_discoverable(page)}
            if not pending:
                return None
        return {
            "pageInventoryChanged": True,
            "next_instruction": NEXT_INSTRUCTION,
        }

    def forget_fleet(self, fleet_id: Any) -> None:
        fleet = str(fleet_id or "").strip()
        self._pending.pop(fleet, None)
        self._discharged.pop(fleet, None)
