"""progress_tools — Lead uses init_progress to set quantitative goals;
Worker uses update_progress to report.

Per design intent: **only used for quantitative tasks; never auto-applied**.
- Non-quantitative tasks (e.g. "analyze this article") never call init_progress
  and don't render any board.
- Once init_progress is called, subsequent spawned worker tasks have the current
  board snippet auto-appended to their task description; workers call
  update_progress(goal_id, increment, status, note) after each batch.

Design:
- All state lives on spawner.session_states[session_id].progress (a ProgressBoard)
- update_progress just mutates state, no broadcast — the next spawn renders the
  fresh snippet at the end of effective_task automatically
- Both Lead and Worker can call update_progress; init_progress is Lead-only
  (controlled via allowed_tools)
"""
from typing import Any, Dict, List, Optional

from .base import ToolSpec, ToolContext
from core.session_state import Goal, ProgressBoard


# ──────────────────────────────────
# init_progress  — Lead declares quantitative goals once
# ──────────────────────────────────

def _init_progress_handler(tool_input: dict, ctx: ToolContext) -> dict:
    if not ctx.spawner:
        raise RuntimeError("ToolContext.spawner is not injected")

    goals_raw = tool_input.get("goals", [])
    if not isinstance(goals_raw, list) or len(goals_raw) == 0:
        return {"ok": False, "msg": "goals must be a non-empty array"}
    if len(goals_raw) > 10:
        return {"ok": False, "msg": f"max 10 goals, got {len(goals_raw)}"}

    state = ctx.spawner.get_session_state(ctx.session_id)

    # Re-init (calling init_progress again in the same session resets the board)
    board = ProgressBoard()
    seen_ids = set()
    for g in goals_raw:
        gid = str(g.get("id", "")).strip()
        if not gid:
            return {"ok": False, "msg": "every goal must have an id"}
        if gid in seen_ids:
            return {"ok": False, "msg": f"duplicate goal id: {gid}"}
        seen_ids.add(gid)
        board.add_goal(Goal(
            id=gid,
            description=str(g.get("description", "")),
            target=int(g.get("target", 0)),
        ))
    state.progress = board

    return {
        "ok": True,
        "session_id": ctx.session_id,
        "goals_count": len(board.goals),
        "summary": board.summary(),
        "msg": (
            "Progress board initialized. Subsequent spawned worker tasks will see the "
            "current board appended to their task description. Workers should call "
            "update_progress(goal_id, increment, status, note) after each batch."
        ),
    }


INIT_PROGRESS = ToolSpec(
    name="init_progress",
    description=(
        "Initialize a quantitative progress board for the session. "
        "Use ONLY for tasks with countable deliverables (e.g. 'scrape 50 products', "
        "'process 5 files'). Skip for open-ended tasks ('analyze this article'). "
        "Each goal: {id: short_str, description: human-readable, target: int}. "
        "Once set, every spawned worker sees the current board in its task description "
        "and is expected to call update_progress() after each batch. "
        "Calling init_progress again resets the board."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "goals": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string"},
                        "description": {"type": "string"},
                        "target": {"type": "integer", "minimum": 0},
                    },
                    "required": ["id", "description", "target"],
                },
                "minItems": 1,
                "maxItems": 10,
            },
        },
        "required": ["goals"],
    },
    handler=_init_progress_handler,
)


# ──────────────────────────────────
# update_progress  — Worker reports
# ──────────────────────────────────

def _update_progress_handler(tool_input: dict, ctx: ToolContext) -> dict:
    if not ctx.spawner:
        raise RuntimeError("ToolContext.spawner is not injected")
    state = ctx.spawner.session_states.get(ctx.session_id)
    if state is None or state.progress is None:
        return {
            "ok": False,
            "msg": "no progress board for this session — Lead should call init_progress first if this task is quantitative",
        }

    goal_id = str(tool_input["goal_id"])
    increment = int(tool_input.get("increment", 0))
    status = tool_input.get("status")
    note = str(tool_input.get("note", ""))[:200]

    if status and status not in ("pending", "in_progress", "completed", "failed"):
        return {"ok": False, "msg": f"status must be one of pending/in_progress/completed/failed, got {status!r}"}

    ok = state.progress.update(goal_id, increment=increment, status=status, note=note)
    if not ok:
        return {
            "ok": False,
            "msg": f"goal_id={goal_id!r} not found — current goals: {list(state.progress.goals.keys())}",
        }

    g = state.progress.goals[goal_id]
    return {
        "ok": True,
        "goal_id": goal_id,
        "current": g.current,
        "target": g.target,
        "status": g.status,
        "all_done": state.progress.all_done(),
    }


UPDATE_PROGRESS = ToolSpec(
    name="update_progress",
    description=(
        "Report progress on a goal previously declared via init_progress. "
        "Call AFTER each batch (e.g. saved 10 of 50 products → update_progress('products', increment=10)). "
        "Set status='completed' when done, 'failed' if you hit a hard block. "
        "Note: short observation, max 200 chars. "
        "Returns {current, target, all_done}."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "goal_id": {"type": "string", "description": "Goal id declared in init_progress"},
            "increment": {"type": "integer", "default": 0, "description": "How many units completed in this batch"},
            "status": {
                "type": "string",
                "enum": ["pending", "in_progress", "completed", "failed"],
                "description": "Set explicitly if you want to override auto-derived status",
            },
            "note": {"type": "string", "description": "Short observation (max 200 chars)"},
        },
        "required": ["goal_id"],
    },
    handler=_update_progress_handler,
)


ALL_PROGRESS_TOOLS: List[ToolSpec] = [INIT_PROGRESS, UPDATE_PROGRESS]
