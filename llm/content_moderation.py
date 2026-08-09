"""
llm.content_moderation - Detect provider refusals of the request INPUT.

A provider content filter that rejects what we sent is not a retryable
transport fault and not a malformed request: resending the same bytes is
guaranteed to fail again, so the timeout/connection retry ladder in
``llm.base`` cannot help and a bare 400 ends the agent with no trace at all.
The caller that owns the conversation is the only layer that can act — by
removing the bulk it contributed and asking once more — so this module only
answers the narrow question "did the provider refuse our input on moderation
grounds", and leaves the remedy to that caller.
"""

from typing import Any, Optional


# Providers report input moderation with their own vendor code, so this is a
# catalog rather than a rule. Keep it to explicit codes and one unmistakable
# phrase: a generic word such as "content" also appears in ordinary schema
# complaints, and misreading one of those as moderation would strip a tool
# result for nothing. Extend it only with a code seen in a real response.
_INPUT_MODERATION_MARKERS = (
    "sensitivecontentdetected",   # Volcengine Ark
    "datainspectionfailed",       # Alibaba DashScope / Bailian
    "invalid_prompt",             # OpenAI: input rejected before generation
    "input_moderation",
    "may contain sensitive",
)

_MODERATION_STATUS_CODES = (400, 422)


def exception_status_code(exc: BaseException) -> Optional[int]:
    """HTTP status carried by a provider SDK exception, when it has one."""
    status_code = getattr(exc, "status_code", None)
    response = getattr(exc, "response", None)
    if status_code is None and response is not None:
        status_code = getattr(response, "status_code", None)
    try:
        return int(status_code) if status_code is not None else None
    except (TypeError, ValueError):
        return None


def input_moderation_rejection(exc: BaseException) -> Optional[str]:
    """Return the matched moderation marker, or None if this is another 400.

    Fail-closed by design: an unrecognized 400 returns None and keeps its
    existing handling, because folding a tool result cannot fix a malformed
    request and would only destroy context.
    """
    if exception_status_code(exc) not in _MODERATION_STATUS_CODES:
        return None
    text = _exception_text(exc).casefold()
    if not text:
        return None
    return next(
        (marker for marker in _INPUT_MODERATION_MARKERS if marker in text),
        None,
    )


def _exception_text(exc: BaseException) -> str:
    """Flatten the message and any structured body into one searchable string.

    Some SDKs keep the vendor code only in a parsed ``body`` and render a
    generic string, so matching on ``str(exc)`` alone would miss it.
    """
    parts = [str(exc) or ""]
    body: Any = getattr(exc, "body", None)
    if body is not None and not isinstance(body, (str, bytes)):
        parts.append(repr(body))
    elif isinstance(body, str):
        parts.append(body)
    code = getattr(exc, "code", None)
    if isinstance(code, str):
        parts.append(code)
    return " ".join(part for part in parts if part)
