#!/usr/bin/env python3
"""Controlled probe for Tmall detail rendering and server-side risk flags.

The probe keeps one Fleet and one target page constant while changing only the
page activation/focus/input state.  It saves the inline SSR flags together with
AXTree and SemanticTree snapshots for every stage.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, Optional, Tuple


REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT))

from abcp_client import ABCPClient  # noqa: E402
from runtime_config import ABCPClientConfig  # noqa: E402


JsonDict = Dict[str, Any]
DEFAULT_ITEM_URL = "https://detail.tmall.com/item.htm?id=1057868673696"
DEFAULT_OUTPUT = (
    REPO_ROOT
    / "worktree"
    / "05abac8c1ef34215a8e48a984f7caa0b"
    / "render_probe_controlled"
)


SSR_PROBE_EXPRESSION = r"""
return (() => {
  const result = {
    scriptFound: false,
    parseError: null,
    renderMode: null,
    pcResistDetail: null,
    pcIdentityRisk: null,
    traceId: null,
    itemId: null,
    ratePreviewCount: null,
    rateTotalCount: null,
    viewport: {
      innerWidth: window.innerWidth,
      innerHeight: window.innerHeight,
      scrollY: window.scrollY,
      scrollHeight: document.documentElement.scrollHeight,
      visibilityState: document.visibilityState,
      hidden: document.hidden,
      hasFocus: document.hasFocus()
    },
    dom: {
      commentClassCount: document.querySelectorAll('[class*="Comment--"]').length,
      hasViewAllReviewsText: (document.body && document.body.innerText || '').includes('查看全部评价'),
      hasUserReviewsText: (document.body && document.body.innerText || '').includes('用户评价')
    }
  };

  try {
    let context = null;
    for (const script of Array.from(document.scripts || [])) {
      const text = script.textContent || '';
      if (!text.includes('__ICE_APP_CONTEXT__') || !text.includes('var b')) continue;
      const match = text.match(/var b\s*=\s*(\{[\s\S]*\});for\s*\(var k in a\)/);
      if (!match) continue;
      result.scriptFound = true;
      context = JSON.parse(match[1]);
      break;
    }
    if (!context) return JSON.stringify(result);

    const home = context?.loaderData?.home?.data?.res || {};
    const feature = home.feature || {};
    const components = home.componentsVO || {};
    const rate = components.rateVO || {};
    result.renderMode = context.renderMode ?? null;
    result.pcResistDetail = feature.pcResistDetail ?? null;
    result.pcIdentityRisk = feature.pcIdentityRisk ?? null;
    result.traceId = components.debugVO?.traceId ?? null;
    result.itemId = home.item?.itemId ?? home.ssrItemId ?? null;
    result.ratePreviewCount = Array.isArray(rate.group?.items) ? rate.group.items.length : 0;
    result.rateTotalCount = rate.totalCount ?? null;
  } catch (error) {
    result.parseError = String(error && error.message || error);
  }
  return JSON.stringify(result);
})()
"""


def response_data(response: Any) -> Any:
    if isinstance(response, dict):
        return response.get("data", response)
    return response


def node_count(response: Any) -> Optional[int]:
    data = response_data(response)
    if not isinstance(data, dict):
        return None
    direct = data.get("nodeCount")
    if isinstance(direct, int):
        return direct
    for key in ("tree", "root", "lines"):
        nested = data.get(key)
        if isinstance(nested, dict) and isinstance(nested.get("nodeCount"), int):
            return nested["nodeCount"]
    return None


def parse_runtime_value(response: Any) -> JsonDict:
    data = response_data(response)
    candidates = [data]
    if isinstance(data, dict):
        candidates.extend(data.get(key) for key in ("value", "result", "returnValue"))
    for candidate in candidates:
        if isinstance(candidate, str):
            try:
                parsed = json.loads(candidate)
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, dict):
                return parsed
        if isinstance(candidate, dict) and "pcResistDetail" in candidate:
            return candidate
    return {"probeError": "Runtime.evaluate returned no parseable JSON", "rawData": data}


def contains_marker(value: Any, marker: str) -> bool:
    return marker in json.dumps(value, ensure_ascii=False)


def load_config(config_path: Path) -> Tuple[ABCPClientConfig, str]:
    raw = json.loads(config_path.read_text(encoding="utf-8"))
    browser_raw = raw.get("browser") or {}
    config = ABCPClientConfig.from_dict(browser_raw)
    config.call_timeout_seconds = max(config.call_timeout_seconds, 90.0)
    return config, browser_raw.get("agent_id") or "codex-tmall-render-probe"


async def settle(browser: ABCPClient, page_id: str, seconds: float = 5.0) -> JsonDict:
    await asyncio.sleep(seconds)
    return await browser.call(
        "Page.getState",
        {
            "pageId": page_id,
            "purpose": "Synchronize the controlled Tmall render probe after navigation or activation.",
        },
    )


async def reload_and_settle(browser: ABCPClient, page_id: str, label: str) -> JsonDict:
    await browser.call(
        "Page.reload",
        {
            "pageId": page_id,
            "ignoreCache": True,
            "purpose": f"Reload the same Tmall item after only the {label} condition changed.",
        },
    )
    return await settle(browser, page_id)


async def capture_stage(
    browser: ABCPClient,
    page_id: str,
    output_dir: Path,
    stage: str,
    state: Optional[JsonDict] = None,
) -> JsonDict:
    state_response = state or await settle(browser, page_id, seconds=1.0)
    runtime_response = await browser.call(
        "Runtime.evaluate",
        {
            "pageId": page_id,
            "expression": SSR_PROBE_EXPRESSION,
            "returnByValue": True,
            "purpose": (
                "Structured DOM methods cannot expose the inline Tmall SSR feature flags; "
                "read only the inline script text to correlate server risk classification with DOM trees."
            ),
        },
    )
    ax_response = await browser.call(
        "DOM.getAXTree",
        {
            "pageId": page_id,
            "purpose": "Measure the accessible page structure at this controlled render-probe stage.",
        },
    )
    semantic_response = await browser.call(
        "DOM.getSemanticTree",
        {
            "pageId": page_id,
            "includeShadowDom": True,
            "filterNoise": False,
            "purpose": (
                "AXTree omits Tmall comment bodies, so inspect the full semantic DOM to determine "
                "whether the review component mounted at this controlled stage."
            ),
        },
    )

    stage_dir = output_dir / stage
    stage_dir.mkdir(parents=True, exist_ok=True)
    (stage_dir / "state.json").write_text(
        json.dumps(state_response, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (stage_dir / "runtime_flags.json").write_text(
        json.dumps(runtime_response, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (stage_dir / "axtree.json").write_text(
        json.dumps(ax_response, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (stage_dir / "semantictree.json").write_text(
        json.dumps(semantic_response, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    flags = parse_runtime_value(runtime_response)
    return {
        "stage": stage,
        "capturedAt": int(time.time() * 1000),
        "state": response_data(state_response),
        "flags": flags,
        "axNodeCount": node_count(ax_response),
        "semanticNodeCount": node_count(semantic_response),
        "semanticHasViewAllReviews": contains_marker(semantic_response, "查看全部评价"),
        "semanticHasCommentClass": contains_marker(semantic_response, "Comment--"),
    }


def activate_app(application_name: str) -> JsonDict:
    completed = subprocess.run(
        ["osascript", "-e", f'tell application "{application_name}" to activate'],
        check=False,
        capture_output=True,
        text=True,
        timeout=15,
    )
    return {
        "command": f"activate {application_name}",
        "returnCode": completed.returncode,
        "stdout": completed.stdout.strip(),
        "stderr": completed.stderr.strip(),
    }


async def run(args: argparse.Namespace) -> int:
    config, configured_agent_id = load_config(args.config)
    agent_id = args.agent_id or f"{configured_agent_id}-tmall-render-probe"
    output_dir = args.output.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    summary: JsonDict = {
        "itemUrl": args.url,
        "agentId": agent_id,
        "startedAt": int(time.time() * 1000),
        "stages": [],
        "notes": [
            "The target page is the second tab, so Page.create leaves it inactive/offscreen for the baseline.",
            "System.revealPage is not Dispatcher-exposed; foregrounding uses macOS activation of ABCP.app.",
        ],
    }
    fleet_id: Optional[str] = None

    try:
        async with ABCPClient(config) as browser:
            await browser.call("System.register", {"agentId": agent_id})
            capabilities = await browser.call("System.getCapabilities", {"skillFile": False})
            summary["systemRevealPageExposed"] = contains_marker(capabilities, "System.revealPage")

            fleet_response = await browser.call(
                "Fleet.create", {"scope": f"tmall-render-probe:{int(time.time())}"}
            )
            fleet_data = response_data(fleet_response)
            fleet_id = fleet_data.get("fleetId") if isinstance(fleet_data, dict) else None
            if not fleet_id:
                raise RuntimeError(f"Fleet.create returned no fleetId: {fleet_response}")
            summary["fleetId"] = fleet_id

            blank_response = await browser.call(
                "Page.create", {"fleetId": fleet_id, "url": "about:blank"}
            )
            blank_data = response_data(blank_response)
            summary["blankPageId"] = blank_data.get("pageId") if isinstance(blank_data, dict) else None

            target_response = await browser.call(
                "Page.create", {"fleetId": fleet_id, "url": args.url}
            )
            target_data = response_data(target_response)
            page_id = target_data.get("pageId") if isinstance(target_data, dict) else None
            if not page_id:
                raise RuntimeError(f"Page.create returned no pageId: {target_response}")
            summary["pageId"] = page_id

            if args.force_background:
                summary["initialBackgroundActivation"] = activate_app("Finder")
                await asyncio.sleep(2.0)

            state = await settle(browser, page_id)
            summary["stages"].append(
                await capture_stage(browser, page_id, output_dir, "01_inactive_initial", state)
            )

            await browser.call(
                "Page.switchTo",
                {
                    "pageId": page_id,
                    "purpose": "Make the existing target tab active without changing Fleet, URL, cookies, or fingerprint.",
                },
            )
            summary["stages"].append(
                await capture_stage(browser, page_id, output_dir, "02_active_no_reload")
            )

            state = await reload_and_settle(browser, page_id, "active-tab")
            summary["stages"].append(
                await capture_stage(browser, page_id, output_dir, "03_active_after_reload", state)
            )

            if args.skip_os_focus:
                if args.force_background:
                    summary["backgroundActivationBeforeSecondReload"] = activate_app("Finder")
                summary["foregroundActivation"] = {
                    "skipped": True,
                    "reason": "No-focus control run",
                }
            else:
                summary["foregroundActivation"] = activate_app("ABCP")
            await asyncio.sleep(2.0)
            summary["stages"].append(
                await capture_stage(
                    browser,
                    page_id,
                    output_dir,
                    "04_no_focus_control" if args.skip_os_focus else "04_foreground_no_reload",
                )
            )

            state = await reload_and_settle(
                browser,
                page_id,
                "second-active-reload-without-OS-focus" if args.skip_os_focus else "OS-foreground",
            )
            summary["stages"].append(
                await capture_stage(
                    browser,
                    page_id,
                    output_dir,
                    "05_no_focus_after_second_reload"
                    if args.skip_os_focus
                    else "05_foreground_after_reload",
                    state,
                )
            )

            await browser.call(
                "Input.scroll",
                {
                    "pageId": page_id,
                    "direction": "down",
                    "amount": 900,
                    "purpose": "Inject one physical scroll while keeping Fleet, page, URL, cookies, and fingerprint fixed.",
                },
            )
            await asyncio.sleep(2.0)
            summary["stages"].append(
                await capture_stage(browser, page_id, output_dir, "06_after_physical_scroll")
            )

            state = await reload_and_settle(browser, page_id, "physical-input")
            summary["stages"].append(
                await capture_stage(browser, page_id, output_dir, "07_input_after_reload", state)
            )

            for index, comparison_url in enumerate(args.comparison_url, start=1):
                await browser.call(
                    "Page.navigate",
                    {
                        "pageId": page_id,
                        "url": comparison_url,
                        "purpose": (
                            "Compare another Tmall item while preserving the same Fleet, window, "
                            "cookies, fingerprint, and page activation state."
                        ),
                    },
                )
                state = await settle(browser, page_id)
                summary["stages"].append(
                    await capture_stage(
                        browser,
                        page_id,
                        output_dir,
                        f"{7 + index:02d}_comparison_item_{index}",
                        state,
                    )
                )

            if args.comparison_url:
                await browser.call(
                    "Page.navigate",
                    {
                        "pageId": page_id,
                        "url": args.url,
                        "purpose": "Return to the original item to test whether its risk flags remain stable.",
                    },
                )
                state = await settle(browser, page_id)
                summary["stages"].append(
                    await capture_stage(
                        browser,
                        page_id,
                        output_dir,
                        f"{8 + len(args.comparison_url):02d}_return_original_item",
                        state,
                    )
                )

            if not args.keep_fleet:
                await browser.call("Fleet.close", {"fleetId": fleet_id})
                summary["fleetClosed"] = True
                fleet_id = None
    except Exception as error:
        summary["error"] = f"{type(error).__name__}: {error}"
        summary["fleetLeftOpen"] = fleet_id
    finally:
        summary["finishedAt"] = int(time.time() * 1000)
        (output_dir / "summary.json").write_text(
            json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)

    return 1 if summary.get("error") else 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run a same-Fleet controlled experiment for Tmall detail rendering."
    )
    parser.add_argument("--url", default=DEFAULT_ITEM_URL)
    parser.add_argument("--config", type=Path, default=REPO_ROOT / "config.json")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--agent-id", default=None)
    parser.add_argument("--keep-fleet", action="store_true")
    parser.add_argument(
        "--comparison-url",
        action="append",
        default=[],
        help="Navigate additional item URLs in the same Fleet after the controlled stages.",
    )
    parser.add_argument(
        "--skip-os-focus",
        action="store_true",
        help="Run the same timing/reload sequence without activating ABCP.app.",
    )
    parser.add_argument(
        "--force-background",
        action="store_true",
        help="Activate Finder after opening the target and before the second reload.",
    )
    return asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
