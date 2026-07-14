#!/usr/bin/env python3
"""Inspect ABCP Memory and active fleets/pages in one WebSocket session."""

import asyncio
import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple


REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT))

from abcp_client import ABCPClient  # noqa: E402
from runtime_config import ABCPClientConfig  # noqa: E402


JsonDict = Dict[str, Any]


def load_browser_config(agent_id_override: Optional[str] = None) -> Tuple[ABCPClientConfig, str]:
    raw = json.loads((REPO_ROOT / "config.json").read_text(encoding="utf-8"))
    browser_raw = raw.get("browser") or {}
    cfg = ABCPClientConfig.from_dict(browser_raw)
    return cfg, agent_id_override or browser_raw.get("agent_id") or "codex-abcp-direct"


def data(resp: JsonDict) -> Any:
    return resp.get("data", resp)


def as_list(value: Any) -> List[Any]:
    if isinstance(value, list):
        return value
    if isinstance(value, dict):
        for key in ("items", "memories", "records", "fleets", "pages", "tabs"):
            if isinstance(value.get(key), list):
                return value[key]
    return []


def scrub(value: Any) -> Any:
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            lk = str(key).lower()
            if any(secret in lk for secret in ("token", "secret", "password", "apikey", "api_key", "key")):
                out[key] = "***"
            else:
                out[key] = scrub(item)
        return out
    if isinstance(value, list):
        return [scrub(item) for item in value]
    return value


def memory_scopes(memory_payload: Any) -> List[str]:
    scopes = []
    for item in as_list(memory_payload):
        if isinstance(item, str):
            scopes.append(item)
        elif isinstance(item, dict):
            for key in ("scope", "key", "id", "fleetId", "pageId"):
                if item.get(key):
                    scopes.append(str(item[key]))
                    break
    return list(dict.fromkeys(scopes))


def fleet_ids(fleet_payload: Any) -> List[str]:
    ids = []
    for item in as_list(fleet_payload):
        if isinstance(item, str):
            ids.append(item)
        elif isinstance(item, dict):
            for key in ("fleetId", "id"):
                if item.get(key):
                    ids.append(str(item[key]))
                    break
    if isinstance(fleet_payload, dict):
        for key in ("fleetId", "id"):
            if fleet_payload.get(key):
                ids.append(str(fleet_payload[key]))
    return list(dict.fromkeys(ids))


def page_ids(payloads: Iterable[Any]) -> List[str]:
    ids = []

    def visit(value: Any) -> None:
        if isinstance(value, dict):
            for key in ("pageId", "id"):
                raw = value.get(key)
                if isinstance(raw, str) and raw.count("-") == 4:
                    ids.append(raw)
            for item in value.values():
                visit(item)
        elif isinstance(value, list):
            for item in value:
                visit(item)

    for payload in payloads:
        visit(payload)
    return list(dict.fromkeys(ids))


async def safe_call(browser: ABCPClient, method: str, params: Optional[JsonDict] = None) -> JsonDict:
    try:
        resp = await browser.call(method, params or {})
        observation = str(resp.get("observation") or "")
        if observation:
            print(f"{method}: {observation[:220]}", flush=True)
        return resp
    except Exception as exc:
        print(f"{method} failed: {exc}", flush=True)
        return {"error": str(exc)}


async def main() -> int:
    parser = argparse.ArgumentParser(description="Inspect ABCP Memory and active fleets/pages.")
    parser.add_argument("--agent-id", default=None)
    args = parser.parse_args()

    cfg, agent_id = load_browser_config(args.agent_id)
    result: JsonDict = {"agentId": agent_id}

    async with ABCPClient(cfg) as browser:
        await safe_call(browser, "System.register", {"agentId": agent_id})
        await safe_call(browser, "System.getCapabilities", {"skillFile": False})

        memory_resp = await safe_call(browser, "Memory.list", {})
        memory_payload = data(memory_resp)
        result["memoryList"] = scrub(memory_payload)
        result["memoryRecords"] = {}
        for scope in memory_scopes(memory_payload):
            mem = await safe_call(browser, "Memory.get", {"scope": scope})
            result["memoryRecords"][scope] = scrub(data(mem))

        fleet_resp = await safe_call(browser, "Fleet.list", {})
        fleet_payload = data(fleet_resp)
        result["fleetList"] = scrub(fleet_payload)
        result["fleets"] = {}
        collected_page_payloads: List[Any] = []
        for fleet_id in fleet_ids(fleet_payload):
            status = await safe_call(browser, "Fleet.status", {"fleetId": fleet_id})
            pages = await safe_call(browser, "Page.list", {"fleetId": fleet_id})
            status_payload = data(status)
            pages_payload = data(pages)
            collected_page_payloads.extend([status_payload, pages_payload])
            result["fleets"][fleet_id] = {
                "status": scrub(status_payload),
                "pages": scrub(pages_payload),
            }

        result["pageStates"] = {}
        for page_id in page_ids(collected_page_payloads):
            state = await safe_call(
                browser,
                "Page.getState",
                {
                    "pageId": page_id,
                    "purpose": "Identify active ABCP pages and find the logged-in 1688 browser instance requested by the user.",
                },
            )
            result["pageStates"][page_id] = scrub(data(state))

    print(json.dumps(result, ensure_ascii=False, indent=2), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
