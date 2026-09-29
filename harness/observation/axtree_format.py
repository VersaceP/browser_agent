"""The page-observation node-line format: one parser, shared by every reader.

`DOM.getAXTree` publishes an immutable text artifact (WebCross unified page
observation, schemaVersion 2). Each node is one line, written by the
platform's `formatNodeLine`::

    depth [n_<16 hex>] role "name" description="…" ariaValueText="…"
        [flag,flag,ev{a|b}] @x,y,w,h text="…" vis=↓|∅ state{k=v}
        scroll{k=v} attrs{k="v"} rel{name:[id,null]} component=c_… truncated{"f"}

Only `depth`, the id and the role are always present, and the optional parts
always appear in that order. Every free-text value (name, description,
ariaValueText, text) is a JSON string, so a label containing quotes, brackets
or `@` can no longer be mistaken for the grammar around it.

This module exists because the format used to be parsed in several places and
each copy drifted differently when the panel changed it. Keep the format
knowledge here, and a future change is caught in one place.

Node ids are opaque and carry no frame: frame membership comes from the
artifact's `frame [f_…] root=[n_…]` records (see page_observation).
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional, Tuple

from harness.utils import JsonDict


# The platform issues 16 hex digits today; its design document specifies 32.
# Accept exactly one of the two so neither a rollout nor a truncated token
# slips past, and never the retired `frame:ax:dom` form.
AX_NODE_ID_PATTERN = r"n_[0-9a-f]{16}(?:[0-9a-f]{16})?"
AX_FRAME_ID_PATTERN = r"f_[0-9a-f]{32}"
AX_NODE_ID_RE = re.compile(rf"^{AX_NODE_ID_PATTERN}$")
AX_NODE_ID_TOKEN_RE = re.compile(rf"\[({AX_NODE_ID_PATTERN})\]")
AX_NODE_ID_ANYWHERE_RE = re.compile(rf"(?<![0-9a-z_]){AX_NODE_ID_PATTERN}(?![0-9a-z_])")

# Head of a node line. The name, when present, is a JSON string, so the
# pattern for it is the JSON string grammar rather than "up to the last quote".
AXTREE_LINE_RE = re.compile(
    rf'^(?P<depth>\d+) \[(?P<id>{AX_NODE_ID_PATTERN})\] (?P<role>[^\s"\[]+)'
    r'(?: (?P<name>"(?:[^"\\]|\\.)*"))?(?P<rest>.*)$'
)
AXTREE_RECT_RE = re.compile(
    r"@(-?\d+(?:\.\d+)?),(-?\d+(?:\.\d+)?),(\d+(?:\.\d+)?),(\d+(?:\.\d+)?)"
)

# State the platform writes into the flag group, translated to the vocabulary
# the harness has always reasoned in. Positive and negative forms stay
# explicit: `unchecked` is a positive report, which is NOT the same as the
# flag being absent (absent means the state does not apply to the node).
AXTREE_STATE_FLAGS = frozenset({
    "checked", "unchecked", "mixed",
    "selected", "unselected",
    "expanded", "collapsed",
    "disabled", "focused", "required", "invalid", "valueRedacted",
})

# Layout evidence, SPARSE by contract: a missing flag does not prove the
# negative. `hidden` is the platform's not-rendered marker (vis=∅), `off` its
# out-of-view marker (vis=↓, offscreen or clipped), `scroll` a node that is
# actually scrollable.
AXTREE_LAYOUT_FLAGS = frozenset({"hidden", "off", "scroll"})

# Target evidence. `targetable` means the platform can locate the node;
# `actionable` and `candidate` are its interaction verdicts. None of them
# proves a click would land - the platform says so itself.
AXTREE_TARGET_FLAGS = frozenset({"targetable", "actionable", "candidate", "ignored"})

AXTREE_KNOWN_FLAGS = AXTREE_STATE_FLAGS | AXTREE_LAYOUT_FLAGS | AXTREE_TARGET_FLAGS

_FLAG_TRANSLATION = {
    "checked": "checked",
    "checked=false": "unchecked",
    "checked=mixed": "mixed",
    "selected": "selected",
    "selected=false": "unselected",
    "expanded": "expanded",
    "expanded=false": "collapsed",
}
_VISIBILITY = {"↓": "out-of-view", "∅": "not-rendered"}
_KEY_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_-]*")
_JSON = json.JSONDecoder()


def _skip_spaces(text: str, index: int) -> int:
    while index < len(text) and text[index] == " ":
        index += 1
    return index


def _read_json(text: str, index: int) -> Tuple[Any, int]:
    value, end = _JSON.raw_decode(text, index)
    return value, end


def _read_braced(text: str, index: int) -> Tuple[str, int]:
    """Return the `{…}` block starting at `index` (inclusive), string-aware."""
    if index >= len(text) or text[index] != "{":
        raise ValueError("expected {")
    depth = 0
    position = index
    while position < len(text):
        char = text[position]
        if char == '"':
            _value, position = _read_json(text, position)
            continue
        if char in "{[":
            depth += 1
        elif char in "}]":
            depth -= 1
            if depth == 0:
                return text[index:position + 1], position + 1
        position += 1
    raise ValueError("unterminated {")


def parse_compact_object(block: str) -> Any:
    """Parse the platform's compact `{k=v,…}` / `{name:[ids]}` / `{"a","b"}`.

    Keys are bare identifiers or JSON strings; values are JSON (numbers,
    strings, booleans, objects) or, for relations, a `[id,null]` list of bare
    ids. A block whose items have no `=`/`:` is a list (truncated fields).
    """
    inner = block[1:-1]
    result: Dict[str, Any] = {}
    items: List[Any] = []
    index = 0
    while index < len(inner):
        index = _skip_spaces(inner, index)
        if index >= len(inner):
            break
        if inner[index] == '"':
            key, index = _read_json(inner, index)
        else:
            match = _KEY_RE.match(inner, index)
            if match is None:
                raise ValueError(f"bad key at {index}")
            key, index = match.group(0), match.end()
        if index >= len(inner) or inner[index] == ",":
            items.append(key)
            index += 1
            continue
        separator = inner[index]
        index += 1
        if separator == ":" and index < len(inner) and inner[index] == "[":
            end = inner.index("]", index)
            result[str(key)] = [
                None if token == "null" else token
                for token in (part.strip() for part in inner[index + 1:end].split(","))
                if token
            ]
            index = end + 1
        elif separator in "=:":
            value, index = _read_json(inner, index)
            result[str(key)] = value
        else:
            raise ValueError(f"bad separator {separator!r}")
        index = _skip_spaces(inner, index)
        if index < len(inner) and inner[index] == ",":
            index += 1
    if items and not result:
        return items
    return result


def _translate_flags(raw_flags: List[str]) -> Tuple[List[str], List[str]]:
    flags: List[str] = []
    sources: List[str] = []
    for raw in raw_flags:
        token = raw.strip()
        if not token:
            continue
        if token.startswith("ev{") and token.endswith("}"):
            sources.extend(part for part in token[3:-1].split("|") if part)
            continue
        translated = _FLAG_TRANSLATION.get(token, token)
        if translated in AXTREE_KNOWN_FLAGS and translated not in flags:
            flags.append(translated)
    return flags, sources


def _parse_tail(rest: str, node: JsonDict) -> None:
    index = 0
    while True:
        index = _skip_spaces(rest, index)
        if index >= len(rest):
            return
        for key in ("description", "ariaValueText", "text"):
            prefix = key + "="
            if rest.startswith(prefix, index) and rest[index + len(prefix): index + len(prefix) + 1] == '"':
                node[key], index = _read_json(rest, index + len(prefix))
                break
        else:
            char = rest[index]
            if char == "[":
                end = rest.index("]", index)
                flags, sources = _translate_flags(rest[index + 1:end].split(","))
                node["flags"].extend(flag for flag in flags if flag not in node["flags"])
                node["interactionSources"] = sources
                index = end + 1
                continue
            if char == "@":
                match = AXTREE_RECT_RE.match(rest, index)
                if match is None:
                    raise ValueError("bad rect")
                x, y, w, h = (float(group) for group in match.groups())
                node["rect"] = {
                    key: int(value) if value.is_integer() else value
                    for key, value in (("x", x), ("y", y), ("w", w), ("h", h))
                }
                index = match.end()
                continue
            if rest.startswith("vis=", index):
                token_end = rest.find(" ", index)
                token_end = len(rest) if token_end < 0 else token_end
                node["visibility"] = _VISIBILITY.get(rest[index + 4:token_end], rest[index + 4:token_end])
                index = token_end
                continue
            if rest.startswith("component=", index):
                token_end = rest.find(" ", index)
                token_end = len(rest) if token_end < 0 else token_end
                node["component"] = rest[index + len("component="):token_end]
                index = token_end
                continue
            for key in ("state", "scroll", "attrs", "rel", "truncated"):
                if rest.startswith(key + "{", index):
                    block, index = _read_braced(rest, index + len(key))
                    node[key] = parse_compact_object(block)
                    break
            else:
                # An unknown field added by a future platform build must not
                # take the rest of the line down with it: skip one token.
                token_end = rest.find(" ", index)
                index = len(rest) if token_end < 0 else token_end


def parse_axtree_line(line: str) -> Optional[JsonDict]:
    """Parse one node line, or None if it is not one.

    Returns the long-standing reader keys ``{depth, id, role, name, flags,
    rect, marker}`` - `role` casefolded, `marker` is ``"#"`` for an actionable node, ``"~"`` for
    a candidate, else ``""`` - plus the richer fields the platform now
    publishes: ``visibility`` (visible/out-of-view/not-rendered), ``state``,
    ``attrs``, ``text``, ``description``, ``ariaValueText``, ``scroll``,
    ``rel``, ``component``, ``truncated`` and ``interactionSources``.

    A malformed tail keeps the head (id, role, name) and whatever parsed
    before the fault, flagged with ``tailError``, rather than dropping a
    node the platform did publish.
    """
    head = AXTREE_LINE_RE.match(line if isinstance(line, str) else "")
    if not head:
        return None
    name_token = head.group("name")
    node: JsonDict = {
        "depth": int(head.group("depth")),
        "id": head.group("id"),
        # Casefolded: the platform now writes Chromium roles in camelCase
        # (`rootWebArea`, `checkBox`), every reader compares them lowercase.
        "role": head.group("role").casefold(),
        "name": json.loads(name_token) if name_token else "",
        "flags": [],
        "rect": None,
        "marker": "",
        "visibility": "visible",
        "interactionSources": [],
    }
    try:
        _parse_tail(str(head.group("rest") or ""), node)
    except (ValueError, json.JSONDecodeError) as exc:
        node["tailError"] = str(exc)[:120]
    if node["visibility"] == "not-rendered":
        node["flags"].append("hidden")
    elif node["visibility"] == "out-of-view":
        node["flags"].append("off")
    if isinstance(node.get("scroll"), dict):
        node["flags"].append("scroll")
    if "actionable" in node["flags"]:
        node["marker"] = "#"
    elif "candidate" in node["flags"]:
        node["marker"] = "~"
    return node


def axtree_flags_and_rect(rest: str) -> Tuple[List[str], Optional[JsonDict]]:
    """Flags and rect from the tail AFTER a line's name.

    Prefer `parse_axtree_line`; this exists for callers that hold only a tail.
    """
    node: JsonDict = {"flags": [], "rect": None, "visibility": "visible"}
    try:
        _parse_tail(str(rest or ""), node)
    except (ValueError, json.JSONDecodeError):
        pass
    if node["visibility"] == "not-rendered":
        node["flags"].append("hidden")
    elif node["visibility"] == "out-of-view":
        node["flags"].append("off")
    if isinstance(node.get("scroll"), dict):
        node["flags"].append("scroll")
    return node["flags"], node["rect"]


def axtree_layout_flags(flags: List[str]) -> List[str]:
    """The layout subset of a parsed flag list."""
    return [flag for flag in flags if flag in AXTREE_LAYOUT_FLAGS]


def axtree_state_flags(flags: List[str]) -> List[str]:
    """The generic-AX-state subset of a parsed flag list."""
    return [flag for flag in flags if flag in AXTREE_STATE_FLAGS]
