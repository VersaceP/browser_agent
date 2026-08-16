"""
harness.tools.browser_tools.visual - visual_verify, VL arbitration, reality checks and repair evidence.
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
from harness.utils import optional_int
from .axtree_state import _check_stale_axtree_target

def _bt():
    import harness.tools.browser_tools as bt

    return bt

def _result_occlusion_blocked(result: Any) -> bool:
    """True when an action failed specifically because an overlay occluded the
    target. Distinct from generic failure: an occluded load-more is recoverable
    (dismiss the overlay and retry), not exhaustion."""
    if not isinstance(result, dict):
        return False
    classification = result.get("errorClassification")
    return isinstance(classification, dict) and classification.get("type") == "occlusion_blocked"

def _layers_from_result(result: JsonDict) -> List[JsonDict]:
    data = _bt()._response_data(result)
    layers = data.get("layers")
    return [layer for layer in layers if isinstance(layer, dict)] if isinstance(layers, list) else []

def _viewport_from_layers(layers: List[JsonDict]) -> JsonDict:
    for layer in layers:
        if layer.get("isMainFrame"):
            bounds = layer.get("viewportBounds")
            if isinstance(bounds, dict):
                return bounds
    for layer in layers:
        bounds = layer.get("viewportBounds")
        if isinstance(bounds, dict):
            return bounds
    return {}

def _log_dismiss_overlay(
    agent: Any,
    page_id: str,
    status: str,
    overlay: Optional[JsonDict],
    attempts: List[JsonDict],
) -> None:
    logger = getattr(agent, "logger", None)
    if logger is not None:
        logger.write(
            "dismiss_overlay.result",
            {
                "pageId": page_id,
                "status": status,
                "subtype": (overlay or {}).get("subtype"),
                "attemptCount": len(attempts),
                "attempts": attempts,
            },
        )

def _repair_identity_text(value: Any) -> str:
    return str(value).strip() if value is not None else ""

def _repair_visual_target_signature(identity: Any, field: Any) -> str:
    identity_field = (
        str(identity.get("field") or "").strip()
        if isinstance(identity, dict) else ""
    )
    identity_value = (
        _repair_identity_text(identity.get("value"))
        if isinstance(identity, dict) else ""
    )
    return json.dumps(
        [identity_field, identity_value, str(field or "").strip()],
        ensure_ascii=False,
        separators=(",", ":"),
    )

def _normalized_repair_page(url: Any) -> Tuple[str, str]:
    """Normalize a repair evidence URL to its stable host/path destination."""
    raw = str(url or "").strip()
    try:
        parsed = urlparse(raw)
    except ValueError:
        return "", raw.rstrip("/")
    host = parsed.netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    path = (parsed.path or "/").rstrip("/") or "/"
    return host, path

def _repair_page_binding(raw: Any) -> Optional[JsonDict]:
    if not isinstance(raw, dict):
        return None
    field = str(raw.get("field") or "").strip()
    url = str(raw.get("url") or "").strip()
    host, _ = _normalized_repair_page(url)
    if not field or not host:
        return None
    return {"field": field, "url": url}

def _validated_repair_visual_targets(
    agent: Any,
    raw_targets: Any,
) -> Tuple[List[JsonDict], Optional[JsonDict]]:
    if raw_targets in (None, []):
        return [], None
    if not isinstance(raw_targets, list):
        return [], {
            "status": "rejected",
            "error": "visual_verify.repair_targets must be an array",
            "tool_was_executed": False,
        }
    contract = getattr(agent, "worker_contract", None)
    manifest = (
        contract.get("_repair_manifest") if isinstance(contract, dict) else None
    )
    repairs = manifest.get("repairs") if isinstance(manifest, dict) else None
    if not isinstance(repairs, list) or not repairs:
        return [], {
            "status": "rejected",
            "error": "repair_targets require an active repair manifest",
            "tool_was_executed": False,
        }
    allowed: Dict[Tuple[str, str], Set[str]] = {}
    identity_values: Dict[Tuple[str, str], Any] = {}
    page_bindings: Dict[Tuple[str, str], JsonDict] = {}
    for item in repairs:
        identity = item.get("identity") if isinstance(item, dict) else None
        identity_field = (
            str(identity.get("field") or "").strip()
            if isinstance(identity, dict) else ""
        )
        identity_value = (
            _repair_identity_text(identity.get("value"))
            if isinstance(identity, dict) else ""
        )
        fields = item.get("fields") if isinstance(item, dict) else None
        if identity_field and identity_value and isinstance(fields, list):
            key = (identity_field, identity_value)
            allowed[key] = {
                str(field).strip() for field in fields if str(field).strip()
            }
            identity_values[key] = identity.get("value")
            page_binding = _repair_page_binding(item.get("pageBinding"))
            if page_binding is not None:
                page_bindings[key] = page_binding

    normalized: List[JsonDict] = []
    seen_signatures: Set[str] = set()
    for index, raw_target in enumerate(raw_targets):
        identity = raw_target.get("identity") if isinstance(raw_target, dict) else None
        identity_field = (
            str(identity.get("field") or "").strip()
            if isinstance(identity, dict) else ""
        )
        identity_value = (
            _repair_identity_text(identity.get("value"))
            if isinstance(identity, dict) else ""
        )
        fields = raw_target.get("fields") if isinstance(raw_target, dict) else None
        target_fields = sorted({
            str(field).strip() for field in fields if str(field).strip()
        }) if isinstance(fields, list) else []
        key = (identity_field, identity_value)
        if (
            key not in allowed
            or not target_fields
            or any(field not in allowed[key] for field in target_fields)
        ):
            return [], {
                "status": "rejected",
                "error": (
                    f"visual_verify.repair_targets[{index}] must match one"
                    " manifest identity and its repair fields"
                ),
                "tool_was_executed": False,
            }
        fresh_fields = []
        for field in target_fields:
            signature = _repair_visual_target_signature(identity, field)
            if signature in seen_signatures:
                continue
            seen_signatures.add(signature)
            fresh_fields.append(field)
        if fresh_fields:
            target = {
                "identity": {
                    "field": identity_field,
                    "value": identity_values[key],
                },
                "fields": fresh_fields,
            }
            if key in page_bindings:
                target["pageBinding"] = dict(page_bindings[key])
            normalized.append(target)
    return normalized, None

async def _verify_repair_visual_page(
    agent: Any,
    page_id: str,
    targets: List[JsonDict],
    step: int,
) -> Tuple[JsonDict, Optional[JsonDict]]:
    target_bindings = [
        _repair_page_binding(target.get("pageBinding")) for target in targets
    ]
    bindings = [binding for binding in target_bindings if binding is not None]
    if not bindings:
        return {"status": "unavailable"}, None
    if len(bindings) != len(targets):
        return {"status": "mixed_bindings"}, {
            "status": "rejected",
            "error": (
                "repair_targets mix page-bound and unbound rows; verify them"
                " in separate visual_verify calls"
            ),
            "tool_was_executed": False,
        }

    destinations = {
        _normalized_repair_page(binding["url"]) for binding in bindings
    }
    if len(destinations) != 1:
        return {"status": "conflicting_targets"}, {
            "status": "rejected",
            "error": (
                "repair_targets resolve to different pages; verify each page"
                " in a separate visual_verify call"
            ),
            "tool_was_executed": False,
        }

    expected_urls = sorted({binding["url"] for binding in bindings})
    state = await _bt()._invoke_browser_method(
        agent,
        "Page.getState",
        {
            "pageId": page_id,
            "purpose": "Bind repair absence evidence to its expected baseline page",
        },
        step,
    )
    data = _bt()._response_data(state)
    current_url = str(data.get("url") or data.get("currentUrl") or "").strip()
    binding_result = {
        "status": "unverified",
        "expectedUrls": expected_urls,
        "currentUrl": current_url,
    }
    if not current_url:
        return binding_result, {
            "status": "repair_visual_page_unverified",
            "error": "Page.getState did not return a URL for repair evidence",
            "expectedPageUrls": expected_urls,
            "tool_was_executed": True,
            "next_instruction": (
                "Re-establish the target page and retry visual_verify; repair"
                " absence evidence cannot be attached without a current URL."
            ),
        }
    if _normalized_repair_page(current_url) not in destinations:
        binding_result["status"] = "mismatch"
        return binding_result, {
            "status": "repair_visual_wrong_page",
            "error": "visual repair evidence was requested on the wrong page",
            "expectedPageUrls": expected_urls,
            "currentUrl": current_url,
            "tool_was_executed": True,
            "next_instruction": (
                "Navigate or switch to the manifest-bound target page, confirm"
                " it with Page.getState, then retry visual_verify."
            ),
        }
    binding_result["status"] = "matched"
    return binding_result, None

def _record_repair_visual_evidence(
    agent: Any,
    targets: List[JsonDict],
    result: JsonDict,
    *,
    question: str,
) -> List[JsonDict]:
    if (
        not targets
        or str(result.get("status") or "") != "done"
        or str(result.get("verdict") or "").strip().lower() != "absent"
    ):
        return []
    has_page_binding = any(
        _repair_page_binding(target.get("pageBinding")) is not None
        for target in targets
    )
    page_binding = result.get("repairPageBinding")
    if has_page_binding and (
        not isinstance(page_binding, dict)
        or page_binding.get("status") != "matched"
    ):
        return []
    contract = getattr(agent, "worker_contract", None)
    manifest = (
        contract.get("_repair_manifest") if isinstance(contract, dict) else None
    )
    if not isinstance(manifest, dict):
        return []
    satisfied = manifest.get("visualEvidenceSatisfied")
    if not isinstance(satisfied, dict):
        satisfied = {}
        manifest["visualEvidenceSatisfied"] = satisfied
    recorded: List[JsonDict] = []
    for target in targets:
        identity = target.get("identity")
        for field in target.get("fields") or []:
            signature = _repair_visual_target_signature(identity, field)
            evidence = {
                "identity": dict(identity) if isinstance(identity, dict) else {},
                "field": str(field),
                "signature": signature,
                "screenshotPath": str(result.get("screenshotPath") or ""),
                "verdict": "absent",
                "question": question[:500],
            }
            if isinstance(page_binding, dict):
                evidence["pageBinding"] = dict(page_binding)
            satisfied[signature] = evidence
            recorded.append(evidence)
    if recorded:
        pending = manifest.get("visualEvidencePending")
        recorded_signatures = {item["signature"] for item in recorded}
        if isinstance(pending, list):
            remaining = [
                item for item in pending
                if isinstance(item, dict)
                and str(item.get("signature") or "") not in recorded_signatures
            ]
            if remaining:
                manifest["visualEvidencePending"] = remaining
            else:
                manifest.pop("visualEvidencePending", None)
        agent.logger.write("repair.visual_evidence_satisfied", {
            "targets": recorded,
        })
    return recorded

async def _visual_verify(agent: Any, tool_input: JsonDict, step: int) -> JsonDict:
    vl_config = getattr(agent.runtime.harness, "vl", None)
    if vl_config is None or not getattr(vl_config, "enabled", False):
        return {
            "status": "disabled",
            "reason": "vl.enabled is false or vl config is missing",
        }
    raw_max_checks = optional_int(
        getattr(vl_config, "max_checks_per_worker", 2),
        2,
    )
    max_checks = max(0, raw_max_checks if raw_max_checks is not None else 2)
    page_id = str(tool_input.get("pageId") or "").strip()
    if not page_id:
        return {"status": "failed", "error": "pageId is required"}
    selector = str(tool_input.get("selector") or "").strip()
    element_id = str(tool_input.get("id") or "").strip()
    requested_mode = str(tool_input.get("mode") or "action_outcome").strip()
    mode = requested_mode
    question = str(tool_input.get("question") or "").strip()
    repair_targets, repair_target_error = _validated_repair_visual_targets(
        agent, tool_input.get("repair_targets"),
    )
    if repair_target_error is not None:
        return repair_target_error
    # Target-bound repair evidence is a machine-enforced completion gate, so an
    # earlier overlay/layout check must not exhaust its budget. It uses the
    # separate forced counter and remains bounded by the worker's step limit.
    force_check = bool(tool_input.get("_force", False)) or bool(repair_targets)
    if not force_check and getattr(agent, "vl_check_count", 0) >= max_checks:
        return {
            "status": "rejected",
            "reason": "vl_check_limit_reached",
            "maxChecksPerWorker": max_checks,
            "next_instruction": (
                "Do not keep using screenshots. Use DOM/Runtime evidence or"
                " finalize with the blocker."
            ),
        }
    if repair_targets:
        mode = "repair_absence"
        question = (
            f"{question}\nRepair evidence targets: "
            f"{json.dumps(repair_targets, ensure_ascii=False, default=str)}. "
            "Determine whether the expected content for these exact fields is"
            " absent on the current page."
        ).strip()
    expected = tool_input.get("expected")
    if not isinstance(expected, dict):
        expected = {}
    elif repair_targets:
        expected = dict(expected)
    if repair_targets:
        expected["repair_targets"] = repair_targets
    full_page = bool(tool_input.get("fullPage", False))

    repair_page_binding: JsonDict = {"status": "not_applicable"}
    if repair_targets:
        repair_page_binding, page_binding_error = await _verify_repair_visual_page(
            agent, page_id, repair_targets, step,
        )
        if page_binding_error is not None:
            agent.logger.write("repair.visual_page_rejected", {
                **page_binding_error,
                "pageId": page_id,
                "repairTargets": repair_targets,
            })
            return page_binding_error

    screenshot_params: JsonDict = {
        "pageId": page_id,
        "fullPage": full_page,
        "options": {"format": "file"},
        "purpose": f"Visual verification for {mode or 'action_outcome'}",
    }
    if selector:
        screenshot_params["selector"] = selector
    if element_id:
        screenshot_params["id"] = element_id
    stale_target = _check_stale_axtree_target(
        agent,
        "Page.screenshot",
        screenshot_params,
    )
    if stale_target is not None:
        return stale_target

    screenshot_scope = (
        "element" if (selector or element_id)
        else ("fullpage" if full_page else "viewport")
    )
    before_artifacts = set(str(path) for path in getattr(agent, "artifacts", []))
    screenshot = await _bt()._invoke_browser_method(
        agent,
        "Page.screenshot",
        screenshot_params,
        step,
    )
    image_path = _bt()._screenshot_saved_path(screenshot)
    if not image_path:
        after_artifacts = [
            str(path) for path in getattr(agent, "artifacts", [])
            if str(path) not in before_artifacts
        ]
        image_path = after_artifacts[-1] if after_artifacts else ""
    if not image_path and (selector or element_id or full_page):
        # skillsGuide §5: if element capture fails, do not repeat it — resync
        # once with Page.getState, then fall back to a viewport screenshot. The
        # verdict consumer sees screenshotScope so it knows the crop widened.
        #
        # `full_page` takes the same road for a different reason: the platform
        # serves it from CDP `Page.captureScreenshot{captureBeyondViewport}`,
        # which fails on a page taller than the compositor will surface. That
        # is not an edge case — across two live canaries 113 of 114 full-page
        # captures failed, and the single success was a freshly loaded page
        # before the worker expanded anything. So the capture worked only
        # while there was nothing worth looking at, and failed on exactly the
        # content-heavy pages a stuck worker needs to see. A viewport shot is
        # bounded by construction, and the reality check scrolls its region
        # into view first, so the narrower frame is also the better-aimed one.
        await _bt()._invoke_browser_method(
            agent,
            "Page.getState",
            {
                "pageId": page_id,
                "purpose": "Resync page state after element screenshot failed before viewport fallback",
            },
            step,
        )
        fallback_params: JsonDict = {
            "pageId": page_id,
            "fullPage": False,
            "options": {"format": "file"},
            "purpose": (
                "Viewport fallback after full-page screenshot failure"
                if full_page and not (selector or element_id)
                else "Viewport fallback after element screenshot failure"
            ),
        }
        before_artifacts = set(str(path) for path in getattr(agent, "artifacts", []))
        screenshot = await _bt()._invoke_browser_method(
            agent,
            "Page.screenshot",
            fallback_params,
            step,
        )
        image_path = _bt()._screenshot_saved_path(screenshot)
        if not image_path:
            after_artifacts = [
                str(path) for path in getattr(agent, "artifacts", [])
                if str(path) not in before_artifacts
            ]
            image_path = after_artifacts[-1] if after_artifacts else ""
        if image_path:
            screenshot_scope = "viewport_fallback"
    if not image_path:
        return {
            "status": "failed",
            "error": "screenshot did not produce a saved image path",
            "screenshot": agent._trim_for_model(screenshot),
        }

    if force_check:
        agent.vl_force_check_count = getattr(agent, "vl_force_check_count", 0) + 1
    else:
        agent.vl_check_count = getattr(agent, "vl_check_count", 0) + 1
    verdict = await _bt().visual_verify_image(
        config=vl_config,
        image_path=image_path,
        expected=expected,
        mode=mode,
        question=question,
    )
    # VL Role A: promote a located pixel back to a durable canonical id (bbox→id),
    # so the agent acts on a stable handle instead of raw coordinates. Gated by
    # vl.visual_locate_enabled; best-effort (any failure leaves the raw point).
    if (
        mode == "visual_locate"
        and isinstance(verdict, dict)
        and verdict.get("verdict") == "located"
        and verdict.get("point")
        and bool(getattr(vl_config, "visual_locate_enabled", False))
    ):
        verdict = await _promote_visual_locate(
            agent, page_id, image_path, verdict, step,
            expected_text=" ".join(
                part for part in (question, str(expected.get("target") or ""))
                if part
            ),
        )
    vl_check_count = getattr(agent, "vl_check_count", 0)
    vl_force_check_count = getattr(agent, "vl_force_check_count", 0)
    result = {
        **verdict,
        "mode": mode,
        "screenshotPath": image_path,
        "screenshotScope": screenshot_scope,
        "selector": selector or None,
        "id": element_id or None,
        "vlCheckCount": vl_check_count,
        "vlForceCheckCount": vl_force_check_count,
        "maxChecksPerWorker": max_checks,
        "forced": force_check,
        "usage_boundary": (
            "visual_verify is evidence for action/state verification only;"
            " do not use it as final structured extraction."
        ),
    }
    if repair_targets:
        result["repairPageBinding"] = repair_page_binding
        if requested_mode != mode:
            result["requestedMode"] = requested_mode
    repair_evidence = _record_repair_visual_evidence(
        agent,
        repair_targets,
        result,
        question=question,
    )
    if repair_targets:
        result["repairTargets"] = repair_targets
    if repair_evidence:
        result["repairEvidenceSatisfied"] = repair_evidence
    elif repair_targets and result.get("status") == "done":
        verdict_name = str(result.get("verdict") or "uncertain")
        if verdict_name == "present":
            result["status"] = "repair_visual_contradiction"
            result["next_instruction"] = (
                "The visual check found the target content present. Do not mark"
                " it confirmed_absent; extract the visible value and submit a"
                " non-empty repair patch instead."
            )
            event_type = "repair.visual_evidence_contradicted"
        else:
            result["status"] = "repair_visual_inconclusive"
            result["next_instruction"] = (
                "The screenshot did not prove absence. Reframe or expand the"
                " relevant page region and retry, or leave the repair unresolved."
            )
            event_type = "repair.visual_evidence_inconclusive"
        agent.logger.write(event_type, {
            "pageId": page_id,
            "verdict": verdict_name,
            "repairTargets": repair_targets,
            "screenshotPath": image_path,
        })
    agent.logger.write(
        "vl.visual_verify",
        {
            key: value
            for key, value in result.items()
            if key not in {"visible_evidence"}
        },
    )
    return result

def _arbiter_error_text(result: JsonDict) -> str:
    """Pull a failure string from a browser_call result (else '')."""
    if not isinstance(result, dict):
        return ""
    if result.get("error"):
        return str(result["error"])
    response = result.get("response")
    if isinstance(response, dict):
        if response.get("error"):
            return str(response["error"])
        obs = response.get("observation")
        if isinstance(obs, str) and "fail" in obs.lower():
            return obs
    return ""

def _arbiter_next_instruction(rec: JsonDict) -> str:
    action = rec.get("action")
    if action == "retry_by_id":
        return (f"VL arbiter located the target and promoted it to durable id"
                f" {rec.get('id')!r}. Retry the failed action targeting that id"
                f" (a durable handle — not coordinates).")
    if action == "hitl":
        return (f"VL arbiter assessment: {rec.get('reason', 'needs human/challenge handling')}."
                f" Take the HITL/challenge path instead of retrying blindly.")
    if action == "dismiss":
        label = rec.get("label")
        return (f"VL arbiter found a safe dismiss control{(' (' + str(label) + ')') if label else ''}."
                f" Dismiss the overlay, then retry the action.")
    if action == "coordinate":
        return ("VL arbiter located the target but no AXTree node covers it; if safe and"
                " not consequential, use one coordinate action at cssPoint (never persist coordinates).")
    if action == "reperceive":
        return "VL arbiter suggests re-perceiving: refresh Page.getState + DOM.getAXTree before retrying."
    return ""

async def _maybe_vl_arbitrate(
    agent: Any,
    method: str,
    params: JsonDict,
    result: JsonDict,
    step: int,
) -> JsonDict:
    """Role D auto-trigger: on a visually-related failure, route to the VL arbiter
    and attach a recovery recommendation. Best-effort + gated (vl.arbiter_enabled);
    bounded per worker by max_checks_per_worker. Never raises into the call path."""
    if not isinstance(result, dict):
        return result
    vl_config = getattr(getattr(getattr(agent, "runtime", None), "harness", None), "vl", None)
    if (vl_config is None or not getattr(vl_config, "enabled", False)
            or not getattr(vl_config, "arbiter_enabled", False)):
        return result
    error_text = _arbiter_error_text(result)
    if not error_text:
        return result
    classification = ""
    cl = result.get("errorClassification")
    if isinstance(cl, dict):
        classification = str(cl.get("type") or "")
    page_id = str((params or {}).get("pageId") or "")
    browser = getattr(agent, "browser", None)
    if not page_id or browser is None:
        return result
    # bound the number of arbiter VL calls per worker
    max_checks = optional_int(getattr(vl_config, "max_checks_per_worker", 2), 2) or 2
    if getattr(agent, "vl_arbiter_count", 0) >= max_checks:
        return result
    try:
        from harness.vl.arbiter import arbitrate, is_visual_failure
        if not is_visual_failure(classification, error_text):
            return result
        agent.vl_arbiter_count = getattr(agent, "vl_arbiter_count", 0) + 1
        rec = await arbitrate(
            browser, page_id, classification_type=classification, error_text=error_text,
            target_description=str((params or {}).get("purpose") or ""),
            vl_config=vl_config, logger=getattr(agent, "logger", None),
        )
    except Exception as exc:  # arbitration must never break the call path
        logger = getattr(agent, "logger", None)
        if logger is not None:
            logger.write("vl.arbiter.error", {"method": method, "error": str(exc)})
        return result
    if not isinstance(rec, dict) or rec.get("action") in (None, "none"):
        return result
    out = {**result, "vlArbiter": rec}
    instruction = _arbiter_next_instruction(rec)
    if instruction:
        out["next_instruction"] = instruction
    return out

def _reality_check_region(tool_input: JsonDict) -> JsonDict:
    """The region the failing tool was actually working on.

    Read off the caller's own params rather than any list of known page
    structures: a container/selector/id the worker passed IS its declaration of
    where it expected the content, and it is the only region the harness can
    honestly name. No locator means no region, and the check stays page-scoped.
    """
    region: JsonDict = {}
    container = tool_input.get("container")
    if isinstance(container, dict):
        for key in ("id", "selector"):
            value = str(container.get(key) or "").strip()
            if value:
                region[key] = value
    for source, key in (
        ("containerId", "id"),
        ("containerSelector", "selector"),
        ("id", "id"),
        ("selector", "selector"),
    ):
        if key in region:
            continue
        value = str(tool_input.get(source) or "").strip()
        if value:
            region[key] = value
    # A human-readable hint for the prompt, never a decision: the model cannot
    # see a selector, so whatever the caller wrote about what it was looking
    # for describes the region better. `name` is last because it is ambiguous
    # across tools (an accessible-name query in find_in_axtree, an artifact
    # name in record_extraction) — useful as a hint, wrong as a source of truth.
    for key in ("purpose", "query", "text", "name"):
        description = str(tool_input.get(key) or "").strip()
        if description:
            region["description"] = description[:200]
            break
    return region

def _region_hint_text(region: JsonDict) -> str:
    """Human-readable region for the VL prompt, preferring the worker's own
    words over a selector the model cannot see anyway."""
    description = str(region.get("description") or "").strip()
    if description:
        return description
    selector = str(region.get("selector") or "").strip()
    if selector:
        return f"the page section matching {selector}"
    return ""

async def _scroll_region_into_view(
    agent: Any,
    page_id: str,
    region: JsonDict,
    step: int,
) -> JsonDict:
    """Bring the region into the root viewport before capturing it.

    Uses Input.scroll target mode, whose receipt answers the one question a
    screenshot cannot: was the thing we are about to ask about actually in
    frame. A failure here is not fatal — it just leaves the capture unproven,
    which downgrades what the verdict may be used for rather than blocking it.
    """
    locator = {
        key: region[key] for key in ("id", "selector") if region.get(key)
    }
    if not locator:
        return {}
    return await _bt()._invoke_browser_method(
        agent,
        "Input.scroll",
        {
            "pageId": page_id,
            "target": locator,
            "purpose": "reality check: bring the region into view before capture",
        },
        step,
    )

def _reality_check_summary(row: JsonDict) -> JsonDict:
    """What the worker sees of the check.

    Carries the standing fields (`evidenceGrade`, `mayTerminate`, whether the
    region was provably in frame) alongside the observation, so a model reading
    only the tool result — never the persisted artifact — still sees that this
    is an assertion and what it may be used for.
    """
    summary: JsonDict = {
        "verdict": row.get("verdict"),
        "observation": row.get("observation"),
        "screenshotPath": row.get("screenshotPath"),
        "targetShortfallStreak": row.get("targetShortfallStreak"),
        "evidenceGrade": row.get("evidenceGrade"),
        "mayTerminate": row.get("mayTerminate"),
        "claimScope": row.get("claimScope"),
    }
    for key in ("rowKey", "verdictClass", "claimedClass", "overrideReason",
                "regionInCapture", "itemCount", "armedBy",
                "turnsSinceArtifactProgress"):
        if key in row:
            summary[key] = row[key]
    return summary

def _page_reality_check_instruction(evidence_path: str) -> str:
    """Instruction for the page-scoped fallback (no assigned row matched this
    URL — a listing page, or a contract carrying no row keys).

    The verdict is free-form here, so the worker does the comparing. What the
    harness must still say is what the verdict is WORTH: the old wording told
    the worker to declare target_absent citing this artifact, which contradicts
    the mayTerminate=False the same artifact records and walks straight into
    the spawner's visual-evidence-only rejection.
    """
    return (
        "A visual reality check ran because perception kept falling short of"
        " the task target. It is an advisory model reading of one screenshot,"
        " not a measurement, and it cannot close anything on its own: if it"
        " shows the content somewhere on the page, adjust your perception"
        " (scroll/selector) and go read it; if it agrees the content is not"
        " there, that is a reason to verify mechanically — materialize the"
        " region, enumerate it to exhaustion, calibrate your selector against"
        " a page where it does match — not a reason to stop. When you do"
        " report a blocker, cite what you actually observed alongside"
        f" {evidence_path or 'the reality-check artifact'}; a citation naming"
        " only this artifact is rejected."
    )

def _reality_check_instruction(
    *,
    reconciled: Optional[JsonDict],
    grading: Optional[JsonDict],
    capture: JsonDict,
    evidence_path: str,
) -> str:
    """What the worker should do with this verdict, given its standing.

    Deliberately asymmetric. "There is content here" always redirects work and
    is stated as an instruction. Everything else is reported as an observation
    that does not close anything, because an advisory model claim that ends a
    row is the failure this whole path exists to prevent.
    """
    from harness.vl.capture_geometry import (
        CAPTURE_DISPROVEN,
        CLASS_AUTH_OVERLAY,
        CLASS_CONTENT_PRESENT,
        CLASS_EXPLICIT_EMPTY,
        CLASS_REGION_NOT_IN_CAPTURE,
    )

    if not reconciled or not grading:
        return _page_reality_check_instruction(evidence_path)
    resolved = str(reconciled.get("class") or "")
    citation = evidence_path or "the reality-check artifact"
    if resolved == CLASS_CONTENT_PRESENT:
        return (
            "A visual check reports that the region DOES hold content. Do not"
            " declare absence for it. Re-read that region — refresh"
            " DOM.getAXTree, then extract from the container the check"
            " describes."
        )
    if resolved == CLASS_REGION_NOT_IN_CAPTURE:
        detail = (
            " The scroll receipt confirms the region was not in the captured"
            " frame, so this says nothing about whether the content exists."
            if str(capture.get("state") or "") == CAPTURE_DISPROVEN else
            " This says only that the region was not visible in this capture."
        )
        return (
            "The visual check could not see the region." + detail
            + " Materialize it first (open the tab/accordion that owns it,"
            " scroll it into view, or wait for it to load) and observe again."
            " Do not report absence from this."
        )
    if resolved == CLASS_AUTH_OVERLAY:
        return (
            "The visual check reports a login/paywall overlay over this page."
            " That is a fact about THIS page epoch, not about the content"
            " behind it and not about any other item: run the safe dismiss"
            " ladder, re-navigate, and re-observe before recording a blocker,"
            f" citing {citation}."
        )
    if resolved == CLASS_EXPLICIT_EMPTY:
        if not grading.get("directsWork"):
            return (
                "The visual check read an explicit empty state in the region."
                " This is an advisory model observation, not proof: it does not"
                " by itself satisfy confirmed_absent. To record the field as"
                " absent you still owe the mechanical obligations — the region"
                " materialized in this navigation epoch, the overlay clear, the"
                " selector calibrated against a peer that HAS content, and the"
                f" page's own empty-state text captured. Cite {citation}"
                " alongside them, never instead of them."
            )
        return (
            "The visual check read an explicit empty state in the region. Use"
            " it to corroborate a confirmed_absent declaration, and still"
            " discharge the mechanical obligations (region materialized this"
            " epoch, overlay clear, selector calibrated, empty-state text"
            f" captured), citing {citation}."
        )
    return (
        "The visual check was inconclusive about the region. It is not evidence"
        " of absence. Observe again after materializing the region, or record"
        " the outstanding obligations rather than a verdict."
    )

REALITY_CHECK_CAPTURE_FAILURE_LIMIT = 2

async def _maybe_reality_check(
    agent: Any,
    tool_call: JsonDict,
    result: JsonDict,
    step: int,
) -> JsonDict:
    """Layer-2 visual reality check: after a target-shortfall streak (tools
    keep yielding nothing OR yielding rows that never satisfy the phase
    contract — mis-attributed rows look productive while missing the target),
    auto-run a full-page screenshot + VL against a claim synthesized from the
    worker contract, persist the observation through record_extraction (so
    its savedPath is ledger-valid evidence for target_absent claims), and
    attach the verdict to the tool result. Task-type agnostic — the trigger
    is the streak, not any validator kind. Best-effort + gated; never raises
    into the path."""
    if not isinstance(result, dict):
        return result
    vl_config = getattr(
        getattr(getattr(agent, "runtime", None), "harness", None), "vl", None
    )
    if (
        vl_config is None
        or not getattr(vl_config, "enabled", False)
        or not getattr(vl_config, "reality_check_enabled", True)
    ):
        return result
    try:
        from harness.vl.reality_check import (
            artifact_stall_turns,
            build_reality_check_row,
            classify_target_yield,
            stall_armed,
            synthesize_claim,
        )
        name = str(tool_call.get("name") or "")
        tool_input = tool_call.get("input") or {}
        if not isinstance(tool_input, dict):
            tool_input = {}
        threshold = max(
            1,
            optional_int(
                getattr(vl_config, "reality_check_shortfall_threshold", 3), 3
            ) or 3,
        )
        stall_threshold = optional_int(
            getattr(vl_config, "reality_check_stall_turns", 15), 15
        )
        stall_threshold = 15 if stall_threshold is None else stall_threshold

        yield_state = classify_target_yield(name, result)
        if yield_state is False:
            agent.target_shortfall_streak = 0
            return result
        if yield_state is True:
            agent.target_shortfall_streak = (
                getattr(agent, "target_shortfall_streak", 0) + 1
            )

        # Two independent ways to be stuck, and the second one has no yield to
        # count: a worker looping on DOM.getAXTree / DOM.getSemanticTree /
        # local_fs_read produces nothing the shortfall streak can see, so
        # before this it could spend its whole budget with the streak at 0 and
        # the check never armed (observed live in task e3173b5b).
        armed_by = ""
        if getattr(agent, "target_shortfall_streak", 0) >= threshold:
            armed_by = "target_shortfall"
        elif stall_armed(agent, stall_threshold):
            armed_by = "artifact_stall"
        if not armed_by:
            return result
        if getattr(agent, "reality_check_count", 0) >= 1:
            return result
        if (
            getattr(agent, "reality_check_capture_failures", 0)
            >= REALITY_CHECK_CAPTURE_FAILURE_LIMIT
        ):
            # Perception is unavailable on this worker, not merely unhelpful.
            # Re-arming would keep spending the step budget on a capture that
            # has already proven it cannot land.
            return result
        page_id = str(tool_input.get("pageId") or "").strip()
        if not page_id:
            # The stall trigger fires on tools that carry no pageId at all
            # (local_fs_read, and any call made after the page moved on). The
            # last AXTree page is the surface the worker was actually reading,
            # so the check still has something to look at instead of being
            # dropped exactly when the worker is most lost.
            page_id = str(getattr(agent, "axtree_page_id", "") or "").strip()
        if not page_id:
            urls = getattr(agent, "page_urls", None)
            if isinstance(urls, dict) and urls:
                page_id = str(next(reversed(list(urls))) or "").strip()
        if not page_id:
            return result
        from harness.vl.capture_geometry import (
            evidence_grade,
            reconcile_region_verdict,
            region_in_capture,
            scroll_coverage,
        )
        from harness.vl.reality_check import (
            assigned_row_keys,
            build_row_scoped_claim,
            resolve_current_row,
        )

        contract = getattr(agent, "worker_contract", None)
        page_url = str(getattr(agent, "page_urls", {}).get(page_id) or "")
        row_key = resolve_current_row(
            page_url, assigned_row_keys(contract, getattr(agent, "phase", None)),
        ) or ""
        region = _reality_check_region(tool_input)
        # Scope the question to the item this page actually is. Asking a detail
        # page whether the whole cohort's expectation is met invites a truthful
        # "no" that means nothing about the field the worker is missing — the
        # 5324506f defect.
        if row_key:
            claim = build_row_scoped_claim(
                worker_contract=contract,
                row_key=row_key,
                region_hint=_region_hint_text(region),
            )
            mode = "region_reality"
        else:
            claim = synthesize_claim(contract)
            mode = "page_state"

        # Put the region in frame first, and keep the receipt: `targetVisible`
        # is the only mechanical answer to "was it in the picture?".
        scroll_result = (
            await _scroll_region_into_view(agent, page_id, region, step)
            if region else {}
        )
        coverage = scroll_coverage(scroll_result)
        # An element-bound crop is self-evidencing; without a locator the
        # full-page shot is the widest honest coverage available.
        capture_request: JsonDict = {
            "pageId": page_id,
            "mode": mode,
            "question": claim,
            "_force": True,
        }
        if region.get("id"):
            capture_request["id"] = region["id"]
        elif region.get("selector"):
            capture_request["selector"] = region["selector"]
        else:
            capture_request["fullPage"] = True
        verdict = await _bt()._visual_verify(agent, capture_request, step)
        if not isinstance(verdict, dict) or verdict.get("status") in {
            "disabled",
            "failed",
            "rejected",
        }:
            # Do NOT consume the per-worker budget on a failed capture — the
            # streak stays armed so a later shortfall can retry. But count the
            # failures: after REALITY_CHECK_CAPTURE_FAILURE_LIMIT the gate above
            # stops arming, because a capture that cannot land will not start
            # landing on the fifty-third try.
            failures = getattr(agent, "reality_check_capture_failures", 0) + 1
            agent.reality_check_capture_failures = failures
            logger = getattr(agent, "logger", None)
            if logger is not None and hasattr(logger, "write"):
                # Without this event the run log cannot distinguish "the check
                # never armed" from "the check armed and was blind" — and the
                # second is a far more serious statement about the run. It is
                # what actually happened in d32a810d, where the log showed
                # nothing at all.
                logger.write("vl.reality_check.capture_unavailable", {
                    "triggerTool": name,
                    "armedBy": armed_by,
                    "pageId": page_id,
                    "captureScope": (
                        "element" if (region.get("id") or region.get("selector"))
                        else "fullPage"
                    ),
                    "status": str(
                        (verdict or {}).get("status") or "no_verdict"
                    ) if isinstance(verdict, dict) else "no_verdict",
                    "error": str(
                        (verdict or {}).get("error") or ""
                    )[:300] if isinstance(verdict, dict) else "",
                    "consecutiveFailures": failures,
                    "armingDisabled": (
                        failures >= REALITY_CHECK_CAPTURE_FAILURE_LIMIT
                    ),
                })
            return result
        # A capture landed: the worker's perception is working, so an earlier
        # transient failure must not count toward the circuit breaker.
        agent.reality_check_capture_failures = 0
        capture = region_in_capture(
            region_declared=bool(region.get("id") or region.get("selector")),
            screenshot_scope=str(verdict.get("screenshotScope") or ""),
            coverage=coverage,
        )
        reconciled = reconcile_region_verdict(
            verdict.get("classification") or verdict.get("verdict"), capture,
        )
        grading = evidence_grade(
            evidence_mode=getattr(
                vl_config, "reality_check_evidence_mode", "advisory",
            ),
            resolved_class=reconciled.get("class"),
            capture=capture,
        )
        # The class taxonomy only exists in region_reality mode. On the
        # page-scoped fallback the verdict is free-form, so no class is
        # asserted and the worker does its own comparing.
        row_reconciled = reconciled if mode == "region_reality" else None
        row_grading = grading if mode == "region_reality" else None
        row = build_reality_check_row(
            claim=claim,
            verdict=verdict,
            trigger_tool=name,
            shortfall_streak=getattr(agent, "target_shortfall_streak", 0),
            armed_by=armed_by,
            stall_turns=artifact_stall_turns(agent),
            page_id=page_id,
            page_url=page_url,
            row_key=row_key,
            region=region,
            capture=capture,
            coverage=coverage,
            reconciled=row_reconciled,
            grading=row_grading,
        )
        record = _bt()._record_extraction(agent, {
            "name": "vl_reality_check",
            "rows": [row],
            "schema": {"source": "vl_reality_check"},
            "description": (
                "Automatic visual reality check triggered by a"
                " target-shortfall perception streak"
            ),
        })
        # The check ran: consume the budget either way. Re-arming on a
        # persist failure would burn an unbounded _force VL call per further
        # shortfall while the worker never sees the verdict.
        agent.reality_check_count = getattr(agent, "reality_check_count", 0) + 1
        if not str(record.get("savedPath") or "").strip():
            # VL succeeded but the evidence did not persist: hand the verdict
            # to the worker anyway (the observation is still real) and tell
            # it to persist its own copy — the layer-3 pass and the B3 gate
            # need a ledger entry to verify.
            agent.target_shortfall_streak = 0
            logger = getattr(agent, "logger", None)
            if logger is not None and hasattr(logger, "write"):
                logger.write("vl.reality_check.persist_failed", {
                    "triggerTool": name,
                    "recordStatus": str(record.get("status") or ""),
                })
            out = {**result, "realityCheck": {
                **_reality_check_summary(row),
                "evidencePersisted": False,
            }}
            out["next_instruction"] = (
                "A visual reality check ran but its evidence artifact failed"
                " to persist. The observation above is still valid: persist"
                " it yourself via record_extraction and cite that savedPath"
                " in evidenceArtifacts before declaring"
                " target_absent/instruction_infeasible. "
            ) + _reality_check_instruction(
                reconciled=row_reconciled,
                grading=row_grading,
                capture=capture,
                evidence_path="",
            )
            return out
        reality: JsonDict = {
            **_reality_check_summary(row),
            "evidenceSavedPath": str(record.get("savedPath") or ""),
        }
        logger = getattr(agent, "logger", None)
        if logger is not None and hasattr(logger, "write"):
            logger.write("vl.reality_check", {**reality, "triggerTool": name})
        agent.target_shortfall_streak = 0
        out = {**result, "realityCheck": reality}
        out["next_instruction"] = _reality_check_instruction(
            reconciled=row_reconciled,
            grading=row_grading,
            capture=capture,
            evidence_path=reality["evidenceSavedPath"],
        )
        return out
    except Exception as exc:  # reality check must never break the call path
        logger = getattr(agent, "logger", None)
        if logger is not None and hasattr(logger, "write"):
            logger.write("vl.reality_check.error", {"error": str(exc)[:300]})
        return result

async def _promote_visual_locate(
    agent: Any,
    page_id: str,
    image_path: str,
    verdict: JsonDict,
    step: int,
    *,
    expected_text: str = "",
) -> JsonDict:
    """Reverse-look-up the VL `point` to a canonical AXTree id via bbox containment
    (the AXTree bbox space == screenshot px space). Attaches `resolvedId` (durable)
    or `cssPoint` (coords fallback for a genuine AXTree blind spot). Best-effort."""
    try:
        from harness.vl.locate import (
            _screenshot_dims,
            apply_promotion_guard,
            promote_locate,
        )

        shot_w, shot_h = await _screenshot_dims(image_path)
        ax = await _bt()._invoke_browser_method(
            agent, "DOM.getAXTree",
            {"pageId": page_id, "purpose": "promote the VL pixel to a canonical id"},
            step,
        )
        lines = (_bt()._response_data(ax) or {}).get("lines") or []
        # Avoid hidden Runtime.evaluate probes. AXTree rectangles and the
        # standard screenshot path use the same CSS-pixel coordinate contract;
        # promotion is guarded by label/role matching before any action.
        dpr = 1.0
        promo = promote_locate(lines, verdict["point"], shot_w=shot_w, shot_h=shot_h, dpr=dpr)
        promo = apply_promotion_guard(
            promo, vl_label=verdict.get("control_label"),
            expected_text=expected_text, dpr=dpr,
            logger=getattr(agent, "logger", None),
            page_id=page_id,
        )
        out = {**verdict, "promotion": promo}
        if promo.get("resolved"):
            out["resolvedId"] = promo.get("id")
            out["resolvedLabel"] = promo.get("label")
            out["next_instruction"] = (
                f"VL located the target and it was promoted to durable id"
                f" {promo.get('id')!r}. Act on that id (Input.click/DOM.getText with"
                f" id), NOT raw coordinates."
            )
        elif promo.get("promotionGuard"):
            out["cssPoint"] = promo.get("cssPoint")
            out["next_instruction"] = (
                "VL located the target but the bbox promotion failed a sanity"
                f" check ({promo['promotionGuard'].get('reason')}) and was demoted."
                " If safe and not consequential, use a single coordinate action at"
                " cssPoint; coordinates must never be persisted into a skill."
            )
        else:
            out["cssPoint"] = promo.get("cssPoint")
            out["next_instruction"] = (
                "VL located the target but no AXTree node covers it (blind spot)."
                " If safe and not consequential, use a single coordinate action at"
                " cssPoint; coordinates must never be persisted into a skill."
            )
        return out
    except Exception as exc:  # promotion is best-effort; keep the raw verdict
        return {**verdict, "promotion_error": str(exc)}

def _screenshot_saved_path(result: JsonDict) -> Optional[str]:
    data = _bt()._response_data(result)
    if not data:
        data = _bt()._raw_response_data(result)
    for key in ("savedPath", "path", "filePath"):
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            return value
    if str(data.get("encoding") or "").lower() == "file":
        value = data.get("data")
        if isinstance(value, str) and value.strip():
            return value
    response = result.get("response") if isinstance(result, dict) else None
    if isinstance(response, dict):
        data = response.get("data")
        if isinstance(data, dict):
            for key in ("savedPath", "path", "filePath"):
                value = data.get(key)
                if isinstance(value, str) and value.strip():
                    return value
            if str(data.get("encoding") or "").lower() == "file":
                value = data.get("data")
                if isinstance(value, str) and value.strip():
                    return value
    return None
