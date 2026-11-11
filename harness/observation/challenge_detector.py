"""
harness.observation.challenge_detector - Cheap challenge suspicion tracking.

The detector accumulates behavior and lexical signals per page for adjudication.
Keyword matches are attributed suspicion, not proof that a task is blocked and
not authority to pause when visual adjudication is unavailable.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from harness.constants import CHALLENGE_KEYWORDS, NAVIGATION_CHALLENGE_TITLE_KEYWORDS
from harness.observation.axtree_format import parse_axtree_line
from harness.observation.page_observation import node_document_roots
from harness.utils import JsonDict


HIGH_CONFIDENCE_CHALLENGE_KEYWORDS = (
    "captcha",
    "hcaptcha",
    "recaptcha",
    "turnstile",
    "cf-chl",
    "__cf_chl",
    "cloudflare ray id",
    "cloudflare security challenge",
    "attention required! | cloudflare",
    "verify you are human",
    "验证码",
    "人机验证",
)

STRUCTURAL_CHALLENGE_CONTROL_MARKERS = (
    "captcha",
    "challenge",
    "verify",
    "verification",
    "slide",
    "slider",
    "滑块",
    "拖动",
    "验证",
    "我不是机器人",
)

STRUCTURAL_CHALLENGE_ROOT_MARKERS = tuple(dict.fromkeys((
    *HIGH_CONFIDENCE_CHALLENGE_KEYWORDS,
    *CHALLENGE_KEYWORDS,
    "security verification",
    "security check",
    "verification required",
    "安全检查",
)))

_STRUCTURAL_ACTION_ROLES = {
    "button",
    "checkbox",
    "combobox",
    "link",
    "slider",
    "textfield",
}

LINGERING_LOADING_TITLES = (
    "just a moment",
    "please wait",
    "checking your browser",
    "one more step",
    "loading",
)

# Union of every challenge/loading title vocabulary in the harness, for gates
# that must decide "is this title safe to treat as past-the-challenge?".
# Deliberately broad: these gates control auto-resume of a HITL pause, where a
# false "challenge" only means waiting for VL adjudication or the timeout,
# while a false "clear" silently bypasses a human verification step.
CHALLENGE_TITLE_KEYWORDS = tuple(dict.fromkeys((
    *LINGERING_LOADING_TITLES,
    *NAVIGATION_CHALLENGE_TITLE_KEYWORDS,
    *HIGH_CONFIDENCE_CHALLENGE_KEYWORDS,
    *CHALLENGE_KEYWORDS,
    "attention required",
    "performing security verification",
    "security check",
    "ddos protection",
    "bot verification",
    "安全验证",
)))


# The only VL verdicts that count as POSITIVE evidence a challenge surface is
# gone. Everything else (confirmed_challenge, uncertain, an unavailable VL) is
# treated as "still blocked", because both consumers — the HITL verified-
# settlement gate and the auto-solve clearance check — decide whether a human
# gets skipped. A false "clear" silently bypasses human verification; a false
# "still blocked" only costs a retry or a wait.
VL_CLEARANCE_VERDICTS = frozenset({
    "normal_loading",
    "unrelated_block",
    "no_challenge",
    "passed",
})


def is_lingering_loading_title(title: str) -> bool:
    title_lower = str(title or "").lower()
    return bool(title_lower and any(marker in title_lower for marker in LINGERING_LOADING_TITLES))


def title_looks_like_challenge(title: str) -> bool:
    """True if a page title matches any known challenge/interstitial wording.

    Use for resume/clearance gates (e.g. the HITL verified-settlement title
    fallback), NOT as a task-blockage verdict. Detection collects behavioral,
    structural and lexical evidence for adjudication; this title union is
    intentionally over-broad in the safe direction for clearance checks.
    """
    title_lower = str(title or "").lower()
    return bool(title_lower and any(marker in title_lower for marker in CHALLENGE_TITLE_KEYWORDS))

CHALLENGE_TEXT_KEYS = {
    "accessibletext",
    "arialabel",
    "bodytext",
    "description",
    "errormessage",
    "innertext",
    "label",
    "message",
    "name",
    "observation",
    "outertext",
    "placeholder",
    "text",
    "textcontent",
    "title",
    "visibletext",
}

CHALLENGE_CONTAINER_KEYS = {
    "data",
    "response",
    "rows",
    "row",
    "items",
    "item",
    "lines",
    "records",
    "record",
    "fields",
}

CHALLENGE_SKIP_KEYS = {
    "artifact",
    "artifacts",
    "descriptionpath",
    "file",
    "filename",
    "filepath",
    "nextinstruction",
    "path",
    "purpose",
    "reason",
    "recordname",
    "savedpath",
    "suggestedprompt",
    "tracepath",
}


@dataclass
class ChallengeSignal:
    step: int
    method: str
    kind: str
    detail: str
    evidence: Optional[JsonDict] = None

    def to_dict(self) -> JsonDict:
        return {"step": self.step, "method": self.method, "kind": self.kind,
                "detail": self.detail, "evidence": self.evidence}


@dataclass
class PageChallengeState:
    page_id: str
    recent_signals: List[ChallengeSignal] = field(default_factory=list)
    last_signal_step: int = 0
    document_epoch: str = ""
    observation_version: str = ""
    last_url: str = ""
    last_title: str = ""
    last_status: str = ""
    repeated_state_count: int = 0

    def to_summary(self) -> JsonDict:
        return {
            "pageId": self.page_id,
            "documentEpoch": self.document_epoch or None,
            "observationVersion": self.observation_version or None,
            "observedAtStep": self.last_signal_step,
            "signals": [signal.to_dict() for signal in self.recent_signals],
        }


class ChallengeTracker:
    """Bound observations to a page/document; never classify task blockage."""

    def __init__(self):
        self._per_page: Dict[str, PageChallengeState] = {}

    def feed(self, *, method: str, params: Any, result: Any,
             step: int) -> Optional[PageChallengeState]:
        page_id = extract_page_id(params, result)
        if not page_id:
            return None
        if method in {"Page.close", "Page.navigate", "Page.reload", "Page.go"}:
            self.clear_page(page_id)
            return None
        data = response_data(result)
        observation = data.get("observation")
        observation = observation if isinstance(observation, dict) else {}
        component = _structural_challenge_receipt(result)
        epoch = str(observation.get("documentEpoch") or data.get("documentEpoch")
                    or (component or {}).get("documentEpoch") or "")
        version = str(observation.get("version") or data.get("version")
                      or (component or {}).get("observationVersion") or "")
        url = str(data.get("url") or "")
        state = self._per_page.setdefault(page_id, PageChallengeState(page_id=page_id))
        if ((epoch and state.document_epoch and epoch != state.document_epoch)
                or (version and state.observation_version and version != state.observation_version)
                or (url and state.last_url and url != state.last_url)):
            state = PageChallengeState(page_id=page_id)
            self._per_page[page_id] = state
        state.document_epoch = epoch or state.document_epoch
        state.observation_version = version or state.observation_version
        signals: List[ChallengeSignal] = []
        if component:
            signals.append(ChallengeSignal(
                step, method, "challenge_component",
                "Challenge-related document and control facts; task access is unconfirmed",
                component,
            ))
        keyword = _first_high_confidence_keyword(result)
        if keyword:
            signals.append(ChallengeSignal(step, method, "keyword_match",
                                           "Text matched a challenge term", keyword))
        title = str(data.get("title") or "")
        status = str(data.get("status") or "")
        if is_lingering_loading_title(title):
            signals.append(ChallengeSignal(step, method, "loading_title",
                                           "Page title contains loading wording", {"title": title}))
        current = (url, title, status)
        previous = (state.last_url, state.last_title, state.last_status)
        state.repeated_state_count = state.repeated_state_count + 1 if all(current) and current == previous else 0
        if state.repeated_state_count >= 3:
            signals.append(ChallengeSignal(step, method, "repeated_state",
                "Repeated page state; no conclusion about its cause",
                {"count": state.repeated_state_count, "url": url, "title": title, "status": status}))
        state.last_url = url or state.last_url
        state.last_title = title or state.last_title
        state.last_status = status or state.last_status
        # A fresh full tree replaces prior component facts even when it contains
        # no match. A state read may retain the last tree's attributed witnesses.
        if signals or method == "DOM.getAXTree":
            state.recent_signals = signals
            state.last_signal_step = step
        return state

    def get_state(self, page_id: str) -> Optional[PageChallengeState]:
        return self._per_page.get(page_id)

    def on_page_closed(self, page_id: str) -> None:
        self.clear_page(page_id)

    def clear_page(self, page_id: str) -> None:
        self._per_page.pop(page_id, None)

    def cleanup_stale(self, current_step: int, max_age_steps: int = 30) -> None:
        for page_id, state in list(self._per_page.items()):
            if current_step - state.last_signal_step > max_age_steps:
                self.clear_page(page_id)

    def suspected_pages(self) -> List[JsonDict]:
        return [state.to_summary() for state in self._per_page.values() if state.recent_signals]


def extract_page_id(params: Any, result: Any) -> str:
    for container in (params, result):
        if isinstance(container, dict):
            for key in ("pageId", "page_id"):
                value = container.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
    data = response_data(result)
    for key in ("pageId", "page_id"):
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def response_data(result: Any) -> JsonDict:
    if not isinstance(result, dict):
        return {}
    response = result.get("response")
    if isinstance(response, dict):
        data = response.get("data")
        if isinstance(data, dict):
            return data
    data = result.get("data")
    return data if isinstance(data, dict) else {}


def _component_node_facts(node: JsonDict) -> JsonDict:
    """Publish state facts without copying form values or asserting actionability."""
    attrs = node.get("attrs") or {}
    return {
        "id": node["id"], "role": node["role"], "label": str(node.get("name") or "")[:200],
        "visibility": node.get("visibility"), "flags": list(node.get("flags") or []),
        "interactionSources": list(node.get("interactionSources") or []),
        "rect": node.get("rect"),
        "attributes": {key: attrs[key] for key in ("type", "hidden", "aria-hidden") if key in attrs},
    }


def detect_structural_challenge_from_lines(
    lines: Any, *, source_method: str = "DOM.getAXTree", frames: Any = None,
) -> Optional[JsonDict]:
    """Report component facts, including hidden nodes; never prove blockage.

    Frame-local root visibility does not establish visibility through its owner.
    Preserve both when frame identity is available, so the model can judge them.
    """
    if not isinstance(lines, list):
        return None
    normalized = [line for line in lines if isinstance(line, str)]
    documents = node_document_roots(normalized)
    parsed = [parse_axtree_line(line) for line in normalized]
    nodes = {node["id"]: node for node in parsed if node is not None}
    for start, root in enumerate(parsed):
        if root is None or root["role"] != "rootwebarea":
            continue
        if not any(marker.casefold() in str(root.get("name") or "").casefold()
                   for marker in STRUCTURAL_CHALLENGE_ROOT_MARKERS):
            continue
        controls = []
        for index, node in enumerate(parsed):
            if (node is None or documents.get(node["id"]) != root["id"]
                    or node["role"] not in _STRUCTURAL_ACTION_ROLES):
                continue
            if any(marker.casefold() in normalized[index].casefold()
                   for marker in STRUCTURAL_CHALLENGE_CONTROL_MARKERS):
                controls.append({**_component_node_facts(node), "sourceLine": index + 1})
        if not controls:
            continue
        frame = next((frame for frame in (frames if isinstance(frames, list) else [])
                      if isinstance(frame, dict) and frame.get("rootId") == root["id"]), {})
        owner = nodes.get(frame.get("ownerId"))
        return {
            "kind": "challenge_component", "sourceMethod": source_method,
            "rootId": root["id"], "rootLabel": str(root.get("name") or "")[:200],
            "root": _component_node_facts(root), "sourceLine": start + 1,
            "frameId": frame.get("frameId"), "ownerId": frame.get("ownerId"),
            "owner": _component_node_facts(owner) if owner else None,
            "controls": controls[:5], "controlCount": len(controls),
            "coverage": "first_matching_document",
            "blockingStatus": "unconfirmed",
        }
    return None


def detect_structural_challenge(method: str, response: Any) -> Optional[JsonDict]:
    if method != "DOM.getAXTree" or not isinstance(response, dict):
        return None
    payload = response.get("data")
    if not isinstance(payload, dict):
        return None
    evidence = detect_structural_challenge_from_lines(
        payload.get("lines"), source_method=method, frames=payload.get("frames"))
    if evidence:
        observation = payload.get("observation")
        observation = observation if isinstance(observation, dict) else {}
        evidence["pageId"] = payload.get("pageId")
        evidence["documentEpoch"] = observation.get("documentEpoch") or payload.get("documentEpoch")
        evidence["observationVersion"] = observation.get("version") or payload.get("version")
    return evidence


def _structural_challenge_receipt(result: Any) -> Optional[JsonDict]:
    if not isinstance(result, dict):
        return None
    receipt = result.get("challengeComponent")
    if isinstance(receipt, dict) and receipt.get("kind") == "challenge_component":
        return receipt
    return detect_structural_challenge(str(result.get("method") or ""), result.get("response"))


def _contains_high_confidence_keyword(value: Any) -> bool:
    return _first_high_confidence_keyword(value) is not None


def _first_high_confidence_keyword(value: Any) -> Optional[JsonDict]:
    """Return one bounded lexical witness, without interpreting its UI meaning."""
    parts: List[tuple[str, str]] = []
    _collect_challenge_strings(value, parts, limit=80)
    for path, text in parts:
        text_lower = text.lower()
        for keyword in HIGH_CONFIDENCE_CHALLENGE_KEYWORDS:
            index = text_lower.find(keyword.lower())
            if index >= 0:
                start = max(0, index - 100)
                return {"keyword": keyword, "sourcePath": path, "excerpt": text[start:start + 400]}
    return None


def _collect_challenge_strings(
    value: Any, out: List[tuple[str, str]], *, limit: int, path: str = "$",
) -> None:
    if len(out) >= limit:
        return
    if isinstance(value, dict):
        for raw_key, item in value.items():
            key = str(raw_key).replace("_", "").lower()
            if key in CHALLENGE_SKIP_KEYS:
                continue
            if isinstance(item, str) and key in CHALLENGE_TEXT_KEYS:
                out.append((f"{path}.{raw_key}", item[:2000]))
            elif isinstance(item, (dict, list)) and (
                key in CHALLENGE_CONTAINER_KEYS or key in CHALLENGE_TEXT_KEYS
            ):
                _collect_challenge_strings(item, out, limit=limit, path=f"{path}.{raw_key}")
            if len(out) >= limit:
                break
    elif isinstance(value, list):
        for index, item in enumerate(value):
            if isinstance(item, str):
                out.append((f"{path}[{index}]", item[:2000]))
            elif isinstance(item, (dict, list)):
                _collect_challenge_strings(item, out, limit=limit, path=f"{path}[{index}]")
            if len(out) >= limit:
                break
