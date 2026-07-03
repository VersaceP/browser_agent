"""harness.skill.visual_contract — VL Role B: judge a skill's visual success checks.

A workflow returning no error (and filling its variables) does NOT prove the task
succeeded — e.g. "submit" may have silently failed, a table may be empty, a
challenge may still be up. When a skill's `fallback.yaml` declares
`success_contract.visual_checks`, this screenshots the page and asks VL
(`contract_verify` mode) to judge them.

Authority discipline (doc §7.1 ladder): VL is L4 (weak visual corroboration). It
NEVER overrides the already-passed variable contract on mere uncertainty — only a
definitive `violated` vetoes. Screenshot/VL infra failures also never veto (fail
open), so a flaky VL can't sink a structurally-successful run.
"""
from __future__ import annotations

from typing import Any, Awaitable, Callable, Dict, List, Optional


def visual_checks_of(skill: Any) -> List[Dict[str, Any]]:
    contract = getattr(skill, "success_contract", {}) or {}
    checks = contract.get("visual_checks") if isinstance(contract, dict) else None
    return checks if isinstance(checks, list) and checks else []


async def evaluate_visual_contract(
    browser: Any,
    skill: Any,
    page_id: str,
    *,
    vl_config: Any,
    screenshot_fn: Optional[Callable[..., Awaitable[Optional[str]]]] = None,
    contract_verify_fn: Optional[Callable[..., Awaitable[Dict[str, Any]]]] = None,
    logger: Any = None,
) -> Dict[str, Any]:
    """Judge the skill's declared visual_checks. Returns:
      {applicable:False, ok:True}                         — nothing to check / disabled
      {applicable:True, ok:True,  verdict:'satisfied'|'uncertain'|...}
      {applicable:True, ok:False, verdict:'violated', failed_checks:[...]}  — VETO
    `ok` is False ONLY on a definitive `violated`."""
    checks = visual_checks_of(skill)
    if not checks:
        return {"applicable": False, "ok": True}
    if (vl_config is None or not getattr(vl_config, "enabled", False)
            or not getattr(vl_config, "contract_verify_enabled", True)):
        return {"applicable": False, "ok": True, "reason": "disabled"}

    shot_fn = screenshot_fn or _default_screenshot
    shot = await shot_fn(browser, page_id)
    if not shot:
        # Fail open: an infra problem must not veto a structurally-successful run.
        _log(logger, "skill.visual_contract.no_screenshot", {"pageId": page_id})
        return {"applicable": True, "ok": True, "skipped": "no_screenshot"}

    vl = await (contract_verify_fn or _default_contract_verify)(vl_config, shot, checks)
    verdict = str(vl.get("verdict") or "uncertain")
    ok = verdict != "violated"   # only a definitive violation vetoes
    out = {"applicable": True, "ok": ok, "verdict": verdict,
           "failed_checks": vl.get("failed_checks") or [],
           "evidence": vl.get("visible_evidence") or []}
    _log(logger, "skill.visual_contract.result",
         {"pageId": page_id, "verdict": verdict, "ok": ok,
          "failed_checks": out["failed_checks"]})
    return out


async def _default_screenshot(browser: Any, page_id: str) -> Optional[str]:
    from harness.skill.control import _default_screenshot as shot
    return await shot(browser, page_id)


async def _default_contract_verify(vl_config: Any, image_path: str,
                                   checks: List[Dict[str, Any]]) -> Dict[str, Any]:
    from harness.vl import visual_verify_image
    return await visual_verify_image(
        config=vl_config, image_path=image_path,
        expected={"visual_checks": checks}, mode="contract_verify",
        question="Judge whether the visible end state satisfies the success checks.",
    )


def _log(logger: Any, event: str, payload: Dict[str, Any]) -> None:
    if logger is not None and hasattr(logger, "write"):
        try:
            logger.write(event, payload)
        except Exception:  # pragma: no cover
            pass
