"""
harness.semantic_frames - Frame-graph shape of DOM.getSemanticTree results.

DOM.getSemanticTree returns a bounded FRAME GRAPH rather than a single tree:
``{rootFrameId, frames[], summary}``. Every frame carries its own local
``tree``, ``nodeCount``, ``status`` and truncation reason, and each accessible
iframe document appears as a sibling frame. ``rootFrameId`` names the anchored
frame — the one owning the ``id``/``selector`` the request was scoped to, or
the main document when it was not scoped.

Two facts drive every consumer here:

  * **A frame-local CSS selector is not usable outside its frame.** ABCP
    resolves selectors in the main document plus its author shadow trees, never
    across a frame boundary, and there is no frame-switch action. A selector
    mined from an iframe's tree would silently match nothing — or the wrong
    node — when handed back to a DOM read. So consumers work from the ROOT
    frame and merely COUNT the rest; merging every frame into one selector
    namespace would manufacture targets that cannot be acted on.
  * **The node count moved.** ``summary.nodeCount`` is the graph-wide total,
    while DOM.getAXTree still reports its count at the top level of ``data``.
    A reader that knows only one of the two shapes scores a perfectly good
    structure read as empty, which is the failure mode this module exists to
    prevent.

Shape validation is fail-closed: a response missing ``frames`` or naming a
``rootFrameId`` that no frame carries is treated as no graph at all, never as
an empty one.
"""

from __future__ import annotations

from typing import Any, List, Optional

from harness.utils import JsonDict


# A frame is reported as one of these; only `unavailable` guarantees no tree.
FRAME_STATUSES = frozenset({"complete", "truncated", "unavailable"})


def frame_graph(data: Any) -> Optional[JsonDict]:
    """The frame graph carried by a getSemanticTree ``response.data``.

    Returns None for any other envelope, so a caller can use this as the test
    for "is this a frame graph at all" without a second method check.
    """
    if not isinstance(data, dict):
        return None
    if not isinstance(data.get("frames"), list):
        return None
    return data


def frames(data: Any) -> List[JsonDict]:
    """Every well-formed frame in the graph, in platform order."""
    graph = frame_graph(data)
    if graph is None:
        return []
    return [frame for frame in graph["frames"] if isinstance(frame, dict)]


def root_frame(data: Any) -> Optional[JsonDict]:
    """The anchored frame named by ``rootFrameId``.

    No fallback to "the first frame with a tree": the platform guarantees the
    root id resolves, so a graph where it does not is malformed, and guessing
    a different frame would hand the caller a tree from a document it never
    asked about.
    """
    graph = frame_graph(data)
    if graph is None:
        return None
    root_id = str(graph.get("rootFrameId") or "").strip()
    if not root_id:
        return None
    for frame in frames(data):
        if str(frame.get("frameId") or "").strip() == root_id:
            return frame
    return None


def root_tree(data: Any) -> Optional[JsonDict]:
    """Local DOM tree of the anchored frame, or None when it carries none.

    ``status: "unavailable"`` frames always carry ``tree: null`` plus an error;
    they are a real answer ("that document could not be read"), not a tree.
    """
    frame = root_frame(data)
    if frame is None:
        return None
    tree = frame.get("tree")
    return tree if isinstance(tree, dict) else None


def graph_digest(data: Any) -> JsonDict:
    """Compact, log-sized account of what the graph contained.

    Records the frames a consumer did NOT read. Without it, "one root frame
    was digested" and "one root frame was digested while four iframes were
    skipped" leave identical traces, and the second one is the case where a
    later empty result deserves a different explanation.
    """
    graph = frame_graph(data)
    if graph is None:
        return {}
    summary = graph.get("summary")
    summary = summary if isinstance(summary, dict) else {}
    all_frames = frames(data)
    root_id = str(graph.get("rootFrameId") or "").strip()
    unavailable = [
        {
            "frameId": str(frame.get("frameId") or ""),
            "code": str((frame.get("error") or {}).get("code") or "")
            if isinstance(frame.get("error"), dict)
            else "",
        }
        for frame in all_frames
        if str(frame.get("status") or "") == "unavailable"
    ]
    return {
        "rootFrameId": root_id or None,
        "frameCount": len(all_frames),
        "otherFrameCount": max(0, len(all_frames) - 1),
        "nodeCount": summary.get("nodeCount"),
        "truncated": bool(summary.get("truncated")),
        "unavailableFrames": unavailable[:8],
    }


def response_node_count(data: Any) -> Optional[int]:
    """Node count of a structure read, wherever its envelope puts it.

    DOM.getAXTree reports ``data.nodeCount``; DOM.getSemanticTree reports the
    graph-wide total at ``data.summary.nodeCount``. None means "this envelope
    states no count" and must stay distinct from 0, which is a page that
    genuinely yielded no nodes.
    """
    if not isinstance(data, dict):
        return None
    for candidate in (data.get("nodeCount"), _summary_node_count(data)):
        if isinstance(candidate, bool) or not isinstance(candidate, int):
            continue
        return max(0, candidate)
    return None


def _summary_node_count(data: JsonDict) -> Any:
    summary = data.get("summary")
    return summary.get("nodeCount") if isinstance(summary, dict) else None
