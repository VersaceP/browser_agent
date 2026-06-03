"""
harness.utils - Shared helpers for ABCP agent harness modules.
"""

import hashlib
import json
import re
import traceback
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set, Tuple


JsonDict = Dict[str, Any]
EventSink = Callable[[str, JsonDict], None]
LLM_HIDDEN_FIELDS = {"suggested_prompt"}
_SUGGESTED_PROMPT_STRING_RE = re.compile(
    r'\s*,?\s*"suggested_prompt"\s*:\s*"(?:\\.|[^"\\])*"\s*,?'
)


def _resolve_context_file(context_file: Optional[str]) -> Optional[Path]:
    if not context_file:
        return None
    path = Path(context_file).expanduser()
    if not path.is_absolute():
        path = Path.cwd() / path
    return path


def build_static_context_block(
    context_file: Optional[str],
) -> Tuple[str, Optional[str]]:
    path = _resolve_context_file(context_file)
    if path is None:
        return "", None
    try:
        content = path.read_text(encoding="utf-8")
    except OSError:
        return "", None
    digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
    block = (
        "\n\n<context source=\"context_file\" "
        f"sha256=\"{digest}\">\n{content}\n</context>"
    )
    return block, digest


def _usage_int(usage: JsonDict, key: str) -> int:
    try:
        return int(usage.get(key, 0) or 0)
    except (TypeError, ValueError):
        return 0


def _cache_rate(cache_read: int, *parts: int) -> float:
    total = cache_read + sum(parts)
    if total <= 0:
        return 0.0
    return round(cache_read / total, 4)


@dataclass
class UsageState:
    prev_creation: int = 0
    prev_signature: Optional[str] = None


@dataclass
class UsageBucket:
    calls: int = 0
    cache_read: int = 0
    cache_creation: int = 0
    uncached_input: int = 0
    output: int = 0

    def add(
        self,
        cache_read: int,
        cache_creation: int,
        uncached_input: int,
        output: int,
    ) -> None:
        self.calls += 1
        self.cache_read += cache_read
        self.cache_creation += cache_creation
        self.uncached_input += uncached_input
        self.output += output

    def summary(self) -> JsonDict:
        return {
            "calls": self.calls,
            "cache_read": self.cache_read,
            "cache_creation": self.cache_creation,
            "uncached_input": self.uncached_input,
            "output": self.output,
            "cache_read_rate": _cache_rate(
                self.cache_read,
                self.cache_creation,
                self.uncached_input,
            ),
            "cache_reuse_rate": _cache_rate(
                self.cache_read,
                self.uncached_input,
            ),
        }


@dataclass
class UsageAggregator:
    total: UsageBucket = field(default_factory=UsageBucket)
    by_source: Dict[str, UsageBucket] = field(default_factory=dict)
    warnings: List[str] = field(default_factory=list)
    context_hashes: Set[str] = field(default_factory=set)
    _states: Dict[str, UsageState] = field(default_factory=dict)

    def add(
        self,
        usage: JsonDict,
        *,
        source: str,
        provider: str,
        model: str,
        conversation_id: str,
        context_hash: Optional[str] = None,
    ) -> JsonDict:
        cache_read = _usage_int(usage, "cache_read")
        cache_creation = _usage_int(usage, "cache_creation")
        uncached_input = _usage_int(usage, "uncached_input")
        output = _usage_int(usage, "output")

        self.total.add(cache_read, cache_creation, uncached_input, output)
        bucket = self.by_source.setdefault(source, UsageBucket())
        bucket.add(cache_read, cache_creation, uncached_input, output)
        if context_hash:
            self.context_hashes.add(context_hash)

        diagnostics = usage.get("cache_diagnostics")
        if not isinstance(diagnostics, dict):
            diagnostics = {}
        warnings = [
            str(item)
            for item in diagnostics.get("warnings", [])
            if item is not None
        ]
        signature = diagnostics.get("cache_control_signature")

        state_key = f"{provider}:{model}:{conversation_id}"
        state = self._states.setdefault(state_key, UsageState())
        if state.prev_signature is not None and signature != state.prev_signature:
            warnings.append(
                f"{source}: cache_control_signature changed for {conversation_id}"
            )
        if state.prev_creation > 0 and cache_read == 0:
            warnings.append(
                f"{source}: cache miss after creation for {conversation_id} "
                f"(prev_create={state.prev_creation}, curr_read=0)"
            )
        state.prev_creation = cache_creation
        state.prev_signature = signature if isinstance(signature, str) else None

        if warnings:
            self.warnings.extend(warnings)

        cache_diagnostics = {
            **diagnostics,
            "warnings": warnings,
        }
        if context_hash:
            cache_diagnostics["context_hash"] = context_hash

        return {
            "source": source,
            "conversation_id": conversation_id,
            "provider": provider,
            "model": model,
            "cache_read": cache_read,
            "cache_creation": cache_creation,
            "uncached_input": uncached_input,
            "output": output,
            "cache_read_rate": _cache_rate(
                cache_read,
                cache_creation,
                uncached_input,
            ),
            "cache_reuse_rate": _cache_rate(cache_read, uncached_input),
            "estimated_cost_usd": None,
            "cache_diagnostics": cache_diagnostics,
        }

    def summary(self) -> JsonDict:
        summary = self.total.summary()
        summary.update({
            "hit_rate": summary["cache_read_rate"],
            "estimated_cost_usd": None,
            "warnings": self.warnings,
            "context_hashes": sorted(self.context_hashes),
            "by_source": {
                source: bucket.summary()
                for source, bucket in sorted(self.by_source.items())
            },
        })
        return summary


class RunLogger:
    def __init__(
        self,
        worktree_dir: str,
        task_id: Optional[str] = None,
        on_event: Optional[EventSink] = None,
    ):
        self.task_id = task_id or uuid.uuid4().hex
        self.task_dir = Path(worktree_dir) / self.task_id
        self.artifacts_dir = self.task_dir / "artifacts"
        self.task_dir.mkdir(parents=True, exist_ok=True)
        self.artifacts_dir.mkdir(parents=True, exist_ok=True)
        self.path = self.task_dir / "run.jsonl"
        self.usage_aggregator = UsageAggregator()
        self._usage_summary_written = False
        self.on_event = on_event

    def write(self, event_type: str, payload: JsonDict) -> None:
        event = {
            "ts": datetime.now().isoformat(timespec="seconds"),
            "taskId": self.task_id,
            "type": event_type,
            "payload": payload,
        }
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(event, ensure_ascii=False, default=str) + "\n")
        if self.on_event:
            try:
                self.on_event(event_type, payload)
            except Exception:
                pass

    def record_llm_usage(
        self,
        *,
        source: str,
        provider: str,
        model: str,
        usage: JsonDict,
        step: Optional[int] = None,
        conversation_id: Optional[str] = None,
        context_hash: Optional[str] = None,
    ) -> JsonDict:
        payload = self.usage_aggregator.add(
            usage,
            source=source,
            provider=provider,
            model=model,
            conversation_id=conversation_id or source,
            context_hash=context_hash,
        )
        if step is not None:
            payload["step"] = step
        self.write("llm.usage", payload)
        return payload

    def write_usage_summary(self) -> None:
        if self._usage_summary_written:
            return
        self.write("llm.usage_summary", self.usage_aggregator.summary())
        self._usage_summary_written = True


def make_browser_event_logger(
    logger: RunLogger,
    enabled: bool,
    prefix: str = "browser.transport",
) -> Optional[Any]:
    if not enabled:
        return None

    def on_event(event_type: str, payload: JsonDict) -> None:
        logger.write(f"{prefix}.{event_type}", payload)

    return on_event


def trim_large_strings(value: Any, max_chars: int) -> Any:
    if isinstance(value, str):
        if len(value) <= max_chars:
            return value
        return value[:max_chars] + f"... <truncated {len(value) - max_chars} chars>"
    if isinstance(value, list):
        return [trim_large_strings(item, max_chars) for item in value]
    if isinstance(value, dict):
        return {
            key: trim_large_strings(item, max_chars)
            for key, item in value.items()
        }
    return value


def strip_llm_hidden_fields(value: Any) -> Any:
    """Remove server steering fields from model-facing payloads.

    Raw run logs keep the original ABCP response. This function is only for
    values that are about to be placed back into the LLM conversation.
    """
    if isinstance(value, dict):
        return {
            key: strip_llm_hidden_fields(item)
            for key, item in value.items()
            if key not in LLM_HIDDEN_FIELDS
        }
    if isinstance(value, list):
        return [strip_llm_hidden_fields(item) for item in value]
    if isinstance(value, str) and "suggested_prompt" in value:
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            cleaned = _SUGGESTED_PROMPT_STRING_RE.sub("", value)
            return cleaned.replace("{,", "{").replace(",}", "}")
        return json.dumps(
            strip_llm_hidden_fields(parsed),
            ensure_ascii=False,
            default=str,
        )
    return value


def json_size_bytes(value: Any) -> int:
    return len(
        json.dumps(value, ensure_ascii=False, default=str).encode("utf-8")
    )


def safe_path_component(value: Any, fallback: str = "item") -> str:
    text = str(value or fallback).strip() or fallback
    text = re.sub(r"[^A-Za-z0-9_.-]+", "-", text)
    text = text.strip(".-")
    return text[:80] or fallback


def task_subdir(logger: RunLogger, name: str) -> Path:
    path = logger.task_dir / name
    path.mkdir(parents=True, exist_ok=True)
    return path


def write_context_snapshot(
    logger: RunLogger,
    *,
    actor: str,
    name: str,
    system_prompt: str,
    messages: List[JsonDict],
    tools: List[JsonDict],
    metadata: Optional[JsonDict] = None,
) -> str:
    """Persist the compacted model context currently held by an agent."""
    safe_name = safe_path_component(name, fallback=actor)
    path = task_subdir(logger, "contexts") / f"{safe_name}-final-context.json"
    payload = {
        "actor": actor,
        "name": name,
        "metadata": metadata or {},
        "system_prompt": system_prompt,
        "messages": messages,
        "tools": tools,
    }
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    logger.write(
        "context.snapshot.saved",
        {
            "actor": actor,
            "name": name,
            "path": str(path.resolve()),
            "messageCount": len(messages),
            "toolCount": len(tools),
        },
    )
    return str(path.resolve())


def resolve_task_file(logger: RunLogger, raw_path: Any) -> Tuple[Optional[Path], Optional[str]]:
    if not isinstance(raw_path, str) or not raw_path.strip():
        return None, "path must be a non-empty string"
    root = logger.task_dir.resolve()
    candidate = Path(raw_path).expanduser()
    if not candidate.is_absolute():
        candidate = root / candidate
    try:
        resolved = candidate.resolve()
    except OSError as exc:
        return None, str(exc)
    try:
        resolved.relative_to(root)
    except ValueError:
        return None, f"path escapes the current task worktree: {raw_path}"
    return resolved, None


def extract_offloaded_paths(value: Any) -> List[str]:
    paths: List[str] = []
    stack = [value]
    while stack:
        item = stack.pop()
        if isinstance(item, dict):
            if item.get("_offloaded") and isinstance(item.get("savedPath"), str):
                paths.append(item["savedPath"])
            stack.extend(item.values())
        elif isinstance(item, list):
            stack.extend(item)
    return sorted(set(paths))


def outline_value(value: Any, max_items: int = 10, depth: int = 2) -> Any:
    if depth <= 0:
        if isinstance(value, dict):
            return {
                "type": "object",
                "keys": list(value.keys())[:max_items],
                "keyCount": len(value),
            }
        if isinstance(value, list):
            return {"type": "array", "length": len(value)}
        text = str(value)
        return text[:240] + ("..." if len(text) > 240 else "")

    if isinstance(value, dict):
        summary: JsonDict = {}
        for key in ("id", "nodeId", "tag", "role", "name", "text", "value"):
            if key in value and value[key] is not None:
                item = value[key]
                summary[key] = (
                    item[:240] + "..."
                    if isinstance(item, str) and len(item) > 240
                    else item
                )
        children = value.get("children")
        if isinstance(children, list):
            summary["childCount"] = len(children)
            summary["children"] = [
                outline_value(child, max_items=max_items, depth=depth - 1)
                for child in children[:max_items]
            ]
        if not summary:
            for key, item in list(value.items())[:max_items]:
                summary[str(key)] = outline_value(
                    item,
                    max_items=max_items,
                    depth=depth - 1,
                )
        return summary

    if isinstance(value, list):
        return [
            outline_value(item, max_items=max_items, depth=depth - 1)
            for item in value[:max_items]
        ]

    text = str(value)
    return text[:1000] + ("..." if len(text) > 1000 else "")


def count_json_nodes(value: Any) -> int:
    if isinstance(value, dict):
        return 1 + sum(count_json_nodes(item) for item in value.values())
    if isinstance(value, list):
        return 1 + sum(count_json_nodes(item) for item in value)
    return 1


def truncate_utf8_text(text: str, max_bytes: int) -> Tuple[str, bool]:
    max_bytes = max(0, optional_int(max_bytes, 0) or 0)
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text, False
    suffix = "... <truncated>"
    suffix_bytes = suffix.encode("utf-8")
    if max_bytes <= len(suffix_bytes):
        return encoded[:max_bytes].decode("utf-8", errors="ignore"), True
    head = encoded[:max_bytes - len(suffix_bytes)].decode("utf-8", errors="ignore")
    return head + suffix, True


def fit_json_node_for_output(value: Any, max_bytes: int) -> Any:
    max_bytes = max(100, max_bytes)
    try:
        encoded = json.dumps(value, ensure_ascii=False, default=str).encode("utf-8")
    except (TypeError, ValueError):
        text, truncated = truncate_utf8_text(str(value), max_bytes)
        return {
            "_truncated": truncated,
            "originalBytes": len(str(value).encode("utf-8")),
            "value": text,
        }
    if len(encoded) <= max_bytes:
        return value
    return {
        "_truncated": True,
        "originalBytes": len(encoded),
        "outline": outline_value(value),
    }


def optional_int(value: Any, default: Optional[int] = None) -> Optional[int]:
    if value is None or value == "":
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def optional_float(value: Any, default: Optional[float] = None) -> Optional[float]:
    if value is None or value == "":
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def exception_payload(exc: BaseException, **extra: Any) -> JsonDict:
    payload: JsonDict = {
        "errorType": type(exc).__name__,
        "error": str(exc),
        "traceback": "".join(
            traceback.format_exception(type(exc), exc, exc.__traceback__)
        ),
    }
    payload.update(extra)
    return payload
