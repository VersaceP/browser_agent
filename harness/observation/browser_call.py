"""
harness.observation.browser_call - the one boundary a browser response crosses.

This layer used to also drive an automatic render-context recovery: on a lost
renderer it re-ran Page.getState / Page.switchTo / Page.navigate and replayed
the original call. That recovery was gated entirely on the raw Chromium strings
``No RenderWidgetHostView`` / ``No WebContents`` leaking out of a native
failure. The current ABCP public failure envelope is rebuilt from scratch
(``projectPublicFailure``) and carries ``error{code,message}``,
``observation`` and ``suggested_prompt``, with bounded optional ``details``;
every native diagnostic, raw error and stack is stripped at the transport - so
those strings can no longer reach the harness and the recovery could never
fire. A lost renderer is now a stable public code (``renderer-lost`` /
``input-host-destroyed``) that
diagnostics.error_classification maps, and the platform ships its own recovery
prompt with it. Every caller also discarded the recovery outcome, so nothing
downstream depended on it.

What remains is load-bearing. This is the ONE place a browser response enters
the harness, so scrubbing sensitive values here covers every downstream surface
- run log, trace, model result, offload - without each call site remembering to
do it.
"""

from typing import Any, Dict, List, Optional, Set

from abcp_client import ABCPClient, ABCPTransportError
from harness.tool_policy import collect_sensitive_replacements, redact_values
from harness.utils import JsonDict, RunLogger


class BrowserCallRunner:
    """A browser client bound to one agent's logger and redaction policy."""

    def __init__(
        self,
        *,
        browser: ABCPClient,
        logger: RunLogger,
        capability_methods: Set[str],
    ):
        self.browser = browser
        self.logger = logger
        self.capability_methods = capability_methods

    async def call(
        self,
        method: str,
        params: JsonDict,
        *,
        redact_params: Optional[Set[str]] = None,
    ) -> JsonDict:
        return await call_browser_redacted(
            browser=self.browser,
            method=method,
            params=params,
            redact_params=redact_params,
        )


def build_browser_call_runner(
    *,
    browser: ABCPClient,
    logger: RunLogger,
    capability_methods: Set[str],
) -> BrowserCallRunner:
    return BrowserCallRunner(
        browser=browser,
        logger=logger,
        capability_methods=capability_methods,
    )


def extract_page_id_from_values(*values: Any) -> Optional[str]:
    stack: List[Any] = list(values)
    while stack:
        value = stack.pop()
        if isinstance(value, dict):
            for key in ("pageId", "page_id"):
                candidate = value.get(key)
                if isinstance(candidate, str) and candidate:
                    return candidate
            stack.extend(value.values())
        elif isinstance(value, list):
            stack.extend(value)
    return None


def _redact_transport_error(exc: ABCPTransportError, secrets: Dict[str, str]) -> None:
    """Scrub a raised transport error in place before it escapes this call.

    JSON-RPC action failures travel as ``ABCPTransportError``, and callers
    persist both ``str(exc)`` and ``exc.rpc_data``. Mutating the live exception
    is what keeps every one of those call sites covered without each having to
    remember to scrub.
    """
    if not secrets:
        return
    rpc_data = getattr(exc, "rpc_data", None)
    if rpc_data is not None:
        exc.rpc_data = redact_values(rpc_data, secrets)
    receipt = getattr(exc, "receipt", None)
    if isinstance(receipt, dict):
        exc.receipt = redact_values(receipt, secrets)
    if exc.args:
        exc.args = tuple(redact_values(list(exc.args), secrets))


async def call_browser_redacted(
    *,
    browser: ABCPClient,
    method: str,
    params: JsonDict,
    redact_params: Optional[Set[str]] = None,
) -> JsonDict:
    """Dispatch one ABCP method, scrubbing declared secrets from what comes back.

    The browser always gets the real params. Masking the request is not enough:
    ABCP echoes request values back in its own feedback (``Input.type`` returns
    ``data.typed``, ``Page.navigate`` renders the destination URL into
    ``observation``), so the response is scrubbed here too. Value-based rather
    than key-based, because a credential sitting in an undeclared ``url`` would
    otherwise survive in a field nobody declared sensitive.
    """
    secrets = collect_sensitive_replacements(params, redact_params)

    # Forwarded so the transport event log scrubs the same values this layer
    # does. Only when set, so runners predating the kwarg (test fakes) keep
    # working on the ordinary non-redacted path.
    call_kwargs = {"redact_params": redact_params} if redact_params else {}

    try:
        response = await browser.call(method, params, **call_kwargs)
    except ABCPTransportError as exc:
        _redact_transport_error(exc, secrets)
        raise
    return redact_values(response, secrets)


__all__ = [
    "BrowserCallRunner",
    "build_browser_call_runner",
    "call_browser_redacted",
    "extract_page_id_from_values",
]
