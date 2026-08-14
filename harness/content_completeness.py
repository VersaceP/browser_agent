"""Page-region observations for trace evidence and model reflection.

This module deliberately does not participate in CAPTCHA/HITL scoring or own a
business completion verdict. Historical decision labels remain internal
telemetry; model-facing consumers receive only attributed observation facts.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import re
from typing import Any, Dict, Iterable, List, Optional, Set
from urllib.parse import urlsplit, urlunsplit

from harness.call_outcome import evaluate_grant
from harness.semantic_frames import response_node_count
from harness.utils import JsonDict


ROUTE_RECOVERY_REQUIRED = "route_recovery_required"
BLOCKED_CONTENT_SUPPRESSION = "blocked_content_suppression"
MARKER_DECLARATION_SUSPECT = "marker_declaration_suspect"
CONTENT_ABSENT = "absent"
CONTENT_SHELL_SEEN = "shell_seen"
CONTENT_MATERIALIZED = "content_materialized"

# Structured reads that can spend a provisional binding's bounded window. The
# window is what delays route recovery long enough for a worker to persist what
# it saw, so only reads which could have produced fresh region evidence count.
STRUCTURED_BINDING_AGING_METHODS = frozenset({
    "DOM.getAXTree",
    "DOM.getSemanticTree",
    "DOM.getText",
    "DOM.getAttribute",
    "Runtime.evaluate",
    "collect_items",
})

_CONFIRMATORY_SIGNAL_STRENGTHS = frozenset({"confirmatory"})

_MODEL_OBSERVATION_FACT_KEYS = (
    "scope",
    "pageId",
    "url",
    "title",
    "epoch",
    "observationOrder",
    "navigationKind",
    "navigationOutcome",
    "sourcePageId",
    "sourceUrl",
    "shellPresent",
    "observedRegions",
    "missingRegions",
    "materializationAttempts",
    "matchedSignals",
    "matchedConfirmatorySignals",
    "recoveryAttempts",
    "recoveryAttemptsByItem",
    "upstreamBlocker",
    "regionRecordCounts",
    "regionCollectionStates",
    "regionExhaustionEvidence",
    "collectionBinding",
    "collectionBindingStatus",
    "structuredObservedRegions",
    "isRecoverySource",
    "validatedArtifactRegionsByPageUrl",
    "receipts",
)


def content_completeness_observation_facts(value: Any) -> JsonDict:
    """Return model-facing facts while omitting tracker verdict vocabulary."""
    if not isinstance(value, dict):
        return {}
    facts = {
        key: value.get(key)
        for key in _MODEL_OBSERVATION_FACT_KEYS
        if value.get(key) not in (None, "", [], {})
        or key in {"shellPresent", "recoveryAttempts", "isRecoverySource"}
    }
    facts["source"] = "content_completeness_tracker_observation"
    return facts


def _field_tokens(value: Any) -> Set[str]:
    text = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", str(value or ""))
    return {
        token.casefold()
        for token in re.split(r"[^A-Za-z0-9_\u4e00-\u9fff]+|_+", text)
        if token
    }


def _item_key(url: str, title: str = "") -> str:
    value = str(url or "").strip()
    if value:
        try:
            parts = urlsplit(value)
            normalized = urlunsplit((
                parts.scheme.casefold(),
                parts.netloc.casefold(),
                parts.path or "/",
                parts.query,
                "",
            ))
        except ValueError:
            normalized = value.split("#", 1)[0]
        return normalized
    return f"title:{str(title or '').strip().casefold()}" if title else ""


def _route_identity(url: str, title: str = "") -> str:
    """Return a conservative task-local item identity.

    Query parameters are retained because many generic sites encode the item
    identity in the query rather than the path.  A route preference may fail to
    promote when click-through adds tracking noise, but it must never pair two
    distinct query-addressed items merely because their paths match.
    """
    return _item_key(url, title)


def _source_template(url: str) -> str:
    value = str(url or "").strip()
    if not value:
        return ""
    try:
        parts = urlsplit(value)
        return urlunsplit((
            parts.scheme.casefold(),
            parts.netloc.casefold(),
            parts.path or "/",
            "",
            "",
        ))
    except ValueError:
        return value.split("?", 1)[0].split("#", 1)[0]


def _usable_source_url(url: str) -> bool:
    value = str(url or "").strip().casefold()
    return bool(
        value
        and not value.startswith(("about:", "newtab:", "chrome:", "edge:"))
    )


def _dom_snapshot(result: Any) -> tuple[str, int]:
    """Build a bounded, generic DOM signature for click outcome classification."""
    normalized: List[str] = []
    total_length = 0
    for raw in _strings(result):
        text = re.sub(r"\s+", " ", str(raw or "")).strip().casefold()
        if text:
            normalized.append(text)
            total_length += len(text)
        if total_length >= 50000:
            break
    data = _response_data(result)
    node_count = response_node_count(data) or 0
    return "\n".join(normalized)[:50000], node_count


def _strings(value: Any, *, depth: int = 0, limit: int = 800) -> List[str]:
    if depth > 7 or limit <= 0:
        return []
    out: List[str] = []
    if isinstance(value, dict):
        for key, item in value.items():
            key_text = str(key or "").strip()
            if key_text and key_text.lower() not in {
                "savedpath", "path", "filepath", "descriptionpath"
            }:
                out.append(key_text)
            out.extend(_strings(item, depth=depth + 1, limit=limit - len(out)))
            if len(out) >= limit:
                break
    elif isinstance(value, list):
        for item in value:
            out.extend(_strings(item, depth=depth + 1, limit=limit - len(out)))
            if len(out) >= limit:
                break
    elif isinstance(value, str):
        text = value.strip()
        if text and not text.startswith("/"):
            out.append(text[:8000])
    elif isinstance(value, (bool, int, float)):
        out.append(str(value))
    return out[:limit]


def _response_data(result: Any) -> JsonDict:
    if not isinstance(result, dict):
        return {}
    response = result.get("response")
    if isinstance(response, dict):
        data = response.get("data")
        if isinstance(data, dict):
            return data
    data = result.get("data")
    return data if isinstance(data, dict) else {}


def _click_gate_receipt(result: Any) -> JsonDict:
    """Read the Enforced FleetClickGate receipt from either call envelope."""

    if not isinstance(result, dict):
        return {}
    for candidate in (result, result.get("response")):
        if not isinstance(candidate, dict):
            continue
        receipt = candidate.get("harnessClickGate")
        if isinstance(receipt, dict):
            return dict(receipt)
    return {}


def _signal_value_matches(actual: Any, expected: Any) -> bool:
    if isinstance(expected, bool):
        if isinstance(actual, bool):
            return actual is expected
        return str(actual).strip().casefold() == str(expected).casefold()
    return str(actual).strip().casefold() == str(expected).strip().casefold()


def _structured_signal_matches(
    value: Any,
    *,
    name: str,
    expected: Any,
    depth: int = 0,
) -> bool:
    if depth > 8:
        return False
    if isinstance(value, dict):
        for key, item in value.items():
            if str(key).casefold() == name.casefold() and _signal_value_matches(
                item, expected
            ):
                return True
            if isinstance(item, (dict, list)) and _structured_signal_matches(
                item, name=name, expected=expected, depth=depth + 1
            ):
                return True
    elif isinstance(value, list):
        return any(
            _structured_signal_matches(
                item, name=name, expected=expected, depth=depth + 1
            )
            for item in value
        )
    return False


def _text_signal_matches(haystack: str, *, name: str, expected: Any) -> bool:
    expected_text = re.escape(str(expected).strip().casefold())
    name_text = re.escape(name.casefold())
    return bool(re.search(
        rf"{name_text}\s*(?::|=|\bis\b)?\s*[\"']?{expected_text}\b",
        haystack,
    ))


def _page_id(params: Any, result: Any) -> str:
    for value in (params, _response_data(result), result):
        if not isinstance(value, dict):
            continue
        raw = value.get("pageId") or value.get("page_id")
        if isinstance(raw, str) and raw.strip():
            return raw.strip()
    return ""


MARKER_SOURCE_DECLARED = "declared"
MARKER_SOURCE_ID_FALLBACK = "id_fallback"


def _marker_spec(raw: Any) -> Optional[JsonDict]:
    """Normalize one declared region.

    An `id` and a `marker` are different kinds of statement and must not share
    a slot. The id is a name WE choose, so it is always satisfiable; a marker
    asserts that some text actually appears on the page, which only the page
    can settle. Collapsing them lets a plan write
    `expected_regions: ["sizeInfo", "attributes"]` and have the harness go
    hunting a Chinese page for the literal string ``sizeInfo`` — it never
    matches, the region reads `absent` forever, and the harness ends up blaming
    the site for suppressing content that our own declaration never described.

    The fallback is kept (dropping the region silently would be worse) but is
    labelled, so `content_completeness_config_errors` can reject the
    declaration and `classify_region_materialization` can tell a defective
    marker apart from genuinely missing content.
    """
    if isinstance(raw, str) and raw.strip():
        name = raw.strip()
        return {
            "id": name,
            "markers": [name],
            "marker_source": MARKER_SOURCE_ID_FALLBACK,
        }
    if not isinstance(raw, dict):
        return None
    name = str(raw.get("id") or raw.get("name") or "").strip()
    markers = raw.get("markers")
    if not isinstance(markers, list):
        marker = raw.get("marker")
        markers = [marker] if marker is not None else []
    normalized = [str(item).strip() for item in markers if str(item).strip()]
    marker_source = MARKER_SOURCE_DECLARED
    if not normalized and name:
        normalized = [name]
        marker_source = MARKER_SOURCE_ID_FALLBACK
    if not name or not normalized:
        return None
    spec: JsonDict = {
        "id": name,
        "markers": normalized,
        "marker_source": marker_source,
    }
    raw_fields = raw.get("fields")
    if isinstance(raw_fields, list):
        fields = [
            str(item).strip() for item in raw_fields if str(item).strip()
        ]
        if fields:
            spec["fields"] = list(dict.fromkeys(fields))
    raw_min_records = raw.get("min_records")
    if (
        isinstance(raw_min_records, int)
        and not isinstance(raw_min_records, bool)
        and raw_min_records > 0
    ):
        spec["min_records"] = raw_min_records
    return spec


def content_completeness_config_errors(value: Any) -> List[str]:
    """Return declaration errors that normalization must not silently erase."""
    if not isinstance(value, dict):
        return ["content_completeness must be an object"]
    errors: List[str] = []
    raw_regions = value.get("expected_regions")
    if not isinstance(raw_regions, list) or not raw_regions:
        return ["content_completeness.expected_regions must be a non-empty array"]
    for index, raw in enumerate(raw_regions):
        spec = _marker_spec(raw)
        if (
            isinstance(spec, dict)
            and spec.get("marker_source") == MARKER_SOURCE_ID_FALLBACK
        ):
            errors.append(
                "content_completeness.expected_regions"
                f"[{index}] declares no markers, so the region id"
                f" {str(spec.get('id'))!r} is searched for as page text."
                " A region id is a name you choose; a marker must be text that"
                " actually appears on the page. Declare"
                ' {"id": "...", "markers": ["<visible page text>"]}.'
            )
        if not isinstance(raw, dict):
            continue
        if "min_records" in raw:
            raw_min = raw.get("min_records")
            valid = (
                isinstance(raw_min, int)
                and not isinstance(raw_min, bool)
                and raw_min > 0
            )
            if not valid:
                errors.append(
                    "content_completeness.expected_regions"
                    f"[{index}].min_records must be a positive integer"
                )
        if "fields" in raw:
            raw_fields = raw.get("fields")
            if not (
                isinstance(raw_fields, list)
                and raw_fields
                and all(isinstance(item, str) and item.strip() for item in raw_fields)
            ):
                errors.append(
                    "content_completeness.expected_regions"
                    f"[{index}].fields must be a non-empty string array"
                )
    return errors


def classify_region_materialization(
    *,
    marker_seen: bool,
    min_records: int = 0,
    record_count: int = 0,
    collection_state: str = "",
    exhaustion_evidence: Any = None,
) -> str:
    """Classify one declared region from mechanical collection evidence."""
    if min_records <= 0:
        return CONTENT_MATERIALIZED if marker_seen else CONTENT_ABSENT
    if record_count >= min_records:
        return CONTENT_MATERIALIZED
    evidence_observed = (
        exhaustion_evidence.get("observed")
        if isinstance(exhaustion_evidence, dict)
        and isinstance(exhaustion_evidence.get("observed"), dict)
        else {}
    )
    exhaustion_scope = str(evidence_observed.get("scope") or "").strip()
    if (
        collection_state == "explicitly_exhausted"
        and record_count > 0
        and isinstance(exhaustion_evidence, dict)
        and str(exhaustion_evidence.get("kind") or "").strip()
        # Reaching the document bottom proves only that the page stopped
        # scrolling.  It does not prove that a nested/lazy target collection
        # (for example a review drawer) was fully materialized.  Preserve
        # legacy evidence with no scope and targeted container/load-more
        # proofs, but never let an explicitly document-scoped proof waive a
        # declared record shortfall.
        and exhaustion_scope != "document"
    ):
        return CONTENT_MATERIALIZED
    if (
        marker_seen
        or record_count > 0
        or collection_state in {
            "target_reached", "explicitly_exhausted",
            "materialization_stalled", "blocked",
        }
    ):
        return CONTENT_SHELL_SEEN
    return CONTENT_ABSENT


def _collection_completion_evidence_priority(
    collection_state: str,
    exhaustion_evidence: Any,
) -> int:
    """Rank completion proofs without treating a stalled probe as evidence."""
    if collection_state == "target_reached":
        return 2
    if (
        collection_state == "explicitly_exhausted"
        and isinstance(exhaustion_evidence, dict)
        and str(exhaustion_evidence.get("kind") or "").strip()
    ):
        return 1
    return 0


def normalize_content_completeness_config(value: Any) -> JsonDict:
    if not isinstance(value, dict):
        return {}
    expected = [
        item for item in (
            _marker_spec(raw) for raw in value.get("expected_regions", [])
        ) if item is not None
    ]
    shell = [
        item for item in (
            _marker_spec(raw) for raw in value.get("shell_markers", [])
        ) if item is not None
    ]
    signals: List[JsonDict] = []
    for raw in value.get("suppression_signals", []):
        if not isinstance(raw, dict):
            continue
        name = str(raw.get("name") or raw.get("locator") or "").strip()
        if not name:
            continue
        raw_strength = str(raw.get("strength") or "supporting").casefold()
        # `strong` was accepted by early internal builds but never belonged to
        # the public schema. Preserve compatibility while emitting only the two
        # canonical contract values.
        strength = "confirmatory" if raw_strength in {
            "confirmatory", "strong"
        } else "supporting"
        signals.append({
            "name": name,
            "match": raw.get("match", True),
            "strength": strength,
        })
    recovery = value.get("recovery") if isinstance(value.get("recovery"), dict) else {}
    try:
        max_attempts = max(1, min(5, int(recovery.get("max_attempts_per_item", 2))))
    except (TypeError, ValueError):
        max_attempts = 2
    return {
        "shell_markers": shell,
        "expected_regions": expected,
        "suppression_signals": signals,
        "recovery": {
            "mode": str(recovery.get("mode") or "listing_link_click"),
            "max_attempts_per_item": max_attempts,
        },
    } if expected else {}


# Decisions the worker cannot act on from the status word alone. Kept beside
# the decision that produces them so the tool boundary stays generic.
_DECISION_INSTRUCTIONS: Dict[str, str] = {
    MARKER_DECLARATION_SUSPECT: (
        "The page loaded but none of the declared content_completeness markers"
        " were found anywhere on it. Before treating the content as absent or"
        " suppressed, read the page and check whether the markers match text"
        " that is actually rendered — a marker copied from a field name (for"
        " example an English identifier on a Chinese page) never matches. If"
        " the markers are wrong, report the corrected marker text as a blocker"
        " so the plan can be fixed; if they are right, continue working the"
        " page and the ordinary recovery ladder resumes."
    ),
}


@dataclass
class PageContentState:
    page_id: str
    url: str = ""
    title: str = ""
    navigation_kind: str = "unknown"
    navigation_outcome: str = ""
    source_page_id: str = ""
    source_url: str = ""
    epoch: int = 0
    observation_order: int = 0
    shell_present: bool = False
    observed_regions: Set[str] = field(default_factory=set)
    missing_regions: Set[str] = field(default_factory=set)
    materialization_attempts: Set[str] = field(default_factory=set)
    matched_signals: Set[str] = field(default_factory=set)
    matched_confirmatory_signals: Set[str] = field(default_factory=set)
    item_key: str = ""
    recovery_attempts_by_item: Dict[str, int] = field(default_factory=dict)
    pending_recovery_credit: bool = False
    pending_recovery_outcome: str = ""
    recovery_attempts: int = 0
    marker_suspect_emitted: bool = False
    decision: str = "inconclusive"
    evidence_strength: str = "none"
    upstream_blocker: str = ""
    content_state: str = CONTENT_ABSENT
    region_states: Dict[str, str] = field(default_factory=dict)
    region_record_counts: Dict[str, int] = field(default_factory=dict)
    region_collection_states: Dict[str, str] = field(default_factory=dict)
    region_exhaustion_evidence: Dict[str, JsonDict] = field(default_factory=dict)
    last_collection_binding: str = ""
    last_collection_binding_status: str = ""
    last_collection_binding_candidate: str = ""
    last_collection_binding_next_instruction: str = ""
    last_dom_text: str = ""
    last_dom_node_count: int = 0
    route_preference: JsonDict = field(default_factory=dict)
    # Set only when Page.list proves that this distinct page opened a NEW_TAB
    # destination. The detail-region contract does not apply to the retained
    # listing/navigation source. SAME_TAB and IN_PAGE flows never set it.
    is_recovery_source: bool = False
    # Harness-only, page-local early evidence.  It can delay route recovery
    # briefly so validated rows can be persisted, but never certifies content.
    structured_observed_regions: Dict[str, JsonDict] = field(default_factory=dict)
    structured_binding_generations_used: Set[str] = field(default_factory=set)
    # True when the previous observation was aging-eligible and has not yet been
    # charged. `observe_content_binding` clears it when that same observation
    # produced a binding, so sibling regions of one contract never expire each
    # other. See the deferral comment in `observe`.
    pending_binding_age: bool = False

    def summary(self) -> JsonDict:
        return {
            "pageId": self.page_id,
            "url": self.url or None,
            "title": self.title or None,
            "navigationKind": self.navigation_kind,
            "navigationOutcome": self.navigation_outcome or None,
            "sourcePageId": self.source_page_id or None,
            "sourceUrl": self.source_url or None,
            "epoch": self.epoch,
            "observationOrder": self.observation_order,
            "shellPresent": self.shell_present,
            "observedRegions": sorted(self.observed_regions),
            "missingRegions": sorted(self.missing_regions),
            "materializationAttempts": sorted(self.materialization_attempts),
            "matchedSignals": sorted(self.matched_signals),
            "matchedConfirmatorySignals": sorted(
                self.matched_confirmatory_signals
            ),
            "itemKey": self.item_key or None,
            "recoveryAttempts": self.recovery_attempts,
            "recoveryAttemptsByItem": dict(self.recovery_attempts_by_item),
            "decision": self.decision,
            "decisionNextInstruction": _DECISION_INSTRUCTIONS.get(self.decision),
            "evidenceStrength": self.evidence_strength,
            "upstreamBlocker": self.upstream_blocker or None,
            "contentState": self.content_state,
            "regionStates": dict(self.region_states),
            "regionRecordCounts": dict(self.region_record_counts),
            "regionCollectionStates": dict(self.region_collection_states),
            "regionExhaustionEvidence": dict(self.region_exhaustion_evidence),
            "collectionBinding": self.last_collection_binding or None,
            "collectionBindingStatus": self.last_collection_binding_status or None,
            "collectionBindingCandidate": (
                self.last_collection_binding_candidate or None
            ),
            "collectionBindingNextInstruction": (
                self.last_collection_binding_next_instruction or None
            ),
            "routePreference": dict(self.route_preference) or None,
            "isRecoverySource": self.is_recovery_source,
            "structuredObservedRegions": {
                key: dict(value)
                for key, value in self.structured_observed_regions.items()
            },
        }


class ContentCompletenessTracker:
    """Observe page-region evidence for model-visible handoff and telemetry."""

    def __init__(self, config: Any = None, *, config_source: str = "explicit"):
        self.config = normalize_content_completeness_config(config)
        self.config_source = config_source if self.config else "disabled"
        self.pages: Dict[str, PageContentState] = {}
        # Recovery is bounded per destination item, not per page.  A listing
        # anchor commonly opens a fresh tab, so keeping this ledger on
        # PageContentState would reset the budget on every new-tab attempt.
        self.recovery_attempts_by_item: Dict[str, int] = {}
        self._observation_sequence = 0
        self.known_page_ids: Set[str] = set()
        self.page_inventory_baselined = False
        self.route_recovery_pending = False
        self.pending_return_pages: Set[str] = set()
        # Page.create does not expose an opener in ABCP.  A validated
        # harness-only sideband records the intended source, but the source is
        # not exempted from terminal veto until the created page has produced
        # a successful Page.getState with a real URL.  This prevents an unused
        # blank tab or failed Page.navigate from laundering an unresolved
        # source page.
        self.pending_explicit_recovery_sources: Dict[str, str] = {}
        # Listing clicks whose landing page the gate would not name. They stay
        # unresolved until the MODEL declares the link with
        # navigation_context(kind="route_recovery_claimed_page"). Nothing here
        # infers a binding: an earlier revision kept one "awaiting" slot and
        # bound whatever page was touched next, which is the latest-intent-wins
        # attribution this architecture exists to avoid — a rejected claim on
        # another worker's tab earned recovery credit, and a second click stole
        # the first click's candidate.
        self.unresolved_landing_sources: Set[str] = set()
        # Result of the most recent explicit declaration, so the receipt can
        # report what actually happened instead of probing an unrelated map.
        self.last_declaration_accepted: bool = False
        self.direct_suppression_evidence: Dict[str, JsonDict] = {}
        self.route_preferences: Dict[str, JsonDict] = {}
        self.auth_generation: Optional[int] = None
        self._telemetry_events: List[JsonDict] = []
        # Final artifact credit is phase-scoped but page-addressed.  A valid
        # multi-row artifact may span several detail pages; credit from rows for
        # URL A must never clear a missing region on URL B.
        self.validated_artifact_regions_by_url: Dict[str, Set[str]] = {}
        self.validated_artifact_receipts: List[JsonDict] = []

    def _telemetry(self, event: str, state: PageContentState, **extra: Any) -> None:
        self._telemetry_events.append({
            "event": event,
            "pageId": state.page_id,
            "navigationEpoch": state.epoch,
            "sourceUrl": state.source_url or None,
            "targetUrl": state.url or None,
            "itemIdentity": state.item_key or None,
            "attempt": state.recovery_attempts,
            "decision": state.decision,
            **extra,
        })

    def drain_telemetry_events(self) -> List[JsonDict]:
        events = list(self._telemetry_events)
        self._telemetry_events.clear()
        return events

    def observe_auth_generation(self, value: Any) -> None:
        try:
            generation = int(value or 0)
        except (TypeError, ValueError):
            generation = 0
        if generation <= 0:
            return
        if self.auth_generation is not None and generation != self.auth_generation:
            self._invalidate_route_preferences(
                "auth_generation_changed",
                global_scope=True,
            )
        self.auth_generation = generation

    def _invalidate_route_preferences(
        self,
        reason: str,
        state: Optional[PageContentState] = None,
        *,
        global_scope: bool = False,
    ) -> None:
        if not self.route_preferences:
            return
        target = state or max(
            self.pages.values(),
            key=lambda item: item.observation_order,
            default=PageContentState(page_id=""),
        )
        if global_scope or state is None:
            keys = set(self.route_preferences)
        else:
            explicit_key = str(
                (state.route_preference or {}).get("cohortKey") or ""
            )
            source_template = _source_template(
                state.source_url or state.url
            )
            keys = {
                key
                for key, receipt in self.route_preferences.items()
                if (
                    (explicit_key and key == explicit_key)
                    or (
                        source_template
                        and str(receipt.get("sourceTemplate") or "")
                        == source_template
                        and str(
                            receipt.get("strategyScope") or self.config_source
                        )
                        == self.config_source
                    )
                )
            }
        if not keys:
            return
        invalidated_receipts = [
            self.route_preferences[key]
            for key in keys
            if key in self.route_preferences
        ]
        for receipt in invalidated_receipts:
            self._telemetry(
                "navigation_recovery.preference_invalidated",
                target,
                reason=str(reason or "unknown"),
                cohortKey=receipt.get("cohortKey"),
            )
        for key in keys:
            self.route_preferences.pop(key, None)
        if global_scope or state is None:
            self.direct_suppression_evidence.clear()
        else:
            for receipt in invalidated_receipts:
                identity = str(receipt.get("itemIdentity") or "")
                if identity:
                    self.direct_suppression_evidence.pop(identity, None)
        for page in self.pages.values():
            if str(page.route_preference.get("cohortKey") or "") in keys:
                page.route_preference.clear()

    def _record_direct_suppression(self, state: PageContentState) -> None:
        identity = _route_identity(state.url, state.title)
        if not identity:
            return
        self.direct_suppression_evidence[identity] = {
            "pageId": state.page_id,
            "navigationEpoch": state.epoch,
            "itemIdentity": identity,
            "decision": state.decision,
        }

    def _promote_route_preference(self, state: PageContentState) -> None:
        if state.navigation_kind != "link_click":
            return
        identity = _route_identity(state.url, state.title)
        direct = self.direct_suppression_evidence.get(identity)
        source_template = _source_template(state.source_url)
        destination_template = _source_template(state.url)
        # A local reveal click inside the suppressed destination may materialize
        # the region, but it is not listing-link evidence.  Promotion requires
        # a distinct source template plus the same destination identity.
        if (
            not isinstance(direct, dict)
            or not source_template
            or source_template == destination_template
        ):
            return
        region_ids = sorted(
            str(spec["id"])
            for spec in self.config.get("expected_regions") or []
        )
        cohort_key = (
            f"{self.config_source}|{source_template}|{'|'.join(region_ids)}"
        )
        receipt: JsonDict = {
            "mode": "listing_link_click",
            "cohortKey": cohort_key,
            "strategyScope": self.config_source,
            "sourceTemplate": source_template,
            "itemIdentity": identity,
            "regions": region_ids,
            "authGeneration": self.auth_generation,
            "evidence": {
                "direct": dict(direct),
                "clickThrough": {
                    "pageId": state.page_id,
                    "navigationEpoch": state.epoch,
                    "navigationOutcome": state.navigation_outcome or None,
                    "contentState": state.content_state,
                },
            },
        }
        self.route_preferences[cohort_key] = receipt
        state.route_preference = dict(receipt)
        source_state = self.pages.get(state.source_page_id)
        if source_state is not None:
            source_state.route_preference = dict(receipt)
        direct_page_id = str(direct.get("pageId") or "")
        direct_state = self.pages.get(direct_page_id)
        if direct_state is not None and direct_state.decision == ROUTE_RECOVERY_REQUIRED:
            direct_state.decision = "recovered_via_link_click"
        self._refresh_route_recovery_pending()
        self._telemetry(
            "navigation_recovery.preference_promoted",
            state,
            cohortKey=cohort_key,
            routePreference="listing_link_click",
        )

    def recovery_receipt(self, page_id: str) -> Optional[JsonDict]:
        state = self.pages.get(str(page_id or ""))
        if state is None:
            return None
        max_attempts = int(
            (self.config.get("recovery") or {}).get("max_attempts_per_item", 2)
        )
        remaining = max(0, max_attempts - int(state.recovery_attempts or 0))
        if state.decision == ROUTE_RECOVERY_REQUIRED:
            if state.source_page_id and state.source_page_id != state.page_id:
                return_method = "Page.switchTo"
            elif state.source_url:
                return_method = "Page.go"
            else:
                return_method = "listing_source_required"
            return {
                "status": "route_recovery_required",
                "mode": "listing_link_click",
                "pageId": state.page_id,
                "sourcePageId": state.source_page_id or None,
                "sourceUrl": state.source_url or None,
                "itemIdentity": state.item_key or None,
                "remainingAttempts": remaining,
                "returnMethod": return_method,
                "requiredActions": [
                    "restore_listing_source",
                    "refresh_accessibility_tree",
                    "rebind_item_anchor",
                    "click_anchor",
                    "inspect_click_gate_receipt_and_state",
                    "verify_required_regions",
                ],
                "next_instruction": (
                    "The loaded shell is incomplete after bounded local"
                    " materialization. Restore the listing source, refresh the"
                    " accessibility tree, rebind the real item anchor, and"
                    " click it once. The Fleet click gate takes its own raw"
                    " Page.list baseline/final inventory; inspect its receipt"
                    " plus Page.getState, then verify the required regions."
                    " Do not reuse a stale AX id."
                ),
            }
        if (
            state.decision == "inconclusive"
            and state.shell_present
            and state.missing_regions
        ):
            return {
                "status": "materialization_required",
                "mode": "local_reveal_then_probe",
                "pageId": state.page_id,
                "itemIdentity": state.item_key or None,
                "remainingAttempts": remaining,
                "requiredActions": [
                    "reveal_or_scroll_target_region",
                    "probe_semantic_tree",
                    "run_bounded_collection_if_repeated",
                ],
                "next_instruction": (
                    "The page shell is present but required regions are not yet"
                    " materialized. Reveal or scroll the target region, refresh"
                    " DOM.getSemanticTree with includeShadowDom=true when the"
                    " connected schema supports it (custom-element hosts can"
                    " otherwise appear as empty children), and use the bounded"
                    " collection tool for repeated records before escalating"
                    " navigation."
                ),
            }
        return None

    def route_preference_for_page(self, page_id: str) -> Optional[JsonDict]:
        state = self.pages.get(str(page_id or ""))
        if state is None:
            return None
        templates = {
            _source_template(state.url),
            _source_template(state.source_url),
        } - {""}
        if state.route_preference:
            preferred_template = str(
                state.route_preference.get("sourceTemplate") or ""
            )
            if preferred_template in templates:
                return dict(state.route_preference)
            state.route_preference.clear()
        for receipt in self.route_preferences.values():
            if str(receipt.get("sourceTemplate") or "") in templates:
                return dict(receipt)
        return None

    @property
    def enabled(self) -> bool:
        return bool(self.config)

    def _state(self, page_id: str) -> PageContentState:
        state = self.pages.get(page_id)
        if state is None:
            state = PageContentState(
                page_id=page_id,
                recovery_attempts_by_item=self.recovery_attempts_by_item,
            )
            self.pages[page_id] = state
        return state

    def _touch(self, state: PageContentState) -> None:
        # Page-local navigation epochs cannot establish recency across tabs.
        # Keep one tracker-local monotonic order for terminal arbitration.
        self._observation_sequence += 1
        state.observation_order = self._observation_sequence

    def _refresh_route_recovery_pending(self) -> None:
        self.route_recovery_pending = any(
            state.decision == ROUTE_RECOVERY_REQUIRED
            and not state.upstream_blocker
            and not state.is_recovery_source
            for state in self.pages.values()
        )

    def can_designate_recovery_source(self, page_id: str) -> bool:
        """Whether ``page_id`` currently carries unresolved route evidence.

        Page.create has no ABCP opener field, so a caller may provide explicit
        harness-only provenance.  Accept it only for a page the tracker has
        already observed as an unresolved shell/route candidate; a bare model
        assertion can never exempt an arbitrary page from terminal veto.
        """
        page_key = str(page_id or "")
        if page_key in self.unresolved_landing_sources:
            # A listing click the gate refused to attribute. The tracker parked
            # it itself, so this is its own evidence, not a model assertion.
            return True
        state = self.pages.get(page_key)
        if state is None or state.upstream_blocker or state.is_recovery_source:
            return False
        if state.decision in {
            ROUTE_RECOVERY_REQUIRED,
            BLOCKED_CONTENT_SUPPRESSION,
        }:
            return True
        return bool(
            state.decision == "inconclusive"
            and state.shell_present
            and state.missing_regions
        )

    def _settle_explicit_recovery_source(
        self,
        state: PageContentState,
        *,
        evidence_kind: str,
    ) -> bool:
        """Transfer terminal responsibility only after target content evidence.

        ``Page.create`` plus a successful ``Page.getState`` proves page
        identity, not task-content materialization.  Keep the source page in
        the terminal-veto set until a DOM/collection observation classifies
        the destination as a shell, a materialized region, or a suppression
        candidate.  Thus about:blank, browser new-tab pages, failed redirects,
        and uninspected HTTP pages cannot create a veto gap.
        """
        page_id = state.page_id
        source_page_id = self.pending_explicit_recovery_sources.get(page_id, "")
        if (
            not source_page_id
            or not _usable_source_url(state.url)
            or state.upstream_blocker
        ):
            return False
        classified = bool(
            state.shell_present
            or state.content_state in {
                CONTENT_SHELL_SEEN,
                CONTENT_MATERIALIZED,
            }
            or state.observed_regions
            or state.matched_confirmatory_signals
            or state.decision in {
                "complete",
                ROUTE_RECOVERY_REQUIRED,
                BLOCKED_CONTENT_SUPPRESSION,
            }
        )
        if not classified or not self.can_designate_recovery_source(
            source_page_id
        ):
            return False
        source = self._state(source_page_id)
        self._touch(source)
        source.is_recovery_source = True
        source.decision = "navigation_source"
        state.navigation_kind = "explicit_new_page"
        state.navigation_outcome = "new_tab"
        state.source_page_id = source_page_id
        state.source_url = source.url
        self.pending_explicit_recovery_sources.pop(page_id, None)
        self._telemetry(
            "navigation_recovery.outcome",
            state,
            navigationOutcome="new_tab",
            provenance="explicit_page_create_source",
            destinationEvidenceKind=evidence_kind,
        )
        self._refresh_route_recovery_pending()
        return True

    def _bind_landing_page(
        self,
        source: PageContentState,
        landing_page_id: str,
        *,
        source_url: str,
        item_identity: str,
        landing_url: str = "",
        provenance: str = "fleet_click_gate",
    ) -> None:
        """Record that ``landing_page_id`` is where this listing click landed."""
        landing = self._state(landing_page_id)
        self._touch(landing)
        self.known_page_ids.add(landing_page_id)
        landing.navigation_kind = "link_click"
        landing.navigation_outcome = "new_tab"
        landing.source_page_id = source.page_id
        landing.source_url = source_url
        landing.url = landing_url or landing.url or ""
        landing.pending_recovery_credit = True
        landing.pending_recovery_outcome = "new_tab"
        landing.item_key = (
            _item_key(landing.url, landing.title)
            or item_identity
            or landing.item_key
        )
        source.is_recovery_source = True
        source.decision = "navigation_source"
        self._telemetry(
            "navigation_recovery.landing_bound",
            landing,
            navigationOutcome="new_tab",
            provenance=provenance,
        )

    def declare_claimed_landing(
        self,
        page_id: str,
        source_page_id: str,
        *,
        granted: bool,
    ) -> bool:
        """Bind a model-named landing after the central grant policy approved.

        Raw response interpretation belongs to evaluate_grant. Keeping a
        second, slightly different Page.getState predicate here made the same
        transition depend on whichever copy happened to be stricter.

        The harness validates rather than guesses: the named source must be a
        listing click this tracker itself parked as unresolved. No candidate
        set, no "most recent click", no ordering assumption — so overlapping
        clicks cannot steal each other's credit.
        """
        if not granted:
            return False
        page_id = str(page_id or "").strip()
        source_page_id = str(source_page_id or "").strip()
        if not page_id or not source_page_id or page_id == source_page_id:
            return False
        if source_page_id not in self.unresolved_landing_sources:
            return False
        self.unresolved_landing_sources.discard(source_page_id)
        source = self._state(source_page_id)
        self._bind_landing_page(
            source,
            page_id,
            source_url=source.url,
            item_identity=f"{_source_template(source.url)}|unresolved_click",
            provenance="model_declared_claim",
        )
        return True

    def _record_no_effect(
        self,
        state: PageContentState,
        *,
        item_identity: str = "",
        reason: str = "no_navigation_observed",
    ) -> None:
        identity = item_identity or (
            f"{_source_template(state.url)}|unresolved_click"
        )
        if identity:
            self.recovery_attempts_by_item[identity] = (
                self.recovery_attempts_by_item.get(identity, 0) + 1
            )
            state.recovery_attempts = self.recovery_attempts_by_item[identity]
        state.navigation_outcome = "no_effect"
        self._telemetry(
            "navigation_recovery.outcome",
            state,
            navigationOutcome="no_effect",
            reason=reason,
            provenance="fleet_click_gate",
        )
        self._invalidate_route_preferences("click_no_effect", state)

    def _apply_click_gate_receipt(
        self,
        source: PageContentState,
        params: Any,
        receipt: JsonDict,
    ) -> None:
        """Consume the single authoritative FleetClickGate outcome.

        The previous tracker inferred a click result later from whichever
        Page.list/DOM call happened next.  That duplicated pending-click state
        per worker and could assign every newly visible page to the same click.
        The Fleet-scoped gate has already serialized and reconciled the action,
        so completeness only projects its receipt into route state.
        """

        selector_identity = str(
            (params.get("id") or params.get("selector") or "")
            if isinstance(params, dict) else ""
        ).strip()
        item_identity = (
            f"{_source_template(source.url)}|anchor:{selector_identity}"
            if selector_identity else ""
        )
        outcome = str(receipt.get("outcome") or "")
        attribution = str(receipt.get("attribution") or "unknown")
        source_url = str(receipt.get("sourceUrl") or source.url or "")
        source.materialization_attempts.add("click")
        if self.route_recovery_pending:
            self._telemetry(
                "navigation_recovery.click_started",
                source,
                gateOutcome=outcome or "missing",
            )

        if outcome == "new_page" and attribution == "confirmed":
            landing_page_id = str(receipt.get("landingPageId") or "").strip()
            if not landing_page_id:
                self._record_no_effect(
                    source,
                    item_identity=item_identity,
                    reason="confirmed_new_page_missing_page_id",
                )
                return
            self._bind_landing_page(
                source,
                landing_page_id,
                source_url=source_url,
                item_identity=item_identity,
                landing_url=str(receipt.get("landingUrl") or ""),
                provenance="fleet_click_gate",
            )
            return

        if outcome == "same_page_changed" and attribution == "confirmed":
            source.navigation_kind = "link_click"
            source.navigation_outcome = "same_tab"
            source.source_page_id = source.page_id
            source.source_url = source_url
            source.url = str(receipt.get("landingUrl") or source.url or "")
            # The gate observed a document transition before the next
            # Page.getState.  Clear the listing title so the subsequent landing
            # title is not mistaken for an in-document title-only identity
            # change and appended to an already stable URL identity.
            source.title = ""
            source.materialization_attempts.clear()
            source.pending_recovery_credit = True
            source.pending_recovery_outcome = "same_tab"
            source.item_key = (
                _item_key(source.url, source.title)
                or item_identity
                or source.item_key
            )
            return

        # The gate saw pages appear but will not claim they came from this
        # click. That is not evidence the click did nothing — recording
        # no_effect here is exactly how a working listing pivot got scored as a
        # failure. Park the source and let the model bind it by listing the
        # fleet and claiming the landing page.
        if outcome in {
            "page_inventory_changed",
            "no_navigation_observed_within_window",
        }:
            self.unresolved_landing_sources.add(source.page_id)
            source.navigation_outcome = outcome
            self._telemetry(
                "navigation_recovery.landing_discovery_pending",
                source,
                navigationOutcome=outcome,
                reason=receipt.get("reasonCode"),
                provenance="fleet_click_gate",
            )
            return

        if outcome in {
            "ambiguous",
            "baseline_unavailable",
            "attribution_timeout",
        }:
            source.navigation_outcome = outcome
            self._telemetry(
                "navigation_recovery.outcome",
                source,
                navigationOutcome=outcome,
                reason=receipt.get("reasonCode"),
                provenance="fleet_click_gate",
            )
            self._invalidate_route_preferences(
                f"click_{outcome}",
                source,
            )
            return

        self._record_no_effect(
            source,
            item_identity=item_identity,
            reason=outcome or "missing_gate_receipt",
        )

    @staticmethod
    def _materialization_ready(state: PageContentState) -> bool:
        return (
            "collect_items" in state.materialization_attempts
            or (
                "semantic_tree" in state.materialization_attempts
                and bool({"scroll", "click"} & state.materialization_attempts)
            )
        )

    def observe(
        self,
        *,
        method: str,
        params: Any,
        result: Any,
        step: int = 0,
        upstream_blocker: str = "",
    ) -> Optional[JsonDict]:
        if not self.enabled:
            return None
        page_id = _page_id(params, result)
        data = _response_data(result)
        # Every transition that GRANTS something — recovery credit, a
        # route-source exemption, a content-binding window, an inventory
        # baseline — goes through its registered evaluate_grant policy.
        # Conservative
        # invalidation (clearing stale DOM/region state after a navigation whose
        # result is unknown) deliberately does NOT require success: withholding
        # it would leave the tracker asserting evidence from a page that may no
        # longer exist. Granting on failure is unsafe; invalidating on failure
        # is the safe direction.
        self.last_declaration_accepted = False

        if method == "Page.create":
            context = (
                params.get("_harnessNavigationContext")
                if isinstance(params, dict) else None
            )
            source_page_id = str(
                context.get("sourcePageId") if isinstance(context, dict) else ""
            ).strip()
            create_grant = evaluate_grant(
                kind="route_recovery_page_create",
                method=method,
                result=result,
                page_id=page_id,
            )
            if (
                create_grant.allowed
                and page_id
                and source_page_id
                and page_id != source_page_id
                and self.can_designate_recovery_source(source_page_id)
            ):
                source = self._state(source_page_id)
                target = self._state(page_id)
                self._touch(target)
                target.navigation_kind = "explicit_new_page_pending"
                target.navigation_outcome = "page_created"
                target.source_page_id = source_page_id
                target.source_url = source.url
                self.pending_explicit_recovery_sources[page_id] = source_page_id
                self.known_page_ids.add(page_id)
                self._telemetry(
                    "navigation_recovery.page_created",
                    target,
                    navigationOutcome="page_created",
                    provenance="explicit_page_create_source",
                )
                return target.summary()

        if method == "Page.list":
            inventory_grant = evaluate_grant(
                kind="inventory_baseline",
                method=method,
                result=result,
            )
            if not inventory_grant.allowed:
                # A failed listing is not an inventory statement.
                return None
            current = set(self._page_entries(result))
            self.known_page_ids.update(current)
            self.page_inventory_baselined = True
            return None

        if method == "Page.getState":
            context = (
                params.get("_harnessNavigationContext")
                if isinstance(params, dict) else None
            )
            if (
                isinstance(context, dict)
                and str(context.get("kind") or "") == "route_recovery_claimed_page"
                and page_id
            ):
                claim_grant = evaluate_grant(
                    kind="route_recovery_claim",
                    method=method,
                    result=result,
                    page_id=page_id,
                )
                self.last_declaration_accepted = self.declare_claimed_landing(
                    page_id,
                    str(context.get("sourcePageId") or ""),
                    granted=claim_grant.allowed,
                )

        if not page_id:
            return None
        state = self._state(page_id)
        self._touch(state)
        self.known_page_ids.add(page_id)

        if method in {"Page.navigate", "Page.go", "Page.reload"}:
            if method == "Page.navigate" and _usable_source_url(state.url):
                target_url = str(
                    (params.get("url") if isinstance(params, dict) else "") or ""
                ).strip()
                if target_url and target_url != state.url:
                    state.source_page_id = page_id
                    state.source_url = state.url
            if method in {"Page.navigate", "Page.go"} and self.route_recovery_pending:
                self._telemetry(
                    "navigation_recovery.return_started",
                    state,
                    returnMethod=method,
                )
                self.pending_return_pages.add(page_id)
            state.epoch += 1
            # Reload replaces the document but not the page's mechanically
            # established role as a distinct listing source. Explicit route/
            # history navigation may repurpose the page, so only those clear it.
            if method in {"Page.navigate", "Page.go"}:
                state.is_recovery_source = False
            state.shell_present = False
            state.observed_regions.clear()
            state.missing_regions.clear()
            state.materialization_attempts.clear()
            state.matched_signals.clear()
            state.matched_confirmatory_signals.clear()
            state.content_state = CONTENT_ABSENT
            state.region_states.clear()
            state.region_record_counts.clear()
            state.region_collection_states.clear()
            state.region_exhaustion_evidence.clear()
            state.last_collection_binding = ""
            state.last_collection_binding_status = ""
            state.last_collection_binding_candidate = ""
            state.last_collection_binding_next_instruction = ""
            state.structured_observed_regions.clear()
            state.structured_binding_generations_used.clear()
            state.pending_binding_age = False
            state.decision = "inconclusive"
            state.evidence_strength = "none"
            state.upstream_blocker = ""
            if method == "Page.navigate":
                if page_id in self.pending_explicit_recovery_sources:
                    state.navigation_kind = "explicit_new_page_pending"
                    state.navigation_outcome = "page_navigated"
                else:
                    state.navigation_kind = "direct"
                    state.navigation_outcome = "direct"
                state.pending_recovery_credit = False
                state.pending_recovery_outcome = ""
            elif method == "Page.go":
                state.navigation_kind = "same_page_history"
                state.navigation_outcome = "history_return"

        if method == "Input.click" and not upstream_blocker:
            self._apply_click_gate_receipt(
                state,
                params,
                _click_gate_receipt(result),
            )

        if method == "Page.switchTo" and self.route_recovery_pending:
            self._telemetry(
                "navigation_recovery.return_started",
                state,
                returnMethod=method,
            )
            self._telemetry(
                "navigation_recovery.return_settled",
                state,
                returnMethod=method,
            )

        url = str(data.get("url") or data.get("currentUrl") or "").strip()
        title = str(data.get("title") or "").strip()
        prior_url = state.url
        prior_title = state.title
        if url:
            state.url = url
        if title:
            state.title = title
        if url or title:
            destination_key = _item_key(
                url or state.url,
                title or state.title,
            )
            if (
                url == prior_url
                and title
                and prior_title
                and title != prior_title
            ):
                destination_key = f"{destination_key}|title:{title.casefold()}"
            if destination_key:
                state.item_key = destination_key
        if state.pending_recovery_credit and state.item_key:
            self.recovery_attempts_by_item[state.item_key] = (
                self.recovery_attempts_by_item.get(state.item_key, 0) + 1
            )
            state.recovery_attempts = self.recovery_attempts_by_item[state.item_key]
            state.pending_recovery_credit = False
            if state.pending_recovery_outcome:
                self._telemetry(
                    "navigation_recovery.outcome",
                    state,
                    navigationOutcome=state.pending_recovery_outcome,
                )
                state.pending_recovery_outcome = ""
        elif state.item_key:
            state.recovery_attempts = self.recovery_attempts_by_item.get(
                state.item_key, 0
            )
        if method == "Page.getState" and page_id in self.pending_return_pages:
            self.pending_return_pages.discard(page_id)
            self._telemetry(
                "navigation_recovery.return_settled",
                state,
                returnMethod=state.navigation_kind,
            )

        if method == "Input.scroll":
            state.materialization_attempts.add("scroll")
        if method == "DOM.getAXTree":
            state.materialization_attempts.add("axtree")
        if method == "DOM.getSemanticTree":
            state.materialization_attempts.add("semantic_tree")
        if (
            method == "collect_items"
            and isinstance(result, dict)
            and str(result.get("status") or "") == "done"
        ):
            state.materialization_attempts.add("collect_items")

        # Aging is deferred by one observation on purpose. A contract with N
        # declared regions needs N separate binding calls, and each of those is
        # itself an aging-eligible structured read — so aging eagerly here made
        # satisfying the contract consume the very window meant to protect it:
        # with the default window of 2, three regions could never be held at
        # once (bind A, bind B, bind C -> A is already expired). Deferring lets
        # `observe_content_binding` cancel the pending charge when this same
        # observation produced a binding, so only genuinely unproductive reads
        # spend the window.
        if state.pending_binding_age:
            self._age_structured_observations(state)
            state.pending_binding_age = False
        if method in STRUCTURED_BINDING_AGING_METHODS:
            state.pending_binding_age = True

        if upstream_blocker:
            self._invalidate_route_preferences(
                upstream_blocker,
                state,
                global_scope=True,
            )
            state.upstream_blocker = str(upstream_blocker)
            state.shell_present = False
            state.observed_regions.clear()
            state.missing_regions.clear()
            state.matched_signals.clear()
            state.matched_confirmatory_signals.clear()
            state.decision = "inconclusive"
            state.evidence_strength = "none"
            self._refresh_route_recovery_pending()
            return state.summary()

        # A fresh, classifiable state/DOM observation discharges an earlier
        # upstream auth/challenge/lifecycle exclusion for this page.
        if method in {
            "Page.getState",
            "DOM.getAXTree",
            "DOM.getSemanticTree",
            "DOM.getText",
        }:
            state.upstream_blocker = ""

        if method in {
            "DOM.getAXTree",
            "DOM.getSemanticTree",
            "DOM.getText",
            "Runtime.evaluate",
        }:
            dom_text, node_count = _dom_snapshot(result)
            self._evaluate(state, result)
            state.last_dom_text = dom_text
            state.last_dom_node_count = node_count
            self._settle_explicit_recovery_source(
                state,
                evidence_kind=method,
            )
            return state.summary()
        if method == "collect_items":
            self._observe_collection(state, params, result)
            self._settle_explicit_recovery_source(
                state,
                evidence_kind="collect_items",
            )
            return state.summary()
        return None

    def _age_structured_observations(self, state: PageContentState) -> None:
        """Spend one observation from every provisional binding, expiring at zero."""
        expired: List[str] = []
        for region_id, receipt in state.structured_observed_regions.items():
            # The call which established the binding is marked at the current
            # observation order and does not consume its own bounded window.
            if int(receipt.get("boundAtOrder") or 0) >= state.observation_order:
                continue
            remaining = max(0, int(receipt.get("remainingObservations") or 0) - 1)
            receipt["remainingObservations"] = remaining
            if remaining <= 0:
                expired.append(region_id)
        for region_id in expired:
            state.structured_observed_regions.pop(region_id, None)

    def observe_content_binding(
        self,
        *,
        method: str,
        params: Any,
        result: Any,
        binding: Any,
    ) -> Optional[JsonDict]:
        """Accept bounded Harness-only structured evidence for one region."""
        if not self.enabled or not isinstance(binding, dict):
            return None
        page_id = _page_id(params, result)
        region_id = str(binding.get("regionId") or "").strip()
        expected_ids = {
            str(spec.get("id") or "")
            for spec in self.config.get("expected_regions") or []
        }
        state = self.pages.get(page_id)
        if not page_id or state is None or region_id not in expected_ids:
            return {
                "status": "rejected",
                "reason": "invalid_content_binding",
                "validRegionIds": sorted(expected_ids),
            }
        if method not in {
            "DOM.getText", "DOM.getAttribute", "DOM.getSemanticTree",
            "DOM.getAXTree", "Runtime.evaluate", "collect_items",
        }:
            return {
                "status": "rejected",
                "reason": "content_binding_requires_structured_read",
            }
        # This grants CONTENT evidence, the most consequential thing the tracker
        # hands out, so it uses the shared verdict rather than its own error
        # check. The private version missed a browser error carrying only a
        # numeric code and every auto-HITL interrupt, so a failed or paused
        # structured read could still register a region as observed.
        binding_grant = evaluate_grant(
            kind="content_binding",
            method=method,
            result=result,
            page_id=page_id,
        )
        if binding_grant.call_outcome.interrupted:
            return {
                "status": "rejected",
                "reason": "structured_read_interrupted_by_hitl",
            }
        if not binding_grant.call_outcome.succeeded:
            return {
                "status": "rejected",
                "reason": "structured_read_failed",
                "callVerdict": binding_grant.call_outcome.verdict,
            }
        if not binding_grant.allowed:
            return {
                "status": "rejected",
                "reason": "structured_read_missing_method_evidence",
            }
        # Same page+epoch+region cannot refresh the window indefinitely.
        prior = state.structured_observed_regions.get(region_id)
        if isinstance(prior, dict) and int(prior.get("epoch") or -1) == state.epoch:
            return {"status": "unchanged", "reason": "binding_already_active"}
        generation_key = f"{state.epoch}:{region_id}"
        if generation_key in state.structured_binding_generations_used:
            return {"status": "rejected", "reason": "binding_window_consumed"}
        receipt = {
            "epoch": state.epoch,
            "method": method,
            "boundAtOrder": state.observation_order,
            "remainingObservations": 2,
        }
        state.structured_observed_regions[region_id] = receipt
        state.structured_binding_generations_used.add(generation_key)
        # This observation earned a binding, so it is productive work on the
        # contract rather than wandering: cancel its pending aging charge.
        state.pending_binding_age = False
        self._decide(state)
        return {"status": "accepted", "regionId": region_id, **receipt}

    @staticmethod
    def _row_value_present(value: Any) -> bool:
        if value is None:
            return False
        if isinstance(value, str):
            return bool(value.strip())
        if isinstance(value, (list, dict, tuple, set)):
            return bool(value)
        return True

    def observe_contract_validated_artifact(
        self,
        *,
        rows: Any,
        artifact_name: str,
        saved_path: str,
    ) -> JsonDict:
        """Create authoritative phase credit from contract-valid row fields."""
        # Any persistence attempt ends the provisional suppression window,
        # regardless of whether contract validation succeeds.
        for state in self.pages.values():
            state.structured_observed_regions.clear()
        if not isinstance(rows, list) or not rows or not all(
            isinstance(row, dict) for row in rows
        ):
            return {"creditedRegions": [], "reason": "no_validated_rows"}
        observed_urls = {
            _item_key(state.url): state.url
            for state in self.pages.values()
            if _item_key(state.url)
        }
        rows_by_url: Dict[str, List[JsonDict]] = {}
        for row in rows:
            page_key = _item_key(str(row.get("pageUrl") or ""))
            # pageUrl is explicit row provenance, but credit it only when the
            # tracker actually observed that destination in this phase.
            if page_key and page_key in observed_urls:
                rows_by_url.setdefault(page_key, []).append(row)

        credited_by_url: Dict[str, List[str]] = {}
        for page_key, page_rows in rows_by_url.items():
            for spec in self.config.get("expected_regions") or []:
                region_id = str(spec.get("id") or "")
                aliases = [str(value) for value in spec.get("fields") or []]
                exact = [
                    alias for alias in aliases
                    if all(alias in row for row in page_rows)
                ]
                # Declarations without a field mapping cannot manufacture
                # artifact credit. Marker-only regions stay observation-based.
                evidence_field = next(
                    (
                        field for field in exact
                        if all(
                            self._row_value_present(row.get(field))
                            for row in page_rows
                        )
                    ),
                    "",
                )
                if evidence_field:
                    self.validated_artifact_regions_by_url.setdefault(
                        page_key, set(),
                    ).add(region_id)
                    credited_by_url.setdefault(page_key, []).append(region_id)
        credited = sorted({
            region
            for regions in credited_by_url.values()
            for region in regions
        })
        receipt = {
            "artifactName": artifact_name,
            "savedPath": saved_path,
            "rowCount": len(rows),
            "creditedRegions": credited,
            "creditedRegionsByPageUrl": {
                observed_urls.get(page_key, page_key): sorted(set(regions))
                for page_key, regions in credited_by_url.items()
            },
            "unboundRowCount": len(rows) - sum(
                len(page_rows) for page_rows in rows_by_url.values()
            ),
        }
        self.validated_artifact_receipts.append(receipt)
        return receipt

    def _validated_regions_for_page(self, state: PageContentState) -> Set[str]:
        return set(
            self.validated_artifact_regions_by_url.get(_item_key(state.url), set())
        )

    def observe_failed_artifact_attempt(self) -> None:
        for state in self.pages.values():
            state.structured_observed_regions.clear()

    def _page_entries(self, result: Any) -> Iterable[str]:
        found: Set[str] = set()

        def visit(value: Any, depth: int = 0) -> None:
            if depth > 6:
                return
            if isinstance(value, dict):
                raw = value.get("pageId") or value.get("page_id")
                if isinstance(raw, str) and raw.strip():
                    found.add(raw.strip())
                for item in value.values():
                    if isinstance(item, (dict, list)):
                        visit(item, depth + 1)
            elif isinstance(value, list):
                for item in value:
                    visit(item, depth + 1)

        visit(result)
        return found

    def _evaluate(self, state: PageContentState, result: Any) -> None:
        haystack = "\n".join(_strings(result)).casefold()
        expected = self.config.get("expected_regions") or []
        shell = self.config.get("shell_markers") or []

        marker_regions = {
            str(spec["id"])
            for spec in expected
            if any(str(marker).casefold() in haystack for marker in spec["markers"])
        }
        # A focused DOM.getText may omit a region already established by a
        # full SemanticTree. Completion evidence is monotonic within one
        # document epoch; navigation above clears it authoritatively.
        state.observed_regions.update(marker_regions)
        for spec in expected:
            region_id = str(spec["id"])
            state.region_states[region_id] = classify_region_materialization(
                marker_seen=region_id in state.observed_regions,
                min_records=int(spec.get("min_records") or 0),
                record_count=state.region_record_counts.get(region_id, 0),
                collection_state=state.region_collection_states.get(region_id, ""),
                exhaustion_evidence=state.region_exhaustion_evidence.get(region_id),
            )

        if shell:
            state.shell_present = state.shell_present or all(
                any(str(marker).casefold() in haystack for marker in spec["markers"])
                for spec in shell
            )
        else:
            data = _response_data(result)
            node_count = response_node_count(data) or 0
            state.shell_present = state.shell_present or (
                bool(state.title or state.url)
                and (node_count >= 30 or len(haystack) >= 500)
            )

        for signal in self.config.get("suppression_signals") or []:
            name = str(signal.get("name") or "")
            expected_value = signal.get("match", True)
            if _structured_signal_matches(
                result, name=name, expected=expected_value
            ) or _text_signal_matches(
                haystack, name=name, expected=expected_value
            ):
                state.matched_signals.add(name)
                strength = str(signal.get("strength") or "supporting").casefold()
                if strength in _CONFIRMATORY_SIGNAL_STRENGTHS:
                    state.matched_confirmatory_signals.add(name)

        self._recompute_content_state(state)
        self._decide(state)

    def _collection_region_binding(self, params: Any) -> tuple[str, str, str]:
        """Return (trusted region, status, telemetry-only candidate).

        Collection evidence may mutate completeness state only when the caller
        binds it to a declared region explicitly.  A unique inferred region is
        useful for diagnostics, but is not proof that an unrelated collection
        belongs to that region.
        """
        expected = self.config.get("expected_regions") or []
        expected_ids = {str(spec["id"]) for spec in expected}
        raw_region = str(
            (params.get("regionId") if isinstance(params, dict) else "") or ""
        ).strip()
        if raw_region:
            return (
                (raw_region, "explicit", "")
                if raw_region in expected_ids
                else ("", "invalid_region_id", "")
            )
        collection_field = str(
            (params.get("collectionField") if isinstance(params, dict) else "") or ""
        ).strip()
        if collection_field:
            if collection_field in expected_ids:
                return collection_field, "collection_field", ""
            field_tokens = _field_tokens(collection_field)
            alias_matches = [
                str(spec["id"])
                for spec in expected
                if field_tokens and field_tokens & {
                    token
                    for alias in spec.get("fields", [])
                    for token in _field_tokens(alias)
                }
            ]
            if len(alias_matches) == 1:
                return alias_matches[0], "collection_field_alias", ""
            if len(alias_matches) > 1:
                return "", "ambiguous_collection_field", ""
            return "", "invalid_collection_field", ""
        counted = [
            str(spec["id"]) for spec in expected
            if int(spec.get("min_records") or 0) > 0
        ]
        if len(counted) == 1:
            return "", "explicit_binding_required", counted[0]
        if len(expected_ids) == 1:
            return "", "explicit_binding_required", next(iter(expected_ids))
        return "", "ambiguous", ""

    def collection_contract(self, params: Any) -> JsonDict:
        """Return the declared contract for a trusted collection binding.

        Composite tools use this read-only view to align their mechanical stop
        target with ``min_records``.  Inferred candidates are deliberately not
        returned as contracts: an unrelated collection must never inherit a
        counted region merely because it is the only candidate.
        """
        region_id, binding_status, candidate = self._collection_region_binding(
            params
        )
        receipt: JsonDict = {
            "regionId": region_id or None,
            "bindingStatus": binding_status,
            "candidateRegionId": candidate or None,
            "minRecords": 0,
        }
        if not region_id:
            return receipt
        spec = next(
            (
                item
                for item in (self.config.get("expected_regions") or [])
                if str(item.get("id") or "") == region_id
            ),
            None,
        )
        if isinstance(spec, dict):
            receipt["minRecords"] = max(
                0,
                int(spec.get("min_records") or 0),
            )
        return receipt

    def _observe_collection(
        self,
        state: PageContentState,
        params: Any,
        result: Any,
    ) -> None:
        region_id, binding_status, candidate = self._collection_region_binding(
            params
        )
        state.last_collection_binding = region_id
        state.last_collection_binding_status = binding_status
        state.last_collection_binding_candidate = candidate
        state.last_collection_binding_next_instruction = ""
        if not region_id:
            expected_ids = [
                str(spec["id"])
                for spec in self.config.get("expected_regions") or []
            ]
            valid_ids = ", ".join(expected_ids)
            if binding_status in {
                "invalid_region_id",
                "invalid_collection_field",
                "ambiguous_collection_field",
                "explicit_binding_required",
                "ambiguous",
            }:
                candidate_hint = (
                    f" Suggested regionId: {candidate}." if candidate else ""
                )
                state.last_collection_binding_next_instruction = (
                    "This collection was not counted toward content completeness."
                    f" Retry collect_items with an explicit regionId from: {valid_ids}."
                    f"{candidate_hint}"
                )
            return
        try:
            record_count = max(0, int(
                result.get("rowCount", 0) if isinstance(result, dict) else 0
            ))
        except (TypeError, ValueError):
            record_count = 0
        collection_state = str(
            result.get("collectionState") if isinstance(result, dict) else ""
        ).strip()
        evidence = (
            result.get("exhaustionEvidence")
            if isinstance(result, dict)
            and isinstance(result.get("exhaustionEvidence"), dict)
            else None
        )
        prior_region_state = state.region_states.get(region_id, CONTENT_ABSENT)
        prior_collection_state = state.region_collection_states.get(region_id, "")
        prior_evidence = state.region_exhaustion_evidence.get(region_id)
        state.region_record_counts[region_id] = max(
            record_count,
            state.region_record_counts.get(region_id, 0),
        )
        state.region_collection_states[region_id] = collection_state
        if evidence is not None:
            state.region_exhaustion_evidence[region_id] = dict(evidence)
        else:
            state.region_exhaustion_evidence.pop(region_id, None)
        if record_count > 0:
            state.observed_regions.add(region_id)
        spec = next(
            item for item in (self.config.get("expected_regions") or [])
            if str(item["id"]) == region_id
        )
        state.region_states[region_id] = classify_region_materialization(
            marker_seen=region_id in state.observed_regions,
            min_records=int(spec.get("min_records") or 0),
            record_count=state.region_record_counts.get(region_id, 0),
            collection_state=collection_state,
            exhaustion_evidence=evidence,
        )
        # Completion evidence is monotonic within one navigation epoch, while
        # still allowing a stronger proof to replace a weaker one:
        # target_reached > explicitly_exhausted > stalled/blocked.  This keeps
        # redundant probes from producing contradictory telemetry without
        # blocking an explicit-exhaustion proof from later reaching its target.
        prior_priority = _collection_completion_evidence_priority(
            prior_collection_state,
            prior_evidence,
        )
        current_priority = _collection_completion_evidence_priority(
            collection_state,
            evidence,
        )
        if prior_region_state == CONTENT_MATERIALIZED:
            state.region_states[region_id] = CONTENT_MATERIALIZED
            if current_priority <= prior_priority:
                state.region_collection_states[region_id] = prior_collection_state
                if isinstance(prior_evidence, dict):
                    state.region_exhaustion_evidence[region_id] = dict(prior_evidence)
                else:
                    state.region_exhaustion_evidence.pop(region_id, None)
        if state.region_states[region_id] != CONTENT_ABSENT:
            state.shell_present = True
        self._recompute_content_state(state)
        self._decide(state)

    def _recompute_content_state(self, state: PageContentState) -> None:
        expected_ids = {
            str(spec["id"]) for spec in self.config.get("expected_regions") or []
        }
        materialized = {
            region_id for region_id in expected_ids
            if state.region_states.get(region_id) == CONTENT_MATERIALIZED
        }
        state.missing_regions = expected_ids - materialized
        if expected_ids and materialized == expected_ids:
            state.content_state = CONTENT_MATERIALIZED
        elif materialized or any(
            state.region_states.get(region_id) == CONTENT_SHELL_SEEN
            for region_id in expected_ids
        ):
            # A partially materialized contract is evidence that the page
            # shell exists, not that all content is absent.  Missing regions
            # remain eligible for materialization/recovery vetoes.
            state.content_state = CONTENT_SHELL_SEEN
            state.shell_present = True
        else:
            state.content_state = CONTENT_ABSENT

    def _decide(self, state: PageContentState) -> None:
        expected_ids = {
            str(spec["id"]) for spec in self.config.get("expected_regions") or []
        }

        if expected_ids and state.content_state == CONTENT_MATERIALIZED:
            state.decision = "complete"
            self._promote_route_preference(state)
            self._refresh_route_recovery_pending()
            return
        provisional_missing = (
            set(state.missing_regions) & set(state.structured_observed_regions)
        )
        if provisional_missing:
            # Structured values were observed, but only a contract-valid
            # artifact can turn them into phase-level completion credit.
            state.decision = "record_extraction_required"
            state.evidence_strength = "structured_observed"
            self._refresh_route_recovery_pending()
            return
        missing_enough = bool(state.missing_regions) and (
            len(expected_ids) == 1 or len(state.missing_regions) >= 2
        )
        confirmatory = bool(state.matched_confirmatory_signals)
        state.evidence_strength = "confirmatory" if confirmatory else "heuristic"
        route_candidate = state.navigation_kind in {"direct", "unknown"}
        materialization_ready = self._materialization_ready(state)
        if (
            expected_ids
            and state.shell_present
            and materialization_ready
            and not (state.observed_regions & expected_ids)
            and not confirmatory
            and not state.marker_suspect_emitted
        ):
            # The page loaded, we did the work to make content appear, and not
            # one declared marker was ever seen. "Every marker missed" and
            # "some markers missed" are different facts: the first is at least
            # as likely to mean our declaration is wrong as it is to mean the
            # site withheld content, and blaming the site sends the worker down
            # route-recovery for a defect that lives in the plan.
            #
            # Gated on materialization_ready for the same reason the route
            # branch below is: before a semantic read plus a reveal attempt,
            # zero hits is simply the normal state of a page nobody has looked
            # at yet, and neither claim is warranted.
            #
            # Fires at most once per page, then falls through to the ordinary
            # ladder, so a genuine suppression costs one round rather than a
            # verdict. A confirmatory suppression signal skips this entirely —
            # there the site has already said so itself.
            state.marker_suspect_emitted = True
            state.decision = MARKER_DECLARATION_SUSPECT
            state.evidence_strength = "declaration_suspect"
            self._telemetry(
                "content_completeness.marker_declaration_suspect",
                state,
                declaredRegions=sorted(expected_ids),
                markerSources=sorted({
                    str(spec.get("marker_source") or MARKER_SOURCE_DECLARED)
                    for spec in self.config.get("expected_regions") or []
                }),
            )
            self._refresh_route_recovery_pending()
            return
        collection_blocked = any(
            state.region_collection_states.get(region_id) == "blocked"
            for region_id in state.missing_regions
        )
        if state.shell_present and missing_enough and collection_blocked:
            state.decision = BLOCKED_CONTENT_SUPPRESSION
            self._refresh_route_recovery_pending()
            return
        if state.shell_present and missing_enough and route_candidate:
            # A confirmatory server/page signal can prove route suppression
            # without further DOM work.  Heuristic absence must first survive
            # one semantic-tree read plus a bounded reveal/scroll attempt.
            state.decision = (
                ROUTE_RECOVERY_REQUIRED
                if confirmatory or materialization_ready
                else "inconclusive"
            )
            if state.decision == ROUTE_RECOVERY_REQUIRED:
                self._record_direct_suppression(state)
            self._refresh_route_recovery_pending()
            return
        if (
            state.shell_present
            and missing_enough
            and state.navigation_kind == "link_click"
            and confirmatory
        ):
            max_attempts = int(
                (self.config.get("recovery") or {}).get("max_attempts_per_item", 2)
            )
            state.decision = (
                BLOCKED_CONTENT_SUPPRESSION
                if state.recovery_attempts >= max_attempts
                else ROUTE_RECOVERY_REQUIRED
            )
            self._refresh_route_recovery_pending()
            if state.decision == BLOCKED_CONTENT_SUPPRESSION:
                self._telemetry("navigation_recovery.exhausted", state)
            self._invalidate_route_preferences(
                "click_through_content_missing",
                state,
            )
            return
        if (
            state.navigation_kind == "link_click"
            and missing_enough
            and materialization_ready
        ):
            self._invalidate_route_preferences(
                "required_markers_missing",
                state,
            )
        state.decision = "inconclusive"
        self._refresh_route_recovery_pending()

    def unresolved_observation(self) -> Optional[JsonDict]:
        """Return the latest unresolved tracker interpretation for diagnostics.

        This is not a terminal decision, validation veto, scheduling status, or
        completion receipt. Production handoff uses ``summaries()`` and strips
        these historical decision labels down to their underlying facts. The
        method remains a test/diagnostic observation API only.
        """
        blocked_candidates = [
            state for state in self.pages.values()
            if state.decision == BLOCKED_CONTENT_SUPPRESSION
            and not state.upstream_blocker
            and not state.is_recovery_source
        ]
        route_candidates = [
            state for state in self.pages.values()
            if state.decision == ROUTE_RECOVERY_REQUIRED
            and not state.upstream_blocker
            and not state.is_recovery_source
        ]
        pending_materialization = [
            state for state in self.pages.values()
            if (
                state.decision == "inconclusive"
                and not state.upstream_blocker
                and not state.is_recovery_source
                and state.shell_present
                and bool(state.missing_regions)
                and (
                    state.content_state == CONTENT_SHELL_SEEN
                    or bool(state.matched_confirmatory_signals)
                    or not self._materialization_ready(state)
                )
            )
        ]
        pending_record_extraction = [
            state for state in self.pages.values()
            if state.decision == "record_extraction_required"
            and not state.upstream_blocker
            and not state.is_recovery_source
            and bool(
                set(state.missing_regions) - self._validated_regions_for_page(state)
            )
        ]
        marker_suspect = [
            state for state in self.pages.values()
            if state.decision == MARKER_DECLARATION_SUSPECT
            and not state.upstream_blocker
            and not state.is_recovery_source
            and bool(
                set(state.missing_regions) - self._validated_regions_for_page(state)
            )
        ]
        blocked_candidates = [
            state for state in blocked_candidates
            if set(state.missing_regions) - self._validated_regions_for_page(state)
        ]
        route_candidates = [
            state for state in route_candidates
            if set(state.missing_regions) - self._validated_regions_for_page(state)
        ]
        pending_materialization = [
            state for state in pending_materialization
            if set(state.missing_regions) - self._validated_regions_for_page(state)
        ]
        candidates = [
            *blocked_candidates,
            *route_candidates,
            *pending_materialization,
            *pending_record_extraction,
            *marker_suspect,
        ]
        if not candidates:
            return None
        latest = max(candidates, key=lambda item: item.observation_order)
        blocked = latest.decision == BLOCKED_CONTENT_SUPPRESSION
        materialization_only = latest.decision == "inconclusive"
        extraction_only = latest.decision == "record_extraction_required"
        marker_suspect_only = latest.decision == MARKER_DECLARATION_SUSPECT
        return {
            "category": (
                BLOCKED_CONTENT_SUPPRESSION
                if blocked else "route_sensitive_content_suppression"
            ),
            "decision": (
                "record_extraction_required"
                if extraction_only else
                "materialization_required"
                if materialization_only else latest.decision
            ),
            "evidenceStrength": latest.evidence_strength,
            "configSource": self.config_source,
            "page": latest.summary(),
            "next_instruction": (
                _DECISION_INSTRUCTIONS[MARKER_DECLARATION_SUSPECT]
                if marker_suspect_only else
                (
                    "Structured values for a declared region were observed but"
                    " have not yet earned contract-valid artifact credit. Call"
                    " record_extraction now; do not route-recover first. This"
                    " provisional evidence expires after two later observations."
                )
                if extraction_only else
                (
                    "The page shell exists and task-required regions are still"
                    " missing, but bounded materialization is not exhausted."
                    " Scroll/reveal the target region and run"
                    " DOM.getSemanticTree before declaring semantic absence."
                )
                if materialization_only else (
                    (
                        "The bounded listing-link recovery budget for this item"
                        " is exhausted while required content remains suppressed."
                        " Finalize with blocked_content_suppression; do not"
                        " relabel this outcome as target_absent or"
                        " instruction_infeasible."
                    )
                    if blocked else
                    "The current navigation epoch has a complete page shell but"
                    " task-required regions are missing. Do not declare"
                    " target_absent or instruction_infeasible. Return to a"
                    " search/listing source, click the real item anchor, then"
                    " re-run Page.getState and DOM probes."
                )
            ),
        }

    def summaries(self) -> List[JsonDict]:
        summaries = [state.summary() for state in self.pages.values()]
        if self.validated_artifact_regions_by_url:
            summaries.append({
                "scope": "phase_artifact",
                "validatedArtifactRegionsByPageUrl": {
                    page_url: sorted(regions)
                    for page_url, regions in sorted(
                        self.validated_artifact_regions_by_url.items()
                    )
                },
                "receipts": list(self.validated_artifact_receipts),
            })
        return summaries
