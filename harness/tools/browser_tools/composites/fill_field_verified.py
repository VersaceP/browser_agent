"""fill_field_verified composite tool."""

import re
from typing import Any, List, Optional

from harness.utils import JsonDict, optional_int


def _bt() -> Any:
    import harness.tools.browser_tools as bt

    return bt


async def _invoke_browser_method(*args: Any, **kwargs: Any) -> JsonDict:
    return await _bt()._invoke_browser_method(*args, **kwargs)


def _loop_interrupt_from_result(result: Any) -> Optional[JsonDict]:
    return _bt()._loop_interrupt_from_result(result)


def _result_occlusion_blocked(result: Any) -> bool:
    return _bt()._result_occlusion_blocked(result)


def _collect_overlay_stop_reason(status: Any) -> Optional[str]:
    return _bt()._collect_overlay_stop_reason(status)


async def _collect_overlay_recovery(*args: Any, **kwargs: Any) -> JsonDict:
    return await _bt()._collect_overlay_recovery(*args, **kwargs)


def _invoke_result_failed(result: Any) -> bool:
    return _bt()._invoke_result_failed(result)


def build_read_only_oracle(agent: Any, page_id: str, step: int) -> Any:
    return _bt().build_read_only_oracle(agent, page_id, step)


async def verify_field_value(*args: Any, **kwargs: Any) -> Any:
    return await _bt().verify_field_value(*args, **kwargs)


FILL_FIELD_STOPWORDS = {
    "your", "the", "please", "enter", "type", "input", "field", "a", "an",
    "of", "to", "for", "address", "请", "输入", "填写",
}


def _fill_field_keywords(explicit: Any, node_name: str) -> List[str]:
    """Keywords for the verify locator. Prefer explicit verifyKeywords; else
    derive from the target's accessible name. Latin names split into word
    tokens (so "Email Address" -> ["email","address"] matches a label/aria/
    placeholder/name substring); CJK / token-less names fall back to the whole
    normalized name as one keyword."""
    if isinstance(explicit, list):
        kws = [str(k).strip().lower() for k in explicit if str(k).strip()]
        if kws:
            return kws
    name = " ".join(str(node_name or "").split()).lower()
    if not name:
        return []
    tokens = [t for t in re.findall(r"[a-z0-9]{2,}", name) if t not in FILL_FIELD_STOPWORDS]
    return tokens or [name]


def _axtree_node_name(agent: Any, target_id: str) -> str:
    for node in getattr(agent, "axtree_nodes", []) or []:
        if str(node.get("id") or "") == target_id:
            return str(node.get("name") or "")
    return ""


async def _fill_field_action(
    agent: Any, page_id: str, target: JsonDict, step: int, *, clear: bool, text: str,
    redact: bool = False,
) -> JsonDict:
    """Type into the field, recovering once from an occluding overlay. Returns
    {ok, overlay?}. The real text always goes to the browser; when redact is set
    it is masked in result/log/trace via _invoke_browser_method(redact_params)."""
    params = {"pageId": page_id, "text": text, "clear": clear,
              "purpose": "fill_field_verified: type value"}
    params.update(target)
    redact_params = {"text"} if redact else None
    result = await _invoke_browser_method(
        agent, "Input.type", params, step, count_progress=False,
        allow_rematch=bool(target.get("id")), redact_params=redact_params,
    )
    interrupt = _loop_interrupt_from_result(result)
    if interrupt:
        return {"ok": False, "interrupt": interrupt}
    if _result_occlusion_blocked(result):
        recovery = await _collect_overlay_recovery(agent, page_id, None, step, force=True)
        if recovery.get("interrupt"):
            return {"ok": False, "interrupt": recovery["interrupt"]}
        if _collect_overlay_stop_reason(recovery.get("status")):
            return {"ok": False, "overlay": recovery}
        # overlay cleared -> retry the type once
        result = await _invoke_browser_method(
            agent, "Input.type", params, step, count_progress=False,
            allow_rematch=bool(target.get("id")), redact_params=redact_params,
        )
        interrupt = _loop_interrupt_from_result(result)
        if interrupt:
            return {"ok": False, "interrupt": interrupt}
    return {"ok": not _invoke_result_failed(result)}


async def _fill_field_verified(agent: Any, tool_input: JsonDict, step: int) -> JsonDict:
    """Composite tool: type a value and verify it was actually accepted by
    reading the field's live .value (via the read-only title side-channel,
    since returnByValue is dead on the panel). On mismatch, clear harder and
    retry once; on ambiguity or not-found, yield instead of claiming success.
    Never submits — that is a separate verified action."""
    page_id = str(tool_input.get("pageId") or "").strip()
    text = tool_input.get("text")
    target_id = str(tool_input.get("id") or "").strip()
    selector = str(tool_input.get("selector") or "").strip()
    if not page_id:
        return {"status": "failed", "error": "pageId is required"}
    if text is None:
        return {"status": "failed", "error": "text is required"}
    text = str(text)
    if not target_id and not selector:
        return {"status": "failed", "error": "id or selector is required"}
    mask = bool(tool_input.get("mask", False))
    max_retries = max(0, min(optional_int(tool_input.get("maxRetries"), 1) or 1, 3))
    target: JsonDict = {"id": target_id} if target_id else {"selector": selector}

    oracle = build_read_only_oracle(agent, page_id, step)

    # Force a layout viewport (fresh tab) and refresh nodes so we can read the
    # target's accessible name for the verify keywords.
    inspect = await _invoke_browser_method(
        agent, "DOM.getAXTree",
        {"pageId": page_id, "purpose": "fill_field_verified: force layout + node name"},
        step, count_progress=False,
    )
    interrupt = _loop_interrupt_from_result(inspect)
    if interrupt:
        return {**interrupt, "target": target, "attempts": []}
    keywords = _fill_field_keywords(tool_input.get("verifyKeywords"), _axtree_node_name(agent, target_id))

    def shown(value: str) -> str:
        return f"<masked len={len(value)}>" if mask else value

    attempts: List[JsonDict] = []
    last_verdict = None
    for attempt in range(1, max_retries + 2):
        # Focus, then clear+type. The second attempt clears harder first.
        if target_id:
            click = await _invoke_browser_method(
                agent, "Input.click", {**target, "pageId": page_id, "purpose": "fill_field_verified: focus"},
                step, count_progress=False, allow_rematch=True,
            )
            interrupt = _loop_interrupt_from_result(click)
            if interrupt:
                return {**interrupt, "target": target, "attempts": attempts}
            if _result_occlusion_blocked(click):
                recovery = await _collect_overlay_recovery(agent, page_id, None, step, force=True)
                if recovery.get("interrupt"):
                    return {**recovery["interrupt"], "target": target, "attempts": attempts}
                if _collect_overlay_stop_reason(recovery.get("status")):
                    return {"status": "blocked", "reason": "overlay_unresolved",
                            "overlay": recovery, "attempts": attempts}
        if attempt > 1:
            # Stronger clear before the retry type.
            sel_all = await _invoke_browser_method(
                agent, "Input.press", {"pageId": page_id, "key": "Control+a", "purpose": "fill_field_verified: select all"},
                step, count_progress=False,
            )
            interrupt = _loop_interrupt_from_result(sel_all)
            if interrupt:
                return {**interrupt, "target": target, "attempts": attempts}
            delete_res = await _invoke_browser_method(
                agent, "Input.press", {"pageId": page_id, "key": "Delete", "purpose": "fill_field_verified: delete"},
                step, count_progress=False,
            )
            interrupt = _loop_interrupt_from_result(delete_res)
            if interrupt:
                return {**interrupt, "target": target, "attempts": attempts}
        typed = await _bt()._fill_field_action(agent, page_id, target, step, clear=True, text=text, redact=mask)
        if typed.get("interrupt") is not None:
            return {**typed["interrupt"], "target": target, "attempts": attempts}
        if typed.get("overlay") is not None:
            return {"status": "blocked", "reason": "overlay_unresolved",
                    "overlay": typed["overlay"], "attempts": attempts}
        if not typed.get("ok"):
            # The type action itself failed (stale id / bad selector / protocol),
            # not an overlay. Do NOT fall through to verify — a coincidental
            # pre-existing field value would otherwise be misread as success.
            return {"status": "type_failed", "target": target, "attempts": attempts,
                    "next_instruction": (
                        "Input.type failed (target may be stale/invalid). Refresh"
                        " DOM.getAXTree and retry with a fresh id or a concrete"
                        " selector."
                    )}

        if not keywords:
            # No way to locate the field for verification (no name, no explicit
            # keywords). Report honestly rather than claim verified.
            return {
                "status": "typed_unverified", "target": target,
                "next_instruction": (
                    "Typed the value but could not verify it: the target has no"
                    " accessible name to derive a locator. Re-call with"
                    " verifyKeywords, or confirm via DOM.getAttribute(value)."
                ),
            }

        verdict = await verify_field_value(
            oracle=oracle, keywords=keywords, expected_value=text, mask_value=mask,
        )
        last_verdict = verdict
        attempts.append({"attempt": attempt, "ok": verdict.ok,
                         "method": verdict.method, "confidence": verdict.confidence})

        if verdict.method == "oracle_no_match":
            # Field not located by keywords; refresh AXTree once and, on the
            # last attempt, surface it as a locate failure.
            refresh = await _invoke_browser_method(
                agent, "DOM.getAXTree",
                {"pageId": page_id, "purpose": "fill_field_verified: refresh after no-match"},
                step, count_progress=False,
            )
            interrupt = _loop_interrupt_from_result(refresh)
            if interrupt:
                return {**interrupt, "target": target, "attempts": attempts}
            if attempt >= max_retries + 1:
                return {"status": "field_not_found", "keywords": keywords, "target": target,
                        "next_instruction": (
                            "Could not locate the field to verify by keywords"
                            f" {keywords}. Refresh DOM.getAXTree and retry with a"
                            " concrete selector or corrected verifyKeywords."
                        ), "attempts": attempts}
            continue
        if verdict.evidence.get("matchCount", 0) > 1:
            # Ambiguous locator: do not claim success even if some match held
            # the value. Yield for the model to disambiguate.
            return {"status": "ambiguous", "keywords": keywords, "target": target,
                    "evidence": verdict.evidence, "reason": verdict.reason,
                    "next_instruction": (
                        "Multiple fields matched the verify keywords; cannot"
                        " confirm the right one. Provide a more specific selector"
                        " or verifyKeywords."
                    ), "attempts": attempts}
        if verdict.ok:
            return {"status": "done", "target": target, "verified": True,
                    "confidence": verdict.confidence, "keywords": keywords,
                    "attempts": attempts}
        # Plain single-field mismatch: loop clears harder and retries; if the
        # budget is exhausted, fall through to the mismatch yield below.

    actual = ""
    if last_verdict is not None:
        matches = last_verdict.evidence.get("matches") or []
        if matches and isinstance(matches[0], dict):
            actual = str(matches[0].get("value") or "")
    return {"status": "mismatch", "target": target, "keywords": keywords,
            "expected": shown(text), "actual": actual,
            "next_instruction": (
                "The field value did not match after a clear-and-retry. The"
                " element may be a custom/controlled widget; inspect it or use a"
                " different input strategy."
            ), "attempts": attempts}
