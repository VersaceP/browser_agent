"""Tools-layer infrastructure — turns LLM JSON tool_use blocks into Python calls.

Design:
- ToolSpec dataclass describes a tool's schema (LLM-facing) + handler (process-facing)
- Handlers can be sync or async — dispatch adapts automatically
- ToolRegistry is a plain dict; no hook bus, no trust_level, no dispatch interception
- dispatch() is async — sync handlers go through to_thread (don't block the loop);
  async handlers are awaited directly

Compared to v5:
- v5: BaseTool subclassing + ToolRegistry + 5 hook events + filter_tools + dispatch ≈ 800 LOC
- v6: ToolSpec dataclass + dict + dispatch ≈ 130 LOC
"""
import asyncio
import inspect
import json
import traceback
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional


@dataclass
class ToolSpec:
    """Single tool's metadata + implementation.

    Attributes:
        name: unique tool name (called by LLM, e.g. "navigate")
        description: short description for the LLM (also embedded in the system prompt)
        input_schema: JSON Schema dict for the input. Used directly by both the
            Anthropic and OpenAI tool protocols.
        handler: the actual function. Signature: (input: dict, ctx: ToolContext) -> Any
        readonly: read-only flag (verification agents only get readonly tools)
        long_running: long-running flag (helps the UI display progress)
    """
    name: str
    description: str
    input_schema: Dict[str, Any]
    handler: Callable[..., Any]
    readonly: bool = False
    long_running: bool = False

    def to_anthropic_schema(self) -> Dict[str, Any]:
        """Anthropic Messages API tool schema."""
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_schema,
        }

    def to_openai_schema(self) -> Dict[str, Any]:
        """OpenAI function-calling tool schema."""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.input_schema,
            },
        }


@dataclass
class ToolContext:
    """Per-dispatch context passed to handlers.

    Handlers reach the current agent's worktree / shared / pool / spawner / daemon /
    summarizer and other runtime resources via this context.
    """
    worktree: str                                   # absolute path of the current agent's worktree
    shared_dir: str                                 # session-shared directory (cross-agent data)
    session_id: str = ""
    agent_type: str = ""
    pool: Optional[Any] = None                       # WarmPool instance (for run_browser_python)
    spawner: Optional[Any] = None                    # AgentSpawner instance (for spawn_agent)
    daemon: Optional[Any] = None                     # BrowserDaemon (for PageDisconnectedError self-heal)
    summarizer: Optional[Any] = None                 # low-cost LLM (Haiku-class) — for read_file large-file summaries
    tool_use_id: Optional[str] = None                # current dispatch's LLM tool_use id (set by execution_loop)
    extra: Dict[str, Any] = field(default_factory=dict)


class ToolRegistry:
    """Tool registry — minimal: no trust_level, no hook bus, no filtering."""

    def __init__(self):
        self._tools: Dict[str, ToolSpec] = {}

    def register(self, spec: ToolSpec) -> None:
        if spec.name in self._tools:
            raise ValueError(f"tool {spec.name!r} already registered")
        self._tools[spec.name] = spec

    def register_many(self, specs: List[ToolSpec]) -> None:
        for s in specs:
            self.register(s)

    def get(self, name: str) -> Optional[ToolSpec]:
        return self._tools.get(name)

    def names(self) -> List[str]:
        return list(self._tools.keys())

    def filter_by_names(self, allowed: List[str]) -> List[ToolSpec]:
        """Filter by whitelist (used to give different agents different tool subsets)."""
        return [self._tools[n] for n in allowed if n in self._tools]

    def all(self) -> List[ToolSpec]:
        return list(self._tools.values())


# ──────────────────────────────────
# Dispatch — main-process entry; called when the LLM picks a tool
# ──────────────────────────────────

_DISCONNECT_EXC_NAMES = {
    "PageDisconnectedError", "PageClosedError", "WebSocketException",
    "BrowserDisconnectedError", "TargetClosedError",
}
_DISCONNECT_MSG_KEYWORDS = (
    "page disconnected", "页面的连接已断开", "websocket is closed",
    "no such target", "browser has crashed", "remote end closed",
    "tab is closed", "page is closed",
)


def _looks_like_disconnect_error(exc_name: str, msg: str) -> bool:
    if not exc_name:
        return False
    if exc_name in _DISCONNECT_EXC_NAMES:
        return True
    m = (msg or "").lower()
    return any(k in m for k in _DISCONNECT_MSG_KEYWORDS)


async def _invoke_safe(spec: "ToolSpec", tool_input: dict, ctx: "ToolContext") -> Dict[str, Any]:
    """Execute a single dispatch and return a status='ok'|'error' dict."""
    try:
        sig = inspect.signature(spec.handler)
        if asyncio.iscoroutinefunction(spec.handler):
            if "ctx" in sig.parameters:
                result = await spec.handler(tool_input, ctx=ctx)
            else:
                result = await spec.handler(tool_input)
        else:
            if "ctx" in sig.parameters:
                result = await asyncio.to_thread(spec.handler, tool_input, ctx)
            else:
                result = await asyncio.to_thread(spec.handler, tool_input)
        return {"status": "ok", "result": result}
    except Exception as e:
        return {
            "status": "error",
            "exception": type(e).__name__,
            "message": str(e),
            "traceback": traceback.format_exc(),
        }


async def dispatch(
    registry: ToolRegistry,
    name: str,
    tool_input: dict,
    ctx: ToolContext,
) -> Dict[str, Any]:
    """Unified async dispatch entry.

    Auto-adapts:
    - sync handler → asyncio.to_thread (doesn't block the loop)
    - async handler → awaited directly (e.g. spawn_agent that needs await spawner.spawn)

    Auto-recovery (main process):
    - When result['exception'] is PageDisconnectedError or message contains a
      browser-disconnect keyword, AND ctx.daemon is injected, automatically calls
      daemon.restart() and retries once.
    - On retry success, result['recovered_via_restart']=True
    - If daemon.restart() itself fails, the original error gets a
      restart_attempted_failed field

    Returns:
        {"status": "ok", "result": ...}
        or
        {"status": "error", "exception": "...", "message": "...", "traceback": "..."}
    """
    spec = registry.get(name)
    if not spec:
        return {
            "status": "error",
            "exception": "ToolNotFound",
            "message": f"unknown tool {name!r}; available: {registry.names()}",
        }

    result = await _invoke_safe(spec, tool_input, ctx)

    # Browser-disconnect self-heal — only when the main process holds the daemon
    if (
        result.get("status") == "error"
        and ctx.daemon is not None
        and _looks_like_disconnect_error(
            result.get("exception", ""), result.get("message", "")
        )
    ):
        try:
            print(f"[dispatch] {name}: browser disconnected -> daemon.restart() and retry")
            ctx.daemon.restart(wait_seconds=10.0)
        except Exception as re:
            result["restart_attempted_failed"] = f"{type(re).__name__}: {re}"
            return result
        # Retry once
        retry = await _invoke_safe(spec, tool_input, ctx)
        if retry.get("status") == "ok":
            retry["recovered_via_restart"] = True
            return retry
        # Retry still failed — annotate the original error
        retry["restart_attempted"] = True
        return retry

    return result


def format_result_for_llm(result: Dict[str, Any], max_inline_chars: int = 2000) -> str:
    """Convert a dispatch result dict into the string the LLM sees.

    - ok + small result → JSON dump
    - ok + large result → JSON + truncation hint (LLM should use save_artifact instead)
    - error → multi-line format with exception type + message + tail of traceback
    """
    if result["status"] == "ok":
        body = result["result"]
        if isinstance(body, (dict, list)):
            text = json.dumps(body, ensure_ascii=False, indent=2, default=str)
        else:
            text = str(body)
        if len(text) > max_inline_chars:
            text = (
                f"{text[:max_inline_chars]}\n"
                f"... [truncated, total {len(text)} chars] — "
                f"too large to fit; pass 'save_to' parameter or use save_artifact()"
            )
        return text

    # error
    parts = [f"ERROR {result['exception']}: {result.get('message', '')}"]
    tb = result.get("traceback", "")
    if tb:
        # Keep only the last 5 traceback lines to avoid drowning the LLM
        tb_lines = tb.strip().splitlines()
        if len(tb_lines) > 5:
            tb = "...\n" + "\n".join(tb_lines[-5:])
        parts.append(f"traceback (tail):\n{tb}")
    return "\n".join(parts)
