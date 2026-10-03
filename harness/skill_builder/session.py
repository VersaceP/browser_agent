"""The first-phase Skill Builder application session."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sys
import uuid
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from harness._vendor.tau_agent import AgentHarness, AgentHarnessConfig, AgentTool, AgentToolResult
from harness._vendor.tau_agent.events import MessageEndEvent, ToolExecutionEndEvent
from harness._vendor.tau_agent.messages import AgentMessage, AssistantMessage, TextContent
from harness.skill_builder.browser import BuilderBrowser
from harness.skill_builder.catalog import SkillCatalog, inspect_skill, validate_skill_id
from harness.skill_builder.context import SourceTaskContext
from harness.skill_builder.prompt import system_prompt
from harness.skill_builder.provider import HarnessProviderAdapter
from harness.storage.factory import resolve_sqlite_path
from harness.storage.sqlite_connection import write_transaction
from harness.storage.sqlite_store import SqliteStore
from harness.tools.browser_tools.dispatch import BROWSER_TOOLS
from harness.utils import RunLogger
from harness.version import HARNESS_VERSION
from llm.factory import LLMFactory
from pydantic import TypeAdapter


_MAX_FILE_BYTES = 2_000_000


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
        kinds = rule.get("type", "object")
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


class BuilderSession:
    def __init__(self, runtime: Any, *, source_task_id: str, skill_id: str | None = None,
                 builder_task_id: str | None = None,
                 source_invocation_id: str | None = None):
        if sys.version_info < (3, 12):
            raise RuntimeError("Skill Builder 需要 Python 3.12 或更新版本")
        self.runtime = runtime
        self.source_task_id = source_task_id.removeprefix("@")
        self.skill_id = validate_skill_id(skill_id) if skill_id else None
        self.source_invocation_id = source_invocation_id
        worktree = Path(runtime.harness.worktree_dir).resolve()
        database = resolve_sqlite_path(runtime.harness.storage_sqlite_path, str(worktree))
        self.storage = SqliteStore(database, worktree_dir=str(worktree))
        self.source = SourceTaskContext(self.source_task_id, worktree, self.storage)
        self.catalog = SkillCatalog(Path(__file__).resolve().parents[2] / "skills", database)
        self.catalog.sync_existing()
        self.logger = RunLogger(str(worktree), task_id=builder_task_id or uuid.uuid4().hex,
                                run_id="skill-builder-" + uuid.uuid4().hex[:12],
                                storage=self.storage)
        self.work_dir = self.logger.task_dir / "coding" / "builder"
        self.work_dir.mkdir(parents=True, exist_ok=True)
        self.browser = BuilderBrowser(runtime.browser, self.logger)
        self.finished: dict[str, Any] | None = None
        self._started = False
        self._load_or_prepare()

    def _load_or_prepare(self) -> None:
        prior = self.storage.connection.execute(
            "SELECT * FROM skill_builder_sessions WHERE builder_task_id=?",
            (self.logger.task_id,),
        ).fetchone()
        if prior and (prior["source_task_id"] != self.source_task_id
                      or prior["source_skill_id"] != self.skill_id
                      or (self.source_invocation_id is not None and
                          prior["source_invocation_id"] != self.source_invocation_id)):
            raise ValueError("Builder task_id 与来源任务或 Skill 不匹配")
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
                       updated_at=datetime('now') WHERE builder_task_id=?""",
                    (self.logger.task_id,),
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
            if len(matching) > 1 and not self.source_invocation_id:
                raise ValueError("来源任务有多次 Skill 调用，请指定 invocation_id")
            if self.source_invocation_id:
                matching = [item for item in matching
                            if item["invocation_id"] == self.source_invocation_id]
                if not matching:
                    raise ValueError("指定的 Skill invocation 不属于来源任务")
            if len(matching) == 1:
                invocation_id = matching[0]["invocation_id"]
                base_hash = matching[0]["skill_hash"]
            elif not matching:
                manifest = self.source.manifest()
                if manifest.get("forced_skill_id") == self.skill_id:
                    base_hash = str((manifest.get("startup_args") or {}).get(
                        "forced_skill_hash") or "") or None
            if base_hash:
                path = self.catalog.version_path(self.skill_id, base_hash)
                if path:
                    shutil.copytree(path, self.work_dir, dirs_exist_ok=True)
            # Legacy task records often identify only skill_id/runId. The
            # current version may be offered as an explicitly nonhistorical
            # starting point, never labeled as the executed source version.
            if not list(self.work_dir.iterdir()):
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
                    work_rel_path, status, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, 'active', datetime('now'))""",
                (self.logger.task_id, self.source_task_id, invocation_id,
                 self.skill_id, base_hash, base_current_hash, "coding/builder"),
            )
        self.logger.write("skill_builder.started", {
            "sourceTaskId": self.source_task_id, "sourceSkillId": self.skill_id,
            "sourceInvocationId": invocation_id, "sourceSkillHash": base_hash,
        })

    def _path(self, relative: str) -> Path:
        raw = Path(str(relative or ""))
        if raw.is_absolute() or not raw.parts or any(part in (".", "..") or part.startswith(".") for part in raw.parts):
            raise ValueError("文件路径须位于 Builder 工作目录内")
        candidate = self.work_dir.joinpath(*raw.parts)
        if any(part.is_symlink() for part in (candidate, *candidate.parents)):
            raise ValueError("符号链接路径不可用于 Builder 工作文件")
        if not candidate.resolve(strict=False).is_relative_to(self.work_dir.resolve()):
            raise ValueError("文件路径越界")
        return candidate

    def _list_files(self) -> dict[str, Any]:
        files = []
        for path in sorted(self.work_dir.rglob("*")):
            if path.is_file() and not path.is_symlink():
                body = path.read_bytes()
                files.append({"path": path.relative_to(self.work_dir).as_posix(),
                              "sha256": hashlib.sha256(body).hexdigest(),
                              "byteSize": len(body)})
        return {"files": files}

    def _read_file(self, path: str, offset: int = 0, max_chars: int = 20000) -> dict[str, Any]:
        target = self._path(path)
        text = target.read_text(encoding="utf-8")
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
            raise ValueError("单个 Builder 文件超过 2 MB")
        old_hash = hashlib.sha256(target.read_bytes()).hexdigest() if target.exists() else None
        if old_hash != expected_hash:
            raise ValueError(f"文件版本已变化: expected={expected_hash}, actual={old_hash}")
        target.parent.mkdir(parents=True, exist_ok=True)
        staged = target.with_name(target.name + ".tmp-" + uuid.uuid4().hex)
        staged.write_bytes(raw)
        os.replace(staged, target)
        digest = hashlib.sha256(raw).hexdigest()
        record = self.storage.save_resource(
            task_id=self.logger.task_id, run_id=self.logger.run_id,
            resource_type="coding_agent_output", logical_path="coding/builder/" + path,
            external_path=str(target), media_type="text/plain",
            metadata={"source": "skill_builder", "sha256": digest},
        )
        self.logger.write("skill_builder.file.written", {
            "path": path, "sha256": digest, "resource": record.get("saved_path")})
        return {"path": path, "sha256": digest, "resource": record.get("saved_path")}

    def _describe_tool(self, name: str) -> dict[str, Any]:
        tool = BROWSER_TOOLS.get(name)
        if tool is None:
            return {"status": "unknown_tool", "name": name}
        return {"name": name, "description": tool.description,
                "inputSchema": tool.to_tool_spec().get("input_schema"),
                "evidenceLimit": "Tool registry is a public contract; it does not reveal every internal branch."}

    def _describe_action(self, name: str) -> dict[str, Any]:
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9]*\.[A-Za-z][A-Za-z0-9]*", name):
            raise ValueError("ABCP 方法名称格式错误")
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
        try:
            info = inspect_skill(self.work_dir)
        except Exception as exc:
            info = {"status": "incomplete", "reason": str(exc)}
        self.finished = {"builderTaskId": self.logger.task_id,
                         "workDir": str(self.work_dir), "files": files["files"],
                         "skill": info, "summary": summary}
        with write_transaction(self.storage.connection):
            self.storage.connection.execute(
                """UPDATE skill_builder_sessions
                   SET status='finished', draft_hash=?, updated_at=datetime('now')
                   WHERE builder_task_id=?""",
                (info.get("hash"), self.logger.task_id),
            )
        return self.finished

    async def _dispatch(self, name: str, arguments: Mapping[str, Any]) -> AgentToolResult:
        args = dict(arguments)
        try:
            if name == "source_overview":
                row = self.storage.connection.execute(
                    "SELECT source_invocation_id, source_skill_hash, base_current_hash "
                    "FROM skill_builder_sessions WHERE builder_task_id=?",
                    (self.logger.task_id,),
                ).fetchone()
                value = {"manifest": self.source.manifest(), "invocations":
                         self.source.invocations(self.skill_id), "sourceTaskId": self.source_task_id,
                         "repairBase": dict(row) if row else None,
                         "historicalSourceKnown": bool(row and row["source_skill_hash"]),
                         "historicalSnapshotAvailable": bool(
                             row and row["source_skill_hash"] and self.skill_id and
                             self.catalog.version_path(self.skill_id, row["source_skill_hash"]))}
            elif name == "read_source_events":
                value = self.source.events(event_type=args.get("event_type"),
                                           after=int(args.get("after") or 0),
                                           limit=int(args.get("limit") or 50))
            elif name == "search_source_resources":
                value = self.source.resources(path_glob=str(args.get("path_glob") or "**/*"),
                                              pattern=args.get("pattern"),
                                              limit=int(args.get("limit") or 20))
            elif name == "read_source_resource":
                value = self._source_resource(str(args["reference"]),
                                              int(args.get("offset") or 0),
                                              int(args.get("max_chars") or 20000))
            elif name == "list_work_files":
                value = self._list_files()
            elif name == "read_work_file":
                value = self._read_file(str(args["path"]), int(args.get("offset") or 0),
                                        int(args.get("max_chars") or 20000))
            elif name == "write_work_file":
                value = self._write_file(str(args["path"]), str(args["content"]),
                                         args.get("expected_hash"))
            elif name == "describe_harness_tool":
                value = self._describe_tool(str(args["name"]))
            elif name == "describe_abcp_action":
                value = self._describe_action(str(args["method"]))
            elif name == "read_workflow_guide":
                guide = (Path(__file__).resolve().parents[1] / "prompts" / "resources"
                         / "browser" / "workflow-segments.md")
                value = {"content": guide.read_text(encoding="utf-8")[:50000],
                         "source": str(guide),
                         "scope": "protocol guidance; Builder chooses longer boundaries from evidence"}
            elif name == "browser_call":
                value = await self.browser.call(str(args["method"]), dict(args.get("params") or {}))
            elif name == "run_workflow_trial":
                value = await self.browser.trial(self._path(str(args["path"])),
                                                 str(args["page_id"]),
                                                 dict(args.get("variables") or {}))
            elif name == "finish_skill_create":
                return _result(self._finish(str(args.get("summary") or "")), terminate=True)
            else:
                raise ValueError(f"unknown Builder tool: {name}")
            return _result(value)
        except Exception as exc:
            return _result({"status": "error", "type": type(exc).__name__,
                            "message": str(exc)[:800]}, error=True)

    def _tools(self) -> list[AgentTool]:
        definitions = [
            ("source_overview", "Read the referenced task's objective and exact Skill invocations.", _schema({})),
            ("read_source_events", "Page through source task run events.", _schema({"event_type": _STRING, "after": {"type": "integer"}, "limit": {"type": "integer"}})),
            ("search_source_resources", "Search indexed files and execution artifacts in the referenced task.", _schema({"path_glob": _STRING, "pattern": _STRING, "limit": {"type": "integer"}})),
            ("read_source_resource", "Read a bounded page of one referenced source resource.", _schema({"reference": _STRING, "offset": {"type": "integer"}, "max_chars": {"type": "integer"}}, ("reference",))),
            ("list_work_files", "List Builder work files and hashes.", _schema({})),
            ("read_work_file", "Read a bounded page of a Builder work file.", _schema({"path": _STRING, "offset": {"type": "integer"}, "max_chars": {"type": "integer"}}, ("path",))),
            ("write_work_file", "Write one Builder file with compare-and-swap hash; null expected_hash creates it.", _schema({"path": _STRING, "content": _STRING, "expected_hash": {"type": ["string", "null"]}}, ("path", "content"))),
            ("describe_harness_tool", "Read the public contract of a Harness composite/tool, without source access.", _schema({"name": _STRING}, ("name",))),
            ("describe_abcp_action", "Read cached platform action schema and catalog revision.", _schema({"method": _STRING}, ("method",))),
            ("read_workflow_guide", "Read the protocol guide for Workflow steps and event windows; online segment boundaries are not Builder boundaries.", _schema({})),
            ("browser_call", "Call an authorized ABCP action on a Builder-owned page; Page.create creates one.", _schema({"method": _STRING, "params": _OBJECT}, ("method",))),
            ("run_workflow_trial", "Execute a Workflow JSON file on a Builder-owned page through WebCross.", _schema({"path": _STRING, "page_id": _STRING, "variables": _OBJECT}, ("path", "page_id"))),
            ("finish_skill_create", "Finish the current Builder turn and present files and actual trial facts.", _schema({"summary": _STRING}, ("summary",))),
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
        usage = result.usage
        self.logger.write("skill_builder.model_usage", {
            "input": usage.input_tokens, "cacheRead": usage.cache_read_tokens,
            "cacheCreation": usage.cache_write_tokens,
            "uncachedInput": (max(0, usage.input_tokens - (usage.cache_read_tokens or 0)
                                  - (usage.cache_write_tokens or 0))
                              if usage.input_tokens is not None else None),
            "output": usage.output_tokens, "diagnostics": result.diagnostics,
        })

    def _prior_messages(self) -> list[Any]:
        row = self.storage.connection.execute(
            """SELECT resource_id FROM task_resources
               WHERE task_id=? AND logical_path='coding/builder/session.json'
               ORDER BY created_at DESC, rowid DESC LIMIT 1""",
            (self.logger.task_id,),
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
            raise ValueError("Builder 会话记录不可恢复")
        adapter = TypeAdapter(AgentMessage)
        return [adapter.validate_python(item) for item in value]

    async def run(self, instruction: str) -> dict[str, Any]:
        provider = HarnessProviderAdapter(LLMFactory.create_provider(self.runtime.worker),
                                          on_result=self._on_model_result)
        prior_messages = self._prior_messages()
        brain = AgentHarness(AgentHarnessConfig(
            provider=provider, model=self.runtime.worker.model_id,
            system=system_prompt(self.work_dir, self.source_task_id, self.skill_id),
            tools=self._tools(), max_turns=60, session_id=self.logger.task_id,
        ), messages=prior_messages)
        prompt = f"来源任务 @{self.source_task_id}。用户要求：{instruction}"
        if self.skill_id:
            prompt = f"修复 Skill {self.skill_id}；" + prompt
        history: list[Any] = list(prior_messages)
        try:
            async for event in brain.prompt(prompt):
                if isinstance(event, MessageEndEvent):
                    message = event.message
                    history.append(message)
                    # One durable transcript projection, updated after every
                    # completed message; Tau's context is an in-memory view.
                    self.storage.save_resource(
                        task_id=self.logger.task_id, run_id=self.logger.run_id,
                        resource_type="builder_session", logical_path="coding/builder/session.json",
                        content=[item.model_dump(by_alias=True, exclude_none=True)
                                 for item in history],
                        metadata={"sourceTaskId": self.source_task_id,
                                  "sourceSkillId": self.skill_id},
                    )
                    if isinstance(message, AssistantMessage) and message.text:
                        print(message.text, flush=True)
                elif isinstance(event, ToolExecutionEndEvent):
                    self.logger.write("skill_builder.tool", {
                        "tool": event.tool_name, "toolCallId": event.tool_call_id,
                        "isError": event.is_error,
                        "result": event.result.model_dump(by_alias=True, exclude_none=True),
                    })
            if self.finished is None:
                self.finished = self._finish("Builder 停止；请检查工作文件与会话记录")
            self.storage.finish_run(task_id=self.logger.task_id, run_id=self.logger.run_id,
                                    status="completed")
            return self.finished
        except BaseException as exc:
            self.storage.finish_run(task_id=self.logger.task_id, run_id=self.logger.run_id,
                                    status="failed", error={"type": type(exc).__name__,
                                                            "message": str(exc)[:500]})
            raise
        finally:
            await self.browser.close()
            self.catalog.close()
            self.storage.close()
