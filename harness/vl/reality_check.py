"""Visual reality check: task-type-agnostic VL second opinion.

When a worker's perception keeps falling short of what the task expects —
zero yield, OR plenty of rows that never satisfy the phase contract (the
2cb616 failure mode: 10/3/228 rows harvested, none of them the requested
#40-#50) — a screenshot + VL verdict answers "what is actually on this
page?" independently of DOM probing. The trigger is the *target-shortfall
streak*, never a specific validator kind, so collections, forms, detail
pages and navigation checks are all covered.

Streak semantics (the important subtlety): raw non-zero yield does NOT
reset the streak — mis-attributed rows look productive while still missing
the target. Only contract-level satisfaction (a record_extraction that
passed validation, or collect_items reporting target_met) resets it.

This module holds the pure logic (yield classification, claim synthesis,
semantic-terminal claim detection, ledger citation check). The hot-path
hook that executes the check lives in harness.tools.browser_tools (same
split as harness.vl.arbiter / its _maybe_vl_arbitrate hook).
"""
from __future__ import annotations

import json
import os
import re
from typing import Any, Collection, Dict, List, Optional

JsonDict = Dict[str, Any]

# Categories a final_answer blocker may claim that require visual evidence
# before the worker is allowed to terminate on them (layer-3 soft reject).
SEMANTIC_TERMINAL_CATEGORIES = frozenset({
    "target_absent",
    "instruction_infeasible",
})

# Natural-language fallback for semantic-terminal claims: the final_answer
# `answer` field is unconstrained, so workers may declare absence in prose
# instead of the blockers JSON. Erring wide is safe — the soft reject fires
# at most once and offers an explicit re-issue escape hatch.
_NL_ABSENT_RE = re.compile(
    r"target[\s_-]?absent"
    r"|instructions?\b[^.\n]{0,24}\binfeasible"
    r"|(?:page|list|site)[^.\n]{0,40}only\s+"
    r"(?:shows|renders|displays|has|contains)[^.\n]{0,24}#?\d"
    r"|(?:page|target|range|rank|item|row|section|content)s?[^.\n]{0,60}"
    r"(?:do(?:es)? not exist|do(?:es)? not appear|(?:is|are) missing|"
    r"not present|absent|"
    r"(?:cannot|can't|could not) be found|not found on (?:the |this )?page)",
    re.IGNORECASE,
)

# Recovery narratives that mention absence in passing ("was absent initially
# but I revealed it", "not found on the page, but after scrolling it
# appeared") describe SUCCESS — subtract them from the absent match.
# Deliberately PAST/present-tense only (appeared/appears, never bare
# "appear"): speculative absence like "absent, but more items may appear
# after scrolling" uses the bare infinitive after a modal, and subtracting
# it would let an absence claim through unverified (a false negative, which
# is the expensive direction for this gate).
_NL_RECOVERY_RE = re.compile(
    r"\bbut\b[^.\n]{0,80}"
    r"(?:appear(?:ed|s)|reveal(?:ed|s)|extract(?:ed|s)|recover(?:ed|s)|"
    r"found it|became visible|loaded|render(?:ed|s))",
    re.IGNORECASE,
)

# Tools whose results carry a countable yield. Everything else returns None
# from classify_target_yield (does not touch the streak).
_ROW_YIELD_TOOLS = frozenset({
    "collect_items",
    "find_in_axtree",
    "record_extraction",
})

# Legacy collect_items stop reasons that indicate target shortfall.  Despite
# historical naming, these are not trusted exhaustion evidence.
_COLLECT_SHORTFALL_STOP_REASONS = frozenset({
    "stagnant",
    "load_more_exhausted",
    "max_rounds",
})


def _record_extraction_shortfall(record: JsonDict) -> Optional[bool]:
    """Classify an embedded/standalone record_extraction outcome.

    needs_fix = rows persisted but failed the contract (wrong shape/range);
    done + validationPending = saved but the expected count is not reached.
    Both are shortfall. A clean done is the ONLY satisfaction signal.
    """
    status = str(record.get("status") or "")
    if status == "needs_fix":
        return True
    if status == "done":
        pending = record.get("validationPending")
        if isinstance(pending, list) and pending:
            return True
        return False
    return None


def classify_target_yield(tool_name: str, result: JsonDict) -> Optional[bool]:
    """Classify a perception-tool result for the target-shortfall streak.

    Returns True for a shortfall outcome (nothing found, or found-but-not-
    the-target per the contract), False for contract-level satisfaction
    (resets the streak), and None for tools/outcomes that say nothing about
    the target (errors, rejections, gates, and — deliberately — raw non-zero
    yield that never went through contract validation).
    """
    if tool_name not in _ROW_YIELD_TOOLS or not isinstance(result, dict):
        return None
    status = str(result.get("status") or "")
    if status not in {"done", "partial", "needs_fix", ""}:
        return None

    if tool_name == "record_extraction":
        return _record_extraction_shortfall(result)

    # Embedded persistence outcome (record_name paths of extract/eval/collect)
    # is the contract-level signal and takes precedence over raw counts.
    record = result.get("recordExtraction")
    if isinstance(record, dict):
        embedded = _record_extraction_shortfall(record)
        if embedded is not None:
            return embedded

    if tool_name == "collect_items":
        collection_state = str(result.get("collectionState") or "")
        if collection_state in {"target_reached", "explicitly_exhausted"}:
            return False
        if collection_state == "materialization_stalled":
            return True
        if collection_state == "blocked":
            # Challenge/auth/overlay classifiers own this outcome.
            return None
        # Legacy traces have no collectionState; keep their conservative
        # stopReason/count fallback in the same branch.
        stop_reason = str(result.get("stopReason") or "")
        if stop_reason == "target_met":
            return False
        rows = result.get("rows")
        items = result.get("itemsCollected")
        reported_count = result.get("rowCount")
        row_count = (
            len(rows) if isinstance(rows, list)
            else items if isinstance(items, (int, float))
            else reported_count if isinstance(reported_count, (int, float))
            else None
        )
        if row_count == 0:
            return True
        if stop_reason in _COLLECT_SHORTFALL_STOP_REASONS:
            # Legacy traces have no mechanical exhaustion evidence.  Treat
            # these stops conservatively as shortfall.
            return True
        return None

    if tool_name == "find_in_axtree":
        matches = result.get("matches")
        if isinstance(matches, list):
            return True if len(matches) == 0 else None
        count = result.get("matchCount")
        if isinstance(count, (int, float)):
            return True if count == 0 else None
        return None

    rows = result.get("rows")
    if isinstance(rows, list):
        return True if len(rows) == 0 else None
    row_count = result.get("rowCount")
    if isinstance(row_count, (int, float)):
        return True if row_count == 0 else None
    return None


def _expected_row_count(contract: JsonDict) -> Optional[tuple]:
    """Expected row count lives in expected_artifact.exact_rows in ideal
    plans, but real contracts carry it only as a validator — and plan-author
    validators are copied through normalization verbatim, so the count may
    sit under "value", "count", "exact" or "min" (the same fallback chain
    _run_validator uses). Returns (count, is_minimum) or None."""
    expected = contract.get("expected_artifact")
    expected = expected if isinstance(expected, dict) else {}
    raw = expected.get("exact_rows")
    if isinstance(raw, (int, float)) and raw:
        return int(raw), False
    validators = contract.get("validators")
    if isinstance(validators, list):
        for validator in validators:
            if not isinstance(validator, dict):
                continue
            vtype = str(validator.get("type") or "")
            if vtype in {"exact_rows", "min_rows"}:
                for key in ("value", "count", "exact", "min"):
                    # Same tolerance as _run_validator: "11" counts like 11.
                    try:
                        number = int(float(validator.get(key)))
                    except (TypeError, ValueError):
                        continue
                    if number:
                        return number, vtype == "min_rows"
    return None


def synthesize_claim(worker_contract: Optional[JsonDict], task_text: str = "") -> str:
    """Build a VL-checkable claim from the worker contract.

    The claim states what the task EXPECTS to exist on the page; the VL
    verdict (satisfied / violated + observation) then confirms or refutes
    the DOM conclusion. Falls back to a generic content question when the
    contract carries no usable expectation.
    """
    contract = worker_contract if isinstance(worker_contract, dict) else {}
    expected = contract.get("expected_artifact")
    expected = expected if isinstance(expected, dict) else {}
    parts = []
    objective = str(contract.get("objective") or "").strip()
    worker_task = str(contract.get("worker_task") or "").strip()
    if objective:
        parts.append(f"Task objective: {objective.rstrip('.')}.")
    elif worker_task:
        parts.append(f"Task: {worker_task[:300].rstrip('.')}.")
    elif task_text:
        parts.append(f"Task: {str(task_text)[:200].rstrip('.')}.")
    fields = expected.get("fields")
    if isinstance(fields, list) and fields:
        parts.append(
            "The page is expected to show content with: "
            + ", ".join(str(f) for f in fields[:8])
            + "."
        )
    row_expectation = _expected_row_count(contract)
    if row_expectation:
        expected_rows, is_minimum = row_expectation
        qualifier = "At least" if is_minimum else "About"
        parts.append(f"{qualifier} {expected_rows} matching items are expected.")
    parts.append(
        "Question: does the expected content actually exist on this page?"
        " Report what IS visible instead (e.g. the highest rank/index/section"
        " you can see), and whether more content plausibly exists beyond the"
        " current viewport."
    )
    return " ".join(parts)


def artifact_stall_turns(agent: Any) -> Optional[int]:
    """Tool calls this worker has made without persisting anything.

    Read from the ProgressAccountant rather than counted here: the harness
    already maintains exactly this number (resetting it when an extraction
    artifact lands) and already acts on it at
    PRODUCTIVE_WITHOUT_ARTIFACT_HARD_LIMIT. A second counter with its own
    notion of "stuck" would drift from that one and there would be no way to
    say which was right.
    """
    tracker = getattr(agent, "progress", None)
    turns = getattr(tracker, "turns_since_artifact_progress", None)
    if isinstance(turns, bool) or not isinstance(turns, int):
        return None
    return turns


def stall_armed(agent: Any, threshold: int) -> bool:
    """Whether flailing alone should arm the visual check.

    The target-shortfall streak only counts tools that report a row yield, so
    a worker looping on DOM.getAXTree / DOM.getSemanticTree / local_fs_read
    never arms it — observed live in task e3173b5b, where a worker spent 30+
    steps on those three and the streak stayed at 0 the whole time. Perception
    that produces nothing at all is the same predicament the shortfall streak
    describes; it just leaves no yield to count.
    """
    if threshold <= 0:
        return False
    turns = artifact_stall_turns(agent)
    return turns is not None and turns >= threshold


def resolve_current_row(url: str, row_keys: Any) -> Optional[str]:
    """Which assigned row the page now open belongs to, or None.

    Delegates to the row ledger's URL matcher so a reality check and the ledger
    it will be compared against can never disagree about which row a page is —
    including its refusal to guess when two assigned rows both prefix the URL.
    """
    from harness.row_ledger import row_key_for_url

    keys = [str(key) for key in (row_keys or []) if str(key or "").strip()]
    return row_key_for_url(str(url or ""), keys)


def assigned_row_keys(worker_contract: Any, phase: Any = None) -> List[str]:
    """Row keys this worker owns, from whichever shape its contract carries.

    Delegates to the row ledger's resolver so the visual check and the ledger
    agree on which rows exist. Reading only the materializer's receipts missed
    direct `batch_rows` entirely — a contract carrying user-supplied URLs got
    no row keys, the claim silently stayed cohort-scoped, and a detail page was
    asked whether all three products were on it (observed live in task
    857616aa, the exact 5324506f shape this projection exists to prevent).
    """
    from harness.row_ledger import assigned_row_keys_from_contract

    return assigned_row_keys_from_contract(worker_contract, phase)


def build_row_scoped_claim(
    *,
    worker_contract: Any,
    row_key: str,
    fields: Any = None,
    region_hint: str = "",
) -> str:
    """A claim about ONE item's region — never about the cohort's total.

    The defect this closes: the generic claim carried the whole phase's
    expectation ("about 16 matching items are expected") onto a detail page
    showing one product. The model answered the question it was asked — no,
    this page does not have 16 — and the worker read that as confirmation that
    the content it wanted was missing. A screenshot can only answer a question
    scoped to what it depicts, so the row count is dropped here by design; the
    cohort-level expectation is checked against artifacts, not pixels.
    """
    from harness.extraction_artifacts import field_names_from_specs

    contract = worker_contract if isinstance(worker_contract, dict) else {}
    # `fields` may arrive as bare names or as the object specs a contract
    # normally carries ({"name": "reviews", "type": "array"}). str() on a spec
    # would put a Python dict repr in front of the model, so both shapes go
    # through the one parser the rest of the harness uses.
    field_names = field_names_from_specs(fields) if isinstance(fields, list) else []
    if not field_names:
        expected = contract.get("expected_artifact")
        expected = expected if isinstance(expected, dict) else {}
        field_names = field_names_from_specs(expected.get("fields"))[:8]

    parts = [f"Item under inspection: {str(row_key)[:200]}."]
    if region_hint:
        parts.append(f"Region: {str(region_hint)[:200]}.")
    elif field_names:
        parts.append(
            "Region: the section of this page that would carry "
            + ", ".join(field_names[:8])
            + "."
        )
    else:
        parts.append("Region: the main content section of this page.")
    parts.append(
        "Question: for THIS item only, is that region present on the page, and"
        " does it hold content?"
    )
    return " ".join(parts)


def _nl_absence_claimed(text: str) -> bool:
    """Prose absence claim, minus recovery narratives that mention absence in
    passing while describing success."""
    match = _NL_ABSENT_RE.search(text)
    if not match:
        return False
    # Scope the recovery subtraction to the matched sentence so an unrelated
    # "but ..." elsewhere in a long answer does not mask a real claim.
    sentence_start = text.rfind("\n", 0, match.start())
    sentence_start = max(sentence_start, text.rfind(". ", 0, match.start()))
    sentence_end = text.find("\n", match.end())
    if sentence_end == -1:
        sentence_end = len(text)
    period = text.find(". ", match.end())
    if period != -1:
        sentence_end = min(sentence_end, period + 1)
    sentence = text[max(sentence_start, 0):sentence_end]
    return not _NL_RECOVERY_RE.search(sentence)


def semantic_terminal_claimed(answer_text: str) -> bool:
    """True when a final_answer payload declares a semantic-terminal blocker
    (target_absent / instruction_infeasible) — structured blockers JSON in
    any accepted phrasing, with a natural-language fallback because the
    answer field is schema-unconstrained."""
    text = str(answer_text or "")
    try:
        payload = json.loads(text)
    except (TypeError, json.JSONDecodeError):
        payload = None
    if isinstance(payload, dict):
        blockers = payload.get("blockers")
        if isinstance(blockers, list):
            for blocker in blockers:
                if not isinstance(blocker, dict):
                    continue
                raw = blocker.get("classification")
                if isinstance(raw, dict):
                    category = str(raw.get("category") or "").strip()
                else:
                    category = str(
                        raw or blocker.get("category") or blocker.get("type") or ""
                    ).strip()
                if category in SEMANTIC_TERMINAL_CATEGORIES:
                    return True
        # Structured payloads may still phrase absence in prose fields.
        return _nl_absence_claimed(text)
    return _nl_absence_claimed(text)


def cites_ledger_evidence(
    answer_text: str,
    ledger_paths: Collection[str],
) -> bool:
    """True when the final_answer cites at least one evidenceArtifacts path
    that the harness itself persisted (ledger membership — same trust model
    as the spawner evidence gate's B3 check)."""
    ledger = {
        os.path.normpath(str(path).strip())
        for path in (ledger_paths or [])
        if str(path).strip()
    }
    if not ledger:
        return False
    try:
        payload = json.loads(str(answer_text or ""))
    except (TypeError, json.JSONDecodeError):
        return False
    if not isinstance(payload, dict):
        return False
    cited: List[str] = []
    sources: List[Any] = [payload.get("evidenceArtifacts")]
    blockers = payload.get("blockers")
    if isinstance(blockers, list):
        for blocker in blockers:
            if isinstance(blocker, dict):
                sources.append(blocker.get("evidenceArtifacts"))
                # Same acceptance surface as the spawner B3 gate, which also
                # reads classification.evidenceArtifacts — a legal phrasing
                # there must not be bounced here.
                classification = blocker.get("classification")
                if isinstance(classification, dict):
                    sources.append(classification.get("evidenceArtifacts"))
    for source in sources:
        if isinstance(source, list):
            cited.extend(str(item).strip() for item in source if str(item).strip())
    return any(os.path.normpath(path) in ledger for path in cited)


def build_reality_check_row(
    *,
    claim: str,
    verdict: JsonDict,
    trigger_tool: str,
    shortfall_streak: int,
    page_id: str,
    armed_by: str = "target_shortfall",
    stall_turns: Any = None,
    page_url: str = "",
    row_key: str = "",
    region: Any = None,
    capture: Any = None,
    coverage: Any = None,
    reconciled: Any = None,
    grading: Any = None,
) -> JsonDict:
    """Shape the persisted observation row (goes through record_extraction so
    the savedPath lands in the harness ledger and satisfies the evidence
    gate's B3 check when cited in evidenceArtifacts).

    The row states its own standing. `originClass` and `evidenceGrade` travel
    with the observation so anything that later reads this artifact — the Lead,
    the auditor, a repair pass — sees a model assertion labelled as one, rather
    than a harness-persisted file that looks like a measurement because of
    where it lives.
    """
    row: JsonDict = {
        "kind": "vl_reality_check",
        "claim": claim[:500],
        "verdict": str(verdict.get("verdict") or verdict.get("status") or ""),
        "observation": str(
            verdict.get("observation")
            or verdict.get("reason")
            or verdict.get("answer")
            or ""
        )[:1000],
        "screenshotPath": str(verdict.get("screenshotPath") or ""),
        "screenshotScope": str(verdict.get("screenshotScope") or ""),
        "triggerTool": trigger_tool,
        "targetShortfallStreak": shortfall_streak,
        # Which predicament armed the check. A verdict reached because the
        # worker was producing nothing at all is read differently from one
        # reached because its rows kept missing the target.
        "armedBy": armed_by or "target_shortfall",
        "pageUrl": str(page_url or verdict.get("pageUrl") or ""),
        "pageId": page_id,
        "sourceTool": "visual_verify",
        "originClass": "model_assertion",
        "evidenceGrade": "advisory",
        "mayTerminate": False,
    }
    if isinstance(stall_turns, int) and not isinstance(stall_turns, bool):
        row["turnsSinceArtifactProgress"] = stall_turns
    if row_key:
        row["rowKey"] = str(row_key)[:300]
        row["claimScope"] = "row"
    else:
        row["claimScope"] = "page"
    if isinstance(region, dict) and region:
        row["region"] = region
    if isinstance(verdict.get("itemCount"), int):
        row["itemCount"] = verdict["itemCount"]
    if isinstance(capture, dict) and capture:
        row["regionInCapture"] = str(capture.get("state") or "")
        row["regionInCaptureReason"] = str(capture.get("reason") or "")
    if isinstance(coverage, dict) and coverage.get("available"):
        row["scrollReceipt"] = {
            key: coverage.get(key)
            for key in (
                "mode", "completedReason", "targetVisible",
                "delta", "position", "extent", "steps",
            )
            if coverage.get(key) is not None
        }
    if isinstance(reconciled, dict) and reconciled:
        row["verdictClass"] = str(reconciled.get("class") or "")
        if reconciled.get("overridden"):
            row["claimedClass"] = str(reconciled.get("claimedClass") or "")
            row["overrideReason"] = str(reconciled.get("overrideReason") or "")
    if isinstance(grading, dict) and grading:
        row["evidenceGrade"] = str(grading.get("grade") or "advisory")
        row["mayTerminate"] = bool(grading.get("mayTerminate"))
        row["gradeReason"] = str(grading.get("reason") or "")
    return row
