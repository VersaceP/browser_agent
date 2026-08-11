"""What the capture provably contained — and what a model may therefore claim.

Geometry cannot tell us what IS in a screenshot; only the model reading it can.
What geometry can do is the other, cheaper half: rule out claims about a region
that was never in the frame. That asymmetry is the whole design here.

  * `Input.scroll` in target mode returns `targetVisible`. True means the
    element sits inside the root viewport at capture time; a verdict of
    "the region is not in this picture" is then mechanically false.
  * False (or a boundary the scroll could not pass) means the region is not in
    the frame, so any verdict ABOUT ITS CONTENT — present or empty — describes
    something the model did not see, and is inadmissible whatever its stated
    confidence.

An overlay verdict is deliberately exempt from the second rule: a login modal
covers the viewport regardless of where the region sits, so a capture that
missed the region still evidences the overlay.

This module holds no site knowledge and issues no VL calls: it takes a scroll
receipt plus the scope the screenshot was actually taken at, and returns which
of the model's claims survive.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from harness.utils import JsonDict

# Whether the region provably reached the captured frame.
CAPTURE_PROVEN = "proven"
CAPTURE_UNPROVEN = "unproven"
CAPTURE_DISPROVEN = "disproven"

# The classes a `region_reality` verdict may carry. Kept here rather than in
# the prompt module because both the runtime reconciliation and the offline
# precision evaluation must score the same enum.
CLASS_CONTENT_PRESENT = "content_present"
CLASS_EXPLICIT_EMPTY = "explicit_empty_state"
CLASS_AUTH_OVERLAY = "auth_overlay_present"
CLASS_REGION_NOT_IN_CAPTURE = "region_not_in_capture"
CLASS_UNCERTAIN = "uncertain"
REGION_CLASSES = (
    CLASS_CONTENT_PRESENT,
    CLASS_EXPLICIT_EMPTY,
    CLASS_AUTH_OVERLAY,
    CLASS_REGION_NOT_IN_CAPTURE,
    CLASS_UNCERTAIN,
)

# Claims about what the region CONTAINS. Only these need the region to have
# been in frame; an overlay is observable from any capture of the page.
_CONTENT_CLASSES = frozenset({CLASS_CONTENT_PRESENT, CLASS_EXPLICIT_EMPTY})

# Screenshot scopes `_visual_verify` reports. Only an element-bound crop is
# self-evidencing: the platform could not have produced it without resolving
# and framing the element.
SCOPE_ELEMENT = "element"


def _axis(value: Any, *keys: str) -> Optional[float]:
    """Read one axis out of a `{x, y}` / `{width, height}` receipt object."""
    if not isinstance(value, dict):
        return None
    for key in keys:
        raw = value.get(key)
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            continue
        return float(raw)
    return None


def _magnitude(value: Any) -> Optional[float]:
    """Largest absolute axis component of a `{x, y}` delta, or None."""
    if not isinstance(value, dict):
        return None
    magnitude: Optional[float] = None
    for key in ("x", "y"):
        raw = value.get(key)
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            continue
        magnitude = max(magnitude or 0.0, abs(float(raw)))
    return magnitude


def _receipt_data(result: Any) -> Optional[JsonDict]:
    """Unwrap `{response: {data: ...}}`, or accept an already-unwrapped receipt."""
    if not isinstance(result, dict):
        return None
    response = result.get("response")
    if isinstance(response, dict):
        data = response.get("data")
        if isinstance(data, dict):
            return data
    if "completedReason" in result or "totalDelta" in result:
        return result
    return None


def scroll_coverage(scroll_result: Any) -> JsonDict:
    """Normalize an `Input.scroll` receipt into the facts a capture check needs.

    `available: False` means this call produced no receipt at all — a different
    statement from a receipt saying nothing moved, and the two must not collapse
    or an un-run scroll starts looking like a page that refused to move.

    Deliberately omits any at-end inference computed from `position`+`extent`:
    the receipt carries the scrollable extent but not the viewport height, so
    that arithmetic would be a guess. `boundary-reached` is the platform's own
    answer to the same question and is used instead.
    """
    data = _receipt_data(scroll_result)
    if data is None:
        return {"available": False}
    completed = str(data.get("completedReason") or "")
    layers = data.get("layers")
    layers = layers if isinstance(layers, list) else []
    delta = _magnitude(data.get("totalDelta"))
    if delta is None:
        magnitudes = [
            magnitude
            for layer in layers
            if isinstance(layer, dict)
            and (magnitude := _magnitude(layer.get("delta"))) is not None
        ]
        delta = max(magnitudes) if magnitudes else None

    position: Optional[float] = None
    extent: Optional[float] = None
    for layer in layers:
        if not isinstance(layer, dict):
            continue
        if position is None:
            position = _axis(layer.get("position"), "y")
        if extent is None:
            extent = _axis(layer.get("extent"), "height")

    target_visible = data.get("targetVisible")
    coverage: JsonDict = {
        "available": True,
        "mode": str(data.get("mode") or ""),
        "completedReason": completed,
        "stateProbe": completed == "amount-zero",
        "atBoundary": completed == "boundary-reached",
        "delta": delta,
        "position": position,
        "extent": extent,
        "steps": data.get("steps") if isinstance(data.get("steps"), int) else None,
        "layerCount": len(layers),
    }
    if isinstance(target_visible, bool):
        coverage["targetVisible"] = target_visible
    return coverage


def region_in_capture(
    *,
    region_declared: bool,
    screenshot_scope: str,
    coverage: Any,
) -> JsonDict:
    """Did the named region provably reach the captured frame?

    `unproven` is the honest default and covers most captures: a viewport or
    full-page shot may well contain the region, we simply cannot show it did.
    Only `disproven` overrides a model, so an unknown never silences one.
    """
    facts = coverage if isinstance(coverage, dict) else {}
    if not region_declared:
        return {
            "state": CAPTURE_UNPROVEN,
            "reason": "no_region_declared",
        }

    target_visible = facts.get("targetVisible")
    element_bound = str(screenshot_scope or "") == SCOPE_ELEMENT
    if target_visible is False:
        # Two receipts disagree: an earlier scroll says the element was not in
        # the root viewport, a later element-bound capture came back anyway.
        # Which one settles it depends on whether the platform's element
        # capture frames the element itself — a native binding this repo cannot
        # inspect — so neither claim is asserted. `unproven` overrides nothing
        # in either direction, which is the only answer that stays correct
        # under both readings.
        return {
            "state": CAPTURE_UNPROVEN if element_bound else CAPTURE_DISPROVEN,
            "reason": (
                "element_capture_contradicts_scroll_receipt" if element_bound
                else "scroll_reported_target_not_visible"
            ),
        }
    if element_bound:
        # `screenshot_scope` reflects the request path the capture took, not a
        # platform statement that the element was framed. Treated as proof only
        # because nothing contradicts it; the branch above is what happens when
        # something does.
        return {
            "state": CAPTURE_PROVEN,
            "reason": "element_bound_capture",
        }
    if target_visible is True:
        return {
            "state": CAPTURE_PROVEN,
            "reason": "scroll_reported_target_visible",
        }
    # A scroll that hit the boundary without moving never brought anything new
    # into frame; if the region was not already visible it is still not.
    if facts.get("atBoundary") and facts.get("delta") == 0:
        return {
            "state": CAPTURE_UNPROVEN,
            "reason": "scroll_at_boundary_without_movement",
        }
    return {
        "state": CAPTURE_UNPROVEN,
        "reason": f"{str(screenshot_scope or 'unknown')}_capture_not_region_bound",
    }


def reconcile_region_verdict(vl_class: Any, capture: Any) -> JsonDict:
    """Apply the capture facts to the model's class, keeping both on record.

    Returns the class the harness will act on plus, when it differs, what the
    model originally said and which mechanical fact overrode it. Nothing is
    discarded: an override that later proves wrong has to be auditable.
    """
    claimed = str(vl_class or "").strip() or CLASS_UNCERTAIN
    if claimed not in REGION_CLASSES:
        claimed = CLASS_UNCERTAIN
    state = str((capture or {}).get("state") or CAPTURE_UNPROVEN)
    outcome: JsonDict = {"class": claimed, "claimedClass": claimed, "overridden": False}

    if claimed == CLASS_REGION_NOT_IN_CAPTURE and state == CAPTURE_PROVEN:
        outcome.update({
            "class": CLASS_UNCERTAIN,
            "overridden": True,
            "overrideReason": "region_proven_in_capture",
        })
        return outcome
    if claimed in _CONTENT_CLASSES and state == CAPTURE_DISPROVEN:
        outcome.update({
            "class": CLASS_REGION_NOT_IN_CAPTURE,
            "overridden": True,
            "overrideReason": "region_absent_from_capture",
        })
        return outcome
    return outcome


# Evidence grades. `advisory` is the shipping default: a VL class may direct
# more work but may never be the thing that ends it (see
# `vl.reality_check_evidence_mode`).
GRADE_ADVISORY = "advisory"
GRADE_CORROBORATING = "corroborating"


def evidence_grade(
    *,
    evidence_mode: Any,
    resolved_class: Any,
    capture: Any,
) -> Dict[str, Any]:
    """How much weight this verdict carries, and whether it may end work.

    Two rules, and the asymmetry between them is the point:

      * A class that would STOP work (nothing here / cannot see it) carries
        weight only once the model's precision has been measured and the mode
        raised to `corroborating`. Until then it is advisory — it may not
        close a row, and the mechanical absence obligations still apply in
        full.
      * `content_present` always directs work: it can only cause the worker to
        keep reading a page it was about to give up on. A wrong one costs
        steps; a wrong absence costs the answer. So it is never gated on
        precision.
    """
    mode = str(evidence_mode or GRADE_ADVISORY).strip().lower()
    if mode not in {GRADE_ADVISORY, GRADE_CORROBORATING}:
        mode = GRADE_ADVISORY
    resolved = str(resolved_class or CLASS_UNCERTAIN)
    state = str((capture or {}).get("state") or CAPTURE_UNPROVEN)

    if resolved == CLASS_CONTENT_PRESENT:
        return {
            "grade": GRADE_CORROBORATING,
            "mayTerminate": False,
            "directsWork": True,
            "originClass": "model_assertion",
            "reason": "content_present_only_ever_adds_work",
        }
    if mode != GRADE_CORROBORATING:
        return {
            "grade": GRADE_ADVISORY,
            "mayTerminate": False,
            "directsWork": False,
            "originClass": "model_assertion",
            "reason": "vl_precision_not_established",
        }
    if resolved in {CLASS_EXPLICIT_EMPTY, CLASS_AUTH_OVERLAY} and state == CAPTURE_PROVEN:
        return {
            "grade": GRADE_CORROBORATING,
            "mayTerminate": False,
            "directsWork": True,
            "originClass": "model_assertion",
            "reason": "precision_established_and_region_in_capture",
        }
    return {
        "grade": GRADE_ADVISORY,
        "mayTerminate": False,
        "directsWork": False,
        "originClass": "model_assertion",
        "reason": (
            "region_not_proven_in_capture" if state != CAPTURE_PROVEN
            else "class_carries_no_positive_observation"
        ),
    }
