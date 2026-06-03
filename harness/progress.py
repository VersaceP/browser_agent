"""
harness.progress - Lightweight per-worker progress accounting.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from harness.utils import JsonDict


LOCAL_FS_TOOLS = {"local_fs_search", "local_fs_read", "local_fs_jsonpath"}
ARTIFACT_PROGRESS_TOOLS = {
    "record_extraction",
    "extract_dom_records",
    "eval_js_json",
    "navigate_verified",
    "Runtime.evaluate",
    "DOM.getText",
}


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
                    " call record_extraction with verified rows, pivot to a different"
                    " extraction method, or finalize with the blocker."
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
                    " artifact. Return to browser extraction, call record_extraction"
                    " with verified rows, or finalize with a blocker."
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
                    " Use a browser extraction method such as extract_dom_records,"
                    " navigate_verified, Runtime.evaluate with explicit return, or"
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
        if artifact_count > self.extraction_artifact_count:
            self.extraction_artifact_count = artifact_count
            self.turns_since_artifact_progress = 0
            self.local_fs_without_extraction = 0
            self.local_fs_streak = 0
            self.repeated_local_result_count = 0
            self.pending_intervention = None
            return
        self.turns_since_artifact_progress += 1
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
                        " pivot to extract_dom_records / browser action or finalize."
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
