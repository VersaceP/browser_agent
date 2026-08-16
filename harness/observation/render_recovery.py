"""
harness.observation.render_recovery - Render context loss detection and recovery for ABCP calls.
"""

import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Set, Tuple

from abcp_client import ABCPClient, ABCPTransportError
from harness.constants import (
    ACTION_METHODS,
    ANCHOR_PARAM_KEYS,
    READ_METHODS_RETRY_AFTER_NAVIGATE,
    RENDER_LOST_MARKERS,
    RENDER_RECOVERY_METHODS,
    RENDER_RECOVERY_WINDOW_SECONDS,
)
from harness.tool_policy import mask_params
from harness.utils import JsonDict, RunLogger, trim_large_strings


@dataclass
class RenderRecoveryOutcome:
    outcome: str
    strategy: str = "none"
    recovered: bool = False
    dom_invalidated: bool = False
    elapsed_ms: int = 0
    page_id: Optional[str] = None
    url: Optional[str] = None
    reason: Optional[str] = None
    recently_recovered: bool = False

    def to_dict(self) -> JsonDict:
        return {
            "outcome": self.outcome,
            "strategy": self.strategy,
            "recovered": self.recovered,
            "dom_invalidated": self.dom_invalidated,
            "elapsed_ms": self.elapsed_ms,
            "pageId": self.page_id,
            "url": self.url,
            "reason": self.reason,
            "recently_recovered": self.recently_recovered,
        }


class RenderRecoveryRunner:
    def __init__(
        self,
        *,
        browser: ABCPClient,
        logger: RunLogger,
        capability_methods: Set[str],
        recent_recoveries: Optional[Dict[str, float]] = None,
    ):
        self.browser = browser
        self.logger = logger
        self.capability_methods = capability_methods
        self.recent_recoveries = recent_recoveries if recent_recoveries is not None else {}

    async def call(
        self,
        method: str,
        params: JsonDict,
        *,
        redact_params: Optional[Set[str]] = None,
    ) -> Tuple[JsonDict, Optional[RenderRecoveryOutcome]]:
        return await call_with_render_recovery(
            browser=self.browser,
            logger=self.logger,
            recent_recoveries=self.recent_recoveries,
            capability_methods=self.capability_methods,
            method=method,
            params=params,
            redact_params=redact_params,
        )


def build_render_recovery_runner(
    *,
    browser: ABCPClient,
    logger: RunLogger,
    capability_methods: Set[str],
    recent_recoveries: Optional[Dict[str, float]] = None,
) -> RenderRecoveryRunner:
    return RenderRecoveryRunner(
        browser=browser,
        logger=logger,
        capability_methods=capability_methods,
        recent_recoveries=recent_recoveries,
    )


def _collect_observation_error_text(value: Any) -> List[str]:
    texts: List[str] = []
    stack = [value]
    while stack:
        current = stack.pop()
        if isinstance(current, dict):
            for key, item in current.items():
                if key in {"observation", "error", "message"} and isinstance(item, str):
                    texts.append(item)
                elif isinstance(item, (dict, list)):
                    stack.append(item)
        elif isinstance(current, list):
            stack.extend(current)
        elif isinstance(current, str):
            texts.append(current)
    return texts


def detect_render_lost(value: Any) -> Optional[str]:
    for text in _collect_observation_error_text(value):
        for marker in RENDER_LOST_MARKERS:
            if marker in text:
                return marker
    return None


def extract_page_id_from_values(*values: Any) -> Optional[str]:
    stack = list(values)
    while stack:
        value = stack.pop()
        if isinstance(value, dict):
            for key in ("pageId", "page_id"):
                candidate = value.get(key)
                if isinstance(candidate, str) and candidate:
                    return candidate
            stack.extend(value.values())
        elif isinstance(value, list):
            stack.extend(value)
    return None


def extract_url_from_values(*values: Any) -> Optional[str]:
    stack = list(values)
    while stack:
        value = stack.pop()
        if isinstance(value, dict):
            candidate = value.get("url")
            if isinstance(candidate, str) and candidate:
                return candidate
            stack.extend(value.values())
        elif isinstance(value, list):
            stack.extend(value)
    return None


def uses_dom_anchor(params: JsonDict) -> bool:
    return any(params.get(key) is not None for key in ANCHOR_PARAM_KEYS)


def can_retry_after_render_recovery(
    method: str,
    params: JsonDict,
    recovery: RenderRecoveryOutcome,
) -> bool:
    if not recovery.recovered:
        return False
    if recovery.dom_invalidated:
        return method in READ_METHODS_RETRY_AFTER_NAVIGATE
    if method in ACTION_METHODS and uses_dom_anchor(params):
        return False
    return True


def build_render_recovery_advisory(
    method: str,
    params: JsonDict,
    original: Any,
    recovery: RenderRecoveryOutcome,
) -> JsonDict:
    if recovery.dom_invalidated:
        observation = (
            "Page was re-mounted after render loss; AXTree IDs from previous "
            "calls are now invalid. Re-fetch DOM.getAXTree before retrying actions."
        )
    elif recovery.recently_recovered:
        observation = (
            "Render context was lost again shortly after a recent recovery. "
            "The harness skipped another automatic recovery to avoid repeated reloads."
        )
    elif recovery.recovered:
        observation = (
            "Render context was recovered without re-mounting the page, but the "
            "previous action was not retried automatically. Re-fetch DOM.getAXTree "
            "before retrying anchored actions."
        )
    else:
        observation = (
            "Render context was lost and the harness could not recover automatically. "
            "Re-check the page state before retrying."
        )
    return {
        "observation": observation,
        "suggested_prompt": "Call Page.getState and DOM.getAXTree before retrying the action.",
        "error": "render_context_recovered_reinspect_required"
        if recovery.recovered else "render_context_lost",
        "recovered": recovery.recovered,
        "dom_invalidated": recovery.dom_invalidated,
        "selectors_should_be_refetched": True,
        "url": recovery.url,
        "previous_action": {
            "method": method,
            "params": trim_large_strings(params, 4000),
        },
        "original_response": trim_large_strings(original, 4000),
        "render_recovery": recovery.to_dict(),
    }


async def call_with_render_recovery(
    *,
    browser: ABCPClient,
    logger: RunLogger,
    recent_recoveries: Dict[str, float],
    capability_methods: Set[str],
    method: str,
    params: JsonDict,
    redact_params: Optional[Set[str]] = None,
) -> Tuple[JsonDict, Optional[RenderRecoveryOutcome]]:
    started = time.monotonic()
    # The browser always gets the real params; everything we LOG or surface in
    # the advisory uses the masked copy so a render-lost during a sensitive
    # action (e.g. Input.type of a password) never persists the value. Recovery
    # logic only reads pageId/anchor keys, which masking leaves intact.
    safe_params = mask_params(params, redact_params)

    try:
        response = await browser.call(method, params)
    except ABCPTransportError as exc:
        reason = detect_render_lost(str(exc))
        if not reason:
            raise
        response = {"error": str(exc)}
    else:
        reason = detect_render_lost(response)
        if not reason:
            return response, None

    page_id = extract_page_id_from_values(params, response)
    logger.write(
        "browser.call.render_lost",
        trim_large_strings(
            {
                "method": method,
                "params": safe_params,
                "pageId": page_id,
                "reason": reason,
                "response": response,
            },
            8000,
        ),
    )

    skipped_strategies: Set[str] = set()
    known_url: Optional[str] = None
    latest_original = response

    while True:
        recovery = await recover_render_context(
            browser=browser,
            logger=logger,
            recent_recoveries=recent_recoveries,
            capability_methods=capability_methods,
            method=method,
            # recover_render_context only forwards params into recovery_attempt
            # logging (original_params); the masked copy is correct there.
            params=safe_params,
            page_id=page_id,
            original=latest_original,
            started=started,
            skipped_strategies=skipped_strategies,
            known_url=known_url,
        )
        known_url = recovery.url or known_url

        if not can_retry_after_render_recovery(method, params, recovery):
            recovery.elapsed_ms = int((time.monotonic() - started) * 1000)
            if recovery.recovered and recovery.page_id:
                recent_recoveries[recovery.page_id] = time.monotonic()
            log_render_recovery_result(logger, method, safe_params, recovery)
            return (
                build_render_recovery_advisory(method, safe_params, latest_original, recovery),
                recovery,
            )

        try:
            retry_response = await browser.call(method, params)
        except ABCPTransportError as exc:
            retry_reason = detect_render_lost(str(exc))
            if not retry_reason:
                raise
            latest_original = {"error": str(exc)}
        else:
            retry_reason = detect_render_lost(retry_response)
            if not retry_reason:
                recovery.outcome = "succeeded"
                recovery.elapsed_ms = int((time.monotonic() - started) * 1000)
                if recovery.page_id:
                    recent_recoveries[recovery.page_id] = time.monotonic()
                log_render_recovery_result(logger, method, safe_params, recovery)
                return retry_response, recovery
            latest_original = retry_response

        if recovery.strategy in {"getstate", "switchto"}:
            skipped_strategies.add(recovery.strategy)
            continue

        recovery.outcome = "failed_retry_still_render_lost"
        recovery.reason = retry_reason
        recovery.elapsed_ms = int((time.monotonic() - started) * 1000)
        if recovery.recovered and recovery.page_id:
            recent_recoveries[recovery.page_id] = time.monotonic()
        log_render_recovery_result(logger, method, safe_params, recovery)
        return build_render_recovery_advisory(method, safe_params, latest_original, recovery), recovery


async def recover_render_context(
    *,
    browser: ABCPClient,
    logger: RunLogger,
    recent_recoveries: Dict[str, float],
    capability_methods: Set[str],
    method: str,
    params: JsonDict,
    page_id: Optional[str],
    original: Any,
    started: float,
    skipped_strategies: Optional[Set[str]] = None,
    known_url: Optional[str] = None,
) -> RenderRecoveryOutcome:
    skipped_strategies = skipped_strategies or set()
    if method in RENDER_RECOVERY_METHODS:
        return RenderRecoveryOutcome(
            outcome="failed_recovery_method_render_lost",
            page_id=page_id,
            reason="render lost while calling recovery method",
            elapsed_ms=int((time.monotonic() - started) * 1000),
        )
    if not page_id:
        return RenderRecoveryOutcome(
            outcome="failed_missing_page_id",
            reason="pageId not found in params or response",
            elapsed_ms=int((time.monotonic() - started) * 1000),
        )

    now = time.monotonic()
    last_recovery = recent_recoveries.get(page_id)
    if (
        last_recovery is not None
        and now - last_recovery < RENDER_RECOVERY_WINDOW_SECONDS
    ):
        return RenderRecoveryOutcome(
            outcome="skipped_recently_recovered",
            page_id=page_id,
            reason="recently_recovered",
            recently_recovered=True,
            elapsed_ms=int((time.monotonic() - started) * 1000),
        )

    state_response: Any = None
    url: Optional[str] = known_url

    state_reason: Optional[str] = None
    if "getstate" not in skipped_strategies:
        state_response, state_reason = await attempt_render_recovery_strategy(
            browser=browser,
            logger=logger,
            method=method,
            params=params,
            page_id=page_id,
            strategy="getstate",
            call_method="Page.getState",
            call_params={
                "pageId": page_id,
                "purpose": "Probe page state after render context loss",
            },
        )
        if state_response is not None and not state_reason:
            url = extract_url_from_values(state_response) or url
            return RenderRecoveryOutcome(
                outcome="ready_for_retry",
                strategy="getstate",
                recovered=True,
                dom_invalidated=False,
                page_id=page_id,
                url=url,
                elapsed_ms=int((time.monotonic() - started) * 1000),
            )

    if "switchto" not in skipped_strategies and "Page.switchTo" in capability_methods:
        switch_response, switch_reason = await attempt_render_recovery_strategy(
            browser=browser,
            logger=logger,
            method=method,
            params=params,
            page_id=page_id,
            strategy="switchto",
            call_method="Page.switchTo",
            call_params={
                "pageId": page_id,
                "purpose": "Reactivate tab after render context loss",
            },
        )
        if switch_response is not None and not switch_reason:
            return RenderRecoveryOutcome(
                outcome="ready_for_retry",
                strategy="switchto",
                recovered=True,
                dom_invalidated=False,
                page_id=page_id,
                url=url or extract_url_from_values(switch_response, state_response),
                elapsed_ms=int((time.monotonic() - started) * 1000),
            )

    url = url or extract_url_from_values(state_response, original)
    if not url:
        return RenderRecoveryOutcome(
            outcome="failed_missing_url",
            strategy="navigate",
            page_id=page_id,
            reason="current URL not available for Page.navigate recovery",
            elapsed_ms=int((time.monotonic() - started) * 1000),
        )

    navigate_response, navigate_reason = await attempt_render_recovery_strategy(
        browser=browser,
        logger=logger,
        method=method,
        params=params,
        page_id=page_id,
        strategy="navigate",
        call_method="Page.navigate",
        call_params={
            "pageId": page_id,
            "url": url,
            "purpose": "Re-mount page after render context loss",
        },
    )
    if navigate_response is not None and not navigate_reason:
        return RenderRecoveryOutcome(
            outcome="ready_for_retry",
            strategy="navigate",
            recovered=True,
            dom_invalidated=True,
            page_id=page_id,
            url=extract_url_from_values(navigate_response) or url,
            elapsed_ms=int((time.monotonic() - started) * 1000),
        )

    return RenderRecoveryOutcome(
        outcome="failed",
        strategy="navigate",
        page_id=page_id,
        url=url,
        reason=navigate_reason or state_reason or "render recovery failed",
        elapsed_ms=int((time.monotonic() - started) * 1000),
    )


async def attempt_render_recovery_strategy(
    *,
    browser: ABCPClient,
    logger: RunLogger,
    method: str,
    params: JsonDict,
    page_id: str,
    strategy: str,
    call_method: str,
    call_params: JsonDict,
) -> Tuple[Optional[JsonDict], Optional[str]]:
    logger.write(
        "browser.call.recovery_attempt",
        trim_large_strings(
            {
                "method": method,
                "pageId": page_id,
                "strategy": strategy,
                "recovery_method": call_method,
                "recovery_params": call_params,
                "original_params": params,
            },
            8000,
        ),
    )
    try:
        response = await browser.call(call_method, call_params)
    except ABCPTransportError as exc:
        reason = detect_render_lost(str(exc)) or str(exc)
        return None, reason
    reason = detect_render_lost(response)
    return response, reason


def log_render_recovery_result(
    logger: RunLogger,
    method: str,
    params: JsonDict,
    recovery: RenderRecoveryOutcome,
) -> None:
    logger.write(
        "browser.call.recovery_result",
        trim_large_strings(
            {
                "method": method,
                "params": params,
                "recovery_result": recovery.to_dict(),
                "selectors_invalid": recovery.dom_invalidated,
                "selectors_should_be_refetched": (
                    recovery.dom_invalidated
                    or (method in ACTION_METHODS and uses_dom_anchor(params))
                ),
            },
            8000,
        ),
    )
