"""
harness.progress - Lightweight per-worker progress accounting.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import re
from typing import Any, Dict, List, Optional, Set

from harness.utils import JsonDict


LOCAL_FS_TOOLS = {"local_fs_search", "local_fs_read"}
NO_ARTIFACT_DIAGNOSTIC_TOOLS = frozenset({
    "find_in_axtree",
    "visual_verify",
    "DOM.getAXTree",
    "DOM.getSemanticTree",
    "DOM.getText",
    "DOM.getAttribute",
    "Input.scroll",
    "Input.press",
    "Memory.get",
    "Memory.save",
    "Page.create",
    "Page.getState",
    "Page.list",
    "Page.screenshot",
    "Page.navigate",
    "Page.reload",
    "Page.switchTo",
    "Page.go",
    "System.describeAction",
    "System.describeEvent",
    "System.getCapabilities",
})

ARTIFACT_PROGRESS_TOOLS = {
    "record_extraction",
    "navigate_verified",
    "local_fs_search",
    "local_fs_read",
    "System.describeAction",
    "System.describeEvent",
    "System.getCapabilities",
    "Memory.get",
    "Memory.save",
    "Runtime.evaluate",
    "DOM.getAXTree",
    # DOM.getImg is an output-producing primitive.  One call can export an
    # arbitrary batch of declared targets, so a small call-count diagnostic
    # budget would reject legitimate image-heavy pages.  Keep it behind the
    # worker step/loop bounds, but never treat it as no-artifact diagnostics.
    "DOM.getImg",
    "DOM.getText",
    "DOM.getAttribute",
    "Input.click",
    "Input.type",
    "Input.press",
    "Input.scroll",
    "Page.getState",
    *NO_ARTIFACT_DIAGNOSTIC_TOOLS,
}

PRODUCTIVE_PRIMITIVE_TOOLS = {
    "DOM.getAXTree",
    "DOM.getText",
    "DOM.getAttribute",
    "Input.click",
    "Input.type",
    "Input.press",
    "Input.scroll",
    "Page.getState",
}

PRODUCTIVE_WITHOUT_ARTIFACT_HARD_LIMIT = 30
MIN_HISTORY_NAVIGATION_CREDITS = 4
MAX_HISTORY_NAVIGATION_CREDITS = 12

# These tools are valid pre-artifact diagnostics/recovery actions, but unlike
# cheap state/DOM reads they must not receive an unlimited bypass.  Counts are
# scoped to a page navigation epoch; a verified navigation resets that page.
HEAVY_DIAGNOSTIC_LIMITS = {
    # A review drawer can require several scroll -> SemanticTree rounds before
    # the requested record count materializes.  Keep this bounded, but leave
    # enough room for the verified 5-round flow plus re-perception.
    "DOM.getSemanticTree": 12,
    "Page.screenshot": 3,
    "visual_verify": 3,
    "Page.reload": 2,
    # Page.create/Page.list do not carry a pageId.  They use a worker-scoped
    # budget (see WORKER_SCOPED_HEAVY_DIAGNOSTICS) instead of pretending to
    # belong to a navigation epoch.
    "Page.create": 12,
    "Page.navigate": 4,
    "Page.go": 4,
    "Page.list": 32,
}

WORKER_SCOPED_HEAVY_DIAGNOSTICS = frozenset({"Page.create", "Page.list"})

DIAGNOSTIC_TOOLS_AFTER_INFRA_ERROR = {
    "System.register",
    "System.describeAction",
    "System.describeEvent",
    "System.getCapabilities",
    "Fleet.status",
    "Fleet.list",
    "Fleet.close",
    "Page.list",
    "Page.getState",
    "Memory.get",
    "Memory.save",
}

INFRA_ERROR_RE = re.compile(
    r"("
    r"-32603|internal error|transport|websocket|connection[_ ]closed|"
    r"err_connection_closed|err_connection_reset|err_connection_refused|"
    r"err_connection_aborted|err_timed_out|err_network|"
    r"cannot read properties of undefined|abcptransporterror"
    r")",
    re.I,
)
PAUSED_ERROR_RE = re.compile(
    r"(err_page_paused|paused for human intervention)",
    re.I,
)


@dataclass
class ProgressAccountant:
    """Track whether a worker is producing trusted structured artifacts.

    v1 deliberately keeps this small: it catches the common failure mode where
    a worker keeps reading/searching local offload files without ever recording
    extraction artifacts.
    """

    local_fs_without_extraction: int = 0
    local_fs_streak: int = 0
    repeated_local_result_count: int = 0
    turns_since_artifact_progress: int = 0
    tool_calls: int = 0
    extraction_artifact_count: int = 0
    distinct_tools: Dict[str, int] = field(default_factory=dict)
    interventions: List[JsonDict] = field(default_factory=list)
    last_local_result_signature: Optional[str] = None
    pending_intervention: Optional[JsonDict] = None
    infra_error_streak: int = 0
    last_infra_error: Optional[JsonDict] = None
    infra_diagnostic_bypass_count: int = 0
    last_blocked_reason: Optional[str] = None
    last_blocked_step: Optional[int] = None
    repair_progress_signatures: Set[str] = field(default_factory=set)
    navigation_epochs: Dict[str, int] = field(default_factory=dict)
    heavy_diagnostic_counts: Dict[str, int] = field(default_factory=dict)
    last_diagnostic_allowance: Optional[JsonDict] = None
    history_navigation_credit_limit: int = MIN_HISTORY_NAVIGATION_CREDITS
    history_navigation_credits_used: int = 0
    mandatory_recovery_credits_used: Set[str] = field(default_factory=set)
    last_mandatory_recovery_allowance: Optional[JsonDict] = None

    def configure_history_navigation_credits(self, batch_size: int = 0) -> int:
        """Size Page.go progress credit to the declared worker batch.

        One history return per row is legitimate for same-tab detail batches.
        Two recovery slots cover an initial probe and one corrective return,
        while the hard ceiling ensures back/forward oscillation cannot mint an
        unbounded sequence of fresh diagnostic epochs.
        """
        try:
            declared = max(0, int(batch_size or 0))
        except (TypeError, ValueError):
            declared = 0
        self.history_navigation_credit_limit = min(
            MAX_HISTORY_NAVIGATION_CREDITS,
            max(MIN_HISTORY_NAVIGATION_CREDITS, declared + 2),
        )
        return self.history_navigation_credit_limit

    def _diagnostic_key(self, tool_name: str, page_id: str) -> str:
        scope = (
            "__worker__"
            if tool_name in WORKER_SCOPED_HEAVY_DIAGNOSTICS
            else (page_id or "__global__")
        )
        epoch = self.navigation_epochs.get(scope, 0)
        return f"{scope}:{epoch}:{tool_name}"

    def _diagnostic_scope(self, tool_name: str, page_id: str) -> str:
        if tool_name in WORKER_SCOPED_HEAVY_DIAGNOSTICS:
            return "__worker__"
        return page_id or "__global__"

    def _emit_intervention(
        self, intervention: JsonDict, step: Optional[int] = None,
    ) -> JsonDict:
        """Record + return an intervention, deduplicating within one parallel
        batch: a SAME-STEP consecutive block with the same reason (two parallel
        calls in one step each hitting the same gate) keeps the accounting but
        collapses the repeated instruction text — task 9d5655d3 step 29
        injected the same directive twice. Cross-step blocks are never
        deduplicated (each model turn deserves the full instruction), and a
        caller that does not pass step opts out entirely."""
        reason = str(intervention.get("reason") or "")
        if (
            reason
            and reason == self.last_blocked_reason
            and step is not None
            and step == self.last_blocked_step
        ):
            intervention["duplicate_of_previous"] = True
            intervention["next_instruction"] = (
                "Duplicate of the previous intervention in this same step"
                " (parallel tool call blocked by the same rule); follow that"
                " instruction."
            )
        self.last_blocked_reason = reason
        self.last_blocked_step = step
        self.interventions.append(intervention)
        return intervention

    def before_tool(
        self,
        *,
        tool_name: str,
        artifact_count: int,
        local_fs_limit: int,
        no_artifact_limit: int,
        requires_artifact: bool,
        own_artifact_read: bool = False,
        step: Optional[int] = None,
        page_id: str = "",
        charge_heavy_diagnostic: bool = True,
        mandatory_recovery_generation: Optional[int] = None,
    ) -> Optional[JsonDict]:
        self.extraction_artifact_count = max(
            self.extraction_artifact_count,
            artifact_count,
        )
        self.last_diagnostic_allowance = None
        self.last_mandatory_recovery_allowance = None
        if (
            requires_artifact
            and artifact_count == 0
            and tool_name in HEAVY_DIAGNOSTIC_LIMITS
            and charge_heavy_diagnostic
        ):
            normalized_page_id = str(page_id or "")
            scope = self._diagnostic_scope(tool_name, normalized_page_id)
            key = self._diagnostic_key(tool_name, normalized_page_id)
            used = self.heavy_diagnostic_counts.get(key, 0)
            limit = HEAVY_DIAGNOSTIC_LIMITS[tool_name]
            if used >= limit:
                intervention = {
                    "status": "progress_intervention",
                    "reason": "diagnostic_budget_exhausted",
                    "tool": tool_name,
                    "pageId": str(page_id or "") or None,
                    "diagnosticScope": scope,
                    "navigationEpoch": self.navigation_epochs.get(scope, 0),
                    "diagnosticUses": used,
                    "diagnosticLimit": limit,
                    "tool_was_executed": False,
                    "next_instruction": (
                        "The bounded diagnostic budget for this navigation"
                        " epoch is exhausted. Use the evidence already"
                        " collected, perform a justified materialization or"
                        " navigation recovery, persist verified rows, or"
                        " finalize with the blocker; do not repeat the same"
                        " diagnostic surface."
                    ),
                }
                return self._emit_intervention(intervention, step)
            used += 1
            self.heavy_diagnostic_counts[key] = used
            self.last_diagnostic_allowance = {
                "tool": tool_name,
                "pageId": str(page_id or "") or None,
                "diagnosticScope": scope,
                "navigationEpoch": self.navigation_epochs.get(scope, 0),
                "diagnosticUses": used,
                "diagnosticLimit": limit,
            }
        if (
            requires_artifact
            and artifact_count == 0
            and tool_name in PRODUCTIVE_PRIMITIVE_TOOLS
            and self.turns_since_artifact_progress >= PRODUCTIVE_WITHOUT_ARTIFACT_HARD_LIMIT
        ):
            recovery_key = ""
            if mandatory_recovery_generation is not None and tool_name in {
                "Page.getState", "DOM.getAXTree",
            }:
                recovery_key = (
                    f"{str(page_id or '') or '__global__'}:"
                    f"{int(mandatory_recovery_generation)}:{tool_name}"
                )
            if recovery_key and recovery_key not in self.mandatory_recovery_credits_used:
                # PageLifecycleTracker, not model input, authorizes this one-shot
                # crossing.  Consume on dispatch (success or failure) so a broken
                # page cannot turn the mandatory recovery into an infinite bypass.
                self.mandatory_recovery_credits_used.add(recovery_key)
                self.last_mandatory_recovery_allowance = {
                    "tool": tool_name,
                    "pageId": str(page_id or "") or None,
                    "lifecycleGeneration": int(mandatory_recovery_generation),
                    "turnsSinceArtifactProgress": self.turns_since_artifact_progress,
                }
                return None
            intervention = {
                "status": "progress_intervention",
                "reason": "productive_primitives_without_artifact",
                "tool": tool_name,
                "turnsSinceArtifactProgress": self.turns_since_artifact_progress,
                "toolCalls": self.tool_calls,
                "tool_was_executed": False,
                "next_instruction": (
                    "This worker has spent many productive browser steps without"
                    " producing record_extraction. Persist verified rows now,"
                    " narrow the target using params derived from the latest"
                    " DOM/response feedback, or finalize with the blocker."
                ),
            }
            return self._emit_intervention(intervention, step)
        if (
            requires_artifact
            and artifact_count == 0
            and self.infra_error_streak > 0
            and tool_name in DIAGNOSTIC_TOOLS_AFTER_INFRA_ERROR
        ):
            self.infra_diagnostic_bypass_count += 1
            return None
        if (
            requires_artifact
            and artifact_count == 0
            and tool_name not in ARTIFACT_PROGRESS_TOOLS
            and self.turns_since_artifact_progress >= no_artifact_limit
        ):
            intervention = {
                "status": "progress_intervention",
                "reason": "no_artifact_progress",
                "tool": tool_name,
                "turnsSinceArtifactProgress": self.turns_since_artifact_progress,
                "toolCalls": self.tool_calls,
                "tool_was_executed": False,
                "next_instruction": (
                    "This worker has not produced any record_extraction artifact"
                    " after several tool calls. Stop repeating the current surface:"
                    " call record_extraction with verified rows, pivot with fresh"
                    " params from DOM.getAXTree/DOM.getText/DOM.getAttribute,"
                    " or finalize with the blocker."
                ),
            }
            return self._emit_intervention(intervention, step)
        if tool_name in LOCAL_FS_TOOLS and own_artifact_read:
            # Reading an artifact THIS RUN persisted (e.g. analyzing why its
            # own save came back needs_fix) is ledger analysis, not offload
            # spinning — exempt from the local_fs gates. Kept narrow: the
            # caller verifies an exact match against this run's own extraction
            # attempt paths, so the "trust the ledger" guard semantics from
            # the 07-08 deadlock fixes stay intact for everything else.
            return None
        if tool_name in LOCAL_FS_TOOLS and self.pending_intervention:
            intervention = dict(self.pending_intervention)
            intervention["tool_was_executed"] = False
            self.pending_intervention = None
            return self._emit_intervention(intervention, step)
        if (
            tool_name in LOCAL_FS_TOOLS
            and artifact_count == 0
            and self.local_fs_without_extraction >= local_fs_limit
        ):
            intervention = {
                "status": "progress_intervention",
                "reason": "local_fs_without_extraction",
                "tool": tool_name,
                "localFsWithoutExtraction": self.local_fs_without_extraction,
                "tool_was_executed": False,
                "next_instruction": (
                    "Local file searches have not produced any record_extraction"
                    " artifact. Return to browser extraction: call DOM.getAXTree"
                    " for a fresh snapshot (this re-enables local file reads),"
                    " call record_extraction with verified rows, or finalize with"
                    " a blocker. Do not reuse stale AXTree ids or selectors from"
                    " old offload files."
                ),
            }
            return self._emit_intervention(intervention, step)
        if (
            tool_name in LOCAL_FS_TOOLS
            and artifact_count == 0
            and self.local_fs_streak >= local_fs_limit
        ):
            intervention = {
                "status": "progress_intervention",
                "reason": "local_fs_without_browser_action",
                "tool": tool_name,
                "localFsStreak": self.local_fs_streak,
                "tool_was_executed": False,
                "next_instruction": (
                    "This worker has only searched/read local files recently."
                    " Use browser primitives such as DOM.getAXTree,"
                    " DOM.getText/DOM.getAttribute, Input.*, or"
                    " finalize with a blocker."
                ),
            }
            return self._emit_intervention(intervention, step)
        return None

    def after_tool(
        self,
        *,
        tool_name: str,
        artifact_count: int,
        result: Optional[JsonDict] = None,
        own_artifact_read: bool = False,
    ) -> None:
        self.tool_calls += 1
        # An executed tool separates intervention batches: the next block is a
        # fresh intervention, not a duplicate of the previous one.
        self.last_blocked_reason = None
        self.last_blocked_step = None
        self.distinct_tools[tool_name] = self.distinct_tools.get(tool_name, 0) + 1
        infra_error = _infra_error_summary(result)
        if infra_error:
            self.infra_error_streak += 1
            self.last_infra_error = {
                "tool": tool_name,
                **infra_error,
            }
        elif (
            self.infra_error_streak
            and tool_name not in DIAGNOSTIC_TOOLS_AFTER_INFRA_ERROR
            and _tool_result_success(result)
        ):
            self.infra_error_streak = 0
            self.last_infra_error = None
        if artifact_count > self.extraction_artifact_count:
            self.extraction_artifact_count = artifact_count
            self.infra_error_streak = 0
            self.last_infra_error = None
            self.turns_since_artifact_progress = 0
            self.local_fs_without_extraction = 0
            self.local_fs_streak = 0
            self.repeated_local_result_count = 0
            self.pending_intervention = None
            self.history_navigation_credits_used = 0
            return
        self.turns_since_artifact_progress += 1
        if tool_name in {"DOM.getAXTree", "DOM.getSemanticTree"} and _tool_result_success(result):
            # A fresh DOM snapshot (and its offload file) makes follow-up
            # local file reads legitimate perception work again. Without this
            # reset the counter is sticky until record_extraction, which
            # deadlocks offloaded pages: the model can fetch the tree but can
            # never read the offload file it points to.
            self.local_fs_without_extraction = 0
            self.repeated_local_result_count = 0
            self.last_local_result_signature = None
            self.pending_intervention = None
        if tool_name in LOCAL_FS_TOOLS and own_artifact_read:
            # Neutral: reading this run's own persisted artifacts neither
            # feeds nor resets the offload-spinning counters.
            return
        if tool_name in LOCAL_FS_TOOLS and artifact_count == 0:
            self.local_fs_without_extraction += 1
            self.local_fs_streak += 1
            signature = _local_result_signature(result)
            if signature and signature == self.last_local_result_signature:
                self.repeated_local_result_count += 1
            else:
                self.repeated_local_result_count = 1 if signature else 0
            self.last_local_result_signature = signature
            if self.repeated_local_result_count >= 2 and signature:
                self.pending_intervention = {
                    "status": "progress_intervention",
                    "reason": "repeated_local_search_result",
                    "tool": tool_name,
                    "repeatedLocalResultCount": self.repeated_local_result_count,
                    "resultSignature": signature[:500],
                    "next_instruction": (
                        "The last local search/read returned the same file/line"
                        " evidence again. Do not keep searching the same offload;"
                        " pivot to DOM/Page/Input browser action with fresh params"
                        " from the current page, or finalize."
                    ),
                }
            return

        if tool_name not in LOCAL_FS_TOOLS:
            self.local_fs_streak = 0

    def notify_navigation_success(
        self,
        page_id: str = "",
        *,
        navigation_kind: str = "verified",
    ) -> JsonDict:
        """Reset no-artifact stall after a verified navigation has landed.

        navigate_verified may perform many internal state probes before the
        model regains control. Once the target page is verified, the next tool
        should not be blocked by stale "no artifact yet" accounting.
        """
        history_navigation = navigation_kind == "history"
        if (
            history_navigation
            and self.history_navigation_credits_used
            >= self.history_navigation_credit_limit
        ):
            return {
                "status": "history_navigation_credit_exhausted",
                "pageId": page_id,
                "navigationKind": navigation_kind,
                "historyNavigationCreditsUsed": (
                    self.history_navigation_credits_used
                ),
                "historyNavigationCreditLimit": (
                    self.history_navigation_credit_limit
                ),
                "turnsSinceArtifactProgress": self.turns_since_artifact_progress,
                "toolCalls": self.tool_calls,
                "creditApplied": False,
                "next_instruction": (
                    "Page.go changed the document, but this worker has exhausted"
                    " its bounded history-navigation progress credits without"
                    " producing a new extraction artifact. Persist verified"
                    " rows or real repair progress before relying on another"
                    " history return to refresh progress/diagnostic budgets."
                ),
            }
        if history_navigation:
            self.history_navigation_credits_used += 1
        self.turns_since_artifact_progress = 0
        self.local_fs_streak = 0
        self.pending_intervention = None
        scope = str(page_id or "") or "__global__"
        self.navigation_epochs[scope] = self.navigation_epochs.get(scope, 0) + 1
        prefix = f"{scope}:"
        self.heavy_diagnostic_counts = {
            key: count
            for key, count in self.heavy_diagnostic_counts.items()
            if not key.startswith(prefix)
        }
        return {
            "status": "navigation_success",
            "pageId": page_id,
            "navigationKind": navigation_kind,
            "creditApplied": True,
            "historyNavigationCreditsUsed": self.history_navigation_credits_used,
            "historyNavigationCreditLimit": self.history_navigation_credit_limit,
            "turnsSinceArtifactProgress": self.turns_since_artifact_progress,
            "toolCalls": self.tool_calls,
            "navigationEpoch": self.navigation_epochs[scope],
        }

    def consume_diagnostic_allowance(self) -> Optional[JsonDict]:
        value = self.last_diagnostic_allowance
        self.last_diagnostic_allowance = None
        return value

    def notify_repair_progress(self, applied: Any) -> JsonDict:
        """Credit new manifest-authorized fields without crediting the artifact.

        A partial merge may still be needs_fix because another target remains.
        Identity+field signatures reset the stall counter for real repair work,
        while replaying the same patch cannot buy another reset.
        """
        new_items: List[JsonDict] = []
        for item in applied if isinstance(applied, list) else []:
            if not isinstance(item, dict):
                continue
            identity = item.get("identity")
            identity_field = (
                str(identity.get("field") or "").strip()
                if isinstance(identity, dict) else ""
            )
            identity_value = (
                str(identity.get("value")).strip()
                if isinstance(identity, dict) and identity.get("value") is not None
                else ""
            )
            fields = sorted({
                str(repair_field).strip()
                for repair_field in (item.get("fields") or [])
                if str(repair_field).strip()
            }) if isinstance(item.get("fields"), list) else []
            if not identity_field or not identity_value or not fields:
                continue
            fresh_fields: List[str] = []
            for repair_field in fields:
                signature = "\x1f".join(
                    (identity_field, identity_value, repair_field)
                )
                if signature in self.repair_progress_signatures:
                    continue
                self.repair_progress_signatures.add(signature)
                fresh_fields.append(repair_field)
            if fresh_fields:
                new_items.append({
                    "identity": {
                        "field": identity_field,
                        "value": identity.get("value"),
                    },
                    "fields": fresh_fields,
                })
        if new_items:
            self.turns_since_artifact_progress = 0
            self.local_fs_without_extraction = 0
            self.local_fs_streak = 0
            self.repeated_local_result_count = 0
            self.pending_intervention = None
            self.history_navigation_credits_used = 0
        return {
            "status": "repair_progress" if new_items else "repair_progress_duplicate",
            "newRepairs": new_items,
            "newFieldCount": sum(len(item["fields"]) for item in new_items),
            "repairProgressFieldCount": len(self.repair_progress_signatures),
            "turnsSinceArtifactProgress": self.turns_since_artifact_progress,
        }

    def to_log_payload(self) -> JsonDict:
        return {
            "toolCalls": self.tool_calls,
            "extractionArtifactCount": self.extraction_artifact_count,
            "localFsWithoutExtraction": self.local_fs_without_extraction,
            "localFsStreak": self.local_fs_streak,
            "repeatedLocalResultCount": self.repeated_local_result_count,
            "turnsSinceArtifactProgress": self.turns_since_artifact_progress,
            "infraErrorStreak": self.infra_error_streak,
            "lastInfraError": self.last_infra_error,
            "infraDiagnosticBypassCount": self.infra_diagnostic_bypass_count,
            "navigationEpochs": dict(sorted(self.navigation_epochs.items())),
            "heavyDiagnosticCounts": dict(sorted(self.heavy_diagnostic_counts.items())),
            "historyNavigationCreditsUsed": self.history_navigation_credits_used,
            "historyNavigationCreditLimit": self.history_navigation_credit_limit,
            "repairProgressFieldCount": len(self.repair_progress_signatures),
            "distinctTools": dict(sorted(self.distinct_tools.items())),
            "interventionCount": len(self.interventions),
        }


def extraction_artifact_count(artifacts: Any) -> int:
    if not isinstance(artifacts, list):
        return 0
    return sum(1 for item in artifacts if "/artifacts/extractions/" in str(item))


def _local_result_signature(result: Optional[JsonDict]) -> Optional[str]:
    if not isinstance(result, dict):
        return None
    results = result.get("results")
    if not isinstance(results, list):
        path = result.get("path") or result.get("relativePath")
        if path:
            line_offset = result.get("lineOffset")
            line_limit = result.get("lineLimit")
            if line_offset is not None or line_limit is not None:
                content = str(result.get("content") or "")
                digest = hashlib.sha256(content.encode("utf-8", errors="replace")).hexdigest()[:16]
                return f"{path}:lines:{line_offset or 0}:{line_limit or ''}:sha:{digest}"
            return str(path)
        return None
    keys: List[str] = []
    for item in results[:20]:
        if not isinstance(item, dict):
            continue
        path = item.get("relativePath") or item.get("path") or ""
        line = item.get("line")
        if line is None:
            keys.append(str(path))
        else:
            keys.append(f"{path}:{line}")
    if not keys:
        return None
    return "|".join(keys)


def _infra_error_summary(result: Optional[JsonDict]) -> Optional[JsonDict]:
    if not isinstance(result, dict):
        return None
    text = _flatten_error_text(result)
    if PAUSED_ERROR_RE.search(text):
        return None
    match = INFRA_ERROR_RE.search(text)
    if not match:
        return None
    return {
        "pattern": match.group(0)[:80],
        "status": str(result.get("status") or "")[:80],
        "message": text[:500],
    }


def _flatten_error_text(value: Any, *, depth: int = 0) -> str:
    if depth > 4:
        return ""
    if isinstance(value, dict):
        chunks: List[str] = []
        for key, item in value.items():
            if str(key).lower() in {
                "error",
                "message",
                "reason",
                "status",
                "details",
                "exception",
                "classification",
            }:
                chunks.append(_flatten_error_text(item, depth=depth + 1))
            elif isinstance(item, (dict, list)):
                chunks.append(_flatten_error_text(item, depth=depth + 1))
        return " ".join(chunk for chunk in chunks if chunk)
    if isinstance(value, list):
        return " ".join(
            chunk for chunk in (_flatten_error_text(item, depth=depth + 1) for item in value)
            if chunk
        )
    if isinstance(value, str):
        return value
    return ""


def _tool_result_success(result: Optional[JsonDict]) -> bool:
    if not isinstance(result, dict):
        return False
    status = str(result.get("status") or "").strip().lower()
    if status in {"done", "ok", "success", "navigation_success"}:
        return True
    if status in {"error", "failed", "rejected", "needs_fix", "progress_intervention"}:
        return False
    return not _infra_error_summary(result)
