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
mid-execute, the control channel resolves the *page* (human via
`Hitl.requestPause`→wait `Hitl.resumed`, or VL auto-solve as an `on_pause`
extension). The workflow continues on its own — it must be authored with a
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

# on_pause resolver: (control, page_id, onset_signal) -> resolved? (True ⇒ page cleared)
PauseResolver = Callable[["ControlChannel", str, Dict[str, Any]], Awaitable[bool]]


class ControlChannel:
    """A second ABCP connection for PAGE-level control while the primary is blocked
    in Workflow.execute. Duck-typed inner client: anything with async `call`,
    `connect`, `close` (i.e. abcp_client.ABCPClient). Engine-level Workflow.pause/
    resume are intentionally absent — they are session-bound and unreachable here."""

    def __init__(self, client: Any, *, owns_client: bool = True, logger: Any = None):
        self.client = client
        self._owns_client = owns_client
        self._logger = logger
        self._open = False

    @classmethod
    def from_browser(cls, browser: Any, *, logger: Any = None) -> Optional["ControlChannel"]:
        """Build a control channel mirroring the primary browser's connection
        config (ws_url + auth). Returns None if the primary exposes no config."""
        config = getattr(browser, "config", None)
        if config is None:
            return None
        try:
            from abcp_client import ABCPClient
        except Exception:  # pragma: no cover - import environment guard
            return None
        return cls(ABCPClient(config), owns_client=True, logger=logger)

    async def try_open(self) -> bool:
        """Connect the control channel. Never raises — returns False on failure so
        the caller degrades to the observe-only hand-off."""
        connect = getattr(self.client, "connect", None)
        try:
            if callable(connect):
                await connect()
            self._open = True
            return True
        except Exception as exc:
            self._log("skill.control.open_failed", {"error": str(exc)})
            self._open = False
            return False

    async def call(self, method: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        return await self.client.call(method, params or {})

    async def request_pause(self, page_id: str, reason: str = "skill workflow challenge") -> Dict[str, Any]:
        """Page-level HITL pause (cross-connection verified)."""
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
        try:
            resp = await control.call("Runtime.evaluate", {
                "pageId": page_id, "returnByValue": True, "expression": expression,
                "purpose": "active in-page challenge poll (2nd connection) mirroring the skill's challengeFlag detection",
            })
        except Exception:
            return None
        data = ((resp or {}).get("data") or {})
        value = data.get(result_key) if isinstance(data, dict) else None
        if _flag_matches(value, match):
            return {"kind": "challenge_in_page", "flag": flag, "value": value,
                    "pageId": page_id}
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


def _norm_to_css(point: Dict[str, Any], w: float, h: float) -> tuple[float, float]:
    """Map a normalized 0-1000 grounding point to CSS px (qwen-VL convention:
    css = norm/1000 * innerWidth; DPR/image-size independent)."""
    return float(point.get("x", 0.0)) / 1000.0 * w, float(point.get("y", 0.0)) / 1000.0 * h


def solve_plan_to_input_calls(
    solve_plan: List[Dict[str, Any]],
    page_id: str,
    metrics: Dict[str, Any],
    *,
    purpose: str = "VL-driven CAPTCHA solve step",
) -> List[tuple[str, Dict[str, Any]]]:
    """Pure translation: normalized SolveSteps → ordered (method, params) Input
    calls, using viewport metrics {w,h} to back-translate to CSS px."""
    w, h = float(metrics.get("w") or 0.0), float(metrics.get("h") or 0.0)
    calls: List[tuple[str, Dict[str, Any]]] = []
    for step in solve_plan:
        action = step.get("action")
        if action == "drag":  # slider: source + relative offset
            x, y = _norm_to_css(step["from"], w, h)
            dx = float(step.get("dx", 0.0)) / 1000.0 * w
            dy = float(step.get("dy", 0.0)) / 1000.0 * h
            calls.append(("Input.drag", {"pageId": page_id, "x": x, "y": y,
                                         "dx": dx, "dy": dy, "purpose": purpose}))
        elif action == "drag_arc":  # rotate: source → destination
            x, y = _norm_to_css(step["from"], w, h)
            tx, ty = _norm_to_css(step["to"], w, h)
            calls.append(("Input.drag", {"pageId": page_id, "x": x, "y": y,
                                         "toX": tx, "toY": ty, "purpose": purpose}))
        elif action == "click":  # grid / click_target
            x, y = _norm_to_css(step["at"], w, h)
            calls.append(("Input.click", {"pageId": page_id, "x": x, "y": y,
                                          "clickCount": 1, "purpose": purpose}))
        elif action == "type":  # text_ocr: focus then type
            x, y = _norm_to_css(step["into"], w, h)
            calls.append(("Input.click", {"pageId": page_id, "x": x, "y": y,
                                          "clickCount": 1, "purpose": purpose}))
            calls.append(("Input.type", {"pageId": page_id, "text": step.get("text", ""),
                                         "clear": True, "delay": 40, "purpose": purpose}))
    return calls


async def _default_metrics(control: "ControlChannel", page_id: str) -> Dict[str, Any]:
    resp = await control.call("Runtime.evaluate", {
        "pageId": page_id, "returnByValue": True,
        "expression": "return {w: window.innerWidth, h: window.innerHeight};",
        "purpose": "read viewport size to map VL normalized coords to CSS px",
    })
    data = ((resp or {}).get("data") or {})
    if isinstance(data, dict) and ("w" in data or "h" in data):
        return {"w": data.get("w") or 0, "h": data.get("h") or 0}
    return {"w": 0, "h": 0}


async def _default_safety(control: "ControlChannel", page_id: str, x: Any, y: Any) -> bool:
    """elementFromPoint guard: refuse to act on consequential controls (login/pay/
    submit/oauth) — VL hallucination must not click a sensitive button."""
    if x is None or y is None:
        return False
    resp = await control.call("Runtime.evaluate", {
        "pageId": page_id, "returnByValue": True,
        "expression": (
            f"var el=document.elementFromPoint({float(x)},{float(y)});"
            "if(!el)return {ok:false};"
            "var t=((el.innerText||el.value||el.getAttribute('aria-label')||'')+' '+(el.name||'')+' '+(el.id||'')).toLowerCase();"
            "var bad=['log in','login','sign in','sign up','signup','subscribe','pay','purchase','checkout','continue with google','continue with apple','delete','confirm order'];"
            "return {ok: !bad.some(function(s){return t.indexOf(s)>=0;}), tag: el.tagName};"
        ),
        "purpose": "safety-check the VL target point before acting (avoid sensitive controls)",
    })
    data = ((resp or {}).get("data") or {})
    return bool(isinstance(data, dict) and data.get("ok"))


async def _default_verify(control: "ControlChannel", page_id: str, vl_config: Any) -> bool:
    """L0 authority check: the challenge is gone iff Page.getState url/title no
    longer reads as a challenge. (VL does not self-verify — doc §7.1 ladder.)

    LIMITATION: this only catches NAVIGATION-level challenges (Cloudflare etc. that
    change url/title). An IN-PAGE widget (a slider that mutates no url/title) reads as
    "gone" by default here — i.e. this default is permissive for in-page challenges.
    So an in-page solve MUST pass its own `verify_fn` (e.g. polling the widget text),
    as the yue slider e2e does; the VL-solve loop's max_retries + the downstream
    contract_verify (Role B) are the backstop if a misjudged `solvable` never cleared."""
    from harness.skill.pause import _is_challenge_url, _title_is_challenge

    resp = await control.call("Page.getState", {
        "pageId": page_id, "purpose": "confirm the challenge cleared after VL solve",
    })
    data = ((resp or {}).get("data") or {})
    url = str(data.get("url") or "")
    title = str(data.get("title") or "")
    return not (_is_challenge_url(url) or _title_is_challenge(title))


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
    verify_fn: Callable[..., Awaitable[bool]] = _default_verify,
    max_retries: int = 2,
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
        try:
            resolved = await _vl_solve_loop(
                control, page_id, vl_config,
                captcha_solve_fn=captcha_solve_fn or _default_captcha_solve,
                screenshot_fn=screenshot_fn, metrics_fn=metrics_fn,
                safety_fn=safety_fn, exec_fn=exec_fn or _default_exec,
                verify_fn=verify_fn, max_retries=max_retries, logger=logger,
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
    verify_fn: Callable[..., Awaitable[bool]],
    max_retries: int,
    logger: Any,
) -> bool:
    metrics = await metrics_fn(control, page_id)
    for attempt in range(max(1, int(max_retries))):
        shot = await screenshot_fn(control, page_id)
        if not shot:
            _log(logger, "skill.control.vl_no_screenshot", {"pageId": page_id})
            return False
        vl = await captcha_solve_fn(vl_config, shot)
        verdict = str(vl.get("verdict") or "uncertain")
        _log(logger, "skill.control.vl_verdict", {
            "pageId": page_id, "attempt": attempt, "verdict": verdict,
            "challenge_category": vl.get("challenge_category"),
            "challenge_type": vl.get("challenge_type"),
            "steps": len(vl.get("solve_plan") or []),
        })
        if verdict == "not_a_challenge":
            return True  # misdetection — page is fine, let the workflow continue
        plan = vl.get("solve_plan") or []
        if verdict != "solvable" or not plan:
            return False  # behavioral / unsolvable / uncertain → human path
        calls = solve_plan_to_input_calls(plan, page_id, metrics)
        aborted = False
        for method, params in calls:
            # Only coordinate-bearing calls go through elementFromPoint safety. A
            # coordinate-less Input.type (text_ocr: focus-then-type) follows an
            # already-safety-checked Input.click; gating it on its absent x/y would
            # make _default_safety reject it and abort the whole solve.
            has_coords = params.get("x") is not None and params.get("y") is not None
            if has_coords and not await safety_fn(control, page_id, params.get("x"), params.get("y")):
                _log(logger, "skill.control.vl_unsafe_point",
                     {"pageId": page_id, "x": params.get("x"), "y": params.get("y")})
                aborted = True
                break
            await exec_fn(control, method, params)
        if aborted:
            return False  # a sensitive control was in the way → human path
        if await verify_fn(control, page_id, vl_config):
            await control.signal_resumed(page_id)  # emit Hitl.resumed → workflow listen continues
            _log(logger, "skill.control.vl_solved", {"pageId": page_id, "attempt": attempt})
            return True
        # not gone → retry (slider micro-adjust / next grid pass)
    _log(logger, "skill.control.vl_exhausted", {"pageId": page_id, "retries": max_retries})
    return False


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
