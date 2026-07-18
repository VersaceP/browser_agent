"""Normalization helpers shared by browser artifact capture and validators."""

from __future__ import annotations

from typing import Any, List


_SAVED_PATH_KEYS = frozenset({
    "savedpath",   # DOM.getImg/Page.screenshot
    "savepath",    # Download.getStatus
    "localpath",
    "filepath",
    "downloaded",  # File.download returns {downloaded: <path>, url}
})


def saved_paths_from_value(value: Any) -> List[str]:
    """Collect native file-result paths from nested ABCP envelopes."""
    paths: List[str] = []

    def walk(item: Any) -> None:
        if isinstance(item, dict):
            for key, child in item.items():
                if str(key).lower() in _SAVED_PATH_KEYS:
                    if isinstance(child, str) and child.strip():
                        paths.append(child.strip())
                else:
                    walk(child)
        elif isinstance(item, list):
            for child in item:
                walk(child)

    walk(value)
    return list(dict.fromkeys(paths))
