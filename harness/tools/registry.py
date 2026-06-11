"""
harness.tools.registry - Declarative tool registration helpers.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Dict, List, Optional, Union

from harness.utils import JsonDict


ToolHandler = Callable[["ToolContext"], Awaitable[JsonDict]]
SchemaFactory = Callable[[Optional[Any]], JsonDict]
SchemaLike = Union[JsonDict, SchemaFactory]


@dataclass(frozen=True)
class ToolContext:
    agent: Any
    tool_call: JsonDict
    tool_input: JsonDict
    step: int = 0


@dataclass(frozen=True)
class ToolAction:
    name: str
    description: str
    input_schema: SchemaLike
    handler: ToolHandler
    terminal: bool = False
    strict: bool = True
    loop_guard: bool = True
    contract_check: bool = False
    progress_check: bool = False
    trace_type: str = ""

    def to_tool_spec(self, schema_context: Optional[Any] = None) -> JsonDict:
        schema = (
            self.input_schema(schema_context)
            if callable(self.input_schema)
            else self.input_schema
        )
        spec: JsonDict = {
            "name": self.name,
            "description": self.description,
            "input_schema": schema,
        }
        if self.strict:
            spec["strict"] = True
        return spec


class ToolRegistry:
    def __init__(self, actor: str):
        self.actor = actor
        self._actions: Dict[str, ToolAction] = {}

    def register(
        self,
        *,
        name: str,
        description: str,
        input_schema: SchemaLike,
        terminal: bool = False,
        strict: bool = True,
        loop_guard: bool = True,
        contract_check: bool = False,
        progress_check: bool = False,
        trace_type: str = "",
    ) -> Callable[[ToolHandler], ToolHandler]:
        def decorator(handler: ToolHandler) -> ToolHandler:
            if name in self._actions:
                raise ValueError(f"duplicate {self.actor} tool registration: {name}")
            self._actions[name] = ToolAction(
                name=name,
                description=description,
                input_schema=input_schema,
                handler=handler,
                terminal=terminal,
                strict=strict,
                loop_guard=loop_guard,
                contract_check=contract_check,
                progress_check=progress_check,
                trace_type=trace_type,
            )
            return handler

        return decorator

    def get(self, name: str) -> Optional[ToolAction]:
        return self._actions.get(name)

    def names(self) -> List[str]:
        return list(self._actions.keys())

    def tool_specs(self, schema_context: Optional[Any] = None) -> List[JsonDict]:
        return [
            action.to_tool_spec(schema_context)
            for action in self._actions.values()
        ]
