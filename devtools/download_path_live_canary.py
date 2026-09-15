#!/usr/bin/env python3
"""Manual WebCross download canary; creates an isolated Fleet and tiny files.

Run from the repository: python devtools/download_path_live_canary.py
Requires the repository's Python dependencies plus jsonschema.
No LLM, credentials, external website, or existing user Fleet is used. Evidence
and downloaded files are retained. Only the Fleet created by this run is closed.
An RPC acceptance alone is never counted as a completed download.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import jsonschema

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from abcp_client import ABCPClient, ABCPTransportError
from runtime_config import ABCPClientConfig


def save(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


async def run(args):
    token = uuid.uuid4().hex
    report_dir = args.report_dir.resolve() / token
    report_dir.mkdir(parents=True, exist_ok=False)
    root = args.destination_root.expanduser().resolve() / ("webcross-download-live-" + token)
    root.mkdir(parents=True, exist_ok=False)
    payload = ("WebCross directory canary " + token + "\n").encode() * 1024
    expected_hash = hashlib.sha256(payload).hexdigest()
    report = {"runId": token, "endpoint": args.ws_url, "destination": str(root),
              "expectedSha256": expected_hash, "cases": [], "status": "incomplete"}
    notifications = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            is_file = self.path.startswith("/payload/")
            body = payload if is_file else b"<!doctype html><title>Download canary</title>Local download fixture"
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream" if is_file else "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            if is_file:
                self.send_header("Content-Disposition", 'attachment; filename="fixture.bin"')
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    origin = "http://127.0.0.1:" + str(server.server_port)
    client = ABCPClient(ABCPClientConfig(
        ws_url=args.ws_url, request_shape="jsonrpc", connect_timeout_seconds=5,
        call_timeout_seconds=20,
    ))
    unsubscribe = client.subscribe_notifications(notifications.append)
    fleet_id = None
    schemas = {}

    async def call(method, params):
        # Refuse schema drift before any business operation.
        if method in schemas:
            jsonschema.validate(params, schemas[method]["inputSchema"])
        return await client.call(method, params)

    def terminal_event(download_id):
        for message in notifications:
            event = (message.get("params") or {}).get("data") or {}
            if event.get("event") != "Download.stateChanged":
                continue
            data = event.get("payload") or event.get("data") or {}
            if data.get("downloadId") == download_id and data.get("currentState") in {
                "completed", "failed", "cancelled",
            }:
                return event
        return None

    try:
        await client.connect()
        registration = (await client.call("System.register", {}))["data"]
        report["registration"] = {key: registration.get(key) for key in (
            "agentId", "catalogRevision", "eventCatalogRevision", "guideRevision",
        )}
        await client.call("System.getCapabilities", {})
        await client.call("System.listEvents", {})
        for method in ("Fleet.create", "Fleet.close", "Page.create", "Page.getState",
                       "Download.start", "Download.list"):
            schemas[method] = (await client.call("System.describeAction", {"method": method}))["data"]
        schemas["Download.stateChanged"] = (await client.call(
            "System.describeEvent", {"event": "Download.stateChanged"},
        ))["data"]
        save(report_dir / "contracts.json", schemas)
        fleet_id = (await call("Fleet.create", {"tags": ["download-live-canary", token]}))["data"]["fleetId"]
        report["fleetId"] = fleet_id
        page_id = (await call("Page.create", {"fleetId": fleet_id, "url": origin + "/"}))["data"]["pageId"]
        ready = False
        for _ in range(30):
            state = await call("Page.getState", {
                "pageId": page_id, "purpose": "Verify the local canary fixture is ready",
            })
            if state.get("data", {}).get("status") == "ready":
                ready = True
                break
            await asyncio.sleep(0.5)
        if not ready:
            raise RuntimeError("Local fixture page did not become ready")

        prepared = root / "预先创建" / "商品图片"
        prepared.mkdir(parents=True)
        cases = [
            ("existing_parent", root / "existing.bin"),
            ("missing_nested_parent", root / "missing" / "level2" / "nested.bin"),
            ("missing_unicode_parent", root / "新目录 空格" / "商品图片" / "image.bin"),
            ("new_precreated_parent", prepared / "prepared.bin"),
        ]
        for name, destination in cases:
            item = {"name": name, "savePath": str(destination),
                    "parentExistedBefore": destination.parent.is_dir(),
                    "fileExistedBefore": destination.exists()}
            report["cases"].append(item)
            started = time.monotonic()
            try:
                response = await call("Download.start", {
                    "pageId": page_id, "url": origin + "/payload/" + name,
                    "savePath": str(destination), "overwrite": False,
                    "purpose": "Verify download parent directory preparation: " + name,
                })
                item["startResponse"] = response
                download_id = response["data"]["downloadId"]
                # Passive notification collection starts before Download.start,
                # so a fast completion cannot race the waiter registration.
                deadline = time.monotonic() + args.timeout
                while time.monotonic() < deadline and terminal_event(download_id) is None:
                    await asyncio.sleep(0.1)
                item["terminalEvent"] = terminal_event(download_id)
                listed = await call("Download.list", {
                    "fleetId": fleet_id, "downloadId": download_id, "limit": 10,
                    "purpose": "Cross-check the exact canary download receipt",
                })
                item["downloadRecords"] = listed.get("data", {}).get("downloads", [])
            except ABCPTransportError as exc:
                item["error"] = {"message": str(exc), "rpcCode": exc.rpc_code,
                                 "rpcData": exc.rpc_data, "connectionFatal": exc.connection_fatal}
                if exc.connection_fatal:
                    raise
            finally:
                item["elapsedSeconds"] = round(time.monotonic() - started, 3)
                item["parentExistsAfter"] = destination.parent.is_dir()
                item["fileExistsAfter"] = destination.is_file()
                item["sha256"] = hashlib.sha256(destination.read_bytes()).hexdigest() if destination.is_file() else None
                event = item.get("terminalEvent") or {}
                event_data = event.get("payload") or event.get("data") or {}
                item["passed"] = (
                    not item.get("error") and item["sha256"] == expected_hash
                    and event_data.get("currentState") == "completed"
                    and any(r.get("state") == "completed" and r.get("savePath") == str(destination)
                            for r in item.get("downloadRecords", []))
                )
                save(report_dir / "summary.json", report)
                print(name, "PASS" if item["passed"] else "FAIL", flush=True)
        report["status"] = "passed" if all(c["passed"] for c in report["cases"]) else "failed"
    except Exception as exc:
        report["status"] = "error"
        report["error"] = str(exc)
    finally:
        if fleet_id:
            try:
                report["cleanup"] = await call("Fleet.close", {"fleetId": fleet_id})
            except Exception as exc:
                report["cleanupError"] = str(exc)
        unsubscribe()
        await client.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        save(report_dir / "notifications.json", notifications)
        save(report_dir / "summary.json", report)
        print("Report:", report_dir / "summary.json", flush=True)
    return 0 if report["status"] == "passed" and not report.get("cleanupError") else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ws-url", default="ws://127.0.0.1:61168/ws")
    parser.add_argument("--destination-root", type=Path, default=Path.home() / "Desktop")
    parser.add_argument("--report-dir", type=Path, default=REPO / "reports" / "download-path-live")
    parser.add_argument("--timeout", type=float, default=15)
    args = parser.parse_args()
    if args.timeout <= 0:
        parser.error("--timeout must be positive")
    raise SystemExit(asyncio.run(run(args)))
