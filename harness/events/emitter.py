"""harness.events.emitter - One synchronous fan-out from event to sinks.

Not a bus: no queue, no replay, no subscriber management. It exists so there is
a single place where an event becomes persisted bytes, a console line and a
trace entry, instead of three call sites each remembering to do all three.

Re-entrancy is refused rather than handled. A sink that logs while handling an
event would emit into the same emitter, whose sinks would log again; the only
safe answer is to drop the nested event and count it, because every other
answer is a storm.
"""

from __future__ import annotations

import sys
import threading
from typing import Any, Dict, Iterable, List, Optional

from harness.events.sinks import SinkBinding


class RunEventEmitter:
    def __init__(self, bindings: Optional[Iterable[SinkBinding]] = None) -> None:
        self._bindings: List[SinkBinding] = list(bindings or [])
        self._state = threading.local()
        self._lock = threading.Lock()
        self.failures: Dict[str, int] = {}
        self.reentrant_drops = 0
        self._reported: set = set()

    def add_sink(self, sink: Any, *, critical: bool = False) -> None:
        self._bindings.append(SinkBinding(sink, critical=critical))

    def remove_sink(self, name: str) -> None:
        self._bindings = [
            binding for binding in self._bindings if binding.name != name
        ]

    @property
    def sink_names(self) -> List[str]:
        return [binding.name for binding in self._bindings]

    def emit(self, event: Any) -> None:
        if getattr(self._state, "dispatching", False):
            with self._lock:
                self.reentrant_drops += 1
            return
        self._state.dispatching = True
        try:
            for binding in self._bindings:
                try:
                    binding.sink.handle(event)
                except Exception as exc:
                    if binding.critical:
                        raise
                    self._note_failure(binding.name, exc)
        finally:
            self._state.dispatching = False

    def _note_failure(self, name: str, exc: BaseException) -> None:
        with self._lock:
            self.failures[name] = self.failures.get(name, 0) + 1
            first = name not in self._reported
            self._reported.add(name)
        if first:
            # Straight to stderr, never back through the emitter that is
            # already mid-dispatch.
            print(
                f"[events] sink {name!r} failed and was skipped: "
                f"{type(exc).__name__}: {exc}",
                file=sys.stderr,
                flush=True,
            )

    def summary(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "sinks": self.sink_names,
                "failures": dict(self.failures),
                "reentrantDrops": self.reentrant_drops,
            }


__all__ = ["RunEventEmitter"]
