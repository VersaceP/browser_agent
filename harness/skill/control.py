"""harness.skill.control — active PAGE-level control during a blocked Workflow.execute.

The primary ABCPClient serializes every call behind one `_call_lock`, so while a
skill's `Workflow.execute` is blocked, the owning connection cannot issue control
calls. To *drive* a resolution mid-execute we open a SECOND ABCP connection.

LIVE-VERIFIED cross-connection semantics (2026-06-27, three-connection probe):
  - `Workflow.pause/resume/getStatus(runId)` are **session-bound**: from any
    connection other than the run's owner they fail `-32005` (even for a completed
    run). The owner is blocked inside its own execute. ⇒ **engine-level pause/resume
    of a running workflow is impossible** — do NOT use Workflow.pause/resume here.
  - PAGE-level ops cross-connection **work**: a 2nd connection can `Page.getState`,
    `Page.navigate`, `Hitl.requestPause`, `Hitl.resolvePause`, Input/DOM on the
    primary's page.

So active control here is PAGE-level, not engine-level: when a challenge is observed
mid-execute, production Skill dispatch resolves the *page* through the human
`Hitl.requestPause`→wait `Hitl.resumed` path. CAPTCHA VL auto-solving is owned by
the worker/browser-tools hot path because this control transport cannot observe
the live panel's `Runtime.evaluate` return values. The experimental VL resolver
below is intentionally not wired into Skill dispatch. The workflow continues on
its own — it must be authored with a
challenge-gated `listen Hitl.resumed` boundary (see skills/_template) so it waits at
the challenge point instead of failing past it; the `Hitl.resumed` that ends the
pause also satisfies that listen. Everything is gated
(`skill_workflow_active_control_enabled`, default OFF) and fail-safe: any control
error degrades to the observe-only hand-off (`harness/skill_pause.py`).
"""
from __future__ import annotations

import asyncio
from typing import Any, Awaitable, Callable, Dict, List, Optional

from harness.skill.pause import hitl_onset_signal
from harness.skill.workflow import run_skill_workflow
from harness.screenshot_policy import normalize_screenshot_output_params
from harness.vl import captcha
from harness.vl.captcha import run_captcha_solve_loop, solve_plan_to_input_calls

# on_pause resolver: (control, page_id, onset_signal) -> resolved? (True ⇒ page cleared)
PauseResolver = Callable[["ControlChannel", str, Dict[str, Any]], Awaitable[bool]]


class ControlChannel:
    """A second ABCP connection for PAGE-level control while the primary is blocked
    in Workflow.execute. Duck-typed inner client: anything with async `call`,
    `connect`, `close` (i.e. abcp_client.ABCPClient). Engine-level Workflow.pause/
    resume are intentionally absent — they are session-bound and unreachable here."""

    def __init__(
        self,
        client: Any,
        *,
        owns_client: bool = True,
        logger: Any = None,
        fleet_auth_barrier: Any = None,
        fleet_id: str = "",
        worker_id: str = "",
    ):
        self.client = client
        self._owns_client = owns_client
        self._logger = logger
        self._open = False
        self._fleet_auth_barrier = fleet_auth_barrier
        self._fleet_id = str(fleet_id or "").strip()
        self._worker_id = str(worker_id or "").strip()

    @classmethod
    def from_browser(
        cls,
        browser: Any,
        *,
        logger: Any = None,
        fleet_auth_barrier: Any = None,
        fleet_id: str = "",
        worker_id: str = "",
    ) -> Optional["ControlChannel"]:
        """Build a control channel mirroring the primary browser's connection
        config (ws_url + auth). Returns None if the primary exposes no config."""
        config = getattr(browser, "config", None)
        if config is None:
            return None
        try:
            from abcp_client import ABCPClient
        except Exception:  # pragma: no cover - import environment guard
            return None
        return cls(
            ABCPClient(config),
            owns_client=True,
            logger=logger,
            fleet_auth_barrier=fleet_auth_barrier,
            fleet_id=fleet_id,
            worker_id=worker_id,
        )

    async def try_open(self) -> bool:
        """Connect the control channel. Never raises — returns False on failure so
        the caller degrades to the observe-only hand-off."""
        connect = getattr(self.client, "connect", None)
        try:
            if callable(connect):
                await connect()
            # Never register the secondary socket with the primary worker's
            # agentId. ABCP routes page notifications to one agent socket; a
            # duplicate registration can steal notifications from the primary
            # connection. A made-up second identity is not safe either because
            # it may not own the page. Until ABCP exposes delegated-control
            # identity, this channel relies only on cross-connection page calls.
            self._open = True
            return True
        except Exception as exc:
            self._log("skill.control.open_failed", {"error": str(exc)})
            if self._owns_client:
                close = getattr(self.client, "close", None)
                if callable(close):
                    try:
                        await close()
                    except Exception:
                        pass
            self._open = False
            return False

    async def call(self, method: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        # This connection bypasses the BrowserAgent dispatcher, so reuse the
        # same transport boundary directly rather than maintaining a literal.
        forwarded, _receipt = normalize_screenshot_output_params(method, params)
        return await self.client.call(method, forwarded)

    async def request_pause(self, page_id: str, reason: str = "skill workflow challenge") -> Dict[str, Any]:
        """Page-level HITL pause (cross-connection verified)."""
        barrier = self._fleet_auth_barrier
        if (
            barrier is not None
            and self._fleet_id
            and self._worker_id
        ):
            claim = await barrier.claim(
                self._fleet_id,
                self._worker_id,
                "skill control channel requested opaque Workflow HITL",
            )
            self._log(
                "skill.control.auth_barrier_claimed",
                {
                    "fleetId": self._fleet_id,
                    "workerId": self._worker_id,
                    **dict(claim),
                },
            )
            if not claim.get("claimed"):
                raise RuntimeError(
                    "fleet_auth_gated: another worker owns HITL recovery"
                )
        return await self.call("Hitl.requestPause", {
            "pageId": page_id, "reason": reason,
            "purpose": "pause page so the challenge can be cleared (human/VL)",
        })

    async def resolve_pause(self, page_id: str) -> Dict[str, Any]:
        """Page-level HITL resolve — emits Hitl.resumed (cross-connection verified).
        Use after a VL auto-solve; the human path lets the playground resolve."""
        return await self.call("Hitl.resolvePause", {
            "pageId": page_id,
            "purpose": "resume page after the challenge was cleared",
        })

    async def signal_resumed(self, page_id: str) -> Dict[str, Any]:
        """Emit Hitl.resumed so the workflow's `listen Hitl.resumed` boundary
        continues, after the control channel cleared an IN-PAGE challenge (the page
        was never HITL-paused). requestPause→resolvePause is the verified way to
        fire the event cross-connection (probe_listen_resumed: the workflow listen
        catches it). requestPause is best-effort (ignored if already paused)."""
        try:
            await self.request_pause(page_id, reason="VL solved the challenge; releasing control")
        except Exception:  # already paused or transient — resolvePause still emits resumed
            pass
        return await self.resolve_pause(page_id)

    async def close(self) -> None:
        if self._owns_client and self._open:
            close = getattr(self.client, "close", None)
            if callable(close):
                try:
                    await close()
                except Exception:  # pragma: no cover - best-effort
                    pass
        self._open = False

    def _log(self, event: str, payload: Dict[str, Any]) -> None:
        if self._logger is not None and hasattr(self._logger, "write"):
            try:
                self._logger.write(event, payload)
            except Exception:  # pragma: no cover
                pass

    async def __aenter__(self) -> "ControlChannel":
        await self.try_open()
        return self

    async def __aexit__(self, *exc) -> None:
        await self.close()


async def _await_notification_onset(primary: Any, page_id: str, timeout: float) -> Optional[Dict[str, Any]]:
    """Wait (via the primary's notification hub, which bypasses _call_lock) for a
    navigation-level pause/challenge onset for `page_id`. Returns the signal or None."""
    waiter = getattr(primary, "wait_for_notification", None)
    if not callable(waiter):
        await asyncio.sleep(timeout)
        return None
    msg = await waiter(
        lambda m: hitl_onset_signal(m, page_id) is not None,
        timeout=timeout,
    )
    return hitl_onset_signal(msg, page_id) if msg is not None else None


async def _wait_for_challenge(
    primary: Any,
    control: "ControlChannel",
    page_id: str,
    exec_task: "asyncio.Task",
    *,
    onset_timeout: float,
    poll_interval: float,
    poll_challenge_fn: Optional[Callable[..., Awaitable[Optional[Dict[str, Any]]]]],
) -> Optional[Dict[str, Any]]:
    """Race the running execute against challenge detection. Detection has two legs:
    (a) navigation-level NOTIFICATIONS on the primary hub (Cloudflare etc.), and
    (b) an active POLL on the control channel for IN-PAGE widgets (the primary is
    blocked in execute, so the poll must run on the 2nd connection). Returns the
    onset signal, or None when execute finishes first / overall timeout elapses."""
    loop = asyncio.get_event_loop()
    deadline = loop.time() + max(0.0, onset_timeout)
    while not exec_task.done() and loop.time() < deadline:
        notif_task = asyncio.create_task(
            _await_notification_onset(primary, page_id, min(poll_interval, deadline - loop.time()))
        )
        done, _pending = await asyncio.wait({exec_task, notif_task}, return_when=asyncio.FIRST_COMPLETED)
        if exec_task in done:
            notif_task.cancel()
            return None
        signal = notif_task.result()
        if signal is not None:
            return signal
        # notification window elapsed without onset → active poll on the control channel
        if poll_challenge_fn is not None:
            try:
                signal = await poll_challenge_fn(control, page_id)
            except Exception:
                signal = None
            if signal is not None:
                return signal
    return None


def _iter_steps(steps: Any) -> Any:
    """Walk steps depth-first, descending into if/then/else branches."""
    if not isinstance(steps, list):
        return
    for step in steps:
        if not isinstance(step, dict):
            continue
        yield step
        for branch in ("then", "else"):
            yield from _iter_steps(step.get(branch))


def _listens_for_resumed(branch: Any) -> bool:
    for sub in _iter_steps(branch):
        if sub.get("type") == "listen" and str(sub.get("event") or "") == "Hitl.resumed":
            return True
    return False


def _find_challenge_boundary(skill: Any) -> Optional[Dict[str, Any]]:
    """Recover a skill's own challenge-detection convention (skills/_template):
      • an `if` whose `then` listens for Hitl.resumed, gated on `$vars.<flag>`
      • a `Runtime.evaluate` whose `extract` produces that `<flag>`
    Returns {expression, result_key, flag, match} so the active poll can mirror the
    SAME detection on the 2nd connection. None when the skill declares no boundary
    (then no in-page poll runs — navigation-level onset still works)."""
    steps = getattr(skill, "steps", None)
    if steps is None:
        steps = (getattr(skill, "workflow", {}) or {}).get("steps")
    flag: Optional[str] = None
    match = "yes"
    for step in _iter_steps(steps):
        if step.get("type") == "if" and _listens_for_resumed(step.get("then")):
            cond = step.get("condition") or {}
            path = str(cond.get("path") or "")
            if path.startswith("$vars."):
                flag = path[len("$vars."):]
                match = str(cond.get("value") or "yes")
                break
    if not flag:
        return None
    for step in _iter_steps(steps):
        if step.get("action") == "Runtime.evaluate":
            extract = step.get("extract") or {}
            if isinstance(extract, dict) and flag in extract:
                expr = ((step.get("params") or {}).get("expression"))
                if expr:
                    return {"expression": str(expr), "result_key": str(extract[flag]),
                            "flag": flag, "match": match}
    return None


def _flag_matches(value: Any, match: str) -> bool:
    text = str(value or "")
    if not text:
        return False
    if not match:
        return True
    try:
        import re
        return re.search(match, text) is not None
    except re.error:
        return match in text


def make_challenge_poller(
    skill: Any,
) -> Optional[Callable[["ControlChannel", str], Awaitable[Optional[Dict[str, Any]]]]]:
    """Build a per-skill IN-PAGE challenge poll for the active-control loop, or None.

    The primary connection is blocked inside Workflow.execute, so an in-page widget
    that fires NO navigation (e.g. a slider that appears after "send code") is
    invisible to the notification leg. This poll runs the skill's OWN challengeFlag
    expression on the 2nd connection and reports onset when the flag matches. Faithful
    by construction (same JS the workflow uses) and fail-safe (any error → None)."""
    boundary = _find_challenge_boundary(skill)
    if boundary is None:
        return None
    expression = boundary["expression"]
    result_key = boundary["result_key"]
    flag = boundary["flag"]
    match = boundary["match"]

    async def poll(control: "ControlChannel", page_id: str) -> Optional[Dict[str, Any]]:
        # Hidden page-world JavaScript is forbidden. Notification and fresh
        # AXTree challenge detection remain the authoritative control signals.
        _ = (control, page_id, expression, result_key, flag, match)
        return None

    return poll


async def run_workflow_with_control(
    *,
    primary: Any,
    control: ControlChannel,
    skill: Any,
    run_id: str,
    page_id: str,
    fleet_id: str,
    variables: Dict[str, Any],
    on_pause: PauseResolver,
    onset_timeout: float = 1200.0,
    poll_interval: float = 2.5,
    poll_challenge_fn: Optional[Callable[..., Awaitable[Optional[Dict[str, Any]]]]] = None,
    max_cycles: int = 3,
    logger: Any = None,
    primary_runner: Callable[..., Awaitable[Dict[str, Any]]] = run_skill_workflow,
) -> Dict[str, Any]:
    """Run a skill workflow on the primary while resolving any observed challenge at
    the PAGE level over the control channel. The workflow is NOT frozen (engine
    pause is impossible cross-connection) — it must carry a challenge-gated
    `listen Hitl.resumed` boundary so it waits while `on_pause` clears the page; the
    `Hitl.resumed` that ends the page pause also satisfies that listen.

    Returns the normalized run_result with an added `interventions` list. Fail-safe:
    any control error is swallowed and the workflow finishes/fails naturally (then
    classify → hand-off downstream)."""

    exec_task = asyncio.create_task(primary_runner(
        primary, skill, run_id=run_id,
        page_id=page_id, fleet_id=fleet_id, variables=variables,
    ))
    interventions: List[Dict[str, Any]] = []

    try:
        cycles = 0
        while not exec_task.done() and cycles < max_cycles:
            signal = await _wait_for_challenge(
                primary, control, page_id, exec_task,
                onset_timeout=onset_timeout, poll_interval=poll_interval,
                poll_challenge_fn=poll_challenge_fn,
            )
            if signal is None:
                break  # execute finished (or timed out) without a challenge
            cycles += 1
            interventions.append(
                await _handle_challenge(control, page_id, signal, on_pause, logger)
            )
        run_result = await exec_task
    finally:
        if not exec_task.done():
            try:
                run_result = await exec_task
            except Exception as exc:  # pragma: no cover - surfaced as failed run
                run_result = {"succeeded": False, "runId": run_id, "exc": str(exc)}

    run_result["interventions"] = interventions
    return run_result


async def _handle_challenge(
    control: ControlChannel,
    page_id: str,
    signal: Dict[str, Any],
    on_pause: PauseResolver,
    logger: Any,
) -> Dict[str, Any]:
    """Resolve one observed challenge at the page level. Never raises."""
    record: Dict[str, Any] = {"signal": signal, "resolved": False}
    _log(logger, "skill.control.challenge_onset", {"pageId": page_id, "signal": signal})
    try:
        record["resolved"] = bool(await on_pause(control, page_id, signal))
    except Exception as exc:
        record["error"] = str(exc)
        _log(logger, "skill.control.resolve_error", {"pageId": page_id, "error": str(exc)})
    _log(logger, "skill.control.challenge_resolved",
         {"pageId": page_id, "resolved": record["resolved"]})
    return record


async def resolve_via_hitl(
    control: ControlChannel,
    page_id: str,
    signal: Dict[str, Any],
    *,
    timeout_seconds: float = 1200.0,
    poll_interval_seconds: float = 2.0,
    logger: Any = None,
) -> bool:
    """Default human-path resolver (doc §8.1): page-level `Hitl.requestPause` (so the
    human sees the challenge in the playground), then wait for the authoritative
    `Hitl.resumed` (the playground's resolvePause). Returns True iff resumed.

    All primitives here are live-verified to work cross-connection. VL-first
    auto-solve is a future `on_pause` that drives the page (Input/DOM on the 2nd
    connection) then calls `control.resolve_pause(page_id)` before falling through
    to this human path."""
    from harness.hitl import wait_for_hitl_resume

    try:
        await control.request_pause(page_id)
    except Exception as exc:
        _log(logger, "skill.control.request_pause_error", {"pageId": page_id, "error": str(exc)})
        # If the page is already HITL-paused, continue to wait anyway.
    try:
        outcome = await wait_for_hitl_resume(
            browser=control.client,
            page_id=page_id,
            timeout_seconds=timeout_seconds,
            poll_interval_seconds=poll_interval_seconds,
            diagnostics=None,
            logger=logger,
        )
    except Exception as exc:
        _log(logger, "skill.control.wait_resume_error", {"pageId": page_id, "error": str(exc)})
        return False
    return outcome.get("status") == "resumed"


# ── VL-first resolver (§13.4 captcha_solve driver) ──────────────────────────────
# VL classifies the challenge and (only for visual_self_consistent puzzles) emits a
# solve_plan of normalized 0-1000 steps; the harness maps them to CSS, safety-checks
# each point, drives Input over the control channel, re-verifies, and on success
# resolves the page pause (emits Hitl.resumed → workflow continues). Behavioral-risk
# / unknown short-circuit to the human path (enforced in vl._finalize_captcha_solve).


# The translation and the bounded solve loop itself are transport-agnostic and
# shared with the worker hot path (harness.tools.browser_tools.captcha_autosolve),
# which runs the same ladder BEFORE any Hitl.requestPause. This module keeps only
# the control-channel bindings (2nd connection, already-paused page).


async def _default_metrics(control: "ControlChannel", page_id: str) -> Dict[str, Any]:
    resp = await control.call("Page.getState", {
        "pageId": page_id,
        "purpose": "read native viewport metrics for VL coordinate mapping",
    })
    data = ((resp or {}).get("data") or {})
    viewport = data.get("viewport") if isinstance(data, dict) else None
    viewport = viewport if isinstance(viewport, dict) else {}
    if isinstance(data, dict):
        return {
            "w": viewport.get("width") or data.get("viewportWidth") or 0,
            "h": viewport.get("height") or data.get("viewportHeight") or 0,
        }
    return {"w": 0, "h": 0}


async def _default_safety(control: "ControlChannel", page_id: str, x: Any, y: Any) -> bool:
    """Live element guard before any VL-proposed action.

    Reads the WHOLE elementsFromPoint stack (topmost + ancestors) and delegates the
    verdict to the shared `captcha_point_is_safe` block-list, so this path and the
    worker hot path cannot drift apart again: a transparent node over a "Sign in"
    button must not pass, while a puzzle inside a login dialog still may.

    ABCP currently exposes no native hit-test operation on this connection, so
    the gate fails CLOSED (no action). Runtime.evaluate is not used to emulate
    one because it would enter page execution space and weaken risk controls."""
    # Without a native hit-test API, fail closed instead of evaluating page JS.
    _ = (control, page_id, x, y)
    return False


async def _default_text_target_safety(control: "ControlChannel", page_id: str) -> bool:
    """OCR guard: what has focus now is what will receive the typed answer, so it
    must be a plain text box (shared allow-list) and never a credential field."""
    # Focus safety cannot be proven natively on this control connection.
    _ = (control, page_id)
    return False


async def _default_verify(
    control: "ControlChannel", page_id: str, vl_config: Any
) -> Optional[bool]:
    """Tri-state clearance check: True cleared, False still blocked, None unknown.

    Two independent gates, mirroring the worker hot path: Page.getState url/title,
    then a fresh AXTree run through the structural challenge detector (which is
    what catches an in-page slider that mutates no url/title). An unreadable
    Page.getState or an empty AXTree returns None — "the probes saw nothing" is
    never clearance, because only a positive clear skips the human."""
    from harness.observation.challenge_detector import detect_structural_challenge_from_lines
    from harness.skill.pause import _is_challenge_url, _title_is_challenge

    resp = await control.call("Page.getState", {
        "pageId": page_id, "purpose": "confirm the challenge cleared after VL solve",
    })
    data = ((resp or {}).get("data") or {})
    if not isinstance(data, dict) or not data:
        return None
    url = str(data.get("url") or "")
    title = str(data.get("title") or "")
    if _is_challenge_url(url) or _title_is_challenge(title):
        return False

    tree = await control.call("DOM.getAXTree", {
        "pageId": page_id,
        "purpose": "verify no embedded verification frame remains after the VL solve",
    })
    lines = ((tree or {}).get("data") or {})
    lines = lines.get("lines") if isinstance(lines, dict) else None
    if not isinstance(lines, list) or not lines:
        return None  # cannot rule the frame out
    if detect_structural_challenge_from_lines(
        [str(line) for line in lines], source_method="DOM.getAXTree"
    ):
        return False
    return True


async def resolve_via_vl_then_hitl(
    control: "ControlChannel",
    page_id: str,
    signal: Dict[str, Any],
    *,
    vl_config: Any = None,
    captcha_solve_fn: Optional[Callable[..., Awaitable[Dict[str, Any]]]] = None,
    screenshot_fn: Optional[Callable[..., Awaitable[Optional[str]]]] = None,
    metrics_fn: Callable[..., Awaitable[Dict[str, Any]]] = _default_metrics,
    safety_fn: Callable[..., Awaitable[bool]] = _default_safety,
    exec_fn: Optional[Callable[..., Awaitable[Any]]] = None,
    verify_fn: Callable[..., Awaitable[Optional[bool]]] = _default_verify,
    max_retries: int = 2,
    budget_seconds: float = 180.0,
    timeout_seconds: float = 1200.0,
    poll_interval_seconds: float = 2.0,
    logger: Any = None,
) -> bool:
    """VL-first page-level resolver (the §13.4 closed loop), with the human path as
    the safety net. Tries to VL-solve a *visual* CAPTCHA over the control channel;
    on success resolves the page pause; on unsolvable / behavioral / uncertain /
    retries-exhausted / VL disabled, falls through to `resolve_via_hitl`.

    I/O is injectable (screenshot/VL/metrics/safety/exec/verify) so the orchestration
    is unit-testable; defaults wire the live-verified Page/Input/Runtime primitives."""
    if vl_config is not None and getattr(vl_config, "enabled", False) and screenshot_fn is not None:
        # Ownership before any pixel: driving a challenge mutates shared session
        # state, so same-fleet workers must be serialized here exactly as they are
        # on the worker hot path. Losing the claim means someone else owns
        # recovery — go straight to the human path without touching the page.
        claimed, claim_receipt = await _claim_barrier_for_vl_solve(control, page_id, logger)
        if not claimed:
            _log(logger, "skill.control.vl_solve_gated", {
                "pageId": page_id, **claim_receipt,
            })
            return await resolve_via_hitl(
                control, page_id, signal,
                timeout_seconds=timeout_seconds,
                poll_interval_seconds=poll_interval_seconds,
                logger=logger,
            )
        try:
            resolved = await _vl_solve_loop(
                control, page_id, vl_config,
                captcha_solve_fn=captcha_solve_fn or _default_captcha_solve,
                screenshot_fn=screenshot_fn, metrics_fn=metrics_fn,
                safety_fn=safety_fn, exec_fn=exec_fn or _default_exec,
                verify_fn=verify_fn, max_retries=max_retries,
                budget_seconds=budget_seconds, logger=logger,
            )
            if resolved:
                return True
        except Exception as exc:  # VL path must never break the fall-back
            _log(logger, "skill.control.vl_solve_error", {"pageId": page_id, "error": str(exc)})
    # Fall through: human path (verified cross-connection).
    return await resolve_via_hitl(
        control, page_id, signal,
        timeout_seconds=timeout_seconds, poll_interval_seconds=poll_interval_seconds,
        logger=logger,
    )


async def _vl_solve_loop(
    control: "ControlChannel",
    page_id: str,
    vl_config: Any,
    *,
    captcha_solve_fn: Callable[..., Awaitable[Dict[str, Any]]],
    screenshot_fn: Callable[..., Awaitable[Optional[str]]],
    metrics_fn: Callable[..., Awaitable[Dict[str, Any]]],
    safety_fn: Callable[..., Awaitable[bool]],
    exec_fn: Callable[..., Awaitable[Any]],
    verify_fn: Callable[..., Awaitable[Optional[bool]]],
    max_retries: int,
    logger: Any,
    budget_seconds: float = 0.0,
    text_target_safety_fn: Callable[..., Awaitable[bool]] = _default_text_target_safety,
) -> bool:
    """Control-channel binding of the shared Role C loop (harness.vl.captcha).

    The engine owns the ladder and the budget; this wrapper only binds the
    2nd-connection I/O and adds the control-channel-specific success action:
    emitting `Hitl.resumed` so the blocked workflow's `listen` continues."""

    async def guarded_exec(method: str, params: Dict[str, Any]) -> Any:
        if method == "Input.type" and not await text_target_safety_fn(control, page_id):
            raise RuntimeError(
                "refusing to type an OCR answer: the focused element is not a"
                " plain text box"
            )
        return await exec_fn(control, method, params)

    receipt = await run_captcha_solve_loop(
        page_id=page_id,
        vl_config=vl_config,
        screenshot_fn=lambda: screenshot_fn(control, page_id),
        captcha_solve_fn=captcha_solve_fn,
        metrics_fn=lambda: metrics_fn(control, page_id),
        safety_fn=lambda x, y: safety_fn(control, page_id, x, y),
        exec_fn=guarded_exec,
        verify_fn=lambda: verify_fn(control, page_id, vl_config),
        max_attempts=max_retries,
        budget_seconds=budget_seconds,
        min_confidence=float(getattr(vl_config, "captcha_solve_min_confidence", 0.0) or 0.0),
        min_confidence_ocr=float(
            getattr(vl_config, "captcha_solve_min_confidence_ocr", 0.0) or 0.0
        ),
        logger=logger,
        log_prefix="skill.control",
    )
    status = str(receipt.get("status") or "")
    if status == captcha.NOT_A_CHALLENGE:
        return True  # misdetection — page is fine, let the workflow continue
    if status == captcha.SOLVED:
        await control.signal_resumed(page_id)  # emit Hitl.resumed → workflow listen continues
        return True
    return False  # behavioral / unsafe / exhausted / error → human path


async def _claim_barrier_for_vl_solve(
    control: "ControlChannel",
    page_id: str,
    logger: Any,
) -> tuple[bool, Dict[str, Any]]:
    """Claim the fleet auth barrier before a VL solve touches the page.

    Mirrors the worker hot path: the same claim the human path makes, taken
    earlier, so two same-fleet workers can never drive one challenge. Claiming is
    idempotent for the owner, so the HITL fall-through inherits this ownership."""
    barrier = getattr(control, "_fleet_auth_barrier", None)
    fleet_id = str(getattr(control, "_fleet_id", "") or "").strip()
    worker_id = str(getattr(control, "_worker_id", "") or "").strip()
    if barrier is None or not fleet_id or not worker_id:
        return True, {"enabled": False}
    claim = await barrier.claim(
        fleet_id, worker_id, f"VL captcha auto-solve on {page_id}"
    )
    return bool(claim.get("claimed")), {
        "enabled": True,
        "fleetId": fleet_id,
        "workerId": worker_id,
        "claimed": bool(claim.get("claimed")),
        "resolverWorkerId": claim.get("resolverWorkerId"),
        "generation": claim.get("generation"),
    }


async def _default_captcha_solve(vl_config: Any, image_path: str) -> Dict[str, Any]:
    from harness.vl import visual_verify_image
    return await visual_verify_image(
        config=vl_config, image_path=image_path, expected={}, mode="captcha_solve",
        question="Classify this challenge and, only if it is a purely visual puzzle, give a solve plan.",
    )


async def _default_screenshot(control: "ControlChannel", page_id: str) -> Optional[str]:
    """Page.screenshot returns data.savedPath (encoding='file') — a path the VL
    helper reads directly. Returns the path or None."""
    resp = await control.call("Page.screenshot", {
        "pageId": page_id, "fullPage": False,
        "options": {"format": "file"},
        "purpose": "capture the challenge for VL captcha_solve",
    })
    data = ((resp or {}).get("data") or {})
    path = data.get("savedPath") or (data.get("data") if data.get("encoding") == "file" else None)
    return path if isinstance(path, str) and path else None


async def _default_exec(control: "ControlChannel", method: str, params: Dict[str, Any]) -> Any:
    return await control.call(method, params)


def _log(logger: Any, event: str, payload: Dict[str, Any]) -> None:
    if logger is not None and hasattr(logger, "write"):
        try:
            logger.write(event, payload)
        except Exception:  # pragma: no cover
            pass
