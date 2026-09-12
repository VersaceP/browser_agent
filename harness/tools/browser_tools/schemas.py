"""JSON schemas for BrowserAgent tools."""

from typing import Dict, Tuple

from harness.utils import JsonDict
from harness.workflow_policy import LISTENABLE_EVENTS
from harness.workflow_schema_source import platform_constraints, workflow_contract


def _workflow_condition_schema() -> JsonDict:
    # workflowConditionOrGroupSchema is a union of two strict objects. A bare
    # string used to be offered here and has no platform counterpart, so a
    # string condition rejects the whole workflow with -32602.
    return {
        "anyOf": [
            {"$ref": "#/$defs/workflowConditionLeaf"},
            {"$ref": "#/$defs/workflowConditionGroup"},
        ],
    }


#: Written here, not derived: the platform's op descriptions say what each op
#: does, not what it fails to do, and `find`'s silence on both a miss and a
#: crowd is the sharp edge. Kept deliberately site-neutral — an example naming
#: real labels or roles would teach one page's shape as if it were the format's.
#: The measurements behind these sentences live with the regression tests.
_TRANSFORM_OP_NOTES = {
    "find": (
        "Returns the FIRST matching item and does not check uniqueness; an"
        " empty string when nothing matches, which then fails downstream"
        " rather than here. A bare visible label is rarely unique in an AXTree"
        " — the same text appears on a control, on its label, and on any"
        " heading that mentions it. Narrow with mode=regex, combining the"
        " parts each line carries: its role, its full quoted accessible name,"
        " and its bracketed state. Read them off the tree you just captured"
        " rather than assuming which role a control uses."
    ),
    "regex": (
        "Applied to the current value as text. Use group to lift a canonical"
        " id out of a matched AXTree line: ids are"
        " frameId:axNodeId:domNodeId inside square brackets, e.g."
        " '\\[([0-9]+:[0-9]+:[0-9]+)\\]' with group 1."
    ),
    "querySelector": (
        "Matched against a simplified semantic tree by tag/class/id, not"
        " against AXTree lines and not by visible text."
    ),
}


def _execute_property(name: str) -> JsonDict:
    """The platform's schema for one top-level Workflow.execute param."""
    return workflow_contract().execute_property_schemas.get(name) or {}


def _transform_op_members() -> list:
    """Build one strict member per platform transform op.

    The platform's ops are a discriminated union of `.strict()` objects, so a
    flattened object that offers every op's fields at once lets the model write
    `{"op": "regex", "selector": ...}` — accepted here, rejected wholesale by
    the dispatcher. Property names, types, bounds and required sets therefore
    come from the platform contract; only the notes above are ours.
    """
    contract = workflow_contract()
    members = []
    for name, shape in sorted(contract.transform_ops.items()):
        properties: JsonDict = {}
        for prop in sorted(shape.properties):
            source = shape.property_schemas.get(prop) or {}
            if prop == "op":
                properties["op"] = {"type": "string", "enum": [name]}
                continue
            properties[prop] = {
                key: source[key]
                for key in ("type", "enum", "minimum", "maximum", "minLength")
                if key in source
            }
            description = source.get("description")
            if description:
                properties[prop]["description"] = str(description)
        member: JsonDict = {
            "type": "object",
            "properties": properties,
            "required": sorted(shape.required),
            "additionalProperties": False,
        }
        note = _TRANSFORM_OP_NOTES.get(name)
        if note:
            member["description"] = note
        members.append(member)
    return members


def _workflow_step_definitions() -> JsonDict:
    contract = workflow_contract()
    condition = _workflow_condition_schema()
    # Every member of the platform step union carries an optional stable id;
    # it is what failure receipts and recovery segments refer back to.
    step_id: JsonDict = {
        "type": "string",
        "minLength": 1,
        "description": "Optional stable step identifier echoed in results.",
    }
    extract_schema: JsonDict = {
        "type": "object",
        "additionalProperties": {"type": "string", "minLength": 1},
        "description": "Map workflow variable names to result/event dot paths.",
    }
    action_step: JsonDict = {
        "type": "object",
        "properties": {
            "type": {"type": "string", "enum": ["action"]},
            "id": step_id,
            "action": {
                "type": "string",
                "minLength": 1,
                "description": "ABCP action such as Page.getState.",
            },
            "params": {"type": "object", "additionalProperties": True},
            "purpose": {"type": "string", "minLength": 1},
            "extract": extract_schema,
            "onError": {
                "type": "string",
                "enum": ["stop", "continue"],
                "description": (
                    "Stop the workflow on failure (default) or record it and"
                    " continue. There is no retry setting anywhere in the"
                    " workflow language — re-observe and submit a new segment"
                    " instead."
                ),
            },
        },
        "required": ["action"],
        "additionalProperties": False,
    }
    # Mirrors the platform's workflowWaitEventStepSchema. The harness used to
    # emit {"type": "listen", "event": ...}; the dispatcher has no such step
    # type and rejects the whole workflow with -32602, so the model is shown
    # only the real spelling. Stored `listen` steps are still rewritten by
    # harness.workflow_policy._normalize_wait_events before transport.
    wait_event_step: JsonDict = {
        "type": "object",
        "properties": {
            "type": {"type": "string", "enum": ["waitEvent"]},
            "id": step_id,
            "focus": {
                "type": "array",
                "minItems": 1,
                "items": {"type": "string", "enum": sorted(LISTENABLE_EVENTS)},
                "description": (
                    "Event names to wait for AFTER the preceding Action"
                    " completes. It cannot see events emitted during that"
                    " Action's own window — the engine advances the wait cursor"
                    " past it. Use a readEvents step for those. A real page load"
                    " normally fires Page.loaded after Page.navigate returns, so"
                    " waitEvent settles navigation; pair it with readEvents when"
                    " the event may already have fired."
                ),
            },
            "pageId": {"type": "string", "minLength": 1},
            "fleetId": {"type": "string", "minLength": 1},
            "taskId": {"type": "string", "minLength": 1},
            "timeout": {
                "type": "integer",
                "minimum": 100,
                "maximum": 300000,
                "description": (
                    "Maximum wait in ms (default 30000). A timeout is not a"
                    " failure: the step returns timedOut and the workflow"
                    " continues, so assert on the extracted events if the"
                    " event is mandatory."
                ),
            },
            "extract": extract_schema,
        },
        "required": ["type", "focus"],
        "additionalProperties": False,
    }
    # readEvents reads the window of the PRECEDING Action; waitEvent only sees
    # what comes after it (the engine advances the wait cursor to that window's
    # end). Both are needed: which one settles a page depends on whether the
    # event fires before or after the Action returns.
    read_events_step: JsonDict = {
        "type": "object",
        "properties": {
            "type": {"type": "string", "enum": ["readEvents"]},
            "id": step_id,
            "focus": {
                "type": "array",
                "minItems": 1,
                "items": {"type": "string", "enum": sorted(LISTENABLE_EVENTS)},
                "description": (
                    "Event names to read from the preceding Action's own event"
                    " window. Returns immediately — it never waits."
                ),
            },
            "pageId": {"type": "string", "minLength": 1},
            "fleetId": {"type": "string", "minLength": 1},
            "taskId": {"type": "string", "minLength": 1},
            "extract": extract_schema,
        },
        "required": ["type", "focus"],
        "additionalProperties": False,
    }
    store_step: JsonDict = {
        "type": "object",
        "properties": {
            "type": {"type": "string", "enum": ["store"]},
            "id": step_id,
            "op": {
                "type": "string",
                "enum": ["set", "merge", "append", "delete"],
                "description": (
                    "append accumulates a collection across loop iterations;"
                    " the whole store is returned with the workflow result."
                ),
            },
            "path": {
                "type": "string",
                "minLength": 1,
                "description": "Dot-separated path in the workflow store.",
            },
            "value": {"description": "Required for every op except delete."},
        },
        "required": ["type", "op", "path"],
        "additionalProperties": False,
    }
    if_step: JsonDict = {
        "type": "object",
        "properties": {
            "type": {"type": "string", "enum": ["if"]},
            "id": step_id,
            "condition": condition,
            "then": {
                "type": "array",
                "items": {"$ref": "#/$defs/workflowStep"},
            },
            "else": {
                "type": "array",
                "items": {"$ref": "#/$defs/workflowStep"},
            },
        },
        "required": ["type", "condition", "then"],
        "additionalProperties": False,
    }
    loop_step: JsonDict = {
        "type": "object",
        "properties": {
            "type": {"type": "string", "enum": ["loop"]},
            "id": step_id,
            "maxIterations": {"type": "integer", "minimum": 1, "maximum": 50},
            "condition": condition,
            "body": {
                "type": "array",
                "items": {"$ref": "#/$defs/workflowStep"},
            },
        },
        "required": ["type", "maxIterations", "condition", "body"],
        "additionalProperties": False,
    }
    transform_step: JsonDict = {
        "type": "object",
        "properties": {
            "type": {"type": "string", "enum": ["transform"]},
            "id": step_id,
            "input": {
                "type": "string",
                # `^\\$.*`: the platform accepts only a reference here, and
                # "last.lines" (no $) used to pass the harness and die at the
                # dispatcher. Copied from the contract, never restated.
                **platform_constraints(
                    contract.step_shapes["transform"].property_schemas["input"],
                    "pattern",
                ),
                "description": (
                    "Workflow reference supplying the input. The roots are"
                    " $last (the PRECEDING step's result), $cache, $store and"
                    " $vars.NAME — there is no $steps[N]. To search an"
                    " observation, put this step directly after the read and"
                    " use $last.lines."
                ),
            },
            "ops": {
                "type": "array",
                "minItems": 1,
                "description": "Ordered transform operations.",
                "items": {"oneOf": _transform_op_members()},
            },
            "output": {"type": "string", "minLength": 1},
        },
        "required": ["type", "input", "ops", "output"],
        "additionalProperties": False,
    }
    return {
        "workflowConditionLeaf": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "minLength": 1},
                "operator": {
                    "type": "string",
                    "enum": [
                        "exists", "notExists", "equals", "notEquals",
                        "contains", "notContains", "matches",
                        "gt", "gte", "lt", "lte",
                    ],
                },
                "value": {},
            },
            "required": ["path", "operator"],
            "additionalProperties": False,
        },
        "workflowConditionGroup": {
            "type": "object",
            "properties": {
                "operator": {"type": "string", "enum": ["and", "or"]},
                "conditions": {
                    "type": "array",
                    "minItems": 1,
                    "items": _workflow_condition_schema(),
                },
            },
            "required": ["operator", "conditions"],
            "additionalProperties": False,
        },
        "workflowStep": {
            "oneOf": [
                action_step,
                wait_event_step,
                read_events_step,
                store_step,
                if_step,
                loop_step,
                transform_step,
            ],
        },
    }

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
                        "Optional legacy Runtime.evaluate metadata. It is never"
                        " forwarded to ABCP and does not authorize, restrict, or"
                        " classify the expression. result_mode=json and record_name"
                        " retain the legacy JSON extraction envelope."
                    ),
                    "properties": {
                        "result_mode": {
                            "type": "string",
                            "enum": ["raw", "json"],
                            "description": "Optional legacy JSON extraction envelope.",
                        },
                        "record_name": {"type": "string"},
                    },
                    "additionalProperties": True,
                },
                "content_binding": {
                    "type": "object",
                    "description": (
                        "Harness-only provisional binding for a successful"
                        " structured read. It is never forwarded to ABCP and"
                        " never certifies completion; contract-valid"
                        " record_extraction is still required. Use only a"
                        " declared content_completeness region id."
                    ),
                    "properties": {
                        "regionId": {"type": "string"},
                    },
                    "required": ["regionId"],
                    "additionalProperties": False,
                },
                "navigation_context": {
                    "type": "object",
                    "description": (
                        "Harness-only provenance for Page.create when a new"
                        " page is opened from a retained route-recovery source."
                        " It is never forwarded to ABCP. Omit for ordinary"
                        " Page.create/Page.navigate calls."
                    ),
                    "properties": {
                        "kind": {
                            "type": "string",
                            "enum": [
                                "route_recovery_new_page",
                                "route_recovery_claimed_page",
                            ],
                            "description": (
                                "route_recovery_new_page on Page.create;"
                                " route_recovery_claimed_page on the first"
                                " Page.getState against a page you claimed"
                                " from Page.list as the landing for"
                                " sourcePageId."
                            ),
                        },
                        "sourcePageId": {"type": "string"},
                    },
                    "required": ["kind", "sourcePageId"],
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
            "$defs": _workflow_step_definitions(),
            "properties": {
                # Both are `z.string().uuid()` upstream. Without the pattern a
                # page handle copied from prose reaches the dispatcher and dies
                # there instead of here.
                "pageId": {
                    "type": "string",
                    **platform_constraints(
                        _execute_property("pageId"), "pattern", "format",
                    ),
                },
                "fleetId": {
                    "type": "string",
                    **platform_constraints(
                        _execute_property("fleetId"), "pattern", "format",
                    ),
                },
                "description": {
                    "type": "string",
                    "description": (
                        "Stable bounded sequence. Example: navigate, waitEvent"
                        " on Page.loaded, Page.getState, then DOM.getAXTree."
                    ),
                },
                "variables": {"type": "object", "additionalProperties": True},
                "steps": {
                    "type": "array",
                    "minItems": 1,
                    "description": (
                        "Ordered action/waitEvent/readEvents/store/if/loop/"
                        "transform steps. Minimal"
                        " example: [{\"action\":\"Page.getState\","
                        "\"purpose\":\"Confirm current page\"}]."
                    ),
                    "items": {"$ref": "#/$defs/workflowStep"},
                },
                # Workflow.execute declares exactly description/steps/
                # variables/pageId/fleetId/timeout. Its action schema is not
                # strict, so `stepTimeout` and `errorConfig` used to be offered
                # here, travelled, and were dropped in silence: a live probe
                # sent stepTimeout=1000 against a step that then ran 5005 ms.
                # Per-step bounds live on waitEvent.timeout alone.
                "timeout": {
                    "type": "integer",
                    "minimum": 1000,
                    "maximum": 600000,
                    "description": (
                        "Total workflow budget in ms. There is no per-step"
                        " timeout and no retry config; a step that must not"
                        " run long needs its own waitEvent timeout."
                    ),
                },
            },
            "required": ["pageId", "fleetId", "description", "variables", "steps", "timeout"],
            "additionalProperties": False,
        },
        "navigate_verified": {
            "type": "object",
            "properties": {
                "pageId": {"type": "string"},
                "url": {"type": "string"},
                "expectedUrlPattern": {
                    "type": "string",
                    "description": (
                        "Regex searched against the final URL. Pass \"\" to accept"
                        " the requested URL itself (compared after normalizing"
                        " host case, default port, and a bare trailing slash)."
                        " Write it as a plain regex: \"1688\\\\.com\" in JSON means"
                        " a literal backslash and can never match a URL."
                    ),
                },
                "expectedTitlePattern": {
                    "type": "string",
                    "description": "Optional regex for final title; pass \"\" to skip title check.",
                },
                "timeoutSeconds": {"type": "number"},
                "maxRetries": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 3,
                    "description": (
                        "Deprecated: read as maxStateChecks. This tool always"
                        " dispatches exactly one Page.navigate; retries never"
                        " re-request the URL."
                    ),
                },
                "maxStateChecks": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 10,
                    "description": (
                        "Total Page.getState reads allowed while the page is"
                        " still settling or redirecting. Read-side only; it"
                        " never re-navigates."
                    ),
                },
            },
            # maxRetries is deprecated and maxStateChecks defaults to 5, so
            # neither is required: a caller must not have to send a field whose
            # documented meaning no longer exists.
            "required": [
                "pageId",
                "url",
                "expectedUrlPattern",
                "expectedTitlePattern",
                "timeoutSeconds",
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
                    "description": (
                        "Native close-control/Escape ladder attempts (1-5)."
                        " Pass 0 for default (3)."
                    ),
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
                    "description": (
                        "CSS selector for one single-level homogeneous repeated"
                        " item set. Probe an unknown site before supplying it;"
                        " nested lists are not fully supported."
                    ),
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
                "containerId": {"type": "string", "description": "Canonical id of the one probed scroll container. Pass \"\" for viewport."},
                "containerSelector": {"type": "string", "description": "CSS selector for the one probed scroll container fallback. Pass \"\" if unused."},
                "loadMoreId": {"type": "string", "description": "Canonical id of the load-more button (click_load_more mode). Pass \"\" if unused."},
                "loadMoreSelector": {"type": "string", "description": "CSS selector for the one probed load-more control (fallback). Pass \"\" if unused."},
                "targetCount": {"type": "integer", "description": "Stop once this many unique rows are collected. Pass 0 for no target."},
                "maxRounds": {"type": "integer", "description": "Max expansion rounds (1-50). Pass 0 for default 12."},
                "stabilityThreshold": {"type": "integer", "description": "Consecutive no-new-row rounds before stopping. Pass 0 for default 3."},
                "settleMs": {"type": "integer", "description": "Wait after each expansion before harvesting. Pass 0 for default 600."},
                "harvestLimit": {"type": "integer", "description": "Max rows read per harvest window. Pass 0 for default 200."},
                "harvestMaxWindows": {"type": "integer", "description": "Max harvest windows per round (paging a large DOM list). Pass 0 for default 10 (=2000 rows/round)."},
                "maxDurationMs": {
                    "type": "integer",
                    "minimum": 5000,
                    "maximum": 300000,
                    "description": "Hard collection budget in ms (5000-300000). Omit for default 120000.",
                },
                "recordName": {
                    "type": "string",
                    "description": "If set, the collected rows are persisted via record_extraction under this name. Pass \"\" to only return a summary.",
                },
                "collectionField": {
                    "type": "string",
                    "description": (
                        "Optional outer-row field receiving the collected rows"
                        " (for example reviews). When set, recordName and"
                        " baseRowRef are required; fields describes each nested item."
                    ),
                },
                "regionId": {
                    "type": "string",
                    "description": (
                        "Optional content_completeness expected-region id for"
                        " this collection. Omit only when collectionField uniquely"
                        " matches the region id or one of its declared field aliases."
                        " Ambiguous/unmatched collections require an explicit regionId;"
                        " unbound unique-region inference is telemetry-only."
                    ),
                },
                "baseRowRef": {
                    "type": "object",
                    "description": (
                        "Trusted outer row from a validated upstream extraction"
                        " artifact. Only collectionField is injected/overwritten."
                    ),
                    "properties": {
                        "savedPath": {"type": "string"},
                        "rowIndex": {"type": "integer", "minimum": 0},
                    },
                    "required": ["savedPath", "rowIndex"],
                    "additionalProperties": False,
                },
            },
            "required": ["pageId", "selector"],
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
                    "description": "action_outcome | validator_failure | overlay_check | captcha_check | layout_check | overlay_adjudicate (runtime-owned classification of an occluded target; returns presentation, purpose, target accessibility and a safe recovery recommendation; it does not authorize a login or payment action) | visual_locate (locate a target the structured surfaces cannot name, described in `expected.target`. Returns ONE of: `resolvedId` — the pixel was promoted to a durable canonical id for the AX node that covered it; that says where the node is, not what it is, so act on it with a method the node's own role supports and the live schema accepts (Input.select needs the CONTROL, not an option id); `cssPoint` — the later AX observation has no matching bbox, so capture scale/origin produced a viewport CSS point. This proves coordinate mapping, not current interactability or hit identity; the unmatched bbox can be a structured blind spot or a state change. Check current evidence for whether the target remains usable before deciding at most ONE Page.click{pageId,x,y}, then re-observe. Never persist the point or reuse it after the page changes; or `coordinateRefused` — the capture geometry could not be proven, so re-observe and act on an id. `visualTargetEvidence` reports observation provenance and leaves screenshot-to-click state continuity unverified. A `consequential` field means the target reads as submit/pay/delete/sign-in: locating it does not authorize performing it) | contract_verify (judge structured visual_checks in `expected.visual_checks`; returns satisfied/violated/uncertain + failed_checks). Calls with repair_targets automatically use the internal repair_absence mode and return absent/present/uncertain.",
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
                    "default": "",
                    "description": "Regex grep; pass an empty string to list matches by glob / event_type only.",
                },
                "glob": {
                    "type": "string",
                    "default": "**/*",
                    "description": "Glob relative to the current task worktree, e.g. observations/*.json or **/*.json.",
                },
                "event_type": {
                    "type": ["string", "null"],
                    "default": None,
                    "description": "Only for .jsonl files: restrict the search to lines whose `type` matches this string. Pass null when not needed (searching .txt offloads, listing files, plain grep). The strings \"null\"/\"none\" are treated as null.",
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
        },
        "local_fs_read": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "savedPath from the offloaded receipt."},
                "line_offset": {
                    "type": "integer", "minimum": 0, "default": 0,
                    "description": (
                        "First line to read. Continue a paged read from the"
                        " previous receipt's nextLineOffset; do not re-derive it."
                    ),
                },
                "line_limit": {
                    "type": "integer", "minimum": 1, "maximum": 5000, "default": 200,
                    "description": (
                        "Lines to ask for. This is a ceiling, not a budget: the"
                        " read stops at line_limit OR max_bytes, whichever comes"
                        " first, so max_bytes is what actually bounds the"
                        " response. Ask for the whole region you need (up to"
                        " totalLines - line_offset) and let the byte budget cut"
                        " it; the receipt reports truncated and nextLineOffset"
                        " so you never have to guess how many lines fit. A small"
                        " fixed window here just turns one read into five."
                    ),
                },
                "max_bytes": {
                    "type": "integer", "minimum": 1000, "maximum": 200000, "default": 20000,
                    "description": (
                        "The real bound on one read. Raise it for a large region"
                        " instead of lowering line_limit and paging repeatedly."
                    ),
                },
            },
            "required": ["path", "line_offset", "line_limit", "max_bytes"],
            "additionalProperties": False,
        },
        "read_harness_guide": {
            "type": "object",
            "properties": {
                "guide_id": {
                    "type": "string",
                    "description": (
                        "An id from <available_harness_guides>. This reads a "
                        "versioned Harness operating guide, not a task file."
                    ),
                },
                "line_offset": {
                    "type": "integer",
                    "minimum": 0,
                    "default": 0,
                },
                "line_limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 500,
                    "default": 200,
                },
            },
            "required": ["guide_id", "line_offset", "line_limit"],
            "additionalProperties": False,
        },
        "search_harness_guides": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": (
                        "Plain text: an error/reason code from a receipt, a "
                        "method name, or a phrase in any language. Not a regex."
                    ),
                },
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 5,
                    "default": 5,
                },
            },
            "required": ["query", "limit"],
            "additionalProperties": False,
        },
    }
