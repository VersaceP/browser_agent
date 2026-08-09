"""harness.vl.locate — VL Role A: AXTree-blindspot locate + bbox→id promotion.

When the AXTree can't resolve a target (canvas, text-in-image, purely visual
control), VL points at it visually; the harness then PROMOTES that pixel back to a
durable canonical id by reverse-looking-up the AXTree bbox that contains it, so
subsequent actions use a stable handle (id / role+name) instead of raw coordinates.

LIVE-VERIFIED foundation (2026-06-27 probes):
  - `DOM.getAXTree` lines carry `# @x,y,w,h` (viewport px bbox) on positioned/
    interactive elements, e.g. `[3:13:13] link "Learn more" # @512,398,164,39`.
  - The AXTree bbox space == the `Page.screenshot` pixel space (both 2560×1600 on
    the probe), and VL grounds on the screenshot. So:
        px = norm/1000 * screenshotWidth   (same space as the bbox)
    and a containment test `x ≤ px ≤ x+w ∧ y ≤ py ≤ y+h` gives the id directly.

Promote-then-heal discipline (doc §13.2): coordinates NEVER persist into a skill
(they rot faster than CSS selectors). A located pixel is converted to the durable
id immediately; only when NO bbox contains it (genuine AXTree blind spot) does the
caller fall back to a one-shot coordinate action.
"""
from __future__ import annotations

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


def promote_locate(
    axtree_lines: List[Any],
    point_norm: Dict[str, Any],
    *,
    shot_w: float,
    shot_h: float,
    dpr: float = 1.0,
) -> Dict[str, Any]:
    """Map a normalized 0-1000 VL point to screenshot px, then promote to a durable
    AXTree id via bbox containment. Returns:
      {resolved:True, id, label, role, bbox, pxPoint}                — durable handle
      {resolved:False, cssPoint, pxPoint, reason:"no_bbox_contains"} — coords fallback
    `cssPoint` divides by dpr because Input.* takes CSS px while the bbox/screenshot
    are device px."""
    px = float(point_norm.get("x", 0.0)) / 1000.0 * float(shot_w or 0.0)
    py = float(point_norm.get("y", 0.0)) / 1000.0 * float(shot_h or 0.0)
    bboxes = parse_axtree_bboxes(axtree_lines)
    # Promotion only trusts main-frame boxes: iframe bboxes are frame-local, and
    # iframe elements have no screen-space rect in the AXTree on this build, so a
    # genuine iframe target correctly falls through to the cssPoint fallback
    # (coordinate clicks are screen-space and hit iframe content just fine).
    frame = main_frame_id(bboxes, shot_w=shot_w, shot_h=shot_h)
    hit = point_to_id(bboxes, px, py, frame=frame)
    if hit is not None:
        return {"resolved": True, "id": hit["id"], "label": hit["name"],
                "role": hit["role"], "bbox": hit, "pxPoint": {"x": px, "y": py}}
    d = float(dpr or 1.0) or 1.0
    return {"resolved": False, "reason": "no_bbox_contains",
            "pxPoint": {"x": px, "y": py},
            "cssPoint": {"x": px / d, "y": py / d}}


async def locate_target(
    browser: Any,
    page_id: str,
    target: str,
    *,
    vl_config: Any,
    screenshot_fn: Callable[..., Awaitable[Optional[str]]],
    axtree_fn: Optional[Callable[..., Awaitable[List[Any]]]] = None,
    visual_locate_fn: Optional[Callable[..., Awaitable[Dict[str, Any]]]] = None,
    logger: Any = None,
) -> Dict[str, Any]:
    """Full Role-A flow: screenshot → VL visual_locate → promote pixel to a durable
    id (or coords fallback). Returns:
      {ok:True, id, label, ...}                 — use Input.click({id})  (durable)
      {ok:True, coordinate:True, cssPoint, ...} — AXTree blind spot; one-shot coords
      {ok:False, reason}                        — VL disabled / not found / error
    `is_consequential` from VL is surfaced so the caller can block sensitive targets.
    """
    if vl_config is None or not getattr(vl_config, "enabled", False):
        return {"ok": False, "reason": "vl_disabled"}
    shot = await screenshot_fn(browser, page_id)
    if not shot:
        return {"ok": False, "reason": "no_screenshot"}
    vl = await (visual_locate_fn or _default_visual_locate)(vl_config, shot, target)
    if vl.get("verdict") != "located" or not vl.get("point"):
        return {"ok": False, "reason": f"vl_{vl.get('verdict', 'failed')}",
                "consequential": bool(vl.get("is_consequential"))}

    dims = await _screenshot_dims(shot)
    metrics = await _viewport_dpr(browser, page_id)
    dpr = metrics.get("dpr", 1.0)
    promo = promote_locate(
        await _resolve_axtree(browser, page_id, axtree_fn),
        vl["point"], shot_w=dims[0], shot_h=dims[1], dpr=dpr,
    )
    promo = apply_promotion_guard(
        promo, vl_label=vl.get("control_label"), expected_text=target,
        dpr=dpr, logger=logger, page_id=page_id,
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
    out = {**common, "coordinate": True, "cssPoint": promo["cssPoint"],
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
    logger: Any = None,
    page_id: str = "",
) -> Dict[str, Any]:
    """Run `promotion_guard` over a resolved promotion; on a hit, demote it to
    the same shape as the no-containment fallback (resolved:False + cssPoint)
    with `promotionGuard`/`demoted*` attached for logging and tuning."""
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
    d = float(dpr or 1.0) or 1.0
    px_point = dict(promo.get("pxPoint") or {})
    demoted = {
        "resolved": False,
        "reason": "promotion_guard",
        "promotionGuard": guard,
        "pxPoint": px_point,
        "cssPoint": {
            "x": float(px_point.get("x", 0.0)) / d,
            "y": float(px_point.get("y", 0.0)) / d,
        },
        "demotedId": promo.get("id"),
        "demotedRole": promo.get("role"),
        "demotedLabel": promo.get("label"),
    }
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


async def _viewport_dpr(browser: Any, page_id: str) -> Dict[str, Any]:
    """Read an optional native page-state scale factor without executing JS."""
    try:
        resp = await browser.call("Page.getState", {
            "pageId": page_id,
            "purpose": "read native page metrics for VL coordinate mapping",
        })
        data = ((resp or {}).get("data") or {})
        dpr = float(
            data.get("deviceScaleFactor")
            or data.get("devicePixelRatio")
            or data.get("dpr")
            or 1.0
        ) or 1.0
        return {"dpr": dpr}
    except Exception:
        return {"dpr": 1.0}


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
