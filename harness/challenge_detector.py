"""
harness.challenge_detector - Cheap challenge suspicion tracking.

The detector is intentionally not a keyword wall. It accumulates behavior
signals per page and only uses a tiny high-confidence keyword set as a fallback
when visual adjudication is unavailable.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

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

LINGERING_LOADING_TITLES = (
    "just a moment",
    "please wait",
    "checking your browser",
    "one more step",
    "loading",
)


def is_lingering_loading_title(title: str) -> bool:
    title_lower = str(title or "").lower()
    return bool(title_lower and any(marker in title_lower for marker in LINGERING_LOADING_TITLES))

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
    vl_attempts: int = 0

    def add_signal(self, signal: ChallengeSignal) -> None:
        weight = signal.weight
        if (
            self.last_vl_step is not None
            and self.last_vl_verdict == "normal_loading"
            and signal.step - self.last_vl_step < 5
            and signal.kind not in {"high_confidence_keyword"}
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
        if signal.kind == "high_confidence_keyword":
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
            "lastVlVerdict": self.last_vl_verdict or None,
            "lastVlStep": self.last_vl_step,
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

        if _contains_high_confidence_keyword(result):
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
