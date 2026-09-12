"""
harness.tools.lead_tools - LeadAgent tool schemas and dispatch factory.
"""

import copy
import hashlib
import json
import time
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

from harness.evidence.extraction_artifacts import (
    save_extraction_artifact,
    validate_extraction_rows,
)
from harness.evidence.artifact_evidence import VALIDATOR_TYPES
from harness.fleet.coordinator import VALID_PAGE_POLICIES, VALID_REUSE_SCOPES
from harness.lifecycle import LifecycleContext, lifecycle_for
from harness.local_fs import local_fs_read, local_fs_search
from harness.prompts import read_harness_guide
from harness.prompts import search_harness_guides
from harness.strategy_bank import render_strategy_guidance
from harness.task_control import (
    EXECUTION_ROLES,
    TERMINAL_PHASE_STATUSES,
    VALID_STAGE_HINTS,
    assess_batch_source_binding,
    contract_hash_for_phase,
    direct_batch_rows_provenance_errors,
    find_phase,
    mark_phase_exhausted_if_needed,
    materialize_batch_rows_from_source,
    phase_contract,
    phase_prior_artifact_paths,
    reactivate_resumable_hitl_phases,
    replan_checkpoint_spawn_rejection,
    load_task_state,
    write_task_state,
)
from harness.results.completion_receipt import (
    artifact_generation_view,
    build_completion_receipt,
    terminal_consistency_contradictions,
)
from harness.evidence.field_semantics import (
    array_fields_without_semantic_evidence,
    build_field_semantic_worklist,
    review_field_semantics,
)
from harness.numeric_facts import (
    build_numeric_fact_index,
    extract_numeric_claims,
    reconcile_numeric_claims,
)
from harness.task_types import (
    VALID_TASK_TYPES,
    normalize_task_type,
    task_type_choices_for_error,
)
from harness.tool_policy import describe_task_types
from harness.tools.argument_pipeline import SchemaIssue
from harness.tools.argument_pipeline import apply_registered_tool_defaults
from harness.tools.argument_pipeline import prepare_model_tool_call
from harness.tools.argument_pipeline import tool_argument_error
from harness.tools.argument_pipeline import validate_registered_tool_call
from harness.tools.loop_guard import check_tool_call_loop
from harness.tools.registry import ToolContext, ToolRegistry
from harness.utils import (
    JsonDict,
    contains_affirmative_semantic_marker,
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


def _normalize_lead_task_type_aliases(
    tool_call: Any,
) -> Tuple[Any, List[str]]:
    """Canonicalise supported legacy task-type spellings before schema checks.

    The model-facing schema advertises only policy-bearing canonical values.
    Older saved prompts and lifecycle middleware may still produce an accepted
    alias, so preparation maps known aliases before validation rather than
    weakening the public enum. Unknown strings remain unchanged and fail
    schema validation.
    """
    if not isinstance(tool_call, dict):
        return tool_call, []
    raw_input = tool_call.get("input")
    if not isinstance(raw_input, dict):
        return tool_call, []
    prepared_input = copy.deepcopy(raw_input)
    changed: List[str] = []

    def visit(value: Any, path: Tuple[str, ...]) -> None:
        if isinstance(value, dict):
            raw_task_type = value.get("task_type")
            if isinstance(raw_task_type, str):
                canonical = normalize_task_type(raw_task_type)
                if canonical != raw_task_type and canonical in VALID_TASK_TYPES:
                    value["task_type"] = canonical
                    changed.append(".".join(path + ("task_type",)))
            for key, child in value.items():
                if key != "task_type":
                    visit(child, path + (str(key),))
        elif isinstance(value, list):
            for index, child in enumerate(value):
                visit(child, path + (str(index),))

    visit(prepared_input, ())
    if not changed:
        return tool_call, []
    prepared_call = dict(tool_call)
    prepared_call["input"] = prepared_input
    return prepared_call, changed


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
                "enum": sorted(VALIDATOR_TYPES),
                "description": (
                    "Validator kind. range is for numeric scalar values; "
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
                "type": "array",
                "items": {"type": "string"},
                "description": "Target fields for required_fields/field_nonempty/unique.",
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
                "description": "Regex for url_pattern/field_pattern.",
            },
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
                "minimum": 1,
                "description": "Minimum selected/downloaded/exported file count for file validators.",
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
            "path_pattern": {"type": "string"},
        },
        "required": ["type"],
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


def _emit_task_plan_schema(_: Any = None) -> JsonDict:
    from harness.pacing import MAX_PACING_INTERVAL_SECONDS

    pacing_schema = {
        "type": "object",
        "properties": {
            "row_interval_seconds": {
                "type": "number", "minimum": 0,
                "maximum": MAX_PACING_INTERVAL_SECONDS,
            },
            "phase_interval_seconds": {
                "type": "number", "minimum": 0,
                "maximum": MAX_PACING_INTERVAL_SECONDS,
            },
            "jitter_ratio": {"type": "number", "minimum": 0, "maximum": 1},
        },
        "additionalProperties": False,
    }
    schema = {
        "type": "object",
        "properties": {
            "plan": {
                "type": "object",
                "description": (
                    "Plan object with a goal and phases array. Overall"
                    " task_type is optional and derived from phase types."
                    " Each phase needs id, type='browser_worker', task_type,"
                    " objective,"
                    " expected_artifact. Validators are derived from that"
                    " contract; explicit special validators are optional."
                    " max_attempts is an"
                    " optional explicit resource budget."
                    " Every phase declares its OWN task_type — it is not"
                    " inherited from the plan, because that is what decides"
                    " which method domains the phase's worker can call."
                    " Scheduling: depends_on OMITTED = the phase implicitly"
                    " depends on ALL phases listed before it (strict serial"
                    " order); depends_on=[] = independent, startable"
                    " immediately; depends_on=[ids] = exactly those phases"
                    " must be validated_done first. Phases whose dependencies"
                    " are satisfied can be spawned in parallel."
                ),
                "properties": {
                    "goal": {"type": "string"},
                    "task_type": {
                        "type": "string",
                        "enum": sorted(VALID_TASK_TYPES),
                        "description": (
                            "Overall classification of the task, used for"
                            " strategy selection and audit. It does NOT set"
                            " worker method access — each phase declares its"
                            " own task_type for that."
                        ),
                    },
                    "replan_reason": {
                        "type": "string",
                        "minLength": 1,
                        "description": (
                            "REQUIRED once a plan has been accepted, because"
                            " this call then REPLACES it: say why the accepted"
                            " plan has to go. Omit it only on the first plan of"
                            " a run. Without it the call is rejected with"
                            " replan_reason_required and nothing changes —"
                            " re-sending the same phases will not help."
                        ),
                    },
                    "replan_checkpoint_id": {
                        "type": "string",
                        "description": (
                            "Legacy single-checkpoint acknowledgement. Use"
                            " replan_checkpoint_ids when more than one cohort"
                            " is active."
                        ),
                    },
                    "replan_checkpoint_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "On replan, the exact set of every active"
                            " checkpointId returned by the harness."
                        ),
                    },
                    "pacing": pacing_schema,
                    "output_contracts": {
                        "type": "object",
                        "description": (
                            "Reusable output contracts. A phase references one with"
                            " output_ref and declares only its row range or identity."
                            " The harness expands the reference before review and"
                            " execution, so do not repeat common fields per phase."
                        ),
                        "additionalProperties": {"type": "object"},
                    },
                    "phases": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "id": {"type": "string"},
                                "type": {"type": "string"},
                                "task_type": {
                                    "type": "string",
                                    "enum": sorted(VALID_TASK_TYPES),
                                    "description": (
                                        "REQUIRED per phase. Phases do NOT"
                                        " inherit the plan task_type: a plan"
                                        " that collects listings and then"
                                        " exports media has a web_scrape phase"
                                        " and a file_download phase, and each"
                                        " must say so itself. "
                                        + describe_task_types()
                                    ),
                                },
                                "task": {
                                    "type": "string",
                                    "description": (
                                        "Compact phase instruction. When objective and"
                                        " worker_task are omitted, the harness uses this"
                                        " one statement for both."
                                    ),
                                },
                                "objective": {"type": "string"},
                                "worker_task": {
                                    "type": "string",
                                    "description": (
                                        "Stable phase goal and observable"
                                        " obligations, not a single tactical"
                                        " script. For listing-derived detail"
                                        " work preserve the source page and"
                                        " identity, and make freshly rebound"
                                        " source-card clicks the normal first"
                                        " route. Direct URL navigation is a"
                                        " fallback when source traversal is"
                                        " unavailable or cannot be verified."
                                    ),
                                },
                                "stage_hint": {"type": "string"},
                                "stage_hint_reason": {"type": "string"},
                                "execution_role": {
                                    "type": "string",
                                    "enum": sorted(EXECUTION_ROLES),
                                    "description": (
                                        "Evidence-driven execution role, not a mandatory three-"
                                        "stage template. Use probe (at most one row) only when a"
                                        " reusable path is unknown. Use validation (at most two)"
                                        " only when the probe checkpoint authorizes confidence"
                                        " testing, and bulk only after validation authorizes it."
                                        " If no reusable candidate was produced, use continuation"
                                        " for remaining BrowserAgent slow-path rows. remediation"
                                        " consumes an explicit failed-row set. Do not invent empty"
                                        " validation/bulk phases merely to complete a ladder."
                                    ),
                                },
                                "dispatch_wave": {
                                    "type": "integer",
                                    "minimum": 1,
                                    "description": (
                                        "Optional operator-visible scheduling wave."
                                        " A phase in wave N is not dispatched until"
                                        " every declared lower-wave phase is"
                                        " validated_done. Use this for the one-page"
                                        " first sample followed by concurrent sibling"
                                        " groups; do not encode this scheduling wait"
                                        " as a false data dependency."
                                    ),
                                },
                                "expected_artifact": {
                                    **_expected_artifact_schema(),
                                },
                                "output_ref": {
                                    "type": "string",
                                    "description": (
                                        "Name of a plan.output_contracts entry. The phase"
                                        " may add output_contract row-specific overrides."
                                    ),
                                },
                                "output_contract": {
                                    "type": "object",
                                    "description": (
                                        "Compact output contract or override. It supports"
                                        " rows:{exact|min|max,identity} and fields keyed by"
                                        " field name with required, type, empty, provenance,"
                                        " minItems/maxItems, pattern, or allowedDomains. For"
                                        " empty='with_evidence', declare a non-empty"
                                        " allow_empty_with_outcome list in that field spec,"
                                        " e.g. fields.reviews={type:'array',empty:"
                                        "'with_evidence',allow_empty_with_outcome:"
                                        "['confirmed_absent']}. The legacy contract-level"
                                        " map is also accepted."
                                    ),
                                    "additionalProperties": True,
                                },
                                "depends_on": {
                                    "type": "array",
                                    "items": {"type": "string"},
                                    "description": (
                                        "Phase ids that must be validated_done"
                                        " before this phase can start. OMIT for"
                                        " strict serial order (implicitly"
                                        " depends on all prior phases); [] for"
                                        " an independent phase; list only the"
                                        " true data dependencies (e.g. every"
                                        " detail phase depends only on the"
                                        " collection phase) so independent"
                                        " phases can run in parallel."
                                    ),
                                },
                                "input_artifacts": {
                                    "type": "array",
                                    "minItems": 1,
                                    "description": (
                                        "Explicit data lineage for artifacts this phase consumes. "
                                        "Each reference names the producing phase and its reviewed "
                                        "expected_artifact.name. This is not inferred from plan order "
                                        "or equal row counts. Also include every referenced phase in "
                                        "depends_on so it is validated before this phase starts."
                                    ),
                                    "items": {
                                        "type": "object",
                                        "properties": {
                                            "phase_id": {"type": "string", "minLength": 1},
                                            "artifact_name": {"type": "string", "minLength": 1},
                                        },
                                        "required": ["phase_id", "artifact_name"],
                                        "additionalProperties": False,
                                    },
                                },
                                "inputs": {
                                    "type": "object",
                                    "description": (
                                        "Compact input source. Use artifact:{phase_id,"
                                        "artifact_name,selector?} for a validated upstream"
                                        " artifact, or direct:{rows,identity_fields} only"
                                        " for user-supplied targets."
                                    ),
                                    "additionalProperties": True,
                                },
                                "pacing": pacing_schema,
                                "validators": {
                                    "type": "array",
                                    "description": (
                                        "Optional array of special validators"
                                        " that cannot be derived from"
                                        " expected_artifact. Common field and"
                                        " row validators are derived."
                                    ),
                                    "items": _validator_item_schema(),
                                },
                                "additional_checks": {
                                    "type": "array",
                                    "description": (
                                        "Only checks that cannot be derived from output_contract."
                                        " Prefer this compact name for new plans."
                                    ),
                                    "items": _validator_item_schema(),
                                },
                                "worker_contract": {
                                    "type": "object",
                                    "properties": {
                                        "reuse_scope": {
                                            "type": "string",
                                            "enum": sorted(VALID_REUSE_SCOPES),
                                        },
                                        "fleet_id": {
                                            "type": "string",
                                            "description": (
                                                "Existing Fleet UUID or unique"
                                                " UUID prefix from the user or"
                                                " authoritative evidence. Never"
                                                " use it as session_key."
                                            ),
                                        },
                                        "session_key": {"type": "string"},
                                        "page_policy": {
                                            "type": "string",
                                            "enum": sorted(VALID_PAGE_POLICIES),
                                        },
                                        "needs_isolated_session": {"type": "boolean"},
                                        "auth_verification": _auth_verification_schema(),
                                        "content_completeness": _content_completeness_schema(),
                                        "batch_rows": {
                                            "type": "array",
                                            "items": {"type": "object"},
                                            "minItems": 1,
                                            "description": (
                                                "Explicit homogeneous input rows, allowed only when"
                                                " their identities/URLs were supplied directly by the"
                                                " user and no upstream browser artifact exists. Use"
                                                " batch_source for browser-discovered rows."
                                            ),
                                        },
                                        "batch_source": {
                                            "type": "object",
                                            "description": (
                                                "Validated extraction artifact used to mechanically"
                                                " construct batch_rows at spawn time."
                                            ),
                                            "properties": {
                                                "artifact_name": {"type": "string"},
                                                "cohort_selector": {
                                                    "type": "object",
                                                    "description": (
                                                        "Optional stable target"
                                                        " universe inside a larger"
                                                        " artifact. It remains"
                                                        " identical across probe,"
                                                        " validation, and bulk."
                                                    ),
                                                    "properties": {
                                                        "field": {"type": "string"},
                                                        "values": {
                                                            "type": "array",
                                                            "minItems": 1,
                                                        },
                                                    },
                                                    "required": ["field", "values"],
                                                    "additionalProperties": False,
                                                },
                                                "selector": {
                                                    "type": "object",
                                                    "properties": {
                                                        "field": {"type": "string"},
                                                        "values": {"type": "array"},
                                                        "indices": {
                                                            "type": "array",
                                                            "items": {"type": "integer", "minimum": 0},
                                                            "minItems": 1,
                                                        },
                                                        "offset": {"type": "integer", "minimum": 0},
                                                        "limit": {"type": "integer", "minimum": 1},
                                                    },
                                                    "additionalProperties": False,
                                                },
                                            },
                                            "required": ["artifact_name"],
                                            "additionalProperties": False,
                                        },
                                        "replan_checkpoint_id": {
                                            "type": "string",
                                            "description": (
                                                "Bind this phase to exactly one"
                                                " active checkpoint when a"
                                                " replan advances multiple"
                                                " cohorts."
                                            ),
                                        },
                                        "batch_rows_provenance": {
                                            "type": "object",
                                            "description": (
                                                "Required only with direct"
                                                " batch_rows. Mechanically"
                                                " proves each row identity came"
                                                " from the immutable user"
                                                " instruction rather than a"
                                                " browser-discovered summary."
                                            ),
                                            "properties": {
                                                "source": {
                                                    "type": "string",
                                                    "enum": ["user_instruction"],
                                                },
                                                "identity_fields": {
                                                    "type": "array",
                                                    "minItems": 1,
                                                    "items": {
                                                        "type": "string",
                                                        "minLength": 1,
                                                    },
                                                },
                                            },
                                            "required": [
                                                "source",
                                                "identity_fields",
                                            ],
                                            "additionalProperties": False,
                                        },
                                        "batch_policy": {
                                            "type": "object",
                                            "properties": {
                                                "max_rows_per_phase": {
                                                    "type": "integer", "minimum": 1
                                                },
                                                "row_independent": {"type": "boolean"},
                                                "requires_isolation_per_row": {
                                                    "type": "boolean",
                                                    "description": (
                                                        "True only when each row requires a distinct"
                                                        " identity/session boundary. This exempts"
                                                        " singleton phases from cohort consolidation;"
                                                        " needs_isolated_session alone is worker-level."
                                                    ),
                                                },
                                            },
                                            "additionalProperties": False,
                                        },
                                    },
                                    "additionalProperties": True,
                                },
                                "max_attempts": {"type": "integer", "minimum": 1},
                            },
                            "required": ["id", "task_type"],
                            "additionalProperties": True,
                        },
                    },
                },
                "required": ["goal", "phases"],
                "additionalProperties": True,
            },
        },
        "required": ["plan"],
        "additionalProperties": False,
    }
    return schema


def _direct_task_plan_schema(_: Any = None) -> JsonDict:
    """Small external contract for a single coherent BrowserAgent task.

    The runtime compiles this declaration to the normal one-phase v1 plan, so
    all existing mechanical checks, independent review and operator approval
    still apply.  Keeping the phase/scheduling scaffolding out of this tool is
    intentional: the Lead chooses the route, while the harness owns the
    executable representation.
    """
    return {
        "type": "object",
        "description": (
            "Submit a compact direct-worker plan for one coherent browser task. "
            "The harness expands it to one canonical browser_worker phase, runs "
            "the same PlanValidator and operator approval, then dispatches and "
            "waits without asking Lead to copy spawn arguments. Use this only "
            "when no second phase, cross-worker merge, or parallel coordination "
            "is required."
        ),
        "properties": {
            "goal": {"type": "string", "minLength": 1},
            "task_type": {
                "type": "string",
                "enum": sorted(VALID_TASK_TYPES),
            },
            "stage_hint": {
                "type": "string",
                "enum": sorted(VALID_STAGE_HINTS),
            },
            "task": {
                "type": "string",
                "minLength": 1,
                "description": "Complete worker instruction for this one task.",
            },
            "output_contract": {
                **_expected_artifact_schema(),
                "description": (
                    "The compact deliverable contract. Use fields/rows and "
                    "provenance requirements exactly as in emit_task_plan."
                ),
            },
            "worker_contract": {
                "type": "object",
                "additionalProperties": True,
                "description": (
                    "Optional routing/session and worker policy details. "
                    "Do not put a second objective or alternate artifact here."
                ),
            },
            "additional_checks": {
                "type": "array",
                "items": {"type": "object", "additionalProperties": True},
                "description": "Optional checks the output contract cannot express.",
            },
            "max_steps": {"type": "integer", "minimum": 1},
            "max_attempts": {"type": "integer", "minimum": 1, "maximum": 8},
        },
        "required": [
            "goal", "task_type", "stage_hint", "task", "output_contract",
        ],
        "additionalProperties": False,
    }


def _compile_direct_task_plan(raw: Any) -> Tuple[Optional[JsonDict], Optional[JsonDict]]:
    """Compile the compact direct declaration to the canonical plan shape."""
    if not isinstance(raw, dict):
        return None, {
            "status": "failed",
            "error": "direct plan must be an object",
            "tool_was_executed": False,
        }
    goal = str(raw.get("goal") or "").strip()
    task = str(raw.get("task") or "").strip()
    task_type = normalize_task_type(raw.get("task_type"))
    stage_hint = str(raw.get("stage_hint") or "").strip()
    contract = raw.get("output_contract")
    if not goal or not task or task_type not in VALID_TASK_TYPES:
        return None, {
            "status": "failed",
            "error": "goal, task and a canonical task_type are required",
            "tool_was_executed": False,
        }
    if stage_hint not in VALID_STAGE_HINTS:
        return None, {
            "status": "failed",
            "error": f"stage_hint must be one of {sorted(VALID_STAGE_HINTS)}",
            "tool_was_executed": False,
        }
    if not isinstance(contract, dict):
        return None, {
            "status": "failed",
            "error": "output_contract must be an object",
            "tool_was_executed": False,
        }
    phase: JsonDict = {
        "id": "direct_worker",
        "type": "browser_worker",
        "task_type": task_type,
        "objective": goal,
        "worker_task": task,
        "stage_hint": stage_hint,
        "depends_on": [],
        "expected_artifact": copy.deepcopy(contract),
        "worker_contract": copy.deepcopy(
            raw.get("worker_contract")
            if isinstance(raw.get("worker_contract"), dict) else {}
        ),
    }
    if isinstance(raw.get("additional_checks"), list):
        phase["additional_checks"] = copy.deepcopy(raw["additional_checks"])
    # Make the bounded continuation budget visible in the approved canonical
    # phase.  The direct external contract may omit it, but the user should be
    # able to review the exact retry ceiling before execution begins.
    phase["max_attempts"] = 3
    for key in ("max_steps", "max_attempts"):
        value = raw.get(key)
        if isinstance(value, int) and not isinstance(value, bool) and value > 0:
            phase[key] = value
    return {
        "version": "v1",
        "execution_mode": "direct_worker",
        "goal": goal,
        "task_type": task_type,
        "phases": [phase],
    }, None


def _direct_row_count(result: Any) -> int:
    if not isinstance(result, dict):
        return 0
    digest = result.get("attemptDigest")
    if isinstance(digest, dict):
        try:
            return max(0, int(digest.get("rowCount") or 0))
        except (TypeError, ValueError):
            pass
    validation = result.get("artifactValidation")
    if isinstance(validation, dict):
        try:
            return max(0, int(validation.get("rowCount") or 0))
        except (TypeError, ValueError):
            pass
    progress = result.get("progressSnapshot")
    if isinstance(progress, dict):
        try:
            return max(0, int(progress.get("rowCount") or 0))
        except (TypeError, ValueError):
            pass
    return 0


def _direct_failure_signature(result: Any) -> Tuple[str, ...]:
    if not isinstance(result, dict):
        return ("invalid_result",)
    digest = result.get("attemptDigest")
    if isinstance(digest, dict) and isinstance(digest.get("failureSignature"), list):
        values = tuple(str(item or "")[:160] for item in digest["failureSignature"])
        if any(values):
            return values
    classification = result.get("errorClassification")
    category = (
        classification.get("category")
        if isinstance(classification, dict) else ""
    )
    return (
        str(result.get("status") or "unknown"),
        str(result.get("statusCategory") or "unknown"),
        str(category or ""),
    )


def _direct_dispatch_manifest(agent: Any, phase: JsonDict, attempt: int) -> JsonDict:
    """Stable, mechanical identity for one runtime-owned direct dispatch."""
    state = load_task_state(agent.logger)
    plan_version = int(state.get("plan_version") or 0)
    plan_hash = str(state.get("plan_hash") or "")
    phase_id = str(phase.get("id") or "direct_worker")
    worker_contract = (
        agent.build_worker_contract(phase)
        if hasattr(agent, "build_worker_contract") else phase_contract(phase)
    )
    contract_hash = contract_hash_for_phase(phase, worker_contract)
    return {
        "planVersion": plan_version,
        "planHash": plan_hash,
        "phaseId": phase_id,
        "attempt": int(attempt),
        "contractHash": contract_hash,
        "taskType": str(phase.get("task_type") or ""),
    }


def _direct_continuation_receipt(
    phase: JsonDict, result: JsonDict, dispatch_identity: JsonDict,
) -> JsonDict:
    """Expose only mechanically enumerable units; unknown coverage stays unknown."""
    validation = result.get("artifactValidation")
    unit_receipt = (
        validation.get("enumeratedUnitReceipt")
        if isinstance(validation, dict) else None
    )
    if (
        isinstance(unit_receipt, dict)
        and unit_receipt.get("kind") == "required_controls"
        and unit_receipt.get("coverage") == "row_validated"
        and isinstance(unit_receipt.get("completedUnitIds"), list)
        and isinstance(unit_receipt.get("remainingUnitIds"), list)
    ):
        return {
            "protocol": "direct-v1",
            "sourcePlanVersion": dispatch_identity["planVersion"],
            "sourceContractHash": dispatch_identity["contractHash"],
            "unitKind": "required_controls",
            "completedUnitIds": list(unit_receipt["completedUnitIds"]),
            "remainingUnitIds": list(unit_receipt["remainingUnitIds"]),
            "coverage": "row_validated",
            "sourceArtifactPaths": list(
                unit_receipt.get("sourceArtifactPaths") or []
            ),
            "invalidUnits": list(unit_receipt.get("invalidUnits") or []),
        }
    contract = phase.get("worker_contract")
    rows = contract.get("batch_rows") if isinstance(contract, dict) else None
    if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
        return {
            "protocol": "direct-v1",
            "sourcePlanVersion": dispatch_identity["planVersion"],
            "sourceContractHash": dispatch_identity["contractHash"],
            "completedUnitIds": [],
            "remainingUnitIds": [],
            "coverage": "not_enumerable",
        }
    units = [
        "row:" + hashlib.sha256(json.dumps(
            row, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str,
        ).encode("utf-8")).hexdigest()[:16]
        for row in rows
    ]
    # A partial artifact is deliberately not in the validated-artifact ledger,
    # so its rows cannot be removed from the next worker's contract. The receipt
    # says that explicitly instead of guessing from a row count or worker prose.
    validated_done = (
        str(result.get("status") or "").lower() == "done"
        and str(result.get("validatedStatus") or "").lower() == "validated_done"
    )
    return {
        "protocol": "direct-v1",
        "sourcePlanVersion": dispatch_identity["planVersion"],
        "sourceContractHash": dispatch_identity["contractHash"],
        "completedUnitIds": units if validated_done else [],
        "remainingUnitIds": [] if validated_done else units,
        "coverage": "validated" if validated_done else "declared_units_unproven",
    }


def _direct_continuation_context(base_context: str, receipt: Any) -> str:
    """Give a direct retry an auditable unit boundary without changing its plan.

    The worker keeps the approved, complete output contract. It emits only the
    newly completed control rows; artifact validation merges those rows with
    prior persisted evidence and still applies the original full contract.
    """
    if not isinstance(receipt, dict):
        return base_context
    if (
        receipt.get("unitKind") != "required_controls"
        or receipt.get("coverage") != "row_validated"
    ):
        return base_context
    remaining = [
        str(item)[len("control:"):]
        for item in receipt.get("remainingUnitIds") or []
        if isinstance(item, str) and item.startswith("control:")
    ]
    completed = [
        str(item)[len("control:"):]
        for item in receipt.get("completedUnitIds") or []
        if isinstance(item, str) and item.startswith("control:")
    ]
    if not remaining:
        return base_context
    guidance = (
        "DIRECT CONTINUATION RECEIPT: The following form controls already have "
        "individually validated persisted rows and must not be re-entered: "
        f"{json.dumps(completed, ensure_ascii=False)}. Complete only these remaining "
        "controls: "
        f"{json.dumps(remaining, ensure_ascii=False)}. Record only the newly completed "
        "control rows. The harness will merge them with the prior persisted rows and "
        "validate the original complete output contract."
    )
    return f"{base_context}\n\n{guidance}".strip()


def _direct_dispatch_key(manifest: JsonDict) -> str:
    return ":".join([
        str(manifest.get("planVersion") or 0),
        str(manifest.get("phaseId") or "direct_worker"),
        str(manifest.get("attempt") or 0),
    ])


def _reserve_direct_dispatch(
    agent: Any, phase: JsonDict, attempt: int,
) -> Tuple[Optional[JsonDict], JsonDict]:
    """Atomically reserve one direct attempt before it can create a worker."""
    state = load_task_state(agent.logger)
    manifest = _direct_dispatch_manifest(agent, phase, attempt)
    key = _direct_dispatch_key(manifest)
    ledger = state.setdefault("direct_dispatches", {})
    if not isinstance(ledger, dict):
        return {
            "status": "direct_dispatch_identity_invalid",
            "tool_was_executed": False,
            "error": "task_state.direct_dispatches must be an object",
        }, manifest
    existing = ledger.get(key)
    if isinstance(existing, dict):
        comparable = {name: existing.get(name) for name in manifest}
        if comparable != manifest:
            return {
                "status": "direct_dispatch_identity_conflict",
                "tool_was_executed": False,
                "dispatchIdentity": manifest,
                "existingDispatch": existing,
                "next_instruction": (
                    "The saved direct attempt belongs to a different plan or "
                    "contract. Do not reuse or spawn it automatically."
                ),
            }, manifest
        return {
            "status": "direct_dispatch_already_reserved",
            "tool_was_executed": False,
            "dispatchIdentity": manifest,
            "existingDispatch": existing,
            "next_instruction": (
                "This exact direct attempt was already reserved. Reattach its "
                "worker or reconcile the reservation; do not spawn a duplicate."
            ),
        }, manifest
    ledger[key] = {
        **manifest,
        "status": "reserved",
        "reservedAt": time.time(),
        "dispatchedBy": "runtime_direct_initial" if attempt == 1
        else "runtime_direct_continuation",
    }
    write_task_state(agent.logger, state)
    return None, manifest


def _update_direct_dispatch(agent: Any, manifest: JsonDict, **updates: Any) -> None:
    state = load_task_state(agent.logger)
    ledger = state.setdefault("direct_dispatches", {})
    if not isinstance(ledger, dict):
        return
    key = _direct_dispatch_key(manifest)
    record = ledger.get(key)
    if not isinstance(record, dict):
        return
    record.update({name: value for name, value in updates.items() if value is not None})
    write_task_state(agent.logger, state)


def _release_direct_dispatch(agent: Any, manifest: JsonDict) -> None:
    """A rejected spawn created no worker, so its logical attempt may be retried."""
    state = load_task_state(agent.logger)
    ledger = state.get("direct_dispatches")
    if not isinstance(ledger, dict):
        return
    ledger.pop(_direct_dispatch_key(manifest), None)
    write_task_state(agent.logger, state)


def _direct_page_crash_continuation_rejection(
    agent: Any, phase: JsonDict, attempt: int, status: str,
) -> Optional[JsonDict]:
    """Keep page-crash recovery narrow enough to preserve browser continuity."""
    if status != "page_crashed" or attempt <= 1:
        return None
    state = load_task_state(agent.logger)
    ledger = state.get("direct_dispatches")
    prior = (
        ledger.get(_direct_dispatch_key(_direct_dispatch_manifest(agent, phase, attempt - 1)))
        if isinstance(ledger, dict) else None
    )
    if not isinstance(prior, dict):
        return {
            "status": "direct_continuation_identity_missing",
            "tool_was_executed": False,
            "next_instruction": "The prior page-crash attempt has no durable dispatch identity.",
        }
    binding = prior.get("taskSessionBinding")
    # Dispatch records persist TaskSessionBinding.to_dict(), whose canonical
    # serialized distinction is bindingScope (requires_exact_page is only the
    # in-memory dataclass property). An exact page cannot be replaced after a
    # crash; hand the routing decision back to Lead before any new spawn.
    if isinstance(binding, dict) and binding.get("bindingScope") == "page":
        return {
            "status": "direct_continuation_requires_lead",
            "tool_was_executed": False,
            "dispatchIdentity": prior,
            "next_instruction": (
                "This task requires its exact prior page. A page crash cannot "
                "automatically switch to a new page; preserve the blocker for Lead review."
            ),
        }
    return None


def _direct_continuation_identity_rejection(
    agent: Any, phase: JsonDict, attempt: int,
) -> Optional[JsonDict]:
    """A continuation may advance only the exact prior direct contract."""
    if attempt <= 1:
        return None
    state = load_task_state(agent.logger)
    ledger = state.get("direct_dispatches")
    current = _direct_dispatch_manifest(agent, phase, attempt)
    prior_key = _direct_dispatch_key({**current, "attempt": attempt - 1})
    prior = ledger.get(prior_key) if isinstance(ledger, dict) else None
    if not isinstance(prior, dict):
        return {
            "status": "direct_continuation_identity_missing",
            "tool_was_executed": False,
            "next_instruction": "The prior direct attempt has no durable dispatch identity.",
        }
    identity_fields = ("planVersion", "planHash", "phaseId", "contractHash", "taskType")
    if any(prior.get(name) != current.get(name) for name in identity_fields):
        return {
            "status": "direct_continuation_identity_conflict",
            "tool_was_executed": False,
            "priorDispatch": prior,
            "dispatchIdentity": current,
            "next_instruction": (
                "The accepted plan, contract, task type or phase changed after "
                "the prior attempt. Do not automatically continue it."
            ),
        }
    binding = getattr(getattr(agent, "spawner", None), "_task_session_binding", None)
    current_binding = binding.to_dict() if hasattr(binding, "to_dict") else None
    prior_binding = prior.get("taskSessionBinding")
    if isinstance(current_binding, dict):
        if current_binding.get("state") == "stale":
            return {
                "status": "direct_continuation_session_stale",
                "tool_was_executed": False,
                "next_instruction": "The task session binding is stale; do not replace its Fleet automatically.",
            }
        if (
            isinstance(prior_binding, dict)
            and prior_binding.get("fleetId")
            and current_binding.get("fleetId")
            and prior_binding.get("fleetId") != current_binding.get("fleetId")
        ):
            return {
                "status": "direct_continuation_fleet_conflict",
                "tool_was_executed": False,
                "priorDispatch": prior,
                "next_instruction": "The task Fleet changed after the prior attempt.",
            }
    return None


def _direct_continuation_decision(
    result: Any,
    *,
    previous_result: Optional[JsonDict],
    attempt_number: int,
    max_attempts: int,
    continuation_receipt: Optional[JsonDict] = None,
) -> JsonDict:
    """Return a conservative, receipt-only continuation decision.

    This is deliberately a runtime safety policy rather than a business
    semantic verdict.  It never turns a challenge, validation contradiction or
    unknown outcome into an automatic retry.
    """
    if not isinstance(result, dict):
        return {"continue": False, "reason": "worker_result_missing"}
    status = str(result.get("status") or "unknown").strip().lower()
    challenge = result.get("challengeReceipt")
    unresolved_challenge = (
        isinstance(challenge, dict) and bool(challenge.get("unresolved"))
    )
    if unresolved_challenge or status in {
        "blocked_by_challenge", "hitl_required", "hitl_waiting", "hitl_timeout",
        "page_settled_after_hitl", "stale_pause_deadlock", "session_fleet_lost",
        "page_continuation_lost",
    }:
        return {"continue": False, "reason": "human_or_session_blocker"}
    if status not in {
        "partial", "step_budget_exhausted", "context_limit_exceeded",
        "incomplete", "page_crashed", "fleet_assignment_lost",
    }:
        return {"continue": False, "reason": "status_requires_lead_review"}
    if attempt_number >= max_attempts:
        return {"continue": False, "reason": "attempt_budget_reached"}
    if (
        isinstance(continuation_receipt, dict)
        and continuation_receipt.get("unitKind") == "required_controls"
        and continuation_receipt.get("coverage") == "row_validated"
        and isinstance(continuation_receipt.get("remainingUnitIds"), list)
        and not continuation_receipt["remainingUnitIds"]
    ):
        return {
            "continue": False,
            "reason": "no_remaining_enumerated_units",
        }
    current_rows = _direct_row_count(result)
    previous_rows = _direct_row_count(previous_result)
    signature = _direct_failure_signature(result)
    previous_signature = _direct_failure_signature(previous_result)
    repeated_no_progress = (
        previous_result is not None
        and current_rows <= previous_rows
        and signature == previous_signature
    )
    if repeated_no_progress:
        return {
            "continue": False,
            "reason": "repeated_no_progress_same_signature",
            "currentRows": current_rows,
            "previousRows": previous_rows,
            "failureSignature": list(signature),
        }
    return {
        "continue": True,
        "reason": "bounded_receipt_continuation",
        "currentRows": current_rows,
        "previousRows": previous_rows,
        "failureSignature": list(signature),
    }


async def _direct_finalize_from_worker(
    ctx: ToolContext,
    result: JsonDict,
    *,
    attempts: int,
    max_attempts: int,
    decision: Optional[JsonDict] = None,
) -> JsonDict:
    status = str(result.get("status") or "incomplete").strip().lower()
    phase_state = load_task_state(ctx.agent.logger)
    phase_states = phase_state.get("phases") if isinstance(phase_state, dict) else {}
    phase_state = phase_states.get("direct_worker") if isinstance(phase_states, dict) else {}
    validated_done = (
        isinstance(phase_state, dict)
        and str(phase_state.get("status") or "") == "validated_done"
    )
    if status == "done" and validated_done:
        final_status = "done"
    elif status == "partial" or _direct_row_count(result) > 0:
        final_status = "partial"
    else:
        final_status = "incomplete"
    answer = str(result.get("answer") or "").strip()
    if not answer:
        answer = json.dumps(
            {
                "outcome": final_status,
                "data": {},
                "evidence": result.get("artifacts") or [],
                "blockers": [result.get("reason") or result.get("error") or status],
                "next_steps": [],
            },
            ensure_ascii=False,
        )
    final_result = await _lead_final_answer(
        ToolContext(
            agent=ctx.agent,
            tool_call={"name": "final_answer", "id": "direct-final"},
            tool_input={
                "status": final_status,
                "answer": answer,
                "reason": (
                    "direct worker bounded continuation ended after "
                    f"{attempts}/{max_attempts} attempt(s)"
                    if final_status != "done" else ""
                ),
            },
            step=ctx.step,
        )
    )
    if not isinstance(final_result, dict):
        return {"status": "failed", "error": "direct finalization returned no receipt"}
    if final_result.get("tool_was_executed") is False:
        return final_result
    final_result["directExecution"] = {
        "mode": "direct_worker",
        "attempts": attempts,
        "maxAttempts": max_attempts,
        "workerId": result.get("workerId"),
        "workerStatus": status,
        "continuation": decision or {"continue": False, "reason": "completed"},
    }
    final_result["_terminate_lead"] = True
    return final_result


def _direct_live_worker_id(agent: Any) -> str:
    """Return the one live direct worker, if a prior invocation already owns it."""
    spawner = getattr(agent, "spawner", None)
    handles = getattr(spawner, "_handles", None)
    if not isinstance(handles, dict):
        return ""
    for handle in reversed(list(handles.values())):
        if (
            str(getattr(handle, "phase_id", "") or "") == "direct_worker"
            and not handle.async_task.done()
        ):
            return str(getattr(handle, "worker_id", "") or "").strip()
    return ""


async def _direct_attach_or_recover(ctx: ToolContext) -> Optional[JsonDict]:
    """Attach a replayed direct call to its existing phase without spawning."""
    agent = ctx.agent
    state = load_task_state(agent.logger)
    phases = state.get("phases") if isinstance(state, dict) else {}
    phase_state = phases.get("direct_worker") if isinstance(phases, dict) else {}
    status = str(phase_state.get("status") or "") if isinstance(phase_state, dict) else ""
    worker_id = _direct_live_worker_id(agent)
    if worker_id:
        waited = await agent.spawner.wait_browser_agents(
            worker_ids=[worker_id], mode="all",
        )
        completed = waited.get("completed") if isinstance(waited, dict) else None
        result = completed[-1] if isinstance(completed, list) and completed else None
        if not isinstance(result, dict):
            return waited if isinstance(waited, dict) else {
                "status": "failed", "error": "attached direct worker produced no result",
            }
        return {
            "_direct_attached_result": result,
            "_direct_attached_worker_id": worker_id,
        }
    if status == "running":
        return {
            "status": "direct_worker_recovery_required",
            "phaseId": "direct_worker",
            "tool_was_executed": False,
            "next_instruction": (
                "The direct phase is recorded as running but has no live local "
                "worker handle. Reconcile the prior worker before any new spawn; "
                "do not create a duplicate worker."
            ),
        }
    if status == "validated_done":
        return {
            "status": "direct_worker_finalization_required",
            "phaseId": "direct_worker",
            "tool_was_executed": False,
            "next_instruction": (
                "The direct phase is already validated_done. Use its stored "
                "completion receipt to finalize; do not replan or spawn it again."
            ),
        }
    return None


async def _run_direct_worker(ctx: ToolContext) -> JsonDict:
    """Run one approved direct phase, retrying only structured continuations."""
    agent = ctx.agent
    phase = find_phase(agent.task_plan, "direct_worker") if agent.task_plan else None
    if not isinstance(phase, dict):
        return {
            "status": "failed",
            "error": "compiled direct phase is missing",
            "tool_was_executed": False,
        }
    max_attempts = optional_int(phase.get("max_attempts"), 3) or 3
    max_attempts = max(1, min(max_attempts, 8))
    attached_result: Optional[JsonDict] = None
    attached_worker_id = ""
    spawn_input: JsonDict = {
        "phase_id": "direct_worker",
        "task": str(phase.get("worker_task") or ""),
        "context": str(phase.get("context") or ""),
        "name": "direct_worker",
    }
    base_context = str(spawn_input["context"])
    previous: Optional[JsonDict] = None
    last_decision: Optional[JsonDict] = None
    attached_or_recovery = await _direct_attach_or_recover(ctx)
    if isinstance(attached_or_recovery, dict) and isinstance(
        attached_or_recovery.get("_direct_attached_result"), dict
    ):
        attached_result = attached_or_recovery["_direct_attached_result"]
        attached_worker_id = str(
            attached_or_recovery.get("_direct_attached_worker_id") or ""
        )
    elif attached_or_recovery is not None:
        return attached_or_recovery
    start_attempt = 1
    if attached_result is not None:
        state = load_task_state(agent.logger)
        phase_states = state.get("phases") if isinstance(state, dict) else {}
        phase_record = (
            phase_states.get("direct_worker")
            if isinstance(phase_states, dict) else {}
        )
        completed_attempts = len(phase_record.get("attempts") or []) \
            if isinstance(phase_record, dict) else 1
        completed_attempts = max(1, completed_attempts)
        attached_status = str(attached_result.get("status") or "unknown").lower()
        if attached_status == "done" and isinstance(phase_record, dict) \
                and phase_record.get("status") == "validated_done":
            return await _direct_finalize_from_worker(
                ctx, attached_result, attempts=completed_attempts,
                max_attempts=max_attempts,
            )
        attached_identity = _direct_dispatch_manifest(agent, phase, completed_attempts)
        attached_receipt = _direct_continuation_receipt(
            phase, attached_result, attached_identity,
        )
        _update_direct_dispatch(
            agent,
            attached_identity,
            status="completed",
            workerStatus=attached_status,
            workerId=attached_worker_id,
            continuationReceipt=attached_receipt,
        )
        attached_decision = _direct_continuation_decision(
            attached_result,
            previous_result=None,
            attempt_number=completed_attempts,
            max_attempts=max_attempts,
            continuation_receipt=attached_receipt,
        )
        if not attached_decision.get("continue"):
            if attached_status in {"partial", "step_budget_exhausted", "context_limit_exceeded"}:
                return await _direct_finalize_from_worker(
                    ctx, attached_result, attempts=completed_attempts,
                    max_attempts=max_attempts, decision=attached_decision,
                )
            return {
                **attached_result,
                "directExecution": {
                    "mode": "direct_worker",
                    "attachedExistingWorker": True,
                    "workerId": attached_worker_id,
                    "attempts": completed_attempts,
                    "maxAttempts": max_attempts,
                    "continuation": attached_decision,
                },
            }
        previous = attached_result
        last_decision = attached_decision
        spawn_input["context"] = _direct_continuation_context(
            base_context, attached_receipt,
        )
        spawn_input["reuse_from_worker_id"] = attached_worker_id
        if attached_status == "page_crashed":
            spawn_input["reuse_scope"] = "connection"
            spawn_input["page_policy"] = "new"
        elif attached_status == "fleet_assignment_lost":
            spawn_input.pop("reuse_from_worker_id", None)
        else:
            spawn_input["reuse_scope"] = "page"
            spawn_input["page_policy"] = "existing"
        start_attempt = completed_attempts + 1
    for attempt_number in range(start_attempt, max_attempts + 1):
        identity_rejection = _direct_continuation_identity_rejection(
            agent, phase, attempt_number,
        )
        if identity_rejection is not None:
            return identity_rejection
        page_crash_rejection = _direct_page_crash_continuation_rejection(
            agent, phase, attempt_number,
            str(previous.get("status") or "").lower() if isinstance(previous, dict) else "",
        )
        if page_crash_rejection is not None:
            return page_crash_rejection
        reservation_rejection, dispatch_identity = _reserve_direct_dispatch(
            agent, phase, attempt_number,
        )
        if reservation_rejection is not None:
            return reservation_rejection
        dispatch_origin = (
            "runtime_direct_initial" if attempt_number == 1
            else "runtime_direct_continuation"
        )
        agent.logger.write("lead.direct_worker.attempt", {
            "attempt": attempt_number,
            "maxAttempts": max_attempts,
            "phaseId": "direct_worker",
            "dispatchIdentity": dispatch_identity,
            "dispatchedBy": dispatch_origin,
            "reuseFromWorkerId": spawn_input.get("reuse_from_worker_id"),
            "reuseScope": spawn_input.get("reuse_scope"),
            "pagePolicy": spawn_input.get("page_policy"),
        })
        spawned = await _lead_spawn_browser_agent(
            ToolContext(
                agent=agent,
                tool_call={"name": "spawn_browser_agent", "id": f"direct-spawn-{attempt_number}"},
                tool_input={
                    **spawn_input,
                    "_runtime_dispatch_origin": dispatch_origin,
                    "_runtime_dispatch_identity": dispatch_identity,
                },
                step=ctx.step,
            )
        )
        if not isinstance(spawned, dict) or spawned.get("status") != "running":
            _release_direct_dispatch(agent, dispatch_identity)
            return spawned if isinstance(spawned, dict) else {
                "status": "failed", "error": "direct worker spawn returned no receipt",
            }
        worker_id = str(spawned.get("workerId") or "").strip()
        _update_direct_dispatch(
            agent, dispatch_identity,
            status="running",
            workerId=worker_id,
            taskSessionBinding=spawned.get("taskSessionBinding"),
            fleetAssignment=spawned.get("fleetAssignment"),
        )
        waited = await agent.spawner.wait_browser_agents(
            worker_ids=[worker_id] if worker_id else None,
            mode="all",
        )
        completed = waited.get("completed") if isinstance(waited, dict) else None
        result = completed[-1] if isinstance(completed, list) and completed else None
        if not isinstance(result, dict):
            _update_direct_dispatch(agent, dispatch_identity, status="wait_incomplete")
            return waited if isinstance(waited, dict) else {
                "status": "failed", "error": "direct worker produced no result",
            }
        status = str(result.get("status") or "unknown").strip().lower()
        continuation_receipt = _direct_continuation_receipt(
            phase, result, dispatch_identity,
        )
        _update_direct_dispatch(
            agent, dispatch_identity,
            status="completed",
            workerStatus=status,
            workerId=str(result.get("workerId") or worker_id),
            continuationReceipt=continuation_receipt,
        )
        phase_state = load_task_state(agent.logger)
        phase_states = phase_state.get("phases") if isinstance(phase_state, dict) else {}
        phase_record = phase_states.get("direct_worker") if isinstance(phase_states, dict) else {}
        if status == "done" and isinstance(phase_record, dict) and phase_record.get("status") == "validated_done":
            return await _direct_finalize_from_worker(
                ctx, result, attempts=attempt_number, max_attempts=max_attempts,
            )
        decision = _direct_continuation_decision(
            result,
            previous_result=previous,
            attempt_number=attempt_number,
            max_attempts=max_attempts,
            continuation_receipt=continuation_receipt,
        )
        last_decision = decision
        agent.logger.write("lead.direct_worker.continuation", {
            "attempt": attempt_number,
            "workerId": result.get("workerId"),
            "status": status,
            "enumeratedUnitCoverage": continuation_receipt.get("coverage"),
            "remainingUnitCount": len(
                continuation_receipt.get("remainingUnitIds") or []
            ),
            **decision,
        })
        if not decision.get("continue"):
            # A bounded partial can be delivered safely.  A semantic or
            # routing uncertainty remains a Lead decision and is returned as a
            # normal non-terminal receipt for the model to inspect.
            if status in {"partial", "step_budget_exhausted", "context_limit_exceeded"}:
                return await _direct_finalize_from_worker(
                    ctx, result, attempts=attempt_number,
                    max_attempts=max_attempts, decision=decision,
                )
            return {
                **result,
                "directExecution": {
                    "mode": "direct_worker",
                    "attempts": attempt_number,
                    "maxAttempts": max_attempts,
                    "continuation": decision,
                },
            }
        previous = result
        spawn_input["context"] = _direct_continuation_context(
            base_context, continuation_receipt,
        )
        spawn_input["reuse_from_worker_id"] = worker_id
        if status == "page_crashed":
            spawn_input["reuse_scope"] = "connection"
            spawn_input["page_policy"] = "new"
        elif status == "fleet_assignment_lost":
            spawn_input.pop("reuse_from_worker_id", None)
        else:
            spawn_input["reuse_scope"] = "page"
            spawn_input["page_policy"] = "existing"
    if previous is not None:
        return await _direct_finalize_from_worker(
            ctx, previous, attempts=max_attempts,
            max_attempts=max_attempts, decision=last_decision,
        )
    return {"status": "failed", "error": "direct worker ended without attempts"}


@LEAD_TOOLS.register(
    name="emit_direct_task_plan",
    description=(
        "Submit a compact one-worker plan. The harness compiles it into one "
        "canonical phase, applies the same mechanical validation, independent "
        "PlanValidator review and operator approval as emit_task_plan, then "
        "dispatches/waits and performs bounded receipt-based continuation. "
        "Choose this only for one coherent task with no cross-worker merge or "
        "parallel coordination."
    ),
    input_schema=_direct_task_plan_schema,
    loop_guard=False,
)
async def _lead_emit_direct_task_plan(ctx: ToolContext) -> JsonDict:
    plan, error = _compile_direct_task_plan(ctx.tool_input)
    if plan is None:
        return error or {"status": "failed", "error": "invalid direct plan"}
    current = getattr(ctx.agent, "task_plan", None)
    if (
        isinstance(current, dict)
        and current.get("execution_mode") == "direct_worker"
        and ctx.agent.raw_plan_candidate_hash(current)
        == ctx.agent.raw_plan_candidate_hash(plan)
    ):
        approval_rejection = ctx.agent.task_plan_user_approval_rejection()
        if approval_rejection is not None:
            return approval_rejection
        return await _run_direct_worker(ctx)
    accepted = await _lead_emit_task_plan(
        ToolContext(
            agent=ctx.agent,
            tool_call=ctx.tool_call,
            tool_input={"plan": plan},
            step=ctx.step,
        )
    )
    if not isinstance(accepted, dict) or accepted.get("status") != "done":
        return accepted
    return await _run_direct_worker(ctx)


def _repair_task_plan_schema(_: Any = None) -> JsonDict:
    """Schema for a small edit against the latest rejected plan candidate."""
    return {
        "type": "object",
        "description": (
            "Repair the latest mechanically rejected emit_task_plan candidate "
            "without regenerating its full plan JSON. Paths are RFC 6901 JSON "
            "Pointers relative to the plan object, for example "
            "'/phases/0/expected_artifact/requiredControls'. set replaces an "
            "existing value; add creates one missing object property whose parent "
            "already exists; remove deletes an existing object property, never "
            "an array element. For remove, provide value:null because "
            "conservative tool schemas do not express op-specific required "
            "fields."
        ),
        "properties": {
            "baseCandidateHash": {
                "type": "string",
                "minLength": 1,
                "description": (
                    "candidateHash from the immediately preceding mechanical "
                    "rejection; prevents applying an edit to stale plan input."
                ),
            },
            "operations": {
                "type": "array",
                "minItems": 1,
                "maxItems": 16,
                "items": {
                    "type": "object",
                    "properties": {
                        "op": {"type": "string", "enum": ["add", "set", "remove"]},
                        "path": {"type": "string", "minLength": 2},
                        "value": {
                            "description": (
                                "Value for add/set; null placeholder for remove."
                            ),
                        },
                    },
                    "required": ["op", "path", "value"],
                    "additionalProperties": False,
                },
            },
        },
        "required": ["baseCandidateHash", "operations"],
        "additionalProperties": False,
    }


def _json_pointer_parts(path: Any) -> Tuple[Optional[List[str]], Optional[str]]:
    """Parse a deliberately small, non-root JSON Pointer for plan repair."""
    if not isinstance(path, str) or not path.startswith("/") or path == "/":
        return None, "path must be a non-root RFC 6901 JSON Pointer"
    parts: List[str] = []
    for raw_part in path[1:].split("/"):
        if not raw_part:
            return None, "path must not contain an empty property segment"
        decoded: List[str] = []
        index = 0
        while index < len(raw_part):
            char = raw_part[index]
            if char != "~":
                decoded.append(char)
                index += 1
                continue
            if index + 1 >= len(raw_part) or raw_part[index + 1] not in {"0", "1"}:
                return None, "path has an invalid RFC 6901 escape"
            decoded.append("~" if raw_part[index + 1] == "0" else "/")
            index += 2
        parts.append("".join(decoded))
    return parts, None


def _repair_list_index(token: str, size: int) -> Optional[int]:
    if not token.isdigit():
        return None
    index = int(token)
    return index if 0 <= index < size else None


def _apply_task_plan_repair(
    candidate: JsonDict,
    operations: Any,
) -> Tuple[Optional[JsonDict], List[str]]:
    """Apply bounded object-property edits without synthesizing containers."""
    if not isinstance(operations, list) or not operations:
        return None, ["operations must be a non-empty array"]
    repaired = copy.deepcopy(candidate)
    errors: List[str] = []
    seen_paths = set()
    for index, operation in enumerate(operations):
        where = f"operations[{index}]"
        if not isinstance(operation, dict):
            errors.append(f"{where} must be an object")
            continue
        op = str(operation.get("op") or "").strip()
        if op not in {"add", "set", "remove"}:
            errors.append(f"{where}.op must be 'add', 'set', or 'remove'")
            continue
        parts, path_error = _json_pointer_parts(operation.get("path"))
        if path_error is not None or parts is None:
            errors.append(f"{where}.path {path_error or 'is invalid'}")
            continue
        path = str(operation["path"])
        if path in seen_paths:
            errors.append(f"{where}.path duplicates a prior repair operation")
            continue
        seen_paths.add(path)

        parent: Any = repaired
        invalid_parent = False
        for part in parts[:-1]:
            if isinstance(parent, dict):
                if part not in parent:
                    errors.append(f"{where}.path does not exist at {part!r}")
                    invalid_parent = True
                    break
                parent = parent[part]
            elif isinstance(parent, list):
                list_index = _repair_list_index(part, len(parent))
                if list_index is None:
                    errors.append(f"{where}.path has invalid list index {part!r}")
                    invalid_parent = True
                    break
                parent = parent[list_index]
            else:
                errors.append(f"{where}.path crosses a scalar value at {part!r}")
                invalid_parent = True
                break
        if invalid_parent:
            continue

        leaf = parts[-1]
        if isinstance(parent, dict):
            if op == "add":
                if leaf in parent:
                    errors.append(
                        f"{where}.path already exists; use set to replace it"
                    )
                    continue
                parent[leaf] = copy.deepcopy(operation.get("value"))
                continue
            if leaf not in parent:
                errors.append(
                    f"{where}.path must reference an existing value; emit a "
                    "complete revised plan to add new structure"
                )
                continue
            if op == "remove":
                del parent[leaf]
            else:
                parent[leaf] = copy.deepcopy(operation.get("value"))
        elif isinstance(parent, list):
            if op == "add":
                errors.append(
                    f"{where}.path cannot add an array element; array insertion"
                    " is structural and requires a materially changed complete plan"
                )
                continue
            list_index = _repair_list_index(leaf, len(parent))
            if list_index is None:
                errors.append(f"{where}.path has invalid list index {leaf!r}")
                continue
            if op == "remove":
                errors.append(
                    f"{where}.path cannot remove an array element; array "
                    "deletion/reordering is structural and requires a materially "
                    "changed complete plan"
                )
            else:
                parent[list_index] = copy.deepcopy(operation.get("value"))
        else:
            errors.append(f"{where}.path parent is not an object or array")
    return (None, errors) if errors else (repaired, [])


def _auto_applicable_repairs(repair_issues: Any) -> Tuple[List[JsonDict], List[str]]:
    """Collect the repairs the controller may apply without asking the model.

    Auto-application is opt-in per option (``autoApplicable``) because the
    validator is the only layer that still knows whether an edit picks between
    two readings of the deliverable or removes something inert. Inferring it
    here from the operation list's shape would silently enrol every future
    single-option repair, including one that rewrites a semantic field.

    Issues without such an option are simply skipped: their errors survive into
    the rejection, so the model still sees them.

    Operations and their originating codes come out of the same pass. Reading
    the codes off the full issue list instead made a mixed candidate claim the
    controller had repaired an issue it had only reported.
    """
    if not isinstance(repair_issues, list):
        return [], []
    operations: List[JsonDict] = []
    codes: List[str] = []
    for issue in repair_issues:
        if not isinstance(issue, dict):
            continue
        options = issue.get("repairOptions")
        if not isinstance(options, list) or len(options) != 1:
            continue
        option = options[0]
        if not isinstance(option, dict):
            continue
        if option.get("autoApplicable") is not True:
            continue
        if option.get("requiresCompletePlan"):
            continue
        raw_operations = option.get("operations")
        if not isinstance(raw_operations, list) or not raw_operations:
            continue
        applied: List[JsonDict] = []
        for operation in raw_operations:
            if not isinstance(operation, dict):
                return [], []
            if str(operation.get("op") or "") not in {"set", "remove"}:
                return [], []
            if not str(operation.get("path") or "").strip():
                return [], []
            applied.append(dict(operation))
        operations.extend(applied)
        code = str(issue.get("code") or "").strip()
        if code and code not in codes:
            codes.append(code)
    return operations, sorted(codes)


def _extend_task_plan_schema(_: Any = None) -> JsonDict:
    plan_schema = _emit_task_plan_schema()["properties"]["plan"]["properties"]
    return {
        "type": "object",
        "properties": {
            "new_phases": {
                "type": "array",
                "minItems": 1,
                "description": (
                    "ONLY the phases being added. The accepted phases are"
                    " carried forward by the harness and must not appear here."
                    " Each new phase follows the same shape as an emit_task_plan"
                    " phase and needs an id no accepted phase already uses."
                    " depends_on may reference accepted phase ids when a new"
                    " phase has to wait for one of them or read its artifact."
                    " Every phase here is browser work: do not add one whose job"
                    " is to merge or reshape artifacts that already exist."
                ),
                "items": plan_schema["phases"]["items"],
            },
            "replan_reason": {
                "type": "string",
                "minLength": 1,
                "description": (
                    "Why the user's resume instruction authorizes these phases."
                ),
            },
        },
        "required": ["new_phases", "replan_reason"],
        "additionalProperties": False,
    }


def _spawn_browser_agent_schema(_: Any = None) -> JsonDict:
    return {
        "type": "object",
        "properties": {
            "name": {
                **_nullable("string"),
                "description": "BrowserAgent name; pass null to auto-name.",
            },
            "phase_id": {
                "type": "string",
                "minLength": 1,
                "description": (
                    "The accepted task_plan phase id this worker executes."
                    " Required: naming it is how a plan with several startable"
                    " phases gets them spawned in parallel instead of one"
                    " guessed phase at a time."
                ),
            },
            "task": {"type": "string"},
            "context": {
                "type": "string",
                "description": (
                    "Subtask context; pass an empty string when none. Include artifact paths"
                    " or prior result fields that the worker may use as dynamic-param sources."
                ),
            },
            "result_contract": {
                "type": "string",
                "description": "Structure / fields you expect the BrowserAgent to put in `answer`; pass an empty string when there are no extra requirements.",
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
                    " previous page. For a detail cohort discovered on a live"
                    " listing, point this to the source-list worker."
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
                    " Never put a Fleet UUID or UUID prefix here; use fleet_id"
                    " for an existing Fleet."
                ),
            },
            "fleet_id": {
                **_nullable("string"),
                "description": (
                    "Existing Fleet UUID or unique UUID prefix. The harness"
                    " resolves it only against authoritative Fleet inventory;"
                    " no match or multiple matches fail closed and never"
                    " create a replacement Fleet. Mutually exclusive with"
                    " session_key and needs_isolated_session."
                ),
            },
            "page_policy": {
                "type": ["string", "null"],
                "enum": [*sorted(VALID_PAGE_POLICIES), None],
                "description": (
                    "Use new for a fresh page in assignedFleetId. existing is"
                    " valid only with reuse_scope=page. Use existing with the"
                    " source-list worker when details should be entered by"
                    " clicking freshly rebound source cards."
                ),
            },
            "worker_contract": {
                "type": "object",
                "additionalProperties": True,
                "properties": {
                    "task_type": {
                        "type": "string",
                        "enum": sorted(VALID_TASK_TYPES),
                        "description": (
                            "Optional consistency assertion only; when present"
                            " it MUST equal phase.task_type. Method access is"
                            " always controlled by the reviewed phase.task_type;"
                            " re-emit the plan to change it."
                        ),
                    },
                    "needs_isolated_session": {
                        "type": "boolean",
                        "description": (
                            "Request coordinator creation of a distinct fleet"
                            " because cookies/storage/proxy identity must not be"
                            " shared with the slot default. The resulting fleet"
                            " never becomes the generic slot default."
                            " SET THIS TRUE when the task itself asks for a"
                            " fresh fleet or browser profile, for an environment"
                            " that must not inherit an existing login, or for a"
                            " different account than a previous run — in any"
                            " language the task is written in. This field is the"
                            " only channel that request travels through; stating"
                            " it only in objective or worker_task prose leaves"
                            " the routing layer unable to honour it. Needing a"
                            " new page, tab, worker or slot is NOT such a"
                            " request: those share the task fleet by design, and"
                            " an isolated fleet holds a task fleet budget slot"
                            " the harness never reclaims."
                        ),
                    },
                    "fleet_id": {
                        "type": "string",
                        "description": (
                            "Existing Fleet UUID or unique UUID prefix. Use"
                            " session_key instead only for a new named session."
                        ),
                    },
                    "auth_verification": _auth_verification_schema(),
                    "content_completeness": _content_completeness_schema(),
                },
                "description": (
                    "Contract override; pass {} when the phase contract is enough."
                    " The harness merges it with the"
                    " phase's expected_artifact, validators, allowed_methods,"
                    " forbidden_methods, max_surface_attempts, and stop_condition."
                    " Optional: set skill_id (a known reusable skill) +"
                    " skill_variables (its required inputs, e.g. detailUrl) to run"
                    " that skill's fast path. If spawn_browser_agent returns"
                    " skill_selection_required, read candidate skillMarkdown and"
                    " retry with skill_id+skill_variables, or decline with"
                    " skill_selection={\"use_skill\":false,\"reason\":\"...\"}."
                ),
            },
        },
        "required": ["phase_id"],
        "additionalProperties": False,
    }


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
    return {"type": "object", "properties": {}, "required": [], "additionalProperties": False}


def _local_fs_search_schema(_: Any = None) -> JsonDict:
    return {
        "type": "object",
        "properties": {
            "pattern": {
                "type": "string",
                "default": "",
                "description": "Regex grep; pass an empty string to list matches by glob / event_type only.",
            },
            "glob": {
                "type": "string",
                "default": "**/*",
                "description": "Glob relative to the current task worktree, e.g. traces/*.jsonl or observations/*.json.",
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
        prepared_call, normalized_task_types = _normalize_lead_task_type_aliases(
            prepared_call
        )
        prepared_fields.extend(normalized_task_types)
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
            effective_call, normalized_task_types = _normalize_lead_task_type_aliases(
                effective_call
            )
            prepared_fields.extend(normalized_task_types)
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
    name="emit_task_plan",
    description=(
        "Submit the structured v1 task plan before spawning any worker."
        " The harness validates and persists task_plan.json and task_state.json."
    ),
    input_schema=_emit_task_plan_schema,
    loop_guard=False,
)
async def _lead_emit_task_plan(ctx: ToolContext) -> JsonDict:
    raw_plan = ctx.tool_input.get("plan")
    # Replacing an accepted plan without a stated reason is decided
    # mechanically, so it is answered before the PlanValidator runs. Asking it
    # afterwards let the reason error mask the candidate's real schema errors:
    # in task 294889c8 the Lead re-sent the same plan nine times, never seeing
    # that its detail_save phase had a requiredControls/exact_rows conflict.
    rejection = ctx.agent.replan_reason_rejection(raw_plan)
    if rejection is not None:
        ctx.agent.logger.write("task_plan.rejected", rejection)
        return rejection
    unchanged = ctx.agent.unchanged_plan_candidate_rejection(raw_plan)
    if unchanged is not None:
        ctx.agent.logger.write("task_plan.rejected", unchanged)
        return unchanged
    review = await ctx.agent.review_task_plan_candidate(raw_plan)
    auto_repair: Optional[JsonDict] = None
    if review.get("status") == "mechanical_invalid":
        # One bounded controller repair, never a loop: the repaired candidate is
        # revalidated once and whatever it still gets wrong is reported as an
        # ordinary rejection. Task eb939033 rejected the same requiredControls
        # four times while carrying the exact remove operation in every reply,
        # and spent the Lead run doing it. A deterministic edit the harness can
        # name is not a decision worth a round trip.
        operations, applied_codes = _auto_applicable_repairs(
            review.get("repairIssues")
        )
        repaired = None
        if operations:
            repaired, _ = _apply_task_plan_repair(raw_plan, operations)
        if repaired is not None:
            auto_repair = {
                "originalCandidateHash": ctx.agent.raw_plan_candidate_hash(raw_plan),
                "operations": operations,
                "appliedIssueCodes": applied_codes,
            }
            raw_plan = repaired
            review = await ctx.agent.review_task_plan_candidate(raw_plan)
            auto_repair["repairedCandidateHash"] = (
                ctx.agent.raw_plan_candidate_hash(raw_plan)
            )
            auto_repair["resolvedAllMechanicalErrors"] = (
                review.get("status") != "mechanical_invalid"
            )
            ctx.agent.logger.write("task_plan.auto_repaired", auto_repair)
    if review.get("status") == "mechanical_invalid":
        # The base for any follow-up repair is the repaired candidate, so a
        # manual fix cannot reintroduce a field the controller just removed.
        result = ctx.agent.plan_schema_rejection(
            review.get("errors"),
            raw_plan=raw_plan,
            repair_issues=review.get("repairIssues"),
        )
        if auto_repair is not None:
            result["autoRepaired"] = auto_repair
        ctx.agent.logger.write("task_plan.rejected", result)
        return result
    if review.get("status") == "rejected":
        result = {
            "status": "failed",
            "error": "independent PlanValidator rejected the candidate plan",
            "planValidator": review,
            "next_instruction": (
                "Keep the currently accepted plan unchanged. Correct the"
                " semantic findings and emit one complete revised plan."
            ),
        }
        ctx.agent.logger.write("task_plan.rejected", result)
        return result
    if (
        review.get("status") == "error"
        and review.get("errorKind") == "verdict_invalid"
    ):
        result = {
            "status": "failed",
            "error": "independent PlanValidator returned an invalid verdict",
            "planValidator": review,
            "next_instruction": (
                "Keep the candidate plan unchanged. The reviewer made a"
                " self-contradictory or out-of-catalog decision; do not"
                " rewrite the plan to repair the reviewer protocol."
            ),
        }
        ctx.agent.logger.write("task_plan.rejected", result)
        return result
    accepted = await ctx.agent.approve_and_accept_task_plan(
        raw_plan,
        plan_validator_review=(
            review
            if review.get("status")
            in {"approved", "operational_continuation", "error"}
            else None
        ),
    )
    if auto_repair is not None and isinstance(accepted, dict):
        accepted["autoRepaired"] = auto_repair
    return accepted


@LEAD_TOOLS.register(
    name="approve_current_task_plan",
    description=(
        "Display the existing accepted plan for operator approval on a resumed"
        " task. It never rewrites phases, contracts, artifacts, or state other"
        " than the approval receipt."
    ),
    input_schema={
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    },
    loop_guard=False,
)
async def _lead_approve_current_task_plan(ctx: ToolContext) -> JsonDict:
    return await ctx.agent.approve_current_task_plan()


@LEAD_TOOLS.register(
    name="begin_task_plan_draft",
    description=(
        "Start an inert task-plan draft for a large plan. Add complete phase"
        " chunks with append_task_plan_draft, then submit once for whole-plan"
        " validation, independent review, and operator approval."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "draft_id": {"type": "string", "minLength": 1},
            "plan": {
                "type": "object",
                "description": (
                    "Plan-level fields only: goal is required; task_type,"
                    " output_contracts, pacing, and replan metadata are optional."
                    " Do not include phases here."
                ),
                "properties": {
                    "goal": {"type": "string", "minLength": 1},
                    "task_type": {"type": "string", "enum": sorted(VALID_TASK_TYPES)},
                    "output_contracts": {"type": "object"},
                    "pacing": {"type": "object"},
                    "replan_reason": {"type": "string"},
                    "replan_checkpoint_id": {"type": "string"},
                    "replan_checkpoint_ids": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["goal"],
                "additionalProperties": False,
            },
        },
        "required": ["draft_id", "plan"],
        "additionalProperties": False,
    },
    loop_guard=False,
)
async def _lead_begin_task_plan_draft(ctx: ToolContext) -> JsonDict:
    return ctx.agent.begin_task_plan_draft(
        str(ctx.tool_input.get("draft_id") or ""),
        ctx.tool_input.get("plan"),
    )


@LEAD_TOOLS.register(
    name="append_task_plan_draft",
    description=(
        "Append one complete, small group of phases to an inert plan draft."
        " This does not validate, approve, or execute the draft."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "draft_id": {"type": "string", "minLength": 1},
            "phases": {
                "type": "array", "minItems": 1, "maxItems": 8,
                "items": {"type": "object"},
            },
        },
        "required": ["draft_id", "phases"],
        "additionalProperties": False,
    },
    loop_guard=False,
)
async def _lead_append_task_plan_draft(ctx: ToolContext) -> JsonDict:
    return ctx.agent.append_task_plan_draft(
        str(ctx.tool_input.get("draft_id") or ""),
        ctx.tool_input.get("phases"),
    )


@LEAD_TOOLS.register(
    name="submit_task_plan_draft",
    description=(
        "Submit the complete accumulated draft. This is equivalent to"
        " emit_task_plan for validation, independent review, user approval,"
        " and acceptance; only an accepted draft may dispatch workers."
    ),
    input_schema={
        "type": "object",
        "properties": {"draft_id": {"type": "string", "minLength": 1}},
        "required": ["draft_id"],
        "additionalProperties": False,
    },
    loop_guard=False,
)
async def _lead_submit_task_plan_draft(ctx: ToolContext) -> JsonDict:
    draft_id = str(ctx.tool_input.get("draft_id") or "")
    plan = ctx.agent.task_plan_draft(draft_id)
    if plan is None:
        return {
            "status": "failed",
            "error": "task-plan draft is unavailable",
            "draftId": draft_id or None,
        }
    result = await _lead_emit_task_plan(
        ToolContext(
            agent=ctx.agent,
            tool_call=ctx.tool_call,
            tool_input={"plan": plan},
            step=ctx.step,
        )
    )
    if isinstance(result, dict) and result.get("status") == "done":
        ctx.agent.discard_task_plan_draft(draft_id)
    return result


@LEAD_TOOLS.register(
    name="repair_task_plan",
    description=(
        "Apply small add/set/remove JSON-Pointer edits to the latest mechanically "
        "rejected task-plan candidate, then validate and accept the repaired "
        "complete plan. Available after ANY mechanical rejection — use the "
        "candidateHash that rejection returned; never guess a baseCandidateHash "
        "or use it for semantic review findings."
    ),
    input_schema=_repair_task_plan_schema,
    loop_guard=False,
)
async def _lead_repair_task_plan(ctx: ToolContext) -> JsonDict:
    base_hash = str(ctx.tool_input.get("baseCandidateHash") or "").strip()
    candidate = ctx.agent.last_mechanical_plan_candidate(base_hash)
    if candidate is None:
        return {
            "status": "failed",
            "error": "mechanically rejected plan candidate is unavailable",
            "errorCode": "task_plan_repair_base_unavailable",
            "candidateHash": base_hash or None,
            "next_instruction": (
                "Use the candidateHash from the latest task_plan_schema_invalid "
                "or task_plan_candidate_unchanged result. If the candidate has "
                "changed since then, emit one complete revised plan instead."
            ),
        }
    repaired, patch_errors = _apply_task_plan_repair(
        candidate,
        ctx.tool_input.get("operations"),
    )
    if repaired is None:
        return {
            "status": "failed",
            "error": "task_plan repair operations are invalid",
            "errorCode": "task_plan_repair_invalid_operations",
            "errors": patch_errors,
            "candidateHash": base_hash,
            "next_instruction": (
                "Choose one complete repairOptions entry from repairIssues when "
                "present; mustChangePaths is only a direct-field summary. set only "
                "replaces an existing value; add creates one missing object "
                "property under an existing object; remove deletes an existing "
                "object property, never an array element."
            ),
        }
    rejection = ctx.agent.replan_reason_rejection(repaired)
    if rejection is not None:
        ctx.agent.logger.write("task_plan.rejected", rejection)
        return rejection
    unchanged = ctx.agent.unchanged_plan_candidate_rejection(repaired)
    if unchanged is not None:
        ctx.agent.logger.write("task_plan.rejected", unchanged)
        return unchanged
    review = await ctx.agent.review_task_plan_candidate(repaired)
    if review.get("status") == "mechanical_invalid":
        result = ctx.agent.plan_schema_rejection(
            review.get("errors"),
            raw_plan=repaired,
            repair_issues=review.get("repairIssues"),
        )
        ctx.agent.logger.write("task_plan.rejected", result)
        return result
    if review.get("status") == "rejected":
        result = {
            "status": "failed",
            "error": "independent PlanValidator rejected the repaired candidate",
            "planValidator": review,
            "next_instruction": (
                "Keep the currently accepted plan unchanged. Correct the semantic "
                "findings and emit one complete revised plan."
            ),
        }
        ctx.agent.logger.write("task_plan.rejected", result)
        return result
    if (
        review.get("status") == "error"
        and review.get("errorKind") == "verdict_invalid"
    ):
        result = {
            "status": "failed",
            "error": "independent PlanValidator returned an invalid verdict",
            "planValidator": review,
            "next_instruction": (
                "Keep the repaired candidate unchanged. The reviewer made a"
                " self-contradictory or out-of-catalog decision; do not"
                " rewrite the plan to repair the reviewer protocol."
            ),
        }
        ctx.agent.logger.write("task_plan.rejected", result)
        return result
    accepted = await ctx.agent.approve_and_accept_task_plan(
        repaired,
        plan_validator_review=(
            review
            if review.get("status")
            in {"approved", "operational_continuation", "error"}
            else None
        ),
    )
    return accepted


@LEAD_TOOLS.register(
    name="resume_keep_plan",
    description=(
        "Acknowledge that the user's resume instruction changes execution"
        " guidance only and does not change the accepted plan's sources,"
        " artifact schema, validators, phases, or dependencies. Available only"
        " while a resumed run is waiting for instruction review."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "reason": {"type": "string", "minLength": 1},
        },
        "required": ["reason"],
        "additionalProperties": False,
    },
    loop_guard=False,
)
async def _lead_resume_keep_plan(ctx: ToolContext) -> JsonDict:
    agent = ctx.agent
    resume = getattr(agent, "resume", None)
    reason = str(ctx.tool_input.get("reason") or "").strip()
    if resume is None:
        return {
            "status": "not_resumed",
            "error": "resume_keep_plan is only valid during a resumed run",
            "tool_was_executed": False,
        }
    if not getattr(agent, "_resume_instruction_pending", False):
        return {
            "status": "done",
            "decision": "already_reviewed",
            "tool_was_executed": False,
        }
    if not reason:
        return {
            "status": "invalid_resume_review",
            "error": "reason must be non-empty",
            "tool_was_executed": False,
        }
    decision = {
        "decision": "keep_plan",
        "reason": reason,
        "runId": getattr(resume, "run_id", "") or None,
    }
    try:
        state = load_task_state(agent.logger)
        resumes = state.get("resumes") if isinstance(state, dict) else None
        if (
            not isinstance(resumes, list)
            or not resumes
            or not isinstance(resumes[-1], dict)
        ):
            raise ValueError("current resume audit entry is unavailable")
        resumes[-1]["instructionDecision"] = decision
        write_task_state(agent.logger, state)
    except Exception as exc:
        # The orchestration gate is process-local and must remain usable even
        # when an old worktree lacks the new audit shape or audit I/O fails.
        # Surface the durability gap explicitly instead of pretending it wrote.
        agent.logger.write(
            "resume.instruction.audit_failed",
            {
                **decision,
                "error": str(exc)[:500],
            },
        )
    agent._resume_instruction_pending = False
    reactivated_phase_ids = reactivate_resumable_hitl_phases(
        agent.logger,
        plan=getattr(agent, "task_plan", None),
    )
    agent.logger.write(
        "resume.instruction.reviewed",
        {**decision, "reactivatedPhaseIds": reactivated_phase_ids},
    )
    return {
        "status": "done",
        "decision": "keep_plan",
        "reason": reason,
        "reactivatedPhaseIds": reactivated_phase_ids,
        "next_instruction": (
            "Resume the reactivated HITL-interrupted phases under their"
            " accepted contracts. Re-observe live browser state and request"
            " HITL again if the challenge is still present."
            if reactivated_phase_ids else
            "Continue from the next pending phase."
        ),
    }


@LEAD_TOOLS.register(
    name="extend_task_plan",
    description=(
        "Append new phases the user's resume instruction asks for. Every"
        " accepted phase keeps its validated status, evidence and artifacts."
        " Use this when the instruction adds targets and changes nothing about"
        " the existing ones — typically more URLs of the same kind."
        " Use emit_task_plan with replan_reason instead when the instruction"
        " revisits existing targets: re-collecting them, changing their fields,"
        " sources, validators, or acceptance criteria."
        " To deliver one combined table over old and new results, do NOT add a"
        " phase for it: every plan phase runs in a browser, and merging"
        " artifacts is not browser work. Append only the collection phases, and"
        " once they are validated call lead_save_artifact with"
        " mode=\"reference_merge\" citing the old and new artifacts."
        " Available only while resuming a run that carries a user instruction."
    ),
    input_schema=_extend_task_plan_schema,
    loop_guard=False,
)
async def _lead_extend_task_plan(ctx: ToolContext) -> JsonDict:
    return await ctx.agent.extend_task_plan(
        ctx.tool_input.get("new_phases"),
        str(ctx.tool_input.get("replan_reason") or ""),
    )


def _resume_instruction_gate_rejection(agent: Any) -> Optional[JsonDict]:
    if not getattr(agent, "_resume_instruction_pending", False):
        return None
    return {
        "status": "resume_instruction_review_required",
        "error": (
            "The new resume instruction has not been reconciled with the"
            " accepted task plan."
        ),
        "tool_was_executed": False,
        "next_instruction": (
            "Call resume_keep_plan with a concrete reason if the plan's"
            " sources/artifacts/validators/phases/dependencies are still"
            " correct, call extend_task_plan if the instruction only adds new"
            " targets or deliverables on top of them, or emit a complete revised"
            " task_plan with replan_reason before spawning or finishing."
        ),
    }


@LEAD_TOOLS.register(
    name="spawn_browser_agent",
    description=(
        "Asynchronously run a BrowserAgent worker in a pooled browser slot."
        " The coordinator assigns a fleet before execution; normal workers"
        " start a fresh page in that fleet. Use fleet_id for an existing Fleet"
        " UUID or unique prefix. Use reuse_scope/session_key for cookie/session"
        " affinity when a new key should start a fresh fleet, and"
        " reuse_scope=page plus"
        " reuse_from_worker_id or preferred_slot_id only when prior pages must"
        " be exposed."
        " Use one worker for the serial rows assigned to one phase. Independent"
        " sibling phases may deliberately run in separate slots within runtime"
        " limits; omit reuse_from_worker_id for those siblings because that pin"
        " serializes them."
        " The task/context should state which fields to collect and how to derive dynamic"
        " params from the original user instruction, accepted plan artifacts,"
        " authoritative routing receipts, or current browser evidence"
        " (response.data handles, DOM.getAXTree ids, DOM.getText/DOM.getAttribute"
        " evidence, or cited record_extraction artifacts). A pageId remains the"
        " page identity across navigation, but is invalid after that page is"
        " authoritatively closed, replaced, or absent from Page.list. AXTree ids,"
        " selectors tied to a rendered document, and geometry are epoch-bound;"
        " never guess or reuse them after invalidation."
    ),
    input_schema=_spawn_browser_agent_schema,
)
async def _lead_spawn_browser_agent(ctx: ToolContext) -> JsonDict:
    agent = ctx.agent
    tool_input = ctx.tool_input
    resume_rejection = _resume_instruction_gate_rejection(agent)
    if resume_rejection is not None:
        return resume_rejection
    if getattr(agent, "task_plan", None) is None:
        return {
            "status": "plan_required",
            "error": "LeadAgent must call emit_task_plan successfully before spawning BrowserAgents.",
            "next_instruction": "Emit a valid task_plan with phases, expected_artifact, and validators.",
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
    raw_contract = tool_input.get("worker_contract")
    # Runtime twin of the plan-time task_type check: execute_lead_tool does no
    # local JSON-schema validation, so the spawn schema's enum only constrains
    # a well-behaved provider — a gateway that ignores schemas (the recurring
    # failure class here) can still send anything. tool_policy fail-opens on
    # unknown task_type (dict lookup → no disabled domains), so an unchecked
    # override typo like 'scraping' would re-enable Download/File on a
    # web_scrape phase. Reject loud before the contract is built.
    if isinstance(raw_contract, dict):
        raw_task_type = str(raw_contract.get("task_type") or "").strip()
        if raw_task_type:
            canonical_task_type = normalize_task_type(raw_task_type)
            if canonical_task_type not in VALID_TASK_TYPES:
                return {
                    "status": "invalid_worker_contract",
                    "error": (
                        "worker_contract.task_type must be one of"
                        f" {task_type_choices_for_error()}; got {raw_task_type!r}"
                    ),
                    "tool_was_executed": False,
                    "next_instruction": (
                        "Retry spawn_browser_agent without worker_contract.task_type"
                        " (the phase type is authoritative), or use the same canonical"
                        " value as phase.task_type. Never invent task_type names."
                    ),
                }
            raw_contract["task_type"] = canonical_task_type
    phase, rejection = agent.resolve_phase_for_spawn_with_rejection(
        str(phase_id) if isinstance(phase_id, str) and phase_id.strip() else None,
        worker_contract=raw_contract if isinstance(raw_contract, dict) else None,
    )
    if phase is not None and isinstance(raw_contract, dict):
        asserted_task_type = str(raw_contract.get("task_type") or "").strip()
        phase_task_type = normalize_task_type(phase.get("task_type"))
        if asserted_task_type and asserted_task_type != phase_task_type:
            return {
                "status": "invalid_worker_contract",
                "error": (
                    "worker_contract.task_type cannot override phase.task_type"
                    f" ({asserted_task_type!r} != {phase_task_type!r})"
                ),
                "tool_was_executed": False,
                "next_instruction": (
                    "Re-emit task_plan with a revised phase.task_type if the"
                    " phase needs different method access; otherwise omit the"
                    " worker_contract.task_type assertion."
                ),
            }
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
    dispatch_wave = phase.get("dispatch_wave")
    if isinstance(dispatch_wave, int) and not isinstance(dispatch_wave, bool):
        lower_wave_ids = [
            str(item.get("id") or "")
            for item in (agent.task_plan.get("phases") or [])
            if isinstance(item, dict)
            and isinstance(item.get("dispatch_wave"), int)
            and not isinstance(item.get("dispatch_wave"), bool)
            and int(item.get("dispatch_wave")) < dispatch_wave
            and str(item.get("id") or "")
        ]
        state = load_task_state(agent.logger)
        phase_states = state.get("phases") if isinstance(state, dict) else {}
        phase_states = phase_states if isinstance(phase_states, dict) else {}
        waiting_for = [
            prior_id for prior_id in lower_wave_ids
            if not isinstance(phase_states.get(prior_id), dict)
            or phase_states[prior_id].get("status") != "validated_done"
        ]
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
    sibling_handoff = (
        _sibling_phase_handoff(agent, state, phase)
        if prior_handoff is None else None
    )
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
    strategies = (
        agent.strategies_for_phase(phase)
        if hasattr(agent, "strategies_for_phase")
        else []
    )
    strategy_guidance = render_strategy_guidance(strategies)
    worker_contract["strategy_ids"] = [
        str(item.get("id"))
        for item in strategies
        if isinstance(item, dict) and str(item.get("id") or "").strip()
    ]
    base_task = str(tool_input.get("task") or phase.get("worker_task") or "")
    base_context = str(tool_input.get("context") or phase.get("context") or "")
    if isinstance(sibling_handoff, dict):
        # `context` is a separate spawn argument and never lands in the
        # spawner.browser.spawn payload, so without this line "did the route
        # actually get attached?" is unanswerable from the run log — which is
        # exactly the question the first run after this feature raised.
        agent.logger.write("spawn.sibling_route_attached", {
            "phaseId": str(phase.get("id") or ""),
            "sourcePhaseId": sibling_handoff["sourcePhaseId"],
            "sourceOutcome": sibling_handoff["sourceOutcome"],
        })
        base_context = (
            f"{base_context}\n\nSIBLING PHASE ROUTE from"
            f" {sibling_handoff['sourcePhaseId']}"
            f" ({sibling_handoff['sourceOutcome']}; same stage and task_type,"
            " different entity). Its receipts and claims retain their stated"
            " ownership and describe ANOTHER page: reuse what worked, avoid"
            " what did not, and verify every target on your own:\n"
            + json.dumps(
                sibling_handoff["handoff"],
                ensure_ascii=False,
                separators=(",", ":"),
                default=str,
            )
        ).strip()
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
    if strategy_guidance:
        base_context = f"{base_context}\n\n{strategy_guidance}".strip()
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
    _remember_phase_dispatch(
        agent, phase, tool_input, task=base_task, context=base_context,
    )
    return await agent.spawner.spawn_browser_agent(
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
        fleet_id=tool_input.get("fleet_id"),
        session_key=tool_input.get("session_key"),
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


_AUTH_GATE_FIELD_NAMES = {
    "auth_required",
    "authentication_required",
    "login_required",
    "signin_required",
    "sign_in_required",
    "requires_login",
    "requires_auth",
    "auth_method",
    "authentication_method",
    "login_method",
    "auth_surface",
    "login_surface",
    "auth_evidence",
    "login_evidence",
    "next_phase_requires_hitl",
}

_AUTH_GATE_MARKERS = (
    "auth",
    "authentication",
    "authenticate",
    "login",
    "log in",
    "logged in",
    "sign in",
    "signin",
    "sso",
    "oauth",
    "credential",
    "password",
    "captcha",
    "human verification",
    "identity verification",
    "phone verification",
    "security verification",
    "verification code",
    "sms verification",
    "2fa",
    "mfa",
    "paywall",
    "subscribe",
    "subscription",
    "hitl",
    "登录",
    "登陆",
    "认证",
    "手机号",
    "验证码",
    "人机",
    "扫码",
    "微信",
    "付费墙",
)

_AUTH_GATE_PROBE_MARKERS = (
    "probe",
    "explore",
    "assess",
    "identify",
    "detect",
    "discover",
    "check auth",
    "check authentication",
    "check login",
    "check sign-in",
    "check signin",
    "check gate",
    "check paywall",
    "inspect",
    "understand",
    "requirements",
    "page state",
    "visible",
    "门禁",
    "探测",
    "探索",
    "识别",
    "判断",
    "确认",
    "可见",
)

_AUTH_GATE_EXECUTION_MARKERS = (
    "handle login",
    "handle auth",
    "handle authentication",
    "complete login",
    "complete auth",
    "complete authentication",
    "perform login",
    "perform auth",
    "perform authentication",
    "requestpause",
    "request pause",
    "request hitl",
    "hitl.requestpause",
    "after login",
    "after authentication",
    "post-login",
    "post login",
    "post-auth",
    "post auth",
    "verify login",
    "verify auth",
    "verify authentication",
    "verify authenticated",
    "verify by page.getstate",
    "login verification",
    "auth verification",
    "authentication verification",
    "login status",
    "login_status",
    "form accessible",
    "form_accessible",
    "完成登录",
    "完成认证",
    "处理登录",
    "处理认证",
    "请求 hitl",
    "人工登录",
    "登录后",
    "认证后",
    "验证登录",
    "验证认证",
)

_POST_AUTH_TARGET_MARKERS = (
    "form",
    "field",
    "section",
    "fill",
    "submit",
    "row",
    "item",
    "detail",
    "download",
    "collect",
    "extract",
    "list",
    "表单",
    "字段",
    "版块",
    "部分",
    "填写",
    "提交",
    "采集",
    "下载",
    "详情",
)


def _auth_gate_probe_guidance(phase: JsonDict, worker_contract: JsonDict) -> str:
    """Inject guardrails only for an explicitly diagnostic gate probe.

    The trigger is intentionally semantic rather than site-specific: it catches
    phases whose final deliverable is gate diagnosis, while skipping business
    phases and phases whose job explicitly includes requesting HITL/login.
    Unpredicted gates in ordinary work are handled by the BrowserAgent's global
    runtime-auth interrupt SOP, not by ending this worker and spawning another.
    """
    expected = (
        worker_contract.get("expected_artifact")
        if isinstance(worker_contract.get("expected_artifact"), dict)
        else phase.get("expected_artifact")
        if isinstance(phase.get("expected_artifact"), dict)
        else {}
    )
    fields = _expected_field_names(expected)
    normalized_fields = {
        str(item or "").strip().lower()
        for item in fields
        if str(item or "").strip()
    }
    has_gate_fields = bool(normalized_fields & _AUTH_GATE_FIELD_NAMES)

    validators = worker_contract.get("validators")
    if not isinstance(validators, list):
        validators = phase.get("validators")

    parts = [
        phase.get("id"),
        phase.get("objective"),
        phase.get("worker_task"),
        phase.get("stage_hint"),
        phase.get("stage_hint_reason"),
        phase.get("context"),
        worker_contract.get("task_type"),
        worker_contract.get("objective"),
        worker_contract.get("stage_hint"),
        worker_contract.get("stage_hint_reason"),
        *fields,
    ]
    for structured in (expected, validators):
        if structured:
            try:
                parts.append(json.dumps(structured, ensure_ascii=False, default=str))
            except TypeError:
                parts.append(str(structured))
    text = " ".join(str(item or "") for item in parts)
    if not any(
        contains_semantic_marker(text, marker)
        for marker in _AUTH_GATE_MARKERS
    ):
        return ""

    is_probe = has_gate_fields or any(
        contains_semantic_marker(text, marker)
        for marker in _AUTH_GATE_PROBE_MARKERS
    )
    if not is_probe:
        return ""

    if any(
        contains_affirmative_semantic_marker(text, marker)
        for marker in _AUTH_GATE_EXECUTION_MARKERS
    ):
        return ""

    target_terms = [
        marker
        for marker in _POST_AUTH_TARGET_MARKERS
        if contains_semantic_marker(text, marker)
    ][:8]
    target_note = ""
    if target_terms:
        target_note = (
            "- Treat target-content terms in this phase as post-auth scope if a"
            f" gate is present: {target_terms!r}. Mark them behind_auth or"
            " unknown; do not spend steps discovering them before auth.\n"
        )

    return (
        "<auth_gate_probe_guidance>\n"
        "- The phase contract explicitly makes gate diagnosis the final deliverable; this is the narrow probe-only exception to the runtime-auth interrupt rule.\n"
        "- Stop as soon as login/auth/SSO/OAuth/QR/phone verification/CAPTCHA/HITL/paywall is confirmed with Page.getState and DOM.getAXTree evidence.\n"
        "- Report only gate facts: auth_required/login_required, auth_surface, auth_method/options/providers, current URL/title, evidence text/source, and whether the next phase needs HITL.\n"
        "- Because the user asked only for diagnosis, do not call Hitl.requestPause, dismiss auth/paywall overlays, click provider/login/submit buttons, fill credentials, direct-navigate around the gate, or inspect post-auth form/list/detail/download fields.\n"
        f"{target_note}"
        "- If the protected target is not visible before auth, return it as behind_auth/unknown and finish this diagnostic contract; do not assume or request a follow-up login/HITL phase.\n"
        "</auth_gate_probe_guidance>"
    )


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


def _remember_phase_dispatch(
    agent: Any,
    phase: JsonDict,
    tool_input: JsonDict,
    *,
    task: str,
    context: str,
) -> None:
    """Record the exact dispatch so the harness can repeat it verbatim.

    A harness-owned continuation must re-issue what the Lead actually asked
    for, not a reconstruction of it from the plan. A spawn can carry routing
    the phase alone does not express - session_key, fleet_id, page_policy, a
    worker_contract override - and silently dropping any of those changes the
    objective while looking like a retry.
    """
    store = getattr(agent, "phase_dispatch_inputs", None)
    if not isinstance(store, dict):
        store = {}
        setattr(agent, "phase_dispatch_inputs", store)
    phase_id = str(phase.get("id") or "").strip()
    if not phase_id:
        return
    record: JsonDict = {
        "task": str(task or ""),
        "context": str(context or ""),
        "result_contract": str(tool_input.get("result_contract") or ""),
    }
    for key in (
        "name", "preferred_slot_id", "reuse_scope", "fleet_id",
        "session_key", "page_policy",
    ):
        value = tool_input.get(key)
        if value is not None:
            record[key] = value
    raw_contract = tool_input.get("worker_contract")
    if isinstance(raw_contract, dict):
        record["worker_contract"] = copy.deepcopy(raw_contract)
    store[phase_id] = record


def _phase_continuation_note(
    agent: Any,
    phase_id: str,
    result: JsonDict,
    *,
    attempt: int,
    max_attempts: int,
) -> str:
    """State what is mechanically known about the attempt being continued.

    Deliberately no claim about WHICH rows remain. The harness knows the prior
    attempt's status, its row count and its persisted artifact paths, all from
    receipts. It does not know row identity for a contract whose units are not
    enumerable, and inventing a remaining range here would reintroduce exactly
    the model-authored guess this path exists to remove -
    ``_direct_continuation_context`` still adds the precise unit list on the
    contracts where one is provable.
    """
    paths = phase_prior_artifact_paths(agent.logger, phase_id=phase_id)
    status = str(result.get("status") or "unknown")
    rows = _direct_row_count(result)
    lines = [
        "HARNESS PHASE CONTINUATION"
        f" (automatic continuation {attempt} of {max_attempts}).",
        "You are continuing the SAME phase a previous worker did not finish."
        f" That worker ended with status={status} after {rows} recorded row(s).",
    ]
    if paths:
        lines.append(
            "Its persisted artifacts are:"
            f" {json.dumps(paths[:10], ensure_ascii=False)}."
            " Read them before acting and continue where they end; do not"
            " re-collect what they already contain. Record only new rows - the"
            " harness merges them with the prior persisted evidence and"
            " validates the original complete contract."
        )
    else:
        lines.append(
            "It persisted no artifact, so nothing of its output is trusted."
            " Start the phase's deliverable from the beginning."
        )
    lines.append(
        "The objective, the contract and the deliverable are unchanged. Do not"
        " narrow or restate them."
    )
    return "\n".join(lines)


# The statuses _direct_continuation_decision is willing to continue. Duplicated
# as a set here only to decide whether a refusal is worth a log line; the
# decision itself stays in that one function.
_CONTINUABLE_WORKER_STATUSES = frozenset({
    "partial", "step_budget_exhausted", "context_limit_exceeded",
    "incomplete", "page_crashed", "fleet_assignment_lost",
})


def _phase_auto_continuation_limit(agent: Any) -> int:
    harness_config = getattr(getattr(agent, "runtime", None), "harness", None)
    if not getattr(harness_config, "phase_auto_continuation_enabled", False):
        return 0
    return max(0, int(
        getattr(harness_config, "phase_auto_continuation_max_attempts", 0) or 0
    ))


def _phase_auto_continuation_block(agent: Any, result: Any) -> Optional[str]:
    """Why this completed worker may not be continued by the harness, or None.

    Every gate is mechanical. Anything needing a judgement - which phase to run
    next, whether a blocker is worth another try, whether the objective should
    change - stays with the Lead, and the reason is logged so an operator can
    see which fence stopped an expected continuation.
    """
    if not isinstance(result, dict):
        return "worker_result_missing"
    phase_id = str(result.get("phaseId") or "").strip()
    if not phase_id or phase_id == "direct_worker":
        # direct_worker already runs its own continuation loop in
        # _run_direct_worker; continuing it again here would double-dispatch.
        return "phase_not_eligible"
    plan = getattr(agent, "task_plan", None)
    if not isinstance(find_phase(plan, phase_id), dict):
        return "phase_not_in_plan"
    store = getattr(agent, "phase_dispatch_inputs", None)
    if not isinstance(store, dict) or phase_id not in store:
        return "no_recorded_dispatch"
    state = load_task_state(agent.logger)
    phase_states = state.get("phases") if isinstance(state, dict) else None
    phase_state = (
        phase_states.get(phase_id) if isinstance(phase_states, dict) else None
    )
    status = (
        str(phase_state.get("status") or "")
        if isinstance(phase_state, dict) else ""
    )
    if status in TERMINAL_PHASE_STATUSES:
        return f"phase_terminal:{status}"
    return None


async def _auto_continue_phase(ctx: ToolContext, waited: JsonDict) -> JsonDict:
    """Re-dispatch one unfinished phase instead of waking the Lead to do it.

    Bounded three ways, all mechanical and all already used by the direct-worker
    loop: the status must be one _direct_continuation_decision accepts, the
    attempt must show progress against the previous one
    (repeated_no_progress_same_signature stops a loop), and the automatic
    attempt count is capped separately from phase.max_attempts because a
    `partial` attempt does not consume the phase budget.

    Only one completed worker with nothing still pending is eligible. Two
    finished workers is a merge decision, and a live sibling makes a re-spawn a
    concurrency question - both belong to the Lead.

    A Lead that passed `timeout_seconds` asked to have control back at that
    deadline, and a continuation would run past it. Across every recorded run
    no wait has ever carried one (0 of 146), so honouring it costs nothing.
    """
    agent = ctx.agent
    limit = _phase_auto_continuation_limit(agent)
    if limit <= 0:
        return waited
    if ctx.tool_input.get("timeout_seconds") is not None:
        return waited
    completed = waited.get("completed") if isinstance(waited, dict) else None
    if not isinstance(completed, list) or len(completed) != 1:
        return waited
    if waited.get("pending"):
        return waited
    result = completed[0]
    block = _phase_auto_continuation_block(agent, result)
    if block is not None:
        if str(result.get("status") or "").lower() in _CONTINUABLE_WORKER_STATUSES:
            # Only worth a line when the status alone would have continued:
            # otherwise every ordinary `done` would log a refusal.
            agent.logger.write("lead.phase_continuation.blocked", {
                "phaseId": result.get("phaseId"),
                "workerId": result.get("workerId"),
                "status": result.get("status"),
                "reason": block,
            })
        return waited
    phase_id = str(result.get("phaseId") or "")
    chain: List[JsonDict] = []
    previous: Optional[JsonDict] = None
    for attempt in range(1, limit + 1):
        phase = find_phase(getattr(agent, "task_plan", None), phase_id)
        if not isinstance(phase, dict):
            break
        manifest = _direct_dispatch_manifest(agent, phase, attempt)
        receipt = _direct_continuation_receipt(phase, result, manifest)
        decision = _direct_continuation_decision(
            result,
            previous_result=previous,
            attempt_number=attempt,
            # The Lead's own dispatch was attempt 0, so `limit` automatic
            # continuations need a budget of limit + 1 for the shared decision
            # helper, whose fence is `attempt_number >= max_attempts`.
            max_attempts=limit + 1,
            continuation_receipt=receipt,
        )
        agent.logger.write("lead.phase_continuation.decision", {
            "phaseId": phase_id,
            "attempt": attempt,
            "maxAttempts": limit,
            "workerId": result.get("workerId"),
            "status": result.get("status"),
            **decision,
        })
        if not decision.get("continue"):
            break
        recorded = getattr(agent, "phase_dispatch_inputs", {}).get(phase_id) or {}
        spawn_input: JsonDict = {
            key: value for key, value in recorded.items()
            if key not in {"context", "name"}
        }
        spawn_input["phase_id"] = phase_id
        spawn_input["context"] = "\n\n".join(part for part in (
            _direct_continuation_context(
                str(recorded.get("context") or ""), receipt,
            ),
            _phase_continuation_note(
                agent, phase_id, result, attempt=attempt, max_attempts=limit,
            ),
        ) if part)
        spawn_input["reuse_from_worker_id"] = str(result.get("workerId") or "")
        worker_status = str(result.get("status") or "").lower()
        if worker_status == "page_crashed":
            spawn_input["reuse_scope"] = "connection"
            spawn_input["page_policy"] = "new"
        elif worker_status == "fleet_assignment_lost":
            spawn_input.pop("reuse_from_worker_id", None)
        else:
            spawn_input["reuse_scope"] = "page"
            spawn_input["page_policy"] = "existing"
        spawn_input["_runtime_dispatch_origin"] = "runtime_phase_continuation"
        spawned = await _lead_spawn_browser_agent(ToolContext(
            agent=agent,
            tool_call={
                "name": "spawn_browser_agent",
                "id": f"phase-continuation-{phase_id}-{attempt}",
            },
            tool_input=spawn_input,
            step=ctx.step,
        ))
        if not isinstance(spawned, dict) or spawned.get("status") != "running":
            # A refused spawn is a real answer - phase_exhausted, a dependency
            # fence, a replan checkpoint. Hand it back to the Lead attached to
            # the worker result rather than retrying around it.
            chain.append({
                "attempt": attempt,
                "spawnRejected": spawned if isinstance(spawned, dict) else None,
            })
            break
        worker_id = str(spawned.get("workerId") or "")
        agent.logger.write("lead.phase_continuation.attempt", {
            "phaseId": phase_id,
            "attempt": attempt,
            "workerId": worker_id,
            "continuedFromWorkerId": result.get("workerId"),
        })
        next_wait = await agent.spawner.wait_browser_agents(
            worker_ids=[worker_id] if worker_id else None,
            mode="all",
        )
        next_completed = (
            next_wait.get("completed") if isinstance(next_wait, dict) else None
        )
        next_result = (
            next_completed[-1]
            if isinstance(next_completed, list) and next_completed else None
        )
        if not isinstance(next_result, dict):
            chain.append({"attempt": attempt, "waitIncomplete": True})
            break
        chain.append({
            "attempt": attempt,
            "workerId": next_result.get("workerId"),
            "status": next_result.get("status"),
            "rowCount": _direct_row_count(next_result),
        })
        previous, result = result, next_result
        waited = next_wait
        if _phase_auto_continuation_block(agent, result) is not None:
            break
    if not chain:
        return waited
    enriched = dict(waited)
    enriched["phaseContinuation"] = {
        "phaseId": phase_id,
        "dispatchedBy": "harness",
        "maxAutomaticAttempts": limit,
        "attempts": chain,
        "note": (
            "The harness continued this phase without waking the Lead."
            " The worker result below is the LAST attempt; earlier attempts are"
            " listed here and their artifacts are already in the phase ledger."
        ),
    }
    return enriched


@LEAD_TOOLS.register(
    name="wait_browser_agents",
    description="Wait for spawned BrowserAgents to complete; wait for all of them or the first one to finish.",
    input_schema=_wait_browser_agents_schema,
)
async def _lead_wait_browser_agents(ctx: ToolContext) -> JsonDict:
    waited = await ctx.agent.spawner.wait_browser_agents(
        worker_ids=ctx.tool_input.get("worker_ids"),
        mode=ctx.tool_input.get("mode", "all"),
        timeout_seconds=ctx.tool_input.get("timeout_seconds"),
    )
    return await _auto_continue_phase(ctx, waited)


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
    return ctx.agent.spawner.list_browser_agents()


@LEAD_TOOLS.register(
    name="local_fs_search",
    description="Read-only search across files inside the current task worktree; supports glob, JSONL event-type filtering, and per-hit / total output caps.",
    input_schema=_local_fs_search_schema,
)
async def _lead_local_fs_search(ctx: ToolContext) -> JsonDict:
    tool_input = ctx.tool_input
    return local_fs_search(
        ctx.agent.logger,
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


@LEAD_TOOLS.register(
    name="local_fs_read",
    description="Read-only line-range read of a file inside the current task worktree; well suited to JSONL traces and AXTree lines.txt offload files.",
    input_schema=_local_fs_read_schema,
)
async def _lead_local_fs_read(ctx: ToolContext) -> JsonDict:
    tool_input = ctx.tool_input
    return local_fs_read(
        ctx.agent.logger,
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
        state, raw_ledger_paths,
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
    resume_rejection = _resume_instruction_gate_rejection(ctx.agent)
    if resume_rejection is not None:
        return resume_rejection
    state = load_task_state(ctx.agent.logger)
    final_status = str(ctx.tool_input.get("status", "done"))
    receipt = build_completion_receipt(
        state=state,
        spawner=getattr(ctx.agent, "spawner", None),
    )
    contradictions = terminal_consistency_contradictions(
        state=state,
        plan=getattr(ctx.agent, "task_plan", None),
        final_status=final_status,
    )
    if contradictions:
        return {
            "status": "rejected_terminal_inconsistency",
            "tool_was_executed": False,
            "completionReceipt": receipt,
            "contradictions": contradictions,
            "next_instruction": (
                "The proposed done status contradicts raw worker receipts for"
                " required artifact phases. Continue those phases or return a"
                " non-done final status; this receipt does not claim the task"
                " is otherwise complete."
            ),
        }
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
            if rejection_count > 2:
                # The evidence, user request and reviewer verdict are all the
                # same as the previous attempts. Asking the Lead to retry done
                # again cannot create new evidence; settle as partial and make
                # the mismatch a structured disclosure instead of a loop.
                final_status = "partial"
                answer = _append_field_semantic_disclosure(answer, field_review)
                field_review = {
                    **field_review,
                    "boundedDisclosure": {
                        "rejectionCount": rejection_count,
                        "reason": "unchanged_semantic_mismatch",
                    },
                }
            else:
                return {
                    "status": "rejected",
                    "error": "field_semantic_mismatch",
                    "tool_was_executed": False,
                    "fieldSemanticReview": field_review,
                    "completionReceipt": receipt,
                    "next_instruction": (
                        "The semantic reviewer found delivered field values whose"
                        " evidence describes a different subject or unit than the"
                        " original request. Inspect the listed rows. Correct them"
                        " from validated evidence and re-issue done, continue"
                        " collection if the requested value remains obtainable,"
                        " or return a truthful non-done status that discloses the"
                        " unresolved requested fields. Do not rename a substitute"
                        " value as the requested field."
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
        index = build_numeric_fact_index(
            state, task_dir=getattr(logger, "task_dir", None),
        )
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
        cache_key = hashlib.sha256(json.dumps(
            {
                "userTask": str(getattr(agent, "original_user_task", "") or ""),
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
            user_task=str(getattr(agent, "original_user_task", "") or ""),
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


def _append_field_semantic_disclosure(answer: str, review: JsonDict) -> str:
    """Make a system-forced partial result truthful to a human reader."""
    marker = "## System delivery disclosure"
    if marker in answer:
        return answer
    lines = [
        "",
        marker,
        "Status: partial. The following delivered values have evidence that does not match the requested subject or unit:",
    ]
    for item in (review.get("mismatches") or [])[:8]:
        if not isinstance(item, dict):
            continue
        lines.append(
            "- field={field}; value={value}; requested={requested}; evidence={evidence}; affectedRows={rows}".format(
                field=str(item.get("field") or ""),
                value=str(item.get("value") or ""),
                requested=str(item.get("requestedSubject") or ""),
                evidence=str(item.get("evidenceSubject") or ""),
                rows=int(item.get("affectedRows") or 0),
            )
        )
    lines.append("See fieldSemanticReview for the complete structured evidence.")
    return answer.rstrip() + "\n" + "\n".join(lines)


def _sibling_phase_handoff(
    agent: Any, state: Any, phase: JsonDict,
) -> Optional[JsonDict]:
    """The route a sibling entity phase already proved, when this one has none.

    `prior_handoff` is indexed by phaseId, so it only ever replays a phase into
    itself. Splitting a stage into one phase per entity — which is what keeps a
    worker inside its step budget — therefore made every sibling start from
    zero: in task 9d490dc3 three detail workers explored the same product-page
    layout independently at 21, 17 and 30 steps, and the two that finished
    later learned nothing from the one that finished first.

    Nothing in the plan connects them explicitly, but `stage_hint` plus
    `task_type` already says mechanically that they do the same kind of work,
    so the harness can hand the route over instead of hoping the Lead thinks to
    copy it into spawn context.

    A FAILED sibling is carried too, and ranked below a successful one rather
    than discarded. "This entry led nowhere" is often the cheapest thing a
    worker can be told: browser-008 spent eight steps enumerating a region its
    page never rendered, and the next worker on the same layout had no way to
    know that had already been tried. Only the handoff travels — rows keep
    their own artifact lineage.
    """
    plan = getattr(agent, "task_plan", None)
    if not isinstance(plan, dict) or not isinstance(state, dict):
        return None
    phase_id = str(phase.get("id") or "")
    stage = str(phase.get("stage_hint") or "").strip()
    task_type = str(phase.get("task_type") or "").strip()
    if not stage or not task_type:
        return None
    phases_state = state.get("phases")
    if not isinstance(phases_state, dict):
        return None
    best: Optional[Tuple[Tuple[int, str], str, JsonDict, bool]] = None
    for candidate in plan.get("phases") or []:
        if not isinstance(candidate, dict):
            continue
        candidate_id = str(candidate.get("id") or "")
        if not candidate_id or candidate_id == phase_id:
            continue
        if str(candidate.get("stage_hint") or "").strip() != stage:
            continue
        if str(candidate.get("task_type") or "").strip() != task_type:
            continue
        candidate_state = phases_state.get(candidate_id)
        if not isinstance(candidate_state, dict):
            continue
        succeeded = str(candidate_state.get("status") or "") == "validated_done"
        for attempt in reversed(candidate_state.get("attempts") or []):
            if not isinstance(attempt, dict):
                continue
            if not str(attempt.get("finished_at") or ""):
                continue
            digest = attempt.get("attemptDigest")
            if not isinstance(digest, dict):
                continue
            handoff = digest.get("handoff")
            if not isinstance(handoff, dict):
                continue
            # Rank on outcome first, recency second: a proven route always
            # beats a fresher dead end, but a dead end still beats nothing.
            rank = (1 if succeeded else 0, str(attempt.get("finished_at") or ""))
            if best is None or rank > best[0]:
                best = (rank, candidate_id, handoff, succeeded)
            break
    if best is None:
        return None
    return {
        "sourcePhaseId": best[1],
        "handoff": best[2],
        "sourceOutcome": "succeeded" if best[3] else "did_not_complete",
    }


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
            state, task_dir=getattr(logger, "task_dir", None),
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
    if status != "failed":
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
            "A DATA problem, unlike a coverage rejection: each entry below was"
            " recomputed and came out different. "
            "These numbers disagree with the artifacts this task actually"
            " delivers. `actualValue` is recomputed from the active validated"
            " generation; a dataConflict means a superseded artifact holds MORE"
            " than the delivered one, so the data regressed and calling it"
            " complete would be wrong. Correct the numbers to match the"
            " delivered artifacts — or, for a dataConflict, restore the missing"
            " rows with lead_save_artifact mode=\"reference_merge\" — then"
            " re-issue final_answer."
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


RESUME_ONLY_LEAD_TOOLS = frozenset({
    "resume_keep_plan", "extend_task_plan", "approve_current_task_plan",
})


PLANNING_LEAD_TOOLS = frozenset({
    "emit_task_plan",
    "emit_direct_task_plan",
    "begin_task_plan_draft",
    "append_task_plan_draft",
    "submit_task_plan_draft",
    "repair_task_plan",
    "local_fs_search",
    "local_fs_read",
    "read_harness_guide",
    "search_harness_guides",
    # Needed to report an operator cancellation or terminal harness failure.
    # It records an auditable completion receipt; it does not prove that a plan
    # was completed. A planning-stage done with validatedPhases=0 remains
    # visible in that receipt, is not refused, and still ends the run completed.
    "final_answer",
})


def build_lead_agent_tool_specs(
    *, include_resume: bool = False, stage: str = "all",
) -> List[JsonDict]:
    specs = LEAD_TOOLS.tool_specs()
    if not include_resume:
        specs = [
        spec for spec in specs
        if spec.get("name") not in RESUME_ONLY_LEAD_TOOLS
        ]
    if stage == "planning":
        return [
            spec for spec in specs
            if spec.get("name") in PLANNING_LEAD_TOOLS
        ]
    return specs
