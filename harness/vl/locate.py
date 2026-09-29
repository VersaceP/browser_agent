"""harness.vl.locate — VL Role A: AXTree-blindspot locate + bbox→id promotion.

When the AXTree can't resolve a target (canvas, text-in-image, purely visual
control), VL points at it visually; the harness then PROMOTES that pixel back to a
durable canonical id by reverse-looking-up the AXTree bbox that contains it, so
subsequent actions use a stable handle (id / role+name) instead of raw coordinates.

LIVE-VERIFIED foundation (WebCross 0.9.3 unified page observation, measured
2026-09-21; this replaces the 2026-08-31 reading of the retired AX format):
  - `DOM.getAXTree` node lines carry `@x,y,w,h`, the node's box in VIEWPORT
    CSS pixels: rootWebArea is `@0,0,1224,724` beside a 2448x1448 PNG at
    scaleFactor 2, and an in-flow paragraph moved from y=5156 to y=3156 when
    the page scrolled 2000px. The retired format was device pixels offset by
    the root scroll; neither translation applies any more.
  - The screenshot is device pixels of the CROP, whose (0,0) is the crop's own
    corner, and `Input.click` takes the same VIEWPORT CSS pixels as the boxes.
    See `capture_origin` and `promote_locate` for the one translation left.
  - An iframe's boxes are FRAME-LOCAL (its button sat at `@8,8` inside a frame
    placed far down the page), so they must never win a main-document
    containment test — `point_to_id` filters them out by document.

SCOPE of that verification: the main document scrolled and unscrolled, a
same-page iframe, fixed and stuck-sticky positioning, a nested scroll
container, a CSS transform (the painted box, 180x60 for a scaled 120x40
button) and an open shadow root, with viewport / region / element captures.
Element captures scroll the page to reveal their target (1107 -> 400 for a
fixed button), and both the capture and the AXTree rect are the integer rect
enclosing the element's fractional box.

NOT covered, and not claimed: browser zoom (the Action catalog has no setter,
so it can only be recorded); a relayout that leaves both the scroll offset and
the target's rect unchanged; and the gap between the image being taken and the
first geometry read, which no receipt in this catalog closes. Those are why
the harness withholds a coordinate rather than trusting an unproven one, and
why a located target still has to be verified by re-observing after the click.

Promote-then-heal discipline (doc §13.2): coordinates NEVER persist into a skill
(they rot faster than CSS selectors). A located pixel is converted to the durable
id immediately; only when NO bbox contains it (genuine AXTree blind spot) does the
caller fall back to a one-shot coordinate action.
"""
from __future__ import annotations

import math
import re
from typing import Any, Awaitable, Callable, Dict, List, Optional

from harness.observation.axtree_format import parse_axtree_line
from harness.observation.page_observation import node_document_roots
from harness.observation.scroll_receipt import STATE_READ_REASONS, scroll_state_read_position

# Page-level containers are never a useful click target — resolving a pixel to one
# of them means "no specific element here" → coords fallback (AXTree blind spot).
_NON_PROMOTABLE_ROLES = frozenset({"rootwebarea", "webarea", "document"})

# Sentinel: "resolve the main frame from the bboxes themselves".
MAIN_FRAME = "auto"


def parse_axtree_bboxes(lines: List[Any]) -> List[Dict[str, Any]]:
    """Parse rect-bearing AXTree lines into
    {id, frame, depth, role, name, x, y, w, h, area}. `frame` identifies the
    document that holds the node - the id of its nearest `rootWebArea`
    ancestor, since ids no longer encode a frame. Nodes of an embedded iframe
    belong to that iframe's document and their bbox is FRAME-LOCAL (starts at
    0,0 inside the iframe), not viewport-space.

    Reads the line through the shared parser rather than a local regex. The
    local one searched for the first `@x,y,w,h` ANYWHERE after the name, and an
    accessible name is free text the formatter does not escape - so a label
    reading `Read "@0,0,100,100" manual` handed this function a rectangle from
    the label instead of the element's own. That is not a cosmetic error here:
    these boxes are what a located pixel is tested against, so a phantom box
    promotes a VL point to an element it never touched.
    """
    out: List[Dict[str, Any]] = []
    text_lines = [ln for ln in lines or [] if isinstance(ln, str)]
    documents = node_document_roots(text_lines)
    for ln in text_lines:
        parsed = parse_axtree_line(ln)
        if parsed is None:
            continue
        rect = parsed["rect"]
        if not isinstance(rect, dict):
            continue
        gid = str(parsed["id"])
        x, y = float(rect["x"]), float(rect["y"])
        w, h = float(rect["w"]), float(rect["h"])
        out.append({"id": gid, "frame": documents.get(gid),
                    "depth": parsed["depth"],
                    "role": parsed["role"], "name": parsed["name"],
                    "x": x, "y": y, "w": w, "h": h, "area": max(0.0, w) * max(0.0, h)})
    return out


def main_frame_id(
    bboxes: List[Dict[str, Any]],
    *,
    shot_w: Optional[float] = None,
    shot_h: Optional[float] = None,
) -> Optional[str]:
    """Frame seq of the main document. Preference order:
      1. when screenshot dims are known, the page container whose bbox is
         closest to them (the main root tracks the viewport; iframe roots are
         frame-sized). `min` is stable, so an exact-tie full-viewport iframe
         still loses to the earlier main root in document order;
      2. the first depth-0 page-level container in document order with a sane
         bbox (the payload starts with the main frame's rootwebarea; iframe
         subtrees are appended after it);
      3. the largest-area page container;
      4. the first bbox's frame (subtree-scoped payloads without a root)."""
    roots = [
        b for b in bboxes
        if str(b.get("role", "")).lower() in _NON_PROMOTABLE_ROLES
    ]
    if roots and shot_w and shot_h:
        best = min(roots, key=lambda b: abs(b["w"] - shot_w) + abs(b["h"] - shot_h))
        return str(best.get("frame") or "") or None
    for b in roots:
        if b.get("depth") in (0, None) and b["w"] > 0 and b["h"] > 0:
            return str(b.get("frame") or "") or None
    if roots:
        best = max(roots, key=lambda b: b["area"])
        return str(best.get("frame") or "") or None
    return str(bboxes[0].get("frame") or "") or None if bboxes else None


def point_to_id(
    bboxes: List[Dict[str, Any]],
    px: float,
    py: float,
    *,
    frame: Optional[str] = MAIN_FRAME,
) -> Optional[Dict[str, Any]]:
    """Return the SMALLEST-area bbox containing (px, py) — the most specific element
    — or None if nothing contains it. Skips zero-area boxes.

    Frame handling: iframe boxes are frame-local, so a screen-space containment
    test against them is meaningless and false-hits (e.g. a video ad's 64×64
    Pause button at local @10,308 capturing a main-frame point). By DEFAULT only
    main-frame boxes are considered (`frame=MAIN_FRAME` resolves it from the
    bboxes); pass an explicit frame seq to scope differently, or `frame=None`
    to opt out of filtering entirely."""
    if frame == MAIN_FRAME:
        frame = main_frame_id(bboxes)
    best: Optional[Dict[str, Any]] = None
    for b in bboxes:
        if b["w"] <= 0 or b["h"] <= 0:
            continue
        if frame is not None and str(b.get("frame") or "") != frame:
            continue
        if str(b.get("role", "")).lower() in _NON_PROMOTABLE_ROLES:
            continue  # page container → not a real target
        if b["x"] <= px <= b["x"] + b["w"] and b["y"] <= py <= b["y"] + b["h"]:
            if best is None or b["area"] < best["area"]:
                best = b
    return best


# Scopes whose crop origin is the viewport's own (0,0) by construction, so no
# receipt is needed to prove it. Every other scope carries an offset that has to
# be recovered from the capture receipt — see `capture_origin`.
COORDINATE_SAFE_SCOPES = frozenset({"", "viewport", "viewport_fallback"})

# Reported sizes are CSS pixels and come back as whole numbers; one pixel of
# slack absorbs sub-pixel layout rounding without letting a genuinely different
# element pass the identity check.
_SIZE_MATCH_TOLERANCE = 1.0

def capture_origin(
    *,
    scope: str,
    shot_data: Optional[Dict[str, Any]] = None,
    scroll: Optional[Dict[str, float]] = None,
) -> Dict[str, Any]:
    """Prove where a capture's pixel (0,0) sits, in VIEWPORT CSS pixels.

    A cropped capture's pixel (0,0) is the crop's origin, not the page's, so a
    point read off one is meaningless until that offset is known. The harness
    used to refuse every crop for want of the offset; measuring the running
    build (2026-08-31, catalogRevision sha256:cfd8fb90…) showed it is carried by
    the capture's own receipt:

      * element — `targetDetail.info.boundsInWidget` of the captured node, in
        viewport CSS pixels. Measured on WebCross 0.9.3 (2026-09-21): the
        platform scrolls an out-of-view element into view before capturing and
        reports the box at its capture-time position (a button at viewport
        y=2621 was captured at y=342). The box is the element's FULL rect; the
        capture is that rect clipped to the viewport, so the origin is its
        near edge clamped at 0.
      * region — the requested `x`/`y`, echoed on `target`. Measured to be CSS
        pixels and viewport-relative: after the page scrolled 1660px the same
        request captured a different part of the document.
      * full page — nothing in the receipt states the scroll offset, so this
        stays unprovable.

    The origin is only ever accepted alongside its own proof: the node named by
    the capture's canonical id must still report the visible size the capture
    reports. Returns ``{"x", "y", "source", "proven"}``; an unproven origin must
    withhold the coordinate rather than fall back to (0, 0).

    RESIDUAL RACE, unmitigated: the platform reads the target detail AFTER
    taking the image, so an element that translated in between keeps both its
    identity and its size while its reported x/y no longer describe the crop.
    Nothing in the receipt exposes that, so this returns `proven` for it, and
    NOTHING DOWNSTREAM CATCHES IT — `promotion_guard` compares role and label,
    not position, and the coordinate path only suggests a point to the model
    with no enforced post-click verification. The identity check narrows the
    window (a wrong node is rejected; only a moved right node slips through)
    but does not close it. Closing it needs either an atomic
    capture-plus-geometry receipt from the platform or an enforced outcome
    check after the action.

    CURRENT POSTURE (2026-09-01), stated plainly because the window is still
    open: `visual_locate_enabled` now defaults ON, and the residual race is
    handled by DISCIPLINE, not by a mechanism. A coordinate is offered to the
    model, never executed by the harness; the model issues the single
    Input.click itself and is instructed — in the tool receipt, the
    `visualRecoveryHint`, and the BrowserAgent SOP — to re-observe afterwards
    rather than assume the click landed. That converts an undetected wrong
    click into a detected one, which is the best available answer until the
    platform ships an atomic receipt. It is not equivalent to closing the race,
    and nothing here should be read as claiming it is.
    """
    data = shot_data if isinstance(shot_data, dict) else {}
    # `scroll` is the bracketed scroll offset (`scroll_bracket`): present only
    # when the reads taken after the capture and after the AXTree agree. Both
    # the capture and the AXTree are viewport-relative, so the offset itself
    # is not needed; its presence is the evidence that the page did not scroll
    # between them.
    offset = scroll

    def _out(x, y, source, proven):
        receipt = {"x": x, "y": y, "source": source, "proven": proven}
        if offset is not None:
            receipt["scrollX"] = offset["x"]
            receipt["scrollY"] = offset["y"]
            receipt["scrollProven"] = True
        else:
            receipt["scrollProven"] = False
        return receipt

    if str(scope or "") in COORDINATE_SAFE_SCOPES:
        return _out(0.0, 0.0, "viewport", True)
    target = data.get("target") if isinstance(data.get("target"), dict) else {}
    width, height = data.get("width"), data.get("height")

    if str(scope) == "region":
        x, y = target.get("x"), target.get("y")
        if x is None or y is None:
            return _out(0.0, 0.0, "region_origin_missing", False)
        # Measured: a request reaching past the right or bottom edge keeps its
        # origin and is trimmed at the FAR edge (x=1220 w=200 returned 60), so
        # a smaller capture does not invalidate the origin. A NEGATIVE request
        # is clamped to 0 and trimmed at the NEAR edge (x=-50 w=200 returned
        # 150), which moves the origin — hence max(0, ...) rather than the
        # echoed value.
        try:
            req_x, req_y = float(x), float(y)
            req_w, req_h = float(target.get("width")), float(target.get("height"))
            got_w, got_h = float(width), float(height)
        except (TypeError, ValueError):
            return _out(0.0, 0.0, "region_receipt_incomplete", False)
        max_w = req_w - max(0.0, -req_x)
        max_h = req_h - max(0.0, -req_y)
        if not (0 < got_w <= max_w + _SIZE_MATCH_TOLERANCE
                and 0 < got_h <= max_h + _SIZE_MATCH_TOLERANCE):
            return _out(0.0, 0.0, "region_size_impossible", False)
        return _out(max(0.0, req_x), max(0.0, req_y), "region_request", True)

    if str(scope) != "element":
        return _out(0.0, 0.0, f"unsupported_scope:{scope}", False)

    detail = data.get("targetDetail") if isinstance(data.get("targetDetail"), dict) else None
    if detail is None or width is None or height is None:
        return _out(0.0, 0.0, "element_receipt_incomplete", False)
    # Identity has to come from a node id, never from a matching size: an
    # element that moved between the capture and the detail read keeps its
    # width and height, so a size match would hand back a confidently wrong
    # x/y. The platform reads the detail for the node it actually captured and
    # names it in `context.targetId`; a requested id must agree with it.
    context = detail.get("context") if isinstance(detail.get("context"), dict) else {}
    info = detail.get("info") if isinstance(detail.get("info"), dict) else {}
    requested = str(target.get("id") or "")
    captured = str(context.get("targetId") or "")
    if requested and captured and requested != captured:
        return _out(0.0, 0.0, "element_node_unmatched", False)
    wanted = requested or captured
    if not wanted:
        return _out(0.0, 0.0, "element_no_canonical_id", False)
    bounds = info.get("boundsInWidget")
    if not isinstance(bounds, dict):
        return _out(0.0, 0.0, "element_node_unmatched", False)
    try:
        bx, by = float(bounds["x"]), float(bounds["y"])
        bw, bh = float(bounds["width"]), float(bounds["height"])
        got_w, got_h = float(width), float(height)
    except (KeyError, TypeError, ValueError):
        return _out(0.0, 0.0, "element_bounds_invalid", False)
    # The capture is the element's box clipped to the viewport (measured: the
    # platform scrolls the element into view first, then reports its
    # `boundsInWidget` in viewport CSS pixels). An overhang past the near edge
    # moves the origin to 0 and shortens the capture; one past the far edge
    # only shortens it. A capture LARGER than the visible box is a layout that
    # no longer matches the detail read.
    visible_w = bw - max(0.0, -bx)
    visible_h = bh - max(0.0, -by)
    if not (0 < got_w <= visible_w + _ENCLOSING_RECT_SLACK
            and 0 < got_h <= visible_h + _ENCLOSING_RECT_SLACK):
        return _out(0.0, 0.0, "element_bounds_stale", False)
    # The capture is the integer rect enclosing the box, so its pixel (0,0)
    # sits at the box's near edges rounded DOWN (y=517.5 captured from 517).
    receipt = _out(
        float(math.floor(max(0.0, bx))), float(math.floor(max(0.0, by))),
        "element_target_detail", True,
    )
    # Carried so the promotion can ask, from the AXTree it already reads,
    # whether this element is still where the capture found it. The FULL box:
    # the AXTree also stores the unclipped rect, so comparing it with the
    # clipped origin would call every overhanging element "moved".
    receipt["nodeId"] = wanted
    receipt["nodeBounds"] = {"x": bx, "y": by, "width": bw, "height": bh}
    return receipt


def _coordinate_fallback(
    px: float,
    py: float,
    *,
    reason: str,
    dpr_receipt: Optional[Dict[str, Any]],
    scope: str,
    origin_receipt: Optional[Dict[str, Any]] = None,
    refuse: Optional[str] = None,
    extra: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Build the no-promotion result, refusing coordinates unless they are safe.

    `Input.click` takes viewport CSS pixels while the screenshot is device
    pixels of a crop, so the fallback needs a proven scale AND a provable
    origin. Missing either, the point is withheld: a wrong coordinate click
    lands on a real element and reports success, which is worse than no
    fallback at all. `refuse` withholds it for a reason the receipts cannot
    express by themselves — both may be internally sound while describing a
    page that has since changed.
    """
    receipt = dict(dpr_receipt or {"dpr": 1.0, "source": "unproven", "proven": False})
    origin = dict(origin_receipt or capture_origin(scope=scope))
    out: Dict[str, Any] = {
        "resolved": False,
        "reason": reason,
        "pxPoint": {"x": px, "y": py},
        "dpr": receipt,
        "origin": origin,
        **(extra or {}),
    }
    if refuse:
        # The receipts each look sound in isolation but no longer describe the
        # page, so the coordinate they would produce is as wrong as the id.
        out["coordinateRefused"] = refuse
        return out
    if not receipt.get("proven"):
        out["coordinateRefused"] = "dpr_unproven"
        return out
    if not origin.get("proven"):
        out["coordinateRefused"] = f"crop_origin_unprovable:{origin.get('source')}"
        return out
    d = float(receipt.get("dpr") or 1.0) or 1.0
    # Crop-local device pixels -> viewport CSS, which is the space `Input.*`
    # takes. For a viewport capture the origin is (0,0) and this reduces to the
    # plain scale division it has always been.
    out["cssPoint"] = {
        "x": float(origin.get("x") or 0.0) + px / d,
        "y": float(origin.get("y") or 0.0) + py / d,
    }
    return out


# Both the AXTree rect and the capture are the integer rect ENCLOSING the
# element's fractional box (measured: a box at y=337.5 h=50 read `@0,337,140,51`
# and captured 51px tall), so each edge may round once - up to ~2 CSS px per
# dimension. Anything larger is the element actually having moved.
_TARGET_DRIFT_TOLERANCE = 2.0
_ENCLOSING_RECT_SLACK = 2.0


def _capture_target_moved(
    bboxes: List[Dict[str, Any]],
    origin: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    """Whether the captured element's rect has changed since the capture.

    Free evidence: the AXTree the promotion already reads carries the target's
    current rect, and the receipt recorded the one the capture saw. If they
    disagree, the crop's geometry describes a layout that has moved on.

    Both rects must be the SAME rect, and both are viewport CSS pixels: the
    AXTree stores the full box, so this compares against the receipt's full
    `nodeBounds`, never the clamped crop origin — those two diverge exactly
    when the element is clipped by the viewport, which is the shape a dropdown
    popup usually has. Width and height are compared too: a popup that loads
    more options grows downward without its origin moving at all.

    Returns None when there is nothing to compare — a viewport capture, or a
    target with no accessibility node (a plain `div` never has one), so absence
    is not evidence either way and must not refuse a promotion.
    """
    node_id = origin.get("nodeId")
    recorded = origin.get("nodeBounds")
    if not node_id or not origin.get("proven") or not isinstance(recorded, dict):
        return None
    box = next((b for b in bboxes if b["id"] == node_id), None)
    if box is None:
        return None
    expected = {
        "x": float(recorded["x"]),
        "y": float(recorded["y"]),
        "w": float(recorded["width"]),
        "h": float(recorded["height"]),
    }
    found = {"x": box["x"], "y": box["y"], "w": box["w"], "h": box["h"]}
    if all(abs(found[k] - expected[k]) <= _TARGET_DRIFT_TOLERANCE
           for k in ("x", "y", "w", "h")):
        return None
    return {"capturedAt": expected, "foundAt": found}


# The two axes of a derived scale must agree this closely; a capture whose axes
# scale differently is not a uniform rescale of the root box.
_ROOT_SCALE_AXIS_TOLERANCE = 0.02


def _root_scale(
    bboxes: List[Dict[str, Any]],
    shot_w: float,
    shot_h: float,
) -> Optional[Dict[str, Any]]:
    """Device-pixel ratio of a viewport capture, from the main root box.

    The main document's rootWebArea box is the viewport in CSS pixels and a
    viewport capture is the same area in device pixels, so their ratio is the
    scale - but only when both axes give the same answer.
    """
    main = main_frame_id(bboxes)
    root = next(
        (b for b in bboxes
         if b.get("frame") == main and str(b.get("role", "")).lower() in _NON_PROMOTABLE_ROLES
         and b["w"] > 0 and b["h"] > 0),
        None,
    )
    if root is None or not shot_w or not shot_h:
        return None
    scale_x = float(shot_w) / root["w"]
    scale_y = float(shot_h) / root["h"]
    if abs(scale_x - scale_y) > _ROOT_SCALE_AXIS_TOLERANCE * max(scale_x, scale_y):
        return None
    if not (_MIN_PROVABLE_DPR <= scale_x <= _MAX_PROVABLE_DPR):
        return None
    return {"dpr": (scale_x + scale_y) / 2, "source": "png_vs_axtree_root", "proven": True}


def promote_locate(
    axtree_lines: List[Any],
    point_norm: Dict[str, Any],
    *,
    shot_w: float,
    shot_h: float,
    dpr: float = 1.0,
    dpr_receipt: Optional[Dict[str, Any]] = None,
    scope: str = "",
    origin_receipt: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Map a normalized 0-1000 VL point to screenshot px, then promote to a durable
    AXTree id via bbox containment. Returns:
      {resolved:True, id, label, role, bbox, pxPoint}                — durable handle
      {resolved:False, cssPoint, pxPoint, reason:"no_bbox_contains"} — coords fallback
      {resolved:False, pxPoint, coordinateRefused:"..."}             — no safe point

    Containment is tested in VIEWPORT CSS pixels. Measured on WebCross 0.9.3
    (2026-09-21): every AXTree rect is the node's box in viewport CSS pixels -
    rootWebArea `@0,0,1224,724` beside a 2448x1448 PNG at scaleFactor 2 - and
    it follows scrolling (an in-flow paragraph moved from y=5156 to y=3156
    across a 2000px scroll). The screenshot is device pixels of the crop, so a
    pixel maps to `origin + px/scale`, which is also the point `Input.click`
    takes. An iframe's boxes stay frame-local and are excluded by document.

    The scale is the capture's proven device-pixel ratio. A viewport capture
    whose ratio is unproven derives it from the PNG against the main document's
    root box, accepted only when both axes agree.

    The bracketed scroll read (`scrollProven`) no longer translates anything:
    it is the evidence that the page did not scroll between the capture and
    the AXTree read, without which the boxes describe another viewport.

    An unproven scroll costs only the promotion: the coordinate is still exact,
    so the result degrades to the coordinate fallback instead of refusing."""
    px = float(point_norm.get("x", 0.0)) / 1000.0 * float(shot_w or 0.0)
    py = float(point_norm.get("y", 0.0)) / 1000.0 * float(shot_h or 0.0)
    if dpr_receipt is None and dpr:
        # Legacy callers that passed a bare, already-trusted scale factor.
        dpr_receipt = {"dpr": float(dpr), "source": "caller", "proven": True}
    origin = dict(origin_receipt or capture_origin(scope=scope))
    # Containment must be gated BEFORE the lookup, not after: the AXTree covers
    # the whole viewport, so an untranslated crop-local pixel lands on whatever
    # unrelated node happens to occupy those coordinates and comes back as a
    # confident — and wrong — durable id.
    offset_x = float(origin.get("x") or 0.0)
    offset_y = float(origin.get("y") or 0.0)
    if not origin.get("proven"):
        return _coordinate_fallback(
            px, py,
            reason="crop_origin_unprovable",
            dpr_receipt=dpr_receipt,
            scope=scope,
            origin_receipt=origin,
        )
    bboxes = parse_axtree_bboxes(axtree_lines)
    viewport_capture = str(scope or "") in COORDINATE_SAFE_SCOPES
    if not (dpr_receipt or {}).get("proven") and viewport_capture:
        derived = _root_scale(bboxes, shot_w, shot_h)
        if derived is not None:
            dpr_receipt = derived
    if not (dpr_receipt or {}).get("proven"):
        return _coordinate_fallback(
            px, py,
            reason="crop_scale_unprovable",
            dpr_receipt=dpr_receipt,
            scope=scope,
            origin_receipt=origin,
        )
    if not origin.get("scrollProven"):
        # Without a stable scroll the boxes may describe another viewport than
        # the image. The coordinate does not depend on them, so hand back the
        # exact viewport point rather than refusing outright.
        return _coordinate_fallback(
            px, py,
            reason="scroll_unprovable",
            dpr_receipt=dpr_receipt,
            scope=scope,
            origin_receipt=origin,
        )
    scale = float((dpr_receipt or {}).get("dpr") or 1.0) or 1.0
    hit_x = offset_x + px / scale
    hit_y = offset_y + py / scale
    moved = _capture_target_moved(bboxes, origin)
    if moved is not None:
        # The element the crop was taken of is no longer where the receipt put
        # it, so the origin describes a layout that has since changed — and
        # both products of that origin are wrong, the durable id and the
        # coordinate alike. This covers the window holding the visual-locate
        # call, where a popup animating into place or a list reflowing does its
        # damage — but ONLY for an element capture whose target has an id AND
        # appears in the AXTree. A popup the accessibility tree cannot see gets
        # no guard at all, and that is one of the main reasons visual recovery
        # exists, so this is a narrow mitigation, not a solved race.
        return _coordinate_fallback(
            px, py,
            reason="capture_target_moved",
            dpr_receipt=dpr_receipt,
            scope=scope,
            origin_receipt=origin,
            refuse="capture_target_moved",
            extra={"capturedAt": moved["capturedAt"],
                   "foundAt": moved["foundAt"]},
        )
    # Promotion only trusts main-document boxes: iframe boxes are frame-local,
    # so a genuine iframe target correctly falls through to the cssPoint
    # fallback (coordinate clicks are viewport-space and reach iframe content).
    # A crop is smaller than the viewport, so its dimensions must not be used
    # to pick the main document — the root box would never look closest.
    frame = (
        main_frame_id(bboxes, shot_w=shot_w / scale, shot_h=shot_h / scale)
        if viewport_capture
        else main_frame_id(bboxes)
    )
    hit = point_to_id(bboxes, hit_x, hit_y, frame=frame)
    if hit is not None:
        return {"resolved": True, "id": hit["id"], "label": hit["name"],
                "role": hit["role"], "bbox": hit,
                "pxPoint": {"x": px, "y": py},
                "viewportCssPoint": {"x": hit_x, "y": hit_y},
                "dpr": dpr_receipt,
                "origin": origin}
    return _coordinate_fallback(
        px, py,
        reason="no_bbox_contains",
        dpr_receipt=dpr_receipt,
        scope=scope,
        origin_receipt=origin,
    )


async def locate_target(
    browser: Any,
    page_id: str,
    target: str,
    *,
    vl_config: Any,
    screenshot_fn: Callable[..., Awaitable[Any]],
    axtree_fn: Optional[Callable[..., Awaitable[List[Any]]]] = None,
    visual_locate_fn: Optional[Callable[..., Awaitable[Dict[str, Any]]]] = None,
    dpr_receipt: Optional[Dict[str, Any]] = None,
    scope: str = "viewport",
    origin_receipt: Optional[Dict[str, Any]] = None,
    logger: Any = None,
) -> Dict[str, Any]:
    """Full Role-A flow: screenshot → VL visual_locate → promote pixel to a durable
    id (or coords fallback). Returns:
      {ok:True, id, label, ...}                 — use Input.click({id})  (durable)
      {ok:True, coordinate:True, cssPoint, ...} — AXTree blind spot; one-shot coords
      {ok:False, reason:"coordinate_refused"}   — no id and no provable scale
      {ok:False, reason}                        — VL disabled / not found / error
    `is_consequential` from VL is surfaced so the caller can block sensitive targets.

    A caller holding the capture receipt passes `origin_receipt` from
    `capture_origin` so a cropped capture can be translated instead of refused;
    without one the origin is derived from `scope` alone, which only a viewport
    capture can prove.
    """
    if vl_config is None or not getattr(vl_config, "enabled", False):
        return {"ok": False, "reason": "vl_disabled"}
    shot = await screenshot_fn(browser, page_id)
    # Immediately after the capture, before the model call. An element capture
    # scrolls its target into view first, so reading before the capture would
    # describe a position the image never had. This is the earliest harness can
    # read, but it is not atomic with image capture: movement between the image
    # and this read remains unobservable (and keeps the feature flag off).
    scroll_at_capture = await read_scroll(browser, page_id)
    # The contract accepts either a bare path or `{path, receipt}`. The receipt
    # form is what lets this entry point prove the capture's scale itself,
    # instead of depending on a caller to have passed one.
    shot_receipt: Optional[Dict[str, Any]] = None
    if isinstance(shot, dict):
        shot_receipt = shot.get("receipt") if isinstance(shot.get("receipt"), dict) else None
        shot = shot.get("path")
    if not shot:
        return {"ok": False, "reason": "no_screenshot"}
    vl = await (visual_locate_fn or _default_visual_locate)(vl_config, shot, target)
    if vl.get("verdict") != "located" or not vl.get("point"):
        return {"ok": False, "reason": f"vl_{vl.get('verdict', 'failed')}",
                "consequential": bool(vl.get("is_consequential"))}

    dims = await _screenshot_dims(shot)
    # Scale, in order of preference: what the caller passed, what the capture's
    # own receipt proves, then the page-state reader — which on ABCP 1.1.9
    # reports no scale factor at all and therefore yields an explicitly
    # unproven receipt. An unproven scale refuses the coordinate rather than
    # assuming 1.0 and clicking at half the intended point.
    receipt = dpr_receipt
    if receipt is None and shot_receipt is not None:
        receipt = screenshot_dpr(
            png_width=dims[0], png_height=dims[1],
            reported_width=shot_receipt.get("width"),
            reported_height=shot_receipt.get("height"),
            scale_factor=shot_receipt.get("scaleFactor"),
        )
    if receipt is None:
        receipt = await _viewport_dpr(browser, page_id)
    axtree_lines = await _resolve_axtree(browser, page_id, axtree_fn)
    origin = origin_receipt
    if origin is None:
        # The far end of the bracket: taken after the AXTree. Agreement proves
        # stability from the first post-capture read through this read; it
        # cannot prove the small image-to-first-read gap.
        origin = capture_origin(
            scope=scope, shot_data=shot_receipt,
            scroll=scroll_bracket(
                scroll_at_capture, await read_scroll(browser, page_id)
            ),
        )
    promo = promote_locate(
        axtree_lines,
        vl["point"], shot_w=dims[0], shot_h=dims[1],
        dpr_receipt=receipt, scope=scope, origin_receipt=origin,
    )
    promo = apply_promotion_guard(
        promo, vl_label=vl.get("control_label"), expected_text=target,
        dpr_receipt=receipt, scope=scope, origin_receipt=origin,
        logger=logger, page_id=page_id,
    )
    _log(logger, "vl.locate.result", {
        "pageId": page_id, "target": target[:80],
        "resolved_id": promo.get("id"), "label": promo.get("label") or vl.get("control_label"),
        "consequential": bool(vl.get("is_consequential")),
        "promotionGuard": promo.get("promotionGuard"),
    })
    common = {"ok": True, "label": vl.get("control_label") or promo.get("label"),
              "consequential": bool(vl.get("is_consequential")),
              "confidence": vl.get("confidence")}
    if promo.get("resolved"):
        return {**common, "id": promo["id"], "role": promo.get("role"),
                "bbox": promo.get("bbox")}
    if promo.get("coordinateRefused"):
        return {**common, "ok": False, "reason": "coordinate_refused",
                "coordinateRefused": promo["coordinateRefused"],
                "dpr": promo.get("dpr")}
    out = {**common, "coordinate": True, "cssPoint": promo["cssPoint"],
           "dpr": promo.get("dpr"),
           "reason": promo.get("reason") or "axtree_blind_spot"}
    if promo.get("promotionGuard"):
        out["promotionGuard"] = promo["promotionGuard"]
        out["demotedId"] = promo.get("demotedId")
    return out


_LABEL_TOKEN_RE = re.compile(r"[a-z0-9一-鿿]{2,}")

# Post-promotion sanity families. `media` roles are scrub/playback controls —
# a locate that asked for a link/button/field should never promote to one.
_ROLE_FAMILIES = {
    "link": "nav", "button": "nav", "menuitem": "nav", "tab": "nav",
    "checkbox": "form", "radio": "form", "radiobutton": "form", "switch": "form",
    "textbox": "form", "searchbox": "form", "combobox": "form", "listbox": "form",
    "option": "form", "spinbutton": "form",
    "slider": "media", "togglebutton": "media", "timer": "media",
    "progressbar": "media", "scrollbar": "media", "video": "media",
    "audio": "media",
}
# Tokens in the caller's target description / VL label that declare an expected
# control kind. Only `media`-family promotions conflict with them: nav vs form
# confusion ("button" that is really a checkbox) is common and harmless.
_EXPECTED_KIND_TOKENS = {
    "nav": ("link", "button", "menu", "tab", "链接", "按钮", "菜单", "选项卡"),
    "form": ("checkbox", "radio", "switch", "textbox", "input", "search",
             "field", "输入", "搜索", "复选", "单选", "开关"),
}
# When the expectation itself talks about playback ("the video pause button"),
# a media-family promotion is consistent — the role rule must stay silent.
_MEDIA_CONTEXT_TOKENS = (
    "play", "pause", "volume", "seek", "mute", "video", "audio", "slider",
    "progress", "scrub", "播放", "暂停", "音量", "进度", "静音", "滑块",
    "视频", "音频",
)


def _labels_disagree(vl_label: Any, promoted_label: Any) -> bool:
    """True only when BOTH labels are non-empty and share no token and neither
    contains the other — icon buttons / empty AX names never trigger this."""
    a = str(vl_label or "").strip().lower()
    b = str(promoted_label or "").strip().lower()
    if not a or not b:
        return False
    if a in b or b in a:
        return False
    return not (set(_LABEL_TOKEN_RE.findall(a)) & set(_LABEL_TOKEN_RE.findall(b)))


def promotion_guard(
    *,
    expected_text: Any = None,
    vl_label: Any = None,
    promoted_role: Any = None,
    promoted_label: Any = None,
) -> Optional[Dict[str, Any]]:
    """Light post-promotion sanity rules. Returns a {reason, ...} dict when the
    promoted node should be DEMOTED to the cssPoint fallback, else None.

    Deliberately light — demotion costs only the durable id (the coordinate
    click still lands on the exact pixel VL located), so rules favor recall of
    bad promotions over precision:
      - role_conflict: the request names a link/button/field kind but the bbox
        resolved to a media/scrub control (slider, togglebutton, video...).
      - label_disjoint: both labels have text and share zero vocabulary. This
        WILL demote cross-language pairs like "submit" vs "Continue"; that is
        accepted for now — tune via the promotion_guard logs. Empty/icon labels
        never trigger it.
    """
    role = str(promoted_role or "").strip().lower()
    family = _ROLE_FAMILIES.get(role)
    expectation = f"{expected_text or ''} {vl_label or ''}".lower()
    if family == "media" and not any(
        token in expectation for token in _MEDIA_CONTEXT_TOKENS
    ):
        for kind, tokens in _EXPECTED_KIND_TOKENS.items():
            if any(token in expectation for token in tokens):
                return {
                    "reason": "role_conflict",
                    "expectedKind": kind,
                    "promotedRole": role,
                }
    if _labels_disagree(vl_label, promoted_label):
        return {
            "reason": "label_disjoint",
            "vlLabel": str(vl_label or "")[:80],
            "promotedLabel": str(promoted_label or "")[:80],
        }
    return None


def apply_promotion_guard(
    promo: Dict[str, Any],
    *,
    vl_label: Any = None,
    expected_text: Any = None,
    dpr: float = 1.0,
    dpr_receipt: Optional[Dict[str, Any]] = None,
    scope: str = "",
    origin_receipt: Optional[Dict[str, Any]] = None,
    logger: Any = None,
    page_id: str = "",
) -> Dict[str, Any]:
    """Run `promotion_guard` over a resolved promotion; on a hit, demote it to
    the same shape as the no-containment fallback (resolved:False + cssPoint)
    with `promotionGuard`/`demoted*` attached for logging and tuning.

    The demotion goes through the same coordinate gate as the no-containment
    path: a guard hit means the id was wrong, which is no reason to trust an
    unproven pixel-to-CSS conversion instead."""
    if not promo.get("resolved"):
        return promo
    guard = promotion_guard(
        expected_text=expected_text,
        vl_label=vl_label,
        promoted_role=promo.get("role"),
        promoted_label=promo.get("label"),
    )
    if guard is None:
        return promo
    px_point = dict(promo.get("pxPoint") or {})
    if dpr_receipt is None and dpr:
        dpr_receipt = {"dpr": float(dpr), "source": "caller", "proven": True}
    demoted = _coordinate_fallback(
        float(px_point.get("x", 0.0)),
        float(px_point.get("y", 0.0)),
        reason="promotion_guard",
        dpr_receipt=dpr_receipt,
        scope=scope,
        origin_receipt=origin_receipt or promo.get("origin"),
        extra={
            "promotionGuard": guard,
            "demotedId": promo.get("id"),
            "demotedRole": promo.get("role"),
            "demotedLabel": promo.get("label"),
        },
    )
    _log(logger, "vl.locate.promotion_guard", {
        "pageId": page_id,
        "guard": guard,
        "demotedId": promo.get("id"),
        "demotedRole": promo.get("role"),
        "demotedLabel": str(promo.get("label") or "")[:80],
        "vlLabel": str(vl_label or "")[:80],
    })
    return demoted


# ── default I/O wiring (live-verified primitives) ───────────────────────────────

async def _default_visual_locate(vl_config: Any, image_path: str, target: str) -> Dict[str, Any]:
    from harness.vl.core import visual_verify_image
    return await visual_verify_image(
        config=vl_config, image_path=image_path, expected={"target": target},
        mode="visual_locate", question=target,
    )


# Kept as this module's exported names. The certification itself lives in
# harness.observation.scroll_receipt, which knows both zero-movement shapes: `Input.scroll`
# proves it with a scalar `actualDistance`, `Page.wheel` with a two-axis
# `observedDelta`. Reading only the scalar made every wheel state read fail
# certification and return None, which silently disabled cssPoint promotion.
_STATE_READ_REASONS = STATE_READ_REASONS
_scroll_position_from_state_read = scroll_state_read_position


def _is_state_read(data: Any) -> bool:
    """Compatibility predicate for callers that only need certification."""
    return _scroll_position_from_state_read(data) is not None


def scroll_bracket(
    before: Optional[Dict[str, float]],
    after: Optional[Dict[str, float]],
) -> Optional[Dict[str, float]]:
    """The scroll offset when two post-capture observations agree.

    A scroll read AFTER the fact proves where the page was when the READ
    happened, not when the image was taken, and seconds can pass in between —
    a visual-locate call, a lazy load, a popup that scrolls itself into view.
    The pair is taken immediately after the capture and again after the AXTree
    read, covering the model call and the large majority of the image-to-bbox
    window. Agreement does NOT prove the small gap between the image being
    captured and the first read; the platform would need an atomic geometry
    receipt to close that gap. This helper is therefore a fail-closed stability
    check, not an atomic snapshot proof.

    Returns None when either read is missing or they disagree, which withholds
    the promotion rather than promoting against stale geometry. Note this
    cannot see a relayout that leaves the scroll offset unchanged; that is the
    same unmitigated race `capture_origin` documents.
    """
    if before is None or after is None:
        return None
    if abs(before["x"] - after["x"]) > 0.5 or abs(before["y"] - after["y"]) > 0.5:
        return None
    return dict(after)


async def read_scroll(browser: Any, page_id: str) -> Optional[Dict[str, float]]:
    """Read the document scroll offset, the one piece of geometry no capture
    receipt reliably carries.

    Uses `Page.wheel` with a zero delta, which the platform documents and was
    measured to treat as a state read: it answers `completedReason:
    "state-read"` with `observedDelta: {x: 0, y: 0}` and the current
    `position`, leaving the page untouched. That receipt is why a scroll ACTION
    is acceptable as a read here — it states that it did not scroll, so a read
    that silently moved the page cannot be mistaken for one that did not, and
    this refuses anything that does not say so.

    This used to send `Input.scroll` in viewport mode. That mode was removed:
    every branch of the Action now requires `target` or `container`, so the
    call came back `invalid-params` and this returned None for every capture.
    Root-viewport scrolling moved to `Page.wheel`, whose `x`/`y` must be inside
    the viewport.

    `Page.getState` carries no scroll field on this build.
    """
    try:
        resp = await browser.call("Page.wheel", {
            "pageId": page_id,
            "x": 0,
            "y": 0,
            "deltaX": 0,
            "deltaY": 0,
            "purpose": "read the scroll offset for VL coordinate mapping",
        })
    except Exception:
        return None
    data = ((resp or {}).get("data") or {})
    return _scroll_position_from_state_read(data)


async def _resolve_axtree(browser: Any, page_id: str,
                          axtree_fn: Optional[Callable[..., Awaitable[List[Any]]]]) -> List[Any]:
    if axtree_fn is not None:
        return await axtree_fn(browser, page_id)
    resp = await browser.call("DOM.getAXTree", {
        "pageId": page_id, "purpose": "read structure to promote a VL pixel to a canonical id",
    })
    data = ((resp or {}).get("data") or {})
    lines = data.get("lines")
    return lines if isinstance(lines, list) else []


# A device-pixel ratio outside this range is not a display scale factor, it is
# a parse or capture accident. Refusing it is what keeps an unproven number
# from silently becoming a click 2x away from the target.
_MIN_PROVABLE_DPR = 0.5
_MAX_PROVABLE_DPR = 8.0
# The two axes must agree; a capture whose axes scale differently is not a
# uniform rescale and no single ratio can map it back to CSS pixels.
_DPR_AXIS_TOLERANCE = 0.02
# The two independent signals should normally be identical. Leave a small
# margin for integer image dimensions and capture metadata rounding.
_DPR_SIGNAL_TOLERANCE = 0.01


def screenshot_dpr(
    *,
    png_width: float,
    png_height: float,
    reported_width: Any,
    reported_height: Any,
    scale_factor: Any = None,
) -> Dict[str, Any]:
    """Prove the capture's device-pixel ratio from the screenshot receipt.

    `Page.screenshot` reports CSS dimensions while the encoded file contains
    device pixels, so their ratio is the measured scale. Current ABCP also
    returns `scaleFactor`, but that field can silently fall back to 1 when
    display metrics are unavailable. Treat it as corroboration, never as a
    substitute for the image-to-receipt measurement.

    Returns ``{"dpr", "source", "proven"}``. When the ratio cannot be proven
    the caller must refuse a coordinate action rather than assume 1.0.
    """
    receipt: Dict[str, Any] = {"dpr": 1.0, "source": "unproven", "proven": False}
    explicit: Optional[float] = None
    if scale_factor is not None:
        try:
            explicit = (
                None if isinstance(scale_factor, bool) else float(scale_factor)
            )
        except (TypeError, ValueError):
            explicit = None
        if (
            explicit is None
            or not (_MIN_PROVABLE_DPR <= explicit <= _MAX_PROVABLE_DPR)
        ):
            receipt["source"] = "invalid_screenshot_scale_factor"
            receipt["scaleFactor"] = scale_factor
            return receipt
    try:
        css_w = float(reported_width or 0.0)
        css_h = float(reported_height or 0.0)
    except (TypeError, ValueError):
        if explicit is not None:
            receipt["source"] = "scale_factor_without_pixel_ratio"
            receipt["scaleFactor"] = explicit
        return receipt
    if css_w <= 0 or css_h <= 0 or png_width <= 0 or png_height <= 0:
        if explicit is not None:
            receipt["source"] = "scale_factor_without_pixel_ratio"
            receipt["scaleFactor"] = explicit
        return receipt
    ratio_x = float(png_width) / css_w
    ratio_y = float(png_height) / css_h
    if abs(ratio_x - ratio_y) > _DPR_AXIS_TOLERANCE * max(ratio_x, ratio_y):
        receipt["source"] = "axis_mismatch"
        receipt["ratioX"] = round(ratio_x, 4)
        receipt["ratioY"] = round(ratio_y, 4)
        return receipt
    if not (_MIN_PROVABLE_DPR <= ratio_x <= _MAX_PROVABLE_DPR):
        receipt["source"] = "out_of_range"
        receipt["ratioX"] = round(ratio_x, 4)
        if explicit is not None:
            receipt["scaleFactor"] = explicit
        return receipt
    if explicit is not None:
        if abs(ratio_x - explicit) > _DPR_SIGNAL_TOLERANCE * max(
            ratio_x, explicit
        ):
            return {
                "dpr": 1.0,
                "source": "scale_factor_disagrees_with_pixel_ratio",
                "proven": False,
                "ratio": round(ratio_x, 4),
                "scaleFactor": explicit,
            }
        return {
            "dpr": ratio_x,
            "source": "screenshot_receipt+scale_factor",
            "proven": True,
            "ratio": round(ratio_x, 4),
            "scaleFactor": explicit,
        }
    return {
        "dpr": ratio_x,
        "source": "screenshot_receipt",
        "proven": True,
    }


async def _viewport_dpr(browser: Any, page_id: str) -> Dict[str, Any]:
    """Read an optional native page-state scale factor without executing JS.

    Kept as a last-resort compatibility path for builds that expose a scale
    factor through page state but not the screenshot receipt.
    """
    try:
        resp = await browser.call("Page.getState", {
            "pageId": page_id,
            "purpose": "read native page metrics for VL coordinate mapping",
        })
        data = ((resp or {}).get("data") or {})
        raw = (
            data.get("deviceScaleFactor")
            or data.get("devicePixelRatio")
            or data.get("dpr")
        )
        if raw:
            dpr = float(raw)
            if _MIN_PROVABLE_DPR <= dpr <= _MAX_PROVABLE_DPR:
                return {"dpr": dpr, "source": "page_state", "proven": True}
    except Exception:
        pass
    return {"dpr": 1.0, "source": "unproven", "proven": False}


async def _screenshot_dims(path: str) -> tuple[float, float]:
    """Read a PNG's width/height from its IHDR header (no PIL dependency)."""
    import struct
    try:
        with open(path, "rb") as f:
            head = f.read(26)
        if head[:8] == b"\x89PNG\r\n\x1a\n":
            w, h = struct.unpack(">II", head[16:24])
            return float(w), float(h)
    except (OSError, struct.error):
        pass
    return 0.0, 0.0


def _log(logger: Any, event: str, payload: Dict[str, Any]) -> None:
    if logger is not None and hasattr(logger, "write"):
        try:
            logger.write(event, payload)
        except Exception:  # pragma: no cover
            pass
