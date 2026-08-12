"""
harness.observation.semantic_index - Phase B: harness-internal SemanticTree use.

This module is the HARNESS-INTERNAL auto-digest path, independent of the model
tool surface (the model may now call DOM.getSemanticTree directly as a limited
diagnostic — see tool_policy). Gated by HarnessConfig.semantic_tree == "internal",
the harness may make a one-shot, render_recovery-wrapped call to derive a tiny
DIGEST that never enters model context. The raw tree is always digested and
discarded.

NOT used for (DISPROVEN on this build — abcp-panel-quirks #8/#9): scroll-container
discovery (isScrollable only flags #document) and shadow-host mapping (shadow not
traversed). Do NOT call per scroll iteration.

USED for (validated): repeated-ROW detection -> CSS selector candidates when an
extraction selector matched nothing, and a scoped subtree digest at an AX
canonical id. Row detection is SAME-PARENT sibling grouping with an area + text
quality gate, NOT a global class count — so it surfaces row/card CONTAINERS
(tr.athing, div.card) rather than utility/leaf classes (span.flex). Selectors are
CSS-escaped so Tailwind-style classes (text-[#9d9d9d], hover:bg-black/5) are safe.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from harness.render_recovery import build_render_recovery_runner
from harness.semantic_frames import graph_digest, root_tree
from harness.utils import JsonDict

# A real list/card row has non-trivial geometry; utility/leaf groups (inline
# spans, icons) are tiny. Area gate filters those out of selector candidates.
MIN_ROW_AREA = 1500.0

# Layer-2 digest discipline: a candidate's selector/signature is capped so an
# abnormal page (e.g. a 5000-char className) can't balloon the model-facing
# summary. A capped selector is incomplete CSS, so it is flagged truncated and
# never recommended for a blind retry.
MAX_SELECTOR_LEN = 240


def _semantic_internal(agent: Any) -> bool:
    harness = getattr(getattr(agent, "runtime", None), "harness", None)
    return str(getattr(harness, "semantic_tree", "off") or "off") == "internal"


def _bounds_area(bounds: Any) -> float:
    if not isinstance(bounds, dict):
        return 0.0
    width = bounds.get("w", bounds.get("width", 0))
    height = bounds.get("h", bounds.get("height", 0))
    try:
        return float(width) * float(height)
    except (TypeError, ValueError):
        return 0.0


def _classes(class_name: Any) -> List[str]:
    return [c for c in str(class_name or "").split() if c]


def _class_signature(tag: Any, class_name: Any) -> str:
    """Human-readable grouping key: tag + raw classes, e.g. 'div.li_row'. Empty
    when the node has no class (a bare 'div' is too generic to be a candidate)."""
    classes = _classes(class_name)
    if not classes:
        return ""
    return str(tag or "").strip().lower() + "." + ".".join(classes)


def _css_escape_class(value: str) -> str:
    """Escape a class value for safe use after '.' in a CSS selector (subset of
    CSS.escape covering Tailwind-style classes: ':', '/', '[', ']', '#', '.',
    and a leading digit). Unknown specials are backslash-escaped."""
    out: List[str] = []
    for i, ch in enumerate(str(value)):
        code = ord(ch)
        is_word = ("a" <= ch <= "z") or ("A" <= ch <= "Z") or ("0" <= ch <= "9") or ch in "_-" or code >= 0x80
        if is_word:
            if i == 0 and "0" <= ch <= "9":
                out.append("\\3" + ch + " ")  # a leading digit must be hex-escaped
            else:
                out.append(ch)
        else:
            out.append("\\" + ch)
    return "".join(out)


def _css_selector(tag: Any, class_name: Any) -> str:
    """A CSS-escaped selector, e.g. ('a','ai_link new_tab') -> 'a.ai_link.new_tab'
    and ('div','hover:bg-black/5') -> 'div.hover\\:bg-black\\/5'."""
    classes = _classes(class_name)
    if not classes:
        return ""
    tag_text = str(tag or "").strip().lower()
    return tag_text + "".join("." + _css_escape_class(c) for c in classes)


def _first_text(node: Any) -> str:
    found: List[str] = []

    def walk(n: Any) -> None:
        if not isinstance(n, dict) or found:
            return
        text = str(n.get("text") or "").strip()
        if text:
            found.append(text[:120])
            return
        for child in n.get("children") or []:
            if found:
                break
            walk(child)

    walk(node)
    return found[0] if found else ""


def repeated_row_candidates(
    tree: Any, *, min_count: int = 5, top: int = 8, min_area: float = MIN_ROW_AREA
) -> List[JsonDict]:
    """SAME-PARENT sibling groups that repeat (the list/card-row signature),
    quality-gated by average bounds area + text presence so utility/leaf classes
    don't outrank real rows. Pure -> testable. Returns up to `top`
    [{cssSelector, classSignature, count, avgArea, sampleText}] ranked by
    (count, area). These are CANDIDATES for the model/JS extractor to try —
    getSemanticTree has no href so it cannot replace JS extraction, only point at
    where rows live."""
    agg: Dict[str, JsonDict] = {}

    def walk(node: Any) -> None:
        if not isinstance(node, dict):
            return
        children = [c for c in (node.get("children") or []) if isinstance(c, dict)]
        groups: Dict[str, List[JsonDict]] = {}
        for child in children:
            sig = _class_signature(child.get("tag"), child.get("className"))
            if sig:
                groups.setdefault(sig, []).append(child)
        for sig, members in groups.items():
            if len(members) < 2:  # must repeat AMONG SIBLINGS, not globally
                continue
            entry = agg.setdefault(sig, {
                "count": 0, "area_sum": 0.0, "area_n": 0, "sampleText": "",
                "tag": members[0].get("tag"), "className": members[0].get("className"),
            })
            entry["count"] += len(members)
            for member in members:
                area = _bounds_area(member.get("bounds"))
                if area > 0:
                    entry["area_sum"] += area
                    entry["area_n"] += 1
                if not entry["sampleText"]:
                    entry["sampleText"] = _first_text(member)
        for child in children:
            walk(child)

    walk(tree)

    out: List[JsonDict] = []
    for sig, entry in agg.items():
        if entry["count"] < min_count:
            continue
        avg_area = (entry["area_sum"] / entry["area_n"]) if entry["area_n"] else 0.0
        if avg_area < min_area:
            continue                       # filters tiny utility/leaf-class groups
        if not entry["sampleText"]:
            continue                       # rows carry text; pure-layout wrappers don't
        css = _css_selector(entry["tag"], entry["className"])
        candidate: JsonDict = {
            "cssSelector": css[:MAX_SELECTOR_LEN],
            "classSignature": sig[:MAX_SELECTOR_LEN],
            "count": entry["count"],
            "avgArea": round(avg_area, 1),
            "sampleText": entry["sampleText"][:120],
        }
        if len(css) > MAX_SELECTOR_LEN or len(sig) > MAX_SELECTOR_LEN:
            candidate["truncated"] = True   # incomplete CSS — do not blind-retry
        out.append(candidate)
    out.sort(key=lambda c: (c["count"], c["avgArea"]), reverse=True)
    return out[:top]


def digest_subtree(tree: Any, *, max_repeated: int = 5) -> JsonDict:
    """Compact digest of a scoped subtree: root identity + repeated child rows +
    a text sample. Pure -> testable."""
    if not isinstance(tree, dict):
        return {}
    return {
        "tag": tree.get("tag"),
        "className": tree.get("className"),
        "elementId": tree.get("elementId"),
        "bounds": tree.get("bounds"),
        "sampleText": _first_text(tree),
        "repeatedChildRows": repeated_row_candidates(
            tree, min_count=2, top=max_repeated, min_area=0.0
        ),
    }


async def _call_semantic_tree(agent: Any, params: JsonDict) -> Optional[JsonDict]:
    """One render_recovery-wrapped getSemanticTree call. Returns the ANCHORED
    frame's local tree or None (failed / recovery exhausted / no tree). Never
    raises into the caller.

    The action returns a frame graph; only the root frame's tree is digested,
    because a selector mined from an iframe cannot be handed back to a DOM read
    (see harness.semantic_frames). Frames left unread are logged rather than
    dropped silently, so a later "no candidates" has an explanation."""
    runner = getattr(agent, "render_recovery_runner", None)
    if runner is None:
        runner = build_render_recovery_runner(
            browser=agent.browser,
            logger=getattr(agent, "logger", None),
            capability_methods=getattr(agent, "capability_methods", set()),
            recent_recoveries=getattr(agent, "_render_recovery_recent", {}),
        )
    try:
        response, _recovery = await runner.call("DOM.getSemanticTree", params)
    except Exception as exc:  # noqa: BLE001 - internal probe must never break the caller
        logger = getattr(agent, "logger", None)
        if logger is not None:
            logger.write("semantic_index.error", {"error": str(exc)[:300]})
        return None
    data = response.get("data") if isinstance(response, dict) else None
    digest = graph_digest(data)
    logger = getattr(agent, "logger", None)
    if logger is not None and (
        digest.get("otherFrameCount") or digest.get("unavailableFrames")
    ):
        logger.write("semantic_index.frame_graph", {
            "pageId": str(params.get("pageId") or ""),
            **digest,
        })
    return root_tree(data)


async def discover_selector_candidates(
    agent: Any, page_id: str, *, min_count: int = 5, top: int = 8
) -> Optional[List[JsonDict]]:
    """One-shot (gated, internal) repeated-ROW selector candidates for a 0-row
    extraction. Cached per (pageId, axtree_epoch, min_count, top) so repeated
    bad-selector retries on the same page don't re-pull the full tree. Returns []
    when nothing qualifies, or None when disabled/failed."""
    if not _semantic_internal(agent):
        return None
    epoch = int(getattr(agent, "axtree_epoch", 0) or 0)
    key = (str(page_id), epoch, int(min_count), int(top))
    cache = getattr(agent, "_semantic_selcand_cache", None)
    if isinstance(cache, dict) and cache.get("key") == key:
        return cache.get("candidates")

    tree = await _call_semantic_tree(
        agent,
        {"pageId": page_id, "filterNoise": True,
         "purpose": "semantic_index: selector-candidate discovery (internal)"},
    )
    if tree is None:
        return None  # transient failure -> don't cache, allow retry
    candidates = repeated_row_candidates(tree, min_count=min_count, top=top)
    agent._semantic_selcand_cache = {"key": key, "candidates": candidates}
    logger = getattr(agent, "logger", None)
    if logger is not None:
        logger.write("semantic_index.selector_candidates", {
            "pageId": str(page_id), "epoch": epoch, "count": len(candidates),
            "top": candidates[0]["cssSelector"] if candidates else None,
        })
    return candidates


async def inspect_semantic_subtree(
    agent: Any, page_id: str, ax_id: str
) -> Optional[JsonDict]:
    """Scoped DOM structure digest anchored at an AX canonical id (gated,
    internal) to supplement AXTree for disambiguation. Returns the digest, or
    None when disabled/failed."""
    if not _semantic_internal(agent) or not ax_id:
        return None
    tree = await _call_semantic_tree(
        agent,
        {"pageId": page_id, "id": ax_id, "filterNoise": True,
         "purpose": "semantic_index: scoped subtree inspect (internal)"},
    )
    if tree is None:
        return None
    digest = digest_subtree(tree)
    logger = getattr(agent, "logger", None)
    if logger is not None:
        logger.write("semantic_index.subtree",
                     {"pageId": str(page_id), "axId": ax_id, "tag": digest.get("tag")})
    return digest
