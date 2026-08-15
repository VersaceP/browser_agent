"""
harness.version - The released harness version, recorded on every task run.

Bump HARNESS_VERSION by hand before publishing a release. It lives alone in
this file so a release edit cannot accidentally touch an unrelated constant.

``git_sha()`` is the safety net for the release that forgot to bump: it costs
nothing and answers "which code actually produced this row" when the hand
written version is stale.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Dict


HARNESS_VERSION = "1.0.0"

# Successful answers only. lru_cache would also memoise a failure, and one
# `git` call that times out under load would then blank the provenance of
# every run in the process - the field is cheap to retry and useless to lose.
_GIT_CACHE: Dict[str, object] = {}


def git_sha() -> str:
    """Short commit hash of the working tree, or "" when unavailable."""

    if "sha" in _GIT_CACHE:
        return str(_GIT_CACHE["sha"])
    try:
        completed = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=str(Path(__file__).resolve().parent.parent),
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    if completed.returncode != 0:
        return ""
    sha = completed.stdout.strip()
    if sha:
        _GIT_CACHE["sha"] = sha
    return sha


def git_is_dirty() -> bool:
    """True when tracked files differ from HEAD.

    A sha alone is misleading then: the code that ran is not the code at that
    commit, which is exactly the situation where provenance matters most.
    """

    if "dirty" in _GIT_CACHE:
        return bool(_GIT_CACHE["dirty"])
    try:
        completed = subprocess.run(
            ["git", "status", "--porcelain", "--untracked-files=no"],
            cwd=str(Path(__file__).resolve().parent.parent),
            capture_output=True, text=True, timeout=5, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    if completed.returncode != 0:
        return False
    dirty = bool(completed.stdout.strip())
    _GIT_CACHE["dirty"] = dirty
    return dirty


def git_revision() -> str:
    """The sha, marked when the working tree carries uncommitted changes."""

    sha = git_sha()
    if not sha:
        return ""
    return f"{sha}-dirty" if git_is_dirty() else sha


def version_info() -> dict:
    return {
        "harnessVersion": HARNESS_VERSION,
        "gitSha": git_sha(),
        "gitDirty": git_is_dirty(),
    }
