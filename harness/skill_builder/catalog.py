"""Current published Skills and task-owned execution sources."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from harness.storage.factory import open_database
from harness.storage.sqlite_connection import write_transaction


_SKILL_ID = re.compile(r"^[\w][\w.\-]{0,119}$", re.UNICODE)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def validate_skill_id(skill_id: str) -> str:
    value = str(skill_id or "").strip()
    if not _SKILL_ID.fullmatch(value) or value.startswith("_") or value in {".", ".."}:
        raise ValueError("Skill IDs may contain letters, digits, underscores, dots or hyphens and cannot start with an underscore")
    return value


def builder_directory(worktree_dir: Path, task_id: str, work_rel_path: str,
                      owner_task_id: str | None = None) -> Path:
    worktree = Path(worktree_dir).resolve()
    relative = Path(work_rel_path)
    if (not task_id or Path(task_id).name != task_id or task_id in {".", ".."}
            or relative.is_absolute() or not relative.parts
            or any(part in {".", ".."} for part in relative.parts)):
        raise ValueError("Builder index work directory escapes its task directory")
    if owner_task_id:
        if (Path(owner_task_id).name != owner_task_id or owner_task_id in {".", ".."}
                or relative not in {Path("coding") / folder / task_id / "builder"
                                    for folder in ("repairs", "workflows")}):
            raise ValueError("Coding repair index does not match its owner and request identity")
    directory = worktree / (owner_task_id or task_id) / relative
    if directory.resolve() != directory:
        raise ValueError("Builder work directory cannot traverse symbolic links")
    return directory


def _files(directory: Path) -> list[tuple[str, bytes]]:
    if directory.is_symlink() or not directory.is_dir():
        raise ValueError("Skill directory is missing or is a symbolic link")
    items: list[tuple[str, bytes]] = []
    for path in sorted(directory.rglob("*")):
        if path.is_symlink():
            raise ValueError(f"Skill files cannot be symbolic links: {path}")
        if path.is_dir():
            continue
        relative = path.relative_to(directory)
        if any(part.startswith(".") for part in relative.parts):
            continue
        items.append((relative.as_posix(), path.read_bytes()))
    if not any(name == "SKILL.md" for name, _ in items):
        raise ValueError("Skill is missing SKILL.md")
    return items


def _skill_metadata(raw: str) -> dict[str, Any]:
    if raw.startswith("---\n"):
        parts = raw.split("\n---\n", 1)
        if len(parts) == 2:
            value = yaml.safe_load(parts[0][4:]) or {}
            if not isinstance(value, dict):
                raise ValueError("SKILL.md frontmatter must be a mapping")
            return value
    return {}


def inspect_skill(directory: Path) -> dict[str, Any]:
    items = _files(directory)
    digest = hashlib.sha256()
    manifest: list[dict[str, Any]] = []
    for name, body in items:
        digest.update(name.encode("utf-8") + b"\0")
        digest.update(len(body).to_bytes(8, "big") + body)
        manifest.append({"path": name, "sha256": hashlib.sha256(body).hexdigest(),
                         "byteSize": len(body)})
        if name.endswith(".json"):
            try:
                json.loads(body)
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ValueError(f"Invalid Skill JSON file {name}: {exc}") from exc
    raw = dict(items)["SKILL.md"].decode("utf-8")
    metadata = _skill_metadata(raw)
    return {
        "hash": digest.hexdigest(), "files": manifest, "metadata": metadata,
        "version": str(metadata.get("version") or "unversioned"),
    }


def _copy_contents(source: Path, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=False)
    for name, body in _files(source):
        path = destination / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body)


class SkillCatalog:
    def __init__(self, skills_root: Path, database_path: Path, *, worktree_dir: Path | None = None):
        self.root = Path(skills_root).resolve()
        self.worktree = Path(worktree_dir or Path(database_path).parent).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.connection = open_database(database_path)
        self._recover_publish()
        self._recover_delete()

    def close(self) -> None:
        self.connection.close()

    def sync_existing(self) -> None:
        """Index files edited outside the CLI without rewriting their content."""
        self.migrate_legacy_sources()
        for path in sorted(self.root.iterdir()):
            if path.name.startswith((".", "_")) or not path.is_dir():
                continue
            try:
                skill_id = validate_skill_id(path.name)
                info = inspect_skill(path)
            except (ValueError, OSError, UnicodeError, yaml.YAMLError, json.JSONDecodeError):
                continue
            row = self.get(skill_id)
            if row and row["current_hash"] == info["hash"] and not row["deleted"]:
                continue
            with write_transaction(self.connection):
                self._set_current(skill_id, info, deleted=0)

    def _set_current(self, skill_id: str, info: dict[str, Any], *, deleted: int) -> None:
        self.connection.execute(
            """INSERT INTO skill_index
               (skill_id, relative_path, current_version, current_hash, metadata_json,
                deleted, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(skill_id) DO UPDATE SET
                relative_path=excluded.relative_path,
                current_version=excluded.current_version,
                current_hash=excluded.current_hash,
                metadata_json=excluded.metadata_json,
                deleted=excluded.deleted, updated_at=excluded.updated_at""",
            (skill_id, skill_id, info["version"], info["hash"],
             json.dumps(info["metadata"], ensure_ascii=False), deleted, _now()),
        )

    def get(self, skill_id: str) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT * FROM skill_index WHERE skill_id = ?", (validate_skill_id(skill_id),)
        ).fetchone()
        return dict(row) if row else None

    def list(self) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            """SELECT i.*, (SELECT group_concat(s.builder_task_id) FROM skill_builder_sessions s
                JOIN tasks t ON t.task_id=s.builder_task_id
                WHERE s.published_skill_id=i.skill_id AND s.status!='discarded'
                AND t.is_deleted=0 AND t.purge_status='none') AS builder_ids
               FROM skill_index i WHERE i.deleted=0 ORDER BY i.skill_id"""
        ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["builder_task_ids"] = sorted((item.pop("builder_ids") or "").split(","))
            item["builder_task_ids"] = [value for value in item["builder_task_ids"] if value]
            result.append(item)
        return result

    def builder_publication(self, builder_task_id: str, skill_id: str,
                            draft_hash: str | None = None) -> dict[str, Any]:
        row = self.connection.execute(
            "SELECT * FROM skill_builder_sessions WHERE builder_task_id=?",
            (builder_task_id,),
        ).fetchone()
        saved = dict(row) if row else {}
        target = saved.get("published_skill_id") or saved.get("source_skill_id")
        if target and target != skill_id:
            raise ValueError("Draft name differs from the bound published Skill; create a separate Skill")
        expected = (saved.get("published_hash") if saved.get("published_skill_id")
                    else saved.get("base_current_hash") if saved.get("source_skill_id") else None)
        current = self.get(skill_id)
        actual = current["current_hash"] if current and not current["deleted"] else None
        return {"skillId": skill_id, "expectedCurrentHash": expected,
                "currentHash": actual,
                "currentVersion": current["current_version"] if actual else None,
                "status": ("published_same_version" if draft_hash and draft_hash == actual
                           else "target_conflict" if expected != actual else "draft_unpublished")}

    def list_drafts(self, worktree_dir: Path) -> list[dict[str, Any]]:
        """Project saved Builder sessions without changing their files or status."""
        worktree = Path(worktree_dir).resolve()
        rows = self.connection.execute(
            """SELECT s.* FROM skill_builder_sessions s JOIN tasks t
               ON t.task_id=s.builder_task_id
               WHERE s.status != 'discarded' AND t.is_deleted=0
                     AND t.purge_status='none'
                     AND s.work_rel_path NOT LIKE 'coding/workflows/%'
               ORDER BY s.updated_at DESC, s.builder_task_id""",
        ).fetchall()
        drafts = []
        for saved in rows:
            draft = {**dict(saved), "metadata": {}, "file_error": None,
                     "current_hash": None, "published_same_version": False}
            try:
                bound = draft.get("published_skill_id") or draft["source_skill_id"]
                if bound:
                    draft["publication"] = self.builder_publication(draft["builder_task_id"], bound)
                directory = builder_directory(worktree, draft["builder_task_id"],
                                              draft["work_rel_path"], draft.get("owner_task_id"))
                draft["work_dir"] = str(directory)
                document = directory / "SKILL.md"
                if document.is_symlink():
                    raise ValueError("SKILL.md cannot be a symbolic link")
                if not document.is_file():
                    raise ValueError("Builder work files are missing SKILL.md")
                draft["metadata"] = _skill_metadata(document.read_text(encoding="utf-8"))
                if draft["status"] == "finished":
                    info = inspect_skill(directory)
                    draft["metadata"] = info["metadata"]
                    skill_id = validate_skill_id(str(info["metadata"].get("name") or ""))
                    if draft["source_skill_id"] and skill_id != draft["source_skill_id"]:
                        raise ValueError("Repair draft Skill name differs from the original Skill")
                    draft["current_hash"] = info["hash"]
                    if not draft["draft_hash"]:
                        raise ValueError("No complete draft version has been saved; continue editing and review it")
                    if info["hash"] != draft["draft_hash"]:
                        raise ValueError("Work files changed; continue editing and review them")
                    published = self.get(skill_id)
                    draft["publication"] = self.builder_publication(
                        draft["builder_task_id"], skill_id, info["hash"])
                    draft["published_same_version"] = bool(
                        published and not published["deleted"] and published["current_hash"] == info["hash"])
            except (ValueError, OSError, UnicodeError, RuntimeError, yaml.YAMLError) as exc:
                draft["file_error"] = str(exc)
            drafts.append(draft)
        return drafts

    def source_path(self, skill_id: str, content_hash: str, *, task_id: str | None = None) -> Path | None:
        from harness.skill_builder.task_sources import source_path
        return source_path(self, skill_id, content_hash, task_id=task_id)

    def pin_source(self, skill_id: str, content_hash: str, *, task_id: str) -> Path:
        from harness.skill_builder.task_sources import pin_source
        return pin_source(self, skill_id, content_hash, task_id=task_id)

    def migrate_legacy_sources(self) -> dict:
        from harness.skill_builder.task_sources import migrate_legacy_sources
        return migrate_legacy_sources(self)

    def publish(self, skill_id: str, source: Path, *, expected_hash: str,
                expected_current_hash: str | None, source_task_id: str | None = None) -> dict[str, Any]:
        skill_id = validate_skill_id(skill_id)
        source = Path(source).resolve(strict=True)
        info = inspect_skill(source)
        if info["hash"] != expected_hash:
            raise ValueError("Work files changed after publication was selected; check the content hash again")
        self.sync_existing()
        current = self.get(skill_id)
        actual = current["current_hash"] if current and not current["deleted"] else None
        if expected_current_hash != actual:
            raise ValueError("Published Skill has changed; reopen the target version")
        stage_root = self.root / ".publishing"
        stage_root.mkdir(exist_ok=True)
        nonce = uuid.uuid4().hex
        stage = stage_root / nonce
        backup = stage_root / (nonce + ".old")
        journal = stage_root / (nonce + ".json")
        _copy_contents(source, stage)
        if inspect_skill(stage)["hash"] != info["hash"]:
            shutil.rmtree(stage)
            raise ValueError("Work files changed during publication")
        journal.write_text(json.dumps({"skill_id": skill_id, "new_hash": info["hash"],
                                       "old_hash": actual, "stage": str(stage),
                                       "backup": str(backup)}), encoding="utf-8")
        target = self.root / skill_id
        try:
            if target.exists():
                os.replace(target, backup)
            os.replace(stage, target)
            with write_transaction(self.connection):
                self._set_current(skill_id, info, deleted=0)
                # Commit the Builder's new CAS baseline with the current index.
                # Recovery must not leave a successfully published draft with
                # the previous baseline after a crash.
                if source_task_id:
                    self.connection.execute(
                        """UPDATE skill_builder_sessions SET published_skill_id=?,
                           published_hash=? WHERE builder_task_id=?""",
                        (skill_id, info["hash"], source_task_id),
                    )
            journal.unlink()
            if backup.exists():
                shutil.rmtree(backup)
            return {"skill_id": skill_id, "version": info["version"],
                    "hash": info["hash"], "path": str(target)}
        except Exception:
            self._recover_publish()
            raise

    def _recover_publish(self) -> None:
        stage_root = self.root / ".publishing"
        if not stage_root.is_dir():
            return
        for journal in stage_root.glob("*.json"):
            if journal.is_symlink():
                raise RuntimeError("Skill publication journal cannot be a symbolic link")
            data = json.loads(journal.read_text(encoding="utf-8"))
            skill_id = validate_skill_id(data["skill_id"])
            target = self.root / skill_id
            # Derive cleanup paths from the journal filename. A partially
            # written or tampered journal must never nominate an outside path
            # for rmtree/replace during startup recovery.
            if not re.fullmatch(r"[0-9a-f]{32}", journal.stem):
                raise RuntimeError("Invalid Skill publication journal name")
            stage = stage_root / journal.stem
            backup = stage_root / (journal.stem + ".old")
            if data.get("stage") != str(stage) or data.get("backup") != str(backup):
                raise RuntimeError("Skill publication journal paths do not match")
            row = self.get(skill_id)
            committed = row is not None and row["current_hash"] == data["new_hash"]
            if committed:
                if not target.is_dir() or inspect_skill(target)["hash"] != data["new_hash"]:
                    raise RuntimeError(f"Cannot recover committed Skill files: {skill_id}")
                if backup.exists():
                    shutil.rmtree(backup)
            elif backup.exists():
                if target.exists():
                    shutil.rmtree(target)
                os.replace(backup, target)
            elif data["old_hash"] is None and target.exists():
                shutil.rmtree(target)
            if stage.exists():
                shutil.rmtree(stage)
            journal.unlink()

    def delete(self, skill_id: str, *, expected_hash: str) -> None:
        skill_id = validate_skill_id(skill_id)
        self.sync_existing()
        row = self.get(skill_id)
        if row is None or row["deleted"] or row["current_hash"] != expected_hash:
            raise ValueError("The deletion target version has changed")
        target = self.root / skill_id
        if inspect_skill(target)["hash"] != expected_hash:
            raise ValueError("Published files do not match the index")
        deleted_dir = self.root / ".deleted"
        deleted_dir.mkdir(exist_ok=True)
        nonce = uuid.uuid4().hex
        moved = deleted_dir / (skill_id + "-" + nonce)
        journal = deleted_dir / (nonce + ".json")
        journal.write_text(json.dumps({"skill_id": skill_id, "hash": expected_hash}),
                           encoding="utf-8")
        try:
            os.replace(target, moved)
            with write_transaction(self.connection):
                self.connection.execute(
                    "UPDATE skill_index SET deleted=1, updated_at=? WHERE skill_id=?",
                    (_now(), skill_id),
                )
        except Exception:
            self._recover_delete()
            raise
        shutil.rmtree(moved)
        journal.unlink()

    def _recover_delete(self) -> None:
        deleted_dir = self.root / ".deleted"
        if not deleted_dir.is_dir():
            return
        for journal in deleted_dir.glob("*.json"):
            if journal.is_symlink() or not re.fullmatch(r"[0-9a-f]{32}", journal.stem):
                raise RuntimeError("Invalid Skill deletion journal name")
            data = json.loads(journal.read_text(encoding="utf-8"))
            skill_id = validate_skill_id(data["skill_id"])
            target = self.root / skill_id
            moved = deleted_dir / (skill_id + "-" + journal.stem)
            row = self.get(skill_id)
            committed = bool(row and row["deleted"] and row["current_hash"] == data["hash"])
            if committed:
                if target.exists():
                    raise RuntimeError(f"Deleted Skill directory reappeared: {skill_id}")
                if moved.exists():
                    shutil.rmtree(moved)
            elif moved.exists():
                if target.exists() or inspect_skill(moved)["hash"] != data["hash"]:
                    raise RuntimeError(f"Skill deletion rollback identity mismatch: {skill_id}")
                os.replace(moved, target)
            elif not target.exists():
                raise RuntimeError(f"Skill deletion rollback is missing published files: {skill_id}")
            journal.unlink()

    def begin_invocation(self, *, task_id: str, run_id: str, skill_id: str,
                         content_hash: str, input_hash: str | None = None) -> dict[str, Any]:
        skill_id = validate_skill_id(skill_id)
        snapshot = self.pin_source(skill_id, content_hash, task_id=task_id)
        version = inspect_skill(snapshot)["version"]
        invocation_id = uuid.uuid4().hex
        with write_transaction(self.connection):
            self.connection.execute(
                """INSERT INTO skill_invocations
                   (invocation_id, task_id, run_id, skill_id, skill_hash,
                    skill_version, input_ref, status, started_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, 'started', ?)""",
                (invocation_id, task_id, run_id, skill_id, content_hash,
                 version, input_hash, _now()),
            )
        return {"invocationId": invocation_id, "skillId": skill_id,
                "hash": content_hash, "version": version}

    def finish_invocation(self, invocation_id: str, *, status: str,
                          result_ref: str | None = None) -> None:
        with write_transaction(self.connection):
            updated = self.connection.execute(
                """UPDATE skill_invocations SET status=?, result_ref=?, finished_at=?
                   WHERE invocation_id=? AND status='started'""",
                (str(status), result_ref, _now(), invocation_id),
            )
            if updated.rowcount != 1:
                raise ValueError("Skill invocation is missing or has ended")
