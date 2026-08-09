"""
harness.observation.overlay_actions - Pure decision helpers for dismiss_overlay.

No I/O and no agent coupling: these functions take parsed AXTree nodes / layer
dicts / rects and return decisions. The async ladder orchestration lives in
browser_tools._dismiss_overlay and calls these. Keeping the policy here makes
the safety-critical parts (what is a close control, what must never be clicked,
where a backdrop click may land) unit-testable in isolation.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from harness.utils import JsonDict


# Weak-dismiss controls: clicking these has no consequential side effect
# regardless of overlay type, so they are always eligible dismiss candidates.
#
# Matched two ways. SUBSTRING phrases are distinctive enough that they never sit
# inside a consequential action label. "close"-family labels are EXACT-match
# only: "close" as a substring would wrongly accept "Close account" / "Close
# position" / "Close order" — real, irreversible actions. Single-char close
# glyphs (×, x, ...) are matched exactly (CLOSE_GLYPHS) for icon-only buttons.
WEAK_DISMISS_SUBSTRINGS = (
    "not now", "maybe later", "skip", "got it",
    "no thanks", "continue without", "don't show", "remind me later",
    "稍后", "跳过", "知道了", "不再提示", "暂不", "以后再说",
)
WEAK_DISMISS_EXACT = {
    # "close"/"dismiss" are EXACT (or with an explicit UI-surface noun) only:
    # bare-substring matching would accept "Close account" / "Dismiss employee" /
    # "Dismiss case" — real, consequential business actions.
    "close", "close dialog", "close modal", "close popup", "close banner",
    "close window", "close overlay", "close notification", "close menu",
    "close this", "close this dialog",
    "dismiss", "dismiss dialog", "dismiss modal", "dismiss popup",
    "dismiss banner", "dismiss notification", "dismiss message",
    "关闭", "关闭弹窗", "关闭对话框", "关闭窗口", "关闭提示",
}

# Single-character close glyphs, matched EXACTLY (not as substrings, which would
# false-positive on "next"/"box") so an icon-only close button whose live text or
# VL label is just "X"/"×" still qualifies as a dismiss control.
CLOSE_GLYPHS = {"x", "×", "✕", "✖", "╳", "✗", "⨯"}

# Consent controls: "Agree"/"Accept all"/"OK"/"同意" dismiss a cookie/consent
# banner safely, but in a generic modal the same words can confirm terms, age,
# or a permission grant — a real side effect. Only eligible when the overlay was
# classified cookie/consent (find_close_control / vl_dismiss_target_is_safe
# subtype). Bare words ("agree"/"ok"/"accept") are EXACT-match only to avoid
# substring hits ("disagree", "token", "lookbook"); multi-word phrases are safe
# as substrings ("Accept all cookies" contains "accept all").
CONSENT_DISMISS_SUBSTRINGS = (
    "accept all", "accept cookies", "reject all", "reject cookies",
    "接受所有", "全部接受", "拒绝所有",
)
CONSENT_DISMISS_EXACT = {
    "agree", "i agree", "ok", "okay", "accept", "同意", "接受",
}

# Subtypes under which consent-class controls may be auto-clicked.
CONSENT_OVERLAY_SUBTYPES = {"cookie_banner", "consent"}

# Back-compat unions (subtype-agnostic; prefer the is_*_dismiss_label helpers).
WEAK_DISMISS_KEYWORDS = WEAK_DISMISS_SUBSTRINGS + tuple(sorted(WEAK_DISMISS_EXACT))
CONSENT_DISMISS_KEYWORDS = CONSENT_DISMISS_SUBSTRINGS + tuple(sorted(CONSENT_DISMISS_EXACT))
DISMISS_KEYWORDS = WEAK_DISMISS_KEYWORDS + CONSENT_DISMISS_KEYWORDS

# Hard block: never auto-click a control whose name matches these, even if it
# also looks like a dismiss control ("Sign in" buttons sometimes sit in the
# same dialog as a close X). This guards the dismiss step itself.
NEVER_CLICK_KEYWORDS = (
    "sign in", "log in", "login", "signin", "authenticate", "continue with",
    "sign up", "register", "subscribe", "upgrade", "pay", "purchase", "buy",
    "checkout", "connect with google", "connect with apple",
    "continue with google", "continue with apple", "continue with facebook",
    "登录", "登陆", "注册", "付费", "订阅", "购买", "支付", "升级",
    "微信登录", "支付宝", "立即购买", "去支付",
)

# Block: after an overlay is dismissed, only auto-retry the ORIGINAL action when
# its target is NOT one of these irreversible / consequential intents. Anything
# matching here returns dismissed_pending_action for the model to decide.
SENSITIVE_ACTION_KEYWORDS = (
    "submit", "checkout", "place order", "confirm order", "pay", "purchase",
    "buy", "delete", "remove", "authorize", "authorise", "confirm", "send",
    "publish", "post", "transfer", "withdraw", "sign in", "log in", "login",
    "提交", "下单", "确认订单", "支付", "付款", "购买", "删除", "授权",
    "确认", "发送", "发布", "转账", "提现", "登录",
)

DISMISS_CONTAINER_ROLES = {"dialog", "alertdialog"}
ACTIONABLE_ROLES = {"button", "link", "menuitem", "tab", "checkbox", "switch"}


def _name_matches(name: str, keywords: Any) -> Optional[str]:
    lowered = str(name or "").strip().lower()
    if not lowered:
        return None
    for keyword in keywords:
        kw = str(keyword or "").lower()
        if kw and kw in lowered:
            return kw
    return None


def is_never_click(name: str) -> bool:
    return _name_matches(name, NEVER_CLICK_KEYWORDS) is not None


def is_sensitive_target(role: str, name: str) -> bool:
    """True when auto-retrying a click/type on this target after dismissal is
    too consequential to do without the model. role is advisory; the name
    intent dominates."""
    return _name_matches(name, SENSITIVE_ACTION_KEYWORDS) is not None


def is_sensitive_method(method: str) -> bool:
    """Only Input.click is eligible for auto-retry after a dismiss.

    Typing/key presses can autosubmit or fire async handlers. Input.scroll is
    also excluded: the retry path replays only {pageId, id}, which does not
    carry scroll geometry (direction/amount), so a reconstructed scroll would
    be malformed — and an occlusion-blocked scroll is not the click-shaped
    case this retry exists for. Returns True (= do not auto-retry) for both."""
    return str(method or "") != "Input.click"


def is_weak_dismiss_label(name: str) -> bool:
    """Always-safe dismiss control by label: a close glyph, an exact close-family
    label, or a distinctive soft-dismiss substring. EXACT for "close" so
    "Close account"/"Close position"/"Close order" do NOT qualify."""
    low = str(name or "").strip().lower()
    if not low:
        return False
    if low in CLOSE_GLYPHS or low in WEAK_DISMISS_EXACT:
        return True
    return any(kw in low for kw in WEAK_DISMISS_SUBSTRINGS)


def is_consent_dismiss_label(name: str) -> bool:
    """Consent-class dismiss by label (Agree/Accept all/OK/...). Only eligible
    under a cookie/consent overlay subtype — callers gate on that."""
    low = str(name or "").strip().lower()
    if not low:
        return False
    if low in CONSENT_DISMISS_EXACT:
        return True
    return any(kw in low for kw in CONSENT_DISMISS_SUBSTRINGS)


def is_safe_dismiss_label(name: str, *, subtype: Optional[str] = None) -> bool:
    """Single source of truth for "is this label a safe dismiss control to
    click". True only when the label POSITIVELY reads as a dismiss control AND
    is not a never-click / sensitive action. Consent controls qualify only when
    `subtype` marks a cookie/consent overlay. Used by BOTH the deterministic
    find_close_control and the VL arbiter's vl_dismiss_target_is_safe so neither
    path can click "Close account" or bypass consent gating."""
    if not name:
        return False
    if is_never_click(name) or is_sensitive_target("", name):
        return False
    if is_weak_dismiss_label(name):
        return True
    if subtype in CONSENT_OVERLAY_SUBTYPES and is_consent_dismiss_label(name):
        return True
    return False


def find_close_control(
    nodes: List[JsonDict],
    *,
    subtype: Optional[str] = None,
) -> Optional[JsonDict]:
    """Pick the safest dismiss control from current AXTree nodes.

    Preference: explicit close/X first, then soft-dismiss (skip/not now), and
    controls inside a dialog/alertdialog scope rank above page-level ones.
    Controls whose name hits NEVER_CLICK_KEYWORDS are excluded outright.

    Consent-class controls ("Agree"/"Accept all"/"OK") are only eligible when
    `subtype` marks a cookie/consent banner; for a generic modal those words
    may confirm terms/age/permissions, so only weak-dismiss controls qualify.
    "close" is matched exactly via is_safe_dismiss_label, so "Close account" /
    "Close position" / "Close order" are never selected."""
    candidates: List[Tuple[Tuple[int, int], JsonDict]] = []
    dialog_depth: Optional[int] = None

    for node in nodes or []:
        role = str(node.get("role") or "").lower()
        depth = int(node.get("depth") or 0)
        if role in DISMISS_CONTAINER_ROLES:
            dialog_depth = depth
            continue
        if dialog_depth is not None and depth <= dialog_depth:
            dialog_depth = None
        if not node.get("interactive"):
            continue
        if role and role not in ACTIONABLE_ROLES:
            continue
        name = str(node.get("name") or "")
        if not is_safe_dismiss_label(name, subtype=subtype):
            continue
        in_dialog = dialog_depth is not None
        keyword_rank = _dismiss_keyword_rank(name)
        # Lower tuple sorts first: prefer in-dialog (0) then keyword strength.
        score = (0 if in_dialog else 1, keyword_rank)
        candidates.append((score, node))

    if not candidates:
        return None
    candidates.sort(key=lambda item: item[0])
    return candidates[0][1]


def _dismiss_keyword_rank(name: str) -> int:
    lowered = str(name or "").strip().lower()
    if lowered in {"close", "x", "×", "关闭"}:
        return 0
    if any(k in lowered for k in ("close", "dismiss", "×", "关闭")):
        return 1
    if any(k in lowered for k in ("skip", "not now", "no thanks", "跳过", "稍后", "暂不")):
        return 2
    return 3


def visible_layers_occluded(layers: Any) -> List[JsonDict]:
    """Frame-level occlusion signal: layers whose occlusionState is occluded."""
    occluded: List[JsonDict] = []
    for layer in layers or []:
        if isinstance(layer, dict) and layer.get("occlusionState") == "occluded":
            occluded.append(layer)
    return occluded


def compute_backdrop_point(
    dialog_rect: Optional[JsonDict],
    viewport: JsonDict,
) -> Optional[Tuple[float, float]]:
    """A point inside the viewport but outside the dialog rect, biased to the
    widest margin band. Returns None when the dialog (effectively) fills the
    viewport, since there is then no safe backdrop to click."""
    vw = float(viewport.get("width") or 0)
    vh = float(viewport.get("height") or 0)
    if vw <= 0 or vh <= 0:
        return None
    if not dialog_rect:
        # No known dialog rect: a top-edge point is the least-risky guess.
        return (round(vw * 0.5, 1), round(min(8.0, vh * 0.02), 1))

    dx = float(dialog_rect.get("x") or 0)
    dy = float(dialog_rect.get("y") or 0)
    dw = float(dialog_rect.get("w") or dialog_rect.get("width") or 0)
    dh = float(dialog_rect.get("h") or dialog_rect.get("height") or 0)

    # Margin band sizes around the dialog.
    margins = {
        "top": dy,
        "bottom": vh - (dy + dh),
        "left": dx,
        "right": vw - (dx + dw),
    }
    side, size = max(margins.items(), key=lambda kv: kv[1])
    if size < 12:  # dialog basically fills the viewport: no safe backdrop
        return None
    if side == "top":
        return (round(vw * 0.5, 1), round(dy * 0.5, 1))
    if side == "bottom":
        return (round(vw * 0.5, 1), round(dy + dh + (vh - (dy + dh)) * 0.5, 1))
    if side == "left":
        return (round(dx * 0.5, 1), round(vh * 0.5, 1))
    return (round(dx + dw + (vw - (dx + dw)) * 0.5, 1), round(vh * 0.5, 1))


VL_NORMALIZED_SCALE = 1000.0


def normalized_point_to_css(
    point: Any,
    css_viewport: Any,
    scale: float = VL_NORMALIZED_SCALE,
) -> Optional[Tuple[float, float]]:
    """Back-translate a VL dismiss_point in normalized grounding space (0..scale,
    top-left origin — the convention qwen-VL and most grounding VLs use) into CSS
    coordinates. A normalized fraction of the image is the same fraction of the
    CSS viewport regardless of devicePixelRatio or screenshot pixel size, so this
    needs only the CSS viewport — no image dimensions, no DPR. Returns None when
    the viewport is unusable or the point is out of range."""
    def _num(value: Any) -> float:
        try:
            return float(value)
        except (TypeError, ValueError):
            return 0.0

    if isinstance(point, dict):
        nx, ny = _num(point.get("x")), _num(point.get("y"))
    elif isinstance(point, (list, tuple)) and len(point) >= 2:
        nx, ny = _num(point[0]), _num(point[1])
    else:
        return None

    vp = css_viewport if isinstance(css_viewport, dict) else {}
    cw = _num(vp.get("width"))
    ch = _num(vp.get("height"))
    if cw <= 0 or ch <= 0 or scale <= 0:
        return None
    # Reject a point clearly outside the normalized range (small overshoot ok).
    if nx < -1 or ny < -1 or nx > scale + 1 or ny > scale + 1:
        return None
    css_x = max(0.0, min((nx / scale) * cw, cw - 1))
    css_y = max(0.0, min((ny / scale) * ch, ch - 1))
    return (round(css_x, 1), round(css_y, 1))


def vl_dismiss_target_is_safe(
    top_element: Any,
    control_label: str = "",
    *,
    subtype: Optional[str] = None,
) -> bool:
    """Positive-whitelist safety gate for a VL-proposed dismiss click.

    The INDEPENDENT second check on top of the VL's judgement, so it must not
    merely trust "the VL said dismiss". The LIVE elementFromPoint element is
    ground truth for what will actually be clicked:
      - Hard block if ANY live identity field (text/aria-label/title/value/alt)
        OR the VL label looks consequential (never-click / sensitive) — a wrong
        or adversarial VL label must never override a dangerous live element.
      - The live ACCESSIBLE NAME (ARIA precedence: aria-label > text >
        title/value/alt) must itself positively read as a dismiss control
        (exact "close", glyph, soft-dismiss; consent only under a cookie/consent
        subtype). aria-label dominates innerText, so a button rendering only "×"
        but labelled "Close position" is rejected; and a VL label cannot promote
        a non-dismiss live element ("Close account") into a safe click.
      - Only when the live element exposes NO accessible name at all (rare) do we
        fall back to the VL label, and only on an actionable element.
    Everything else fails closed = no click."""
    if not isinstance(top_element, dict):
        return False
    label = str(control_label or "").strip()
    text = str(top_element.get("text") or "").strip()
    aria = str(top_element.get("ariaLabel") or top_element.get("name") or "").strip()
    title = str(top_element.get("title") or "").strip()
    value = str(top_element.get("value") or "").strip()
    alt = str(top_element.get("alt") or "").strip()
    live_fields = [s for s in (text, aria, title, value, alt) if s]

    # Hard block if ANY live identity field or the VL label is consequential.
    for candidate in live_fields + ([label] if label else []):
        if is_never_click(candidate) or is_sensitive_target("", candidate):
            return False

    # Accessible name is the ground truth for what this control actually is.
    accessible_name = aria or text or title or value or alt
    if accessible_name:
        return is_safe_dismiss_label(accessible_name, subtype=subtype)
    # No accessible name at all: fall back to the VL label, only on an actionable
    # element (button/link/role), never a blank/unidentifiable surface.
    tag = str(top_element.get("tag") or "").lower()
    role = str(top_element.get("role") or "").lower()
    if label and (tag in {"button", "a"} or role in ACTIONABLE_ROLES):
        return is_safe_dismiss_label(label, subtype=subtype)
    return False


def _element_identity_fields(element: Any) -> List[str]:
    source = element if isinstance(element, dict) else {}
    return [
        text for text in (
            str(source.get(key) or "").strip()
            for key in ("text", "ariaLabel", "name", "title", "value", "alt")
        ) if text
    ]


def _element_is_actionable(element: Any) -> bool:
    source = element if isinstance(element, dict) else {}
    tag = str(source.get("tag") or "").lower()
    role = str(source.get("role") or "").lower()
    return tag in {"button", "a", "input", "select", "textarea"} or role in ACTIONABLE_ROLES


def captcha_point_is_safe(occluder_stack: Any) -> Tuple[bool, JsonDict]:
    """Negative safety gate for a VL-proposed CAPTCHA solve point.

    A puzzle piece, slider handle, or grid tile has no stable positive identity
    to whitelist against (that is exactly why the VL is being asked), so unlike
    `vl_dismiss_target_is_safe` this gate is a BLOCK-list over the LIVE elements
    at the point.

    It inspects the WHOLE elementsFromPoint stack, not just the topmost node:
    the hit-test stack is the topmost element plus its ancestor chain, and a
    transparent/empty `<span>` sitting on a "Sign in" `<button>` would otherwise
    pass while the click still lands on the button.

    Two rules, both fail-closed:
      * the topmost element must not read consequential in ANY identity field —
        it is what actually receives the event;
      * no ACTIONABLE element anywhere in the stack (button/link/input/role) may
        read consequential.
    Non-actionable ancestors are deliberately not blocking: a slider inside a
    "手机号登录" dialog is a legitimate, live-verified case, and the dialog's own
    wording must not veto solving the puzzle it contains.

    Returns (safe, evidence) so callers can log WHY a solve was abandoned."""
    stack = occluder_stack if isinstance(occluder_stack, list) else []
    elements = [item for item in stack if isinstance(item, dict)]
    if not elements:
        return False, {"reason": "no_element_at_point"}
    for depth, element in enumerate(elements):
        actionable = _element_is_actionable(element)
        if depth and not actionable:
            continue  # non-actionable ancestor: its wording is context, not a target
        for candidate in _element_identity_fields(element):
            if is_never_click(candidate) or is_sensitive_target("", candidate):
                return False, {
                    "reason": "consequential_control_at_point",
                    "label": candidate[:60],
                    "tag": str(element.get("tag") or ""),
                    "role": str(element.get("role") or ""),
                    "stackDepth": depth,
                }
    top = elements[0]
    return True, {
        "tag": str(top.get("tag") or ""),
        "role": str(top.get("role") or ""),
        "label": next(iter(_element_identity_fields(top)), "")[:60],
        "stackChecked": len(elements),
    }


# Positive allow-list for the field an OCR answer may be typed into. A block-list
# would silently accept every future/exotic input type (checkbox, radio, range,
# date, color, button ...), so only these are considered a plain text box.
ALLOWED_TEXT_INPUT_TYPES = {"", "text", "search"}


def captcha_text_target_is_safe(active_element: Any) -> Tuple[bool, JsonDict]:
    """Gate the element that will actually receive an OCR answer.

    The focus point was already coordinate-checked; this checks what has focus
    NOW, and only ALLOW-LISTED shapes pass: `<input>` with no type / text /
    search, `<textarea>`, an explicitly contenteditable node, or role=textbox —
    plus a label that does not read credential-shaped. Everything else
    (password/email/tel/file/checkbox/date/…, an unreadable probe, nothing
    focused) fails closed."""
    element = active_element if isinstance(active_element, dict) else {}
    if element.get("status") == "oracle_unavailable" or not element.get("present"):
        return False, {"reason": "no_focused_element"}
    tag = str(element.get("tag") or "").lower()
    field_type = str(element.get("type") or "").lower()
    role = str(element.get("role") or "").lower()
    editable = bool(element.get("editable"))
    if tag == "input":
        if field_type not in ALLOWED_TEXT_INPUT_TYPES:
            return False, {"reason": "input_type_not_a_plain_text_box", "type": field_type}
    elif not (tag == "textarea" or editable or role == "textbox"):
        return False, {"reason": "focus_is_not_a_text_field", "tag": tag, "role": role}
    for candidate in (
        str(element.get("ariaLabel") or ""),
        str(element.get("placeholder") or ""),
        str(element.get("name") or ""),
        str(element.get("id") or ""),
    ):
        if candidate and (is_never_click(candidate) or is_sensitive_target("", candidate)):
            return False, {
                "reason": "consequential_field_label",
                "label": candidate[:60],
            }
    return True, {"tag": tag, "type": field_type or None, "role": role or None}


def backdrop_point_is_safe(occluder_stack: List[JsonDict]) -> bool:
    """A backdrop click is safe only when the topmost element at the point is a
    non-interactive surface (the overlay/backdrop itself), never a button,
    link, or input that could trigger a real action."""
    if not occluder_stack:
        return False
    top = occluder_stack[0]
    if not isinstance(top, dict):
        return False
    tag = str(top.get("tag") or "").lower()
    role = str(top.get("role") or "").lower()
    if tag in {"a", "button", "input", "select", "textarea", "label", "option"}:
        return False
    if role in {"button", "link", "textbox", "checkbox", "radio", "menuitem", "tab"}:
        return False
    name = str(top.get("text") or "")
    if is_never_click(name) or is_sensitive_target(role, name):
        return False
    return True
