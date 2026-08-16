"""
harness.spawner.spawner_registry - SpawnerRegistryMixin - slot/page registry sync, quarantine and summaries.
"""

import json
import time
from typing import Any
from typing import Dict
from typing import List
from typing import Optional
from typing import Set
from abcp_client import ABCPTransportError
from harness.results.call_outcome import evaluate_grant
from harness.fleet.coordinator import FleetAssignment
from harness.fleet.coordinator import handle_records_from_value
from harness.observation.render_recovery import extract_page_id_from_values
from harness.utils import JsonDict
from harness.utils import optional_int
from harness.utils import trim_large_strings
from .spawner_classification import _origin_from_url, _page_hidden_from_reuse, _state_response_indicates_paused, _text_indicates_paused_error  # noqa: F401
from .spawner_helpers import BrowserAgentSlot, SLOT_FULL_SYNC_TTL_SECONDS, _is_fleet_open_timeout  # noqa: F401

def _sp():
    import harness.spawner as sp

    return sp

class SpawnerRegistryMixin:

    def _slot_sync_due(self, slot: BrowserAgentSlot) -> bool:
        if slot.last_sync_at <= 0:
            return True
        if slot.sync_errors:
            return True
        return (time.monotonic() - slot.last_sync_at) >= SLOT_FULL_SYNC_TTL_SECONDS

    async def _sync_slot_registry(
        self,
        slot: BrowserAgentSlot,
        *,
        worker_id: str,
        required_fleet_id: str = "",
        include_page_details: bool = True,
    ) -> None:
        if slot.client is None:
            return
        slot.sync_errors = []
        try:
            fleet_response = await slot.client.call("Fleet.list", {})
            self._replace_slot_fleets_from_response(slot, fleet_response)
            self._update_slot_registry_from_value(slot, fleet_response)
        except Exception as exc:
            slot.sync_errors.append(f"Fleet.list: {str(exc)[:240]}")

        # Self-heal: drop registry keys that are obviously not real ids. A
        # leaked schema-dict repr (pre-as_id-guard bug, or any future leak) is
        # str({...}) and starts with '{'; calling Page.list/Page.getState on it
        # returns -32602 and just pollutes sync_errors. Real fleet/page ids are
        # UUIDs and never start with '{'.
        slot.fleet_ids = {
            fid for fid in slot.fleet_ids
            if isinstance(fid, str) and fid and not fid.startswith("{")
        }
        bogus_page_ids = [
            pid for pid in list(slot.page_registry)
            if not isinstance(pid, str) or pid.startswith("{")
        ]
        for pid in bogus_page_ids:
            slot.page_registry.pop(pid, None)
            slot.page_quarantine.pop(pid, None)

        fleet_timeout_counts: Dict[str, int] = {}
        unavailable_fleets: Set[str] = set()

        def note_fleet_timeout(fleet_id: str, exc: BaseException) -> None:
            if not _is_fleet_open_timeout(exc):
                return
            count = fleet_timeout_counts.get(fleet_id, 0) + 1
            fleet_timeout_counts[fleet_id] = count
            if count >= 2:
                unavailable_fleets.add(fleet_id)

        required = str(required_fleet_id or "").strip()
        fleet_ids_to_scan = (
            [required]
            if include_page_details and required and required in slot.fleet_ids
            else sorted(slot.fleet_ids)[:6]
            if include_page_details
            else []
        )
        for fleet_id in fleet_ids_to_scan:
            try:
                pages_response = await slot.client.call(
                    "Page.list",
                    {"fleetId": fleet_id},
                )
                self._replace_fleet_pages_from_list(
                    slot,
                    fleet_id=fleet_id,
                    pages_response=pages_response,
                )
                self._update_slot_registry_from_value(slot, pages_response)
            except Exception as exc:
                note_fleet_timeout(fleet_id, exc)
                slot.sync_errors.append(f"Page.list({fleet_id}): {str(exc)[:240]}")

        eligible_page_ids = [
            page_id
            for page_id in sorted(slot.page_registry.keys())
            if not required
            or str(
                (slot.page_registry.get(page_id) or {}).get("fleetId") or ""
            ).strip() == required
        ]
        # The scan is capped, and the cap used to be applied to an id-sorted
        # list. A slot holding more than the cap could therefore starve its
        # quarantined pages of the very Page.getState that decides whether they
        # are usable again or due for retirement, leaving them stuck forever on
        # a technicality of id ordering. Quarantined pages are also the ones
        # whose state we know the least about, so they go first: expired ones
        # (a retirement decision is pending) ahead of the rest.
        def _scan_rank(page_id: str) -> int:
            if page_id not in slot.page_quarantine:
                return 2
            return 0 if self._quarantine_expired(slot, page_id) else 1

        def _last_seen(page_id: str) -> float:
            # Within a rank, least-recently-rechecked first. Every scanned page
            # that is not retired gets re-marked, which refreshes
            # lastQuarantinedAt and sends it to the back — so consecutive syncs
            # rotate through the quarantine set instead of replaying the same
            # id-sorted prefix. Without this, more quarantined pages than the
            # cap would leave the tail permanently unexamined whenever the head
            # keeps failing to close.
            entry = slot.page_quarantine.get(page_id)
            if not isinstance(entry, dict):
                return 0.0
            stamp = entry.get("lastQuarantinedAt") or entry.get("quarantinedAt")
            return float(stamp) if isinstance(stamp, (int, float)) else 0.0

        page_ids_to_scan = sorted(
            eligible_page_ids,
            key=lambda pid: (_scan_rank(pid), _last_seen(pid), pid),
        )[:12] if include_page_details else []
        for page_id in page_ids_to_scan:
            page_fleet_id = str(
                (slot.page_registry.get(page_id) or {}).get("fleetId") or ""
            ).strip()
            if page_fleet_id and page_fleet_id in unavailable_fleets:
                continue
            try:
                state_response = await slot.client.call(
                    "Page.getState",
                    {
                        "pageId": page_id,
                        "purpose": (
                            "Synchronize reusable slot page state before assigning"
                            f" worker {worker_id}."
                        ),
                    },
                )
                state_data = (
                    state_response.get("data")
                    if isinstance(state_response, dict)
                    and isinstance(state_response.get("data"), dict)
                    else {}
                )
                self._update_slot_registry_from_value(
                    slot,
                    {"pageId": page_id, **state_data, "state": state_response},
                )
                if _state_response_indicates_paused(state_response):
                    # This getState IS the TTL re-check: the sync already asks
                    # the authoritative question every pass, so retirement
                    # needs no extra probe — only the verdict plus the age of
                    # the quarantine.
                    retired = False
                    if self._quarantine_expired(slot, page_id):
                        retired = await self._retire_expired_quarantined_page(
                            slot,
                            page_id,
                            reason="Page.getState still reports paused after the quarantine TTL.",
                            verdict="still_paused",
                        )
                    if not retired:
                        self._mark_page_quarantined(
                            slot,
                            page_id,
                            reason="Page.getState reports the page is still paused.",
                            worker_id=worker_id,
                            status="paused",
                            recheck_verdict=True,
                        )
                else:
                    self._clear_page_quarantine(
                        slot,
                        page_id,
                        reason="Page.getState confirmed the page is usable.",
                    )
                    self._mark_page_fresh(slot, page_id)
            except Exception as exc:
                if page_fleet_id:
                    note_fleet_timeout(page_fleet_id, exc)
                error_text = str(exc)[:240]
                quarantine = slot.page_quarantine.get(page_id)
                was_quarantined = isinstance(quarantine, dict)
                # Two independent questions. The paused-error text decides
                # whether a page enters quarantine in the first place; whether
                # a page is ALREADY quarantined decides whether this pass is a
                # failed re-check. Gating the re-check on the error text too
                # would mean a quarantined page whose Page.getState keeps
                # timing out (a plain transport error, no "paused" anywhere in
                # it) never accrues a failure and so never reaches retirement —
                # the exact indefinite quarantine the TTL exists to end.
                if was_quarantined or _text_indicates_paused_error(error_text):
                    # The re-check produced no verdict — only a bounded
                    # tolerance for verdict-less passes before an already
                    # expired quarantine is retired anyway.
                    retired = False
                    if was_quarantined and self._quarantine_expired(slot, page_id):
                        failures = (optional_int(
                            quarantine.get("recheckFailures"), 0
                        ) or 0) + 1
                        quarantine["recheckFailures"] = failures
                        max_failures = int(getattr(
                            self.runtime.harness,
                            "page_quarantine_recheck_max_failures",
                            2,
                        ))
                        if failures > max_failures:
                            retired = await self._retire_expired_quarantined_page(
                                slot,
                                page_id,
                                reason=error_text,
                                verdict="recheck_failed",
                            )
                    if not retired:
                        self._mark_page_quarantined(
                            slot,
                            page_id,
                            reason=error_text,
                            worker_id=worker_id,
                            # A failed re-check is not evidence about WHY the
                            # page was quarantined, so it must not relabel it.
                            status=str(
                                quarantine.get("status") or "paused"
                            ) if was_quarantined else "paused",
                        )
                else:
                    page = dict(slot.page_registry.get(page_id) or {"pageId": page_id})
                    page["status"] = "stale"
                    page["lastStateError"] = error_text
                    slot.page_registry[page_id] = page
                slot.sync_errors.append(f"Page.getState({page_id}): {str(exc)[:240]}")
        slot.last_sync_at = time.monotonic()
        if slot.sync_errors:
            self.logger.write(
                "spawner.slot.sync_warning",
                {
                    "slotId": slot.slot_id,
                    "workerId": worker_id,
                    "errors": slot.sync_errors[-5:],
                    "fleetTimeoutCounts": fleet_timeout_counts,
                    "unavailableFleetIds": sorted(unavailable_fleets),
                },
            )
        if required and required in unavailable_fleets:
            raise ABCPTransportError(
                f"-32012 Fleet open timeout for required fleet {required}; "
                "retry the same phase id after the acquisition cooldown"
            )

    def _render_slot_context(
        self,
        slot: BrowserAgentSlot,
        *,
        expose_reusable_pages: bool,
        assignment: Optional[FleetAssignment] = None,
    ) -> str:
        payload = self._slot_context_summary(
            slot,
            expose_reusable_pages=expose_reusable_pages,
            assignment=assignment,
        )
        payload["reuseRules"] = [
            (
                "A live worker holds a persistent page lease. Another worker's"
                " call to that page is rejected with page_busy; different pages"
                " in the assigned fleet may run concurrently."
                if getattr(
                    self.runtime.harness,
                    "same_fleet_multiworker_enabled",
                    False,
                )
                else "Do not issue concurrent calls targeting the same page."
            ),
            "During login/CAPTCHA resolution the fleet-wide auth barrier pauses every non-resolver worker.",
            "After any Page.switchTo, Page.create, Page.navigate, Page.reload, or Page.go, refresh Page.getState and DOM.getAXTree before targeting elements.",
        ]
        if assignment is not None:
            payload["reuseRules"].extend([
                (
                    "Every Page.create must explicitly use assignedFleetId="
                    f"{assignment.fleet_id}. Fresh page does not mean fresh fleet."
                ),
                (
                    "Do not call Fleet.create/Fleet.close and do not fabricate"
                    " or substitute another fleetId; fleet routing and lifecycle"
                    " are coordinator/Dispatcher-owned."
                ),
            ])
        if expose_reusable_pages:
            payload["reuseRules"].extend([
                (
                    "This is an explicit continuation. Existing pageIds are"
                    " candidates only; verify with Page.getState/Page.switchTo"
                    " before acting."
                ),
                (
                    "Use an existing page only when it clearly belongs to this"
                    " continuation; otherwise create a fresh page."
                ),
            ])
        else:
            payload["reuseRules"].extend([
                (
                    "This assignment reuses only the browser connection. Begin"
                    " on a fresh page; Page.list may also reveal a result tab"
                    " opened by your action. A same-fleet row is usable only"
                    " when claimable=true and quarantined=false."
                ),
                (
                    "Use Page.create for a fresh task page. If an action opens a"
                    " new tab, call Page.list and address its pageId on first use;"
                    " the harness atomically claims it."
                ),
            ])
        return (
            "<slot_context>\n"
            f"{json.dumps(payload, ensure_ascii=False, indent=2, default=str)}\n"
            "</slot_context>"
        )

    def _page_bindings_for_worker(
        self,
        slot: BrowserAgentSlot,
        *,
        assignment: Optional[FleetAssignment],
        expose_reusable_pages: bool,
    ) -> Dict[str, str]:
        """Return existing page handles explicitly delegated to this worker."""

        if assignment is None or not expose_reusable_pages:
            return {}
        allowed_fleets = set(assignment.allowed_fleet_ids)
        pinned_page_id = (
            self.pinned_browser_context.page_id
            if self.pinned_browser_context is not None
            else ""
        )
        resume_page_id = (
            self.resume_browser_hint.page_id
            if (
                self.resume_browser_hint is not None
                and assignment.assignment_reason == "resume_browser_hint"
                and assignment.page_policy == "existing"
            )
            else ""
        )
        return {
            str(page_id): str(page.get("fleetId") or "")
            for page_id, page in slot.page_registry.items()
            if (
                str(page_id).strip()
                and str(page.get("fleetId") or "") in allowed_fleets
                and (not pinned_page_id or str(page_id) == pinned_page_id)
                and (not resume_page_id or str(page_id) == resume_page_id)
                and not _page_hidden_from_reuse(slot, page)
                and not self.page_lease_manager.owner_for(str(page_id))
            )
        }

    def _replace_fleet_pages_from_list(
        self,
        slot: BrowserAgentSlot,
        *,
        fleet_id: str,
        pages_response: Any,
    ) -> None:
        page_items = self._extract_page_items(pages_response)
        if page_items is None:
            return
        current_page_ids = {
            str(item.get("pageId") or item.get("page_id") or "")
            for item in page_items
            if isinstance(item, dict)
        }
        current_page_ids.discard("")
        removed_page_ids = {
            page_id
            for page_id, page in slot.page_registry.items()
            if (
                str(page.get("fleetId") or "") == fleet_id
                and page_id not in current_page_ids
            )
        }
        slot.page_registry = {
            page_id: page
            for page_id, page in slot.page_registry.items()
            if (
                str(page.get("fleetId") or "") != fleet_id
                or page_id in current_page_ids
            )
        }
        for page_id in removed_page_ids:
            slot.page_quarantine.pop(page_id, None)
        for item in page_items:
            if not isinstance(item, dict):
                continue
            page_id = str(item.get("pageId") or item.get("page_id") or "")
            if not page_id:
                continue
            page = dict(slot.page_registry.get(page_id) or {})
            self._clear_stale_fields(page)
            page.update(item)
            self._clear_stale_fields(page)
            page["pageId"] = page_id
            page["fleetId"] = str(page.get("fleetId") or fleet_id)
            self._apply_page_quarantine(slot, page_id, page)
            slot.page_registry[page_id] = page

    def _mark_page_fresh(self, slot: BrowserAgentSlot, page_id: str) -> None:
        page = slot.page_registry.get(page_id)
        if isinstance(page, dict):
            self._clear_stale_fields(page)

    def _clear_stale_fields(self, page: JsonDict) -> None:
        page.pop("lastStateError", None)
        if str(page.get("status") or "") == "stale":
            page.pop("status", None)

    def _mark_page_quarantined(
        self,
        slot: BrowserAgentSlot,
        page_id: str,
        *,
        reason: str,
        worker_id: str = "",
        phase_id: Optional[str] = None,
        status: str = "stale_pause_deadlock",
        recheck_verdict: bool = False,
    ) -> None:
        page_id = str(page_id or "").strip()
        if not page_id:
            return
        now = time.time()
        existing = slot.page_quarantine.get(page_id)
        existing = existing if isinstance(existing, dict) else {}
        # `quarantinedAt` is the FIRST time this page was quarantined, and it
        # deliberately survives re-marking. The registry sync re-runs this call
        # on every pass for as long as Page.getState keeps reporting `paused`,
        # so refreshing the timestamp here would hold the TTL permanently in
        # the future and the retirement path below could never fire — exactly
        # the leak it exists to close. `lastQuarantinedAt` carries the
        # per-observation time for anyone who needs it.
        first_at = existing.get("quarantinedAt")
        quarantine = {
            "pageId": page_id,
            "status": status or "quarantined",
            "reason": str(reason or "")[:300],
            "workerId": str(worker_id or "")[:120],
            "phaseId": str(phase_id or "")[:120],
            "quarantinedAt": (
                float(first_at) if isinstance(first_at, (int, float)) else now
            ),
            "lastQuarantinedAt": now,
            # A pass that produced an authoritative verdict clears the
            # verdict-less streak: the tolerance exists for re-checks that
            # could not answer, not for answers we did not like.
            "recheckFailures": 0 if recheck_verdict else (
                optional_int(existing.get("recheckFailures"), 0) or 0
            ),
            "doNotUse": True,
        }
        slot.page_quarantine[page_id] = quarantine
        self.page_lease_manager.quarantine_page(page_id)
        page = dict(slot.page_registry.get(page_id) or {"pageId": page_id})
        self._apply_page_quarantine(slot, page_id, page)
        slot.page_registry[page_id] = page
        self.logger.write(
            "spawner.slot.page_quarantined",
            {
                "slotId": slot.slot_id,
                "pageId": page_id,
                "reason": quarantine["reason"],
                "status": quarantine["status"],
                "workerId": quarantine["workerId"],
                "phaseId": quarantine["phaseId"],
            },
        )

    def _quarantine_expired(self, slot: BrowserAgentSlot, page_id: str) -> bool:
        """Has this page been quarantined longer than the configured TTL?"""
        ttl = float(
            getattr(self.runtime.harness, "page_quarantine_ttl_seconds", 300.0)
            or 0.0
        )
        if ttl <= 0.0:
            return False
        quarantine = slot.page_quarantine.get(page_id)
        if not isinstance(quarantine, dict):
            return False
        first_at = quarantine.get("quarantinedAt")
        if not isinstance(first_at, (int, float)):
            return False
        return (time.time() - float(first_at)) > ttl

    async def _retire_expired_quarantined_page(
        self,
        slot: BrowserAgentSlot,
        page_id: str,
        *,
        reason: str,
        verdict: str,
    ) -> bool:
        """Close a page whose quarantine outlived the TTL. True if retired.

        Only reached after a re-check already said the page is still unusable,
        so this is not a timer guessing that the challenge expired — closing is
        the same remedy `_close_deadlocked_page` applies when the platform will
        not clear a pause flag: give up on this page so a fresh one can be
        created, rather than leaving it open and assignable to nobody.

        Clearing the quarantine entry is safe precisely BECAUSE the page is
        gone: the entry exists to keep workers off a live-but-unusable page.
        If the close fails the page still exists, so the quarantine stays.
        """
        closed = False
        close_error = ""
        client = getattr(slot, "client", None)
        if client is not None:
            try:
                close_response = await client.call("Page.close", {
                    "pageId": page_id,
                    "purpose": (
                        "Retire a page whose quarantine outlived its TTL so the"
                        " fleet can reclaim the slot."
                    ),
                })
                # "Did not raise" is NOT "closed", and neither is "the call
                # succeeded". ABCPClient only raises on a JSON-RPC
                # {error:{...}} envelope, so a domain-level failure arrives as
                # an ordinary response with a negative observation; and a
                # generically-successful envelope still says nothing about THIS
                # page — `classify_call_outcome` alone accepts an observation-
                # only failure, and even a receipt announcing that some OTHER
                # page was closed. Discharging inventory needs the registered
                # Page.close evidence (data.closed is True AND data.pageId
                # matches), which is exactly what the grant layer encodes.
                # Getting this wrong is fail-open: the quarantine would be
                # cleared on a page that is still open and still unusable,
                # strictly worse than the leak this method exists to close.
                decision = evaluate_grant(
                    kind="inventory_discharge_page_close",
                    method="Page.close",
                    result={"response": close_response},
                    page_id=page_id,
                )
                closed = decision.allowed
                if not closed:
                    close_error = str(
                        decision.reason or "close not acknowledged"
                    )[:240]
            except Exception as exc:  # noqa: BLE001 - retirement is best effort
                close_error = str(exc)[:240]
        self.logger.write("spawner.slot.page_quarantine_retired", {
            "slotId": slot.slot_id,
            "pageId": page_id,
            "verdict": verdict,
            "reason": str(reason or "")[:300],
            "closed": closed,
            "error": close_error or None,
            "ttlSeconds": float(
                getattr(self.runtime.harness, "page_quarantine_ttl_seconds", 300.0)
            ),
        })
        if not closed:
            return False
        slot.page_registry.pop(page_id, None)
        self._clear_page_quarantine(
            slot,
            page_id,
            reason="Page retired after its quarantine outlived the TTL.",
        )
        return True

    def _clear_page_quarantine(
        self,
        slot: BrowserAgentSlot,
        page_id: str,
        *,
        reason: str = "",
    ) -> None:
        page_id = str(page_id or "").strip()
        if not page_id or page_id not in slot.page_quarantine:
            return
        slot.page_quarantine.pop(page_id, None)
        self.page_lease_manager.clear_page_quarantine(page_id)
        page = slot.page_registry.get(page_id)
        if isinstance(page, dict):
            page.pop("quarantineReason", None)
            page.pop("quarantineStatus", None)
            page.pop("quarantinedAt", None)
            page.pop("doNotUse", None)
            if str(page.get("status") or "") == "quarantined":
                page.pop("status", None)
        self.logger.write(
            "spawner.slot.page_quarantine_cleared",
            {
                "slotId": slot.slot_id,
                "pageId": page_id,
                "reason": str(reason or "")[:300],
            },
        )

    def _apply_page_quarantine(
        self,
        slot: BrowserAgentSlot,
        page_id: str,
        page: JsonDict,
    ) -> None:
        quarantine = slot.page_quarantine.get(page_id)
        if not isinstance(quarantine, dict):
            return
        page["status"] = "quarantined"
        page["quarantineStatus"] = quarantine.get("status") or "quarantined"
        page["quarantineReason"] = quarantine.get("reason") or ""
        page["quarantinedAt"] = quarantine.get("quarantinedAt")
        page["doNotUse"] = True

    def _extract_page_items(self, value: Any) -> Optional[List[JsonDict]]:
        return self._extract_collection_items(
            value,
            collection_keys=("pages", "tabs"),
            id_keys=("pageId", "page_id"),
        )

    def _extract_fleet_items(self, value: Any) -> Optional[List[JsonDict]]:
        return self._extract_collection_items(
            value,
            collection_keys=("fleets",),
            id_keys=("fleetId", "fleet_id"),
        )

    def _extract_collection_items(
        self,
        value: Any,
        *,
        collection_keys: tuple[str, ...],
        id_keys: tuple[str, ...],
    ) -> Optional[List[JsonDict]]:
        def normalize_list(items: Any) -> Optional[List[JsonDict]]:
            if not isinstance(items, list):
                return None
            records = [item for item in items if isinstance(item, dict)]
            if not records:
                return []
            if any(any(item.get(key) for key in id_keys) for item in records):
                return records
            return None

        if isinstance(value, dict):
            for key in collection_keys:
                records = normalize_list(value.get(key))
                if records is not None:
                    return records
            data = value.get("data")
            if isinstance(data, dict):
                for key in collection_keys:
                    records = normalize_list(data.get(key))
                    if records is not None:
                        return records
            elif isinstance(data, list):
                records = normalize_list(data)
                if records is not None:
                    return records
        return normalize_list(value)

    def _replace_slot_fleets_from_response(
        self,
        slot: BrowserAgentSlot,
        response: Any,
    ) -> bool:
        """Converge slot inventory when a response carries an owner fleet list."""

        fleet_items = self._extract_fleet_items(response)
        if fleet_items is None:
            return False
        observed_fleet_ids = {
            str(item.get("fleetId") or item.get("fleet_id") or "").strip()
            for item in fleet_items
            if isinstance(item, dict)
        }
        observed_fleet_ids.discard("")
        removed_fleet_ids = slot.fleet_ids.difference(observed_fleet_ids)
        slot.fleet_ids = observed_fleet_ids
        if removed_fleet_ids:
            removed_page_ids = {
                page_id
                for page_id, page in slot.page_registry.items()
                if str(page.get("fleetId") or "") in removed_fleet_ids
            }
            for page_id in removed_page_ids:
                slot.page_registry.pop(page_id, None)
                slot.page_quarantine.pop(page_id, None)
        return True

    def _update_slot_after_worker(
        self,
        slot: BrowserAgentSlot,
        *,
        worker_id: str,
        phase_id: Optional[str],
        worker_contract: JsonDict,
        result: JsonDict,
        trace: List[JsonDict],
    ) -> None:
        self._record_slot_result(
            slot,
            worker_id=worker_id,
            phase_id=phase_id,
            worker_contract=worker_contract,
            result=result,
            trace=trace,
        )
        self._mark_slot_idle(slot, worker_id=worker_id)

    def _record_slot_result(
        self,
        slot: BrowserAgentSlot,
        *,
        worker_id: str,
        phase_id: Optional[str],
        worker_contract: JsonDict,
        result: JsonDict,
        trace: Optional[List[JsonDict]] = None,
    ) -> None:
        if trace is not None:
            self._update_slot_registry_from_trace(slot, trace)
        slot.last_phase_id = phase_id
        slot.last_task_type = str(worker_contract.get("task_type") or "")
        slot.last_result_summary = {
            "workerId": worker_id,
            "phaseId": phase_id,
            "status": result.get("status"),
            "statusCategory": result.get("statusCategory"),
            "validatedStatus": result.get("validatedStatus"),
            "artifactCount": len(result.get("artifacts") or []),
            "traceSummary": trim_large_strings(result.get("traceSummary") or {}, 2000),
        }
        self._quarantine_deadlock_page_from_result(
            slot,
            worker_id=worker_id,
            phase_id=phase_id,
            result=result,
        )
        self._observe_slot_fleets(slot)
        self.fleet_coordinator.touch_worker(worker_id)
        assignment = self.fleet_coordinator.assignment_for_worker(worker_id)
        if assignment is not None:
            try:
                task_pages = self._task_browser_page_ids.setdefault(
                    assignment.fleet_id, set()
                )
                for item in trace or []:
                    if not isinstance(item, dict) or item.get("type") != "browser_call":
                        continue
                    method = str(item.get("method") or "")
                    page_id = str(extract_page_id_from_values(
                        item.get("params"), item.get("result")
                    ) or "").strip()
                    if not page_id:
                        continue
                    if method == "Page.close":
                        task_pages.discard(page_id)
                        continue
                    if method == "Page.list":
                        continue
                    page = slot.page_registry.get(page_id)
                    if (
                        isinstance(page, dict)
                        and str(page.get("fleetId") or "")
                        == assignment.fleet_id
                    ):
                        task_pages.add(page_id)
                self._persist_task_browser_context(
                    slot,
                    assignment,
                    phase_id=phase_id,
                )
            except Exception as exc:
                self.logger.write(
                    "spawner.browser_context.persist_failed",
                    {
                        "workerId": worker_id,
                        "fleetId": assignment.fleet_id,
                        "stage": "worker_result",
                        "error": str(exc)[:500],
                    },
                )

    def _quarantine_deadlock_page_from_result(
        self,
        slot: BrowserAgentSlot,
        *,
        worker_id: str,
        phase_id: Optional[str],
        result: JsonDict,
    ) -> None:
        if str(result.get("status") or "") != "stale_pause_deadlock":
            return
        diagnostics = result.get("diagnostics")
        page_id = ""
        if isinstance(diagnostics, dict):
            page_id = str(diagnostics.get("last_pause_pageId") or "").strip()
        if not page_id:
            return
        self._mark_page_quarantined(
            slot,
            page_id,
            reason=(
                "Worker ended with stale_pause_deadlock; do not reuse this"
                " paused page unless a later Page.getState confirms it is usable."
            ),
            worker_id=worker_id,
            phase_id=phase_id,
            status="stale_pause_deadlock",
        )

    def _mark_slot_idle(
        self,
        slot: BrowserAgentSlot,
        *,
        worker_id: str,
    ) -> None:
        self._release_slot_to_pool(
            slot,
            worker_id=worker_id,
            event="spawner.slot.released",
            remember_worker=True,
        )

    def _update_slot_registry_from_trace(
        self,
        slot: BrowserAgentSlot,
        trace: List[JsonDict],
    ) -> None:
        for item in trace or []:
            if not isinstance(item, dict) or item.get("type") != "browser_call":
                continue
            method = str(item.get("method") or "")
            params = item.get("params")
            if method == "Page.close" and isinstance(params, dict):
                page_id = str(params.get("pageId") or "")
                if page_id:
                    slot.page_registry.pop(page_id, None)
                    slot.page_quarantine.pop(page_id, None)
            if method == "Fleet.close" and isinstance(params, dict):
                fleet_id = str(params.get("fleetId") or "")
                if fleet_id:
                    slot.fleet_ids.discard(fleet_id)
                    removed_page_ids = [
                        page_id
                        for page_id, page in slot.page_registry.items()
                        if str(page.get("fleetId") or "") == fleet_id
                    ]
                    slot.page_registry = {
                        page_id: page
                        for page_id, page in slot.page_registry.items()
                        if str(page.get("fleetId") or "") != fleet_id
                    }
                    for page_id in removed_page_ids:
                        slot.page_quarantine.pop(page_id, None)
            self._update_slot_registry_from_value(slot, params)
            self._update_slot_registry_from_value(slot, item.get("result"))

    def _update_slot_registry_from_value(
        self,
        slot: BrowserAgentSlot,
        value: Any,
    ) -> None:
        def as_id(raw: Any) -> str:
            # Real fleet/page ids are string scalars (UUIDs). A dict/list value
            # here is a JSON-schema fragment echoed inside an attached
            # methodSchema.params (e.g. {"pageId": {"type":"string","pattern":...}}
            # from describeAction). str()-coercing it would poison page_registry
            # / fleet_ids with a bogus key that later makes Page.getState /
            # Page.list fail with -32602. Only accept real string ids.
            return raw if isinstance(raw, str) and raw else ""

        for item in handle_records_from_value(value):
            fleet_id = as_id(item.get("fleetId") or item.get("fleet_id"))
            page_id = as_id(item.get("pageId") or item.get("page_id"))
            url = str(item.get("url") or item.get("currentUrl") or "")
            title = str(item.get("title") or "")
            status = str(item.get("status") or "")
            if fleet_id:
                slot.fleet_ids.add(fleet_id)
            if page_id:
                page = dict(slot.page_registry.get(page_id) or {})
                page["pageId"] = page_id
                if fleet_id:
                    page["fleetId"] = fleet_id
                if url:
                    page["url"] = url
                    origin = _origin_from_url(url)
                    if origin:
                        page["origin"] = origin
                        slot.origins.add(origin)
                if title:
                    page["title"] = title
                if status:
                    page["status"] = status
                self._apply_page_quarantine(slot, page_id, page)
                slot.page_registry[page_id] = page

    def _slot_summary(self, slot: BrowserAgentSlot) -> JsonDict:
        return {
            "slotId": slot.slot_id,
            "agentId": slot.agent_id,
            "status": slot.status,
            "currentWorkerId": slot.current_worker_id,
            "lastWorkerId": slot.last_worker_id,
            "lastPhaseId": slot.last_phase_id,
            "lastTaskType": slot.last_task_type,
            "fleetIds": sorted(slot.fleet_ids),
            "origins": sorted(slot.origins),
            "pages": [
                dict(page)
                for page in list(slot.page_registry.values())[:20]
            ],
            "quarantinedPages": [
                dict(page)
                for page in list(slot.page_quarantine.values())[:20]
            ],
            "syncErrors": slot.sync_errors[-5:],
            "lastResult": slot.last_result_summary,
            "fleetRouting": self.fleet_coordinator.slot_snapshot(slot.slot_id),
        }

    def _slot_context_summary(
        self,
        slot: BrowserAgentSlot,
        *,
        expose_reusable_pages: bool,
        assignment: Optional[FleetAssignment] = None,
    ) -> JsonDict:
        payload = {
            "slotId": slot.slot_id,
            "agentId": slot.agent_id,
            "status": slot.status,
            "lastWorkerId": slot.last_worker_id,
            "lastPhaseId": slot.last_phase_id,
            "lastTaskType": slot.last_task_type,
            "pageReuseMode": (
                "explicit_continuation" if expose_reusable_pages else "fresh_page_required"
            ),
            "existingPageCount": len(slot.page_registry),
            "quarantinedPageCount": len(slot.page_quarantine),
            "fleetCount": len(slot.fleet_ids),
            "originCount": len(slot.origins),
            "syncErrors": slot.sync_errors[-5:],
            "isolation": (
                "Only browser connection and page registry are reused. Worker"
                " AXTree snapshots, diagnostics, progress, artifacts, and challenge"
                " state are reset for this assignment."
            ),
        }
        if assignment is not None:
            payload["fleetAssignment"] = assignment.to_dict()
            payload["assignedFleetId"] = assignment.fleet_id
            payload["allowedFleetIds"] = list(assignment.allowed_fleet_ids)
            payload["pageReuseMode"] = (
                "explicit_page_continuation"
                if expose_reusable_pages
                else "fresh_page_same_fleet"
            )
        if not expose_reusable_pages:
            return payload
        allowed_fleet_ids = (
            set(assignment.allowed_fleet_ids)
            if assignment is not None
            else {
                *slot.fleet_ids,
                *{
                    str(page.get("fleetId") or "")
                    for page in slot.page_registry.values()
                    if str(page.get("fleetId") or "")
                },
            }
        )
        payload["fleetIds"] = sorted(allowed_fleet_ids)
        payload["pages"] = [
            dict(page)
            for page in list(slot.page_registry.values())[:20]
            if (
                str(page.get("fleetId") or "") in allowed_fleet_ids
                and (
                    self.pinned_browser_context is None
                    or not self.pinned_browser_context.page_id
                    or str(page.get("pageId") or "")
                    == self.pinned_browser_context.page_id
                )
                and not _page_hidden_from_reuse(slot, page)
            )
        ]
        payload["origins"] = (
            sorted({
                str(page.get("origin") or _origin_from_url(page.get("url") or ""))
                for page in payload["pages"]
                if str(page.get("origin") or _origin_from_url(page.get("url") or ""))
            })
            if assignment is not None
            else sorted(slot.origins)
        )
        payload["stalePages"] = [
            {
                "pageId": page.get("pageId"),
                "fleetId": page.get("fleetId"),
                "url": page.get("url"),
                "lastStateError": page.get("lastStateError"),
            }
            for page in list(slot.page_registry.values())[:20]
            if (
                str(page.get("fleetId") or "") in allowed_fleet_ids
                and str(page.get("status") or "") == "stale"
            )
        ]
        payload["quarantinedPages"] = [
            {
                "pageId": quarantine.get("pageId"),
                "status": quarantine.get("status"),
                "reason": quarantine.get("reason"),
                "workerId": quarantine.get("workerId"),
                "phaseId": quarantine.get("phaseId"),
                "doNotUse": True,
            }
            for quarantine in list(slot.page_quarantine.values())[:20]
            if str(
                (slot.page_registry.get(str(quarantine.get("pageId") or "")) or {}).get(
                    "fleetId"
                )
                or ""
            ) in allowed_fleet_ids
        ]
        return payload
