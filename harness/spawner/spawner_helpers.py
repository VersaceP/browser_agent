"""
harness.spawner.spawner_helpers - Module-level helpers, dataclasses and client wrappers.
"""

import asyncio
import re
import uuid
from dataclasses import dataclass
from dataclasses import field
from typing import Any
from typing import Callable
from typing import Dict
from typing import List
from typing import Optional
from typing import Set
from abcp_client import ABCPClient
from abcp_client import ABCPTransportError
from harness.fleet.runtime import PageLeasedBrowserClient
from runtime_config import RuntimeConfig
from harness.utils import JsonDict
from harness.utils import RunLogger
from harness.utils import optional_int

def _sp():
    import harness.spawner as sp

    return sp

BrowserAgentFactory = Callable[[Any, ABCPClient, RuntimeConfig, RunLogger], Any]

class FleetReadinessError(ABCPTransportError):
    """Assigned Fleet did not become usable before worker construction."""

    # Readiness has already spent its bounded Fleet.ready event wait.
    # Re-entering the same acquisition path immediately only repeats Fleet
    # startup/restore pressure, so reuse the existing acquisition ledger's
    # cooldown. Keep the duration authoritative in task_control rather than
    # duplicating it here.
    requires_spawn_acquisition_cooldown = True

    def __init__(self, message: str, *, fleet_id: str, owner_slot_id: str):
        super().__init__(message)
        self.fleet_id = str(fleet_id)
        self.owner_slot_id = str(owner_slot_id)

def _is_fleet_open_timeout(exc: BaseException) -> bool:
    text = str(exc or "").lower()
    return "-32012" in text and "fleet open timeout" in text

def _fresh_click_settlement_class(
    agent: Any,
    method: str,
    params: JsonDict,
) -> str:
    """Classify only a current canonical AX target; never read model purpose.

    Unknown selectors/coordinates and stale snapshots keep the conservative
    settlement window. A current non-link role may use the short window, while
    links retain the full popup allowance.
    """

    if agent is None or method != "Input.click":
        return "conservative"
    page_id = str(params.get("pageId") or "").strip()
    target_id = str(params.get("id") or "").strip()
    if (
        not page_id
        or not target_id
        or bool(getattr(agent, "axtree_invalidated", True))
        or str(getattr(agent, "axtree_page_id", "") or "") != page_id
    ):
        return "conservative"
    current_ids = set(getattr(agent, "axtree_ids", set()) or set())
    if target_id not in current_ids:
        return "conservative"
    from harness.tools.browser_tools.axtree_state import _current_axtree_nodes

    nodes = _current_axtree_nodes(agent)
    for node in nodes:
        if not isinstance(node, dict):
            continue
        if str(node.get("id") or "") != target_id:
            continue
        role = str(node.get("role") or "").strip().lower()
        if not role:
            return "conservative"
        return "fresh_link" if role == "link" else "fresh_non_link"
    return "conservative"

async def _verified_workflow_hitl_settlement(
    agent: Any,
    page_id: str,
) -> JsonDict:
    """Run the existing verified barrier-open path after opaque Workflow HITL."""

    if agent is None:
        return {
            "enabled": True,
            "opened": False,
            "reason": "browser_agent_unavailable",
        }
    # Local import avoids making spawner/browser_tools initialization cyclic.
    from harness.tools.browser_tools import (
        _verify_and_open_fleet_auth_barrier,
    )

    return await _verify_and_open_fleet_auth_barrier(
        agent,
        str(page_id or ""),
        0,
    )

def _prompt_worker_contract(worker_contract: Any) -> JsonDict:
    """Return the contract view exposed to the worker LLM.

    Top-level underscore-prefixed fields are harness-private provenance/state.
    Keep them on ``harness.worker_contract`` while excluding them from prompt
    text so implementation details cannot influence the worker's decisions.
    """
    if not isinstance(worker_contract, dict):
        return {}
    return {
        key: value for key, value in worker_contract.items()
        if not str(key).startswith("_")
    }

@dataclass
class BrowserAgentHandle:
    worker_id: str
    agent_id: str
    name: str
    task: str
    context: str
    result_contract: str
    phase_id: Optional[str]
    worker_contract: JsonDict
    async_task: Any
    slot_id: Optional[str] = None

@dataclass
class BrowserAgentSlot:
    slot_id: str
    # Harness-local, stable routing identity.  It is never sent to WebCross as
    # an authentication claim.
    agent_id: str
    # Server-assigned identity for the current protocol session.  WebCross 0.9
    # derives this from the WebSocket connection and can change it on an
    # unauthenticated reconnect.
    protocol_agent_id: str = ""
    # Last server cursor delivered to this slot.  It is transport metadata,
    # separate from BrowserAgent's semantic event reducers.
    event_cursor: Optional[int] = None
    event_catalog_revision: str = ""
    client: Optional[ABCPClient] = None
    registration: JsonDict = field(default_factory=dict)
    status: str = "new"
    current_worker_id: Optional[str] = None
    last_worker_id: Optional[str] = None
    last_phase_id: Optional[str] = None
    last_contract_hash: str = ""
    last_result_summary: JsonDict = field(default_factory=dict)
    last_sync_at: float = 0.0
    fleet_ids: Set[str] = field(default_factory=set)
    page_registry: Dict[str, JsonDict] = field(default_factory=dict)
    page_quarantine: Dict[str, JsonDict] = field(default_factory=dict)
    origins: Set[str] = field(default_factory=set)
    sync_errors: List[str] = field(default_factory=list)
    recovery_failure_cycles: int = 0
    recovery_unavailable_since: float = 0.0
    idle_event_logger: Optional[Callable[[str, JsonDict], None]] = None
    # True only after this exact WebSocket connection completed the current
    # Dispatcher handshake (System.register -> System.getCapabilities).
    protocol_initialized: bool = False

@dataclass(frozen=True)
class PinnedBrowserContext:
    """Trusted task-level routing target supplied outside the Lead plan."""

    fleet_id: str
    page_id: str = ""
    source: str = "api"

    @classmethod
    def from_value(cls, value: Any) -> Optional["PinnedBrowserContext"]:
        if value in (None, {}, ""):
            return None
        if isinstance(value, cls):
            return value
        if not isinstance(value, dict):
            raise ValueError("pinned_browser_context must be an object")
        fleet_id = str(
            value.get("fleet_id") or value.get("fleetId") or ""
        ).strip()
        page_id = str(
            value.get("page_id") or value.get("pageId") or ""
        ).strip()
        source = str(value.get("source") or "api").strip() or "api"
        if not fleet_id:
            raise ValueError("pinned_browser_context.fleet_id is required")
        for label, raw in (("fleet_id", fleet_id), ("page_id", page_id)):
            if not raw:
                continue
            try:
                uuid.UUID(raw)
            except (ValueError, AttributeError) as exc:
                raise ValueError(
                    f"pinned_browser_context.{label} must be a UUID"
                ) from exc
        return cls(fleet_id=fleet_id, page_id=page_id, source=source)

    def to_dict(self) -> JsonDict:
        return {
            "fleetId": self.fleet_id,
            "pageId": self.page_id or None,
            "source": self.source,
            "mode": "existing_only",
        }


@dataclass(frozen=True)
class TaskSessionBinding:
    """Task-local Fleet or Page continuation constraint.

    It deliberately is not an auth-ledger record: verified authentication may
    preserve Fleet identity, while an unfinished page-local form may preserve
    one exact Page. Neither case grants cross-task login reuse rights.
    """

    phase_id: str
    fleet_id: str
    page_id: str = ""
    # The session_key the bound Fleet was reserved under, when it had one.
    #
    # Without it the binding is unusable for the thing it exists to permit. A
    # continuation that names the key is refused ("omit session_key so the
    # exact bound Fleet/Page can continue"), and one that omits it reaches the
    # coordinator as an unnamed request for a named fleet and is refused again
    # ("fleet ... is already bound to another session_key"). Task 69cab1c4 rode
    # that loop for 38 Lead steps and ended blocked with the page still open.
    session_key: str = ""
    session_generation: int = 0
    source: str = "hitl_resume"
    binding_scope: str = "page"
    state: str = "active"
    reason: str = ""
    created_at_ms: int = 0
    last_verified_at_ms: int = 0
    auth_verified: bool = False

    @classmethod
    def from_value(
        cls,
        value: Any,
        *,
        phase_id: str = "",
    ) -> Optional["TaskSessionBinding"]:
        if value in (None, {}, ""):
            return None
        if isinstance(value, cls):
            return value
        if not isinstance(value, dict):
            return None
        resolved_phase_id = str(
            phase_id or value.get("phaseId") or value.get("phase_id") or ""
        ).strip()
        fleet_id = str(value.get("fleetId") or value.get("fleet_id") or "").strip()
        page_id = str(value.get("pageId") or value.get("page_id") or "").strip()
        binding_scope = str(
            value.get("bindingScope")
            or value.get("binding_scope")
            or "page"
        ).strip().lower()
        if binding_scope not in {"fleet", "page"}:
            return None
        if not resolved_phase_id or not fleet_id:
            return None
        if binding_scope == "page" and not page_id:
            return None
        try:
            uuid.UUID(fleet_id)
            if page_id:
                uuid.UUID(page_id)
        except (ValueError, AttributeError):
            return None
        generation = optional_int(
            value.get("sessionGeneration", value.get("session_generation")), 0
        ) or 0
        return cls(
            phase_id=resolved_phase_id,
            fleet_id=fleet_id,
            page_id=page_id,
            session_key=str(
                value.get("sessionKey") or value.get("session_key") or ""
            ).strip(),
            session_generation=max(0, generation),
            source=str(value.get("source") or "hitl_resume").strip() or "hitl_resume",
            binding_scope=binding_scope,
            state=(
                str(value.get("state") or "active").strip().lower()
                if str(value.get("state") or "active").strip().lower()
                in {"active", "needs_reverification", "stale"}
                else "needs_reverification"
            ),
            reason=str(value.get("reason") or "").strip(),
            created_at_ms=max(
                0, optional_int(value.get("createdAtMs", value.get("created_at_ms")), 0) or 0
            ),
            last_verified_at_ms=max(
                0,
                optional_int(
                    value.get("lastVerifiedAtMs", value.get("last_verified_at_ms")),
                    0,
                ) or 0,
            ),
            auth_verified=bool(
                value.get("authVerified", value.get("auth_verified", False))
            ),
        )

    @property
    def requires_exact_page(self) -> bool:
        return self.binding_scope == "page"

    def to_dict(self) -> JsonDict:
        return {
            "phaseId": self.phase_id,
            "fleetId": self.fleet_id,
            "pageId": self.page_id,
            "sessionKey": self.session_key or None,
            "sessionGeneration": self.session_generation,
            "source": self.source,
            "bindingScope": self.binding_scope,
            "state": self.state,
            "reason": self.reason or None,
            "createdAtMs": self.created_at_ms or None,
            "lastVerifiedAtMs": self.last_verified_at_ms or None,
            "authVerified": self.auth_verified,
            "continuity": "required",
            "scope": "task",
        }

@dataclass(frozen=True)
class ResumeBrowserHint:
    """Best-effort browser target recovered from this task's prior state.

    Unlike :class:`PinnedBrowserContext`, this is not a routing constraint. A
    missing or conflicting hint is ignored, and ordinary assignment continues.
    """

    fleet_id: str
    page_id: str = ""
    phase_id: str = ""
    source: str = "task_state"

    @classmethod
    def from_value(cls, value: Any) -> Optional["ResumeBrowserHint"]:
        if value in (None, {}, ""):
            return None
        if isinstance(value, cls):
            return value
        if not isinstance(value, dict):
            raise ValueError("resume_browser_hint must be an object")
        fleet_id = str(
            value.get("fleet_id") or value.get("fleetId") or ""
        ).strip()
        page_id = str(
            value.get("page_id") or value.get("pageId") or ""
        ).strip()
        phase_id = str(
            value.get("phase_id") or value.get("phaseId") or ""
        ).strip()
        source = str(value.get("source") or "task_state").strip() or "task_state"
        if not fleet_id:
            raise ValueError("resume_browser_hint.fleet_id is required")
        for label, raw in (("fleet_id", fleet_id), ("page_id", page_id)):
            if not raw:
                continue
            try:
                uuid.UUID(raw)
            except (ValueError, AttributeError) as exc:
                raise ValueError(
                    f"resume_browser_hint.{label} must be a UUID"
                ) from exc
        return cls(
            fleet_id=fleet_id,
            page_id=page_id,
            phase_id=phase_id,
            source=source,
        )

    def to_dict(self) -> JsonDict:
        return {
            "fleetId": self.fleet_id,
            "pageId": self.page_id or None,
            "phaseId": self.phase_id or None,
            "source": self.source,
            "mode": "best_effort",
        }

class _TaskContextTrackingBrowserClient(PageLeasedBrowserClient):
    """Observe successful browser calls before control returns to the worker."""

    def __init__(self, *args: Any, after_call: Optional[Callable[..., None]] = None,
                 **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._after_call = after_call

    async def call(self, method: str, params: Any = None) -> Any:
        result = await super().call(method, params)
        if self._after_call is not None:
            self._after_call(method, params, result)
        return result

@dataclass
class _SessionStartLock:
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    users: int = 0

URL_RE = re.compile(r"https?://[^\s\"'<>]+")

SLOT_FULL_SYNC_TTL_SECONDS = 30.0
