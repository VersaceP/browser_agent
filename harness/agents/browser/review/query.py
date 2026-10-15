"""Bounded, source-addressable queries over already authorized evidence.

This module only projects facts. It does not decide which facts establish an
outcome, or whether a historical record applies to a later operation.
"""
from __future__ import annotations

import json
import re
from typing import Any


_MAX_NODES = 100_000
_MAX_DEPTH = 14
_MAX_TEXT = 4_000_000
_MAX_ENCODED_JSON = 2_000_000
_PROVENANCE_KEYS = frozenset({
    "pageId", "documentEpoch", "version", "capturedAt", "checkedAt",
    "freshness", "pendingChanges", "requestId", "url", "method", "status",
    "timestamp", "durationMs", "mimeType", "responseBodyAvailable",
    "responseBodyBase64Encoded", "collectionStartedAt", "oldestAvailableAt",
    "evictedCount", "pendingCount",
})
_RECORD_KEYS = frozenset({
    "pageId", "documentEpoch", "capturedAt", "checkedAt", "requestId",
    "timestamp", "collectionStartedAt",
})


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


def _child_path(path: str, key: Any) -> str:
    return path + "/" + str(key).replace("~", "~0").replace("/", "~1")


class EvidenceQuery:
    def __init__(self, root: Any):
        self.root = root
        self.decoded: dict[str, Any] = {}
        self.records: dict[str, dict] = {}
        self.truncated = False

    def decode(self, value: Any, path: str) -> Any:
        if not isinstance(value, str) or not value.lstrip().startswith(("{", "[")):
            return None
        if len(value) > _MAX_ENCODED_JSON:
            self.truncated = True
            return None
        if path not in self.decoded:
            try:
                self.decoded[path] = json.loads(value)
            except ValueError:
                self.decoded[path] = None
        return self.decoded[path]

    def resolve(self, pointer: str) -> tuple[Any, list[tuple[str, dict]]]:
        """Resolve returned pointers, including an explicit encoded-JSON hop."""
        value, path, ancestors = self.root, "", []
        if not pointer:
            return value, ancestors
        for token in pointer[1:].split("/"):
            if re.search(r"~(?![01])", token):
                raise ValueError("Invalid JSON pointer escape")
            token = token.replace("~1", "/").replace("~0", "~")
            if isinstance(value, str) and token == "$json":
                decoded = self.decode(value, path)
                if decoded is None:
                    raise ValueError("This value is not an available JSON object/array")
                value = decoded
            elif isinstance(value, dict):
                ancestors.append((path, value))
                value = value[token]
            elif isinstance(value, list):
                if not re.fullmatch(r"0|[1-9][0-9]*", token):
                    raise ValueError("Invalid JSON array index")
                value = value[int(token)]
            else:
                raise ValueError("JSON pointer cannot traverse a scalar")
            path = _child_path(path, token)
        return value, ancestors

    def context(self, ancestors: list[tuple[str, dict]]) -> list[str]:
        """Attach scalar siblings and the nearest protocol identity/time record.

        Both are literal source values. Record metadata is bounded and deduped
        across hits; omitted metadata never implies a field was absent.
        """
        chosen = []
        for path, obj in reversed(ancestors):
            if not chosen or _RECORD_KEYS.intersection(obj):
                fields = {key: value for key, value in obj.items()
                          if isinstance(value, (str, int, float, bool)) or value is None}
                if fields:
                    priority = [key for key in fields if key in _PROVENANCE_KEYS]
                    names = priority + [key for key in fields if key not in _PROVENANCE_KEYS]
                    visible = {}
                    for key in names[:16]:
                        value = fields[key]
                        # Long bodies stay accessible by path, not in metadata.
                        if len(str(key)) <= 200 and (not isinstance(value, str) or len(value) <= 256):
                            visible[key] = value
                    if visible:
                        self.records[path] = {"path": path, "fields": visible,
                                              "fieldsTruncated": len(visible) != len(fields)}
                        chosen.append(path)
            if chosen and _RECORD_KEYS.intersection(obj):
                break
        return chosen

    def walk(self, root: Any, path: str, ancestors: list[tuple[str, dict]]):
        stack = [(root, path, ancestors, 0, False)]
        seen = 0
        while stack and seen < _MAX_NODES:
            value, path, ancestors, depth, object_field = stack.pop()
            seen += 1
            if depth > _MAX_DEPTH:
                self.truncated = True
                continue
            yield value, path, ancestors, object_field
            decoded = self.decode(value, path)
            if decoded is not None:
                stack.append((decoded, path + "/$json", ancestors, depth + 1, False))
            elif isinstance(value, dict):
                parents = ancestors + [(path, value)]
                for name, child in reversed(list(value.items())):
                    stack.append((child, _child_path(path, name), parents, depth + 1, True))
            elif isinstance(value, list):
                for index in range(len(value) - 1, -1, -1):
                    stack.append((value[index], _child_path(path, index), ancestors, depth + 1, False))
        self.truncated |= bool(stack)

    def query(self, *, scope: str, patterns: list[str], keys: list[str], paths: list[str],
              offset: int, limit: int, key_match: str, pattern_mode: str,
              raw_text: str, ax_text: str | None = None, ax_path: str = "") -> dict:
        root, ancestors = self.resolve(scope)
        regexes = [(term, re.compile(term, re.IGNORECASE)) for term in patterns]
        regex_hits = {term: [] for term in patterns}
        key_hits = {term: [] for term in keys}
        counts = {("regex", term): 0 for term in patterns}
        counts.update({("jsonKey", term): 0 for term in keys})

        def collect(kind, term, hit, parents):
            counter = (kind, term)
            index = counts[counter]
            counts[counter] += 1
            if offset <= index < offset + limit:
                context = self.context(parents)
                if context:
                    hit["contextPaths"] = context
                (regex_hits if kind == "regex" else key_hits)[term].append(hit)

        def search(text, path, parents, encoding):
            if len(text) > _MAX_TEXT:
                self.truncated = True
            for term, regex in regexes:
                if counts[("regex", term)] > offset + limit:
                    continue
                for match in regex.finditer(text[:_MAX_TEXT]):
                    start = max(0, match.start() - 120)
                    end = min(len(text), start + 1000, max(start + 400, match.end() + 120))
                    collect("regex", term, {"path": path, "encoding": encoding,
                        "charOffset": match.start(), "matchLength": match.end() - match.start(),
                        "snippet": text[start:end], "snippetStart": start,
                        "snippetTruncated": end < match.end() or end < min(len(text), match.end() + 120)}, parents)
                    if counts[("regex", term)] > offset + limit:
                        break

        if patterns and (ax_text is not None or pattern_mode == "raw"):
            search(ax_text if ax_text is not None else raw_text,
                   ax_path if ax_text is not None else scope,
                   ancestors + ([(scope, root)] if isinstance(root, dict) else []),
                   "text" if ax_text is not None else "raw_json")
        nodes = (self.walk(root, scope, ancestors)
                 if keys or patterns and ax_text is None and pattern_mode == "decoded" else ())
        for value, path, parents, object_field in nodes:
            if isinstance(value, dict):
                current = parents + [(path, value)]
                for name, child in value.items():
                    for term in keys:
                        matched = (term == name if key_match == "exact"
                                   else term.casefold() in str(name).casefold())
                        if matched and counts[("jsonKey", term)] <= offset + limit:
                            preview = _json(child)
                            collect("jsonKey", term, {"path": _child_path(path, name),
                                "value": preview[:3000], "valueTruncated": len(preview) > 3000}, current)
                if patterns and ax_text is None and pattern_mode == "decoded":
                    # Search one decoded scalar-field document per object. JSON
                    # bodies are searched after decoding, not again as escapes.
                    scalars = {name: child for name, child in value.items()
                               if not isinstance(child, (dict, list))
                               and self.decode(child, _child_path(path, name)) is None}
                    if scalars:
                        search(_json(scalars), path, current, "json_scalar_fields")
            elif (patterns and ax_text is None and pattern_mode == "decoded"
                  and not isinstance(value, list) and not object_field
                  and self.decode(value, path) is None):
                # Scalar array entries and a scalar scope have no enclosing
                # object field document. Their text is searchable directly.
                search(value if isinstance(value, str) else _json(value), path, parents, "text")

        results = []
        for kind, terms, hits in (("regex", patterns, regex_hits), ("jsonKey", keys, key_hits)):
            for term in terms:
                results.append({"type": kind, "query": term, "matches": hits[term],
                    "nextOffset": offset + limit if counts[(kind, term)] > offset + limit else None})
        for pointer in paths:
            result = {"type": "jsonPath", "query": pointer, "matches": [], "nextOffset": None}
            try:
                value, parents = self.resolve(pointer)
            except (KeyError, IndexError, TypeError, ValueError):
                result["unavailableReason"] = "observation_path_not_found"
            else:
                preview = _json(value)
                hit = {"path": pointer, "value": preview[:3000], "valueTruncated": len(preview) > 3000}
                context = self.context(parents + ([(pointer, value)] if isinstance(value, dict) else []))
                if context:
                    hit["contextPaths"] = context
                result["matches"].append(hit)
            results.append(result)
        return {"results": results, "records": list(self.records.values()),
                "jsonCoverageTruncated": self.truncated}
