"""
harness.observation - Page observation, overlay, and loop-stability helpers.
"""

from harness.observation.loop_nudge import ActionLoopNudge
from harness.observation.overlay_detector import (
    detect_overlay_from_result,
    detect_overlay_from_text,
)
from harness.observation.page_fingerprint import (
    PageFingerprint,
    PageObservationTracker,
    page_fingerprint_from_result,
    render_page_stats_for_prompt,
    snapshot_diff,
)


__all__ = [
    "ActionLoopNudge",
    "PageFingerprint",
    "PageObservationTracker",
    "detect_overlay_from_result",
    "detect_overlay_from_text",
    "page_fingerprint_from_result",
    "render_page_stats_for_prompt",
    "snapshot_diff",
]
