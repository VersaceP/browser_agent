"""
harness.tools - Tool schemas, parsers, and dispatch factories.
"""

__all__ = [
    "build_browser_agent_tool_specs",
    "build_browser_tool_dispatcher",
    "build_lead_agent_tool_specs",
    "build_lead_tool_dispatcher",
]


def __getattr__(name):
    """Compatibility exports load only the requested tool family."""
    if name not in __all__:
        raise AttributeError(name)
    from importlib import import_module
    owner = "browser_tools" if name.startswith("build_browser_") else "lead_tools"
    value = getattr(import_module(f".{owner}", __name__), name)
    globals()[name] = value
    return value
