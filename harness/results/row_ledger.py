"""Per-row, per-field outcome and cause: what happened to each item, mechanically.

The failure this closes: task 5324506f reported "评论需要登录" for three
products. One had shown a login modal after a "View more" click; the other two
had simply run out of worker steps before their reviews were read. The run held
both facts — a step-budget receipt for the latter two, an overlay receipt for
the first — but nowhere did it hold them PER ROW, so the Lead's single narrative
covered all three and no check could contradict it.

Two things live here, and they are deliberately different in kind:

  * `absence_proof` decides whether a declared "there is nothing here" carries
    the evidence that claim requires. An empty field is not evidence of
    absence; a zero count on a region that never rendered is not either. The
    obligations are declaration-driven — the caller says which region, which
    selector, which epoch — so nothing here knows anything about any site.
  * `derive_row_ledger` reads the receipts and says, per row and field, which
    outcome occurred and which mechanical cause explains it. Causes come from
    receipts only. A Lead may describe a row, but it may not rename what the
    ledger recorded about it.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional
from urllib.parse import urlsplit

from harness.utils import JsonDict

# What became of one field on one row.
OUTCOME_VALUE_FOUND = "value_found"
OUTCOME_OBSERVED_EMPTY = "observed_empty"
OUTCOME_CONFIRMED_ABSENT = "confirmed_absent"
OUTCOME_BLOCKED = "blocked"
OUTCOME_UNRESOLVED = "unresolved"
ROW_OUTCOMES = frozenset({
    OUTCOME_VALUE_FOUND,
    OUTCOME_OBSERVED_EMPTY,
    OUTCOME_CONFIRMED_ABSENT,
    OUTCOME_BLOCKED,
    OUTCOME_UNRESOLVED,
})

# Why, in terms a receipt can prove. `none` belongs with value_found.
CAUSE_NONE = "none"
CAUSE_STEP_BUDGET = "step_budget_exhausted"
CAUSE_AUTH_PROMPT = "auth_prompt_observed"
CAUSE_BLOCKED_AFTER_LADDER = "blocked_after_ladder"
CAUSE_MATERIALIZATION_STALLED = "materialization_stalled"
CAUSE_SELECTOR_UNVERIFIED = "selector_unverified"
CAUSE_NAVIGATION_FAILED = "navigation_failed"
CAUSE_TOOL_FAILED = "tool_failed"
CAUSE_CONTRACT_CONFLICT = "contract_conflict"
ROW_CAUSES = frozenset({
    CAUSE_NONE,
    CAUSE_STEP_BUDGET,
    CAUSE_AUTH_PROMPT,
    CAUSE_BLOCKED_AFTER_LADDER,
    CAUSE_MATERIALIZATION_STALLED,
    CAUSE_SELECTOR_UNVERIFIED,
    CAUSE_NAVIGATION_FAILED,
    CAUSE_TOOL_FAILED,
    CAUSE_CONTRACT_CONFLICT,
})

# Suffix a row uses to attach an absence declaration to field <name>.
ABSENCE_SUFFIX = "Absence"

# Each obligation maps to the recovery action that would discharge it, so a
# rejected absence tells the worker what to DO rather than only what is wrong.
_OBLIGATION_ACTIONS = {
    "current_epoch_region_materialized": "materialize_region",
    "overlay_clear": "clear_overlay_then_reobserve",
    "target_collection_exhausted": "enumerate_target_collection",
    "selector_calibrated": "calibrate_selector_against_positive_peer",
    "source_provenance": "record_source_tool_and_selector",
    "positive_evidence_text": "capture_evidence_text_for_the_empty_region",
    "navigation_epoch_bound": "bind_observation_to_current_navigation_epoch",
    "outcome_declared_absent": "declare_outcome_confirmed_absent",
}


def absence_field_name(field: str) -> str:
    return f"{field}{ABSENCE_SUFFIX}"


def _truthy(value: Any) -> bool:
    return value is True


def _nonempty_text(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _positive_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def absence_proof(declaration: Any, *, region_id: str = "") -> JsonDict:
    """Judge one declared absence against its evidence obligations.

    Returns `state: "complete"` only when every obligation is discharged. An
    incomplete proof is reported as `satisfiable: True` with the obligations
    still outstanding: "we have not proved this yet" is a different statement
    from "this can never be proved", and collapsing them turns a page we simply
    have not finished reading into a permanent verdict about the site.
    """
    data = declaration if isinstance(declaration, dict) else {}
    outcome = str(data.get("outcome") or "").strip()
    checks = {
        "current_epoch_region_materialized": _truthy(data.get("regionMaterialized")),
        "overlay_clear": _truthy(data.get("overlayClear")),
        "target_collection_exhausted": _truthy(data.get("enumerationExhausted")),
        "selector_calibrated": _nonempty_text(data.get("selectorCalibratedBy")),
        "source_provenance": (
            _nonempty_text(data.get("sourceTool"))
            and _nonempty_text(data.get("sourceSelectorOrAxId"))
        ),
        "positive_evidence_text": _nonempty_text(data.get("evidenceText")),
        # Epoch 0 is the pre-navigation state derive_row_facts starts every row
        # at, so a proof bound to it is bound to nothing. Requiring a positive
        # epoch is what makes "in the CURRENT page epoch" mean anything.
        "navigation_epoch_bound": _positive_int(data.get("navigationEpoch")),
        # Saying it out loud is part of the proof. Without this a declaration
        # that never names an outcome collects all seven flags and is read as
        # confirmed_absent by default — absence granted for not asking.
        "outcome_declared_absent": outcome == OUTCOME_CONFIRMED_ABSENT,
    }
    missing = [name for name, satisfied in checks.items() if not satisfied]
    if outcome and outcome != OUTCOME_CONFIRMED_ABSENT:
        missing.append("outcome_must_be_confirmed_absent")
    proof: JsonDict = {
        "state": "complete" if not missing else "incomplete",
        "satisfiable": True,
        "missingObligations": missing,
        "recommendedActions": [
            f"{_OBLIGATION_ACTIONS[name]}:{region_id}" if region_id
            else _OBLIGATION_ACTIONS[name]
            for name in missing
            if name in _OBLIGATION_ACTIONS
        ],
    }
    if region_id:
        proof["regionId"] = region_id
    return proof


def field_absence_accepted(
    row: Any, field: str, *, allowed_outcomes: Any = None,
) -> JsonDict:
    """Whether row[field] may be empty because its absence is proven.

    `allowed_outcomes` is the contract's `allow_empty_with_outcome` list. An
    empty field with no such allowance always fails: staying silent about which
    fields may legitimately be empty is what made a 16-row contract demand
    reviews from a product that has none.
    """
    allowed = {
        str(item).strip()
        for item in (allowed_outcomes or [])
        if str(item or "").strip()
    }
    if OUTCOME_CONFIRMED_ABSENT not in allowed:
        return {
            "accepted": False,
            "reason": "field_not_declared_emptiable",
            "field": field,
        }
    declaration = (row or {}).get(absence_field_name(field)) if isinstance(row, dict) else None
    if not isinstance(declaration, dict):
        return {
            "accepted": False,
            "reason": "absence_declaration_missing",
            "field": field,
            "expectedKey": absence_field_name(field),
        }
    proof = absence_proof(declaration, region_id=str(declaration.get("regionId") or ""))
    if proof["state"] != "complete":
        return {
            "accepted": False,
            "reason": "absence_proof_incomplete",
            "field": field,
            "absenceProof": proof,
        }
    return {"accepted": True, "field": field, "absenceProof": proof}


# Every shape a contract may use to say "this field names a row". The list is
# here, once, because getting it wrong is silent: a missed shape yields no
# identity, every rowKey comes back None, and the per-row machinery degrades to
# worker-level facts without anything reporting a problem. That failure has now
# been found three separate times — the cohort form, a phase that produces
# rather than consumes rows, and direct user-supplied batch_rows — each time in
# a different copy of the same lookup. One resolver, all shapes.
_IDENTITY_SOURCES = (
    "_source_cohort_receipt",
    "_execution_slice_receipt",
    "_batch_source_receipt",
    "batch_source",
    "cohort_source",
)
_IDENTITY_KEYS = ("identityField", "identity_field")
_ROW_KEY_FIELDS = ("selectedRowKeys", "rowKeys", "cohortRowKeys")


def identity_fields_from_contract(
    worker_contract: Any, phase: Any = None,
) -> List[str]:
    """The field(s) that name a row, from wherever this contract declares them."""
    contract = worker_contract if isinstance(worker_contract, dict) else {}
    for key in _IDENTITY_SOURCES:
        source = contract.get(key)
        if not isinstance(source, dict):
            continue
        for name in _IDENTITY_KEYS:
            field = str(source.get(name) or "").strip()
            if field:
                return [field]
        selector = source.get("selector")
        field = (
            str(selector.get("field") or "").strip()
            if isinstance(selector, dict) else ""
        )
        if field:
            return [field]

    # Direct user-supplied rows name their own identity in the provenance
    # declaration the spawn gate already verifies against the user's task.
    provenance = contract.get("batch_rows_provenance")
    if isinstance(provenance, dict):
        fields = [
            str(name).strip()
            for name in (provenance.get("identity_fields") or [])
            if str(name or "").strip()
        ]
        if fields:
            return fields

    # A phase that produces a cohort carries no source at all, but its `unique`
    # validator states exactly which fields may not repeat.
    validators = (phase or {}).get("validators") if isinstance(phase, dict) else None
    for validator in validators if isinstance(validators, list) else []:
        if not isinstance(validator, dict):
            continue
        if str(validator.get("type") or "") != "unique":
            continue
        fields = [
            str(name).strip()
            for name in (validator.get("fields") or [])
            if str(name or "").strip()
        ]
        if fields:
            return fields
    return []


def assigned_row_keys_from_contract(
    worker_contract: Any, phase: Any = None,
) -> List[str]:
    """Which rows this worker owns, however the contract expresses them.

    Receipts win when present (they are what the materializer actually
    selected); otherwise the keys are derived from the contract's own rows.
    """
    contract = worker_contract if isinstance(worker_contract, dict) else {}
    for key in _IDENTITY_SOURCES:
        source = contract.get(key)
        if not isinstance(source, dict):
            continue
        for field in _ROW_KEY_FIELDS:
            values = source.get(field)
            if isinstance(values, list) and values:
                return [str(item) for item in values if str(item or "").strip()]

    identity_fields = identity_fields_from_contract(contract, phase)
    if not identity_fields:
        return []
    keys: List[str] = []
    for rows_key in ("batch_rows", "skill_rows"):
        rows = contract.get(rows_key)
        for row in rows if isinstance(rows, list) else []:
            key = row_identity(row, identity_fields)
            if key and key not in keys:
                keys.append(key)
        if keys:
            break
    return keys


def row_identity(row: Any, identity_fields: List[str]) -> Optional[str]:
    if not isinstance(row, dict) or not identity_fields:
        return None
    parts: List[str] = []
    for field in identity_fields:
        value = row.get(field)
        if value is None or isinstance(value, (dict, list, bool)):
            return None
        text = str(value).strip()
        if not text:
            return None
        parts.append(text)
    return " | ".join(parts)


def _is_empty(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        return not value.strip()
    if isinstance(value, (list, dict)):
        return not value
    return False


def mechanical_causes(trace_facts: Any) -> Dict[str, Any]:
    """Normalize the receipt-derived facts a cause may be drawn from.

    Kept as an explicit projection so a caller cannot slip a model's opinion in
    beside the receipts: anything not in this shape is not a cause.
    """
    facts = trace_facts if isinstance(trace_facts, dict) else {}
    return {
        "budgetExhausted": bool(facts.get("budgetExhausted")),
        "overlayOutcome": str(facts.get("overlayOutcome") or ""),
        "materializationStalled": bool(facts.get("materializationStalled")),
        "navigationFailed": bool(facts.get("navigationFailed")),
        "toolFailed": bool(facts.get("toolFailed")),
        "selectorCalibrated": bool(facts.get("selectorCalibrated")),
        "navigationEpoch": facts.get("navigationEpoch"),
        "reached": bool(facts.get("reached", True)),
    }


def _cause_for_unresolved(facts: Dict[str, Any]) -> str:
    # Order matters: the most specific receipt wins, and a plain step-budget
    # miss must never be reported as an access problem. That substitution is
    # the exact error the Lead made for Syndie.io and Face Privacy.
    if facts["overlayOutcome"] == "blocked_after_ladder":
        return CAUSE_BLOCKED_AFTER_LADDER
    if facts["overlayOutcome"] == "auth_prompt_observed":
        return CAUSE_AUTH_PROMPT
    if facts["navigationFailed"]:
        return CAUSE_NAVIGATION_FAILED
    if facts["materializationStalled"]:
        return CAUSE_MATERIALIZATION_STALLED
    if facts["budgetExhausted"]:
        return CAUSE_STEP_BUDGET
    if facts["toolFailed"]:
        return CAUSE_TOOL_FAILED
    return CAUSE_NONE


def derive_field_entry(
    row: Any,
    field: str,
    *,
    allowed_outcomes: Any = None,
    trace_facts: Any = None,
) -> JsonDict:
    """Outcome and cause for one field of one row."""
    facts = mechanical_causes(trace_facts)
    entry: JsonDict = {"field": field}
    if facts["navigationEpoch"] is not None:
        entry["navigationEpoch"] = facts["navigationEpoch"]

    present = isinstance(row, dict) and field in row
    if not present:
        entry["outcome"] = OUTCOME_UNRESOLVED
        entry["cause"] = (
            CAUSE_STEP_BUDGET if facts["budgetExhausted"] and not facts["reached"]
            else _cause_for_unresolved(facts)
        )
        return entry

    value = row.get(field)
    if not _is_empty(value):
        entry["outcome"] = OUTCOME_VALUE_FOUND
        entry["cause"] = CAUSE_NONE
        if isinstance(value, list):
            entry["itemCount"] = len(value)
        return entry

    verdict = field_absence_accepted(row, field, allowed_outcomes=allowed_outcomes)
    if verdict["accepted"]:
        entry["outcome"] = OUTCOME_CONFIRMED_ABSENT
        entry["cause"] = CAUSE_NONE
        entry["absenceProof"] = verdict["absenceProof"]
        return entry

    cause = _cause_for_unresolved(facts)
    if cause in {CAUSE_BLOCKED_AFTER_LADDER, CAUSE_AUTH_PROMPT}:
        entry["outcome"] = OUTCOME_BLOCKED
    elif cause == CAUSE_NONE:
        # The extractor wrote the key and left it empty with nothing blocking
        # it and no proof attached: an observation, not yet a finding.
        entry["outcome"] = OUTCOME_OBSERVED_EMPTY
    else:
        entry["outcome"] = OUTCOME_UNRESOLVED
    entry["cause"] = cause
    if verdict.get("absenceProof"):
        entry["absenceProof"] = verdict["absenceProof"]
    elif verdict["reason"] == "absence_declaration_missing":
        entry["absenceProof"] = {
            "state": "incomplete",
            "satisfiable": True,
            "missingObligations": ["absence_declaration_missing"],
            "recommendedActions": [
                f"declare_{absence_field_name(field)}_with_evidence",
            ],
        }
    return entry


def derive_row_ledger(
    rows: Any,
    *,
    fields: List[str],
    identity_fields: List[str],
    allow_empty_with_outcome: Any = None,
    trace_facts: Any = None,
    row_facts: Any = None,
) -> List[JsonDict]:
    """Per-row outcome/cause entries for the fields a contract asks for.

    `row_facts` may carry per-row receipt facts (keyed by row identity) so a
    row the worker never reached is not tarred with another row's blocker.
    """
    allowance = allow_empty_with_outcome if isinstance(allow_empty_with_outcome, dict) else {}
    per_row = row_facts if isinstance(row_facts, dict) else {}
    entries: List[JsonDict] = []
    for index, row in enumerate(rows if isinstance(rows, list) else []):
        key = row_identity(row, identity_fields)
        facts = per_row.get(key) if key is not None else None
        field_entries: Dict[str, JsonDict] = {}
        for field in fields:
            field_entries[field] = derive_field_entry(
                row,
                field,
                allowed_outcomes=allowance.get(field),
                trace_facts=facts if facts is not None else trace_facts,
            )
        entries.append({
            "rowIndex": index,
            "rowKey": key,
            "fields": field_entries,
        })
    return entries


_NAVIGATION_METHODS = frozenset({"Page.navigate", "Page.reload", "Page.go"})
_NAVIGATION_TOOLS = frozenset({"navigate_verified"})


def _normalize_url(value: Any) -> str:
    text = str(value or "").strip().casefold()
    if not text:
        return ""
    for prefix in ("https://", "http://"):
        if text.startswith(prefix):
            text = text[len(prefix):]
            break
    if text.startswith("www."):
        text = text[4:]
    return text.rstrip("/")


def _navigation_target(item: JsonDict) -> str:
    params = item.get("params") if isinstance(item.get("params"), dict) else {}
    method = str(item.get("method") or "")
    name = str(item.get("name") or item.get("tool") or "")
    if method in _NAVIGATION_METHODS or name in _NAVIGATION_TOOLS:
        return str(params.get("url") or "")
    return ""


def _is_url_like(value: str) -> bool:
    """Whether a row key is a URL that may be compared against a navigation.

    The docstring below always claimed non-URL keys could not bind, but nothing
    enforced it, so a name-valued identity_field matched by containment:
    `row_key_for_url("https://x/ai/codedesign/", ["CodeDesign"])` returned
    CodeDesign, filing one product's receipts under whatever product name
    happened to appear in the path. An http(s) scheme with a host is required,
    and anything else keeps the worker-level facts — the conservative side.
    """
    try:
        parts = urlsplit(str(value or "").strip())
    except ValueError:
        return False
    return parts.scheme in {"http", "https"} and bool(parts.netloc)


def _row_key_for_url(url: str, row_keys: List[str]) -> Optional[str]:
    """Which assigned row this navigation belongs to.

    Matching is on normalized URLs only. A row key that is not a URL cannot be
    tied to a navigation, and guessing from partial text would attach one
    item's receipts to another — the precise confusion this ledger exists to
    prevent — so such rows keep the worker-level facts instead.
    """
    if not _is_url_like(url):
        return None
    target = _normalize_url(url)
    if not target:
        return None
    best: Optional[str] = None
    for key in row_keys:
        if not _is_url_like(key):
            continue
        candidate = _normalize_url(key)
        if not candidate:
            continue
        if candidate == target:
            return key
        if candidate in target or target in candidate:
            if best is not None:
                return None  # ambiguous prefix: refuse rather than mis-attribute
            best = key
    return best


def row_key_for_url(url: str, row_keys: List[str]) -> Optional[str]:
    """Public entry to the row/URL matcher.

    Exported so the visual reality check scopes its question to the same row
    this ledger will later record an outcome for. Two components deciding "which
    item is this page?" by different rules is how a verdict about one product
    ends up filed against another.
    """
    return _row_key_for_url(url, row_keys)


def _overlay_outcome(result: Any) -> str:
    if not isinstance(result, dict):
        return ""
    status = str(result.get("status") or "")
    if status in {"policy_refused", "failed"}:
        return (
            "blocked_after_ladder"
            if str(result.get("dismissOutcome") or "") == "blocked_after_ladder"
            else "auth_prompt_observed"
        )
    return ""


def derive_row_facts(
    trace: Any,
    *,
    row_keys: List[str],
    budget_exhausted: bool = False,
) -> Dict[str, JsonDict]:
    """Attribute receipts to the assigned row whose page produced them.

    Navigation segments the trace: everything until the next navigation
    describes the item just opened. A row the worker never navigated to is
    marked unreached, so a budget that ran out cannot be reported as anything
    that happened on a page nobody opened.
    """
    facts: Dict[str, JsonDict] = {
        key: {
            "reached": False,
            "budgetExhausted": budget_exhausted,
            "overlayOutcome": "",
            "materializationStalled": False,
            "navigationFailed": False,
            "toolFailed": False,
            "navigationEpoch": 0,
        }
        for key in row_keys
    }
    current: Optional[str] = None
    epoch = 0
    for item in trace if isinstance(trace, list) else []:
        if not isinstance(item, dict):
            continue
        url = _navigation_target(item)
        if url:
            epoch += 1
            current = _row_key_for_url(url, row_keys)
            if current is not None:
                entry = facts[current]
                entry["reached"] = True
                entry["navigationEpoch"] = epoch
                # A row reached in this attempt was not lost to the budget.
                entry["budgetExhausted"] = False
                result = item.get("result")
                if isinstance(result, dict) and str(result.get("status") or "") == "failed":
                    entry["navigationFailed"] = True
            continue
        if current is None:
            continue
        entry = facts[current]
        result = item.get("result")
        overlay = _overlay_outcome(result)
        if overlay and not entry["overlayOutcome"]:
            entry["overlayOutcome"] = overlay
        if isinstance(result, dict):
            if str(result.get("collectionState") or "") == "materialization_stalled":
                entry["materializationStalled"] = True
            if str(result.get("status") or "") == "failed":
                entry["toolFailed"] = True
    return facts


def ledger_contradicts_cause(
    ledger: Any, row_key: str, claimed_cause: str,
) -> Optional[JsonDict]:
    """A claimed cause the ledger's own record for that row rules out.

    Reporting Syndie.io as auth-blocked when its receipts say the worker ran
    out of steps is not a difference of interpretation; the ledger holds an
    entry and the claim disagrees with it.
    """
    wanted = str(claimed_cause or "").strip()
    if not wanted:
        return None
    for entry in ledger if isinstance(ledger, list) else []:
        if not isinstance(entry, dict) or str(entry.get("rowKey") or "") != row_key:
            continue
        recorded = {
            str(item.get("cause") or "")
            for item in (entry.get("fields") or {}).values()
            if isinstance(item, dict)
        }
        recorded.discard(CAUSE_NONE)
        if recorded and wanted not in recorded:
            return {
                "rowKey": row_key,
                "claimedCause": wanted,
                "recordedCauses": sorted(recorded),
            }
    return None
