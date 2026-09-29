"""
harness.tools.lead_tools - LeadAgent tool schemas and dispatch factory.
"""

import copy
import hashlib
import json
import time
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional, Set, Tuple

from harness.evidence.extraction_artifacts import (
    save_extraction_artifact,
    validate_extraction_rows,
)
from harness.evidence.artifact_evidence import VALIDATOR_TYPES
from harness.fleet.coordinator import VALID_PAGE_POLICIES, VALID_REUSE_SCOPES
from harness.runtime.lifecycle import LifecycleContext, lifecycle_for
from harness.tools.local_fs import local_fs_read, local_fs_search
from harness.tools.path_authorization import authorize_tool_call
from harness.prompts import read_harness_guide
from harness.prompts import search_harness_guides
from harness.task_control import (
    EXECUTION_ROLES,
    VALID_STAGE_HINTS,
    assess_batch_source_binding,
    direct_batch_rows_provenance_errors,
    dispatch_wave_blockers,
    find_phase,
    mark_phase_exhausted_if_needed,
    materialize_batch_rows_from_source,
    phase_contract,
    replan_checkpoint_spawn_rejection,
    schedule_snapshot,
    load_task_state,
    write_task_state,
)
from harness.task_control.transport_recovery import (
    note_transport_recovery_required,
    record_transport_recovery_probe,
)
from harness.results.completion_receipt import (
    artifact_generation_view,
    build_completion_receipt,
)
from harness.evidence.field_semantics import (
    array_fields_without_semantic_evidence,
    build_field_semantic_worklist,
    build_semantic_fact_index,
    SEMANTIC_PROJECTION_VERSION,
    review_field_semantics,
)
from harness.results.numeric_facts import (
    build_numeric_fact_index,
    extract_numeric_claims,
    reconcile_numeric_claims,
)
from harness.tools.argument_pipeline import SchemaIssue
from harness.tools.argument_pipeline import apply_registered_tool_defaults
from harness.tools.argument_pipeline import prepare_model_tool_call
from harness.tools.argument_pipeline import tool_argument_error
from harness.tools.argument_pipeline import validate_registered_tool_call
from harness.tools.loop_guard import check_tool_call_loop
from harness.tools.registry import ToolContext, ToolRegistry
from harness.utils import (
    JsonDict,
    contains_semantic_marker,
    optional_int,
)


LeadToolDispatcher = Callable[[JsonDict], Awaitable[Tuple[JsonDict, bool]]]

LEAD_TOOLS = ToolRegistry("lead_agent")


_OPTIONAL_IDENTIFIER_FIELDS = {
    "spawn_browser_agent": {
        "name",
        "phase_id",
        "preferred_slot_id",
        "reuse_from_worker_id",
        "session_key",
        # Retain null/empty normalization for stale model payloads. Any
        # non-empty value is rejected by _lead_spawn_browser_agent below.
        "fleet_id",
    },
}
_OPTIONAL_WORKER_CONTRACT_IDENTIFIER_FIELDS = {"session_key", "fleet_id"}
_AUTO_BIND_SEMANTIC_OVERRIDE_KEYS = frozenset({
    "expected_artifact",
    "validators",
    "input_artifacts",
    "objective",
    "worker_task",
    "stage_hint",
    "execution_role",
    "batch_policy",
    "replan_checkpoint_id",
    "batch_source",
    "cohort_source",
    "row_selection",
    "batch_rows",
})


def _normalize_optional_identifiers(
    tool_name: str,
    tool_input: JsonDict,
) -> Tuple[JsonDict, List[str]]:
    """Treat model null spellings as absence only for declared identifiers."""
    fields = _OPTIONAL_IDENTIFIER_FIELDS.get(tool_name, set())
    if not fields:
        return tool_input, []
    normalized = dict(tool_input)
    changed: List[str] = []
    for field in fields:
        if field not in normalized:
            continue
        value = normalized.get(field)
        if value is None or (
            isinstance(value, str)
            and value.strip().lower() in {"", "null"}
        ):
            normalized.pop(field, None)
            changed.append(field)
    contract = normalized.get("worker_contract")
    if isinstance(contract, dict):
        normalized_contract = dict(contract)
        for field in _OPTIONAL_WORKER_CONTRACT_IDENTIFIER_FIELDS:
            if field not in normalized_contract:
                continue
            value = normalized_contract.get(field)
            if value is None or (
                isinstance(value, str)
                and value.strip().lower() in {"", "null"}
            ):
                normalized_contract.pop(field, None)
                changed.append(f"worker_contract.{field}")
        normalized["worker_contract"] = normalized_contract
    return normalized, sorted(changed)


def _nullable(type_name: str) -> JsonDict:
    return {"type": [type_name, "null"]}


def _auth_verification_schema() -> JsonDict:
    return {
        "type": "object",
        "description": (
            "Optional pre-HITL proof contract for durable session reuse. Both"
            " the protected URL and an authenticated UI marker must match;"
            " without this contract HITL may clear the current barrier but the"
            " fleet is not persisted as a verified login session."
        ),
        "properties": {
            "protected_url_prefixes": {
                "type": "array",
                "minItems": 1,
                "items": {"type": "string"},
            },
            "authenticated_markers": {
                "type": "array",
                "minItems": 1,
                "description": (
                    "Stable visible AX nodes that prove authentication. Match"
                    " is exact on role+name; ordinary page text does not count,"
                    " and a node whose AXTree line carries a hidden or blocked"
                    " layout flag is rejected. Those flags are sparse, so their"
                    " absence is not a visibility guarantee — choose markers"
                    " that are genuinely on screen when signed in, not ones"
                    " that merely exist in the tree."
                ),
                "items": {
                    "type": "object",
                    "properties": {
                        "role": {
                            "type": "string",
                            "pattern": "^[A-Za-z][A-Za-z0-9_-]*$",
                        },
                        "name": {"type": "string", "minLength": 3},
                        "match": {"type": "string", "enum": ["exact"]},
                    },
                    "required": ["role", "name"],
                    "additionalProperties": False,
                },
            },
        },
        "required": ["protected_url_prefixes", "authenticated_markers"],
        "additionalProperties": False,
    }


def _content_completeness_schema() -> JsonDict:
    marker = {
        "type": ["string", "object"],
        "description": (
            "A task-declared semantic region name, or an object with id/name"
            " plus marker/markers strings derived from the user contract,"
            " selected strategy/skill, or verified live evidence. Set"
            " min_records only for repeated-record targets; it is a trigger"
            " line, not a hard minimum when explicit exhaustion is proven."
        ),
        "properties": {
            "id": {"type": "string"},
            "name": {"type": "string"},
            "marker": {"type": "string"},
            "marker_source": {
                "type": "string",
                "description": "Origin metadata preserved from a normalized region observation.",
            },
            "markers": {
                "type": "array",
                "items": {"type": "string"},
            },
            "fields": {
                "type": "array",
                "minItems": 1,
                "items": {"type": "string", "minLength": 1},
                "description": (
                    "Artifact field aliases that uniquely bind a"
                    " collect_items collectionField to this region."
                ),
            },
            "min_records": {"type": "integer", "minimum": 1},
        },
        "additionalProperties": False,
    }
    return {
        "type": "object",
        "description": (
            "Optional observation declaration for task-required regions. It"
            " reports shell, marker, region, and suppression-signal facts to"
            " the model; it does not choose a route, prove absence, or decide"
            " completion."
        ),
        "properties": {
            "shell_markers": {"type": "array", "items": marker},
            "expected_regions": {
                "type": "array",
                "minItems": 1,
                "items": marker,
            },
            "suppression_signals": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "source": {"type": "string"},
                        "locator": {"type": "string"},
                        "match": {},
                        "strength": {
                            "type": "string",
                            "enum": ["supporting", "confirmatory"],
                        },
                    },
                    "required": ["name"],
                    "additionalProperties": True,
                },
            },
            "recovery": {
                "type": "object",
                "description": (
                    "Legacy normalized observation metadata, retained when"
                    " resubmitting an existing plan. New plans should omit it;"
                    " this is not an instruction to choose a navigation route."
                ),
                "properties": {
                    "mode": {"type": "string"},
                    "max_attempts_per_item": {"type": "integer", "minimum": 1, "maximum": 5},
                },
                "additionalProperties": False,
            },
        },
        "required": ["expected_regions"],
        "additionalProperties": False,
    }


def _validator_item_schema() -> JsonDict:
    """Typed schema for one plan validator.

    The type enum is generated from VALIDATOR_TYPES (single source of truth)
    so the model sees the exact canonical names UP FRONT — task 9d5655d3
    burned two plan rejections learning them from error messages because the
    old schema was an opaque `additionalProperties: true` object.
    """
    return {
        "type": "object",
        "properties": {
            "type": {
                "type": "string",
                "enum": sorted(VALIDATOR_TYPES | {"allowed_domain"}),
                "description": (
                    "Validator kind; use only the enum names. Legacy allowed_domain is accepted as advisory only; do not add domain affiliation rules. path_pattern is a parameter of file_integrity, not a validator kind. See lead.plan-contracts for complete examples. range is for numeric scalar values; "
                    "array_length is for the number of items in an array field. "
                    "For an up-to-N request, set max=N only unless the user "
                    "explicitly requires a minimum or exact count."
                ),
            },
            "field": {
                "type": "string",
                "description": "Target field for single-field validators (range/array_length/url_pattern/field_pattern).",
            },
            "fields": {
                "type": ["array", "object"],
                "items": {"type": "string"},
                "description": (
                    "Field-name array for required_fields/field_nonempty/unique."
                    " field_provenance also accepts a field-name-to-evidence-spec"
                    " object; preserve its evidence_field, evidence_aliases,"
                    " source_tool_field, selector_field and require_* settings"
                    " when resubmitting a normalized plan."
                ),
            },
            "count": {
                "type": "integer",
                "minimum": 1,
                "description": "Row count for exact_rows (aliases value/exact accepted).",
            },
            "value": {"type": "integer", "minimum": 1},
            "min": {
                "type": "number",
                "description": "Inclusive bound for range; array_length requires a non-negative integer.",
            },
            "max": {
                "type": "number",
                "description": "Inclusive bound for range; array_length requires a non-negative integer.",
            },
            "pattern": {
                "type": "string",
                "description": "Regex observation for url_pattern/field_pattern; enforcement=literal is only for explicit literal user/protocol requirements.",
            },
            "enforcement": {"type": "string", "enum": ["advisory", "literal"], "description": "Patterns and cross_field_contains default to advisory. Use literal only when the user/protocol explicitly requires the exact string relation; never for inferred domain affiliation."},
            "values": {
                "type": "array",
                "items": {},
                "description": (
                    "Exact required value set for set_equals; use it for any"
                    " concrete identity cohort, including contiguous ranks"
                    " 11-20 and non-contiguous ranks [38, 40]."
                ),
            },
            "min_files": {
                "type": "integer",
                "minimum": 0,
                "description": "Explicit file count. file_integrity defaults to 0 and checks every declared file; upload/image receipt checks default to 1.",
            },
            "min_bytes": {
                "type": "integer",
                "minimum": 0,
                "description": "Minimum on-disk byte size for file_integrity.",
            },
            "extensions": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Allowed file extensions for file_integrity.",
            },
            "sha256": {"type": "string"},
            "path_pattern": {
                "type": "string",
                "description": "Python regular expression matched against each attributed file path.",
            },
            "path_fields": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Artifact row fields containing delivery paths. Declare these"
                    " explicitly to verify that exact population, including empty"
                    " populations. Without them, attributed phase files are checked."
                ),
            },
        },
        "required": ["type"],
        # Discriminate by validator kind: accepting provenance maps must not
        # make a map valid for required_fields, field_nonempty or unique.
        # anyOf is supported by the shared local validator and avoids oneOf.
        "anyOf": [
            {"properties": {
                "type": {"enum": sorted((VALIDATOR_TYPES | {"allowed_domain"}) - {"field_provenance"})},
                "fields": {"type": "array", "items": {"type": "string"}},
            }},
            {"properties": {"type": {"const": "field_provenance"}}},
        ],
        "additionalProperties": True,
    }


def _expected_artifact_schema() -> JsonDict:
    # `type: [...]` is already used by this tool surface (_nullable). Avoid
    # introducing oneOf here: several Anthropic-compatible gateways implement
    # only a conservative JSON-schema subset even though native providers accept
    # oneOf. Runtime normalization still validates object field specs fully.
    field_items: JsonDict = {
        "type": ["string", "object"],
        "description": (
            "A field name string, or an object field spec using name/field/key"
            " plus optional type/allow_empty/nonempty metadata. For a repeated"
            " nested collection, use the canonical shape"
            " {name, type:'array', items:{required:[...]}}."
        ),
        # These properties make the nested contract discoverable to the Lead.
        # Keep additional properties allowed for legacy field metadata and for
        # conservative gateways that only partially implement JSON Schema.
        "properties": {
            "name": {"type": "string", "minLength": 1},
            "field": {"type": "string", "minLength": 1},
            "key": {"type": "string", "minLength": 1},
            "type": {"type": "string", "minLength": 1},
            "allow_empty": {"type": "boolean"},
            "nonempty": {"type": "boolean"},
            "items": {
                "type": "object",
                "description": (
                    "Nested item contract. required lists the exact fields"
                    " that each collected child row must provide."
                ),
                "properties": {
                    "required": {
                        "type": "array",
                        "minItems": 1,
                        "items": {"type": "string", "minLength": 1},
                    },
                },
                "additionalProperties": True,
            },
        },
        "additionalProperties": True,
    }
    field_list: JsonDict = {"type": "array", "items": field_items}
    required_controls: JsonDict = {
        "type": "array",
        "minItems": 1,
        "description": (
            "ONLY for form_filling/form_interaction when the requested"
            " deliverable is one artifact row per independent business"
            " control. Every row uses a stable controlKey and a non-empty"
            " filledValue read back from the page. Do NOT use this for"
            " incidental search/pagination/download controls or for fields"
            " within product/file/listing rows; use fields/required_fields"
            " and nonempty_fields for those row contracts instead."
        ),
        "items": {
            "type": "object",
            "properties": {
                "controlKey": {
                    "type": "string",
                    "minLength": 1,
                    "description": (
                        "Stable business identity for the control; never an"
                        " AXTree id or transient DOM selector."
                    ),
                },
                "label": {"type": "string", "minLength": 1},
                "section": {"type": "string", "minLength": 1},
            },
            "required": ["controlKey"],
            "additionalProperties": False,
        },
    }
    return {
        "type": "object",
        "description": (
            "Structured output contract. Declare name, fields and row-count"
            " constraints here; equivalent explicit validators are accepted"
            " but normalized/deduplicated by the harness. Row count alone does"
            " not prove a named cohort: pair exact_rows with set_equals and"
            " unique validators when the user specifies concrete identities."
        ),
        "properties": {
            "name": {"type": "string"},
            "fields": field_list,
            "required_fields": field_list,
            "exact_rows": {"type": "integer", "minimum": 1},
            "min_rows": {"type": "integer", "minimum": 1},
            "max_rows": {"type": "integer", "minimum": 1},
            "count_range": {
                "type": "array",
                "items": {"type": "integer"},
                "minItems": 2,
                "maxItems": 2,
            },
            "nonempty_fields": field_list,
            "field_nonempty": field_list,
            "provenance_required": field_list,
            "requiredControls": required_controls,
        },
        "propertyNames": {"minLength": 1},
        "additionalProperties": True,
    }


def _browser_routing_schema() -> JsonDict:
    return {
        "name": {
            **_nullable("string"),
            "description": "BrowserAgent name; pass null to auto-name.",
        },
        "context": {
            "type": "string",
            "description": (
                "Optional new evidence or continuation context; omit when the phase is sufficient. Include artifact paths"
                " or prior result fields that the worker may use as dynamic-param sources."
            ),
        },
        "preferred_slot_id": {
            **_nullable("string"),
            "description": (
                "Optional idle BrowserAgent slotId for an explicit related"
                " continuation. Passing it allows reusable page candidates"
                " from that slot to be exposed to the worker."
            ),
        },
        "reuse_from_worker_id": {
            **_nullable("string"),
            "description": (
                "Optional previous workerId whose idle slot should be reused"
                " for an explicit related continuation. Passing it allows"
                " reusable page candidates from that slot to be exposed only"
                " when reuse_scope=page and page_policy=existing; with"
                " page_policy=new it reuses slot/fleet context but not the"
                " previous page. This pins a slot and may serialize work;"
                " omit for independent siblings unless that exact slot"
                " is required."
            ),
        },
        "reuse_scope": {
            "type": ["string", "null"],
            "enum": [*sorted(VALID_REUSE_SCOPES), None],
            "description": (
                "Fleet/page reuse boundary. Omit or use connection for a"
                " fresh page in the slot's assigned fleet; fleet keeps the"
                " same fleet/session with a fresh page; page explicitly"
                " exposes prior pages for a related continuation."
            ),
        },
        "session_key": {
            **_nullable("string"),
            "description": (
                "Stable harness session-affinity key for related phases."
                " First use creates a fresh fleet; later uses bind only to"
                " that exact fleet and fail terminally if it is lost. It is"
                " not an account credential and must not contain secrets."
                " Never put a Fleet UUID or UUID prefix here. A user can"
                " bind an existing Fleet only with @<id> in the original"
                " task; the runtime applies that binding itself."
            ),
        },
        "page_policy": {
            "type": ["string", "null"],
            "enum": [*sorted(VALID_PAGE_POLICIES), None],
            "description": (
                "Use new for a fresh page in assignedFleetId. existing is"
                " valid only with reuse_scope=page. Use existing with the"
                " prior page context when current evidence requires"
                " source-card traversal or unfinished page state."
            ),
        },
    }


def _spawn_browser_agent_schema(_: Any = None) -> JsonDict:
    from harness.delegation import spawn_schema
    return spawn_schema(_expected_artifact_schema(), _browser_routing_schema())


def _wait_browser_agents_schema(_: Any = None) -> JsonDict:
    return {
        "type": "object",
        "properties": {
            "worker_ids": {
                "type": ["array", "null"],
                "items": {"type": "string"},
                "description": "List of workerIds to wait for; pass null to wait for every spawned agent.",
            },
            "mode": {
                "type": "string",
                "enum": ["all", "first"],
            },
            "timeout_seconds": {
                **_nullable("number"),
                "description": "Wait timeout in seconds; pass null for no limit.",
            },
        },
        "required": ["worker_ids", "mode", "timeout_seconds"],
        "additionalProperties": False,
    }


def _list_browser_agents_schema(_: Any = None) -> JsonDict:
    return {"type": "object", "properties": {
        "refresh_connection": {"type": "boolean", "default": False,
            "description": "After external endpoint recovery, explicitly run one bounded registration/capability/Fleet inventory probe. Does not start workers or replay business actions."}
    }, "required": [], "additionalProperties": False}


def _local_fs_search_schema(_: Any = None) -> JsonDict:
    return {
        "type": "object",
        "properties": {
            "pattern": {
                "type": "string",
                "default": "",
                "description": "Regex grep; pass an empty string to list matches by glob / event_type only.",
            },
            "path": {"type": "string", "default": ".", "description": "Directory to search; external roots require terminal approval."},
            "glob": {
                "type": "string",
                "default": "**/*",
                "description": "Glob relative to path, e.g. traces/*.jsonl or observations/*.json.",
            },
            "event_type": {
                "type": ["string", "null"],
                "default": None,
                "description": "JSONL-only: restrict the search to lines whose `event` matches this string; pass null when not needed.",
            },
            "max_results": {"type": "integer", "minimum": 1, "maximum": 100, "default": 20},
            "max_bytes_per_hit": {"type": "integer", "minimum": 200, "maximum": 20000, "default": 2000},
            "max_total_bytes": {"type": "integer", "minimum": 1000, "maximum": 200000, "default": 20000},
        },
        "required": [
            "path",
            "pattern",
            "glob",
            "event_type",
            "max_results",
            "max_bytes_per_hit",
            "max_total_bytes",
        ],
        "additionalProperties": False,
    }


def _local_fs_read_schema(_: Any = None) -> JsonDict:
    return {
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "line_offset": {"type": "integer", "minimum": 0, "default": 0},
            "line_limit": {"type": "integer", "minimum": 1, "maximum": 5000, "default": 200},
            # The handler clamps this public default to the configured per-run
            # cap, so omitting it keeps the existing runtime-specific limit.
            "max_bytes": {"type": "integer", "minimum": 1000, "maximum": 200000, "default": 20000},
        },
        "required": ["path", "line_offset", "line_limit", "max_bytes"],
        "additionalProperties": False,
    }


def _read_harness_guide_schema(_: Any = None) -> JsonDict:
    return {
        "type": "object",
        "properties": {
            "guide_id": {
                "type": "string",
                "description": (
                    "An id from <available_harness_guides>. This reads a "
                    "versioned Harness operating guide, not a task file."
                ),
            },
            "line_offset": {"type": "integer", "minimum": 0, "default": 0},
            "line_limit": {
                "type": "integer", "minimum": 1, "maximum": 500, "default": 200,
            },
        },
        "required": ["guide_id", "line_offset", "line_limit"],
        "additionalProperties": False,
    }


def _search_harness_guides_schema(_: Any = None) -> JsonDict:
    return {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": (
                    "Plain text: an error/reason code from a receipt, a tool "
                    "name, or a phrase in any language. Not a regex."
                ),
            },
            "limit": {"type": "integer", "minimum": 1, "maximum": 5, "default": 5},
        },
        "required": ["query", "limit"],
        "additionalProperties": False,
    }


def _lead_save_artifact_schema(_: Any = None) -> JsonDict:
    return {
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": "Short dataset name, matching expected_artifact.name when applicable.",
            },
            "mode": {
                "type": "string",
                "enum": ["reference_merge", "rows"],
                "description": (
                    "reference_merge (preferred for consolidating worker"
                    " artifacts): name the sources and row keys and the harness"
                    " copies each row verbatim, so row content never passes"
                    " through your context and cannot lose fields. rows: submit"
                    " row content yourself; only for rows that no source"
                    " artifact already holds."
                ),
            },
            "sources": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "artifactPath": {"type": "string"},
                        "rowKeys": {"type": "array", "items": {"type": "string"}},
                    },
                    "required": ["artifactPath", "rowKeys"],
                    "additionalProperties": False,
                },
                "description": (
                    "reference_merge only: which rows to copy from which"
                    " artifact. A row key claimed by two sources is rejected —"
                    " name the one source you mean."
                ),
            },
            "identity_fields": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Field(s) whose value identifies a row (e.g. detailUrl)."
                    " Required for reference_merge; in rows mode it enables the"
                    " regression check that catches silently shrunk arrays."
                ),
            },
            "rows": {
                "type": "array",
                "items": {"type": "object", "additionalProperties": True},
                "description": "rows mode only: structured rows using the exact expected field names.",
            },
            "schema": {
                "type": "object",
                "additionalProperties": True,
                "description": "Optional schema/field description for the saved rows.",
            },
            "description": {
                "type": "string",
                "description": "Why this artifact was saved and which evidence it came from.",
            },
            "source_artifacts": {
                "type": "array",
                "items": {"type": "string"},
                "description": "rows mode only: extraction artifact paths used as evidence for this reshape.",
            },
        },
        "required": ["name", "schema", "description"],
        "additionalProperties": False,
    }


def _final_answer_schema(_: Any = None) -> JsonDict:
    return {
        "type": "object",
        "properties": {
            "status": {
                "type": "string",
                "enum": ["done", "partial", "blocked", "failed"],
            },
            "answer": {"type": "string"},
        },
        "required": ["status", "answer"],
        "additionalProperties": False,
    }


def build_lead_tool_dispatcher(agent: Any) -> LeadToolDispatcher:
    async def dispatch(tool_call: JsonDict) -> Tuple[JsonDict, bool]:
        step = getattr(agent, "_current_step", 0)
        lifecycle = lifecycle_for(agent)
        context = LifecycleContext(actor="lead_agent", step=step)
        prepared_call, prepared_fields, preparation_error = prepare_model_tool_call(
            tool_call
        )
        if preparation_error is not None or prepared_call is None:
            result = tool_argument_error(
                tool_call,
                [SchemaIssue((), "type", preparation_error or "invalid tool call")],
                stage="prepare_arguments",
            )
            _record_lead_argument_rejection(agent, result)
            return result, False
        prepared_call, defaulted_fields = apply_registered_tool_defaults(
            LEAD_TOOLS, prepared_call
        )
        prepared_fields.extend(defaulted_fields)
        issues = validate_registered_tool_call(LEAD_TOOLS, prepared_call)
        if issues:
            result = tool_argument_error(
                prepared_call,
                issues,
                stage="validate_tool_arguments",
                normalized_fields=prepared_fields,
            )
            _record_lead_argument_rejection(agent, result)
            return result, False
        effective_call = prepared_call
        post_call_attempted = False
        try:
            effective_call = lifecycle.tool_pre_call(context, prepared_call)
            effective_call, defaulted_fields = apply_registered_tool_defaults(
                LEAD_TOOLS, effective_call
            )
            prepared_fields.extend(defaulted_fields)
            issues = validate_registered_tool_call(LEAD_TOOLS, effective_call)
            if issues:
                result = tool_argument_error(
                    effective_call,
                    issues,
                    stage="validate_after_before_tool_call",
                    normalized_fields=prepared_fields,
                )
                _record_lead_argument_rejection(agent, result)
                return result, False
            authorization_error = await authorize_tool_call(agent, effective_call)
            if authorization_error is not None:
                return authorization_error, False
            result, should_stop = await execute_lead_tool(agent, effective_call)
            post_call_attempted = True
            try:
                result = lifecycle.tool_post_call(context, effective_call, result)
            except Exception as exc:
                # The handler completed.  Preserve both its receipt and its
                # terminal signal when observational middleware fails.
                _record_lead_after_call_exception(agent, effective_call, exc)
        except Exception as exc:
            # Do not echo a lifecycle/handler exception: Lead tools can carry
            # task text and artifact values.  A pre-dispatch failure is safe to
            # retry after correcting the next action; an execution failure is
            # deliberately marked uncertain.
            result = {
                "isError": True,
                "status": "tool_exception",
                "stage": "lead_tool_pipeline",
                "tool": str(effective_call.get("name") or "lead_tool")
                if isinstance(effective_call, dict) else "lead_tool",
                "error": "Lead tool pipeline raised an exception.",
                "exceptionType": type(exc).__name__,
                "replayForbidden": True,
            }
            should_stop = False
            _record_lead_tool_exception(agent, result)
        if not post_call_attempted:
            try:
                result = lifecycle.tool_post_call(context, effective_call, result)
            except Exception as exc:
                # This hook observes the already-constructed fallback.  Do
                # not overwrite the actionable original failure with another
                # exception envelope.
                _record_lead_after_call_exception(agent, effective_call, exc)
        return result, should_stop

    return dispatch


def _record_lead_argument_rejection(agent: Any, result: JsonDict) -> None:
    logger = getattr(agent, "logger", None)
    write = getattr(logger, "write", None)
    if callable(write):
        write("lead.tool.arguments_rejected", result)
    trace = getattr(agent, "trace", None)
    if isinstance(trace, list):
        trace.append({"type": "lead_tool_arguments_rejected", "result": result})


def _record_lead_tool_exception(agent: Any, result: JsonDict) -> None:
    logger = getattr(agent, "logger", None)
    write = getattr(logger, "write", None)
    if callable(write):
        try:
            write("lead.tool.exception", result)
        except Exception:
            pass
    trace = getattr(agent, "trace", None)
    if isinstance(trace, list):
        trace.append({"type": "lead_tool_exception", "result": result})


def _record_lead_after_call_exception(
    agent: Any,
    tool_call: Any,
    exc: Exception,
) -> None:
    name = str(tool_call.get("name") or "lead_tool") if isinstance(tool_call, dict) else "lead_tool"
    receipt = {"tool": name, "exceptionType": type(exc).__name__}
    logger = getattr(agent, "logger", None)
    write = getattr(logger, "write", None)
    if callable(write):
        try:
            write("lead.tool.after_call_exception", receipt)
        except Exception:
            pass
    trace = getattr(agent, "trace", None)
    if isinstance(trace, list):
        trace.append({"type": "lead_tool_after_call_exception", "result": receipt})


async def execute_lead_tool(agent: Any, tool_call: JsonDict) -> Tuple[JsonDict, bool]:
    name = str(tool_call.get("name") or "")
    raw_tool_input = tool_call.get("input") or {}
    tool_input = raw_tool_input if isinstance(raw_tool_input, dict) else {"value": raw_tool_input}
    tool_input, normalized_fields = _normalize_optional_identifiers(
        name,
        tool_input,
    )
    action = LEAD_TOOLS.get(name)
    if action is None:
        result = {
            "status": "failed",
            "error": f"Unknown LeadAgent tool: {name}",
        }
        agent.logger.write("lead.tool.error", result)
        return result, False

    agent._pending_loop_observations = []
    if action.loop_guard:
        short_circuit = check_tool_call_loop(
            agent,
            name=name,
            tool_input=tool_input,
            step=getattr(agent, "_current_step", 0),
        )
        if short_circuit is not None:
            return short_circuit

    result = await action.handler(
        ToolContext(
            agent=agent,
            tool_call=tool_call,
            tool_input=tool_input,
            step=getattr(agent, "_current_step", 0),
        )
    )
    if normalized_fields and isinstance(result, dict):
        result["normalizedFields"] = normalized_fields
    loop_observations = list(
        getattr(agent, "_pending_loop_observations", None) or []
    )
    if loop_observations and isinstance(result, dict):
        result["loopObservations"] = loop_observations
        result["loopObservationNotice"] = (
            "The requested tool was executed. These repetition facts are"
            " evidence for your next ReAct decision, not a stop directive."
        )
    # A terminal handler may soft-reject its call (tool_was_executed False) to
    # bounce it back to the model with guidance instead of terminating — the
    # same contract the worker dispatcher has always honoured. Without it a
    # rejected final_answer still ended the run, so the numeric gate could
    # catch a wrong count, write out exactly how to fix it, and then stop the
    # task before anyone could act on it (observed in runs 636d591d and
    # cd6718ea). Rejecting an answer has to mean sending it back, not killing
    # the task.
    soft_rejected = (
        isinstance(result, dict) and result.get("tool_was_executed") is False
    )
    force_terminal = bool(
        result.pop("_terminate_lead", False)
        if isinstance(result, dict) else False
    )
    return result, (force_terminal or (action.terminal and not soft_rejected))


@LEAD_TOOLS.register(
    name="spawn_browser_agent",
    description=(
        "Delegate browser work. Submit one assignment with its inputs, deliverable "
        "and budget, or pass an existing phase_id to continue the same assignment. "
        "Harness records the contract, reviews it and obtains required operator "
        "approval before dispatch. Returns a worker handle or review feedback. "
        "Use wait_browser_agents to collect evidence, then judge it against the "
        "original user goal. A contract check never proves goal completion."
    ),
    input_schema=_spawn_browser_agent_schema,
)
async def _lead_spawn_browser_agent(ctx: ToolContext) -> JsonDict:
    from harness.delegation import dispatch_assignment
    return await dispatch_assignment(ctx, _spawn_accepted_browser_agent)


async def _spawn_accepted_browser_agent(ctx: ToolContext) -> JsonDict:
    agent = ctx.agent
    tool_input = ctx.tool_input
    raw_contract = tool_input.get("worker_contract")
    forbidden_fleet_inputs = []
    if "fleet_id" in tool_input:
        forbidden_fleet_inputs.append("spawn_browser_agent.fleet_id")
    if isinstance(raw_contract, dict) and "fleet_id" in raw_contract:
        forbidden_fleet_inputs.append("worker_contract.fleet_id")
    if forbidden_fleet_inputs:
        return {
            "status": "fleet_routing_input_forbidden",
            "error": (
                ", ".join(forbidden_fleet_inputs)
                + " is not a Fleet routing input."
            ),
            "tool_was_executed": False,
            "next_instruction": (
                "Name an existing Fleet only as @<Fleet UUID or unique prefix>"
                " in the original user task. Do not put Fleet identifiers in"
                " task plans, worker contracts, or tool calls."
            ),
        }
    if getattr(agent, "task_plan", None) is None:
        return {
            "status": "plan_required",
            "error": "No accepted browser assignment is available for dispatch.",
            "next_instruction": (
                "Submit a fresh assignment to spawn_browser_agent; the harness"
                " will compile and review its phase before dispatch."
            ),
        }
    approval_rejection = agent.task_plan_user_approval_rejection()
    if approval_rejection is not None:
        return approval_rejection
    exhausted = mark_phase_exhausted_if_needed(agent.task_plan, agent.logger)
    phase_id = tool_input.get("phase_id")
    # The override contract must reach the pre-check: a spawn that genuinely
    # changes the objective via worker_contract would otherwise be rejected
    # against the raw phase's exhausted fingerprint before ever reaching the
    # spawner (which already receives the effective contract).
    if isinstance(raw_contract, dict):
        raw_contract.pop("task_type", None)  # legacy override has no authority.
    phase, rejection = agent.resolve_phase_for_spawn_with_rejection(
        str(phase_id) if isinstance(phase_id, str) and phase_id.strip() else None,
        worker_contract=raw_contract if isinstance(raw_contract, dict) else None,
    )
    exhausted_match = _matching_exhaustion(exhausted, phase_id)
    if exhausted_match is not None:
        # An exhausted phase reports its budget the same way whether the Lead
        # named it or the resolver reached it. Requiring an explicit phase_id
        # must not cost the attempts/classification receipt: naming the phase
        # you meant is not new information the harness can charge for.
        return {
            "status": "phase_exhausted",
            "phaseId": exhausted_match.get("phaseId"),
            "attempts": exhausted_match.get("attempts"),
            "max_attempts": exhausted_match.get("max_attempts"),
            "last_failure": exhausted_match.get("last_failure"),
            "classification": exhausted_match.get("classification"),
            "next_instruction": (
                "The phase's explicitly declared worker-attempt resource"
                " budget is used. If more global budget should be allocated,"
                " update max_attempts without changing the objective;"
                " otherwise report the raw blocker. This receipt does not"
                " imply the target is absent or infeasible."
            ),
        }
    if phase is None:
        # Pass the structured rejection through verbatim: it carries the real
        # status (dependency_not_ready / blocked_by_dependency /
        # phase_already_running / explicit resource exhaustion / ...) plus a
        # next_instruction. Task 2ed5a466 collapsed these into a generic
        # "phase not found" and the Lead blind-retried a dependency-gated
        # phase twice.
        if rejection is not None:
            return rejection
        return {
            "status": "failed",
            "error": f"phase not found in the accepted plan: {phase_id}",
            "errorCode": "phase_not_found",
            "scheduleSnapshot": agent.phase_schedule_snapshot(),
        }
    planned_contract = phase.get("worker_contract")
    if isinstance(planned_contract, dict) and "fleet_id" in planned_contract:
        return {
            "status": "fleet_routing_input_forbidden",
            "error": "phase.worker_contract.fleet_id is not a Fleet routing input.",
            "tool_was_executed": False,
            "next_instruction": (
                "Revise the plan without worker_contract.fleet_id. An existing"
                " Fleet is bound only by @<id> in the original user task."
            ),
        }
    dispatch_wave = phase.get("dispatch_wave")
    if isinstance(dispatch_wave, int) and not isinstance(dispatch_wave, bool):
        state = load_task_state(agent.logger)
        phase_states = state.get("phases") if isinstance(state, dict) else {}
        phase_states = phase_states if isinstance(phase_states, dict) else {}
        waiting_for = dispatch_wave_blockers(
            agent.task_plan, phase, phase_states,
        )
        if waiting_for:
            return {
                "status": "dispatch_wave_not_ready",
                "phaseId": str(phase.get("id") or ""),
                "dispatchWave": dispatch_wave,
                "waitingForPhaseIds": waiting_for,
                "tool_was_executed": False,
                "next_instruction": (
                    "Wait for the declared earlier scheduling wave to become"
                    " validated_done. This is the operator-approved dispatch"
                    " order, separate from artifact data dependencies."
                ),
            }
    # The automatic input-binding proof reads only this reviewed-plan view.
    # Spawn overrides remain available for routing/session purposes, but must
    # not rewrite the expected artifact or validators used as proof.
    reviewed_worker_contract = agent.build_worker_contract(phase)
    worker_contract = agent.build_worker_contract(
        phase,
        raw_contract if isinstance(raw_contract, dict) else None,
    )
    # The immutable @Fleet reference is the user's explicit identity choice.
    # Model-authored session routing is lower-authority planning metadata; if it
    # survived in an accepted plan, passing it onward creates an unresolvable
    # conflict and tempts the Lead to rewrite business phases to fix routing.
    # Normalize that conflict at the boundary while retaining the low-level
    # spawner's strict check for non-Lead/programmatic callers.
    task_fleet_reference = str(
        getattr(agent, "task_fleet_reference", "") or ""
    ).strip()
    ignored_task_fleet_route_fields: List[str] = []
    if task_fleet_reference:
        if worker_contract.pop("session_key", None) is not None:
            ignored_task_fleet_route_fields.append("worker_contract.session_key")
        if worker_contract.pop("needs_isolated_session", None) is not None:
            ignored_task_fleet_route_fields.append(
                "worker_contract.needs_isolated_session"
            )
        if str(tool_input.get("session_key") or "").strip():
            ignored_task_fleet_route_fields.append("spawn_browser_agent.session_key")
        if ignored_task_fleet_route_fields:
            agent.logger.write("task.fleet_reference.routing_normalized", {
                "fleetReference": task_fleet_reference,
                "phaseId": str(phase.get("id") or ""),
                "ignoredFields": ignored_task_fleet_route_fields,
                "source": "original_user_task",
            })
    # Initial ordinary downstream phases do not need the Lead to copy row
    # identities into a worker_contract. Derive only from an explicitly
    # declared input artifact, then verify that producer at runtime; never
    # guess a source from phase order or same-shaped artifacts. Explicit
    # batch/cohort/checkpoint contracts remain authoritative.
    binding_override_keys = (
        sorted(
            key for key in raw_contract
            if key in _AUTO_BIND_SEMANTIC_OVERRIDE_KEYS
        )
        if isinstance(raw_contract, dict)
        else []
    )
    if not binding_override_keys:
        binding_decision = assess_batch_source_binding(
            agent.logger,
            phase=phase,
            plan=agent.task_plan,
            worker_contract=reviewed_worker_contract,
        )
        derived_batch_source = binding_decision.get("batch_source")
        if isinstance(derived_batch_source, dict):
            worker_contract["batch_source"] = derived_batch_source
            agent.logger.write(
                "batch_source.derived",
                {
                    "phaseId": str(phase.get("id") or ""),
                    "artifactName": derived_batch_source.get("artifact_name"),
                    "identityField": derived_batch_source.get("identity_field"),
                    "selector": derived_batch_source.get("selector"),
                    "dependencyPhaseId": binding_decision.get("dependencyPhaseId"),
                    "upstreamRowCount": binding_decision.get("upstreamRowCount"),
                    "reason": "declared_input_artifact_exact_row_count",
                },
            )
    elif hasattr(agent, "logger"):
        agent.logger.write(
            "batch_source.not_derived",
            {
                "phaseId": str(phase.get("id") or ""),
                "reason": "spawn_override_changes_binding_semantics",
                "overrideKeys": binding_override_keys,
            },
        )
    state = load_task_state(agent.logger)
    phase_state = (
        (state.get("phases") or {}).get(str(phase.get("id") or ""))
        if isinstance(state.get("phases"), dict)
        else None
    )
    prior_attempts = (
        phase_state.get("attempts")
        if isinstance(phase_state, dict)
        and isinstance(phase_state.get("attempts"), list)
        else []
    )
    prior_handoff = None
    for prior_attempt in reversed(prior_attempts):
        digest = (
            prior_attempt.get("attemptDigest")
            if isinstance(prior_attempt, dict)
            and isinstance(prior_attempt.get("attemptDigest"), dict)
            else None
        )
        if isinstance(digest, dict) and isinstance(digest.get("handoff"), dict):
            prior_handoff = digest["handoff"]
            break
    # Route exploration is a Lead dispatch decision. Only declared
    # dependencies impose ordering; a similar running phase is not a blocker.
    direct_batch_errors = direct_batch_rows_provenance_errors(
        worker_contract,
        user_task=str(getattr(agent, "original_user_task", "") or ""),
        phase_id=str(phase.get("id") or ""),
    )
    if direct_batch_errors:
        return {
            "status": "invalid_batch_rows_provenance",
            "errors": direct_batch_errors,
            "tool_was_executed": False,
            "next_instruction": (
                "Use batch_source for rows discovered by a BrowserAgent. Use"
                " direct batch_rows only with batch_rows_provenance whose"
                " identity_fields values are present in the original user task."
            ),
        }
    batch_rejection = materialize_batch_rows_from_source(
        agent.logger,
        phase=phase,
        worker_contract=worker_contract,
    )
    if batch_rejection is not None:
        return batch_rejection
    derived_source = worker_contract.get("batch_source")
    if isinstance(derived_source, dict):
        # The resolved local path is a harness pin used during materialization,
        # not BrowserAgent input. The receipt below retains the audited path.
        derived_source.pop("_artifact_path", None)
    base_task = str(tool_input.get("task") or phase.get("worker_task") or "")
    base_context = str(tool_input.get("context") or phase.get("context") or "")
    if isinstance(prior_handoff, dict):
        base_context = (
            f"{base_context}\n\nPREVIOUS WORKER HANDOFF (receipts and claims"
            " retain their stated ownership):\n"
            + json.dumps(
                prior_handoff,
                ensure_ascii=False,
                separators=(",", ":"),
                default=str,
            )
        ).strip()
    batch_receipt = worker_contract.get("_batch_source_receipt")
    if isinstance(batch_receipt, dict):
        base_context = (
            f"{base_context}\n\nBATCH EXECUTION CONTRACT: The harness loaded"
            f" {batch_receipt.get('rowCount')} validated input row(s) into"
            " worker_contract.batch_rows. Process them serially in artifact"
            " order, preserve each row identity, and do not silently skip or"
            " substitute rows. The execution_role and dependency gate define"
            " whether this is probe, validation, bulk, or remediation."
        ).strip()
    auth_gate_guidance = _auth_gate_probe_guidance(phase, worker_contract)
    collection_guidance = _collection_contract_guidance(phase, worker_contract)
    if auth_gate_guidance:
        base_context = f"{base_context}\n\n{auth_gate_guidance}".strip()
    if collection_guidance:
        base_context = f"{base_context}\n\n{collection_guidance}".strip()
    # Skill selection is a LeadAgent decision gate. Soft recall returns candidate
    # SKILL.md content first; the LeadAgent must retry with explicit skill_id or
    # an explicit decline. The worker fast path then takes the explicit path.
    try:
        spawner = getattr(agent, "spawner", None)
        runtime = getattr(spawner, "runtime", None)
        harness_cfg = getattr(runtime, "harness", None)
        registry = spawner._get_skill_registry() if spawner is not None and hasattr(spawner, "_get_skill_registry") else None
        if registry is not None and getattr(harness_cfg, "skill_fast_path_enabled", True):
            from harness.skill.contract import (
                apply_forced_skill,
                build_skill_selection_request,
                enrich_worker_contract_with_skill,
            )
            from harness.skill.guidance import default_guidance_health
            from harness.skill.health import default_health
            # Operator override wins first: a configured forced_skill_id stamps
            # skill_id (clearing any Lead decline), so selection is skipped and the
            # worker runs that skill wherever its variables are derivable.
            selection_mode = str(getattr(harness_cfg, "skill_selection_mode", "manual") or "manual")
            forced = apply_forced_skill(
                worker_contract,
                registry=registry,
                forced_skill_id=str(getattr(harness_cfg, "forced_skill_id", "") or ""),
                phase=phase,
                logger=agent.logger,
                workflow_health=default_health(),
                guidance_health=default_guidance_health(),
            )
            if not forced:
                # manual mode: the Lead is never interrupted with a selection
                # request — only the user's /skill choice engages a skill.
                selection_request = build_skill_selection_request(
                    worker_contract,
                    registry=registry,
                    phase=phase,
                    task=base_task,
                    context=base_context,
                    logger=agent.logger,
                    mode=selection_mode,
                )
                if selection_request is not None:
                    return selection_request
            enrich_worker_contract_with_skill(
                worker_contract, registry=registry, phase=phase,
                task=base_task, context=base_context, logger=agent.logger,
                mode=selection_mode,
            )
    except Exception:  # never break spawning
        pass
    checkpoint_rejection = replan_checkpoint_spawn_rejection(
        agent.logger,
        phase=phase,
        worker_contract=worker_contract,
    )
    if checkpoint_rejection is not None:
        return checkpoint_rejection
    if task_fleet_reference:
        agent.logger.write("task.fleet_reference.injected", {
            "fleetReference": task_fleet_reference,
            "phaseId": str(phase.get("id") or ""),
            "source": "original_user_task",
        })
    from harness.planning.context import user_context
    worker_contract["_user_context"] = user_context(agent.logger, agent.original_user_task)
    spawned = await agent.spawner.spawn_browser_agent(
        task=base_task,
        context=base_context,
        name=tool_input.get("name") or None,
        result_contract=str(tool_input.get("result_contract") or ""),
        phase_id=str(phase.get("id") or ""),
        worker_contract=worker_contract,
        phase=phase,
        task_plan=getattr(agent, "task_plan", None),
        preferred_slot_id=tool_input.get("preferred_slot_id"),
        reuse_from_worker_id=tool_input.get("reuse_from_worker_id"),
        reuse_scope=tool_input.get("reuse_scope"),
        task_fleet_reference=task_fleet_reference or None,
        session_key=(None if task_fleet_reference else tool_input.get("session_key")),
        page_policy=tool_input.get("page_policy"),
        dispatch_origin=str(
            tool_input.get("_runtime_dispatch_origin") or "lead_model"
        ),
        dispatch_identity=(
            tool_input.get("_runtime_dispatch_identity")
            if isinstance(tool_input.get("_runtime_dispatch_identity"), dict)
            else None
        ),
    )
    # A fatal failure can happen during startup before a worker handle exists.
    # Treat that receipt exactly like a fatal worker completion: perform one
    # bounded control-plane probe before returning control to the Lead.  The
    # helper never retries the failed spawn or any browser operation.
    if isinstance(spawned, dict):
        recovery = await _recover_transport_before_lead_decision(
            ctx, [spawned],
        )
        if recovery is not None:
            spawned = dict(spawned)
            spawned["connectionRecovery"] = recovery
    return spawned


def _auth_gate_probe_guidance(phase: JsonDict, worker_contract: JsonDict) -> str:
    """Task prose is authoritative; keyword matches cannot change its role.

    A diagnostic task already tells the worker what to report. Ordinary tasks
    often mention login diagnosis as a conditional recovery step. Never convert
    those words into a fabricated instruction that the user wanted only a probe.
    """
    return ""


def _collection_contract_guidance(phase: JsonDict, worker_contract: JsonDict) -> str:
    """Inject collection guardrails from declared validators, not field names."""
    stage = str(
        worker_contract.get("stage_hint")
        or phase.get("stage_hint")
        or ""
    ).strip()
    if stage != "collection":
        return ""
    expected = (
        worker_contract.get("expected_artifact")
        if isinstance(worker_contract.get("expected_artifact"), dict)
        else {}
    )
    fields = _expected_field_names(expected)
    if not fields:
        return ""
    exact_rows = _exact_rows_from_contract(worker_contract)
    validators = (
        worker_contract.get("validators")
        if isinstance(worker_contract.get("validators"), list)
        else phase.get("validators")
    )
    range_fields = [
        str(item.get("field") or "").strip()
        for item in (validators if isinstance(validators, list) else [])
        if isinstance(item, dict)
        and str(item.get("type") or "") == "range"
        and str(item.get("field") or "").strip()
    ]
    exact_text = (
        f"exactly {exact_rows} rows"
        if exact_rows is not None
        else "the requested row count"
    )
    range_note = ""
    if range_fields:
        range_note = (
            "- For range-validated fields "
            f"{sorted(set(range_fields))!r}, derive values from the declared"
            " live ordering/source evidence and persist the contract-declared"
            " provenance fields; do not infer semantics from a field name.\n"
        )
    return (
        "<collection_contract_guidance>\n"
        f"- The final record_extraction must satisfy fields {fields!r} and produce {exact_text}; do not treat a broad link harvest as final target data.\n"
        "- Use collect_items for repeated candidates when useful, but first verify the selector represents the task-declared entity sequence rather than navigation, featured, or otherwise unrelated elements.\n"
        "- If collect_items returns many more rows than expected or recordExtraction.status=needs_fix, treat the selector as too broad or the row schema as wrong. Narrow the repeated card selector or transform/slice trusted DOM-order rows before final_answer.\n"
        f"{range_note}"
        "</collection_contract_guidance>"
    )


def _expected_field_names(expected: JsonDict) -> List[str]:
    raw_fields = expected.get("required_fields")
    if not isinstance(raw_fields, list) or not raw_fields:
        raw_fields = expected.get("fields")
    if not isinstance(raw_fields, list):
        return []
    out: List[str] = []
    seen = set()
    for item in raw_fields:
        value = (
            item.get("name") or item.get("field") or item.get("key")
            if isinstance(item, dict)
            else item
        )
        text = str(value or "").strip()
        if text and text not in seen:
            out.append(text)
            seen.add(text)
    return out


def _exact_rows_from_contract(worker_contract: JsonDict) -> Optional[int]:
    expected = (
        worker_contract.get("expected_artifact")
        if isinstance(worker_contract.get("expected_artifact"), dict)
        else {}
    )
    value = optional_int(expected.get("exact_rows"))
    if value is not None and value > 0:
        return value
    validators = worker_contract.get("validators")
    if not isinstance(validators, list):
        return None
    for validator in validators:
        if not isinstance(validator, dict):
            continue
        if str(validator.get("type") or "") != "exact_rows":
            continue
        value = optional_int(validator.get("value"))
        if value is not None and value > 0:
            return value
    return None


def _lead_wait_seen_worker_ids(agent: Any) -> Set[str]:
    """Return the in-process Lead set of completion receipts already consumed.

    ``wait_browser_agents`` historically includes completed handles in every
    later wait.  That is useful for inspection, but unsafe for automatic
    scheduling: a repeated wait could otherwise treat the same p1 completion
    as a fresh event and re-dispatch p2.  Worker ids are generated once per
    spawn and are therefore a sufficient event identity within one Lead process.
    This set is not a persistent replay ledger.  After a process restart, task
    state and the ordinary spawn gate remain responsible for recovery safety.
    """
    seen = getattr(agent, "_lead_wait_seen_worker_ids", None)
    if not isinstance(seen, set):
        seen = set()
        setattr(agent, "_lead_wait_seen_worker_ids", seen)
    return seen


def _new_wait_completions(agent: Any, waited: Any) -> List[JsonDict]:
    """Consume each worker completion once, preserving anonymous test receipts."""
    completed = waited.get("completed") if isinstance(waited, dict) else None
    values = completed if isinstance(completed, list) else []
    seen = _lead_wait_seen_worker_ids(agent)
    fresh: List[JsonDict] = []
    for value in values:
        if not isinstance(value, dict):
            continue
        worker_id = str(value.get("workerId") or "").strip()
        if worker_id and worker_id in seen:
            continue
        if worker_id:
            seen.add(worker_id)
        fresh.append(value)
    return fresh


def _wait_candidate_worker_ids(
    agent: Any,
    requested: Any,
) -> Tuple[Optional[List[str]], bool]:
    """Choose live or not-yet-consumed workers for one event wait.

    The second return value says whether the spawner exposed its handle
    registry.  Older embedding callers and focused test doubles keep their
    existing wait behaviour when it is unavailable.
    """
    spawner = getattr(agent, "spawner", None)
    handles = getattr(spawner, "_handles", None)
    if not isinstance(handles, dict):
        return (
            list(requested) if isinstance(requested, list) else requested,
            False,
        )
    requested_ids = (
        [str(value) for value in requested if str(value).strip()]
        if isinstance(requested, list) and requested else list(handles)
    )
    seen = _lead_wait_seen_worker_ids(agent)
    candidates: List[str] = []
    for worker_id in requested_ids:
        handle = handles.get(worker_id)
        if handle is None:
            # Preserve the public API's behaviour for an unknown explicit id.
            if isinstance(requested, list):
                candidates.append(worker_id)
            continue
        task = getattr(handle, "async_task", None)
        done = bool(task is not None and task.done())
        if not done or worker_id not in seen:
            candidates.append(worker_id)
    return candidates, True


async def _recover_transport_before_lead_decision(
    ctx: ToolContext,
    completed: Any,
) -> Optional[JsonDict]:
    """Probe once for a new fatal transport batch before Lead sees the wait.

    This is deliberately limited to the control-plane probe implemented by the
    spawner.  It never retries the failed browser Action, starts a worker, or
    creates a Fleet.  The durable fingerprint prevents repeated waits from
    issuing the same probe again; a new fatal worker result creates a new batch.
    """
    results = completed if isinstance(completed, list) else []
    required = note_transport_recovery_required(ctx.agent.logger, results)
    if not isinstance(required, dict):
        return None
    if str(required.get("status") or "") == "ready":
        return dict(required)
    probe = getattr(ctx.agent.spawner, "refresh_browser_connection", None)
    if not callable(probe):
        recovery = {
            "status": "blocked",
            "reason": "transport recovery probe is unavailable",
            "businessActionsReplayed": 0,
        }
    else:
        try:
            recovery = await probe(
                str(getattr(ctx.agent, "task_fleet_reference", "") or "")
            )
        except Exception as exc:
            # A probe failure is a control-plane blocker, not a reason to let
            # the Lead retry business work or lose the durable fatal receipt.
            recovery = {
                "status": "blocked",
                "reason": type(exc).__name__,
                "businessActionsReplayed": 0,
            }
    recorded = record_transport_recovery_probe(
        ctx.agent.logger, recovery,
    )
    receipt = dict(recovery) if isinstance(recovery, dict) else {
        "status": "blocked",
        "reason": "invalid recovery probe receipt",
    }
    receipt["requiredFingerprint"] = required.get("fingerprint")
    try:
        receipt["businessActionsReplayed"] = max(
            0, int(receipt.get("businessActionsReplayed") or 0)
        )
    except (TypeError, ValueError):
        receipt["businessActionsReplayed"] = 0
    if isinstance(recorded, dict):
        receipt["controlState"] = recorded.get("status")
        receipt["probeAttempts"] = recorded.get("probeAttempts")
    return receipt


@LEAD_TOOLS.register(
    name="wait_browser_agents",
    description=(
        "Wait for BrowserAgent results and return execution evidence, user input "
        "and scheduling facts. Lead judges remaining work and explicitly dispatches "
        "the next assignment; this tool never starts a worker."
    ),
    input_schema=_wait_browser_agents_schema,
)
async def _lead_wait_browser_agents(ctx: ToolContext) -> JsonDict:
    from harness.delegation import worker_return_receipt
    result = await _wait_and_collect_browser_agents(ctx)
    if isinstance(result, dict):
        for worker in result.get("completed") or []:
            if isinstance(worker, dict):
                worker["returnReview"] = worker_return_receipt(worker)
        result["goalCompletion"] = "requires_lead_judgment"
    return result


async def _wait_and_collect_browser_agents(ctx: ToolContext) -> JsonDict:
    agent = ctx.agent
    requested = ctx.tool_input.get("worker_ids")
    ids, has_registry = _wait_candidate_worker_ids(agent, requested)
    if has_registry and not ids:
        waited = {"status": "done", "completed": [], "pending": []}
    else:
        waited = await agent.spawner.wait_browser_agents(
            worker_ids=ids, mode=str(ctx.tool_input.get("mode") or "all"),
            timeout_seconds=ctx.tool_input.get("timeout_seconds"))
    fresh = _new_wait_completions(agent, waited)
    result = dict(waited)
    if requested is None:
        result["completed"] = fresh
    recovery = await _recover_transport_before_lead_decision(ctx, fresh)
    if recovery is not None:
        result["connectionRecovery"] = recovery
    result["scheduleSnapshot"] = schedule_snapshot(getattr(agent, "task_plan", None), agent.logger)
    from harness.planning.context import user_context
    inputs = user_context(agent.logger, getattr(agent, "original_user_task", ""))["operatorInputs"]
    seen = getattr(agent, "_lead_seen_operator_input_ids", set())
    result["operatorInputRecords"] = [item for item in inputs if item.get("inputId") not in seen]
    agent._lead_seen_operator_input_ids = seen | {item.get("inputId") for item in inputs}
    return result


@LEAD_TOOLS.register(
    name="list_browser_agents",
    description=(
        "Inspect workers and the pooled BrowserAgent slots. Use idle slotId or"
        " a previous workerId only when spawning explicit related continuation"
        " work that should see reusable page candidates."
    ),
    input_schema=_list_browser_agents_schema,
)
async def _lead_list_browser_agents(ctx: ToolContext) -> JsonDict:
    recovery = None
    if ctx.tool_input.get("refresh_connection") is True:
        recovery = await ctx.agent.spawner.refresh_browser_connection(
            str(getattr(ctx.agent, "task_fleet_reference", "") or ""))
        recorded = record_transport_recovery_probe(ctx.agent.logger, recovery)
        if isinstance(recorded, dict):
            recovery = dict(recovery or {})
            recovery["controlState"] = recorded.get("status")
            recovery["probeAttempts"] = recorded.get("probeAttempts")
    result = ctx.agent.spawner.list_browser_agents()
    if recovery is not None:
        result["connectionRecovery"] = recovery
    from harness.results.recovery import recovery_overview
    overview = recovery_overview(result)
    if overview:
        result["recoveryOverview"] = overview
    return result


def _persist_source_evidence(agent: Any) -> None:
    agent.logger.storage.save_snapshot(
        task_id=agent.logger.task_id, snapshot_key="lead_source_evidence",
        base=None, proposed={
            "reads": list(getattr(agent, "_source_read_facts", []) or []),
            "searches": list(getattr(agent, "_source_search_facts", []) or []),
        }, updated_run_id=str(agent.logger.run_id or ""), replace=True,
    )


@LEAD_TOOLS.register(
    name="local_fs_search",
    description="Search authorized local files, including user-supplied sources and task traces. Results are scoped by root, glob, pattern and output caps; a truncated result cannot prove absence.",
    input_schema=_local_fs_search_schema,
)
async def _lead_local_fs_search(ctx: ToolContext) -> JsonDict:
    tool_input = ctx.tool_input
    result = local_fs_search(
        ctx.agent.logger,
        agent=ctx.agent,
        path=str(tool_input.get("path") or "."),
        glob_pattern=str(tool_input.get("glob") or "**/*"),
        pattern=(
            str(tool_input.get("pattern"))
            if tool_input.get("pattern") is not None else None
        ),
        event_type=(
            str(tool_input.get("event_type"))
            if tool_input.get("event_type") is not None else None
        ),
        max_results=optional_int(tool_input.get("max_results"), 20) or 20,
        max_bytes_per_hit=(
            optional_int(tool_input.get("max_bytes_per_hit"), 2000) or 2000
        ),
        max_total_bytes=(
            optional_int(tool_input.get("max_total_bytes"), 20000) or 20000
        ),
    )
    if result.get("status") == "done":
        fact = {key: result.get(key) for key in (
            "root", "glob", "pattern", "eventType", "count", "truncated", "maxTotalBytes",
        )}
        fact["hits"] = [
            {key: hit.get(key) for key in ("relativePath", "line", "kind") if hit.get(key) is not None}
            for hit in (result.get("results") or [])[:16] if isinstance(hit, dict)
        ]
        facts = list(getattr(ctx.agent, "_source_search_facts", []) or [])
        facts = [item for item in facts if item != fact]
        facts.append(fact)
        ctx.agent._source_search_facts = facts[-16:]
        _persist_source_evidence(ctx.agent)
        ctx.agent.logger.write("local_fs.search_scope", fact)
    return result


@LEAD_TOOLS.register(
    name="local_fs_read",
    description="Read a line range from an authorized local file, including user-supplied source material and task traces. A truncated result has unread content; follow nextLineOffset or search the file before claiming something is absent.",
    input_schema=_local_fs_read_schema,
)
async def _lead_local_fs_read(ctx: ToolContext) -> JsonDict:
    tool_input = ctx.tool_input
    result = local_fs_read(
        ctx.agent.logger,
        agent=ctx.agent,
        path=str(tool_input.get("path") or ""),
        line_offset=optional_int(tool_input.get("line_offset"), 0) or 0,
        line_limit=optional_int(tool_input.get("line_limit"), 200) or 200,
        max_bytes=min(
            optional_int(
                tool_input.get("max_bytes"),
                ctx.agent.runtime.harness.local_fs_max_read_bytes,
            ) or ctx.agent.runtime.harness.local_fs_max_read_bytes,
            ctx.agent.runtime.harness.local_fs_max_read_bytes,
        ),
    )
    if result.get("status") == "done":
        fact = {
            key: result.get(key) for key in (
                "path", "lineOffset", "linesRead", "totalLines", "truncated",
                "nextLineOffset", "byteSize", "storage",
            )
        }
        fact["contentSha256"] = hashlib.sha256(
            str(result.get("content") or "").encode("utf-8")
        ).hexdigest()
        fact["completeFileRead"] = bool(
            not fact["truncated"] and fact["lineOffset"] == 0
            and fact["linesRead"] >= fact["totalLines"]
        )
        facts = list(getattr(ctx.agent, "_source_read_facts", []) or [])
        facts = [item for item in facts if item != fact]
        facts.append(fact)
        ctx.agent._source_read_facts = facts[-16:]
        _persist_source_evidence(ctx.agent)
        ctx.agent.logger.write("local_fs.read_range", fact)
    return result


@LEAD_TOOLS.register(
    name="read_harness_guide",
    description=(
        "Read a paged, versioned Harness operating guide listed in "
        "<available_harness_guides>. Use it when its topic is useful for "
        "reasoning about a complex orchestration receipt or recovery path."
    ),
    input_schema=_read_harness_guide_schema,
)
async def _lead_read_harness_guide(ctx: ToolContext) -> JsonDict:
    return read_harness_guide(
        guide_id=str(ctx.tool_input.get("guide_id") or ""),
        audience="lead",
        line_offset=optional_int(ctx.tool_input.get("line_offset"), 0) or 0,
        line_limit=optional_int(ctx.tool_input.get("line_limit"), 200) or 200,
    )


@LEAD_TOOLS.register(
    name="search_harness_guides",
    description=(
        "Find candidate Harness operating guides by an error/reason code from "
        "a receipt, a tool name, or a phrase in any language. Returns ids and "
        "why each matched; read one with read_harness_guide when it helps."
    ),
    input_schema=_search_harness_guides_schema,
)
async def _lead_search_harness_guides(ctx: ToolContext) -> JsonDict:
    return search_harness_guides(
        query=str(ctx.tool_input.get("query") or ""),
        audience="lead",
        limit=optional_int(ctx.tool_input.get("limit"), 5) or 5,
    )


@LEAD_TOOLS.register(
    name="revalidate_phase_artifacts",
    description="Recheck existing phase artifacts without spawning a worker or replaying browser actions. Inspect first; accept only after reviewing the original goal and evidence. Keeps phase identity, raw attempt history and budgets.",
    input_schema={"type": "object", "properties": {
        "phase_id": {"type": "string"}, "plan_version": {"type": "integer"},
        "decision": {"type": "string", "enum": ["inspect", "accept"], "default": "inspect"},
        "reason": {"type": "string"}}, "required": ["phase_id", "plan_version"], "additionalProperties": False},
    loop_guard=False,
)
async def _lead_revalidate_phase_artifacts(ctx: ToolContext) -> JsonDict:
    from harness.task_control.revalidate import revalidate_phase_artifacts
    return revalidate_phase_artifacts(ctx.agent, **ctx.tool_input)


@LEAD_TOOLS.register(
    name="lead_save_artifact",
    description=(
        "Persist rows as a standard extraction artifact under the current task"
        " worktree, instead of re-scraping. To consolidate rows that worker"
        " artifacts already hold, use mode=\"reference_merge\": name the source"
        " artifact and row keys and the harness copies each row verbatim."
        " Re-typing row content through your own context is how verified data"
        " loses fields, so mode=\"rows\" is only for rows no source holds, and"
        " it is rejected when a row shrinks an array its cited source has in"
        " full."
    ),
    input_schema=_lead_save_artifact_schema,
)
async def _lead_save_artifact(ctx: ToolContext) -> JsonDict:
    agent = ctx.agent
    tool_input = ctx.tool_input
    raw_name = str(tool_input.get("name") or "").strip()
    if not raw_name:
        return {"status": "rejected", "error": "name required"}

    identity_fields = [
        str(field).strip()
        for field in (tool_input.get("identity_fields") or [])
        if isinstance(field, str) and str(field).strip()
    ]
    mode = str(tool_input.get("mode") or "").strip()
    if not mode:
        mode = "reference_merge" if tool_input.get("sources") else "rows"

    if mode == "reference_merge":
        merged, merge_error = _reference_merge_rows(
            agent, tool_input.get("sources"), identity_fields,
        )
        if merge_error is not None:
            return merge_error
        saved = save_extraction_artifact(
            logger=agent.logger,
            runtime=agent.runtime,
            artifacts=None,
            name=raw_name,
            rows=merged["rows"],
            schema=tool_input.get("schema"),
            description=str(tool_input.get("description") or ""),
            source_artifacts=merged["sourceArtifacts"],
            row_lineage=merged["rowLineage"],
            event_type="tool.lead_save_artifact",
        )
        if not isinstance(saved, dict):
            return saved
        supersession = _record_artifact_supersession(
            agent,
            deliverable=str(saved.get("savedPath") or ""),
            cited=list(merged.get("sourceArtifacts") or []),
            absorbed=list(merged.get("absorbedSources") or []),
        )
        if supersession:
            saved = {**saved, "supersedes": supersession}
        return saved

    rows, error = validate_extraction_rows(tool_input.get("rows"))
    if error is not None:
        return error
    raw_sources = tool_input.get("source_artifacts") or []
    if isinstance(raw_sources, str):
        raw_sources = [raw_sources]
    source_artifacts, payloads, source_error = _validate_lead_save_sources(
        agent, raw_sources,
    )
    if source_error is not None:
        return source_error

    regressions = _array_cardinality_regressions(
        rows or [], payloads, identity_fields,
    )
    if regressions:
        return {
            "status": "rejected",
            "error": "array_cardinality_regression",
            "regressions": regressions,
            "next_instruction": (
                "These rows carry FEWER array items than the source artifact"
                " you cited for the same row. Retyping row content through the"
                " Lead context is how verified data gets truncated. Re-issue"
                " this call with mode=\"reference_merge\" and name the source"
                " artifact plus row keys; the harness will copy each row"
                " verbatim. Submit rows yourself only for rows no source holds."
            ),
        }

    return save_extraction_artifact(
        logger=agent.logger,
        runtime=agent.runtime,
        artifacts=None,
        name=raw_name,
        rows=rows or [],
        schema=tool_input.get("schema"),
        description=str(tool_input.get("description") or ""),
        source_artifacts=source_artifacts,
        event_type="tool.lead_save_artifact",
    )


def _row_identity(row: Any, identity_fields: List[str]) -> Optional[str]:
    """Identity string for a row, or None when a declared field is missing."""
    if not isinstance(row, dict) or not identity_fields:
        return None
    parts: List[str] = []
    for field in identity_fields:
        value = row.get(field)
        if value is None or isinstance(value, (dict, list)):
            return None
        text = str(value).strip()
        if not text:
            return None
        parts.append(text)
    return " | ".join(parts)


def _array_lengths(row: Any) -> Dict[str, int]:
    if not isinstance(row, dict):
        return {}
    return {
        str(field): len(value)
        for field, value in row.items()
        if isinstance(value, list)
    }


def _array_cardinality_regressions(
    rows: List[JsonDict],
    payloads: Dict[str, JsonDict],
    identity_fields: List[str],
) -> List[JsonDict]:
    """Rows that shrink an array a cited source already holds in full.

    This is the CodeDesign failure: a source artifact held 18 reviews, the
    consolidated artifact kept 3, and the final answer still said 18. Verbatim
    reference_merge makes that structurally impossible; this check covers the
    rows path that remains available.
    """
    if not identity_fields:
        return []
    # Per rowKey, per FIELD, the longest array any cited source holds — not the
    # single richest source row. Choosing one row by its total array length
    # lets a source with reviews=3 but images=20 outrank a source with
    # reviews=18, and the 18 goes unnoticed: exactly the loss this guards.
    source_max: Dict[str, Dict[str, Tuple[int, str]]] = {}
    for path, payload in payloads.items():
        for candidate in payload.get("rows") or []:
            key = _row_identity(candidate, identity_fields)
            if key is None:
                continue
            per_field = source_max.setdefault(key, {})
            for field, count in _array_lengths(candidate).items():
                best = per_field.get(field)
                if best is None or count > best[0]:
                    per_field[field] = (count, path)

    regressions: List[JsonDict] = []
    for row in rows:
        key = _row_identity(row, identity_fields)
        if key is None:
            continue
        per_field = source_max.get(key)
        if not per_field:
            continue
        submitted = _array_lengths(row)
        for field, (source_count, source_path) in per_field.items():
            # A field that vanished entirely is maximal shrinkage, so it counts
            # as 0 rather than being skipped. Treating "absent" as "unknown"
            # let the worst case through the check aimed at it.
            submitted_count = submitted.get(field, 0)
            if submitted_count >= source_count:
                continue
            regressions.append({
                "rowKey": key,
                "field": field,
                "before": source_count,
                "after": submitted_count,
                "sourceArtifact": source_path,
            })
    return regressions


def _reference_merge_rows(
    agent: Any,
    raw_sources: Any,
    identity_fields: List[str],
) -> Tuple[JsonDict, Optional[JsonDict]]:
    """Copy the named rows out of the named artifacts, verbatim.

    The Lead names sources and row keys; row CONTENT never passes through its
    context, so a consolidation cannot quietly drop fields it did not re-type.
    """
    if not identity_fields:
        return {}, {
            "status": "rejected",
            "error": "identity_fields is required for mode=reference_merge",
            "next_instruction": (
                "Declare the field(s) that identify a row (e.g."
                " [\"detailUrl\"]) so the harness can find each row key in its"
                " source artifact."
            ),
        }
    if not isinstance(raw_sources, list) or not raw_sources:
        return {}, {
            "status": "rejected",
            "error": "mode=reference_merge requires a non-empty sources array",
        }

    paths: List[str] = []
    requested: List[Tuple[str, List[str]]] = []
    for entry in raw_sources:
        if not isinstance(entry, dict):
            return {}, {
                "status": "rejected",
                "error": "each sources entry must be an object with artifactPath and rowKeys",
            }
        path_text = str(entry.get("artifactPath") or "").strip()
        row_keys = [
            str(key).strip()
            for key in (entry.get("rowKeys") or [])
            if isinstance(key, str) and str(key).strip()
        ]
        if not path_text or not row_keys:
            return {}, {
                "status": "rejected",
                "error": "each sources entry needs artifactPath and at least one rowKey",
                "sourceArtifact": path_text or None,
            }
        paths.append(path_text)
        requested.append((path_text, row_keys))

    validated, payloads, source_error = _validate_lead_save_sources(agent, paths)
    if source_error is not None:
        return {}, source_error
    # Resolve each entry's own path rather than zipping against `validated`:
    # that list is deduplicated, so two entries naming the same artifact (a
    # legitimate way to split row keys) would shift every later pairing by one.
    # The security checks stay in the validator; this only maps entry -> key.
    resolved_paths: Dict[str, str] = {}
    for path_text in paths:
        resolved = str(Path(path_text).expanduser().resolve(strict=False))
        if resolved not in payloads:
            return {}, {
                "status": "rejected",
                "error": "source_artifact could not be resolved",
                "sourceArtifact": path_text,
            }
        resolved_paths[path_text] = resolved

    claimed: Dict[str, List[str]] = {}
    for path_text, row_keys in requested:
        for key in row_keys:
            claimed.setdefault(key, []).append(resolved_paths[path_text])
    conflicts = [
        {"rowKey": key, "claimedBy": sources}
        for key, sources in claimed.items()
        if len(sources) > 1
    ]
    if conflicts:
        return {}, {
            "status": "rejected",
            "error": "row_key_claimed_by_multiple_sources",
            "conflicts": conflicts,
            "next_instruction": (
                "Pick ONE source artifact per row key. Choosing between two"
                " versions of a row is a decision about evidence, not something"
                " the harness may guess."
            ),
        }

    rows: List[JsonDict] = []
    lineage: List[JsonDict] = []
    missing: List[JsonDict] = []
    for path_text, row_keys in requested:
        resolved = resolved_paths[path_text]
        index: Dict[str, Tuple[int, JsonDict]] = {}
        for position, candidate in enumerate(payloads[resolved].get("rows") or []):
            key = _row_identity(candidate, identity_fields)
            if key is not None and key not in index:
                index[key] = (position, candidate)
        for key in row_keys:
            found = index.get(key)
            if found is None:
                missing.append({"rowKey": key, "sourceArtifact": resolved})
                continue
            position, candidate = found
            rows.append(copy.deepcopy(candidate))
            lineage.append({
                "rowKey": key,
                "sourceArtifact": resolved,
                "sourceRowIndex": position,
            })

    if missing:
        return {}, {
            "status": "rejected",
            "error": "row_key_not_found_in_source",
            "missing": missing,
            "identityFields": identity_fields,
            "next_instruction": (
                "Each rowKey must match an identity_fields value in the named"
                " artifact. Read the source artifact and copy the exact value,"
                " or point at the artifact that actually holds that row."
            ),
        }

    return {
        "rows": rows,
        "rowLineage": lineage,
        "sourceArtifacts": validated,
        "absorbedSources": _fully_absorbed_sources(
            rows, payloads, identity_fields,
        ),
    }, None


def _resolved_path_text(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        return str(Path(text).expanduser().resolve(strict=False))
    except (OSError, ValueError):
        return text


def _record_artifact_supersession(
    agent: Any,
    *,
    deliverable: str,
    cited: List[str],
    absorbed: List[str],
) -> List[str]:
    """Make a fully-absorbing merge the delivered generation.

    `task_state["artifacts"]` is the ledger the numeric gate and the completion
    receipt read as "what this task delivers". A consolidated artifact was
    never in it, so the deliverable itself was the one thing no number could be
    checked against — and worse, it landed in the superseded bucket, where a
    merge that dropped rows read as *verified* against the sources it had just
    damaged (task d32a810d).

    The supersession is recorded as its own entry rather than by editing
    `artifacts` in place, because that list is also the input ledger for
    downstream batch_source materialization, replan checkpoints, collect_items
    and skill dispatch. Those consumers want the full history; only
    `_validated_artifacts` wants the delivered view, and it is the only reader
    that applies this.

    All-or-nothing across the cited sources that are in the ledger: a merge
    that absorbs two artifacts and half of a third would leave the third's
    absorbed rows in two active artifacts at once, which is the double-count
    this design exists to avoid. Returns the paths retired, empty if none.
    """
    if not deliverable:
        return []
    state = _lead_task_state(agent)
    raw_ledger_paths = [
        _resolved_path_text(path)
        for path in (state.get("artifacts") or [])
        if str(path or "").strip()
    ]
    active_paths, lineages = artifact_generation_view(
        state, raw_ledger_paths, logger=getattr(agent, "logger", None),
    )
    active_identities = {_resolved_path_text(path) for path in active_paths}

    def _leaves(paths: List[str]) -> set[str]:
        leaves: set[str] = set()
        for path in paths:
            identity = _resolved_path_text(path)
            leaves.update(lineages.get(identity, {identity}))
        return leaves

    # A source outside the raw ledger or current active generation is an
    # auxiliary citation, not a new lineage leaf. Ignore it here exactly as
    # the reader does, so it cannot prevent a complete known merge.
    known_references = set(raw_ledger_paths) | set(lineages)
    cited_leaves = _leaves([
        path for path in cited
        if _resolved_path_text(path) in known_references
    ])
    absorbed_leaves = _leaves([
        path for path in absorbed
        if _resolved_path_text(path) in known_references
    ])
    if not cited_leaves or not cited_leaves.issubset(absorbed_leaves):
        return []
    # An active merge may represent multiple source leaves. It can be retired
    # only when every one of those leaves survives into this deliverable. This
    # handles a source reference to M1 just like a reference to M1's original
    # leaves, without allowing a reference to only one half of M1 to discard
    # the other half.
    touched = {
        identity for identity in active_identities
        if lineages.get(identity, {identity}) & absorbed_leaves
    }
    if not touched or any(
        not lineages.get(identity, {identity}).issubset(absorbed_leaves)
        for identity in touched
    ):
        return []

    supersessions = state.get("artifact_supersessions")
    if not isinstance(supersessions, list):
        supersessions = []
    supersessions.append({
        "deliverable": _resolved_path_text(deliverable),
        # Store leaf identities. A later write need not know whether an earlier
        # merge was cited by name or its sources were listed again.
        "absorbed": sorted(absorbed_leaves),
        "mode": "reference_merge",
    })
    state["artifact_supersessions"] = supersessions
    # Integrity metadata covers delivered generations too.  The merge output
    # deliberately stays out of the raw source ledger, so mark_phase_result
    # cannot be relied on to hash it later.
    try:
        deliverable_path = Path(_resolved_path_text(deliverable))
        digest = hashlib.sha256(deliverable_path.read_bytes()).hexdigest()
    except (OSError, ValueError):
        digest = ""
    if digest:
        digests = state.get("artifact_digests")
        digests = dict(digests) if isinstance(digests, dict) else {}
        digests[str(deliverable_path)] = digest
        state["artifact_digests"] = digests
    write_task_state(agent.logger, state)
    logger = getattr(agent, "logger", None)
    if logger is not None and hasattr(logger, "write"):
        logger.write("lead.artifact_supersession", {
            "deliverable": _resolved_path_text(deliverable),
            "absorbed": sorted(absorbed_leaves),
        })
    return sorted(absorbed_leaves)


def _lead_task_state(agent: Any) -> JsonDict:
    state = load_task_state(getattr(agent, "logger", None))
    return state if isinstance(state, dict) else {}


def _fully_absorbed_sources(
    merged_rows: List[JsonDict],
    payloads: Dict[str, JsonDict],
    identity_fields: List[str],
) -> List[str]:
    """Source artifacts whose every row survived into the merged output.

    "Fully absorbed" is the licence to call the merged artifact the delivered
    generation and retire the source into history. It has to be per-artifact
    and total: a source that kept nine of ten rows is not superseded by the
    merge, it was partially copied, and treating it as history would delete
    that tenth row from the delivered set with nothing recording the loss.

    A row whose identity cannot be computed (a declared identity field is
    missing or non-scalar) can never be shown to have survived, so it blocks
    absorption. That is the conservative direction: the cost is a merge that
    does not get to supersede, against a row that quietly stops being
    delivered.
    """
    merged_identities = {
        identity for identity in (
            _row_identity(row, identity_fields) for row in merged_rows
        ) if identity is not None
    }
    absorbed: List[str] = []
    for path_text, payload in payloads.items():
        source_rows = payload.get("rows")
        if not isinstance(source_rows, list) or not source_rows:
            continue
        identities = [_row_identity(row, identity_fields) for row in source_rows]
        if any(identity is None for identity in identities):
            continue
        if all(identity in merged_identities for identity in identities):
            absorbed.append(path_text)
    return absorbed


def _validate_lead_save_sources(
    agent: Any, raw_sources: Any,
) -> Tuple[List[str], Dict[str, JsonDict], Optional[JsonDict]]:
    """Validate cited source artifacts and return their parsed payloads.

    The payloads are returned rather than discarded because both the reference
    merge and the regression check need the source rows, and re-reading the
    files after validating them invites the two reads to disagree.
    """
    if not isinstance(raw_sources, list):
        return [], {}, {
            "status": "rejected",
            "error": "source_artifacts must be a non-empty array of extraction artifact paths",
        }
    source_texts = [
        str(path).strip()
        for path in raw_sources
        if isinstance(path, str) and str(path).strip()
    ]
    if not source_texts:
        return [], {}, {
            "status": "rejected",
            "error": "lead_save_artifact requires at least one source extraction artifact",
            "next_instruction": (
                "Read a worker record_extraction or extractionAttemptArtifacts path"
                " first; do not create reshaped rows without cited source evidence."
            ),
        }

    task_dir = Path(getattr(getattr(agent, "logger", None), "task_dir", "") or ".")
    try:
        task_root = task_dir.resolve(strict=False)
    except (OSError, ValueError):
        task_root = task_dir.absolute()

    validated: List[str] = []
    payloads: Dict[str, JsonDict] = {}
    for source in source_texts:
        try:
            path = Path(source).expanduser().resolve(strict=False)
        except (OSError, ValueError) as exc:
            return [], {}, {
                "status": "rejected",
                "error": f"invalid source_artifact path: {source}",
                "details": str(exc),
            }
        try:
            path.relative_to(task_root)
        except ValueError:
            return [], {}, {
                "status": "rejected",
                "error": "source_artifact must stay inside the current task worktree",
                "sourceArtifact": str(path),
                "taskDir": str(task_root),
            }
        normalized = str(path).replace("\\", "/")
        if "/artifacts/extractions/" not in normalized:
            return [], {}, {
                "status": "rejected",
                "error": "source_artifact must be an extraction artifact path",
                "sourceArtifact": str(path),
            }
        if not path.exists() or not path.is_file():
            return [], {}, {
                "status": "rejected",
                "error": "source_artifact does not exist",
                "sourceArtifact": str(path),
            }
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            return [], {}, {
                "status": "rejected",
                "error": "source_artifact must be readable JSON",
                "sourceArtifact": str(path),
                "details": str(exc),
            }
        if not isinstance(payload, dict) or not isinstance(payload.get("rows"), list):
            return [], {}, {
                "status": "rejected",
                "error": "source_artifact must contain a rows array",
                "sourceArtifact": str(path),
            }
        text = str(path)
        if text not in validated:
            validated.append(text)
        payloads[text] = payload
    return validated, payloads, None


@LEAD_TOOLS.register(
    name="final_answer",
    description="Terminate LeadAgent orchestration and return the final result.",
    input_schema=_final_answer_schema,
    terminal=True,
    loop_guard=False,
)
async def _lead_final_answer(ctx: ToolContext) -> JsonDict:
    state = load_task_state(ctx.agent.logger)
    final_status = str(ctx.tool_input.get("status", "done"))
    receipt = build_completion_receipt(
        state=state,
        spawner=getattr(ctx.agent, "spawner", None),
    )
    answer = str(ctx.tool_input.get("answer", "")).strip()
    reconciliation = await _reconcile_final_answer_numbers(ctx.agent, answer, state)
    rejection = _numeric_reconciliation_rejection(reconciliation)
    if rejection is not None:
        return rejection
    field_review: JsonDict = {}
    if final_status == "done":
        field_review = await _review_final_field_semantics(ctx.agent, state)
    if field_review.get("mismatches"):
        ctx.agent.logger.write("lead.field_semantic_mismatch", field_review)
        if final_status == "done":
            rejection_count = _record_field_semantic_rejection(
                ctx.agent, state, field_review,
            )
            return {
                "status": "rejected",
                "error": "field_semantic_mismatch",
                "tool_was_executed": False,
                "fieldSemanticReview": field_review,
                "unchangedRejectionCount": rejection_count,
                "completionReceipt": receipt,
                "next_instruction": (
                    "The semantic reviewer found delivered field values whose"
                    " evidence describes a different subject or unit than the"
                    " original request. Inspect the listed rows. Correct them"
                    " from validated evidence and re-issue done, continue"
                    " collection if the requested value remains obtainable,"
                    " or return a truthful non-done status that discloses the"
                    " unresolved requested fields. Do not rename a substitute"
                    " value as the requested field. This review targets artifact"
                    " content at sourceRefs, not answer wording; rewriting the"
                    " answer alone does not change that evidence."
                ),
            }
    ctx.agent.logger.write("lead.completion_receipt", receipt)
    result: JsonDict = {
        "status": final_status,
        "answer": answer,
        "trigger": "lead_decided",
        "completionReceipt": receipt,
    }
    if reconciliation:
        result["numericReconciliation"] = {
            key: value for key, value in reconciliation.items()
            if key != "claims"
        }
    if field_review:
        result["fieldSemanticReview"] = field_review
    return result


async def _review_final_field_semantics(agent: Any, state: Any) -> JsonDict:
    """Review fields whose evidence may describe a different requested value.

    Mechanical validators prove shape and provenance. The semantic reviewer
    alone decides subject/unit alignment; only its explicit mismatch verdicts
    can stop a `done` answer. Ambiguity and reviewer availability remain
    visible facts and are not promoted to mismatches.
    """
    provider = getattr(agent, "claim_extractor_provider", None)
    logger = getattr(agent, "logger", None)
    if provider is None:
        return {}
    try:
        index = build_semantic_fact_index(state, logger=logger)
        if index.get("readErrors"):
            return {"status": "unavailable", "reason": "artifact_content_unavailable",
                    "readErrors": index["readErrors"], "mismatches": []}
        entries = build_field_semantic_worklist(
            index, allowed_fields=_semantic_output_fields(agent),
        )
        array_evidence_gaps = array_fields_without_semantic_evidence(
            index, allowed_fields=_semantic_output_fields(agent),
        )
        if not entries:
            return (
                {"status": "ok", "arrayEvidenceGaps": array_evidence_gaps}
                if array_evidence_gaps else {}
            )
        from harness.planning.context import user_context, assignment_view
        context = user_context(logger, getattr(agent, "original_user_task", ""), state=state)
        phases = {p["id"]: p for p in (getattr(agent, "task_plan", None) or {}).get("phases", [])}
        for entry in entries:
            phase = phases.get((entry.get("validationReceipt") or {}).get("phaseId"))
            if phase:
                view = assignment_view(phase)
                entry["assignmentContext"] = {key: view[key] for key in ("id", "task", "output", "replaces")}
        cache_key = hashlib.sha256(json.dumps(
            {
                "projectionVersion": SEMANTIC_PROJECTION_VERSION,
                "userContext": context,
                "entries": entries,
                "arrayEvidenceGaps": array_evidence_gaps,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")).hexdigest()
        cached = getattr(agent, "_field_semantic_review_cache", None)
        if (
            isinstance(cached, dict)
            and cached.get("key") == cache_key
            and isinstance(cached.get("review"), dict)
        ):
            if logger is not None and hasattr(logger, "write"):
                logger.write("field_semantic_review.cache_hit", {
                    "entriesSupplied": len(entries),
                    "cacheKey": cache_key,
                })
            return dict(cached["review"])
        review = await review_field_semantics(
            provider,
            user_context=context,
            entries=entries,
            logger=logger,
            provider_name=str(getattr(agent, "claim_extractor_provider_name", "")),
            model_id=str(getattr(agent, "claim_extractor_model", "")),
        )
        if array_evidence_gaps:
            review = {**review, "arrayEvidenceGaps": array_evidence_gaps}
        if review.get("status") == "reviewed" and not review.get("coverageErrors"):
            agent._field_semantic_review_cache = {
                "key": cache_key,
                "review": dict(review),
            }
        return review
    except Exception as exc:  # noqa: BLE001 - an advisory review never fails a run
        return {
            "status": "unavailable",
            "reason": "field_semantic_review_failed",
            "error": str(exc)[:300],
            "mismatches": [],
        }


def _semantic_output_fields(agent: Any) -> Optional[set[str]]:
    """Fields declared as task deliverables, excluding provenance plumbing."""
    plan = getattr(agent, "task_plan", None)
    phases = plan.get("phases") if isinstance(plan, dict) else None
    if not isinstance(phases, list):
        return None
    fields: set[str] = set()
    for phase in phases:
        expected = phase.get("expected_artifact") if isinstance(phase, dict) else None
        if not isinstance(expected, dict):
            continue
        for item in expected.get("fields") or expected.get("required_fields") or []:
            if isinstance(item, str) and item.strip():
                fields.add(item.strip())
            elif isinstance(item, dict) and str(item.get("name") or "").strip():
                fields.add(str(item["name"]).strip())
    return fields or None


def _record_field_semantic_rejection(
    agent: Any, state: Any, review: JsonDict,
) -> int:
    """Persist bounded retries for one unchanged semantic-review input.

    The cache key includes the task and every reviewed evidence entry. It is a
    stable identity for the question, not a verdict: changed evidence receives
    a fresh review and a fresh retry budget. The state record survives resume,
    preventing a process restart from recreating the same terminal loop.
    """
    cached = getattr(agent, "_field_semantic_review_cache", None)
    key = str(cached.get("key") or "") if isinstance(cached, dict) else ""
    if not key:
        mismatches = review.get("mismatches") if isinstance(review, dict) else None
        stable_findings = [
            {
                "entryId": str(item.get("entryId") or ""),
                "assessment": str(item.get("assessment") or ""),
                "field": str(item.get("field") or ""),
                "value": str(item.get("value") or ""),
                "artifact": str(item.get("artifact") or ""),
            }
            for item in mismatches if isinstance(item, dict)
        ] if isinstance(mismatches, list) else []
        if stable_findings:
            key = hashlib.sha256(json.dumps(
                sorted(stable_findings, key=lambda item: (
                    item["entryId"], item["field"], item["artifact"], item["assessment"],
                )),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")).hexdigest()
    if not key or not isinstance(state, dict):
        return 1
    attempts = state.get("field_semantic_rejections")
    attempts = dict(attempts) if isinstance(attempts, dict) else {}
    previous = attempts.get(key)
    count = int(previous.get("count") or 0) + 1 if isinstance(previous, dict) else 1
    attempts[key] = {"count": count}
    # Bound durable bookkeeping: only current/recent evidence keys can affect
    # terminal behavior, while older keys are merely stale diagnostics.
    if len(attempts) > 20:
        attempts = dict(list(attempts.items())[-20:])
    state["field_semantic_rejections"] = attempts
    write_task_state(getattr(agent, "logger", None), state)
    return count


async def _reconcile_final_answer_numbers(
    agent: Any, answer: str, state: Any,
) -> JsonDict:
    """Recompute the quantities the answer asserts, from the ledgers.

    Extraction is a model call because binding "18 条" to a review count needs
    the sentence around it; the comparison that follows is pure lookup. An
    unreachable extractor is reported, not treated as a verdict — but a claim
    it cannot anchor in the answer verbatim fails the whole report, because
    skipping the number we could not parse is how the wrong one gets through.
    """
    provider = getattr(agent, "claim_extractor_provider", None)
    logger = getattr(agent, "logger", None)
    if provider is None or not answer:
        return {}
    try:
        index = build_numeric_fact_index(
            state, task_dir=getattr(logger, "task_dir", None), logger=logger,
        )
        extracted = await extract_numeric_claims(
            provider,
            answer=answer,
            index=index,
            logger=logger,
            provider_name=str(getattr(agent, "claim_extractor_provider_name", "")),
            model_id=str(getattr(agent, "claim_extractor_model", "")),
        )
        extraction_status = str(extracted.get("status") or "")
        if extraction_status == "partial":
            report = reconcile_numeric_claims(
                list(extracted.get("claims") or []),
                answer=answer,
                index=index,
                spans=extracted.get("spans"),
                enforce_coverage=False,
            )
            if report.get("status") not in {"failed", "span_validation_failed"}:
                report["status"] = "inconclusive"
            report["extractorErrors"] = str(extracted.get("error") or "")[:300]
            report["repairAttempted"] = bool(extracted.get("repairAttempted"))
        elif extraction_status != "ok":
            # Carry the extractor's own distinction through: `unavailable`
            # (could not reach it) is not a finding about the answer, while
            # `extractor_unusable` (reached it, got nothing usable back) means
            # this answer went unchecked.
            report = {
                "status": str(extracted.get("status") or "unavailable"),
                "error": str(extracted.get("error") or "")[:300],
                "checked": 0,
                "verifiedClaimCount": 0,
            }
        else:
            report = reconcile_numeric_claims(
                list(extracted.get("claims") or []),
                answer=answer,
                index=index,
                spans=extracted.get("spans"),
            )
    except Exception as exc:  # never block termination on a checker defect
        report = {"status": "unavailable", "error": f"{type(exc).__name__}: {exc}"}
    if logger is not None and hasattr(logger, "write"):
        # `claims` is too large to log, but dropping it wholesale made a
        # `passed` unreadable: twelve numbers confirmed against the ledger and
        # twelve nobody could find both logged as "checked: 12, contradicted:
        # 0". The histogram is small and is the difference between a gate that
        # held and a gate that had nothing to hold on to.
        logger.write("lead.numeric_reconciliation", {
            **{key: value for key, value in report.items() if key != "claims"},
            **(
                {"verdicts": _verdict_histogram(report.get("claims"))}
                if isinstance(report.get("claims"), list) else {}
            ),
            **(
                {"unresolvedReasons": _unresolved_reason_histogram(report.get("claims"))}
                if isinstance(report.get("claims"), list) else {}
            ),
        })
    return report


def _verdict_histogram(claims: Any) -> JsonDict:
    """Counts per verdict, so `passed` says which kind of pass it was."""
    histogram: Dict[str, int] = {}
    for claim in claims if isinstance(claims, list) else []:
        verdict = str((claim or {}).get("verdict") or "unknown")
        histogram[verdict] = histogram.get(verdict, 0) + 1
    return dict(sorted(histogram.items()))


def _unresolved_reason_histogram(claims: Any) -> JsonDict:
    """Expose why otherwise valid numeric bindings could not be resolved."""
    histogram: Dict[str, int] = {}
    for claim in claims if isinstance(claims, list) else []:
        if str((claim or {}).get("verdict") or "") != "unresolved":
            continue
        reason = str((claim or {}).get("reason") or "unspecified")[:160]
        histogram[reason] = histogram.get(reason, 0) + 1
    return dict(sorted(histogram.items()))


def _numeric_reconciliation_rejection(report: JsonDict) -> Optional[JsonDict]:
    """Reject only an exact, mechanically bound ledger contradiction.

    Extractor, span and coverage failures remain visible in the final receipt,
    but are observations about what the checker could not establish.  They are
    not evidence that the model's statement is false.
    """
    status = str((report or {}).get("status") or "")
    if status in {
        "span_validation_failed",
        "extractor_unusable",
        "coverage_failed",
        "inconclusive",
        "unavailable",
    }:
        return None
    if status != "failed" or not report.get("contradicted"):
        return None
    return {
        "status": "rejected",
        "error": "numeric_claim_mismatch",
        # Send the answer back for repair; do not end the task on it.
        "tool_was_executed": False,
        "cause": (
            "a number disagrees with the artifacts this task delivers"
        ),
        "contradicted": report.get("contradicted"),
        "dataConflicts": report.get("dataConflicts"),
        "next_instruction": (
            "These quantities differ from their bound current artifact values. "
            "Review each sourceArtifact and counted field, then correct the "
            "answer or its binding. Historical differences are advisory: "
            "deduplication, filtering or correction may legitimately reduce "
            "counts; do not restore rows solely to match an older generation."
        ),
    }


def _matching_exhaustion(exhausted: List[JsonDict], phase_id: Any) -> Any:
    if not exhausted:
        return None
    wanted = str(phase_id or "").strip()
    if wanted:
        for item in exhausted:
            if str(item.get("phaseId") or "") == wanted:
                return item
        return None
    return exhausted[-1]


PLANNING_LEAD_TOOLS = frozenset({
    "spawn_browser_agent", "wait_browser_agents", "list_browser_agents",
    "local_fs_search", "local_fs_read", "read_harness_guide", "search_harness_guides",
    "revalidate_phase_artifacts", "lead_save_artifact", "final_answer",
})


def build_lead_agent_tool_specs(
    *, include_resume: bool = False, stage: str = "all",
) -> List[JsonDict]:
    # The model sees one execution surface.  Internal helpers used for state
    # validation and evidence bookkeeping are not part of the Lead contract
    # and cannot be selected by a provider from its schema.
    return [spec for spec in LEAD_TOOLS.tool_specs()
            if spec.get("name") in PLANNING_LEAD_TOOLS]
