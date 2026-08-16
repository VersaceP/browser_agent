"""
harness.tools.lead_tools - LeadAgent tool schemas and dispatch factory.
"""

import copy
import hashlib
import json
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
from harness.strategy_bank import render_strategy_guidance
from harness.task_control import (
    EXECUTION_ROLES,
    direct_batch_rows_provenance_errors,
    mark_phase_exhausted_if_needed,
    materialize_batch_rows_from_source,
    replan_checkpoint_spawn_rejection,
    load_task_state,
    write_task_state,
)
from harness.results.completion_receipt import (
    build_completion_receipt,
    terminal_consistency_contradictions,
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
                    " is exact; ordinary page text and hidden/blocked nodes do"
                    " not count."
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
            },
            "field": {
                "type": "string",
                "description": "Target field for single-field validators (range/url_pattern/field_pattern).",
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
            "min": {"type": "number"},
            "max": {"type": "number"},
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
                        # Canonical names only — legacy aliases are accepted at
                        # runtime with a warning receipt, same policy as the
                        # validator type enum.
                        "enum": sorted(VALID_TASK_TYPES),
                        "description": (
                            "Overall classification of the task, used for"
                            " strategy selection and audit. It does NOT set"
                            " worker method access — each phase declares its"
                            " own task_type for that."
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
                                "expected_artifact": {
                                    **_expected_artifact_schema(),
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
                            "required": [
                                "id",
                                "task_type",
                                "objective",
                                "expected_artifact",
                            ],
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
    phase_properties = schema["properties"]["plan"]["properties"]["phases"][
        "items"
    ]["properties"]
    phase_properties["worker_contract"] = {
        "type": "object",
        "description": (
            "Optional exceptional execution overrides only (session/auth,"
            " pacing, batch source, explicit method policy, or content"
            " observation markers). Ordinary task, artifact, validator, and"
            " tactic fields are derived from the phase and prior handoff."
        ),
        "additionalProperties": True,
    }
    return schema


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
                **_nullable("string"),
                "description": "The task_plan phase id this worker executes. Pass null to use the next pending phase.",
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
            "max_steps": {
                **_nullable("integer"),
                "description": "Override the worker's max step count; pass null to use the default.",
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
                "description": "Regex grep; pass an empty string to list matches by glob / event_type only.",
            },
            "glob": {
                "type": "string",
                "description": "Glob relative to the current task worktree, e.g. traces/*.jsonl or observations/*.json.",
            },
            "event_type": {
                "type": ["string", "null"],
                "description": "JSONL-only: restrict the search to lines whose `event` matches this string; pass null when not needed.",
            },
            "max_results": {"type": "integer", "minimum": 1, "maximum": 100},
            "max_bytes_per_hit": {"type": "integer", "minimum": 200, "maximum": 20000},
            "max_total_bytes": {"type": "integer", "minimum": 1000, "maximum": 200000},
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
            "line_offset": {"type": "integer", "minimum": 0},
            "line_limit": {"type": "integer", "minimum": 1, "maximum": 5000},
            "max_bytes": {"type": "integer", "minimum": 1000, "maximum": 200000},
        },
        "required": ["path", "line_offset", "line_limit", "max_bytes"],
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
                "enum": ["done", "blocked", "failed"],
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
        effective_call = lifecycle.tool_pre_call(
            LifecycleContext(actor="lead_agent", step=step),
            tool_call,
        )
        result, should_stop = await execute_lead_tool(agent, effective_call)
        result = lifecycle.tool_post_call(
            LifecycleContext(actor="lead_agent", step=step),
            effective_call,
            result,
        )
        return result, should_stop

    return dispatch


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
    return result, (action.terminal and not soft_rejected)


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
    review = await ctx.agent.review_task_plan_candidate(raw_plan)
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
    return ctx.agent.accept_task_plan(
        raw_plan,
        plan_validator_review=(
            review
            if review.get("status")
            in {"approved", "operational_continuation", "error"}
            else None
        ),
    )


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
    agent.logger.write(
        "resume.instruction.reviewed",
        decision,
    )
    return {
        "status": "done",
        "decision": "keep_plan",
        "reason": reason,
        "next_instruction": "Continue from the next pending phase.",
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
        " Keep BrowserAgent slots scarce; when related work only needs additional"
        " pages/tabs, put that into one worker as serial Page.create and"
        " Page.switchTo work instead of fan-out."
        " The task/context should state which fields to collect and how to derive dynamic"
        " params from live feedback (response.data handles, DOM.getAXTree ids,"
        " DOM.getText/DOM.getAttribute evidence, or cited record_extraction artifacts),"
        " not hard-code stale pageIds, AXTree ids, selectors, or assumed positions."
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
    if phase is None:
        # Pass the structured rejection through verbatim: it carries the real
        # status (dependency_not_ready / blocked_by_dependency /
        # phase_already_running / explicit resource exhaustion / ...) plus a
        # next_instruction. Task 2ed5a466 collapsed these into a generic
        # "phase not found" and the Lead blind-retried a dependency-gated
        # phase twice.
        if rejection is not None:
            return rejection
        exhausted_match = _matching_exhaustion(exhausted, phase_id)
        if exhausted_match is not None:
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
        return {
            "status": "failed",
            "error": f"phase not found or no pending phase: {phase_id}",
        }
    worker_contract = agent.build_worker_contract(
        phase,
        raw_contract if isinstance(raw_contract, dict) else None,
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
    return await agent.spawner.spawn_browser_agent(
        task=base_task,
        context=base_context,
        name=tool_input.get("name") or None,
        max_steps=tool_input.get("max_steps"),
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


@LEAD_TOOLS.register(
    name="wait_browser_agents",
    description="Wait for spawned BrowserAgents to complete; wait for all of them or the first one to finish.",
    input_schema=_wait_browser_agents_schema,
)
async def _lead_wait_browser_agents(ctx: ToolContext) -> JsonDict:
    return await ctx.agent.spawner.wait_browser_agents(
        worker_ids=ctx.tool_input.get("worker_ids"),
        mode=ctx.tool_input.get("mode", "all"),
        timeout_seconds=ctx.tool_input.get("timeout_seconds"),
    )


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
    ledger_entries = {
        _resolved_path_text(path)
        for path in (state.get("artifacts") or [])
        if str(path or "").strip()
    }
    absorbed_resolved = {_resolved_path_text(path) for path in absorbed}
    cited_in_ledger = [
        resolved for resolved in (
            _resolved_path_text(path) for path in cited
        ) if resolved in ledger_entries
    ]
    if not cited_in_ledger:
        return []
    if any(resolved not in absorbed_resolved for resolved in cited_in_ledger):
        return []

    supersessions = state.get("artifact_supersessions")
    if not isinstance(supersessions, list):
        supersessions = []
    supersessions.append({
        "deliverable": _resolved_path_text(deliverable),
        "absorbed": cited_in_ledger,
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
            "absorbed": cited_in_ledger,
        })
    return cited_in_ledger


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
    return result


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
        if str(extracted.get("status")) != "ok":
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
        })
    return report


def _verdict_histogram(claims: Any) -> JsonDict:
    """Counts per verdict, so `passed` says which kind of pass it was."""
    histogram: Dict[str, int] = {}
    for claim in claims if isinstance(claims, list) else []:
        verdict = str((claim or {}).get("verdict") or "unknown")
        histogram[verdict] = histogram.get(verdict, 0) + 1
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


RESUME_ONLY_LEAD_TOOLS = frozenset({"resume_keep_plan", "extend_task_plan"})


def build_lead_agent_tool_specs(*, include_resume: bool = False) -> List[JsonDict]:
    specs = LEAD_TOOLS.tool_specs()
    if include_resume:
        return specs
    return [
        spec for spec in specs
        if spec.get("name") not in RESUME_ONLY_LEAD_TOOLS
    ]
