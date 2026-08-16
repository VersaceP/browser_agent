"""
harness.tools.browser_tools.downloads - File.download reconciliation and receipt reuse.
"""

import asyncio
import hashlib
import re
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

def _download_operation_key(params: Any) -> str:
    if not isinstance(params, dict):
        return ""
    url = str(params.get("url") or "").strip()
    save_path = str(params.get("savePath") or "").strip()
    return json.dumps([url, save_path], ensure_ascii=False) if url and save_path else ""

def _download_records(value: Any) -> List[JsonDict]:
    records: List[JsonDict] = []
    seen: Set[str] = set()

    def visit(item: Any, depth: int = 0) -> None:
        if depth > 6:
            return
        if isinstance(item, dict):
            url = str(item.get("url") or "").strip()
            save_path = str(item.get("savePath") or "").strip()
            state = str(item.get("state") or "").strip()
            if url and save_path and state:
                identity = str(item.get("id") or item.get("downloadId") or "")
                dedupe = identity or json.dumps(
                    [url, save_path, state, item.get("startedAt")],
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
    """

    state = str(receipt.get("state") or "").strip().lower()
    save_path = str(receipt.get("savePath") or "").strip()
    logger = getattr(agent, "logger", None)
    if state not in {"downloading", "paused", "completed"} or not save_path or logger is None:
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
    key = str(record.get("operationKey") or "") or _download_operation_key(record)
    receipt = {
        "downloadId": str(record.get("id") or record.get("downloadId") or ""),
        "url": str(record.get("url") or ""),
        "savePath": str(record.get("savePath") or ""),
        "state": str(record.get("state") or ""),
        "totalBytes": int(record.get("totalBytes") or 0),
        "receivedBytes": int(record.get("receivedBytes") or 0),
        "source": str(record.get("source") or "Download.list"),
    }
    if key:
        _download_receipt_store(agent)[key] = receipt
    _register_download_resource(agent, receipt, operation_key=key)
    return receipt

def _remember_unverified_download_timeout(
    agent: Any,
    params: JsonDict,
    *,
    rpc_code: Optional[int],
) -> JsonDict:
    """Remember an uncertain side effect without laundering it as success."""
    key = _download_operation_key(params)
    receipt = {
        "downloadId": "",
        "url": str(params.get("url") or ""),
        "savePath": str(params.get("savePath") or ""),
        "state": "timeout_unverified",
        "totalBytes": 0,
        "receivedBytes": 0,
        "source": "Download.start_timeout",
        "rpcCode": rpc_code,
        "possibleSideEffect": True,
    }
    if key:
        _download_receipt_store(agent)[key] = receipt
    return receipt

def _reusable_download_response(agent: Any, params: JsonDict) -> Optional[JsonDict]:
    key = _download_operation_key(params)
    store = _download_receipt_store(agent)
    receipt = store.get(key) if key else None
    requested_url = str(params.get("url") or "").strip()
    # An uncertain redirect side effect is URL-scoped, not path-scoped: merely
    # changing savePath must not let the model re-dispatch the same URL and
    # create another file in the browser's default download directory.
    unverified = next(
        (
            item for item in store.values()
            if isinstance(item, dict)
            and str(item.get("state") or "") == "timeout_unverified"
            and str(item.get("url") or "").strip() == requested_url
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
        "downloading", "paused",
    }:
        return None
    download_id = str(receipt.get("downloadId") or "").strip()
    fleet_id = str(getattr(agent, "assigned_fleet_id", "") or "").strip()
    if not download_id or not fleet_id:
        if key:
            _download_receipt_store(agent).pop(key, None)
        return None
    try:
        listed, _recovery = await runner.call(
            "Download.list",
            {
                "fleetId": fleet_id,
                "downloadId": download_id,
                "limit": 1,
                "purpose": "Refresh an existing download before retrying it",
            },
        )
    except ABCPTransportError:
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
    if state not in {"downloading", "paused", "completed"}:
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

def _classify_download_reconciliation(
    *,
    params: JsonDict,
    list_response: Any,
) -> JsonDict:
    url = str(params.get("url") or "").strip()
    save_path = str(params.get("savePath") or "").strip()
    matches = [
        row for row in _download_records(list_response)
        if str(row.get("url") or "").strip() == url
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
        else "active" if state in {"downloading", "paused"}
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
    hook and can appear a few seconds after the RPC timeout.  Only an exact
    requested URL/path match is authoritative here.  Redirected orphan records
    are deliberately not claimed by time proximity because concurrent workers
    (or a human) may download in the same Fleet.
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
            list_response, _list_recovery = await runner.call(
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
