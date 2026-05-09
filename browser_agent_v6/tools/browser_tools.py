"""Single-step browser tools — for explore / interact / single-shot scenarios.

Design: each tool is a thin wrapper over a helpers function, doing only argument
extraction + exception translation. The LLM uses these in simple / uncertain
scenarios; batch scrape uses run_browser_python (or batch_browser_actions).

Inventory:
- navigate          — single-step navigation
- click             — single-step click
- fill              — fill a form field
- extract_text      — extract text from page or element (JS DOM)
- screenshot        — screenshot, returns path only
- scroll            — scroll
- wait_user         — block until human intervention (login / captcha / Cloudflare)
- accept_dialog / dismiss_dialog
"""
from typing import Any, Dict

from .base import ToolSpec, ToolContext


# ──────────────────────────────────
# navigate
# ──────────────────────────────────

def _navigate_handler(tool_input: dict) -> dict:
    from browser.helpers import goto
    url = tool_input["url"]
    wait = float(tool_input.get("wait", 2))
    timeout = float(tool_input.get("timeout", 30))
    return goto(url, wait=wait, timeout=timeout)


NAVIGATE = ToolSpec(
    name="navigate",
    description=(
        "Navigate the active browser tab to a URL (http/https only). "
        "Waits for DOM ready + optional extra `wait` seconds. "
        "Returns {url, title, status}. Use this for single-page entry; "
        "for batch crawling 5+ pages, use run_browser_python instead."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "Target http(s) URL"},
            "wait": {"type": "number", "default": 2, "description": "Extra seconds to wait after DOM ready (for SPA rendering)"},
            "timeout": {"type": "number", "default": 30, "description": "Hard timeout in seconds"},
        },
        "required": ["url"],
    },
    handler=_navigate_handler,
)


# ──────────────────────────────────
# click
# ──────────────────────────────────

def _click_handler(tool_input: dict) -> dict:
    from browser.helpers import click
    selector = tool_input["selector"]
    timeout = float(tool_input.get("timeout", 5))
    return click(selector, timeout=timeout)


CLICK = ToolSpec(
    name="click",
    description=(
        "Click an element by CSS selector (or 'xpath:...' for XPath). "
        "Waits for visibility before clicking. Auto-detects intercepted/covered elements. "
        "If a native dialog is pending, raises PendingDialogError — call accept_dialog/dismiss_dialog first."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "selector": {"type": "string", "description": "CSS selector (default) or 'xpath:...'"},
            "timeout": {"type": "number", "default": 5},
        },
        "required": ["selector"],
    },
    handler=_click_handler,
)


# ──────────────────────────────────
# fill
# ──────────────────────────────────

def _fill_handler(tool_input: dict) -> dict:
    from browser.helpers import fill
    selector = tool_input["selector"]
    text = tool_input["text"]
    clear = bool(tool_input.get("clear", True))
    return fill(selector, text, clear=clear)


FILL = ToolSpec(
    name="fill",
    description=(
        "Fill a form input (input/textarea/contenteditable) by selector. "
        "Default: clears existing content first. Returns {filled, length}."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "selector": {"type": "string"},
            "text": {"type": "string"},
            "clear": {"type": "boolean", "default": True},
        },
        "required": ["selector", "text"],
    },
    handler=_fill_handler,
)


# ──────────────────────────────────
# extract_text — JS DOM extraction, avoids selenium-style serialization overhead
# ──────────────────────────────────

def _extract_text_handler(tool_input: dict) -> Any:
    from browser.helpers import js
    selector = tool_input.get("selector")
    if selector:
        safe_sel = selector.replace("'", "\\'").replace("\\", "\\\\")
        expr = (
            f"const e = document.querySelector('{safe_sel}');"
            f"return e ? e.textContent.replace(/\\s+/g, ' ').trim() : null;"
        )
    else:
        expr = "return document.body.innerText.replace(/\\s+/g, ' ').trim();"
    text = js(expr, max_result_bytes=50_000)
    # Hint at recovery when a selector matches nothing — same trap as dom_query.
    if selector and (text is None or text == ""):
        return {
            "text": text,
            "hint": (
                f"selector {selector!r} matched nothing (or empty text). "
                "read_skill('workflow_dom_exploration') for selector discovery, "
                "OR read_skill('workflow_vision_first') if you suspect the field "
                "should be visible but DOM disagrees."
            ),
        }
    return text


EXTRACT_TEXT = ToolSpec(
    name="extract_text",
    description=(
        "Extract visible text from page (or single element by selector). "
        "Whitespace-collapsed. Returns null if selector not found. "
        "For batch extraction (e.g. all .product-card), use run_browser_python with js() helper instead."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "selector": {"type": "string", "description": "Optional CSS selector. If omitted, extracts whole page body."},
        },
    },
    handler=_extract_text_handler,
)


# ──────────────────────────────────
# screenshot — returns path only
# ──────────────────────────────────

def _screenshot_handler(tool_input: dict, ctx: ToolContext) -> dict:
    from browser.helpers import screenshot
    from pathlib import Path
    name = tool_input.get("filename", f"shot_{int(__import__('time').time() * 1000)}.png")
    full_page = bool(tool_input.get("full_page", False))
    # Save to the current worktree's screenshots/ subdirectory
    target = Path(ctx.worktree) / "screenshots" / name
    path = screenshot(path=target, full_page=full_page)
    from browser.helpers import current_url, current_title
    return {"path": path, "url": current_url(), "title": current_title()}


SCREENSHOT = ToolSpec(
    name="screenshot",
    description=(
        "Capture a screenshot to the worktree (returns path only — never embeds image bytes in LLM context). "
        "If you need to *understand* what's on screen, call analyze_screenshot(filename=...) "
        "or vision_page_skeleton() afterwards. For a vision-first scan of a page you've never "
        "extracted from before, vision_page_skeleton() is the right starting point."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "filename": {"type": "string", "description": "Optional filename (default: timestamped)"},
            "full_page": {"type": "boolean", "default": False, "description": "Capture full scrollable page"},
        },
    },
    handler=_screenshot_handler,
)


# ──────────────────────────────────
# analyze_screenshot — generic vision Q&A over a saved screenshot
# vision_page_skeleton — structured "what sections are on this page?" scan
# ──────────────────────────────────

def _resolve_screenshot_path(filename: str, ctx: ToolContext) -> "Path":
    """Accept either a bare filename or an absolute/relative path.

    Bare filenames are looked up in <worktree>/screenshots/.
    """
    from pathlib import Path
    p = Path(filename)
    if not p.is_absolute():
        # bare name → look in worktree/screenshots/
        candidate = Path(ctx.worktree) / "screenshots" / p.name
        if candidate.exists():
            return candidate
        # fall back to literal path under worktree
        candidate2 = Path(ctx.worktree) / filename
        if candidate2.exists():
            return candidate2
    return p


def _analyze_screenshot_handler(tool_input: dict, ctx: ToolContext) -> dict:
    from browser.vision import analyze_image, VisionNotConfiguredError
    filename = tool_input["filename"]
    query = tool_input["query"]
    path = _resolve_screenshot_path(filename, ctx)
    if not path.exists():
        raise FileNotFoundError(
            f"screenshot {filename!r} not found "
            f"(checked {Path(ctx.worktree) / 'screenshots' / Path(filename).name} and "
            f"absolute path). Take a screenshot first, then pass its returned path or basename."
        )
    try:
        result = analyze_image(str(path), query)
    except VisionNotConfiguredError as e:
        raise RuntimeError(
            f"Vision is not configured: {e}. "
            f"Add a `vision` section with model_id + api_key to config.json."
        ) from e
    return {
        "filename": str(path),
        "query": query,
        "answer": result["answer"],
        "model": result["model"],
        "tokens": result["tokens"],
    }


ANALYZE_SCREENSHOT = ToolSpec(
    name="analyze_screenshot",
    description=(
        "Ask a vision model a natural-language question about a saved screenshot. "
        "Use this when DOM extraction returns null/[] but you suspect the field IS visible — "
        "vision can confirm or deny what's actually rendered, breaking the single-modality "
        "trap where worker concludes 'absent' just because a CSS selector missed.\n"
        "\n"
        "Common queries:\n"
        "  - 'Is there a Reviews / Comments / FAQ section visible? If yes, how many items?'\n"
        "  - 'Compare this page with this JSON: {...}. Are any visible structured fields missing?'\n"
        "  - 'Where on the page is the price displayed? Quote the exact visible text.'\n"
        "\n"
        "Returns {filename, query, answer, model, tokens}. The 'answer' is plain text from "
        "the vision model — no parsing done here. For a structured page-section list, "
        "prefer vision_page_skeleton() instead.\n"
        "\n"
        "PREREQUISITE: a screenshot must already exist on disk. Call screenshot(full_page=True) "
        "first, then pass either the returned path or the bare filename."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "filename": {
                "type": "string",
                "description": "Path or bare filename of an existing screenshot in the worktree.",
            },
            "query": {
                "type": "string",
                "description": "What you want the vision model to tell you about the image.",
            },
        },
        "required": ["filename", "query"],
    },
    handler=_analyze_screenshot_handler,
    readonly=True,
    long_running=True,
)


def _vision_page_skeleton_handler(tool_input: dict, ctx: ToolContext) -> dict:
    """Take a viewport (or full-page) screenshot, then ask vision to enumerate sections.

    DEFAULT is viewport-only, NOT full_page — because long pages produce 18000+px
    screenshots that vision models heavily downsample, destroying detail in the
    bottom region (we measured this on TAAFT detail pages: a single full_page
    skeleton missed the entire Comments section because of downsampling). The
    worker drives multi-pass coverage via scroll + re-skeleton.

    Returns sections as parsed JSON + a `viewport` block (page geometry the worker
    needs to plan scroll positions and detect end-of-page).
    """
    from pathlib import Path
    import time
    from browser.helpers import screenshot as _shot, current_url, current_title, js
    from browser.vision import vision_skeleton_scan, VisionNotConfiguredError

    full_page = bool(tool_input.get("full_page", False))
    name = tool_input.get("filename") or (
        f"skeleton{'_full' if full_page else ''}_{int(time.time() * 1000)}.png"
    )
    target = Path(ctx.worktree) / "screenshots" / name
    path = _shot(path=target, full_page=full_page)

    # Page geometry — the worker uses this to plan the next scroll and detect
    # when it's reached the bottom (atBottom=true → stop the rescan loop).
    try:
        geom = js(
            "return {scrollY: window.scrollY, "
            "innerHeight: window.innerHeight, "
            "scrollHeight: document.documentElement.scrollHeight, "
            "atBottom: (window.scrollY + window.innerHeight) >= "
            "(document.documentElement.scrollHeight - 4)};"
        )
    except Exception:
        geom = {"scrollY": None, "innerHeight": None, "scrollHeight": None, "atBottom": None}

    try:
        out = vision_skeleton_scan(path)
    except VisionNotConfiguredError as e:
        raise RuntimeError(
            f"Vision is not configured: {e}. "
            f"Add a `vision` section with model_id + api_key to config.json."
        ) from e

    # If user opted into full_page and the result is very tall, warn that
    # bottom sections may be lost to vision-side downsampling.
    downsample_warning = None
    if full_page and isinstance(geom.get("scrollHeight"), (int, float)):
        sh = geom["scrollHeight"]; ih = geom.get("innerHeight") or 1
        if sh / ih > 4:
            downsample_warning = (
                f"full_page screenshot is {sh}px tall vs viewport {ih}px "
                f"(ratio {sh/ih:.1f}x). Vision models heavily downsample tall images — "
                f"sections in the bottom half may be missing. Prefer viewport mode "
                f"(default) and the scroll-and-rescan loop."
            )

    payload: Dict[str, Any] = {
        "screenshot_path": str(path),
        "url": current_url(),
        "title": current_title(),
        "sections": out["sections"],
        "section_count": len(out["sections"]),
        "parse_ok": out["parse_ok"],
        "raw_if_parse_failed": (out["raw"] if not out["parse_ok"] else None),
        "viewport": geom,
        "full_page_used": full_page,
        "downsample_warning": downsample_warning,
        "model": out["model"],
        "tokens": out["tokens"],
    }
    # Hint when the multi-pass coverage workflow is needed
    if not full_page and isinstance(geom.get("scrollHeight"), (int, float)) \
       and isinstance(geom.get("innerHeight"), (int, float)) \
       and not geom.get("atBottom") \
       and geom.get("scrollHeight") > geom.get("innerHeight") * 1.5:
        payload["hint"] = (
            "page is taller than viewport — this skeleton covers only the visible "
            "region. To map the full page, drive the multi-pass scroll-and-rescan "
            "loop: read_skill('workflow_vision_first') for the pattern, then "
            "scroll(dy=int(viewport.innerHeight * 0.85)) and re-call this tool "
            "until viewport.atBottom=true."
        )
    return payload


VISION_PAGE_SKELETON = ToolSpec(
    name="vision_page_skeleton",
    description=(
        "Take a screenshot of the CURRENT viewport and ask the vision model to enumerate "
        "every visible content section. The PRIMARY tool for the vision-first workflow.\n"
        "\n"
        "DEFAULT is viewport-only (not full_page), because vision models heavily downsample "
        "very tall images — bottom sections of long pages get lost. To cover a long page, "
        "drive a scroll-and-rescan loop yourself (see worker prompt for the pattern).\n"
        "\n"
        "WHEN TO USE:\n"
        "  ✓ First time on a new site (no skill matched in navigate's available_skills)\n"
        "  ✓ Skill matched but doesn't document every field your task requires\n"
        "  ✓ A DOM probe returned [] / null for a field you suspect should exist —\n"
        "    use this to confirm the section is visually there before guessing more selectors\n"
        "\n"
        "Returns:\n"
        "  {\n"
        "    screenshot_path, url, title,\n"
        "    sections: [{name, heading_text, approx_y, has_repeating_items, item_count_visible, sample_text}, ...],\n"
        "    section_count, parse_ok,\n"
        "    viewport: {scrollY, innerHeight, scrollHeight, atBottom},\n"
        "    full_page_used, downsample_warning,\n"
        "    model, tokens\n"
        "  }\n"
        "\n"
        "MULTI-PASS PATTERN for full coverage of a long page:\n"
        "  accumulated = []\n"
        "  while True:\n"
        "    skel = vision_page_skeleton()\n"
        "    accumulated.extend(skel.sections)\n"
        "    if skel.viewport.atBottom: break\n"
        "    scroll(dy = int(skel.viewport.innerHeight * 0.85))   # 15% overlap\n"
        "    sleep(0.4)                                            # let lazy-load fire\n"
        "  → dedupe accumulated by (name, heading_text); you now have a full page map\n"
        "\n"
        "USE THE OUTPUT TO DRIVE DOM:\n"
        "  - For each section the task cares about → derive a DOM anchor from heading_text\n"
        "    (e.g. xpath:'//h2[contains(., \"Reviews\")]/parent::*'), then dom_query / dom_outline\n"
        "    inside that subtree. Don't query the whole document.\n"
        "  - has_repeating_items=true with item_count_visible>0 means the DOM MUST contain\n"
        "    repeating elements — keep searching even if your first guess returns 0.\n"
        "\n"
        "If parse_ok is false, raw_if_parse_failed contains the model's raw text. "
        "Re-issue with a more specific question via analyze_screenshot."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "filename": {
                "type": "string",
                "description": "Optional filename for the new screenshot (default: timestamped).",
            },
            "full_page": {
                "type": "boolean",
                "default": False,
                "description": (
                    "If true, capture the full scrollable page in one shot (legacy mode). "
                    "DEFAULT false — full_page on tall pages causes downsampling and lost sections. "
                    "Use the multi-pass viewport pattern instead."
                ),
            },
        },
    },
    handler=_vision_page_skeleton_handler,
    readonly=True,
    long_running=True,
)


# ──────────────────────────────────
# scroll
# ──────────────────────────────────

def _scroll_handler(tool_input: dict) -> dict:
    from browser.helpers import scroll
    return scroll(
        dy=int(tool_input.get("dy", 500)),
        dx=int(tool_input.get("dx", 0)),
    )


SCROLL = ToolSpec(
    name="scroll",
    description="Scroll page. dy>0 down, dy<0 up. Returns {scrolled, scroll_y}.",
    input_schema={
        "type": "object",
        "properties": {
            "dy": {"type": "integer", "default": 500},
            "dx": {"type": "integer", "default": 0},
        },
    },
    handler=_scroll_handler,
)


# ──────────────────────────────────
# wait_user — human intervention
# ──────────────────────────────────

def _wait_user_handler(tool_input: dict) -> dict:
    """Truly blocking — returns only after the user presses Enter in the terminal.

    Bug fix: previously used input(), which in some environments (IDE redirected
    stdin / non-tty) raises EOFError immediately and the handler silently
    returned, letting the flow continue. Current behavior:
    - Detect stdin.isatty first; non-tty raises RuntimeError (LLM sees a clear error)
    - EOFError is treated as a real error, not silently returned
    - Use sys.stdin.readline() + explicit stdout flush, avoiding readline-lib pollution
    """
    import sys

    msg = tool_input.get("message", "Complete the action in the browser, then press Enter to continue")

    # 1. Environment check — non-interactive terminal is rejected directly
    if not sys.stdin.isatty():
        raise RuntimeError(
            "wait_user requires an interactive terminal (stdin is not a tty). "
            "This environment cannot accept human input. "
            "If running in --task / --demo mode, login/captcha must be resolved beforehand."
        )

    # 2. Explicit flush so the prompt is visible (avoid IDE line-buffering stalls)
    banner = (
        "\n" + "!" * 60 + "\n"
        f"  [WAIT_USER] {msg}\n"
        + "!" * 60 + "\n"
        "[Press Enter after completing the manual step]: "
    )
    sys.stdout.write(banner)
    sys.stdout.flush()

    # 3. Read one stdin line directly — avoid input() and readline-lib interference
    try:
        line = sys.stdin.readline()
    except KeyboardInterrupt:
        raise RuntimeError("wait_user interrupted by Ctrl-C") from None

    if line == "":
        # readline returning empty == EOF; environment is broken
        raise RuntimeError(
            "wait_user got EOF before user input — stdin closed or non-interactive. "
            "Tool aborted to prevent silent fall-through."
        )

    return {"status": "resumed", "user_input_len": len(line.rstrip("\n"))}


WAIT_USER = ToolSpec(
    name="wait_user",
    description=(
        "BLOCK until the human types Enter in the terminal. ONLY use for: "
        "(1) login / 2FA forms, "
        "(2) Cloudflare / reCAPTCHA / slider challenges, "
        "(3) payment confirmation. "
        "DO NOT use for selector tuning, exploration, or 'try-this-and-tell-me-the-result'. "
        "Raises RuntimeError if stdin is not a tty (--demo / --task / piped stdin can't use this)."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "message": {"type": "string", "description": "Specific instruction shown to the user"},
        },
        "required": ["message"],
    },
    handler=_wait_user_handler,
    long_running=True,
)


# ──────────────────────────────────
# dialogs
# ──────────────────────────────────

def _accept_dialog_handler(tool_input: dict) -> dict:
    from browser.helpers import accept_dialog
    return accept_dialog(text=tool_input.get("text"))


ACCEPT_DIALOG = ToolSpec(
    name="accept_dialog",
    description="Accept (click OK) the currently open native dialog (alert/confirm/prompt).",
    input_schema={
        "type": "object",
        "properties": {
            "text": {"type": "string", "description": "Optional text to fill (for prompt dialogs)"},
        },
    },
    handler=_accept_dialog_handler,
)


def _dismiss_dialog_handler(tool_input: dict) -> dict:
    from browser.helpers import dismiss_dialog
    return dismiss_dialog()


DISMISS_DIALOG = ToolSpec(
    name="dismiss_dialog",
    description="Dismiss (click Cancel) the currently open native dialog.",
    input_schema={"type": "object", "properties": {}},
    handler=_dismiss_dialog_handler,
)


# ──────────────────────────────────
# Skill tools (read site-specific playbooks on demand)
# ──────────────────────────────────

def _list_skills_handler(tool_input: dict) -> list:
    from browser.helpers import list_skills
    return list_skills(url=tool_input.get("url"))


LIST_SKILLS = ToolSpec(
    name="list_skills",
    description=(
        "List skill files matching a URL (default: current page). "
        "Returns [{name, description, tokens}, ...]. "
        "Skills contain site-specific URL patterns / selectors / known pitfalls "
        "written by humans or learned from past tasks. "
        "Note: navigate() already returns 'available_skills' in its response when on a known site — "
        "you may not need to call this explicitly."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "Optional URL (defaults to current page)"},
        },
    },
    handler=_list_skills_handler,
    readonly=True,
)


def _read_skill_handler(tool_input: dict) -> str:
    from browser.helpers import read_skill
    return read_skill(tool_input["name"])


READ_SKILL = ToolSpec(
    name="read_skill",
    description=(
        "Read the full markdown content of a skill (returned by list_skills or by navigate's "
        "'available_skills' field). Skills typically contain: URL patterns, key selectors, "
        "common pitfalls, recommended workflow code. "
        "READ THIS BEFORE writing batch scrape code on a known site — it can save you from "
        "the typical 'wrong selector / wrong url pattern' multi-turn debugging."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "Skill name (e.g. 'taaft_aitools')"},
        },
        "required": ["name"],
    },
    handler=_read_skill_handler,
    readonly=True,
)


# ──────────────────────────────────
# DOM exploration — let the LLM find selectors without entering run_browser_python.
# This is the heart of the "explore -> batch" workflow: the LLM uses these 3 tools
# to confirm selectors, then calls batch_browser_actions directly without ever
# entering code mode.
# ──────────────────────────────────

def _dom_classes_handler(tool_input: dict) -> list:
    from browser.helpers import dom_classes
    return dom_classes(
        min_count=int(tool_input.get("min_count", 3)),
        top_n=int(tool_input.get("top_n", 30)),
    )


DOM_CLASSES = ToolSpec(
    name="dom_classes",
    description=(
        "Return classes that appear >= min_count times on the current page, sorted by count. "
        "BEST tool to find list-item selector — a className appearing 50 times is almost certainly "
        "the product card / row class. Each entry: {class_name, count, tag_sample, text_sample}. "
        "Typical usage: navigate(list_url) → dom_classes(min_count=10) → pick the high-count class → "
        "verify with dom_query → use it as iteration anchor in batch_browser_actions."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "min_count": {"type": "integer", "default": 3,
                          "description": "Only return classes appearing >= this many times"},
            "top_n": {"type": "integer", "default": 30, "description": "Max entries to return"},
        },
    },
    handler=_dom_classes_handler,
    readonly=True,
)


def _dom_outline_handler(tool_input: dict) -> list:
    from browser.helpers import dom_outline
    return dom_outline(
        max_items=int(tool_input.get("max_items", 30)),
        min_text_len=int(tool_input.get("min_text_len", 4)),
    )


DOM_OUTLINE = ToolSpec(
    name="dom_outline",
    description=(
        "Sample N candidate interactive/text elements from the page in DOM order. "
        "Each entry: {tag, id, classes, text_preview, attrs, selector_hint}. "
        "Use when dom_classes is unhelpful (page is React/SPA with hashed classNames). "
        "Useful for finding a specific button / form / nav element by its text / attrs."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "max_items": {"type": "integer", "default": 30},
            "min_text_len": {"type": "integer", "default": 4,
                              "description": "Skip elements with textContent shorter than this"},
        },
    },
    handler=_dom_outline_handler,
    readonly=True,
)


def _dom_query_handler(tool_input: dict) -> Any:
    from browser.helpers import dom_query
    fields = tool_input.get("fields")
    matches = dom_query(
        selector=tool_input["selector"],
        max_items=int(tool_input.get("max_items", 20)),
        fields=fields if isinstance(fields, list) else None,
    )
    # Hint at vision-first recovery when a probe returns nothing — this is the
    # exact failure mode the prompt's decision ladder asks workers to recognize.
    if isinstance(matches, list) and len(matches) == 0:
        return {
            "matches": [],
            "count": 0,
            "hint": (
                "0 matches for selector. If you suspect this field exists but "
                "your selector is wrong, read_skill('workflow_dom_exploration') "
                "for selector discovery, OR read_skill('workflow_vision_first') "
                "for the multi-modal recovery (use vision_page_skeleton to see "
                "what's actually visible, then derive a selector from the heading)."
            ),
        }
    return matches


DOM_QUERY = ToolSpec(
    name="dom_query",
    description=(
        "VERIFY a CSS selector before committing to it. Returns up to max_items matching elements, "
        "each with {text, href, src, value, inner_html_size, dataset}. "
        "MUST do this before writing a batch loop — a 0-result query means the selector is wrong, "
        "and you'll save yourself N failed iterations. "
        "Allowed in batch_browser_actions step too (when you need per-iter probing). "
        "Returns [] (not error) when selector matches nothing."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "selector": {"type": "string", "description": "CSS selector to test"},
            "max_items": {"type": "integer", "default": 20},
            "fields": {"type": "array", "items": {"type": "string"},
                       "description": "Optional subset of {text, href, src, value, dataset} to return"},
        },
        "required": ["selector"],
    },
    handler=_dom_query_handler,
    readonly=True,
)


# ──────────────────────────────────
# Aggregated export
# ──────────────────────────────────

ALL_BROWSER_TOOLS = [
    NAVIGATE, CLICK, FILL, EXTRACT_TEXT, SCREENSHOT,
    ANALYZE_SCREENSHOT, VISION_PAGE_SKELETON,
    SCROLL, WAIT_USER, ACCEPT_DIALOG, DISMISS_DIALOG,
    DOM_CLASSES, DOM_OUTLINE, DOM_QUERY,
    LIST_SKILLS, READ_SKILL,
]
