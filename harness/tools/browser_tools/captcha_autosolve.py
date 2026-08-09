"""Worker hot-path binding of the VL CAPTCHA auto-solve (harness.vl.captcha).

Ordering contract: this runs BEFORE `Hitl.requestPause`. The worker's own
connection is free (unlike the skill control channel, the page is not paused
yet), so the solve drives Input directly and a human is only asked once the
bounded attempts are spent.

Every leg is deliberately independent of the VL that proposed the action:

  * viewport + coarse point classification come from fresh native AXTree
    snapshots; when a point resolves to a canonical node, the eventual Input
    action is promoted to that id so ABCP performs the final native hit-test;
  * the block-list check (`overlay_actions.captcha_point_is_safe`) aborts on a
    login/pay control. A missing positioned node is reported as unavailable
    safety capability, not mislabeled as an unsafe target. Text OCR is handed to
    HITL because the current native tree has no trustworthy focused-element
    signal;
  * clearance is decided deterministically first (Page.getState title/url, then
    a fresh AXTree run through the structural challenge detector, which is what
    catches an in-page slider that changes no url/title), and only a VL-only
    detection falls back to a VL re-check.

All browser calls use `count_progress=False`, so an auto-solve costs the model
no step, and `agent.challenge_adjudicating` is held for the whole episode so the
solve's own Input/screenshot traffic cannot re-enter challenge adjudication.
"""

from __future__ import annotations

import shutil
import time
import uuid
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from harness.call_outcome import classify_call_outcome
from harness.challenge_detector import (
    VL_CLEARANCE_VERDICTS,
    detect_structural_challenge_from_lines,
    title_looks_like_challenge,
)
from harness.observation.overlay_actions import (
    captcha_point_is_safe,
)
from harness.utils import (
    JsonDict,
    optional_int,
    safe_path_component,
    task_subdir,
)
from harness.vl import captcha
from harness.vl.captcha import run_captcha_solve_loop
from harness.vl.locate import main_frame_id, parse_axtree_bboxes

# Episode outcomes that mean "the page is usable; do not pause".
CLEARED_STATUSES = captcha.CLEARED_STATUSES


def _bt() -> Any:
    import harness.tools.browser_tools as bt

    return bt


class _SolveAborted(RuntimeError):
    """A solve leg failed in a way that must hand the page to a human."""


def _vl_config(agent: Any) -> Any:
    return getattr(getattr(getattr(agent, "runtime", None), "harness", None), "vl", None)


def autosolve_enabled(agent: Any) -> bool:
    """Role C needs BOTH `vl.enabled` and `vl.captcha_solve_enabled`
    (operator decision, 2026-08-05, superseding the 2026-07-31 single-switch
    decision): the master switch keeps every visual capability behind one flag,
    and the second one scopes the CAPTCHA role alone.

    The consequential-action guards are still NOT config flags: bounded
    attempts, the wall-clock budget, per-worker episodes, the type allow-list,
    confidence floors, the live element safety gates, and the independent
    clearance verifier all apply regardless, and behavioral-risk challenges are
    never driven at all.
    """
    vl_config = _vl_config(agent)
    if vl_config is None:
        return False
    allowed = getattr(vl_config, "captcha_autosolve_allowed", None)
    if callable(allowed):
        return bool(allowed())
    # Lightweight test doubles may carry only the raw fields.
    return bool(
        getattr(vl_config, "enabled", False)
        and getattr(vl_config, "captcha_solve_enabled", True)
    )


def _confidence_setting(vl_config: Any, name: str, default: float) -> float:
    """Read a confidence floor, clamped to [0, 1]. A malformed value falls back
    to the default rather than silently disabling the gate."""
    try:
        value = float(getattr(vl_config, name, default))
    except (TypeError, ValueError):
        value = default
    return max(0.0, min(value, 1.0))


async def _claim_auth_barrier(agent: Any, reason: str) -> Tuple[bool, JsonDict]:
    """Become the fleet's challenge resolver before touching the page.

    Same claim the HITL path makes, taken earlier: driving a CAPTCHA changes
    shared session state, so it must be serialized across same-fleet workers.
    Claiming is idempotent for the owner, so the later HITL claim is a no-op when
    the solve fails and we hand the page over while keeping ownership.
    """
    barrier = getattr(agent, "fleet_auth_barrier", None)
    fleet_id = str(getattr(agent, "assigned_fleet_id", "") or "").strip()
    worker_id = str(getattr(agent, "worker_id", "") or "").strip()
    if barrier is None or not fleet_id or not worker_id:
        return True, {"enabled": False}
    claim = await barrier.claim(
        fleet_id, worker_id, f"VL captcha auto-solve: {reason}"[:500]
    )
    return bool(claim.get("claimed")), {
        "enabled": True,
        "fleetId": fleet_id,
        "workerId": worker_id,
        **{key: claim.get(key) for key in ("claimed", "resolverWorkerId", "generation")},
    }


def skill_forbids_autosolve(agent: Any) -> Optional[JsonDict]:
    """Honor a bound skill's `allow_auto_captcha: false` declaration.

    This is the enforcement point for that frontmatter flag. It used to live in
    the skill dispatch gate; once the skill workflow path stopped choosing a VL
    resolver, the flag had no executor anywhere, which silently turned a
    site-level "do not auto-solve my challenges" declaration into dead metadata.
    Default-deny is preserved: a worker bound to a skill may auto-solve only when
    that skill opts in. A worker with no skill is governed by config alone.
    """
    contract = getattr(agent, "worker_contract", None)
    skill_id = ""
    if isinstance(contract, dict):
        skill_id = str(contract.get("skill_id") or "").strip()
    if not skill_id:
        return None
    try:
        from harness.skill.registry import SkillRegistry

        skill = SkillRegistry().get(skill_id)
    except Exception:
        skill = None
    if skill is None:
        return None
    frontmatter = getattr(skill, "frontmatter", None) or {}
    if bool(frontmatter.get("allow_auto_captcha", False)):
        return None
    return {
        "status": "skill_forbids_auto_captcha",
        "reason": (
            f"skill {skill_id!r} does not declare allow_auto_captcha: true;"
            " its challenges are resolved by a human"
        ),
        "skillId": skill_id,
    }


def _episode_budget(agent: Any) -> Tuple[int, int]:
    vl_config = _vl_config(agent)
    raw = optional_int(getattr(vl_config, "captcha_solve_max_episodes_per_worker", 2), 2)
    limit = max(0, raw if raw is not None else 2)
    used = int(getattr(agent, "captcha_autosolve_episodes", 0) or 0)
    return used, limit


def _page_is_blacklisted(agent: Any, page_id: str) -> bool:
    """A page whose solve already failed belongs to the human path; never spend a
    second episode (and a second stretch of the user's waiting time) on it."""
    failed = getattr(agent, "captcha_autosolve_failed_pages", None)
    return bool(isinstance(failed, set) and str(page_id) in failed)


def _blacklist_page(agent: Any, page_id: str) -> None:
    failed = getattr(agent, "captcha_autosolve_failed_pages", None)
    if not isinstance(failed, set):
        failed = set()
        agent.captcha_autosolve_failed_pages = failed
    failed.add(str(page_id))


_SCREENSHOT_ATTEMPTS = 2


async def _capture_screenshot(agent: Any, page_id: str, step: int, purpose: str) -> Optional[str]:
    """Capture the challenge viewport, retrying once on an empty saved path.

    A CAPTCHA page is exactly where the screenshot pipeline is most likely to
    hiccup, and a missing path costs the whole episode: task 48b4d7d7 lost
    browser-003 to `no_screenshot` with `vlCalls: 0` — the model was never even
    asked. The retry keeps the SAME viewport framing: `fullPage=True` would
    change the image extent, and VL's normalized coordinates are mapped through
    the viewport, so a full-page image silently mis-scales every drag.
    """
    bt = _bt()
    logger = getattr(agent, "logger", None)
    for attempt in range(1, _SCREENSHOT_ATTEMPTS + 1):
        before = {str(path) for path in getattr(agent, "artifacts", [])}
        shot = await bt._invoke_browser_method(
            agent,
            "Page.screenshot",
            {
                "pageId": page_id,
                "fullPage": False,
                "options": {"format": "file"},
                "purpose": purpose,
            },
            step,
            count_progress=False,
        )
        path = bt._screenshot_saved_path(shot)
        if not path:
            added = [
                str(item) for item in getattr(agent, "artifacts", [])
                if str(item) not in before
            ]
            path = added[-1] if added else ""
        if path:
            if attempt > 1 and logger is not None:
                logger.write("vl.captcha_autosolve.screenshot_retry_recovered", {
                    "pageId": page_id,
                    "attempt": attempt,
                })
            _retain_challenge_screenshot(agent, path, page_id, step)
            return path
        if logger is not None:
            logger.write("vl.captcha_autosolve.screenshot_attempt_failed", {
                "pageId": page_id,
                "attempt": attempt,
                "attempts": _SCREENSHOT_ATTEMPTS,
                "error": classify_call_outcome(shot).error[:200],
            })
    return None


def _retain_challenge_screenshot(
    agent: Any, source_path: str, page_id: str, step: int
) -> None:
    """Copy the frame the VL judged into the run's own observations directory.

    The panel writes screenshots to the OS temp directory, so the single most
    important artifact of a challenge post-mortem — what the model actually
    looked at when it declared a challenge unsolvable — is gone by the time
    anyone investigates. Keeping a copy next to the run log is what makes a
    verdict auditable after the fact.

    Best effort only: a failed copy must never break an in-flight solve.
    """

    logger = getattr(agent, "logger", None)
    if logger is None:
        return
    try:
        source = Path(source_path)
        if not source.is_file():
            return
        target_dir = task_subdir(logger, "observations")
        suffix = source.suffix or ".png"
        name = "-".join(part for part in (
            safe_path_component(
                str(getattr(agent, "agent_id", "") or ""), ""
            ),
            f"captcha-step{step or 0}",
            safe_path_component(page_id, "page"),
            uuid.uuid4().hex[:8],
        ) if part)
        target = target_dir / f"{name}{suffix}"
        shutil.copyfile(source, target)
    except Exception as exc:  # noqa: BLE001 - never break a solve on artifacts
        logger.write("vl.captcha_screenshot.retain_failed", {
            "pageId": page_id,
            "sourcePath": str(source_path)[:300],
            "errorType": type(exc).__name__,
            "error": str(exc)[:200],
        })
        return
    logger.write("vl.captcha_screenshot.retained", {
        "pageId": page_id,
        "step": step,
        "sourcePath": str(source_path)[:300],
        "retainedPath": str(target),
    })


async def _native_axtree_snapshot(
    agent: Any,
    page_id: str,
    step: int,
    *,
    purpose: str,
) -> JsonDict:
    """Refresh AXTree and return main-frame viewport/bboxes.

    No hidden JS is used.  If ABCP offloads the raw result, the browser-tools
    AXTree observer has already populated ``agent.axtree_lines`` before the
    offload occurs, so bbox safety remains available.
    """
    bt = _bt()
    tree = await bt._invoke_browser_method(
        agent,
        "DOM.getAXTree",
        {"pageId": page_id, "purpose": purpose},
        step,
        count_progress=False,
    )
    if bt._invoke_result_failed(tree):
        return {"status": "unavailable", "reason": "DOM.getAXTree failed"}
    lines = list(getattr(agent, "axtree_lines", []) or bt._axtree_lines_from_value(tree))
    bboxes = parse_axtree_bboxes(lines)
    if not lines or not bboxes:
        return {"status": "unavailable", "reason": "AXTree contained no positioned nodes"}

    viewport = bt._viewport_from_layers(bt._layers_from_result(tree))
    width = viewport.get("width", viewport.get("w")) if isinstance(viewport, dict) else None
    height = viewport.get("height", viewport.get("h")) if isinstance(viewport, dict) else None
    try:
        width_f, height_f = float(width or 0), float(height or 0)
    except (TypeError, ValueError):
        width_f, height_f = 0.0, 0.0
    frame = main_frame_id(bboxes, shot_w=width_f or None, shot_h=height_f or None)
    if width_f <= 0 or height_f <= 0:
        roots = [
            box for box in bboxes
            if str(box.get("frame") or "") == str(frame or "")
            and str(box.get("role") or "").lower() in {"rootwebarea", "webarea", "document"}
            and float(box.get("w") or 0) > 0
            and float(box.get("h") or 0) > 0
        ]
        if roots:
            root = max(roots, key=lambda item: float(item.get("area") or 0))
            width_f, height_f = float(root["w"]), float(root["h"])
    if width_f <= 0 or height_f <= 0 or not frame:
        return {"status": "unavailable", "reason": "main-frame viewport unavailable"}
    return {
        "status": "done",
        "width": width_f,
        "height": height_f,
        "dpr": 1.0,
        "frame": frame,
        "bboxes": bboxes,
    }


def _remember_viewport(agent: Any, page_id: str, metrics: JsonDict) -> None:
    cache = getattr(agent, "captcha_viewport_cache", None)
    if not isinstance(cache, dict):
        cache = {}
        agent.captcha_viewport_cache = cache
    cache[str(page_id)] = {
        "width": float(metrics["width"]),
        "height": float(metrics["height"]),
        "dpr": float(metrics.get("dpr", 1.0) or 1.0),
    }


def _recall_viewport(agent: Any, page_id: str) -> Optional[JsonDict]:
    cache = getattr(agent, "captcha_viewport_cache", None)
    if not isinstance(cache, dict):
        return None
    remembered = cache.get(str(page_id))
    if not isinstance(remembered, dict):
        return None
    if float(remembered.get("width") or 0) <= 0 or float(remembered.get("height") or 0) <= 0:
        return None
    return dict(remembered)


async def _viewport_metrics(agent: Any, page_id: str, step: int) -> JsonDict:
    """Resolve the CSS viewport VL coordinates are mapped through.

    Fallback chain, because a single `DOM.getAXTree` failure used to end the
    whole episode with `vlCalls: 0`. In task 48b4d7d7, browser-004 solved a
    CAPTCHA on this very page with a 2560x1600 viewport and, 6.5 minutes later,
    was handed to a human on the re-challenge purely because one AXTree read
    failed — the viewport had not changed at all.

    Two candidate stages were considered and dropped, both deliberately:

    * `Runtime.evaluate` reading innerWidth/innerHeight would be the exact CSS
      answer with no DPR problem — but harness-internal Runtime.evaluate is
      forbidden by policy (`runtime_internal_path_forbidden`, see the guard in
      browser_tools `_invoke_browser_method`): only the model-facing
      browser_call boundary and the registered collect_items templates may
      execute script. Adding a viewport probe would mean punching a hole in
      that policy for a fallback, so it is out until the policy grants a
      registered read-only template for page metrics.
    * Screenshot dimensions are device pixels, and ABCP's Page.getState does
      not report a device scale factor (no `deviceScaleFactor` in any observed
      response), so DPR would have to be guessed. A missing viewport costs one
      hand-off; a wrong one submits a wrong answer and burns one of the site's
      own attempts.
    """
    logger = getattr(agent, "logger", None)
    snapshot = await _native_axtree_snapshot(
        agent,
        page_id,
        step,
        purpose="captcha autosolve: refresh native viewport before mapping VL coordinates",
    )
    if snapshot.get("status") == "done":
        metrics = {
            "status": "done",
            "width": snapshot["width"],
            "height": snapshot["height"],
            "dpr": snapshot.get("dpr", 1.0),
            "source": "native_axtree",
        }
        _remember_viewport(agent, page_id, metrics)
        return metrics

    remembered = _recall_viewport(agent, page_id)
    if remembered is not None:
        if logger is not None:
            logger.write("vl.captcha_autosolve.viewport_fallback", {
                "pageId": page_id,
                "source": "last_known_viewport",
                "liveReadFailure": snapshot.get("reason"),
                "width": remembered["width"],
                "height": remembered["height"],
            })
        return {"status": "done", "source": "last_known_viewport", **remembered}

    if logger is not None:
        logger.write("vl.captcha_autosolve.viewport_exhausted", {
            "pageId": page_id,
            "liveReadFailure": snapshot.get("reason"),
            "triedSources": ["native_axtree", "last_known_viewport"],
        })
    return {"status": "oracle_unavailable", "reason": snapshot.get("reason")}


async def _point_is_safe(
    agent: Any,
    page_id: str,
    step: int,
    x: Any,
    y: Any,
    evidence: list,
    promoted_targets: Optional[Dict[Tuple[float, float], str]] = None,
) -> bool:
    snapshot = await _native_axtree_snapshot(
        agent,
        page_id,
        step,
        purpose="captcha autosolve: native point-safety refresh",
    )
    if snapshot.get("status") != "done":
        evidence.append({
            "point": [x, y],
            "classification": "point_safety_capability_unavailable",
            "reason": snapshot.get("reason") or "axtree_unavailable",
        })
        return False
    px, py = float(x), float(y)
    frame = str(snapshot.get("frame") or "")
    containing = [
        box for box in list(snapshot.get("bboxes") or [])
        if str(box.get("frame") or "") == frame
        and float(box.get("w") or 0) > 0
        and float(box.get("h") or 0) > 0
        and float(box.get("x") or 0) <= px <= float(box.get("x") or 0) + float(box.get("w") or 0)
        and float(box.get("y") or 0) <= py <= float(box.get("y") or 0) + float(box.get("h") or 0)
    ]
    # Smallest box is the most specific receiver; larger containing boxes are
    # its ancestor/context chain.  If AXTree exposes no specific node, fail
    # closed rather than treating the page root as a safe target.
    containing.sort(key=lambda item: float(item.get("area") or 0))
    stack = [
        {
            "role": str(box.get("role") or ""),
            "name": str(box.get("name") or ""),
            "text": str(box.get("name") or ""),
        }
        for box in containing
        if str(box.get("role") or "").lower() not in {"rootwebarea", "webarea", "document"}
    ]
    if not stack:
        evidence.append({
            "point": [x, y],
            "classification": "point_safety_capability_unavailable",
            "reason": "no_specific_axtree_node_at_point",
        })
        return False
    safe, detail = captcha_point_is_safe(stack)
    if not safe:
        evidence.append({
            "point": [x, y],
            "classification": "unsafe_target",
            **detail,
        })
    elif promoted_targets is not None:
        target_id = str(containing[0].get("id") or "")
        if target_id:
            promoted_targets[(px, py)] = target_id
    return safe


def _promote_input_params(
    method: str,
    params: JsonDict,
    promoted_targets: Dict[Tuple[float, float], str],
) -> JsonDict:
    """Replace a VL source coordinate with its freshly resolved canonical id."""
    out = dict(params)
    if method not in {"Input.click", "Input.drag"}:
        return out
    try:
        key = (float(out.get("x")), float(out.get("y")))
    except (TypeError, ValueError):
        return out
    target_id = promoted_targets.pop(key, "")
    if not target_id:
        return out
    out.pop("x", None)
    out.pop("y", None)
    out["id"] = target_id
    return out


async def _drive_input(
    agent: Any,
    method: str,
    params: JsonDict,
    step: int,
    *,
    evidence: Optional[list] = None,
) -> None:
    bt = _bt()
    if method == "Input.type":
        # AXTree does not expose a trustworthy focused-element signal on the
        # current ABCP build. text_ocr is excluded by the core allow-list; this
        # second guard keeps a malformed/mixed plan fail-closed.
        if isinstance(evidence, list):
            evidence.append({"step": "Input.type", "reason": "native_focus_verification_unavailable"})
        raise _SolveAborted("text input is not auto-solved without native focus verification")
    result = await bt._invoke_browser_method(agent, method, params, step, count_progress=False)
    interrupt = bt._loop_interrupt_from_result(result)
    if interrupt:
        raise _SolveAborted(
            f"{method} hit a HITL/challenge interrupt: {interrupt.get('status')}"
        )
    if bt._invoke_result_failed(result):
        raise _SolveAborted(f"{method} failed: {str(result.get('error') or '')[:160]}")


async def _clearance_check(
    agent: Any,
    page_id: str,
    step: int,
    *,
    vl_recheck: bool,
    min_confidence: float,
) -> Tuple[Optional[bool], JsonDict]:
    """Independent clearance oracle — the ONLY thing that may skip a human.

    Tri-state on purpose: `True` cleared, `False` still blocked (a retry may
    help), `None` could not be determined. Unavailability is never clearance: a
    failed Page.getState reads as an empty title, and a failed AXTree reads as an
    empty tree, so "no challenge signature found" would otherwise be
    indistinguishable from "the probes did not work".
    """
    bt = _bt()
    evidence: JsonDict = {}

    state = await bt._invoke_browser_method(
        agent,
        "Page.getState",
        {"pageId": page_id, "purpose": "captcha autosolve: confirm the challenge cleared"},
        step,
        count_progress=False,
    )
    if bt._loop_interrupt_from_result(state):
        # Someone (or the platform) already paused this page: we can neither
        # verify nor keep driving it.
        return None, {"gate": "page_state", "reason": "page reports a HITL/challenge interrupt"}
    if bt._invoke_result_failed(state):
        return None, {
            "gate": "page_state",
            "reason": "Page.getState failed; clearance is unknown",
            "error": str(state.get("error") or "")[:160],
        }
    data = bt._response_data(state) or {}
    title = str(data.get("title") or "")
    url = str(data.get("url") or "")
    evidence["title"] = title[:120]
    evidence["url"] = url[:200]
    hitl = data.get("hitl") if isinstance(data.get("hitl"), dict) else {}
    if hitl.get("isPaused") is True:
        return None, {**evidence, "gate": "page_state", "reason": "page is HITL-paused"}
    if title_looks_like_challenge(title) or _is_challenge_url(url):
        return False, {**evidence, "gate": "title_url"}

    tree = await bt._invoke_browser_method(
        agent,
        "DOM.getAXTree",
        {"pageId": page_id, "purpose": "captcha autosolve: verify the verification frame is gone"},
        step,
        count_progress=False,
    )
    if bt._invoke_result_failed(tree):
        return None, {
            "gate": "structural_axtree",
            **evidence,
            "reason": "DOM.getAXTree failed; the verification frame cannot be ruled out",
            "error": str(tree.get("error") or "")[:160],
        }
    lines = bt._axtree_lines_from_value(tree)
    if not lines:
        return None, {
            "gate": "structural_axtree",
            **evidence,
            "reason": "DOM.getAXTree returned no lines; nothing to rule the frame out with",
        }
    structural = detect_structural_challenge_from_lines(lines, source_method="DOM.getAXTree")
    if structural:
        return False, {
            **evidence,
            "gate": "structural_axtree",
            "rootLabel": str(structural.get("rootLabel") or "")[:80],
        }

    if not vl_recheck:
        return True, {**evidence, "gate": "deterministic", "axtreeLines": len(lines)}

    # The challenge was detected visually only (no url/title/AXTree signature),
    # so a deterministic "clean" reading proves nothing on its own.
    verdict = await bt._visual_verify(
        agent,
        {
            "pageId": page_id,
            "selector": "",
            "id": "",
            "fullPage": False,
            "mode": "challenge_detection",
            "_force": True,
            "question": (
                "An automated CAPTCHA solve was just attempted. Is this page still"
                " blocked by a CAPTCHA or human-verification challenge?"
            ),
            "expected": {"pageId": page_id, "phase": "post_autosolve_recheck"},
        },
        step,
    )
    name = str(verdict.get("verdict") or "uncertain")
    status = str(verdict.get("status") or "done")
    try:
        confidence = float(verdict.get("confidence"))
    except (TypeError, ValueError):
        confidence = 0.0
    detail = {
        **evidence,
        "gate": "vl_recheck",
        "verdict": name,
        "vlStatus": status,
        "confidence": confidence,
        "confidenceFloor": min_confidence,
    }
    if status != "done":
        return None, {**detail, "reason": "the clearance VL call did not complete"}
    if name == "confirmed_challenge":
        return False, detail
    if name in VL_CLEARANCE_VERDICTS and confidence >= min_confidence:
        return True, detail
    # `uncertain`, or a positive verdict the model is not confident about: no
    # positive clearance evidence exists, so the human keeps the page.
    return None, {**detail, "reason": "no confident clearance verdict"}


def _is_challenge_url(url: str) -> bool:
    from harness.hitl import _is_challenge_url as impl

    return bool(impl(url))


async def maybe_autosolve_captcha(
    agent: Any,
    page_id: str,
    step: int,
    *,
    trigger: str,
    vl_only_detection: bool,
    reason: str = "",
) -> JsonDict:
    """Attempt a bounded VL solve of the challenge on `page_id`.

    Returns a receipt. `status` in CLEARED_STATUSES means the page is usable and
    the caller must NOT request a human pause; every other status is a hand-off
    with the evidence attached. Never raises.
    """
    receipt: JsonDict = {"trigger": trigger, "pageId": page_id, "attempted": False}
    if not autosolve_enabled(agent):
        return {
            **receipt,
            "status": "disabled",
            "reason": "vl.enabled and vl.captcha_solve_enabled must both be on",
        }
    forbidden = skill_forbids_autosolve(agent)
    if forbidden is not None:
        return {**receipt, **forbidden}
    if _page_is_blacklisted(agent, page_id):
        return {
            **receipt,
            "status": "skipped",
            "reason": "a previous auto-solve on this page failed; the human path owns it",
        }
    used, limit = _episode_budget(agent)
    if used >= limit:
        return {
            **receipt,
            "status": "skipped",
            "reason": f"per-worker auto-solve budget exhausted ({used}/{limit})",
        }

    vl_config = _vl_config(agent)
    raw_attempts = optional_int(getattr(vl_config, "captcha_solve_max_retries", 3), 3)
    max_attempts = max(1, raw_attempts if raw_attempts is not None else 3)
    try:
        budget_seconds = float(getattr(vl_config, "captcha_solve_budget_seconds", 240.0) or 0.0)
    except (TypeError, ValueError):
        budget_seconds = 240.0
    min_confidence = _confidence_setting(vl_config, "captcha_solve_min_confidence", 0.8)
    min_confidence_ocr = _confidence_setting(
        vl_config, "captcha_solve_min_confidence_ocr", 0.9
    )

    # Ownership BEFORE any pixel is touched: with same-fleet multi-worker runs,
    # two workers must never drive the same challenge (or mutate the shared
    # cookie jar) concurrently. Losing the claim means another worker already
    # owns recovery — hand off untouched.
    claimed, barrier_receipt = await _claim_auth_barrier(agent, reason or trigger)
    receipt["fleetAuthBarrier"] = barrier_receipt
    if not claimed:
        return {
            **receipt,
            "status": "fleet_auth_gated",
            "reason": (
                "another worker owns authentication/challenge recovery for this"
                " fleet; no auto-solve was attempted"
            ),
        }

    bt = _bt()
    point_safety_evidence: list = []
    promoted_targets: Dict[Tuple[float, float], str] = {}
    started = time.monotonic()

    async def screenshot_fn() -> Optional[str]:
        return await _capture_screenshot(
            agent, page_id, step, "captcha autosolve: capture the challenge for VL"
        )

    async def captcha_solve_fn(config: Any, image_path: str) -> Dict[str, Any]:
        from harness.vl import visual_verify_image

        verdict = await visual_verify_image(
            config=config,
            image_path=image_path,
            expected={"pageId": page_id, "trigger": trigger},
            mode="captcha_solve",
            question=(
                "Classify this challenge and, only if it is a purely visual puzzle"
                " a program can finish on its own, give a solve plan."
            ),
        )
        agent.logger.write("vl.captcha_solve", {
            key: value for key, value in verdict.items()
            if key not in {"usage", "visible_evidence"}
        })
        return verdict

    async def metrics_fn() -> JsonDict:
        return await _viewport_metrics(agent, page_id, step)

    async def safety_fn(x: Any, y: Any) -> bool:
        return await _point_is_safe(
            agent,
            page_id,
            step,
            x,
            y,
            point_safety_evidence,
            promoted_targets,
        )

    async def exec_fn(method: str, params: JsonDict) -> None:
        promoted = _promote_input_params(method, params, promoted_targets)
        await _drive_input(
            agent, method, promoted, step, evidence=point_safety_evidence
        )

    async def verify_fn() -> Optional[bool]:
        cleared, evidence = await _clearance_check(
            agent,
            page_id,
            step,
            vl_recheck=vl_only_detection,
            min_confidence=min_confidence,
        )
        receipt["clearance"] = evidence
        return cleared

    agent.captcha_autosolve_episodes = used + 1
    previous_adjudicating = getattr(agent, "challenge_adjudicating", False)
    agent.challenge_adjudicating = True
    try:
        loop_receipt = await run_captcha_solve_loop(
            page_id=page_id,
            vl_config=vl_config,
            screenshot_fn=screenshot_fn,
            captcha_solve_fn=captcha_solve_fn,
            metrics_fn=metrics_fn,
            safety_fn=safety_fn,
            exec_fn=exec_fn,
            verify_fn=verify_fn,
            max_attempts=max_attempts,
            budget_seconds=budget_seconds,
            min_confidence=min_confidence,
            min_confidence_ocr=min_confidence_ocr,
            logger=getattr(agent, "logger", None),
            log_prefix="vl.captcha_autosolve",
        )
        if (
            str(loop_receipt.get("status") or "") == captcha.UNSAFE_ABORT
            and point_safety_evidence
            and point_safety_evidence[-1].get("classification")
            == "point_safety_capability_unavailable"
        ):
            loop_receipt = dict(loop_receipt)
            loop_receipt["status"] = captcha.VERIFICATION_UNAVAILABLE
            loop_receipt["reason"] = (
                "native point-safety capability was unavailable; the VL point"
                " was not clicked and was not classified as an unsafe target"
            )
        # Every clearance claim (including `not_a_challenge`) already went
        # through verify_fn inside the engine, so there is nothing to re-check
        # here — only the shared fleet gate is still ours to settle.
        if str(loop_receipt.get("status") or "") in CLEARED_STATUSES:
            opened = await bt._verify_and_open_fleet_auth_barrier(agent, page_id, step)
            loop_receipt = dict(loop_receipt)
            loop_receipt["fleetAuthBarrierOpened"] = opened
            if opened.get("enabled") and not opened.get("opened"):
                # We could not verify the shared session well enough to release
                # the fleet: do not report a clear the peers cannot trust.
                loop_receipt["status"] = captcha.VERIFICATION_UNAVAILABLE
                loop_receipt["reason"] = (
                    "the challenge looked cleared, but the fleet authentication"
                    f" barrier stayed shut ({opened.get('reason') or 'unknown reason'})"
                )
    except Exception as exc:  # the human fall-back must stay reachable
        loop_receipt = {
            "status": captcha.ERROR,
            "reason": str(exc)[:300],
            "errorType": type(exc).__name__,
        }
    finally:
        agent.challenge_adjudicating = previous_adjudicating

    merged = {
        **receipt,
        **loop_receipt,
        "attempted": True,
        "episode": used + 1,
        "episodeBudget": limit,
        "elapsedMs": int((time.monotonic() - started) * 1000),
        "detectionReason": reason[:200],
    }
    if point_safety_evidence:
        merged["pointSafetyEvidence"] = point_safety_evidence[-3:]
        unsafe_points = [
            item for item in point_safety_evidence
            if item.get("classification") == "unsafe_target"
        ]
        if unsafe_points:
            merged["unsafePoints"] = unsafe_points[-3:]
    if merged.get("status") not in CLEARED_STATUSES:
        _blacklist_page(agent, page_id)
    agent.logger.write("vl.captcha_autosolve.result", {
        key: value for key, value in merged.items() if key != "attempts"
    })
    receipts = getattr(agent, "captcha_autosolve_receipts", None)
    if not isinstance(receipts, list):
        receipts = []
        agent.captcha_autosolve_receipts = receipts
    receipts.append(dict(merged))
    return merged


def solve_summary(receipt: Any) -> str:
    """One-line, human-readable digest for a HITL reason or a model instruction."""
    if not isinstance(receipt, dict) or not receipt.get("attempted"):
        return ""
    status = str(receipt.get("status") or "")
    attempts = receipt.get("attempts")
    rounds = len(attempts) if isinstance(attempts, list) else 0
    category = str(receipt.get("challengeCategory") or "")
    detail = str(receipt.get("reason") or "")[:120]
    head = f"VL auto-solve: {status} after {rounds} attempt(s)"
    if category:
        head += f", category={category}"
    return f"{head}{f'; {detail}' if detail else ''}"
