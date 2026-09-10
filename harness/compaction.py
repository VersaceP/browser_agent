"""
harness.compaction - Prompt context compaction while preserving tool pairing.
"""

import inspect
import json
import uuid
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional, Tuple

from runtime_config import HarnessConfig
from harness.lifecycle import LifecycleContext, LifecycleManager
from harness.offload import store_offloaded
from harness.messages.convert import to_model_messages
from harness.messages.models import CompactionSummaryMessage
from harness.utils import (
    JsonDict,
    RunLogger,
    extract_offloaded_paths,
    safe_path_component,
    task_subdir,
)


SUMMARY_HEADINGS = (
    "## Goal",
    "## Constraints & Preferences",
    "## Progress",
    "### Done",
    "### In Progress",
    "### Blocked",
    "## Key Decisions",
    "## Next Steps",
    "## Critical Context",
)

SummaryGenerator = Callable[..., Any]


def _wire_message(message: Any) -> JsonDict:
    """Return one provider-shaped message for structural inspection.

    The session transcript intentionally keeps a compaction checkpoint as a
    distinct canonical message.  Pairing and token estimation, however, must
    inspect exactly what the provider will receive.
    """
    if isinstance(message, CompactionSummaryMessage):
        return {"role": "user", "content": message.content}
    return message if isinstance(message, dict) else {}


def message_has_block(message: Any, block_type: str) -> bool:
    message = _wire_message(message)
    content = message.get("content")
    if not isinstance(content, list):
        return False
    return any(
        isinstance(block, dict) and block.get("type") == block_type
        for block in content
    )


def message_blocks(message: Any) -> List[JsonDict]:
    message = _wire_message(message)
    content = message.get("content")
    if not isinstance(content, list):
        return []
    return [block for block in content if isinstance(block, dict)]


def message_tool_use_ids(message: Any) -> List[str]:
    ids: List[str] = []
    for block in message_blocks(message):
        if block.get("type") != "tool_use":
            continue
        tool_use_id = block.get("id")
        ids.append(str(tool_use_id) if tool_use_id is not None else "")
    return ids


def message_tool_result_ids(message: Any) -> List[str]:
    ids: List[str] = []
    for block in message_blocks(message):
        if block.get("type") != "tool_result":
            continue
        tool_use_id = block.get("tool_use_id")
        ids.append(str(tool_use_id) if tool_use_id is not None else "")
    return ids


def _ids_match(left: List[str], right: List[str]) -> bool:
    return bool(left) and len(left) == len(right) and set(left) == set(right)


def validate_tool_pairing(messages: List[Any]) -> Optional[str]:
    wire_messages = [_wire_message(message) for message in messages]
    for index, message in enumerate(wire_messages):
        role = message.get("role")
        if role == "assistant":
            tool_use_ids = message_tool_use_ids(message)
            if any(not item for item in tool_use_ids):
                return f"tool_use missing id at msg {index}"
            if not tool_use_ids:
                continue
            if len(tool_use_ids) != len(set(tool_use_ids)):
                return f"duplicate tool_use id at msg {index}"
            if index + 1 >= len(wire_messages) or wire_messages[index + 1].get("role") != "user":
                return f"orphan tool_use(s) {sorted(tool_use_ids)} at msg {index}"
            result_ids = message_tool_result_ids(wire_messages[index + 1])
            if any(not item for item in result_ids):
                return f"tool_result missing tool_use_id at msg {index + 1}"
            if not _ids_match(tool_use_ids, result_ids):
                missing = sorted(set(tool_use_ids) - set(result_ids))
                extra = sorted(set(result_ids) - set(tool_use_ids))
                return (
                    f"pairing mismatch at msg {index}: "
                    f"missing={missing} extra={extra}"
                )
        elif role == "user":
            result_ids = message_tool_result_ids(message)
            if any(not item for item in result_ids):
                return f"tool_result missing tool_use_id at msg {index}"
            if not result_ids:
                continue
            previous = wire_messages[index - 1] if index > 0 else None
            if not isinstance(previous, dict) or previous.get("role") != "assistant":
                return f"orphan tool_result at msg {index}"
            tool_use_ids = message_tool_use_ids(previous)
            if not _ids_match(tool_use_ids, result_ids):
                missing = sorted(set(tool_use_ids) - set(result_ids))
                extra = sorted(set(result_ids) - set(tool_use_ids))
                return (
                    f"reverse pairing mismatch at msg {index}: "
                    f"missing={missing} extra={extra}"
                )
    return None


def estimate_prompt_tokens(
    system_prompt: str,
    messages: List[Any],
    tools: List[JsonDict],
) -> int:
    payload = json.dumps(
        {
            "system": system_prompt,
            "messages": to_model_messages(messages),
            "tools": tools,
        },
        ensure_ascii=False,
        default=str,
    )
    # Conservative approximation for mixed CJK/JSON prompts.
    return max(1, len(payload.encode("utf-8")) // 3)


def split_message_pairs(messages: List[Any]) -> List[List[Any]]:
    """Split only on complete provider turn boundaries.

    A raw ``role:user`` after the initial prompt commonly contains tool
    results, so it is not a reliable user-turn delimiter.  Assistant tool-use
    plus its immediately following tool-result entry is therefore atomic.
    """
    groups: List[List[Any]] = []
    index = 0
    while index < len(messages):
        current = messages[index]
        next_message = messages[index + 1] if index + 1 < len(messages) else None
        if (
            _wire_message(current).get("role") == "assistant"
            and message_has_block(current, "tool_use")
            and _wire_message(next_message).get("role") == "user"
            and message_has_block(next_message, "tool_result")
        ):
            groups.append([current, next_message])
            index += 2
            continue
        groups.append([current])
        index += 1
    return groups


def validate_structured_summary(text: str) -> Optional[str]:
    """Validate only the checkpoint's universal structure, not its meaning."""
    _bodies, error = _structured_summary_bodies(text)
    return error


def _structured_summary_bodies(
    text: str,
) -> Tuple[Optional[Dict[str, List[str]]], Optional[str]]:
    """Parse exact heading lines and exclude checkpoint-side fact blocks."""
    lines = str(text or "").splitlines()
    positions: List[int] = []
    cursor = 0
    for heading in SUMMARY_HEADINGS:
        try:
            found = next(
                index for index in range(cursor, len(lines))
                if lines[index].strip() == heading
            )
        except StopIteration:
            return None, f"missing heading: {heading}"
        positions.append(found)
        cursor = found + 1
    bodies: Dict[str, List[str]] = {}
    for index, heading in enumerate(SUMMARY_HEADINGS):
        end = positions[index + 1] if index + 1 < len(positions) else len(lines)
        body = lines[positions[index] + 1:end]
        if index + 1 == len(positions):
            for fact_index, line in enumerate(body):
                if line.strip().startswith("<"):
                    body = body[:fact_index]
                    break
        bodies[heading] = body
    return bodies, None


def _group_tokens(group: List[Any]) -> int:
    return max(1, len(json.dumps(to_model_messages(group), ensure_ascii=False,
                                  default=str).encode("utf-8")) // 3)


def select_groups_for_compaction(
    groups: List[List[Any]], *, keep_recent_tokens: int,
) -> Optional[Tuple[List[List[Any]], List[List[Any]]]]:
    """Return ``(compacted, retained)`` by walking backwards over full turns."""
    retained: List[List[Any]] = []
    retained_tokens = 0
    for group in reversed(groups):
        cost = _group_tokens(group)
        # Keep at least the newest complete group; then respect the budget.
        if retained and retained_tokens + cost > keep_recent_tokens:
            break
        retained.insert(0, group)
        retained_tokens += cost
    compacted = groups[:len(groups) - len(retained)]
    return (compacted, retained) if compacted and retained else None


def summarize_messages_for_compaction(
    groups: List[List[Any]],
    *,
    actor: str = "",
    previous_details: Optional[JsonDict] = None,
) -> JsonDict:
    tool_counts: Dict[str, int] = {}
    errors: List[str] = []
    offloaded: List[str] = []
    artifacts: List[str] = []
    worker_results: List[JsonDict] = []
    blockers: List[JsonDict] = []
    persisted_rows: List[JsonDict] = []
    page_ids: Dict[str, JsonDict] = {}
    fleet_ids: Dict[str, JsonDict] = {}
    recent_page_states: List[JsonDict] = []
    read_files: List[str] = []
    modified_files: List[str] = []
    total_tool_results = 0

    for group in groups:
        tool_names: Dict[str, str] = {}
        for message in group:
            message = _wire_message(message)
            content = message.get("content")
            if not isinstance(content, list):
                continue
            for block in content:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "tool_use":
                    name = str(block.get("name") or "unknown")
                    tool_counts[name] = tool_counts.get(name, 0) + 1
                    if block.get("id"):
                        tool_names[str(block["id"])] = name
                    tool_input = block.get("input")
                    if isinstance(tool_input, dict):
                        _collect_runtime_handles(
                            tool_input,
                            source=f"tool_use:{name}",
                            page_ids=page_ids,
                            fleet_ids=fleet_ids,
                            recent_page_states=recent_page_states,
                        )
                if block.get("type") != "tool_result":
                    continue
                total_tool_results += 1
                raw = block.get("content")
                if not isinstance(raw, str):
                    continue
                try:
                    parsed = json.loads(raw)
                except json.JSONDecodeError:
                    parsed = raw
                offloaded.extend(extract_offloaded_paths(parsed))
                if isinstance(parsed, dict):
                    tool_name = tool_names.get(str(block.get("tool_use_id") or ""), "")
                    _collect_file_tracking(
                        parsed,
                        tool_name=tool_name,
                        read_files=read_files,
                        modified_files=modified_files,
                    )
                    handoffs = parsed.get("workerHandoffs")
                    if isinstance(handoffs, list):
                        worker_results.extend(
                            item for item in handoffs if isinstance(item, dict)
                        )
                    artifacts.extend(_collect_artifact_paths(parsed))
                    for candidate in _result_candidates(parsed):
                        worker_result = _summarize_worker_result(candidate)
                        if worker_result:
                            worker_results.append(worker_result)
                        blocker = _summarize_blocker(candidate)
                        if blocker:
                            blockers.append(blocker)
                        verified = _summarize_verified_data(candidate)
                        if verified:
                            persisted_rows.append(verified)
                    _collect_runtime_handles(
                        parsed,
                        source=str(parsed.get("method") or "tool_result"),
                        page_ids=page_ids,
                        fleet_ids=fleet_ids,
                        recent_page_states=recent_page_states,
                    )
                    error = parsed.get("error")
                    if error:
                        errors.append(str(error)[:500])
                    response = parsed.get("response")
                    if isinstance(response, dict) and response.get("error"):
                        errors.append(str(response.get("error"))[:500])
    scope = "lead" if "lead" in str(actor).lower() else "browser"
    facts: JsonDict = {
        "schemaVersion": "context_compaction.v3",
        "actor": actor,
        "scope": scope,
        "messageGroups": len(groups),
        "toolResults": total_tool_results,
        "toolUsage": {
            "counts": tool_counts,
        },
        # Legacy alias retained for existing readers/tests.
        "toolCounts": tool_counts,
        "errors": errors[:10],
        "offloadedFiles": sorted(set(offloaded))[:100],
        "artifacts": sorted(set(artifacts))[:100],
        "workerResults": worker_results[-20:],
        # Persisted rows are not necessarily phase-complete or globally
        # validated; preserve the narrower provenance in the field name.
        "persistedRows": persisted_rows[-20:],
        "blockers": blockers[-20:],
        "runtimeHandles": {
            "pageIds": list(page_ids.values())[-20:],
            "fleetIds": list(fleet_ids.values())[-20:],
            "recentPageStates": recent_page_states[-10:],
        },
    }
    _merge_detail_lists(facts, previous_details or {})
    facts["readFiles"] = sorted(set(read_files) | set(facts.get("readFiles", [])))[:100]
    facts["modifiedFiles"] = sorted(
        set(modified_files) | set(facts.get("modifiedFiles", []))
    )[:100]
    return facts


def _merge_detail_lists(target: JsonDict, previous: JsonDict) -> None:
    """Union only mechanical collection fields from an earlier checkpoint."""
    for key in ("offloadedFiles", "artifacts", "readFiles", "modifiedFiles"):
        old = previous.get(key)
        new = target.get(key)
        if isinstance(old, list) or isinstance(new, list):
            target[key] = sorted({str(item) for item in (old or []) + (new or []) if item})[:100]


def _collect_file_tracking(
    value: JsonDict,
    *,
    tool_name: str,
    read_files: List[str],
    modified_files: List[str],
) -> None:
    """Record only paths proven by generic local filesystem tool receipts."""
    name = tool_name.lower()
    candidates: List[str] = []
    for key in ("path", "savedPath", "relativePath", "filePath"):
        raw = value.get(key)
        if isinstance(raw, str) and raw.strip():
            candidates.append(raw)
    if name in {"local_fs_read", "local_fs_search"}:
        read_files.extend(candidates)
    elif name == "record_extraction":
        modified_files.extend(candidates)


def _collect_artifact_paths(value: JsonDict) -> List[str]:
    paths: List[str] = []
    for key in ("artifacts", "offloadedFiles"):
        raw = value.get(key)
        if isinstance(raw, list):
            paths.extend(str(item) for item in raw if item)
    if isinstance(value.get("savedPath"), str):
        paths.append(str(value.get("savedPath")))
    completed = value.get("completed")
    if isinstance(completed, list):
        for item in completed:
            if isinstance(item, dict):
                paths.extend(_collect_artifact_paths(item))
    result_levels = value.get("resultLevels")
    if isinstance(result_levels, dict):
        l2 = result_levels.get("l2")
        if isinstance(l2, dict):
            evidence = l2.get("evidence")
            if isinstance(evidence, dict):
                raw_artifacts = evidence.get("artifacts")
                if isinstance(raw_artifacts, list):
                    paths.extend(str(item) for item in raw_artifacts if item)
    return paths


def _result_candidates(value: JsonDict) -> List[JsonDict]:
    candidates = [value]
    completed = value.get("completed")
    if isinstance(completed, list):
        candidates.extend(item for item in completed if isinstance(item, dict))
    results = value.get("results")
    if isinstance(results, list):
        candidates.extend(item for item in results if isinstance(item, dict))
    return candidates


def _summarize_worker_result(value: JsonDict) -> Optional[JsonDict]:
    from harness.results.worker_result import build_worker_handoff_projection

    projection = build_worker_handoff_projection(value)
    if projection is not None:
        return projection
    result_levels = value.get("resultLevels")
    if isinstance(result_levels, dict):
        l1 = result_levels.get("l1")
        if isinstance(l1, dict):
            trace_path = _trace_path_from_result_levels(result_levels)
            return {
                "workerId": l1.get("workerId"),
                "phaseId": l1.get("phaseId"),
                "status": l1.get("status"),
                "validatedStatus": l1.get("validatedStatus"),
                "artifactCount": l1.get("artifactCount"),
                "errorCount": l1.get("errorCount"),
                "traceSaved": l1.get("traceSaved"),
                "tracePath": trace_path,
            }
    if value.get("workerId") and value.get("traceSummary"):
        return {
            "workerId": value.get("workerId"),
            "phaseId": value.get("phaseId"),
            "status": value.get("status"),
            "validatedStatus": value.get("validatedStatus"),
            "tracePath": value.get("tracePath"),
        }
    return None


def _trace_path_from_result_levels(result_levels: JsonDict) -> Optional[str]:
    for level_name in ("l2", "l3"):
        level = result_levels.get(level_name)
        if not isinstance(level, dict):
            continue
        direct = level.get("tracePath")
        if isinstance(direct, str) and direct:
            return direct
        evidence = level.get("evidence")
        if isinstance(evidence, dict):
            path = evidence.get("tracePath")
            if isinstance(path, str) and path:
                return path
    return None


def _summarize_blocker(value: JsonDict) -> Optional[JsonDict]:
    public_failure = value.get("rpcData")
    public_prompt = (
        public_failure.get("suggested_prompt")
        if isinstance(public_failure, dict)
        else None
    )
    has_public_prompt = isinstance(public_prompt, str) and bool(public_prompt.strip())
    classification = value.get("errorClassification")
    compact_classification = (
        dict(classification) if isinstance(classification, dict) else classification
    )
    if has_public_prompt and isinstance(compact_classification, dict):
        compact_classification.pop("suggested_action", None)
        compact_classification.pop("platformSuggestedPrompt", None)
    status = str(value.get("status") or "")
    if status and status not in {"done", "running"}:
        summary = {
            "status": status,
            "workerId": value.get("workerId"),
            "phaseId": value.get("phaseId"),
            "error": str(value.get("error") or "")[:500],
            "errorClassification": compact_classification,
        }
        if has_public_prompt:
            summary["suggested_prompt"] = public_prompt
        return summary
    if isinstance(compact_classification, dict):
        summary = {
            "status": value.get("status"),
            "type": compact_classification.get("type"),
            "error": str(value.get("error") or "")[:500],
        }
        if not has_public_prompt and compact_classification.get("suggested_action"):
            summary["suggested_action"] = compact_classification["suggested_action"]
        if has_public_prompt:
            summary["suggested_prompt"] = public_prompt
        return summary
    return None


def _summarize_verified_data(value: JsonDict) -> Optional[JsonDict]:
    if value.get("rowCount") is not None and value.get("savedPath"):
        return {
            "name": value.get("name"),
            "rowCount": value.get("rowCount"),
            "savedPath": value.get("savedPath"),
        }
    result_levels = value.get("resultLevels")
    if isinstance(result_levels, dict):
        l2 = result_levels.get("l2")
        if isinstance(l2, dict):
            data = l2.get("data")
            if isinstance(data, dict):
                return {
                    "totalExtractedRows": data.get("totalExtractedRows"),
                    "extractionArtifacts": data.get("extractionArtifacts"),
                }
    return None


def _collect_runtime_handles(
    value: object,
    *,
    source: str,
    page_ids: Dict[str, JsonDict],
    fleet_ids: Dict[str, JsonDict],
    recent_page_states: List[JsonDict],
) -> None:
    if not isinstance(value, dict):
        return
    method = str(value.get("method") or source or "")
    params = value.get("params")
    response = value.get("response")
    data = response.get("data") if isinstance(response, dict) else value.get("data")

    for container, key, target in (
        (value, "pageId", page_ids),
        (value, "fleetId", fleet_ids),
        (params, "pageId", page_ids),
        (params, "fleetId", fleet_ids),
        (data, "pageId", page_ids),
        (data, "fleetId", fleet_ids),
    ):
        if not isinstance(container, dict):
            continue
        handle = container.get(key)
        if not isinstance(handle, str) or not handle.strip():
            continue
        target[handle] = {
            key: handle,
            "source": method,
        }

    if isinstance(data, dict):
        page_id = data.get("pageId")
        if isinstance(page_id, str) and page_id.strip():
            state = {
                "pageId": page_id,
                "source": method,
            }
            for key in ("url", "title", "status"):
                if data.get(key) is not None:
                    state[key] = data.get(key)
            recent_page_states.append(state)


def _previous_checkpoint(messages: List[Any]) -> Tuple[str, JsonDict, Optional[str]]:
    for message in reversed(messages):
        if isinstance(message, CompactionSummaryMessage):
            return message.content, dict(message.details), message.checkpoint_id
        wire = _wire_message(message)
        content = wire.get("content")
        text = ""
        if isinstance(content, str):
            text = content
        elif isinstance(content, list):
            text = "\n".join(
                str(block.get("text") or "") for block in content
                if isinstance(block, dict) and block.get("type") == "text"
            )
        if text.startswith("<CONTEXT_COMPRESSED>"):
            return text, {}, None
    return "", {}, None


def _render_transcript(groups: List[List[Any]], *, limit: int = 24000) -> str:
    lines: List[str] = []
    for group in groups:
        for message in to_model_messages(group):
            role = str(message.get("role") or "unknown")
            content = message.get("content")
            if isinstance(content, str):
                rendered = content
            else:
                rendered = json.dumps(content, ensure_ascii=False, default=str)
            lines.append(f"[{role}] {rendered[:1800]}")
            if sum(len(line) + 1 for line in lines) >= limit:
                return "\n".join(lines)[:limit]
    return "\n".join(lines)[:limit]


def _bounded_summary_sections(summary: str, max_chars: int) -> str:
    """Keep every required heading while bounding prose per section."""
    bodies, error = _structured_summary_bodies(summary)
    if error or bodies is None:
        return summary[:max_chars]
    content_budget = max(80, (max_chars - sum(len(h) + 1 for h in SUMMARY_HEADINGS)) // len(SUMMARY_HEADINGS))
    parts: List[str] = []
    for heading in SUMMARY_HEADINGS:
        body = "\n".join(bodies[heading]).strip()
        parts.append(f"{heading}\n{body[:content_budget].rstrip()}")
    return "\n".join(parts)


def _checkpoint_content(
    summary: str, details: JsonDict, *, max_tokens: int,
) -> str:
    max_chars = max(1024, int(max_tokens) * 3)
    semantic = _bounded_summary_sections(summary, max_chars // 2)

    def bounded_tag(name: str, values: Any, remaining: int) -> str:
        if not isinstance(values, list) or not values or remaining <= 0:
            return ""
        tag = name.replace("Files", "-files")
        kept: List[str] = []
        for value in values[:100]:
            candidate = kept + [str(value)]
            rendered = f"<{tag}>\n" + "\n".join(candidate) + f"\n</{tag}>"
            if len(rendered) > remaining:
                break
            kept = candidate
        if not kept:
            return ""
        omitted = len(values[:100]) - len(kept)
        suffix = (
            f"\n<{tag}-truncated omitted=\"{omitted}\"/>"
            if omitted else ""
        )
        rendered = f"<{tag}>\n" + "\n".join(kept) + f"\n</{tag}>" + suffix
        return rendered if len(rendered) <= remaining else f"<{tag}>\n" + "\n".join(kept) + f"\n</{tag}>"

    def bounded_facts(remaining: int) -> str:
        if remaining <= 0:
            return ""
        selected: JsonDict = {}
        omitted: List[str] = []
        for key in (
            "offloadedFiles", "artifacts", "workerResults", "persistedRows",
            "blockers", "runtimeHandles", "errors",
        ):
            if not details.get(key):
                continue
            candidate = {**selected, key: details[key]}
            rendered = "<compaction-facts>\n" + json.dumps(
                candidate, ensure_ascii=False, indent=2, default=str,
            ) + "\n</compaction-facts>"
            if len(rendered) <= remaining:
                selected = candidate
            else:
                omitted.append(key)
        if omitted:
            selected["truncated"] = {"omittedFields": omitted}
        rendered = "<compaction-facts>\n" + json.dumps(
            selected, ensure_ascii=False, indent=2, default=str,
        ) + "\n</compaction-facts>"
        return rendered if len(rendered) <= remaining else ""

    rendered = semantic
    for name in ("readFiles", "modifiedFiles"):
        remaining = max_chars - len(rendered) - 2
        part = bounded_tag(name, details.get(name), remaining)
        if part:
            rendered += "\n\n" + part
    remaining = max_chars - len(rendered) - 2
    fact_part = bounded_facts(remaining)
    if fact_part:
        rendered += "\n\n" + fact_part
    return rendered


def _mechanical_fallback_summary(previous_summary: str) -> str:
    """An explicit non-semantic checkpoint for provider-recovery paths."""
    bodies, error = _structured_summary_bodies(previous_summary)
    if bodies is not None and error is None:
        critical = "\n".join(bodies["## Critical Context"]).strip()
        notice = (
            "This compaction did not regenerate a semantic summary; the "
            "preceding checkpoint remains authoritative through its coverage."
        )
        bodies["## Critical Context"] = [
            *( [critical] if critical else [] ), notice,
        ]
        rendered_sections: List[str] = []
        for heading in SUMMARY_HEADINGS:
            body = "\n".join(bodies[heading]).strip()
            rendered_sections.append(f"{heading}\n{body}")
        return "\n".join(rendered_sections)
    return "\n".join([
        "## Goal", "The verbatim original task remains in the pinned first user message.",
        "## Constraints & Preferences", "Not semantically re-evaluated; consult the pinned task and retained messages.",
        "## Progress", "",
        "### Done", "Mechanical facts are retained below; completion is not inferred.",
        "### In Progress", "Compaction fell back because the summary model was unavailable.",
        "### Blocked", "Model-generated semantic summary unavailable for this checkpoint.",
        "## Key Decisions", "Use the persisted compaction resource and retained raw turns as evidence.",
        "## Next Steps", "Resume from the retained raw context; re-evaluate task state with the model.",
        "## Critical Context", "This is a mechanical fallback, not an LLM semantic summary.",
    ])


async def _generate_summary(
    *,
    provider: Any,
    generator: Optional[SummaryGenerator],
    actor: str,
    step: int,
    previous_summary: str,
    facts: JsonDict,
    middle_groups: List[List[Any]],
    logger: RunLogger,
) -> str:
    prompt = """Create a durable context-compaction checkpoint. Preserve exact task facts,
identifiers, error text, completed work, unresolved blockers, and the immediate next action.
Do not invent status. Use these headings exactly and in this order:
## Goal
## Constraints & Preferences
## Progress
### Done
### In Progress
### Blocked
## Key Decisions
## Next Steps
## Critical Context

Previous checkpoint (may be empty):
---
{previous}
---
Mechanical facts (authoritative; retain relevant items):
---
{facts}
---
Transcript being replaced:
---
{transcript}
---""".format(
        previous=previous_summary[:12000],
        facts=json.dumps(facts, ensure_ascii=False, indent=2, default=str),
        transcript=_render_transcript(middle_groups),
    )
    if generator is not None:
        result = generator(prompt=prompt, actor=actor, step=step)
        if inspect.isawaitable(result):
            result = await result
        text = str(result)
    elif provider is not None:
        text, tool_calls, stop_reason, usage = await provider.generate_response(
            system_prompt="You summarize agent context accurately. Do not call tools.",
            messages=[{"role": "user", "content": prompt}],
            tools=[],
        )
        if tool_calls:
            raise RuntimeError("compaction summary attempted tool calls")
        if str(stop_reason or "").lower() in {"error", "max_tokens"}:
            raise RuntimeError(f"compaction summary stopped: {stop_reason}")
        provider_config = getattr(provider, "config", None)
        logger.record_llm_usage(
            source="context_compaction",
            provider=str(getattr(provider_config, "provider", "unknown")),
            model=str(getattr(provider_config, "model_id", "unknown")),
            usage=usage if isinstance(usage, dict) else {},
            step=step,
            conversation_id=f"{actor}:compaction",
        )
    else:
        raise RuntimeError("no summary provider configured")
    error = validate_structured_summary(text)
    if error:
        raise RuntimeError(f"invalid compaction summary: {error}")
    return text


def _compaction_reason(force_reason: Optional[str]) -> str:
    if not force_reason:
        return "threshold"
    reason = force_reason.lower()
    if "overflow" in reason or "context" in reason:
        return "overflow"
    if "manual" in reason:
        return "manual"
    if "cache_pressure" in reason:
        return "cache_pressure"
    return "provider_recovery"


async def compact_messages_if_needed(
    *,
    logger: RunLogger,
    actor: str,
    step: int,
    system_prompt: str,
    messages: List[Any],
    tools: List[JsonDict],
    config: HarnessConfig,
    lifecycle: Optional[LifecycleManager] = None,
    force_reason: Optional[str] = None,
    provider: Any = None,
    summary_generator: Optional[SummaryGenerator] = None,
) -> List[Any]:
    if lifecycle is not None:
        payload = lifecycle.compact_before(
            LifecycleContext(actor=actor, step=step),
            {
                "messageCount": len(messages),
                "toolCount": len(tools),
            },
        )
        if payload.get("skip") is True:
            logger.write(
                "context.compaction_skipped",
                {
                    "actor": actor,
                    "step": step,
                    "reason": "lifecycle_skip",
                },
            )
            return messages

    estimated = estimate_prompt_tokens(system_prompt, messages, tools)
    threshold = int(
        max(1, config.model_context_window_tokens)
        * max(0.1, min(config.context_compaction_threshold_ratio, 0.95))
    )
    if estimated <= threshold and not force_reason:
        return messages

    original_pairing_error = validate_tool_pairing(messages)
    groups = split_message_pairs(messages)
    keep_head = max(0, int(getattr(config, "context_compaction_keep_head_pairs", 1)))
    pinned_groups = groups[:keep_head]
    compactable_groups = groups[keep_head:]
    keep_recent = max(1, int(getattr(config, "context_compaction_keep_recent_tokens", 24000)))
    selected = select_groups_for_compaction(
        compactable_groups, keep_recent_tokens=keep_recent,
    )
    if selected is None:
        logger.write(
            "context.compaction_skipped",
            {
                "actor": actor,
                "step": step,
                "reason": "not_enough_complete_turns",
                "estimatedTokensBefore": estimated,
                "thresholdTokens": threshold,
                "forceReason": force_reason,
                "messageCount": len(messages),
                "originalPairingError": original_pairing_error,
                "groupCount": len(groups),
                "pinnedGroupCount": len(pinned_groups),
            },
        )
        return messages

    middle_groups, tail_groups = selected
    previous_summary, previous_details, previous_checkpoint_id = _previous_checkpoint(messages)
    details = summarize_messages_for_compaction(
        middle_groups, actor=actor, previous_details=previous_details,
    )
    bound_factory = getattr(logger, "bound_event_factory", None)
    event_factory = (
        bound_factory() if callable(bound_factory)
        else getattr(logger, "event_factory", None)
    )
    event_context = None
    event_scope = None
    if event_factory is not None and hasattr(event_factory, "compaction_scope"):
        event_context = event_factory.compaction_scope(
            reason=_compaction_reason(force_reason),
            trigger_detail=force_reason,
            estimated_tokens_before=estimated,
            threshold_tokens=threshold,
            message_count_before=len(messages),
        )
        event_scope = event_context.__enter__()
    summary_mode = "semantic"
    summary_error: Optional[str] = None
    try:
        semantic_summary = await _generate_summary(
            provider=provider,
            generator=summary_generator,
            actor=actor,
            step=step,
            previous_summary=previous_summary,
            facts=details,
            middle_groups=middle_groups,
            logger=logger,
        )
    except Exception as exc:
        semantic_summary = _mechanical_fallback_summary(previous_summary)
        summary_mode = "mechanical_fallback"
        summary_error = str(exc)[:1000]
        logger.write("context.compaction_fallback", {
            "actor": actor, "step": step, "error": summary_error,
            "estimatedTokensBefore": estimated, "thresholdTokens": threshold,
            "forceReason": force_reason,
            "summaryMode": summary_mode,
        })

    checkpoint_id = f"compaction-{uuid.uuid4().hex[:12]}"
    compactions_dir = task_subdir(logger, "context_compactions")
    path = compactions_dir / (
        f"{safe_path_component(actor)}-step{step}-"
        f"{datetime.now().strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:8]}.json"
    )
    saved_payload = {
        "schemaVersion": "context_compaction.v3",
        "checkpointId": checkpoint_id,
        "previousCheckpointId": previous_checkpoint_id,
        "actor": actor,
        "step": step,
        "estimatedTokensBefore": estimated,
        "forceReason": force_reason,
        "strategy": "backward_complete_turn_token_budget",
        "summary": semantic_summary,
        "summaryMode": summary_mode,
        "details": details,
        "middleGroups": middle_groups,
    }
    checkpoint_budget = max(
        512, int(getattr(config, "context_compaction_checkpoint_max_tokens", 12000)),
    )
    content = _checkpoint_content(
        semantic_summary, details, max_tokens=checkpoint_budget,
    )
    compacted_message = CompactionSummaryMessage(
        content=content,
        replaced_message_count=sum(len(group) for group in middle_groups),
        details=details,
        checkpoint_id=checkpoint_id,
        tokens_before=estimated,
    )
    new_messages: List[Any] = []
    for group in pinned_groups:
        new_messages.extend(group)
    new_messages.append(compacted_message)
    for group in tail_groups:
        new_messages.extend(group)
    final_pairing_error = validate_tool_pairing(new_messages)
    if final_pairing_error:
        logger.write(
            "context.compaction_skipped",
            {
                "actor": actor,
                "step": step,
                "reason": "final_pairing_validation_failed",
                "pairingError": final_pairing_error,
                "estimatedTokensBefore": estimated,
                "thresholdTokens": threshold,
                "forceReason": force_reason,
                "messageCount": len(messages),
                "originalPairingError": original_pairing_error,
                "checkpointId": checkpoint_id,
            },
        )
        if event_scope is not None:
            event_scope.fail(final_pairing_error)
            event_context.__exit__(None, None, None)
        return messages

    try:
        resource = store_offloaded(
            logger, path, resource_type="context_compaction", content=saved_payload,
            metadata={"checkpointId": checkpoint_id, "actor": actor,
                      "summarySchema": "six_section_v1", "previousCheckpointId": previous_checkpoint_id},
        )
    except Exception as exc:
        if event_scope is not None:
            event_scope.fail(str(exc))
            event_context.__exit__(None, None, None)
        logger.write("context.compaction_failed", {
            "actor": actor, "step": step, "error": str(exc)[:1000],
            "reason": "checkpoint_persistence_failed",
            "checkpointId": checkpoint_id,
        })
        return messages

    # What the compaction actually bought. Without this the only observable was
    # "a compaction happened", so a trigger that fires on healthy runs and frees
    # nothing looked identical to one doing real work: run 48b97d84 spent 18
    # compactions (~900K equivalent input) while the context never exceeded 40%
    # of its window, and nothing in the log said so.
    estimated_after = estimate_prompt_tokens(system_prompt, new_messages, tools)
    tokens_freed = estimated - estimated_after
    if tokens_freed <= 0 and _compaction_reason(force_reason) == "cache_pressure":
        # Decline only the OPTIMISING trigger. cache_pressure fires to make the
        # next prefix cheaper, so a checkpoint that frees nothing has bought
        # nothing and swapping it in just pays for the summary twice. Every
        # other reason is asked for rather than inferred: threshold and overflow
        # mean the window is genuinely close (returning the old messages walks
        # further toward it), manual is an operator's explicit request, and
        # provider_recovery is repairing a transport failure. The checkpoint
        # stays on disk either way, so the evidence is not lost.
        logger.write(
            "context.compaction_rejected",
            {
                "actor": actor,
                "step": step,
                "reason": "no_tokens_freed",
                "forceReason": force_reason,
                "estimatedTokensBefore": estimated,
                "estimatedTokensAfter": estimated_after,
                "tokensFreed": tokens_freed,
                "thresholdTokens": threshold,
                "checkpointId": checkpoint_id,
                "savedPath": str(path.resolve()),
            },
        )
        if event_scope is not None:
            event_scope.complete(
                message_count_after=len(messages),
                estimated_tokens_after=estimated,
                checkpoint_ref=str(path.resolve()),
                summary_mode=summary_mode,
                summary_error=summary_error,
            )
            event_context.__exit__(None, None, None)
        return messages

    logger.write(
        "context.compacted",
        {
            "actor": actor,
            "step": step,
            "strategy": "backward_complete_turn_token_budget",
            "keepRecentTokens": keep_recent,
            "keepHeadGroups": len(pinned_groups),
            "checkpointMaxTokens": checkpoint_budget,
            "summaryMode": summary_mode,
            "estimatedTokensBefore": estimated,
            "estimatedTokensAfter": estimated_after,
            "tokensFreed": tokens_freed,
            "thresholdTokens": threshold,
            "forceReason": force_reason,
            "messageCountBefore": len(messages),
            "messageCountAfter": len(new_messages),
            "savedPath": str(path.resolve()),
            "resource": resource,
            "checkpointId": checkpoint_id,
            "originalPairingError": original_pairing_error,
        },
    )
    if lifecycle is not None:
        lifecycle.compact_after(
            LifecycleContext(actor=actor, step=step),
            {
                "strategy": "backward_complete_turn_token_budget",
                "messageCountBefore": len(messages),
                "messageCountAfter": len(new_messages),
                "savedPath": str(path.resolve()),
            },
        )
    if event_scope is not None:
        event_scope.complete(
            message_count_after=len(new_messages),
            estimated_tokens_after=estimated_after,
            checkpoint_ref=str(path.resolve()),
            summary_mode=summary_mode,
            summary_error=summary_error,
        )
        event_context.__exit__(None, None, None)
    return new_messages
