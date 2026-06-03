"""
harness.tool_policy - Shared tool policy for BrowserAgent workers.

`allowed_methods` from an LLM-authored worker contract is intentionally not
used as a hard allow-list for ABCP atomic methods. The stable policy is owned
by the harness: task_type narrows obviously irrelevant domains, explicit
forbidden_methods still wins, and progress/loop guards handle overuse.
"""

from __future__ import annotations

from typing import FrozenSet, Iterable, Set


HARNESS_DEFAULT_ALLOWED_TOOLS: FrozenSet[str] = frozenset({
    "final_answer",
    "record_extraction",
    "local_fs_search",
    "local_fs_read",
    "local_fs_jsonpath",
    "extract_dom_records",
    "eval_js_json",
    "navigate_verified",
    "visual_verify",
})

HARNESS_TOOL_NAMES: FrozenSet[str] = frozenset({
    "browser_call",
    *HARNESS_DEFAULT_ALLOWED_TOOLS,
})

ALWAYS_FORBIDDEN_ABCP_METHODS: FrozenSet[str] = frozenset({
    "DOM.getSemanticTree",
    "Hitl.getTaskSummary",
    "Hitl.resumeEvent",
})

TASK_TYPE_DISABLED_DOMAINS = {
    "web_search": frozenset({"Bookmark", "Download", "File", "History", "Memory"}),
    "web_scrape": frozenset({"Bookmark", "Download", "File", "History", "Memory"}),
    "form_fill": frozenset({"Bookmark", "Download", "History"}),
    "download_file": frozenset({"Bookmark", "History", "Memory"}),
}

TASK_TYPE_ALLOWED_EXCEPTIONS = {
    "form_fill": frozenset({"File.handleChooser"}),
}


def method_domain(method: str) -> str:
    text = str(method or "").strip()
    return text.split(".", 1)[0] if "." in text else ""


def normalize_task_type(task_type: object) -> str:
    return str(task_type or "general").strip() or "general"


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
