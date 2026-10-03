"""File-authoritative Skill versions with a global SQLite index."""

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
        raise ValueError("Skill 名称只能包含字母、数字、下划线、点或连字符，且不能以下划线开头")
    return value


def _files(directory: Path) -> list[tuple[str, bytes]]:
    if directory.is_symlink() or not directory.is_dir():
        raise ValueError("Skill 目录不存在或是符号链接")
    items: list[tuple[str, bytes]] = []
    for path in sorted(directory.rglob("*")):
        if path.is_symlink():
            raise ValueError(f"Skill 文件不能是符号链接: {path}")
        if path.is_dir():
            continue
        relative = path.relative_to(directory)
        if any(part.startswith(".") for part in relative.parts):
            continue
        items.append((relative.as_posix(), path.read_bytes()))
    if not any(name == "SKILL.md" for name, _ in items):
        raise ValueError("Skill 缺少 SKILL.md")
    return items


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
            json.loads(body)
    raw = dict(items)["SKILL.md"].decode("utf-8")
    metadata: dict[str, Any] = {}
    if raw.startswith("---\n"):
        parts = raw.split("\n---\n", 1)
        if len(parts) == 2:
            value = yaml.safe_load(parts[0][4:]) or {}
            if not isinstance(value, dict):
                raise ValueError("SKILL.md frontmatter 必须为映射")
            metadata = value
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
    def __init__(self, skills_root: Path, database_path: Path):
        self.root = Path(skills_root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.connection = open_database(database_path)
        self._recover_publish()
        self._recover_delete()

    def close(self) -> None:
        self.connection.close()

    def _snapshot(self, skill_id: str, source: Path, info: dict[str, Any],
                  source_task_id: str | None = None) -> Path:
        target = self.root / ".versions" / skill_id / info["hash"]
        if not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            staged = target.with_name(target.name + ".tmp-" + uuid.uuid4().hex)
            _copy_contents(source, staged)
            if inspect_skill(staged)["hash"] != info["hash"]:
                shutil.rmtree(staged)
                raise ValueError("Skill 文件在复制时发生变化")
            os.replace(staged, target)
        elif inspect_skill(target)["hash"] != info["hash"]:
            raise RuntimeError(f"Skill {skill_id} 的历史版本快照已损坏")
        with write_transaction(self.connection):
            self.connection.execute(
                """INSERT OR IGNORE INTO skill_versions
                   (skill_id, content_hash, version, snapshot_path, files_json,
                    metadata_json, source_task_id, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (skill_id, info["hash"], info["version"],
                 str(target.relative_to(self.root)),
                 json.dumps(info["files"], ensure_ascii=False),
                 json.dumps(info["metadata"], ensure_ascii=False),
                 source_task_id, _now()),
            )
        return target

    def sync_existing(self) -> None:
        """Index files edited outside the CLI without rewriting their content."""
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
            self._snapshot(skill_id, path, info)
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
        return [dict(row) for row in self.connection.execute(
            "SELECT * FROM skill_index WHERE deleted = 0 ORDER BY skill_id"
        ).fetchall()]

    def version_path(self, skill_id: str, content_hash: str) -> Path | None:
        row = self.connection.execute(
            "SELECT snapshot_path FROM skill_versions WHERE skill_id=? AND content_hash=?",
            (validate_skill_id(skill_id), content_hash),
        ).fetchone()
        if row is None:
            return None
        path = self.root / row["snapshot_path"]
        return path if path.is_dir() and inspect_skill(path)["hash"] == content_hash else None

    def publish(self, skill_id: str, source: Path, *, expected_hash: str,
                expected_current_hash: str | None, source_task_id: str | None = None) -> dict[str, Any]:
        skill_id = validate_skill_id(skill_id)
        source = Path(source).resolve(strict=True)
        info = inspect_skill(source)
        if info["hash"] != expected_hash:
            raise ValueError("工作文件在选择发布后发生变化，请重新检查 hash")
        self.sync_existing()
        current = self.get(skill_id)
        actual = current["current_hash"] if current and not current["deleted"] else None
        if expected_current_hash != actual:
            raise ValueError("正式 Skill 已发生变化，请重新打开目标版本")
        self._snapshot(skill_id, source, info, source_task_id)
        stage_root = self.root / ".publishing"
        stage_root.mkdir(exist_ok=True)
        nonce = uuid.uuid4().hex
        stage = stage_root / nonce
        backup = stage_root / (nonce + ".old")
        journal = stage_root / (nonce + ".json")
        _copy_contents(source, stage)
        if inspect_skill(stage)["hash"] != info["hash"]:
            shutil.rmtree(stage)
            raise ValueError("发布期间工作文件发生变化")
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
                raise RuntimeError("Skill 发布日志不能是符号链接")
            data = json.loads(journal.read_text(encoding="utf-8"))
            skill_id = validate_skill_id(data["skill_id"])
            target = self.root / skill_id
            # Derive cleanup paths from the journal filename. A partially
            # written or tampered journal must never nominate an outside path
            # for rmtree/replace during startup recovery.
            if not re.fullmatch(r"[0-9a-f]{32}", journal.stem):
                raise RuntimeError("Skill 发布日志名称无效")
            stage = stage_root / journal.stem
            backup = stage_root / (journal.stem + ".old")
            if data.get("stage") != str(stage) or data.get("backup") != str(backup):
                raise RuntimeError("Skill 发布日志路径不一致")
            row = self.get(skill_id)
            committed = row is not None and row["current_hash"] == data["new_hash"]
            if committed:
                if not target.is_dir() or inspect_skill(target)["hash"] != data["new_hash"]:
                    raise RuntimeError(f"已提交 Skill {skill_id} 的文件不可恢复")
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
            raise ValueError("删除目标的版本已变化")
        target = self.root / skill_id
        if inspect_skill(target)["hash"] != expected_hash:
            raise ValueError("正式文件与索引不一致")
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
                raise RuntimeError("Skill 删除日志名称无效")
            data = json.loads(journal.read_text(encoding="utf-8"))
            skill_id = validate_skill_id(data["skill_id"])
            target = self.root / skill_id
            moved = deleted_dir / (skill_id + "-" + journal.stem)
            row = self.get(skill_id)
            committed = bool(row and row["deleted"] and row["current_hash"] == data["hash"])
            if committed:
                if target.exists():
                    raise RuntimeError(f"已删除 Skill {skill_id} 的正式目录又出现")
                if moved.exists():
                    shutil.rmtree(moved)
            elif moved.exists():
                if target.exists() or inspect_skill(moved)["hash"] != data["hash"]:
                    raise RuntimeError(f"Skill {skill_id} 删除回滚身份不一致")
                os.replace(moved, target)
            elif not target.exists():
                raise RuntimeError(f"Skill {skill_id} 删除回滚缺少正式文件")
            journal.unlink()

    def begin_invocation(self, *, task_id: str, run_id: str, skill_id: str,
                         content_hash: str, input_hash: str | None = None) -> dict[str, Any]:
        skill_id = validate_skill_id(skill_id)
        row = self.connection.execute(
            "SELECT version FROM skill_versions WHERE skill_id=? AND content_hash=?",
            (skill_id, content_hash),
        ).fetchone()
        if row is None or self.version_path(skill_id, content_hash) is None:
            raise ValueError("待执行 Skill 版本快照不可用")
        invocation_id = uuid.uuid4().hex
        with write_transaction(self.connection):
            self.connection.execute(
                """INSERT INTO skill_invocations
                   (invocation_id, task_id, run_id, skill_id, skill_hash,
                    skill_version, input_ref, status, started_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, 'started', ?)""",
                (invocation_id, task_id, run_id, skill_id, content_hash,
                 row["version"], input_hash, _now()),
            )
        return {"invocationId": invocation_id, "skillId": skill_id,
                "hash": content_hash, "version": row["version"]}

    def finish_invocation(self, invocation_id: str, *, status: str,
                          result_ref: str | None = None) -> None:
        with write_transaction(self.connection):
            updated = self.connection.execute(
                """UPDATE skill_invocations SET status=?, result_ref=?, finished_at=?
                   WHERE invocation_id=? AND status='started'""",
                (str(status), result_ref, _now(), invocation_id),
            )
            if updated.rowcount != 1:
                raise ValueError("Skill invocation 不存在或已结束")
