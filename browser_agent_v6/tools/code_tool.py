"""run_browser_python — batch code-execution tool (core of the fused approach).

In batch scenarios the LLM writes a Python block (for-loops + helper calls) and
gets aggregated results back in one call. This tool ships the code to
sandbox.WarmPool and returns stdout / stderr / exception.

Key prompt guidance (also in the description):
- print only necessary observations; never print large raw data
- For multi-record processing, save_artifact to disk and print only a summary
  (success count / failure count / field-fill rate / 1-2 sample records)
- On PendingDialogError → next turn use single-step dialog tools
- On HumanInterventionRequired → report upward; don't blindly retry
"""
from .base import ToolSpec, ToolContext


def _run_browser_python_handler(tool_input: dict, ctx: ToolContext) -> dict:
    if not ctx.pool:
        raise RuntimeError("ToolContext.pool is not injected — main.py should inject the WarmPool instance at startup")

    code = tool_input["code"]
    timeout = float(tool_input.get("timeout", 120))

    # shared_dir is read+write (cross-agent data delivery)
    result = ctx.pool.exec_code(
        code=code,
        worktree=ctx.worktree,
        shared=[ctx.shared_dir] if ctx.shared_dir else [],
        timeout=timeout,
    )

    # ── Flush update_progress buffer from the sandbox subprocess into main-process SessionState ──
    # worker.py _execute embeds ns["__progress_buffer__"] into result["progress_updates"]
    progress_updates = result.pop("progress_updates", None) if isinstance(result, dict) else None
    if progress_updates and ctx.spawner is not None:
        st = ctx.spawner.session_states.get(ctx.session_id)
        if st and st.progress is not None:
            applied = []
            for u in progress_updates:
                ok = st.progress.update(
                    goal_id=u.get("goal_id", ""),
                    increment=int(u.get("increment", 0) or 0),
                    status=u.get("status"),
                    note=str(u.get("note", "")),
                )
                applied.append({
                    "goal_id": u.get("goal_id"),
                    "applied": ok,
                    "current": (st.progress.goals[u["goal_id"]].current
                                if ok and u.get("goal_id") in st.progress.goals else None),
                })
            # Brief receipt for the LLM showing which goals really updated
            if isinstance(result, dict):
                result["progress_applied"] = applied
        else:
            # No progress_board declared → updates can't land; tell the LLM to ask Lead to init
            if isinstance(result, dict):
                result["progress_applied"] = (
                    "no progress board for this session — Lead should call init_progress first; "
                    f"{len(progress_updates)} buffered update(s) discarded"
                )
    return result


RUN_BROWSER_PYTHON = ToolSpec(
    name="run_browser_python",
    description=(
        "Execute a Python code block in the sandbox. The code has access to:\n"
        "- BROWSER HELPERS: goto(url), click(sel), fill(sel,text), extract_text, screenshot,\n"
        "  scroll, page_info, current_url, current_title, wait_for_element(sel), js(expr),\n"
        "  list_tabs, switch_tab, new_tab, close_tab, sleep, accept_dialog, dismiss_dialog\n"
        "- DOM EXPLORATION (use these BEFORE writing batch loops on unknown pages):\n"
        "  • dom_classes(min_count=3) → list of {class_name, count, tag_sample, text_sample}\n"
        "    of className appearing >= min_count times. BEST for finding list-item selectors\n"
        "    (a class with count=50 is almost certainly the list-item class).\n"
        "  • dom_outline(max_items=30) → list of {tag, id, classes, text_preview, attrs, selector_hint}\n"
        "    of top N text/interactive elements (in DOM order). Useful when dom_classes is unhelpful.\n"
        "  • dom_query(selector, max_items=20) → list of {text, href, dataset, ...} for elements matching\n"
        "    a CSS selector. Use to VERIFY a selector before writing a batch loop.\n"
        "- DATA HELPERS: save_artifact(name, data), read_artifact(name), list_artifacts()\n"
        "- PROGRESS: update_progress(goal_id, increment=0, status=None, note='') —\n"
        "  call after each batch (only useful if Lead called init_progress earlier);\n"
        "  buffered in-process, committed to SessionState when this run_browser_python returns.\n"
        "- LIBRARIES: json, re, math, time, datetime, pathlib, Path, pd (pandas), np (numpy)\n"
        "- EXCEPTIONS: NavigationTimeoutError, ElementNotFoundError, JSResultTooLargeError,\n"
        "  PendingDialogError, HumanInterventionRequired, BlockedImportError\n"
        "\n"
        "BLOCKED: requests/httpx/urllib/socket — use the browser helpers above for ALL network access.\n"
        "\n"
        "WHEN TO USE THIS TOOL (not single-step navigate/click):\n"
        "  ✓ Loop over 5+ items with same operation pattern\n"
        "  ✓ Step sequence is deterministic (no need to inspect each result)\n"
        "  ✓ Aggregating data → save_artifact + print summary\n"
        "WHEN NOT TO USE (use single-step tools instead):\n"
        "  ✗ 1-3 atomic actions\n"
        "  ✗ Need to see each step's result before deciding next\n"
        "  ✗ Login / captcha / dialog handling\n"
        "\n"
        "DEFENSIVE JS — write js() expressions that won't crash on missing elements:\n"
        "  ✗ document.querySelector('.x').textContent              ← will throw if .x missing\n"
        "  ✓ document.querySelector('.x')?.textContent ?? null     ← safe\n"
        "  ✓ [...document.querySelectorAll('.row')].map(r => ({\n"
        "      name: r.querySelector('.name')?.textContent?.trim() ?? null,\n"
        "      price: r.querySelector('.price')?.textContent?.trim() ?? null\n"
        "    }))\n"
        "\n"
        "SELECTOR FAILURE — when js() returns None / [] or throws NoneType errors:\n"
        "  1st failure → call dom_outline() to see real DOM\n"
        "  2nd failure → call dom_query('guess', max_items=3) to verify before looping\n"
        "  3rd failure → screenshot + give up + report to lead. DO NOT keep guessing.\n"
        "\n"
        "OUTPUT RULES — IMPORTANT for token efficiency:\n"
        "  - print() small structured observations (counts, samples of 1-2 records, field-fill rates)\n"
        "  - DO NOT print raw HTML, full datasets, base64. Save those via save_artifact and print path.\n"
        "  - On exception inside the code, the traceback is captured automatically — no need to wrap whole code in try/except\n"
        "\n"
        "Returns: {status, stdout, stderr, duration_s} (status='ok') or "
        "{status:'error', exception, message, traceback, stdout, stderr}."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "code": {
                "type": "string",
                "description": "Python source code to execute. Must end with print() or save_artifact() to surface results.",
            },
            "timeout": {
                "type": "number",
                "default": 120,
                "description": "Hard timeout in seconds (max 120). On timeout the worker is killed.",
            },
        },
        "required": ["code"],
    },
    handler=_run_browser_python_handler,
    long_running=True,
)


ALL_CODE_TOOLS = [RUN_BROWSER_PYTHON]
