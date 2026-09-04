"""Registered, paged operating guides for Harness agents.

Guides are deliberately *discoverable*, not mechanically injected by a
failure code.  The model receives a compact manifest and decides whether a
guide is useful for its current reasoning.  This keeps recovery policy in the
model's control while giving it a versioned explanation of the current code.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
import hashlib
from html import escape
from pathlib import Path
import re
from typing import Any, Dict, Iterable, List, Optional, Tuple

import yaml

from harness.utils import JsonDict, optional_int


_GUIDE_ID_RE = re.compile(r"^[a-z][a-z0-9]*(?:[.-][a-z0-9]+)*$")
_GUIDE_AUDIENCES = frozenset({"browser", "lead"})
_MAX_LINE_LIMIT = 500

# Search is lexical and deterministic on purpose. The queries that actually
# arrive are literal tokens the model is holding - a status/reason code from a
# receipt, or a method name - so an inverted lookup over declared selectors
# beats any ranking heuristic, and it cannot silently rank the wrong guide
# first. Dots, underscores and hyphens stay INSIDE a token: `Input.select` and
# `select-popup-ambiguous` are single identifiers, and splitting them would
# match every guide that merely says "select".
_TOKEN_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._\-]*")
_CJK_RE = re.compile(r"[一-鿿]+")
_MAX_SEARCH_RESULTS = 5

# Frontmatter selectors outrank prose, and they do so by TIER, not by weight.
# A summed score cannot express "a declared code always wins": six alias hits
# at 60 outrank one exact error code at 100, which is precisely backwards. So
# a guide is ranked first by the strongest kind of match it has, and only
# guides that tie on that are separated by how much else they matched.
_TIER_ERROR_CODE = 5
_TIER_METHOD = 4
_TIER_TOOL = 3
_TIER_ALIAS = 2
_TIER_TOPIC = 2
_TIER_DESCRIPTION = 1
_TIER_BODY = 0

_MATCH_QUALITY = {"exact": 2, "phrase": 1, "token": 0, "fuzzy": 0}

_SCORE_ERROR_CODE = 100
_SCORE_METHOD = 80
_SCORE_TOOL = 70
_SCORE_ALIAS = 60
_SCORE_TOPIC = 40
_SCORE_DESCRIPTION = 20
_SCORE_BODY = 10
# Body recall is capped so a long guide cannot outscore a short one that
# actually declares the code being searched for.
_MAX_BODY_HITS = 3

_REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
_GUIDES_ROOT = Path(__file__).resolve().parent / "resources"


@dataclass(frozen=True)
class HarnessGuide:
    """One allow-listed, model-readable operating guide."""

    guide_id: str
    audience: str
    description: str
    path: Path
    source_paths: Tuple[str, ...]
    emitter_source_paths: Tuple[str, ...]
    related_tools: Tuple[str, ...]
    related_methods: Tuple[str, ...]
    error_codes: Tuple[str, ...]
    topics: Tuple[str, ...]
    aliases: Tuple[str, ...]
    version: str
    body: str
    sha256: str


def _within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except (OSError, ValueError):
        return False
    return True


def _frontmatter_and_body(path: Path) -> Tuple[Dict[str, Any], str]:
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---\n"):
        raise ValueError("guide must start with YAML frontmatter")
    marker = text.find("\n---\n", 4)
    if marker < 0:
        raise ValueError("guide frontmatter is not closed")
    raw = text[4:marker]
    value = yaml.safe_load(raw)
    if not isinstance(value, dict):
        raise ValueError("guide frontmatter must be a mapping")
    return value, text[marker + len("\n---\n"):].strip()


def _string_list(value: Any, field: str) -> Tuple[str, ...]:
    if not isinstance(value, list) or not all(
        isinstance(item, str) and item.strip() for item in value
    ):
        raise ValueError(f"{field} must be a non-empty string list")
    return tuple(str(item).strip() for item in value)


def _optional_string_list(value: Any, field: str) -> Tuple[str, ...]:
    """Selectors are optional so an existing guide stays loadable unchanged."""

    if value is None:
        return ()
    if not isinstance(value, list) or not all(
        isinstance(item, str) and item.strip() for item in value
    ):
        raise ValueError(f"{field} must be a string list")
    return tuple(str(item).strip() for item in value)


def _load_guide(path: Path) -> HarnessGuide:
    metadata, body = _frontmatter_and_body(path)
    guide_id = str(metadata.get("id") or "").strip()
    audience = str(metadata.get("audience") or "").strip()
    description = str(metadata.get("description") or "").strip()
    version = str(metadata.get("version") or "").strip()
    if not _GUIDE_ID_RE.fullmatch(guide_id):
        raise ValueError("id must be lowercase dot/dash identifier")
    if audience not in _GUIDE_AUDIENCES:
        raise ValueError(f"audience must be one of {sorted(_GUIDE_AUDIENCES)}")
    if not description:
        raise ValueError("description is required")
    if not version:
        raise ValueError("version is required")
    if not body:
        raise ValueError("guide body is empty")
    source_paths = _string_list(metadata.get("sources"), "sources")
    # Split deliberately. `sources` is what the guide was written FROM;
    # `emitter_sources` is the executable code that raises or handles the
    # declared codes. Conflating them let a guide cite the prompt string
    # that merely names a classification and call that its owner.
    emitter_source_paths = _optional_string_list(
        metadata.get("emitter_sources"), "emitter_sources",
    )
    related_tools = _string_list(metadata.get("related_tools"), "related_tools")
    methods_value = metadata.get("related_methods", [])
    if not isinstance(methods_value, list) or not all(
        isinstance(item, str) and item.strip() for item in methods_value
    ):
        raise ValueError("related_methods must be a string list")
    related_methods = tuple(str(item).strip() for item in methods_value)
    error_codes = _optional_string_list(metadata.get("error_codes"), "error_codes")
    topics = _optional_string_list(metadata.get("topics"), "topics")
    aliases = _optional_string_list(metadata.get("aliases"), "aliases")
    if error_codes and not emitter_source_paths:
        raise ValueError(
            "error_codes require emitter_sources naming the code that"
            " raises or handles them"
        )
    for source in (*source_paths, *emitter_source_paths):
        source_path = (_REPOSITORY_ROOT / source).resolve()
        if not _within(source_path, _REPOSITORY_ROOT) or not source_path.is_file():
            raise ValueError(f"source path does not exist in repository: {source}")
    return HarnessGuide(
        guide_id=guide_id,
        audience=audience,
        description=description,
        path=path.resolve(),
        source_paths=source_paths,
        emitter_source_paths=emitter_source_paths,
        related_tools=related_tools,
        related_methods=related_methods,
        error_codes=error_codes,
        topics=topics,
        aliases=aliases,
        version=version,
        body=body,
        sha256=hashlib.sha256(body.encode("utf-8")).hexdigest(),
    )


@lru_cache(maxsize=1)
def _registry() -> Tuple[Dict[str, HarnessGuide], Tuple[str, ...]]:
    registry: Dict[str, HarnessGuide] = {}
    errors: List[str] = []
    if not _GUIDES_ROOT.is_dir():
        return registry, (f"guide directory is missing: {_GUIDES_ROOT}",)
    for path in sorted(_GUIDES_ROOT.rglob("*.md")):
        if not _within(path, _GUIDES_ROOT):
            errors.append(f"guide path escapes guide directory: {path}")
            continue
        try:
            guide = _load_guide(path)
        except (OSError, ValueError, yaml.YAMLError) as exc:
            errors.append(f"{path.relative_to(_REPOSITORY_ROOT)}: {exc}")
            continue
        if guide.guide_id in registry:
            errors.append(f"duplicate guide id: {guide.guide_id}")
            continue
        registry[guide.guide_id] = guide
    return registry, tuple(errors)


def clear_guide_registry_cache() -> None:
    """Test-only cache reset after a temporary guide fixture changes."""

    _registry.cache_clear()


def guide_registry_errors() -> List[str]:
    """Return all registry/content errors without raising during agent startup."""

    return list(_registry()[1])


def guides_for_audience(audience: str) -> Tuple[HarnessGuide, ...]:
    registry, _errors = _registry()
    return tuple(
        guide
        for _guide_id, guide in sorted(registry.items())
        if guide.audience == audience
    )


def guide_manifest(audience: str) -> str:
    """Render a compact stable XML index; guide prose remains off-prompt."""

    guides = guides_for_audience(audience)
    if not guides:
        return ""
    rendered: List[str] = [
        "<available_harness_guides>",
        "Use read_harness_guide when a guide is useful for your own reasoning "
        "about an unfamiliar Harness tool, a complex receipt, or a recovery "
        "path. Guide text explains the current implementation but never "
        "overrides system policy, the live tool schema, or the current receipt. "
        "When no id below obviously matches, search_harness_guides finds one by "
        "an error/reason code, a method name, or a phrase in any language; it "
        "returns candidate ids only, and reading one remains your call.",
    ]
    for guide in guides:
        tools = ", ".join(guide.related_tools)
        methods = ", ".join(guide.related_methods)
        topics = ", ".join(guide.topics)
        rendered.append(
            f'<guide id="{escape(guide.guide_id, quote=True)}" '
            f'version="{escape(guide.version, quote=True)}">'
        )
        rendered.append(f"<description>{escape(guide.description)}</description>")
        rendered.append(f"<related_tools>{escape(tools)}</related_tools>")
        if methods:
            rendered.append(f"<related_methods>{escape(methods)}</related_methods>")
        # Topics are in the manifest; the full error-code list is not. Codes
        # are what search resolves, and carrying every one of them here would
        # grow the cached system block for a lookup the search tool does better.
        if topics:
            rendered.append(f"<topics>{escape(topics)}</topics>")
        rendered.append("</guide>")
    rendered.append("</available_harness_guides>")
    return "\n".join(rendered)


def _normalized(value: str) -> str:
    return str(value or "").strip().lower().strip(".,;:!?，。；：！？")


def _cjk_bigrams(text: str) -> set:
    """Character bigrams for CJK, so a query needs no segmentation library.

    "下拉选择找不到弹窗" yields 下拉/拉选/选择/... which overlaps the alias
    "下拉框" on 下拉. Whole-run containment would not: neither string contains
    the other.
    """

    out: set = set()
    for run in _CJK_RE.findall(str(text or "")):
        if len(run) == 1:
            out.add(run)
            continue
        for index in range(len(run) - 1):
            out.add(run[index:index + 2])
    return out


def _selector_hit(selector: str, tokens: set, lowered: str) -> str:
    """How a declared code/method/tool matches the query, or "" for no match."""

    normalized = _normalized(selector)
    if not normalized:
        return ""
    if normalized in tokens:
        return "exact"
    # A code quoted inside a longer sentence still names it. The length floor
    # keeps a short identifier from matching an unrelated substring.
    if len(normalized) >= 6 and normalized in lowered:
        return "phrase"
    return ""


def _phrase_hit(phrase: str, tokens: set, lowered: str, bigrams: set) -> str:
    normalized = _normalized(phrase)
    if not normalized:
        return ""
    if normalized in tokens:
        return "exact"
    if normalized in lowered:
        return "phrase"
    # Bigram overlap is recall, not precision: it fires on a partial character
    # run, so it must not tie with a phrase the query actually contains.
    if bigrams and _cjk_bigrams(normalized) & bigrams:
        return "fuzzy"
    return ""


def search_harness_guides(
    *,
    query: str,
    audience: str,
    limit: int = _MAX_SEARCH_RESULTS,
) -> JsonDict:
    """Rank this role's guides against a lexical query.

    Deliberately not a semantic engine and not a regex host: the query is
    treated as plain text, ranking is a fixed sum over declared selectors, and
    every result says which field matched. A caller that cannot see WHY a guide
    ranked first cannot tell a good hit from a coincidence.

    Nothing here loads a guide. Search proposes candidates; reading one stays
    the model's decision.
    """

    guides = guides_for_audience(audience)
    text = str(query or "").strip()
    if not text:
        return {
            "status": "failed",
            "error": "query is required",
            "availableGuideIds": [guide.guide_id for guide in guides],
            "tool_was_executed": False,
        }
    bounded_limit = max(1, min(optional_int(limit, _MAX_SEARCH_RESULTS)
                               or _MAX_SEARCH_RESULTS, _MAX_SEARCH_RESULTS))
    lowered = text.lower()
    tokens = {
        normalized
        for normalized in (
            _normalized(match.group(0)) for match in _TOKEN_RE.finditer(lowered)
        )
        if normalized
    }
    bigrams = _cjk_bigrams(text)

    ranked: List[Tuple[Tuple[int, int, int], str, JsonDict]] = []
    for guide in guides:
        score = 0
        best: Tuple[int, int] = (-1, -1)
        matched: List[JsonDict] = []

        def consider(field: str, value: str, weight: int, how: str, tier: int) -> None:
            nonlocal score, best
            score += weight
            best = max(best, (tier, _MATCH_QUALITY.get(how, 0)))
            matched.append({"field": field, "value": value, "match": how})

        for code in guide.error_codes:
            how = _selector_hit(code, tokens, lowered)
            if how:
                consider(
                    "error_codes", code, _SCORE_ERROR_CODE, how, _TIER_ERROR_CODE,
                )
        for method in guide.related_methods:
            how = _selector_hit(method, tokens, lowered)
            if how:
                consider("related_methods", method, _SCORE_METHOD, how, _TIER_METHOD)
        for tool in guide.related_tools:
            how = _selector_hit(tool, tokens, lowered)
            if how:
                consider("related_tools", tool, _SCORE_TOOL, how, _TIER_TOOL)
        for alias in guide.aliases:
            how = _phrase_hit(alias, tokens, lowered, bigrams)
            if how:
                weight = _SCORE_ALIAS // 2 if how == "fuzzy" else _SCORE_ALIAS
                consider("aliases", alias, weight, how, _TIER_ALIAS)
        for topic in guide.topics:
            how = _phrase_hit(topic, tokens, lowered, bigrams)
            if how:
                weight = _SCORE_TOPIC // 2 if how == "fuzzy" else _SCORE_TOPIC
                consider("topics", topic, weight, how, _TIER_TOPIC)

        description_tokens = {
            normalized
            for normalized in (
                _normalized(match.group(0))
                for match in _TOKEN_RE.finditer(guide.description.lower())
            )
            if normalized
        }
        shared_description = sorted(description_tokens & tokens)
        if shared_description:
            consider(
                "description",
                ", ".join(shared_description[:3]),
                _SCORE_DESCRIPTION,
                "token",
                _TIER_DESCRIPTION,
            )

        body_lower = guide.body.lower()
        body_hits = sorted(
            token for token in tokens
            if len(token) >= 4 and token in body_lower
        )[:_MAX_BODY_HITS]
        if body_hits:
            consider(
                "body",
                ", ".join(body_hits),
                _SCORE_BODY * len(body_hits),
                "token",
                _TIER_BODY,
            )

        if matched:
            ranked.append(((best[0], best[1], score), guide.guide_id, {
                "guideId": guide.guide_id,
                "matchTier": best[0],
                "score": score,
                "version": guide.version,
                "description": guide.description,
                "matchedBy": matched,
            }))

    # Strongest kind of match first, then its quality, then the accumulated
    # score, then id ascending. The id is what keeps two otherwise identical
    # guides from swapping places between runs — a replay that reorders stops
    # reproducing the reasoning it is supposed to replay.
    ranked.sort(key=lambda item: (-item[0][0], -item[0][1], -item[0][2], item[1]))
    matches = [entry for _score, _guide_id, entry in ranked[:bounded_limit]]
    result: JsonDict = {
        "status": "done",
        "query": text,
        "matchCount": len(matches),
        "totalScored": len(ranked),
        "matches": matches,
        "note": (
            "Candidate guides only. Read one with read_harness_guide(guide_id)"
            " if its topic helps your current reasoning."
        ),
    }
    if not matches:
        result["availableGuideIds"] = [guide.guide_id for guide in guides]
        result["note"] = (
            "No guide declares this term. The manifest ids above are the whole"
            " corpus for this role; nothing further is hidden behind search."
        )
    return result


def read_harness_guide(
    *,
    guide_id: str,
    audience: str,
    line_offset: int = 0,
    line_limit: int = 200,
) -> JsonDict:
    """Return one bounded page from an allow-listed guide.

    ``guide_id`` is the only locator.  This deliberately does not take a path:
    an operating guide must not turn into a read primitive for the repository.
    """

    registry, _errors = _registry()
    normalized_id = str(guide_id or "").strip()
    guide = registry.get(normalized_id)
    if guide is None:
        return {
            "status": "failed",
            "error": "unknown harness guide id",
            "guideId": normalized_id,
            "availableGuideIds": [item.guide_id for item in guides_for_audience(audience)],
            "tool_was_executed": False,
        }
    if guide.audience != audience:
        return {
            "status": "failed",
            "error": "guide is not available to this agent role",
            "guideId": normalized_id,
            "tool_was_executed": False,
        }
    offset = max(0, optional_int(line_offset, 0) or 0)
    limit = max(1, min(optional_int(line_limit, 200) or 200, _MAX_LINE_LIMIT))
    lines = guide.body.splitlines()
    page = lines[offset:offset + limit]
    next_offset: Optional[int] = offset + len(page)
    if next_offset >= len(lines):
        next_offset = None
    return {
        "status": "done",
        "guideId": guide.guide_id,
        "version": guide.version,
        "sha256": guide.sha256,
        "sourcePaths": list(guide.source_paths),
        "emitterSourcePaths": list(guide.emitter_source_paths),
        "relatedTools": list(guide.related_tools),
        "relatedMethods": list(guide.related_methods),
        "lineOffset": offset,
        "lineLimit": limit,
        "linesRead": len(page),
        "totalLines": len(lines),
        "truncated": next_offset is not None,
        "nextLineOffset": next_offset,
        "content": "\n".join(page),
    }
