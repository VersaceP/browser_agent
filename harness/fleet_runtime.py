"""Shared runtime guards for multi-worker fleet reuse.

The Dispatcher routes browser actions by ``pageId``.  The harness therefore
gives each live worker persistent ownership of the pages it uses while allowing
different pages in one fleet to make progress concurrently. Opaque Workflow is
Fleet-exclusive because nested steps may switch to a newly opened page without
re-entering the Harness. Authentication/challenge state is also fleet-wide, so
``FleetAuthBarrier`` gates the whole fleet until one resolver has completed
verified HITL recovery.
"""

from __future__ import annotations

import asyncio
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import (
    Any,
    AsyncIterator,
    Awaitable,
    Callable,
    Dict,
    Iterable,
    Optional,
)

from abcp_client import ABCPTransportError


class PageLeaseManager:
    """Process-local page ownership plus opaque-Workflow Fleet exclusion.

    A page lease belongs to a worker until that worker terminates; it is not a
    per-call mutex.  This is the only way to prevent two workers from
    alternating DOM reads and actions on one page while still allowing
    different pages in the same Fleet to make progress concurrently.

    ``Workflow.execute`` is opaque to the Harness and may open and continue on
    a new page between nested steps.  It therefore holds a short-lived Fleet
    interaction lease for the duration of the whole call.  Ordinary page calls
    wait outside that Fleet while the Workflow is active.
    """

    def __init__(self, *, wait_timeout_seconds: float = 30.0) -> None:
        self._page_owners: Dict[str, str] = {}
        self._page_fleets: Dict[str, str] = {}
        self._quarantined_pages: set[str] = set()
        self._worker_pages: Dict[str, set[str]] = {}
        self._fleet_active_calls: Dict[
            str, Dict[asyncio.Task[Any], tuple[str, int]]
        ] = {}
        self._fleet_workflow_owner: Dict[
            str, tuple[str, asyncio.Task[Any]]
        ] = {}
        self._fleet_creation_owner: Dict[
            str, tuple[str, asyncio.Task[Any]]
        ] = {}
        self._fleet_workflow_waiters: Dict[
            str, Dict[asyncio.Task[Any], str]
        ] = {}
        self._fleet_changed: Dict[str, asyncio.Event] = {}
        self.wait_timeout_seconds = max(0.01, float(wait_timeout_seconds))
        self._legacy_locks: Dict[str, asyncio.Lock] = {}
        self._legacy_lock_users: Dict[str, int] = {}

    def _signal(self, fleet_id: str) -> None:
        fleet = str(fleet_id or "").strip()
        previous = self._fleet_changed.pop(fleet, None)
        if previous is None:
            return
        previous.set()

    async def _wait_for_change(self, fleet_id: str, deadline: float) -> None:
        fleet = str(fleet_id or "").strip()
        changed = self._fleet_changed.setdefault(fleet, asyncio.Event())
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise asyncio.TimeoutError
        await asyncio.wait_for(changed.wait(), timeout=remaining)

    def observe_inventory(self, fleet_id: str, page_ids: Iterable[str]) -> None:
        fleet = str(fleet_id or "").strip()
        if not fleet:
            return
        for raw in page_ids:
            page_id = str(raw or "").strip()
            if page_id:
                self._page_fleets[page_id] = fleet

    def owner_for(self, page_id: str) -> str:
        return str(self._page_owners.get(str(page_id or "").strip()) or "")

    def fleet_for(self, page_id: str) -> str:
        return str(self._page_fleets.get(str(page_id or "").strip()) or "")

    def page_fleets_for_worker(self, worker_id: str) -> Dict[str, str]:
        """Return a snapshot of usable pages authoritatively owned by a worker."""

        worker = str(worker_id or "").strip()
        if not worker:
            return {}
        return {
            page_id: self._page_fleets.get(page_id, "")
            for page_id in self._worker_pages.get(worker, set())
            if (
                page_id not in self._quarantined_pages
                and self._page_owners.get(page_id) == worker
                and self._page_fleets.get(page_id, "")
            )
        }

    def page_is_quarantined(self, page_id: str) -> bool:
        return str(page_id or "").strip() in self._quarantined_pages

    def quarantine_page(self, page_id: str) -> None:
        page = str(page_id or "").strip()
        if page:
            self._quarantined_pages.add(page)

    def clear_page_quarantine(self, page_id: str) -> None:
        self._quarantined_pages.discard(str(page_id or "").strip())

    def seed_worker_pages(
        self,
        worker_id: str,
        page_fleets: Dict[str, str],
    ) -> None:
        """Register coordinator-delegated pages before a worker starts."""

        worker = str(worker_id or "").strip()
        if not worker:
            return
        normalized: Dict[str, str] = {}
        for raw_page_id, raw_fleet_id in (page_fleets or {}).items():
            page_id = str(raw_page_id or "").strip()
            fleet_id = str(raw_fleet_id or "").strip()
            if not page_id:
                continue
            normalized[page_id] = fleet_id
            if page_id in self._quarantined_pages:
                raise PageLeaseConflict(
                    page_id=page_id,
                    worker_id=worker,
                    owner_worker_id="",
                    reason="delegated_page_quarantined",
                )
            owner = self._page_owners.get(page_id)
            if owner and owner != worker:
                raise PageLeaseConflict(
                    page_id=page_id,
                    worker_id=worker,
                    owner_worker_id=owner,
                    reason="delegated_page_held_by_other_worker",
                )
        for page_id, fleet_id in normalized.items():
            self._page_owners[page_id] = worker
            self._worker_pages.setdefault(worker, set()).add(page_id)
            if fleet_id:
                self._page_fleets[page_id] = fleet_id

    def release_worker(self, worker_id: str) -> None:
        """Release every persistent page/Fleet lease held by one worker."""

        worker = str(worker_id or "").strip()
        changed = False
        for page_id in self._worker_pages.pop(worker, set()):
            if self._page_owners.get(page_id) == worker:
                self._page_owners.pop(page_id, None)
                changed = True
        fleets_changed: set[str] = set()
        for fleet_id, owner in list(self._fleet_workflow_owner.items()):
            if owner[0] == worker:
                self._fleet_workflow_owner.pop(fleet_id, None)
                changed = True
                fleets_changed.add(fleet_id)
        for fleet_id, owner in list(self._fleet_creation_owner.items()):
            if owner[0] == worker:
                self._fleet_creation_owner.pop(fleet_id, None)
                changed = True
                fleets_changed.add(fleet_id)
        for fleet_id, calls in list(self._fleet_active_calls.items()):
            for task, (owner, _count) in list(calls.items()):
                if owner == worker:
                    calls.pop(task, None)
                    changed = True
                    fleets_changed.add(fleet_id)
            if not calls:
                self._fleet_active_calls.pop(fleet_id, None)
        for fleet_id, waiters in list(self._fleet_workflow_waiters.items()):
            for task, owner in list(waiters.items()):
                if owner == worker:
                    waiters.pop(task, None)
                    changed = True
                    fleets_changed.add(fleet_id)
            if not waiters:
                self._fleet_workflow_waiters.pop(fleet_id, None)
        if changed:
            for fleet_id in fleets_changed:
                self._signal(fleet_id)

    def release_page(self, page_id: str, worker_id: str) -> None:
        page = str(page_id or "").strip()
        worker = str(worker_id or "").strip()
        if not page or self._page_owners.get(page) != worker:
            return
        self._page_owners.pop(page, None)
        pages = self._worker_pages.get(worker)
        if pages is not None:
            pages.discard(page)
            if not pages:
                self._worker_pages.pop(worker, None)
        fleet_id = self._page_fleets.get(page, "")
        self._signal(fleet_id)

    def forget_page(self, page_id: str, worker_id: str = "") -> None:
        """Forget a page proven closed/crashed, including its inventory row."""

        page = str(page_id or "").strip()
        worker = str(worker_id or "").strip()
        if not page:
            return
        owner = self._page_owners.get(page, "")
        if worker and owner and owner != worker:
            return
        fleet_id = self._page_fleets.pop(page, "")
        self._quarantined_pages.discard(page)
        self._page_owners.pop(page, None)
        if owner:
            pages = self._worker_pages.get(owner)
            if pages is not None:
                pages.discard(page)
                if not pages:
                    self._worker_pages.pop(owner, None)
        self._signal(fleet_id)

    def claim_created_pages(
        self,
        *,
        worker_id: str,
        fleet_id: str,
        page_ids: Iterable[str],
    ) -> None:
        """Synchronously adopt Page.create results before releasing Fleet access."""

        fleet = str(fleet_id or "").strip()
        pages = tuple(
            str(page_id or "").strip()
            for page_id in page_ids
            if str(page_id or "").strip()
        )
        if not fleet:
            raise PageLeaseConflict(
                page_id=pages[0] if pages else "",
                worker_id=worker_id,
                owner_worker_id="",
                reason="fleet_required_for_page_create",
            )
        self.observe_inventory(fleet, pages)
        self._validate_and_claim_pages(
            worker_id=str(worker_id or "").strip(),
            fleet_id=fleet,
            page_ids=pages,
        )

    def _resolve_fleet(self, fleet_id: str, page_ids: Iterable[str]) -> str:
        fleet = str(fleet_id or "").strip()
        if fleet:
            return fleet
        known = {
            self._page_fleets.get(str(page_id or "").strip(), "")
            for page_id in page_ids
        }
        known.discard("")
        if len(known) == 1:
            return next(iter(known))
        return ""

    def _validate_and_claim_pages(
        self,
        *,
        worker_id: str,
        fleet_id: str,
        page_ids: Iterable[str],
    ) -> None:
        pages = {str(item or "").strip() for item in page_ids}
        pages.discard("")
        for page_id in pages:
            if page_id in self._quarantined_pages:
                raise PageLeaseConflict(
                    page_id=page_id,
                    worker_id=worker_id,
                    owner_worker_id="",
                    reason="page_quarantined",
                )
            known_fleet = self._page_fleets.get(page_id, "")
            if known_fleet and fleet_id and known_fleet != fleet_id:
                raise PageLeaseConflict(
                    page_id=page_id,
                    worker_id=worker_id,
                    owner_worker_id="",
                    reason="page_outside_assigned_fleet",
                )
            owner = self._page_owners.get(page_id, "")
            if owner and owner != worker_id:
                raise PageLeaseConflict(
                    page_id=page_id,
                    worker_id=worker_id,
                    owner_worker_id=owner,
                    reason="page_held_by_other_worker",
                )
        for page_id in pages:
            if not self._page_owners.get(page_id):
                self._page_owners[page_id] = worker_id
                self._worker_pages.setdefault(worker_id, set()).add(page_id)
            if fleet_id and not self._page_fleets.get(page_id):
                self._page_fleets[page_id] = fleet_id

    @asynccontextmanager
    async def interaction(
        self,
        *,
        page_ids: Iterable[str],
        fleet_id: str,
        worker_id: str,
        workflow: bool = False,
        fleet_scoped: bool = False,
    ) -> AsyncIterator[None]:
        """Atomically claim pages and enter one page/Fleet interaction."""

        worker = str(worker_id or "").strip()
        pages = tuple({
            str(item or "").strip()
            for item in page_ids
            if str(item or "").strip()
        })
        fleet = self._resolve_fleet(fleet_id, pages)
        if not worker:
            # Lightweight/direct clients without a worker identity retain the
            # legacy per-call mutex; production workers always have an identity
            # and therefore use persistent ownership below.
            locks: list[tuple[str, asyncio.Lock]] = []
            acquired_locks: list[asyncio.Lock] = []
            for page_id in sorted(pages):
                lock = self._legacy_locks.setdefault(page_id, asyncio.Lock())
                self._legacy_lock_users[page_id] = (
                    self._legacy_lock_users.get(page_id, 0) + 1
                )
                locks.append((page_id, lock))
            try:
                for _page_id, lock in locks:
                    await lock.acquire()
                    acquired_locks.append(lock)
                yield
            finally:
                for lock in reversed(acquired_locks):
                    lock.release()
                for page_id, lock in locks:
                    users = self._legacy_lock_users.get(page_id, 0) - 1
                    if users > 0:
                        self._legacy_lock_users[page_id] = users
                    else:
                        self._legacy_lock_users.pop(page_id, None)
                        if self._legacy_locks.get(page_id) is lock:
                            self._legacy_locks.pop(page_id, None)
            return

        if not fleet and (workflow or fleet_scoped or pages):
            raise PageLeaseConflict(
                page_id=pages[0] if pages else "",
                worker_id=worker,
                owner_worker_id="",
                reason="fleet_identity_required",
            )

        task = asyncio.current_task()
        if task is None:
            raise RuntimeError("page lease interaction requires an asyncio task")
        deadline = time.monotonic() + self.wait_timeout_seconds

        if workflow:
            self._validate_and_claim_pages(
                worker_id=worker, fleet_id=fleet, page_ids=pages
            )
            waiters = self._fleet_workflow_waiters.setdefault(fleet, {})
            waiters[task] = worker
            acquired = False
            try:
                while True:
                    owner = self._fleet_workflow_owner.get(fleet)
                    active = self._fleet_active_calls.get(fleet, {})
                    if owner is None and not active:
                        self._fleet_workflow_owner[fleet] = (worker, task)
                        waiters.pop(task, None)
                        if not waiters:
                            self._fleet_workflow_waiters.pop(fleet, None)
                        acquired = True
                        break
                    try:
                        await self._wait_for_change(fleet, deadline)
                    except asyncio.TimeoutError as exc:
                        raise FleetInteractionTimeout(
                            fleet_id=fleet,
                            worker_id=worker,
                            method_kind="workflow",
                        ) from exc
            finally:
                pending = self._fleet_workflow_waiters.get(fleet)
                if pending is not None:
                    pending.pop(task, None)
                    if not pending:
                        self._fleet_workflow_waiters.pop(fleet, None)
                self._signal(fleet)
            try:
                yield
            finally:
                if (
                    acquired
                    and self._fleet_workflow_owner.get(fleet) == (worker, task)
                ):
                    self._fleet_workflow_owner.pop(fleet, None)
                    self._signal(fleet)
            return

        while True:
            workflow_owner = self._fleet_workflow_owner.get(fleet)
            workflow_waiting = self._fleet_workflow_waiters.get(fleet, {})
            creation_owner = self._fleet_creation_owner.get(fleet)
            # Workflow re-entry is scoped to its execution context, not merely
            # worker_id: two parallel Workflows from one worker must remain
            # mutually exclusive. The current HITL settlement chain directly
            # awaits its verification calls, so it retains this task identity.
            owner_is_current_task = workflow_owner == (worker, task)
            creation_is_current_task = creation_owner == (worker, task)
            creation_available = creation_owner is None or creation_is_current_task
            if creation_available and (owner_is_current_task or (
                workflow_owner is None and not workflow_waiting
            )):
                if fleet_scoped and creation_owner is None:
                    self._fleet_creation_owner[fleet] = (worker, task)
                self._validate_and_claim_pages(
                    worker_id=worker, fleet_id=fleet, page_ids=pages
                )
                active = self._fleet_active_calls.setdefault(fleet, {})
                active_worker, count = active.get(task, (worker, 0))
                active[task] = (active_worker, count + 1)
                break
            try:
                await self._wait_for_change(fleet, deadline)
            except asyncio.TimeoutError as exc:
                raise FleetInteractionTimeout(
                    fleet_id=fleet,
                    worker_id=worker,
                    method_kind="page_call" if pages else "fleet_call",
                ) from exc
        try:
            yield
        finally:
            active = self._fleet_active_calls.get(fleet)
            if active is not None and task in active:
                active_worker, count = active[task]
                if count > 1:
                    active[task] = (active_worker, count - 1)
                else:
                    active.pop(task, None)
                if not active:
                    self._fleet_active_calls.pop(fleet, None)
            if (
                fleet_scoped
                and self._fleet_creation_owner.get(fleet) == (worker, task)
            ):
                self._fleet_creation_owner.pop(fleet, None)
            self._signal(fleet)


class PageLeaseConflict(ABCPTransportError):
    """A page is outside the assigned Fleet or persistently held elsewhere."""

    def __init__(
        self,
        *,
        page_id: str,
        worker_id: str,
        owner_worker_id: str,
        reason: str,
    ) -> None:
        quarantined = "quarantined" in str(reason or "")
        self.receipt = {
            "status": (
                "page_quarantined"
                if quarantined
                else "page_busy"
                if owner_worker_id
                else "page_binding_violation"
            ),
            "reasonKind": reason,
            "pageId": page_id,
            "workerId": worker_id,
            "ownerWorkerId": owner_worker_id or None,
            "tool_was_executed": False,
            "next_instruction": (
                "This page is quarantined and must not be reused. Call Page.list"
                " and choose claimable=true, quarantined=false, or create a new page."
                if quarantined
                else
                "Another live worker holds this page. Call Page.list and choose"
                " a row with busy=false, or wait for that worker to finish."
                if owner_worker_id
                else "Call Page.list for the assigned Fleet and use a listed pageId."
            ),
        }
        super().__init__(self.receipt["next_instruction"])


class FleetInteractionTimeout(ABCPTransportError):
    """A page/Fleet call could not enter before the bounded wait expired."""

    def __init__(
        self,
        *,
        fleet_id: str,
        worker_id: str,
        method_kind: str,
    ) -> None:
        self.receipt = {
            "status": "fleet_busy",
            "reasonKind": "fleet_interaction_wait_timeout",
            "fleetId": fleet_id,
            "workerId": worker_id,
            "methodKind": method_kind,
            "retryable": True,
            "tool_was_executed": False,
            "next_instruction": (
                "Another opaque Workflow or page interaction still owns this"
                " Fleet. Continue with other work or retry after it settles;"
                " do not repeat the browser action immediately."
            ),
        }
        super().__init__(self.receipt["next_instruction"])


# Keys that submit the focused control and can therefore open a popup window.
# Everything else (arrows, Tab, Escape, editing keys) is left ungated so
# ordinary keyboard work is never serialized Fleet-wide.
SUBMITTING_KEYS = frozenset({"enter", "numpadenter"})


def _is_submitting_key_press(method: str, payload: Any) -> bool:
    """Return whether an ``Input.press`` can submit and thus open a new page."""

    if method != "Input.press" or not isinstance(payload, dict):
        return False
    key = str(payload.get("key") or "").strip().casefold().replace("_", "")
    return key in SUBMITTING_KEYS


def workflow_contains_navigating_action(steps: Any) -> bool:
    """Return whether a Workflow step tree can dispatch a page-opening action.

    Workflow actions execute inside ABCP and therefore do not re-enter the
    Harness browser-call boundary one step at a time.  The outer request is,
    however, fully available before dispatch.  Recursing through all supported
    branch containers lets the Fleet click gate be acquired before ABCP can run
    the first nested action.  Malformed containers are handled by the existing
    workflow validator; this helper only answers the positive capability
    question and never tries to infer navigation intent.

    A submitting key press counts alongside ``Input.click``: a form submitted
    with Enter opens its result in a new tab exactly as often as a clicked link.
    """

    if not isinstance(steps, list):
        return False
    for raw in steps:
        if not isinstance(raw, dict):
            continue
        action = str(raw.get("action") or "").strip()
        if action == "Input.click":
            return True
        if _is_submitting_key_press(action, raw.get("params")) or (
            action == "Input.press"
            and _is_submitting_key_press("Input.press", raw)
        ):
            return True
        for key in ("then", "else", "body"):
            if workflow_contains_navigating_action(raw.get(key)):
                return True
    return False


# Retained for callers that still import the click-only predicate by name.
workflow_contains_input_click = workflow_contains_navigating_action


def _notification_event(message: Any) -> tuple[str, Dict[str, Any]]:
    if not isinstance(message, dict):
        return "", {}
    envelope = message
    if str(message.get("method") or "") == "System.notification":
        params = message.get("params")
        if not isinstance(params, dict):
            return "", {}
        data = params.get("data")
        if not isinstance(data, dict):
            return "", {}
        envelope = data
    name = str(envelope.get("event") or "")
    payload = envelope.get("payload")
    if not isinstance(payload, dict):
        return name, {}
    normalized = dict(payload)
    execution_id = str(
        envelope.get("executionId")
        or envelope.get("execution_id")
        or ""
    ).strip()
    if execution_id:
        normalized["executionId"] = execution_id
    page = payload.get("page")
    if isinstance(page, dict):
        for key in (
            "fleetId",
            "pageId",
            "url",
            "title",
            "openedBy",
            "openerPageId",
            "sourceUrl",
        ):
            if normalized.get(key) in (None, "") and page.get(key) not in (
                None,
                "",
            ):
                normalized[key] = page.get(key)
    return name, normalized


def _execution_id(value: Any) -> str:
    """Extract one ABCP action execution id from a response envelope."""

    if isinstance(value, dict):
        candidate = value.get("executionId") or value.get("execution_id")
        if candidate:
            return str(candidate).strip()
        for nested in value.values():
            found = _execution_id(nested)
            if found:
                return found
    elif isinstance(value, list):
        for nested in value:
            found = _execution_id(nested)
            if found:
                return found
    return ""


def _execution_attributed_popup_receipt(
    *,
    source_page_id: str,
    event_name: str,
    event_payload: Any,
    action_result: Any,
) -> Optional[Dict[str, Any]]:
    """Build a strong popup receipt from ABCP's execution-linked Page.open.

    Current ABCP attaches the initiating Input.click executionId to an
    action-owned Page.open and targets that event only to the caller. A
    Page.open without action execution identity is not attributable: it may be
    an unsolicited site popup and must not be promoted into click success by
    opener/time proximity. Inventory reconciliation remains useful for
    same-page state comparison; legacy popup attribution is separately gated.
    """

    if event_name != "Page.open" or not isinstance(event_payload, dict):
        return None
    action_execution_id = _execution_id(action_result)
    event_execution_id = str(event_payload.get("executionId") or "").strip()
    source = str(source_page_id or "").strip()
    opener = str(event_payload.get("openerPageId") or "").strip()
    landing = str(event_payload.get("pageId") or "").strip()
    if not (
        action_execution_id
        and event_execution_id == action_execution_id
        and source
        and opener == source
        and landing
        and str(event_payload.get("openedBy") or "") == "popup"
    ):
        return None
    return {
        "outcome": "new_page",
        "attribution": "confirmed",
        "attributionSource": "abcp_execution_event",
        "executionId": action_execution_id,
        "sourcePageId": source,
        "sourceUrl": event_payload.get("sourceUrl") or None,
        "landingPageId": landing,
        "landingUrl": (
            event_payload.get("currentUrl")
            or event_payload.get("url")
            or None
        ),
        "openedBy": event_payload.get("openedBy"),
        "openerPageId": opener,
        "eventName": event_name,
        "quarantinedPageIds": [],
        "popupAttributionPolicy": "execution_only",
    }


def _page_rows(value: Any) -> list[Dict[str, Any]]:
    """Extract raw Page.list rows without depending on model-facing wrappers."""

    if not isinstance(value, dict):
        return []
    candidates: list[Any] = [value.get("data")]
    response = value.get("response")
    if isinstance(response, dict):
        candidates.append(response.get("data"))
        candidates.append(response.get("result"))
    candidates.append(value.get("result"))
    for candidate in candidates:
        if isinstance(candidate, list):
            return [dict(row) for row in candidate if isinstance(row, dict)]
        if isinstance(candidate, dict):
            for key in ("pages", "items"):
                rows = candidate.get(key)
                if isinstance(rows, list):
                    return [
                        dict(row) for row in rows if isinstance(row, dict)
                    ]
    return []


def _page_id(row: Dict[str, Any]) -> str:
    return str(row.get("pageId") or row.get("id") or "").strip()


def _page_url(row: Dict[str, Any]) -> str:
    return str(row.get("currentUrl") or row.get("url") or "").strip()


@dataclass
class _RetiredClick:
    source_page_id: str
    expires_at: float
    registered_agent_id: str = ""
    worker_id: str = ""
    method: str = ""


@dataclass
class _ClickOwner:
    source_page_id: str
    registered_agent_id: str
    worker_id: str
    method: str


@dataclass
class _FleetClickEntry:
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    users: int = 0
    retired: list[_RetiredClick] = field(default_factory=list)
    cleanup_handle: Any = None
    discard_when_idle: bool = False
    active_owner: Optional[_ClickOwner] = None


class FleetClickGateTimeout(ABCPTransportError):
    """Retryable local admission rejection before a click is dispatched."""

    def __init__(self, receipt: Dict[str, Any]) -> None:
        self.receipt = dict(receipt)
        status = str(receipt.get("status") or "fleet_click_gated")
        if status == "fleet_click_gated":
            message = (
                "Fleet click gate remained busy for "
                f"{int(receipt.get('waitedMs') or 0)}ms"
            )
        else:
            message = str(
                receipt.get("next_instruction")
                or receipt.get("reasonKind")
                or status
            )
        super().__init__(message)


@dataclass
class FleetClickLease:
    """One cancellation-safe ownership token for a Fleet click interval."""

    manager: "FleetClickGate"
    fleet_id: str
    entry: _FleetClickEntry
    registered_agent_id: str
    worker_id: str
    method: str
    acquired_at: float
    wait_ms: int
    released: bool = False

    def release(self, reason: str) -> None:
        if self.released:
            return
        self.released = True
        active = self.entry.active_owner
        if (
            active is not None
            and active.worker_id == self.worker_id
            and active.method == self.method
        ):
            self.entry.active_owner = None
        if self.entry.lock.locked():
            self.entry.lock.release()
        self.manager._log(
            "fleet_click_gate.released",
            {
                "fleetId": self.fleet_id,
                "registeredAgentId": self.registered_agent_id or None,
                "workerId": self.worker_id or None,
                "method": self.method,
                "reason": str(reason or "completed"),
                "waitMs": self.wait_ms,
                "holdMs": max(
                    0, int((time.monotonic() - self.acquired_at) * 1000)
                ),
            },
        )
        self.manager._release_entry(self.fleet_id, self.entry)


class FleetClickGate:
    """Process-local, Fleet-scoped serialization for every Input.click.

    The gate deliberately does not classify controls by role, href, selector,
    or model purpose.  A JavaScript button can navigate just as readily as an
    anchor, so only serializing link-shaped calls would not establish the
    promised invariant.

    ``soft_settlement_seconds`` is not a page-load timeout.  Page.open is emitted
    while the new page is still ``loading``; the interval only allows the local
    browser/Dispatcher notification to arrive after Input.click has returned.
    A bounded retired-click guard prevents a late same-page mutation or a late
    popup from being silently promoted to the next gated action.

    This gate does NOT attribute popups. Opener/sourceUrl/window agreement is
    reported as an observation (``page_inventory_changed``) and never as
    causality: a page that opens an ad on a timer satisfies all of those
    conditions, and observed runs show ABCP omitting ``popupCauseId`` even on
    the mouse path, so there is no stronger signal to fall back on. Ownership of
    a page the worker did not create is established by Page.list plus an atomic
    lease claim — a fact the harness performs, not one it infers.
    """

    def __init__(
        self,
        *,
        acquire_timeout_seconds: float = 5.0,
        soft_settlement_seconds: float = 0.75,
        non_link_settlement_seconds: float = 0.10,
        submit_settlement_seconds: float = 2.5,
        late_guard_seconds: float = 5.0,
        workflow_hitl_late_guard_seconds: Optional[float] = None,
        max_retired_per_fleet: int = 8,
        popup_inventory_observation_enabled: bool = True,
        logger: Any = None,
    ) -> None:
        self.acquire_timeout_seconds = max(
            0.01, float(acquire_timeout_seconds)
        )
        self.soft_settlement_seconds = max(
            0.0, float(soft_settlement_seconds)
        )
        self.non_link_settlement_seconds = max(
            0.0, float(non_link_settlement_seconds)
        )
        # A form submit reaches the server before the popup exists, so the
        # window that is generous enough for a click is routinely too short
        # here. This bounds the Fleet lock, not the page load: a popup that
        # lands later is still adopted off the notification stream.
        self.submit_settlement_seconds = max(
            self.soft_settlement_seconds,
            float(submit_settlement_seconds),
        )
        self.late_guard_seconds = max(0.0, float(late_guard_seconds))
        workflow_guard = (
            self.late_guard_seconds
            if workflow_hitl_late_guard_seconds is None
            else float(workflow_hitl_late_guard_seconds)
        )
        self.workflow_hitl_late_guard_seconds = max(
            self.late_guard_seconds,
            workflow_guard,
        )
        self.max_retired_per_fleet = max(1, int(max_retired_per_fleet))
        self.popup_inventory_observation_enabled = bool(
            popup_inventory_observation_enabled
        )
        self.logger = logger
        self._entries: Dict[str, _FleetClickEntry] = {}

    async def acquire(
        self,
        fleet_id: str,
        *,
        registered_agent_id: str,
        worker_id: str,
        method: str,
        source_page_id: str = "",
    ) -> FleetClickLease:
        fleet = str(fleet_id or "").strip()
        if not fleet:
            raise ValueError("FleetClickGate requires a non-empty fleetId")
        entry = self._entries.get(fleet)
        if entry is None:
            entry = _FleetClickEntry()
            self._entries[fleet] = entry
        entry.users += 1
        started = time.monotonic()
        try:
            await asyncio.wait_for(
                entry.lock.acquire(),
                timeout=self.acquire_timeout_seconds,
            )
        except asyncio.TimeoutError as exc:
            waited_ms = max(
                0, int((time.monotonic() - started) * 1000)
            )
            self._release_entry(fleet, entry)
            receipt = {
                "status": "fleet_click_gated",
                "reasonKind": "fleet_click_gate_wait_timeout",
                "fleetId": fleet,
                "method": str(method or ""),
                "waitedMs": waited_ms,
                "retryable": True,
                "tool_was_executed": False,
                "next_instruction": (
                    "Another click-capable command still owns this Fleet."
                    " Re-check Fleet/HITL state and retry after the active"
                    " command completes; do not bypass the Fleet click gate."
                ),
            }
            self._log("fleet_click_gate.rejected", receipt)
            raise FleetClickGateTimeout(receipt) from exc
        except BaseException:
            self._release_entry(fleet, entry)
            raise
        lease = FleetClickLease(
            manager=self,
            fleet_id=fleet,
            entry=entry,
            registered_agent_id=str(registered_agent_id or ""),
            worker_id=str(worker_id or ""),
            method=str(method or ""),
            acquired_at=time.monotonic(),
            wait_ms=max(0, int((time.monotonic() - started) * 1000)),
        )
        entry.active_owner = _ClickOwner(
            source_page_id=str(source_page_id or "").strip(),
            registered_agent_id=lease.registered_agent_id,
            worker_id=lease.worker_id,
            method=lease.method,
        )
        self._log(
            "fleet_click_gate.acquired",
            {
                "fleetId": fleet,
                "registeredAgentId": lease.registered_agent_id or None,
                "workerId": lease.worker_id or None,
                "method": lease.method,
                "waitMs": lease.wait_ms,
            },
        )
        return lease

    def classify(
        self,
        *,
        fleet_id: str,
        source_page_id: str,
        baseline_rows: Iterable[Dict[str, Any]],
        final_rows: Iterable[Dict[str, Any]],
        event_name: str = "",
    ) -> Dict[str, Any]:
        now = time.monotonic()
        entry = self._entries.get(str(fleet_id or "").strip())
        retired = self._prune_retired(entry, now) if entry is not None else []
        before = {
            _page_id(row): dict(row)
            for row in baseline_rows
            if _page_id(row)
        }
        after = {
            _page_id(row): dict(row)
            for row in final_rows
            if _page_id(row)
        }
        source = str(source_page_id or "").strip()
        source_url = _page_url(before.get(source, {}))
        new_rows = [
            row for page_id, row in after.items() if page_id not in before
        ]
        inventory_compatible = [
            row
            for row in new_rows
            if (
                str(row.get("openedBy") or "") == "popup"
                and str(row.get("openerPageId") or "") == source
                and (
                    not source_url
                    or not str(row.get("sourceUrl") or "")
                    or str(row.get("sourceUrl") or "") == source_url
                )
            )
        ]
        incompatible = [
            _page_id(row)
            for row in new_rows
            if row not in inventory_compatible and _page_id(row)
        ]
        observed = (
            inventory_compatible
            if self.popup_inventory_observation_enabled
            else []
        )
        ignored_unattributed_count = (
            0
            if self.popup_inventory_observation_enabled
            else len(inventory_compatible)
        )
        prior_click = any(
            item.source_page_id == source
            for item in retired
        )
        source_after = after.get(source, {})
        source_url_after = _page_url(source_after)

        # Same opener + same sourceUrl + inside the window + single candidate
        # lowers the odds of a coincidence; it does not establish one. A page
        # that opens an ad on a timer satisfies every one of those conditions,
        # so this never yields `attribution: confirmed` and never names a
        # landing page the caller may act on. Discovery goes through Page.list
        # and an atomic claim, which is a fact rather than an inference.
        if observed:
            return {
                "outcome": "page_inventory_changed",
                "attribution": "unknown",
                "reasonCode": (
                    "ambiguous_late_popup"
                    if prior_click
                    else "multiple_compatible_popups"
                    if len(observed) > 1
                    else "single_opener_compatible_page"
                ),
                "sourcePageId": source,
                "sourceUrl": source_url or None,
                "observedPageCount": len(observed),
                "quarantinedPageIds": incompatible,
                "eventName": event_name or None,
                "popupAttributionPolicy": "observation_only",
                "next_instruction": (
                    "New pages exist in this fleet that you do not hold. Call"
                    " Page.list once and claim the claimable landing page;"
                    " do not assume the click failed and do not re-click."
                ),
            }
        if (
            source_url
            and source_url_after
            and source_url_after != source_url
        ):
            if prior_click:
                return {
                    "outcome": "ambiguous",
                    "attribution": "unknown",
                    "reasonCode": "ambiguous_late_same_page_navigation",
                    "sourcePageId": source,
                    "sourceUrl": source_url,
                    "candidatePageIds": [source],
                    "landingUrl": source_url_after,
                    "eventName": event_name or None,
                    "quarantinedPageIds": incompatible,
                    "ignoredUnattributedPageCount": ignored_unattributed_count,
                    "popupAttributionPolicy": "observation_only",
                }
            return {
                "outcome": "same_page_changed",
                "attribution": "confirmed",
                "sourcePageId": source,
                "sourceUrl": source_url,
                "landingPageId": source,
                "landingUrl": source_url_after,
                "eventName": event_name or None,
                "quarantinedPageIds": incompatible,
                "ignoredUnattributedPageCount": ignored_unattributed_count,
                "popupAttributionPolicy": "observation_only",
            }
        receipt = {
            "outcome": "no_navigation_observed_within_window",
            "attribution": "unknown",
            "sourcePageId": source,
            "sourceUrl": source_url or None,
            "eventName": event_name or None,
            "quarantinedPageIds": incompatible,
            "ignoredUnattributedPageCount": ignored_unattributed_count,
            "popupAttributionPolicy": "observation_only",
        }
        if ignored_unattributed_count:
            receipt["reasonCode"] = "unattributed_page_open_ignored"
        return receipt

    def retire(
        self,
        fleet_id: str,
        receipt: Dict[str, Any],
        *,
        lease: Optional[FleetClickLease] = None,
    ) -> None:
        fleet = str(fleet_id or "").strip()
        entry = self._entries.get(fleet)
        if entry is None:
            return
        outcome = str(receipt.get("outcome") or "")
        if outcome == "dispatch_failed" or (
            receipt.get("attributionSource") == "abcp_execution_event"
        ):
            return
        method = lease.method if lease is not None else ""
        guard_seconds = (
            self.workflow_hitl_late_guard_seconds
            if method == "Workflow.execute"
            else self.late_guard_seconds
        )
        if guard_seconds <= 0:
            return
        now = time.monotonic()
        self._prune_retired(entry, now)
        entry.retired.append(_RetiredClick(
            source_page_id=str(receipt.get("sourcePageId") or ""),
            expires_at=now + guard_seconds,
            registered_agent_id=(
                lease.registered_agent_id if lease is not None else ""
            ),
            worker_id=lease.worker_id if lease is not None else "",
            method=method,
        ))
        if len(entry.retired) > self.max_retired_per_fleet:
            del entry.retired[:-self.max_retired_per_fleet]
        self._schedule_retired_cleanup(fleet, entry)

    async def claim_workflow_hitl(
        self,
        *,
        event_name: str,
        payload: Any,
        barrier: Optional["FleetAuthBarrier"],
    ) -> Dict[str, Any]:
        """Claim the Fleet auth barrier from a persistent HITL observer.

        Control events can arrive after the per-call Workflow subscriber has
        already unwound.  Actor identity therefore comes from the enforced
        click owner (or its bounded retired tombstone), never from the socket
        that happened to receive the event.
        """

        name = str(event_name or "").strip()
        if name not in {"Hitl.paused", "Hitl.requested"} or barrier is None:
            return {"claimed": False, "reason": "unsupported_event"}
        event_payload = payload if isinstance(payload, dict) else {}
        page = (
            event_payload.get("page")
            if isinstance(event_payload.get("page"), dict)
            else {}
        )
        fleet_id = str(
            event_payload.get("fleetId") or page.get("fleetId") or ""
        ).strip()
        page_id = str(
            event_payload.get("pageId") or page.get("pageId") or ""
        ).strip()
        if not fleet_id or not page_id:
            return {
                "claimed": False,
                "reason": "hitl_event_scope_unavailable",
            }
        entry = self._entries.get(fleet_id)
        if entry is None:
            return {"claimed": False, "reason": "fleet_not_tracked"}
        now = time.monotonic()
        retired = self._prune_retired(entry, now)
        owner = entry.active_owner
        provenance = "active"
        if not (
            owner is not None
            and owner.method == "Workflow.execute"
            and owner.source_page_id == page_id
            and owner.worker_id
        ):
            owner = next(
                (
                    _ClickOwner(
                        source_page_id=item.source_page_id,
                        registered_agent_id=item.registered_agent_id,
                        worker_id=item.worker_id,
                        method=item.method,
                    )
                    for item in reversed(retired)
                    if (
                        item.method == "Workflow.execute"
                        and item.source_page_id == page_id
                        and item.worker_id
                    )
                ),
                None,
            )
            provenance = "retired"
        if owner is None:
            return {
                "claimed": False,
                "reason": "no_matching_workflow_owner",
                "fleetId": fleet_id,
                "pageId": page_id,
            }
        claim = await barrier.claim(
            fleet_id,
            owner.worker_id,
            f"persistent Workflow HITL event: {name}",
        )
        receipt = {
            **dict(claim),
            "fleetId": fleet_id,
            "pageId": page_id,
            "event": name,
            "ownerSource": provenance,
            "workerId": owner.worker_id,
            "registeredAgentId": owner.registered_agent_id or None,
        }
        self._log(
            "workflow.hitl_barrier.claimed_by_event_observer",
            receipt,
        )
        return receipt

    def discard_fleet(self, fleet_id: str) -> None:
        fleet = str(fleet_id or "").strip()
        entry = self._entries.get(fleet)
        if entry is None:
            return
        handle = entry.cleanup_handle
        if handle is not None:
            handle.cancel()
            entry.cleanup_handle = None
        entry.retired.clear()
        if entry.lock.locked() or entry.users > 0:
            entry.discard_when_idle = True
            return
        self._entries.pop(fleet, None)

    def _prune_retired(
        self,
        entry: _FleetClickEntry,
        now: Optional[float] = None,
    ) -> list[_RetiredClick]:
        current = time.monotonic() if now is None else now
        entry.retired[:] = [
            item for item in entry.retired if item.expires_at > current
        ]
        return list(entry.retired)

    def _schedule_retired_cleanup(
        self,
        fleet_id: str,
        entry: _FleetClickEntry,
    ) -> None:
        handle = entry.cleanup_handle
        if handle is not None and not handle.cancelled():
            return
        if not entry.retired:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        delay = max(
            0.0,
            min(item.expires_at for item in entry.retired) - time.monotonic(),
        )
        entry.cleanup_handle = loop.call_later(
            delay,
            self._cleanup_retired,
            fleet_id,
            entry,
        )

    def _cleanup_retired(
        self,
        fleet_id: str,
        entry: _FleetClickEntry,
    ) -> None:
        entry.cleanup_handle = None
        self._prune_retired(entry)
        if entry.retired:
            self._schedule_retired_cleanup(fleet_id, entry)
            return
        if (
            entry.users <= 0
            and not entry.lock.locked()
            and self._entries.get(fleet_id) is entry
        ):
            self._entries.pop(fleet_id, None)

    def _release_entry(
        self,
        fleet_id: str,
        entry: _FleetClickEntry,
    ) -> None:
        entry.users = max(0, entry.users - 1)
        self._prune_retired(entry)
        if (
            entry.discard_when_idle
            and entry.users <= 0
            and not entry.lock.locked()
            and self._entries.get(fleet_id) is entry
        ):
            handle = entry.cleanup_handle
            if handle is not None:
                handle.cancel()
            self._entries.pop(fleet_id, None)
            return
        if (
            entry.users <= 0
            and not entry.retired
            and not entry.lock.locked()
            and self._entries.get(fleet_id) is entry
        ):
            handle = entry.cleanup_handle
            if handle is not None:
                handle.cancel()
            self._entries.pop(fleet_id, None)

    def _log(self, event: str, payload: Dict[str, Any]) -> None:
        logger = self.logger
        if logger is not None and hasattr(logger, "write"):
            try:
                logger.write(event, payload)
            except Exception:
                pass


class PageLeasedBrowserClient:
    """ABCP facade for the shared page lease and enforced Fleet click gate.

    This sits below BrowserAgent tools and Workflow fast paths, so no caller can
    accidentally bypass either process-local serialization rule.
    """

    def __init__(
        self,
        client: Any,
        leases: PageLeaseManager,
        *,
        fleet_owner_client: Any = None,
        fleet_click_gate: Optional[FleetClickGate] = None,
        fleet_auth_barrier: Optional["FleetAuthBarrier"] = None,
        assigned_fleet_id: str = "",
        registered_agent_id: str = "",
        worker_id: str = "",
        click_settlement_classifier: Optional[
            Callable[[str, Dict[str, Any]], str]
        ] = None,
        workflow_hitl_settlement_handler: Optional[
            Callable[[str], Awaitable[Dict[str, Any]]]
        ] = None,
    ) -> None:
        self._client = client
        self._leases = leases
        self._fleet_owner_client = fleet_owner_client
        self._fleet_click_gate = fleet_click_gate
        self._fleet_auth_barrier = fleet_auth_barrier
        self._assigned_fleet_id = str(assigned_fleet_id or "").strip()
        self._registered_agent_id = str(registered_agent_id or "").strip()
        self._worker_id = str(worker_id or "").strip()
        self._click_settlement_classifier = click_settlement_classifier
        self._workflow_hitl_settlement_handler = (
            workflow_hitl_settlement_handler
        )

    def set_click_settlement_classifier(
        self,
        classifier: Optional[Callable[[str, Dict[str, Any]], str]],
    ) -> None:
        """Bind a Harness-owned, non-model-writable target classifier."""

        self._click_settlement_classifier = classifier

    def set_workflow_hitl_settlement_handler(
        self,
        handler: Optional[
            Callable[[str], Awaitable[Dict[str, Any]]]
        ],
    ) -> None:
        """Bind verified post-Workflow HITL recovery owned by the Harness."""

        self._workflow_hitl_settlement_handler = handler

    @staticmethod
    async def _cancel_task(task: "asyncio.Task[Any]") -> None:
        if not task.done():
            task.cancel()
        try:
            await task
        except BaseException:
            pass

    @staticmethod
    def _reperception_receipt(
        *,
        fleet_id: str,
        method: str,
        generation: int,
    ) -> Dict[str, Any]:
        return {
            "status": "fleet_reperception_required",
            "reasonKind": "fleet_auth_generation_changed",
            "fleetId": fleet_id,
            "method": method,
            "generation": int(generation),
            "retryable": True,
            "tool_was_executed": False,
            "next_instruction": (
                "Shared authentication changed while this command was queued."
                " Refresh Page.getState and DOM.getAXTree, rebind the target,"
                " then decide whether the click is still needed."
            ),
        }

    async def _acquire_click_gate(
        self,
        gate: FleetClickGate,
        *,
        fleet_id: str,
        method: str,
        source_page_id: str,
    ) -> FleetClickLease:
        """Race ordinary click admission against an opaque-Workflow HITL.

        A queued click must not surface a five/ thirty-second click timeout
        while another worker is actually waiting for a human. Once the auth
        barrier closes, abandon the stale click, wait on the existing barrier,
        and return exactly one structured auth/reperception receipt.
        """

        barrier = self._fleet_auth_barrier
        if barrier is None:
            return await gate.acquire(
                fleet_id,
                registered_agent_id=self._registered_agent_id,
                worker_id=self._worker_id,
                method=method,
                source_page_id=source_page_id,
            )

        seen_generation = barrier.generation(fleet_id)
        gate_task = asyncio.create_task(gate.acquire(
            fleet_id,
            registered_agent_id=self._registered_agent_id,
            worker_id=self._worker_id,
            method=method,
            source_page_id=source_page_id,
        ))
        auth_task = asyncio.create_task(barrier.wait_until_resolving(
            fleet_id,
            self._worker_id,
        ))
        owned_lease: Optional[FleetClickLease] = None
        try:
            done, _pending = await asyncio.wait(
                {gate_task, auth_task},
                return_when=asyncio.FIRST_COMPLETED,
            )

            if auth_task in done:
                if gate_task.done():
                    try:
                        owned_lease = gate_task.result()
                    except BaseException:
                        owned_lease = None
                else:
                    await self._cancel_task(gate_task)
                if owned_lease is not None:
                    owned_lease.release("fleet_auth_barrier_closed")
                    owned_lease = None

                auth_signal = auth_task.result()
                admission = await barrier.before_call(
                    fleet_id,
                    self._worker_id,
                    seen_generation=seen_generation,
                )
                if admission.get("allowed"):
                    receipt = self._reperception_receipt(
                        fleet_id=fleet_id,
                        method=method,
                        generation=int(admission.get("generation") or 0),
                    )
                else:
                    receipt = {
                        **dict(admission),
                        "method": method,
                    }
                gate._log(
                    "fleet_click_gate.redirected_to_auth_barrier",
                    {
                        **dict(auth_signal),
                        **dict(receipt),
                        "registeredAgentId": (
                            self._registered_agent_id or None
                        ),
                        "workerId": self._worker_id or None,
                    },
                )
                raise FleetClickGateTimeout(receipt)

            await self._cancel_task(auth_task)
            owned_lease = gate_task.result()
            # Close the race where lock acquisition and auth claim complete
            # together.
            admission = await barrier.before_call(
                fleet_id,
                self._worker_id,
                seen_generation=seen_generation,
            )
            if not admission.get("allowed"):
                owned_lease.release(
                    "fleet_auth_barrier_closed_after_acquire"
                )
                owned_lease = None
                raise FleetClickGateTimeout({
                    **dict(admission),
                    "method": method,
                })
            if admission.get("generationChanged"):
                owned_lease.release(
                    "fleet_auth_generation_changed_after_acquire"
                )
                owned_lease = None
                raise FleetClickGateTimeout(self._reperception_receipt(
                    fleet_id=fleet_id,
                    method=method,
                    generation=int(admission.get("generation") or 0),
                ))
            lease = owned_lease
            owned_lease = None
            return lease
        except BaseException:
            await self._cancel_task(auth_task)
            if not gate_task.done():
                await self._cancel_task(gate_task)
            elif owned_lease is None:
                try:
                    candidate = gate_task.result()
                except BaseException:
                    candidate = None
                if isinstance(candidate, FleetClickLease):
                    candidate.release("click_gate_admission_cancelled")
            if owned_lease is not None:
                owned_lease.release("click_gate_admission_cancelled")
            raise

    @staticmethod
    def _literal_page_ids(method: str, payload: Dict[str, Any]) -> set[str]:
        """Collect explicit page handles without guessing Workflow variables."""

        found: set[str] = set()

        def visit(value: Any) -> None:
            if isinstance(value, dict):
                for key, nested in value.items():
                    if key in {"pageId", "page_id"} and isinstance(nested, str):
                        candidate = nested.strip()
                        if candidate and "${" not in candidate:
                            found.add(candidate)
                    elif method == "Workflow.execute" or key not in {"params"}:
                        visit(nested)
            elif isinstance(value, list):
                for nested in value:
                    visit(nested)

        visit(payload)
        return found

    @staticmethod
    def _result_page_ids(value: Any) -> set[str]:
        found: set[str] = set()

        def visit(item: Any) -> None:
            if isinstance(item, dict):
                page_id = item.get("pageId") or item.get("page_id")
                if isinstance(page_id, str) and page_id.strip():
                    found.add(page_id.strip())
                for nested in item.values():
                    visit(nested)
            elif isinstance(item, list):
                for nested in item:
                    visit(nested)

        visit(value)
        return found

    async def call(self, method: str, params: Any = None) -> Any:
        payload = params if isinstance(params, dict) else {}
        fleet_id = str(
            payload.get("fleetId")
            or payload.get("fleet_id")
            or self._assigned_fleet_id
            or ""
        ).strip()
        page_ids = self._literal_page_ids(method, payload)
        workflow = method == "Workflow.execute"
        page_create = method == "Page.create"
        if workflow and self._worker_id:
            top_page_id = str(
                payload.get("pageId") or payload.get("page_id") or ""
            ).strip()
            for page_id in page_ids:
                if page_id == top_page_id:
                    continue
                known_fleet = self._leases.fleet_for(page_id)
                if not known_fleet or (fleet_id and known_fleet != fleet_id):
                    raise PageLeaseConflict(
                        page_id=page_id,
                        worker_id=self._worker_id,
                        owner_worker_id=self._leases.owner_for(page_id),
                        reason="workflow_page_not_listed_in_assigned_fleet",
                    )
        if workflow or page_ids or page_create:
            async with self._leases.interaction(
                page_ids=page_ids,
                fleet_id=fleet_id,
                worker_id=self._worker_id,
                workflow=workflow,
                fleet_scoped=page_create,
            ):
                result = await self._call_under_interaction(method, params)
                if page_create:
                    created_ids = self._result_page_ids(result)
                    if created_ids:
                        if self._worker_id:
                            self._leases.claim_created_pages(
                                worker_id=self._worker_id,
                                fleet_id=fleet_id,
                                page_ids=created_ids,
                            )
                        else:
                            self._leases.observe_inventory(fleet_id, created_ids)
        else:
            # Fleet/Page inventory and other page-less reads stay observable
            # during an opaque Workflow; only page-scoped interaction is
            # excluded by the Fleet lease.
            result = await self._call_under_interaction(method, params)

        if method == "Page.list":
            for row in _page_rows(result):
                row_page_id = _page_id(row)
                row_fleet_id = str(
                    row.get("fleetId") or row.get("fleet_id") or ""
                ).strip()
                if row_page_id and row_fleet_id:
                    self._leases.observe_inventory(row_fleet_id, [row_page_id])
        elif method == "Page.close" and not (
            isinstance(result, dict) and result.get("error")
        ):
            for page_id in page_ids:
                self._leases.forget_page(page_id, self._worker_id)
        return result

    async def _call_under_interaction(self, method: str, params: Any = None) -> Any:
        payload = params if isinstance(params, dict) else {}
        page_id = str(payload.get("pageId") or payload.get("page_id") or "")
        fleet_id = str(
            payload.get("fleetId")
            or payload.get("fleet_id")
            or self._assigned_fleet_id
            or ""
        ).strip()
        target = (
            self._fleet_owner_client
            if method == "Page.create" and self._fleet_owner_client is not None
            else self._client
        )
        gated = (
            method == "Input.click"
            or _is_submitting_key_press(method, payload)
            or (
                method == "Workflow.execute"
                and workflow_contains_navigating_action(payload.get("steps"))
            )
        )
        gate = self._fleet_click_gate
        if gated and gate is not None and not fleet_id:
            # A configured process-wide gate must never silently degrade to an
            # ungated click. Spawner always supplies the assignment Fleet;
            # reaching this branch means the ownership wiring is incomplete.
            raise RuntimeError(
                f"{method} requires a fleetId while FleetClickGate is enabled"
            )
        if not gated or gate is None:
            return await target.call(method, params)

        lease = await self._acquire_click_gate(
            gate,
            fleet_id=fleet_id,
            method=method,
            source_page_id=page_id,
        )
        unsubscribe: Optional[Callable[[], None]] = None
        loop = asyncio.get_running_loop()
        settlement_event: "asyncio.Future[tuple[str, Dict[str, Any]]]" = (
            loop.create_future()
        )
        settlement_events: list[tuple[str, Dict[str, Any]]] = []
        baseline_rows: list[Dict[str, Any]] = []
        receipt: Dict[str, Any] = {
            "outcome": "baseline_unavailable",
            "attribution": "unknown",
            "sourcePageId": page_id or None,
        }
        settlement_class = "conservative"
        settlement_seconds = gate.soft_settlement_seconds
        classifier = self._click_settlement_classifier
        if method == "Input.click" and callable(classifier):
            try:
                classified = str(classifier(method, payload) or "")
            except Exception:
                classified = ""
            if classified == "fresh_non_link":
                settlement_class = classified
                settlement_seconds = gate.non_link_settlement_seconds
            elif classified == "fresh_link":
                settlement_class = classified
        elif _is_submitting_key_press(method, payload):
            settlement_class = "submit_key"
            settlement_seconds = gate.submit_settlement_seconds
        elif method == "Workflow.execute":
            settlement_class = "opaque_workflow"
        action_call_started = False
        workflow_hitl_events: list[Dict[str, Any]] = []
        workflow_hitl_claim_task: Optional[
            "asyncio.Task[Dict[str, Any]]"
        ] = None
        workflow_hitl_receipt: Dict[str, Any] = {}

        def observe(message: Any) -> None:
            nonlocal workflow_hitl_claim_task
            name, event_payload = _notification_event(message)
            event_fleet = str(event_payload.get("fleetId") or "").strip()
            if event_fleet and event_fleet != fleet_id:
                return
            event_page = str(event_payload.get("pageId") or "").strip()
            workflow_event_matches = (
                method == "Workflow.execute"
                and name in {
                    "Hitl.paused",
                    "Hitl.requested",
                    "Hitl.resumed",
                }
                and (not event_page or not page_id or event_page == page_id)
            )
            if workflow_event_matches:
                workflow_hitl_events.append({
                    "event": name,
                    "pageId": event_page or page_id or None,
                })
                barrier = self._fleet_auth_barrier
                if (
                    name in {"Hitl.paused", "Hitl.requested"}
                    and workflow_hitl_claim_task is None
                    and barrier is not None
                    and fleet_id
                    and self._worker_id
                ):
                    # Never await inside the websocket callback. The claim is
                    # actor-safe because this closure exists only around the
                    # Workflow.execute currently dispatched by this worker.
                    workflow_hitl_claim_task = loop.create_task(barrier.claim(
                        fleet_id,
                        self._worker_id,
                        f"opaque Workflow HITL event: {name}",
                    ))
            opener = str(event_payload.get("openerPageId") or "").strip()
            relevant = (
                (
                    name == "Page.open"
                    and str(event_payload.get("openedBy") or "") == "popup"
                    and bool(page_id)
                    and opener == page_id
                )
                or (
                    name in {"Page.navigate", "Page.close", "Page.crashed"}
                    and bool(page_id)
                    and event_page == page_id
                )
                or name in {"Hitl.paused", "Hitl.requested"}
            )
            if relevant:
                settlement_events.append((name, dict(event_payload)))
                if not settlement_event.done():
                    settlement_event.set_result((name, event_payload))

        async def settle_workflow_hitl(
            *,
            allow_verified_open: bool = True,
        ) -> Dict[str, Any]:
            nonlocal workflow_hitl_receipt

            def completed(value: Dict[str, Any]) -> Dict[str, Any]:
                nonlocal workflow_hitl_receipt
                workflow_hitl_receipt = dict(value)
                gate._log(
                    "workflow.hitl_barrier.settled",
                    {
                        "fleetId": fleet_id,
                        "workerId": self._worker_id or None,
                        "registeredAgentId": (
                            self._registered_agent_id or None
                        ),
                        **value,
                    },
                )
                return dict(value)

            if workflow_hitl_receipt:
                return dict(workflow_hitl_receipt)
            if workflow_hitl_claim_task is None:
                barrier = self._fleet_auth_barrier
                if barrier is None:
                    return {}
                ownership = await barrier.resolver_status(
                    fleet_id, self._worker_id
                )
                if not ownership.get("owned"):
                    return {}
                claim = {
                    "claimed": True,
                    "resolverWorkerId": self._worker_id,
                    "generation": ownership.get("generation"),
                    "preclaimed": True,
                }
            else:
                try:
                    claim = await workflow_hitl_claim_task
                except BaseException as exc:
                    claim = {
                        "claimed": False,
                        "reason": "claim_failed",
                        "errorType": type(exc).__name__,
                        "error": str(exc)[:300],
                    }
            receipt: Dict[str, Any] = {
                "observed": True,
                "events": list(workflow_hitl_events),
                "claim": dict(claim),
            }
            if not claim.get("claimed"):
                receipt.update({
                    "opened": False,
                    "reason": "auth_barrier_claim_not_owned",
                })
                return completed(receipt)
            if not allow_verified_open:
                receipt.update({
                    "opened": False,
                    "reason": (
                        "workflow_response_unavailable_barrier_kept_closed"
                    ),
                })
                return completed(receipt)
            handler = self._workflow_hitl_settlement_handler
            hitl_page_id = next(
                (
                    str(item.get("pageId") or "")
                    for item in reversed(workflow_hitl_events)
                    if item.get("pageId")
                ),
                page_id,
            )
            if not callable(handler) or not hitl_page_id:
                receipt.update({
                    "opened": False,
                    "reason": "verified_settlement_handler_unavailable",
                })
                return completed(receipt)
            try:
                settlement = await handler(hitl_page_id)
            except BaseException as exc:
                settlement = {
                    "opened": False,
                    "reason": "verified_settlement_handler_failed",
                    "errorType": type(exc).__name__,
                    "error": str(exc)[:300],
                }
            receipt["settlement"] = dict(settlement or {})
            receipt["opened"] = bool(
                isinstance(settlement, dict)
                and settlement.get("opened")
            )
            if not receipt["opened"]:
                receipt["reason"] = str(
                    (settlement or {}).get("reason")
                    if isinstance(settlement, dict)
                    else ""
                ) or "verified_settlement_failed"
            return completed(receipt)

        subscribe = getattr(target, "subscribe_notifications", None)
        try:
            try:
                baseline_rows = _page_rows(await target.call(
                    "Page.list", {"fleetId": fleet_id}
                ))
            except Exception as exc:
                gate._log(
                    "fleet_click_gate.reconciliation_error",
                    {
                        "fleetId": fleet_id,
                        "method": method,
                        "stage": "baseline",
                        "error": str(exc)[:300],
                    },
                )
            if callable(subscribe):
                unsubscribe = subscribe(observe)
            action_call_started = True
            result = await target.call(method, params)

            event_name = ""
            settled_event_payload: Dict[str, Any] = {}
            if not settlement_event.done():
                try:
                    await asyncio.wait_for(
                        asyncio.shield(settlement_event),
                        timeout=settlement_seconds,
                    )
                except asyncio.TimeoutError:
                    pass
            if settlement_event.done() and not settlement_event.cancelled():
                try:
                    event_name, settled_event_payload = settlement_event.result()
                except Exception:
                    event_name = ""
                    settled_event_payload = {}

            final_rows: list[Dict[str, Any]] = []
            try:
                final_rows = _page_rows(await target.call(
                    "Page.list", {"fleetId": fleet_id}
                ))
            except Exception as exc:
                gate._log(
                    "fleet_click_gate.reconciliation_error",
                    {
                        "fleetId": fleet_id,
                        "method": method,
                        "stage": "final",
                        "error": str(exc)[:300],
                    },
                )
            workflow_hitl_receipt = await settle_workflow_hitl()
            execution_receipt = None
            if method == "Input.click":
                for candidate_name, candidate_payload in reversed(
                    settlement_events
                ):
                    execution_receipt = _execution_attributed_popup_receipt(
                        source_page_id=page_id,
                        event_name=candidate_name,
                        event_payload=candidate_payload,
                        action_result=result,
                    )
                    if execution_receipt is not None:
                        break
            if execution_receipt is not None:
                receipt = execution_receipt
            elif baseline_rows and final_rows:
                receipt = gate.classify(
                    fleet_id=fleet_id,
                    source_page_id=page_id,
                    baseline_rows=baseline_rows,
                    final_rows=final_rows,
                    event_name=event_name,
                )
            else:
                receipt = {
                    "outcome": "baseline_unavailable",
                    "attribution": "unknown",
                    "sourcePageId": page_id or None,
                    "eventName": event_name or None,
                }
            if method == "Workflow.execute":
                inventory_observation = dict(receipt)
                # Preserve the raw inventory shape for diagnostics without
                # leaving a nested `confirmed` value that a future consumer
                # could accidentally promote into per-action causality.
                inventory_observation["attribution"] = "diagnostic_only"
                inventory_observation["actionAttributionEligible"] = False
                receipt = {
                    "outcome": "ambiguous",
                    "attribution": "unknown",
                    "reasonCode": "opaque_workflow_not_atomic",
                    "sourcePageId": page_id or None,
                    "inventoryObservation": inventory_observation,
                }
                if workflow_hitl_receipt:
                    receipt["workflowHitl"] = dict(workflow_hitl_receipt)
            receipt["settlementClass"] = settlement_class
            receipt["settlementWindowMs"] = max(
                0, int(settlement_seconds * 1000)
            )
            gate.retire(fleet_id, receipt, lease=lease)
            gate._log(
                "fleet_click_gate.outcome",
                {
                    "fleetId": fleet_id,
                    "registeredAgentId": self._registered_agent_id or None,
                    "workerId": self._worker_id or None,
                    "method": method,
                    **receipt,
                },
            )
            if isinstance(result, dict):
                result = dict(result)
                result["harnessClickGate"] = dict(receipt)
            return result
        except BaseException as exc:
            if method == "Workflow.execute":
                workflow_hitl_receipt = await settle_workflow_hitl(
                    allow_verified_open=False,
                )
            outcome = (
                "dispatch_response_failed"
                if action_call_started
                else "dispatch_failed"
            )
            receipt = {
                "outcome": outcome,
                "attribution": "unknown",
                "sourcePageId": page_id or None,
                "settlementClass": settlement_class,
                "settlementWindowMs": max(
                    0, int(settlement_seconds * 1000)
                ),
                "reasonCode": type(exc).__name__,
            }
            if workflow_hitl_receipt:
                receipt["workflowHitl"] = dict(workflow_hitl_receipt)
            if action_call_started:
                gate.retire(fleet_id, receipt, lease=lease)
                gate._log(
                    "fleet_click_gate.outcome",
                    {
                        "fleetId": fleet_id,
                        "registeredAgentId": (
                            self._registered_agent_id or None
                        ),
                        "workerId": self._worker_id or None,
                        "method": method,
                        **receipt,
                    },
                )
            raise
        finally:
            if unsubscribe is not None:
                try:
                    unsubscribe()
                except Exception:
                    pass
            if not settlement_event.done():
                settlement_event.cancel()
            lease.release(str(receipt.get("outcome") or "completed"))

    def __getattr__(self, name: str) -> Any:
        return getattr(self._client, name)


@dataclass
class _AuthBarrierState:
    condition: asyncio.Condition = field(default_factory=asyncio.Condition)
    resolver_worker_id: str = ""
    reason: str = ""
    generation: int = 0
    resolving: bool = False


class FleetAuthBarrier:
    """Fleet-wide, fail-closed authentication/challenge barrier.

    A detecting worker becomes the resolver. Other workers wait for a bounded
    interval. A timeout never opens the gate; callers receive a structured
    ``fleet_auth_gated`` result and can retry after the resolver finishes.
    """

    def __init__(self, *, wait_timeout_seconds: float = 120.0) -> None:
        self.wait_timeout_seconds = max(0.01, float(wait_timeout_seconds))
        self._states: Dict[str, _AuthBarrierState] = {}

    def generation(self, fleet_id: str) -> int:
        state = self._states.get(str(fleet_id or "").strip())
        return state.generation if state is not None else 0

    async def resolver_status(
        self,
        fleet_id: str,
        worker_id: str,
    ) -> dict:
        """Return an atomic ownership view for post-Workflow settlement."""

        fleet = str(fleet_id or "").strip()
        worker = str(worker_id or "").strip()
        state = self._states.get(fleet)
        if state is None:
            return {
                "owned": False,
                "resolving": False,
                "generation": 0,
            }
        async with state.condition:
            return {
                "owned": bool(
                    state.resolving
                    and worker
                    and state.resolver_worker_id == worker
                ),
                "resolving": state.resolving,
                "resolverWorkerId": state.resolver_worker_id or None,
                "generation": state.generation,
            }

    async def wait_until_resolving(
        self,
        fleet_id: str,
        worker_id: str,
    ) -> dict:
        """Wait until another worker closes this Fleet's auth barrier.

        FleetClickGate races this signal against ordinary lock acquisition.
        The callback that observes a Workflow HITL cannot block the websocket
        reader, so ``claim`` notifies this condition and queued clicks move to
        the auth wait without first exhausting the click-lock timeout.
        """

        fleet = str(fleet_id or "").strip()
        worker = str(worker_id or "").strip()
        if not fleet:
            return {"resolving": False, "generation": 0}
        state = self._states.setdefault(fleet, _AuthBarrierState())
        async with state.condition:
            await state.condition.wait_for(
                lambda: (
                    state.resolving
                    and state.resolver_worker_id != worker
                )
            )
            return {
                "resolving": True,
                "fleetId": fleet,
                "resolverWorkerId": state.resolver_worker_id or None,
                "barrierReason": state.reason,
                "generation": state.generation,
            }

    async def workflow_fence_before(
        self,
        fleet_id: str,
        worker_id: str,
        *,
        seen_generation: int,
    ) -> dict:
        """Non-blocking atomic preflight for an opaque Workflow.execute call."""

        fleet = str(fleet_id or "").strip()
        worker = str(worker_id or "").strip()
        if not fleet:
            return {
                "allowed": True,
                "generation": 0,
                "generationChanged": False,
            }
        state = self._states.setdefault(fleet, _AuthBarrierState())
        async with state.condition:
            if state.resolving:
                return {
                    "allowed": False,
                    "status": "fleet_auth_gated",
                    "reasonKind": "workflow_auth_barrier_closed",
                    "fleetId": fleet,
                    "resolverWorkerId": state.resolver_worker_id or None,
                    "barrierReason": state.reason,
                    "generation": state.generation,
                    "retryable": True,
                    "tool_was_executed": False,
                }
            changed = int(seen_generation) != state.generation
            return {
                "allowed": not changed,
                "status": (
                    "fleet_reperception_required" if changed else "allowed"
                ),
                "reasonKind": (
                    "workflow_auth_generation_changed" if changed else "allowed"
                ),
                "fleetId": fleet,
                "generation": state.generation,
                "generationChanged": changed,
                "retryable": changed,
                "tool_was_executed": False if changed else None,
            }

    async def workflow_fence_after(
        self,
        fleet_id: str,
        *,
        started_generation: int,
    ) -> dict:
        """Validate that an opaque workflow returned in the same auth epoch."""

        fleet = str(fleet_id or "").strip()
        if not fleet:
            return {
                "valid": True,
                "generation": 0,
                "generationChanged": False,
            }
        state = self._states.setdefault(fleet, _AuthBarrierState())
        async with state.condition:
            changed = int(started_generation) != state.generation
            valid = not state.resolving and not changed
            reason = (
                "workflow_auth_barrier_closed"
                if state.resolving
                else (
                    "workflow_auth_generation_changed"
                    if changed
                    else "valid"
                )
            )
            return {
                "valid": valid,
                "status": "valid" if valid else "workflow_row_quarantined",
                "reasonKind": reason,
                "fleetId": fleet,
                "resolverWorkerId": state.resolver_worker_id or None,
                "barrierReason": state.reason or None,
                "startedGeneration": int(started_generation),
                "generation": state.generation,
                "generationChanged": changed,
                "retryable": not valid,
            }

    @staticmethod
    def _resolver_required_receipt(
        fleet_id: str,
        state: _AuthBarrierState,
    ) -> dict:
        return {
            "allowed": False,
            "status": "fleet_auth_gated",
            "reasonKind": "fleet_auth_resolver_required",
            "resolverRequired": True,
            "fleetId": fleet_id,
            "resolverWorkerId": None,
            "barrierReason": state.reason,
            "generation": state.generation,
            "retryable": True,
            "tool_was_executed": False,
            "next_instruction": (
                "The prior authentication resolver relinquished ownership"
                " without opening the fleet. Refresh Page.getState and"
                " DOM.getAXTree if needed, then explicitly call"
                " Hitl.requestPause to claim authentication recovery."
            ),
        }

    async def claim(self, fleet_id: str, worker_id: str, reason: str) -> dict:
        fleet = str(fleet_id or "").strip()
        worker = str(worker_id or "").strip()
        if not fleet or not worker:
            return {"claimed": False, "reason": "missing_identity"}
        state = self._states.setdefault(fleet, _AuthBarrierState())
        async with state.condition:
            if state.resolving:
                if not state.resolver_worker_id:
                    state.resolver_worker_id = worker
                    state.reason = str(reason or state.reason)[:500]
                    state.condition.notify_all()
                    return {
                        "claimed": True,
                        "resolverWorkerId": worker,
                        "generation": state.generation,
                        "takeover": True,
                    }
                return {
                    "claimed": state.resolver_worker_id == worker,
                    "resolverWorkerId": state.resolver_worker_id,
                    "generation": state.generation,
                }
            state.resolving = True
            state.resolver_worker_id = worker
            state.reason = str(reason or "authentication challenge")[:500]
            state.condition.notify_all()
            return {
                "claimed": True,
                "resolverWorkerId": worker,
                "generation": state.generation,
            }

    async def claim_ownerless(
        self,
        fleet_id: str,
        worker_id: str,
        reason: str,
    ) -> dict:
        """Atomically claim only an already-closed, ownerless barrier.

        This is the recovery entry used before ``Page.create`` when the prior
        resolver ended without verified clearance and no usable pageId remains.
        It never closes an open barrier, so an ordinary Page.create cannot
        accidentally turn a healthy fleet into an authentication recovery.
        """

        fleet = str(fleet_id or "").strip()
        worker = str(worker_id or "").strip()
        if not fleet or not worker:
            return {
                "required": False,
                "claimed": False,
                "reason": "missing_identity",
            }
        state = self._states.get(fleet)
        if state is None:
            return {
                "required": False,
                "claimed": False,
                "generation": 0,
            }
        async with state.condition:
            if not state.resolving:
                return {
                    "required": False,
                    "claimed": False,
                    "generation": state.generation,
                }
            if state.resolver_worker_id:
                return {
                    "required": True,
                    "claimed": state.resolver_worker_id == worker,
                    "resolverWorkerId": state.resolver_worker_id,
                    "generation": state.generation,
                    "takeover": False,
                }
            state.resolver_worker_id = worker
            state.reason = str(
                reason or state.reason or "authentication recovery"
            )[:500]
            return {
                "required": True,
                "claimed": True,
                "resolverWorkerId": worker,
                "generation": state.generation,
                "takeover": True,
            }

    async def before_call(
        self,
        fleet_id: str,
        worker_id: str,
        *,
        seen_generation: int,
    ) -> dict:
        fleet = str(fleet_id or "").strip()
        worker = str(worker_id or "").strip()
        if not fleet:
            return {"allowed": True, "generation": 0, "generationChanged": False}
        state = self._states.setdefault(fleet, _AuthBarrierState())
        async with state.condition:
            if state.resolving and not state.resolver_worker_id:
                return self._resolver_required_receipt(fleet, state)
            if state.resolving and state.resolver_worker_id != worker:
                try:
                    await asyncio.wait_for(
                        state.condition.wait_for(
                            lambda: (
                                not state.resolving
                                or not state.resolver_worker_id
                            )
                        ),
                        timeout=self.wait_timeout_seconds,
                    )
                except asyncio.TimeoutError:
                    return {
                        "allowed": False,
                        "status": "fleet_auth_gated",
                        "reasonKind": "fleet_auth_gated",
                        "fleetId": fleet,
                        "resolverWorkerId": state.resolver_worker_id,
                        "barrierReason": state.reason,
                        "generation": state.generation,
                        "retryable": True,
                        "tool_was_executed": False,
                        "next_instruction": (
                            "Wait for the fleet authentication resolver to finish; "
                            "do not act on this shared cookie jar or create another fleet."
                        ),
                    }
                if state.resolving and not state.resolver_worker_id:
                    return self._resolver_required_receipt(fleet, state)
            return {
                "allowed": True,
                "generation": state.generation,
                "generationChanged": int(seen_generation) != state.generation,
                "resolverWorkerId": state.resolver_worker_id or None,
            }

    async def resolve(self, fleet_id: str, worker_id: str) -> dict:
        fleet = str(fleet_id or "").strip()
        worker = str(worker_id or "").strip()
        state = self._states.get(fleet)
        if state is None:
            return {"resolved": False, "reason": "not_claimed"}
        async with state.condition:
            if not state.resolving or state.resolver_worker_id != worker:
                return {
                    "resolved": False,
                    "reason": "resolver_mismatch",
                    "resolverWorkerId": state.resolver_worker_id,
                    "generation": state.generation,
                }
            state.resolving = False
            state.resolver_worker_id = ""
            state.reason = ""
            state.generation += 1
            state.condition.notify_all()
            return {"resolved": True, "generation": state.generation}

    async def relinquish(
        self,
        fleet_id: str,
        worker_id: str,
        *,
        reason: str,
    ) -> dict:
        """Drop resolver ownership while deliberately keeping the gate shut."""

        fleet = str(fleet_id or "").strip()
        worker = str(worker_id or "").strip()
        state = self._states.get(fleet)
        if state is None:
            return {"relinquished": False, "reason": "not_claimed"}
        async with state.condition:
            if not state.resolving or state.resolver_worker_id != worker:
                return {
                    "relinquished": False,
                    "reason": "resolver_mismatch",
                    "resolverWorkerId": state.resolver_worker_id,
                    "generation": state.generation,
                }
            state.resolver_worker_id = ""
            state.reason = str(reason or "resolver relinquished")[:500]
            state.condition.notify_all()
            return {
                "relinquished": True,
                "gateOpen": False,
                "generation": state.generation,
            }

    async def abandon_worker(self, worker_id: str) -> None:
        """Remove a dead resolver without opening its fleet gate.

        The next explicit ``claim`` may become resolver. Ordinary browser calls
        remain gated while ownership is empty.
        """

        worker = str(worker_id or "").strip()
        if not worker:
            return
        for state in list(self._states.values()):
            async with state.condition:
                if state.resolving and state.resolver_worker_id == worker:
                    state.resolver_worker_id = ""
                    state.reason = (
                        f"prior resolver {worker} ended before verified clearance"
                    )
                    state.condition.notify_all()

    async def discard_fleet(self, fleet_id: str, *, force: bool = False) -> dict:
        """Release barrier bookkeeping after a fleet is retired or reset."""

        fleet = str(fleet_id or "").strip()
        state = self._states.get(fleet)
        if state is None:
            return {"discarded": False, "reason": "not_found"}
        async with state.condition:
            if state.resolving and not force:
                return {
                    "discarded": False,
                    "reason": "resolver_active",
                    "resolverWorkerId": state.resolver_worker_id,
                }
            state.resolving = False
            state.resolver_worker_id = ""
            state.reason = "fleet barrier retired"
            state.condition.notify_all()
            self._states.pop(fleet, None)
            return {"discarded": True}

    def discard_inactive(self, fleet_id: str) -> bool:
        """Synchronously prune a retired fleet when no resolver/waiter exists."""

        fleet = str(fleet_id or "").strip()
        state = self._states.get(fleet)
        if state is None or state.resolving or state.condition.locked():
            return False
        self._states.pop(fleet, None)
        return True

    async def shutdown(self) -> None:
        for fleet_id in list(self._states):
            await self.discard_fleet(fleet_id, force=True)
