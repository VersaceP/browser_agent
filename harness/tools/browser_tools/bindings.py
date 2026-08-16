"""
harness.tools.browser_tools.bindings - Fleet/page binding guards and the fleet auth barrier.
"""

import copy
from typing import Any
from typing import List
from typing import Optional
from typing import Tuple
from harness.results.call_outcome import classify_call_outcome
from harness.utils import JsonDict

def _bt():
    import harness.tools.browser_tools as bt

    return bt

def _apply_fleet_binding(
    agent: Any,
    method: str,
    params: JsonDict,
) -> Tuple[Optional[JsonDict], JsonDict]:
    """Enforce the coordinator-issued fleet binding on model-initiated calls.

    Internal harness plumbing does not pass through this function.  This makes
    Fleet.create coordinator-owned while still allowing the fast path and other
    deterministic harness code to use its explicit assignment.
    """

    if not _fleet_reuse_enabled(agent):
        return None, {}

    assigned_fleet_id = str(
        getattr(agent, "assigned_fleet_id", "") or ""
    ).strip()
    allowed = {
        str(item).strip()
        for item in (getattr(agent, "allowed_fleet_ids", set()) or set())
        if str(item).strip()
    }
    if assigned_fleet_id:
        allowed.add(assigned_fleet_id)
    assignment_reason = str(
        getattr(agent, "fleet_assignment_reason", "") or ""
    ).strip()

    receipt = {
        "assignedFleetId": assigned_fleet_id,
        "assignmentReason": assignment_reason,
        "fleetInjected": False,
    }
    pinned_page_id = str(
        getattr(agent, "pinned_page_id", "") or ""
    ).strip()
    if pinned_page_id and method == "Page.create":
        return {
            "status": "pinned_browser_context_violation",
            "error": (
                "Page.create cannot replace the user-pinned existing page"
                f" {pinned_page_id!r}."
            ),
            "assignedFleetId": assigned_fleet_id,
            "pinnedPageId": pinned_page_id,
            "tool_was_executed": False,
            "next_instruction": (
                "Use the pinned pageId from slot_context. If that page is no"
                " longer usable, report pinned_page_unavailable to LeadAgent;"
                " do not create a substitute page."
            ),
        }, receipt
    if (
        pinned_page_id
        and method == "Page.close"
        and str(params.get("pageId") or "").strip() == pinned_page_id
    ):
        return {
            "status": "pinned_browser_context_violation",
            "error": (
                "Page.close cannot close the user-pinned existing page"
                f" {pinned_page_id!r}."
            ),
            "assignedFleetId": assigned_fleet_id,
            "pinnedPageId": pinned_page_id,
            "tool_was_executed": False,
            "next_instruction": (
                "Leave the pinned page open and continue on that page, or"
                " report pinned_page_unavailable if it cannot be used."
            ),
        }, receipt
    if method == "Fleet.create":
        return {
            "status": "fleet_create_coordinator_owned",
            "error": (
                "Fleet.create is coordinator-owned while fleet reuse is enabled;"
                " the worker must create pages inside its assigned fleet."
            ),
            "assignedFleetId": assigned_fleet_id,
            "assignmentReason": assignment_reason,
            "tool_was_executed": False,
            "next_instruction": (
                "Call Page.create with the assignedFleetId. If true session"
                " isolation is required, declare needs_isolated_session before"
                " spawning the worker."
            ),
        }, receipt
    if method == "Fleet.close":
        return {
            "status": "fleet_close_coordinator_owned",
            "error": (
                "Fleet.close is disabled for workers while fleet reuse is"
                " enabled because close clears ownership and makes the fleet"
                " claimable by another agent that knows its fleetId."
            ),
            "assignedFleetId": assigned_fleet_id,
            "assignmentReason": assignment_reason,
            "tool_was_executed": False,
            "next_instruction": (
                "Close task pages with Page.close when appropriate. Fleet"
                " ownership transfer and retention are Dispatcher lifecycle"
                " responsibilities."
            ),
        }, receipt

    requested_fleet_id = str(params.get("fleetId") or "").strip()
    if requested_fleet_id:
        if not assigned_fleet_id:
            return {
                "status": "fleet_assignment_required",
                "error": "No coordinator fleet assignment is attached to this worker.",
                "requestedFleetId": requested_fleet_id,
                "tool_was_executed": False,
            }, receipt
        if requested_fleet_id not in allowed:
            return {
                "status": "fleet_binding_violation",
                "error": (
                    f"fleetId {requested_fleet_id!r} is outside this worker's"
                    " coordinator-issued binding."
                ),
                "assignedFleetId": assigned_fleet_id,
                "allowedFleetIds": sorted(allowed),
                "tool_was_executed": False,
                "next_instruction": (
                    "Use assignedFleetId from slot_context; never fabricate or"
                    " substitute fleet identifiers."
                ),
            }, receipt
    elif method in {"Page.create", "Page.list"}:
        if not assigned_fleet_id:
            return {
                "status": "fleet_assignment_required",
                "error": (
                    f"{method} requires a coordinator-issued fleetId;"
                    " fleetless Dispatcher selection is intentionally disabled."
                ),
                "tool_was_executed": False,
            }, receipt
        params["fleetId"] = assigned_fleet_id
        receipt["fleetInjected"] = True

    if method not in {"Page.create", "Page.list"} and not method.startswith("Fleet."):
        return None, {}
    return None, receipt

def _fleet_reuse_enabled(agent: Any) -> bool:
    runtime = getattr(agent, "runtime", None)
    harness_config = getattr(runtime, "harness", None)
    if harness_config is None or not hasattr(harness_config, "fleet_reuse_enabled"):
        # Compatibility for direct helper users and lightweight test doubles.
        # Real RuntimeConfig always carries the explicit flag (default: true).
        return False
    return bool(getattr(harness_config, "fleet_reuse_enabled", True))

def _check_page_binding(
    agent: Any,
    method: str,
    params: JsonDict,
) -> Optional[JsonDict]:
    """Reject model-visible page handles outside the worker delegation."""

    if not _fleet_reuse_enabled(agent) or not isinstance(params, dict):
        return None
    # Page.list stays readable for every assignment: a worker that cannot see
    # the Fleet cannot tell "my action did nothing" from "my result opened in a
    # tab I am not allowed to look at". Visibility and usability are separate
    # concerns — the pageId binding check below still governs what may be
    # operated, and _filter_page_list_response marks which rows are delegated.
    page_id = str(params.get("pageId") or "").strip()
    if not page_id:
        return None
    allowed_pages = {
        str(item).strip()
        for item in (getattr(agent, "allowed_page_ids", set()) or set())
        if str(item).strip()
    }
    page_fleets = getattr(agent, "page_fleet_ids", None)
    page_fleets = page_fleets if isinstance(page_fleets, dict) else {}
    allowed_fleets = {
        str(item).strip()
        for item in (getattr(agent, "allowed_fleet_ids", set()) or set())
        if str(item).strip()
    }
    assigned_fleet = str(getattr(agent, "assigned_fleet_id", "") or "").strip()
    if assigned_fleet:
        allowed_fleets.add(assigned_fleet)
    page_fleet = str(page_fleets.get(page_id) or "").strip()
    if page_id in allowed_pages and not (
        page_fleet and page_fleet not in allowed_fleets
    ):
        return None
    if _page_is_claimable(agent, page_id):
        # This is admission only. The shared PageLeaseManager performs the
        # authoritative atomic claim immediately before transport dispatch;
        # mutating allowed_page_ids here would recreate a check-then-act race.
        return None
    manager = getattr(agent, "page_lease_manager", None)
    worker_id = str(getattr(agent, "worker_id", "") or "").strip()
    owner = (
        str(manager.owner_for(page_id) or "")
        if manager is not None and hasattr(manager, "owner_for")
        else ""
    )
    quarantined = bool(
        manager is not None
        and hasattr(manager, "page_is_quarantined")
        and manager.page_is_quarantined(page_id)
    )
    return {
        "status": (
            "page_quarantined"
            if quarantined
            else "page_busy"
            if owner and owner != worker_id
            else "page_binding_violation"
        ),
        "error": (
            f"pageId {page_id!r} is outside this worker's Fleet or is held by"
            " another worker."
        ),
        "pageId": page_id,
        "pageFleetId": page_fleet,
        "assignedFleetId": assigned_fleet,
        "ownerWorkerId": owner or None,
        "quarantined": quarantined,
        "tool_was_executed": False,
        "next_instruction": (
            "Call Page.list to see this Fleet's pages; rows with"
            " claimable=true can be used directly. Quarantined rows must not"
            " be used. Otherwise create your own"
            " page with Page.create."
        ),
    }

def _page_is_claimable(agent: Any, page_id: str) -> bool:
    """Whether an undelegated page may be taken over on first use.

    Two conditions, both plain facts rather than inferences: the page belongs
    to a Fleet this worker was assigned, and no other live worker holds it.
    Cross-worker interference is the risk worth guarding; a stray site popup is
    only a wasted step the model corrects on its own.
    """

    if not page_id:
        return False
    fleet_pages = getattr(agent, "fleet_page_fleet_ids", None)
    if not isinstance(fleet_pages, dict):
        return False
    page_fleet = str(fleet_pages.get(page_id) or "").strip()
    if not page_fleet:
        return False
    allowed_fleets = {
        str(item).strip()
        for item in (getattr(agent, "allowed_fleet_ids", set()) or set())
        if str(item).strip()
    }
    assigned = str(getattr(agent, "assigned_fleet_id", "") or "").strip()
    if assigned:
        allowed_fleets.add(assigned)
    if page_fleet not in allowed_fleets:
        return False
    manager = getattr(agent, "page_lease_manager", None)
    worker_id = str(getattr(agent, "worker_id", "") or "").strip()
    if manager is not None and hasattr(manager, "owner_for"):
        if (
            hasattr(manager, "page_is_quarantined")
            and manager.page_is_quarantined(page_id)
        ):
            return False
        owner = str(manager.owner_for(page_id) or "")
        return not owner or owner == worker_id
    # Lightweight helper users have no concurrent worker runtime. Production
    # workers always receive the shared manager from BrowserAgentSpawner.
    return True

def _observe_page_binding_after(
    agent: Any,
    method: str,
    params: JsonDict,
    result: JsonDict,
) -> None:
    """Register only pages proven to belong to the assigned fleet."""

    if not _fleet_reuse_enabled(agent) or not isinstance(result, dict):
        return
    response = result.get("response")
    if result.get("error") or (
        isinstance(response, dict) and response.get("error")
    ):
        return
    allowed_pages = getattr(agent, "allowed_page_ids", None)
    if not isinstance(allowed_pages, set):
        allowed_pages = set()
        agent.allowed_page_ids = allowed_pages
    page_fleets = getattr(agent, "page_fleet_ids", None)
    if not isinstance(page_fleets, dict):
        page_fleets = {}
        agent.page_fleet_ids = page_fleets
    allowed_fleets = {
        str(item).strip()
        for item in (getattr(agent, "allowed_fleet_ids", set()) or set())
        if str(item).strip()
    }
    assigned_fleet = str(getattr(agent, "assigned_fleet_id", "") or "").strip()
    if assigned_fleet:
        allowed_fleets.add(assigned_fleet)

    addressed_page_id = str(params.get("pageId") or "").strip()
    manager = getattr(agent, "page_lease_manager", None)
    worker_id = str(getattr(agent, "worker_id", "") or "").strip()
    if (
        addressed_page_id
        and method != "Page.close"
        and manager is not None
        and hasattr(manager, "owner_for")
        and str(manager.owner_for(addressed_page_id) or "") == worker_id
    ):
        fleet_id = str(
            getattr(agent, "fleet_page_fleet_ids", {}).get(addressed_page_id)
            or assigned_fleet
            or ""
        ).strip()
        if fleet_id in allowed_fleets:
            allowed_pages.add(addressed_page_id)
            page_fleets[addressed_page_id] = fleet_id

    if method in {"Page.create", "Page.list"}:
        inherited_fleet = str(params.get("fleetId") or assigned_fleet).strip()
        for page in _bt()._pages_from_value(result):
            page_id = str(page.get("pageId") or page.get("page_id") or "").strip()
            row_fleet_id = str(
                page.get("fleetId") or page.get("fleet_id") or ""
            ).strip()
            fleet_id = (
                row_fleet_id
                if method == "Page.list"
                else row_fleet_id or inherited_fleet
            )
            if method == "Page.list" and page_id not in allowed_pages:
                continue
            if page_id and fleet_id in allowed_fleets:
                allowed_pages.add(page_id)
                page_fleets[page_id] = fleet_id
    elif method == "Page.close":
        page_id = str(params.get("pageId") or "").strip()
        if page_id:
            allowed_pages.discard(page_id)
            page_fleets.pop(page_id, None)
            fleet_pages = getattr(agent, "fleet_page_fleet_ids", None)
            if isinstance(fleet_pages, dict):
                fleet_pages.pop(page_id, None)

def _shown_page_inventory_rows(value: Any) -> List[JsonDict]:
    """Return the page identities actually present in a Page.list response.

    This is a harness-private evidence sidecar, not an authorization decision.
    Keep it available even when Fleet reuse is disabled: in that mode the raw
    response is shown unchanged, so those rows still count as pages the model
    has seen and may discharge the inventory-change notification.
    """
    rows: List[JsonDict] = []
    seen = set()
    for page in _bt()._pages_from_value(value):
        page_id = str(page.get("pageId") or page.get("page_id") or "").strip()
        fleet_id = str(
            page.get("fleetId") or page.get("fleet_id") or ""
        ).strip()
        key = (fleet_id, page_id)
        if not page_id or not fleet_id or key in seen:
            continue
        seen.add(key)
        rows.append({"pageId": page_id, "fleetId": fleet_id})
    return rows

def _filter_page_list_response(
    agent: Any,
    response: Any,
) -> Tuple[Any, JsonDict]:
    """Annotate Page.list rows with delegation and claimability.

    Hiding non-delegated rows made a worker unable to observe that its own
    submit had opened a result tab, which reads as "the action did nothing" and
    drives pointless retries. Every row in the assigned Fleet is therefore
    returned, tagged ``delegated`` (already this worker's) and ``claimable``
    (usable on first touch because no other worker holds it).

    This listing is also what teaches the binding guard which pages exist and
    where: a page can only be claimed after the worker has seen it here, which
    keeps discovery an explicit model act rather than an inference.

    Rows outside the assigned Fleet remain hidden — that is a tenancy boundary,
    not a usability one.
    """

    if not _fleet_reuse_enabled(agent):
        return response, {
            "_shownInventoryPages": _shown_page_inventory_rows(response),
        }
    allowed_pages = {
        str(item).strip()
        for item in (getattr(agent, "allowed_page_ids", set()) or set())
        if str(item).strip()
    }
    allowed_fleets = {
        str(item).strip()
        for item in (getattr(agent, "allowed_fleet_ids", set()) or set())
        if str(item).strip()
    }
    assigned_fleet = str(getattr(agent, "assigned_fleet_id", "") or "").strip()
    if assigned_fleet:
        allowed_fleets.add(assigned_fleet)
    manager = getattr(agent, "page_lease_manager", None)
    worker_id = str(getattr(agent, "worker_id", "") or "").strip()
    shown_inventory_pages: List[JsonDict] = []
    # Remember where each visible page lives so the binding guard can decide
    # claimability later without re-listing.
    fleet_pages = getattr(agent, "fleet_page_fleet_ids", None)
    if not isinstance(fleet_pages, dict):
        fleet_pages = {}
        agent.fleet_page_fleet_ids = fleet_pages
    hidden_count = 0
    delegated_count = 0
    claimable_count = 0
    held_count = 0
    quarantined_count = 0

    def filtered(value: Any) -> Any:
        nonlocal hidden_count, delegated_count, claimable_count
        nonlocal held_count, quarantined_count
        if isinstance(value, list):
            is_page_list = any(
                isinstance(item, dict)
                and bool(str(item.get("pageId") or item.get("page_id") or "").strip())
                for item in value
            )
            if is_page_list:
                kept: List[Any] = []
                for item in value:
                    if not isinstance(item, dict):
                        continue
                    page_id = str(
                        item.get("pageId") or item.get("page_id") or ""
                    ).strip()
                    row_fleet = str(
                        item.get("fleetId") or item.get("fleet_id") or ""
                    ).strip()
                    if not page_id or not row_fleet:
                        hidden_count += 1
                        continue
                    if allowed_fleets and row_fleet not in allowed_fleets:
                        hidden_count += 1
                        continue
                    fleet_pages[page_id] = row_fleet
                    if manager is not None and hasattr(manager, "observe_inventory"):
                        manager.observe_inventory(
                            row_fleet,
                            [page_id],
                        )
                    shown_inventory_pages.append({
                        "pageId": page_id,
                        "fleetId": row_fleet,
                    })
                    owner = (
                        str(manager.owner_for(page_id) or "")
                        if manager is not None and hasattr(manager, "owner_for")
                        else ""
                    )
                    delegated = page_id in allowed_pages or bool(
                        worker_id and owner == worker_id
                    )
                    quarantined = bool(
                        manager is not None
                        and hasattr(manager, "page_is_quarantined")
                        and manager.page_is_quarantined(page_id)
                    )
                    if manager is not None and hasattr(manager, "owner_for"):
                        claimable = not delegated and not owner and not quarantined
                    else:
                        claimable = not delegated and not quarantined
                    row = filtered(item)
                    if isinstance(row, dict):
                        row["delegated"] = delegated
                        row["claimable"] = claimable
                        row["leasedByMe"] = bool(worker_id and owner == worker_id)
                        row["busy"] = bool(owner and owner != worker_id)
                        row["quarantined"] = quarantined
                    if quarantined:
                        quarantined_count += 1
                    elif delegated:
                        delegated_count += 1
                    elif claimable:
                        claimable_count += 1
                    else:
                        held_count += 1
                    kept.append(row)
                return kept
            return [filtered(item) for item in value]
        if isinstance(value, dict):
            return {key: filtered(item) for key, item in value.items()}
        return value

    sanitized = filtered(copy.deepcopy(response))
    receipt: JsonDict = {
        "pageListFiltered": True,
        "delegatedPageCount": delegated_count,
        "claimablePageCount": claimable_count,
        "heldByOtherWorkerCount": held_count,
        "quarantinedPageCount": quarantined_count,
        "hiddenPageCount": hidden_count,
        # Harness-private sidecar. The caller removes it before merging the
        # public receipt and defers discharge until all result post-processing
        # has completed successfully.
        "_shownInventoryPages": shown_inventory_pages,
    }
    if claimable_count:
        receipt["next_instruction"] = (
            "Rows with claimable=true are free: address one by its pageId and"
            " it becomes yours on first use. If an action of yours looked like"
            " it did nothing, its result most likely rendered in one of these"
            " tabs rather than in your current page. Rows with busy=true are"
            " held by another worker; rows with quarantined=true are unusable."
        )
    return sanitized, receipt

_REPERCEPTION_ALLOWED_METHODS = {
    "Page.getState",
    "DOM.getAXTree",
    "Hitl.requestPause",
}

async def _fleet_auth_barrier_before_call(
    agent: Any,
    method: str,
    params: JsonDict,
    *,
    emit_workflow_telemetry: bool = False,
) -> Optional[JsonDict]:
    barrier = getattr(agent, "fleet_auth_barrier", None)
    fleet_id = str(getattr(agent, "assigned_fleet_id", "") or "").strip()
    worker_id = str(getattr(agent, "worker_id", "") or "").strip()
    if barrier is None or not fleet_id or not worker_id:
        return None
    if method == "Workflow.execute":
        receipt = await barrier.workflow_fence_before(
            fleet_id,
            worker_id,
            seen_generation=int(
                getattr(agent, "fleet_barrier_generation", 0) or 0
            ),
        )
        if emit_workflow_telemetry:
            payload = {
                **dict(receipt),
                "source": "raw_workflow",
                "method": method,
                "runId": None,
                "workerId": worker_id,
            }
            logger = getattr(agent, "logger", None)
            if logger is not None and hasattr(logger, "write"):
                logger.write("workflow.auth_fence.before", payload)
                if (
                    not receipt.get("allowed")
                    and receipt.get("generationChanged")
                ):
                    logger.write(
                        "workflow.auth_generation_changed",
                        payload,
                    )
        if receipt.get("allowed"):
            return None
        return {
            **receipt,
            "next_instruction": (
                "Do not start or trust an opaque workflow while shared"
                " authentication is changing. After the barrier opens, call"
                " Page.getState and DOM.getAXTree, then retry the same row."
            ),
        }
    receipt = await barrier.before_call(
        fleet_id,
        worker_id,
        seen_generation=int(
            getattr(agent, "fleet_barrier_generation", 0) or 0
        ),
    )
    if not receipt.get("allowed"):
        if receipt.get("resolverRequired") and method in {
            "Page.getState",
            "Page.create",
            "DOM.getAXTree",
            "Hitl.requestPause",
        }:
            # An ownerless but still-closed gate permits page-scoped diagnosis.
            # Page.create and Hitl.requestPause proceed to explicit atomic
            # claims below; Page.list remains delegation-scoped and no
            # arbitrary business call becomes resolver.
            return None
        return receipt
    if receipt.get("generationChanged"):
        generation = int(receipt.get("generation") or 0)
        # Latch one target generation. ``seen_generation`` intentionally stays
        # unchanged until both observations complete, so before_call will keep
        # reporting generationChanged in the meantime.  Resetting the flags on
        # every such call makes Page.getState and DOM.getAXTree erase each
        # other's progress forever.
        if (
            not getattr(agent, "fleet_reperception_pending", False)
            or int(
                getattr(agent, "fleet_reperception_generation", -1) or -1
            )
            != generation
        ):
            agent.fleet_reperception_generation = generation
            agent.fleet_reperception_pending = True
            agent.fleet_reperception_state_seen = False
            agent.fleet_reperception_tree_seen = False
            agent.axtree_invalidated = True
    if not getattr(agent, "fleet_reperception_pending", False):
        return None
    if method not in _REPERCEPTION_ALLOWED_METHODS:
        return {
            "status": "fleet_reperception_required",
            "reasonKind": "fleet_reperception_required",
            "fleetId": fleet_id,
            "generation": receipt.get("generation"),
            "tool_was_executed": False,
            "retryable": True,
            "next_instruction": (
                "The shared authentication state changed. Call Page.getState"
                " and then DOM.getAXTree for this page before any other action."
            ),
        }
    return None

def _workflow_auth_started_generation(agent: Any, method: str) -> Optional[int]:
    if method != "Workflow.execute":
        return None
    barrier = getattr(agent, "fleet_auth_barrier", None)
    fleet_id = str(getattr(agent, "assigned_fleet_id", "") or "").strip()
    worker_id = str(getattr(agent, "worker_id", "") or "").strip()
    if barrier is None or not fleet_id or not worker_id:
        return None
    return int(barrier.generation(fleet_id))

async def _quarantine_workflow_result_after_auth_change(
    agent: Any,
    method: str,
    result: JsonDict,
    *,
    started_generation: Optional[int],
    emit_telemetry: bool,
) -> JsonDict:
    if method != "Workflow.execute" or started_generation is None:
        return result
    barrier = getattr(agent, "fleet_auth_barrier", None)
    fleet_id = str(getattr(agent, "assigned_fleet_id", "") or "").strip()
    if barrier is None or not fleet_id:
        return result
    receipt = await barrier.workflow_fence_after(
        fleet_id,
        started_generation=int(started_generation),
    )
    if receipt.get("valid"):
        return result
    logger = getattr(agent, "logger", None)
    payload = {
        **receipt,
        "source": "raw_workflow",
        "method": method,
        "runId": None,
        "workerId": str(getattr(agent, "worker_id", "") or ""),
    }
    if emit_telemetry and logger is not None and hasattr(logger, "write"):
        if receipt.get("generationChanged"):
            logger.write("workflow.auth_generation_changed", payload)
        logger.write("workflow.row_quarantined", payload)
    return {
        "method": method,
        "params": result.get("params") if isinstance(result, dict) else {},
        "status": "workflow_row_quarantined",
        "error": (
            "Workflow result was isolated because the shared authentication"
            " barrier or generation changed while it was in flight."
        ),
        "authFence": receipt,
        "tool_was_executed": True,
        "retryable": True,
        "next_instruction": (
            "Call Page.getState and DOM.getAXTree after the barrier opens, then"
            " retry only this row. Do not persist variables from this run."
        ),
    }

def _fleet_auth_barrier_after_call(
    agent: Any,
    method: str,
    result: JsonDict,
) -> None:
    if not getattr(agent, "fleet_reperception_pending", False):
        return
    # The exit condition is "this worker re-read the page", which is a fact
    # about the CALL, not about the page's health. `_invoke_result_failed`
    # answers a different question: it also fails on `response.data.error`,
    # which for Page.getState is the page's own last-navigation error. On a
    # risk-controlled page that field is permanent, so the gate whose exit
    # requires reading the page could never be opened by reading the page.
    if not classify_call_outcome(result).succeeded:
        return
    if method == "Page.getState":
        agent.fleet_reperception_state_seen = True
    elif method == "DOM.getAXTree":
        agent.fleet_reperception_tree_seen = True
    if not (
        getattr(agent, "fleet_reperception_state_seen", False)
        and getattr(agent, "fleet_reperception_tree_seen", False)
    ):
        return
    generation = int(
        getattr(agent, "fleet_reperception_generation", 0) or 0
    )
    agent.fleet_barrier_generation = generation
    agent.fleet_reperception_pending = False

async def _claim_fleet_auth_barrier_for_hitl(
    agent: Any,
    method: str,
    params: JsonDict,
) -> Optional[JsonDict]:
    """Admit, then atomically select the one worker allowed to enter HITL.

    Every `Hitl.requestPause` — the model's own call and the harness's
    auto-adjudicated one — passes through here, so this is where attendance and
    the cumulative pause budget are ENFORCED and ACCOUNTED. Putting them only in
    the auto path left the manual call as a hole wide enough to drive the whole
    mechanism through: a model handed an `hitl_unattended` verdict could reach
    the same 900-second wait by calling the tool itself.
    """

    if method != "Hitl.requestPause":
        return None
    page_id = str(params.get("pageId") or "").strip()
    admission = _bt()._hitl_admission(agent, page_id)
    if admission is not None:
        return await _bt()._refuse_hitl(agent, admission, page_id, method)
    barrier = getattr(agent, "fleet_auth_barrier", None)
    fleet_id = str(getattr(agent, "assigned_fleet_id", "") or "").strip()
    worker_id = str(getattr(agent, "worker_id", "") or "").strip()
    if barrier is None or not fleet_id or not worker_id:
        _bt()._count_hitl_pause_round(agent, page_id)
        return None
    claim = await barrier.claim(
        fleet_id,
        worker_id,
        str(params.get("reason") or params.get("purpose") or "manual HITL"),
    )
    if claim.get("claimed"):
        # Counted only once the pause is actually going to be dispatched: a
        # worker turned away at the gate never bothered a human.
        _bt()._count_hitl_pause_round(agent, page_id)
        return None
    return {
        "status": "fleet_auth_gated",
        "reasonKind": "fleet_auth_gated",
        "fleetId": fleet_id,
        "resolverWorkerId": claim.get("resolverWorkerId"),
        "generation": claim.get("generation"),
        "tool_was_executed": False,
        "retryable": True,
        "next_instruction": (
            "Another worker owns authentication recovery for this fleet. "
            "Do not request HITL or act on the shared cookie jar until it finishes."
        ),
    }

async def _claim_ownerless_fleet_auth_barrier_for_page_create(
    agent: Any,
    method: str,
    params: JsonDict,
) -> Tuple[Optional[JsonDict], bool]:
    """Select one resolver before Page.create crosses an ownerless gate.

    Returns ``(guard, takeover_claimed)``. Open fleets do not need a claim;
    an existing resolver may continue; a competing worker remains gated.
    """

    if method != "Page.create":
        return None, False
    barrier = getattr(agent, "fleet_auth_barrier", None)
    fleet_id = str(getattr(agent, "assigned_fleet_id", "") or "").strip()
    worker_id = str(getattr(agent, "worker_id", "") or "").strip()
    if barrier is None or not fleet_id or not worker_id:
        return None, False
    claim = await barrier.claim_ownerless(
        fleet_id,
        worker_id,
        "Create or recover a page for ownerless authentication recovery",
    )
    if not claim.get("required"):
        return None, False
    if claim.get("claimed"):
        takeover = bool(claim.get("takeover"))
        if takeover:
            logger = getattr(agent, "logger", None)
            if logger is not None:
                logger.write(
                    "auth_fleet.resolver_claimed_for_page_create",
                    {
                        "fleetId": fleet_id,
                        "workerId": worker_id,
                        "generation": claim.get("generation"),
                    },
                )
        return None, takeover
    return {
        "status": "fleet_auth_gated",
        "reasonKind": "fleet_auth_gated",
        "fleetId": fleet_id,
        "resolverWorkerId": claim.get("resolverWorkerId"),
        "generation": claim.get("generation"),
        "tool_was_executed": False,
        "retryable": True,
        "next_instruction": (
            "Another worker atomically claimed ownerless authentication recovery. "
            "Do not create a page or act on the shared cookie jar until it finishes."
        ),
    }, False

async def _relinquish_fleet_auth_resolver_after_failed_pause(
    agent: Any,
    method: str,
    *,
    pause_succeeded: bool,
) -> JsonDict:
    if method != "Hitl.requestPause" or pause_succeeded:
        return {}
    barrier = getattr(agent, "fleet_auth_barrier", None)
    fleet_id = str(getattr(agent, "assigned_fleet_id", "") or "").strip()
    worker_id = str(getattr(agent, "worker_id", "") or "").strip()
    if barrier is None or not fleet_id or not worker_id:
        return {}
    receipt = await barrier.relinquish(
        fleet_id,
        worker_id,
        reason="Hitl.requestPause failed before the human wait began",
    )
    if receipt.get("relinquished"):
        logger = getattr(agent, "logger", None)
        if logger is not None:
            logger.write(
                "auth_fleet.resolver_relinquished",
                {"fleetId": fleet_id, "workerId": worker_id, **receipt},
            )
    return receipt

async def _relinquish_fleet_auth_resolver_after_failed_recovery_page_create(
    agent: Any,
    method: str,
    *,
    takeover_claimed: bool,
    call_succeeded: bool,
) -> JsonDict:
    """Release a failed Page.create takeover without opening the gate."""

    if method != "Page.create" or not takeover_claimed or call_succeeded:
        return {}
    barrier = getattr(agent, "fleet_auth_barrier", None)
    fleet_id = str(getattr(agent, "assigned_fleet_id", "") or "").strip()
    worker_id = str(getattr(agent, "worker_id", "") or "").strip()
    if barrier is None or not fleet_id or not worker_id:
        return {}
    receipt = await barrier.relinquish(
        fleet_id,
        worker_id,
        reason="Recovery Page.create failed before a challenge page was available",
    )
    if receipt.get("relinquished"):
        logger = getattr(agent, "logger", None)
        if logger is not None:
            logger.write(
                "auth_fleet.resolver_relinquished_after_page_create",
                {"fleetId": fleet_id, "workerId": worker_id, **receipt},
            )
    return receipt
