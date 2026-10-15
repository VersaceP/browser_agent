"""The first-phase Skill Builder application session."""

from __future__ import annotations

import hashlib
import json
import asyncio
import os
import re
import shutil
import sys
import uuid
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from harness._vendor.tau_agent import AgentTool, AgentToolResult
from harness._vendor.tau_agent.events import MessageEndEvent, ToolExecutionEndEvent
from harness._vendor.tau_agent.messages import AgentMessage, AssistantMessage, TextContent
from harness.skill_builder.browser import BuilderBrowser
from harness.skill_builder.catalog import SkillCatalog, inspect_skill, validate_skill_id
from harness.skill_builder.context import SourceTaskContext, task_references
from harness.skill_builder.prompt import system_prompt
from harness.skill_builder.provider import HarnessProviderAdapter
from harness.storage.factory import resolve_sqlite_path
from harness.storage.sqlite_connection import write_transaction
from harness.storage.sqlite_store import SqliteStore
from harness.tools.browser_tools.dispatch import BROWSER_TOOLS
from harness.tools.path_authorization import (
    LOCAL_AUTHORIZATION_DESCRIPTION, LOCAL_AUTHORIZATION_SCHEMA, authorize_local_paths,
)
from harness.utils import RunLogger
from harness.version import HARNESS_VERSION
from llm.factory import LLMFactory
from pydantic import TypeAdapter


_MAX_FILE_BYTES = 2_000_000


class WorkFileWriteError(RuntimeError):
    """Preserve partial filesystem effects when indexing/logging fails."""

    def __init__(self, cause, outcome):
        super().__init__(str(cause))
        self.payload = {"status": "error", "type": type(cause).__name__,
            "message": str(cause)[:800], "writeOutcome": outcome,
            "next": "Read the current file/hash and reconcile its index before retrying. No browser action was executed by this write."}


def _result(value: Any, *, error: bool = False, terminate: bool = False) -> AgentToolResult:
    payload = json.dumps(value, ensure_ascii=False, default=str)
    return AgentToolResult(content=[TextContent(text=payload)],
                           details={"is_error": error}, terminate=terminate)


def _schema(properties: dict[str, Any], required: tuple[str, ...] = ()) -> dict[str, Any]:
    return {"type": "object", "properties": properties, "required": list(required),
            "additionalProperties": False}


_STRING = {"type": "string"}
_OBJECT = {"type": "object"}


def _prepare_tool_arguments(value: object, schema: dict[str, Any]) -> dict[str, Any]:
    """Validate the JSON Schema subset used by Builder tool declarations."""
    def check(item: Any, rule: dict[str, Any], path: str) -> None:
        # Untyped schemas (e.g. continueWhen.equals: {}) admit JSON values;
        # description-only reference schemas do not imply an object type.
        kinds = rule.get("type")
        if kinds is not None:
            allowed = kinds if isinstance(kinds, list) else [kinds]
            matching = {
                "object": lambda x: isinstance(x, dict),
                "array": lambda x: isinstance(x, list),
                "string": lambda x: isinstance(x, str),
                "integer": lambda x: isinstance(x, int) and not isinstance(x, bool),
                "number": lambda x: isinstance(x, (int, float)) and not isinstance(x, bool),
                "boolean": lambda x: isinstance(x, bool),
                "null": lambda x: x is None,
            }
            if not any(matching[kind](item) for kind in allowed):
                raise ValueError(f"{path}: expected {allowed}")
        if isinstance(item, dict):
            props = rule.get("properties") or {}
            missing = set(rule.get("required") or []) - item.keys()
            if missing:
                raise ValueError(f"{path}: missing {sorted(missing)}")
            if rule.get("additionalProperties") is False:
                unknown = item.keys() - props.keys()
                if unknown:
                    raise ValueError(f"{path}: unknown {sorted(unknown)}")
            for key, child in item.items():
                if key in props:
                    check(child, props[key], f"{path}.{key}")
        if isinstance(item, list) and isinstance(rule.get("items"), dict):
            for index, child in enumerate(item):
                check(child, rule["items"], f"{path}[{index}]")
    check(value, schema, "arguments")
    return dict(value)


from harness.runtime.steering import steerable_task, current_steering


class BuilderSession:
    def __init__(self, runtime: Any, *, source_task_id: str, skill_id: str | None = None,
                 builder_task_id: str | None = None,
                 source_invocation_id: str | None = None,
                 analysis_task_id: str | None = None,
                 owner_task_dir: Path | None = None, profile: str = "builder",
                 base_directory: Path | None = None, on_model_result=None):
        if sys.version_info < (3, 12):
            raise RuntimeError("Skill Builder requires Python 3.12 or newer")
        self.runtime = runtime
        self.profile = profile
        self.owner_usage_sink = on_model_result
        self.required_orchestration = False
        self.owner_base_hash = (inspect_skill(base_directory)["hash"]
            if profile in {"repair", "workflow"} and owner_task_dir is not None and base_directory is not None else None)
        if owner_task_dir is not None:
            owner_task_dir = owner_task_dir.resolve()
        self.source_task_id = source_task_id.removeprefix("@")
        self.skill_id = validate_skill_id(skill_id) if skill_id else None
        self.source_invocation_id = source_invocation_id
        worktree = Path(runtime.harness.worktree_dir).resolve()
        database = resolve_sqlite_path(runtime.harness.storage_sqlite_path, str(worktree))
        self.storage = SqliteStore(database, worktree_dir=str(worktree))
        prior = self.storage.connection.execute(
            "SELECT * FROM skill_builder_sessions WHERE builder_task_id=?",
            (builder_task_id,),
        ).fetchone() if builder_task_id else None
        if prior and owner_task_dir is not None and prior["owner_task_id"] != owner_task_dir.name:
            self.storage.close()
            raise ValueError("Coding repair does not belong to the requested owner")
        self.bound_skill_id = (prior["published_skill_id"] if prior else None) or self.skill_id
        self.referenced_tasks = set(json.loads(prior["referenced_tasks_json"])) if prior else set()
        self.referenced_tasks.add(self.source_task_id)
        active = (analysis_task_id or (prior["analysis_task_id"] if prior else None)
                  or self.source_task_id).removeprefix("@")
        self.referenced_tasks.add(active)
        try:
            self.source = SourceTaskContext(active, worktree, self.storage)
        except BaseException:
            self.storage.close()
            raise
        self.catalog = SkillCatalog(Path(__file__).resolve().parents[2] / "skills", database, worktree_dir=worktree)
        self.catalog.sync_existing()
        self.logger = RunLogger(str(worktree), task_id=builder_task_id or uuid.uuid4().hex,
                                run_id="skill-builder-" + uuid.uuid4().hex[:12],
                                storage=self.storage)
        self.resource_root = owner_task_dir or self.logger.task_dir
        folder = "workflows" if profile == "workflow" else "repairs"
        workspace = (owner_task_dir / "coding" / folder / self.logger.task_id
                     if owner_task_dir else self.logger.task_dir / "coding")
        if prior and prior["owner_task_id"]:
            from harness.skill_builder.catalog import builder_directory
            recovered = builder_directory(worktree, self.logger.task_id, prior["work_rel_path"], prior["owner_task_id"])
            workspace = recovered.parent
            self.resource_root = recovered.parents[3]
            self.profile = "workflow" if Path(prior["work_rel_path"]).parts[1] == "workflows" else "repair"
        self.work_dir = workspace / "builder"
        self.work_dir.mkdir(parents=True, exist_ok=True)
        self.trial_dir = workspace / "trials"
        self.trial_dir.mkdir(parents=True, exist_ok=True)
        self.browser = BuilderBrowser(runtime.browser, self.logger, runtime)
        self.finished: dict[str, Any] | None = None
        self._started = False
        try:
            self._load_or_prepare()
            if base_directory is not None and prior is None:
                # Host-bound active version, including an authorized task-local patch.
                shutil.rmtree(self.work_dir)
                shutil.copytree(base_directory, self.work_dir)
                copied_hash = inspect_skill(self.work_dir)["hash"]
                if self.owner_base_hash and copied_hash != self.owner_base_hash:
                    raise ValueError("Owner Skill changed while preparing the repair copy")
                self.logger.write("skill_builder.base.owner_copy", {
                    "source": str(base_directory), "hash": copied_hash})
            elif prior is None and self.profile == "builder" and not self.skill_id:
                from harness.workflow_mode.artifacts import import_workflow_package
                import_workflow_package(self)
            self._separate_probes()
        except BaseException as exc:
            try:
                if self._started:
                    self.storage.finish_run(task_id=self.logger.task_id, run_id=self.logger.run_id,
                        status="interrupted" if isinstance(exc, asyncio.CancelledError) else "failed", error={"type": type(exc).__name__, "message": str(exc)[:500]})
            finally:
                self.catalog.close()
                self.storage.close()
            raise

    def _separate_probes(self) -> None:
        """Keep development probes outside the directory copied on publish.

        This migrates only the editable Builder copy; published versions retain
        their exact bytes and hashes. Collisions preserve both probe files.
        """
        old = self.work_dir / "trials"
        if not old.exists() or old.is_symlink():
            return
        for source in sorted(old.rglob("*")):
            if source.is_file() and not source.is_symlink():
                target = self.trial_dir / source.relative_to(old)
                target.parent.mkdir(parents=True, exist_ok=True)
                if target.exists() and target.read_bytes() != source.read_bytes():
                    target = target.with_name(target.stem + "-" + uuid.uuid4().hex[:8] + target.suffix)
                shutil.copy2(source, target)
                self.storage.save_resource(
                    task_id=self.logger.task_id, run_id=self.logger.run_id,
                    resource_type="coding_agent_output",
                    logical_path=target.relative_to(self.resource_root).as_posix(),
                    external_path=str(target), media_type="text/plain",
                    metadata={"source": "skill_builder", "role": "development_probe", "sha256": hashlib.sha256(target.read_bytes()).hexdigest()},
                )
                # Existing resource rows explicitly describe mutable external
                # files. Relocate their pointer so historical references still
                # resolve; their recorded hash remains the historical fact.
                with write_transaction(self.storage.connection):
                    self.storage.connection.execute(
                        "UPDATE task_resources SET external_path=? WHERE task_id=? AND external_path IN (?, ?)",
                        (target.relative_to(self.logger.task_dir.resolve()).as_posix()
                         if target.is_relative_to(self.logger.task_dir.resolve()) else str(target), self.logger.task_id,
                         source.relative_to(self.resource_root).as_posix(), str(source)),
                    )
                source.unlink()
        for directory in sorted(old.rglob("*"), reverse=True):
            if directory.is_dir() and not directory.is_symlink() and not list(directory.iterdir()):
                directory.rmdir()
        if not list(old.iterdir()):
            old.rmdir()

    def _load_or_prepare(self) -> None:
        prior = self.storage.connection.execute(
            "SELECT * FROM skill_builder_sessions WHERE builder_task_id=?",
            (self.logger.task_id,),
        ).fetchone()
        if prior and (prior["source_task_id"] != self.source_task_id
                      or prior["source_skill_id"] != self.skill_id
                      or (self.source_invocation_id is not None and
                          prior["source_invocation_id"] != self.source_invocation_id)):
            raise ValueError("Builder task_id does not match the source task or Skill")
        existing = self.storage.get_task(self.logger.task_id)
        if existing is None:
            self.storage.create_task(task_id=self.logger.task_id,
                                     harness_version=HARNESS_VERSION)
        self.storage.start_run(task_id=self.logger.task_id, run_id=self.logger.run_id,
                               harness_version=HARNESS_VERSION)
        self._started = True
        if prior:
            with write_transaction(self.storage.connection):
                self.storage.connection.execute(
                    """UPDATE skill_builder_sessions SET status='active', draft_hash=NULL,
                       analysis_task_id=?, referenced_tasks_json=?, updated_at=datetime('now')
                       WHERE builder_task_id=?""",
                    (self.source.task_id, json.dumps(sorted(self.referenced_tasks)), self.logger.task_id),
                )
            return
        base_hash: str | None = None
        base_current_hash: str | None = None
        invocation_id: str | None = None
        if self.skill_id:
            initial_current = self.catalog.get(self.skill_id)
            base_current_hash = (initial_current["current_hash"]
                                 if initial_current and not initial_current["deleted"] else None)
            matching = self.source.invocations(self.skill_id)
            source_hashes = {item["skill_hash"] for item in matching}
            if len(source_hashes) > 1 and not self.source_invocation_id and not self.owner_base_hash:
                raise ValueError("Source task has multiple Skill versions; select a source invocation")
            if self.source_invocation_id:
                matching = [item for item in matching
                            if item["invocation_id"] == self.source_invocation_id]
                if not matching:
                    raise ValueError("The selected Skill invocation does not belong to the source task")
            if len(matching) == 1:
                invocation_id = matching[0]["invocation_id"]
                base_hash = matching[0]["skill_hash"]
            elif matching and len(source_hashes) == 1:
                # Repeated stages using identical bytes do not create a version
                # ambiguity. Retain task-wide evidence without inventing one
                # privileged invocation as the selected business run.
                base_hash = next(iter(source_hashes))
                self.logger.write("skill_builder.base.task_version", {
                    "skill": self.skill_id, "hash": base_hash,
                    "invocationIds": [item["invocation_id"] for item in matching],
                })
            elif not matching:
                manifest = self.source.manifest()
                if manifest.get("forced_skill_id") == self.skill_id:
                    base_hash = str((manifest.get("startup_args") or {}).get(
                        "forced_skill_hash") or "") or None
            if self.owner_base_hash:
                # The owner already pinned the active source. Many stage
                # invocations are evidence, not an ambiguous version choice.
                base_hash = self.owner_base_hash
            if base_hash:
                path = self.catalog.source_path(self.skill_id, base_hash, task_id=self.source.task_id)
                if path:
                    shutil.copytree(path, self.work_dir, dirs_exist_ok=True)
            # Legacy task records often identify only skill_id/runId. The
            # current version may be offered as an explicitly nonhistorical
            # starting point, never labeled as the executed source version.
            if not list(self.work_dir.iterdir()) and not self.owner_base_hash:
                current = self.catalog.get(self.skill_id)
                if current and not current["deleted"]:
                    shutil.copytree(self.catalog.root / self.skill_id, self.work_dir,
                                    dirs_exist_ok=True, ignore=shutil.ignore_patterns(".*"))
                    self.logger.write("skill_builder.base.current_copy", {
                        "skill": self.skill_id, "currentHash": current["current_hash"],
                        "historicalVersionKnown": False,
                    })
        with write_transaction(self.storage.connection):
            self.storage.connection.execute(
                """INSERT INTO skill_builder_sessions
                   (builder_task_id, source_task_id, source_invocation_id,
                    source_skill_id, source_skill_hash, base_current_hash,
                    work_rel_path, analysis_task_id, referenced_tasks_json, status, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', datetime('now'))""",
                (self.logger.task_id, self.source_task_id, invocation_id,
                 self.skill_id, base_hash, base_current_hash, "coding/builder",
                 self.source.task_id, json.dumps(sorted(self.referenced_tasks))),
            )
            if self.resource_root != self.logger.task_dir:
                self.storage.connection.execute(
                    "UPDATE skill_builder_sessions SET work_rel_path=?, owner_task_id=? WHERE builder_task_id=?",
                    (self.work_dir.relative_to(self.resource_root).as_posix(), self.resource_root.name, self.logger.task_id))
        self.logger.write("skill_builder.started", {
            "sourceTaskId": self.source_task_id, "sourceSkillId": self.skill_id,
            "sourceInvocationId": invocation_id, "sourceSkillHash": base_hash,
            "analysisTaskId": self.source.task_id,
        })

    def _register_references(self, instruction: str) -> None:
        # Only the current human turn can extend read scope. Old tool results
        # and model-generated IDs never authorize another task.
        self.referenced_tasks.update(task_references(instruction))
        with write_transaction(self.storage.connection):
            self.storage.connection.execute(
                "UPDATE skill_builder_sessions SET referenced_tasks_json=? WHERE builder_task_id=?",
                (json.dumps(sorted(self.referenced_tasks)), self.logger.task_id),
            )

    def _select_source(self, task_id: str | None = None) -> SourceTaskContext:
        requested = (task_id or self.source.task_id).removeprefix("@")
        if requested not in self.referenced_tasks:
            raise PermissionError("Task was not explicitly referenced by the user; reference it in /resume <builder-task-id> advice")
        if requested != self.source.task_id:
            source = SourceTaskContext(requested, Path(self.runtime.harness.worktree_dir), self.storage)
            previous = self.source.task_id
            with write_transaction(self.storage.connection):
                self.storage.connection.execute(
                    "UPDATE skill_builder_sessions SET analysis_task_id=? WHERE builder_task_id=?",
                    (requested, self.logger.task_id),
                )
            self.source = source
            self.logger.write("skill_builder.analysis.bound", {
                "originTaskId": self.source_task_id, "previousTaskId": previous,
                "analysisTaskId": requested, "authorization": "human_reference",
            })
        return self.source

    def _path(self, relative: str) -> Path:
        raw = Path(str(relative or ""))
        if raw.is_absolute() or not raw.parts or any(part in (".", "..") or part.startswith(".") for part in raw.parts):
            raise ValueError("File path must be inside the Builder work directory")
        root = self.trial_dir if raw.parts[0] == "trials" else self.work_dir
        parts = raw.parts[1:] if root == self.trial_dir else raw.parts
        if not parts:
            raise ValueError("Path must point to a file")
        candidate = root.joinpath(*parts)
        if any(part.is_symlink() for part in (candidate, *candidate.parents)):
            raise ValueError("Builder work files cannot use symbolic link paths")
        if not candidate.resolve(strict=False).is_relative_to(root.resolve()):
            raise ValueError("File path escapes the work directory")
        return candidate

    def _list_files(self) -> dict[str, Any]:
        files = []
        for root, prefix, role in ((self.work_dir, "", "skill_resource"),
                                   (self.trial_dir, "trials/", "development_probe")):
            for path in sorted(root.rglob("*")):
                if path.is_file() and not path.is_symlink():
                    body = path.read_bytes()
                    files.append({"path": prefix + path.relative_to(root).as_posix(),
                                  "role": role, "sha256": hashlib.sha256(body).hexdigest(),
                                  "byteSize": len(body)})
        return {"files": files}

    def _read_file(self, path: str, offset: int = 0, max_chars: int = 20000) -> dict[str, Any]:
        target = self._path(path)
        text = target.read_bytes().decode("utf-8")
        offset = max(0, int(offset))
        max_chars = max(1, min(int(max_chars), 50000))
        return {"path": path, "sha256": hashlib.sha256(text.encode()).hexdigest(),
                "totalChars": len(text), "offset": offset,
                "content": text[offset:offset + max_chars],
                "hasMore": offset + max_chars < len(text)}

    def _write_file(self, path: str, content: str,
                    expected_hash: str | None = None) -> dict[str, Any]:
        target = self._path(path)
        raw = content.encode("utf-8")
        if len(raw) > _MAX_FILE_BYTES:
            raise ValueError("Builder file exceeds 2 MB")
        old_hash = hashlib.sha256(target.read_bytes()).hexdigest() if target.exists() else None
        if old_hash != expected_hash:
            raise ValueError(f"File version has changed: expected={expected_hash}, actual={old_hash}")
        digest = hashlib.sha256(raw).hexdigest()
        outcome = {"path": path, "workspaceId": self.logger.task_id,
            "previousHash": old_hash, "intendedHash": digest,
            "fileWritten": False, "indexed": False, "phase": "preflight"}
        staged = target.with_name(target.name + ".tmp-" + uuid.uuid4().hex)
        try:
            # Fail a closed registry before changing bytes. Filesystem and SQL
            # are not one transaction; report any later partial effect explicitly.
            self.storage.connection.execute("SELECT 1")
            outcome["phase"] = "file"
            target.parent.mkdir(parents=True, exist_ok=True)
            staged.write_bytes(raw)
            os.replace(staged, target)
            outcome.update(fileWritten=True, phase="index")
            record = self.storage.save_resource(
                task_id=self.logger.task_id, run_id=self.logger.run_id,
                resource_type="coding_agent_output", logical_path=target.relative_to(self.resource_root).as_posix(),
                external_path=str(target), media_type="text/plain",
                metadata={"source": "skill_builder", "sha256": digest},
            )
            outcome.update(indexed=True, resource=record.get("saved_path"), phase="event")
            self.logger.write("skill_builder.file.written", {
                "path": path, "sha256": digest, "resource": record.get("saved_path")})
        except Exception as exc:
            raise WorkFileWriteError(exc, outcome) from exc
        finally:
            staged.unlink(missing_ok=True)
        return {"path": path, "sha256": digest, "resource": record.get("saved_path")}

    def _describe_tool(self, name: str) -> dict[str, Any]:
        tool = BROWSER_TOOLS.get(name)
        if tool is None:
            from harness.skill_runtime.session import SkillSession
            match = next((t for t in SkillSession.__new__(SkillSession).tools() if t.name == name), None)
            return {"name": name, "description": match.description, "inputSchema": match.parameters} if match else {
                "status": "unknown_tool", "name": name}
        return {"name": name, "description": tool.description,
                "inputSchema": tool.to_tool_spec().get("input_schema"),
                "evidenceLimit": "Tool registry is a public contract; it does not reveal every internal branch."}

    def _describe_action(self, name: str) -> dict[str, Any]:
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9]*\.[A-Za-z][A-Za-z0-9]*", name):
            raise ValueError("Invalid ABCP method name")
        schema = Path(__file__).resolve().parents[2] / "global_schema_cache" / "schemas" / (name + ".json")
        if not schema.is_file():
            return {"status": "schema_unavailable", "method": name}
        return json.loads(schema.read_text(encoding="utf-8"))

    def _source_resource(self, reference: str, offset: int, max_chars: int) -> dict[str, Any]:
        row = self.source.read_resource(reference)
        if row is None:
            return {"status": "not_found", "reference": reference}
        value = row.get("content_json")
        if value is None:
            value = row.get("content_text")
        if value is None and row.get("external_path"):
            # Storage rehashes the external file. Read only inside the explicitly
            # referenced task directory, and label bytes that drifted since
            # the original record instead of presenting them as historical.
            resolved = Path(str(row.get("resolved_path") or ""))
            source_root = self.source.directory.resolve()
            if (not row.get("content_available") or not resolved.is_file()
                    or resolved.is_symlink()
                    or not resolved.resolve().is_relative_to(source_root)):
                return {"status": "external_resource_unavailable", "metadata": row}
            byte_offset = max(0, int(offset))
            byte_count = max(1, min(int(max_chars), 50000))
            with resolved.open("rb") as handle:
                handle.seek(byte_offset)
                chunk = handle.read(byte_count)
                has_more = bool(handle.read(1))
            return {"reference": reference, "content": chunk.decode("utf-8", errors="replace"),
                    "offsetBytes": byte_offset, "nextOffsetBytes": byte_offset + len(chunk),
                    "hasMore": has_more, "historicalBytesVerified": not row.get("content_drifted"),
                    "recordedSha256": row.get("sha256"),
                    "currentSha256": row.get("current_sha256")}
        text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
        offset = max(0, int(offset))
        max_chars = max(1, min(int(max_chars), 50000))
        return {"reference": reference, "content": text[offset:offset + max_chars],
                "totalChars": len(text), "hasMore": offset + max_chars < len(text),
                "sha256": row.get("sha256")}

    def _finish(self, summary: str) -> dict[str, Any]:
        files = self._list_files()
        # A finish signal can end the model turn only when the draft is
        # structurally publishable. Otherwise return a tool error so the
        # Builder can repair the named file without losing its session.
        info = inspect_skill(self.work_dir)
        if self.required_orchestration and not (self.work_dir / "orchestration.json").is_file():
            raise ValueError("The Skill-mode build contract requires orchestration.json; keep editing this draft")
        if (self.work_dir / "orchestration.json").exists():
            from harness.skill_runtime.plan import read_source, validate_plan
            validate_plan(read_source(self.work_dir, "orchestration.json"), self.work_dir)
        actual_skill_id = validate_skill_id(str(info["metadata"].get("name") or ""))
        publication = self.catalog.builder_publication(self.logger.task_id, actual_skill_id, info["hash"])
        finished = {"builderTaskId": self.logger.task_id,
                    "workDir": str(self.work_dir), "files": files["files"],
                    "skill": info, "summary": summary, "status": "draft_saved",
                    "originTaskId": self.source_task_id, "analysisTaskId": self.source.task_id,
                    "publication": publication,
                    "next": [f"/resume {self.logger.task_id} [advice]",
                             f"/skill-publish {actual_skill_id}",
                             f"/skill-delete {actual_skill_id}"]}
        from harness.skill_builder.runtime import draft_evidence
        finished["validation"] = draft_evidence(self)
        with write_transaction(self.storage.connection):
            self.storage.connection.execute(
                """UPDATE skill_builder_sessions
                   SET status='finished', draft_hash=?, updated_at=datetime('now')
                   WHERE builder_task_id=?""",
                (info["hash"], self.logger.task_id),
            )
        self.finished = finished
        return finished

    async def _dispatch(self, name: str, arguments: Mapping[str, Any]) -> AgentToolResult:
        args = dict(arguments)
        try:
            if name in {"resolve_trial_step", "investigate_skill_trial", "run_trial_recovery", "resume_trial_recovery"}:
                from harness.skill_builder.validation import restore_trial
                await restore_trial(self, args["trial_task_id"])
            if name == "select_source_task":
                source = self._select_source(str(args["task_id"]))
                value = {"originTaskId": self.source_task_id, "analysisTaskId": source.task_id}
            elif name == "source_overview":
                source = self._select_source(args.get("task_id"))
                row = self.storage.connection.execute(
                    "SELECT source_invocation_id, source_skill_hash, base_current_hash "
                    "FROM skill_builder_sessions WHERE builder_task_id=?",
                    (self.logger.task_id,),
                ).fetchone()
                value = {"manifest": source.manifest(), "invocations":
                         source.invocations(self.bound_skill_id), "sourceTaskId": source.task_id,
                         "originTaskId": self.source_task_id, "analysisTaskId": source.task_id,
                         "referencedTasks": sorted(self.referenced_tasks),
                         "repairBase": dict(row) if row else None,
                         "repairBaseTaskId": self.source_task_id,
                         "historicalSourceKnown": bool(row and row["source_skill_hash"]),
                         "historicalSnapshotAvailable": bool(
                             row and row["source_skill_hash"] and self.skill_id and
                             self.catalog.source_path(self.skill_id, row["source_skill_hash"], task_id=self.source_task_id))}
            elif name == "read_source_events":
                source = self._select_source(args.get("task_id"))
                events = source.events(event_type=args.get("event_type"),
                                           after=int(args.get("after") or 0),
                                           limit=int(args.get("limit") or 50))
                value = {"taskId": source.task_id, "events": events,
                         "nextAfter": events[-1]["event_id"] if events else int(args.get("after") or 0)}
            elif name == "search_source_resources":
                source = self._select_source(args.get("task_id"))
                resources = source.resources(path_glob=str(args.get("path_glob") or "**/*"),
                                              pattern=args.get("pattern"),
                                              limit=int(args.get("limit") or 20))
                value = {"taskId": source.task_id, "resources": resources}
            elif name == "read_source_resource":
                self._select_source(args.get("task_id"))
                value = {"taskId": self.source.task_id, **self._source_resource(str(args["reference"]),
                                              int(args.get("offset") or 0),
                                              int(args.get("max_chars") or 20000))}
            elif name == "list_work_files":
                value = self._list_files()
            elif name == "read_work_file":
                value = self._read_file(str(args["path"]), int(args.get("offset") or 0),
                                        int(args.get("max_chars") or 20000))
            elif name == "write_work_file":
                value = self._write_file(str(args["path"]), str(args["content"]),
                                         args.get("expected_hash"))
            elif name in {"patch_work_file", "search_work_files", "build_workflow_package"}:
                from harness.skill_builder import workspace
                function = {"patch_work_file": workspace.patch_file, "search_work_files": workspace.search_files,
                            "build_workflow_package": workspace.build_package}[name]
                value = function(self, **args)
            elif name == "install_workflow_helpers":
                from harness.skill_builder.helpers import install
                value = install(self, **args)
            elif name == "describe_harness_tool":
                value = self._describe_tool(str(args["name"]))
            elif name == "describe_abcp_action":
                method = str(args["method"])
                if not re.fullmatch(r"[A-Za-z][A-Za-z0-9]*\.[A-Za-z][A-Za-z0-9]*", method):
                    raise ValueError("Invalid ABCP method name")
                value = await self.browser.describe_action(method)
            elif name == "read_workflow_guide":
                value = await self.browser.guide()
            elif name == "list_abcp_actions":
                value = await self.browser.catalog()
            elif name == "describe_skill_runtime":
                from harness.skill_builder.runtime import consumer_contract
                value = consumer_contract()
            elif name == "observe_skill_run":
                from harness.skill_builder.runtime import observe_source
                value = observe_source(self, **args)
            elif name == "run_skill_trial":
                from harness.skill_builder.runtime import run_trial
                await self.browser.restore_page(args["page_id"])
                value = await run_trial(self, page_id=args["page_id"], bindings=args["bindings"],
                    goal=args["goal"], plan=args.get("plan"), trial_task_id=args.get("trial_task_id"),
                    new_trial=args.get("new_trial", False), reason=args.get("reason"), prepare_only=args.get("prepare_only", False))
            elif name == "resolve_trial_step":
                from harness.skill_builder.runtime import resolve_trial
                value = await resolve_trial(self, **args)
            elif name == "investigate_skill_trial":
                from harness.skill_builder.runtime import investigate_trial
                value = await investigate_trial(self, **args)
            elif name in {"read_trial_state", "run_trial_recovery", "resume_trial_recovery"}:
                from harness.skill_builder import recovery
                value = await getattr(recovery, name)(self, **args)
            elif name == "read_draft_execution":
                from harness.skill_builder.runtime import draft_evidence
                value = draft_evidence(self)
            elif name == "create_workflow_test_page":
                value = await self.browser.create_page(args.get("url", "about:blank"), fleet_id=args["fleet_id"])
            elif name in {"read_validation_receipt", "record_workflow_validation", "inspect_trial_page"}:
                from harness.skill_builder import validation
                if name == "inspect_trial_page":
                    return await validation.inspect_page(self, **args)
                function = validation.read_receipt if name == "read_validation_receipt" else validation.record_assessment
                value = function(self, **args)
            elif name == "local_fs_authorize":
                value = await authorize_local_paths(self.browser, args.get("paths"), args.get("mode", "read"))
            elif name == "authorize_trial_files":
                value = await self.browser.authorize_files(list(args["paths"]))
            elif name == "attach_trial_page":
                value = await self.browser.attach_page(str(args["page_id"]), str(args["fleet_id"]))
            elif name == "browser_call":
                value = await self.browser.call(str(args["method"]), dict(args.get("params") or {}))
            elif name == "run_workflow_trial":
                value = await self.browser.trial(self._path(str(args["path"])),
                                                 str(args["page_id"]),
                                                 dict(args.get("variables") or {}))
            elif name == "finish_skill_create":
                manager = current_steering()
                if manager and not manager.can_finish():
                    return _result({"status": "user_input_pending", "tool_was_executed": False})
                result = self._finish(str(args.get("summary") or ""))
                if manager and manager.logger.task_id == self.logger.task_id: manager.commit()
                return _result(result, terminate=True)
            else:
                raise ValueError(f"unknown Builder tool: {name}")
            return _result(value)
        except WorkFileWriteError as exc:
            return _result(exc.payload, error=True)
        except Exception as exc:
            return _result({"status": "error", "type": type(exc).__name__,
                            "message": str(exc)[:800]}, error=True)

    def _tools(self) -> list[AgentTool]:
        definitions = [
            ("select_source_task", "Bind analysis to a task explicitly referenced by the user; origin and work files stay intact.", _schema({"task_id": _STRING}, ("task_id",))),
            ("source_overview", "Read the active task, or bind a user-referenced task_id and read its objective/invocations.", _schema({"task_id": _STRING})),
            ("read_source_events", "Read events of the active or user-referenced task_id; returns taskId, events and nextAfter cursor.", _schema({"task_id": _STRING, "event_type": _STRING, "after": {"type": "integer"}, "limit": {"type": "integer"}})),
            ("search_source_resources", "Read indexed resources of the active or user-referenced task_id; returns taskId and resources.", _schema({"task_id": _STRING, "path_glob": _STRING, "pattern": _STRING, "limit": {"type": "integer"}})),
            ("read_source_resource", "Read one active task resource, or bind a user-referenced task_id first.", _schema({"task_id": _STRING, "reference": _STRING, "offset": {"type": "integer"}, "max_chars": {"type": "integer"}}, ("reference",))),
            ("list_work_files", "List Builder work files and hashes.", _schema({})),
            ("read_work_file", "Read a bounded page of a Builder work file.", _schema({"path": _STRING, "offset": {"type": "integer"}, "max_chars": {"type": "integer"}}, ("path",))),
            ("write_work_file", "Write one Builder file with compare-and-swap hash; null expected_hash creates it.", _schema({"path": _STRING, "content": _STRING, "expected_hash": {"type": ["string", "null"]}}, ("path", "content"))),
            ("patch_work_file", "Apply exact unique old_text/new_text edits atomically under expected_hash. Unchanged file content need not be repeated. Read current source on a stale hash or ambiguous anchor.",
             _schema({"path": _STRING, "expected_hash": _STRING, "edits": {"type": "array", "items":
                _schema({"old_text": _STRING, "new_text": _STRING}, ("old_text", "new_text"))}}, ("path", "expected_hash", "edits"))),
            ("search_work_files", "Search source and probes by regex with file/line/hash results; paths stay in this workspace.",
             _schema({"pattern": _STRING, "path_glob": _STRING, "limit": {"type": "integer"}}, ("pattern",))),
            ("build_workflow_package", "Assemble a WebCross JS/TS JSON package from workspace source files. files maps package module names to workspace paths. This saves exact source bytes; real execution still validates behavior.",
             _schema({"path": _STRING, "name": _STRING, "entry": _STRING, "files": {"type": "object", "additionalProperties": {"type": "string"}},
                "expected_hash": {"type": ["string", "null"]}}, ("path", "name", "entry", "files"))),
            ("describe_harness_tool", "Read the public contract of a Harness composite/tool, without source access.", _schema({"name": _STRING}, ("name",))),
            ("describe_abcp_action", "Read cached platform action schema and catalog revision.", _schema({"method": _STRING}, ("method",))),
            ("list_abcp_actions", "Read the live platform action directory and revision. Choose names from this directory; describe unfamiliar methods for full parameter and response schemas.", _schema({})),
            ("install_workflow_helpers", "Install versioned JS response helpers with workspace hash checks. Preserves raw Page.list rows, distinguishes action errors from empty values; no business targeting or implicit retry. Include installed source in a package explicitly.",
             _schema({"path": _STRING, "expected_hash": _STRING})),
            ("read_workflow_guide", "Read the live Workflow source-package and SDK contract; browser decision boundaries differ from Builder development boundaries.", _schema({})),
            ("describe_skill_runtime", "Read the actual Skill consumer tool and orchestration schemas, including binding, continuation and recovery.", _schema({})),
            ("observe_skill_run", "Observe authorized source task execution and Coding rounds; distinguish real Workflow operations from Browser recovery before choosing an optimization. Default omits repeated inline sources; section/offset/max_chars pages full evidence.", _schema({"task_id": _STRING, "section": _STRING, "offset": {"type": "integer"}, "max_chars": {"type": "integer"}})),
            ("run_skill_trial", "Execute the saved orchestration (or explicit plan) through the shared executor. Default: continue the owned trial on this page, retaining checkpoints. new_trial=true plus reason starts an independent test with empty checkpoints; it does not reset the webpage or erase earlier effects. Never combine new_trial with trial_task_id. prepare_only creates/binds the trial without executing, so you can observe or delegate Browser setup first. Reconcile effects before repeating writes.", _schema({"page_id": _STRING, "bindings": _OBJECT, "goal": _STRING, "plan": _OBJECT, "trial_task_id": _STRING, "new_trial": {"type": "boolean"}, "reason": _STRING, "prepare_only": {"type": "boolean"}}, ("page_id", "bindings", "goal"))),
            ("resolve_trial_step", "Record your semantic reconciliation of an owned trial checkpoint, before retrying or continuing a changed revision. No automatic replay.", _schema({"trial_task_id": _STRING, "step_id": _STRING, "decision": {"type": "string", "enum": ["complete", "skip", "retry"]}, "reason": _STRING, "evidence_refs": {"type": "array", "items": _STRING}, "output": _OBJECT}, ("trial_task_id", "step_id", "decision", "reason", "evidence_refs"))),
            ("read_trial_state", "Read the owned trial's preserved plan, checkpoints and page recovery state, including after interruption.",
             _schema({"trial_task_id": _STRING}, ("trial_task_id",))),
            ("run_trial_recovery", "Execute a saved recovery/observation file on the interrupted trial page without replacing its program or checkpoints. You choose the UI action from evidence. Continue returned attempts with recovery_id; uncertain effects need reconciliation.",
             _schema({"trial_task_id": _STRING, "step_id": _STRING, "path": _STRING, "variables": _OBJECT,
                "reason": _STRING, "evidence_refs": {"type": "array", "items": _STRING}, "recovery_id": _STRING},
                ("trial_task_id", "step_id", "path", "reason", "evidence_refs"))),
            ("resume_trial_recovery", "After checking current page and prior effects, continue the preserved plan, accept the interrupted step as complete, or defer. continue starts the saved program at its entry; optional remaining_input replaces only that step's input. It does not resume a failed JS stack or activate workspace edits.",
             _schema({"trial_task_id": _STRING, "recovery_id": _STRING,
                "decision": {"type": "string", "enum": ["continue", "complete", "defer"]},
                "reason": _STRING, "evidence_refs": {"type": "array", "items": _STRING},
                "remaining_input": _OBJECT, "output": _OBJECT},
                ("trial_task_id", "recovery_id", "decision", "reason", "evidence_refs"))),
            ("investigate_skill_trial", "Delegate bounded Browser investigation, or review=true for independent read-only validation, within this owned trial. The facts return to this Builder; no nested Orchestrator.", _schema({"trial_task_id": _STRING, "instruction": _STRING, "review": {"type": "boolean"},
                "step_id": _STRING, "purpose": _STRING, "starting_state": _OBJECT,
                "success_criteria": {"type": "array", "items": _STRING},
                "evidence_refs": {"type": "array", "items": _STRING}}, ("trial_task_id", "instruction"))),
            ("read_draft_execution", "Compare the current draft's program hashes with real executions, input variations and Browser resolutions. Structural validity is not business verification.", _schema({})),
            ("create_workflow_test_page", "Create an independent test page in a named Fleet after task authorization. Use the intended logged-in Fleet; a new page does not reset server state. Source-task logs alone do not grant mutation rights.",
             _schema({"fleet_id": _STRING, "url": _STRING}, ("fleet_id",))),
            ("inspect_trial_page", "Observe an owned trial page through the shared executor; receipts are indexed without a Browser model.",
             _schema({"trial_task_id": _STRING, "method": _STRING, "params": _OBJECT}, ("trial_task_id", "method", "params"))),
            ("read_validation_receipt", "Read attributed evidence from this draft, its trials or explicitly referenced source tasks.",
             _schema({"reference": _STRING, "offset": {"type": "integer"}, "max_chars": {"type": "integer"}}, ("reference",))),
            ("record_workflow_validation", "Record an evidence-based stage assessment bound to current source and contract hashes. Browser rescue does not verify unexecuted code. Changed hashes retire prior assessments.",
             _schema({"stage_id": _STRING, "verdict": {"type": "string", "enum": ["verified", "needs_work", "blocked", "not_applicable"]},
                "reason": _STRING, "evidence_refs": {"type": "array", "items": _STRING}},
                ("stage_id", "verdict", "reason", "evidence_refs"))),
            ("local_fs_authorize", LOCAL_AUTHORIZATION_DESCRIPTION, LOCAL_AUTHORIZATION_SCHEMA),
            ("authorize_trial_files", "Request READ access to trial files or their explicit material directory. Paths are confirmed as one batch; an approved directory covers descendants. Same authorization as local_fs_authorize(mode=read). Only the human terminal decision grants permission.", _schema({"paths": {"type": "array", "minItems": 1, "items": _STRING}}, ("paths",))),
            ("attach_trial_page", "Request human terminal authorization to use an existing test page and Fleet. Attached user pages are never closed by Builder cleanup.", _schema({"page_id": _STRING, "fleet_id": _STRING}, ("page_id", "fleet_id"))),
            ("browser_call", "Call an authorized ABCP action on a Builder-owned page; Page.create creates one.", _schema({"method": _STRING, "params": _OBJECT}, ("method",))),
            ("run_workflow_trial", "Execute a Workflow JSON file on a Builder-owned page through WebCross.", _schema({"path": _STRING, "page_id": _STRING, "variables": _OBJECT}, ("path", "page_id"))),
            ("finish_skill_create", "Validate the complete Skill draft, then finish this Builder turn. Structural errors keep the session active for repair.", _schema({"summary": _STRING}, ("summary",))),
        ]
        tools = []
        for name, description, parameters in definitions:
            async def execute(call_id: str, arguments: Mapping[str, Any], signal: Any = None,
                              on_update: Any = None, *, _name: str = name) -> AgentToolResult:
                if signal is not None and signal.is_cancelled():
                    return _result({"status": "cancelled"}, error=True)
                return await self._dispatch(_name, arguments)
            tools.append(AgentTool(name=name, label=name, description=description,
                                   parameters=parameters, execute_fn=execute,
                                   prepare_arguments=lambda value, _schema=parameters:
                                       _prepare_tool_arguments(value, _schema),
                                   execution_mode="sequential"))
        return tools

    def _on_model_result(self, result: Any) -> None:
        if self.owner_usage_sink is not None:
            self.owner_usage_sink(result)
        role = "workflow.coding" if self.profile == "workflow" else "skill.coding" if self.profile == "repair" else "skill.builder"
        self.logger.record_llm_usage(source=role,
            provider=result.message.provider or "unknown", model=result.message.model or "unknown",
            usage={**result.usage.legacy(), **result.diagnostics})
        from harness.skill_runtime.usage import neutral_counters
        self.logger.write("skill_builder.model_usage", {
            **neutral_counters(result.usage), "diagnostics": result.diagnostics})

    def _prior_messages(self, *, context: bool = True) -> list[Any]:
        row = self.storage.connection.execute(
            """SELECT resource_id FROM task_resources
               WHERE task_id=? AND (logical_path='coding/builder/session.json' OR (? AND logical_path='coding/builder/context.json'))
               ORDER BY created_at DESC, rowid DESC LIMIT 1""",
            (self.logger.task_id, context),
        ).fetchone()
        if row is None:
            return []
        from harness.storage.sqlite_store import build_resource_uri
        record = self.storage.read_resource(
            current_task_id=self.logger.task_id,
            resource_uri=build_resource_uri(self.logger.task_id, row["resource_id"]),
        )
        value = record.get("content_json") if record else None
        # SqliteStore returns the logical JSON column as serialized text,
        # including when the physical row was compressed.
        if isinstance(value, str):
            value = json.loads(value)
        if not isinstance(value, list):
            raise ValueError("Builder conversation cannot be restored")
        adapter = TypeAdapter(AgentMessage)
        return [adapter.validate_python(item) for item in value]

    async def prepare_model_context(self, messages):
        manager = current_steering()
        if manager:
            sequence = getattr(self, "_steering_reference_sequence", 0)
            records = [r for r in manager.records if r["sequence"] > sequence]
            for record in records: self._register_references(record["text"])
            self._steering_reference_sequence = manager.revision
        return messages

    @steerable_task
    async def run(self, instruction: str, *, register_human_references: bool = True) -> dict[str, Any]:
        if register_human_references:
            self._register_references(instruction)
        raw_provider = LLMFactory.create_provider(self.runtime.worker)
        provider = HarnessProviderAdapter(raw_provider,
                                          on_result=self._on_model_result)
        prior_messages = self._prior_messages()
        role_prompt = self.role_prompt()
        tools = self._tools()
        def facts():
            from harness.skill_builder.validation import evidence
            return {"workDir": str(self.work_dir), "sourceTaskId": self.source.task_id,
                    "files": self._list_files(), "profile": self.profile,
                    "stageValidation": evidence(self),
                    "instruction": instruction, "warning": "Workspace hashes and raw receipts override older conversation claims"}
        prompt = (f"Origin task @{self.source_task_id}; current analysis task @{self.source.task_id}; "
                  f"Readable tasks explicitly referenced by the user: {sorted(self.referenced_tasks)}. User request: {instruction}")
        if self.bound_skill_id:
            prompt = f"Edit Skill {self.bound_skill_id}; " + prompt
        from harness.runtime.agent_loop import agent_events
        history: list[Any] = self._prior_messages(context=False)
        last_model = None
        try:
            async for event, active in agent_events(logger=self.logger, runtime_config=self.runtime.harness,
                    raw_provider=raw_provider, provider=provider, model=self.runtime.worker.model_id,
                    system=role_prompt, tools=tools, messages=prior_messages, prompt=prompt,
                    context_path="coding/builder/context.json", actor="coding", facts=facts,
                    prepare_context=self.prepare_model_context,
                    label=getattr(self, "output_label", "Skill Builder"), quiet=getattr(self, "quiet", False),
                    usage_sink=(lambda result: self.owner_usage_sink(result, "context_compaction"))
                        if self.owner_usage_sink else None):
                if isinstance(event, MessageEndEvent):
                    message = event.message
                    if isinstance(message, AssistantMessage):
                        last_model = message
                    history.append(message)
                    # One durable transcript projection, updated after every
                    # completed message; Tau's context is an in-memory view.
                    self.storage.save_resource(
                        task_id=self.logger.task_id, run_id=self.logger.run_id,
                        resource_type="builder_session", logical_path="coding/builder/session.json",
                        content=[item.model_dump(by_alias=True, exclude_none=True)
                                 for item in history],
                        metadata={"sourceTaskId": self.source_task_id,
                                  "analysisTaskId": self.source.task_id,
                                  "sourceSkillId": self.skill_id},
                    )
                    self.storage.save_resource(task_id=self.logger.task_id, run_id=self.logger.run_id,
                        resource_type="agent_context", logical_path="coding/builder/context.json",
                        content=[m.model_dump(by_alias=True, exclude_none=True) for m in active])
                    if isinstance(message, AssistantMessage) and message.text and not getattr(self, "quiet", False):
                        from harness.runtime.session_output import message as print_message
                        print_message(getattr(self, "output_label", "Coding " + self.profile if self.profile != "builder" else "Skill Builder"), message.text)
                elif isinstance(event, ToolExecutionEndEvent):
                    from harness.runtime.session_output import tool_result
                    if not getattr(self, "quiet", False):
                        tool_result(getattr(self, "output_label", "Coding " + self.profile if self.profile != "builder" else "Skill Builder"), event.tool_name, event.result, error=event.is_error)
                    self.logger.write("skill_builder.tool", {
                        "tool": event.tool_name, "toolCallId": event.tool_call_id,
                        "isError": event.is_error,
                        "result": event.result.model_dump(by_alias=True, exclude_none=True),
                    })
            if self.finished is None:
                from harness.runtime.agent_loop import stop_facts
                stopped = stop_facts(last_model) if last_model else {"stopReason": "no_response"}
                result = {"status": "incomplete", "builderTaskId": self.logger.task_id,
                    "workDir": str(self.work_dir), "modelStop": stopped,
                    "summary": "模型会话已停止，尚未明确完成草稿。已保存文件和执行记录可继续使用。",
                    "files": self._list_files()["files"],
                    "next": [f"/resume {self.logger.task_id} 继续完成草稿和验证"]}
                self.storage.save_resource(task_id=self.logger.task_id, run_id=self.logger.run_id,
                    resource_type="builder_result", logical_path="coding/builder/result.json", content=result)
                with write_transaction(self.storage.connection):
                    self.storage.connection.execute(
                        "UPDATE skill_builder_sessions SET status='incomplete', draft_hash=NULL WHERE builder_task_id=?",
                        (self.logger.task_id,))
                self.logger.write_usage_summary()
                self.storage.finish_run(task_id=self.logger.task_id, run_id=self.logger.run_id,
                    status="interrupted" if stopped["stopReason"] == "aborted" else "failed",
                    error={"type": "ModelSessionStopped", **stopped})
                return result
            self.storage.save_resource(task_id=self.logger.task_id, run_id=self.logger.run_id,
                resource_type="builder_result", logical_path="coding/builder/result.json", content=self.finished)
            self.logger.write_usage_summary()
            self.storage.finish_run(task_id=self.logger.task_id, run_id=self.logger.run_id,
                                    status="completed")
            return self.finished
        except BaseException as exc:
            with write_transaction(self.storage.connection):
                self.storage.connection.execute(
                    "UPDATE skill_builder_sessions SET status='incomplete', draft_hash=NULL WHERE builder_task_id=?",
                    (self.logger.task_id,))
            self.storage.finish_run(task_id=self.logger.task_id, run_id=self.logger.run_id,
                                    status="interrupted" if isinstance(exc, asyncio.CancelledError) else "failed", error={"type": type(exc).__name__,
                                                            "message": str(exc)[:500]})
            if not getattr(self, "quiet", False):
                from harness.runtime.session_output import block
                block(getattr(self, "output_label", "Skill Builder"), "运行中断，已保存工作可恢复", [
                    ("原因", f"{type(exc).__name__}: {exc}"),
                    ("恢复", f"/resume {self.logger.task_id} [补充指令]")])
            raise
        finally:
            manager = current_steering()
            if manager and manager.logger.task_id == self.logger.task_id and manager.workflow_tasks:
                from harness.runtime.workflow_steering import shutdown_workflows
                await shutdown_workflows(manager)
            await self.browser.close()
            self.catalog.close()
            self.storage.close()

    def role_prompt(self) -> str:
        role_prompt = system_prompt(self.work_dir, self.source_task_id, self.bound_skill_id,
                                    analysis_task_id=self.source.task_id,
                                    referenced_tasks=sorted(self.referenced_tasks))
        if self.profile == "repair":
            role_prompt += ("\nYou are a delegated Coding repair worker. Repair the bounded subgoal in this work copy. "
                "Owner instructions cannot grant access to additional tasks or production pages. "
                "Preserve input definitions and orchestration unless the actual defect requires a change. "
                "Use raw execution records; return changes, validation facts, unknowns and a recovery suggestion. "
                "Your finish returns to the Skill owner. It does not finish the user task or publish changes.")
        return role_prompt
