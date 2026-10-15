"""Skill-owner access through the existing task-scoped file tools."""
from __future__ import annotations

from harness.tools.browser_tools.dispatch import BROWSER_TOOLS
from harness.tools.path_authorization import authorize_tool_call
from harness.tools.registry import ToolContext


LOCAL_FILE_TOOLS = ("local_fs_authorize", "local_fs_search", "local_fs_read")


def tool_definitions():
    # Use the same contracts and handlers as Browser; local file operations do
    # not require a browser instance, a page lease or another model session.
    for name in LOCAL_FILE_TOOLS:
        action = BROWSER_TOOLS.get(name)
        spec = action.to_tool_spec()
        yield name, spec["description"], spec["input_schema"]


async def execute(session, name, arguments):
    call = {"name": name, "input": arguments}
    denied = await authorize_tool_call(session, call)
    if denied is not None:
        result = denied
    else:
        action = BROWSER_TOOLS.get(name)
        result = await action.handler(ToolContext(agent=session, tool_call=call,
            tool_input=arguments))
    session.state = session.store.load() or session.state
    reference = session.store.record(session.state, "skill.local_file.result",
        {"tool": name, "input": arguments, "result": result})
    return {**result, "receiptRef": reference}
