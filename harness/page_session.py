"""
harness.page_session - What an earlier worker already did to one page.

Why this exists
---------------
A worker that dies on its step cap takes everything it learned with it. The
next worker on the same page re-derives it: it re-reads the tree, re-enters
values that are already entered, and re-walks paths that already failed. In
run 8208ed49 fifty such rebuilds cost 1,421s of pure warm-up (28.4s average,
against 18.8s for a first worker), and 63 of 437 workers died on the cap.

What is recorded, and what is deliberately not
----------------------------------------------
Recorded: the ACTION-level facts, keyed by page.

  * ``filledValues``     - what was typed/selected, and the stated purpose
  * ``completedActions`` - state-changing actions that returned success
  * ``failedPaths``      - method + purpose + the platform's error code
  * ``navigations``      - where the page was sent
  * ``artifacts``        - files the worker produced

NOT recorded: AX node ids, CSS selectors, coordinates, or any page snapshot.
Those are epoch-scoped - an AX id is valid only for the document generation
that produced it, and handing a stale one to the next worker is worse than
handing it nothing, because ``stale-target`` costs a round trip to discover.
The label that survives is ``params.purpose``: the platform marks every
state-changing method ``requiresPurpose``, so the model has already written a
sentence saying what the action was for, in the page's own words. That is a
run-time string, not a rule in this file - no site or field name is compiled
into the harness.

Everything here is extracted MECHANICALLY from the worker's trace. The model
is never asked to summarize its own work: a self-report is a claim, and a
claim that the next worker treats as page state is exactly the failure this
record exists to avoid. The trace is already the redacted, model-facing copy
(``shown_params``), and declared secrets are scrubbed again on the way in.

The injected block says, in the prompt, that this is the PREVIOUS worker's
observation and not a guarantee about the page right now. It is a head start,
not a substitute for looking.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from harness.tool_policy import (
    collect_sensitive_replacements,
    redact_values,
    sensitive_browser_method_params,
)
from harness.utils import JsonDict, safe_path_component, task_subdir

PAGE_SESSION_PROTOCOL = "page-session-v1"
PAGE_SESSION_DIR = "page_sessions"

# Methods whose params carry the value that was put into a control. The mapped
# name is the params key holding it; ``Input.select`` needs a shape-aware read
# and is handled separately.
VALUE_METHODS: Dict[str, str] = {
    "Input.type": "text",
}
SELECT_METHOD = "Input.select"

# State-changing methods with no value of their own. Reads (DOM.*, Page.getState,
# Page.screenshot, Input.scroll, Page.wheel) are absent on purpose: they change
# what the worker can see, not what the page holds, and recording them would
# bury the four facts that matter in scroll noise.
ACTION_METHODS = frozenset({
    "Input.click",
    "Page.click",
    "Input.press",
    "Input.drag",
    "Download.start",
})
NAVIGATION_METHODS = frozenset({
    "Page.navigate",
    "Page.go",
})

# Harness recovery composites write their own `purpose`, and their actions are
# about machinery rather than about the task: an overlay that was dismissed is
# gone, and a CAPTCHA the VL tried to solve is either cleared or still there —
# either way the next worker learns nothing from the attempt and everything
# from looking. Written by composites/dismiss_overlay.py and
# browser_tools/captcha_autosolve.py. A rename there makes this filter miss and
# the noise reappear in the record; it cannot break the run.
RECOVERY_PURPOSE_PREFIXES = (
    "dismiss_overlay:",
    "captcha autosolve:",
)

# Size bounds. A page session rides in the next worker's first user message, so
# it competes with the task itself for context. These caps keep the block around
# a couple of thousand tokens even for a long form.
MAX_FILLED_VALUES = 40
MAX_COMPLETED_ACTIONS = 60
MAX_FAILED_PATHS = 30
MAX_NAVIGATIONS = 12
MAX_ARTIFACTS = 20
MAX_PURPOSE_CHARS = 180
MAX_VALUE_CHARS = 120
MAX_WORKERS_LISTED = 8


def _text(value: Any, limit: int) -> str:
    text = str(value if value is not None else "").strip()
    if len(text) > limit:
        return text[: limit - 1] + "…"
    return text


def _entry_error_code(result: Any) -> str:
    """The platform's own error code for a failed call, or "" when it succeeded.

    Three shapes reach the trace: a transport error carries ``rpcData.error``,
    a soft failure carries ``response.error``, and an older path leaves only a
    prose ``error`` string. The code is preferred because it is a closed
    vocabulary the next worker can act on (``stale-target`` means refresh the
    tree); the prose is only a last resort.
    """
    if not isinstance(result, dict):
        return ""
    for container_key in ("rpcData", "response"):
        container = result.get(container_key)
        if isinstance(container, dict):
            error = container.get("error")
            if isinstance(error, dict) and error.get("code"):
                return _text(error["code"], 80)
            if isinstance(error, str) and error.strip():
                return _text(error, 80)
    if result.get("error"):
        return _text(result["error"], 80)
    return ""


def _entry_failed(result: Any) -> bool:
    if not isinstance(result, dict):
        return True
    if result.get("error"):
        return True
    response = result.get("response")
    if isinstance(response, dict) and response.get("error"):
        return True
    return False


def _selected_value(params: JsonDict) -> str:
    selections = params.get("selections")
    if not isinstance(selections, list):
        return ""
    labels: List[str] = []
    for item in selections:
        if isinstance(item, dict):
            label = item.get("label")
            if label is None:
                label = item.get("value")
            if label is not None and str(label).strip():
                labels.append(str(label).strip())
        elif item is not None and str(item).strip():
            labels.append(str(item).strip())
    return _text(", ".join(labels), MAX_VALUE_CHARS)


def _scrub(entry: JsonDict, method: str, params: JsonDict) -> JsonDict:
    """Apply the same secret substitution the transport boundary applies.

    The trace params are already masked for declared secret-bearing keys, but a
    credential inside a navigation URL is recognised by query-parameter NAME and
    scrubbed by value, which happens on the response path. Re-running it here
    means the page session cannot become the one copy that kept the secret.
    """
    secrets = collect_sensitive_replacements(
        params, sensitive_browser_method_params(method),
    )
    return redact_values(entry, secrets) if secrets else entry


def extract_page_sessions(
    trace: Any,
    *,
    worker_id: str,
    phase_id: str = "",
) -> Dict[str, JsonDict]:
    """Group one worker's state-changing calls by the page they acted on."""

    sessions: Dict[str, JsonDict] = {}
    if not isinstance(trace, list):
        return sessions
    for item in trace:
        if not isinstance(item, dict) or item.get("type") != "browser_call":
            continue
        method = str(item.get("method") or "")
        params = item.get("params")
        if not isinstance(params, dict):
            continue
        page_id = str(params.get("pageId") or "").strip()
        if not page_id:
            continue
        is_value = method in VALUE_METHODS or method == SELECT_METHOD
        if not (is_value or method in ACTION_METHODS or method in NAVIGATION_METHODS):
            continue
        purpose = _text(params.get("purpose"), MAX_PURPOSE_CHARS)
        if purpose.startswith(RECOVERY_PURPOSE_PREFIXES):
            continue
        session = sessions.setdefault(page_id, _empty_session(page_id))
        if worker_id and worker_id not in session["workers"]:
            session["workers"].append(str(worker_id))
        entry: JsonDict = {
            "method": method,
            "purpose": purpose,
            "workerId": str(worker_id or ""),
        }
        step = item.get("step")
        if isinstance(step, int):
            entry["step"] = step
        if phase_id:
            entry["phaseId"] = str(phase_id)
        if method == SELECT_METHOD:
            entry["value"] = _selected_value(params)
        elif method in VALUE_METHODS:
            entry["value"] = _text(params.get(VALUE_METHODS[method]), MAX_VALUE_CHARS)
        elif method in NAVIGATION_METHODS:
            entry["url"] = _text(params.get("url") or params.get("direction"), 300)
        entry = _scrub(entry, method, params)
        result = item.get("result")
        if _entry_failed(result):
            entry["errorCode"] = _entry_error_code(result)
            session["failedPaths"].append(entry)
        elif method in NAVIGATION_METHODS:
            session["navigations"].append(entry)
            if entry.get("url"):
                session["url"] = entry["url"]
        elif is_value:
            session["filledValues"].append(entry)
        else:
            session["completedActions"].append(entry)
    return sessions


def _empty_session(page_id: str) -> JsonDict:
    return {
        "protocol": PAGE_SESSION_PROTOCOL,
        "pageId": page_id,
        "url": "",
        "updatedAt": 0.0,
        "workers": [],
        "filledValues": [],
        "completedActions": [],
        "failedPaths": [],
        "navigations": [],
        "artifacts": [],
    }


def _dedupe_latest(entries: List[JsonDict], cap: int) -> List[JsonDict]:
    """Keep the LAST entry per (method, purpose) and the most recent `cap`.

    A field that was filled, cleared and refilled should read as its final
    value, not as three conflicting ones; and the tail of a run is the part the
    next worker is continuing from.
    """
    latest: Dict[Tuple[str, str], JsonDict] = {}
    for entry in entries:
        latest[(str(entry.get("method") or ""), str(entry.get("purpose") or ""))] = entry
    ordered = list(latest.values())
    return ordered[-cap:] if len(ordered) > cap else ordered


def _aggregate_failures(entries: List[JsonDict], cap: int) -> List[JsonDict]:
    """Collapse a repeated failure into one row with a count.

    Twelve identical ``stale-target`` rejections are one fact, not twelve, and
    the count is the part that tells the next worker the path is not merely
    unlucky.
    """
    grouped: Dict[Tuple[str, str, str], JsonDict] = {}
    for entry in entries:
        key = (
            str(entry.get("method") or ""),
            str(entry.get("purpose") or ""),
            str(entry.get("errorCode") or ""),
        )
        existing = grouped.get(key)
        if existing is None:
            merged = dict(entry)
            merged["attempts"] = 1
            grouped[key] = merged
        else:
            existing["attempts"] = int(existing.get("attempts") or 1) + 1
            if isinstance(entry.get("step"), int):
                existing["step"] = entry["step"]
    ordered = list(grouped.values())
    return ordered[-cap:] if len(ordered) > cap else ordered


def merge_page_session(
    existing: Optional[JsonDict], incoming: JsonDict,
) -> JsonDict:
    """Fold one worker's observations into the page's running record."""

    base = _empty_session(str(incoming.get("pageId") or ""))
    if isinstance(existing, dict) and existing.get("protocol") == PAGE_SESSION_PROTOCOL:
        for key in (
            "workers", "filledValues", "completedActions",
            "failedPaths", "navigations", "artifacts",
        ):
            value = existing.get(key)
            if isinstance(value, list):
                base[key] = list(value)
        base["url"] = str(existing.get("url") or "")
    for worker in incoming.get("workers") or []:
        if worker not in base["workers"]:
            base["workers"].append(worker)
    base["workers"] = base["workers"][-MAX_WORKERS_LISTED:]
    base["filledValues"] = _dedupe_latest(
        base["filledValues"] + list(incoming.get("filledValues") or []),
        MAX_FILLED_VALUES,
    )
    base["completedActions"] = _dedupe_latest(
        base["completedActions"] + list(incoming.get("completedActions") or []),
        MAX_COMPLETED_ACTIONS,
    )
    base["failedPaths"] = _aggregate_failures(
        base["failedPaths"] + list(incoming.get("failedPaths") or []),
        MAX_FAILED_PATHS,
    )
    navigations = base["navigations"] + list(incoming.get("navigations") or [])
    base["navigations"] = navigations[-MAX_NAVIGATIONS:]
    artifacts = base["artifacts"] + [
        path for path in (incoming.get("artifacts") or [])
        if path not in base["artifacts"]
    ]
    base["artifacts"] = artifacts[-MAX_ARTIFACTS:]
    base["url"] = str(incoming.get("url") or base["url"] or "")
    base["updatedAt"] = time.time()
    return base


def _session_path(logger: Any, page_id: str) -> Path:
    return task_subdir(logger, PAGE_SESSION_DIR) / f"{safe_path_component(page_id)}.json"


def load_page_session(logger: Any, page_id: str) -> Optional[JsonDict]:
    path = _session_path(logger, str(page_id or ""))
    try:
        if not path.exists():
            return None
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return loaded if isinstance(loaded, dict) else None


def record_page_sessions(
    logger: Any,
    trace: Any,
    *,
    worker_id: str,
    phase_id: str = "",
    artifacts: Any = None,
) -> List[str]:
    """Persist this worker's page observations. Never raises into the worker path.

    A page session is an optimization. Failing to write one must cost the run
    nothing beyond the optimization itself, so every error is logged and
    swallowed - the worker's own outcome is already decided by this point.
    """
    written: List[str] = []
    try:
        sessions = extract_page_sessions(
            trace, worker_id=worker_id, phase_id=phase_id,
        )
        if not sessions:
            return written
        artifact_paths = [
            str(path) for path in (artifacts or [])
            if isinstance(path, (str, Path)) and str(path).strip()
        ][:MAX_ARTIFACTS]
        for page_id, session in sessions.items():
            session["artifacts"] = artifact_paths
            merged = merge_page_session(load_page_session(logger, page_id), session)
            path = _session_path(logger, page_id)
            path.write_text(
                json.dumps(merged, ensure_ascii=False, indent=2, default=str),
                encoding="utf-8",
            )
            written.append(page_id)
        logger.write("page_session.recorded", {
            "workerId": str(worker_id or ""),
            "phaseId": str(phase_id or ""),
            "pageIds": written,
        })
    except Exception as exc:
        try:
            logger.write("page_session.record_failed", {
                "workerId": str(worker_id or ""),
                "error": str(exc)[:500],
            })
        except Exception:
            pass
    return written


def render_page_session_context(sessions: List[JsonDict]) -> str:
    """The block injected into the next worker's first message.

    The wording is load-bearing. It names the record as an earlier worker's
    observation and says the ids behind it are gone, so the next worker treats
    it as a starting hypothesis to verify rather than as current page state.
    """
    usable = [
        session for session in sessions
        if isinstance(session, dict) and (
            session.get("filledValues")
            or session.get("completedActions")
            or session.get("failedPaths")
        )
    ]
    if not usable:
        return ""
    payload = {
        "note": (
            "What an EARLIER worker on this task already did to these pages. "
            "It is that worker's observation at the time it acted, not a "
            "guarantee about the page now: the page may have reloaded, reset "
            "or navigated since. Element ids and coordinates are deliberately "
            "absent because they expire with the document - refresh "
            "DOM.getAXTree and confirm before acting."
        ),
        "howToUse": [
            "Treat filledValues as likely-already-entered: verify the control "
            "still shows that value before re-entering it, and do not clear a "
            "correct value to re-type it.",
            "Treat completedActions as already performed: repeating a submit, "
            "confirm or purchase click is not idempotent.",
            "Treat failedPaths as paths that cost the earlier worker time: "
            "prefer a different route, or fix the stated errorCode first.",
        ],
        "pages": usable,
    }
    return (
        "<page_session>\n"
        f"{json.dumps(payload, ensure_ascii=False, indent=2, default=str)}\n"
        "</page_session>"
    )


def page_session_context_for_pages(logger: Any, page_ids: Any) -> str:
    """Load and render the sessions for the pages handed to one worker.

    Scoped to the pages this worker is actually allowed to touch. A worker
    starting on a fresh page gets nothing, which is correct: there is no prior
    observation of a page that does not exist yet.
    """
    try:
        sessions = [
            session for session in (
                load_page_session(logger, page_id)
                for page_id in (page_ids or [])
                if str(page_id or "").strip()
            )
            if isinstance(session, dict)
        ]
        return render_page_session_context(sessions)
    except Exception as exc:
        try:
            logger.write("page_session.render_failed", {"error": str(exc)[:500]})
        except Exception:
            pass
        return ""
