"""harness.events.publisher - Where a finished event goes.

A slot, not a bus. Replay, back-pressure, async queues and cross-process
subscribers are all deliberately absent: the agent loop needs a stable
signature now, and the day a real bus is warranted it drops in behind this
protocol without touching a single call site.

``enabled`` exists so a disabled publisher costs nothing. Building a validated
event and throwing it away 370 times per step is the kind of overhead that
turns a logging refactor into a latency regression.
"""

from __future__ import annotations

import threading
from typing import Any, Callable, List, Optional

from typing_extensions import Protocol, runtime_checkable


@runtime_checkable
class AgentEventPublisher(Protocol):
    enabled: bool

    def publish(self, event: Any) -> None: ...


class NullAgentEventPublisher:
    """Accepts everything, keeps nothing. The default."""

    enabled = False

    def publish(self, event: Any) -> None:
        return None


class RecordingPublisher:
    """Keeps every event in order. For tests and lifecycle pairing assertions."""

    def __init__(self, enabled: bool = True) -> None:
        self.enabled = enabled
        self.events: List[Any] = []
        self._lock = threading.Lock()

    def publish(self, event: Any) -> None:
        with self._lock:
            self.events.append(event)

    def types(self) -> List[str]:
        return [str(getattr(event, "type", "")) for event in self.events]

    def of_type(self, type_name: str) -> List[Any]:
        return [
            event for event in self.events
            if str(getattr(event, "type", "")) == type_name
        ]


class CallbackPublisher:
    """Adapts a plain callable into the publisher protocol."""

    def __init__(self, callback: Callable[[Any], None], enabled: bool = True) -> None:
        self._callback = callback
        self.enabled = enabled

    def publish(self, event: Any) -> None:
        self._callback(event)


def resolve_publisher(candidate: Optional[Any]) -> Any:
    """Normalise anything publisher-shaped, including None and bare callables."""
    if candidate is None:
        return NullAgentEventPublisher()
    if callable(candidate) and not hasattr(candidate, "publish"):
        return CallbackPublisher(candidate)
    return candidate


__all__ = [
    "AgentEventPublisher",
    "CallbackPublisher",
    "NullAgentEventPublisher",
    "RecordingPublisher",
    "resolve_publisher",
]
