"""
harness.utils - Shared helpers for ABCP agent harness modules.
"""

import hashlib
from html import escape
import json
import re
import traceback
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set, Tuple


from harness.events.factory import RunEventSequencer

JsonDict = Dict[str, Any]
EventSink = Callable[[str, JsonDict], None]
LLM_HIDDEN_FIELDS: Set[str] = set()


_ENGLISH_SEMANTIC_NEGATION_RE = re.compile(
    r"(?:"
    r"\b(?:do|does|did|must|should|shall|will|would|can|could)\s+not\b"
    r"|\b(?:don't|doesn't|didn't|mustn't|shouldn't|shan't|won't|"
    r"wouldn't|can't|cannot|couldn't)\b"
    r"|\bnever\b|\bwithout\b|\bno\s+need\s+to\b"
    r")"
    r"(?:[\s,:()\[\]{}\"'`-]+[a-z0-9_.]+){0,6}"
    r"[\s,:()\[\]{}\"'`.-]*$",
    re.IGNORECASE,
)
_CJK_SEMANTIC_NEGATION_RE = re.compile(
    r"(?:不要|不得|禁止|无需|不需要|不可|切勿)"
    r"[^。！？!?;；\n]{0,24}$"
)
_PARENTHETICAL_SEMANTIC_NEGATION_RE = re.compile(
    r"(?:"
    r"\b(?:do|does|did|must|should|shall|will|would|can|could)\s+not\b"
    r"|\b(?:don't|doesn't|didn't|mustn't|shouldn't|shan't|won't|"
    r"wouldn't|can't|cannot|couldn't)\b"
    r"|\bnever\b|不要|不得|禁止|无需|不需要|不可|切勿"
    r")\s*[,，][^,，!?;。！？；\n]{1,64}[,，][^,，]*$",
    re.IGNORECASE,
)
_CONDITIONAL_SEMANTIC_LEAD_RE = re.compile(
    r"\b(?:if|when|unless|while|once|provided|assuming)\b"
    r"|(?:如果|若|当|除非|倘若|假如)",
    re.IGNORECASE,
)


def semantic_marker_spans(text: str, marker: str) -> List[Tuple[int, int]]:
    """Return token-safe spans for a semantic marker.

    Latin markers use alphanumeric boundaries so ``auth`` does not match
    ``author``. CJK markers retain substring semantics because they are not
    whitespace-delimited.
    """
    haystack = str(text or "").lower()
    needle = str(marker or "").lower()
    if not needle:
        return []
    if re.fullmatch(r"[a-z0-9 ._-]+", needle):
        pattern = re.escape(needle).replace(r"\ ", r"\s+")
        return [
            match.span()
            for match in re.finditer(
                rf"(?<![a-z0-9]){pattern}(?![a-z0-9])",
                haystack,
            )
        ]
    spans: List[Tuple[int, int]] = []
    offset = 0
    while True:
        start = haystack.find(needle, offset)
        if start < 0:
            return spans
        end = start + len(needle)
        spans.append((start, end))
        offset = end


def contains_semantic_marker(text: str, marker: str) -> bool:
    """Return whether ``marker`` occurs with token-safe semantics."""
    return bool(semantic_marker_spans(text, marker))


def _has_direct_parenthetical_negation(sentence_prefix: str) -> bool:
    """Distinguish a direct prohibition from a negated conditional branch."""
    match = _PARENTHETICAL_SEMANTIC_NEGATION_RE.search(sentence_prefix)
    if match is None:
        return False
    previous_comma = max(
        sentence_prefix.rfind(",", 0, match.start()),
        sentence_prefix.rfind("，", 0, match.start()),
    )
    negation_lead = sentence_prefix[previous_comma + 1:match.start()]
    return _CONDITIONAL_SEMANTIC_LEAD_RE.search(negation_lead) is None


def contains_affirmative_semantic_marker(text: str, marker: str) -> bool:
    """Return whether at least one marker occurrence is not locally negated.

    This is intentionally local rather than a general natural-language
    negation engine. It protects imperative phase contracts such as
    ``do NOT call Hitl.requestPause`` / ``不要调用 Hitl.requestPause`` while
    still treating a later affirmative occurrence in the same text as an
    execution instruction.
    """
    source = str(text or "")
    for start, _end in semantic_marker_spans(source, marker):
        prefix = source[max(0, start - 96):start]
        # Negation is clause-local. A prohibited occurrence in a previous
        # sentence must not suppress a later affirmative instruction.
        # A dot is a sentence boundary when followed by whitespace, an
        # uppercase Latin character, or CJK text.
        # The trailing dot in the prefix of the shorter ``requestpause``
        # alias is the method separator from ``Hitl.requestPause`` and must
        # retain the preceding negation context.
        sentence_prefix = re.split(
            r"[!?;。！？；\n]|\.(?=\s|[A-Z\u3400-\u9fff])",
            prefix,
        )[-1]
        # A comma normally separates the negated condition/action from the
        # later affirmative instruction ("cannot proceed, request HITL").
        # Preserve an immediately parenthesized prohibition such as
        # "Do not, under any circumstances, call HITL", except when the
        # negation belongs to a conditional branch ("If you cannot, stop,
        # otherwise call HITL").
        clause_prefix = (
            sentence_prefix
            if _has_direct_parenthetical_negation(sentence_prefix)
            else re.split(r"[,，]", sentence_prefix)[-1]
        )
        if _ENGLISH_SEMANTIC_NEGATION_RE.search(clause_prefix):
            continue
        if _CJK_SEMANTIC_NEGATION_RE.search(clause_prefix):
            continue
        return True
    return False


def _resolve_context_file(context_file: Optional[str]) -> Optional[Path]:
    if not context_file:
        return None
    path = Path(context_file).expanduser()
    if not path.is_absolute():
        path = Path.cwd() / path
    return path


def _project_context_specs(
    context_file: Optional[str],
    project_context_files: Optional[Any],
) -> List[JsonDict]:
    """Normalize trusted, static project-instruction configuration.

    ``context_file`` is retained as the legacy one-file spelling.  The newer
    list deliberately preserves configured order: that order is the only
    precedence signal exposed to the model, rather than an inferred filesystem
    hierarchy or the process working directory.
    """

    specs: List[JsonDict] = []
    if context_file and str(context_file).strip():
        specs.append({
            "path": str(context_file).strip(),
            "scope": "legacy_context_file",
        })
    if not isinstance(project_context_files, list):
        return specs
    for item in project_context_files:
        if isinstance(item, str) and item.strip():
            specs.append({"path": item.strip(), "scope": "project"})
            continue
        if not isinstance(item, dict):
            continue
        raw_path = item.get("path")
        if not isinstance(raw_path, str) or not raw_path.strip():
            continue
        raw_scope = item.get("scope", "project")
        scope = (
            str(raw_scope).strip()
            if isinstance(raw_scope, str) and str(raw_scope).strip()
            else "project"
        )
        specs.append({"path": raw_path.strip(), "scope": scope})
    return specs


def build_static_context_block(
    context_file: Optional[str],
    *,
    project_context_files: Optional[Any] = None,
    append_system_prompt: Optional[str] = None,
) -> Tuple[str, Optional[str]]:
    """Build the stable deployment/project portion of a system prompt.

    Files are operator-configured, read once at agent construction and emitted
    as escaped XML.  They are project-scoped instructions, not executable
    prompt syntax; escaping prevents a file body from closing or manufacturing
    prompt sections.  Relative paths are retained in the visible ``path``
    attribute so a machine-specific current working directory never leaks into
    the prompt.
    """

    project_entries: List[JsonDict] = []
    seen_paths: Set[str] = set()
    for spec in _project_context_specs(context_file, project_context_files):
        raw_path = str(spec["path"])
        path = _resolve_context_file(raw_path)
        if path is None:
            continue
        try:
            resolved_key = str(path.resolve())
            content = path.read_text(encoding="utf-8")
        except OSError:
            continue
        # The same configured source is never injected twice through the
        # legacy and list forms.  First occurrence wins, matching list order.
        if resolved_key in seen_paths:
            continue
        seen_paths.add(resolved_key)
        project_entries.append({
            "path": raw_path,
            "scope": str(spec["scope"]),
            "sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
            "content": content,
        })

    append_text = (
        append_system_prompt.strip()
        if isinstance(append_system_prompt, str)
        else ""
    )
    rendered: List[str] = []
    if append_text:
        append_digest = hashlib.sha256(append_text.encode("utf-8")).hexdigest()
        rendered.extend([
            f'<append_system_prompt sha256="{append_digest}">',
            "Deployment-specific static instructions. They remain subject to "
            "the surrounding system policy and live tool schemas.",
            escape(append_text),
            "</append_system_prompt>",
        ])
    if project_entries:
        rendered.extend([
            '<project_context version="1">',
            "Project-specific instructions and guidelines. Earlier entries "
            "are broader context; later entries may refine them for their "
            "declared scope, but none override surrounding system policy or "
            "live tool schemas.",
        ])
        for entry in project_entries:
            rendered.extend([
                "<project_instructions "
                f'path="{escape(entry["path"], quote=True)}" '
                f'scope="{escape(entry["scope"], quote=True)}" '
                f'sha256="{entry["sha256"]}">',
                escape(entry["content"]),
                "</project_instructions>",
            ])
        rendered.append("</project_context>")
    if not rendered:
        return "", None
    block = "\n\n" + "\n".join(rendered)
    fingerprint_payload = {
        "version": 1,
        "appendSystemPrompt": append_text,
        "projectInstructions": [
            {
                "path": entry["path"],
                "scope": entry["scope"],
                "sha256": entry["sha256"],
            }
            for entry in project_entries
        ],
    }
    digest = hashlib.sha256(
        json.dumps(
            fingerprint_payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
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
    timeout_retries: int = 0
    degenerate_retries: int = 0
    connection_retries: int = 0
    stream_decode_retries: int = 0

    def add(
        self,
        cache_read: int,
        cache_creation: int,
        uncached_input: int,
        output: int,
        timeout_retries: int = 0,
        degenerate_retries: int = 0,
        connection_retries: int = 0,
        stream_decode_retries: int = 0,
    ) -> None:
        self.calls += 1
        self.cache_read += cache_read
        self.cache_creation += cache_creation
        self.uncached_input += uncached_input
        self.output += output
        self.add_retries(
            timeout_retries=timeout_retries,
            degenerate_retries=degenerate_retries,
            connection_retries=connection_retries,
            stream_decode_retries=stream_decode_retries,
        )

    def add_retries(
        self,
        *,
        timeout_retries: int = 0,
        degenerate_retries: int = 0,
        connection_retries: int = 0,
        stream_decode_retries: int = 0,
    ) -> None:
        self.timeout_retries += timeout_retries
        self.degenerate_retries += degenerate_retries
        self.connection_retries += connection_retries
        self.stream_decode_retries += stream_decode_retries

    def summary(self) -> JsonDict:
        return {
            "calls": self.calls,
            "cache_read": self.cache_read,
            "cache_creation": self.cache_creation,
            "uncached_input": self.uncached_input,
            "output": self.output,
            "timeout_retries": self.timeout_retries,
            "degenerate_retries": self.degenerate_retries,
            "connection_retries": self.connection_retries,
            "stream_decode_retries": self.stream_decode_retries,
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

    def add_failed_call_retries(self, usage: JsonDict, *, source: str) -> None:
        """Fold retries from a call that raised instead of returning.

        Such a call has no token usage to report, and counting it under `calls`
        would dilute the per-call averages with a turn that produced nothing —
        so only the retry counters move.
        """
        counters = {
            key: _usage_int(usage, key)
            for key in (
                "timeout_retries",
                "degenerate_retries",
                "connection_retries",
                "stream_decode_retries",
            )
        }
        if not any(counters.values()):
            return
        for bucket in (
            self.total,
            self.by_source.setdefault(source, UsageBucket()),
        ):
            bucket.add_retries(**counters)

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
        timeout_retries = _usage_int(usage, "timeout_retries")
        degenerate_retries = _usage_int(usage, "degenerate_retries")
        connection_retries = _usage_int(usage, "connection_retries")
        stream_decode_retries = _usage_int(usage, "stream_decode_retries")

        for bucket in (
            self.total,
            self.by_source.setdefault(source, UsageBucket()),
        ):
            bucket.add(
                cache_read,
                cache_creation,
                uncached_input,
                output,
                timeout_retries=timeout_retries,
                degenerate_retries=degenerate_retries,
                connection_retries=connection_retries,
                stream_decode_retries=stream_decode_retries,
            )
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
            "timeout_retries": timeout_retries,
            "degenerate_retries": degenerate_retries,
            "connection_retries": connection_retries,
            "stream_decode_retries": stream_decode_retries,
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


def read_task_file_text(logger: Any, raw_path: Any) -> Optional[str]:
    """Read a task-scoped file's text from disk or from the backend.

    Every internal reader must go through here rather than ``Path.read_text``.
    ``local_fs_read`` alone is not enough: it only serves the model, while the
    gates that decide whether a phase completed - artifact validation, resume
    integrity, worker result summaries - read the same paths themselves. When
    those kept reading the filesystem directly, a db-mode extraction artifact
    existed, was readable by the agent, and was still judged missing.
    """

    resolved, error = resolve_task_file(logger, raw_path)
    if error or resolved is None:
        return None
    from harness.storage.virtual_fs import db_authoritative_for, virtual_fs_for

    if db_authoritative_for(logger):
        try:
            relative = str(resolved.relative_to(Path(logger.task_dir).resolve()))
        except (OSError, ValueError):
            return None
        view = virtual_fs_for(logger)
        if view is not None:
            lines = view.iter_lines(relative)
            if lines is not None:
                return "".join(lines)
    if resolved.is_file():
        try:
            return resolved.read_text(encoding="utf-8")
        except OSError:
            return None
    view = virtual_fs_for(logger)
    if view is None:
        return None
    try:
        relative = str(resolved.relative_to(Path(logger.task_dir).resolve()))
    except (OSError, ValueError):
        return None
    lines = view.iter_lines(relative)
    if lines is None:
        return None
    # The view's lines carry their terminators, so concatenating reproduces
    # the file byte for byte - including whether it ends in a newline, which
    # "\n".join would have silently dropped.
    return "".join(lines)


def task_file_exists(logger: Any, raw_path: Any) -> bool:
    """True when a task path resolves to bytes, wherever they are stored."""

    resolved, error = resolve_task_file(logger, raw_path)
    if error or resolved is None:
        return False
    from harness.storage.virtual_fs import db_authoritative_for, virtual_fs_for

    if db_authoritative_for(logger):
        try:
            relative = str(resolved.relative_to(Path(logger.task_dir).resolve()))
        except (OSError, ValueError):
            return False
        view = virtual_fs_for(logger)
        if view is not None and view.exists(relative):
            return True
    if resolved.is_file():
        return True
    view = virtual_fs_for(logger)
    if view is None:
        return False
    try:
        relative = str(resolved.relative_to(Path(logger.task_dir).resolve()))
    except (OSError, ValueError):
        return False
    return view.exists(relative)


def load_task_json(logger: Any, raw_path: Any) -> Optional[Any]:
    """Parse a task-scoped JSON document from whichever backend holds it."""

    text = read_task_file_text(logger, raw_path)
    if text is None:
        return None
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return None


def storage_for_logger(logger: Any) -> Tuple[Any, str]:
    """Resolve ``(storage, task_id)`` from anything logger-shaped.

    ``task_dir`` is the anchor the rest of this codebase already treats as
    authoritative, and plenty of callers pass a lightweight object carrying
    only that. Deriving the task id and a file backend from it keeps those
    callers working; a real RunLogger hands over its own backend instead,
    which may be the database.
    """

    task_dir = Path(getattr(logger, "task_dir", "") or ".")
    task_id = str(getattr(logger, "task_id", "") or "") or task_dir.name
    storage = getattr(logger, "storage", None)
    if storage is None:
        # Imported lazily: harness.storage.file_store depends on this module.
        from harness.storage.file_store import FileStore

        storage = FileStore(worktree_dir=str(task_dir.parent))
    return storage, task_id


class RunLogger:
    def __init__(
        self,
        worktree_dir: str,
        task_id: Optional[str] = None,
        on_event: Optional[EventSink] = None,
        run_id: str = "",
        storage: Optional[Any] = None,
    ):
        self.task_id = task_id or uuid.uuid4().hex
        self.worktree_dir = str(worktree_dir)
        self.task_dir = Path(worktree_dir) / self.task_id
        self.artifacts_dir = self.task_dir / "artifacts"
        self.task_dir.mkdir(parents=True, exist_ok=True)
        self.artifacts_dir.mkdir(parents=True, exist_ok=True)
        self.path = self.task_dir / "run.jsonl"
        self.usage_aggregator = UsageAggregator()
        self._usage_summary_written = False
        self.on_event = on_event
        # run_id is a relational field, not payload context: it identifies
        # which launch or resume produced an event and is a foreign key in the
        # database backend. bind_context must never be used to carry it.
        self.run_id = str(run_id or "")
        self._storage = storage
        # Every event this run produces draws from one sequence, so lead and
        # worker events interleave into a single totally ordered stream.
        self._sequencer = RunEventSequencer()
        self._emitter: Optional[Any] = None
        self._event_factory: Optional[Any] = None
        self._storage_sink: Optional[Any] = None

    @property
    def storage(self) -> Any:
        """The configured backend, defaulting to the historical file layout.

        Constructed lazily so the 100+ existing RunLogger call sites - tests
        included - keep writing exactly the files they always did without
        passing anything new.
        """

        if self._storage is None:
            from harness.storage.file_store import FileStore

            self._storage = FileStore(worktree_dir=self.worktree_dir)
        return self._storage

    @property
    def storage_attached(self) -> bool:
        """True once a backend was attached, without lazily creating one."""

        return self._storage is not None

    def attach_storage(self, storage: Any) -> None:
        """Swap in a configured backend after construction.

        main.py builds the logger before it knows the run id, so the backend
        arrives in the same late-binding step.
        """

        self._storage = storage

    @property
    def emitter(self) -> Any:
        """The fan-out this logger writes through.

        Built on first use because main.py attaches the real backend after
        construction; binding the storage sink to a callable rather than an
        object means a later attach_storage() is picked up automatically.
        """

        if self._emitter is None:
            from harness.events.emitter import RunEventEmitter
            from harness.events.sinks import ConsoleEventSink, StorageEventSink

            emitter = RunEventEmitter()
            # Order matters: the audit row is written before anything is
            # printed, so a crash mid-dispatch cannot leave a line on screen
            # that no record backs up.
            self._storage_sink = StorageEventSink(lambda: self.storage)
            emitter.add_sink(self._storage_sink, critical=True)
            emitter.add_sink(
                ConsoleEventSink(lambda event_type, payload: (
                    self.on_event(event_type, payload) if self.on_event else None
                )),
                critical=False,
            )
            self._emitter = emitter
        return self._emitter

    def set_persist_message_content(self, enabled: bool) -> None:
        """Let assistant text reach storage through message_end events.

        Off while the legacy `agent.model` / `lead.model` events still carry
        the same text; turning it on is what retires them.
        """

        self.emitter  # ensure the sink exists
        if self._storage_sink is not None:
            self._storage_sink.set_persist_message_content(enabled)

    @property
    def event_factory(self) -> Any:
        """Typed event source for this run, sharing the logger's sequence."""

        if self._event_factory is None:
            from harness.events.factory import EventFactory
            from harness.events.models import EventContext
            from harness.events.publisher import CallbackPublisher

            self._event_factory = EventFactory(
                context=EventContext(
                    task_id=self.task_id, run_id=str(self.run_id or ""),
                ),
                sequencer=self._sequencer,
                publisher=CallbackPublisher(self.emitter.emit),
            )
        return self._event_factory

    def write(
        self,
        event_type: str,
        payload: JsonDict,
        *,
        event_context: Optional[Any] = None,
    ) -> None:
        factory = self.event_factory
        context = event_context or factory.context
        if isinstance(payload, dict) and not context.worker_id:
            # Unbound call sites have always carried workerId in the payload;
            # promoting it keeps the relational column filled without asking
            # 370 call sites to change.
            worker_id = str(payload.get("workerId") or "") or None
            if worker_id:
                context = context.merge(worker_id=worker_id)
        factory.legacy(event_type, payload, context=context)

    def bind_context(self, **context: Any) -> "BoundRunLogger":
        """Return a logger view that injects immutable event identity.

        The underlying file, event sink, task paths, and usage aggregator stay
        shared.  Context is merged last so a caller cannot spoof or accidentally
        overwrite coordinator-owned worker identity.
        """
        return BoundRunLogger(self, context)

    def record_llm_retries(self, *, source: str, usage: JsonDict) -> None:
        """Account retries for a model call that raised instead of returning.

        The failure itself is already reported as its own event; this only
        keeps the retry counters in the usage summary honest.
        """
        self.usage_aggregator.add_failed_call_retries(usage, source=source)

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
        for key in (
            "timeout_attempts",
            "timeout_seconds",
            "timeout_max_retries",
            "timeout_retry_interval_seconds",
        ):
            if key in usage:
                payload[key] = usage[key]
        self.write("llm.usage", payload)
        return payload

    def write_usage_summary(self) -> None:
        if self._usage_summary_written:
            return
        self.write("llm.usage_summary", self.usage_aggregator.summary())
        self._usage_summary_written = True


class BoundRunLogger:
    """Immutable per-actor view over a task-scoped :class:`RunLogger`."""

    # bind_context() speaks the payload's camelCase; EventContext is the
    # relational form of the same identity. One table, so the two can never
    # drift into disagreeing about who wrote an event.
    _CONTEXT_FIELDS = {
        "workerId": "worker_id",
        "slotId": "slot_id",
        "agentId": "agent_id",
        "phaseId": "phase_id",
        "actorType": "actor_type",
    }

    def __init__(self, logger: RunLogger, context: Dict[str, Any]):
        self._logger = logger
        self._context = dict(context)
        self._event_context: Optional[Any] = None

    @property
    def event_context(self) -> Any:
        if self._event_context is None:
            patch = {
                field: str(self._context[key])
                for key, field in self._CONTEXT_FIELDS.items()
                if self._context.get(key)
            }
            self._event_context = self._logger.event_factory.context.merge(**patch)
        return self._event_context

    def bound_event_factory(self) -> Any:
        """A typed event factory carrying this view's actor identity."""

        context = self.event_context
        return self._logger.event_factory.bind(
            **context.model_dump(exclude_none=True, exclude={"task_id", "run_id"})
        )

    def __getattr__(self, name: str) -> Any:
        return getattr(self._logger, name)

    def bind_context(self, **context: Any) -> "BoundRunLogger":
        return BoundRunLogger(
            self._logger,
            {**self._context, **dict(context)},
        )

    def write(
        self,
        event_type: str,
        payload: JsonDict,
        *,
        event_context: Optional[Any] = None,
    ) -> None:
        self._logger.write(
            event_type,
            {**dict(payload or {}), **self._context},
            event_context=event_context or self.event_context,
        )

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
        payload = self._logger.usage_aggregator.add(
            usage,
            source=source,
            provider=provider,
            model=model,
            conversation_id=conversation_id or source,
            context_hash=context_hash,
        )
        if step is not None:
            payload["step"] = step
        for key in (
            "timeout_attempts",
            "timeout_seconds",
            "timeout_max_retries",
            "timeout_retry_interval_seconds",
        ):
            if key in usage:
                payload[key] = usage[key]
        self.write("llm.usage", payload)
        return payload


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
    """Remove fields configured as hidden from model-facing payloads.

    `suggested_prompt` is intentionally model-facing: ABCP uses it for
    next-step and recovery guidance, so it must remain in context.
    """
    if not LLM_HIDDEN_FIELDS:
        return value
    if isinstance(value, dict):
        return {
            key: strip_llm_hidden_fields(item)
            for key, item in value.items()
            if key not in LLM_HIDDEN_FIELDS
        }
    if isinstance(value, list):
        return [strip_llm_hidden_fields(item) for item in value]
    if isinstance(value, str) and any(field in value for field in LLM_HIDDEN_FIELDS):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return value
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
    run_id: Optional[str] = None,
) -> str:
    """Persist the compacted model context currently held by an agent.

    Existing callers retain the historical ``<name>-final-context.json``
    filename.  Resume-aware callers can provide ``run_id`` to retain one
    snapshot per harness run instead of overwriting the previous run's audit
    record.
    """
    safe_name = safe_path_component(name, fallback=actor)
    safe_run_id = safe_path_component(run_id, fallback="run") if run_id else ""
    filename = (
        f"{safe_name}-{safe_run_id}-final-context.json"
        if safe_run_id
        else f"{safe_name}-final-context.json"
    )
    path = task_subdir(logger, "contexts") / filename
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
    event = {
        "actor": actor,
        "name": name,
        "path": str(path.resolve()),
        "messageCount": len(messages),
        "toolCount": len(tools),
    }
    if run_id:
        event["runId"] = str(run_id)
    logger.write("context.snapshot.saved", event)
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
