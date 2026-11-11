"""Per-task usage accounting, with no token or time limits.

Charged tokens are uncached_input + cache_write + output; cache_read is
reported but not charged. Net time excludes waiting for the user or HITL and,
waiting for child tasks remains part of end-to-end time. Child usage is separate.
Usage never decides whether a task may continue.
"""
from __future__ import annotations

import time
from contextlib import contextmanager
from typing import Any, Iterator


def migrate_usage(state: dict[str, Any]) -> dict[str, Any]:
    """Keep historical counters but retire the old budget fields and stop status."""
    legacy = state.pop("budget", None) or {}
    if legacy and state.get("usage") is None:
        state["usage"] = legacy.get("spent") or {}
    if state.get("status") == "budget_exhausted":
        state["status"] = "active"
        state.pop("stopReason", None)
    return state


class Usage:
    def __init__(self, profile: str, *, spent: dict[str, Any] | None = None):
        self.profile = profile
        spent = spent or {}
        self.tokens = int(spent.get("tokens", 0))
        self.cache_read = int(spent.get("cacheRead", 0))
        self.prior_seconds = float(spent.get("netSeconds", 0.0))
        names = ("uncached_input", "cache_read", "cache_creation", "output")
        self.counters = {name: spent.get("tokenClasses", {}).get(name, None if self.tokens else 0) for name in names}
        self.model_calls = spent.get("modelCalls", None if self.tokens else 0)
        self.tools = {name: dict(row) for name, row in spent.get("tools", {}).items()}
        self.started = time.monotonic()
        self.waiting = 0.0
        self._wait_depth = 0
        self._wait_started = 0.0
        from harness.runtime.wait_timing import Timeline
        self.timeline = Timeline(spent.get('treeTiming'))
        self.tree_timing_complete = spent.get('treeTimingComplete', not self.prior_seconds)

    def charge(self, result: Any) -> None:
        if self.model_calls is not None:
            self.model_calls += 1
        usage = getattr(result, "usage", None)
        if usage is None:
            self.counters = {name: None for name in self.counters}
            return
        read = int(usage.cache_read_tokens or 0)
        write = int(usage.cache_write_tokens or 0)
        total_input = int(usage.input_tokens or 0)
        self.tokens += max(0, total_input - read - write) + write + int(usage.output_tokens or 0)
        self.cache_read += read
        from harness.runtime.tau_bridge import neutral_counters
        observed = neutral_counters(usage)
        for name, key in (("uncached_input", "uncachedInput"), ("cache_read", "cacheRead"),
                          ("cache_creation", "cacheCreation"), ("output", "output")):
            value = observed[key]
            self.counters[name] = (self.counters[name] + value
                                   if self.counters[name] is not None and value is not None else None)

    def record_tool(self, name: str, *, failed: bool, duration_ms: int):
        row = self.tools.setdefault(name, {"calls": 0, "failed": 0, "durationMs": 0})
        row["calls"] += 1
        row["failed"] += int(failed)
        row["durationMs"] += max(0, duration_ms)

    @contextmanager
    def waiting_for_human(self) -> Iterator[None]:
        """Exclude this session's user/HITL wait; child execution is elapsed work."""
        if self._wait_depth == 0:
            self._wait_started = time.monotonic()
        self._wait_depth += 1
        try:
            from harness.runtime.wait_timing import human_wait
            with human_wait(self.timeline):
                yield
        finally:
            self._wait_depth -= 1
            if self._wait_depth == 0:
                self.waiting += time.monotonic() - self._wait_started

    def net_seconds(self) -> float:
        current = self.waiting + (time.monotonic() - self._wait_started if self._wait_depth else 0.0)
        return self.prior_seconds + max(0.0, time.monotonic() - self.started - current)

    def snapshot(self) -> dict[str, Any]:
        return {"profile": self.profile, "tokens": self.tokens,
                "cacheRead": self.cache_read, "tokenClasses": dict(self.counters), "modelCalls": self.model_calls,
                "netSeconds": round(self.net_seconds(), 1), "tools": self.tools,
                "treeTiming": self.timeline.snapshot(), "treeTimingComplete": self.tree_timing_complete,
                "metric": "tokens = uncached_input + cache_write + output; cache_read not charged; "
                          "legacy tokens is an unweighted compatibility counter, not a billing amount; "
                          "net time excludes user/HITL waits and includes child execution"}

    def persisted(self) -> dict[str, Any]:
        return {"tokens": self.tokens, "cacheRead": self.cache_read, "tokenClasses": dict(self.counters),
                "modelCalls": self.model_calls, "netSeconds": self.net_seconds(), "tools": self.tools,
                "treeTiming": self.timeline.snapshot(), "treeTimingComplete": self.tree_timing_complete}
