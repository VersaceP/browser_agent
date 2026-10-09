"""Read and verify WebCross page-observation artifacts.

WebCross 0.9.6 returns a full artifact from DOM.getAXTree and a detail artifact
from DOM.getDetails. Detail source=live is a current bounded query;
source=snapshot reads saved values and never refreshes live state. Host paths
are consumed at receipt time and removed from model-visible references.

References, page identity and the artifact's own end hash are verified before
any facts are exposed. A missing or malformed artifact is unavailable evidence,
never an empty page. Versions are opaque identifiers, not ordering evidence.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from harness.observation.axtree_format import (
    AX_FRAME_ID_PATTERN,
    AX_NODE_ID_PATTERN,
    AXTREE_LINE_RE,
    parse_axtree_line,
)
from harness.utils import JsonDict


OBSERVATION_SCHEMA = "abcp.page-observation"
# A single full view of a very large page measured ~150 KB for 400 rows; the
# cap only stops a corrupt reference from pulling an arbitrary file into memory.
MAX_ARTIFACT_BYTES = 64 * 1024 * 1024

_JSON = json.JSONDecoder()
_KEY_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_-]*")
_FRAME_LINE_RE = re.compile(rf"^frame \[(?P<id>{AX_FRAME_ID_PATTERN})\](?P<rest>.*)$")
_FRAME_REF_RE = re.compile(
    rf"\b(?P<key>parent|owner|root)=\[(?P<id>{AX_FRAME_ID_PATTERN}|{AX_NODE_ID_PATTERN})\]"
)
_COMPONENT_LINE_RE = re.compile(r"^component \[(?P<id>c_[0-9a-f]+)\](?P<rest>.*)$")



class ObservationArtifactError(ValueError):
    """An artifact reference could not be turned into trustworthy content."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


# ---------------------------------------------------------------------------
# Control-line fields: `key=value` pairs, values bare tokens or JSON.
# ---------------------------------------------------------------------------

def parse_text_fields(text: str) -> JsonDict:
    """Parse the platform's `key=value key2="…" key3={…}` field list."""
    result: JsonDict = {}
    index = 0
    while index < len(text):
        while index < len(text) and text[index].isspace():
            index += 1
        if index >= len(text):
            break
        match = _KEY_RE.match(text, index)
        if match is None or match.end() >= len(text) or text[match.end()] != "=":
            raise ObservationArtifactError("invalid-field", f"invalid field at {index}")
        key = match.group(0)
        index = match.end() + 1
        if index < len(text) and text[index] in '"{[':
            value, index = _JSON.raw_decode(text, index)
        else:
            end = index
            while end < len(text) and not text[end].isspace():
                end += 1
            token = text[index:end]
            try:
                value = json.loads(token)
            except json.JSONDecodeError:
                value = token
            index = end
        result[key] = value
    return result


# ---------------------------------------------------------------------------
# Artifact I/O
# ---------------------------------------------------------------------------

def read_artifact(
    reference: Any,
    *,
    kind: str,
    page_id: Optional[str],
) -> Tuple[JsonDict, List[str], JsonDict]:
    """Read one artifact reference; return ``(meta, body_lines, end)``.

    `body_lines` excludes the meta and end control lines and blank lines.
    Raises ObservationArtifactError with a stable code on any mismatch.
    """
    if not isinstance(reference, dict) or not isinstance(reference.get("path"), str):
        raise ObservationArtifactError("artifact-reference-invalid", "artifact has no path")
    path = Path(reference["path"])
    if not path.is_absolute():
        raise ObservationArtifactError("artifact-path-invalid", "artifact path is not absolute")
    if page_id and page_id not in path.parts:
        raise ObservationArtifactError(
            "artifact-page-mismatch", "artifact path does not belong to the requested page",
        )
    try:
        size = path.stat().st_size
    except FileNotFoundError as exc:
        raise ObservationArtifactError(
            "artifact-unavailable", "artifact expired or was retired before it was read",
        ) from exc
    except OSError as exc:
        raise ObservationArtifactError("artifact-unreadable", str(exc)[:200]) from exc
    expected_bytes = reference.get("bytes")
    if isinstance(expected_bytes, int) and expected_bytes != size:
        raise ObservationArtifactError("artifact-size-mismatch", "artifact size differs from its reference")
    if size > MAX_ARTIFACT_BYTES:
        raise ObservationArtifactError("artifact-too-large", f"artifact exceeds {MAX_ARTIFACT_BYTES} bytes")
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise ObservationArtifactError("artifact-unreadable", str(exc)[:200]) from exc
    expected_hash = reference.get("sha256")
    if isinstance(expected_hash, str) and hashlib.sha256(raw).hexdigest() != expected_hash:
        raise ObservationArtifactError("artifact-hash-mismatch", "artifact content differs from its reference")
    return parse_artifact_text(raw.decode("utf-8"), kind=kind, page_id=page_id)


def parse_artifact_text(
    text: str,
    *,
    kind: str,
    page_id: Optional[str],
) -> Tuple[JsonDict, List[str], JsonDict]:
    lines = text.split("\n")
    indexed = [(index, line) for index, line in enumerate(lines) if line.strip()]
    if not indexed or indexed[0][0] != 0 or not indexed[0][1].startswith("# meta "):
        raise ObservationArtifactError("artifact-format-invalid", "artifact has no meta line")
    if not indexed[-1][1].startswith("# end "):
        raise ObservationArtifactError("artifact-format-invalid", "artifact has no end line")
    end_index = indexed[-1][0]
    meta = parse_text_fields(indexed[0][1][len("# meta "):])
    end = parse_text_fields(indexed[-1][1][len("# end "):])
    body = "\n".join(lines[:end_index]) + "\n"
    if hashlib.sha256(body.encode("utf-8")).hexdigest() != end.get("contentSha256"):
        raise ObservationArtifactError("artifact-content-hash-mismatch", "artifact end hash does not match")
    if meta.get("schema") != OBSERVATION_SCHEMA:
        raise ObservationArtifactError("artifact-schema-unsupported", f"unsupported schema {meta.get('schema')!r}")
    if meta.get("mode") != kind:
        raise ObservationArtifactError("artifact-mode-mismatch", f"expected {kind}, got {meta.get('mode')!r}")
    if page_id and meta.get("pageId") != page_id:
        raise ObservationArtifactError("artifact-page-mismatch", "artifact meta names another page")
    body_lines = [line for _index, line in indexed[1:-1] if not line.startswith("#")]
    return meta, body_lines, end


# ---------------------------------------------------------------------------
# Record parsing
# ---------------------------------------------------------------------------

def parse_frame_line(line: str) -> Optional[JsonDict]:
    match = _FRAME_LINE_RE.match(line)
    if match is None:
        return None
    rest = match.group("rest")
    refs = {ref.group("key"): ref.group("id") for ref in _FRAME_REF_RE.finditer(rest)}
    fields = parse_text_fields(_FRAME_REF_RE.sub("", rest))
    return {
        "frameId": match.group("id"),
        "parentFrameId": refs.get("parent"),
        "ownerId": refs.get("owner"),
        "rootId": refs.get("root"),
        "url": fields.get("url"),
        "status": fields.get("status"),
        **({"reason": fields["reason"]} if "reason" in fields else {}),
    }


def parse_component_line(line: str) -> Optional[JsonDict]:
    match = _COMPONENT_LINE_RE.match(line)
    if match is None:
        return None
    rest = match.group("rest")
    component: JsonDict = {"componentId": match.group("id")}
    for key in ("kind", "primary", "active"):
        found = re.search(rf"\b{key}=(\S+)", rest)
        if found:
            component[key] = found.group(1)
    members = re.search(r"members\{([^}]*)\}", rest)
    if members:
        component["members"] = {
            role: [item for item in ids.split(",") if item]
            for role, ids in re.findall(r"([A-Za-z]+):\[([^\]]*)\]", members.group(1))
        }
    targets = re.search(r"targets\{([^}]*)\}", rest)
    if targets:
        component["targets"] = dict(re.findall(r"([A-Za-z]+):(\S+?)(?:,|$)", targets.group(1)))
    return component


def split_full_records(body_lines: List[str]) -> Tuple[List[str], List[JsonDict], List[JsonDict]]:
    nodes: List[str] = []
    frames: List[JsonDict] = []
    components: List[JsonDict] = []
    for line in body_lines:
        if AXTREE_LINE_RE.match(line):
            nodes.append(line)
        elif line.startswith("frame "):
            frame = parse_frame_line(line)
            if frame is not None:
                frames.append(frame)
        elif line.startswith("component "):
            component = parse_component_line(line)
            if component is not None:
                components.append(component)
    return nodes, frames, components


def order_frame_blocks(lines: List[str], frames: List[JsonDict]) -> List[str]:
    """Put the main document's block first, then child frames parent-first.

    The platform writes each frame's tree as a depth-0 block, in frameId order
    - an arbitrary order in which an iframe can precede the page itself. Every
    reader that looks at node lines alone ("the first depth-0 root is the page")
    relied on the main document coming first, as it always used to. Blocks are
    moved whole, so each keeps its own document order.
    """
    blocks: List[List[str]] = []
    for line in lines:
        head = AXTREE_LINE_RE.match(line)
        if not blocks or (head is not None and head.group("depth") == "0"):
            blocks.append([line])
        else:
            blocks[-1].append(line)
    by_frame = {str(frame.get("frameId")): frame for frame in frames if frame.get("frameId")}
    root_frame = {
        str(frame.get("rootId")): str(frame.get("frameId"))
        for frame in frames if frame.get("rootId") and frame.get("frameId")
    }

    def frame_depth(frame_id: Optional[str]) -> int:
        depth, seen = 0, set()
        while frame_id and frame_id in by_frame and frame_id not in seen:
            seen.add(frame_id)
            parent = by_frame[frame_id].get("parentFrameId")
            if not parent:
                return depth
            depth += 1
            frame_id = str(parent)
        return 10_000  # a block with no known frame goes last

    def block_key(item: Tuple[int, List[str]]) -> Tuple[int, int]:
        index, block = item
        head = AXTREE_LINE_RE.match(block[0])
        frame_id = root_frame.get(head.group("id")) if head is not None else None
        return (frame_depth(frame_id), index)

    ordered = sorted(enumerate(blocks), key=block_key)
    return [line for _index, block in ordered for line in block]


def node_document_roots(lines: List[str]) -> Dict[str, Optional[str]]:
    """Map every node id to the root of the document that contains it.

    Every document - the page and each frame - is rooted at a `rootWebArea`,
    so the nearest ancestor-or-self with that role identifies the document
    from node lines alone. Replaces the frame prefix that ids used to carry.
    """
    stack: List[Tuple[int, Optional[str]]] = []
    result: Dict[str, Optional[str]] = {}
    for line in lines:
        head = AXTREE_LINE_RE.match(line)
        if head is None:
            continue
        depth = int(head.group("depth"))
        while stack and stack[-1][0] >= depth:
            stack.pop()
        node_id = head.group("id")
        if head.group("role").casefold() == "rootwebarea":
            root: Optional[str] = node_id
        else:
            root = stack[-1][1] if stack else None
        result[node_id] = root
        stack.append((depth, root))
    return result


def node_frame_map(lines: List[str], frames: List[JsonDict]) -> Dict[str, Optional[str]]:
    """Map every node id to the frame whose document contains it.

    Ids no longer encode their frame. Each frame record names its root node,
    so a node belongs to the frame of its nearest ancestor that is a frame
    root; the depth prefix reconstructs the ancestry in document order. A
    node with no such ancestor maps to None rather than to a guessed frame.
    """
    roots = {
        str(frame.get("rootId")): str(frame.get("frameId"))
        for frame in frames if frame.get("rootId") and frame.get("frameId")
    }
    stack: List[Tuple[int, Optional[str]]] = []
    result: Dict[str, Optional[str]] = {}
    for line in lines:
        head = AXTREE_LINE_RE.match(line)
        if head is None:
            continue
        depth = int(head.group("depth"))
        node_id = head.group("id")
        while stack and stack[-1][0] >= depth:
            stack.pop()
        frame_id = roots.get(node_id) or (stack[-1][1] if stack else None)
        result[node_id] = frame_id
        stack.append((depth, frame_id))
    return result


def parse_detail_line(line: str) -> Optional[JsonDict]:
    if not line.startswith("detail "):
        return None
    try:
        return parse_text_fields(line[len("detail "):])
    except ObservationArtifactError:
        return None


# ---------------------------------------------------------------------------
# Response hydration
# ---------------------------------------------------------------------------

def _public_reference(reference: Any) -> Any:
    if not isinstance(reference, dict):
        return reference
    return {key: value for key, value in reference.items() if key != "path"}


def hydrate_page_observation_response(params: Any, response: Any, *, method: Optional[str] = None) -> Any:
    """Hydrate one full or detail receipt without modifying its input."""
    if not isinstance(response, dict):
        return response
    data = response.get("data")
    if not isinstance(data, dict) or data.get("mode") not in {"full", "detail"}:
        return response
    if data.get("mode") == "detail" and isinstance(data.get("details"), list):
        hydrated = copy.deepcopy(response)
        out = hydrated["data"]
        params = params if isinstance(params, dict) else {}
        records = out["details"]
        summary = out.get("summary") or {}
        source = "snapshot" if "nodeIds" in params else "live"
        try:
            if out.get("pageId") != params.get("pageId") or out.get("source") != source:
                raise ObservationArtifactError("detail-identity-mismatch", "Details do not match page/source binding")
            if source == "snapshot" and out.get("version") != params.get("version"):
                raise ObservationArtifactError("detail-version-mismatch", "Saved details belong to a different observation")
            succeeded = sum(r.get("ok") is True for r in records if isinstance(r, dict))
            failed = sum(r.get("ok") is False for r in records if isinstance(r, dict))
            if (summary.get("total") != len(records) or summary.get("succeeded") != succeeded
                    or summary.get("failed") != failed or succeeded + failed != len(records)
                    or [r.get("index") for r in records] != list(range(len(records)))):
                raise ObservationArtifactError("detail-count-mismatch", "Details contradict their ordered summary")
            if source == "snapshot" and [r.get("target", {}).get("id") for r in records] != params.get("nodeIds"):
                raise ObservationArtifactError("detail-target-mismatch", "Saved details belong to different requested nodes")
            out["observation"] = {k: out[k] for k in ("pageId", "documentEpoch", "capturedAt", "checkedAt", "version", "source", "completeness", "pendingChanges") if k in out}
            out["records"] = [{**r, **({"value": {"nodes": [r["node"]]}} if r.get("ok") and "node" in r else {})} for r in records]
        except ObservationArtifactError as exc:
            out["observationError"] = {"code": exc.code, "message": str(exc)}
        return hydrated
    if not isinstance(data.get("artifact"), dict):
        return response
    page_id = str(params.get("pageId") or "") if isinstance(params, dict) else ""
    page_id = page_id or str(data.get("pageId") or "")
    hydrated = copy.deepcopy(response)
    out = hydrated["data"]
    # Retired platform fields must not leak host paths or imply a change chain.
    out.pop("diff", None)
    out.pop("details", None)
    try:
        expected_mode = {"DOM.getAXTree": "full", "DOM.getDetails": "detail"}.get(method)
        if expected_mode and out["mode"] != expected_mode:
            raise ObservationArtifactError("artifact-mode-mismatch", "observation mode contradicts the called Action")
        if data.get("pageId") and data["pageId"] != page_id:
            raise ObservationArtifactError("artifact-page-mismatch", "response names another page")
        meta, body, end = read_artifact(out["artifact"], kind=out["mode"], page_id=page_id)
        if out.get("version") and out["version"] != meta.get("version"):
            raise ObservationArtifactError("artifact-version-mismatch", "artifact names another observation version")
        fields = ("version", "documentEpoch", "capturedAt", "checkedAt", "freshness",
                  "pendingChanges", "completeness", "filteredCounts")
        out["observation"] = {key: meta[key] for key in fields if key in meta}
        for key in ("checkedAt", "freshness", "pendingChanges", "completeness"):
            if key in out:
                out["observation"][key] = out[key]
        if out["mode"] == "detail":
            source = out.get("source")
            if source not in {"live", "snapshot"} or meta.get("source") != source:
                raise ObservationArtifactError("artifact-source-mismatch", "detail source is missing or inconsistent")
            requested_source = "live" if isinstance(params, dict) and "query" in params else "snapshot"
            if source != requested_source:
                raise ObservationArtifactError("artifact-source-mismatch", "detail source contradicts the requested form")
            out["observation"]["source"] = source
            if source == "snapshot":
                requested_version = params.get("version") if isinstance(params, dict) else None
                if requested_version != meta.get("version"):
                    raise ObservationArtifactError("artifact-version-mismatch", "saved details do not match the requested version")
            records = [parse_detail_line(line) for line in body]
            summary = out.get("summary") or {}
            if (any(record is None for record in records)
                    or len(records) != end.get("details")
                    or len(records) != summary.get("total")
                    or any(type(record.get("ok")) is not bool for record in records)
                    or sum(record["ok"] is True for record in records) != summary.get("succeeded")
                    or sum(record["ok"] is False for record in records) != summary.get("failed")):
                raise ObservationArtifactError("artifact-record-count-mismatch", "detail records contradict their summary")
            out["records"] = records
        else:
            lines, frames, components = split_full_records(body)
            out["lines"] = order_frame_blocks(lines, frames)
            out["frames"] = frames
            out["components"] = components
            out["nodeCount"] = int(end.get("nodes") or len(lines))
        out["artifact"] = _public_reference(out["artifact"])
    except (ObservationArtifactError, UnicodeError, json.JSONDecodeError) as exc:
        out.pop("lines", None)
        out.pop("records", None)
        out.pop("observation", None)
        out["observationError"] = {"code": getattr(exc, "code", "artifact-format-invalid"), "message": str(exc)}
        out["artifact"] = _public_reference(out["artifact"])
    return hydrated


# Kept for callers importing the pre-split hydration helper.
hydrate_axtree_response = hydrate_page_observation_response


def main_document_can_scroll_down(lines: Any) -> Optional[bool]:
    """Whether a full view's main-document root still has room below it.

    The root's `scroll{can=...}` lists the directions it can still move; a
    root without `scroll{}` does not scroll at all, so the whole page is in
    view. None when the view has no main-document root line to read.
    """
    if not isinstance(lines, list):
        return None
    for line in lines:
        node = parse_axtree_line(line) if isinstance(line, str) else None
        if node is None:
            continue
        if node["role"] != "rootwebarea":
            return None
        scroll = node.get("scroll") if isinstance(node.get("scroll"), dict) else {}
        return "down" in str(scroll.get("can") or "").split("|")
    return None


def response_node_count(data: Any) -> Optional[int]:
    """Node count of a hydrated full view; None (no count stated) stays distinct from 0."""
    if not isinstance(data, dict):
        return None
    count = data.get("nodeCount")
    if isinstance(count, bool) or not isinstance(count, int):
        return None
    return max(0, count)
