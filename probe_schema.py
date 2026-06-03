"""
probe_schema.py - One-shot probe of ABCP System.* introspection methods.

Connects to the running ABCP dispatcher, calls System.register, then exercises
System.describeAction for a curated set of methods plus System.skillsDoc, and
dumps the raw responses so we can see what shape the dispatcher actually
returns. Probe results land in ./probe_results/<timestamp>/.

Usage:
    python probe_schema.py [--config config.json] [--method DOM.click ...]
"""

import argparse
import asyncio
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List

from abcp_client import ABCPClient, ABCPClientConfig, ABCPTransportError


DEFAULT_PROBE_METHODS = [
    "Page.navigate",
    "Page.create",
    "Page.getState",
    "DOM.getAXTree",
    "DOM.getAttribute",
    "DOM.getText",
    "Input.scroll",
    "Input.click",
    "Runtime.evaluate",
    "Fleet.create",
    "Memory.save",
    "Hitl.requestPause",
]


def load_browser_config(config_path: str) -> ABCPClientConfig:
    raw = json.loads(Path(config_path).read_text(encoding="utf-8"))
    return ABCPClientConfig.from_dict(raw.get("browser", {}))


def summarize(response: Any, max_chars: int = 400) -> str:
    text = json.dumps(response, ensure_ascii=False, default=str)
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + f"... <+{len(text) - max_chars} chars>"


def describe_keys(value: Any, depth: int = 0, max_depth: int = 3) -> str:
    if depth >= max_depth:
        return f"<{type(value).__name__}>"
    if isinstance(value, dict):
        lines = []
        for key, item in value.items():
            child = describe_keys(item, depth + 1, max_depth)
            lines.append(f"{'  ' * depth}{key}: {child}")
        return "\n" + "\n".join(lines) if lines else "{}"
    if isinstance(value, list):
        if not value:
            return "[]"
        sample = describe_keys(value[0], depth + 1, max_depth)
        return f"[{len(value)} items, first: {sample}]"
    if isinstance(value, str):
        return f'"{value[:60]}{"…" if len(value) > 60 else ""}"'
    return f"<{type(value).__name__}={value!r}>" if not isinstance(value, (dict, list)) else f"<{type(value).__name__}>"


async def main(argv: List[str]) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="config.json")
    parser.add_argument("--agent-id", default="probe-agent")
    parser.add_argument(
        "--method",
        action="append",
        dest="methods",
        help="Override probe methods (repeat). Defaults to a curated list.",
    )
    args = parser.parse_args(argv)

    config = load_browser_config(args.config)
    methods = args.methods or DEFAULT_PROBE_METHODS

    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    out_dir = Path("probe_results") / timestamp
    out_dir.mkdir(parents=True, exist_ok=True)
    print(f"[probe] config ws_url={config.ws_url}")
    print(f"[probe] output dir: {out_dir}")

    try:
        async with ABCPClient(config) as browser:
            print("[probe] connected")

            reg = await browser.call("System.register", {"agentId": args.agent_id})
            print(f"[probe] System.register → {summarize(reg)}")

            caps = await browser.call("System.getCapabilities", {})
            caps_data = caps.get("data") if isinstance(caps, dict) else None
            cap_methods = []
            if isinstance(caps_data, list):
                cap_methods = [
                    str(item.get("method"))
                    for item in caps_data
                    if isinstance(item, dict) and item.get("method")
                ]
            print(f"[probe] System.getCapabilities → {len(cap_methods)} methods")
            (out_dir / "getCapabilities.json").write_text(
                json.dumps(caps, ensure_ascii=False, indent=2, default=str),
                encoding="utf-8",
            )

            described: Dict[str, Any] = {}
            for method in methods:
                if cap_methods and method not in cap_methods:
                    print(f"[probe] SKIP {method} (not in capabilities)")
                    continue
                print(f"\n[probe] System.describeAction ← {method}")
                try:
                    resp = await browser.call(
                        "System.describeAction",
                        {"method": method},
                    )
                except ABCPTransportError as exc:
                    print(f"  ERROR: {exc}")
                    described[method] = {"error": str(exc)}
                    continue
                described[method] = resp
                if isinstance(resp, dict):
                    print(f"  top-level keys: {sorted(resp.keys())}")
                    data = resp.get("data")
                    print(f"  data shape:{describe_keys(data, max_depth=4)}")
                    obs = resp.get("observation")
                    if obs:
                        print(f"  observation: {str(obs)[:200]}")
                    sp = resp.get("suggested_prompt")
                    if sp:
                        print(f"  suggested_prompt: {str(sp)[:200]}")
                else:
                    print(f"  non-dict response: {summarize(resp)}")

            (out_dir / "describeAction.json").write_text(
                json.dumps(described, ensure_ascii=False, indent=2, default=str),
                encoding="utf-8",
            )

            print("\n[probe] System.skillsDoc")
            try:
                skills = await browser.call("System.skillsDoc", {})
                if isinstance(skills, dict):
                    print(f"  top-level keys: {sorted(skills.keys())}")
                    data = skills.get("data")
                    print(f"  data shape:{describe_keys(data, max_depth=3)}")
                    obs = skills.get("observation")
                    if obs:
                        print(f"  observation: {str(obs)[:300]}")
                    if isinstance(data, dict):
                        content = data.get("content") or data.get("markdown")
                        if isinstance(content, str):
                            print(f"  content length: {len(content)} chars")
                            print(f"  content preview: {content[:300]}")
                    elif isinstance(data, str):
                        print(f"  data length: {len(data)} chars")
                        print(f"  data preview: {data[:300]}")
                else:
                    print(f"  non-dict: {summarize(skills)}")
                (out_dir / "skillsDoc.json").write_text(
                    json.dumps(skills, ensure_ascii=False, indent=2, default=str),
                    encoding="utf-8",
                )
            except ABCPTransportError as exc:
                print(f"  ERROR: {exc}")
                (out_dir / "skillsDoc.json").write_text(
                    json.dumps({"error": str(exc)}, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )

            print(f"\n[probe] done. Full JSON dumps under {out_dir}")
            return 0
    except ABCPTransportError as exc:
        print(f"[probe] transport error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main(sys.argv[1:])))
