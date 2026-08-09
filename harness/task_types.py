"""Shared task_type definitions and alias normalization."""

from __future__ import annotations

from typing import Dict, FrozenSet


VALID_TASK_TYPES: FrozenSet[str] = frozenset({
    "general",
    "web_search",
    "web_scrape",
    "form_filling",
    "file_download",
    "file_upload",
    "browser_state_management",
})

TASK_TYPE_ALIASES = {
    "browser_data_collection": "web_scrape",
    "browser_action": "form_filling",
    "form_fill": "form_filling",
    "download_file": "file_download",
}

# What each task_type means in terms of WORK, one line each. The capability
# consequences are deliberately absent: tool_policy derives those from its own
# disabled-domain table (describe_task_types) so a policy change can never leave
# a stale promise behind in this prose.
TASK_TYPE_SCENARIOS: Dict[str, str] = {
    "general": (
        "legacy/unclassifiable work only; NEVER use it to combine kinds of work"
        " or bypass a restricted domain — split mixed work into phases"
    ),
    "web_search": "query a search engine and read its result listings",
    "web_scrape": (
        "read-only extraction of page content — text, attributes, links,"
        " structured rows; may export a rendered page <img> with DOM.getImg,"
        " but cannot use Download.* for videos/PDFs/arbitrary URL files"
    ),
    "form_filling": (
        "fill, select, or submit page controls, including attaching a local"
        " file to an upload widget"
    ),
    "file_download": (
        "the phase must SAVE a file to disk — image, video, PDF, export;"
        " keeps every read capability web_scrape has and adds the Download"
        " domain"
    ),
    "file_upload": "send local files into the page",
    "browser_state_management": (
        "maintain bookmarks, history, or fleet state rather than page content"
    ),
}

# The one classification rule models get wrong on their own: URL-file download
# needs Download.*, while DOM.getImg remains a native page read/export. Mixed
# work should be split when possible; if one page-owning phase must both inspect
# and download a non-image asset, file_download retains the DOM read surface.
TASK_TYPE_SELECTION_RULE = (
    "Saving a video, PDF, archive, export, or arbitrary URL file requires"
    " file_download; exporting an actual rendered <img> with DOM.getImg may"
    " remain web_scrape. Split other mixed work into phase-specific types, and"
    " never use general as a permission bypass."
)


def normalize_task_type(task_type: object) -> str:
    normalized = str(task_type or "general").strip() or "general"
    return TASK_TYPE_ALIASES.get(normalized, normalized)


def resolve_task_type_fail_closed(
    task_type: object,
    *,
    fallback: str = "web_scrape",
) -> str:
    """Resolve a policy-bearing task type without conflating missing with general.

    Explicit ``general`` remains valid. Missing, blank, or unknown values use a
    restricted fallback so a future validation bypass cannot silently expose
    every method domain.
    """
    raw = str(task_type or "").strip()
    canonical = normalize_task_type(raw) if raw else ""
    if raw and canonical in VALID_TASK_TYPES:
        return canonical
    fallback_canonical = normalize_task_type(fallback)
    return (
        fallback_canonical
        if fallback_canonical in VALID_TASK_TYPES
        else "web_scrape"
    )


def task_type_choices_for_error() -> list[str]:
    return sorted(set(VALID_TASK_TYPES) | set(TASK_TYPE_ALIASES))
