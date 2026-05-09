"""Tools layer — unified exports.

External usage:
    from tools import build_default_registry
    registry = build_default_registry()
    spec = registry.get("navigate")
"""
import sys

# Windows utf-8 (subpackage level)
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass

from .base import ToolSpec, ToolContext, ToolRegistry, dispatch, format_result_for_llm
from .browser_tools import ALL_BROWSER_TOOLS
from .code_tool import ALL_CODE_TOOLS
from .file_tools import ALL_FILE_TOOLS
from .lead_tools import ALL_LEAD_TOOLS, clear_plan_for_session
from .progress_tools import ALL_PROGRESS_TOOLS
from .batch_tool import ALL_BATCH_TOOLS


def build_default_registry() -> ToolRegistry:
    """Build a registry containing all built-in tools."""
    reg = ToolRegistry()
    reg.register_many(ALL_BROWSER_TOOLS)
    reg.register_many(ALL_CODE_TOOLS)
    reg.register_many(ALL_FILE_TOOLS)
    reg.register_many(ALL_LEAD_TOOLS)
    reg.register_many(ALL_PROGRESS_TOOLS)
    reg.register_many(ALL_BATCH_TOOLS)
    return reg


__all__ = [
    "ToolSpec", "ToolContext", "ToolRegistry",
    "dispatch", "format_result_for_llm",
    "build_default_registry",
    "clear_plan_for_session",
    "ALL_BROWSER_TOOLS", "ALL_CODE_TOOLS", "ALL_FILE_TOOLS", "ALL_LEAD_TOOLS",
    "ALL_PROGRESS_TOOLS", "ALL_BATCH_TOOLS",
]
