"""Explicit CLI commands for building, repairing and publishing Skills."""

from __future__ import annotations

import json
import shlex
import shutil
from pathlib import Path
from typing import Any, Callable

from harness.skill_builder.catalog import SkillCatalog, inspect_skill, validate_skill_id
from harness.storage.factory import resolve_sqlite_path
from harness.storage.sqlite_connection import write_transaction
from runtime_config import load_runtime_config


def _tokens(line: str) -> list[str]:
    return shlex.split(line.replace("　", " ").replace("\xa0", " "))


def recognizes(line: str, *, config_path: str = "config.json") -> bool:
    try:
        words = _tokens(line)
    except ValueError:
        return line.startswith("/")
    if not words:
        return False
    command = words[0]
    if command in {"/skill-create", "/skill-publish", "/skill-edit", "/skill-delete"}:
        return True
    if command.startswith("/") and len(words) > 1 and words[1].startswith("@"):
        # Exact Skill names are checked by dispatch. Treat an unknown name as
        # this command family so it cannot accidentally become a browser task.
        return True
    return False


def _catalog(runtime: Any) -> SkillCatalog:
    root = Path(__file__).resolve().parents[2] / "skills"
    db = resolve_sqlite_path(runtime.harness.storage_sqlite_path,
                             runtime.harness.worktree_dir)
    return SkillCatalog(root, db)


def _builder_row(catalog: SkillCatalog, builder_task_id: str) -> dict[str, Any]:
    task_id = builder_task_id.removeprefix("@")
    row = catalog.connection.execute(
        "SELECT * FROM skill_builder_sessions WHERE builder_task_id=?", (task_id,),
    ).fetchone()
    if row is None:
        raise ValueError(f"Builder 任务不存在: {task_id}")
    return dict(row)


def execute(line: str, *, config_path: str = "config.json",
            run_blocking: Callable[[Any], Any]) -> int:
    """Return a CLI exit code. No command here publishes without user input."""
    words = _tokens(line)
    if not words:
        return 2
    runtime = load_runtime_config(config_path)
    catalog = _catalog(runtime)
    try:
        catalog.sync_existing()
        command = words[0]
        if command == "/skill-create" or (
            command.startswith("/") and command not in {
                "/skill-publish", "/skill-edit", "/skill-delete"
            } and len(words) > 1 and words[1].startswith("@")
        ):
            if len(words) < 3 or not words[1].startswith("@"):
                print("用法: /skill-create @<task_id> 用户需求/用户建议；"
                      "/<确切Skill名> @<task_id> 用户需求/用户建议")
                return 2
            skill_id = None if command == "/skill-create" else validate_skill_id(command[1:])
            if skill_id and not catalog.get(skill_id):
                print(f"没有名为 {skill_id!r} 的已登记 Skill")
                return 2
            invocation_id = None
            request_words = words[2:]
            if skill_id and request_words[:1] == ["--invocation"]:
                if len(request_words) < 3:
                    print("用法: /<Skill名> @<task_id> --invocation <id> 用户建议")
                    return 2
                invocation_id = request_words[1]
                request_words = request_words[2:]
            if skill_id:
                rows = catalog.connection.execute(
                    """SELECT invocation_id, skill_hash, skill_version, status,
                              started_at, finished_at FROM skill_invocations
                       WHERE task_id=? AND skill_id=? ORDER BY started_at, invocation_id""",
                    (words[1][1:], skill_id),
                ).fetchall()
                if len(rows) > 1 and not invocation_id:
                    print(json.dumps({"status": "select_invocation", "skill": skill_id,
                                      "sourceTaskId": words[1][1:],
                                      "invocations": [dict(row) for row in rows],
                                      "next": f"/{skill_id} {words[1]} --invocation <id> 用户建议"},
                                     ensure_ascii=False, indent=2))
                    return 3
                if invocation_id and not any(row["invocation_id"] == invocation_id for row in rows):
                    print("指定的 invocation_id 不属于该 Skill 与任务")
                    return 2
            from harness.skill_builder.session import BuilderSession
            session = BuilderSession(runtime, source_task_id=words[1][1:],
                                     skill_id=skill_id, source_invocation_id=invocation_id)
            result = run_blocking(session.run(" ".join(request_words)))
            print(json.dumps({"builderTaskId": result["builderTaskId"],
                              "workDir": result["workDir"],
                              "files": result["files"],
                              "next": [f"/skill-edit @{result['builderTaskId']} <建议>",
                                       f"/skill-publish @{result['builderTaskId']}"],
                              "summary": result["summary"]}, ensure_ascii=False, indent=2))
            return 0
        if command == "/skill-edit":
            if len(words) < 3 or not words[1].startswith("@"):
                print("用法: /skill-edit @<builder_task_id> 修改建议")
                return 2
            row = _builder_row(catalog, words[1])
            if row["status"] == "discarded":
                raise ValueError("Builder 工作文件已删除")
            from harness.skill_builder.session import BuilderSession
            session = BuilderSession(runtime, source_task_id=row["source_task_id"],
                                     skill_id=row["source_skill_id"],
                                     builder_task_id=row["builder_task_id"])
            result = run_blocking(session.run(" ".join(words[2:])))
            print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
            return 0
        if command == "/skill-publish":
            if len(words) != 2 or not words[1].startswith("@"):
                print("用法: /skill-publish @<builder_task_id>")
                return 2
            row = _builder_row(catalog, words[1])
            if row["status"] != "finished" or not row["draft_hash"]:
                raise ValueError("Builder 工作文件尚未形成可发布的完整 Skill")
            worktree = Path(runtime.harness.worktree_dir).resolve()
            work_dir = worktree / row["builder_task_id"] / row["work_rel_path"]
            info = inspect_skill(work_dir)
            if info["hash"] != row["draft_hash"]:
                raise ValueError("Builder 展示后文件已变化；请先用 /skill-edit 复核")
            skill_id = validate_skill_id(str(info["metadata"].get("name") or
                                             row["source_skill_id"] or ""))
            if row["source_skill_id"] and skill_id != row["source_skill_id"]:
                raise ValueError("修复任务的 Skill 名称不能静默改变；请另建 Skill")
            if row["source_skill_id"]:
                expected_current = row["base_current_hash"]
            else:
                expected_current = None
            published = catalog.publish(
                skill_id, work_dir, expected_hash=info["hash"],
                expected_current_hash=expected_current,
                source_task_id=row["builder_task_id"],
            )
            print(json.dumps({"published": published}, ensure_ascii=False, indent=2))
            return 0
        if command == "/skill-delete":
            if len(words) != 3 or not words[2].startswith("@"):
                print("用法: /skill-delete <确切Skill名> @<当前hash>；"
                      "/skill-delete @<builder_task_id> @<展示的draft_hash>")
                return 2
            if words[1].startswith("@"):
                row = _builder_row(catalog, words[1])
                if row["status"] != "finished" or row["draft_hash"] != words[2][1:]:
                    raise ValueError("Builder 工作文件版本或状态已变化")
                worktree = Path(runtime.harness.worktree_dir).resolve()
                task_dir = worktree / row["builder_task_id"]
                work_dir = task_dir / row["work_rel_path"]
                if (work_dir.is_symlink() or not work_dir.resolve().is_relative_to(task_dir.resolve())
                        or inspect_skill(work_dir)["hash"] != row["draft_hash"]):
                    raise ValueError("Builder 工作文件身份或内容已变化")
                shutil.rmtree(work_dir)
                with write_transaction(catalog.connection):
                    catalog.connection.execute(
                        """UPDATE skill_builder_sessions
                           SET status='discarded', draft_hash=NULL, updated_at=datetime('now')
                           WHERE builder_task_id=?""", (row["builder_task_id"],))
                print(f"已删除 Builder 工作文件: {work_dir}")
                return 0
            skill_id = validate_skill_id(words[1])
            catalog.delete(skill_id, expected_hash=words[2][1:])
            print(f"已删除正式 Skill {skill_id}；历史版本快照按任务回溯保留。")
            return 0
        return 2
    finally:
        catalog.close()
