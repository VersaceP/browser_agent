"""
harness.tools.browser_tools.page_create - Page.create failure recovery and fleet-loss handling.
"""

from typing import Any
from typing import Dict
from typing import List
from typing import Optional
from typing import Set
from typing import Tuple
import json
from urllib.parse import urlparse
from harness.utils import JsonDict

def _bt():
    import harness.tools.browser_tools as bt

    return bt

def _response_data(result: JsonDict) -> JsonDict:
    response = result.get("response") if isinstance(result, dict) else None
    if not isinstance(response, dict):
        return {}
    data = response.get("data")
    return data if isinstance(data, dict) else {}

def _raw_response_data(response: Any) -> JsonDict:
    if not isinstance(response, dict):
        return {}
    data = response.get("data")
    return data if isinstance(data, dict) else {}

def _page_create_error_text(result: Any) -> str:
    parts: List[str] = []

    def visit(value: Any, depth: int = 0) -> None:
        if depth > 5:
            return
        if isinstance(value, str):
            parts.append(value)
        elif isinstance(value, (int, float, bool)):
            parts.append(str(value))
        elif isinstance(value, dict):
            for key in ("error", "message", "code", "data", "observation"):
                if key in value:
                    visit(value.get(key), depth + 1)
            response = value.get("response")
            if isinstance(response, dict):
                visit(response, depth + 1)
        elif isinstance(value, list):
            for item in value[:10]:
                visit(item, depth + 1)

    visit(result)
    return " ".join(parts)

def _is_page_create_32005_failure(method: str, result: Any) -> bool:
    if method != "Page.create":
        return False
    text = _page_create_error_text(result).lower()
    return "-32005" in text and "page.create" in text

FLEET_LOSS_ERROR_CODES = frozenset({
    "FLEET_ARCHIVED",
    "FLEET_NOT_AVAILABLE",
    "FLEET_OWNERSHIP_MISMATCH",
    "FLEET_OWNER_MISMATCH",
})

def _fleet_loss_signal(result: Any) -> str:
    """Prefer Dispatcher structured codes, retaining one compatibility fallback."""

    signals: Set[str] = set()

    def visit(value: Any, depth: int = 0) -> None:
        if depth > 6:
            return
        if isinstance(value, dict):
            for key, nested in value.items():
                if key in {"code", "errorCode", "reasonCode", "reasonKind"}:
                    if isinstance(nested, str):
                        signals.add(nested.strip().upper())
                if key != "methodSchema":
                    visit(nested, depth + 1)
        elif isinstance(value, list):
            for nested in value[:20]:
                visit(nested, depth + 1)

    visit(result)
    structured = sorted(signals.intersection(FLEET_LOSS_ERROR_CODES))
    if structured:
        return structured[0]
    lowered = _page_create_error_text(result).lower()
    if any(marker in lowered for marker in (
        "is archived",
        "has been archived",
        "fleet archived",
        "owned by another agent",
        "not available for this agent",
    )):
        return "LEGACY_ERROR_TEXT"
    return ""

def _assigned_fleet_lost_result(
    agent: Any,
    method: str,
    params: JsonDict,
    result: JsonDict,
) -> Optional[JsonDict]:
    if not _bt()._fleet_reuse_enabled(agent):
        return None
    if method != "Page.create":
        return None
    error_text = _page_create_error_text(result)
    loss_signal = _fleet_loss_signal(result)
    if not loss_signal:
        return None
    session_key = str(getattr(agent, "fleet_session_key", "") or "").strip()
    fleet_id = str(
        params.get("fleetId") or getattr(agent, "assigned_fleet_id", "") or ""
    ).strip()
    status = "session_fleet_lost" if session_key else "fleet_assignment_lost"
    next_instruction = (
        "Treat this authenticated session as stale and follow the"
        " auth-interrupt/login recovery flow; do not retry the same binding."
        if session_key
        else "Stop this worker and request a fresh coordinator assignment."
    )
    lost_handler = getattr(agent, "auth_session_lost_handler", None)
    if session_key and callable(lost_handler):
        try:
            lost_handler({
                "sessionKey": session_key,
                "fleetId": fleet_id,
                "sessionGeneration": int(
                    getattr(agent, "fleet_session_generation", 0) or 0
                ),
                "reason": error_text[:500],
            })
        except Exception as exc:  # recovery bookkeeping must not mask evidence
            logger = getattr(agent, "logger", None)
            if logger is not None:
                logger.write(
                    "auth_fleet.lost_handler_failed",
                    {"sessionKey": session_key, "error": str(exc)[:300]},
                )
    answer = {
        "outcome": "blocked",
        "data": {},
        "evidence": [{
            "method": method,
            "fleetId": fleet_id,
            "error": error_text[:500],
        }],
        "blockers": [{
            "classification": status,
            "message": next_instruction,
        }],
        "next_steps": [next_instruction],
    }
    return {
        **result,
        "status": status,
        "terminal": True,
        "sessionKey": session_key,
        "assignedFleetId": fleet_id,
        "fleetLossSignal": loss_signal,
        "errorClassification": {
            "type": status,
            "suggested_action": "auth_interrupt" if session_key else "respawn_worker",
            "method": method,
        },
        "answer": json.dumps(answer, ensure_ascii=False),
        "next_instruction": next_instruction,
    }

def _pages_from_value(value: Any) -> List[JsonDict]:
    pages: List[JsonDict] = []

    def visit(item: Any, inherited_fleet_id: str = "") -> None:
        if isinstance(item, dict):
            fleet_id = str(item.get("fleetId") or inherited_fleet_id or "")
            page_id = item.get("pageId") or item.get("page_id")
            if isinstance(page_id, str) and page_id.strip():
                page = dict(item)
                if fleet_id and not page.get("fleetId"):
                    page["fleetId"] = fleet_id
                pages.append(page)
            for key, nested in item.items():
                if key == "methodSchema":
                    continue
                visit(nested, fleet_id)
        elif isinstance(item, list):
            for nested in item:
                visit(nested, inherited_fleet_id)

    visit(value)
    dedup: Dict[str, JsonDict] = {}
    for page in pages:
        page_id = str(page.get("pageId") or page.get("page_id") or "").strip()
        if page_id:
            dedup[page_id] = page
    return list(dedup.values())

async def _page_create_probe_call(agent: Any, method: str, params: JsonDict) -> JsonDict:
    try:
        runner = getattr(agent, "render_recovery_runner", None)
        if runner is not None:
            response, _recovery = await runner.call(method, params)
        else:
            response = await agent.browser.call(method, params)
        return {"ok": True, "method": method, "params": params, "response": response}
    except Exception as exc:  # noqa: BLE001 - diagnostic probe must record all failures.
        return {"ok": False, "method": method, "params": params, "error": str(exc)}

def _page_state_is_usable(response: Any) -> bool:
    if not isinstance(response, dict) or response.get("error"):
        return False
    data = _raw_response_data(response)
    status = str(data.get("status") or "").strip().lower()
    if status in {"closed", "crashed", "stale", "quarantined", "paused"}:
        return False
    hitl = data.get("hitl")
    if isinstance(hitl, dict) and hitl.get("isPaused") is True:
        return False
    return True

def _page_create_infrastructure_classification() -> JsonDict:
    return {
        "category": "blocked_infrastructure",
        "type": "browser_unavailable_or_no_page",
        "method": "Page.create",
        "hint": (
            "Page.create failed with -32005 and Fleet/Page probing found no"
            " usable existing page. Reconnect or rebuild the Browser Client"
            " before retrying this worker."
        ),
    }

def _page_create_terminal_answer(
    *,
    original_error: str,
    probe: JsonDict,
) -> str:
    classification = _page_create_infrastructure_classification()
    payload = {
        "outcome": "blocked",
        "data": {},
        "evidence": [
            {
                "method": "Page.create",
                "error": original_error[:500],
                "probeClassification": "browser_unavailable_or_no_page",
                "checkedPageCount": len(probe.get("checkedPages") or []),
            }
        ],
        "blockers": [
            {
                "classification": classification,
                "message": classification["hint"],
                "method": "Page.create",
            }
        ],
        "next_steps": [
            "Reconnect or restart the Browser Client/playground backend, then retry the worker.",
            "If Fleet.list/Page.list shows reusable pages later, prefer reusing one instead of creating a new page.",
        ],
    }
    return json.dumps(payload, ensure_ascii=False, default=str)

async def _recover_page_create_32005(
    agent: Any,
    params: JsonDict,
    result: JsonDict,
) -> Tuple[JsonDict, bool]:
    original_error = _page_create_error_text(result)
    assigned_fleet_id = str(
        params.get("fleetId")
        or getattr(agent, "assigned_fleet_id", "")
        or ""
    ).strip()
    probe: JsonDict = {
        "trigger": "Page.create_-32005",
        "originalError": original_error[:500],
        "fleetList": None,
        "pageLists": [],
        "checkedPages": [],
        "classification": "unknown",
    }
    page_candidates: List[JsonDict] = []
    if _bt()._fleet_reuse_enabled(agent):
        # A coordinator-managed fresh worker must never turn a create failure
        # into implicit adoption of another worker's page. Probe only explicit
        # local bindings plus pages authoritatively leased to this worker (the
        # latter covers direct skill/fast-path calls that bypass tool post-hooks).
        page_fleets = getattr(agent, "page_fleet_ids", None)
        page_fleets = page_fleets if isinstance(page_fleets, dict) else {}
        candidate_page_fleets = {
            str(page_id or "").strip(): str(fleet_id or "").strip()
            for page_id, fleet_id in page_fleets.items()
            if str(page_id or "").strip()
        }
        manager = getattr(agent, "page_lease_manager", None)
        worker_id = str(getattr(agent, "worker_id", "") or "").strip()
        if (
            manager is not None
            and hasattr(manager, "page_fleets_for_worker")
            and worker_id
        ):
            candidate_page_fleets.update(
                manager.page_fleets_for_worker(worker_id)
            )
        allowed_page_ids = {
            str(page_id or "").strip()
            for page_id in (getattr(agent, "allowed_page_ids", set()) or set())
            if str(page_id or "").strip()
        }
        allowed_page_ids.update(
            page_id
            for page_id in candidate_page_fleets
            if (
                manager is not None
                and hasattr(manager, "owner_for")
                and str(manager.owner_for(page_id) or "") == worker_id
            )
        )
        for page_id in sorted(allowed_page_ids):
            page_id = str(page_id or "").strip()
            if not page_id:
                continue
            candidate_fleet_id = str(
                candidate_page_fleets.get(page_id) or ""
            ).strip()
            if not candidate_fleet_id or (
                assigned_fleet_id
                and candidate_fleet_id != assigned_fleet_id
            ):
                continue
            page_candidates.append({
                "pageId": page_id,
                "fleetId": candidate_fleet_id,
            })
        probe["fleetList"] = {
            "skipped": True,
            "reason": "coordinator_page_delegation_only",
        }
    else:
        fleet_list = await _page_create_probe_call(agent, "Fleet.list", {})
        probe["fleetList"] = fleet_list
        page_candidates.extend(_pages_from_value(fleet_list.get("response")))
        fleets = _raw_response_data(fleet_list.get("response")).get("fleets")
        if isinstance(fleets, list):
            for fleet in fleets:
                if not isinstance(fleet, dict):
                    continue
                fleet_id = str(fleet.get("fleetId") or "").strip()
                if not fleet_id or (
                    assigned_fleet_id and fleet_id != assigned_fleet_id
                ):
                    continue
                listed = await _page_create_probe_call(
                    agent,
                    "Page.list",
                    {"fleetId": fleet_id},
                )
                probe["pageLists"].append(listed)
                for page in _pages_from_value(listed.get("response")):
                    page.setdefault("fleetId", fleet_id)
                    page_candidates.append(page)

        page_candidates.extend(
            _pages_from_value(getattr(agent, "preloaded_registration", None))
        )
    deduped: Dict[str, JsonDict] = {}
    for page in page_candidates:
        page_id = str(page.get("pageId") or page.get("page_id") or "").strip()
        fleet_id = str(page.get("fleetId") or "").strip()
        if (
            page_id
            and (not assigned_fleet_id or fleet_id == assigned_fleet_id)
        ):
            deduped[page_id] = page

    for page in list(deduped.values())[:5]:
        page_id = str(page.get("pageId") or page.get("page_id") or "").strip()
        if not page_id:
            continue
        state = await _page_create_probe_call(
            agent,
            "Page.getState",
            {
                "pageId": page_id,
                "purpose": "verify existing page after Page.create -32005",
            },
        )
        state_data = _raw_response_data(state.get("response"))
        candidate_fleet_id = str(page.get("fleetId") or "")
        checked = {
            "pageId": page_id,
            "fleetId": candidate_fleet_id,
            "ok": (
                bool(state.get("ok"))
                and _page_state_is_usable(state.get("response"))
            ),
            "status": state_data.get("status"),
            "title": state_data.get("title"),
            "url": state_data.get("url"),
            "error": state.get("error"),
        }
        probe["checkedPages"].append(checked)
        if checked["ok"]:
            probe["classification"] = "create_failed_but_existing_page_usable"
            response = {
                "observation": (
                    "Page.create failed with -32005, but an existing usable"
                    f" page was found and reused: pageId=\"{page_id}\""
                    f" fleetId=\"{checked['fleetId']}\"."
                ),
                "data": {
                    "pageId": page_id,
                    "fleetId": checked["fleetId"],
                    "reusedExistingPage": True,
                    "pageCreateOriginalError": original_error[:500],
                },
            }
            recovered = {
                "method": "Page.create",
                "params": params,
                "response": response,
                "pageCreateRecovery": probe,
                "next_instruction": (
                    "Continue with the reused pageId. Call Page.getState and"
                    " DOM.getAXTree before targeting page elements."
                ),
            }
            return recovered, False

    probe["classification"] = "browser_unavailable_or_no_page"
    classification = _page_create_infrastructure_classification()
    terminal = {
        "method": "Page.create",
        "params": params,
        "status": "incomplete",
        "terminal": True,
        "error": (
            "Page.create failed with -32005 and no usable existing page was"
            " found via Fleet.list/Page.list/Page.getState."
        ),
        "classification": classification,
        "errorClassification": {
            "type": "browser_unavailable_or_no_page",
            "suggested_action": "abort_worker_reconnect_browser_then_retry",
            "method": "Page.create",
        },
        "pageCreateRecovery": probe,
        "answer": _page_create_terminal_answer(
            original_error=original_error,
            probe=probe,
        ),
        "next_instruction": (
            "Stop this worker: the browser backend has no usable page after"
            " Page.create -32005, so no further browser action can be"
            " dispatched from it."
        ),
    }
    return terminal, True

def _attach_navigation_check(result: JsonDict, *, method: str, params: JsonDict) -> JsonDict:
    if method != "Page.navigate" or not isinstance(result, dict):
        return result
    target_url = str(params.get("url") or "").strip()
    if not target_url:
        return result
    data = _response_data(result)
    current_url = str(data.get("url") or "").strip()
    title = str(data.get("title") or "").strip()
    status = "unknown"
    hint = "Call Page.getState after the reactive load event to verify final URL before extraction."
    if current_url:
        status = "arrived" if _urls_same_destination(target_url, current_url) else "off_target"
    if _looks_like_challenge_title(title):
        status = "challenge_pending"
        hint = (
            "Navigation is on a challenge/interstitial surface. Do not extract target data yet;"
            " wait for settlement, request HITL if confirmed, then verify the final URL."
        )
    elif status == "off_target":
        hint = (
            "Navigation did not report the requested destination. Re-check Page.getState,"
            " then re-navigate or report the redirect/blocker before extracting."
        )
    enriched = dict(result)
    enriched["navigationCheck"] = {
        "status": status,
        "targetUrl": target_url,
        "currentUrl": current_url,
        "title": title,
        "hint": hint,
    }
    return enriched

def _urls_same_destination(expected: str, current: str) -> bool:
    try:
        expected_parts = urlparse(expected)
        current_parts = urlparse(current)
    except ValueError:
        return expected.rstrip("/") == current.rstrip("/")
    if expected_parts.netloc and current_parts.netloc:
        if expected_parts.netloc.lower() != current_parts.netloc.lower():
            return False
    expected_path = (expected_parts.path or "/").rstrip("/") or "/"
    current_path = (current_parts.path or "/").rstrip("/") or "/"
    if expected_path != current_path:
        return False
    if expected_parts.query and expected_parts.query != current_parts.query:
        return False
    if expected_parts.fragment and expected_parts.fragment != current_parts.fragment:
        return False
    return True

def _looks_like_challenge_title(title: str) -> bool:
    lowered = str(title or "").strip().lower()
    return lowered in {"just a moment...", "just a moment", "checking your browser..."}

def _attach_runtime_strategy_hints(result: JsonDict, *, method: str) -> JsonDict:
    if not isinstance(result, dict):
        return result
    classification = result.get("errorClassification")
    if not isinstance(classification, dict):
        return result
    if classification.get("type") != "occlusion_blocked":
        return result
    enriched = dict(result)
    blocked_target = ""
    params = result.get("params") if isinstance(result.get("params"), dict) else {}
    for key in ("id", "nodeId", "targetId", "selector"):
        value = params.get(key)
        if isinstance(value, str) and value.strip():
            blocked_target = value.strip()
            break
    enriched["runtimeStrategy"] = {
        "id": "browser_action.overlay.dismiss_overlay",
        "trigger": "occlusion_blocked",
        "method": method,
        "preferredTool": "dismiss_overlay",
        "call": {
            "tool": "dismiss_overlay",
            "pageId": params.get("pageId") or "",
            "targetId": blocked_target,
            # Only Input.click is auto-retried after dismissal; for any other
            # blocked method the tool returns dismissed_pending_action.
            "targetMethod": method if method == "Input.click" else "",
        },
        "safetyBoundary": (
            "dismiss_overlay never auto-clicks login/payment/provider buttons"
            " and never auto-retries consequential targets."
        ),
    }
    existing = str(enriched.get("next_instruction") or "").strip()
    overlay_instruction = (
        "Occlusion blocked this action. Call the dismiss_overlay tool with this"
        " pageId (and targetId=the blocked element id to auto-retry a safe"
        " action); it runs the close -> Escape -> verified-backdrop ladder"
        " internally and verifies the overlay is gone. Do not hand-run the"
        " ladder step by step."
    )
    enriched["next_instruction"] = (
        f"{existing} {overlay_instruction}".strip()
        if existing
        else overlay_instruction
    )
    return enriched
