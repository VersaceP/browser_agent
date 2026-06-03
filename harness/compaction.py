"""
harness.compaction - Prompt context compaction while preserving tool pairing.
"""

import json
import uuid
from datetime import datetime
from typing import Dict, List, Optional, Tuple

from harness.config import HarnessConfig
from harness.utils import (
    JsonDict,
    RunLogger,
    extract_offloaded_paths,
    safe_path_component,
    task_subdir,
)


def message_has_block(message: JsonDict, block_type: str) -> bool:
    content = message.get("content")
    if not isinstance(content, list):
        return False
    return any(
        isinstance(block, dict) and block.get("type") == block_type
        for block in content
    )


def message_blocks(message: JsonDict) -> List[JsonDict]:
    content = message.get("content")
    if not isinstance(content, list):
        return []
    return [block for block in content if isinstance(block, dict)]


def message_tool_use_ids(message: JsonDict) -> List[str]:
    ids: List[str] = []
    for block in message_blocks(message):
        if block.get("type") != "tool_use":
            continue
        tool_use_id = block.get("id")
        ids.append(str(tool_use_id) if tool_use_id is not None else "")
    return ids


def message_tool_result_ids(message: JsonDict) -> List[str]:
    ids: List[str] = []
    for block in message_blocks(message):
        if block.get("type") != "tool_result":
            continue
        tool_use_id = block.get("tool_use_id")
        ids.append(str(tool_use_id) if tool_use_id is not None else "")
    return ids


def _ids_match(left: List[str], right: List[str]) -> bool:
    return bool(left) and len(left) == len(right) and set(left) == set(right)


def validate_tool_pairing(messages: List[JsonDict]) -> Optional[str]:
    for index, message in enumerate(messages):
        role = message.get("role")
        if role == "assistant":
            tool_use_ids = message_tool_use_ids(message)
            if any(not item for item in tool_use_ids):
                return f"tool_use missing id at msg {index}"
            if not tool_use_ids:
                continue
            if len(tool_use_ids) != len(set(tool_use_ids)):
                return f"duplicate tool_use id at msg {index}"
            if index + 1 >= len(messages) or messages[index + 1].get("role") != "user":
                return f"orphan tool_use(s) {sorted(tool_use_ids)} at msg {index}"
            result_ids = message_tool_result_ids(messages[index + 1])
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
            previous = messages[index - 1] if index > 0 else None
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
    messages: List[JsonDict],
    tools: List[JsonDict],
) -> int:
    payload = json.dumps(
        {
            "system": system_prompt,
            "messages": messages,
            "tools": tools,
        },
        ensure_ascii=False,
        default=str,
    )
    # Conservative approximation for mixed CJK/JSON prompts.
    return max(1, len(payload.encode("utf-8")) // 3)


def split_message_pairs(messages: List[JsonDict]) -> Tuple[List[JsonDict], List[List[JsonDict]]]:
    if not messages:
        return [], []
    head = [messages[0]]
    groups: List[List[JsonDict]] = []
    index = 1
    while index < len(messages):
        current = messages[index]
        next_message = messages[index + 1] if index + 1 < len(messages) else None
        if (
            current.get("role") == "assistant"
            and message_has_block(current, "tool_use")
            and isinstance(next_message, dict)
            and next_message.get("role") == "user"
            and message_has_block(next_message, "tool_result")
        ):
            groups.append([current, next_message])
            index += 2
            continue
        groups.append([current])
        index += 1
    return head, groups


def summarize_messages_for_compaction(groups: List[List[JsonDict]]) -> JsonDict:
    tool_counts: Dict[str, int] = {}
    errors: List[str] = []
    offloaded: List[str] = []
    page_ids: Dict[str, JsonDict] = {}
    fleet_ids: Dict[str, JsonDict] = {}
    recent_page_states: List[JsonDict] = []
    total_tool_results = 0

    for group in groups:
        for message in group:
            content = message.get("content")
            if not isinstance(content, list):
                continue
            for block in content:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "tool_use":
                    name = str(block.get("name") or "unknown")
                    tool_counts[name] = tool_counts.get(name, 0) + 1
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
    return {
        "messageGroups": len(groups),
        "toolResults": total_tool_results,
        "toolCounts": tool_counts,
        "errors": errors[:10],
        "offloadedFiles": sorted(set(offloaded))[:100],
        "runtimeHandles": {
            "pageIds": list(page_ids.values())[-20:],
            "fleetIds": list(fleet_ids.values())[-20:],
            "recentPageStates": recent_page_states[-10:],
        },
    }


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


def make_compaction_message(summary: JsonDict) -> JsonDict:
    return {
        "role": "user",
        "content": [
            {
                "type": "text",
                "text": (
                    "<CONTEXT_COMPRESSED>\n"
                    f"{json.dumps(summary, ensure_ascii=False, indent=2, default=str)}"
                ),
            }
        ],
    }


def assemble_compacted_messages(
    *,
    head_messages: List[JsonDict],
    head_groups: List[List[JsonDict]],
    tail_groups: List[List[JsonDict]],
    compacted_message: Optional[JsonDict],
) -> List[JsonDict]:
    new_messages: List[JsonDict] = list(head_messages)
    for group in head_groups:
        new_messages.extend(group)
    if compacted_message is not None:
        new_messages.append(compacted_message)
    for group in tail_groups:
        new_messages.extend(group)
    return new_messages


def compact_messages_if_needed(
    *,
    logger: RunLogger,
    actor: str,
    step: int,
    system_prompt: str,
    messages: List[JsonDict],
    tools: List[JsonDict],
    config: HarnessConfig,
) -> List[JsonDict]:
    estimated = estimate_prompt_tokens(system_prompt, messages, tools)
    threshold = int(
        max(1, config.model_context_window_tokens)
        * max(0.1, min(config.context_compaction_threshold_ratio, 0.95))
    )
    if estimated <= threshold:
        return messages

    original_pairing_error = validate_tool_pairing(messages)

    head_messages, groups = split_message_pairs(messages)
    keep_head = max(0, config.context_compaction_keep_head_pairs)
    keep_tail = max(0, config.context_compaction_keep_tail_pairs)
    if len(groups) <= keep_head + keep_tail + 1:
        return messages

    compactions_dir = task_subdir(logger, "context_compactions")
    max_keep_head = len(groups) - keep_tail - 1
    validation_failures: List[JsonDict] = []
    chosen: Optional[Tuple[int, List[List[JsonDict]], List[List[JsonDict]], List[List[JsonDict]], str]] = None
    for candidate_keep_head in range(keep_head, max_keep_head + 1):
        head_groups = groups[:candidate_keep_head]
        tail_groups = groups[-keep_tail:] if keep_tail else []
        middle_end = len(groups) - keep_tail if keep_tail else len(groups)
        middle_groups = groups[candidate_keep_head:middle_end]
        if not middle_groups:
            continue
        candidate = assemble_compacted_messages(
            head_messages=head_messages,
            head_groups=head_groups,
            tail_groups=tail_groups,
            compacted_message=make_compaction_message({"status": "candidate"}),
        )
        pairing_error = validate_tool_pairing(candidate)
        if not pairing_error:
            chosen = (
                candidate_keep_head,
                head_groups,
                middle_groups,
                tail_groups,
                "summary",
            )
            break
        validation_failures.append({
            "keepHeadPairs": candidate_keep_head,
            "strategy": "summary",
            "error": pairing_error,
        })

    if chosen is None:
        for candidate_keep_head in range(keep_head, max_keep_head + 1):
            head_groups = groups[:candidate_keep_head]
            tail_groups = groups[-keep_tail:] if keep_tail else []
            middle_end = len(groups) - keep_tail if keep_tail else len(groups)
            middle_groups = groups[candidate_keep_head:middle_end]
            if not middle_groups:
                continue
            candidate = assemble_compacted_messages(
                head_messages=head_messages,
                head_groups=head_groups,
                tail_groups=tail_groups,
                compacted_message=None,
            )
            pairing_error = validate_tool_pairing(candidate)
            if not pairing_error:
                chosen = (
                    candidate_keep_head,
                    head_groups,
                    middle_groups,
                    tail_groups,
                    "drop_middle_no_summary",
                )
                break
            validation_failures.append({
                "keepHeadPairs": candidate_keep_head,
                "strategy": "drop_middle_no_summary",
                "error": pairing_error,
            })

    if chosen is None:
        logger.write(
            "context.compaction_skipped",
            {
                "actor": actor,
                "step": step,
                "reason": "no_pairing_safe_candidate",
                "estimatedTokensBefore": estimated,
                "thresholdTokens": threshold,
                "messageCount": len(messages),
                "originalPairingError": original_pairing_error,
                "validationFailures": validation_failures[:10],
            },
        )
        return messages

    chosen_keep_head, head_groups, middle_groups, tail_groups, strategy = chosen
    path = compactions_dir / (
        f"{safe_path_component(actor)}-step{step}-"
        f"{datetime.now().strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:8]}.json"
    )
    saved_payload = {
        "actor": actor,
        "step": step,
        "estimatedTokensBefore": estimated,
        "strategy": strategy,
        "middleGroups": middle_groups,
    }
    path.write_text(
        json.dumps(saved_payload, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )

    compacted_message = None
    if strategy == "summary":
        summary = summarize_messages_for_compaction(middle_groups)
        summary["originalSavedPath"] = str(path.resolve())
        summary["estimatedTokensBefore"] = estimated
        summary["thresholdTokens"] = threshold
        compacted_message = make_compaction_message(summary)

    new_messages = assemble_compacted_messages(
        head_messages=head_messages,
        head_groups=head_groups,
        tail_groups=tail_groups,
        compacted_message=compacted_message,
    )
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
                "messageCount": len(messages),
                "originalPairingError": original_pairing_error,
                "savedPath": str(path.resolve()),
            },
        )
        return messages

    logger.write(
        "context.compacted",
        {
            "actor": actor,
            "step": step,
            "strategy": strategy,
            "keepHeadPairs": chosen_keep_head,
            "keepTailPairs": keep_tail,
            "estimatedTokensBefore": estimated,
            "messageCountBefore": len(messages),
            "messageCountAfter": len(new_messages),
            "savedPath": str(path.resolve()),
            "originalPairingError": original_pairing_error,
            "validationFailures": validation_failures[:10],
        },
    )
    return new_messages
