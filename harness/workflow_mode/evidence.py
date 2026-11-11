"""Receipt addressing shared by tools and objective acceptance checks."""
from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import unquote


_RECEIPT_URI = re.compile(r"^sqlite://tasks/[^/]+/resources/([0-9a-f]{32})$")


def short_reference(reference: str) -> str | None:
    """Keep the complete resource identity while omitting the repeated task URI."""
    match = _RECEIPT_URI.fullmatch(reference) if isinstance(reference, str) else None
    return "r:" + match[1] if match else None


def resolve_reference(reference: str, known_refs: set[str]) -> str:
    """Short references resolve only through this task's exact owned receipt set."""
    if not isinstance(reference, str) or not reference.startswith("r:"):
        return reference
    if not re.fullmatch(r"r:[0-9a-f]{32}", reference):
        raise ValueError("Invalid short receipt reference")
    matches = [ref for ref in known_refs if short_reference(ref) == reference]
    if len(matches) != 1:
        raise PermissionError("Short reference is not a receipt of this task")
    return matches[0]


def evidence_locations(ref: str, value: Any, paths: dict[str, str]) -> dict[str, dict[str, str]]:
    """Advertise only addresses that exist in the saved receipt, not its projection."""
    locations = {}
    for name, path in paths.items():
        try:
            pointer(value, path)
        except ValueError:
            continue
        locations[name] = {"ref": ref, "pointer": path}
    return locations


def pointer(value: Any, path: str) -> Any:
    """RFC 6901 JSON pointer, read-only."""
    validate_pointer(path)
    if not path:
        return value
    walked = ""
    for raw in path[1:].split("/"):
        key = raw.replace("~1", "/").replace("~0", "~")
        if isinstance(value, list):
            if not re.fullmatch(r"0|[1-9][0-9]*", key) or int(key) >= len(value):
                raise ValueError(f"pointer {walked or '/'} is a list of {len(value)}; {key!r} is not an index in it")
            value = value[int(key)]
        elif isinstance(value, dict):
            if key not in value:
                # Name the keys that exist so the next read can be addressed directly.
                raise ValueError(f"pointer {walked or '/'} has no {key!r}; keys: {sorted(value)[:30]}")
            value = value[key]
        else:
            raise ValueError(f"pointer segment {key!r} does not address a container")
        walked += "/" + raw
    return value


def validate_pointer(path: Any) -> str:
    """Validate pointer syntax without interpreting a target or URI-encoding it."""
    if not isinstance(path, str):
        raise ValueError("Evidence pointer must be a string")
    if path and not path.startswith("/"):
        raise ValueError("Evidence pointer must start with /, or be empty for the root")
    if re.search(r"~(?![01])", path):
        raise ValueError("Evidence pointer has an invalid ~ escape; use ~0 or ~1")
    return path


def compose_pointer(scope: str, relative: str) -> str:
    """Append explicit RFC 6901 tokens to a selected scope, without fallback."""
    return validate_pointer(scope) + validate_pointer(relative)


def evidence_reference(value: Any, path: str | None = None, *,
                       known_refs: set[str] | None = None) -> tuple[str, str]:
    """Resolve a receipt plus RFC 6901 pointer without changing its ownership.

    Structured references are preferred. Existing plain references and URI
    fragments remain readable so saved judgments do not need migration.
    """
    if isinstance(value, dict):
        if set(value) - {"ref", "pointer"}:
            raise ValueError("Evidence reference accepts only ref and pointer")
        reference, selected = value.get("ref"), value.get("pointer", "")
    else:
        reference, selected = value, ""
    if not isinstance(reference, str) or not reference:
        raise ValueError("Evidence reference needs a nonempty ref")
    if not isinstance(selected, str):
        raise ValueError("Evidence pointer must be a string")
    if "#" in reference:
        reference, fragment = reference.split("#", 1)
        fragment = unquote(fragment)
        if selected and selected != fragment:
            raise ValueError("Conflicting evidence pointers")
        selected = fragment
    if path is not None:
        if selected and selected != path:
            raise ValueError("Conflicting evidence pointers")
        selected = path
    validate_pointer(selected)
    if known_refs is not None:
        reference = resolve_reference(reference, known_refs)
    return reference, selected


def receipt_selection(arguments: dict[str, Any], *, known_refs: set[str]
                      ) -> tuple[str, str, list[str] | None]:
    """Resolve explicit scoped reads into owned, receipt-root addresses.

    Package summaries contain output-relative pointers. A caller can pass the
    returned ``summaryEvidenceScope`` as ``ref`` and supply ``relative_pointer``
    or ``relative_pointers``. Plain ``pointer``/``pointers`` retain their original
    receipt-root meaning. This never infers a base from a failed lookup, a field
    name or business output text.
    """
    relative_keys = {key for key in ("relative_pointer", "relative_pointers") if key in arguments}
    absolute_keys = {key for key in ("pointer", "pointers") if key in arguments}
    if len(relative_keys) > 1 or len(absolute_keys) > 1 or (relative_keys and absolute_keys):
        raise ValueError("Use one of pointer, pointers, relative_pointer or relative_pointers")
    if relative_keys and (not isinstance(arguments["ref"], dict)
                          or "pointer" not in arguments["ref"]):
        raise ValueError("Relative reads need an explicit owned scope: ref={ref, pointer}")
    reference, selected = evidence_reference(arguments["ref"], arguments.get("pointer"),
                                             known_refs=known_refs)
    if reference not in known_refs:
        raise PermissionError("Not a receipt of this task")
    if "relative_pointer" in arguments:
        return reference, compose_pointer(selected, arguments["relative_pointer"]), None
    batch_key = "relative_pointers" if relative_keys else "pointers"
    if batch_key not in arguments:
        return reference, selected, None
    paths = arguments[batch_key]
    if not isinstance(paths, list) or not 1 <= len(paths) <= 20:
        raise ValueError(f"{batch_key} needs 1–20 JSON pointers")
    if batch_key == "relative_pointers":
        return reference, "", [compose_pointer(selected, path) for path in paths]
    if selected:
        raise ValueError("pointers are receipt-root addresses; do not combine them with a selected scope")
    return reference, "", [validate_pointer(path) for path in paths]


def child_pointer(path: str, key: Any) -> str:
    return path + "/" + str(key).replace("~", "~0").replace("/", "~1")


def value_index(value: Any, path: str = "") -> dict[str, Any]:
    """A navigable index, not an interpretation of evidence."""
    if isinstance(value, dict):
        return {"type": "object", "keys": list(value), "pointer": path}
    if isinstance(value, list):
        return {"type": "array", "length": len(value), "pointer": path}
    if isinstance(value, str):
        return {"type": "string", "totalChars": len(value), "pointer": path}
    return {"type": type(value).__name__, "pointer": path}


def read_evidence(value: Any, path: str, *, max_chars: int = 20000,
                  offset: int | None = None, limit: int | None = None,
                  search: list[str] | None = None) -> dict[str, Any]:
    """Page saved values or find literal text, retaining exact source addresses.

    Offsets count characters for strings, entries for containers, and matching
    leaves for search. Search coverage is only this saved value, never the live page.
    """
    size = max(512, min(int(max_chars), 60000))
    start = 0 if offset is None else int(offset)
    count = 100 if limit is None else int(limit)
    if start < 0 or not 1 <= count <= 200:
        raise ValueError("offset must be >= 0; limit must be between 1 and 200")

    def bounded(item: Any, address: str, budget: int) -> dict[str, Any]:
        if len(json.dumps(item, ensure_ascii=False)) <= budget:
            return {"value": item}
        return {"truncated": True, **value_index(item, address),
                "next": "Read this pointer with offset/limit, or search its saved values."}

    if search is not None:
        if (not isinstance(search, list) or not 1 <= len(search) <= 20
                or any(not isinstance(s, str) or not s or len(s) > 512 for s in search)):
            raise ValueError("search needs 1–20 nonempty literal strings, each <= 512 characters")
        patterns = [re.compile(re.escape(s), re.IGNORECASE) for s in search]
        stack, hits = [(path, value)], []
        examined = 0
        while stack:
            address, item = stack.pop()
            if isinstance(item, dict):
                stack.extend((child_pointer(address, k), v) for k, v in reversed(list(item.items())))
            elif isinstance(item, list):
                stack.extend((child_pointer(address, i), item[i]) for i in range(len(item) - 1, -1, -1))
            else:
                examined += 1
                text = item if isinstance(item, str) else json.dumps(item, ensure_ascii=False)
                matches = [pattern.search(text) for pattern in patterns]
                found = [m for m in matches if m is not None]
                if not found:
                    continue
                at = min(m.start() for m in found)
                lo, hi = max(0, at - 120), min(len(text), at + 360)
                hits.append({"pointer": address, "matchingTerms": [
                    term for term, match in zip(search, matches) if match is not None],
                    "matchOffset": at, "excerpt": text[lo:hi], "excerptOffset": lo,
                    "totalChars": len(text), "excerptTruncated": lo > 0 or hi < len(text)})
        rows, used = [], 0
        for hit in hits[start:start + count]:
            length = len(json.dumps(hit, ensure_ascii=False))
            if rows and used + length > size:
                break
            rows.append(hit)
            used += length
        end = start + len(rows)
        return {"mode": "search", "search": search, "matching": "case-insensitive literal OR in scalar values",
                "scopePointer": path, "coverage": "selected saved value only",
                "examinedLeaves": examined, "totalMatches": len(hits), "matches": rows,
                "offset": start, "nextOffset": end if end < len(hits) else None}

    if isinstance(value, str) and (offset is not None or len(value) > size):
        return {"totalChars": len(value), "offset": start, "text": value[start:start + size],
                "nextOffset": start + size if start + size < len(value) else None}
    if isinstance(value, (list, dict)) and (offset is not None or limit is not None
                                           or len(json.dumps(value, ensure_ascii=False)) > size):
        rows, used = [], 0
        keys = list(value) if isinstance(value, dict) else range(len(value))
        for index in range(start, min(len(value), start + count)):
            key = keys[index]
            address = child_pointer(path, key)
            row = {"index": index, "pointer": address,
                   **({"key": key} if isinstance(value, dict) else {}),
                   **bounded(value[key], address, size)}
            length = len(json.dumps(row, ensure_ascii=False))
            if rows and used + length > size:
                break
            rows.append(row)
            used += length
        end = start + len(rows)
        return {"mode": "object" if isinstance(value, dict) else "array",
                "length": len(value), "offset": start, "items": rows,
                "nextOffset": end if end < len(value) else None,
                "hasTruncatedItems": any(row.get("truncated") for row in rows)}
    if offset is not None or limit is not None:
        raise ValueError("offset/limit address text, container entries or search matches, not a scalar")
    return bounded(value, path, size)

