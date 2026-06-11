"""
harness.lifecycle - Internal typed lifecycle middleware.

The first version is intentionally in-process only. It gives the harness a
single control surface for context, tools, compaction, and worker handoff
without opening project-level shell/script hooks yet.

LifecycleMiddleware methods are synchronous by design. Async middleware
support should be added by introducing async manager methods and awaiting the
fold, rather than blocking inside these hooks.
"""

from dataclasses import dataclass, field
from typing import Any, List, Optional

from harness.utils import JsonDict


@dataclass
class LifecycleContext:
    actor: str
    step: int = 0
    metadata: JsonDict = field(default_factory=dict)


class LifecycleMiddleware:
    def session_context_build(self, context: LifecycleContext, payload: JsonDict) -> JsonDict:
        return payload

    def agent_before_step(self, context: LifecycleContext, payload: JsonDict) -> JsonDict:
        return payload

    def tool_pre_call(self, context: LifecycleContext, tool_call: JsonDict) -> JsonDict:
        return tool_call

    def tool_post_call(
        self,
        context: LifecycleContext,
        tool_call: JsonDict,
        result: JsonDict,
    ) -> JsonDict:
        return result

    def compact_before(self, context: LifecycleContext, payload: JsonDict) -> JsonDict:
        return payload

    def compact_after(self, context: LifecycleContext, payload: JsonDict) -> JsonDict:
        return payload

    def worker_before_return(self, context: LifecycleContext, result: JsonDict) -> JsonDict:
        return result


class LifecycleManager:
    def __init__(self, middlewares: Optional[List[LifecycleMiddleware]] = None):
        self.middlewares = list(middlewares or [])

    def session_context_build(self, context: LifecycleContext, payload: JsonDict) -> JsonDict:
        return self._fold("session_context_build", context, payload)

    def agent_before_step(self, context: LifecycleContext, payload: JsonDict) -> JsonDict:
        return self._fold("agent_before_step", context, payload)

    def tool_pre_call(self, context: LifecycleContext, tool_call: JsonDict) -> JsonDict:
        return self._fold("tool_pre_call", context, tool_call)

    def tool_post_call(
        self,
        context: LifecycleContext,
        tool_call: JsonDict,
        result: JsonDict,
    ) -> JsonDict:
        value = result
        for middleware in self.middlewares:
            value = middleware.tool_post_call(context, tool_call, value)
        return value

    def compact_before(self, context: LifecycleContext, payload: JsonDict) -> JsonDict:
        return self._fold("compact_before", context, payload)

    def compact_after(self, context: LifecycleContext, payload: JsonDict) -> JsonDict:
        return self._fold("compact_after", context, payload)

    def worker_before_return(self, context: LifecycleContext, result: JsonDict) -> JsonDict:
        return self._fold("worker_before_return", context, result)

    def _fold(self, method_name: str, context: LifecycleContext, value: JsonDict) -> JsonDict:
        current = value
        for middleware in self.middlewares:
            method = getattr(middleware, method_name)
            current = method(context, current)
        return current


def default_lifecycle_manager() -> LifecycleManager:
    return LifecycleManager()


def lifecycle_for(owner: Any) -> LifecycleManager:
    manager = getattr(owner, "lifecycle", None)
    if isinstance(manager, LifecycleManager):
        return manager
    manager = default_lifecycle_manager()
    try:
        owner.lifecycle = manager
    except Exception:
        pass
    return manager
