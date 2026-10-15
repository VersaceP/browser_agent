"""Small CLI surface for Skill creation, publication and deletion."""

from __future__ import annotations

import hashlib
import json
import re
import shlex
import shutil
import sys
import uuid
from pathlib import Path
from typing import Any, Callable

from harness.skill_builder.catalog import SkillCatalog, builder_directory, inspect_skill, validate_skill_id
from harness.skill_builder.references import SkillReferences, draft_skill_id
from harness.storage.factory import resolve_sqlite_path
from harness.storage.sqlite_connection import write_transaction
from runtime_config import load_runtime_config


def _tokens(line: str) -> list[str]:
    return shlex.split(line.replace("　", " ").replace("\xa0", " "))


def split_command(line: str) -> tuple[str, str]:
    lexer = shlex.shlex(line.strip(), posix=True)
    lexer.whitespace_split = True
    lexer.commenters = ""
    command = lexer.get_token()
    return command or "", line.strip()[lexer.instream.tell():].lstrip()


_BUILDER_COMMANDS = {"/skill-create", "/skill-publish", "/skill-delete"}
_REMOVED_COMMANDS = {"/skill-edit", "/skill-repair", "/skill-draft", "/skill-show"}


def command_kind(line: str) -> str | None:
    """Route syntax only; /<Skill> always executes, never repairs a task."""
    try:
        command, _ = split_command(line)
    except ValueError:
        return "invalid" if line.lstrip().startswith("/") else None
    if not command:
        return None
    if command in _BUILDER_COMMANDS:
        return "builder"
    if command in _REMOVED_COMMANDS:
        return "removed"
    if command == "/skill":
        return "skill"
    if not command.startswith("/") or command in {"/browser", "/workflow", "/resume"}:
        return None
    # Absolute paths remain ordinary task text. Quoted Skill names may contain spaces.
    name = command[1:]
    if not name or "/" in name or "\\" in name or name.startswith("@"):
        return None
    return "skill-run"


def recognizes(line: str, *, config_path: str = "config.json") -> bool:
    return command_kind(line) == "builder"


def _catalog(runtime: Any) -> SkillCatalog:
    root = Path(__file__).resolve().parents[2] / "skills"
    db = resolve_sqlite_path(runtime.harness.storage_sqlite_path, runtime.harness.worktree_dir)
    return SkillCatalog(root, db, worktree_dir=Path(runtime.harness.worktree_dir))


def builder_resume_record(runtime: Any, task_id: str) -> dict[str, Any] | None:
    """Identify saved standalone/repair Builders before execution-mode routing."""
    catalog = _catalog(runtime)
    try:
        row = catalog.connection.execute(
            """SELECT s.* FROM skill_builder_sessions s JOIN tasks t
               ON t.task_id=s.builder_task_id WHERE s.builder_task_id=?
               AND t.is_deleted=0 AND t.purge_status='none'
               AND s.work_rel_path NOT LIKE 'coding/workflows/%'""", (task_id,),
        ).fetchone()
        if row and row["status"] == "discarded":
            raise ValueError("Builder work files have been deleted; this draft cannot be resumed")
        return dict(row) if row else None
    finally:
        catalog.close()


async def _run_builder_session(runtime: Any, *, source_task_id: str,
                               instruction: str, skill_id: str | None = None,
                               builder_task_id: str | None = None,
                               source_invocation_id: str | None = None,
                               analysis_task_id: str | None = None,
                               skill_mode: bool = False) -> dict[str, Any]:
    """Use the session on the event-loop thread that owns its DB handle."""
    from harness.skill_builder.session import BuilderSession
    from harness.runtime.resume_state import acquire_run_lock, release_run_lock

    resuming = bool(builder_task_id)
    builder_task_id = builder_task_id or uuid.uuid4().hex
    lock = acquire_run_lock(Path(runtime.harness.worktree_dir) / builder_task_id)
    try:
        if resuming and builder_resume_record(runtime, builder_task_id) is None:
            raise ValueError("Saved Builder task is missing or deleted")
        session = BuilderSession(runtime, source_task_id=source_task_id,
                                 skill_id=skill_id, builder_task_id=builder_task_id,
                                 source_invocation_id=source_invocation_id,
                                 analysis_task_id=analysis_task_id)
        from harness.runtime.session_output import block
        block("Skill Builder", f"Started @{session.logger.task_id}", [
            ("草稿目录", session.work_dir), ("初始来源", "@" + session.source_task_id),
            ("本轮分析", "@" + session.source.task_id),
            ("恢复草稿", f"/resume {session.logger.task_id} [advice]")])
        if skill_mode:
            session.required_orchestration = True
            instruction += ("\nDesign and implement the Skill in this same Coding session. Read describe_skill_runtime "
                            "before writing orchestration.json. Observe the referenced run and use run_skill_trial "
                            "when the modified operation lacks actual Skill Runtime evidence.")
        return await session.run(instruction)
    finally:
        if lock is not None:
            release_run_lock(lock)


async def resume_builder(runtime: Any, row: dict[str, Any], instruction: str) -> dict[str, Any]:
    from harness.runtime.resume_state import acquire_run_lock, release_run_lock
    owner = row.get("owner_task_id")
    lock = acquire_run_lock(Path(runtime.harness.worktree_dir) / owner) if owner else None
    try:
        return await _run_builder_session(
            runtime, source_task_id=row["source_task_id"], skill_id=row["source_skill_id"],
            builder_task_id=row["builder_task_id"], source_invocation_id=row.get("source_invocation_id"),
            instruction=instruction or "Continue the saved Skill creation task using its recorded goal and evidence.",
            skill_mode=True,
        )
    finally:
        if lock is not None:
            release_run_lock(lock)


def _choose_version(candidates: list[dict[str, Any]], *, operation: str) -> dict[str, Any] | None:
    """Only version ambiguity prompts; Skill-name ambiguity requires exact input."""
    if len(candidates) == 1:
        return candidates[0]
    print(f"Multiple versions are available to {operation}; select one:")
    for index, item in enumerate(candidates, 1):
        row = item["row"]
        if item["kind"] == "published":
            detail = f"published | Version: {row['current_version']} | Hash: {row['current_hash']}"
        else:
            metadata = row.get("metadata") or {}
            detail = (f"draft | Task ID: {row['builder_task_id']} | Version: "
                      f"{metadata.get('version') or 'unversioned'} | Status: {row['status']} | "
                      f"Updated: {row.get('updated_at') or '?'} | Hash: {row.get('draft_hash') or '(not saved)'}")
            if row.get("selection_error"):
                detail += f" | Unavailable: {row['selection_error']}"
        print(f"  {index}. {detail}")
    if not sys.stdin.isatty():
        print("No version selected. Run this command interactively to choose a version.")
        return None
    while True:
        try:
            answer = input(f"Select version to {operation} (number; Enter cancels)> ").strip()
        except (EOFError, KeyboardInterrupt):
            return None
        if not answer:
            return None
        if answer.isascii() and answer.isdecimal() and 1 <= int(answer) <= len(candidates):
            return candidates[int(answer) - 1]
        print(f"Enter a number from 1 to {len(candidates)}, or press Enter to cancel.")


def _choose_source_invocation(rows: list[dict[str, Any]], skill_id: str, task_id: str) -> str | None:
    """Choose an evidenced source version only when a task used different hashes."""
    print(f"Task {task_id} executed multiple versions of Skill {skill_id}; select a source invocation:")
    for index, row in enumerate(rows, 1):
        print(f"  {index}. Invocation ID: {row['invocation_id']} | Version: {row['skill_version']} | "
              f"Hash: {row['skill_hash']} | Status: {row['status']} | Started: {row['started_at']}")
    if not sys.stdin.isatty():
        print("No source version selected. Run this command interactively to choose a version.")
        return None
    while True:
        try:
            answer = input("Select source invocation (number; Enter cancels)> ").strip()
        except (EOFError, KeyboardInterrupt):
            return None
        if not answer:
            return None
        if answer.isascii() and answer.isdecimal() and 1 <= int(answer) <= len(rows):
            return rows[int(answer) - 1]["invocation_id"]
        print(f"Enter a number from 1 to {len(rows)}, or press Enter to cancel.")


def _update_binding(runtime: Any, reference: str, task_id: str) -> tuple[str, str | None] | None:
    catalog = _catalog(runtime)
    try:
        catalog.sync_existing()
        skill_id = SkillReferences(catalog, Path(runtime.harness.worktree_dir)).published(reference)["skill_id"]
        rows = [dict(row) for row in catalog.connection.execute(
            """SELECT invocation_id, skill_hash, skill_version, status, started_at
               FROM skill_invocations WHERE task_id=? AND skill_id=?
               ORDER BY started_at, invocation_id""", (task_id, skill_id),
        ).fetchall()]
        if len({row["skill_hash"] for row in rows}) > 1:
            invocation_id = _choose_source_invocation(rows, skill_id, task_id)
            if invocation_id is None:
                return None
            return skill_id, invocation_id
        return skill_id, None
    finally:
        catalog.close()


def _draft_fingerprint(directory: Path) -> str:
    """Hash deletion contents without requiring a complete, valid Skill package."""
    if directory.is_symlink() or not directory.is_dir():
        raise ValueError("Draft directory is missing or is a symbolic link")
    digest = hashlib.sha256()
    for path in sorted(directory.rglob("*")):
        if path.is_symlink():
            raise ValueError("Draft deletion cannot traverse symbolic links")
        relative = path.relative_to(directory).as_posix()
        if path.is_dir():
            digest.update(json.dumps([relative, "directory"]).encode() + b"\n")
        elif path.is_file():
            content = hashlib.sha256()
            with path.open("rb") as stream:
                for chunk in iter(lambda: stream.read(65536), b""):
                    content.update(chunk)
            digest.update(json.dumps([relative, "file", content.hexdigest()]).encode() + b"\n")
        else:
            raise ValueError("Draft contains a non-regular file")
    return digest.hexdigest()


def _publish_draft(catalog: SkillCatalog, worktree: Path, row: dict[str, Any], skill_id: str) -> dict[str, Any]:
    from harness.runtime.resume_state import acquire_run_lock, release_run_lock
    lock = acquire_run_lock(worktree / (row.get("owner_task_id") or row["builder_task_id"]))
    try:
        saved = catalog.connection.execute(
            "SELECT * FROM skill_builder_sessions WHERE builder_task_id=?", (row["builder_task_id"],),
        ).fetchone()
        if (not saved or saved["status"] != "finished" or not saved["draft_hash"]):
            raise ValueError(f"Draft is unfinished. Continue with /resume {row['builder_task_id']}")
        if (saved["draft_hash"] != row["draft_hash"] or saved["work_rel_path"] != row["work_rel_path"]
                or saved["owner_task_id"] != row.get("owner_task_id")):
            raise ValueError("Builder draft version or ownership changed after selection")
        work_dir = builder_directory(worktree, row["builder_task_id"], row["work_rel_path"], row.get("owner_task_id"))
        info = inspect_skill(work_dir)
        if info["hash"] != row["draft_hash"]:
            raise ValueError(f"Builder files changed after review; use /resume {row['builder_task_id']} to review them again")
        actual_id = validate_skill_id(str(info["metadata"].get("name") or row["source_skill_id"] or ""))
        if actual_id != skill_id:
            raise ValueError("Draft Skill identity changed after selection")
        publication = catalog.builder_publication(row["builder_task_id"], skill_id, info["hash"])
        return catalog.publish(skill_id, work_dir, expected_hash=info["hash"],
                               expected_current_hash=publication["expectedCurrentHash"],
                               source_task_id=row["builder_task_id"])
    finally:
        release_run_lock(lock)


def _delete_draft(catalog: SkillCatalog, worktree: Path, row: dict[str, Any]) -> None:
    from harness.runtime.resume_state import acquire_run_lock, release_run_lock
    lock = acquire_run_lock(worktree / (row.get("owner_task_id") or row["builder_task_id"]))
    try:
        if row.get("selection_error"):
            raise ValueError(row["selection_error"])
        saved = catalog.connection.execute(
            "SELECT * FROM skill_builder_sessions WHERE builder_task_id=?", (row["builder_task_id"],),
        ).fetchone()
        if (not saved or saved["status"] != row["status"] or saved["draft_hash"] != row["draft_hash"]
                or saved["work_rel_path"] != row["work_rel_path"] or saved["owner_task_id"] != row.get("owner_task_id")):
            raise ValueError("Builder file version, status or ownership has changed")
        directory = builder_directory(worktree, row["builder_task_id"], row["work_rel_path"], row.get("owner_task_id"))
        if _draft_fingerprint(directory) != row["selection_hash"]:
            raise ValueError("Builder files changed after selection; select the deletion target again")
        shutil.rmtree(directory)
        with write_transaction(catalog.connection):
            catalog.connection.execute(
                """UPDATE skill_builder_sessions SET status='discarded', draft_hash=NULL,
                   updated_at=datetime('now') WHERE builder_task_id=?""", (row["builder_task_id"],),
            )
        print(f"Deleted Builder work files: {directory}")
    finally:
        release_run_lock(lock)


def execute(line: str, *, config_path: str = "config.json",
            run_blocking: Callable[[Any], Any], skill_mode: bool = False) -> int:
    """Resolve the requested identity and retain hash checks inside the CLI."""
    command, tail = split_command(line)
    if command not in _BUILDER_COMMANDS:
        return 2
    if command == "/skill-create":
        reference, instruction = split_command(tail)
        skill_reference = None
        if reference and not reference.startswith("@"):
            skill_reference = reference
            reference, instruction = split_command(instruction)
        if not re.fullmatch(r"@[0-9a-fA-F]{32}", reference):
            print("Usage: /skill-create [Skill-name-or-ID] @<task-id> [request]; a full task ID is required")
            return 2
    else:
        words = _tokens(tail)
        if len(words) != 1 or words[0].startswith("@"):
            print(f"Usage: {command} <Skill-name-or-ID>")
            return 2
        reference = words[0]
    runtime = load_runtime_config(config_path)
    from harness.runtime.resume_state import configure_resume_storage
    configure_resume_storage(backend=getattr(runtime.harness, "storage_backend", "db"),
                             worktree_dir=runtime.harness.worktree_dir,
                             sqlite_path=runtime.harness.storage_sqlite_path)
    if command == "/skill-create":
        skill_id = invocation_id = None
        task_id = reference[1:].lower()
        if skill_reference is not None:
            binding = _update_binding(runtime, skill_reference, task_id)
            if binding is None:
                return 3
            skill_id, invocation_id = binding
            print(f"Update Skill: {skill_id}; source task: {task_id}")
        result = run_blocking(_run_builder_session(
            runtime, source_task_id=task_id, skill_mode=skill_mode,
            skill_id=skill_id, source_invocation_id=invocation_id,
            instruction=instruction or (
                "Update the bound Skill using the referenced task's original goal and execution evidence."
                if skill_id else "Create a reusable Skill from the referenced task's original goal and execution evidence."),
        ))
        from harness.runtime.session_output import result as print_result
        print_result("Skill Builder", result)
        return 0 if result.get("status") == "draft_saved" else 1
    catalog = _catalog(runtime)
    try:
        catalog.sync_existing()
        worktree = Path(runtime.harness.worktree_dir).resolve()
        references = SkillReferences(catalog, worktree)
        identity = references.identity(reference, include_drafts=True)
        skill_id = identity["skill_id"]
        drafts = [row for row in references.drafts() if draft_skill_id(row) == skill_id]
        if command == "/skill-publish":
            if not drafts:
                raise ValueError(f"No Builder draft matches {skill_id!r}. Create a draft first.")
            selected = _choose_version([{"kind": "draft", "row": row} for row in drafts], operation="publish")
            if selected is None:
                return 3
            published = _publish_draft(catalog, worktree, selected["row"], skill_id)
            print(json.dumps({"published": published}, ensure_ascii=False, indent=2))
            return 0
        candidates = ([{"kind": "published", "row": identity["published"]}] if identity["published"] else [])
        for row in drafts:
            row = dict(row)
            try:
                directory = builder_directory(worktree, row["builder_task_id"], row["work_rel_path"], row.get("owner_task_id"))
                row["selection_hash"] = _draft_fingerprint(directory)
            except (ValueError, OSError) as exc:
                row["selection_error"] = str(exc)
            candidates.append({"kind": "draft", "row": row})
        selected = _choose_version(candidates, operation="delete")
        if selected is None:
            return 3
        row = selected["row"]
        if selected["kind"] == "draft":
            _delete_draft(catalog, worktree, row)
        else:
            catalog.delete(skill_id, expected_hash=row["current_hash"])
            print(f"Deleted published Skill {skill_id}; existing task execution sources remain in their task directories.")
        return 0
    finally:
        catalog.close()
