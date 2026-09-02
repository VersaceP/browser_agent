"""harness.vl.locate — VL Role A: AXTree-blindspot locate + bbox→id promotion.

When the AXTree can't resolve a target (canvas, text-in-image, purely visual
control), VL points at it visually; the harness then PROMOTES that pixel back to a
durable canonical id by reverse-looking-up the AXTree bbox that contains it, so
subsequent actions use a stable handle (id / role+name) instead of raw coordinates.

LIVE-VERIFIED foundation (2026-08-31 matrix probe, superseding the 2026-06-27
reading that the two spaces were simply equal):
  - `DOM.getAXTree` lines carry `# @x,y,w,h` on positioned/interactive
    elements, e.g. `[3:13:13] link "Learn more" # @512,398,164,39`. The rect is
    `(viewport position + root scroll) x captureScale`, in device pixels. That
    equals the DOCUMENT position for elements in normal flow — after scrolling
    300px an in-flow button's bbox was unchanged at `@440,7440` — but NOT for a
    fixed element, a stuck sticky element, or one inside a nested scroll
    container, all of which keep a viewport position the page scroll does not
    describe. Measured on all four (probe_capture_topology).
  - The screenshot is device pixels of the CROP, whose (0,0) is the crop's own
    corner, and `Input.click` takes VIEWPORT CSS pixels.
  - The three coincide only on an unscrolled, uncropped page, which is why the
    original reading held for so long. See `capture_origin` and `promote_locate`
    for the two translations that connect them.
  - An iframe's boxes are FRAME-LOCAL and carry that frame's own seq in their
    canonical id, so they must never win a main-document containment test —
    `point_to_id` filters them out by frame.

SCOPE of that verification (probe_capture_geometry + probe_capture_topology):
the main document, nested scroll containers, fixed/sticky positioning,
same-page iframes, CSS transforms, and open Shadow DOM. A closed shadow root is
not exposed at all, which is the safe outcome — a node the harness cannot name
is one whose origin it cannot prove.

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

# `[<canonical id>] <role> "<name>" ... # @x,y,w,h`  (id like 3:13:13 or uuid:.:.)
_AX_LINE = re.compile(
    r"\[([0-9a-fA-F:\-]+)\]\s+(\S+)(?:\s+\"([^\"]*)\")?.*?@(-?\d+),(-?\d+),(\d+),(\d+)"
)
# Page-level containers are never a useful click target — resolving a pixel to one
# of them means "no specific element here" → coords fallback (AXTree blind spot).
_NON_PROMOTABLE_ROLES = frozenset({"rootwebarea", "webarea", "document"})


# Leading depth prefix of the current line format (`3 [3:426:426] link ...`);
# legacy indent-based lines have no prefix → depth stays None.
_AX_DEPTH = re.compile(r"^\s*(\d+)\s+\[")

# Sentinel: "resolve the main frame from the bboxes themselves".
MAIN_FRAME = "auto"


def parse_axtree_bboxes(lines: List[Any]) -> List[Dict[str, Any]]:
    """Parse `# @x,y,w,h`-bearing AXTree lines into
    {id, frame, depth, role, name, x, y, w, h, area}. `frame` is the first
    canonical-id segment: nodes from embedded iframes carry a DIFFERENT frame seq
    and their bbox is FRAME-LOCAL (starts at 0,0 inside the iframe), not
    screen-space. `depth` is the leading original-tree depth when present."""
    out: List[Dict[str, Any]] = []
    for ln in lines or []:
        if not isinstance(ln, str):
            continue
        m = _AX_LINE.search(ln)
        if not m:
            continue
        gid, role, name, x, y, w, h = m.groups()
        x, y, w, h = int(x), int(y), int(w), int(h)
        depth_m = _AX_DEPTH.match(ln)
        out.append({"id": gid, "frame": gid.split(":", 1)[0],
                    "depth": int(depth_m.group(1)) if depth_m else None,
                    "role": role, "name": name or "",
                    "x": x, "y": y, "w": w, "h": h, "area": max(0, w) * max(0, h)})
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

# A capture may legitimately be SHORTER or NARROWER than the node's visible box
# by the width of a scrollbar: `visibleBounds` clips to the layout viewport
# while the capture clips to what was actually painted. Measured at 15px on this
# build (a 1400px-tall element in an 800px viewport reported 785). Anything
# beyond this is a layout that no longer matches the capture.
_SCROLLBAR_ALLOWANCE = 20.0


def _fits_capture(bounds: Any, width: Any, height: Any) -> bool:
    """The node's visible box still describes what was captured."""
    if not isinstance(bounds, dict):
        return False
    try:
        dw = float(bounds.get("width")) - float(width)
        dh = float(bounds.get("height")) - float(height)
    except (TypeError, ValueError):
        return False
    return (
        -_SIZE_MATCH_TOLERANCE <= dw <= _SCROLLBAR_ALLOWANCE
        and -_SIZE_MATCH_TOLERANCE <= dh <= _SCROLLBAR_ALLOWANCE
    )


def tree_scroll(tree: Any) -> Optional[Dict[str, float]]:
    """The document scroll offset a Semantic Tree carries on its root node.

    Needed because an AXTree bbox is `(viewport + root scroll) x scale` while a
    screenshot is viewport-relative — verified live: after scrolling 300px an
    in-flow button's bbox was unchanged at `@440,7440`, and `rootwebarea`
    stayed `@0,0,2560,1600`, so neither the boxes nor the root reveal the
    offset. It is
    not on `Page.getState` either. `Input.scroll` with top-level `amount: 0`
    also reports it without moving on 1.1.9, but the Semantic Tree keeps this
    geometry read independent of the scroll action being diagnosed.

    The frame is resolved through `rootFrameId` rather than taken as `frames[0]`:
    on a page with iframes the first entry need not be the anchored document,
    and an iframe's scroll offset is not the main document's.

    Returns None unless the resolved root really is the document node, because
    only the scrolling element reports the scroll — see the note in the body.
    """
    from harness.semantic_frames import root_tree

    root = root_tree(tree)
    if not isinstance(root, dict):
        return None
    # The root of a tree that came back with an ELEMENT CAPTURE is the target's
    # ancestor chain truncated to a depth limit, so for a deeply nested target
    # it is `body`, not `#document` — and `body` is not the scrolling element,
    # so its `scroll.top` is a genuine 0 while the page is scrolled 1107px
    # (measured). Reading that as the document offset shifts every promotion by
    # the whole scroll distance, silently. Only the document node states the
    # document's own offset.
    if str(root.get("tag") or "").lower() not in ("#document", "html"):
        return None
    scroll = root.get("scroll")
    if not isinstance(scroll, dict):
        return None
    # Fail closed on a scroll object that does not actually state the offset:
    # `{}` means "this payload did not say", not "the page is at the top", and
    # reading it as (0,0) is the silent-zero this whole receipt chain exists to
    # avoid.
    if scroll.get("left") is None or scroll.get("top") is None:
        return None
    try:
        return {"x": float(scroll["left"]), "y": float(scroll["top"])}
    except (TypeError, ValueError):
        return None


def _tree_nodes(node: Any, depth: int = 0):
    """Every dict node in a Semantic Tree payload.

    The tree that rides along with an element capture is the WHOLE document
    (`{rootFrameId, frames: [{frameId, tree}]}`), not the target's subtree
    (verified live), so finding the target means walking all of it.
    """
    if depth > 40:
        return
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from _tree_nodes(value, depth + 1)
    elif isinstance(node, list):
        for item in node:
            yield from _tree_nodes(item, depth + 1)


def _bounds_match(bounds: Any, width: Any, height: Any) -> bool:
    if not isinstance(bounds, dict):
        return False
    try:
        return (
            abs(float(bounds.get("width")) - float(width)) <= _SIZE_MATCH_TOLERANCE
            and abs(float(bounds.get("height")) - float(height)) <= _SIZE_MATCH_TOLERANCE
        )
    except (TypeError, ValueError):
        return False


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

      * element — the target's node in the returned Semantic Tree, whose
        `visibleBounds` is the origin. Note `visibleBounds`, NOT `bounds`: the
        two differ exactly when the element is taller than the viewport, which
        is the case that matters. A 300x1400 element reported `width`/`height`
        of 300x800 and `visibleBounds` of 300x800 while `bounds` still said
        1400, because the platform clips the capture to the viewport. An
        element below the fold is scrolled into view first rather than clipped.
      * region — the requested `x`/`y`, echoed on `target`. Measured to be CSS
        pixels and viewport-relative: after the page scrolled 1660px the same
        request captured a different part of the document.
      * full page — nothing in the receipt states the scroll offset, so this
        stays unprovable.

    The origin is only ever accepted alongside its own proof: the node named by
    the capture's canonical id must still report the visible size the capture
    reports. Returns ``{"x", "y", "source", "proven"}``; an unproven origin must
    withhold the coordinate rather than fall back to (0, 0).

    RESIDUAL RACE, unmitigated: the platform reads the Semantic Tree AFTER
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
    # The scroll offset either comes with the capture (element captures carry a
    # Semantic Tree) or is supplied by a caller that read it separately.
    offset = scroll or tree_scroll(data.get("semanticTree"))

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

    tree = data.get("semanticTree")
    if tree is None or width is None or height is None:
        return _out(0.0, 0.0, "element_receipt_incomplete", False)
    # Identity has to come from a canonical id. A selector capture echoes only
    # the selector, and matching on the capture's size instead proves the wrong
    # thing: a size is not an identity. An element that moved between the
    # capture and the tree read keeps its width and height, so a size match
    # would hand back a confidently wrong x/y — the exact failure mode this
    # whole receipt chain exists to prevent.
    #
    # `recoveredTarget.currentId` is authoritative when present: the platform
    # reads the tree for that same recovered node (`resolveSemanticTreeTarget`
    # prefers `recoveredTarget.currentId` over the requested id/selector), so it
    # names what was actually captured.
    recovered = data.get("recoveredTarget")
    if isinstance(recovered, dict) and recovered.get("currentId"):
        wanted = str(recovered["currentId"])
    elif target.get("id"):
        wanted = str(target["id"])
    else:
        return _out(0.0, 0.0, "element_no_canonical_id", False)

    bounds = None
    node_bounds = None
    for node in _tree_nodes(tree):
        if str(node.get("id") or "") != wanted:
            continue
        candidate = node.get("visibleBounds")
        if isinstance(candidate, dict):
            bounds = candidate
        node_bounds = node.get("bounds")
        break
    if bounds is None:
        return _out(0.0, 0.0, "element_node_unmatched", False)
    if not _fits_capture(bounds, width, height):
        # The node is the right one but its visible box no longer describes
        # what was captured, so the capture and the tree disagree about layout.
        return _out(0.0, 0.0, "element_bounds_stale", False)
    try:
        receipt = _out(float(bounds.get("x")), float(bounds.get("y")),
                       "element_visible_bounds", True)
    except (TypeError, ValueError):
        return _out(0.0, 0.0, "element_bounds_invalid", False)
    # Carried so the promotion can ask, from the AXTree it already reads,
    # whether this element is still where the capture found it. The FULL box,
    # not the visible one: `visibleBounds` is the crop origin, but the AXTree
    # stores the whole painted rect — measured, a button overflowing the left
    # edge reported `visibleBounds.x = 0` against an AXTree x of -120 device
    # px. Comparing the two would call every clipped element "moved".
    receipt["nodeId"] = wanted
    full = node_bounds if isinstance(node_bounds, dict) else None
    if full is not None:
        try:
            receipt["nodeBounds"] = {
                "x": float(full["x"]), "y": float(full["y"]),
                "width": float(full["width"]), "height": float(full["height"]),
            }
        except (KeyError, TypeError, ValueError):
            pass
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

    `Input.click` takes CSS pixels while the AXTree bbox and the screenshot are
    both device pixels, so the fallback needs a proven scale AND a provable
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


# Sub-pixel layout rounding, in DEVICE pixels: anything larger is the element
# actually having moved.
_TARGET_DRIFT_TOLERANCE = 2.0


def _capture_target_moved(
    bboxes: List[Dict[str, Any]],
    origin: Dict[str, Any],
    scale: float,
) -> Optional[Dict[str, Any]]:
    """Whether the captured element's rect has changed since the capture.

    Free evidence: the AXTree the promotion already reads carries the target's
    current rect, and the receipt recorded the one the capture saw. If they
    disagree, the crop's geometry describes a layout that has moved on.

    Both rects must be the SAME rect. The AXTree stores the full painted box,
    so this compares against the receipt's `nodeBounds`, never the
    `visibleBounds` that supplied the crop origin — those two diverge exactly
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
        "x": (float(recorded["x"]) + float(origin.get("scrollX") or 0.0)) * scale,
        "y": (float(recorded["y"]) + float(origin.get("scrollY") or 0.0)) * scale,
        "w": float(recorded["width"]) * scale,
        "h": float(recorded["height"]) * scale,
    }
    found = {"x": box["x"], "y": box["y"], "w": box["w"], "h": box["h"]}
    if all(abs(found[k] - expected[k]) <= _TARGET_DRIFT_TOLERANCE
           for k in ("x", "y", "w", "h")):
        return None
    return {"capturedAt": expected, "foundAt": found}


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

    Containment is tested in DEVICE pixels, not CSS: on this platform the AXTree
    `# @x,y,w,h` rect and the saved PNG occupy the same device-pixel space
    (verified live — rootwebarea `@0,0,2448,1452` against a 2448x1452 PNG whose
    receipt reports 1224x726 CSS). Converting before the hit test would miss
    every box.

    Two translations stand between a screenshot pixel and a bbox, and both were
    measured rather than assumed:

      * the crop origin — a crop's pixel (0,0) is its own corner, not the
        viewport's (`capture_origin`);
      * the scroll offset — an AXTree bbox is `(viewport + root scroll) x
        scale` while a screenshot is viewport-relative. Verified live on an
        in-flow button (bbox unchanged at `@440,7440` across a 300px scroll), a
        fixed button, a sticky one and one inside a nested scroll container.
        The two spaces coincide only at scroll 0, which is why this went
        unnoticed.

    So containment happens at `(scroll + origin + px/scale) * scale`, while
    `cssPoint` stays `origin + px/scale` because `Input.click` takes VIEWPORT
    CSS pixels — also verified live, by clicking a button's viewport centre on
    a page scrolled to 3407px and having the page report that button.

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
    #
    # A viewport capture needs no translation and therefore no scale: its origin
    # is (0,0) and its pixels are already in the AXTree's space. Only a genuine
    # crop has to convert its CSS origin into device pixels, and only that case
    # depends on a proven scale.
    offset_x = float(origin.get("x") or 0.0)
    offset_y = float(origin.get("y") or 0.0)
    scroll_x = float(origin.get("scrollX") or 0.0)
    scroll_y = float(origin.get("scrollY") or 0.0)
    if not origin.get("proven"):
        return _coordinate_fallback(
            px, py,
            reason="crop_origin_unprovable",
            dpr_receipt=dpr_receipt,
            scope=scope,
            origin_receipt=origin,
        )
    # The scale is only needed to convert the CSS offsets into device pixels.
    # A viewport capture of an unscrolled page has none, so it still promotes
    # without a proven scale, exactly as it did before crops were supported.
    needs_scale = bool(offset_x or offset_y or scroll_x or scroll_y)
    if needs_scale and not (dpr_receipt or {}).get("proven"):
        return _coordinate_fallback(
            px, py,
            reason="crop_scale_unprovable",
            dpr_receipt=dpr_receipt,
            scope=scope,
            origin_receipt=origin,
        )
    if not origin.get("scrollProven"):
        # Without the scroll the point cannot be placed in the AXTree's
        # document space. The coordinate does not need it, so hand back the
        # exact viewport point rather than refusing outright.
        return _coordinate_fallback(
            px, py,
            reason="scroll_unprovable",
            dpr_receipt=dpr_receipt,
            scope=scope,
            origin_receipt=origin,
        )
    scale = float((dpr_receipt or {}).get("dpr") or 1.0) or 1.0
    hit_x = (scroll_x + offset_x) * scale + px
    hit_y = (scroll_y + offset_y) * scale + py
    bboxes = parse_axtree_bboxes(axtree_lines)
    moved = _capture_target_moved(bboxes, origin, scale)
    if moved is not None:
        # The element the crop was taken of is no longer where the receipt put
        # it, so the origin describes a layout that has since changed — and
        # both products of that origin are wrong, the durable id and the
        # coordinate alike. This covers the window holding the visual-locate
        # call, where a popup animating into place or a list reflowing does its
        # damage — but ONLY for an element capture whose target has a canonical
        # id AND appears in the AXTree. A popup the accessibility tree cannot
        # see gets no guard at all, and that is one of the main reasons visual
        # recovery exists, so this is a narrow mitigation, not a solved race.
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
    # Promotion only trusts main-frame boxes: iframe bboxes are frame-local, and
    # iframe elements have no screen-space rect in the AXTree on this build, so a
    # genuine iframe target correctly falls through to the cssPoint fallback
    # (coordinate clicks are screen-space and hit iframe content just fine).
    # A crop is smaller than the viewport, so its dimensions must not be used
    # to pick the main frame — the root bbox would never look closest to them.
    frame = (
        main_frame_id(bboxes, shot_w=shot_w, shot_h=shot_h)
        if str(scope or "") in COORDINATE_SAFE_SCOPES
        else main_frame_id(bboxes)
    )
    hit = point_to_id(bboxes, hit_x, hit_y, frame=frame)
    if hit is not None:
        return {"resolved": True, "id": hit["id"], "label": hit["name"],
                "role": hit["role"], "bbox": hit,
                "pxPoint": {"x": px, "y": py},
                "documentPxPoint": {"x": hit_x, "y": hit_y},
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


# 1.1.9 answers `state-read`; older builds and fixtures used `amount-zero`.
# Anything else means the call moved the page and is not a reading of it.
_STATE_READ_REASONS = frozenset({"state-read", "amount-zero"})


def _scroll_position_from_state_read(
    data: Any,
) -> Optional[Dict[str, float]]:
    """Return a certified scroll position, or ``None``.

    Every field is required and must have the JSON type the contract promises.
    In particular, a missing distance or axis is not zero, and booleans are not
    numbers even though Python's ``bool`` subclasses ``int``.  This parser is
    shared by both VL call paths so neither can silently manufacture geometry.
    """
    if not isinstance(data, dict):
        return None
    if str(data.get("completedReason") or "") not in _STATE_READ_REASONS:
        return None
    distance = data.get("actualDistance")
    position = data.get("position")
    if (
        isinstance(distance, bool)
        or not isinstance(distance, (int, float))
        or not math.isfinite(float(distance))
        or float(distance) != 0.0
        or not isinstance(position, dict)
    ):
        return None
    axes = (position.get("x"), position.get("y"))
    if any(
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
        for value in axes
    ):
        return None
    return {"x": float(axes[0]), "y": float(axes[1])}


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

    Uses `Input.scroll` in viewport mode with `amount: 0`, which the platform
    documents and was measured to treat as a state read: it answers
    `completedReason: "state-read"`, `actualDistance: 0`, and leaves the
    position untouched. That receipt is why a scroll ACTION is acceptable as a
    read here — it states that it did not scroll, so a read that silently moved
    the page cannot be mistaken for one that did not, and this refuses anything
    that does not say so.

    `direction` and `amount` are TOP-LEVEL fields. Nesting them under a
    `viewport` object — the shape this code used to send — matches the union's
    viewport variant with no fields at all, because the schema strips unknown
    keys, and the action then runs its 300px default. That is how "amount has
    no effect" came to be believed; `validation.py` now rejects the shape.

    `Page.getState` carries no scroll field on this build, and a Semantic Tree
    read costs a whole document.
    """
    try:
        resp = await browser.call("Input.scroll", {
            "pageId": page_id,
            "direction": "down",
            "amount": 0,
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


def screenshot_dpr(
    *,
    png_width: float,
    png_height: float,
    reported_width: Any,
    reported_height: Any,
) -> Dict[str, Any]:
    """Prove the capture's device-pixel ratio from the screenshot receipt.

    `Page.screenshot` reports `data.width`/`data.height` in CSS pixels while
    the file it saves is in device pixels, so their ratio IS the scale factor —
    no JS probe, no page-state field. This matters because `Page.getState` on
    ABCP 1.1.9 carries no `deviceScaleFactor` at all (verified live against
    catalogRevision sha256:cfd8fb90…), so the old reader always fell back to
    1.0 and every coordinate fallback on a HiDPI display landed at twice the
    intended point while still reporting success.

    Returns ``{"dpr", "source", "proven"}``. When the ratio cannot be proven
    the caller must refuse a coordinate action rather than assume 1.0.
    """
    receipt: Dict[str, Any] = {"dpr": 1.0, "source": "unproven", "proven": False}
    try:
        css_w = float(reported_width or 0.0)
        css_h = float(reported_height or 0.0)
    except (TypeError, ValueError):
        return receipt
    if css_w <= 0 or css_h <= 0 or png_width <= 0 or png_height <= 0:
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
        return receipt
    return {
        "dpr": ratio_x,
        "source": "screenshot_receipt",
        "proven": True,
    }


async def _viewport_dpr(browser: Any, page_id: str) -> Dict[str, Any]:
    """Read an optional native page-state scale factor without executing JS.

    Kept as a last-resort compatibility path for builds that do expose a scale
    factor. ABCP 1.1.9 does not, so `screenshot_dpr` is the real source and
    this returns an explicitly unproven receipt rather than a bare 1.0.
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
