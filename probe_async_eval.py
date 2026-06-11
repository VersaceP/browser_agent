"""
probe_async_eval.py V3 - Test async support via the title side-channel.

Key insight from V2: ABCP Runtime.evaluate ALWAYS returns data:null,
so returnByValue and awaitPromise are completely useless.
The ONLY way to get data back is via document.title (the side-channel
that eval_js_json already uses).

This test verifies whether the title side-channel works with async expressions.
"""

import asyncio
import argparse
import json
import sys
from pathlib import Path
from typing import Any, List, Dict

from abcp_client import ABCPClient, ABCPClientConfig, ABCPTransportError


def load_browser_config(config_path: str) -> ABCPClientConfig:
    raw = json.loads(Path(config_path).read_text(encoding="utf-8"))
    return ABCPClientConfig.from_dict(raw.get("browser", {}))


def short(value: Any, n: int = 400) -> str:
    text = json.dumps(value, ensure_ascii=False, default=str)
    return text if len(text) <= n else text[:n] + f"... <+{len(text) - n} chars>"


PREFIX = "__ABCP_JSON__"
CHUNK_CHARS = 7000  # larger chunk size for efficiency


async def read_title_side_channel(browser, page_id: str, max_polls: int = 10) -> Dict:
    """Poll Page.getState to read title, mimicking _eval_json_via_title."""
    for poll in range(max_polls):
        await asyncio.sleep(0.3)
        state = await browser.call(
            "Page.getState",
            {"pageId": page_id, "purpose": "Read title side-channel"},
        )
        data = state.get("data", {}) if isinstance(state, dict) else {}
        title = data.get("title", "") if isinstance(data, dict) else ""
        if not title and isinstance(state, dict):
            title = state.get("observation", "")
        
        print(f"    poll {poll}: title={short(title, 120)}")

        if title.startswith(f"{PREFIX}|READY|"):
            total_len_str = title.rsplit("|", 1)[-1]
            try:
                total_len = int(total_len_str)
            except ValueError:
                return {"status": "error", "reason": "bad READY length"}
            
            # Read chunks
            chunks = []
            offset = 0
            while offset < total_len:
                chunk_expr = f"""(() => {{
  const text = String(window.__abcpJsonB64 || "bnVsbA==");
  const start = Number(window.__abcpJsonOffset || 0);
  const chunk = text.slice(start, start + {CHUNK_CHARS});
  window.__abcpJsonOffset = start + chunk.length;
  document.title = "{PREFIX}|CHUNK|" + String(start) + "|" + chunk;
  return document.title;
}})()"""
                await browser.call("Runtime.evaluate", {
                    "pageId": page_id,
                    "expression": chunk_expr,
                    "purpose": "Read chunk",
                })
                cstate = await browser.call("Page.getState", {
                    "pageId": page_id,
                    "purpose": "Read chunk data",
                })
                cdata = cstate.get("data", {}) if isinstance(cstate, dict) else {}
                ctitle = cdata.get("title", "") if isinstance(cdata, dict) else ""
                
                parts = ctitle.split("|", 3)
                if len(parts) == 4 and parts[1] == "CHUNK":
                    chunks.append(parts[3])
                    offset += CHUNK_CHARS
                else:
                    return {"status": "error", "reason": "bad chunk", "title": ctitle}

            # Decode
            import base64
            try:
                b64_text = "".join(chunks)
                raw_bytes = base64.b64decode(b64_text)
                text = raw_bytes.decode("utf-8")
                value = json.loads(text)
                return {"status": "success", "value": value}
            except Exception as e:
                return {"status": "error", "reason": str(e), "raw": short(b64_text, 100)}

        elif title.startswith(f"{PREFIX}|PENDING|"):
            continue
        elif title.startswith(f"{PREFIX}|ERROR|"):
            error_msg = title.split("|", 2)[-1] if "|ERROR|" in title else "unknown"
            return {"status": "error", "reason": error_msg}

    return {"status": "timeout", "reason": f"no READY marker after {max_polls} polls"}


async def test_expression_via_title(browser, page_id: str, label: str, expression: str, is_async: bool = False) -> Dict:
    """Test an expression using the title side-channel, with both sync and async wrappers."""
    
    # --- SYNC wrapper (current _build_eval_js_json_expression pattern) ---
    sync_wrapped = f"""(() => {{
  try {{
    document.title = "{PREFIX}|PENDING|0";
    const __abcpExpression = {json.dumps(expression)};
    const __abcpValue = (0, eval)("(" + __abcpExpression + ")");
    const text = JSON.stringify({{ value: __abcpValue }});
    const bytes = new TextEncoder().encode(text);
    let binary = "";
    const step = 0x8000;
    for (let i = 0; i < bytes.length; i += step) {{
      binary += String.fromCharCode.apply(null, bytes.subarray(i, i + step));
    }}
    window.__abcpJsonB64 = btoa(binary);
    window.__abcpJsonOffset = 0;
    document.title = "{PREFIX}|READY|" + String(window.__abcpJsonB64.length);
  }} catch (err) {{
    document.title = "{PREFIX}|ERROR|" + String(err && err.message || err).slice(0, 500);
  }}
}})()"""

    # --- ASYNC wrapper (proposed fix) ---
    async_wrapped = f"""(async () => {{
  try {{
    document.title = "{PREFIX}|PENDING|0";
    const __abcpExpression = {json.dumps(expression)};
    const __abcpValue = await (0, eval)("(" + __abcpExpression + ")");
    const text = JSON.stringify({{ value: __abcpValue }});
    const bytes = new TextEncoder().encode(text);
    let binary = "";
    const step = 0x8000;
    for (let i = 0; i < bytes.length; i += step) {{
      binary += String.fromCharCode.apply(null, bytes.subarray(i, i + step));
    }}
    window.__abcpJsonB64 = btoa(binary);
    window.__abcpJsonOffset = 0;
    document.title = "{PREFIX}|READY|" + String(window.__abcpJsonB64.length);
  }} catch (err) {{
    document.title = "{PREFIX}|ERROR|" + String(err && err.message || err).slice(0, 500);
  }}
}})()"""

    results = {}
    for wrapper_label, wrapped_expr in [("sync wrapper", sync_wrapped), ("async wrapper", async_wrapped)]:
        print(f"\n  [{wrapper_label}]")
        try:
            await browser.call("Runtime.evaluate", {
                "pageId": page_id,
                "expression": wrapped_expr,
                "purpose": f"Test {label} via title side-channel",
            })
            result = await read_title_side_channel(browser, page_id)
            results[wrapper_label] = result
            
            if result["status"] == "success":
                value = result.get("value", {})
                inner = value.get("value", value)
                if inner == {}:
                    print(f"    ⚠️ value={{}} — Promise NOT awaited (JSON.stringify(Promise) = {{}})")
                elif isinstance(inner, dict) and "a" in inner:
                    print(f"    ✅ SUCCESS: {short(inner, 150)}")
                elif isinstance(inner, dict) and "error" in inner:
                    print(f"    ⚠️ Expression error: {short(inner, 100)}")
                else:
                    print(f"    → {short(inner, 150)}")
            elif result["status"] == "timeout":
                print(f"    ⏰ TIMEOUT — async expression didn't resolve before title read")
            else:
                print(f"    ❌ {result['status']}: {result.get('reason', '')[:150]}")
        except ABCPTransportError as exc:
            print(f"    TRANSPORT ERROR: {exc}")
            results[wrapper_label] = {"status": "transport_error", "reason": str(exc)}

    return results


async def main() -> int:
    config = load_browser_config("config.json")
    print(f"[probe] ws_url={config.ws_url}")

    try:
        async with ABCPClient(config) as browser:
            await browser.call("System.register", {"agentId": "probe-async-v3"})
            print("[probe] registered")

            fleet_resp = await browser.call("Fleet.create", {})
            fleet_id = fleet_resp.get("data", {}).get("fleetId") if isinstance(fleet_resp, dict) else None
            if not fleet_id:
                print(f"[probe] no fleetId", file=sys.stderr)
                return 1

            page_resp = await browser.call("Page.create", {"fleetId": fleet_id})
            page_id = page_resp.get("data", {}).get("pageId") if isinstance(page_resp, dict) else None
            if not page_id:
                print(f"[probe] no pageId", file=sys.stderr)
                return 1
            print(f"[probe] pageId={page_id}")

            parser = argparse.ArgumentParser()
            parser.add_argument(
                "--include-network",
                action="store_true",
                help="Also test fetch() against httpbin.org; default tests are offline.",
            )
            args = parser.parse_args()

            await browser.call("Page.navigate", {
                "pageId": page_id,
                "url": "about:blank",
                "purpose": "probe async eval without external network dependency",
            })
            print("[probe] navigated to about:blank")
            await asyncio.sleep(1)

            # ============================================================
            print("=" * 80)
            print("CONFIRMED: ABCP Runtime.evaluate ALWAYS returns data:null")
            print("The ONLY data path is document.title side-channel")
            print("=" * 80)

            # ============================================================
            print("\n" + "=" * 80)
            print("TEST 1: Sync expressions (should work with both wrappers)")
            print("=" * 80)

            await test_expression_via_title(browser, page_id,
                "sync literal", "({a:1, b:2})")
            await test_expression_via_title(browser, page_id,
                "sync IIFE", "(() => { return {a:1, b:2}; })()")

            # ============================================================
            print("\n" + "=" * 80)
            print("TEST 2: Promise expressions (sync wrapper → {}, async wrapper → ✅)")
            print("=" * 80)

            await test_expression_via_title(browser, page_id,
                "Promise.resolve", "Promise.resolve({a:1, b:2})")
            await test_expression_via_title(browser, page_id,
                "async IIFE with await",
                "(async () => { const x = await Promise.resolve(42); return {a: x, b: x*2}; })()")

            # ============================================================
            print("\n" + "=" * 80)
            print("TEST 3: Real async without network")
            print("=" * 80)

            await test_expression_via_title(browser, page_id,
                "async timeout",
                "(async () => { const x = await new Promise(r => setTimeout(() => r(7), 50)); return {a: x, b: x + 1}; })()")

            # ============================================================
            if args.include_network:
                print("\n" + "=" * 80)
                print("TEST 4: Network fetch/XHR")
                print("=" * 80)

                await test_expression_via_title(browser, page_id,
                    "async fetch httpbin",
                    "(async () => { try { const r = await fetch('https://httpbin.org/json'); return await r.json(); } catch(e) { return {error: e.message}; } })()")

                await test_expression_via_title(browser, page_id,
                    "sync XHR",
                    """(() => {
                        const xhr = new XMLHttpRequest();
                        xhr.open('GET', 'https://httpbin.org/json', false);
                        try { xhr.send(); return JSON.parse(xhr.responseText); }
                        catch(e) { return {xhrError: e.message}; }
                    })()""")

            # ============================================================
            print("\n" + "=" * 80)
            print("SUMMARY & CONCLUSION")
            print("=" * 80)
            print("""
If TEST 2 shows:
  sync wrapper  → value={} (Promise not awaited)
  async wrapper → value={a:42, b:84} (Promise properly awaited)

Then the fix for eval_js_json is:
  1. Change _build_eval_js_json_expression from sync IIFE to async IIFE
  2. Use 'await (0, eval)(...) instead of (0, eval)(...)
  3. Add polling in _eval_json_via_title for the READY marker
     (async expressions need a short delay before title is set)
  4. Skip the first (useless) Runtime.evaluate with returnByValue
     since ABCP always returns data:null anyway
""")

            await browser.call("Page.close", {"pageId": page_id, "purpose": "cleanup"})
            print("\n[probe] done")
            return 0

    except ABCPTransportError as exc:
        print(f"[probe] transport error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
