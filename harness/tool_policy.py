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


# ABCP method params that carry a secret. The browser still receives the real
# value; these are masked at every harness log/trace/result boundary, and the
# dispatcher fallback withholds an unknown exception's text for such a call.
SENSITIVE_BROWSER_METHOD_PARAMS: Dict[str, FrozenSet[str]] = {
    "Page.handleDialog": frozenset({"userInput"}),
}

# Harness composite tools hidden from the model tool surface for task types
# where they have no legitimate use — pure schema-token/choice-noise savings.
# Mirrors the ABCP-method task_type policy: explicit general remains broad, but
# missing/unknown values resolve to restricted web_scrape defense-in-depth.
# Empty since fill_field_verified was removed; the gate stays as the hook.
HARNESS_TOOLS_HIDDEN_BY_TASK_TYPE: Dict[str, FrozenSet[str]] = {}


def hidden_harness_tools_for_task_type(task_type: object) -> Set[str]:
    return set(
        HARNESS_TOOLS_HIDDEN_BY_TASK_TYPE.get(
            resolve_task_type_fail_closed(task_type)
        )
        or frozenset()
    )


def sensitive_browser_method_params(method: Any) -> Set[str]:
    """Declared secret-bearing parameters for one ABCP method."""
    return set(SENSITIVE_BROWSER_METHOD_PARAMS.get(str(method or "")) or ())


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


# Query-parameter NAMES whose value is a credential wherever it appears. ABCP
# echoes request values back inside its own feedback — `Page.navigate` renders
# the full destination URL into `observation`, and `Input.type` returns the
# typed text as `data.typed` — so masking the outgoing `params` is not enough:
# the value comes back on the response and reaches the run log, the trace and
# the model. Matching is on the parameter name, never on the value's shape, so
# this stays a naming contract rather than an entropy guess.
SENSITIVE_URL_QUERY_KEYS: FrozenSet[str] = frozenset({
    "access_token",
    "api_key",
    "apikey",
    "auth",
    "authorization",
    "id_token",
    "passwd",
    "password",
    "pwd",
    "refresh_token",
    "secret",
    "session",
    "sessionid",
    "session_id",
    "sig",
    "signature",
    "token",
})

# A short value ("1234", a 4-digit PIN or OTP) occurs inside unrelated ids and
# URLs, so replacing it as a SUBSTRING would corrupt the response instead of
# protecting anything. It is still a secret, so it is never discarded: below
# this length a value is scrubbed only when a string equals it exactly, which
# is safe and still covers `data.typed`-style whole-field echoes.
MIN_SUBSTRING_REDACTABLE_LEN = 6


def collect_sensitive_values(
    params: Any,
    redact_params: Optional[Set[str]] = None,
) -> Set[str]:
    """Real values that must not survive anywhere the harness persists a call.

    Two sources, both declared rather than guessed: the caller's own
    `redact_params` keys, and the values of well-known credential query
    parameters inside any URL-shaped string in `params`. URL scanning runs
    even without `redact_params`, because a token in a navigation URL is a
    secret no caller had to opt into.

    Values are never dropped for being short — `redact_values` decides how a
    given length may safely be substituted.
    """
    secrets: Set[str] = set()
    _walk_sensitive(params, redact_params or set(), secrets, depth=0)
    return {s for s in secrets if s}


def _walk_sensitive(
    value: Any,
    redact_params: Set[str],
    out: Set[str],
    *,
    depth: int,
) -> None:
    if depth > 8:
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if key in redact_params and item is not None:
                out.add(str(item))
            _walk_sensitive(item, redact_params, out, depth=depth + 1)
        return
    if isinstance(value, list):
        for item in value:
            _walk_sensitive(item, redact_params, out, depth=depth + 1)
        return
    if isinstance(value, str) and "?" in value and "=" in value:
        out.update(_sensitive_query_values(value))


def _sensitive_query_values(text: str) -> Set[str]:
    return set(_sensitive_query_replacements(text))


def _sensitive_query_replacements(text: str) -> Dict[str, str]:
    """Needle → replacement for every credential carried in a URL string.

    Emits three needles per hit, because one is never enough:
      * the raw percent-encoded value — what ABCP echoes back verbatim
      * its decoded form — what a page or a later receipt renders
      * the whole `name=value` fragment — the only safe way to scrub a SHORT
        credential. A bare "1234" cannot be substring-replaced without
        rewriting unrelated ids, but `token=1234` is unambiguous, so the
        parameter name supplies the context the value itself lacks.
    """
    from urllib.parse import unquote_plus, urlsplit

    try:
        parts = urlsplit(text)
    except ValueError:
        return {}
    # Hash-routed SPAs carry their query inside the fragment
    # (`https://host/#/sign-up?token=...`), where `urlsplit` reports an empty
    # query. Scan both, plus the raw text when it is a bare query string.
    candidates = [parts.query]
    if "?" in parts.fragment:
        candidates.append(parts.fragment.split("?", 1)[1])
    if not parts.scheme and not parts.netloc:
        candidates.append(text.split("?", 1)[-1])
    found: Dict[str, str] = {}
    for query in candidates:
        if not query:
            continue
        for pair in query.split("&"):
            name, sep, raw = pair.partition("=")
            if not sep or name.strip().lower() not in SENSITIVE_URL_QUERY_KEYS:
                continue
            if not raw:
                continue
            masked = mask_token(raw)
            found[f"{name}={raw}"] = f"{name}={masked}"
            found[raw] = masked
            decoded = unquote_plus(raw)
            if decoded and decoded != raw:
                decoded_mask = mask_token(decoded)
                found[decoded] = decoded_mask
                # The decoded form needs its OWN context needle. A short value
                # that was percent-encoded on the way in ("%31%32%33%34") has a
                # long raw form but a short decoded one, so once something
                # echoes the decoded URL the bare needle is exact-match-only
                # and matches nothing inside it.
                found[f"{name}={decoded}"] = f"{name}={decoded_mask}"
    return found


def collect_sensitive_replacements(
    params: Any,
    redact_params: Optional[Set[str]] = None,
) -> Dict[str, str]:
    """Needle → replacement text for everything that must not survive a call.

    A plain set of values cannot express "scrub `token=1234` but leave the
    number 1234 alone elsewhere", which is exactly what a short URL credential
    needs. Carrying the replacement alongside the needle lets a `name=value`
    fragment be rewritten as `name=<masked len=N>` while a bare short value
    stays restricted to whole-field matches.
    """
    replacements: Dict[str, str] = {}
    for secret in collect_sensitive_values(params, redact_params):
        replacements[secret] = mask_token(secret)
    _walk_sensitive_replacements(params, replacements, depth=0)
    return replacements


def _walk_sensitive_replacements(
    value: Any,
    out: Dict[str, str],
    *,
    depth: int,
) -> None:
    if depth > 8:
        return
    if isinstance(value, dict):
        for item in value.values():
            _walk_sensitive_replacements(item, out, depth=depth + 1)
        return
    if isinstance(value, list):
        for item in value:
            _walk_sensitive_replacements(item, out, depth=depth + 1)
        return
    if isinstance(value, str) and "?" in value and "=" in value:
        out.update(_sensitive_query_replacements(value))


def redact_values(value: Any, secrets: Any) -> Any:
    """Replace every occurrence of each secret in a response tree.

    The platform embeds request values inside prose (`observation`) as well as
    in structured fields (`data.typed`), so this substitutes on the string
    contents rather than on key names. Long values are replaced wherever they
    appear; a short value is replaced only when a string IS that value, since
    substring-replacing "1234" would rewrite unrelated ids. Returns the input
    unchanged when there is nothing to scrub, so the ordinary path pays one
    boolean.
    """
    if not secrets:
        return value
    if isinstance(secrets, dict):
        table = dict(secrets)
    else:
        table = {s: mask_token(s) for s in secrets}
    # Longest needle first, so `token=1234` is rewritten as a unit before the
    # bare `1234` is ever considered.
    substring = sorted(
        ((n, r) for n, r in table.items() if len(n) >= MIN_SUBSTRING_REDACTABLE_LEN),
        key=lambda pair: len(pair[0]),
        reverse=True,
    )
    exact = {
        n: r for n, r in table.items() if len(n) < MIN_SUBSTRING_REDACTABLE_LEN
    }
    return _redact_values(value, substring, exact, depth=0)


def _redact_values(
    value: Any,
    substring: list,
    exact: Dict[str, str],
    *,
    depth: int,
) -> Any:
    if depth > 24:
        return value
    if isinstance(value, dict):
        return {
            key: _redact_values(item, substring, exact, depth=depth + 1)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [
            _redact_values(item, substring, exact, depth=depth + 1) for item in value
        ]
    if isinstance(value, str):
        if value in exact:
            return exact[value]
        if value.strip() in exact:
            return exact[value.strip()]
        scrubbed = value
        for needle, replacement in substring:
            if needle in scrubbed:
                scrubbed = scrubbed.replace(needle, replacement)
        return scrubbed
    return value


# Transport logging budget. `log_browser_payloads` exists so a run log can be
# debugged, not so it can hold a verbatim copy of every frame: one screenshot
# `data` field or one API response body would otherwise land in the log at full
# size.
TRANSPORT_LOG_MAX_CHARS = 2000


def sanitize_transport_payload(
    payload: Any,
    secrets: Any = None,
    *,
    max_chars: int = TRANSPORT_LOG_MAX_CHARS,
) -> Any:
    """Value-scrubbed, size-bounded copy of one transport frame for the run log.

    `ABCPClient` emits the raw request and the raw response to its event hook,
    which the run logger writes verbatim. That path bypasses every redaction the
    harness applies on the model-facing side, so a password sent as
    `Input.type.text` and echoed back as `data.typed` persisted in the log even
    after the model-facing copy was scrubbed. The transport is the one point
    every call passes through, so scrubbing here covers both directions at once.

    Two independent controls. Known secrets - the caller's declared
    `redact_params` plus credentials found in URL query strings - are
    substituted by value. Every string is then bounded. Truncation is a size
    control, not a secrecy one: an undeclared secret inside a response body is
    not something this layer can recognise, and is projected at the tool
    boundary instead.
    """
    scrubbed = redact_values(payload, secrets) if secrets else payload
    return _bound_strings(scrubbed, max_chars, depth=0)


def _bound_strings(value: Any, max_chars: int, *, depth: int) -> Any:
    if depth > 24:
        return value
    if isinstance(value, dict):
        return {
            key: _bound_strings(item, max_chars, depth=depth + 1)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_bound_strings(item, max_chars, depth=depth + 1) for item in value]
    if isinstance(value, str) and len(value) > max_chars:
        return value[:max_chars] + f"... <truncated {len(value) - max_chars} chars>"
    return value


def redact_params_for_display(
    params: Any,
    redact_params: Optional[Set[str]] = None,
) -> Any:
    """Masked params for a receipt/log/trace, including in-URL credentials.

    `mask_params` masks only the keys a caller declared, which leaves a token
    sitting in a `url` parameter that nobody had to declare. This applies the
    same value-based scrub the response goes through, so the request side and
    the response side cannot disagree about what is secret.
    """
    masked = mask_params(params, redact_params)
    secrets = collect_sensitive_replacements(params, redact_params)
    return redact_values(masked, secrets) if secrets else masked


HARNESS_DEFAULT_ALLOWED_TOOLS: FrozenSet[str] = frozenset({
    "final_answer",
    "record_extraction",
    "local_fs_search",
    "local_fs_read",
    "read_harness_guide",
    "search_harness_guides",
    "find_in_axtree",
    "navigate_verified",
    "visual_verify",
    "dismiss_overlay",
    "collect_items",
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
# `Fleet.status` was quarantined here (2026-08-23) because it tore down the
# caller's WebSocket: reading status went through `sendAndWait`, which woke a
# stopped Client. ABCP 1.1.9 reads it from durable state instead
# (`hasFleetDirectory` + `sendAndWaitIfRunning`). Re-verified live against the
# running dispatcher on 2026-08-30: four fleets in prepared/active state plus a
# nonexistent fleetId all answered in 2-41ms, every follow-up call on the same
# socket succeeded, and an unknown fleet returned a clean -32009 fleet-not-found.
# `status` is now the enum prepared|active. It proves the Fleet directory exists
# and whether a Client process is running — NOT that any particular page is
# usable, so the readiness barrier deliberately stays on target-scoped Page.list.
#
# `Memory.delete` is in the live capability surface (verified against the
# running dispatcher, 62 capabilities), so this block is load-bearing — a worker
# could otherwise destroy another phase's memory. Every entry must name a method
# the catalog actually publishes: `Hitl.getTaskSummary` / `Hitl.resumeEvent`
# were listed here long after the platform deleted them, which made the set read
# as broader policy than it enforced. The Hitl domain is now requestPause /
# resolvePause only, and wait/resume is owned by harness/hitl.py.
ALWAYS_FORBIDDEN_ABCP_METHODS: FrozenSet[str] = frozenset({
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
