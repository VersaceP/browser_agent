"""The AXTree compact-line format: one parser, shared by every reader.

`DOM.getAXTree` renders each node as::

    depth [frame:ax:dom] role "label" [flags...] #|~ @x,y,w,h (+N omitted)

Only `depth`, the canonical id and the role are always present. The flag group,
the target marker, the rect and the omitted-children suffix are all optional.

This module exists because the format was parsed in two places. When the panel
moved every flag into a single bracket group, both copies broke - and they broke
differently: the AX cache silently reported no flags at all, while the auth
verifier failed OPEN and began accepting `[hidden]` nodes as proof of a live
session. Neither had a test that used a real line, so both stayed green. Keep
the format knowledge here, and a future change is caught in one place.
"""

from __future__ import annotations

import re
from typing import Dict, List, Optional, Tuple

from harness.utils import JsonDict


# Generic AX state, exposed only where the state APPLIES to the node. Positive
# and negative forms are both explicit, so `unchecked` is a positive report that
# the node is unchecked - which is NOT the same as the flag being absent, and
# absent means AX never exposed that state for this node at all.
# `enabled`/`disabled` ride on interactive nodes; native inertness is reported
# separately as `inert`; `popup` appears only when true.
AXTREE_STATE_FLAGS = frozenset({
    "checked", "unchecked", "mixed",
    "enabled", "disabled", "inert",
    "selected", "unselected",
    "expanded", "collapsed",
    "multi", "single",
    "popup",
})

# Layout evidence, SPARSE by contract: a missing layout flag does not prove the
# negative. These may never be read as "not hidden" or "not occluded" - only a
# present flag says anything. `zN` encodes stacking order.
AXTREE_LAYOUT_FLAGS = frozenset({
    "hidden", "off", "blocked", "scroll", "sticky", "clip",
})

AXTREE_KNOWN_FLAGS = AXTREE_STATE_FLAGS | AXTREE_LAYOUT_FLAGS

AXTREE_Z_FLAG_RE = re.compile(r"^z-?\d+$")
AXTREE_RECT_RE = re.compile(r"@(-?\d+),(-?\d+),(\d+),(\d+)")

# Current builds emit every flag inside ONE bracket group - `[checked enabled]`,
# `[enabled collapsed single popup]`, `[off]`. Splitting such a tail on
# whitespace yields `[checked` and `enabled]`, and a whitelist of bare words
# matches neither, so the group's first token (always carrying `[`) and its last
# (always carrying `]`) are both lost - which for a two-token group is the whole
# group.
#
# Older builds emitted each STATE in its own group and the layout flags bare,
# all on the same line: `[checked] [disabled] hidden off`. So the tail is a
# SEQUENCE of groups and bare tokens, not one group, and "trust the bracket,
# ignore the rest" drops exactly the layout flags the auth check depends on.
AXTREE_FLAG_GROUP_RE = re.compile(r"\[([^\[\]]*)\]")

# Head of a line: optional depth, optional legacy indent, the canonical id.
# The id's own brackets are the one unambiguous anchor on the left - the
# pattern inside them cannot occur in a role or an accessible name.
AXTREE_HEAD_RE = re.compile(
    r"^(?:(?P<depth>\d+)\s+)?(?P<indent>\s*)\[(?P<id>\d+:-?\d+:-?\d+)\]\s+(?P<body>.*)$"
)
_OMITTED_SUFFIX_RE = re.compile(r"\s*\(\+\d+\s+omitted\)$")
_RECT_SUFFIX_RE = re.compile(r"\s*@(-?\d+),(-?\d+),(\d+),(\d+)$")
_MARKER_SUFFIX_RE = re.compile(r"(?:^|\s)([#~])$")
_TRAILING_GROUP_RE = re.compile(r"\s*\[([^\[\]]*)\]$")
_TRAILING_TOKEN_RE = re.compile(r"\s+(\S+)$")


def axtree_flags_and_rect(rest: str) -> Tuple[List[str], Optional[JsonDict]]:
    """Parse the flag list and the rect out of a line's tail.

    `rest` is the text AFTER the accessible name, so it never contains the
    canonical id's brackets or the name's quotes. Prefer `parse_axtree_line`,
    which does not need the caller to have found the name's end; this exists
    for callers that already hold only a tail.

    Shares `_peel_tail` with the whole-line parser, so both read a mixed
    `[checked] hidden` tail identically. Unknown tokens are ignored rather than
    fatal in either entry point.
    """
    flags, rect, _marker, _remainder = _peel_tail(rest)
    return flags, rect


def _is_flag(token: str) -> bool:
    return token in AXTREE_KNOWN_FLAGS or bool(AXTREE_Z_FLAG_RE.match(token))


def _peel_tail(body: str) -> Tuple[List[str], Optional[JsonDict], str, str]:
    """Strip the fixed grammar off the right of a line body.

    Returns ``(flags, rect, marker, remainder)``, where the remainder is
    whatever the grammar stopped consuming - the role and the accessible name.

    The one parser for the tail, so the whole-line reader and the tail-only
    helper cannot disagree about the same characters. They did: one read
    `[checked] hidden` as both flags and the other as `checked` alone.
    """
    body = _OMITTED_SUFFIX_RE.sub("", str(body or "").rstrip())

    rect: Optional[JsonDict] = None
    rect_match = _RECT_SUFFIX_RE.search(body)
    if rect_match:
        x, y, w, h = (int(group) for group in rect_match.groups())
        rect = {"x": x, "y": y, "w": w, "h": h}
        body = body[: rect_match.start()].rstrip()

    marker = ""
    marker_match = _MARKER_SUFFIX_RE.search(body)
    if marker_match:
        marker = marker_match.group(1)
        body = body[: marker_match.start(1)].rstrip()

    # Flag segments, innermost last. Current builds emit one group; older ones
    # emitted a group per state plus bare layout words, and mixed both on the
    # same line - so this consumes whichever comes next until the name is
    # reached. Ending with `"` means the name ends the line and nothing further
    # can be a flag, which is what stops a label like `"Save [enabled]"` from
    # donating a flag it never had.
    flags: List[str] = []
    while body and not body.endswith('"'):
        group = _TRAILING_GROUP_RE.search(body)
        if group is not None:
            # A group outside the name IS the flag group, so consume it whole
            # and keep the tokens we recognise. An unknown token is ignored,
            # never fatal: a flag added by a future platform build must not
            # take the role, the name and every other flag down with it.
            flags = [t for t in group.group(1).split() if _is_flag(t)] + flags
            body = body[: group.start()].rstrip()
            continue
        token = _TRAILING_TOKEN_RE.search(body)
        # `\s+` in that pattern is the role guard: the first token of the body
        # has no whitespace before it, so a role spelled like a flag is never
        # eaten and the line cannot be left without one.
        if token is None or not _is_flag(token.group(1)):
            break
        flags.insert(0, token.group(1))
        body = body[: token.start()].rstrip()

    return flags, rect, marker, body


def parse_axtree_line(line: str) -> Optional[JsonDict]:
    """Parse one compact AXTree line into its parts, or None if it is not one.

    Returns ``{depth, id, role, name, flags, rect, marker}`` where `marker` is
    ``"#"``, ``"~"`` or ``""``.

    Parsed RIGHT to left, which is what makes an accessible name safe. The name
    is the only free-text field on the line and the formatter does not escape
    it, so a label may legally contain `"`, `[`, `]`, `#` and `@`. Scanning
    left to right for the name's closing quote therefore truncates the name and
    hands the leftovers to the flag parser, which then reads fragments of the
    LABEL as flags. Everything to the right of the name is a fixed grammar, so
    peeling it off from the end never has to guess where the name ended: what
    remains when the grammar stops matching IS the name.

    The stop rule is one line: once the remainder ends with `"`, the name ends
    the line and nothing further can be a flag. That is what keeps a label like
    `"Save [enabled]"` from donating a flag it never had.
    """
    head = AXTREE_HEAD_RE.match(line if isinstance(line, str) else "")
    if not head:
        return None
    flags, rect, marker, body = _peel_tail(str(head.group("body") or ""))

    name = ""
    if body.endswith('"'):
        opening = body.find('"')
        if opening >= 0 and opening < len(body) - 1:
            # Greedy: first quote to last quote, so quotes INSIDE the name are
            # kept rather than ending it.
            name = body[opening + 1: -1]
            body = body[:opening].rstrip()
    role = body.strip()

    depth_prefix = head.group("depth")
    return {
        "depth": (
            int(depth_prefix)
            if depth_prefix is not None
            else len(str(head.group("indent") or "")) // 2
        ),
        "id": head.group("id"),
        "role": role,
        "name": name,
        "flags": flags,
        "rect": rect,
        "marker": marker,
    }


def axtree_layout_flags(flags: List[str]) -> List[str]:
    """The layout subset of a parsed flag list, `zN` included."""
    return [
        flag for flag in flags
        if flag in AXTREE_LAYOUT_FLAGS or AXTREE_Z_FLAG_RE.match(flag)
    ]


def axtree_state_flags(flags: List[str]) -> List[str]:
    """The generic-AX-state subset of a parsed flag list."""
    return [flag for flag in flags if flag in AXTREE_STATE_FLAGS]
