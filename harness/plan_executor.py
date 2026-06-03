"""
harness.plan_executor - Deterministic ABCP plan execution, batch, HITL, and challenge handling.
"""

import asyncio
import copy
import json
from typing import Any, Callable, Dict, List, Optional, Set

from abcp_client import ABCPClient
from harness.config import RuntimeConfig
from harness.constants import CHALLENGE_KEYWORDS, NAVIGATION_CHALLENGE_TITLE_KEYWORDS
from harness.hitl import wait_for_hitl_resume
from harness.offload import offload_large_response_fields, strip_image_payload
from harness.render_recovery import (
    build_render_recovery_runner,
    extract_page_id_from_values,
)
from harness.schema_loader import load_capability_bundle
from harness.templates import render_templates
from harness.utils import (
    JsonDict,
    RunLogger,
    exception_payload,
    make_browser_event_logger,
    optional_int,
    trim_large_strings,
)


class ABCPPlanExecutor:
    def __init__(
        self,
        runtime: RuntimeConfig,
        logger: RunLogger,
        next_id: Callable[[str], str],
    ):
        self.runtime = runtime
        self.logger = logger
        self.next_id = next_id
        self.capability_methods: Set[str] = set()
        self.methods_requiring_purpose: Set[str] = set()
        self.purpose_hints: Dict[str, str] = {}
        self._purpose_capabilities_loaded = False
        self._render_recovery_recent: Dict[str, float] = {}

    async def execute_abcp_plan(
        self,
        steps: List[JsonDict],
        variables: Optional[JsonDict] = None,
        agent_name: Optional[str] = None,
        context: str = "",
    ) -> JsonDict:
        if not isinstance(steps, list):
            result = {"status": "failed", "error": "steps must be a JSON array"}
            self.logger.write("spawner.plan.failed", result)
            return result

        worker_id = self.next_id("plan")
        agent_id = f"{self.runtime.agent_id}-{worker_id}"
        return await self._execute_abcp_plan_worker(
            worker_id=worker_id,
            agent_id=agent_id,
            steps=steps,
            variables=variables,
            agent_name=agent_name,
            context=context,
        )

    async def run_abcp_plan_batch(
        self,
        items: List[Any],
        steps: List[JsonDict],
        variables: Optional[JsonDict] = None,
        context_template: str = "",
        concurrency: Optional[int] = None,
        validate_first_n: Optional[int] = None,
    ) -> JsonDict:
        if not isinstance(items, list):
            return {"status": "failed", "error": "items must be a JSON array"}
        if not isinstance(steps, list):
            return {"status": "failed", "error": "steps must be a JSON array"}

        default_concurrency = self.runtime.harness.default_worker_concurrency
        effective_concurrency = optional_int(concurrency, default_concurrency)
        if effective_concurrency is None:
            effective_concurrency = default_concurrency
        effective_concurrency = max(1, min(
            effective_concurrency,
            self.runtime.harness.max_browser_agents,
        ))
        validation_count = optional_int(validate_first_n, 0) or 0
        validation_count = max(0, min(validation_count, len(items)))
        semaphore = asyncio.Semaphore(effective_concurrency)
        base_variables = variables or {}

        async def run_one(index: int, raw_item: Any) -> JsonDict:
            item = raw_item if isinstance(raw_item, dict) else {"value": raw_item}
            result: JsonDict
            try:
                seed_variables = {"item": item, "index": index}
                rendered_variables = render_templates(
                    copy.deepcopy(base_variables),
                    seed_variables,
                )
                if isinstance(rendered_variables, dict):
                    run_variables = {
                        **rendered_variables,
                        "item": item,
                        "index": index,
                    }
                else:
                    run_variables = {
                        "item": item,
                        "index": index,
                        "variables": rendered_variables,
                    }
                context = str(render_templates(context_template, run_variables))
                worker_id = self.next_id("planbatch")
                agent_id = f"{self.runtime.agent_id}-{worker_id}"
                async with semaphore:
                    result = await self._execute_abcp_plan_worker(
                        worker_id=worker_id,
                        agent_id=agent_id,
                        steps=steps,
                        variables=run_variables,
                        agent_name=worker_id,
                        context=context,
                    )
            except Exception as exc:
                result = {
                    "status": "failed",
                    "error": str(exc),
                }
            result["index"] = index
            result["item"] = item
            return result

        normalized: List[JsonDict] = []
        validation_failed = False
        for index in range(validation_count):
            result = await run_one(index, items[index])
            normalized.append(result)
            if result.get("status") != "done":
                validation_failed = True
                break

        if not validation_failed:
            start_index = validation_count
            remaining_results = await asyncio.gather(
                *(
                    run_one(index, item)
                    for index, item in enumerate(items[start_index:], start=start_index)
                ),
                return_exceptions=True,
            )
            normalized.extend(
                result if isinstance(result, dict)
                else {"status": "failed", "error": str(result)}
                for result in remaining_results
            )

        normalized.sort(key=lambda item: optional_int(item.get("index"), 0) or 0)
        bucketed_results = [
            self._summarize_plan_batch_item(item)
            for item in normalized
        ]
        failed_items = [
            item for item in bucketed_results
            if item.get("status") != "done"
        ]
        failed_details = [
            self._summarize_plan_batch_failure(item)
            for item in normalized
            if item.get("status") != "done"
        ]
        status = "done"
        if validation_failed:
            first_failure_status = (
                failed_items[0].get("status")
                if failed_items else "failed"
            )
            status = (
                "validation_hitl_required"
                if str(first_failure_status).startswith("hitl")
                else "validation_failed"
            )
        elif failed_items:
            has_hitl = any(
                str(item.get("status")).startswith("hitl")
                for item in failed_items
            )
            status = "partial_hitl_required" if has_hitl else "partial_failed"

        attempted_count = len(normalized)
        skipped_count = len(items) - attempted_count
        result = {
            "status": status,
            "concurrency": effective_concurrency,
            "validate_first_n": validation_count,
            "count": len(items),
            "attempted_count": attempted_count,
            "skipped_count": skipped_count,
            "success_count": attempted_count - len(failed_items),
            "failed_count": len(failed_items),
            "results": bucketed_results,
            "failed_items": failed_items,
        }
        if failed_details:
            result["failed_details"] = failed_details
        self.logger.write(
            "spawner.plan_batch.result",
            trim_large_strings(result, 8000),
        )
        return result

    def _summarize_plan_batch_item(self, result: JsonDict) -> JsonDict:
        summary: JsonDict = {
            "index": result.get("index"),
            "item": result.get("item"),
            "status": result.get("status", "failed"),
        }
        for key in ("workerId", "agentId"):
            if result.get(key) is not None:
                summary[key] = result.get(key)
        if result.get("failed_step") is not None:
            summary["failed_step"] = result.get("failed_step")
        if result.get("error") is not None:
            summary["error"] = result.get("error")
        artifacts = result.get("artifacts")
        if artifacts:
            summary["artifacts"] = artifacts
        output = self._extract_plan_batch_output(result)
        if output is not None:
            summary["output"] = output
        return summary

    def _summarize_plan_batch_failure(self, result: JsonDict) -> JsonDict:
        return {
            "index": result.get("index"),
            "item": result.get("item"),
            "status": result.get("status", "failed"),
            "workerId": result.get("workerId"),
            "agentId": result.get("agentId"),
            "failed_step": result.get("failed_step"),
            "error": result.get("error"),
            "step_results": self._summarize_failed_step_results(
                result.get("results", [])
            ),
            "artifacts": result.get("artifacts", []),
        }

    def _summarize_failed_step_results(self, step_results: Any) -> List[JsonDict]:
        if not isinstance(step_results, list):
            return []

        latest_by_index: Dict[Any, JsonDict] = {}
        signal_by_index: Dict[Any, JsonDict] = {}
        order: List[Any] = []
        for step_result in step_results:
            if not isinstance(step_result, dict):
                continue
            index = step_result.get("index")
            if index not in latest_by_index:
                order.append(index)
            latest_by_index[index] = step_result
            if step_result.get("hitl") or step_result.get("challenge_probe"):
                signal_by_index[index] = step_result

        summarized: List[JsonDict] = []
        seen: Set[int] = set()
        for index in order:
            for candidate in (
                signal_by_index.get(index),
                latest_by_index.get(index),
            ):
                if candidate is None:
                    continue
                marker = id(candidate)
                if marker in seen:
                    continue
                seen.add(marker)
                trimmed = trim_large_strings(candidate, 6000)
                if isinstance(trimmed, dict):
                    summarized.append(trimmed)
        return summarized

    def _extract_plan_batch_output(self, result: JsonDict) -> Any:
        if result.get("status") != "done":
            return None
        step_results = result.get("results")
        if not isinstance(step_results, list):
            return None
        preferred_names = {"output", "result", "record", "data", "fields"}
        for step_result in reversed(step_results):
            if not isinstance(step_result, dict):
                continue
            if step_result.get("save_as") not in preferred_names:
                continue
            response = step_result.get("response")
            if isinstance(response, dict) and "data" in response:
                return trim_large_strings(response.get("data"), 4000)
            return trim_large_strings(response, 4000)
        return None

    def _detect_challenge(self, value: Any) -> Optional[str]:
        try:
            text = json.dumps(
                trim_large_strings(value, 12000),
                ensure_ascii=False,
                default=str,
            ).lower()
        except (TypeError, ValueError):
            text = str(value).lower()
        for keyword in CHALLENGE_KEYWORDS:
            if keyword.lower() in text:
                return f"Detected challenge keyword: {keyword}"
        return None

    def _detect_navigation_title_challenge(
        self,
        method: str,
        response: Any,
    ) -> Optional[str]:
        if method not in {"Page.navigate", "Page.create", "Page.open"}:
            return None
        title = self._extract_title(response)
        if not title:
            return None
        normalized = title.lower()
        for keyword in NAVIGATION_CHALLENGE_TITLE_KEYWORDS:
            if keyword.lower() in normalized:
                return f"Detected challenge title keyword: {keyword}"
        return None

    def _extract_title(self, value: Any) -> Optional[str]:
        stack = [value]
        while stack:
            current = stack.pop()
            if isinstance(current, dict):
                title = current.get("title")
                if isinstance(title, str) and title.strip():
                    return title
                for key in ("data", "result", "page", "state"):
                    nested = current.get(key)
                    if isinstance(nested, (dict, list)):
                        stack.append(nested)
            elif isinstance(current, list):
                stack.extend(current)
        return None

    def _is_page_paused_error(self, value: Any) -> bool:
        try:
            text = json.dumps(value, ensure_ascii=False, default=str).lower()
        except (TypeError, ValueError):
            text = str(value).lower()
        return "err_page_paused" in text or "currently paused" in text

    async def _request_pause_and_wait_for_hitl(
        self,
        browser: ABCPClient,
        page_id: str,
        reason: str,
        worker_id: str,
        step_index: int,
    ) -> JsonDict:
        # Hitl.requestPause requires `purpose` per current ABCP paramsSchema;
        # without it the proxied-method validator rejects the call.
        pause_response = await browser.call(
            "Hitl.requestPause",
            {
                "pageId": page_id,
                "reason": reason,
                "purpose": (
                    f"plan executor pause for worker {worker_id} step {step_index}"
                )[:200],
            },
        )
        if isinstance(pause_response, dict) and pause_response.get("error"):
            result = {
                "status": "hitl_pause_failed",
                "pageId": page_id,
                "reason": reason,
                "error": pause_response.get("error"),
                "pause_response": trim_large_strings(pause_response, 4000),
            }
            self.logger.write("spawner.plan.hitl", trim_large_strings(result, 8000))
            return result
        wait_result = await self._wait_for_hitl_resume(
            browser=browser,
            page_id=page_id,
            worker_id=worker_id,
            step_index=step_index,
        )
        result = {
            **wait_result,
            "pageId": page_id,
            "reason": reason,
            "pause_response": trim_large_strings(pause_response, 4000),
        }
        self.logger.write("spawner.plan.hitl", trim_large_strings(result, 8000))
        return result

    async def _wait_for_hitl_resume(
        self,
        browser: ABCPClient,
        page_id: str,
        worker_id: str,
        step_index: int,
    ) -> JsonDict:
        """Delegate to the shared notification-hub + Page.getState helper.

        The old implementation polled Hitl.getTaskSummary, which has a known
        server-side schema/routing bug (see repro_hitl_bug.py) and burns
        worker steps. harness.hitl.wait_for_hitl_resume uses System.notification
        as the primary signal with Page.getState as fallback — no broken APIs.
        """
        outcome = await wait_for_hitl_resume(
            browser=browser,
            page_id=page_id,
            timeout_seconds=self.runtime.harness.hitl_wait_timeout_seconds,
            poll_interval_seconds=self.runtime.harness.hitl_poll_interval_seconds,
            diagnostics=None,  # plan_executor does not track WorkerDiagnostics
            logger=self.logger,
        )
        if outcome.get("status") == "resumed":
            return {
                "status": "resumed",
                "workerId": worker_id,
                "step": step_index,
                "via": outcome.get("via"),
                "evidence": outcome.get("evidence"),
                "elapsedMs": outcome.get("elapsedMs"),
            }
        if outcome.get("status") == "page_settled_after_hitl":
            return {
                "status": "page_settled_after_hitl",
                "workerId": worker_id,
                "step": step_index,
                "via": outcome.get("via"),
                "evidence": outcome.get("evidence"),
                "elapsedMs": outcome.get("elapsedMs"),
                "reason": outcome.get("reason"),
            }
        return {
            "status": "hitl_timeout",
            "workerId": worker_id,
            "step": step_index,
            "timeout_seconds": self.runtime.harness.hitl_wait_timeout_seconds,
            "elapsedMs": outcome.get("elapsedMs"),
        }

    async def _probe_challenge_after_failure(
        self,
        browser: ABCPClient,
        page_id: str,
    ) -> Optional[JsonDict]:
        try:
            response = await browser.call("DOM.getAXTree", {"pageId": page_id})
        except Exception as exc:
            return {
                "status": "probe_failed",
                "error": str(exc),
            }
        reason = self._detect_challenge(response)
        if not reason:
            return None
        return {
            "status": "challenge_detected",
            "reason": reason,
            "response": trim_large_strings(response, 4000),
        }

    async def _ensure_purpose_capabilities(self, browser: ABCPClient) -> None:
        if self._purpose_capabilities_loaded:
            return
        bundle = await load_capability_bundle(browser, logger=self.logger)
        self.capability_methods = set(bundle.capability_methods)
        self.methods_requiring_purpose = set(bundle.methods_requiring_purpose)
        self.purpose_hints = dict(bundle.purpose_hints)
        self._purpose_capabilities_loaded = True

    def _ensure_plan_step_purpose(
        self,
        method: str,
        params: JsonDict,
        step: JsonDict,
        step_index: int,
        context: str,
        variables: JsonDict,
    ) -> None:
        if method not in self.methods_requiring_purpose:
            return
        purpose = params.get("purpose")
        if isinstance(purpose, str) and purpose.strip():
            return

        fallback = (
            step.get("purpose")
            or step.get("reason")
            or step.get("description")
            or context
            or self.purpose_hints.get(method)
            or f"plan step {step_index}: {method}"
        )
        rendered = render_templates(fallback, variables) if isinstance(fallback, str) else fallback
        params["purpose"] = str(rendered).strip() or f"plan step {step_index}: {method}"

    async def _execute_abcp_plan_worker(
        self,
        worker_id: str,
        agent_id: str,
        steps: List[JsonDict],
        variables: Optional[JsonDict],
        agent_name: Optional[str],
        context: str,
    ) -> JsonDict:
        run_variables = copy.deepcopy(variables or {})
        artifacts: List[str] = []
        event_logger = make_browser_event_logger(
            self.logger,
            self.runtime.harness.log_browser_payloads,
            prefix=f"{worker_id}.transport",
        )

        results = []
        try:
            async with ABCPClient(self.runtime.browser, on_event=event_logger) as browser:
                registration = await browser.call("System.register", {"agentId": agent_id})
                await self._ensure_purpose_capabilities(browser)
                recovery_runner = build_render_recovery_runner(
                    browser=browser,
                    logger=self.logger,
                    recent_recoveries=self._render_recovery_recent,
                    capability_methods=self.capability_methods,
                )
                run_variables["registration"] = registration
                run_variables["agent"] = {
                    "id": agent_id,
                    "name": agent_name or worker_id,
                    "context": context,
                }

                for index, step in enumerate(steps, start=1):
                    method = str(step.get("method", "")).strip()
                    if not method:
                        result = {
                            "status": "failed",
                            "workerId": worker_id,
                            "agentId": agent_id,
                            "failed_step": index,
                            "error": "step.method cannot be empty",
                            "results": results,
                        }
                        self.logger.write(
                            "spawner.plan.failed",
                            trim_large_strings(result, 8000),
                        )
                        return result
                    raw_params = step.get("params") or {}
                    if not isinstance(raw_params, dict):
                        raw_params = {"value": raw_params}
                    attempts = 0
                    max_retries = max(0, self.runtime.harness.hitl_max_step_retries)

                    while True:
                        params = render_templates(raw_params, run_variables)
                        self._ensure_plan_step_purpose(
                            method,
                            params,
                            step,
                            index,
                            context,
                            run_variables,
                        )
                        response, _recovery = await recovery_runner.call(method, params)
                        response = self._capture_plan_artifact(
                            worker_id,
                            method,
                            response,
                            artifacts,
                        )
                        response = self._offload_plan_response(
                            worker_id,
                            method,
                            params,
                            response,
                            index,
                        )
                        save_as = step.get("save_as") or f"step_{index}"
                        run_variables[str(save_as)] = response
                        step_result: JsonDict = {
                            "index": index,
                            "method": method,
                            "params": params,
                            "save_as": save_as,
                            "attempt": attempts + 1,
                            "response": trim_large_strings(
                                response,
                                self.runtime.harness.max_observation_chars,
                            ),
                        }
                        results.append(step_result)

                        challenge_reason = (
                            self._detect_navigation_title_challenge(method, response)
                            or self._detect_challenge(response)
                        )
                        paused_error = self._is_page_paused_error(response)
                        if challenge_reason or paused_error:
                            page_id = extract_page_id_from_values(
                                params,
                                response,
                                run_variables,
                                results,
                            )
                            if page_id and attempts < max_retries:
                                if paused_error:
                                    hitl_result = await self._wait_for_hitl_resume(
                                        browser=browser,
                                        page_id=page_id,
                                        worker_id=worker_id,
                                        step_index=index,
                                    )
                                else:
                                    hitl_result = await self._request_pause_and_wait_for_hitl(
                                        browser=browser,
                                        page_id=page_id,
                                        reason=challenge_reason
                                        or "Detected browser challenge",
                                        worker_id=worker_id,
                                        step_index=index,
                                    )
                                step_result["hitl"] = trim_large_strings(
                                    hitl_result,
                                    4000,
                                )
                                if hitl_result.get("status") == "resumed":
                                    attempts += 1
                                    continue
                                result = {
                                    "status": hitl_result.get(
                                        "status",
                                        "hitl_required",
                                    ),
                                    "workerId": worker_id,
                                    "agentId": agent_id,
                                    "pageId": page_id,
                                    "failed_step": index,
                                    "error": hitl_result.get("reason")
                                    or hitl_result.get("status"),
                                    "results": results,
                                    "artifacts": artifacts,
                                }
                                self.logger.write(
                                    "spawner.plan.hitl_failed",
                                    trim_large_strings(result, 8000),
                                )
                                return result

                            result = {
                                "status": "hitl_required",
                                "workerId": worker_id,
                                "agentId": agent_id,
                                "pageId": page_id,
                                "failed_step": index,
                                "error": challenge_reason
                                or "Page is paused for human intervention",
                                "results": results,
                                "artifacts": artifacts,
                            }
                            self.logger.write(
                                "spawner.plan.hitl_required",
                                trim_large_strings(result, 8000),
                            )
                            return result

                        if isinstance(response, dict) and response.get("error"):
                            page_id = extract_page_id_from_values(
                                params,
                                response,
                                run_variables,
                                results,
                            )
                            challenge_probe = (
                                await self._probe_challenge_after_failure(
                                    browser,
                                    page_id,
                                )
                                if page_id else None
                            )
                            if challenge_probe:
                                step_result["challenge_probe"] = trim_large_strings(
                                    challenge_probe,
                                    4000,
                                )
                            if (
                                challenge_probe
                                and challenge_probe.get("status") == "challenge_detected"
                            ):
                                if page_id and attempts < max_retries:
                                    hitl_result = await self._request_pause_and_wait_for_hitl(
                                        browser=browser,
                                        page_id=page_id,
                                        reason=str(challenge_probe.get("reason")),
                                        worker_id=worker_id,
                                        step_index=index,
                                    )
                                    step_result["hitl"] = trim_large_strings(
                                        hitl_result,
                                        4000,
                                    )
                                    if hitl_result.get("status") == "resumed":
                                        attempts += 1
                                        continue
                                    result = {
                                        "status": hitl_result.get(
                                            "status",
                                            "hitl_required",
                                        ),
                                        "workerId": worker_id,
                                        "agentId": agent_id,
                                        "pageId": page_id,
                                        "failed_step": index,
                                        "error": hitl_result.get("reason")
                                        or hitl_result.get("status"),
                                        "results": results,
                                        "artifacts": artifacts,
                                    }
                                    self.logger.write(
                                        "spawner.plan.hitl_failed",
                                        trim_large_strings(result, 8000),
                                    )
                                    return result
                                result = {
                                    "status": "hitl_required",
                                    "workerId": worker_id,
                                    "agentId": agent_id,
                                    "pageId": page_id,
                                    "failed_step": index,
                                    "error": challenge_probe.get("reason"),
                                    "results": results,
                                    "artifacts": artifacts,
                                }
                                self.logger.write(
                                    "spawner.plan.hitl_required",
                                    trim_large_strings(result, 8000),
                                )
                                return result
                            result = {
                                "status": "failed",
                                "workerId": worker_id,
                                "agentId": agent_id,
                                "failed_step": index,
                                "error": response.get("error"),
                                "results": results,
                                "artifacts": artifacts,
                            }
                            self.logger.write(
                                "spawner.plan.failed",
                                trim_large_strings(result, 8000),
                            )
                            return result
                        break
        except Exception as exc:
            result = {
                "status": "failed",
                "workerId": worker_id,
                "agentId": agent_id,
                "error": str(exc),
                "results": results,
                "artifacts": artifacts,
            }
            self.logger.write(
                "spawner.plan.error",
                trim_large_strings(
                    exception_payload(
                        exc,
                        workerId=worker_id,
                        agentId=agent_id,
                        results=results,
                        artifacts=artifacts,
                    ),
                    8000,
                ),
            )
            return result

        result = {
            "status": "done",
            "workerId": worker_id,
            "agentId": agent_id,
            "results": results,
            "artifacts": artifacts,
        }
        self.logger.write("spawner.plan.result", trim_large_strings(result, 8000))
        return result

    def _capture_plan_artifact(
        self,
        worker_id: str,
        method: str,
        response: JsonDict,
        artifacts: List[str],
    ) -> JsonDict:
        return strip_image_payload(
            logger=self.logger,
            method=method,
            response=response,
            artifacts=artifacts,
            prefix=worker_id,
        )

    def _offload_plan_response(
        self,
        worker_id: str,
        method: str,
        params: JsonDict,
        response: Any,
        step: int,
    ) -> Any:
        return offload_large_response_fields(
            logger=self.logger,
            method=method,
            params=params,
            response=response,
            step=step,
            prefix=worker_id,
            threshold_bytes=self.runtime.harness.offload_threshold_bytes,
        )
