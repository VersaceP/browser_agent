"""VL Role B: judge a skill's visual success checks without inventing geometry.

A workflow returning no error (and filling its variables) does NOT prove the task
succeeded — e.g. "submit" may have silently failed, a table may be empty, a
challenge may still be up. A declared ``success_contract.visual_checks`` lets VL
judge a screenshot of that visible end state.

VL remains L4: it is a weak visual corroborator. A definitive ``violated`` can
send the workflow to the slow path, but uncertainty and capture/VL failures never
sink an otherwise passed structural contract. An explicitly cropped element or
region is stricter: it may veto only when the *same screenshot receipt* binds
that crop to its declared geometry. This is receipt-bound evidence, not an
atomic screenshot/tree snapshot; page-side asynchronous changes remain possible.
"""
from __future__ import annotations

import inspect
import json
import re
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

from harness.vl.capture_geometry import (
    CAPTURE_PROVEN,
    region_in_capture,
)
from harness.vl.locate import capture_origin


_CANONICAL_ID_RE = re.compile(r"^\d+:\d+:\d+$")
_REGION_KEYS = ("x", "y", "width", "height")


def visual_checks_of(skill: Any) -> List[Dict[str, Any]]:
    contract = getattr(skill, "success_contract", {}) or {}
    checks = contract.get("visual_checks") if isinstance(contract, dict) else None
    return checks if isinstance(checks, list) and checks else []


def _capture_of(check: Any) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """Normalize one optional declarative screenshot scope.

    A static skill normally has no fresh canonical ID, so a selector-only crop
    is supported as useful visual context but intentionally cannot become a
    geometry-backed veto. Supplying a live ``id`` (and, ideally, its stable
    selector fallback) lets the Page.screenshot receipt tie the crop to the
    canonical node it actually captured.
    """
    if not isinstance(check, dict) or "capture" not in check:
        return {}, None  # ordinary viewport contract
    raw = check.get("capture")
    if not isinstance(raw, dict):
        return None, "capture_must_be_an_object"

    element_id = str(raw.get("id") or "").strip()
    selector = str(raw.get("selector") or "").strip()
    region_present = any(key in raw for key in _REGION_KEYS)
    if (element_id or selector) and region_present:
        return None, "capture_mixes_element_and_region"
    if element_id and not _CANONICAL_ID_RE.fullmatch(element_id):
        return None, "capture_id_is_not_canonical"
    if element_id or selector:
        out: Dict[str, Any] = {}
        if element_id:
            out["id"] = element_id
        if selector:
            out["selector"] = selector
        return out, None

    if region_present:
        if not all(key in raw for key in _REGION_KEYS):
            return None, "capture_region_is_incomplete"
        out = {}
        for key in _REGION_KEYS:
            value = raw.get(key)
            if isinstance(value, bool) or not isinstance(value, int):
                return None, "capture_region_must_use_integer_css_pixels"
            out[key] = value
        if out["width"] <= 0 or out["height"] <= 0:
            return None, "capture_region_must_be_nonempty"
        return out, None

    if "fullPage" in raw:
        if not isinstance(raw.get("fullPage"), bool):
            return None, "capture_fullPage_must_be_boolean"
        return ({"fullPage": True} if raw["fullPage"] else {}), None
    if raw:
        return None, "capture_has_no_supported_locator"
    return {}, None


def _scope_of(capture: Dict[str, Any]) -> str:
    if capture.get("id") or capture.get("selector"):
        return "element"
    if all(key in capture for key in _REGION_KEYS):
        return "region"
    return "fullpage" if capture.get("fullPage") else "viewport"


def _vl_check(check: Any) -> Any:
    """Keep harness-only capture locators out of the VL instruction."""
    if not isinstance(check, dict):
        return check
    return {key: value for key, value in check.items() if key != "capture"}


def _capture_key(capture: Dict[str, Any]) -> str:
    return json.dumps(capture, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _check_groups(checks: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Group checks that can be judged from one identically scoped screenshot."""
    groups: Dict[str, Dict[str, Any]] = {}
    for check in checks:
        capture, error = _capture_of(check)
        if error:
            key = f"invalid:{error}:{len(groups)}"
            groups[key] = {"capture": None, "captureError": error, "checks": [check]}
            continue
        assert capture is not None
        key = _capture_key(capture)
        group = groups.setdefault(key, {"capture": capture, "checks": []})
        group["checks"].append(check)
    return list(groups.values())


async def evaluate_visual_contract(
    browser: Any,
    skill: Any,
    page_id: str,
    *,
    vl_config: Any,
    screenshot_fn: Optional[Callable[..., Awaitable[Any]]] = None,
    contract_verify_fn: Optional[Callable[..., Awaitable[Dict[str, Any]]]] = None,
    logger: Any = None,
) -> Dict[str, Any]:
    """Judge declared visual checks and return a fail-open contract decision.

    ``ok`` is False only when VL says ``violated`` *and* a requested element or
    region crop has receipt-backed geometry. Page-wide checks retain their
    established behaviour: they make no claim about a named tree node, so their
    viewport/full-page image is itself their declared scope.
    """
    checks = visual_checks_of(skill)
    if not checks:
        return {"applicable": False, "ok": True}
    if (vl_config is None or not getattr(vl_config, "enabled", False)
            or not getattr(vl_config, "contract_verify_enabled", True)):
        return {"applicable": False, "ok": True, "reason": "disabled"}

    groups_out: List[Dict[str, Any]] = []
    allowed_failures: List[Any] = []
    saw_uncertain = False
    for group in _check_groups(checks):
        capture = group.get("capture")
        group_checks = group.get("checks") or []
        if capture is None:
            saw_uncertain = True
            groups_out.append({
                "scope": "invalid", "captureError": group.get("captureError"),
                "verdict": "uncertain", "geometry": {
                    "state": "unproven", "reason": "invalid_capture_declaration",
                    "sameMomentProven": False,
                },
            })
            continue

        scope = _scope_of(capture)
        raw_shot = await _take_screenshot(
            screenshot_fn or _default_screenshot, browser, page_id, capture,
        )
        shot = _normalize_screenshot_output(raw_shot)
        if shot is None:
            # A visual infrastructure error must never turn a passing workflow
            # into a false failure. Continue so another, independently scoped
            # contract check can still supply useful evidence.
            saw_uncertain = True
            groups_out.append({
                "scope": scope, "verdict": "uncertain", "skipped": "no_screenshot",
                "geometry": _no_receipt_geometry(scope),
            })
            continue

        geometry = _capture_geometry(scope, shot["receipt"])
        try:
            vl = await (contract_verify_fn or _default_contract_verify)(
                vl_config, shot["path"], [_vl_check(check) for check in group_checks],
            )
        except Exception as exc:  # VL is corroboration, never a new hard dependency.
            saw_uncertain = True
            groups_out.append({
                "scope": scope, "verdict": "uncertain", "skipped": "vl_error",
                "geometry": geometry, "error": str(exc)[:300],
            })
            continue

        raw_verdict = str(vl.get("verdict") or "uncertain")
        veto_allowed = scope not in {"element", "region"} or (
            geometry.get("state") == CAPTURE_PROVEN
        )
        effective_verdict = raw_verdict
        if raw_verdict == "violated" and not veto_allowed:
            # The model may have correctly read the pixels, but we cannot prove
            # which declared crop those pixels belong to. Do not turn that
            # ambiguity into a workflow failure.
            effective_verdict = "uncertain"
            saw_uncertain = True
        elif raw_verdict == "uncertain":
            saw_uncertain = True

        failed = vl.get("failed_checks") or []
        if effective_verdict == "violated":
            allowed_failures.extend(failed)
        groups_out.append({
            "scope": scope,
            "verdict": effective_verdict,
            "rawVerdict": raw_verdict,
            "failed_checks": failed,
            "evidence": vl.get("visible_evidence") or [],
            "geometry": geometry,
            "vetoAllowed": veto_allowed,
        })

    violated = any(row.get("verdict") == "violated" for row in groups_out)
    if violated:
        verdict = "violated"
    elif saw_uncertain or any(row.get("verdict") != "satisfied" for row in groups_out):
        verdict = "uncertain"
    else:
        verdict = "satisfied"
    out = {
        "applicable": True,
        "ok": not violated,
        "verdict": verdict,
        "failed_checks": allowed_failures,
        # Keep the pre-receipt public shape as a flattened convenience; callers
        # that need to know which crop supplied a fact use captureGroups.
        "evidence": [
            item
            for row in groups_out
            for item in (row.get("evidence") if isinstance(row.get("evidence"), list) else [])
        ],
        "captureGroups": groups_out,
    }
    if groups_out and all(row.get("skipped") == "no_screenshot" for row in groups_out):
        # Preserve the compact legacy outcome for callers that only need to
        # distinguish an unavailable visual substrate from a VL uncertainty.
        out["skipped"] = "no_screenshot"
    _log(logger, "skill.visual_contract.result", {
        "pageId": page_id, "verdict": verdict, "ok": out["ok"],
        "failed_checks": allowed_failures,
        "geometry": [row.get("geometry") for row in groups_out],
    })
    return out


async def _take_screenshot(
    screenshot_fn: Callable[..., Awaitable[Any]],
    browser: Any,
    page_id: str,
    capture: Dict[str, Any],
) -> Any:
    """Call the new capture-aware hook without breaking old two-arg test hooks."""
    try:
        inspect.signature(screenshot_fn).bind(browser, page_id, capture)
    except (TypeError, ValueError):
        return await screenshot_fn(browser, page_id)
    return await screenshot_fn(browser, page_id, capture)


def _normalize_screenshot_output(raw: Any) -> Optional[Dict[str, Any]]:
    """Accept legacy ``path`` hooks while retaining new receipt-bearing output."""
    if isinstance(raw, str) and raw:
        return {"path": raw, "receipt": {}}
    if not isinstance(raw, dict):
        return None
    path = raw.get("path")
    receipt = raw.get("receipt")
    if not isinstance(path, str) or not path:
        return None
    return {"path": path, "receipt": receipt if isinstance(receipt, dict) else {}}


def _no_receipt_geometry(scope: str) -> Dict[str, Any]:
    return {
        "state": "unproven" if scope in {"element", "region"} else "not_applicable",
        "reason": "screenshot_receipt_unavailable",
        "receiptBound": False,
        # This platform currently has no capture id / layout revision shared by
        # Page.screenshot and a separately read tree. Do not imply otherwise.
        "sameMomentProven": False,
    }


def _capture_geometry(scope: str, receipt: Dict[str, Any]) -> Dict[str, Any]:
    """Convert one Page.screenshot receipt into an auditable geometry verdict."""
    if scope not in {"element", "region"}:
        return {
            "state": "not_applicable",
            "reason": "page_scoped_visual_check",
            "receiptBound": bool(receipt),
            "sameMomentProven": False,
        }
    try:
        origin = capture_origin(scope=scope, shot_data=receipt)
    except Exception:  # a malformed receipt is evidence of nothing, not a crash
        origin = {"proven": False, "source": "receipt_parse_error"}
    bound = bool(origin.get("proven"))
    coverage = region_in_capture(
        region_declared=True,
        screenshot_scope=scope,
        coverage={},
        receipt_bound=bound,
    )
    return {
        "state": coverage.get("state"),
        "reason": coverage.get("reason"),
        "receiptBound": bound,
        "originSource": origin.get("source"),
        # A receipt binds image geometry to the Semantic Tree returned by the
        # same action, but ABCP does not expose one shared layout/tree revision
        # or capture timestamp. This remains deliberately false.
        "sameMomentProven": False,
    }


async def _default_screenshot(
    browser: Any, page_id: str, capture: Optional[Dict[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
    from harness.skill.control import _default_screenshot_with_receipt
    return await _default_screenshot_with_receipt(
        browser, page_id, capture=capture,
        purpose="capture the declared visual success condition for VL verification",
    )


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
