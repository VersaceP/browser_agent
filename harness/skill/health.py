"""harness.skill.health — track skill run outcomes and auto-disable rotted skills.

A skill that keeps failing the fast path should stop firing (so it doesn't waste
attempts) and fall to the BrowserAgent slow path until a self-heal fixes it. This
tracks per-skill outcomes in a small JSON file and answers is_disabled() per the
skill's fallback.yaml `maintenance.disable_after_consecutive_failures`.

State file (default skills/.skill_health.json):
  { "<skill_id>": {"consecutive_failures": int, "total_runs": int,
                   "total_failures": int, "disabled": bool,
                   "last_outcome": "ok"|"fail", "updated": iso8601} }
"""
from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

DEFAULT_HEALTH_PATH = Path(__file__).resolve().parent.parent.parent / "skills" / ".skill_health.json"
DEFAULT_DISABLE_AFTER = 3


def _maintenance(skill: Any) -> Dict[str, Any]:
    fb = getattr(skill, "fallback", {}) or {}
    m = fb.get("maintenance") if isinstance(fb, dict) else None
    return m if isinstance(m, dict) else {}


class SkillHealth:
    def __init__(self, path: Path | str = DEFAULT_HEALTH_PATH):
        self.path = Path(path)
        self._lock = threading.Lock()
        self._state: Dict[str, Dict[str, Any]] = self._load()

    def _load(self) -> Dict[str, Dict[str, Any]]:
        if not self.path.is_file():
            return {}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (json.JSONDecodeError, OSError):
            return {}

    def _save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(self._state, indent=2, ensure_ascii=False) + "\n",
                                 encoding="utf-8")
        except OSError:
            pass  # health tracking is best-effort, never fatal

    def entry(self, skill_id: str) -> Dict[str, Any]:
        return dict(self._state.get(skill_id) or {})

    def is_disabled(self, skill: Any) -> bool:
        """A skill is disabled if explicitly disabled OR consecutive failures
        reached its maintenance threshold."""
        skill_id = getattr(skill, "skill_id", str(skill))
        entry = self._state.get(skill_id)
        if not entry:
            return False
        if entry.get("disabled"):
            return True
        threshold = int(_maintenance(skill).get("disable_after_consecutive_failures", DEFAULT_DISABLE_AFTER) or 0)
        return threshold > 0 and int(entry.get("consecutive_failures", 0)) >= threshold

    def record(self, skill: Any, ok: bool) -> Dict[str, Any]:
        skill_id = getattr(skill, "skill_id", str(skill))
        with self._lock:
            entry = self._state.setdefault(skill_id, {
                "consecutive_failures": 0, "total_runs": 0, "total_failures": 0,
                "disabled": False, "last_outcome": None, "updated": None,
            })
            entry["total_runs"] = int(entry.get("total_runs", 0)) + 1
            if ok:
                entry["consecutive_failures"] = 0
                entry["last_outcome"] = "ok"
            else:
                entry["consecutive_failures"] = int(entry.get("consecutive_failures", 0)) + 1
                entry["total_failures"] = int(entry.get("total_failures", 0)) + 1
                entry["last_outcome"] = "fail"
                threshold = int(_maintenance(skill).get("disable_after_consecutive_failures", DEFAULT_DISABLE_AFTER) or 0)
                if threshold > 0 and entry["consecutive_failures"] >= threshold:
                    entry["disabled"] = True
            entry["updated"] = datetime.now(timezone.utc).isoformat()
            self._save()
            return dict(entry)

    def reset(self, skill_id: str) -> None:
        """Clear a skill's failure state (e.g. after a successful self-heal promote)."""
        with self._lock:
            if skill_id in self._state:
                self._state[skill_id].update(
                    {"consecutive_failures": 0, "disabled": False, "last_outcome": "ok",
                     "updated": datetime.now(timezone.utc).isoformat()})
                self._save()


_singleton: Optional[SkillHealth] = None


def default_health() -> SkillHealth:
    global _singleton
    if _singleton is None:
        _singleton = SkillHealth()
    return _singleton
