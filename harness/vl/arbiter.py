"""harness.vl.arbiter — which browser_call failures visual perception can help with.

This module used to BE the recovery: on a failed call it picked a VL role, made
a VL request from inside the browser_call hot path, and attached a
recommendation (dismiss here / retry this id / hand to a human). That design was
withdrawn on 2026-09-01 for two independent reasons.

  1. It decided for the model. The harness chose when to look, what to ask, and
     what the answer meant, which left no way for the agent to reach the
     capability on its own judgement and no way for it to decline.

  2. Its routing table did not fire. The gate was an ALLOWLIST of
     visually-arbitrable failures matched partly against prose markers, written
     when ABCP failures still arrived as sentences. The rebuilt contract
     delivers hyphenated public codes, and `target-not-found` does not contain
     `"not found"`. Measured against eleven live codes it matched exactly one
     (`target-occluded`, which has its own explicit entry). The path was, in
     practice, dead code that read as a working feature.

What remains here is the classification alone — the one genuinely reusable
piece — inverted into a DENYLIST so that it stays correct as the platform's code
enum grows. `harness.tools.browser_tools.visual` consults it to decide whether a
failure receipt should carry a `visualRecoveryHint`, and the model decides
everything after that.
"""
from __future__ import annotations

from typing import Any

# Failure classes visual perception genuinely cannot repair.
#
# Anything NOT listed here is eligible, including the generic `action_failure`
# and `unknown` buckets an unrecognized platform code falls into. That default
# is the point: a new code the harness has never seen still reaches the model
# with the visual option attached, whereas the allowlist this replaced would
# have silently withheld it.
_INELIGIBLE_TYPES = frozenset({
    # The call itself was malformed. Read the schema; pixels do not help.
    "contract_error",
    "method_not_found",
    # Nothing reached the page, or the page is not where the answer is.
    "timeout",
    "transport_error",
    "transport_connection_lost",
    "transport_timeout",
    "rpc_error",
    # There is no live surface left to photograph.
    "page_crashed",
    "page_create_failed",
    "render_lost",
    # Already routed to a human. A second opinion does not change that.
    "hitl_paused_state",
    # A located pixel is a click point. It is not a drag, not a scroll, and not
    # a repaired coordinate conversion, so hinting here would be misdirection.
    "drag_unsupported",
    "drag_endpoint_lost",
    "drag_endpoint_ambiguous",
    "scroll_failed",
    "coordinate_unavailable",
})


def visual_recovery_ineligible_reason(classification_type: Any) -> str:
    """Return why this failure class is beyond visual recovery, else "".

    Takes the `errorClassification.type` the harness already computed, so the
    public error code is read through one taxonomy rather than re-matched here
    against prose. An empty/missing type is eligible: an unclassified failure is
    not evidence that looking at the page is pointless.
    """
    ctype = str(classification_type or "").strip().lower()
    if ctype in _INELIGIBLE_TYPES:
        return f"class_not_visually_recoverable:{ctype}"
    return ""


def is_visually_recoverable(classification_type: Any) -> bool:
    return not visual_recovery_ineligible_reason(classification_type)
