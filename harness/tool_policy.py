"""
harness.tool_policy - Shared tool policy for BrowserAgent workers.

`allowed_methods` from an LLM-authored worker contract is intentionally not
used as a hard allow-list for ABCP atomic methods. The stable policy is owned
by the harness: task_type narrows obviously irrelevant domains, explicit
forbidden_methods still wins, and progress/loop guards handle overuse.
"""

from __future__ import annotations

from typing import Any, Dict, FrozenSet, Iterable, Optional, Set, Tuple

from harness.task_types import (
    TASK_TYPE_SCENARIOS,
    TASK_TYPE_SELECTION_RULE,
    VALID_TASK_TYPES,
    resolve_task_type_fail_closed,
)


# Tool input fields that carry secrets when the call opts into masking
# (mask=true). The browser still receives the real value; these are masked at
# every logging/trace/snapshot boundary so secrets never get persisted.
SENSITIVE_TOOL_INPUT_FIELDS: Dict[str, FrozenSet[str]] = {
    "fill_field_verified": frozenset({"text"}),
}

# Harness composite tools hidden from the model tool surface for task types
# where they have no legitimate use — pure schema-token/choice-noise savings.
# Mirrors the ABCP-method task_type policy: explicit general remains broad, but
# missing/unknown values resolve to restricted web_scrape defense-in-depth.
HARNESS_TOOLS_HIDDEN_BY_TASK_TYPE: Dict[str, FrozenSet[str]] = {
    "web_scrape": frozenset({"fill_field_verified"}),
    "web_search": frozenset({"fill_field_verified"}),
    "file_download": frozenset({"fill_field_verified"}),
    "browser_state_management": frozenset({"fill_field_verified"}),
}


def hidden_harness_tools_for_task_type(task_type: object) -> Set[str]:
    return set(
        HARNESS_TOOLS_HIDDEN_BY_TASK_TYPE.get(
            resolve_task_type_fail_closed(task_type)
        )
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
# Keeping these entries after the methods vanished from System.getCapabilities
# has now paid for itself: `Memory.delete` is BACK in the live capability
# surface (verified against the running dispatcher, 61 capabilities), so this
# block is load-bearing again rather than inert — a worker could otherwise
# destroy another phase's memory. `Hitl.getTaskSummary` / `Hitl.resumeEvent`
# remain absent and stay listed on the same reasoning: unlike a stale
# TASK_TYPE_ALLOWED_EXCEPTIONS entry (which silently disables a live method), a
# stale entry here costs nothing, and each encodes the policy we would want the
# moment the platform reintroduces the method — Hitl.* wait/resume is owned by
# harness/hitl.py. Re-verify against the capability surface before removing any
# of them.
ALWAYS_FORBIDDEN_ABCP_METHODS: FrozenSet[str] = frozenset({
    "Hitl.getTaskSummary",
    "Hitl.resumeEvent",
    "Memory.delete",
})

# Network is disabled for every declared task_type: cookie read/write and
# request interception are not part of any current business flow, and the
# fleet shares one cookie jar — a single worker mutating it would silently
# change every sibling worker's session. `general` is deliberately absent for
# explicitly reviewed unclassified work; missing/unknown values are resolved
# to web_scrape before this table is consulted.
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


def describe_task_types() -> str:
    """Render the task_type menu the planner picks from, deriving every
    capability consequence from the tables above.

    Hand-written capability prose in a tool schema goes stale the moment a
    domain moves between task types, and a planner that trusts stale prose
    silently loses a method domain worker-side. Generating it means the schema
    the model reads and the policy the worker runs under are the same fact.
    """
    # Exceptions granted to EVERY task type (Memory.get/save today) carry no
    # signal for choosing between them, and listing them on all seven lines
    # buries the one exception that does discriminate. Computed, not hardcoded,
    # so a future universally-granted method drops out on its own.
    # Only task types that actually carry an exception list take part: a type
    # that disables nothing (general) has no exceptions by construction, and
    # counting its empty set would make the intersection empty every time.
    exception_sets = [
        set(exceptions)
        for exceptions in TASK_TYPE_ALLOWED_EXCEPTIONS.values()
        if exceptions
    ]
    universal = set.intersection(*exception_sets) if exception_sets else set()
    lines = []
    for task_type in sorted(VALID_TASK_TYPES):
        scenario = TASK_TYPE_SCENARIOS.get(task_type, "")
        disabled = sorted(TASK_TYPE_DISABLED_DOMAINS.get(task_type, frozenset()))
        exceptions = sorted(
            set(TASK_TYPE_ALLOWED_EXCEPTIONS.get(task_type) or frozenset()) - universal
        )
        detail = (
            f"disabled: {', '.join(disabled)}"
            if disabled else "disables nothing"
        )
        if exceptions:
            detail += f", except {', '.join(exceptions)}"
        lines.append(f"{task_type} — {scenario} [{detail}]")
    return (
        "Pick the value that matches what THIS phase does; a wrong pick removes"
        " method domains from the worker and cannot be recovered without a"
        " replan. "
        + TASK_TYPE_SELECTION_RULE
        + " Options: "
        + " | ".join(lines)
    )


def method_domain(method: str) -> str:
    text = str(method or "").strip()
    return text.split(".", 1)[0] if "." in text else ""


def _task_type_policy_profile(task_type: str) -> Tuple[FrozenSet[str], FrozenSet[str]]:
    """(disabled domains, full-name exceptions) for one task type.

    A type absent from both tables (general) disables nothing and therefore
    needs no exceptions — the widest possible surface.
    """
    return (
        frozenset(TASK_TYPE_DISABLED_DOMAINS.get(task_type) or frozenset()),
        frozenset(TASK_TYPE_ALLOWED_EXCEPTIONS.get(task_type) or frozenset()),
    )


def task_type_capability_covers(task_type: str, other: str) -> bool:
    """True when `task_type` can call everything `other` can.

    Derived from the two policy tables above rather than declared, because a
    hand-written containment table states a fact those tables own: move one
    domain between task types and the hand-written copy silently keeps
    promising the old shape. Inputs are alias-normalized and fail closed;
    otherwise an unknown value absent from both tables would look identical to
    the intentionally unrestricted ``general`` type.
    """
    task_type = resolve_task_type_fail_closed(task_type)
    other = resolve_task_type_fail_closed(other)
    disabled, exceptions = _task_type_policy_profile(task_type)
    other_disabled, other_exceptions = _task_type_policy_profile(other)
    if not disabled <= other_disabled:
        return False
    # An exception `other` holds only has to be matched where `task_type` still
    # disables that whole domain. Where `task_type` leaves the domain enabled it
    # already covers every method in it, exception or not.
    still_gated = {
        method for method in other_exceptions
        if method_domain(method) in disabled
    }
    return still_gated <= exceptions


def derive_task_type_capability_bases() -> Dict[str, FrozenSet[str]]:
    """task_type -> every other type whose capability surface it fully covers."""
    return {
        task_type: frozenset(
            other for other in VALID_TASK_TYPES
            if other != task_type and task_type_capability_covers(task_type, other)
        )
        for task_type in VALID_TASK_TYPES
    }


def disabled_reason_for_method(method: str, task_type: object) -> str:
    method = str(method or "").strip()
    if not method:
        return ""
    if method in ALWAYS_FORBIDDEN_ABCP_METHODS:
        return f"{method} is globally disabled by harness policy"
    normalized = resolve_task_type_fail_closed(task_type)
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
