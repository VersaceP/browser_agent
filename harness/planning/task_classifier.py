"""Bounded task classification for the explicit ``browser`` CLI mode.

The classifier is a router and contract extractor, not a second LeadAgent.  It
must return one structured tool call and is never allowed to invent handles or
business values.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import replace
from typing import Any, Dict, List, Optional, Tuple

from harness.model_config import browser_agent_model_config
from harness.task_types import (
    TASK_TYPE_SCENARIOS,
    TASK_TYPE_SELECTION_RULE,
    VALID_TASK_TYPES,
)
from harness.task_control.plan_validation import VALID_STAGE_HINTS
from llm import LLMFactory
from runtime_config import ModelConfig, RuntimeConfig, _REASONING_PARAM_KEYS

JsonDict = Dict[str, Any]
_CLASSIFIER_TOOL = "classify_browser_task"
# A Fleet routing reference has one deliberately non-semantic spelling: "@"
# immediately followed by the id, with no space.  Do not infer Fleet intent
# from labels such as ``fleet_id`` or from a UUID that happens to be nearby.
_FLEET_REF_RE = re.compile(
    r"@([0-9a-f]{8}(?:-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})?)"
    r"(?![0-9a-f-])",
    re.IGNORECASE,
)
# "@" followed by something id-shaped. Used only to tell a botched sigil form
# apart from text that never tried to name a Fleet.
_FLEET_SIGIL_RE = re.compile(r"@[0-9a-f][0-9a-f-]{3,}", re.IGNORECASE)
FLEET_REFERENCE_SYNTAX = (
    "Fleet 引用只认一种写法：@ 紧跟 Fleet id，中间不要空格，例如 "
    "@2677c96a-7a2b-4119-bec8-2e56cf93a5cd（只写前 8 位 @2677c96a 也可以）。"
)


def extract_fleet_reference(task: str) -> Tuple[Optional[str], Optional[str]]:
    """Extract the one ``@<fleet-id>`` reference from user text.

    Returns ``(value, None)`` for a single canonical reference and ``(None,
    None)`` when there is none.  The only syntax error is a malformed ``@``
    reference; ordinary prose is never interpreted as Fleet routing.
    """
    text = str(task or "")
    matches = [m.group(1).lower() for m in _FLEET_REF_RE.finditer(text)]
    unique = list(dict.fromkeys(matches))
    if len(unique) > 1:
        return None, "任务里出现了多个不同的 Fleet 引用，无法判断该用哪一个。"
    for match in _FLEET_SIGIL_RE.finditer(text):
        if not _FLEET_REF_RE.fullmatch(match.group(0)):
            return None, (
                f"{match.group(0)} 不是合法的 Fleet id（要 8 位十六进制前缀或完整"
                f" UUID）。{FLEET_REFERENCE_SYNTAX}"
            )
    if unique:
        return unique[0], None
    return None, None


def _classifier_model_config(runtime: RuntimeConfig) -> ModelConfig:
    configured = runtime.task_classifier
    if configured.model_id:
        return configured.model_config()
    resolved = browser_agent_model_config(runtime.model, runtime.worker)
    extra = {
        key: value for key, value in (resolved.extra_params or {}).items()
        if key not in _REASONING_PARAM_KEYS
    }
    extra.update({"max_tokens": configured.max_tokens, "tool_choice": "required", "temperature": 0})
    return replace(
        resolved,
        extra_params=extra,
        llm_api_timeout_seconds=min(resolved.llm_api_timeout_seconds, 20.0),
        llm_timeout_max_retries=0,
        llm_timeout_backoff_seconds=0.0,
        llm_timeout_retry_interval_seconds=None,
    )


def _tool_schema() -> JsonDict:
    # Keep the classifier's deliverable contract exactly aligned with the
    # canonical direct-plan contract.  A permissive placeholder here lets the
    # classifier emit a shape that only fails later in plan compilation, which
    # makes the failure look like a dispatch problem and wastes the bounded
    # classification call.
    from harness.tools.lead_tools import _expected_artifact_schema

    return {
        "name": _CLASSIFIER_TOOL,
        "description": "Classify one browser task and extract only literal user-provided items.",
        "input_schema": {
            "type": "object",
            "properties": {
                "task_type": {"type": "string", "enum": sorted(VALID_TASK_TYPES)},
                "stage_hint": {"type": "string", "enum": sorted(VALID_STAGE_HINTS)},
                "output_contract": _expected_artifact_schema(),
                "literal_items": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "label": {"type": "string"},
                            "value": {"type": "string"},
                        },
                        "required": ["label", "value"],
                        "additionalProperties": False,
                    },
                },
            },
            "required": ["task_type", "stage_hint", "output_contract", "literal_items"],
            "additionalProperties": False,
        },
    }


def _system_prompt() -> str:
    """Classifier instructions.

    The requiredControls paragraph is load-bearing too, and for a blunter
    reason: ``form_filling`` with ``stage_hint='form_interaction'`` is the one
    combination whose contract the mechanical validator additionally requires
    (validators.py: form_interaction_missing_required_controls). The schema
    advertised the field but nothing told the classifier when it is mandatory,
    so whether a form task started at all was luck — run a686e03f produced the
    controls and dispatched, run c7c931b7 did not and died before touching the
    browser. This states the same rule the Lead prompt already carries.

    The table paragraph is load-bearing. Without it the classifier read the
    labels the task enumerates as the artifact's own columns: run a686e03f got
    ``fields``/``nonempty_fields`` listing all six item labels plus
    ``exact_rows: 6``, i.e. six rows each repeating all six labelled columns.
    The worker filled the form correctly, then had to record the extraction
    twice and landed a 13-column artifact, which burned its whole step
    extension. The lead-authored contract for the same task used four generic
    columns and ``exact_rows: 6``. Naming the column/row distinction here is
    the general fix; nothing about any site or field belongs in this prompt.
    """
    scenarios = "\n".join(f"- {k}: {v}" for k, v in sorted(TASK_TYPE_SCENARIOS.items()))
    stages = ", ".join(sorted(VALID_STAGE_HINTS))
    return (
        "You are a bounded browser-task classifier. Return exactly one "
        f"{_CLASSIFIER_TOOL} tool call. Do not plan browser actions. "
        "Choose enum values and extract every field value the user actually "
        "supplied. In literal_items the `value` MUST be copied verbatim from the "
        "task — never paraphrase, translate, complete or invent one — while "
        "`label` names the control that value belongs to and may be your own "
        "naming, exactly as in requiredControls. A value the user supplied under "
        "an implied or abbreviated label is still a supplied value: name the "
        "control yourself and copy the value. Never drop a supplied value because "
        "its label is not spelled out in the task, and return [] only when the "
        "task supplies no values at all. When you declare requiredControls, "
        "literal_items must carry a pair for every control the user gave a value "
        "for. literal_items contains only values the user asked the browser to "
        "enter, select, or upload in page business controls. A URL, Fleet id, "
        "page id, session id, or other identifier that merely selects the site, "
        "browser context, or route is navigation context, not a form value; omit "
        "it from literal_items unless the user explicitly asks to enter that "
        "same value into a page control. Never invent URLs, identifiers, values, "
        "fleet ids, page ids, selectors, or evidence. "
        "The output contract may describe only the requested deliverable.\n\n"
        "The output contract describes one table. `fields` and `nonempty_fields` "
        "name the artifact's COLUMNS, and every row carries that same set of "
        "columns. Items the task enumerates are ROWS, not columns: never turn an "
        "item's own label or value into a column name. Choose generic column "
        "names that mean the same thing on every row, and say how many items are "
        "expected with `exact_rows`.\n\n"
        "When you choose task_type=form_filling with stage_hint=form_interaction, "
        "the output contract MUST also carry `requiredControls`: one entry per "
        "control the user independently asked you to complete, each with a stable "
        "`controlKey` and its `label`. Each artifact row is then one receipt for "
        "one control, so `exact_rows`, if you set it, must equal the number of "
        "controls, and the columns are the receipt's own (controlKey, "
        "filledValue), not the controls' labels. If filling a control only "
        "enables a search or a listing extraction, that is task_type=web_search "
        "with stage_hint=collection and no `requiredControls`.\n\n"
        f"Task types:\n{scenarios}\n\n"
        f"Stage hints: {stages}\n"
        f"Selection rule: {TASK_TYPE_SELECTION_RULE}"
    )


def _squeeze_whitespace(text: Any) -> str:
    """Drop every whitespace char, including full-width U+3000.

    User-pasted task text routinely carries line-break artifacts («报 名» from
    a wrapped paste), and the classifier reliably normalizes them away — that
    is semantic correctness, not invention. Requiring every non-whitespace
    character to appear in order is the actual anti-hallucination guarantee;
    the raw substring check conflated paste artifacts with invented values and
    voided whole classifications over them (run d1f40f96).
    """
    return "".join(str(text).split())


def _validate_result(raw: Any, task: str) -> Tuple[Optional[JsonDict], Optional[str]]:
    """Structural check plus the one anti-hallucination guarantee that matters.

    The guarantee is on the VALUE: a value the classifier did not copy from the
    task is invented business data and voids the classification. The label is a
    naming choice, and requiring it verbatim too was incoherent — the same call
    is trusted to name the same control in ``requiredControls``. Run 8615032d
    paid for it: the task wrote "毕业于<school>", so no pair could be quoted
    under a "毕业院校" label and the classifier emitted one of four supplied
    values. The PlanValidator then mistook that incomplete helper index for the
    whole worker instruction even though the original prose still held all four.
    """
    if not isinstance(raw, dict):
        return None, "classifier result is not an object"
    task_type = str(raw.get("task_type") or "").strip()
    stage = str(raw.get("stage_hint") or "").strip()
    if task_type not in VALID_TASK_TYPES or stage not in VALID_STAGE_HINTS:
        return None, "classifier returned an invalid task_type or stage_hint"
    contract = raw.get("output_contract")
    if not isinstance(contract, dict):
        return None, "classifier output_contract is not an object"
    items = raw.get("literal_items")
    if not isinstance(items, list):
        return None, "classifier literal_items is not an array"
    squeezed_task = _squeeze_whitespace(task)
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            return None, f"literal_items[{index}] is not an object"
        label = _squeeze_whitespace(item.get("label"))
        value = _squeeze_whitespace(item.get("value"))
        if not label or not value:
            return None, f"literal_items[{index}] has an empty label or value"
        if value not in squeezed_task:
            return None, f"literal_items[{index}] value is not copied from the task"
    return {
        "task_type": task_type,
        "stage_hint": stage,
        "output_contract": contract,
        "literal_items": items,
    }, None


def _classifier_user_message(task: str, repair_feedback: str) -> str:
    """The task, plus the mechanical reasons its previous contract was refused.

    The feedback is fenced and named so the classifier cannot mistake it for
    part of the user's own request: literal_items must still be copied from
    the task text above it, and _validate_result keeps enforcing exactly that
    against the unchanged task.
    """
    if not repair_feedback.strip():
        return str(task)
    return (
        f"{task}\n\n"
        "<previous_attempt_rejected>\n"
        "Your previous classification compiled into a plan the harness refused "
        "for the reasons below. Classify the same task again and fix exactly "
        "these. Nothing in this block is part of the user's task: do not copy "
        "literal items out of it.\n"
        f"{repair_feedback.strip()}\n"
        "</previous_attempt_rejected>"
    )


async def classify_browser_task(
    task: str,
    runtime: RuntimeConfig,
    logger: Any,
    *,
    repair_feedback: str = "",
) -> Tuple[Optional[JsonDict], Optional[str]]:
    if not runtime.task_classifier.enabled:
        if logger is not None:
            logger.write("direct_mode.classify.fallback", {
                "reason": "task classifier is disabled in configuration",
                "dispatched": False,
            })
        return None, "task classifier is disabled in configuration"
    user_message = _classifier_user_message(str(task), str(repair_feedback or ""))
    digest = hashlib.sha256(user_message.encode("utf-8")).hexdigest()
    started = time.monotonic()
    try:
        # Config and provider construction stay inside the try: a bad
        # classifier model config (or missing SDK) must leave a fallback
        # event, not an exception that escapes without any trace in run.jsonl.
        config = _classifier_model_config(runtime)
        provider = LLMFactory.create_provider(config)
        text, calls, stop_reason, usage = await provider.generate_response(
            system_prompt=_system_prompt(),
            messages=[{"role": "user", "content": user_message}],
            tools=[_tool_schema()],
        )
        if logger is not None:
            logger.record_llm_usage(
                source="task_classifier", provider=config.provider,
                model=config.model_id, usage=usage, step=0,
                conversation_id=f"task-classifier:{digest[:16]}", context_hash=digest,
            )
    except Exception as exc:
        error = f"classifier call failed: {type(exc).__name__}: {exc}"
        if logger is not None:
            logger.write("direct_mode.classify.fallback", {
                "reason": error,
                "durationMs": int((time.monotonic() - started) * 1000),
                "dispatched": False,
            })
        return None, error
    matching = [c for c in calls or [] if isinstance(c, dict) and c.get("name") == _CLASSIFIER_TOOL]
    if len(calls or []) != 1 or len(matching) != 1:
        error = f"classifier must return exactly one {_CLASSIFIER_TOOL} call (stop={stop_reason})"
        if logger is not None:
            logger.write("direct_mode.classify.fallback", {
                "reason": error,
                "durationMs": int((time.monotonic() - started) * 1000),
                "dispatched": False,
            })
        return None, error
    result, error = _validate_result(matching[0].get("input"), task)
    if logger is not None:
        event_payload = {
            "taskType": result.get("task_type") if result else None,
            "stageHint": result.get("stage_hint") if result else None,
            "itemCount": len(result.get("literal_items") or []) if result else 0,
            "durationMs": int((time.monotonic() - started) * 1000),
            "reason": error,
            "repairAttempt": bool(str(repair_feedback or "").strip()),
        }
        if not result:
            event_payload["dispatched"] = False
        logger.write(
            "direct_mode.classify.result" if result else "direct_mode.classify.fallback",
            event_payload,
        )
    return result, error


def synthesize_direct_input(task: str, result: JsonDict) -> JsonDict:
    # Keep the immutable request visibly separate from the classifier's helper
    # index.  The index makes implied labels easier for a worker to act on, but
    # it is still model-extracted data and may be incomplete.  Run 8615032d
    # exposed the ambiguity in concatenating both as ordinary prose: the audit
    # read a one-item helper index as if it replaced the complete request above
    # it, then claimed three values had disappeared even though all three were
    # still present in worker_task.
    original = str(task).strip()
    worker_task = (
        "<original_user_task>\n"
        f"{original}\n"
        "</original_user_task>"
    )
    items = result.get("literal_items") or []
    if items:
        worker_task += (
            "\n\nThe original_user_task above is the complete, authoritative "
            "worker instruction. The classifier_literal_index below is only a "
            "control/value navigation aid. Follow every value in the original "
            "task even if the index accidentally omits it; never treat the index "
            "as permission to drop or replace user input."
            "\n\n<classifier_literal_index>\n"
            + "\n".join(f"- {item['label']} = {item['value']}" for item in items)
            + "\n</classifier_literal_index>"
        )
    payload: JsonDict = {
        "goal": original,
        "task": worker_task,
        "task_type": result["task_type"],
        "stage_hint": result["stage_hint"],
        "output_contract": result["output_contract"],
    }
    return payload


__all__ = ["classify_browser_task", "extract_fleet_reference", "synthesize_direct_input"]
