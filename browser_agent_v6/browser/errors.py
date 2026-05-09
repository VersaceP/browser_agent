"""browser_helpers custom exception classes.

Design principle: each exception carries concrete context (selector / url / size /
etc.) so the LLM can write try/except inside run_browser_python and do precise
recovery, instead of being stuck with a generic RuntimeError.

Typical LLM usage in Python code:
    from browser.helpers import click, screenshot, ElementNotFoundError, PendingDialogError

    try:
        click("button.submit", timeout=5)
    except ElementNotFoundError:
        screenshot("debug.png")            # capture evidence on failure
        raise
    except PendingDialogError as e:
        accept_dialog()                    # handle alert and retry
        click("button.submit")
"""


class BrowserError(Exception):
    """Base class for all browser-helper exceptions. Catch-all friendly."""


class DaemonNotReadyError(BrowserError):
    """daemon not started / Chrome unreachable — typically a config issue or Chrome crashed."""


class NavigationTimeoutError(BrowserError):
    """page.get(url) didn't complete within timeout.

    Attributes:
        url: requested URL
        timeout: timeout in seconds
    """
    def __init__(self, url: str, timeout: float, msg: str = ""):
        self.url = url
        self.timeout = timeout
        super().__init__(msg or f"navigate timeout {timeout}s: {url}")


class ElementNotFoundError(BrowserError):
    """selector didn't match any element within timeout.

    Attributes:
        selector: CSS / XPath selector (raw, with css:/xpath: prefix if present)
        timeout: timeout in seconds
    """
    def __init__(self, selector: str, timeout: float, msg: str = ""):
        self.selector = selector
        self.timeout = timeout
        super().__init__(msg or f"element not found ({timeout}s): {selector!r}")


class ElementNotVisibleError(BrowserError):
    """Element exists but did not become visible within timeout
    (display:none / visibility:hidden / zero size)."""
    def __init__(self, selector: str, timeout: float, msg: str = ""):
        self.selector = selector
        self.timeout = timeout
        super().__init__(msg or f"element not visible ({timeout}s): {selector!r}")


class ElementClickInterceptedError(BrowserError):
    """Click was blocked by an overlay (modal / overlay / scrollbar / etc.).
    Try scrolling or handling a dialog first."""
    def __init__(self, selector: str, msg: str = ""):
        self.selector = selector
        super().__init__(msg or f"click intercepted on {selector!r}")


class JSExecutionError(BrowserError):
    """JavaScript thrown inside page.run_js.

    Attributes:
        snippet: failing JS snippet (first 200 chars)
        js_error: browser-side error description
    """
    def __init__(self, snippet: str, js_error: str):
        self.snippet = snippet[:200]
        self.js_error = js_error
        super().__init__(f"JS error: {js_error}; expression: {self.snippet}")


class JSResultTooLargeError(BrowserError):
    """js() return value exceeded the max_result_bytes soft cap.

    On this exception the LLM should switch to run_js_extract / publish_artifact
    and write data to disk, not push a large payload into the LLM context.

    Attributes:
        size_bytes: actual returned bytes
        limit_bytes: configured limit
    """
    _DEFAULT_HINT = (
        "Use page-side aggregation in JS (count / filter / slice) before return, "
        "or write data to file via save_artifact() and only return a summary."
    )

    def __init__(self, size_bytes: int, limit_bytes: int, hint: str = ""):
        self.size_bytes = size_bytes
        self.limit_bytes = limit_bytes
        tip = hint or self._DEFAULT_HINT
        super().__init__(
            f"JS result too large: {size_bytes} bytes (limit {limit_bytes}). {tip}"
        )


class PendingDialogError(BrowserError):
    """A native dialog (alert/confirm/prompt/beforeunload) is blocking the JS thread.

    The LLM MUST call accept_dialog() or dismiss_dialog() before any further
    page operation.

    Attributes:
        dialog_type: alert / confirm / prompt / beforeunload
        message: dialog text
    """
    def __init__(self, dialog_type: str, message: str):
        self.dialog_type = dialog_type
        self.message = message
        super().__init__(f"pending {dialog_type} dialog: {message[:100]}")


class TabNotFoundError(BrowserError):
    """switch_tab couldn't find the target tab."""
    def __init__(self, target, available: list):
        self.target = target
        self.available = available
        super().__init__(f"tab {target!r} not found; available: {available}")


class HumanInterventionRequired(BrowserError):
    """Detected a login page / captcha / Cloudflare challenge — needs human action.

    The LLM should report this upward (to Lead or the user) instead of retrying.
    """
    def __init__(self, reason: str, url: str = ""):
        self.reason = reason
        self.url = url
        super().__init__(f"human required: {reason} @ {url}")


class PageDisconnectedError(BrowserError):
    """Chrome / page handle lost connection with the daemon
    (browser crashed / port closed / page closed).

    helpers has already attempted to re-attach in-process and failed — Chrome is
    really dead and the main-process daemon must restart it. The LLM should stop
    retrying and wait for system-level recovery. The tools layer catches this in
    dispatch and calls daemon.restart() once.

    Attributes:
        original: str repr of the underlying exception (for diagnosis)
        recovered_attempted: whether in-process re-attach was already tried
    """
    def __init__(self, original: str, recovered_attempted: bool = True):
        self.original = original
        self.recovered_attempted = recovered_attempted
        super().__init__(
            f"page disconnected (in-proc recover attempted={recovered_attempted}): {original[:200]}"
        )
