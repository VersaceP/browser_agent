#!/usr/bin/env python3
"""Live canary for DOM.getImg's widened target surface.

The action's description promises img / picture / SVG image / inline SVG /
canvas / screenshot-backed visual targets and a selector that pierces nested
author Shadow DOM. Everything except the shadow-piercing selector is resolved
inside native Chromium, which this repository cannot inspect — so the six cases
below ASK the platform instead of assuming an answer.

Case 6 is the decisive one. It targets a plain container that merely CONTAINS an
image:

    info.method == "fallback-screenshot"  ->  no descendant drill-down; the
        container was captured as pixels. The harness must keep pointing at the
        real visual node.
    info.method == "native-image"         ->  native DOES resolve a visual
        descendant, and the harness may target containers directly. Record it
        and revisit the prompt guidance.

MEASURED 2026-08-11 against the live dispatcher (ABCP @ 407fd28), all six cases
ok=true:

    1 <img>            native-image  image/png       (source asset, 1x1 —
                                                      NOT the rendered 120x80)
    2 <picture>        native-image  image/png
    3 inline <svg>     native-image  image/svg+xml   (22KB: serialized with the
                                                      full computed style block)
    4 <canvas>         native-image  image/png  80x50 (real pixel content)
    5 shadow DOM img   native-image  image/png       (plain `#shadow-img`
                                                      selector pierced the root)
    6 plain container  fallback-screenshot, fallbackReason.code=
                       "unsupported-image-target", 2528x124 — the CONTAINER's
                       pixels, wrapper text included.

So "deep selector" means shadow-DOM piercing, and any visual node is exportable
via the screenshot fallback — but there is NO container-to-descendant drill-down.
Re-run this canary after an ABCP upgrade rather than trusting the table.

Read-only apart from opening one page in a fleet the caller supplies, plus the
image files written under --out.

Usage:
    python3 abcp_get_img_canary.py --fleet <fleetId> [--url <page>] [--out DIR]
"""

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT))

from abcp_client import ABCPClient  # noqa: E402
from runtime_config import ABCPClientConfig  # noqa: E402

JsonDict = Dict[str, Any]

# A self-contained page carrying one of each target kind, so the canary does not
# depend on a live site keeping its markup stable.
FIXTURE = """
<!doctype html><html><head><meta charset="utf-8"><title>getImg canary</title>
<style>body{font-family:sans-serif}div{margin:12px 0}</style></head><body>
<div id="case-img"><img id="plain-img" width="120" height="80"
  src="data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="></div>
<div id="case-picture"><picture><source srcset="data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==" type="image/png">
  <img id="picture-img" width="100" height="60" src="data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="></picture></div>
<div id="case-inline-svg"><svg id="inline-svg" width="90" height="60" xmlns="http://www.w3.org/2000/svg">
  <rect width="90" height="60" fill="#3366cc"></rect><circle cx="45" cy="30" r="20" fill="#ffcc00"></circle></svg></div>
<div id="case-canvas"><canvas id="live-canvas" width="80" height="50"></canvas></div>
<div id="case-shadow"><div id="shadow-host"></div></div>
<div id="case-container"><div id="plain-container" style="padding:8px;border:1px solid #ccc">
  <span>wrapper text</span>
  <img id="nested-img" width="70" height="40" src="data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==">
</div></div>
<script>
  const c = document.getElementById('live-canvas').getContext('2d');
  c.fillStyle = '#cc3366'; c.fillRect(0, 0, 80, 50);
  c.fillStyle = '#ffffff'; c.fillRect(20, 15, 40, 20);
  const host = document.getElementById('shadow-host').attachShadow({mode: 'open'});
  host.innerHTML = '<img id="shadow-img" width="60" height="40" ' +
    'src="data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==">';
</script></body></html>
"""

CASES = [
    ("1. plain <img>", "#plain-img", None),
    ("2. <picture>", "#case-picture picture", None),
    ("3. inline <svg>", "#inline-svg", None),
    ("4. <canvas>", "#live-canvas", None),
    ("5. image inside Shadow DOM", "#shadow-img", "selector must pierce the shadow root"),
    ("6. plain container wrapping an <img>", "#plain-container",
     "DECIDES whether native drills down to a visual descendant"),
]


def _data(response: Any) -> JsonDict:
    if isinstance(response, dict):
        data = response.get("data")
        if isinstance(data, dict):
            return data
    return {}


async def _call(client: ABCPClient, method: str, params: JsonDict) -> JsonDict:
    try:
        return await client.call(method, params)
    except Exception as exc:  # noqa: BLE001 - a canary reports, it does not raise
        return {"error": str(exc)}


async def run(args: argparse.Namespace) -> int:
    raw = json.loads((REPO_ROOT / "config.json").read_text(encoding="utf-8"))
    config = ABCPClientConfig.from_dict(raw.get("browser") or {})
    out_dir = Path(args.out).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    client = ABCPClient(config)
    await client.connect()
    page_id: Optional[str] = None
    try:
        url = args.url or ("data:text/html;charset=utf-8," + FIXTURE.strip().replace("#", "%23"))
        created = await _call(client, "Page.create", {
            "fleetId": args.fleet,
            "url": url,
            "purpose": "getImg canary: open the fixture page",
        })
        page_id = str(_data(created).get("pageId") or "")
        if not page_id:
            print(json.dumps({"stage": "Page.create", "response": created}, ensure_ascii=False, indent=2))
            return 1
        await asyncio.sleep(1.5)

        rows: List[JsonDict] = []
        for label, selector, note in CASES:
            response = await _call(client, "DOM.getImg", {
                "pageId": page_id,
                "targets": [{"selector": selector}],
                "options": {"path": str(out_dir), "format": "file", "imageFormat": "auto"},
                "purpose": f"getImg canary: {label}",
            })
            items = _data(response).get("items")
            item = items[0] if isinstance(items, list) and items else {}
            info = item.get("info") if isinstance(item, dict) else None
            info = info if isinstance(info, dict) else {}
            error = item.get("error") if isinstance(item, dict) else None
            rows.append({
                "case": label,
                "selector": selector,
                "note": note,
                "ok": bool(item.get("ok")),
                "method": info.get("method"),
                "mimeType": info.get("mimeType"),
                "extension": info.get("extension"),
                "savedPath": info.get("savedPath"),
                "fallbackReason": (info.get("fallbackReason") or {}).get("code")
                if isinstance(info.get("fallbackReason"), dict) else None,
                "resolvedBy": item.get("resolvedBy") if isinstance(item, dict) else None,
                "errorCode": (error or {}).get("code") if isinstance(error, dict) else None,
                "fallbackContext": (error or {}).get("fallbackContext") if isinstance(error, dict) else None,
            })

        print(json.dumps({"pageId": page_id, "outDir": str(out_dir), "cases": rows},
                         ensure_ascii=False, indent=2))
        container = next((row for row in rows if row["case"].startswith("6.")), None)
        if container:
            verdict = (
                "native drill-down CONFIRMED (container resolved to a visual descendant)"
                if container.get("method") == "native-image"
                else "no drill-down: container captured as a screenshot"
                if container.get("method") == "fallback-screenshot"
                else f"inconclusive: {container.get('errorCode') or container.get('method')}"
            )
            print(f"\nCONTAINER VERDICT: {verdict}")
        return 0
    finally:
        if page_id and not args.keep_page:
            await _call(client, "Page.close", {
                "pageId": page_id, "purpose": "getImg canary: clean up"})
        await client.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fleet", required=True, help="Existing fleet UUID to open the page in")
    parser.add_argument("--url", default="", help="Override the built-in fixture page")
    parser.add_argument("--out", default=str(REPO_ROOT / "artifacts" / "getimg-canary"))
    parser.add_argument("--keep-page", action="store_true", help="Leave the page open for inspection")
    return asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    sys.exit(main())
