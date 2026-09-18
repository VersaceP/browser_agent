"""Normalization helpers shared by browser artifact capture and validators."""

from __future__ import annotations

from typing import Any, List


_SAVED_PATH_KEYS = frozenset({
    "savedpath",   # DOM.getImg/Page.screenshot
    "savepath",    # Download.start/Download.list records
    "localpath",
    "filepath",
    "downloaded",  # legacy File.download returned {downloaded: <path>, url}
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


def declared_file_paths(rows: Any, fields: set[str]) -> List[str]:
    """Literal contract-selected paths, including nested object/array fields."""
    paths: List[str] = []
    def walk(value: Any, key: str = "") -> None:
        if isinstance(value, dict):
            for child_key, child in value.items():
                walk(child, str(child_key))
        elif isinstance(value, list):
            for child in value:
                walk(child, key)
        elif isinstance(value, str) and key in fields and value.strip():
            paths.append(value.strip())
    walk(rows)
    return list(dict.fromkeys(paths))
