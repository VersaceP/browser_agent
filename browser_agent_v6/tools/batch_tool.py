"""batch_browser_actions — structured templated loop; the single-step accelerator.

Design principles:
- The batch tool embeds no auto-handling logic
- On failure, stop immediately; classify the exception and expose it to the LLM
- The LLM uses existing single-step tools (dismiss_dialog/wait_user/dom_outline)
  to recover, then resumes via start_from=N (partial results spilled to disk +
  slug-style dedup)
- iter 1 optional approval (--require-first-iteration-approval) lets the user
  confirm selectors before the rest auto-runs
- Intermediate results aggregate to _batch_partial_<id>.json; on completion the
  whole thing is published via publish_artifact

Step whitelist (allowed tools inside steps):
  navigate / click / fill / extract_text / screenshot / scroll
  accept_dialog / dismiss_dialog / wait_user
  list_skills / read_skill / read_file / list_files
Disallowed:
  spawn_agent / spawn_agents_parallel / submit_plan
  init_progress / update_progress
  publish_artifact / write_file / run_browser_python / batch_browser_actions
"""
import asyncio
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

from .base import ToolSpec, ToolContext, dispatch




# ──────────────────────────────────
# Exception classification — tells the LLM which single-step tool to call next
# ──────────────────────────────────

# When a step's inner dispatch fails, wrap the dispatch error dict in a
# BatchStepError so the outer except can route it.
class BatchStepError(Exception):
    def __init__(self, original_result: dict, tool: str, step_idx: int):
        self.original = original_result
        self.tool = tool
        self.step_idx = step_idx
        super().__init__(
            f"step {step_idx} ({tool}): "
            f"{original_result.get('exception','?')}: {original_result.get('message','')}"
        )


def _classify_exception(exc_name: str, exc_msg: str) -> Dict[str, str]:
    """Map an exception name to a structured stop_reason + suggested next action."""
    table = {
        "PendingDialogError": (
            "interaction_required",
            "use accept_dialog or dismiss_dialog (single-step), "
            "then call batch_browser_actions(start_from=<failed_at>)",
        ),
        "HumanInterventionRequired": (
            "human_required",
            "use wait_user('<reason>') so the human can solve it (e.g. captcha / login wall), "
            "then call batch_browser_actions(start_from=<failed_at>)",
        ),
        "ElementNotFoundError": (
            "selector_failed",
            "verify selector with dom_query / dom_outline (run_browser_python), "
            "edit the failing step, then call batch_browser_actions(start_from=<failed_at>) with new steps",
        ),
        "JSExecutionError": (
            "selector_failed",
            "the JS expression in this step crashed; verify selectors and re-issue with corrected steps",
        ),
        "JSResultTooLargeError": (
            "result_too_large",
            "narrow the JS return value (slice/filter page-side) and retry with start_from=<failed_at>",
        ),
        "NavigationTimeoutError": (
            "transient",
            "wait briefly and resume with batch_browser_actions(start_from=<failed_at>)",
        ),
        "BlockedImportError": (
            "configuration_error",
            "an internal helper tried a blocked import — should not happen in batch steps; report to lead",
        ),
    }
    if exc_name in table:
        stop_reason, suggested = table[exc_name]
        return {"stop_reason": stop_reason, "suggested_action": suggested}
    return {
        "stop_reason": "unknown",
        "suggested_action": (
            f"unclassified exception {exc_name}. Inspect message: {exc_msg[:200]}. "
            "Decide: retry with start_from=<failed_at>, edit steps, or fall back to single-step tools."
        ),
    }


# ──────────────────────────────────
# Templating — recursively replace {var} placeholders inside dict / list / str
# ──────────────────────────────────

def _render(value: Any, vars: Dict[str, Any]) -> Any:
    """Replace '{var}' placeholders in input_template with iteration vars.

    Recurses into dict / list; calls .format_map() on str; passes other types
    through unchanged. Missing keys are preserved as literal '{x}' so the LLM
    can see them.
    """
    if isinstance(value, str):
        try:
            return value.format_map(_SafeMap(vars))
        except Exception:
            return value
    if isinstance(value, list):
        return [_render(v, vars) for v in value]
    if isinstance(value, dict):
        return {k: _render(v, vars) for k, v in value.items()}
    return value


class _SafeMap(dict):
    """Forgiving dict for str.format_map — missing keys are kept as literal {x}."""
    def __missing__(self, key):
        return "{" + key + "}"


# ──────────────────────────────────
# iter 1 approval
# ──────────────────────────────────

# Tools whose output is most meaningful for selector health-check; printed first
_HEALTH_SIGNAL_TOOLS = {"extract_text", "navigate", "click", "fill"}


def _ask_iter_approval(iter_result: Dict[str, Any], remaining: int, iter_idx: int) -> bool:
    """Synchronous y/n terminal prompt — runs in to_thread, doesn't block the event loop."""
    if not (sys.stdin and sys.stdin.isatty()):
        print(f"\nWARN: require_first_approval=True but stdin is not a tty -> auto-reject")
        return False

    print("\n" + "═" * 60)
    print(f"  batch_browser_actions — iter {iter_idx + 1} done, please review selector accuracy")
    print("═" * 60)
    # Print step results
    for k, v in iter_result.items():
        if k.startswith("_"):
            continue
        body = v
        try:
            body_str = json.dumps(v, ensure_ascii=False, indent=2, default=str)
        except Exception:
            body_str = str(v)
        if len(body_str) > 800:
            body_str = body_str[:800] + f"\n  ... [truncated, total {len(body_str)} chars]"
        print(f"  > {k}")
        for line in body_str.splitlines():
            print(f"      {line}")
    print("─" * 60)
    print(f"  Remaining {remaining} iter(s) will auto-run without LLM involvement")
    sys.stdout.flush()
    while True:
        try:
            ans = input("approve & auto-run rest? [y/n]: ").strip().lower()
        except EOFError:
            return False
        if ans in ("y", "yes"):
            return True
        if ans in ("n", "no"):
            return False
        print("  please enter y or n")


# ──────────────────────────────────
# Main handler
# ──────────────────────────────────

# Step whitelist
_ALLOWED_STEP_TOOLS = {
    "navigate", "click", "fill", "extract_text", "screenshot", "scroll",
    "accept_dialog", "dismiss_dialog", "wait_user",
    "dom_query",  # allow per-iter selector verification; dom_classes/outline pointless across same-structure iters
    "list_skills", "read_skill", "read_file", "list_files",
}


async def _batch_browser_actions_handler(tool_input: dict, ctx: ToolContext) -> dict:
    if not ctx.spawner:
        raise RuntimeError("ToolContext.spawner is not injected — batch tool needs the spawner for registry / approval flag")
    registry = ctx.spawner.registry

    # ─── Parse parameters ───
    steps: List[Dict[str, Any]] = tool_input.get("steps") or []
    iterations: List[Dict[str, Any]] = tool_input.get("iterations") or []
    start_from: int = int(tool_input.get("start_from", 0))
    on_error: str = tool_input.get("on_error", "stop")  # only 'stop' is supported for now
    throttle: float = float(tool_input.get("throttle_seconds", 0.5))
    require_first_approval: bool = bool(tool_input.get("require_first_approval", False))
    publish_to: Optional[str] = tool_input.get("publish_to")  # publish_artifact(name=) on completion
    partial_filename: str = tool_input.get(
        "partial_filename", f"_batch_partial_{ctx.session_id}.json"
    )

    # spawner-level switch can force-enable first approval
    if getattr(ctx.spawner, "require_first_iteration_approval", False):
        require_first_approval = True
    # spawner-level switch can force-DISABLE first approval (e.g. test scripts /
    # non-interactive automation where blocking on stdin would deadlock and
    # batch would auto-reject). Wins over both the LLM's parameter choice and
    # the force-enable flag above.
    if getattr(ctx.spawner, "force_disable_iter_approval", False):
        require_first_approval = False

    # ─── Validate ───
    if not steps:
        return {"ok": False, "error": "steps cannot be empty — need at least 1 {tool, input} step"}
    if not iterations:
        return {"ok": False, "error": "iterations cannot be empty — need at least 1 vars dict"}
    if start_from < 0 or start_from >= len(iterations):
        return {
            "ok": False,
            "error": f"start_from={start_from} is out of range [0, {len(iterations)})",
        }
    bad_tools = [s.get("tool") for s in steps if s.get("tool") not in _ALLOWED_STEP_TOOLS]
    if bad_tools:
        return {
            "ok": False,
            "error": (
                f"these tools are not allowed inside batch steps: {bad_tools}. "
                f"allowed: {sorted(_ALLOWED_STEP_TOOLS)}"
            ),
        }

    # ─── Load partial (merge prior succeeded results on resume) ───
    # Use the worktree private dir (don't pollute shared)
    partial_path = Path(ctx.worktree) / partial_filename
    aggregated: List[Dict[str, Any]] = []
    if partial_path.exists() and start_from > 0:
        try:
            existing = json.loads(partial_path.read_text(encoding="utf-8"))
            if isinstance(existing, list):
                # Keep only entries before start_from (normally len(existing) == start_from)
                aggregated = existing[:start_from]
        except Exception as e:
            return {
                "ok": False,
                "error": f"failed to read partial file {partial_filename}: {e}",
            }

    # ─── Main loop ───
    failed_at: Optional[int] = None
    error_info: Optional[Dict[str, Any]] = None
    user_rejected = False

    for i in range(start_from, len(iterations)):
        iter_vars = iterations[i]
        if not isinstance(iter_vars, dict):
            failed_at = i
            error_info = {
                "stop_reason": "configuration_error",
                "exception": "TypeError",
                "message": f"iterations[{i}] must be a dict, got {type(iter_vars).__name__}",
                "suggested_action": "fix iterations payload",
            }
            break

        iter_result: Dict[str, Any] = {"_iter": i, **{f"_{k}": v for k, v in iter_vars.items()}}

        try:
            for step_idx, step in enumerate(steps):
                tool_name = step["tool"]
                tool_input_template = step.get("input", {})
                rendered_input = _render(tool_input_template, iter_vars)

                result = await dispatch(registry, tool_name, rendered_input, ctx)
                if result["status"] == "error":
                    raise BatchStepError(result, tool_name, step_idx)

                # Store step result into iter_result (key: step_<idx>_<tool>)
                key = f"step_{step_idx}_{tool_name}"
                iter_result[key] = result["result"]

            aggregated.append(iter_result)
        except BatchStepError as bse:
            failed_at = i
            classification = _classify_exception(
                bse.original.get("exception", "Unknown"),
                bse.original.get("message", ""),
            )
            error_info = {
                "exception": bse.original.get("exception"),
                "message": bse.original.get("message"),
                "failed_tool": bse.tool,
                "failed_step_idx": bse.step_idx,
                **classification,
            }
            break
        except BaseException as e:
            failed_at = i
            classification = _classify_exception(type(e).__name__, str(e))
            error_info = {
                "exception": type(e).__name__,
                "message": str(e),
                **classification,
            }
            break

        # iter-1 approval — only on the start_from iteration
        if i == start_from and require_first_approval:
            remaining = len(iterations) - i - 1
            approved = await asyncio.to_thread(_ask_iter_approval, iter_result, remaining, i)
            if not approved:
                user_rejected = True
                break

        # Throttle
        if throttle > 0 and i < len(iterations) - 1:
            await asyncio.sleep(throttle)

    # ─── Spill partial to disk (saved on success or failure, used for resume) ───
    partial_path.parent.mkdir(parents=True, exist_ok=True)
    partial_path.write_text(
        json.dumps(aggregated, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )

    completed = (failed_at is None) and (not user_rejected)
    succeeded_count = len(aggregated)

    out: Dict[str, Any] = {
        "ok": True,
        "completed": completed,
        "iterations_total": len(iterations),
        "iterations_run_this_call": (
            (failed_at - start_from) if failed_at is not None
            else (succeeded_count - start_from + (0 if not user_rejected else 0))
        ) if failed_at is not None else (len(iterations) - start_from),
        "succeeded_total": succeeded_count,
        "failed_at": failed_at,
        "stop_reason": (
            "completed" if completed else
            "user_rejected" if user_rejected else
            (error_info or {}).get("stop_reason", "unknown")
        ),
        "partial_saved_to": str(partial_path),
        "schema": _infer_schema(aggregated),
        "sample_first": aggregated[0] if aggregated else None,
        "sample_last": aggregated[-1] if aggregated else None,
    }
    if user_rejected:
        out["suggested_action"] = (
            "User rejected iter 1. Re-design steps/selectors and call batch_browser_actions again."
        )
    if error_info:
        out.update(error_info)
        # Replace the <failed_at> placeholder in suggested_action with the actual index
        sa = out.get("suggested_action", "")
        out["suggested_action"] = sa.replace("<failed_at>", str(failed_at))
        # Hint at the dedicated recovery playbook
        out["hint"] = (
            "batch stopped mid-iteration. read_skill('workflow_batch_recovery') "
            "for the stop_reason → recovery action map and resume protocol "
            "(start_from=<failed_at> + same recipe + auto-merged partials)."
        )

    # All succeeded → publish_artifact
    if completed and publish_to:
        spec = registry.get("publish_artifact")
        if spec is None:
            out["publish_warning"] = "publish_artifact tool not registered; skipping final publish"
        else:
            pub_result = await dispatch(
                registry,
                "publish_artifact",
                {"name": publish_to, "spill_path": str(partial_path),
                 "description": f"batch_browser_actions: {succeeded_count}/{len(iterations)} iters"},
                ctx,
            )
            if pub_result["status"] == "ok":
                out["published_to"] = pub_result["result"].get("path")
                out["published_name"] = publish_to
            else:
                out["publish_warning"] = (
                    f"final publish failed: {pub_result.get('exception')}: {pub_result.get('message')}"
                )

    return out


def _infer_schema(records: List[Dict[str, Any]]) -> List[str]:
    """Take field names from the first record as a hint for the LLM (not strict)."""
    if not records:
        return []
    first = records[0]
    return sorted(k for k in first.keys() if not k.startswith("_"))


# ──────────────────────────────────
# ToolSpec
# ──────────────────────────────────

BATCH_BROWSER_ACTIONS = ToolSpec(
    name="batch_browser_actions",
    description=(
        "Execute a templated sequence of browser steps over many iterations WITHOUT re-invoking "
        "the LLM each iteration. Use when iterating over deterministic pages with the same "
        "structure (e.g. 50 detail pages, only the URL/slug differs). "
        "Each `step` is {tool: <name>, input: <dict, may contain {var} placeholders>}; "
        "`iterations` is a list of variable dicts substituted into the placeholders. "
        "On any step failure the batch STOPS and returns a structured error with stop_reason "
        "(interaction_required / human_required / selector_failed / transient / ...) plus "
        "a suggested_action telling you which single-step tool to use to recover (e.g. "
        "dismiss_dialog, wait_user, dom_query). After recovering, RESUME with start_from=<failed_at>; "
        "the partial results are merged automatically. "
        "If require_first_approval=true, the user is prompted in the terminal after iter 1 "
        "to confirm the selectors are correct before the remaining iterations auto-run. "
        "Allowed step tools: navigate, click, fill, extract_text, screenshot, scroll, "
        "accept_dialog, dismiss_dialog, wait_user, dom_query, list_skills, read_skill, "
        "read_file, list_files. "
        "When `publish_to` is set and the batch completes, the aggregated result is automatically "
        "published as shared/<publish_to>."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "steps": {
                "type": "array",
                "minItems": 1,
                "description": "Sequence of {tool, input} executed per iteration. Strings inside input "
                               "may contain {var} placeholders substituted from each iteration's vars.",
                "items": {
                    "type": "object",
                    "properties": {
                        "tool": {"type": "string"},
                        "input": {"type": "object"},
                    },
                    "required": ["tool"],
                },
            },
            "iterations": {
                "type": "array",
                "minItems": 1,
                "description": "List of variable dicts. iterations[i] is substituted into step templates "
                               "for the i-th iteration.",
                "items": {"type": "object"},
            },
            "start_from": {
                "type": "integer",
                "default": 0,
                "description": "Resume index. After a failure, call again with start_from=<failed_at> "
                               "to continue from where it stopped (partial results auto-merged).",
            },
            "require_first_approval": {
                "type": "boolean",
                "default": False,
                "description": "If true, after iter `start_from` completes, pause and ask the user "
                               "in terminal to approve before running the remainder. Useful to verify "
                               "selectors before committing to 49+ iterations.",
            },
            "throttle_seconds": {
                "type": "number",
                "default": 0.5,
                "description": "Sleep between iterations (anti-rate-limit). Set 0 to disable.",
            },
            "publish_to": {
                "type": "string",
                "description": "When the batch completes successfully, publish the aggregated result "
                               "to shared/<publish_to> via publish_artifact (basename, no path).",
            },
            "partial_filename": {
                "type": "string",
                "description": "Filename inside the worktree for partial-result spill (default: "
                               "_batch_partial_<session_id>.json). Pass the same name across calls "
                               "to chain resumes.",
            },
            "on_error": {
                "type": "string",
                "enum": ["stop"],
                "default": "stop",
                "description": "Currently only 'stop' is supported (fail fast + report to LLM).",
            },
        },
        "required": ["steps", "iterations"],
    },
    handler=_batch_browser_actions_handler,
    long_running=True,
)


ALL_BATCH_TOOLS = [BATCH_BROWSER_ACTIONS]
