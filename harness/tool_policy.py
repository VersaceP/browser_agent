"""
harness.tool_policy - Shared tool policy for BrowserAgent workers.

`allowed_methods` from an LLM-authored worker contract is intentionally not
used as a hard allow-list for ABCP atomic methods. The stable policy is owned
by the harness: task_type narrows obviously irrelevant domains, explicit
forbidden_methods still wins, and progress/loop guards handle overuse.
"""

from __future__ import annotations

from typing import Any, Dict, FrozenSet, Iterable, Optional, Set

from harness.task_types import normalize_task_type


# Tool input fields that carry secrets when the call opts into masking
# (mask=true). The browser still receives the real value; these are masked at
# every logging/trace/snapshot boundary so secrets never get persisted.
SENSITIVE_TOOL_INPUT_FIELDS: Dict[str, FrozenSet[str]] = {
    "fill_field_verified": frozenset({"text"}),
}

# Harness composite tools hidden from the model tool surface for task types
# where they have no legitimate use — pure schema-token/choice-noise savings.
# Mirrors the ABCP-method task_type policy: hide only where we are confident
# (fail-open for general/unknown and for form-capable task types).
HARNESS_TOOLS_HIDDEN_BY_TASK_TYPE: Dict[str, FrozenSet[str]] = {
    "web_scrape": frozenset({"fill_field_verified"}),
    "web_search": frozenset({"fill_field_verified"}),
    "file_download": frozenset({"fill_field_verified"}),
    "browser_state_management": frozenset({"fill_field_verified"}),
}


def hidden_harness_tools_for_task_type(task_type: object) -> Set[str]:
    return set(
        HARNESS_TOOLS_HIDDEN_BY_TASK_TYPE.get(normalize_task_type(task_type))
        or frozenset()
    )


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
    "find_in_axtree",
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

# DOM.getSemanticTree is NO LONGER globally forbidden: crash-boundary probes on
# current ABCP builds did not reproduce the historical renderer crash, and the
# model needs it as a diagnostic when AXTree is insufficient (tag hierarchy,
# complete local bounds, selector debugging). It is heavy (~3.65x AXTree) so its
# results are offloaded (constants.OFFLOAD_METHODS) and the model prompt limits
# it to local diagnostics. Keeping it out of this set also lets it appear in
# worker_contract.forbidden_methods without tripping the unknown-method check
# (it is now a known capability method). HARNESS-INTERNAL auto-digest use stays
# separately gated by HarnessConfig.semantic_tree.
#
# The three entries below no longer appear in System.getCapabilities as of ABCP
# v1.1.5, so they are inert today. They are KEPT ON PURPOSE rather than cleaned
# up: unlike a stale TASK_TYPE_ALLOWED_EXCEPTIONS entry (which silently disables
# a live method), a stale entry here costs nothing, and each still encodes a
# policy we would want the moment the platform reintroduces the method —
# Hitl.* wait/resume is owned by harness/hitl.py, and Memory.delete would let a
# worker destroy another phase's memory. Re-verify against the capability
# surface before removing any of them.
ALWAYS_FORBIDDEN_ABCP_METHODS: FrozenSet[str] = frozenset({
    "Hitl.getTaskSummary",
    "Hitl.resumeEvent",
    "Memory.delete",
})

# Network is disabled for every declared task_type: cookie read/write and
# request interception are not part of any current business flow, and the
# fleet shares one cookie jar — a single worker mutating it would silently
# change every sibling worker's session. `general` is deliberately absent
# here (fail-open for unknown/unclassified work), matching the rest of this
# table's policy.
TASK_TYPE_DISABLED_DOMAINS = {
    "web_search": frozenset({"Bookmark", "Download", "File", "History", "Memory", "Network"}),
    "web_scrape": frozenset({"Bookmark", "Download", "File", "History", "Memory", "Network"}),
    "form_filling": frozenset({"Bookmark", "Download", "File", "History", "Memory", "Network"}),
    "file_download": frozenset({"Bookmark", "File", "History", "Memory", "Network"}),
    "file_upload": frozenset({"Bookmark", "Download", "File", "History", "Memory", "Network"}),
    "browser_state_management": frozenset({
        "Bookmark", "Download", "File", "History", "Memory", "Network",
    }),
}

# Exceptions are matched by FULL METHOD NAME, so every entry here must exist in
# the live System.getCapabilities surface. ABCP v1.1.5 (2026-07-31, capability
# 58 -> 60) consolidated the Bookmark/History APIs; the stale pre-v1.1.5 names
# that used to live here silently disabled browser_state_management's own core
# methods (upsert/folder/rename/History.remove) for four weeks, because a name
# that matches nothing cannot exempt anything from the domain rule above.
TASK_TYPE_ALLOWED_EXCEPTIONS = {
    "web_search": frozenset({"Memory.get", "Memory.save"}),
    "web_scrape": frozenset({"Memory.get", "Memory.save"}),
    "form_filling": frozenset({"File.handleChooser", "Memory.get", "Memory.save"}),
    # Downloads run through the Download.* domain, which is not disabled for
    # this task_type; the File domain only carries handleChooser (an upload
    # affordance), so nothing from File needs an exception here.
    "file_download": frozenset({"Memory.get", "Memory.save"}),
    "file_upload": frozenset({"File.handleChooser", "Memory.get", "Memory.save"}),
    "browser_state_management": frozenset({
        "Bookmark.folder",
        "Bookmark.list",
        "Bookmark.remove",
        "Bookmark.rename",
        "Bookmark.upsert",
        "History.list",
        "History.remove",
        "Memory.get",
        "Memory.list",
        "Memory.save",
    }),
}


def method_domain(method: str) -> str:
    text = str(method or "").strip()
    return text.split(".", 1)[0] if "." in text else ""


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
