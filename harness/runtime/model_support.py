"""Shared model-call, context, result projection and event helpers."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, List, Optional, Set, Tuple
from harness.context.compaction import compact_messages_if_needed, estimate_prompt_tokens
from harness.messages.convert import to_model_messages
from runtime_config import HarnessConfig, RuntimeConfig
from harness.constants import CONTEXT_LIMIT_ERROR_MARKERS, WORKER_STATUS_INCOMPLETE
from harness.context.offload import fold_tool_results_after_moderation, offload_large_tool_result, preserve_complete_tool_payload
from harness.evidence.file_evidence import saved_paths_from_value
from harness.prompts import guide_manifest
from harness.prompts import guide_registry_errors
from harness.utils import JsonDict, RunLogger, strip_llm_hidden_fields, trim_large_strings
from llm import BaseLLMProvider, LLMRateLimitError, input_moderation_rejection
from llm.adapters import tool_result_image_accounting

def _guide_manifest_for(
    audience: str,
    logger: Any,
    *,
    exclude_ids: Optional[Set[str]] = None,
) -> str:
    """Render the manifest, and say so when the guide corpus is degraded.

    A guide that fails to load drops out of the manifest silently, so without
    this the only symptom is a model that never reads guidance it was supposed
    to have. The count goes to the run log rather than into the prompt: the
    model cannot repair a guide file, and a person can.
    """

    errors = guide_registry_errors()
    if errors and logger is not None and hasattr(logger, "write"):
        logger.write("prompt.guides.degraded", {
            "audience": audience,
            "invalidCount": len(errors),
            "errors": errors[:5],
        })
    return guide_manifest(audience, exclude_ids=exclude_ids)


TRUNCATION_STREAK_LIMIT = 3


INFRA_STREAK_INCIDENTS = frozenset({"connection", "timeout", "protocol"})


INFRA_STREAK_LIMIT = 5


MODEL_TIMEOUT_ATTEMPT_LIMIT = 5


def llm_rate_limit_terminal_result(exc: LLMRateLimitError) -> JsonDict:
    """Stable host/UI payload for provider throttling that ended this run."""
    incident = exc.to_payload()
    blocker_type = (
        "llm_quota_exhausted"
        if exc.kind == "quota_exhausted"
        else "llm_rate_limited"
    )
    if exc.kind == "quota_exhausted":
        detail = "The configured LLM allocation quota is exhausted."
    else:
        detail = "The configured LLM provider is temporarily rate limited."
    if exc.reset_at:
        detail += f" Provider reset time: {exc.reset_at}."
    elif exc.retry_after_seconds is not None:
        detail += f" Retry after {exc.retry_after_seconds:g} seconds."
    return {
        "status": WORKER_STATUS_INCOMPLETE,
        "blockers": [{
            "type": blocker_type,
            "detail": detail,
        }],
        "providerIncident": incident,
    }


def _effective_streak_limit(streak_kinds: List[str]) -> int:
    """Strict limit unless EVERY turn in the streak was an infrastructure fault.

    `all` rather than "look at the latest": a streak of three dropped
    connections is one story, but three dropped connections followed by an
    empty turn is a different one, and the mixed case must not inherit the
    lenient budget just because the newest entry happens to be transport.
    """
    if not streak_kinds:
        return TRUNCATION_STREAK_LIMIT
    if all(kind in INFRA_STREAK_INCIDENTS for kind in streak_kinds):
        return INFRA_STREAK_LIMIT
    return TRUNCATION_STREAK_LIMIT


_STABLE_BROWSER_METHOD_PREFIXES = (
    "System.get",
    "System.list",
    "System.describe",
    "DOM.get",
    "Download.list",
    "Memory.get",
    "Memory.list",
    "Bookmark.list",
    "History.list",
)


_STABLE_BROWSER_METHODS = {
    "Page.getState",
    "Page.list",
    "Page.screenshot",
}


_STATE_BOUNDARY_HARNESS_TOOLS = {
    "navigate_verified",
    "dismiss_overlay",
    "collect_items",
    "execute_published_skill_workflow",
    "execute_browser_workflow",
    "execute_saved_browser_workflow",
    "request_step_extension",
}


_EXTENSION_HANDOFF_HINT = (
    "spend the remaining steps on the handoff: persist whatever you have"
    " already gathered (record_extraction when there are collected rows),"
    " then use final_answer to state what is done, what remains, and where"
    " the next worker should resume."
)


def _webcross_behavioral_guide(guide: str) -> str:
    """Return the browser-behavior sections of the live WebCross guide.

    The platform guide also documents CLI/MCP/WebSocket connection setup and
    durable event-cursor ownership.  Those are process concerns that this
    harness implements before the worker starts, so exposing them as worker
    instructions would advertise operations the worker cannot perform.  The
    numbered sections hold the shared browser protocol rules instead.
    """
    text = str(guide or "").strip()
    start_marker = "## 1. Action Feedback"
    start = text.find(start_marker)
    if start < 0:
        return ""
    # Only the numbered protocol sections belong in the worker prompt. The
    # following workflow appendix can change title across platform releases.
    appendix = re.search(r"^## (?!\d+\.\s)", text[start:], re.MULTILINE)
    end = start + appendix.start() if appendix else len(text)
    behavioral = text[start:end].strip()

    # Section 3 begins with transport-specific event delivery and durable
    # cursor instructions.  The worker has no events.read/checkpoint surface;
    # keeping those lines would turn harness-owned recovery into a false action
    # plan.  Keep the notification and page-lifecycle rules that follow them.
    lifecycle_heading = "## 3. Events and Page Lifecycle"
    event_rule = "Event names are notifications, not Actions."
    lifecycle_start = behavioral.find(lifecycle_heading)
    event_rule_start = behavioral.find(event_rule, lifecycle_start)
    if lifecycle_start >= 0 and event_rule_start >= 0:
        behavioral = (
            behavioral[:lifecycle_start]
            + lifecycle_heading
            + "\n\n"
            + behavioral[event_rule_start:]
        )

    # The guide points at its own `references/*.md` companions. Only
    # workflow-orchestration.md is reachable as an Action (Workflow.getGuide);
    # the rest ship with the platform's skill bundle and this worker has no
    # surface that can read a file from it. Left alone, those lines are an
    # instruction to do something impossible, and a worker that tries burns a
    # step discovering there is no such call. Redirect each one to the harness
    # guide that already covers the same ground.
    reference_redirects = (
        ("references/select.md", "read_harness_guide id=browser.select-recovery"),
    )
    notes = [
        f"The source guide's `{reference}` is not reachable from this harness;"
        f" use {replacement} instead, which covers the same ground."
        for reference, replacement in reference_redirects
        if reference in behavioral
    ]
    if notes:
        behavioral = behavioral + "\n\n" + "\n".join(notes)
    return behavioral.strip()


def _tool_call_state_boundary(
    tool_call: JsonDict,
    result: Optional[JsonDict] = None,
) -> bool:
    """Conservative same-turn barrier classification.

    Calls not known to be stable reads end the pre-generated tool batch.  The
    next model turn must inspect their result before constructing more calls.
    """
    name = str(tool_call.get("name") or "").strip()
    if name == "record_extraction":
        return bool(
            isinstance(result, dict)
            and (
                result.get("browserStateMayHaveChanged") is True
                or result.get("requiresModelReplan") is True
            )
        )
    if name in _STATE_BOUNDARY_HARNESS_TOOLS:
        return True
    tool_input = tool_call.get("input") if isinstance(tool_call.get("input"), dict) else {}
    method = str(tool_input.get("method") or "").strip() if name == "browser_call" else name
    if not method or "." not in method:
        return False
    if method in _STABLE_BROWSER_METHODS:
        return False
    if any(method.startswith(prefix) for prefix in _STABLE_BROWSER_METHOD_PREFIXES):
        return False
    return True


def _deferred_tool_result(
    tool_call: JsonDict,
    *,
    after_tool_call: JsonDict,
    reason: str,
    workflow_enabled: bool = False,
) -> JsonDict:
    """Tell the model what to do with a call that was not dispatched.

    A browser action can invalidate the handles of everything queued behind it,
    so only the first one in a batch runs. Telling the model to "regenerate this
    call next turn" is correct but expensive: task de60b7f4 spent 10 of its 58
    steps re-issuing clicks that had been deferred this way, including two
    independent checkboxes that could never have invalidated each other.

    When Workflow execution is available there is a cheaper answer than a turn
    per deferred call: submit the sequence as one segment and let the platform
    run it in order, stopping at the first failure. That advice is only offered
    for browser work — a deferred harness-local tool has nothing to do with it.
    """
    deferred_tool = str(tool_call.get("name") or "")
    browser_side = deferred_tool in {"browser_call", "navigate_verified"}
    if workflow_enabled and browser_side:
        instruction = (
            "This call was NOT dispatched: the preceding browser action may have"
            " changed the page, so anything queued behind it is held back."
            " Re-issuing one call per turn is the expensive way to recover."
            " If the remaining actions are a sequence you have already decided"
            " on, submit them as ONE execute_browser_workflow segment instead:"
            " the platform runs them in order against the live page and stops at"
            " the first failure, and the receipt reports which steps ran. A"
            " segment can read a fresh DOM.getAXTree artifact through the"
            " complete `$cache.observation` or `$last` reference and use"
            " transform to extract the newly rendered option id. End the"
            " segment only when the next decision needs model judgment or"
            " another Harness-only capability."
        )
    else:
        instruction = (
            "Inspect the preceding tool result and regenerate this call in"
            " the next model turn with fresh page state and handles."
        )
    return {
        "type": "tool_result",
        "tool_use_id": tool_call.get("id"),
        "content": json.dumps({
            "status": "deferred_due_to_state_change",
            "tool_was_executed": False,
            "deferredTool": tool_call.get("name"),
            "afterTool": after_tool_call.get("name"),
            "reason": reason,
            "next_instruction": instruction,
        }, ensure_ascii=False),
    }






def _expire_multimodal_image_blocks(messages: List[JsonDict]) -> int:
    """Remove screenshot pixels after one successful model observation.

    A captured screenshot is useful as current-turn visual evidence. Retaining
    its base64 body in the transcript would re-send the same pixels on every
    later inference and make context cost grow with browser history. The text
    receipt remains, with an explicit expiry note, so future turns know to
    capture a fresh image instead of assuming they can still inspect it.
    """

    expired = 0
    for message in messages:
        if not isinstance(message, dict) or message.get("role") != "user":
            continue
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict) or block.get("type") != "tool_result":
                continue
            result_content = block.get("content")
            if not isinstance(result_content, list):
                continue
            retained = [
                item for item in result_content
                if not (isinstance(item, dict) and item.get("type") == "image")
            ]
            removed = len(result_content) - len(retained)
            if not removed:
                continue
            retained.append({
                "type": "text",
                "text": (
                    "[Screenshot pixels were attached only to the immediately "
                    "preceding model request and have expired. Capture a fresh "
                    "screenshot if visual evidence is needed again.]"
                ),
            })
            block["content"] = retained
            expired += removed
    return expired


def _pending_multimodal_image_count(messages: List[JsonDict]) -> int:
    """Count image blocks that the next model call still needs to inspect."""

    count = 0
    for message in messages:
        if not isinstance(message, dict) or message.get("role") != "user":
            continue
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict) or block.get("type") != "tool_result":
                continue
            result_content = block.get("content")
            if isinstance(result_content, list):
                count += sum(
                    1 for item in result_content
                    if isinstance(item, dict) and item.get("type") == "image"
                )
    return count


def _first_pending_multimodal_image_pair(messages: List[JsonDict]) -> int:
    """Find the assistant/tool-result pair required by the next image request."""
    for index, message in enumerate(messages):
        if _pending_multimodal_image_count([message]) == 0:
            continue
        if index:
            previous = messages[index - 1]
            role = previous.get("role") if isinstance(previous, dict) else getattr(previous, "role", None)
            if role == "assistant":
                return index - 1
        return index
    return len(messages)


def _tool_result_image_accounting(agent: Any) -> str:
    """How this agent's transport counts an image kept in a tool result.

    Only providers that encode through ``llm.adapters`` have a resolvable
    layout; external legacy providers keep the vision assumption.
    """
    provider = getattr(agent, "provider", None)
    config = getattr(provider, "config", None)
    if not isinstance(provider, BaseLLMProvider) or config is None:
        return "vision"
    return tool_result_image_accounting(config)


async def _compact_before_multimodal_request(
    agent: Any,
    *,
    step: int,
    system_prompt: str,
    messages: List[JsonDict],
    tools: List[JsonDict],
    force_reason: Optional[str],
) -> List[JsonDict]:
    """Compact historical text without giving the text compactor image bytes."""
    image_accounting = _tool_result_image_accounting(agent)
    max_steps = getattr(agent, "effective_max_steps", None)
    if isinstance(max_steps, int) and max_steps > 0 and max_steps - step + 1 <= 5:
        # The current round has not called the model yet. Extensions change
        # effective_max_steps, so consult the live cap rather than the base cap.
        estimated_tokens = estimate_prompt_tokens(
            system_prompt, messages, tools,
            tool_result_image_accounting=image_accounting,
        )
        context_window = max(1, int(agent.runtime.harness.model_context_window_tokens))
        if estimated_tokens < context_window:
            agent._write_agent_event("agent.compaction_skipped", {
                "step": step,
                "remainingSteps": max(0, max_steps - step + 1),
                "reason": "near_step_cap",
                "estimatedTokens": estimated_tokens,
                "deferredForceReason": force_reason,
            })
            return messages
        # A request already estimated to exceed the window is not routine
        # compaction: skipping it would simply terminate this worker.
        force_reason = force_reason or "context_window_pressure"
    protected_start = _first_pending_multimodal_image_pair(messages)
    if protected_start >= len(messages):
        return await compact_and_track_prefix_rebuild(
            agent, actor="browser_agent", step=step, system_prompt=system_prompt,
            messages=messages, tools=tools, force_reason=force_reason,
        )

    prefix, protected = messages[:protected_start], messages[protected_start:]
    threshold = int(
        max(1, int(agent.runtime.harness.model_context_window_tokens))
        * max(0.1, min(float(agent.runtime.harness.context_compaction_threshold_ratio), 0.95))
    )
    combined_tokens = estimate_prompt_tokens(
        system_prompt, messages, tools,
        tool_result_image_accounting=image_accounting,
    )
    compact_reason = force_reason
    if combined_tokens > threshold and not compact_reason:
        compact_reason = "multimodal_pending_attachment"
    if not prefix:
        agent._write_agent_event("agent.multimodal_compaction_deferred", {
            "step": step,
            "imageCount": _pending_multimodal_image_count(messages),
            "deferredForcedCompaction": bool(force_reason),
            "reason": "no_compactable_history",
        })
        return messages
    compacted_prefix = await compact_and_track_prefix_rebuild(
        agent, actor="browser_agent", step=step, system_prompt=system_prompt,
        messages=prefix, tools=tools, force_reason=compact_reason,
    )
    return compacted_prefix + protected




def _is_context_limit_exception(exc: BaseException) -> bool:
    """Provider-agnostic detection of model-context-window errors.

    Matches on the error message rather than on a specific SDK exception class
    so that swapping providers doesn't silently drop this signal.
    """
    msg = str(exc or "").lower()
    if not msg:
        return False
    return any(marker in msg for marker in CONTEXT_LIMIT_ERROR_MARKERS)


async def generate_response_surviving_moderation(
    *,
    provider: BaseLLMProvider,
    logger: RunLogger,
    actor: str,
    step: int,
    system_prompt: str,
    messages: List[JsonDict],
    tools: List[JsonDict],
    max_folds: int = 1,
    return_result: bool = False,
):
    """Call the provider, surviving an input-moderation refusal once.

    A content filter that refuses what we SENT is the one 400 worth acting on.
    It is not retryable as-is — resending the same bytes is refused again, so
    the timeout/connection ladder in ``llm.base`` cannot help — and letting it
    escape ends the agent with `rowCount: 0` and no trace file at all, even
    when its work is already finished on disk. The conversation owner is the
    only layer that can change the request, so fold the bulk the harness itself
    contributed and ask once more.

    Any other 400 re-raises untouched: folding a tool result cannot repair a
    malformed request, and a refusal that survives the fold must stay visible
    so the caller's existing containment ends the step honestly.
    """
    folds = 0
    while True:
        from llm.legacy import request_from_legacy
        request = request_from_legacy(system_prompt, messages, tools)
        try:
            if isinstance(provider, BaseLLMProvider):
                result = await provider.generate(request)
                return result if return_result else result.legacy_fields(include_prefix=True)
            # External legacy providers can migrate independently.
            legacy = await provider.generate_response(
                system_prompt=system_prompt, messages=to_model_messages(messages), tools=tools,
            )
            if return_result:
                from llm.legacy import result_from_legacy
                return result_from_legacy(legacy)
            return legacy
        except Exception as exc:
            marker = input_moderation_rejection(exc)
            if marker is None or folds >= max_folds:
                raise
            receipt = fold_tool_results_after_moderation(messages, reason=marker)
            if receipt is None:
                # Nothing bulky enough to be the plausible trigger, so a retry
                # would resend the same bytes: let the refusal stand.
                raise
            folds += 1
            logger.write(f"{actor}.model_input_moderation_folded", {
                "step": step,
                "attempt": folds,
                "maxFolds": max_folds,
                "marker": marker,
                "error": str(exc)[:500],
                **receipt,
            })


def offload_tool_result_for_model(
    *,
    logger: RunLogger,
    runtime: RuntimeConfig,
    tool_call: JsonDict,
    result: Any,
    step: int,
) -> Any:
    model_result = strip_llm_hidden_fields(result)
    # Two independent limits act on a tool result and they do not agree: whole
    # results move to disk above tool_result_offload_threshold_bytes (50 KB by
    # default), while individual strings are cut at max_observation_chars
    # (24 K) on the way into the model message. A 30 KB string is under the
    # first and over the second, so it used to be trimmed with no complete copy
    # kept anywhere. Preserving first closes that band.
    complete = preserve_complete_tool_payload(
        logger=logger,
        tool_name=str(tool_call.get("name") or "tool"),
        result=model_result,
        step=step,
        prefix=runtime.agent_id,
        projection_limit=int(
            getattr(runtime.harness, "max_observation_chars", 0) or 24000
        ),
    )
    projected = offload_large_tool_result(
        logger=logger,
        tool_name=str(tool_call.get("name") or "tool"),
        result=model_result,
        step=step,
        prefix=runtime.agent_id,
        threshold_bytes=runtime.harness.tool_result_offload_threshold_bytes,
    )
    if not complete:
        return projected
    if isinstance(projected, dict) and "_offloaded" in projected:
        # Already moved to disk whole; a second pointer would be noise.
        return projected
    notice = {key: value for key, value in complete.items() if value is not None}
    if isinstance(projected, dict):
        return {**projected, "_truncation": notice}
    # A bare string or list is a legitimate tool result, and it is about to be
    # cut at max_observation_chars. Attaching the notice to a dict was the only
    # branch implemented, so those results were preserved on disk and the model
    # was never told where. Wrapping is a shape change, but it only happens to
    # a value that was going to reach the model incomplete either way.
    return {"result": projected, "_truncation": notice}


def _json_size_bytes(value: Any) -> int:
    try:
        return len(
            json.dumps(value, ensure_ascii=False, default=str).encode("utf-8")
        )
    except (TypeError, ValueError):
        return 0


def _is_offload_stub(value: Any) -> bool:
    """True only for a harness-produced offload receipt, not arbitrary data."""
    return bool(
        isinstance(value, dict)
        and value.get("_offloaded") is True
        and isinstance(value.get("originalBytes"), int)
        and isinstance(value.get("savedPath"), str)
        and value.get("savedPath")
    )


def _offload_stub_stats(value: Any) -> Tuple[int, int]:
    """Count genuine offload stubs and their declared originalBytes.

    Field-level stubs (AXTree lines, Runtime.evaluate values, ...) embed
    {_offloaded: true, originalBytes: N} inside otherwise-inline results;
    whole-result stubs carry the same keys at the top level.
    """
    count = 0
    original = 0
    stack = [value]
    while stack:
        item = stack.pop()
        if isinstance(item, dict):
            if _is_offload_stub(item):
                count += 1
                declared = item.get("originalBytes")
                original += declared
            stack.extend(item.values())
        elif isinstance(item, list):
            stack.extend(item)
    return count, original


def _offload_file_category(model_result: Any, logger: RunLogger) -> Optional[str]:
    """Category of nested/whole offload paths, without logging any path.

    Inline browser results carry field stubs below response.data, whereas a
    whole-result offload carries its stub at the root.  Walk both shapes.  A
    mixed category is explicit rather than choosing a misleading first path.
    """
    categories: Set[str] = set()
    stack = [model_result]
    task_dir = Path(logger.task_dir).resolve()
    while stack:
        item = stack.pop()
        if isinstance(item, dict):
            if _is_offload_stub(item):
                saved = str(item.get("savedPath") or "")
                saved_path = Path(saved)
                resolved = (
                    saved_path.resolve()
                    if saved_path.is_absolute()
                    else (task_dir / saved_path).resolve()
                )
                try:
                    relative = resolved.relative_to(task_dir)
                except (ValueError, OSError):
                    pass
                else:
                    if relative.parts:
                        categories.add(relative.parts[0])
            stack.extend(item.values())
        elif isinstance(item, list):
            stack.extend(item)
    if not categories:
        # local_fs tools echo the file they served as a top-level
        # relativePath ("observations/x.json"); its category segment answers
        # read-back analysis (repeated payload reads vs artifact probes)
        # without logging the path.
        for source in (
            model_result if isinstance(model_result, list) else [model_result]
        ):
            if isinstance(source, dict):
                relative = str(source.get("relativePath") or "")
                if relative:
                    parts = Path(relative).parts
                    if parts:
                        return parts[0]
        return None
    if len(categories) > 1:
        return "mixed"
    return next(iter(categories))


def log_model_visible_tool_result(
    logger: RunLogger,
    *,
    actor: str,
    step: int,
    tool_name: str,
    raw_result: Any,
    model_result: Any,
    final_content: Any,
    worker_id: str = "",
    method: str = "",
    image_attachment: Optional[JsonDict] = None,
) -> None:
    """Observability: the exact bytes a tool result put into a model message.

    Measured, never estimated, at the single boundary where tool results are
    serialized into conversation messages - after every lossy layer:

      rawBytes     the dispatcher result before the generic model-facing
                   compaction/whole-result offload. Browser capability field
                   offload already happened inside dispatch; its exact field
                   payload sizes are reported separately below.
      modelBytes   after compaction + whole-result offload, before the
                   max_observation_chars string trim
      messageBytes the final content string appended to the conversation

    local_fs_read/search actual returned bytes therefore come for free:
    their results are ordinary tool results at this boundary, so messageBytes
    is what the model actually saw - not the request cap. Harness-internal
    calls (schema bootstrap, transport probes) never reach a model message
    and never appear here by construction. Deferred/batched placeholders are
    harness-authored notices, not tool output, and are not logged.

    Records sizes and status flags only - never content, never full paths.
    """
    content = final_content if isinstance(final_content, str) else ""
    # Capability-level field offload happens inside dispatch, so its stubs are
    # present in raw_result. A later whole-result offload replaces that object
    # with one root stub; counting only model_result would lose the underlying
    # AXTree/Runtime field accounting precisely when both layers fire.
    stub_count, stub_original = _offload_stub_stats(raw_result)
    raw_root_offloaded = _is_offload_stub(raw_result)
    whole_result_offloaded = _is_offload_stub(model_result)
    # If a tool itself returned an offload receipt, its root is not a field
    # offload. Remove it while retaining genuine nested field stubs.
    field_stub_count = stub_count - (1 if raw_root_offloaded else 0)
    root_original = (
        int(raw_result.get("originalBytes") or 0)
        if raw_root_offloaded and isinstance(raw_result, dict)
        else 0
    )
    field_stub_original = max(0, stub_original - root_original)
    serialized_model = json.dumps(
        model_result, ensure_ascii=False, default=str,
    )
    payload: JsonDict = {
        "actor": actor,
        "step": step,
        "tool": tool_name,
        "rawBytes": _json_size_bytes(raw_result),
        "modelBytes": _json_size_bytes(model_result),
        "messageBytes": len(content.encode("utf-8")),
        "wholeResultOffloaded": whole_result_offloaded,
        "fieldOffloadedCount": max(0, field_stub_count),
        "fieldOriginalBytes": field_stub_original,
        # After model_result, the only transformation at these call sites is
        # trim_large_strings. Compare exact serializations so business text
        # containing the literal marker is not misclassified as truncation.
        "truncated": content != serialized_model,
        "fileCategory": _offload_file_category(
            [raw_result, model_result], logger,
        ),
    }
    if method:
        # The ABCP method behind a browser_call ("DOM.getAXTree"), taken
        # from the dispatch input so per-method accounting falls out of the
        # same event. Capped; method names are capability ids, not content.
        payload["method"] = method[:120]
    if worker_id:
        payload["workerId"] = worker_id
    if isinstance(image_attachment, dict):
        # The actual image block is intentionally absent from observability.
        # These receipt facts are enough to correlate multimodal cost and
        # attachment failures with browser methods.
        payload["multimodalImage"] = dict(image_attachment)
    logger.write("tool_result.model_visible", payload)




@dataclass(frozen=True)
class CachePressureState:
    """What the usage receipts have said so far about prompt-prefix reuse.

    `rebuild_cache_read` is the cache_read floor observed on the one call that
    paid for a rebuilt prefix. It is the baseline the next receipt is compared
    against, and it is meaningful only while `awaiting_prefix_reuse` holds.
    """

    streak: int = 0
    awaiting_prefix_reuse: bool = False
    rebuild_cache_read: Optional[int] = None


def update_cache_pressure_state(
    state: CachePressureState,
    *,
    usage_payload: JsonDict,
    config: HarnessConfig,
    step: int,
    max_steps: int,
) -> tuple[CachePressureState, Optional[str]]:
    """Track sustained cache misses, ignoring the ones compaction itself causes.

    A compaction replaces the prompt prefix, so the call that follows it pays
    for the whole prefix again. Counting that as evidence of pressure makes the
    detector observe its own splash: in task a608b5e7 browser-002 compacted at
    step 32 (uncached 44,335), read that as pressure and compacted again at
    step 33 (43,152), and settled at step 34 (6,263). browser-004 did the same
    at steps 47/48. The second compaction in each pair bought nothing and cost
    a summarization round plus the causal detail it dropped.

    While a rebuild is outstanding the miss is attributed to it rather than
    accumulated. Two receipts can end that attribution, and both are arithmetic
    on the usage payload rather than a step count guessed here:

    * `uncached_input` back under the threshold — the prefix is demonstrably
      warm; or
    * `cache_read` above the floor recorded on the rebuild call — the rebuilt
      prefix has been written and read back.

    The second one matters because `cache_read` never falls to zero: the system
    prompt and tool schemas keep their own warm segment across a rebuild. In
    a608b5e7 every compaction landed on a floor of 20,480-24,576 and the very
    next call read 35,072-70,144, so "cache_read > 0" would clear the
    attribution instantly while "back under the threshold" could, on a worker
    whose every step carries a large tool result, never clear at all.
    """
    threshold = int(
        getattr(config, "cache_pressure_uncached_input_threshold",
                HarnessConfig.cache_pressure_uncached_input_threshold) or 0
    )
    required = int(getattr(config, "cache_pressure_consecutive_steps",
                           HarnessConfig.cache_pressure_consecutive_steps) or 0)
    min_remaining = int(
        getattr(config, "cache_pressure_min_remaining_steps",
                HarnessConfig.cache_pressure_min_remaining_steps) or 0
    )
    if threshold <= 0 or required <= 0:
        return CachePressureState(), None
    try:
        uncached_input = int(usage_payload.get("uncached_input") or 0)
    except (TypeError, ValueError):
        uncached_input = 0
    try:
        cache_read = int(usage_payload.get("cache_read") or 0)
    except (TypeError, ValueError):
        cache_read = 0

    if state.awaiting_prefix_reuse:
        if state.rebuild_cache_read is None:
            # The rebuild call itself. Record the floor its cache_read fell to
            # and charge its miss to the rebuild.
            return CachePressureState(0, True, cache_read), None
        if cache_read <= state.rebuild_cache_read and uncached_input > threshold:
            # The rebuilt prefix has still not been read back.
            return state, None

    if uncached_input <= threshold:
        return CachePressureState(), None
    streak = state.streak + 1
    remaining_steps = max_steps - step
    if streak >= required and (max_steps <= 0 or remaining_steps > min_remaining):
        reason = (
            "cache_pressure:"
            f"uncached_input>{threshold} for {streak} consecutive step(s)"
        )
        return CachePressureState(), reason
    return CachePressureState(streak), None


async def compact_and_track_prefix_rebuild(
    agent: Any,
    *,
    actor: str,
    step: int,
    system_prompt: Any,
    messages: List[JsonDict],
    tools: Any,
    force_reason: Optional[str] = None,
) -> List[JsonDict]:
    """Compact, and record it when the prompt prefix was actually rebuilt.

    Every skip path in `compact_messages_if_needed` returns the same list
    object it was handed, so identity is what separates a real rebuild from a
    no-op. Both agents go through here rather than calling the compactor
    directly: the error-recovery paths rebuild the prefix exactly like the
    main loop does, and a detector that only knows about some of the rebuilds
    goes back to counting its own splash on the others.
    """
    continuity_args = {}
    continuity_factory = getattr(agent, "compaction_continuity_factory", None)
    if continuity_factory is not None:
        continuity_args["browser_continuity_factory"] = continuity_factory
    rebuilt = await compact_messages_if_needed(
        logger=agent.logger,
        actor=actor,
        step=step,
        system_prompt=system_prompt,
        messages=messages,
        tools=tools,
        config=agent.runtime.harness,
        lifecycle=agent.lifecycle,
        force_reason=force_reason,
        provider=agent.provider,
        **continuity_args,
    )
    if rebuilt is not messages:
        agent._cache_pressure = CachePressureState(awaiting_prefix_reuse=True)
    return rebuilt


def _saved_paths_from_value(value: Any) -> List[str]:
    # Compatibility wrapper retained for local callers/tests.
    return saved_paths_from_value(value)


def _tool_result_is_error(result: Any) -> bool:
    """A tool result the harness itself classified as a failure."""
    if not isinstance(result, dict):
        return False
    if result.get("ok") is False or result.get("success") is False:
        return True
    status = str(result.get("status") or "").lower()
    return status in {"error", "failed", "rejected"} or bool(result.get("error"))


def _tool_result_digest(result: Any) -> str:
    from harness.events.recorder import result_digest

    return result_digest(result)


def _truncation_info(saved: Optional[JsonDict]) -> Any:
    """Turn a saved-payload receipt into the typed TruncationInfo.

    Without this the model was the only consumer that ever saw truncation
    facts: `MessageEndEvent.truncation` had a field and no producer.
    """

    if not saved:
        return None
    from harness.messages.models import TruncationInfo

    try:
        return TruncationInfo(
            source_complete=bool(saved.get("sourceComplete", False)),
            provider_truncated=bool(saved.get("providerTruncated", False)),
            projection_truncated=bool(saved.get("projectionTruncated", False)),
            saved_path=saved.get("savedPath"),
            received_output_path=saved.get("receivedOutputPath"),
            original_bytes=saved.get("originalBytes"),
            projected_bytes=saved.get("projectedBytes"),
            persisted_payload_sha256=saved.get("persistedPayloadSha256"),
            reason=saved.get("reason"),
        )
    except Exception:
        return None


def _store_received_model_output(**kwargs: Any) -> Optional[JsonDict]:
    from harness.context.offload import store_received_model_output

    return store_received_model_output(**kwargs)


def _assistant_message_to_wire(message: Any) -> JsonDict:
    from harness.messages.convert import assistant_message_to_wire

    return assistant_message_to_wire(message)


def _assistant_message_from_parts(**kwargs: Any) -> Any:
    from harness.events.recorder import assistant_message_from_parts

    return assistant_message_from_parts(**kwargs)


def _lifecycle_recorder_for(
    runtime: Any, logger: Any, trace: Any = None, actor_type: str = "system",
) -> Any:
    """A recorder bound to this actor, or an inert one when the feature is off.

    The identity comes from two places and needs both: the logger carries the
    worker/slot/phase the spawner bound, and the caller carries which KIND of
    agent this is. Leaving the second to a default is how every event - lead,
    worker and browser transition alike - came out labelled "system", filling
    the formerly-NULL actor_type column with a uniformly wrong value.
    """

    from harness.events.recorder import LifecycleRecorder

    harness_config = getattr(runtime, "harness", None)
    if not bool(getattr(harness_config, "events_lifecycle_enabled", False)):
        return LifecycleRecorder(None)
    bind = getattr(logger, "bound_event_factory", None)
    try:
        factory = bind() if callable(bind) else getattr(logger, "event_factory", None)
        if factory is not None:
            factory = factory.bind(actor_type=actor_type)
        setter = getattr(logger, "set_persist_message_content", None)
        if callable(setter):
            setter(bool(
                getattr(harness_config, "events_persist_message_content", False)
            ))
    except Exception:
        return LifecycleRecorder(None)
    if trace is None:
        return LifecycleRecorder(factory)
    probe = LifecycleRecorder(factory)
    if not probe.enabled:
        return probe
    from harness.events.sinks import TraceProjectionSink

    context = factory.context
    sink = TraceProjectionSink(
        trace, agent_id=context.agent_id, worker_id=context.worker_id,
    )
    emitter = logger.emitter
    emitter.add_sink(sink, critical=False)
    return LifecycleRecorder(
        factory, on_close=lambda: emitter.remove_sink(sink.name),
    )
