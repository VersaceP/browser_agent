"""JSON schemas for BrowserAgent tools."""

from typing import Dict, Tuple

from harness.utils import JsonDict
from harness.runtime_evaluation import EVAL_JS_REASON_KINDS

def _browser_input_schemas(capability_methods: Tuple[str, ...]) -> Dict[str, JsonDict]:
    method_schema: JsonDict = {
        "type": "string",
        "description": "ABCP capability method, e.g. Fleet.create, Page.navigate, DOM.getAXTree.",
    }
    if capability_methods:
        method_schema["enum"] = list(capability_methods)

    return {
        "browser_call": {
            "type": "object",
            "properties": {
                "method": method_schema,
                "params": {
                    "type": "object",
                    "description": (
                        "JSON object of params for the ABCP method; pass {} when there are none."
                        " Do not invent handles, copy placeholder ids, reuse stale AXTree ids,"
                        " or encode assumed page order as factual params."
                    ),
                    "additionalProperties": True,
                },
                "reason": {
                    "type": "string",
                    "description": "Short reason for this call (used in logs and as fallback for the `purpose` field).",
                },
                "runtime_policy": {
                    "type": "object",
                    "description": (
                        "Harness-only authorization metadata required when method"
                        " is Runtime.evaluate; it is never forwarded to ABCP."
                    ),
                    "properties": {
                        "intent": {"type": "string", "enum": ["diagnostic", "extract", "state_change"]},
                        "effect": {"type": "string", "enum": ["read_only", "state_changing"]},
                        "reason_kind": {"type": "string", "enum": sorted(EVAL_JS_REASON_KINDS)},
                        "why_structured_tools_insufficient": {"type": "string"},
                        "cross_check_plan": {"type": "string"},
                        "result_mode": {"type": "string", "enum": ["raw", "json"]},
                        "record_name": {"type": "string"},
                    },
                    "additionalProperties": False,
                },
            },
            "required": ["method", "params", "reason"],
            "additionalProperties": False,
        },
        "execute_selected_skill": {
            "type": "object",
            "properties": {
                "pageId": {
                    "type": "string",
                    "description": "Live pageId for the selected skill's frozen workflow.",
                },
                "fleetId": {
                    "type": "string",
                    "description": "Live fleetId for the page; pass \"\" only when unavailable.",
                },
                "variables": {
                    "type": "object",
                    "description": (
                        "One workflow input row. Use this OR rows; pass {} when using rows."
                    ),
                    "additionalProperties": True,
                },
                "rows": {
                    "type": "array",
                    "description": (
                        "Multiple workflow input rows executed strictly serially on the warm tab."
                        " Use this OR variables; pass [] when using variables."
                    ),
                    "items": {
                        "type": "object",
                        "additionalProperties": True,
                    },
                },
            },
            "required": ["pageId", "fleetId", "variables", "rows"],
            "additionalProperties": False,
        },
        "execute_browser_workflow": {
            "type": "object",
            "properties": {
                "pageId": {"type": "string"},
                "fleetId": {"type": "string"},
                "description": {"type": "string"},
                "variables": {"type": "object", "additionalProperties": True},
                "steps": {"type": "array", "items": {"type": "object"}},
                "timeout": {"type": "integer", "minimum": 1000, "maximum": 600000},
                "stepTimeout": {"type": "integer", "minimum": 1000, "maximum": 60000},
                "errorConfig": {"type": "object", "additionalProperties": True},
            },
            "required": ["pageId", "fleetId", "description", "variables", "steps", "timeout", "stepTimeout"],
            "additionalProperties": False,
        },
        "extract_dom_records": {
            "type": "object",
            "properties": {
                "pageId": {"type": "string"},
                "selector": {
                    "type": "string",
                    "description": "CSS selector for repeated elements, e.g. a[href], article, .card.",
                },
                "fields": {
                    "type": "object",
                    "additionalProperties": {"type": "string"},
                    "description": (
                        "Map output field names to built-in extractors: text, href,"
                        " imgAlt, visible, rect, boundingRect, ancestorText, tag, id,"
                        " class, ariaLabel, role, attr:<name>, src."
                    ),
                },
                "visibleOnly": {"type": "boolean"},
                "includeRect": {"type": "boolean"},
                "includeAncestorText": {"type": "boolean"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 1000},
                "record_name": {
                    "type": "string",
                    "description": (
                        "If non-empty, automatically persist the rows via record_extraction"
                        " under this dataset name. Pass \"\" to inspect rows first."
                    ),
                },
            },
            "required": [
                "pageId",
                "selector",
                "fields",
                "visibleOnly",
                "includeRect",
                "includeAncestorText",
                "limit",
                "record_name",
            ],
            "additionalProperties": False,
        },
        "eval_js_json": {
            "type": "object",
            "properties": {
                "pageId": {"type": "string"},
                "expression": {
                    "type": "string",
                    "description": (
                        "JavaScript expression whose value is JSON-serializable."
                        " The harness wraps it and returns JSON.stringify({value})."
                    ),
                },
                "record_name": {
                    "type": "string",
                    "description": (
                        "If the value is rows or {rows:[...]}, persist it via"
                        " record_extraction under this name. Pass \"\" to inspect."
                    ),
                },
                "description": {
                    "type": "string",
                    "description": "Optional artifact description when record_name is set.",
                },
                "why_dom_primitives_insufficient": {
                    "type": "string",
                    "description": (
                        "Explain why DOM.getAXTree plus DOM.getText/"
                        "DOM.getAttribute cannot satisfy this extraction."
                        " Required because eval_js_json is a last-resort fallback."
                    ),
                },
                "reason_kind": {
                    "type": "string",
                    "enum": sorted(EVAL_JS_REASON_KINDS),
                    "description": (
                        "Why native DOM primitives are insufficient."
                        " Required for eval_js_json."
                    ),
                },
                "cross_check_plan": {
                    "type": "string",
                    "description": (
                        "How at least one target field will be checked with"
                        " DOM.getText or DOM.getAttribute before handoff."
                    ),
                },
            },
            "required": [
                "pageId",
                "expression",
                "record_name",
                "description",
                "why_dom_primitives_insufficient",
                "reason_kind",
                "cross_check_plan",
            ],
            "additionalProperties": False,
        },
        "navigate_verified": {
            "type": "object",
            "properties": {
                "pageId": {"type": "string"},
                "url": {"type": "string"},
                "expectedUrlPattern": {
                    "type": "string",
                    "description": "Regex that must match the final URL; pass \"\" to require exact target URL.",
                },
                "expectedTitlePattern": {
                    "type": "string",
                    "description": "Optional regex for final title; pass \"\" to skip title check.",
                },
                "timeoutSeconds": {"type": "number"},
                "maxRetries": {"type": "integer", "minimum": 1, "maximum": 3},
            },
            "required": [
                "pageId",
                "url",
                "expectedUrlPattern",
                "expectedTitlePattern",
                "timeoutSeconds",
                "maxRetries",
            ],
            "additionalProperties": False,
        },
        "dismiss_overlay": {
            "type": "object",
            "properties": {
                "pageId": {"type": "string"},
                "targetId": {
                    "type": "string",
                    "description": (
                        "Optional canonical AXTree id of the action the overlay"
                        " blocked, to auto-retry after dismissal. Pass \"\" to"
                        " just dismiss. Consequential targets are never retried."
                    ),
                },
                "targetMethod": {
                    "type": "string",
                    "description": (
                        "Method that was blocked on targetId. Pass \"\" for the"
                        " default Input.click. Only Input.click is auto-retried"
                        " after dismissal; any other method (scroll/type/press)"
                        " returns dismissed_pending_action for you to decide."
                    ),
                },
                "maxAttempts": {
                    "type": "integer",
                    "description": "Ladder attempts (1-5). Pass 0 for default (3).",
                },
                "maxDurationMs": {
                    "type": "integer",
                    "description": "Hard time budget in ms. Pass 0 for default (15000).",
                },
            },
            "required": [
                "pageId",
                "targetId",
                "targetMethod",
                "maxAttempts",
                "maxDurationMs",
            ],
            "additionalProperties": False,
        },
        "collect_items": {
            "type": "object",
            "properties": {
                "pageId": {"type": "string"},
                "selector": {
                    "type": "string",
                    "description": "CSS selector for the repeated item (card/row/list-item).",
                },
                "mode": {
                    "type": "string",
                    "description": "Expansion mode: \"scroll\" (default) or \"click_load_more\".",
                },
                "fields": {
                    "type": "object",
                    "description": (
                        "Map of output field -> spec (text|href|src|imgAlt|attr:NAME)."
                        " Pass {} for the default {title:text, href:href}."
                    ),
                    "additionalProperties": {"type": "string"},
                },
                "keyField": {
                    "type": "string",
                    "description": "Field used as the stable dedup key. Pass \"\" for href/auto.",
                },
                "direction": {"type": "string", "description": "Scroll direction (scroll mode). Pass \"\" for down."},
                "amount": {"type": "number", "description": "Scroll distance px (scroll mode). Pass 0 for default 800."},
                "containerId": {"type": "string", "description": "Canonical id of a scroll container. Pass \"\" for viewport."},
                "containerSelector": {"type": "string", "description": "CSS selector scroll container fallback. Pass \"\" if unused."},
                "loadMoreId": {"type": "string", "description": "Canonical id of the load-more button (click_load_more mode). Pass \"\" if unused."},
                "loadMoreSelector": {"type": "string", "description": "CSS selector for load-more (fallback). Pass \"\" if unused."},
                "targetCount": {"type": "integer", "description": "Stop once this many unique rows are collected. Pass 0 for no target."},
                "maxRounds": {"type": "integer", "description": "Max expansion rounds (1-50). Pass 0 for default 12."},
                "stabilityThreshold": {"type": "integer", "description": "Consecutive no-new-row rounds before stopping. Pass 0 for default 3."},
                "settleMs": {"type": "integer", "description": "Wait after each expansion before harvesting. Pass 0 for default 600."},
                "harvestLimit": {"type": "integer", "description": "Max rows read per harvest window. Pass 0 for default 200."},
                "harvestMaxWindows": {"type": "integer", "description": "Max harvest windows per round (paging a large DOM list). Pass 0 for default 10 (=2000 rows/round)."},
                "recordName": {
                    "type": "string",
                    "description": "If set, the collected rows are persisted via record_extraction under this name. Pass \"\" to only return a summary.",
                },
            },
            "required": ["pageId", "selector"],
            "additionalProperties": False,
        },
        "fill_field_verified": {
            "type": "object",
            "properties": {
                "pageId": {"type": "string"},
                "id": {"type": "string", "description": "Canonical AXTree id of the field. Pass \"\" to use selector."},
                "selector": {"type": "string", "description": "CSS selector for the field (fallback). Pass \"\" if using id."},
                "text": {"type": "string", "description": "Value to type into the field."},
                "verifyKeywords": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Keywords used to locate the field for value read-back"
                        " (matched against label/aria-label/placeholder/name)."
                        " Pass [] to derive from the field's accessible name."
                    ),
                },
                "mask": {"type": "boolean", "description": "Mask the value in results/logs (passwords/tokens)."},
                "maxRetries": {"type": "integer", "description": "Clear-and-retry attempts on mismatch (0-3). Pass 0 for default 1."},
            },
            "required": ["pageId", "text"],
            "additionalProperties": False,
        },
        "visual_verify": {
            "type": "object",
            "properties": {
                "pageId": {"type": "string"},
                "selector": {
                    "type": "string",
                    "description": "Optional CSS selector to crop; pass \"\" for viewport/fullPage.",
                },
                "id": {
                    "type": "string",
                    "description": "Optional canonical AXTree id to crop; pass \"\" if not used.",
                },
                "fullPage": {
                    "type": "boolean",
                    "description": "Whether to capture full page. Prefer false/cropped screenshots.",
                },
                "mode": {
                    "type": "string",
                    "description": "action_outcome | validator_failure | overlay_check | captcha_check | layout_check | visual_locate (locate an AXTree-blind target by description; returns a durable resolvedId via bbox→id promotion — act on that id, not coordinates) | contract_verify (judge structured visual_checks in `expected.visual_checks`; returns satisfied/violated/uncertain + failed_checks). Calls with repair_targets automatically use the internal repair_absence mode and return absent/present/uncertain.",
                },
                "question": {
                    "type": "string",
                    "description": "Short visual question for the verifier.",
                },
                "expected": {
                    "type": "object",
                    "additionalProperties": True,
                    "description": "Expected visible state, e.g. {\"target\":\"JobBuddy\",\"state\":\"product detail page\"}.",
                },
                "repair_targets": {
                    "type": "array",
                    "description": (
                        "Optional field-repair evidence binding. Use only when"
                        " the harness supplied a repair manifest and this visual"
                        " check verifies confirmed_absent fields. Each identity"
                        " and field must exactly match that manifest; unrelated"
                        " overlay/CAPTCHA/layout checks do not satisfy repair"
                        " evidence. The harness binds the check to the baseline"
                        " row URL when available, and only an absent verdict"
                        " satisfies it. Pass [] for ordinary visual verification."
                    ),
                    "items": {
                        "type": "object",
                        "properties": {
                            "identity": {
                                "type": "object",
                                "properties": {
                                    "field": {"type": "string"},
                                    "value": {},
                                },
                                "required": ["field", "value"],
                                "additionalProperties": False,
                            },
                            "fields": {
                                "type": "array",
                                "items": {"type": "string"},
                            },
                        },
                        "required": ["identity", "fields"],
                        "additionalProperties": False,
                    },
                },
            },
            "required": [
                "pageId",
                "selector",
                "id",
                "fullPage",
                "mode",
                "question",
                "expected",
            ],
            "additionalProperties": False,
        },
        "final_answer": {
            "type": "object",
            "properties": {
                "status": {
                    "type": "string",
                    "enum": [
                        "done",
                        "incomplete",
                        "partial",
                        "extraction_inconclusive",
                    ],
                    "description": (
                        "done = task complete; partial = some trustworthy results but not all targets reached;"
                        " extraction_inconclusive = extraction kept failing and no trustworthy result is available;"
                        " incomplete = any other inability to proceed."
                    ),
                },
                "answer": {
                    "type": "string",
                    "description": (
                        "JSON string containing outcome, data, evidence,"
                        " blockers, and next_steps. Large row sets must stay"
                        " in record_extraction artifacts referenced by savedPath."
                    ),
                },
                "reason": {
                    "type": "string",
                    "description": "Optional; brief justification (≤ 200 chars) for non-done statuses.",
                },
            },
            "required": ["status", "answer"],
            "additionalProperties": False,
        },
        "record_extraction": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Short dataset name, e.g. \"trending-week-products\".",
                },
                "rows": {
                    "type": "array",
                    "items": {"type": "object", "additionalProperties": True},
                    "description": (
                        "Structured rows; every row must be a JSON object. Use exact expected_artifact"
                        " field names. For sensitive fields include pageUrl, sourceTool,"
                        " sourceSelectorOrAxId, and the canonical <field>EvidenceText"
                        " key, e.g. rankEvidenceText. Legacy evidence/<field>Evidence"
                        " aliases may validate but should not be preferred."
                    ),
                },
                "schema": {
                    "type": "object",
                    "description": "Optional; documents the source/meaning of fields in `rows`. Not enforced.",
                    "additionalProperties": True,
                },
                "description": {
                    "type": "string",
                    "description": "Optional; which page / selector this data was extracted from.",
                },
                "repair_resolutions": {
                    "type": "array",
                    "description": (
                        "Field-level outcomes used only when the harness supplied a"
                        " repair manifest. Non-empty repaired values default to"
                        " value_found. Every empty repaired value must declare"
                        " observed_empty (the source explicitly exposes a legal"
                        " empty value) or confirmed_absent (the expected browser"
                        " content does not exist). confirmed_absent may require"
                        " visual_verify before final_answer; Page.screenshot alone"
                        " is not visual verification. This metadata is not written"
                        " into the user artifact."
                    ),
                    "items": {
                        "type": "object",
                        "properties": {
                            "identity": {
                                "type": "object",
                                "properties": {
                                    "field": {"type": "string"},
                                    "value": {},
                                },
                                "required": ["field", "value"],
                                "additionalProperties": False,
                            },
                            "field": {"type": "string"},
                            "outcome": {
                                "type": "string",
                                "enum": [
                                    "value_found",
                                    "observed_empty",
                                    "confirmed_absent",
                                    "unresolved",
                                ],
                            },
                            "evidenceArtifacts": {
                                "type": "array",
                                "items": {"type": "string"},
                            },
                            "note": {"type": "string"},
                        },
                        "required": ["identity", "field", "outcome"],
                        "additionalProperties": False,
                    },
                },
            },
            "required": ["name", "rows"],
            "additionalProperties": False,
        },
        "find_in_axtree": {
            "type": "object",
            "properties": {
                "pageId": {
                    "type": "string",
                    "description": "Page id whose current DOM.getAXTree snapshot should be searched.",
                },
                "role": {
                    "type": "string",
                    "description": "Optional AX role filter, e.g. link, button, textbox. Pass \"\" for any role.",
                },
                "name": {
                    "type": "string",
                    "description": "Accessible name/text to locate. Pass \"\" to list by role only.",
                },
                "text": {
                    "type": "string",
                    "description": "Alias/fallback for name; pass \"\" unless name is empty.",
                },
                "match": {
                    "type": "string",
                    "enum": ["exact", "contains", "regex"],
                    "description": "How to match name/text.",
                },
                "case_sensitive": {"type": "boolean"},
                "interactive_only": {
                    "type": "boolean",
                    "description": "When true, only return AXTree lines marked with # (preferred actionable targets).",
                },
                "max_results": {"type": "integer", "minimum": 1, "maximum": 50},
            },
            "required": [
                "pageId",
                "role",
                "name",
                "text",
                "match",
                "case_sensitive",
                "interactive_only",
                "max_results",
            ],
            "additionalProperties": False,
        },
        "local_fs_search": {
            "type": "object",
            "properties": {
                "pattern": {
                    "type": "string",
                    "description": "Regex grep; pass an empty string to list matches by glob / event_type only.",
                },
                "glob": {
                    "type": "string",
                    "description": "Glob relative to the current task worktree, e.g. observations/*.json or **/*.json.",
                },
                "event_type": {
                    "type": ["string", "null"],
                    "description": "Only for .jsonl files: restrict the search to lines whose `type` matches this string. Pass null when not needed (searching .txt offloads, listing files, plain grep). The strings \"null\"/\"none\" are treated as null.",
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
        },
        "local_fs_read": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "line_offset": {"type": "integer", "minimum": 0},
                "line_limit": {"type": "integer", "minimum": 1, "maximum": 5000},
                "max_bytes": {"type": "integer", "minimum": 1000, "maximum": 200000},
            },
            "required": ["path", "line_offset", "line_limit", "max_bytes"],
            "additionalProperties": False,
        },
    }
