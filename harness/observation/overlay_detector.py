"""
harness.observation.overlay_detector - Soft detection for business overlays and modals.

This module intentionally stays separate from challenge_detector. Login prompts,
cookie banners, and paywalls are often ordinary page UI and may be dismissible;
treating them as CAPTCHA/security challenges would push too many pages straight
to HITL.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, List, Optional

from harness.utils import JsonDict


AUTH_PROMPT_KEYWORDS = (
    "sign in to continue",
    "sign in with google",
    "already have an account",
    "log in to continue",
    "login required",
    "please log in",
    "please sign in",
    "create account",
    "sign up for free",
    "请登录",
    "登录后继续",
    "创建账号",
    "注册账号",
)

PAYWALL_KEYWORDS = (
    "subscribe to continue",
    "upgrade to continue",
    "premium required",
    "payment required",
    "付费后继续",
    "订阅后继续",
)

COOKIE_KEYWORDS = (
    "accept cookies",
    "cookie settings",
    "we use cookies",
    "manage cookies",
    "接受 cookie",
    "使用 cookie",
)

DISMISS_KEYWORDS = (
    "close",
    "dismiss",
    "not now",
    "maybe later",
    "skip",
    "got it",
    "continue without",
    "accept cookies",
    "accept all",
    "reject all",
    "agree",
    "关闭",
    "稍后",
    "跳过",
    "知道了",
)

STRONG_AUTH_PROMPT_KEYWORDS = (
    "sign in to continue",
    "sign in with google",
    "already have an account",
    "log in to continue",
    "登录后继续",
)

STRONG_PAYWALL_KEYWORDS = (
    "subscribe to continue",
    "upgrade to continue",
    "payment required",
    "付费后继续",
    "订阅后继续",
)

# Exact document titles are stronger than an incidental "sign in" phrase in
# article text.  Keep this generic authentication vocabulary in the dedicated
# overlay/auth detector; downstream classifiers consume only its structured
# result and do not duplicate these words.
AUTH_PAGE_TITLES = frozenset({
    "login",
    "log in",
    "sign in",
    "signin",
    "authentication required",
    "登录",
    "用户登录",
    "账号登录",
    "请登录",
})

DIALOG_ROLE_MARKERS = (
    "] dialog",
    "] alertdialog",
)


def detect_overlay_from_result(result: Any) -> Optional[JsonDict]:
    text = _collect_page_text(result)
    detected = detect_overlay_from_text(text)
    if detected is not None:
        return detected
    title = _page_title(result)
    if title_looks_like_auth_page(title):
        return {
            "type": "business_overlay",
            "subtype": "auth_prompt",
            "confidence": 0.85,
            "dismissibleSignal": False,
            "evidence": [f"document title: {title}"],
            "hint": _overlay_hint("auth_prompt"),
        }
    return None


def title_looks_like_auth_page(title: Any) -> bool:
    return str(title or "").strip().casefold() in AUTH_PAGE_TITLES


def detect_overlay_from_text(text: str) -> Optional[JsonDict]:
    lower = str(text or "").lower()
    if not lower.strip():
        return None

    evidence: List[str] = []
    subtype = ""
    confidence = 0.0

    auth_hits = _keyword_evidence(lower, AUTH_PROMPT_KEYWORDS)
    paywall_hits = _keyword_evidence(lower, PAYWALL_KEYWORDS)
    cookie_hits = _keyword_evidence(lower, COOKIE_KEYWORDS)
    has_dialog_role = any(marker in lower for marker in DIALOG_ROLE_MARKERS)
    dismiss_hits = _keyword_evidence(lower, DISMISS_KEYWORDS)

    auth_is_overlay = bool(
        auth_hits
        and (
            has_dialog_role
            or _has_strong_overlay_evidence(auth_hits, STRONG_AUTH_PROMPT_KEYWORDS)
        )
    )
    paywall_is_overlay = bool(
        paywall_hits
        and (
            has_dialog_role
            or _has_strong_overlay_evidence(paywall_hits, STRONG_PAYWALL_KEYWORDS)
        )
    )

    if auth_is_overlay:
        subtype = "auth_prompt"
        confidence = 0.9
        evidence.extend(auth_hits)
    elif paywall_is_overlay:
        subtype = "paywall"
        confidence = 0.9
        evidence.extend(paywall_hits)
    elif cookie_hits:
        subtype = "cookie_banner"
        confidence = 0.75
        evidence.extend(cookie_hits)
    elif has_dialog_role:
        subtype = "modal_dialog"
        confidence = 0.55
        evidence.append("dialog role")
    else:
        return None

    if dismiss_hits:
        evidence.extend(dismiss_hits[:2])

    return {
        "type": "business_overlay",
        "subtype": subtype,
        "confidence": confidence,
        "dismissibleSignal": bool(dismiss_hits),
        "evidence": evidence[:6],
        "hint": _overlay_hint(subtype),
    }


def _overlay_hint(subtype: str) -> str:
    if subtype == "auth_prompt":
        return (
            "Business auth prompt detected. Do not click login/provider buttons"
            " automatically; first try a non-submit dismiss path such as close,"
            " or Escape, then refresh DOM.getAXTree."
        )
    if subtype == "paywall":
        return (
            "Paywall or subscription overlay detected. Do not bypass payment;"
            " try only explicit dismiss controls, otherwise report a blocker."
        )
    if subtype == "cookie_banner":
        return (
            "Cookie banner detected. Prefer explicit accept/reject/manage controls"
            " and verify the banner disappeared before retrying the target action."
        )
    return (
        "Modal or overlay detected. Use DOM.getAXTree to find a close/dismiss"
        " control or try Escape before retrying. Coordinate clicks require an"
        " independent native point hit-test."
    )


def _keyword_evidence(text: str, keywords: Any) -> List[str]:
    evidence: List[str] = []
    for keyword in keywords:
        value = str(keyword or "").lower()
        if value and value in text:
            evidence.append(value)
    return evidence


def _has_strong_overlay_evidence(
    primary_hits: List[str],
    strong_keywords: Any,
) -> bool:
    strong = {str(keyword or "").lower() for keyword in strong_keywords}
    return len(primary_hits) >= 2 or any(hit in strong for hit in primary_hits)


def _collect_page_text(value: Any, *, limit: int = 120_000) -> str:
    chunks: List[str] = []

    def add(text: str) -> None:
        if not text:
            return
        current = sum(len(chunk) for chunk in chunks)
        if current >= limit:
            return
        chunks.append(text[: max(0, limit - current)])

    def visit(item: Any) -> None:
        if sum(len(chunk) for chunk in chunks) >= limit:
            return
        if isinstance(item, dict):
            path = item.get("savedPath")
            if isinstance(path, str) and item.get("format") == "text_lines":
                try:
                    add(Path(path).read_text(encoding="utf-8", errors="replace"))
                except OSError:
                    pass
            for nested in item.values():
                visit(nested)
        elif isinstance(item, list):
            for nested in item:
                visit(nested)
        elif isinstance(item, str):
            add(item)

    visit(value)
    return "\n".join(chunks)


def _page_title(value: Any, *, depth: int = 0) -> str:
    if depth > 5 or not isinstance(value, dict):
        return ""
    title = value.get("title")
    if isinstance(title, str) and title.strip():
        return title.strip()
    for key in ("response", "data"):
        nested = value.get(key)
        if isinstance(nested, dict):
            found = _page_title(nested, depth=depth + 1)
            if found:
                return found
    return ""
