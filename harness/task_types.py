"""Shared task_type definitions and alias normalization."""

from __future__ import annotations

from typing import FrozenSet


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


def normalize_task_type(task_type: object) -> str:
    normalized = str(task_type or "general").strip() or "general"
    return TASK_TYPE_ALIASES.get(normalized, normalized)


def task_type_choices_for_error() -> list[str]:
    return sorted(set(VALID_TASK_TYPES) | set(TASK_TYPE_ALIASES))
