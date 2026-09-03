"""harness.observation.browser_reducers - Deterministic folds over ABCP events.

An asynchronous browser notification is allowed to change harness state. It is
not allowed to do so from wherever it happens to be handled: today that logic
sits in a long ``if name == ...`` chain inside the websocket callback, where it
cannot be tested without a client and cannot be replayed at all.

A reducer is a pure function of (state, event) -> (new state, transitions). It
publishes what changed; it does not decide what the model is told. That
separation is the point:

- a reducer MAY change harness state;
- a plain event subscriber may not;
- neither may claim that an event arriving during a tool call was caused by it.

The last rule has teeth in a fleet. Events are routed per page, and a page with
no owner is broadcast, so "arrived while worker A was clicking" routinely
describes worker B's page. Attribution therefore narrows by page first and only
then by time, and time overlap never graduates into causation.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from typing_extensions import Literal, Protocol, runtime_checkable

JsonDict = Dict[str, Any]

Attribution = Literal[
    "caused_by_execution",
    "correlated_tool_call",
    "observed_during_tool_call",
    "unattributed",
]


def digest(value: Any) -> str:
    """Short, stable hash. State transitions record digests, never contents."""

    try:
        canonical = json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)
    except Exception:
        canonical = str(value)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


@dataclass(frozen=True)
class BrowserEvent:
    """One unwrapped ABCP notification."""

    event_name: str
    payload: JsonDict = field(default_factory=dict)
    event_id: Optional[str] = None
    page_id: Optional[str] = None

    @classmethod
    def from_notification(cls, event: Any) -> Optional["BrowserEvent"]:
        if not isinstance(event, dict):
            return None
        name = str(event.get("event") or "")
        if not name:
            return None
        payload = event.get("payload")
        payload = payload if isinstance(payload, dict) else {}
        return cls(
            event_name=name,
            payload=payload,
            event_id=str(event.get("eventId") or "") or None,
            page_id=str(payload.get("pageId") or "") or None,
        )


@dataclass(frozen=True)
class StateTransition:
    reducer: str
    transition: str
    page_id: Optional[str] = None
    before_digest: Optional[str] = None
    after_digest: Optional[str] = None
    attribution: Attribution = "unattributed"


@dataclass(frozen=True)
class BrowserReduction:
    """What a reducer did. Never a tool call, never model-facing text."""

    changed: bool = False
    transitions: List[StateTransition] = field(default_factory=list)
    log: Optional[JsonDict] = None

    @classmethod
    def unchanged(cls) -> "BrowserReduction":
        return cls()


@runtime_checkable
class BrowserEventReducer(Protocol):
    name: str

    def handles(self, event_name: str) -> bool: ...

    def reduce(self, event: BrowserEvent) -> BrowserReduction: ...


@dataclass(frozen=True)
class BrowserEventPolicy:
    """What may happen to one kind of event."""

    event_name: str
    reducer: Optional[str] = None
    persistence: Literal["none", "digest", "full_offload"] = "digest"
    model_visibility: Literal["never", "projected", "receipt_only"] = "never"
    sensitivity: Literal["normal", "sensitive", "secret"] = "normal"
    dedupe_key: Literal["event_id", "cursor", "none"] = "event_id"


# An event nobody registered changes nothing, is recorded only as a bounded
# digest, never reaches the model, and never participates in attribution. The
# default is the safe answer, not a gap to be filled in later.
UNKNOWN_EVENT_POLICY = BrowserEventPolicy(
    event_name="*",
    reducer=None,
    persistence="digest",
    model_visibility="never",
    sensitivity="normal",
    dedupe_key="event_id",
)


class BrowserEventPolicyRegistry:
    def __init__(self, policies: Optional[List[BrowserEventPolicy]] = None) -> None:
        self._policies: Dict[str, BrowserEventPolicy] = {
            policy.event_name: policy for policy in (policies or [])
        }

    def register(self, policy: BrowserEventPolicy) -> None:
        self._policies[policy.event_name] = policy

    def policy_for(self, event_name: str) -> BrowserEventPolicy:
        return self._policies.get(event_name, UNKNOWN_EVENT_POLICY)

    def known(self) -> List[str]:
        return sorted(self._policies)


class DialogLedgerReducer:
    """Which dialogs a page is still holding open.

    Public routing identity only. The prompt text, its default value and
    anything a person typed into it stay out of harness state entirely - this
    ledger exists so `Page.handleDialog` can be given the right dialogId, and
    that needs an id and a type, nothing more.

    Replaying the same dialogOpened twice is idempotent: the browser re-sends on
    reconnect, and a duplicate must not leave two entries the agent then has to
    close twice.
    """

    name = "dialog_ledger"
    EVENTS = frozenset({"Page.dialogOpened", "Page.dialogClosed"})

    def __init__(self) -> None:
        self._pending: Dict[str, List[JsonDict]] = {}

    def handles(self, event_name: str) -> bool:
        return event_name in self.EVENTS

    # -- state readers ------------------------------------------------------

    def pending(self, page_id: Any) -> List[JsonDict]:
        return [dict(item) for item in self._pending.get(str(page_id or ""), [])]

    def pending_ids(self, page_id: Any) -> List[str]:
        return [
            str(item.get("dialogId") or "")
            for item in self._pending.get(str(page_id or ""), [])
        ]

    def settle(self, page_id: Any, dialog_id: Any = "") -> None:
        """Drop one dialog because the agent handled it, not because of an event."""

        page_key = str(page_id or "")
        pending = self._pending.get(page_key, [])
        target = str(dialog_id or "").strip()
        if target:
            pending = [item for item in pending if item.get("dialogId") != target]
        elif pending:
            pending = pending[:-1]
        if pending:
            self._pending[page_key] = pending
        else:
            self._pending.pop(page_key, None)

    # -- reduction ----------------------------------------------------------

    def reduce(self, event: BrowserEvent) -> BrowserReduction:
        if not self.handles(event.event_name):
            return BrowserReduction.unchanged()
        payload = event.payload
        page_id = str(payload.get("pageId") or "").strip()
        if not page_id:
            return BrowserReduction.unchanged()
        dialog = payload.get("dialog")
        dialog_id = str(
            payload.get("dialogId")
            or (dialog.get("id") if isinstance(dialog, dict) else "")
            or ""
        ).strip()

        before = digest(self._pending.get(page_id, []))
        if event.event_name == "Page.dialogClosed":
            self.settle(page_id, dialog_id)
            transition = "closed"
        elif dialog_id:
            pending = self._pending.setdefault(page_id, [])
            pending[:] = [
                item for item in pending if item.get("dialogId") != dialog_id
            ]
            pending.append({
                "dialogId": dialog_id,
                "type": (
                    str(dialog.get("type") or "") if isinstance(dialog, dict) else ""
                ),
            })
            transition = "opened"
        else:
            return BrowserReduction.unchanged()
        after = digest(self._pending.get(page_id, []))

        return BrowserReduction(
            changed=before != after,
            transitions=[
                StateTransition(
                    reducer=self.name,
                    transition=transition,
                    page_id=page_id,
                    before_digest=before,
                    after_digest=after,
                )
            ],
            log={
                "pageId": page_id,
                "event": event.event_name,
                "pendingDialogIds": self.pending_ids(page_id),
            },
        )


DEFAULT_POLICIES = [
    BrowserEventPolicy(
        event_name="Page.dialogOpened",
        reducer=DialogLedgerReducer.name,
        persistence="digest",
        # The id reaches the model through Page.getState's pendingDialogs, as a
        # receipt field - never as an asynchronous event injected into context.
        model_visibility="receipt_only",
        sensitivity="sensitive",
    ),
    BrowserEventPolicy(
        event_name="Page.dialogClosed",
        reducer=DialogLedgerReducer.name,
        persistence="digest",
        model_visibility="receipt_only",
        sensitivity="sensitive",
    ),
]


def default_registry() -> BrowserEventPolicyRegistry:
    return BrowserEventPolicyRegistry(list(DEFAULT_POLICIES))


__all__ = [
    "Attribution",
    "BrowserEvent",
    "BrowserEventPolicy",
    "BrowserEventPolicyRegistry",
    "BrowserEventReducer",
    "BrowserReduction",
    "DEFAULT_POLICIES",
    "DialogLedgerReducer",
    "StateTransition",
    "UNKNOWN_EVENT_POLICY",
    "default_registry",
    "digest",
]
