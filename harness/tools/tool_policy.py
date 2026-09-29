"""
harness.tools.tool_policy - Shared tool policy for BrowserAgent workers.

`allowed_methods` from an LLM-authored worker contract is intentionally not
used as a hard allow-list for ABCP atomic methods. The stable policy is owned
by the harness: universal method restrictions, explicit forbidden_methods,
path and page authorization, and progress/loop guards.
"""

from __future__ import annotations

import json
from typing import Any, Dict, FrozenSet, Iterable, Optional, Set


# ABCP method params that carry a secret. The browser still receives the real
# value; these are masked at every harness log/trace/result boundary, and the
# dispatcher fallback withholds an unknown exception's text for such a call.
SENSITIVE_BROWSER_METHOD_PARAMS: Dict[str, FrozenSet[str]] = {
    "Page.handleDialog": frozenset({"userInput"}),
}

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


# Network.readApi returns response bodies supplied by the site, including
# credentials that did not appear in the Action's input. Mask declared
# credential fields before either transport logging or model/offload delivery.
# Request bodies are not needed to establish whether an upload was attempted
# or accepted and may include form secrets or entire file payloads.
NETWORK_CREDENTIAL_KEYS: FrozenSet[str] = frozenset({
    "accesstoken", "apikey", "auth", "authorization", "clientsecret",
    "cookie", "credential", "credentials", "idtoken", "password", "passwd",
    "pwd", "refreshtoken", "secret", "session", "sessionid",
    "setcookie", "sig", "signature", "token",
})


def _network_safe_json(value: Any, *, depth: int = 0) -> Any:
    if depth > 24:
        return "<redacted deep network value>"
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            normalized = "".join(
                char for char in str(key).lower() if char.isalnum()
            )
            if normalized in NETWORK_CREDENTIAL_KEYS:
                out[key] = "<redacted credential>"
            elif normalized in {"requestbody", "responsebody"}:
                out[key] = "<redacted nested body>"
            else:
                out[key] = _network_safe_json(item, depth=depth + 1)
        return out
    if isinstance(value, list):
        return [_network_safe_json(item, depth=depth + 1) for item in value]
    return value


def sanitize_network_read_api_response(
    response: Any, secrets: Any = None,
) -> Any:
    """Return the diagnostic response without raw request or credential bodies.

    JSON responses retain business codes/messages and other noncredential
    fields. Non-JSON text cannot be separated reliably into diagnostic facts
    and secrets, so only its availability/HTTP metadata remains. Keep this at
    the transport entry point so log, model, trace and offload see the same copy.
    """
    if not isinstance(response, dict):
        return response

    def visit(value: Any, *, depth: int = 0) -> Any:
        if depth > 24:
            return "<redacted deep network value>"
        if isinstance(value, dict):
            out = {}
            for key, item in value.items():
                if key == "requestBody" and item is not None:
                    out[key] = "<redacted request body>"
                elif key == "responseBody" and item:
                    if not isinstance(item, str):
                        out[key] = "<redacted unexpected response body>"
                    else:
                        try:
                            parsed = json.loads(item)
                        except (ValueError, TypeError):
                            out[key] = "<redacted non-JSON response body>"
                        else:
                            if not isinstance(parsed, dict):
                                out[key] = "<redacted non-object JSON response body>"
                            else:
                                safe = _network_safe_json(parsed)
                                safe = redact_values(
                                    safe, collect_sensitive_replacements(safe)
                                )
                                safe = redact_values(safe, secrets)
                                out[key] = json.dumps(safe, ensure_ascii=False)
                elif (
                    "".join(char for char in str(key).lower() if char.isalnum())
                    in NETWORK_CREDENTIAL_KEYS
                ):
                    out[key] = "<redacted credential>"
                else:
                    out[key] = visit(item, depth=depth + 1)
            return out
        if isinstance(value, list):
            return [visit(item, depth=depth + 1) for item in value]
        if isinstance(value, str):
            safe = redact_values(value, collect_sensitive_replacements(value))
            return redact_values(safe, secrets)
        return value

    # Do not run URL substitution over the *serialized* responseBody: parsing
    # its whole JSON string as one URL could consume the closing quotes and
    # break otherwise useful business-error evidence.
    return visit(response)


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
    "execute_saved_browser_workflow",
    "record_extraction",
    "local_fs_search",
    "local_fs_read",
    "local_fs_batch",
    "read_harness_guide",
    "search_harness_guides",
    "find_in_axtree",
    "await_node_change",
    "navigate_verified",
    "visual_verify",
    "dismiss_overlay",
    "collect_items",
})

HARNESS_TOOL_NAMES: FrozenSet[str] = frozenset({
    "browser_call",
    *HARNESS_DEFAULT_ALLOWED_TOOLS,
})

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
# resolvePause only, and wait/resume is owned by harness/runtime/hitl.py.
ALWAYS_FORBIDDEN_ABCP_METHODS: FrozenSet[str] = frozenset({
    "Memory.delete",
    # A worker cannot safely change or disclose the shared Fleet's cookie jar,
    # interception rules or cache for sibling workers. These are session-wide
    # effects, independent of the business task or its description. A future
    # owner-scoped API can expose them after binding the affected Fleet and
    # obtaining explicit authorization for that scope.
    "Network.clearCache",
    "Network.getCookies",
    "Network.setCookies",
    "Network.setInterception",
})

def disabled_reason_for_method(method: str) -> str:
    """Objective restrictions independent of an assignment's business label."""
    method = str(method or "").strip()
    if method in ALWAYS_FORBIDDEN_ABCP_METHODS:
        return f"{method} is globally disabled by harness policy"
    return ""


def filter_capability_methods(methods: Iterable[str]) -> Set[str]:
    return {
        method
        for method in {str(item).strip() for item in methods if str(item).strip()}
        if not disabled_reason_for_method(method)
    }


def capability_policy_facts() -> Dict[str, Any]:
    """Describe universal policy, without implying platform or path availability."""
    return {
        "globallyDisabledMethods": sorted(ALWAYS_FORBIDDEN_ABCP_METHODS),
        "scope": "harness_method_policy_only; platform availability, page binding and path grants are separate",
    }
