"""
harness.tools.browser_tools.downloads - Download.start reconciliation and receipt reuse.

Covers the rebuilt Download domain: ``Download.start`` is a union (direct
URL download, or a five-minute page-download reservation that returns with
``state="waiting"`` and no URL), ``Download.control`` replaces the old
pause/resume/cancel actions, and records expose ``requestedUrl``/``finalUrl``
instead of ``url``. Receipt identity is ``downloadId``-first; URL+savePath
remains the fallback for legacy envelopes, and pageId+savePath keys a
reservation that has not produced a download yet. ``waiting`` reservations
enter the ledger (they own a real five-minute side-effect window) but are
NEVER registered as file resources - no file exists yet.
"""

import asyncio
import hashlib
import re
import time
from typing import Any
from typing import Dict
from typing import List
from typing import Optional
from typing import Set
import json
from pathlib import Path
from abcp_client import ABCPTransportError
from harness.storage.base import normalize_external_path
from harness.utils import JsonDict
from harness.utils import storage_for_logger

def _bt():
    import harness.tools.browser_tools as bt

    return bt

# Lifecycle states of the rebuilt Download domain. Terminal states carry a
# failure reason in one of two shapes: flat errorCode/errorMessage (local
# platform source) or a structured failure block (online describe schema).
DOWNLOAD_ACTIVE_STATES = frozenset({"waiting", "downloading", "paused"})
DOWNLOAD_TERMINAL_STATES = frozenset({"completed", "failed", "cancelled"})
DOWNLOAD_FILE_REGISTRABLE_STATES = frozenset({"downloading", "paused", "completed"})

def _normalize_failure(raw: JsonDict) -> Dict[str, str]:
    """Accept both failure shapes: flat errorCode/errorMessage and a
    structured ``failure: {code, message, suggested_prompt}`` block."""
    code = str(raw.get("errorCode") or "").strip()
    message = str(raw.get("errorMessage") or "").strip()
    if not code:
        failure = raw.get("failure") if isinstance(raw.get("failure"), dict) else {}
        code = str(failure.get("code") or "").strip()
        if not message:
            message = str(failure.get("message") or "").strip()
    return {"errorCode": code, "errorMessage": message}

def _normalize_download_record(raw: Any) -> Optional[JsonDict]:
    """Normalize one DownloadRecord-shaped dict across schema generations.

    Keeps the receipt fields the rest of the harness already consumes
    (``url`` alias preserved), adds requestedUrl/finalUrl and the normalized
    failure fields, and returns None for values that identify no download.
    """
    if not isinstance(raw, dict):
        return None
    download_id = str(
        raw.get("downloadId") or raw.get("id") or ""
    ).strip()
    save_path = str(raw.get("savePath") or "").strip()
    state = str(raw.get("state") or "").strip()
    if not download_id and not save_path:
        return None
    requested_url = str(raw.get("requestedUrl") or "").strip()
    final_url = str(raw.get("finalUrl") or "").strip()
    legacy_url = str(raw.get("url") or "").strip()
    failure = _normalize_failure(raw)
    record: JsonDict = {
        "downloadId": download_id,
        "url": final_url or requested_url or legacy_url,
        "requestedUrl": requested_url or None,
        "finalUrl": final_url or None,
        "savePath": save_path,
        "state": state,
        "totalBytes": int(raw.get("totalBytes") or 0),
        "receivedBytes": int(raw.get("receivedBytes") or 0),
        "source": str(raw.get("source") or "Download.list"),
    }
    if failure["errorCode"] or failure["errorMessage"]:
        record.update(failure)
    for optional in ("expiresAt", "pageId", "sourceType"):
        value = raw.get(optional)
        if value is not None:
            record[optional] = value
    return record

def _download_operation_key(params: Any) -> str:
    """Primary identity for one download operation.

    downloadId is authoritative once known. A direct-URL start falls back to
    [url, savePath]; a page-download reservation (no URL by contract) falls
    back to [pageId, savePath] so two concurrent reservations for the same
    page path cannot silently alias different captures.
    """
    keys = _download_operation_keys(params)
    return keys[0] if keys else ""

def _download_operation_keys(params: Any) -> List[str]:
    """Every identity a receipt should be filed under.

    A receipt remembered from a Download.start response carries a downloadId,
    but a later identical retry arrives with only url+savePath (or only
    pageId+savePath for a reservation). Filing under ALL derivable keys lets
    every lookup path find the same receipt object instead of silently
    starting a second side-effecting operation:

    - downloadId (authoritative once known);
    - [requestedUrl, savePath], [finalUrl, savePath], and the legacy
      [url, savePath] - a redirected download must answer a retry that
      quotes EITHER the originally requested URL or the final one;
    - ["page", pageId, savePath] for page reservations.
    """
    if not isinstance(params, dict):
        return []
    keys: List[str] = []
    download_id = str(params.get("downloadId") or params.get("id") or "").strip()
    if download_id:
        keys.append(json.dumps(["downloadId", download_id], ensure_ascii=False))
    save_path = str(params.get("savePath") or "").strip()
    if save_path:
        urls: List[str] = []
        for source in ("url", "requestedUrl", "finalUrl"):
            value = str(params.get(source) or "").strip()
            if value and value not in urls:
                urls.append(value)
        for url in urls:
            key = json.dumps([url, save_path], ensure_ascii=False)
            if key not in keys:
                keys.append(key)
        page_id = str(params.get("pageId") or "").strip()
        if page_id:
            keys.append(json.dumps(["page", page_id, save_path], ensure_ascii=False))
    return keys

def _download_records(value: Any) -> List[JsonDict]:
    records: List[JsonDict] = []
    seen: Set[str] = set()

    def visit(item: Any, depth: int = 0) -> None:
        if depth > 6:
            return
        if isinstance(item, dict):
            # A record identifies a download via downloadId (new domain) or
            # via url+savePath (legacy envelopes); url may be absent for a
            # page reservation that has not attached yet.
            download_id = str(item.get("downloadId") or item.get("id") or "").strip()
            save_path = str(item.get("savePath") or "").strip()
            state = str(item.get("state") or "").strip()
            if (download_id or save_path) and state:
                dedupe = download_id or json.dumps(
                    [
                        str(item.get("requestedUrl") or item.get("url") or ""),
                        save_path,
                        state,
                        item.get("startedAt"),
                    ],
                    ensure_ascii=False,
                )
                if dedupe not in seen:
                    seen.add(dedupe)
                    records.append(dict(item))
            for nested in item.values():
                if isinstance(nested, (dict, list)):
                    visit(nested, depth + 1)
        elif isinstance(item, list):
            for nested in item:
                visit(nested, depth + 1)

    visit(value)
    return records

def _download_receipt_store(agent: Any) -> Dict[str, JsonDict]:
    store = getattr(agent, "download_operation_receipts", None)
    if not isinstance(store, dict):
        store = {}
        agent.download_operation_receipts = store
    return store

def _download_resource_registration_store(agent: Any) -> Dict[str, str]:
    """Per-run dedupe for download receipts already handed to Storage."""

    store = getattr(agent, "download_resource_registrations", None)
    if not isinstance(store, dict):
        store = {}
        agent.download_resource_registrations = store
    return store

def _register_download_resource(
    agent: Any,
    receipt: JsonDict,
    *,
    operation_key: str,
) -> Optional[JsonDict]:
    """Record one proven browser download without changing action semantics.

    Electron owns the bytes, so Storage keeps only the canonical path plus the
    receipt and the size/hash observable at registration time. Active receipts
    are useful even before the file becomes readable; a later Download.list
    call registers a new version when the state or file stat changes.

    A bookkeeping failure must not turn a browser-side success into a tool
    failure: retrying Download.start after its side effect already happened can
    create a duplicate download. Dual verification still exposes secondary
    write failures through its own writeErrors channel.

    ``waiting`` reservations are deliberately NOT registered: no file exists
    until the page actually initiates the transfer, and registering one would
    fabricate file evidence. Only states that imply a real file path (or a
    completed one) are registered.
    """

    state = str(receipt.get("state") or "").strip().lower()
    save_path = str(receipt.get("savePath") or "").strip()
    logger = getattr(agent, "logger", None)
    if state not in DOWNLOAD_FILE_REGISTRABLE_STATES or not save_path or logger is None:
        return None

    task_dir = Path(getattr(logger, "task_dir", "") or ".")
    normalized, unmanaged, resolved = normalize_external_path(task_dir, save_path)
    if unmanaged:
        basename = re.sub(r"[^A-Za-z0-9._-]+", "_", resolved.name).strip("._")
        basename = basename or "download"
        path_tag = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]
        logical_path = f"external/downloads/{path_tag}-{basename}"
    else:
        logical_path = normalized
    if not logical_path:
        return None

    try:
        stat = resolved.stat() if resolved.is_file() else None
    except OSError:
        stat = None
    signature = json.dumps(
        [
            str(receipt.get("downloadId") or ""),
            str(receipt.get("url") or ""),
            normalized,
            state,
            int(receipt.get("totalBytes") or 0),
            int(receipt.get("receivedBytes") or 0),
            int(stat.st_size) if stat is not None else None,
            int(stat.st_mtime_ns) if stat is not None else None,
        ],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    registration_key = operation_key or normalized
    registrations = _download_resource_registration_store(agent)
    if registrations.get(registration_key) == signature:
        return None

    try:
        storage, task_id = storage_for_logger(logger)
        stored = storage.save_resource(
            task_id=task_id,
            run_id=str(getattr(logger, "run_id", "") or ""),
            resource_type="download",
            logical_path=logical_path,
            external_path=str(resolved),
            metadata={
                "download": dict(receipt),
                "operationKey": operation_key,
                "external_unmanaged": unmanaged,
            },
        )
    except Exception as exc:  # noqa: BLE001 - never invite a duplicate side effect
        try:
            logger.write("storage.download_registration_failed", {
                "savePath": save_path,
                "state": state,
                "error": f"{type(exc).__name__}: {exc}",
            })
        except Exception:
            pass
        return None
    registrations[registration_key] = signature
    return stored

DOWNLOAD_TIMEOUT_RECONCILIATION_DELAY_SECONDS = 4.0

def _remember_download_record(agent: Any, record: JsonDict) -> JsonDict:
    """Merge one observed download record into the ledger.

    When the record carries a downloadId, an existing receipt filed under
    that id is UPDATED in place - never replaced - so every alias key
    (requestedUrl/finalUrl/page) keeps pointing at the same live object.
    Otherwise a fresh receipt is created. The receipt is then filed under
    every derivable identity (plus any explicit operationKey the caller
    passed), so a retry quoting the original request URL, the final URL, or
    just pageId+savePath all find it.
    """
    normalized = _normalize_download_record(record) or dict(record)
    explicit_key = str(record.get("operationKey") or "").strip()
    store = _download_receipt_store(agent)
    receipt: Optional[JsonDict] = None
    download_id = str(normalized.get("downloadId") or "").strip()
    if download_id:
        download_key = json.dumps(["downloadId", download_id], ensure_ascii=False)
        receipt = store.get(download_key)
    if not isinstance(receipt, dict):
        receipt = {}
    # Latest observation wins for non-empty values; keys the new record
    # lacks keep the previously observed value (aliases stay coherent).
    for key, value in normalized.items():
        if key == "operationKey":
            continue
        if value not in (None, "", 0) or key not in receipt:
            receipt[key] = value
    keys = _download_operation_keys(receipt)
    if explicit_key and explicit_key not in keys:
        keys.append(explicit_key)
    if keys:
        receipt["operationKey"] = explicit_key or str(
            receipt.get("operationKey") or ""
        ) or keys[0]
        for key in keys:
            store[key] = receipt
    # waiting/failed/cancelled enter the ledger only; file registration is
    # restricted to states with a real path (see _register_download_resource).
    _register_download_resource(
        agent, receipt, operation_key=keys[0] if keys else explicit_key
    )
    return receipt

def _remember_unverified_download_timeout(
    agent: Any,
    params: JsonDict,
    *,
    rpc_code: Optional[int],
) -> JsonDict:
    """Remember an uncertain side effect without laundering it as success."""
    keys = _download_operation_keys(params)
    receipt = {
        "downloadId": "",
        "url": str(params.get("url") or ""),
        "savePath": str(params.get("savePath") or ""),
        "pageId": str(params.get("pageId") or "") or None,
        "state": "timeout_unverified",
        "totalBytes": 0,
        "receivedBytes": 0,
        "source": "Download.start_timeout",
        "rpcCode": rpc_code,
        "possibleSideEffect": True,
    }
    store = _download_receipt_store(agent)
    for key in keys:
        store[key] = receipt
    return receipt

def _unverified_timeout_matches(item: JsonDict, params: JsonDict) -> bool:
    """Whether one timeout_unverified receipt describes THIS operation.

    Identity, not a shared field: a direct-URL timeout matches by any of its
    URL aliases; a page reservation (no URL by contract) matches by
    pageId+savePath. Matching by savePath alone would let one page's failed
    reservation block every other page's identical destination path.
    """
    if str(item.get("state") or "") != "timeout_unverified":
        return False
    item_urls = _record_urls(item)
    param_urls = _record_urls(params)
    if param_urls:
        return bool(item_urls & param_urls)
    page_id = str(params.get("pageId") or "").strip()
    save_path = str(params.get("savePath") or "").strip()
    if not (page_id and save_path):
        return False
    return (
        str(item.get("pageId") or "").strip() == page_id
        and str(item.get("savePath") or "").strip() == save_path
    )

def _expires_at_ms(value: Any) -> Optional[float]:
    """Normalize a reservation expiry timestamp to milliseconds.

    The live Download.start/Download.waiting contract defines expiresAt as
    epoch MILLISECONDS; a legacy seconds value (from an older generation or a
    hand-built receipt) would otherwise read as expired in 1970. Values below
    1e12 cannot be millisecond timestamps in any modern era, so they are
    treated as seconds. Anything non-numeric yields None (unknown expiry).
    """
    try:
        stamp = float(value)
    except (TypeError, ValueError):
        return None
    if stamp <= 0:
        return None
    return stamp * 1000.0 if stamp < 1e12 else stamp

def _reusable_download_response(agent: Any, params: JsonDict) -> Optional[JsonDict]:
    key = _download_operation_key(params)
    store = _download_receipt_store(agent)
    receipt = store.get(key) if key else None
    # An uncertain redirect side effect is URL-scoped, not path-scoped: merely
    # changing savePath must not let the model re-dispatch the same URL and
    # create another file in the browser's default download directory. A page
    # reservation timeout is pageId+savePath-scoped for the same reason.
    unverified = next(
        (
            item for item in store.values()
            if isinstance(item, dict)
            and _unverified_timeout_matches(item, params)
        ),
        None,
    )
    if (
        isinstance(unverified, dict)
        and str((receipt or {}).get("state") or "") != "completed"
    ):
        receipt = unverified
    # Active receipts are observations from an earlier instant.  Reusing them
    # forever can make a stalled/failed operation impossible to retry; callers
    # must refresh those by downloadId through Download.list first.
    if not isinstance(receipt, dict):
        return None
    state = str(receipt.get("state") or "")
    if state == "timeout_unverified":
        return {
            "error": "A prior Download.start for this exact URL/savePath timed out with an unverified side effect.",
            "downloadReconciliation": {
                "classification": "timeout_unverified",
                "receipt": dict(receipt),
            },
            "suggested_prompt": (
                "Do not resend the same URL. The redirected file may already"
                " exist in the browser's default download directory. Obtain"
                " the final direct file URL before one bounded retry."
            ),
        }
    if state == "waiting":
        # A live page-download reservation owns its five-minute capture
        # window; a second Download.start could attach a second transfer to
        # the same page. Only block while the reservation is provably still
        # valid: an expired (or unknown-expiry) waiting receipt falls through
        # so the async Download.list refresh - which runs when this returns
        # None - can re-confirm or retire it instead of blocking retries
        # forever when the expiry event was lost.
        expires_ms = _expires_at_ms(receipt.get("expiresAt"))
        still_valid = (
            expires_ms is not None
            and expires_ms > time.time() * 1000.0
        )
        if not still_valid:
            return None
        return {
            "observation": (
                "A page-download reservation is already waiting for this"
                " operation; it was not re-started."
            ),
            "data": {
                "downloadId": receipt.get("downloadId"),
                "state": "waiting",
                "savePath": receipt.get("savePath"),
                "reused": True,
            },
            "downloadReconciliation": {
                "classification": "reservation_waiting",
                "receipt": dict(receipt),
            },
            "suggested_prompt": (
                "Trigger the page-initiated download with a separate Input"
                " action, then observe Download events or Download.list with"
                " this downloadId. Do not start another reservation for the"
                " same capture while this one is waiting; it expires"
                " five minutes after it was made."
            ),
        }
    if state != "completed":
        return None
    return {
        "observation": "Reused an existing reconciled download operation.",
        "data": {
            "success": True,
            "downloadId": receipt.get("downloadId"),
            "state": receipt.get("state"),
            "savePath": receipt.get("savePath"),
            "url": receipt.get("url"),
            "reused": True,
        },
        "downloadReconciliation": {
            "classification": "already_started",
            "receipt": dict(receipt),
        },
    }

async def _refresh_active_download_response(
    agent: Any,
    runner: Any,
    params: JsonDict,
) -> Optional[JsonDict]:
    """Refresh an old active receipt before deciding whether to retry."""
    key = _download_operation_key(params)
    receipt = _download_receipt_store(agent).get(key) if key else None
    if not isinstance(receipt, dict) or str(receipt.get("state") or "") not in {
        "waiting", "downloading", "paused",
    }:
        return None
    download_id = str(receipt.get("downloadId") or "").strip()
    fleet_id = str(getattr(agent, "assigned_fleet_id", "") or "").strip()
    if not download_id or not fleet_id:
        if key:
            _download_receipt_store(agent).pop(key, None)
        return None
    try:
        listed = await runner.call(
            "Download.list",
            {
                "fleetId": fleet_id,
                "downloadId": download_id,
                "limit": 1,
                "purpose": "Refresh an existing download before retrying it",
            },
        )
    except ABCPTransportError as exc:
        if bool(getattr(exc, "connection_fatal", False)):
            raise
        # A failed refresh does not prove the old operation is gone.  Surface
        # uncertainty rather than dispatching a duplicate side effect.
        return {
            "error": "Existing download state could not be refreshed.",
            "downloadReconciliation": {
                "classification": "active_unverified",
                "receipt": dict(receipt),
            },
            "suggested_prompt": (
                "Do not retry this Download.start until Download.list can"
                " confirm the prior operation's terminal state."
            ),
        }
    records = [
        row for row in _download_records(listed)
        if str(row.get("id") or row.get("downloadId") or "") == download_id
    ]
    if len(records) != 1:
        if key:
            _download_receipt_store(agent).pop(key, None)
        return None
    refreshed = _remember_download_record(
        agent,
        {**records[0], "operationKey": key},
    )
    state = str(refreshed.get("state") or "")
    if state not in {"waiting", "downloading", "paused", "completed"}:
        if key:
            _download_receipt_store(agent).pop(key, None)
        return None
    return {
        "observation": "Refreshed and reused an existing download operation.",
        "data": {
            "success": True,
            "downloadId": refreshed.get("downloadId"),
            "state": state,
            "savePath": refreshed.get("savePath"),
            "url": refreshed.get("url"),
            "reused": True,
        },
        "downloadReconciliation": {
            "classification": "already_started",
            "receipt": dict(refreshed),
        },
    }

def _download_start_timed_out(response: Any) -> bool:
    if isinstance(response, ABCPTransportError):
        return getattr(response, "rpc_code", None) == -32014
    if not isinstance(response, dict):
        return False

    candidates: List[Any] = [response]
    nested = response.get("response")
    if isinstance(nested, dict):
        candidates.append(nested)
    for candidate in candidates:
        error = candidate.get("error") if isinstance(candidate, dict) else None
        if isinstance(error, dict) and error.get("code") == -32014:
            return True
    return False

def _record_urls(record: JsonDict) -> Set[str]:
    """Every URL identity a DownloadRecord may carry across generations."""
    return {
        str(record.get(name) or "").strip()
        for name in ("requestedUrl", "finalUrl", "url")
        if str(record.get(name) or "").strip()
    }

def _classify_download_reconciliation(
    *,
    params: JsonDict,
    list_response: Any,
) -> JsonDict:
    """Match a timed-out Download.start against Download.list records.

    The two Download.start variants reconcile differently and never degrade
    to a savePath-only match:

    - Direct URL start: a record matches when the requested URL equals the
      record's requestedUrl OR finalUrl AND the savePath matches.
    - Page reservation (no URL in params by contract): a record matches only
      when it is the same page's reservation - sourceType "page", the same
      pageId, and the same savePath. Without this, one page's reservation
      timeout could adopt another page's completed download that happens to
      share the destination path.
    """
    records = _download_records(list_response)
    param_urls = _record_urls(params)
    save_path = str(params.get("savePath") or "").strip()
    page_id = str(params.get("pageId") or "").strip()

    if param_urls:
        matches = [
            row for row in records
            if param_urls & _record_urls(row)
            and str(row.get("savePath") or "").strip() == save_path
        ]
    else:
        # Page reservation: require the record to BE this page's capture.
        matches = [
            row for row in records
            if str(row.get("sourceType") or "").strip() == "page"
            and str(row.get("pageId") or "").strip() == page_id
            and str(row.get("savePath") or "").strip() == save_path
        ]
    if len(matches) > 1:
        return {"classification": "ambiguous", "matches": matches}
    if not matches:
        return {"classification": "not_observed", "matches": []}
    record = matches[0]
    state = str(record.get("state") or "")
    classification = (
        "completed" if state == "completed"
        else "active" if state in {"waiting", "downloading", "paused"}
        else "failed" if state in {"failed", "cancelled"}
        else "ambiguous"
    )
    return {"classification": classification, "matches": [record]}

async def _reconcile_download_start_timeout(
    *,
    agent: Any,
    runner: Any,
    params: JsonDict,
    timeout_error: Optional[ABCPTransportError] = None,
) -> JsonDict:
    """Reconcile a possibly-side-effecting timeout without blind retry.

    Download records are created asynchronously by Electron's will-download
    hook and can appear a few seconds after the RPC timeout. Only an exact
    operation match is authoritative here: requested/final URL + savePath for
    a direct start, or sourceType=page + pageId + savePath for a reservation.
    Redirected orphan records are deliberately not claimed by time proximity
    because concurrent workers (or a human) may download in the same Fleet.
    """
    fleet_id = str(getattr(agent, "assigned_fleet_id", "") or "").strip()
    rpc_code = getattr(timeout_error, "rpc_code", None)
    if not fleet_id:
        receipt = _remember_unverified_download_timeout(
            agent, params, rpc_code=rpc_code,
        )
        return {
            "classification": "timeout_unverified",
            "matches": [],
            "reason": "assigned_fleet_id_unavailable",
            "receipt": receipt,
        }

    last_result: JsonDict = {
        "classification": "not_observed",
        "matches": [],
    }
    observations: List[JsonDict] = []
    for check_index in range(2):
        if check_index:
            await asyncio.sleep(DOWNLOAD_TIMEOUT_RECONCILIATION_DELAY_SECONDS)
        try:
            list_response = await runner.call(
                "Download.list",
                {
                    "fleetId": fleet_id,
                    "limit": 100,
                    "purpose": (
                        "Reconcile whether a timed-out Download.start already"
                        " produced the exact requested browser-side operation"
                    ),
                },
            )
        except ABCPTransportError as exc:
            if bool(getattr(exc, "connection_fatal", False)):
                raise
            observations.append({
                "check": check_index + 1,
                "classification": "list_failed",
                "error": str(exc),
            })
            last_result = {
                "classification": "ambiguous",
                "matches": [],
                "reason": "download_list_failed",
                "error": str(exc),
            }
            continue
        last_result = _classify_download_reconciliation(
            params=params,
            list_response=list_response,
        )
        observations.append({
            "check": check_index + 1,
            "classification": last_result.get("classification"),
            "matchCount": len(last_result.get("matches") or []),
        })
        if last_result.get("classification") in {
            "completed", "active", "failed", "ambiguous",
        }:
            break

    last_result = dict(last_result)
    last_result["checks"] = observations
    matches = last_result.get("matches") or []
    if len(matches) == 1 and isinstance(matches[0], dict):
        record = {**matches[0], "operationKey": _download_operation_key(params)}
        last_result["receipt"] = _remember_download_record(agent, record)
    elif last_result.get("classification") in {"not_observed", "ambiguous"}:
        last_result["classification"] = "timeout_unverified"
        last_result["reason"] = (
            last_result.get("reason") or "exact_operation_not_observed"
        )
        last_result["receipt"] = _remember_unverified_download_timeout(
            agent, params, rpc_code=rpc_code,
        )
    return last_result

DOWNLOAD_EVENT_METHODS = frozenset({
    "Download.waiting",
    "Download.started",
    "Download.progressed",
    "Download.stateChanged",
})

def _apply_download_event(record: JsonDict, event_name: str, payload: JsonDict) -> JsonDict:
    """Monotone reducer for one Download.* lifecycle event.

    Rules (platform state machine: waiting -> downloading -> paused ->
    downloading -> completed/failed/cancelled; terminal states have no
    outgoing transitions):

    - A terminal receipt never reopens: once completed/failed/cancelled is
      observed, any later non-terminal projection (waiting, started,
      progressed, stateChanged-to-active) is a stale event and is dropped.
    - A conflicting terminal event is also dropped: completed -> failed has no
      legal transition, so the first terminal observation stands.
    - Byte counters only move forward (max); a regressed progressed event is
      out-of-order.
    - progressed is the first event seen -> state becomes "downloading"
      (a transfer reporting bytes has started, whatever was missed).
    """
    stored = str(record.get("state") or "")
    stored_terminal = stored in DOWNLOAD_TERMINAL_STATES

    if event_name == "Download.waiting":
        if stored_terminal:
            return record
        record["state"] = "waiting"
        if payload.get("savePath"):
            record["savePath"] = str(payload.get("savePath"))
        if payload.get("pageId"):
            record["pageId"] = str(payload.get("pageId"))
        if payload.get("expiresAt") is not None:
            record["expiresAt"] = payload.get("expiresAt")
        return record

    if event_name == "Download.started":
        if stored_terminal:
            return record
        for src in ("requestedUrl", "finalUrl", "url"):
            value = str(payload.get(src) or "").strip()
            if value:
                record[src] = value
        if not str(record.get("url") or "").strip():
            record["url"] = (
                record.get("finalUrl")
                or record.get("requestedUrl")
                or ""
            )
        if payload.get("savePath"):
            record["savePath"] = str(payload.get("savePath"))
        record["state"] = "downloading"
        return record

    if event_name == "Download.progressed":
        # A late progressed projection after a terminal state is dropped in
        # full - byte counters included: a regressed or resurrected counter
        # can flip the registration signature and fabricate a new receipt.
        if stored_terminal:
            return record
        try:
            received = int(payload.get("receivedBytes") or 0)
            total = int(payload.get("totalBytes") or 0)
        except (TypeError, ValueError):
            return record
        record["receivedBytes"] = max(int(record.get("receivedBytes") or 0), received)
        record["totalBytes"] = max(int(record.get("totalBytes") or 0), total)
        if stored not in {"downloading", "paused"}:
            # First evidence of a live transfer (possibly after missed
            # waiting/started events).
            record["state"] = "downloading"
        return record

    if event_name == "Download.stateChanged":
        current = str(payload.get("currentState") or "").strip()
        if not current:
            return record
        if stored_terminal:
            # First terminal observation stands; a different terminal is an
            # illegal transition and a non-terminal is a stale event.
            return record
        record["state"] = current
        if payload.get("errorCode") or payload.get("errorMessage"):
            record.update(_normalize_failure(payload))
        return record

    return record

def _remember_download_event(agent: Any, event_name: str, payload: Any) -> Optional[JsonDict]:
    """Fold one Download.* lifecycle event into the receipt ledger.

    Event payloads (browser projections):
      Download.waiting     {fleetId, pageId, downloadId, savePath, expiresAt}
      Download.started     {fleetId, pageId, downloadId, requestedUrl?, finalUrl?, savePath}
      Download.progressed  {fleetId, pageId?, downloadId, receivedBytes, totalBytes}
      Download.stateChanged{fleetId, pageId?, downloadId, previousState, currentState,
                            errorCode?, errorMessage?}

    This keeps page-reservation flows observable end-to-end
    (start(waiting) -> Input action -> started -> progressed -> stateChanged)
    without waiting for the model to poll Download.list. The reducer is
    strictly monotone: a receipt only ever moves forward through the platform
    lifecycle, so an event stamped between two observations can never
    resurrect a terminal state, roll back byte counters, or reopen a
    reservation that already transferred.
    """
    if event_name not in DOWNLOAD_EVENT_METHODS or not isinstance(payload, dict):
        return None
    download_id = str(payload.get("downloadId") or "").strip()
    if not download_id:
        return None
    store = _download_receipt_store(agent)
    download_key = json.dumps(["downloadId", download_id], ensure_ascii=False)
    existing = store.get(download_key)
    # A page reservation remembered under its pageId+savePath key may be the
    # same operation as this event; adopt it so both aliases converge.
    if not isinstance(existing, dict):
        page_id = str(payload.get("pageId") or "").strip()
        save_path = str(payload.get("savePath") or "").strip()
        if page_id and save_path:
            page_key = json.dumps(["page", page_id, save_path], ensure_ascii=False)
            page_receipt = store.get(page_key)
            if isinstance(page_receipt, dict) and not str(
                page_receipt.get("downloadId") or ""
            ).strip():
                existing = page_receipt
    record: JsonDict = dict(existing) if isinstance(existing, dict) else {}
    record["downloadId"] = download_id
    record.setdefault("state", "")
    record.setdefault("totalBytes", 0)
    record.setdefault("receivedBytes", 0)
    before = dict(record)
    record = _apply_download_event(record, event_name, payload)
    # A dropped event (terminal receipt already observed, or a regressed
    # counter) must leave the receipt BYTE-IDENTICAL: no source rewrite, no
    # re-filing, no fresh file registration. Only accepted events advance
    # the ledger.
    accepted = record != before
    logger = getattr(agent, "logger", None)
    if not accepted:
        if logger is not None:
            logger.write(
                "download.event_dropped",
                {
                    "event": event_name,
                    "downloadId": download_id,
                    "state": record.get("state"),
                },
            )
        return record
    record["source"] = event_name
    # File under every derivable alias so both the downloadId lookup and the
    # original url/page-based lookup see the same updated receipt.
    keys = _download_operation_keys(record)
    if download_key not in keys:
        keys.insert(0, download_key)
    for key in keys:
        store[key] = record
    # Terminal/active-with-path states refresh file registration; waiting and
    # rejected events do not (see _register_download_resource whitelist).
    if str(record.get("state") or "") in DOWNLOAD_FILE_REGISTRABLE_STATES:
        _register_download_resource(agent, record, operation_key=keys[0])
    if logger is not None:
        logger.write(
            "download.event_observed",
            {
                "event": event_name,
                "downloadId": download_id,
                "state": record.get("state"),
            },
        )
    return record
