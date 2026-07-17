"""Shared runtime guards for multi-worker fleet reuse.

The Dispatcher routes browser actions by ``pageId``.  The harness therefore
serializes only calls targeting the same page while allowing different pages
in one fleet to make progress concurrently.  Authentication/challenge state is
different: cookies are fleet-wide, so ``FleetAuthBarrier`` gates the whole
fleet until one resolver has completed verified HITL recovery.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Dict


@dataclass
class _PageLockEntry:
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    users: int = 0


class PageLeaseManager:
    """Process-local keyed locks with bounded bookkeeping."""

    def __init__(self) -> None:
        self._entries: Dict[str, _PageLockEntry] = {}

    @asynccontextmanager
    async def lease(self, page_id: str) -> AsyncIterator[None]:
        key = str(page_id or "").strip()
        if not key:
            yield
            return
        entry = self._entries.get(key)
        if entry is None:
            entry = _PageLockEntry()
            self._entries[key] = entry
        entry.users += 1
        try:
            async with entry.lock:
                yield
        finally:
            entry.users -= 1
            if entry.users <= 0 and self._entries.get(key) is entry:
                self._entries.pop(key, None)


class PageLeasedBrowserClient:
    """ABCP client facade that applies the shared same-page lease to every RPC.

    This sits below BrowserAgent tools and Workflow fast paths, so no caller can
    accidentally bypass the page-level serialization rule.
    """

    def __init__(
        self,
        client: Any,
        leases: PageLeaseManager,
        *,
        fleet_owner_client: Any = None,
    ) -> None:
        self._client = client
        self._leases = leases
        self._fleet_owner_client = fleet_owner_client

    async def call(self, method: str, params: Any = None) -> Any:
        payload = params if isinstance(params, dict) else {}
        page_id = str(payload.get("pageId") or payload.get("page_id") or "")
        target = (
            self._fleet_owner_client
            if method == "Page.create" and self._fleet_owner_client is not None
            else self._client
        )
        async with self._leases.lease(page_id):
            return await target.call(method, params)

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
            return {
                "claimed": True,
                "resolverWorkerId": worker,
                "generation": state.generation,
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
