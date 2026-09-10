"""
harness.tools.browser_tools.visual - visual_verify, VL arbitration, reality checks and repair evidence.
"""

from typing import Any
from typing import Dict
from typing import List
from typing import Optional
from typing import Tuple
from typing import Set
import json
from urllib.parse import urlparse
from harness.results.call_outcome import domain_state_read_succeeded
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
    retry: Optional[JsonDict] = None,
) -> None:
    """Record the ladder's result AND what the retry measured.

    The retry is the only direct check that the target became reachable, and
    for a long time its outcome existed only in the tool's return value. Task
    b9a91fd2 logged six `dismissed` verdicts across two runs with nothing to
    contradict them, while the traces showed the retried click coming back
    with the same `-32005 target is covered` as the original. Whether a
    dismissal actually worked has to be answerable from the log alone.
    """
    logger = getattr(agent, "logger", None)
    if logger is None:
        return
    payload: JsonDict = {
        "pageId": page_id,
        "status": status,
        "subtype": (overlay or {}).get("subtype"),
        "attemptCount": len(attempts),
        "attempts": attempts,
    }
    if isinstance(retry, dict):
        for key in ("retried", "stillOccluded", "retryTarget", "reason"):
            if key in retry:
                payload[key] = retry[key]
    logger.write("dismiss_overlay.result", payload)

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
    page_id = str(tool_input.get("pageId") or "").strip()
    if not page_id:
        return {"status": "failed", "error": "pageId is required"}
    if (
        str(tool_input.get("mode") or "").strip() == "visual_locate"
        and not getattr(vl_config, "visual_locate_enabled", False)
    ):
        # Refuse before spending a screenshot and a VL request. The role used to
        # run anyway with the promotion skipped, which returned a normalized
        # 0-1000 grounding point to a model that had no way to know it was not a
        # coordinate. A locate that cannot be translated is worse than none.
        return {
            "status": "disabled",
            "reason": "vl.visual_locate_enabled is false",
            "next_instruction": (
                "Visual locate is turned off for this deployment. Re-observe"
                " with DOM.getAXTree / DOM.getSemanticTree and act on a"
                " canonical id."
            ),
        }
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
    # There is no separate per-worker visual budget (removed 2026-09-01). Every
    # visual_verify call already costs one of the worker's steps, so
    # `worker_max_steps` bounds visual spend the same way it bounds every other
    # tool; the old `max_checks_per_worker=2` was a second, far tighter ceiling
    # stacked on top of it, and it refused the third call of a task that
    # legitimately needed to locate a control and then verify the outcome.
    # `vl_check_count` survives as an observability counter — it is reported
    # back so the model can see its own spend — and `_force` still marks a call
    # the harness required rather than one the model chose.
    force_check = bool(tool_input.get("_force", False)) or bool(repair_targets)
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
    # Pair the capture with the earliest post-capture scroll observation when a
    # promotion will follow. AXTree bboxes are `(viewport + root scroll) ×
    # scale`, so containment needs a stable scroll offset across the model call
    # and AXTree read. This is best-effort rather than atomic with the image.
    promotion_wanted = mode == "visual_locate"
    before_artifacts = set(str(path) for path in getattr(agent, "artifacts", []))
    screenshot, capture_scroll = await _capture_bracketed(
        agent, page_id, screenshot_params, step, bracket=promotion_wanted
    )
    image_path = _bt()._screenshot_saved_path(screenshot)
    if not image_path:
        after_artifacts = [
            str(path) for path in getattr(agent, "artifacts", [])
            if str(path) not in before_artifacts
        ]
        image_path = after_artifacts[-1] if after_artifacts else ""
    if not image_path and (selector or element_id or full_page):
        # Agent guide §5: if element capture fails, do not repeat it — resync
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
        # A second capture needs its OWN bracket. Carrying the first one's
        # forward would attach an offset that was never measured around this
        # image, and the element capture that just failed may well have
        # scrolled the page on its way there.
        screenshot, capture_scroll = await _capture_bracketed(
            agent, page_id, fallback_params, step, bracket=promotion_wanted
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
    # so the agent acts on a stable handle instead of raw coordinates. Promotion
    # is best-effort, but its FAILURE MODE IS NOT: no exit of this path hands
    # back the raw normalized point. A promotion error refuses the coordinate
    # (`coordinateRefused: promotion_error`) and a non-located verdict is
    # scrubbed below, because a 0-1000 grounding point has repeatedly read to a
    # model as something clickable.
    if mode == "visual_locate" and isinstance(verdict, dict):
        if verdict.get("verdict") == "located" and verdict.get("point"):
            verdict = await _promote_visual_locate(
                agent, page_id, image_path, verdict, step,
                expected_text=" ".join(
                    part for part in (question, str(expected.get("target") or ""))
                    if part
                ),
                # The receipt carries the CSS-pixel size that proves the capture's
                # scale, and the scope says whether its origin is the viewport's.
                screenshot=screenshot,
                screenshot_scope=screenshot_scope,
                capture_scroll=capture_scroll,
            )
        else:
            # `not_found` / `uncertain`, or `located` with no usable point. The
            # normalized point is scrubbed on this path too: there is no exit
            # from a locate that hands the model a raw grounding coordinate.
            verdict = _locate_model_view(verdict)
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


# Which failure classes are beyond visual recovery lives in harness.vl.arbiter,
# the module that used to auto-invoke a VL recovery from this hot path. Its
# classification is the one reusable piece of that design and is imported rather
# than restated, so the taxonomy has a single home.


def _visual_hint_ineligible_reason(
    result: JsonDict,
    params: JsonDict,
    vl_config: Any,
    method: str = "",
) -> str:
    """Why this failure gets no visual hint, or "" when it should get one."""
    if vl_config is None or not getattr(vl_config, "enabled", False):
        return "vl_disabled"
    # Advertising a path that answers `disabled` wastes a model step.
    if not getattr(vl_config, "visual_locate_enabled", False):
        return "visual_locate_disabled"
    # Use the harness's own action-failure predicate rather than scraping a
    # string out of the receipt. A browser-side action reports through
    # `response.error` OR `response.data.error`, a guard refusal through
    # `tool_was_executed=False`, and a public-envelope failure only through
    # `rpcData` + `errorClassification` — top-level `error` is set on transport
    # exceptions alone. A prose extractor saw the first of those and missed the
    # rest, silently withholding the hint from most real failures.
    #
    # `_invoke_result_failed` is the right one of the tree's two predicates
    # here: it answers "did the action achieve its page effect", which is what a
    # recovery ladder asks. Its documented hazard — that nothing which GRANTS
    # state may use it — does not apply: this attaches advisory text and grants
    # nothing.
    if not _bt()._invoke_result_failed(result):
        return "no_failure"
    # ...but that predicate also fails on `response.data.error`, and for a
    # DOMAIN-ERROR READ that field describes the page, not this call:
    # Page.getState carries the page's own last-navigation error there, which
    # never clears on a risk-controlled page. A hint reading it as failure would
    # fire on every successful read of such a page, teaching the model that a
    # clean read is a failure. Task 48b4d7d7 is the 84-minute version of that
    # mistake.
    #
    # Scoped to the declared read set rather than applied to every method,
    # because a bare `data.error` IS a real failure for an action —
    # Runtime.evaluate reports exactly that way (see
    # runtime_eval._runtime_evaluation_error_text). The two other sites that met
    # this ambiguity (bindings._observe_fleet_reperception, workflow_auth_fence)
    # could use the call verdict unconditionally because their context was
    # already a state read; this entry point sees actions and reads alike, so
    # the same shortcut here would silently swallow real action failures.
    if domain_state_read_succeeded(result, method):
        return "call_succeeded"
    # A page-facing failure is one the caller aimed at a page. This is the whole
    # method filter: Memory/Fleet/File failures carry no pageId and drop out
    # here without a list of method names that would rot as the catalog grows.
    if not str((params or {}).get("pageId") or "").strip():
        return "no_page_id"
    # Do not recommend a recovery whose own first step is the call that just
    # failed. `visual_verify` opens with Page.screenshot, so hinting it at a
    # failed capture proposes a loop. This is a self-reference check, not a
    # method allowlist: every other method stays eligible, including ones this
    # catalog does not have yet.
    if str(method or "") == "Page.screenshot":
        return "hint_depends_on_the_failed_capability"
    from harness.vl.arbiter import visual_recovery_ineligible_reason

    classification = result.get("errorClassification")
    ctype = (
        classification.get("type")
        if isinstance(classification, dict) else ""
    )
    # The code travels with the type. `select_failure` covers twelve codes
    # whose answers are not all on screen, and the type alone cannot tell them
    # apart; every other class ignores the second argument.
    code = (
        classification.get("errorCode")
        if isinstance(classification, dict) else ""
    )
    return visual_recovery_ineligible_reason(ctype, code)


def _attach_visual_recovery_hint(
    agent: Any,
    method: str,
    params: JsonDict,
    result: JsonDict,
    step: int,
) -> JsonDict:
    """Tell the model that a visual locate exists, once deterministic recovery
    has had its turn and the call still failed.

    This makes NO VL request, reads no page, and executes nothing: it is a
    structured note appended to a failure receipt. The decision to spend a
    visual call, and the decision to act on whatever it returns, both belong to
    the model. The harness's job here is to make sure the option is known —
    a capability the agent is never told about is a capability it never uses.

    The hint goes in its OWN field rather than into `next_instruction`, which
    already carries the deterministic recovery advice for this failure. That
    advice comes first; this is what to try when it has been exhausted.
    """
    if not isinstance(result, dict) or "visualRecoveryHint" in result:
        return result
    vl_config = getattr(
        getattr(getattr(agent, "runtime", None), "harness", None), "vl", None
    )
    reason = _visual_hint_ineligible_reason(result, params or {}, vl_config, method)
    if reason:
        return result
    page_id = str((params or {}).get("pageId") or "").strip()
    hint: JsonDict = {
        "available": True,
        "when": (
            "Use this after the deterministic recovery this receipt already"
            " describes has been tried and the target is still unreachable,"
            " AND you have reason to believe the target is visible on screen"
            " while the structured surfaces (AXTree / SemanticTree) cannot"
            " name it. It is not a shortcut past re-observing the page."
        ),
        "call": {
            "tool": "visual_verify",
            "arguments": {
                "pageId": page_id,
                "selector": "",
                "id": "",
                "fullPage": False,
                "mode": "visual_locate",
                "question": "<what the target looks like and where you expect it>",
                "expected": {"target": "<the control you are trying to reach>"},
            },
        },
        "resultPolicy": {
            "resolvedId": (
                "The pixel was promoted to a canonical id: a handle on the AX"
                " node that covered it, which survives a relayout that a"
                " coordinate does not. It is NOT a permit for any Action."
                " What it proves is where the node is, not what the node is,"
                " so choose the method by what the node actually is and let"
                " the live schema accept the id: a button takes Input.click,"
                " a text field takes Input.type, a scrollable ancestor takes"
                " Input.scroll, and Input.select takes the CONTROL — never an"
                " option id, which is what a visual locate on an open menu"
                " most often returns. Re-observe afterwards either way."
            ),
            "cssPoint": (
                "No node covers the pixel (a genuine structured-surface blind"
                " spot), but the capture's scale and origin were proven. This"
                " is a viewport CSS point, and a point is only clickable:"
                " ONE Page.click{pageId,x,y}, then re-observe to verify the"
                " outcome. That is a limit of what a coordinate CAN express,"
                " not a rule about visual recovery — a resolvedId reaches"
                " whichever methods that node's own role supports. Never"
                " persist a coordinate into a skill or reuse it after the"
                " page changes."
            ),
            "coordinateRefused": (
                "The geometry could not be proven, so no point is offered."
                " Re-observe with DOM.getAXTree / DOM.getSemanticTree. Do not"
                " invent a coordinate and do not reuse a point from an earlier"
                " call."
            ),
        },
        "boundary": (
            "Locating a control visually changes what you can REACH, never what"
            " you are allowed to DO. A target you may not act on by canonical"
            " id is equally off-limits by coordinate."
        ),
    }
    logger = getattr(agent, "logger", None)
    if logger is not None and hasattr(logger, "write"):
        logger.write("vl.visual_recovery_hint", {
            "method": method,
            "pageId": page_id,
            "step": step,
            "errorClassification": (
                result.get("errorClassification", {}).get("type")
                if isinstance(result.get("errorClassification"), dict) else None
            ),
        })
    return {**result, "visualRecoveryHint": hint}

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
    # `claimScope` says what was being judged; `screenshotScope` says what was
    # actually photographed. Dropping the second one let a viewport crop be
    # read as a statement about a whole page. The row has carried it since
    # build_reality_check_row; only this whitelist withheld it.
    for key in ("rowKey", "verdictClass", "claimedClass", "overrideReason",
                "regionInCapture", "itemCount", "armedBy", "screenshotScope",
                "turnsSinceArtifactProgress"):
        if key in row:
            summary[key] = row[key]
    return summary

def _capture_scope_caveat(screenshot_scope: str) -> str:
    """State what the capture did NOT cover, for either claim scope.

    A viewport capture is bounded by the window; everything above or below the
    fold is unobserved, not absent. The distinction matters more here than
    elsewhere because this check is once per worker (`reality_check_count >= 1`
    gates re-arming, and the counter is consumed on every verdict including an
    uncertain one) — so a worker that reads a partial frame as a whole-page
    fact has no second reading to correct it.
    """
    scope = str(screenshot_scope or "").strip()
    if scope not in {"viewport", "viewport_fallback"}:
        return ""
    return (
        "SCOPE: this verdict was reached from a VIEWPORT capture, so content"
        " above or below the current fold was never photographed — it is"
        " unobserved, not absent. Do not generalize it to the whole page or to"
        " a region you have not scrolled into view. This worker's automatic"
        " reality check is now spent; if you need a wider or different frame,"
        " scroll or bind a locator and call visual_verify yourself. "
    )


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
    screenshot_scope: str = "",
) -> str:
    """What the worker should do with this verdict, given its standing.

    Deliberately asymmetric. "There is content here" always redirects work and
    is stated as an instruction. Everything else is reported as an observation
    that does not close anything, because an advisory model claim that ends a
    row is the failure this whole path exists to prevent.

    The scope caveat wraps every branch rather than the page-scoped one alone:
    a row-scoped verdict read off a viewport crop is exactly as partial, and
    `CLASS_EXPLICIT_EMPTY` in particular is the branch where "I did not see it"
    is most likely to be mistaken for "it is not there".
    """
    return _capture_scope_caveat(screenshot_scope) + _reality_check_verdict_body(
        reconciled=reconciled,
        grading=grading,
        capture=capture,
        evidence_path=evidence_path,
    )


def _reality_check_verdict_body(
    *,
    reconciled: Optional[JsonDict],
    grading: Optional[JsonDict],
    capture: JsonDict,
    evidence_path: str,
) -> str:
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
            " behind it and not about any other item. If a current browser"
            " action is occluded, the runtime overlay adjudicator determines"
            " whether a safe dismissal or HITL is appropriate; do not infer"
            " either one from this region verdict alone. Re-observe before"
            f" recording a blocker, citing {citation}."
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
            # A capture whose height is the DOCUMENT's has no upper bound. In
            # task fae5a7b6 this line produced 2448x77912 on a Taobao detail
            # page: a structurally perfect PNG (every chunk CRC verified,
            # 12.79 MB of IDAT) that the endpoint refused as an illegal image,
            # four times across three workers. Two more captures on the same
            # path were refused locally at 48.9 MB. Scaled to model input a
            # strip that tall is unreadable even when accepted, so the widest
            # capture was never the most honest one -- it was an unbounded bet.
            # Viewport is bounded by the window; the caller is told the frame
            # was partial via `screenshotScope`, and an explicit full-page
            # capture is still available to anyone who asks for one.
            capture_request["fullPage"] = False
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
                    # Two facts, because they can disagree. `requestedScope`
                    # is what this call asked for; `effectiveScope` is what the
                    # capture actually produced, and the screenshot's own
                    # report wins when it has one -- an element capture that
                    # fell back to the viewport is not an element failure. The
                    # single hardcoded field these replace said "fullPage" for
                    # every locator-less check, so after the default moved to
                    # viewport it would have counted viewport timeouts as
                    # full-page failures and hidden whether the change worked.
                    "requestedScope": (
                        "element" if (region.get("id") or region.get("selector"))
                        else ("fullPage" if capture_request.get("fullPage")
                              else "viewport")
                    ),
                    "effectiveScope": (
                        str((verdict or {}).get("screenshotScope") or "")
                        if isinstance(verdict, dict) else ""
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
                screenshot_scope=str(row.get("screenshotScope") or ""),
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
            screenshot_scope=str(row.get("screenshotScope") or ""),
        )
        return out
    except Exception as exc:  # reality check must never break the call path
        logger = getattr(agent, "logger", None)
        if logger is not None and hasattr(logger, "write"):
            logger.write("vl.reality_check.error", {"error": str(exc)[:300]})
        return result

async def _read_page_scroll(
    agent: Any, page_id: str, step: int
) -> Optional[Dict[str, float]]:
    """The document scroll offset, or None when it cannot be read.

    `Page.wheel` with a zero delta is the platform's own root-viewport state
    read: measured to answer `completedReason: "state-read"` with
    `observedDelta: {x: 0, y: 0}` and the current `position`, leaving the page
    untouched. Using a scroll action to read the scroll is only defensible
    because of that receipt, so this refuses any answer that does not carry it —
    a read that moved the page is the one thing the surrounding bracket exists
    to detect, and it would be detecting its own instrument.

    This used to send `Input.scroll` in viewport mode. That mode no longer
    exists: the Action now requires `target` or `container` on every branch, so
    the call failed `invalid-params` and this returned None for every capture,
    silently disabling cssPoint promotion. `Page.wheel` is where root-viewport
    scrolling went, and `x`/`y` must be inside the viewport, so the capture's
    own origin is used rather than a fixed guess.

    The alternative, a Semantic Tree read, keeps the instrument independent but
    costs an entire document to obtain two numbers, twice per promotion. This
    matches `harness/vl/capture_geometry.py`, which already reasons from scroll
    receipts, so the two paths agree about what a scroll receipt means.
    """
    from harness.vl.locate import _scroll_position_from_state_read

    try:
        resp = await _bt()._invoke_browser_method(
            agent, "Page.wheel",
            {"pageId": page_id, "x": 0, "y": 0, "scrollX": 0, "scrollY": 0,
             "purpose": "read the scroll offset for VL coordinate mapping"},
            step,
            internal=True,
        )
    except Exception:
        return None
    data = _bt()._response_data(resp) or {}
    return _scroll_position_from_state_read(data)


async def _capture_bracketed(
    agent: Any,
    page_id: str,
    params: JsonDict,
    step: int,
    *,
    bracket: bool,
) -> Tuple[JsonDict, Optional[Dict[str, float]]]:
    """Take one screenshot together with the page's scroll offset as it stood.

    The read happens immediately AFTER the capture, and is one end of a
    stability check the promotion closes after reading the AXTree. It does not
    make the screenshot and geometry atomic: movement between image capture and
    this first read remains unobservable. Two reasons it is not taken
    before: an element capture scrolls its target into view first, so a prior
    read describes a position the image never had; and the window that actually
    needs guarding is the whole span from image to bboxes, since the AXTree is
    read at promotion time, after the model call.

    The capture and its offset come back together, so a later capture cannot
    inherit an earlier one's — the viewport fallback is a second, separate
    image, and the element capture that just failed may have scrolled the page
    on its way there.

    `bracket=False` skips the read for callers that will not promote.
    """
    shot = await _bt()._invoke_browser_method(
        agent, "Page.screenshot", params, step
    )
    if isinstance(shot, dict) and bracket:
        shot["captureObservation"] = {
            "step": step,
            "axtreeEpoch": getattr(agent, "axtree_epoch", None),
            "axtreePageId": getattr(agent, "axtree_page_id", None),
        }
    at_capture = (
        await _read_page_scroll(agent, page_id, step) if bracket else None
    )
    return shot, at_capture


# A VL grounding answer is a point in the 0-1000 normalized space the model
# was never told about, and a promotion receipt additionally carries a
# screenshot-device-pixel `pxPoint`. Neither is a coordinate `Page.click`
# accepts — CSS viewport pixels are — and both have looked, to a model, exactly
# like something to click. Every one of them stays in the log and none reaches
# the model; the only coordinate ever offered is `cssPoint`, and only once the
# capture's scale and origin have been proven.
_LOCATE_PRIVATE_VERDICT_KEYS = ("point",)
_LOCATE_PRIVATE_PROMOTION_KEYS = ("pxPoint",)


def _locate_model_view(verdict: JsonDict) -> JsonDict:
    return {
        key: value for key, value in verdict.items()
        if key not in _LOCATE_PRIVATE_VERDICT_KEYS
    }


def _promotion_model_view(promo: Any) -> Any:
    if not isinstance(promo, dict):
        return promo
    return {
        key: value for key, value in promo.items()
        if key not in _LOCATE_PRIVATE_PROMOTION_KEYS
    }


def _promotion_model_view_without_identity(promo: Any) -> Any:
    """Project a visual promotion without re-advertising a disputed AX id."""
    projected = _promotion_model_view(promo)
    if not isinstance(projected, dict):
        return projected
    return {
        key: value for key, value in projected.items()
        if key not in {"id", "label", "bbox"}
    }


_CONSEQUENTIAL_NOTE = (
    "This target reads as a consequential action (submit / pay / delete /"
    " sign-in class). A coordinate click cannot prove what it landed on before"
    " it lands. Locating it visually does not authorize performing it: follow"
    " the same rule you would follow with a canonical id, and hand login,"
    " payment, deletion and other irreversible account or funds operations to"
    " the user."
)


def _locate_consequential(verdict: Any, *labels: Any) -> Optional[JsonDict]:
    """Mark a located target that reads as a consequential action.

    TWO independent sources, because they fail in opposite directions. The VL
    is asked directly (`is_consequential`, see harness.vl.core) and sees an
    icon-only trash button or a localized label no keyword table lists; the
    keyword table catches a target the VL waved through. Either is enough — the
    field's job is to make the model look before it acts, and a false positive
    costs a moment's attention while a false negative costs the click.

    This ANNOTATES; it does not withhold. The coordinate is still offered,
    because the harness executes nothing here: `Input.click` is a separate call
    the model has to choose to make, and neither a keyword table nor an L4
    visual assertion is a sound basis for the harness to overrule that choice.
    What the model gets is the fact, stated where the decision is made.
    """
    from harness.observation.overlay_actions import is_sensitive_target

    if isinstance(verdict, dict) and verdict.get("is_consequential"):
        return {
            "source": "vl_assessment",
            "matchedLabel": str(verdict.get("control_label") or "")[:200],
            "note": _CONSEQUENTIAL_NOTE,
        }
    for label in labels:
        text = str(label or "").strip()
        if text and is_sensitive_target("", text):
            return {
                "source": "label_keyword",
                "matchedLabel": text[:200],
                "note": _CONSEQUENTIAL_NOTE,
            }
    return None


async def _promote_visual_locate(
    agent: Any,
    page_id: str,
    image_path: str,
    verdict: JsonDict,
    step: int,
    *,
    expected_text: str = "",
    screenshot: Optional[JsonDict] = None,
    screenshot_scope: str = "",
    capture_scroll: Optional[Dict[str, float]] = None,
) -> JsonDict:
    """Reverse-look-up the VL `point` to a canonical AXTree id via bbox containment
    (the AXTree bbox space == screenshot px space). Attaches `resolvedId` (durable)
    or `cssPoint` (coords fallback for a genuine AXTree blind spot). Best-effort."""
    try:
        from harness.vl.locate import (
            _screenshot_dims,
            apply_promotion_guard,
            capture_origin,
            promote_locate,
            screenshot_dpr,
            scroll_bracket,
        )

        shot_w, shot_h = await _screenshot_dims(image_path)
        ax = await _bt()._invoke_browser_method(
            agent, "DOM.getAXTree",
            {"pageId": page_id, "purpose": "promote the VL pixel to a canonical id"},
            step,
        )
        lines = (_bt()._response_data(ax) or {}).get("lines") or []
        # The far end of the stability check. Agreement proves no scrolling
        # from the first post-capture read through this post-AXTree read;
        # disagreement withholds promotion. The earlier image-to-first-read
        # gap remains a platform-level atomicity limitation.
        scroll = scroll_bracket(
            capture_scroll,
            await _read_page_scroll(agent, page_id, step),
        )
        # No hidden Runtime.evaluate probe is needed: the encoded PNG dimensions
        # divided by Page.screenshot's CSS dimensions measure the scale.
        # scaleFactor corroborates that measurement; disagreement withholds the
        # coordinate because the platform may silently default that field to 1.
        shot_data = _bt()._response_data(screenshot or {}) or {}
        dpr_receipt = screenshot_dpr(
            png_width=shot_w,
            png_height=shot_h,
            reported_width=shot_data.get("width"),
            reported_height=shot_data.get("height"),
            scale_factor=shot_data.get("scaleFactor"),
        )
        # The same receipt also proves WHERE the crop started. An element
        # capture carries the target's Semantic Tree and a region capture echoes
        # its requested x/y, so a cropped capture no longer has to be refused —
        # its point is translated into viewport space instead.
        origin_receipt = capture_origin(
            scope=screenshot_scope, shot_data=shot_data,
        )
        if not origin_receipt.get("scrollProven"):
            # Containment needs the scroll offset, and an element capture's own
            # Semantic Tree only states it when that tree is rooted at the
            # document — for a deeply nested target it is truncated to `body`,
            # which is not the scrolling element and reports a genuine 0.
            origin_receipt = capture_origin(
                scope=screenshot_scope, shot_data=shot_data, scroll=scroll,
            )
        promo = promote_locate(
            lines, verdict["point"], shot_w=shot_w, shot_h=shot_h,
            dpr_receipt=dpr_receipt, scope=screenshot_scope,
            origin_receipt=origin_receipt,
        )
        promo = apply_promotion_guard(
            promo, vl_label=verdict.get("control_label"),
            expected_text=expected_text,
            dpr_receipt=dpr_receipt, scope=screenshot_scope,
            origin_receipt=origin_receipt,
            logger=getattr(agent, "logger", None),
            page_id=page_id,
        )
        identity_recovery: Optional[JsonDict] = None
        if promo.get("resolved") and str(promo.get("id") or ""):
            recovery_lookup = getattr(
                _bt(), "_select_identity_recovery_for_locators", None,
            )
            if callable(recovery_lookup):
                identity_recovery = recovery_lookup(
                    agent, page_id, frozenset({str(promo["id"])}),
                )
        logger = getattr(agent, "logger", None)
        if logger is not None and hasattr(logger, "write"):
            # The full record, private coordinates included, so a bad locate can
            # be diagnosed afterwards from the log rather than from the model's
            # transcript.
            logger.write("vl.locate.promotion", {
                "pageId": page_id,
                "scope": screenshot_scope,
                "normalizedPoint": verdict.get("point"),
                "promotion": promo,
            })
        out = _locate_model_view(verdict)
        out["visualTargetEvidence"] = {
            "captureObservation": (screenshot or {}).get("captureObservation"),
            "promotionObservation": {
                "axtreeEpoch": getattr(agent, "axtree_epoch", None),
                "axtreePageId": getattr(agent, "axtree_page_id", None),
            },
            "axMatch": bool(promo.get("resolved")),
            "interactabilityAtClick": "not_verified",
            "stateContinuity": "not_verified",
            "note": "AX epochs identify observations, not atomic screenshot/click state. "
                    "Scroll agreement does not prove a popup remained open or an inner "
                    "scroll ancestor stayed unchanged.",
        }
        out["promotion"] = (
            _promotion_model_view_without_identity(promo)
            if identity_recovery is not None
            else _promotion_model_view(promo)
        )
        consequential = _locate_consequential(
            verdict, verdict.get("control_label"), expected_text,
        )
        if consequential is not None:
            out["consequential"] = consequential
        if promo.get("resolved") and identity_recovery is not None:
            # A bbox proves where the pixels landed, but cannot prove that the
            # native select path can resolve this identity now.  Do not route a
            # visual escape straight back to an id whose same control has
            # already exhausted the observed native recovery pass.
            from harness.vl.locate import _coordinate_fallback

            point = promo.get("pxPoint") if isinstance(promo.get("pxPoint"), dict) else {}
            coordinate = _coordinate_fallback(
                float(point.get("x") or 0.0),
                float(point.get("y") or 0.0),
                reason="select_identity_repeated",
                dpr_receipt=promo.get("dpr") if isinstance(promo.get("dpr"), dict) else dpr_receipt,
                scope=screenshot_scope,
                origin_receipt=promo.get("origin") if isinstance(promo.get("origin"), dict) else origin_receipt,
            )
            out["selectIdentityConflict"] = identity_recovery
            out["dpr"] = coordinate.get("dpr")
            out["origin"] = coordinate.get("origin")
            if isinstance(coordinate.get("cssPoint"), dict):
                out["cssPoint"] = coordinate["cssPoint"]
                out["next_instruction"] = (
                    "Visual location matched a control whose native select"
                    " identity repeatedly failed after re-observation. The"
                    " match does not prove that id is usable, so it is not"
                    " returned. Prefer a separately verified selector and the"
                    " ordinary UI ladder. If no such target is available, use"
                    " this cssPoint only as ONE Page.click after current evidence"
                    " supports that the target is still usable, then re-observe"
                    " and verify the resulting menu or value."
                )
            else:
                out["coordinateRefused"] = coordinate.get("coordinateRefused")
                out["next_instruction"] = (
                    "Visual location matched a control whose native select"
                    " identity repeatedly failed after re-observation. The"
                    " match does not prove that id is usable, so it is not"
                    " returned; and this capture cannot prove a coordinate."
                    " Continue from fresh structured evidence with the"
                    " ordinary UI ladder; do not invent a point."
                )
        elif promo.get("resolved"):
            out["resolvedId"] = promo.get("id")
            out["resolvedLabel"] = promo.get("label")
            out["next_instruction"] = (
                f"Located and promoted to durable id {promo.get('id')!r}. Act on"
                f" that id (Input.click / DOM.getText with id) — it survives a"
                f" relayout that a coordinate does not. No coordinate is needed"
                f" or offered here."
            )
        elif promo.get("coordinateRefused"):
            # No durable id AND no provable pixel-to-CSS mapping. A coordinate
            # offered here would land on some other real element and report
            # success, so it is withheld entirely.
            out["dpr"] = promo.get("dpr")
            out["origin"] = promo.get("origin")
            out["coordinateRefused"] = promo.get("coordinateRefused")
            out["next_instruction"] = (
                "The target was located visually, but it could not be promoted"
                " to a durable id and the capture's scale/origin could not be"
                f" proven ({promo.get('coordinateRefused')}), so NO coordinate"
                " is offered. Re-observe with DOM.getAXTree or"
                " DOM.getSemanticTree and act on an id. Do not invent a point"
                " and do not reuse one from an earlier call."
            )
        else:
            out["cssPoint"] = promo.get("cssPoint")
            out["dpr"] = promo.get("dpr")
            demoted = promo.get("promotionGuard")
            out["next_instruction"] = (
                (
                    "The target was located, but the bbox promotion failed a"
                    f" sanity check ({demoted.get('reason')}) and was demoted."
                    if demoted else
                    "The target was located in the screenshot but the later AX"
                    " observation has no matching bbox. This can be a structured"
                    " blind spot or a change of state between observations."
                )
                + " cssPoint is a VIEWPORT CSS point, which is the space"
                " Page.click takes. After checking current evidence for whether"
                " the target is still usable, it may support at most ONE"
                " Page.click{pageId,x,y}; then re-observe to verify the outcome."
                " It is valid for this"
                " page state only — never persist a coordinate into a skill and"
                " never reuse it after the page changes."
                + (
                    " This target reads as consequential; see `consequential`"
                    " before acting." if consequential is not None else ""
                )
            )
        if isinstance(out.get("cssPoint"), dict):
            out["next_instruction"] += (
                " Coordinate mapping is proven, not target interactability or hit identity. "
                "If current evidence shows the related menu closed, the target hidden, or "
                "another element receiving the point, restore/re-observe the target before "
                "deciding an action. Do not infer that mounted option nodes are an open menu. "
                "Consider a verified keyboard target; read back the selected value afterwards."
            )
        return out
    except Exception as exc:
        # Promotion is best-effort, but a failed promotion must not degrade into
        # handing back the raw normalized point: that point is unusable as a
        # coordinate and has repeatedly read as one.
        logger = getattr(agent, "logger", None)
        if logger is not None and hasattr(logger, "write"):
            logger.write("vl.locate.promotion_error", {
                "pageId": page_id,
                "error": str(exc),
                "normalizedPoint": verdict.get("point"),
            })
        out = _locate_model_view(verdict)
        out["promotion_error"] = str(exc)
        out["coordinateRefused"] = "promotion_error"
        out["next_instruction"] = (
            "The target was located visually but the geometry translation"
            f" failed ({exc}), so no coordinate is offered. Re-observe with"
            " DOM.getAXTree or DOM.getSemanticTree and act on an id."
        )
        return out

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
