#!/usr/bin/env python3
"""Single-Fleet live probe for route-sensitive Taobao detail suppression.

The probe enters an item through a real search-result anchor, verifies detail
regions with AXTree and SemanticTree, and restores the listing according to the
observed navigation shape.  It never enters credentials.  If Taobao presents a
real login surface, it requests HITL and leaves the Fleet open for the user.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple


REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT))

from abcp_client import ABCPClient  # noqa: E402
from runtime_config import ABCPClientConfig  # noqa: E402


JsonDict = Dict[str, Any]
DEFAULT_OUTPUT = REPO_ROOT / "worktree" / "taobao_route_recovery_live"
CANONICAL_ID_RE = re.compile(r"\[(\d+:-?\d+:\d+)\]")
LOGIN_URL_MARKERS = ("login.taobao.com", "passport.taobao.com", "login.tmall.com")
LOGIN_UI_MARKERS = (
    "密码登录",
    "短信登录",
    "扫码登录",
    "会员登录",
    "请登录后继续",
    "登录淘宝",
)
DETAIL_REGION_MARKERS = {
    "reviews": ("用户评价", "查看全部评价", "Comment--", "Comments--"),
    "specifications": ("参数信息", "产品参数", "规格参数"),
    "description": ("图文详情", "商品详情", "详情描述"),
}


def load_config(path: Path) -> Tuple[ABCPClientConfig, str]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    browser_raw = raw.get("browser") or {}
    config = ABCPClientConfig.from_dict(browser_raw)
    config.call_timeout_seconds = max(config.call_timeout_seconds, 90.0)
    return config, browser_raw.get("agent_id") or "codex-taobao-route-recovery"


def data(response: Any) -> Any:
    return response.get("data", response) if isinstance(response, dict) else response


def json_text(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def ax_lines(response: Any) -> List[str]:
    payload = data(response)
    lines = payload.get("lines") if isinstance(payload, dict) else None
    return [line for line in lines or [] if isinstance(line, str)]


def page_state(response: Any) -> JsonDict:
    payload = data(response)
    return payload if isinstance(payload, dict) else {}


def walk_dicts(value: Any) -> Iterable[JsonDict]:
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from walk_dicts(child)
    elif isinstance(value, list):
        for child in value:
            yield from walk_dicts(child)


def page_ids(response: Any) -> List[str]:
    found: List[str] = []
    for item in walk_dicts(data(response)):
        candidate = item.get("pageId")
        if isinstance(candidate, str) and candidate not in found:
            found.append(candidate)
    return found


def canonical_ids_for_links(lines: Iterable[str]) -> List[str]:
    candidates: List[str] = []
    for line in lines:
        if " link " not in line or "#" not in line:
            continue
        match = CANONICAL_ID_RE.search(line)
        if match and match.group(1) not in candidates:
            candidates.append(match.group(1))
    return candidates


def attribute_items(response: Any) -> List[JsonDict]:
    payload = data(response)
    items = payload.get("items") if isinstance(payload, dict) else None
    return [item for item in items or [] if isinstance(item, dict)]


def item_href(item: JsonDict) -> Optional[str]:
    info = item.get("info")
    attrs = info.get("attributes") if isinstance(info, dict) else None
    href = attrs.get("href") if isinstance(attrs, dict) else None
    return href if isinstance(href, str) and href else None


def find_semantic_text_id(response: Any, marker: str) -> Optional[str]:
    for item in walk_dicts(data(response)):
        if item.get("text") == marker and isinstance(item.get("id"), str):
            return item["id"]
    return None


def find_class_node(response: Any, class_fragment: str) -> Optional[JsonDict]:
    for item in walk_dicts(data(response)):
        class_name = item.get("className")
        if isinstance(class_name, str) and class_fragment in class_name:
            return item
    return None


def drawer_comment_count(response: Any) -> int:
    drawer = find_class_node(response, "Drawer--")
    if not drawer:
        return 0
    ids = {
        str(item.get("id"))
        for item in walk_dicts(drawer)
        if isinstance(item.get("className"), str)
        and "Comment--" in item["className"]
        and "Comments--" not in item["className"]
    }
    return len(ids)


def drawer_has_skeleton(response: Any) -> bool:
    drawer = find_class_node(response, "Drawer--")
    if not drawer:
        return False
    return drawer_comment_count(response) == 0 and any(
        item.get("tag") == "img" for item in walk_dicts(drawer)
    )


def login_required(state: JsonDict, lines: Iterable[str]) -> bool:
    url = str(state.get("url") or "").lower()
    if any(marker in url for marker in LOGIN_URL_MARKERS):
        return True
    joined = "\n".join(lines)
    return any(marker in joined for marker in LOGIN_UI_MARKERS)


def detail_regions(ax_response: Any, semantic_response: Any) -> JsonDict:
    evidence = json_text({"ax": data(ax_response), "semantic": data(semantic_response)})
    return {
        name: {
            "present": any(marker in evidence for marker in markers),
            "matchedMarkers": [marker for marker in markers if marker in evidence],
        }
        for name, markers in DETAIL_REGION_MARKERS.items()
    }


async def settle(browser: ABCPClient, page_id: str, purpose: str, delay: float = 5.0) -> JsonDict:
    await asyncio.sleep(delay)
    return await browser.call("Page.getState", {"pageId": page_id, "purpose": purpose})


async def capture_tree_pair(
    browser: ABCPClient, page_id: str, output: Path, stage: str
) -> Tuple[JsonDict, JsonDict]:
    ax = await browser.call(
        "DOM.getAXTree",
        {
            "pageId": page_id,
            "purpose": f"Refresh actionable targets and page structure for live stage {stage}.",
        },
    )
    semantic = await browser.call(
        "DOM.getSemanticTree",
        {
            "pageId": page_id,
            "includeShadowDom": True,
            "filterNoise": False,
            "purpose": (
                "AXTree can omit Taobao review bodies; verify full local detail-region "
                f"materialization for live stage {stage}."
            ),
        },
    )
    output.mkdir(parents=True, exist_ok=True)
    (output / f"{stage}_axtree.json").write_text(
        json.dumps(ax, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (output / f"{stage}_semantictree.json").write_text(
        json.dumps(semantic, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return ax, semantic


async def request_login(
    browser: ABCPClient, page_id: str, summary: JsonDict, output: Path
) -> int:
    pause = await browser.call(
        "Hitl.requestPause",
        {
            "pageId": page_id,
            "reason": "请在当前淘宝页面完成账号登录，完成后在 ABCP 中恢复任务。",
            "purpose": "Taobao presented a concrete login surface that blocks the search-to-detail live verification.",
        },
    )
    summary.update({"status": "login_required", "loginPageId": page_id, "hitl": pause})
    (output / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return 2


async def run(args: argparse.Namespace) -> int:
    config, configured_agent = load_config(args.config)
    agent_id = args.agent_id or f"{configured_agent}-taobao-route-recovery"
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    # Pass the human-readable query to ABCP. Pre-percent-encoding here is
    # encoded again by the browser navigation layer (`%` -> `%25`).
    search_url = args.search_url or f"https://s.taobao.com/search?q={args.query}"
    summary: JsonDict = {
        "status": "starting",
        "agentId": agent_id,
        "searchUrl": search_url,
        "query": args.query,
        "startedAt": int(time.time() * 1000),
        "singleFleetInvariant": True,
    }
    fleet_id: Optional[str] = args.fleet_id
    source_page_id: Optional[str] = args.page_id

    try:
        async with ABCPClient(config) as browser:
            registration = await browser.call("System.register", {"agentId": agent_id})
            summary["registration"] = data(registration)

            if not fleet_id:
                created = await browser.call(
                    "Fleet.create", {"scope": f"taobao-route-recovery:{int(time.time())}"}
                )
                fleet_id = data(created).get("fleetId")
                if not fleet_id:
                    raise RuntimeError(f"Fleet.create returned no fleetId: {created}")
            summary["fleetId"] = fleet_id

            if not source_page_id:
                created_page = await browser.call(
                    "Page.create", {"fleetId": fleet_id, "url": search_url}
                )
                source_page_id = data(created_page).get("pageId")
                if not source_page_id:
                    raise RuntimeError(f"Page.create returned no pageId: {created_page}")
            summary["sourcePageId"] = source_page_id

            source_state_response = await settle(
                browser, source_page_id, "Synchronize the Taobao search listing before selecting a product anchor."
            )
            source_state = page_state(source_state_response)
            source_ax = await browser.call(
                "DOM.getAXTree",
                {
                    "pageId": source_page_id,
                    "purpose": "Locate real product anchors on the Taobao search listing.",
                },
            )
            (output / "source_state.json").write_text(
                json.dumps(source_state_response, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            (output / "source_axtree.json").write_text(
                json.dumps(source_ax, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            if login_required(source_state, ax_lines(source_ax)):
                return await request_login(browser, source_page_id, summary, output)

            before_pages_response = await browser.call("Page.list", {"fleetId": fleet_id})
            before_pages = page_ids(before_pages_response)
            if args.detail_page_id:
                detail_page_id = args.detail_page_id
                navigation_kind = "new_tab"
                summary["resumedExistingDetailTab"] = True
                await settle(
                    browser,
                    detail_page_id,
                    "Synchronize the already-open detail tab after its lifecycle reached idle.",
                    delay=1.0,
                )
                await browser.call(
                    "Page.switchTo",
                    {
                        "pageId": detail_page_id,
                        "purpose": "Inspect the already-open detail tab without clicking the listing anchor again.",
                    },
                )
            else:
                link_ids = canonical_ids_for_links(ax_lines(source_ax))
                if not link_ids and args.page_id:
                    # A pre-fix paused probe may carry a doubly percent-encoded
                    # query. After the human resumes login, repair that same page
                    # rather than creating another Fleet or tab.
                    await browser.call(
                        "Page.navigate",
                        {
                            "pageId": source_page_id,
                            "url": search_url,
                            "tag": "taobao-search-source",
                            "purpose": "Restore the intended Taobao search listing in the existing post-login source page.",
                        },
                    )
                    source_state_response = await settle(
                        browser,
                        source_page_id,
                        "Synchronize the existing listing page after repairing its search URL.",
                    )
                    source_state = page_state(source_state_response)
                    source_ax = await browser.call(
                        "DOM.getAXTree",
                        {
                            "pageId": source_page_id,
                            "purpose": "Refresh product anchors after restoring the intended search query.",
                        },
                    )
                    if login_required(source_state, ax_lines(source_ax)):
                        return await request_login(browser, source_page_id, summary, output)
                    link_ids = canonical_ids_for_links(ax_lines(source_ax))
                if not link_ids:
                    summary["status"] = "no_listing_anchors"
                    raise RuntimeError("Search page AXTree exposed no actionable link anchors")

                attributes = await browser.call(
                    "DOM.getAttribute",
                    {
                        "pageId": source_page_id,
                        "targets": [{"id": target_id} for target_id in link_ids],
                        "attributes": "href",
                        "purpose": "Identify which listing anchors lead to Taobao or Tmall item detail pages.",
                    },
                )
                selected_id: Optional[str] = None
                selected_href: Optional[str] = None
                for target_id, item in zip(link_ids, attribute_items(attributes)):
                    href = item_href(item)
                    if href and ("item.htm" in href or "detail.tmall.com" in href):
                        selected_id, selected_href = target_id, href
                        break
                if not selected_id:
                    summary["status"] = "no_product_anchor"
                    raise RuntimeError("Search page links contained no recognized item detail href")
                summary["selectedAnchor"] = {"id": selected_id, "href": selected_href}

                await browser.call(
                    "Input.click",
                    {
                        "pageId": source_page_id,
                        "id": selected_id,
                        "clickCount": 1,
                        "purpose": "Open one product through its real Taobao search-result anchor to preserve route context.",
                    },
                )
                await asyncio.sleep(6.0)
                after_pages_response = await browser.call("Page.list", {"fleetId": fleet_id})
                after_pages = page_ids(after_pages_response)
                new_pages = [page_id for page_id in after_pages if page_id not in before_pages]
                if new_pages:
                    detail_page_id = new_pages[-1]
                    navigation_kind = "new_tab"
                    await settle(
                        browser,
                        detail_page_id,
                        "Synchronize the detail tab opened by the listing anchor before switching to it.",
                        delay=1.0,
                    )
                    await browser.call(
                        "Page.switchTo",
                        {
                            "pageId": detail_page_id,
                            "purpose": "Inspect the detail tab opened by the listing anchor.",
                        },
                    )
                else:
                    detail_page_id = source_page_id
                    navigation_kind = "same_tab"
            summary.update({"navigationKind": navigation_kind, "detailPageId": detail_page_id})

            detail_state_response = await settle(
                browser, detail_page_id, "Synchronize the product page after the listing-anchor click.", delay=2.0
            )
            detail_state = page_state(detail_state_response)
            detail_ax, detail_semantic = await capture_tree_pair(
                browser, detail_page_id, output, "detail_after_click"
            )
            # Input.click can auto-scroll, but review modules are independently lazy.
            if not any(marker in json_text(data(detail_semantic)) for marker in DETAIL_REGION_MARKERS["reviews"]):
                await browser.call(
                    "Input.scroll",
                    {
                        "pageId": detail_page_id,
                        "direction": "down",
                        "amount": 1200,
                        "purpose": "Materialize lazy detail and review regions after route-correct navigation.",
                    },
                )
                await asyncio.sleep(3.0)
                detail_ax, detail_semantic = await capture_tree_pair(
                    browser, detail_page_id, output, "detail_after_scroll"
                )
            summary["detailState"] = detail_state
            summary["detailRegions"] = detail_regions(detail_ax, detail_semantic)

            view_all_id = find_semantic_text_id(detail_semantic, "查看全部评价")
            drawer_open = find_class_node(detail_semantic, "Drawer--") is not None
            if args.drawer_open and drawer_open:
                summary["reviewDrawer"] = {"clicked": False, "resumedOpenDrawer": True}
            elif view_all_id:
                await browser.call(
                    "Input.click",
                    {
                        "pageId": detail_page_id,
                        "id": view_all_id,
                        "clickCount": 1,
                        "purpose": "Open the complete review drawer from the rendered review preview.",
                    },
                )
                await asyncio.sleep(3.0)
                summary["reviewDrawer"] = {"clicked": True}
            else:
                summary["reviewDrawer"] = {"clicked": False, "reason": "control_not_materialized"}

            if summary["reviewDrawer"].get("clicked") or summary["reviewDrawer"].get("resumedOpenDrawer"):
                materialization_attempts: List[JsonDict] = []
                drawer_ax: JsonDict = {}
                drawer_semantic: JsonDict = {}
                for attempt in range(1, args.drawer_scroll_attempts + 1):
                    drawer_ax, drawer_semantic = await capture_tree_pair(
                        browser, detail_page_id, output, f"reviews_drawer_{attempt:02d}"
                    )
                    comment_count = drawer_comment_count(drawer_semantic)
                    attempt_result = {
                        "attempt": attempt,
                        "commentCount": comment_count,
                        "skeleton": drawer_has_skeleton(drawer_semantic),
                    }
                    materialization_attempts.append(attempt_result)
                    if comment_count >= args.target_comments:
                        break
                    await browser.call(
                        "Input.scroll",
                        {
                            "pageId": detail_page_id,
                            "selector": "[class*='detailContentClassName--']",
                            "direction": "down",
                            "amount": 600,
                            "purpose": (
                                "Materialize additional review cards inside the open drawer; "
                                f"currently observed {comment_count}/{args.target_comments}."
                            ),
                        },
                    )
                    await asyncio.sleep(2.0)
                final_count = materialization_attempts[-1]["commentCount"]
                summary["reviewDrawer"].update(
                    {
                        "semanticContainsCommentClass": final_count > 0,
                        "axContainsReviewText": "用户评价" in json_text(data(drawer_ax)),
                        "targetComments": args.target_comments,
                        "commentCount": final_count,
                        "materializationAttempts": materialization_attempts,
                        "materializationStatus": (
                            "complete"
                            if final_count >= args.target_comments
                            else "materialization_exhausted"
                        ),
                    }
                )

            if navigation_kind == "new_tab":
                await browser.call(
                    "Page.switchTo",
                    {
                        "pageId": source_page_id,
                        "purpose": "Return focus to the unchanged Taobao search listing after inspecting the new detail tab.",
                    },
                )
            else:
                await browser.call(
                    "Page.go",
                    {
                        "pageId": source_page_id,
                        "direction": "back",
                        "n": 1,
                        "purpose": "Restore the Taobao search listing through browser history after same-tab detail navigation.",
                    },
                )
                await settle(browser, source_page_id, "Synchronize the restored listing after Page.go(back).")
            restored_state = await browser.call(
                "Page.getState",
                {
                    "pageId": source_page_id,
                    "purpose": "Verify source listing identity after navigation-shape-specific restoration.",
                },
            )
            restored_ax = await browser.call(
                "DOM.getAXTree",
                {
                    "pageId": source_page_id,
                    "purpose": "Refresh listing targets after returning from the inspected detail page.",
                },
            )
            summary["restoredState"] = page_state(restored_state)
            summary["restoredListingHasLinks"] = bool(canonical_ids_for_links(ax_lines(restored_ax)))
            summary["status"] = (
                "materialization_exhausted"
                if summary.get("reviewDrawer", {}).get("materializationStatus")
                == "materialization_exhausted"
                else "complete"
            )

            if not args.keep_fleet:
                await browser.call("Fleet.close", {"fleetId": fleet_id})
                summary["fleetClosed"] = True
    except Exception as error:
        summary["error"] = f"{type(error).__name__}: {error}"
        summary["status"] = summary.get("status") if summary.get("status") != "starting" else "failed"
        summary["fleetLeftOpen"] = fleet_id
    finally:
        summary["finishedAt"] = int(time.time() * 1000)
        (output / "summary.json").write_text(
            json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)

    if summary.get("status") == "login_required":
        return 2
    return 0 if summary.get("status") == "complete" else 1


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Verify Taobao route-sensitive content recovery in exactly one Fleet."
    )
    parser.add_argument("--query", default="充电宝")
    parser.add_argument("--search-url", default=None)
    parser.add_argument("--config", type=Path, default=REPO_ROOT / "config.json")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--agent-id", default=None)
    parser.add_argument("--fleet-id", default=None, help="Resume an existing single Fleet.")
    parser.add_argument("--page-id", default=None, help="Resume its existing listing page.")
    parser.add_argument(
        "--detail-page-id",
        default=None,
        help="Resume an already-open detail tab without clicking its listing anchor again.",
    )
    parser.add_argument(
        "--drawer-open",
        action="store_true",
        help="Resume an already-open review drawer instead of clicking its trigger again.",
    )
    parser.add_argument("--target-comments", type=int, default=20)
    parser.add_argument("--drawer-scroll-attempts", type=int, default=5)
    parser.add_argument("--keep-fleet", action="store_true")
    return asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
