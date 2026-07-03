#!/usr/bin/env python3
"""Small ABCP Browser RPC helper for Codex direct-drive probes.

This is intentionally narrow: it loads only the `browser` section from a repo
config file, registers the agent on the same WebSocket connection, performs one
RPC call, and prints the response JSON.
"""

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT))

from abcp_client import ABCPClient, ABCPClientConfig  # noqa: E402


JsonDict = Dict[str, Any]


def _load_browser_config(config_path: Path) -> JsonDict:
    raw = json.loads(config_path.read_text(encoding="utf-8"))
    browser = raw.get("browser") or {}
    if not isinstance(browser, dict):
        raise SystemExit(f"{config_path} has no object-valued browser section")
    return dict(browser)


def _append_query_token(ws_url: str, token: Optional[str]) -> str:
    if not token or not ws_url.startswith("wss://"):
        return ws_url
    parts = urlsplit(ws_url)
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    if "token" not in query:
        query["token"] = token
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))


def _parse_params(raw: str) -> JsonDict:
    try:
        params = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SystemExit(f"--params must be valid JSON: {exc}") from exc
    if not isinstance(params, dict):
        raise SystemExit("--params must decode to a JSON object")
    return params


async def _call(args: argparse.Namespace) -> JsonDict:
    browser_config = _load_browser_config(args.config)
    agent_id = args.agent_id or browser_config.get("agent_id") or "codex-abcp-direct"
    config = ABCPClientConfig.from_dict(browser_config)
    config.ws_url = _append_query_token(config.ws_url, config.jwt_token)
    if args.timeout is not None:
        config.call_timeout_seconds = args.timeout

    params = _parse_params(args.params)
    async with ABCPClient(config) as browser:
        if args.bootstrap and args.method == "System.register":
            params.setdefault("agentId", agent_id)
        elif args.bootstrap:
            await browser.call("System.register", {"agentId": agent_id})
            if args.method != "System.getCapabilities":
                await browser.call("System.getCapabilities", {"skillFile": False})
        return await browser.call(args.method, params)


def main() -> None:
    parser = argparse.ArgumentParser(description="Call one ABCP Browser RPC method directly.")
    sub = parser.add_subparsers(dest="command", required=True)

    call = sub.add_parser("call", help="Register if needed, call one method, and print JSON.")
    call.add_argument("method", help="ABCP method name, e.g. Page.create")
    call.add_argument("--params", default="{}", help="JSON object params for the method")
    call.add_argument("--config", type=Path, default=REPO_ROOT / "config.json")
    call.add_argument("--agent-id", default=None)
    call.add_argument("--timeout", type=float, default=None)
    call.add_argument("--no-bootstrap", dest="bootstrap", action="store_false")
    call.set_defaults(bootstrap=True)

    args = parser.parse_args()
    if args.command == "call":
        result = asyncio.run(_call(args))
        print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
