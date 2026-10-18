"""
harness.observation.loop_nudge - Repetition facts from action and page fingerprints.

This detector is deliberately an observer. It never blocks tool execution; it
produces small structured observations for stalled actions and repeated direct
call sequences. Neither observation decides whether business work is correct
or whether the next operation can safely run in a Workflow.
"""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, List, Optional, Tuple

from harness.observation.page_fingerprint import (
    PageFingerprint,
    page_fingerprint_from_result,
    stable_hash,
)
from harness.utils import JsonDict
from harness.results.call_outcome import classify_call_outcome


ACTION_HISTORY_WINDOW = 20
PAGE_HISTORY_WINDOW = 5
STAGNANT_PAGE_COUNT = 3
REPETITION_WINDOW_TURNS = 10
REPETITION_MIN_OCCURRENCES = 3
REPETITION_MAX_PATTERN = 4
REPETITION_NOTICE_HISTORY = 32


@dataclass
class ActionLoopNudge:
    recent_actions: List[JsonDict] = field(default_factory=list)
    recent_pages: List[PageFingerprint] = field(default_factory=list)
    pending_nudge: Optional[JsonDict] = None
    last_nudge_key: str = ""
    nudge_count: int = 0
    repetition_window_turns: int = REPETITION_WINDOW_TURNS
    pending_repetition_facts: Optional[JsonDict] = None
    repetition_notice_keys: List[Tuple[str, Tuple[str, ...]]] = field(default_factory=list)
    repetition_fact_count: int = 0

    def record_action(self, tool_call: JsonDict, *, step: int) -> None:
        signature, label = action_signature(tool_call)
        self.recent_actions.append({
            "step": step,
            "toolCallId": tool_call.get("id"),
            "signature": signature,
            "label": label,
            **_direct_call_facts(tool_call),
            "outcome": "unknown",
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
        workflow_enabled: bool = False,
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
        if self.recent_actions:
            action = self.recent_actions[-1]
            if (action["step"] == step
                    and action["toolCallId"] == tool_call.get("id")):
                action["outcome"] = classify_call_outcome(result).verdict
        if workflow_enabled and self.pending_repetition_facts is None:
            self.pending_repetition_facts = self._repetition_facts(step)
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
            "repetitionFactCount": self.repetition_fact_count,
            "lastPageFingerprint": (
                self.recent_pages[-1].to_log_payload()
                if self.recent_pages else None
            ),
        }

    def consume_repetition_facts(self) -> Optional[JsonDict]:
        facts = self.pending_repetition_facts
        self.pending_repetition_facts = None
        return facts

    def _repetition_facts(self, step: int) -> Optional[JsonDict]:
        # Work only with model-issued calls, never Workflow internals or
        # platform recovery traces. Unsupported tools/page changes delimit the
        # sequence; do not manufacture adjacency by filtering them away.
        history = []
        for item in reversed(self.recent_actions):
            if (item["step"] <= step - self.repetition_window_turns
                    or not item.get("pageId") or not item.get("directCall")):
                break
            if history and item["pageId"] != history[-1]["pageId"]:
                break
            history.append(item)
        history.reverse()
        labels = [item["label"] for item in history]
        for size in range(min(REPETITION_MAX_PATTERN, len(history) // REPETITION_MIN_OCCURRENCES), 0, -1):
            pattern = labels[-size:]
            if not any(_input_method(label) for label in pattern):
                continue
            # A,A,A is one repeated method, not a new three-action pattern.
            if any(size % n == 0 and pattern == pattern[:n] * (size // n)
                   for n in range(1, size)):
                continue
            if size == 1:
                matches = [[item] for item in history if item["label"] == pattern[0]]
            else:
                matches = []
                cursor = len(history)
                while cursor >= size and labels[cursor - size:cursor] == pattern:
                    matches.append(history[cursor - size:cursor])
                    cursor -= size
                matches.reverse()
            if len(matches) < REPETITION_MIN_OCCURRENCES:
                continue
            calls = [item for match in matches for item in match]
            turns = len({item["step"] for item in calls})
            if turns < REPETITION_MIN_OCCURRENCES:
                continue  # Calls already issued in one turn are not extra rounds.
            if size == 1 and len({item["signature"] for item in calls}) < 2:
                continue  # Exact repeats remain covered by the stall observer.
            # Rotating A,B,C to B,C,A does not justify another prompt.
            canonical = _canonical_pattern(pattern)
            page_id = history[-1]["pageId"]
            if any(page == page_id and size <= len(known)
                   and any(_canonical_pattern(list((known * 2)[n:n + size])) == canonical
                           for n in range(len(known)))
                   for page, known in self.repetition_notice_keys):
                continue
            self.repetition_notice_keys.append((page_id, canonical))
            del self.repetition_notice_keys[:-REPETITION_NOTICE_HISTORY]
            self.repetition_fact_count += 1
            counts = Counter(labels)
            locators = {label: len({item["locatorKey"] for item in calls
                                   if item["label"] == label and item["locatorKey"]})
                        for label in dict.fromkeys(pattern)}
            return {
                "status": "repetition_facts",
                "pageId": history[-1]["pageId"],
                "step": step,
                "windowModelTurns": self.repetition_window_turns,
                "retainedToolCalls": len(history),
                "historyCallLimit": ACTION_HISTORY_WINDOW,
                "pattern": pattern,
                "occurrences": len(matches),
                "matchedToolCalls": len(calls),
                "matchedModelTurns": turns,
                "matchedStepRange": [calls[0]["step"], calls[-1]["step"]],
                "windowMethodCounts": dict(counts.most_common(8)),
                "locatorVariantsByMethod": locators,
                "callOutcomes": dict(Counter(item["outcome"] for item in calls)),
                "workflowAvailable": True,
                "note": (
                    "Direct-call counts, not business success or proof of distinct"
                    " records. Locator changes may be rerenders. If upcoming"
                    " actions and observation-to-target rules are decided, consider"
                    " Workflow; otherwise continue inspection. Verify uncertain"
                    " effects before replay."
                ),
            }
        return None

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
            "note": (
                "Attributed observation, not a verdict: the same action pattern"
                " repeated while the observed page fingerprint stayed"
                " unchanged. Whether that means the page is genuinely static,"
                " the action is not reaching it, or this is intentional"
                " polling is a reading of the goal, not something these counts"
                " settle. The call executed; its receipt is authoritative."
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


def _input_method(method: str) -> bool:
    return method.startswith("Input.") or method in {
        "Page.click", "Page.wheel", "File.handleChooser",
    }


def _canonical_pattern(pattern: List[str]) -> Tuple[str, ...]:
    return min(tuple(pattern[n:] + pattern[:n]) for n in range(len(pattern)))


def _direct_call_facts(tool_call: JsonDict) -> JsonDict:
    raw = tool_call.get("input") or {}
    raw = raw if isinstance(raw, dict) else {}
    method = str(raw.get("method") or "")
    params = raw.get("params") or {}
    params = params if isinstance(params, dict) else {}
    direct = (tool_call.get("name") == "browser_call"
              and (_input_method(method) or method.startswith("DOM.")
                   or method == "Page.getState"))
    locator = {key: params[key] for key in (
        "target", "id", "nodeId", "targetId", "selector", "nodeIds", "x", "y",
    ) if key in params}
    return {
        "directCall": direct,
        "pageId": str(params.get("pageId") or ""),
        # No input text, file names or selectors are echoed by this observer.
        "locatorKey": stable_hash(locator) if locator else "",
    }


def normalize_action_params(label: str, params: Any) -> Any:
    params = params if isinstance(params, dict) else {}
    if label == "Input.scroll":
        # Input.scroll's locators moved into nested `target`/`container`
        # objects. Reading only the flat keys collapses every container scroll
        # onto the same fingerprint, so distinct containers look like one
        # repeated action and trip the loop nudge.
        def _locator(value: Any) -> Any:
            if not isinstance(value, dict):
                return None
            return value.get("id") or value.get("selector")

        return {
            "pageId": params.get("pageId"),
            "deltaX": params.get("deltaX"),
            "deltaY": params.get("deltaY"),
            "x": params.get("x"),
            "y": params.get("y"),
            "edge": params.get("edge"),
            "axis": params.get("axis"),
            "containerId": (
                _locator(params.get("container"))
                or params.get("id")
                or params.get("selector")
                or params.get("targetId")
            ),
            "targetId": _locator(params.get("target")),
        }
    if label in {"Input.click", "Page.click", "Input.select", "Input.type", "Input.press", "Input.drag"}:
        select_summary: Optional[JsonDict] = None
        if label == "Input.select":
            # The selection payload's field names belong to the connected
            # platform's contract generation: the `selections` envelope and the
            # nativeValues/optionIds/optionLabels arrays are both live in the
            # wild, and both schemas admit unknown keys, so one request can
            # even carry fields from both. Every present field is read: naming
            # one generation collapsed the other onto `select=None`, and
            # stopping at the first hit hid a changed selection behind an
            # unchanged sibling. Either way two different choices on the same
            # control hashed alike and looked like a repeated action.
            present = {
                field: params.get(field)
                for field in (
                    "nativeValues",
                    "optionIds",
                    "optionLabels",
                    "selections",
                )
                if params.get(field) is not None
            }
            select_summary = present or None
        return {
            "pageId": params.get("pageId"),
            "target": (
                params.get("id")
                or params.get("nodeId")
                or params.get("targetId")
                or params.get("selector")
                or (
                    {"x": params.get("x"), "y": params.get("y")}
                    if label == "Page.click" else None
                )
            ),
            "key": params.get("key") if label == "Input.press" else None,
            "text": params.get("text") if label == "Input.type" else None,
            "select": select_summary,
            # The keyboard-driven contract continues an unfinished option walk
            # by resending the SAME selections with the startOption the
            # platform handed back. Dropping it collapses every continuation
            # step onto one fingerprint, so a walk the platform explicitly
            # asked for reads as a repeated action and trips the nudge at 4.
            # DOM.inspectSelect needs no entry: it falls through to the whole
            # params dict, which already carries startOption.
            "startOption": (
                params.get("startOption") if label == "Input.select" else None
            ),
        }
    if label == "Page.wheel":
        return {
            "pageId": params.get("pageId"),
            "point": {"x": params.get("x"), "y": params.get("y")},
            "delta": {"x": params.get("deltaX"), "y": params.get("deltaY")},
            "edge": params.get("edge"),
            "axis": params.get("axis"),
        }
    if label in {"DOM.getAXTree", "DOM.getDetails"}:
        # Bounded queries for different targets are different reads.
        return {"pageId": params.get("pageId"), "query": params.get("query"),
                "version": params.get("version"), "nodeIds": params.get("nodeIds")}
    if label == "Page.getState":
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
    if label in {"Input.scroll", "Page.wheel"}:
        return 8
    if label in {"DOM.getAXTree", "DOM.getDetails", "Page.getState"}:
        return 5
    if label in {"Input.click", "Page.click", "Input.select", "Input.type", "Input.press"}:
        return 4
    return 5
