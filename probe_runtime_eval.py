"""
probe_runtime_eval.py - Verify how ABCP Runtime.evaluate handles bare
expressions vs explicit `return` statements.

Boots a fresh Fleet + Page, navigates to about:blank, then runs Runtime.evaluate
with a curated set of expression shapes. Prints each expression's response
(observation + data) so we can see what the server actually returns.
"""

import asyncio
import json
import sys
from pathlib import Path
from typing import Any, List

from abcp_client import ABCPClient, ABCPClientConfig, ABCPTransportError


EXPRESSIONS: List[str] = [
    "1+1",
    "return 1+1;",
    "(1+1)",
    "(() => 1+1)()",
    "(() => { return 1+1; })()",
    "[1,2,3].length",
    "return [1,2,3].length;",
    "document.title",
    "return document.title;",
    "(function(){ return 42; })()",
    "Promise.resolve(7)",
    "return Promise.resolve(7);",
]


def load_browser_config(config_path: str) -> ABCPClientConfig:
    raw = json.loads(Path(config_path).read_text(encoding="utf-8"))
    return ABCPClientConfig.from_dict(raw.get("browser", {}))


def short(value: Any, n: int = 200) -> str:
    text = json.dumps(value, ensure_ascii=False, default=str)
    return text if len(text) <= n else text[:n] + f"... <+{len(text) - n} chars>"


async def main() -> int:
    config = load_browser_config("config.json")
    print(f"[probe] ws_url={config.ws_url}")

    try:
        async with ABCPClient(config) as browser:
            await browser.call("System.register", {"agentId": "probe-runtime"})
            print("[probe] registered")

            fleet_resp = await browser.call("Fleet.create", {})
            fleet_id = (
                fleet_resp.get("data", {}).get("fleetId")
                if isinstance(fleet_resp, dict)
                else None
            )
            if not fleet_id:
                print(f"[probe] no fleetId; resp={short(fleet_resp)}", file=sys.stderr)
                return 1
            print(f"[probe] fleetId={fleet_id}")

            page_resp = await browser.call("Page.create", {"fleetId": fleet_id})
            page_id = (
                page_resp.get("data", {}).get("pageId")
                if isinstance(page_resp, dict)
                else None
            )
            if not page_id:
                print(f"[probe] no pageId; resp={short(page_resp)}", file=sys.stderr)
                return 1
            print(f"[probe] pageId={page_id}")

            nav = await browser.call(
                "Page.navigate",
                {
                    "pageId": page_id,
                    "url": "about:blank",
                    "purpose": "probe runtime evaluate behavior",
                },
            )
            print(f"[probe] navigated: {short(nav)}")

            print("\n=== Runtime.evaluate matrix ===")
            for expr in EXPRESSIONS:
                params = {
                    "pageId": page_id,
                    "expression": expr,
                    "purpose": "probe expression vs return semantics",
                }
                try:
                    resp = await browser.call("Runtime.evaluate", params)
                except ABCPTransportError as exc:
                    print(f"\nexpr: {expr!r}")
                    print(f"  TRANSPORT ERROR: {exc}")
                    continue

                if isinstance(resp, dict):
                    obs = resp.get("observation")
                    data = resp.get("data")
                    print(f"\nexpr: {expr!r}")
                    print(f"  observation: {obs}")
                    print(f"  data: {short(data, 200)}")
                else:
                    print(f"\nexpr: {expr!r}")
                    print(f"  non-dict response: {short(resp)}")

            await browser.call(
                "Page.close",
                {"pageId": page_id, "purpose": "cleanup probe page"},
            )
            print("\n[probe] done")
            return 0
    except ABCPTransportError as exc:
        print(f"[probe] transport error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
