"""
harness.tool_policy - Shared tool policy for BrowserAgent workers.

`allowed_methods` from an LLM-authored worker contract is intentionally not
used as a hard allow-list for ABCP atomic methods. The stable policy is owned
by the harness: task_type narrows obviously irrelevant domains, explicit
forbidden_methods still wins, and progress/loop guards handle overuse.
"""

from __future__ import annotations

from typing import Any, Dict, FrozenSet, Iterable, Optional, Set


# Tool input fields that carry secrets when the call opts into masking
# (mask=true). The browser still receives the real value; these are masked at
# every logging/trace/snapshot boundary so secrets never get persisted.
SENSITIVE_TOOL_INPUT_FIELDS: Dict[str, FrozenSet[str]] = {
    "fill_field_verified": frozenset({"text"}),
}


def mask_token(value: Any) -> str:
    return f"<masked len={len(str(value))}>"


def mask_params(params: Any, redact_params: Optional[Set[str]]) -> Any:
    """Return a copy of a params dict with `redact_params` keys masked. The
    original is never mutated, so the real values can still reach the browser."""
    if not redact_params or not isinstance(params, dict):
        return params
    return {
        key: (mask_token(value) if key in redact_params and value is not None else value)
        for key, value in params.items()
    }


def sanitize_tool_input_for_log(name: Any, tool_input: Any) -> Any:
    """Mask sensitive fields in a model tool-call input when the call opted into
    masking (mask truthy). Returns a copy; the original input is untouched."""
    if not isinstance(tool_input, dict):
        return tool_input
    fields = SENSITIVE_TOOL_INPUT_FIELDS.get(str(name or ""))
    if not fields or not tool_input.get("mask"):
        return tool_input
    return mask_params(tool_input, set(fields))


def sanitize_tool_calls_for_log(tool_calls: Any) -> Any:
    """Sanitize a list of model tool calls ({name, input, ...}) for logging."""
    if not isinstance(tool_calls, list):
        return tool_calls
    sanitized = []
    for item in tool_calls:
        if isinstance(item, dict):
            sanitized.append({
                **item,
                "input": sanitize_tool_input_for_log(item.get("name"), item.get("input", {})),
            })
        else:
            sanitized.append(item)
    return sanitized


HARNESS_DEFAULT_ALLOWED_TOOLS: FrozenSet[str] = frozenset({
    "final_answer",
    "record_extraction",
    "local_fs_search",
    "local_fs_read",
    "local_fs_jsonpath",
    "find_in_axtree",
    "extract_dom_records",
    "eval_js_json",
    "navigate_verified",
    "visual_verify",
    "dismiss_overlay",
    "collect_items",
    "fill_field_verified",
})

HARNESS_TOOL_NAMES: FrozenSet[str] = frozenset({
    "browser_call",
    *HARNESS_DEFAULT_ALLOWED_TOOLS,
})

ALWAYS_FORBIDDEN_ABCP_METHODS: FrozenSet[str] = frozenset({
    "DOM.getSemanticTree",
    "Hitl.getTaskSummary",
    "Hitl.resumeEvent",
    "Memory.delete",
})

TASK_TYPE_DISABLED_DOMAINS = {
    "web_search": frozenset({"Bookmark", "Download", "File", "History", "Memory"}),
    "web_scrape": frozenset({"Bookmark", "Download", "File", "History", "Memory"}),
    "form_fill": frozenset({"Bookmark", "Download", "History"}),
    "download_file": frozenset({"Bookmark", "History", "Memory"}),
}

TASK_TYPE_ALLOWED_EXCEPTIONS = {
    "web_search": frozenset({"Memory.get", "Memory.save"}),
    "web_scrape": frozenset({"Memory.get", "Memory.save"}),
    "form_fill": frozenset({"File.handleChooser", "Memory.get", "Memory.save"}),
    "download_file": frozenset({"Memory.get", "Memory.save"}),
}

TASK_TYPE_ALIASES = {
    "browser_data_collection": "web_scrape",
    "browser_action": "form_fill",
}


def method_domain(method: str) -> str:
    text = str(method or "").strip()
    return text.split(".", 1)[0] if "." in text else ""


def normalize_task_type(task_type: object) -> str:
    normalized = str(task_type or "general").strip() or "general"
    return TASK_TYPE_ALIASES.get(normalized, normalized)


def disabled_reason_for_method(method: str, task_type: object) -> str:
    method = str(method or "").strip()
    if not method:
        return ""
    if method in ALWAYS_FORBIDDEN_ABCP_METHODS:
        return f"{method} is globally disabled by harness policy"
    normalized = normalize_task_type(task_type)
    exceptions = TASK_TYPE_ALLOWED_EXCEPTIONS.get(normalized, frozenset())
    if method in exceptions:
        return ""
    domain = method_domain(method)
    disabled_domains = TASK_TYPE_DISABLED_DOMAINS.get(normalized, frozenset())
    if domain in disabled_domains:
        return (
            f"{method} belongs to disabled domain {domain!r} for task_type"
            f" {normalized!r}"
        )
    return ""


def filter_capability_methods_for_task_type(
    methods: Iterable[str],
    task_type: object,
) -> Set[str]:
    return {
        method
        for method in {str(item).strip() for item in methods if str(item).strip()}
        if not disabled_reason_for_method(method, task_type)
    }
