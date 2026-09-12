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
# The trailing negative lookahead makes a longer/malformed hex run (for
# example a UUID with an extra group) fail to match at all instead of
# silently truncating to a valid-looking prefix.
_FLEET_LABEL_RE = re.compile(
    r"(?ix)(?:fleet(?:\s*[_-]?\s*id)?|fleet\s+uuid)\s*"
    r"(?:[:=]|-\s*)?\s*"
    r"([0-9a-f]{8}(?:-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})?)"
    r"(?![0-9a-f-])"
)


def extract_fleet_reference(task: str) -> Tuple[Optional[str], Optional[str]]:
    """Extract one explicitly labelled Fleet UUID/prefix from user text.

    Unlabelled UUIDs remain business data. Multiple distinct labelled values or
    malformed labelled references are rejected by the caller.
    """
    text = str(task or "")
    matches = [m.group(1).lower() for m in _FLEET_LABEL_RE.finditer(text)]
    # A labelled reference that is neither an 8-hex prefix nor a full UUID
    # (malformed tail, extra groups) must be rejected, not silently dropped
    # by the non-matching regex above.
    malformed = re.search(
        r"(?ix)(?:fleet(?:\s*[_-]?\s*id)?|fleet\s+uuid)\s*"
        r"(?:[:=]|-\s*)?\s*"
        r"[0-9a-f-]{8,}",
        text,
    )
    if malformed and not matches:
        return None, "malformed fleet reference in task text"
    unique = list(dict.fromkeys(matches))
    if len(unique) > 1:
        return None, "multiple fleet references in task text"
    return (unique[0], None) if unique else (None, None)


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
        "Choose enum values, copy literal label/value pairs only when they occur "
        "verbatim in the user task, and return [] otherwise. Never invent URLs, "
        "identifiers, values, fleet ids, page ids, selectors, or evidence. "
        "The output contract may describe only the requested deliverable.\n\n"
        "The output contract describes one table. `fields` and `nonempty_fields` "
        "name the artifact's COLUMNS, and every row carries that same set of "
        "columns. Items the task enumerates are ROWS, not columns: never turn an "
        "item's own label or value into a column name. Choose generic column "
        "names that mean the same thing on every row, and say how many items are "
        "expected with `exact_rows`.\n\n"
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
        if label not in squeezed_task:
            return None, f"literal_items[{index}] label is not copied from the task"
        if value not in squeezed_task:
            return None, f"literal_items[{index}] value is not copied from the task"
    return {
        "task_type": task_type,
        "stage_hint": stage,
        "output_contract": contract,
        "literal_items": items,
    }, None


async def classify_browser_task(task: str, runtime: RuntimeConfig, logger: Any) -> Tuple[Optional[JsonDict], Optional[str]]:
    if not runtime.task_classifier.enabled:
        if logger is not None:
            logger.write("direct_mode.classify.fallback", {
                "reason": "task classifier is disabled in configuration",
                "dispatched": False,
            })
        return None, "task classifier is disabled in configuration"
    digest = hashlib.sha256(str(task).encode("utf-8")).hexdigest()
    started = time.monotonic()
    try:
        # Config and provider construction stay inside the try: a bad
        # classifier model config (or missing SDK) must leave a fallback
        # event, not an exception that escapes without any trace in run.jsonl.
        config = _classifier_model_config(runtime)
        provider = LLMFactory.create_provider(config)
        text, calls, stop_reason, usage = await provider.generate_response(
            system_prompt=_system_prompt(),
            messages=[{"role": "user", "content": str(task)}],
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
        }
        if not result:
            event_payload["dispatched"] = False
        logger.write(
            "direct_mode.classify.result" if result else "direct_mode.classify.fallback",
            event_payload,
        )
    return result, error


def synthesize_direct_input(task: str, result: JsonDict, fleet_reference: Optional[str] = None) -> JsonDict:
    worker_task = str(task).strip()
    items = result.get("literal_items") or []
    if items:
        worker_task += "\n\n用户明确列出的字段和值（逐字复制）：\n" + "\n".join(
            f"- {item['label']} = {item['value']}" for item in items
        )
    payload: JsonDict = {
        "goal": str(task).strip(),
        "task": worker_task,
        "task_type": result["task_type"],
        "stage_hint": result["stage_hint"],
        "output_contract": result["output_contract"],
    }
    if fleet_reference:
        payload["worker_contract"] = {"fleet_id": fleet_reference}
    return payload


__all__ = ["classify_browser_task", "extract_fleet_reference", "synthesize_direct_input"]
