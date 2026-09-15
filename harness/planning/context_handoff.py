"""Deterministic handoff after initial plan approval; no extra model call."""
import json


def execution_handoff(messages, plan, task_dir):
    # Preserve all actual user prose; tool results and assistant reasoning belong
    # to the recorded planning transcript. Approval feedback may arrive as a tool result.
    retained = []
    feedback = []
    plan_tools = {"emit_task_plan", "emit_direct_task_plan", "repair_task_plan", "submit_task_plan_draft"}
    plan_calls = {block.get("id") for message in messages if message.get("role") == "assistant"
                  and isinstance(message.get("content"), list) for block in message["content"]
                  if isinstance(block, dict) and block.get("type") == "tool_use"
                  and block.get("name") in plan_tools}
    for message in messages:
        if message.get("role") != "user":
            continue
        content = message.get("content")
        if isinstance(content, str):
            retained.append(message)
        elif isinstance(content, list):
            text_blocks = [b for b in content if b.get("type") == "text"]
            if text_blocks:
                retained.append({"role": "user", "content": text_blocks})
            for block in content:
                if block.get("type") == "tool_result" and block.get("tool_use_id") in plan_calls:
                    raw = block.get("content")
                    if isinstance(raw, str):
                        try:
                            value = json.loads(raw)
                            if (isinstance(value, dict) and value.get("status") == "user_revision_requested"
                                    and isinstance(value.get("operatorFeedback"), str)):
                                feedback.append(value["operatorFeedback"])
                        except (ValueError, TypeError):
                            pass
    retained.append({"role": "user", "content": json.dumps({
        "handoff": "Initial planning is approved. Execute this accepted plan; do not recreate it.",
        "acceptedPlan": plan, "approvalFeedback": list(dict.fromkeys(feedback)),
        "evidenceReferences": {"plan": f"{task_dir}/task_plan.json",
                               "state": f"{task_dir}/task_state.json",
                               "planningTranscript": f"{task_dir}/run.jsonl"},
        "historyPolicy": "Earlier candidates and reasoning are historical, not active contracts. "
                         "Read referenced evidence only for a concrete unresolved question."
    }, ensure_ascii=False, default=str)})
    return retained
