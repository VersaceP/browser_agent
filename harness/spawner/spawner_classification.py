"""
harness.spawner.spawner_classification - Worker feedback classification and evidence helpers.
"""

import json
import os
import re
from dataclasses import field
from pathlib import Path
from typing import Any
from typing import List
from typing import Optional
from typing import Set
from harness.constants import COLLECTION_CONTRACT_REPLAN_REQUIRED
from harness.results.call_outcome import classify_call_outcome
from harness.fleet.auth import canonical_origin
from harness.results.row_ledger import identity_fields_from_contract
from harness.schema_loader import CapabilityBundle
from harness.utils import JsonDict
from .spawner_helpers import BrowserAgentSlot, URL_RE  # noqa: F401

def _sp():
    import harness.spawner as sp

    return sp

CHALLENGE_PHASE_STATUSES = frozenset({
    "blocked_by_challenge",
    "hitl_required",
    "hitl_timeout",
    "page_settled_after_hitl",
    "stale_pause_deadlock",
    "session_fleet_lost",
    "fleet_assignment_lost",
})

def phase_result_status_for(result: JsonDict) -> str:
    """Map a worker result to the status recorded against its phase.

    A challenge/HITL status normally freezes the phase ("do not retry without
    user action"). But when the worker recovered and its artifacts passed
    validation, the phase contract IS fulfilled — marking it as a challenge
    blocker would hide a validated success (observed in task 3b346d7e:
    browser-001 hit a stale-pause deadlock, recovered on a fresh page, and
    delivered the full validated phase-1 artifact).
    """
    worker_status = str(result.get("status") or "unknown")
    validation = result.get("artifactValidation")
    validation_done = (
        isinstance(validation, dict) and validation.get("status") == "done"
    )
    if worker_status in CHALLENGE_PHASE_STATUSES and not validation_done:
        return worker_status
    return str(result.get("validatedStatus") or result.get("status") or "unknown")

def _safe_str_list(value: Any) -> List[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if item is not None]

def _origins_from_text(text: str) -> Set[str]:
    origins: Set[str] = set()
    for match in URL_RE.findall(str(text or "")):
        origin = _origin_from_url(match.rstrip(".,);]"))
        if origin:
            origins.add(origin)
    return origins

def _origin_from_url(url: str) -> str:
    return canonical_origin(url)

def _phase_family(phase_id: Optional[str]) -> str:
    text = str(phase_id or "").strip()
    if not text:
        return ""
    # phase_2a, phase_2b, phase_2c should prefer the same reusable slot.
    return re.sub(r"(?<=\d)[a-z]$", "", text)

def _page_hidden_from_reuse(slot: BrowserAgentSlot, page: JsonDict) -> bool:
    page_id = str(page.get("pageId") or "").strip()
    if page_id and page_id in slot.page_quarantine:
        return True
    status = str(page.get("status") or "").strip().lower()
    if status in {"stale", "quarantined", "stale_pause_deadlock"}:
        return True
    url = str(page.get("url") or "").strip().lower()
    url = url.split("#", 1)[0].split("?", 1)[0]
    if (
        url == "about:blank"
        or url.startswith("chrome://")
        or url.endswith("/newtab.html")
        or url.endswith("://newtab.html")
    ):
        return True
    return bool(page.get("doNotUse"))

def _text_indicates_paused_error(text: Any) -> bool:
    lowered = str(text or "").lower()
    return "err_page_paused" in lowered or "paused for human intervention" in lowered

def _state_response_indicates_paused(value: Any) -> bool:
    if _text_indicates_paused_error(value):
        return True
    if isinstance(value, dict):
        data = value.get("data")
        if isinstance(data, dict):
            if data.get("paused") is True:
                return True
            status = str(data.get("status") or "").strip().lower()
            if status == "paused":
                return True
            hitl = data.get("hitl")
            if isinstance(hitl, dict) and hitl.get("isPaused") is True:
                return True
        response = value.get("response")
        if isinstance(response, dict) and _state_response_indicates_paused(response):
            return True
    return False

def _worker_feedback_classification(
    trace: List[JsonDict],
    answer: str,
    persisted_artifacts: Optional[List[str]] = None,
) -> Optional[JsonDict]:
    """Recover route-relevant classifications from worker feedback.

    Contract/tool-policy blockers are first surfaced as ordinary tool results
    inside the BrowserAgent loop. If the worker later finalizes cleanly or runs
    out of steps without a matching artifact, validation would otherwise report
    only data_missing. Preserve the more useful routing classification.
    """
    trace_classification = _classification_from_contract_violation(trace)
    if trace_classification is not None:
        return trace_classification
    collection_contract = _classification_from_collection_contract_preflight(trace)
    if collection_contract is not None:
        return collection_contract
    browser_call_classification = _classification_from_browser_call(trace)
    if browser_call_classification is not None:
        return browser_call_classification
    return _classification_from_final_answer(
        answer,
        persisted_artifacts=persisted_artifacts,
        traversal=_page_traversal_evidence(trace),
    )

def _classification_from_collection_contract_preflight(
    trace: List[JsonDict],
) -> Optional[JsonDict]:
    """Recover immutable-plan defects mechanically from collect_items trace."""
    for item in reversed(trace or []):
        if not isinstance(item, dict) or item.get("type") != "collect_items":
            continue
        result = item.get("result")
        if not isinstance(result, dict):
            continue
        classification = result.get("classification")
        if not isinstance(classification, dict):
            continue
        if str(classification.get("category") or "").strip() != (
            COLLECTION_CONTRACT_REPLAN_REQUIRED
        ):
            continue
        recovered = dict(classification)
        recovered.setdefault(
            "hint",
            "LeadAgent must replan expected_artifact with a nested array field.",
        )
        recovered["source"] = "collect_items.contractPreflight"
        return recovered
    return None

def _classification_from_browser_call(
    trace: List[JsonDict],
) -> Optional[JsonDict]:
    for item in reversed(trace or []):
        if not isinstance(item, dict) or item.get("type") != "browser_call":
            continue
        result = item.get("result")
        if not isinstance(result, dict):
            continue
        classification = result.get("classification")
        if isinstance(classification, dict):
            category = str(classification.get("category") or "").strip()
            if category == "blocked_infrastructure":
                recovered = dict(classification)
                recovered.setdefault(
                    "hint",
                    "Browser infrastructure failed; rebuild page/fleet or reconnect the Browser Client before retrying.",
                )
                recovered["source"] = "browser_call.classification"
                return recovered
        error_classification = result.get("errorClassification")
        if not isinstance(error_classification, dict):
            continue
        error_type = str(error_classification.get("type") or "").strip()
        if error_type != "browser_unavailable_or_no_page":
            continue
        return {
            "category": "blocked_infrastructure",
            "type": error_type,
            "method": result.get("method") or "Page.create",
            "hint": (
                "Page.create failed with -32005 and no usable existing page was"
                " found."
            ),
            "source": "browser_call.errorClassification",
        }
    return None

def _classification_from_contract_violation(
    trace: List[JsonDict],
) -> Optional[JsonDict]:
    for item in reversed(trace or []):
        if not isinstance(item, dict) or item.get("type") != "contract_violation":
            continue
        result = item.get("result")
        if not isinstance(result, dict):
            continue
        classification = result.get("classification")
        if not isinstance(classification, dict):
            continue
        category = str(classification.get("category") or "").strip()
        if category != "blocked_cross_task_type_required":
            continue
        recovered = dict(classification)
        recovered["source"] = "contract_violation"
        return recovered
    return None

def _blocker_evidence_paths(
    blocker: JsonDict,
    classification: JsonDict,
) -> List[str]:
    raw = blocker.get("evidenceArtifacts")
    if not isinstance(raw, list):
        raw = classification.get("evidenceArtifacts")
    if not isinstance(raw, list):
        return []
    return [str(item).strip() for item in raw if str(item).strip()]

def _cohort_identity_fields(
    worker_contract: JsonDict, phase: Optional[JsonDict] = None,
) -> List[str]:
    """The field(s) that name a row, from whichever shape this contract uses.

    Delegates to the single resolver in harness.results.row_ledger: this lookup had
    already been re-implemented three times with a different set of shapes
    each time, and a missed shape is silent — no identity, every rowKey None,
    per-row attribution quietly gone.
    """
    return identity_fields_from_contract(worker_contract, phase)

def _validated_rows_for_ledger(validation: JsonDict) -> List[JsonDict]:
    paths = validation.get("validExtractionArtifacts")
    if not isinstance(paths, list) or not paths:
        paths = validation.get("allExtractionArtifacts")
    rows: List[JsonDict] = []
    for raw_path in paths if isinstance(paths, list) else []:
        try:
            payload = json.loads(Path(str(raw_path)).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, ValueError):
            continue
        for row in (payload.get("rows") if isinstance(payload, dict) else None) or []:
            if isinstance(row, dict):
                rows.append(row)
    return rows

def _allowance_from_validators(validators: Any) -> JsonDict:
    """Merge every emptiable-field declaration on the phase.

    Dedup only merges validators that share a semantic signature (type+fields),
    so two field_nonempty validators covering different fields both survive to
    here. Returning the first one dropped the second's allowance and pinned the
    phase on a field it had explicitly declared emptiable. Outcomes for a field
    named twice are unioned rather than overwritten.
    """
    merged: JsonDict = {}
    for validator in validators if isinstance(validators, list) else []:
        if (
            not isinstance(validator, dict)
            or str(validator.get("type") or "") != "field_nonempty"
            or not isinstance(validator.get("allow_empty_with_outcome"), dict)
        ):
            continue
        for field, outcomes in validator["allow_empty_with_outcome"].items():
            values = outcomes if isinstance(outcomes, list) else [outcomes]
            existing = merged.setdefault(str(field), [])
            for outcome in values:
                text = str(outcome or "").strip()
                if text and text not in existing:
                    existing.append(text)
    return merged

def _scroll_receipt_data(result: JsonDict) -> Optional[JsonDict]:
    response = result.get("response")
    if not isinstance(response, dict):
        return None
    data = response.get("data")
    return data if isinstance(data, dict) else None

def _scroll_was_state_probe(result: JsonDict) -> bool:
    """True when the receipt says no wheel input was dispatched at all.

    `amount: 0` reads the scroll state without moving anything, so such a call
    is neither a traversal nor a failed traversal — counting it either way
    corrupts the ledger that guards `target_absent`.
    """
    data = _scroll_receipt_data(result)
    if data is None:
        return False
    return str(data.get("completedReason") or "") == "amount-zero"

def _axis_magnitude(value: Any) -> Optional[float]:
    """Largest absolute axis component of a `{x, y}` delta, or None."""
    if not isinstance(value, dict):
        return None
    magnitude: Optional[float] = None
    for axis in ("x", "y"):
        raw = value.get(axis)
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            continue
        magnitude = max(magnitude or 0.0, abs(float(raw)))
    return magnitude

def _scroll_delta_applied(result: JsonDict) -> Optional[float]:
    """Pixels an `Input.scroll` actually moved, or None when unreported.

    None and 0 must stay distinct: None means this platform build ships no
    scroll receipt, while 0 is a positive report that the page did not move.

    Two receipt shapes are accepted on purpose. Current builds return
    `AbcpScrollActionResult` with a `totalDelta {x, y}` plus per-surface
    `layers[].delta`; older builds returned a scalar `deltaApplied`. Reading
    only one of them silently degrades `scrollEffectEvidence` to "unavailable"
    on the other build, which is exactly how a wheel event that travelled zero
    pixels gets to look like a real traversal.
    """
    data = _scroll_receipt_data(result)
    if data is None:
        return None

    total = _axis_magnitude(data.get("totalDelta"))
    if total is not None:
        return total

    layers = data.get("layers")
    if isinstance(layers, list):
        magnitudes = [
            magnitude
            for layer in layers
            if isinstance(layer, dict)
            and (magnitude := _axis_magnitude(layer.get("delta"))) is not None
        ]
        if magnitudes:
            return max(magnitudes)

    if "deltaApplied" not in data:
        return None
    raw = data.get("deltaApplied")
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return None
    return abs(float(raw))

def _page_traversal_evidence(trace: Optional[List[JsonDict]]) -> JsonDict:
    """Did this worker ever move past the first screenful?

    A `target_absent` report can be perfectly self-consistent and still be
    wrong: the upstream batch says `[]`, the worker enumerates what it can see,
    a screenshot confirms the target is not there, confidence is high — and the
    viewport never moved, so every one of those observations describes the same
    screenful. No judge can catch that, because nothing in the report is false;
    the missing piece is a mechanical fact about what WE did, which only our
    own ledger holds.

    Two signals count, both decidable from the trace:
      * an `Input.scroll` that moved the page — we asked, and the page obeyed
      * a collection exhaustion proof — we enumerated a container to its end

    "Moved the page" needs the platform's scroll receipt, because a wheel event
    dispatched into a page that ignores it still returns a success envelope: a
    worker can scroll three times, travel zero pixels, and look fully traversed.
    When `deltaApplied` is present it decides the question; builds that do not
    report it leave `scrollEffectEvidence="unavailable"` and keep the older,
    weaker reading, so this gate never fabricates evidence in either direction.

    The two branches below verify success DIFFERENTLY on purpose, and the
    asymmetry is load-bearing rather than an oversight. `classify_call_outcome`
    reads the native ABCP envelope (`result.response` + executionId/observation
    /data) and is fail-closed: absent that envelope it reports FAILED. A
    composite such as collect_items returns a FLAT result with no `response`
    key at all, so routing the exhaustion branch through the same classifier
    would score every real proof as a failure and silently zero out traversal
    evidence for every collection-based claim. The composite instead proves
    itself: its failure paths return `{"status": "failed", ...}` and never
    construct an exhaustionEvidence block, so a non-empty `kind` is already
    positive evidence that the enumeration ran.
    """
    scrolls = 0
    scrolls_without_effect = 0
    scroll_effect_evidence = "unavailable"
    exhaustion_proofs = 0
    for item in trace or []:
        if not isinstance(item, dict):
            continue
        result = item.get("result")
        if not isinstance(result, dict):
            continue
        if str(item.get("method") or "") == "Input.scroll":
            if not classify_call_outcome(result).succeeded:
                continue
            if _scroll_was_state_probe(result):
                # A zero-amount state read dispatched no input; it is neither
                # traversal nor a failure to traverse.
                continue
            delta = _scroll_delta_applied(result)
            if delta is None:
                # No scroll receipt in this ABCP build. The success envelope
                # proves only that the wheel event was dispatched, so keep
                # counting it and record that the effect is unproven rather
                # than inventing evidence either way.
                scrolls += 1
                continue
            scroll_effect_evidence = "receipt"
            if delta > 0:
                scrolls += 1
            else:
                scrolls_without_effect += 1
            continue
        evidence = result.get("exhaustionEvidence")
        if isinstance(evidence, dict) and str(evidence.get("kind") or "").strip():
            exhaustion_proofs += 1
    return {
        "scrolls": scrolls,
        "scrollsWithoutEffect": scrolls_without_effect,
        "scrollEffectEvidence": scroll_effect_evidence,
        "exhaustionProofs": exhaustion_proofs,
        "traversed": bool(scrolls or exhaustion_proofs),
    }

def _semantic_terminal_counterevidence(
    category: str,
    blocker: JsonDict,
    classification: JsonDict,
    persisted_artifacts: Optional[List[str]],
    traversal: Optional[JsonDict] = None,
) -> Optional[JsonDict]:
    """Return what our own receipts say against a semantic-terminal claim.

    The worker's category is its own to declare and is never rewritten here:
    whether the evidence suffices to prove absence is a semantic reading, and
    the model that gathered the evidence is the one to make it. What this
    returns is the other half of the record. A `target_absent` report can be
    perfectly self-consistent and still be wrong — the upstream batch says
    `[]`, the worker enumerates what it can see, a screenshot agrees, and the
    viewport never moved, so every observation describes the same screenful.
    Nothing in the report is false, so no reader can catch it from the report
    alone; only our ledger holds the missing fact.

    Returns None when the ledger has nothing to add.
    """
    reason_text = str(
        blocker.get("reason")
        or classification.get("reason")
        or blocker.get("message")
        or blocker.get("detail")
        or ""
    ).strip()
    facts: JsonDict = {
        "observation": "semantic_terminal_counterevidence",
        "claimedCategory": category,
        "reasonTextLength": len(reason_text),
        "note": (
            "Attributed facts from this run's own receipts, not a verdict on"
            " the claim."
        ),
    }
    if not reason_text:
        return {**facts, "findings": ["blocker carries no reason text"]}
    if (
        category == "target_absent"
        and isinstance(traversal, dict)
        and not traversal.get("traversed")
    ):
        # Checked ahead of the artifact ledger on purpose: a persisted artifact
        # proves we saved what we saw, never that we looked past the fold.
        try:
            ignored_scrolls = int(traversal.get("scrollsWithoutEffect") or 0)
        except (TypeError, ValueError):
            ignored_scrolls = 0
        finding = (
            f"{ignored_scrolls} scroll(s) were dispatched and the page did not"
            " move; no collection was enumerated to exhaustion"
            if ignored_scrolls
            # Distinguishable from the above only with a scroll receipt, and
            # the difference matters: one says the page refused, the other
            # says we never asked.
            else (
                "the page was never scrolled and no collection was enumerated"
                " to exhaustion"
            )
        )
        return {
            **facts,
            "findings": [finding],
            "pageTraversal": dict(traversal),
        }
    evidence_paths = _blocker_evidence_paths(blocker, classification)
    ledger = {
        os.path.normpath(str(path).strip())
        for path in (persisted_artifacts or [])
        if str(path).strip()
    }
    matched = [
        path for path in evidence_paths if os.path.normpath(path) in ledger
    ]
    if matched:
        if _only_visual_check_evidence(matched):
            # The artifact is real and ledger-bound, and still proves nothing:
            # it holds a model's reading of a screenshot. Task 5324506f turned
            # one page's overlay into "the site requires login for reviews"
            # on exactly this evidence.
            return {
                **facts,
                "findings": [
                    "every cited artifact is a visual reality check, which"
                    " records a model's reading of one screenshot rather than"
                    " a measurement"
                ],
                "citedEvidenceArtifacts": list(evidence_paths),
                "ledgerMatchedArtifacts": list(matched),
            }
        return None
    if category == "instruction_infeasible":
        # Infeasibility often has nothing extractable to persist — the site can
        # lack the requested concept entirely — which makes a missing artifact
        # unremarkable rather than damning. That is a reading of the claim, and
        # it used to be made here by measuring the reason text against a
        # 40-character floor: 40 characters "stood on its own" and 39 did not.
        # The length is a fact and travels as one; what it is worth is not this
        # function's call.
        return {
            **facts,
            "findings": [
                "no evidenceArtifacts entry matches a record_extraction"
                " savedPath from this run"
            ],
            "citedEvidenceArtifacts": list(evidence_paths),
        }
    return {
        **facts,
        "findings": [
            "no evidenceArtifacts listed"
            if not evidence_paths
            else (
                "no evidenceArtifacts entry matches a record_extraction"
                " savedPath from this run"
            )
        ],
        "citedEvidenceArtifacts": list(evidence_paths),
    }

def _only_visual_check_evidence(paths: List[str]) -> bool:
    """True when every cited artifact is a visual reality check and nothing else.

    Deliberately does NOT read the row's own `evidenceGrade`. That field is
    written into an artifact, and artifacts are written by record_extraction,
    which a worker can call with any rows it likes — so trusting the grade let
    a worker mint `{"kind": "vl_reality_check", "evidenceGrade":
    "corroborating"}` and walk past this gate. The grade is not needed anyway:
    `evidence_grade` returns mayTerminate=False in every mode, so a visual
    verdict is never sufficient on its own regardless of how well the model
    scores. Precision buys corroboration, not authority to end work.

    Fails OPEN on an unreadable or unrecognized artifact: this function exists
    to catch a specific, self-labelled artifact kind, and a file we cannot
    parse must not become a reason to reject a blocker that may be perfectly
    well evidenced.
    """
    if not paths:
        return False
    for raw_path in paths:
        try:
            payload = json.loads(Path(str(raw_path)).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, ValueError):
            return False
        rows = payload.get("rows") if isinstance(payload, dict) else None
        if not isinstance(rows, list) or not rows:
            return False
        for row in rows:
            if not isinstance(row, dict):
                return False
            if str(row.get("kind") or "") != "vl_reality_check":
                return False
    return True

def _classification_from_final_answer(
    answer: str,
    persisted_artifacts: Optional[List[str]] = None,
    traversal: Optional[JsonDict] = None,
) -> Optional[JsonDict]:
    try:
        payload = json.loads(str(answer or ""))
    except (TypeError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    blockers = payload.get("blockers")
    if not isinstance(blockers, list):
        return None
    for blocker in blockers:
        if not isinstance(blocker, dict):
            continue
        raw_classification = blocker.get("classification")
        if isinstance(raw_classification, dict):
            category = str(raw_classification.get("category") or "").strip()
            classification = dict(raw_classification)
        else:
            # Models phrase the blocker several ways; accept a top-level
            # "category" key too so a semantic-terminal report is not
            # silently dropped back into the retry loop.
            category = str(
                raw_classification
                or blocker.get("category")
                or blocker.get("type")
                or ""
            ).strip()
            classification = {"category": category}
        if category not in {
            "blocked_cross_task_type_required",
            "blocked_infrastructure",
            COLLECTION_CONTRACT_REPLAN_REQUIRED,
            "target_absent",
            "instruction_infeasible",
            "route_sensitive_content_suppression",
            "blocked_content_suppression",
        }:
            continue
        if category in {"target_absent", "instruction_infeasible"}:
            counterevidence = _semantic_terminal_counterevidence(
                category,
                blocker,
                classification,
                persisted_artifacts,
                traversal=traversal,
            )
            if counterevidence is not None:
                if isinstance(traversal, dict):
                    classification["pageTraversal"] = dict(traversal)
                # The claim stays the worker's own. Our receipts are appended
                # beside it so the Lead reads both and decides; rewriting the
                # category here would be this harness judging a page's
                # behaviour, which is the model's call to make.
                classification["counterevidence"] = counterevidence
                findings = "; ".join(
                    str(item) for item in counterevidence.get("findings") or []
                )
                classification.setdefault("hint", (
                    f"Worker claimed {category}. Counterevidence from this"
                    f" run's own receipts: {findings}."
                )[:500])
                classification["source"] = "final_answer.blockers"
                return classification
        # The worker's own words are its claim and travel verbatim. Where it
        # gave none, this used to substitute a per-category directive
        # ("LeadAgent should stop retrying…", "…should replan…"); that is the
        # harness picking the next move from a category name, with none of the
        # run's evidence in front of it. The category and the receipts beside
        # it are the record; reading them is the Lead's job.
        hint = (
            blocker.get("hint")
            or blocker.get("message")
            or blocker.get("reason")
        )
        if hint:
            classification.setdefault("hint", str(hint)[:500])
        if blocker.get("method"):
            classification.setdefault("method", blocker.get("method"))
        if blocker.get("task_type"):
            classification.setdefault("task_type", blocker.get("task_type"))
        if blocker.get("field"):
            classification.setdefault("field", blocker.get("field"))
        if isinstance(blocker.get("expectedShape"), dict):
            classification.setdefault("expectedShape", blocker.get("expectedShape"))
        classification["source"] = "final_answer.blockers"
        return classification
    return None

def _clone_capability_bundle(bundle: CapabilityBundle) -> CapabilityBundle:
    return CapabilityBundle(
        capabilities=list(bundle.capabilities),
        capability_methods=set(bundle.capability_methods),
        method_schemas=dict(bundle.method_schemas),
        methods_requiring_purpose=set(bundle.methods_requiring_purpose),
        purpose_hints=dict(bundle.purpose_hints),
        skills_doc=bundle.skills_doc,
    )
