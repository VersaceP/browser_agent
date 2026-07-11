"""
llm.cache_control - Prompt cache marker diagnostics and fallback handling.
"""

import hashlib
import json
import os
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from llm.config import ModelConfig
_MARKER_POSITION_INDEX_RE = re.compile(r"\.(messages|content)\[\d+\]")
_CACHE_CONTROL_MODES = {"auto", "on", "off"}
_OPENAI_CACHE_CONTROL_BASE_URL_HINTS = (
    "dashscope",
    "bailian",
    "aliyuncs",
    "alibabacloud",
)
_NON_CACHE_REJECTION_HINTS = (
    "tool",
    "function",
    "schema",
    "json",
    "parameter",
    "argument",
    "field",
    "type",
    "value",
    "role",
    "required",
    "missing",
)


@dataclass(frozen=True)
class CacheControlDecision:
    mode: str
    enabled: bool
    reason: str
    warnings: Tuple[str, ...] = ()


def _emit_cache_log(line: str) -> None:
    """
    缓存命中观测输出。
    LLM_CACHE_DEBUG=1 才生效:同时打到 stdout 和 V5_CACHE_LOG_PATH 指定的文件
    (由 AgentSpawner 在 session 创建时设为 shared/cache_stats.log)
    """
    if not os.getenv("LLM_CACHE_DEBUG"):
        return
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    full_line = f"{ts} {line}"
    print(full_line)
    log_path = os.getenv("V5_CACHE_LOG_PATH")
    if log_path:
        try:
            with open(log_path, "a", encoding="utf-8") as f:
                f.write(full_line + "\n")
        except OSError:
            # 文件写入失败不影响主流程(磁盘满 / 权限等)
            pass


def _collect_cache_markers(value: Any, path: str = "$") -> List[Dict[str, Any]]:
    markers: List[Dict[str, Any]] = []
    if isinstance(value, dict):
        if "cache_control" in value:
            markers.append({
                "position": path,
                "cache_control": value.get("cache_control"),
            })
        for key, item in value.items():
            markers.extend(_collect_cache_markers(item, f"{path}.{key}"))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            markers.extend(_collect_cache_markers(item, f"{path}[{index}]"))
    return markers


def _normalize_marker_position(position: Any) -> str:
    return _MARKER_POSITION_INDEX_RE.sub(
        lambda match: f".{match.group(1)}[last]",
        str(position),
    )


def _build_cache_diagnostics(
    provider: str,
    request_payload: Dict[str, Any],
    max_markers: Optional[int] = None,
) -> Dict[str, Any]:
    markers = _collect_cache_markers(request_payload)
    raw_positions = [str(marker.get("position")) for marker in markers]
    normalized_positions = [
        _normalize_marker_position(marker.get("position"))
        for marker in markers
    ]
    marker_fingerprint = [
        {
            "position": normalized_positions[index],
            "cache_control": marker.get("cache_control"),
        }
        for index, marker in enumerate(markers)
    ]
    signature = None
    if marker_fingerprint:
        raw_signature = json.dumps(
            marker_fingerprint,
            ensure_ascii=False,
            sort_keys=True,
            default=str,
        )
        signature = hashlib.sha256(raw_signature.encode("utf-8")).hexdigest()[:16]

    warnings: List[str] = []
    if max_markers is not None and len(markers) > max_markers:
        warnings.append(
            f"{provider}: cache_control marker count {len(markers)} exceeds {max_markers}"
        )

    return {
        "provider": provider,
        "marker_count": len(markers),
        "marker_positions": normalized_positions,
        "marker_positions_raw": raw_positions,
        "marker_positions_normalized": normalized_positions,
        "cache_control_signature": signature,
        "warnings": warnings,
    }


def _normalize_cache_control_mode(extra_params: Dict[str, Any]) -> Tuple[str, List[str]]:
    warnings: List[str] = []
    raw_mode = extra_params.get("cache_control_mode")
    if raw_mode is None and "enable_cache_control" in extra_params:
        return ("on" if bool(extra_params.get("enable_cache_control")) else "off"), warnings
    if raw_mode is None:
        return "auto", warnings
    if isinstance(raw_mode, bool):
        return ("on" if raw_mode else "off"), warnings

    mode = str(raw_mode).strip().lower()
    aliases = {
        "true": "on",
        "1": "on",
        "yes": "on",
        "enabled": "on",
        "false": "off",
        "0": "off",
        "no": "off",
        "disabled": "off",
    }
    mode = aliases.get(mode, mode)
    if mode not in _CACHE_CONTROL_MODES:
        warnings.append(
            f"unknown cache_control_mode={raw_mode!r}; falling back to auto"
        )
        return "auto", warnings
    return mode, warnings


def _base_url_contains(base_url: Optional[str], hints: Tuple[str, ...]) -> bool:
    value = (base_url or "").lower()
    return any(hint in value for hint in hints)


def _resolve_cache_control_decision(
    provider: str,
    config: ModelConfig,
) -> CacheControlDecision:
    mode, warnings = _normalize_cache_control_mode(config.extra_params)
    provider_key = provider.lower()

    if mode == "off":
        return CacheControlDecision(mode, False, "mode_off", tuple(warnings))
    if mode == "on":
        return CacheControlDecision(mode, True, "mode_on", tuple(warnings))

    if provider_key == "anthropic":
        return CacheControlDecision(
            mode,
            True,
            "anthropic_auto_default",
            tuple(warnings),
        )
    if provider_key == "openai" and _base_url_contains(
        config.base_url,
        _OPENAI_CACHE_CONTROL_BASE_URL_HINTS,
    ):
        return CacheControlDecision(
            mode,
            True,
            "openai_compatible_known_good_base_url",
            tuple(warnings),
        )
    return CacheControlDecision(
        mode,
        False,
        f"{provider_key}_auto_default_off",
        tuple(warnings),
    )


def _exception_status_code(exc: Exception) -> Optional[int]:
    status_code = getattr(exc, "status_code", None)
    response = getattr(exc, "response", None)
    if status_code is None and response is not None:
        status_code = getattr(response, "status_code", None)
    try:
        return int(status_code) if status_code is not None else None
    except (TypeError, ValueError):
        return None


def _cache_control_exclusion_hint(exc: Exception) -> Optional[str]:
    text = str(exc).lower()
    if _exception_status_code(exc) not in (400, 422):
        return None
    if not any(hint in text for hint in ("cache", "system", "content")):
        return None
    return next((hint for hint in _NON_CACHE_REJECTION_HINTS if hint in text), None)


def _annotate_cache_control_rejection_excluded(exc: Exception, hint: str) -> None:
    payload = {
        "cache_control_rejection_excluded": True,
        "matched_hint": hint,
        "status_code": _exception_status_code(exc),
    }
    _emit_cache_log(
        "[Cache Control] rejection candidate excluded by "
        f"hint={hint} status={payload['status_code']}"
    )
    add_note = getattr(exc, "add_note", None)
    note = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    if callable(add_note):
        add_note(note)
    else:
        notes = getattr(exc, "__notes__", None)
        if isinstance(notes, list):
            notes.append(note)
        else:
            try:
                setattr(exc, "__notes__", [note])
            except Exception:
                pass


def _is_cache_control_rejection(exc: Exception) -> bool:
    text = str(exc).lower()
    if "cache_control" in text or "cache control" in text:
        return True
    status_code = _exception_status_code(exc)
    if status_code not in (400, 422):
        return False
    if "cache" in text:
        return True
    excluded_hint = _cache_control_exclusion_hint(exc)
    if excluded_hint:
        _annotate_cache_control_rejection_excluded(exc, excluded_hint)
        return False
    # Some compatible endpoints reject the content-block shape used only when
    # cache markers are attached, without naming cache_control in the error.
    return (
        ("system" in text or "content" in text)
        and ("invalid" in text or "unknown" in text or "extra" in text)
    )


def _with_cache_control_diagnostics(
    diagnostics: Dict[str, Any],
    decision: CacheControlDecision,
    *,
    actual_enabled: Optional[bool] = None,
    accepted: Optional[bool] = None,
    fallback: Optional[str] = None,
    reject_error: Optional[Exception] = None,
) -> Dict[str, Any]:
    actual = decision.enabled if actual_enabled is None else actual_enabled
    warnings = list(diagnostics.get("warnings", []))
    warnings.extend(decision.warnings)
    if reject_error is not None:
        warnings.append(
            f"cache_control rejected by provider; fallback={fallback or 'none'}"
        )
    diagnostics = {
        **diagnostics,
        "warnings": warnings,
        "cache_control": {
            "mode": decision.mode,
            "requested": decision.enabled,
            "enabled": actual,
            "accepted": accepted,
            "reason": decision.reason,
            "fallback": fallback,
        },
    }
    return diagnostics
