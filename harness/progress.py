"""
harness.progress - Lightweight per-worker progress accounting.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import re
from typing import Any, Dict, List, Optional

from harness.utils import JsonDict


LOCAL_FS_TOOLS = {"local_fs_search", "local_fs_read"}
ARTIFACT_PROGRESS_TOOLS = {
    "record_extraction",
    "extract_dom_records",
    "eval_js_json",
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
    "DOM.getText",
    "DOM.getAttribute",
    "Input.click",
    "Input.type",
    "Input.press",
    "Input.scroll",
    "Page.getState",
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

    def before_tool(
        self,
        *,
        tool_name: str,
        artifact_count: int,
        local_fs_limit: int,
        no_artifact_limit: int,
        requires_artifact: bool,
    ) -> Optional[JsonDict]:
        self.extraction_artifact_count = max(
            self.extraction_artifact_count,
            artifact_count,
        )
        if (
            requires_artifact
            and artifact_count == 0
            and tool_name in PRODUCTIVE_PRIMITIVE_TOOLS
            and self.turns_since_artifact_progress >= PRODUCTIVE_WITHOUT_ARTIFACT_HARD_LIMIT
        ):
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
            self.interventions.append(intervention)
            return intervention
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
            self.interventions.append(intervention)
            return intervention
        if tool_name in LOCAL_FS_TOOLS and self.pending_intervention:
            intervention = dict(self.pending_intervention)
            intervention["tool_was_executed"] = False
            self.interventions.append(intervention)
            self.pending_intervention = None
            return intervention
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
            self.interventions.append(intervention)
            return intervention
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
            self.interventions.append(intervention)
            return intervention
        return None

    def after_tool(
        self,
        *,
        tool_name: str,
        artifact_count: int,
        result: Optional[JsonDict] = None,
    ) -> None:
        self.tool_calls += 1
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
            return
        self.turns_since_artifact_progress += 1
        if tool_name == "DOM.getAXTree" and _tool_result_success(result):
            # A fresh AXTree snapshot (and its offload file) makes follow-up
            # local file reads legitimate perception work again. Without this
            # reset the counter is sticky until record_extraction, which
            # deadlocks offloaded pages: the model can fetch the tree but can
            # never read the offload file it points to.
            self.local_fs_without_extraction = 0
            self.repeated_local_result_count = 0
            self.last_local_result_signature = None
            self.pending_intervention = None
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

    def notify_navigation_success(self, page_id: str = "") -> JsonDict:
        """Reset no-artifact stall after a verified navigation has landed.

        navigate_verified may perform many internal state probes before the
        model regains control. Once the target page is verified, the next tool
        should not be blocked by stale "no artifact yet" accounting.
        """
        self.turns_since_artifact_progress = 0
        self.local_fs_streak = 0
        self.pending_intervention = None
        return {
            "status": "navigation_success",
            "pageId": page_id,
            "turnsSinceArtifactProgress": self.turns_since_artifact_progress,
            "toolCalls": self.tool_calls,
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
