"""Scroll receipt shapes: `Input.scroll` and `Page.wheel` do not match.

Both Actions report movement, and they report it with different field names.
Measured against a live 1.1.9-era build:

    Input.scroll  -> totalDelta {x,y}, actualDistance, requestedDistance,
                     layers[].delta, position, extent, completedReason
    Page.wheel    -> observedDelta {x,y}, requestedDelta {x,y},
                     position, extent, completedReason, layers[]  (NO delta)

`Page.wheel` carries neither `totalDelta` nor `layers[].delta`, so a reader
written against the `Input.scroll` shape gets None for every wheel call. That is
not a loud failure: it reads as "this build ships no movement receipt", which is
the same answer a genuinely old build gives, so a wheel that moved 400px and a
wheel that moved nothing become indistinguishable.

Keeping both shapes here means a reader states WHICH question it is asking, not
which field name it happens to remember.
"""

from __future__ import annotations

import math
from typing import Any, Dict, Optional, Tuple

from harness.utils import JsonDict


# Ordered by specificity, not preference: a receipt carries one or the other.
SCROLL_DELTA_FIELDS: Tuple[str, ...] = ("totalDelta", "observedDelta")

# Current builds answer `state-read`; older builds and fixtures used
# `amount-zero`. Anything else means the call MOVED the page and is therefore
# not a reading of it.
STATE_READ_REASONS = frozenset({"state-read", "amount-zero"})

# Fields whose presence identifies an already-unwrapped scroll receipt.
_RECEIPT_MARKERS = ("completedReason",) + SCROLL_DELTA_FIELDS


def axis_magnitude(value: Any) -> Optional[float]:
    """Largest absolute axis component of a `{x, y}` delta, or None."""
    if not isinstance(value, dict):
        return None
    magnitude: Optional[float] = None
    for axis in ("x", "y"):
        raw = value.get(axis)
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            continue
        magnitude = max(magnitude or 0.0, abs(float(raw)))
    return magnitude


def looks_like_scroll_receipt(data: Any) -> bool:
    """Whether this dict is an already-unwrapped scroll/wheel receipt."""
    return isinstance(data, dict) and any(key in data for key in _RECEIPT_MARKERS)


def scroll_delta_magnitude(data: Any) -> Optional[float]:
    """Pixels the action actually moved, or None when genuinely unreported.

    None and 0 must stay distinct: None means no movement receipt was found at
    all, while 0 is a positive report that the surface did not move.
    """
    if not isinstance(data, dict):
        return None

    for field in SCROLL_DELTA_FIELDS:
        magnitude = axis_magnitude(data.get(field))
        if magnitude is not None:
            return magnitude

    layers = data.get("layers")
    if isinstance(layers, list):
        magnitudes = [
            magnitude
            for layer in layers
            if isinstance(layer, dict)
            and (magnitude := axis_magnitude(layer.get("delta"))) is not None
        ]
        if magnitudes:
            return max(magnitudes)

    # Scalar shape from older builds.
    raw = data.get("deltaApplied")
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return None
    return abs(float(raw))


def _finite_number(value: Any) -> Optional[float]:
    """A real, finite number, or None. `bool` is not a number here."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _proves_zero_movement(data: JsonDict) -> bool:
    """Whether the receipt positively reports that nothing moved.

    Absence is never proof: a receipt that reports no distance at all cannot
    certify that the page held still, so it fails. Both shapes are accepted
    because both are the platform's own answer to the same question.
    """
    # Input.scroll: a scalar distance.
    distance = _finite_number(data.get("actualDistance"))
    if distance is not None:
        return distance == 0.0

    # Page.wheel: a {x, y} delta, which must be zero on BOTH axes.
    for field in SCROLL_DELTA_FIELDS:
        delta = data.get(field)
        if not isinstance(delta, dict):
            continue
        axes = [_finite_number(delta.get(axis)) for axis in ("x", "y")]
        if any(axis is None for axis in axes):
            return False
        return all(axis == 0.0 for axis in axes)

    return False


def root_viewport_position(data: Any) -> Optional[Dict[str, float]]:
    """The MAIN DOCUMENT's scroll offset from a receipt, or None.

    The top-level `position` is not it. The schema calls that field "Final
    position of the dispatch target", and a wheel dispatched at a coordinate
    lands on whatever is under that coordinate - a sidebar, a nested list, an
    iframe. Measured: with the document scrolled to y=500, a zero-delta wheel
    at (0,0) over a fixed sidebar returns top-level `position.y == 0` while
    `layers` carries `{"kind": "viewport", "position": {"y": 500}}`. Reading
    the top level there reports 0 for a page that is at 500.

    So the identity comes from `layers[].kind`, which distinguishes the root
    `viewport` from a `frame-viewport` and from an `element`. That layer is
    present and current whether or not the wheel actually moved it, which is
    what makes a state read at an arbitrary point usable at all.

    `frameDepth` is deliberately NOT a gate: it counts document boundaries
    crossed by the coordinate, which describes the dispatch path rather than
    the identity of any layer. When a root `viewport` layer is reported, it is
    the main document regardless of where the coordinate travelled.

    None when no such layer is present. That includes receipts with no `layers`
    at all: a missing layer list is absent attribution, not evidence that the
    top-level position happens to be the document's - the older `Input.scroll`
    could equally be reporting a container it was pointed at.
    """
    if not isinstance(data, dict):
        return None
    layers = data.get("layers")
    if not isinstance(layers, list):
        return None
    for layer in layers:
        if not isinstance(layer, dict) or layer.get("kind") != "viewport":
            continue
        position = layer.get("position")
        if not isinstance(position, dict):
            return None
        axes = [_finite_number(position.get(axis)) for axis in ("x", "y")]
        if any(axis is None for axis in axes):
            return None
        return {"x": axes[0], "y": axes[1]}
    return None


def scroll_dispatch_target(data: Any) -> Optional[JsonDict]:
    """Which layer the wheel/scroll was actually dispatched into, if stated.

    `layers[0]` is the dispatch target and the rest are the surfaces the event
    propagated to; measured against live receipts, the top-level `position`
    equals `layers[0].position` in both the hit-an-element and hit-the-viewport
    cases. Returns ``{"kind", "id"}`` - an observation about WHERE a scroll
    went, never a verdict about whether it should have gone there.

    It cannot say whether that layer MOVED: `Page.wheel` reports each layer's
    final position and no per-layer delta, so a layer appearing here is not
    proof it scrolled.
    """
    if not isinstance(data, dict):
        return None
    layers = data.get("layers")
    if not isinstance(layers, list) or not layers:
        return None
    first = layers[0]
    if not isinstance(first, dict):
        return None
    kind = str(first.get("kind") or "")
    if not kind:
        return None
    target: JsonDict = {"kind": kind}
    layer_id = str(first.get("id") or "")
    if layer_id:
        target["id"] = layer_id
    return target


def scroll_state_read_position(data: Any) -> Optional[Dict[str, float]]:
    """A certified MAIN-DOCUMENT scroll position from a state read, or None.

    Three things must all hold, and each is a positive statement in the
    receipt: the call was a state read, it proves it moved nothing, and it
    reports a root viewport layer to attribute the position to. Shared by every
    VL path so none of them can silently manufacture geometry.

    A missing distance is not zero, booleans are not numbers even though
    Python's ``bool`` subclasses ``int``, and an unattributable position is not
    the document's.
    """
    if not isinstance(data, dict):
        return None
    if str(data.get("completedReason") or "") not in STATE_READ_REASONS:
        return None
    if not _proves_zero_movement(data):
        return None
    return root_viewport_position(data)
