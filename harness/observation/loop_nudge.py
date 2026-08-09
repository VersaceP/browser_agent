"""
harness.observation.loop_nudge - Soft loop nudges based on action and page fingerprints.

This detector is deliberately advisory. It never blocks tool execution; it
produces a small structured nudge that BrowserAgent can inject into the next
model turn when the same action keeps repeating while the observed page
fingerprint is not changing.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, List, Optional, Tuple

from harness.observation.page_fingerprint import (
    PageFingerprint,
    page_fingerprint_from_result,
    stable_hash,
)
from harness.utils import JsonDict


ACTION_HISTORY_WINDOW = 20
PAGE_HISTORY_WINDOW = 5
STAGNANT_PAGE_COUNT = 3


@dataclass
class ActionLoopNudge:
    recent_actions: List[JsonDict] = field(default_factory=list)
    recent_pages: List[PageFingerprint] = field(default_factory=list)
    pending_nudge: Optional[JsonDict] = None
    last_nudge_key: str = ""
    nudge_count: int = 0

    def record_action(self, tool_call: JsonDict, *, step: int) -> None:
        signature, label = action_signature(tool_call)
        self.recent_actions.append({
            "step": step,
            "signature": signature,
            "label": label,
        })
        if len(self.recent_actions) > ACTION_HISTORY_WINDOW:
            del self.recent_actions[: len(self.recent_actions) - ACTION_HISTORY_WINDOW]

    def observe_result(
        self,
        tool_call: JsonDict,
        result: JsonDict,
        *,
        step: int,
        agent: Any = None,
        fingerprint: Optional[PageFingerprint] = None,
    ) -> Optional[JsonDict]:
        if fingerprint is None:
            fingerprint = page_fingerprint_from_result(tool_call, result, agent=agent)
        if fingerprint is not None:
            self.recent_pages.append(fingerprint)
            if len(self.recent_pages) > PAGE_HISTORY_WINDOW:
                del self.recent_pages[: len(self.recent_pages) - PAGE_HISTORY_WINDOW]

        nudge = self._maybe_build_nudge(step)
        if nudge is not None:
            self.pending_nudge = nudge
        return nudge

    def consume_nudge(self) -> Optional[JsonDict]:
        nudge = self.pending_nudge
        self.pending_nudge = None
        return nudge

    def to_log_payload(self) -> JsonDict:
        return {
            "nudgeCount": self.nudge_count,
            "recentActionCount": len(self.recent_actions),
            "recentPageCount": len(self.recent_pages),
            "lastPageFingerprint": (
                self.recent_pages[-1].to_log_payload()
                if self.recent_pages else None
            ),
        }

    def _maybe_build_nudge(self, step: int) -> Optional[JsonDict]:
        if not self.recent_actions or not self._page_is_stagnant():
            return None
        action = self.recent_actions[-1]
        signature = str(action.get("signature") or "")
        repeat_count = sum(
            1 for item in self.recent_actions
            if item.get("signature") == signature
        )
        threshold = nudge_threshold_for_action(str(action.get("label") or ""))
        if threshold is None:
            return None
        if repeat_count < threshold:
            return None

        page_key = self.recent_pages[-1].stagnation_key() if self.recent_pages else ()
        nudge_key = json.dumps(
            {"signature": signature, "page": page_key},
            sort_keys=True,
            default=str,
        )
        if nudge_key == self.last_nudge_key:
            return None
        self.last_nudge_key = nudge_key
        self.nudge_count += 1
        page = self.recent_pages[-1] if self.recent_pages else None
        return {
            "status": "loop_nudge",
            "reason": "repeated_action_page_stalled",
            "step": step,
            "action": str(action.get("label") or ""),
            "repeatCount": repeat_count,
            "threshold": threshold,
            "pageStalledFor": STAGNANT_PAGE_COUNT,
            "pageFingerprint": page.to_log_payload() if page else None,
            "tool_was_executed": True,
            "next_instruction": (
                "The same action pattern is repeating while the observed page"
                " fingerprint is unchanged. If this was intentional polling,"
                " state the fresh evidence you expect next. Otherwise pivot:"
                " refresh DOM.getAXTree, target a different current AXTree id,"
                " change extraction strategy, or finalize with the blocker."
            ),
        }

    def _page_is_stagnant(self) -> bool:
        if len(self.recent_pages) < STAGNANT_PAGE_COUNT:
            return False
        tail = self.recent_pages[-STAGNANT_PAGE_COUNT:]
        first = tail[0].stagnation_key()
        return all(item.stagnation_key() == first for item in tail[1:])


def action_signature(tool_call: JsonDict) -> Tuple[str, str]:
    name = str(tool_call.get("name") or "")
    raw_input = tool_call.get("input") if isinstance(tool_call.get("input"), dict) else {}
    method = str(raw_input.get("method") or "") if name == "browser_call" else name
    params = raw_input.get("params") if isinstance(raw_input.get("params"), dict) else raw_input
    label = method or name
    normalized = normalize_action_params(label, params)
    payload = {"name": name, "method": method, "params": normalized}
    return stable_hash(payload), label


def normalize_action_params(label: str, params: Any) -> Any:
    params = params if isinstance(params, dict) else {}
    if label == "Input.scroll":
        return {
            "pageId": params.get("pageId"),
            "direction": params.get("direction"),
            "containerId": params.get("id") or params.get("selector") or params.get("targetId"),
        }
    if label in {"Input.click", "Input.select", "Input.type", "Input.press", "Input.drag"}:
        return {
            "pageId": params.get("pageId"),
            "target": (
                params.get("id")
                or params.get("nodeId")
                or params.get("targetId")
                or params.get("selector")
            ),
            "key": params.get("key") if label == "Input.press" else None,
            "text": params.get("text") if label == "Input.type" else None,
            "selections": params.get("selections") if label == "Input.select" else None,
        }
    if label in {"DOM.getText", "DOM.getAttribute"}:
        return {
            "pageId": params.get("pageId"),
            "target": params.get("id") or params.get("nodeId") or params.get("selector"),
            "attribute": params.get("attribute") or params.get("name"),
        }
    if label in {"DOM.getAXTree", "Page.getState"}:
        return {"pageId": params.get("pageId")}
    if label.startswith("local_fs_"):
        return {
            "glob": params.get("glob"),
            "pattern": params.get("pattern"),
            "path": params.get("path"),
            "expr": params.get("expr"),
        }
    return params


def nudge_threshold_for_action(label: str) -> Optional[int]:
    if label.startswith("local_fs_"):
        # ProgressAccountant already has a dedicated local_fs repeated-result
        # intervention. Duplicating that as a page-stall nudge creates noise.
        return None
    if label == "Input.scroll":
        return 8
    if label in {"DOM.getAXTree", "Page.getState"}:
        return 5
    if label in {"Input.click", "Input.select", "Input.type", "Input.press"}:
        return 4
    return 5
