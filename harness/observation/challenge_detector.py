"""
harness.observation.challenge_detector - Cheap challenge suspicion tracking.

The detector is intentionally not a keyword wall. It accumulates behavior
signals per page and only uses a tiny high-confidence keyword set as a fallback
when visual adjudication is unavailable.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import re
from typing import Any, Dict, List, Optional

from harness.constants import CHALLENGE_KEYWORDS, NAVIGATION_CHALLENGE_TITLE_KEYWORDS
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

_AX_LINE_RE = re.compile(
    r'^\s*(?P<depth>\d+)\s+\[(?P<id>\d+:-?\d+:\d+)\]\s+'
    r'(?P<role>[a-zA-Z][a-zA-Z0-9_-]*)(?:\s+"(?P<label>[^"]*)")?'
)
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
    fallback), NOT for challenge *detection* — detection stays behavioral with
    only the small HIGH_CONFIDENCE_CHALLENGE_KEYWORDS fallback, while this
    union is intentionally over-broad in the safe direction.
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
    weight: int
    detail: str

    def to_dict(self) -> JsonDict:
        return {
            "step": self.step,
            "method": self.method,
            "kind": self.kind,
            "weight": self.weight,
            "detail": self.detail[:500],
        }


@dataclass
class PageChallengeState:
    page_id: str
    suspicion_score: int = 0
    recent_signals: List[ChallengeSignal] = field(default_factory=list)
    last_signal_step: int = 0
    last_vl_step: Optional[int] = None
    last_vl_verdict: str = ""
    last_url: str = ""
    last_title: str = ""
    last_status: str = ""
    repeated_state_count: int = 0
    lingering_title_count: int = 0
    high_confidence_hit: bool = False
    structural_challenge: bool = False
    structural_evidence: Optional[JsonDict] = None
    vl_attempts: int = 0

    def add_signal(self, signal: ChallengeSignal) -> None:
        weight = signal.weight
        if (
            self.last_vl_step is not None
            and self.last_vl_verdict == "normal_loading"
            and signal.step - self.last_vl_step < 5
            and signal.kind not in {"high_confidence_keyword", "structural_challenge"}
        ):
            weight = max(1, weight // 3)
            signal = ChallengeSignal(
                step=signal.step,
                method=signal.method,
                kind=signal.kind,
                weight=weight,
                detail=f"{signal.detail} (VL normal_loading cooldown)",
            )
        self.suspicion_score += weight
        self.last_signal_step = max(self.last_signal_step, signal.step)
        if signal.kind in {"high_confidence_keyword", "structural_challenge"}:
            self.high_confidence_hit = True
        self.recent_signals.append(signal)
        self.recent_signals = self.recent_signals[-12:]

    def should_adjudicate(self, step: int, threshold: int = 70) -> bool:
        if self.last_vl_step is not None and step - self.last_vl_step < 5:
            return self.suspicion_score >= max(100, threshold + 30)
        return self.suspicion_score >= threshold

    def record_vl_verdict(self, step: int, verdict: str) -> None:
        self.last_vl_step = step
        self.last_vl_verdict = verdict
        self.vl_attempts += 1
        if verdict == "normal_loading":
            self.suspicion_score = min(self.suspicion_score, 20)
        elif verdict == "confirmed_challenge":
            self.suspicion_score = max(self.suspicion_score, 100)

    def to_summary(self) -> JsonDict:
        return {
            "pageId": self.page_id,
            "suspicionScore": self.suspicion_score,
            "highConfidenceHit": self.high_confidence_hit,
            "structuralChallenge": self.structural_challenge,
            "structuralEvidence": self.structural_evidence,
            "lastVlVerdict": self.last_vl_verdict or None,
            "lastVlStep": self.last_vl_step,
            "vlAttempts": self.vl_attempts,
            "signals": [signal.to_dict() for signal in self.recent_signals[-5:]],
        }


class ChallengeTracker:
    def __init__(self, *, threshold: int = 70):
        self.threshold = threshold
        self._per_page: Dict[str, PageChallengeState] = {}

    def feed(
        self,
        *,
        method: str,
        params: Any,
        result: Any,
        step: int,
    ) -> Optional[PageChallengeState]:
        page_id = extract_page_id(params, result)
        if not page_id:
            return None
        state = self._per_page.setdefault(page_id, PageChallengeState(page_id=page_id))
        state.last_signal_step = max(state.last_signal_step, step)
        if method == "Page.close":
            self.on_page_closed(page_id)
            return None

        data = response_data(result)
        title = str(data.get("title") or "")
        url = str(data.get("url") or "")
        status = str(data.get("status") or "")

        structural = _structural_challenge_receipt(result)
        if structural:
            state.structural_challenge = True
            state.structural_evidence = structural
            state.add_signal(ChallengeSignal(
                step=step,
                method=method,
                kind="structural_challenge",
                weight=100,
                detail=(
                    f"Embedded challenge frame {structural.get('rootLabel')!r}"
                    f" exposed {len(structural.get('controls') or [])} verification control(s)"
                ),
            ))
        elif _contains_high_confidence_keyword(result):
            state.add_signal(ChallengeSignal(
                step=step,
                method=method,
                kind="high_confidence_keyword",
                weight=90,
                detail="High-confidence CAPTCHA/challenge keyword detected",
            ))

        title_lower = title.lower()
        if title and any(marker in title_lower for marker in LINGERING_LOADING_TITLES):
            state.lingering_title_count += 1
            weight = 35
            state.add_signal(ChallengeSignal(
                step=step,
                method=method,
                kind="title_lingers_non_target",
                weight=weight,
                detail=f"title={title!r}, url={url!r}, status={status!r}",
            ))
        elif title:
            state.lingering_title_count = 0

        current_key = (url, title, status)
        last_key = (state.last_url, state.last_title, state.last_status)
        if all(current_key) and current_key == last_key:
            state.repeated_state_count += 1
            if state.repeated_state_count >= 3:
                state.add_signal(ChallengeSignal(
                    step=step,
                    method=method,
                    kind="state_stagnant",
                    weight=15,
                    detail=f"Repeated page state {state.repeated_state_count} times",
                ))
        else:
            state.repeated_state_count = 0
            state.last_url, state.last_title, state.last_status = current_key

        return state

    def should_adjudicate(self, page_id: str, step: int) -> bool:
        state = self.get_state(page_id)
        return bool(state and state.should_adjudicate(step, threshold=self.threshold))

    def record_vl_verdict(self, page_id: str, step: int, verdict: str) -> None:
        state = self.get_state(page_id)
        if state is not None:
            state.record_vl_verdict(step, verdict)

    def get_state(self, page_id: str) -> Optional[PageChallengeState]:
        return self._per_page.get(page_id)

    def on_page_closed(self, page_id: str) -> None:
        self._per_page.pop(page_id, None)

    def clear_page(self, page_id: str) -> None:
        self._per_page.pop(page_id, None)

    def cleanup_stale(self, current_step: int, max_age_steps: int = 30) -> None:
        for page_id, state in list(self._per_page.items()):
            if current_step - state.last_signal_step > max_age_steps:
                del self._per_page[page_id]

    def suspected_pages(self) -> List[JsonDict]:
        return [
            state.to_summary()
            for state in self._per_page.values()
            if state.suspicion_score > 0
        ]


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


def detect_structural_challenge_from_lines(
    lines: Any,
    *,
    source_method: str = "DOM.getAXTree",
) -> Optional[JsonDict]:
    """Detect a visible embedded challenge frame from compact AXTree lines.

    A keyword anywhere on a normal page is not enough.  The evidence must be a
    depth-0 ``rootwebarea`` whose own label is challenge-like, plus an
    actionable verification control in that frame.  This catches small or
    visually unobtrusive CAPTCHA iframes without treating documentation text as
    a blocking challenge.
    """
    if not isinstance(lines, list):
        return None
    normalized = [str(line) for line in lines if isinstance(line, str)]
    roots: List[int] = []
    parsed: List[Optional[re.Match[str]]] = []
    for index, line in enumerate(normalized):
        match = _AX_LINE_RE.match(line)
        parsed.append(match)
        if (
            match is not None
            and match.group("depth") == "0"
            and match.group("role").casefold() == "rootwebarea"
        ):
            roots.append(index)

    for root_position, start in enumerate(roots):
        root_match = parsed[start]
        if root_match is None:
            continue
        root_line = normalized[start]
        root_label = str(root_match.group("label") or "").strip()
        root_haystack = f"{root_label} {root_line}".casefold()
        if not any(
            marker.casefold() in root_haystack
            for marker in STRUCTURAL_CHALLENGE_ROOT_MARKERS
        ):
            continue
        if re.search(r"(?:^|\s)(?:hidden|off)(?:\s|$)", root_line.casefold()):
            continue

        end = roots[root_position + 1] if root_position + 1 < len(roots) else len(normalized)
        frame_id = root_match.group("id").split(":", 1)[0]
        controls: List[JsonDict] = []
        for index in range(start + 1, end):
            match = parsed[index]
            if match is None:
                continue
            node_id = match.group("id")
            if node_id.split(":", 1)[0] != frame_id:
                continue
            role = match.group("role").casefold()
            if role not in _STRUCTURAL_ACTION_ROLES:
                continue
            label = str(match.group("label") or "").strip()
            line_haystack = f"{label} {normalized[index]}".casefold()
            if not any(
                marker.casefold() in line_haystack
                for marker in STRUCTURAL_CHALLENGE_CONTROL_MARKERS
            ):
                continue
            controls.append({"id": node_id, "role": role, "label": label})

        if controls:
            return {
                "kind": "embedded_challenge_frame",
                "sourceMethod": source_method,
                "frameId": frame_id,
                "rootId": root_match.group("id"),
                "rootLabel": root_label,
                "controls": controls[:5],
            }
    return None


def detect_structural_challenge(method: str, response: Any) -> Optional[JsonDict]:
    if method != "DOM.getAXTree" or not isinstance(response, dict):
        return None
    payload = response.get("data")
    if not isinstance(payload, dict):
        return None
    return detect_structural_challenge_from_lines(
        payload.get("lines"), source_method=method
    )


def _structural_challenge_receipt(result: Any) -> Optional[JsonDict]:
    if not isinstance(result, dict):
        return None
    receipt = result.get("structuralChallenge")
    if isinstance(receipt, dict) and receipt.get("kind") == "embedded_challenge_frame":
        return receipt
    method = str(result.get("method") or "")
    response = result.get("response")
    return detect_structural_challenge(method, response)


def _contains_high_confidence_keyword(value: Any) -> bool:
    parts: List[str] = []
    _collect_challenge_strings(value, parts, limit=80)
    haystack = "\n".join(parts).lower()
    return any(keyword.lower() in haystack for keyword in HIGH_CONFIDENCE_CHALLENGE_KEYWORDS)


def _collect_challenge_strings(value: Any, out: List[str], *, limit: int) -> None:
    if len(out) >= limit:
        return
    if isinstance(value, dict):
        for raw_key, item in value.items():
            key = str(raw_key).replace("_", "").lower()
            if key in CHALLENGE_SKIP_KEYS:
                continue
            if isinstance(item, str) and key in CHALLENGE_TEXT_KEYS:
                out.append(item[:2000])
            elif isinstance(item, (dict, list)) and (
                key in CHALLENGE_CONTAINER_KEYS or key in CHALLENGE_TEXT_KEYS
            ):
                _collect_challenge_strings(item, out, limit=limit)
            if len(out) >= limit:
                break
    elif isinstance(value, list):
        for item in value:
            if isinstance(item, str):
                out.append(item[:2000])
            elif isinstance(item, (dict, list)):
                _collect_challenge_strings(item, out, limit=limit)
            if len(out) >= limit:
                break
