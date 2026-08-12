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
    reading the exact target's live value through native batched
    DOM.getAttribute. On mismatch, clear harder and retry once. Never submits —
    that is a separate verified action."""
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
    # Both locators travel together when the caller has both: ABCP takes the id
    # as primary and the selector as its fallback, so a field whose id went
    # stale between the AXTree read and the type still gets filled instead of
    # costing a failed attempt.
    target: JsonDict = {}
    if target_id:
        target["id"] = target_id
    if selector:
        target["selector"] = selector

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

        verdict = await _read_field_value_native(
            agent, page_id, target, step, internal=mask,
        )
        last_verdict = verdict
        attempts.append({
            "attempt": attempt,
            "ok": verdict.get("status") == "done" and verdict.get("value") == text,
            "method": "DOM.getAttribute",
            "confidence": "high" if verdict.get("status") == "done" else "low",
        })

        if verdict.get("status") == "not_found":
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
        if verdict.get("status") != "done":
            return {
                "status": "typed_unverified",
                "target": target,
                "reason": str(verdict.get("error") or "native attribute read unavailable"),
                "next_instruction": (
                    "Input.type completed but native DOM.getAttribute could not"
                    " verify the exact target value. Refresh DOM.getAXTree and"
                    " retry with a canonical id."
                ),
                "attempts": attempts,
            }
        if verdict.get("value") == text:
            return {"status": "done", "target": target, "verified": True,
                    "confidence": "high", "keywords": keywords,
                    "attempts": attempts}
        # Plain single-field mismatch: loop clears harder and retries; if the
        # budget is exhausted, fall through to the mismatch yield below.

    actual = shown(str((last_verdict or {}).get("value") or ""))
    return {"status": "mismatch", "target": target, "keywords": keywords,
            "expected": shown(text), "actual": actual,
            "next_instruction": (
                "The field value did not match after a clear-and-retry. The"
                " element may be a custom/controlled widget; inspect it or use a"
                " different input strategy."
            ), "attempts": attempts}


def _attribute_value(info: Any) -> Optional[str]:
    if not isinstance(info, dict):
        return None
    if "value" in info and not isinstance(info.get("value"), (dict, list)):
        return str(info.get("value") or "")
    attributes = info.get("attributes")
    if isinstance(attributes, dict) and "value" in attributes:
        return str(attributes.get("value") or "")
    if isinstance(attributes, list):
        for entry in attributes:
            if isinstance(entry, dict) and str(entry.get("name") or "") == "value":
                return str(entry.get("value") or "")
    return None


async def _read_field_value_native(
    agent: Any,
    page_id: str,
    target: JsonDict,
    step: int,
    *,
    internal: bool,
) -> JsonDict:
    result = await _invoke_browser_method(
        agent,
        "DOM.getAttribute",
        {
            "pageId": page_id,
            "targets": [dict(target)],
            "attributes": ["value"],
            "purpose": "fill_field_verified: verify exact target value",
        },
        step,
        count_progress=False,
        internal=internal,
    )
    if _invoke_result_failed(result):
        return {"status": "not_found", "error": "DOM.getAttribute failed"}
    response = result.get("response") if isinstance(result, dict) else None
    data = response.get("data") if isinstance(response, dict) else None
    items = data.get("items") if isinstance(data, dict) else None
    if isinstance(items, list):
        if not items or not isinstance(items[0], dict) or items[0].get("error"):
            return {"status": "not_found"}
        value = _attribute_value(items[0].get("info"))
    else:
        value = _attribute_value(data)
    if value is None:
        return {"status": "unavailable", "error": "value attribute missing from response"}
    return {"status": "done", "value": value}
