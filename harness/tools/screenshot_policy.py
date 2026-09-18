"""Transport-safe screenshot output normalization shared by every ingress."""

from __future__ import annotations

from typing import Any, Dict, Optional, Tuple

from harness.constants import SCREENSHOT_METHODS


JsonDict = Dict[str, Any]


def normalize_screenshot_output_params(
    method: str,
    params: Any,
) -> Tuple[JsonDict, Optional[JsonDict]]:
    """Force screenshot actions to return a file handle, never image bytes.

    Offloading happens after the WebSocket response and therefore cannot save a
    connection from an oversized base64 frame. Browser calls, nested workflows,
    and the independent control channel must all call this same pure boundary.
    """

    source = dict(params) if isinstance(params, dict) else {}
    if str(method or "").strip() not in SCREENSHOT_METHODS:
        return source, None
    raw_options = source.get("options")
    options = raw_options if isinstance(raw_options, dict) else {}
    requested_format = str(options.get("format") or "default").strip() or "default"
    removed = sorted(str(key) for key in options if key != "format")
    if requested_format == "file" and not removed:
        return source, None
    source["options"] = {"format": "file"}
    return source, {
        "requestedFormat": requested_format,
        "effectiveFormat": "file",
        "removedOutputOptions": removed,
    }
