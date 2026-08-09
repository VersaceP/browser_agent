#!/usr/bin/env python3
"""Run the production collect_items composite against one existing ABCP page.

This is a deterministic live-canary entry point: it bypasses Lead/worker LLM
planning but does not replace any browser or persistence implementation.  The
script calls the same ``_collect_items`` and ``_record_extraction`` code used by
BrowserAgent, against a user-selected existing Fleet/Page.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict


REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT))

from abcp_client import ABCPClient  # noqa: E402
from harness.challenge_detector import ChallengeTracker  # noqa: E402
from harness.content_completeness import ContentCompletenessTracker  # noqa: E402
from harness.offload import offload_large_response_fields  # noqa: E402
from harness.render_recovery import build_render_recovery_runner  # noqa: E402
from harness.tools.browser_tools import _collect_items  # noqa: E402
from harness.utils import RunLogger  # noqa: E402
from runtime_config import ABCPClientConfig, HarnessConfig  # noqa: E402


JsonDict = Dict[str, Any]
DEFAULT_SELECTOR = (
    "[class*='comments--'] [class*='Comment--'] "
    "[class*='contentWrapper--']"
)
DEFAULT_CONTAINER_SELECTOR = "[class*='comments--']"


def _load_config(path: Path) -> tuple[ABCPClientConfig, HarnessConfig]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    browser = ABCPClientConfig.from_dict(raw.get("browser") or {})
    browser.call_timeout_seconds = max(browser.call_timeout_seconds, 90.0)
    return browser, HarnessConfig.from_dict(raw.get("harness") or {})


def _response_data(response: Any) -> JsonDict:
    if not isinstance(response, dict):
        return {}
    data = response.get("data", response)
    return data if isinstance(data, dict) else {}


def _agent(
    *,
    browser: ABCPClient,
    logger: RunLogger,
    harness_config: HarnessConfig,
    worker_contract: JsonDict,
    agent_id: str,
) -> Any:
    runtime = SimpleNamespace(agent_id=agent_id, harness=harness_config)
    capability_methods = {
        "DOM.getAXTree",
        "DOM.getSemanticTree",
        "DOM.getText",
        "Runtime.evaluate",
        "Page.getState",
        "Input.scroll",
        "Input.click",
        "Input.press",
    }
    agent = SimpleNamespace(
        browser=browser,
        logger=logger,
        runtime=runtime,
        capability_methods=capability_methods,
        _render_recovery_recent={},
        method_schemas={},
        diagnostics=SimpleNamespace(observe_browser_call=lambda *args, **kwargs: None),
        trace=[],
        artifacts=[],
        extraction_attempt_artifacts=[],
        pending_unrecorded_extraction=None,
        _capture_artifacts=lambda method, response: response,
        _offload_response=lambda method, params, response, step: (
            offload_large_response_fields(
                logger=logger,
                method=method,
                params=params,
                response=response,
                step=step,
                prefix=agent_id,
                threshold_bytes=harness_config.offload_threshold_bytes,
            )
        ),
        _trim_for_log=lambda value: value,
        _clean_for_model=lambda value: value,
        _trim_for_model=lambda value: value,
        challenge_tracker=ChallengeTracker(),
        challenge_adjudicating=False,
        hitl_no_repause_until=0.0,
        axtree_epoch=0,
        axtree_ids=set(),
        axtree_page_id="",
        axtree_invalidated=False,
        axtree_lines=[],
        axtree_nodes=[],
        axtree_event_serial=0,
        worker_contract=worker_contract,
        worker_id="collect-items-live-canary",
        content_completeness_tracker=ContentCompletenessTracker(
            worker_contract["content_completeness"]
        ),
    )
    agent.render_recovery_runner = build_render_recovery_runner(
        browser=browser,
        logger=logger,
        capability_methods=capability_methods,
        recent_recoveries=agent._render_recovery_recent,
    )
    return agent


async def run(args: argparse.Namespace) -> int:
    browser_config, harness_config = _load_config(args.config)
    if args.ws_url:
        browser_config.ws_url = str(args.ws_url)
    logger = RunLogger(
        str(args.output.resolve().parent),
        task_id=args.output.resolve().name,
    )
    extraction_dir = logger.artifacts_dir / "extractions"
    extraction_dir.mkdir(parents=True, exist_ok=True)

    expected_artifact: JsonDict = {
        "name": args.record_name,
        "exact_rows": 1,
        "fields": [
            {"name": "productName", "type": "string"},
            {
                "name": "reviews",
                "type": "array",
                "items": {"required": ["reviewText"]},
            },
        ],
        "required_fields": ["productName", "reviews"],
    }
    worker_contract: JsonDict = {
        "phase_id": "stage4b_collect_items_live_canary",
        "task_type": "web_scrape",
        "expected_artifact": expected_artifact,
        "validators": [
            {"type": "exact_rows", "value": 1},
            {"type": "required_fields", "fields": ["productName", "reviews"]},
            {"type": "field_nonempty", "fields": ["productName"]},
        ],
        "content_completeness": {
            "shell_markers": ["商品详情", "用户评价"],
            "expected_regions": [
                {
                    "id": "reviews",
                    "name": "reviews",
                    "fields": ["reviews"],
                    "markers": ["用户评价", "全部评价"],
                    "min_records": args.target_count,
                }
            ],
        },
    }

    summary: JsonDict = {
        "status": "starting",
        "fleetId": args.fleet_id,
        "pageId": args.page_id,
        "targetCount": args.target_count,
        "selector": args.selector,
        "containerSelector": args.container_selector,
        "recordName": args.record_name,
    }
    try:
        async with ABCPClient(browser_config) as browser:
            registration = await browser.call(
                "System.register", {"agentId": args.agent_id}
            )
            summary["registration"] = _response_data(registration)
            page_state_response = await browser.call(
                "Page.getState",
                {
                    "pageId": args.page_id,
                    "purpose": (
                        "Confirm the pinned detail page before the production "
                        "collect_items live canary."
                    ),
                },
            )
            page_state = _response_data(page_state_response)
            product_name = str(page_state.get("title") or "").strip()
            if not product_name:
                raise RuntimeError("Page.getState returned no product title")

            source_path = extraction_dir / "trusted_product_base.json"
            source_path.write_text(
                json.dumps(
                    {
                        "name": "trusted_product_base",
                        "rows": [{"productName": product_name}],
                        "schema": {
                            "source": "Page.getState",
                            "pageId": args.page_id,
                        },
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            (logger.task_dir / "task_state.json").write_text(
                json.dumps(
                    {
                        "artifacts": [str(source_path.resolve())],
                        "phases": {},
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )

            agent = _agent(
                browser=browser,
                logger=logger,
                harness_config=harness_config,
                worker_contract=worker_contract,
                agent_id=args.agent_id,
            )
            collection_params = {
                "selector": args.selector,
                "containerSelector": args.container_selector,
                "mode": "scroll",
                "direction": "down",
                "amount": 600,
                "targetCount": args.target_count,
                "maxRounds": 8,
                "maxDurationMs": 120000,
                "settleMs": 600,
                "fields": {"reviewText": "text"},
                "keyField": "reviewText",
                "regionId": "reviews",
                "recordName": args.record_name,
                "collectionField": "reviews",
            }
            result = await _collect_items(
                agent,
                {
                    "pageId": args.page_id,
                    **collection_params,
                    "baseRowRef": {
                        "savedPath": str(source_path.resolve()),
                        "rowIndex": 0,
                    },
                },
                step=1,
            )
            summary["collectItems"] = result
            record = result.get("recordExtraction")
            clean_record = (
                isinstance(record, dict)
                and str(record.get("status") or "") == "done"
                and bool(record.get("savedPath"))
                and not result.get("contractWarning")
            )
            summary["status"] = (
                "complete"
                if result.get("collectionState")
                in {"target_reached", "explicitly_exhausted"}
                and int(result.get("rowCount") or 0) >= args.target_count
                and clean_record
                else "failed"
            )
    except Exception as exc:
        summary["status"] = "failed"
        summary["error"] = f"{type(exc).__name__}: {exc}"
        if exc.__cause__ is not None:
            summary["errorCause"] = (
                f"{type(exc.__cause__).__name__}: {exc.__cause__}"
            )

    summary_path = logger.task_dir / "summary.json"
    summary_path.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if summary["status"] == "complete" else 1


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run production collect_items on one existing ABCP detail page."
    )
    parser.add_argument("--config", type=Path, default=REPO_ROOT / "config.json")
    parser.add_argument(
        "--ws-url",
        default="",
        help="Optional ABCP WebSocket override (for example ws://127.0.0.1:9300/ws).",
    )
    parser.add_argument("--fleet-id", required=True)
    parser.add_argument("--page-id", required=True)
    parser.add_argument("--agent-id", default="collect-items-live-canary")
    parser.add_argument("--target-count", type=int, default=20)
    parser.add_argument("--record-name", default="product_reviews_canary_live")
    parser.add_argument("--selector", default=DEFAULT_SELECTOR)
    parser.add_argument("--container-selector", default=DEFAULT_CONTAINER_SELECTOR)
    parser.add_argument(
        "--output",
        type=Path,
        default=REPO_ROOT / "worktree" / "stage4b_collect_items_live_canary",
    )
    return asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
