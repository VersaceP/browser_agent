"""collect_items composite tool."""

import asyncio
import json
from typing import Any, Dict, List, Optional

from harness.utils import JsonDict, optional_int


def _bt() -> Any:
    import harness.tools.browser_tools as bt

    return bt


async def _invoke_browser_method(*args: Any, **kwargs: Any) -> JsonDict:
    return await _bt()._invoke_browser_method(*args, **kwargs)


def _loop_interrupt_from_result(result: Any) -> Optional[JsonDict]:
    return _bt()._loop_interrupt_from_result(result)


def _invoke_result_failed(result: Any) -> bool:
    return _bt()._invoke_result_failed(result)


def _result_occlusion_blocked(result: Any) -> bool:
    return _bt()._result_occlusion_blocked(result)


def build_read_only_oracle(agent: Any, page_id: str, step: int) -> Any:
    return _bt().build_read_only_oracle(agent, page_id, step)


async def collect_rows(*args: Any, **kwargs: Any) -> JsonDict:
    return await _bt().collect_rows(*args, **kwargs)


async def verify_overlay_gone(*args: Any, **kwargs: Any) -> Any:
    return await _bt().verify_overlay_gone(*args, **kwargs)


async def _dismiss_overlay(*args: Any, **kwargs: Any) -> JsonDict:
    return await _bt()._dismiss_overlay(*args, **kwargs)


def _record_extraction(*args: Any, **kwargs: Any) -> JsonDict:
    return _bt()._record_extraction(*args, **kwargs)


def _record_extraction_persisted(result: JsonDict) -> bool:
    return _bt()._record_extraction_persisted(result)


async def discover_selector_candidates(*args: Any, **kwargs: Any) -> List[JsonDict]:
    return await _bt().discover_selector_candidates(*args, **kwargs)


COLLECT_ITEMS_MAX_ROUNDS = 12
COLLECT_ITEMS_STABILITY_THRESHOLD = 3
COLLECT_ITEMS_MAX_DURATION_MS = 60000
COLLECT_ITEMS_SETTLE_MS = 600
COLLECT_ITEMS_HARVEST_LIMIT = 200
COLLECT_ITEMS_MAX_WINDOWS = 10  # per-round harvest windows (10 * 200 = 2000 rows/round)
COLLECT_ITEMS_DEFAULT_FIELDS = {"title": "text", "href": "href"}


def _collect_dedup_key(row: JsonDict, key_field: str) -> str:
    """Stable per-row identity for cross-round dedup. Prefer the JS-derived
    __key; fall back to the named key field; finally the whole row so distinct
    keyless rows are not collapsed together."""
    for candidate in (row.get("__key"), row.get(key_field) if key_field else None):
        text = str(candidate or "").strip()
        if text:
            return text
    return json.dumps({k: v for k, v in row.items() if k != "__key"}, sort_keys=True, ensure_ascii=False)


def _collect_expected_exact_rows(agent: Any) -> Optional[int]:
    contract = getattr(agent, "worker_contract", None)
    if not isinstance(contract, dict):
        return None
    expected = (
        contract.get("expected_artifact")
        if isinstance(contract.get("expected_artifact"), dict)
        else {}
    )
    value = optional_int(expected.get("exact_rows"))
    if value is not None and value > 0:
        return value
    validators = contract.get("validators")
    if not isinstance(validators, list):
        return None
    for validator in validators:
        if not isinstance(validator, dict):
            continue
        if str(validator.get("type") or "") != "exact_rows":
            continue
        value = optional_int(validator.get("value"))
        if value is not None and value > 0:
            return value
    return None


def _collect_expected_fields(agent: Any) -> List[str]:
    contract = getattr(agent, "worker_contract", None)
    if not isinstance(contract, dict):
        return []
    expected = (
        contract.get("expected_artifact")
        if isinstance(contract.get("expected_artifact"), dict)
        else {}
    )
    fields = expected.get("required_fields")
    if not isinstance(fields, list) or not fields:
        fields = expected.get("fields")
    if not isinstance(fields, list):
        return []
    out: List[str] = []
    seen = set()
    for item in fields:
        if isinstance(item, dict):
            raw = item.get("name") or item.get("field") or item.get("key")
        else:
            raw = item
        text = str(raw or "").strip()
        if text and text not in seen:
            out.append(text)
            seen.add(text)
    return out


def _collect_contract_warning(
    agent: Any,
    *,
    result: JsonDict,
    record_result: JsonDict,
) -> Optional[JsonDict]:
    exact_rows = _collect_expected_exact_rows(agent)
    expected_fields = _collect_expected_fields(agent)
    validation = (
        record_result.get("artifactValidation")
        if isinstance(record_result.get("artifactValidation"), dict)
        else {}
    )
    failures = [
        failure for failure in (validation.get("failures") or [])
        if isinstance(failure, dict)
    ]
    schema_warnings = [
        warning for warning in (record_result.get("schemaWarnings") or [])
        if isinstance(warning, dict)
    ]
    reasons: List[str] = []
    actual = optional_int(record_result.get("rowCount"), result.get("rowCount"))
    if exact_rows is not None and actual is not None and actual > exact_rows:
        reasons.append("row_count_exceeds_exact_rows")
    failure_types = {
        str(failure.get("type") or "")
        for failure in failures
        if str(failure.get("type") or "")
    }
    if "artifact_required" in failure_types:
        reasons.append("artifact_name_mismatch")
    if "required_fields" in failure_types or "field_nonempty" in failure_types:
        reasons.append("required_fields_missing")
    if schema_warnings:
        reasons.append("schema_warnings")
    if record_result.get("status") == "needs_fix" and not reasons:
        reasons.append("record_extraction_needs_fix")
    if not reasons:
        return None
    warning: JsonDict = {
        "type": "collect_items_contract_mismatch",
        "reasons": sorted(set(reasons)),
        "expectedRows": exact_rows,
        "actualRows": actual,
        "expectedFields": expected_fields,
        "recordStatus": record_result.get("status"),
        "next_instruction": (
            "collect_items gathered rows, but the saved artifact does not satisfy"
            " the worker_contract yet. Do not final_answer with this artifact as"
            " complete target data. If actualRows is greater than expectedRows,"
            " treat the selector as too broad; narrow to the repeated card/row"
            " container or transform/slice trusted DOM-order rows into the exact"
            " expected fields before saving again."
        ),
    }
    if failure_types:
        warning["validationFailureTypes"] = sorted(failure_types)
    return warning


async def _collect_items_materialize(
    agent: Any,
    *,
    mode: str,
    page_id: str,
    step: int,
    direction: str,
    amount: float,
    container_id: str,
    container_selector: str,
    load_more_id: str,
    load_more_selector: str,
) -> JsonDict:
    """Perform one expansion action. Returns {ok, exhausted, detail}."""
    if mode == "click_load_more":
        if not load_more_id and not load_more_selector:
            return {"ok": False, "exhausted": True, "detail": "no load-more target"}
        # Try the id first; if it is stale/gone OR the click fails, fall back to
        # the selector before declaring the button exhausted. An occlusion_blocked
        # failure is NOT exhaustion — the button is just covered by an overlay,
        # so signal occlusion and let the caller dismiss + retry.
        if load_more_id:
            result = await _invoke_browser_method(
                agent, "Input.click",
                {"pageId": page_id, "id": load_more_id, "purpose": "collect_items: load more"},
                step, count_progress=False, allow_rematch=True,
            )
            interrupt = _loop_interrupt_from_result(result)
            if interrupt:
                return {"ok": False, "exhausted": False, "interrupt": interrupt}
            if not _invoke_result_failed(result):
                return {"ok": True, "exhausted": False, "detail": "clicked load-more (id)"}
            if _result_occlusion_blocked(result):
                return {"ok": False, "exhausted": False, "occlusion": True, "detail": "load-more occluded (id)"}
            if not load_more_selector:
                return {"ok": False, "exhausted": True, "detail": "load_more id gone/failed"}
            # id failed but a selector fallback exists -> try it below.
        result = await _invoke_browser_method(
            agent, "Input.click",
            {"pageId": page_id, "selector": load_more_selector, "purpose": "collect_items: load more"},
            step, count_progress=False,
        )
        interrupt = _loop_interrupt_from_result(result)
        if interrupt:
            return {"ok": False, "exhausted": False, "interrupt": interrupt}
        if _result_occlusion_blocked(result):
            return {"ok": False, "exhausted": False, "occlusion": True, "detail": "load-more occluded (selector)"}
        if _invoke_result_failed(result):
            return {"ok": False, "exhausted": True, "detail": "load-more click failed"}
        return {"ok": True, "exhausted": False, "detail": "clicked load-more (selector)"}

    # default: scroll. A stale container id must still go through the seen-id
    # rematch guard (Phase 2), not bypass it -> allow_rematch=True.
    params = {"pageId": page_id, "direction": direction or "down",
              "amount": amount, "purpose": "collect_items: scroll"}
    if container_id:
        params["id"] = container_id
    elif container_selector:
        params["selector"] = container_selector
    result = await _invoke_browser_method(
        agent, "Input.scroll", params, step, count_progress=False,
        allow_rematch=bool(container_id),
    )
    interrupt = _loop_interrupt_from_result(result)
    if interrupt:
        return {"ok": False, "exhausted": False, "interrupt": interrupt}
    return {"ok": not _invoke_result_failed(result), "exhausted": False, "detail": "scrolled"}


async def _collect_overlay_recovery(
    agent: Any, page_id: str, oracle: Any, step: int, *, force: bool,
) -> JsonDict:
    """Shared overlay handling for collect_items. `force=True` (used when a
    click reported occlusion_blocked) dismisses directly; force=False (used on a
    plain stall, which may just be end-of-list) first probes verify_overlay_gone
    so we don't run the ladder when there is no overlay. Returns
    {handled, status?, subtype?}."""
    if not force:
        verdict = await verify_overlay_gone(oracle=oracle)
        if not (verdict.method == "semantic_oracle" and not verdict.ok):
            return {"handled": False}
    dismissed = await _dismiss_overlay(
        agent, {"pageId": page_id, "targetId": "", "targetMethod": "",
                "maxAttempts": 0, "maxDurationMs": 0}, step,
    )
    recovery = {"handled": True, "status": dismissed.get("status"),
                "subtype": (dismissed.get("overlay") or {}).get("subtype")}
    if dismissed.get("loopInterrupted"):
        # The dismiss ladder itself hit a HITL/challenge interrupt; surface it so
        # the collect loop stops and reports human-needed, not a generic reason.
        recovery["interrupt"] = dismissed
    return recovery


def _collect_overlay_stop_reason(status: Any) -> Optional[str]:
    """Map a dismiss_overlay status to a terminal collect stopReason, or None to
    keep going. Both an auth/paywall refusal (blocked) and a dismiss that ran
    the ladder but could not clear the overlay (failed) must STOP the loop with
    an explicit reason — otherwise an un-cleared overlay keeps getting clicked
    through until max_rounds/stagnant, hiding the real blocker."""
    if status == "blocked":
        return "overlay_blocked"
    if status == "failed":
        return "overlay_unresolved"
    return None


def _collect_interrupt_result(
    agent: Any,
    interrupt: JsonDict,
    *,
    accumulator: Dict[str, JsonDict],
    mode: str,
    selector: str,
    rounds_log: List[JsonDict],
) -> JsonDict:
    """collect_items' return when a HITL/challenge interrupt aborts the loop.
    Carries the human-needed summary + rows collected so far; deliberately does
    NOT resync the AXTree (the page is paused) or auto-record the partial rows."""
    collected = list(accumulator.values())
    result: JsonDict = {
        **interrupt,
        "stopReason": interrupt.get("status"),
        "mode": mode,
        "selector": selector,
        "rowCount": len(collected),
        "partial": True,
        "roundsUsed": len(rounds_log),
        "rounds": rounds_log[-COLLECT_ITEMS_MAX_ROUNDS:],
        "sample": collected[:3],
    }
    agent.logger.write(
        "collect_items.result", {k: v for k, v in result.items() if k not in {"sample"}}
    )
    return result


async def _collect_items(agent: Any, tool_input: JsonDict, step: int) -> JsonDict:
    """Composite tool: materialize -> harvest -> dedup loop, internally, costing
    no model step. Each round harvests rows through the read-only oracle and
    dedups by stable key; an overlay is probed when a round stalls (the cheap
    signal that expansion is blocked) or handled directly when a load-more click
    reports occlusion_blocked."""
    page_id = str(tool_input.get("pageId") or "").strip()
    selector = str(tool_input.get("selector") or "").strip()
    if not page_id:
        return {"status": "failed", "error": "pageId is required"}
    if not selector:
        return {"status": "failed", "error": "selector is required"}

    mode = str(tool_input.get("mode") or "scroll").strip() or "scroll"
    if mode not in {"scroll", "click_load_more"}:
        return {
            "status": "failed",
            "error": f"unsupported mode {mode!r}; v1 supports scroll | click_load_more",
        }
    fields = tool_input.get("fields")
    if not isinstance(fields, dict) or not fields:
        fields = dict(COLLECT_ITEMS_DEFAULT_FIELDS)
    key_field = str(tool_input.get("keyField") or "href").strip()
    direction = str(tool_input.get("direction") or "down").strip() or "down"
    amount = float(tool_input.get("amount") or 0) or 800.0
    container_id = str(tool_input.get("containerId") or "").strip()
    container_selector = str(tool_input.get("containerSelector") or "").strip()
    load_more_id = str(tool_input.get("loadMoreId") or "").strip()
    load_more_selector = str(tool_input.get("loadMoreSelector") or "").strip()
    target_count = optional_int(tool_input.get("targetCount"), 0) or 0
    max_rounds = optional_int(tool_input.get("maxRounds"), 0) or 0
    if max_rounds <= 0:
        max_rounds = COLLECT_ITEMS_MAX_ROUNDS
    max_rounds = max(1, min(max_rounds, 50))
    stability_threshold = optional_int(tool_input.get("stabilityThreshold"), 0) or 0
    if stability_threshold <= 0:
        stability_threshold = COLLECT_ITEMS_STABILITY_THRESHOLD
    settle_ms = optional_int(tool_input.get("settleMs"), COLLECT_ITEMS_SETTLE_MS) or COLLECT_ITEMS_SETTLE_MS
    harvest_limit = optional_int(tool_input.get("harvestLimit"), COLLECT_ITEMS_HARVEST_LIMIT) or COLLECT_ITEMS_HARVEST_LIMIT
    harvest_max_windows = max(1, min(optional_int(tool_input.get("harvestMaxWindows"), COLLECT_ITEMS_MAX_WINDOWS) or COLLECT_ITEMS_MAX_WINDOWS, 50))
    record_name = str(tool_input.get("recordName") or "").strip()
    deadline = asyncio.get_running_loop().time() + COLLECT_ITEMS_MAX_DURATION_MS / 1000.0

    oracle = build_read_only_oracle(agent, page_id, step)

    accumulator: Dict[str, JsonDict] = {}
    rounds_log: List[JsonDict] = []
    overlay_encountered: Optional[JsonDict] = None
    truncated = False

    # Force a layout viewport (fresh tabs have none until DOM.getAXTree) so the
    # scroll has a viewport and rect-dependent overlay checks are valid.
    layout = await _invoke_browser_method(
        agent, "DOM.getAXTree",
        {"pageId": page_id, "purpose": "collect_items: force layout + baseline"},
        step, count_progress=False,
    )
    interrupt = _loop_interrupt_from_result(layout)
    if interrupt:
        # Page is already challenge-paused before collection began.
        return _collect_interrupt_result(
            agent, interrupt, accumulator=accumulator,
            mode=mode, selector=selector, rounds_log=rounds_log,
        )

    async def harvest() -> JsonDict:
        # Window through the full matched count: a plain (non-virtualized)
        # append list can hold more than harvest_limit nodes at once, and a
        # single slice(0, limit) would re-read the same head every round while
        # the tail never enters the accumulator. Page by offset until we have
        # covered `count` (bounded by harvest_max_windows so a pathological
        # page can't loop unboundedly — then report truncated).
        nonlocal truncated
        all_rows: List[JsonDict] = []
        offset = 0
        count = 0
        last: JsonDict = {}
        for _ in range(harvest_max_windows):
            last = await collect_rows(
                oracle=oracle, selector=selector, fields=fields,
                key_field=key_field, offset=offset, limit=harvest_limit,
            )
            if last.get("status") != "done":
                return last if not all_rows else {
                    "status": "done", "count": count, "rows": all_rows,
                    "partialWindowError": last.get("reason"),
                }
            count = int(last.get("count") or 0)
            window = last.get("rows") or []
            all_rows.extend(window)
            offset += len(window)
            if len(window) < harvest_limit or offset >= count:
                break
        else:
            # Loop exhausted windows without covering count.
            if offset < count:
                truncated = True
        return {"status": "done", "count": count, "rows": all_rows}

    def absorb(rows: List[JsonDict]) -> int:
        added = 0
        for row in rows:
            key = _collect_dedup_key(row, key_field)
            if key not in accumulator:
                accumulator[key] = {k: v for k, v in row.items() if k != "__key"}
                added += 1
        return added

    baseline = await harvest()
    if baseline.get("status") != "done":
        return {
            "status": "failed",
            "error": f"baseline harvest failed: {baseline.get('reason') or baseline.get('status')}",
            "selector": selector,
        }
    absorb(baseline.get("rows") or [])

    stop_reason = "max_rounds"
    stagnant = 0
    for round_index in range(1, max_rounds + 1):
        if asyncio.get_running_loop().time() >= deadline:
            stop_reason = "time_budget"
            break
        if target_count and len(accumulator) >= target_count:
            stop_reason = "target_met"
            break

        action = await _bt()._collect_items_materialize(
            agent, mode=mode, page_id=page_id, step=step, direction=direction,
            amount=amount, container_id=container_id, container_selector=container_selector,
            load_more_id=load_more_id, load_more_selector=load_more_selector,
        )
        if action.get("interrupt"):
            # HITL/challenge fired mid-scroll: stop now with a human-needed
            # summary instead of continuing to scroll a paused page.
            return _collect_interrupt_result(
                agent, action["interrupt"], accumulator=accumulator,
                mode=mode, selector=selector, rounds_log=rounds_log,
            )
        if action.get("exhausted"):
            stop_reason = "load_more_exhausted"
            rounds_log.append({"round": round_index, "action": action.get("detail"), "exhausted": True})
            break
        if action.get("occlusion"):
            # The expansion control is covered by an overlay — recover rather
            # than mistaking it for exhaustion, then retry on the next round.
            recovery = await _bt()._collect_overlay_recovery(agent, page_id, oracle, step, force=True)
            if recovery.get("interrupt"):
                return _collect_interrupt_result(
                    agent, recovery["interrupt"], accumulator=accumulator,
                    mode=mode, selector=selector, rounds_log=rounds_log,
                )
            overlay_encountered = {"round": round_index, "status": recovery.get("status"),
                                   "subtype": recovery.get("subtype")}
            rounds_log.append({"round": round_index, "action": action.get("detail"),
                               "occlusion": True, "overlay": overlay_encountered})
            terminal = _collect_overlay_stop_reason(recovery.get("status"))
            if terminal:
                stop_reason = terminal
                break
            continue
        await asyncio.sleep(max(0.0, settle_ms / 1000.0))

        snapshot = await harvest()
        if snapshot.get("status") != "done":
            rounds_log.append({"round": round_index, "harvest": snapshot.get("status")})
            stagnant += 1
            if stagnant >= stability_threshold:
                stop_reason = "harvest_unavailable"
                break
            continue
        added = absorb(snapshot.get("rows") or [])
        rounds_log.append({
            "round": round_index, "added": added,
            "domCount": snapshot.get("count"), "total": len(accumulator),
        })

        if added > 0:
            stagnant = 0
            continue

        # Stall: before counting it, check whether an overlay is blocking
        # expansion. This ties the (cheap) overlay probe to the stall signal.
        recovery = await _bt()._collect_overlay_recovery(agent, page_id, oracle, step, force=False)
        if recovery.get("interrupt"):
            return _collect_interrupt_result(
                agent, recovery["interrupt"], accumulator=accumulator,
                mode=mode, selector=selector, rounds_log=rounds_log,
            )
        if recovery.get("handled"):
            overlay_encountered = {"round": round_index, "status": recovery.get("status"),
                                   "subtype": recovery.get("subtype")}
            rounds_log[-1]["overlay"] = overlay_encountered
            terminal = _collect_overlay_stop_reason(recovery.get("status"))
            if terminal:
                stop_reason = terminal
                break
            # Overlay cleared; don't count this round as stagnation.
            continue

        stagnant += 1
        if stagnant >= stability_threshold:
            stop_reason = "stagnant"
            break

    # Resync the AXTree id cache that scroll/click invalidated.
    resync = await _invoke_browser_method(
        agent, "DOM.getAXTree",
        {"pageId": page_id, "purpose": "collect_items: resync after collection"},
        step, count_progress=False,
    )
    interrupt = _loop_interrupt_from_result(resync)
    if interrupt:
        # A challenge surfaced as collection finished. Keep the rows gathered so
        # far but report human-needed (and do not auto-record partial data).
        return _collect_interrupt_result(
            agent, interrupt, accumulator=accumulator,
            mode=mode, selector=selector, rounds_log=rounds_log,
        )

    collected = list(accumulator.values())
    if truncated and stop_reason in {"stagnant", "max_rounds"}:
        # The DOM held more matched items than the per-round window budget could
        # read, so the tail was never harvested. Make this loud rather than
        # passing an under-collection off as a clean stop.
        stop_reason = "harvest_limit_reached"
    result: JsonDict = {
        "status": "blocked" if stop_reason in {"overlay_blocked", "overlay_unresolved"} else "done",
        "stopReason": stop_reason,
        "mode": mode,
        "selector": selector,
        "rowCount": len(collected),
        "truncated": truncated,
        "roundsUsed": len(rounds_log),
        "rounds": rounds_log[-COLLECT_ITEMS_MAX_ROUNDS:],
        "sample": collected[:3],
        "overlayEncountered": overlay_encountered,
    }
    if truncated:
        result["next_step"] = (
            "More matched items exist than were harvested (raise harvestLimit or"
            " harvestMaxWindows, or narrow the selector); collection is incomplete."
        )
    # Offer repeated-row selector candidates ONLY on a genuine selector-no-match
    # terminal: 0 rows, a clean "done" exhaustion (not blocked/interrupted), no
    # truncation, and no overlay blocker — otherwise the real blocker (overlay,
    # time budget, harvest limit) must stand, not be masked by "try another
    # selector". Gated internal getSemanticTree, one-shot+cached, digest only.
    selector_no_match = (
        not collected
        and result["status"] == "done"
        and stop_reason in {"stagnant", "max_rounds", "load_more_exhausted"}
        and not truncated
        and overlay_encountered is None
    )
    if selector_no_match:
        candidates = await discover_selector_candidates(agent, page_id)
        if candidates:
            result["selectorCandidates"] = candidates
            # Only recommend USABLE (non-truncated) selectors for a retry; a
            # truncated one is incomplete CSS.
            usable = [c for c in candidates if not c.get("truncated")][:3]
            if usable:
                top = ", ".join(f"{c['cssSelector']} (x{c['count']})" for c in usable)
                result["next_step"] = (
                    f"Selector {selector!r} matched 0 rows. Repeated page structures"
                    f" suggest these row selectors: {top}. Re-call collect_items with"
                    " one of them (verify it carries the fields you need)."
                )
            else:
                result["next_step"] = (
                    f"Selector {selector!r} matched 0 rows. Repeated structures exist"
                    " but their class selectors are abnormally long/unstable; inspect"
                    " the page or use a structural (nth-child/tag) selector."
                )

    if record_name and collected:
        record_result = _record_extraction(
            agent,
            {
                "name": record_name,
                "rows": collected,
                "schema": {"source": "collect_items", "selector": selector,
                           "mode": mode, "fields": fields},
                "description": f"collect_items rows for {selector!r} via {mode}",
            },
        )
        result["recordExtraction"] = record_result
        contract_warning = _collect_contract_warning(
            agent, result=result, record_result=record_result
        )
        if contract_warning is not None:
            candidates = await discover_selector_candidates(agent, page_id, top=5)
            if candidates:
                contract_warning["selectorCandidates"] = candidates[:5]
            result["contractWarning"] = contract_warning
            result["next_step"] = contract_warning["next_instruction"]
        if _record_extraction_persisted(record_result):
            agent.pending_unrecorded_extraction = None
    elif collected:
        agent.pending_unrecorded_extraction = {
            "source": "collect_items", "step": step,
            "rowCount": len(collected), "turns": 0,
        }
        result["next_step"] = "Rows collected but not persisted; call record_extraction if this is target data."
    agent.logger.write("collect_items.result", {
        k: v for k, v in result.items() if k not in {"sample"}
    })
    return result
