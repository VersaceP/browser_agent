"""
harness.version - The released harness version, recorded on every task run.

Bump HARNESS_VERSION by hand before publishing a release. It lives alone in
this file so a release edit cannot accidentally touch an unrelated constant.

``git_sha()`` is the safety net for the release that forgot to bump: it costs
nothing and answers "which code actually produced this row" when the hand
written version is stale.
"""

from __future__ import annotations

import hashlib
import os
import subprocess
from pathlib import Path
from typing import Dict


HARNESS_VERSION = "1.0.0"

# Successful answers only. lru_cache would also memoise a failure, and one
# `git` call that times out under load would then blank the provenance of
# every run in the process - the field is cheap to retry and useless to lose.
_GIT_CACHE: Dict[str, object] = {}
_REPO_ROOT = Path(__file__).resolve().parent.parent


def _git_output(*args: str, text: bool = True, timeout: int = 5):
    """Run one bounded, read-only git query against this checkout."""

    return subprocess.run(
        ["git", *args],
        cwd=str(_REPO_ROOT),
        capture_output=True,
        text=text,
        timeout=timeout,
        check=False,
    )


def git_sha() -> str:
    """Short commit hash of the working tree, or "" when unavailable."""

    if "sha" in _GIT_CACHE:
        return str(_GIT_CACHE["sha"])
    try:
        completed = _git_output("rev-parse", "--short", "HEAD")
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
        completed = _git_output("status", "--porcelain", "--untracked-files=all")
    except (OSError, subprocess.SubprocessError):
        return False
    if completed.returncode != 0:
        return False
    dirty = bool(completed.stdout.strip())
    _GIT_CACHE["dirty"] = dirty
    return dirty


def git_branch() -> str:
    """Current branch name, or ``HEAD`` for a detached checkout."""

    if "branch" in _GIT_CACHE:
        return str(_GIT_CACHE["branch"])
    try:
        completed = _git_output("branch", "--show-current")
    except (OSError, subprocess.SubprocessError):
        return ""
    if completed.returncode != 0:
        return ""
    branch = completed.stdout.strip() or "HEAD"
    _GIT_CACHE["branch"] = branch
    return branch


def git_worktree_sha256() -> str:
    """Hash the tracked diff and every non-ignored untracked file.

    A ``<sha>-dirty`` label proves only that HEAD was not the executed source.
    This digest makes two dirty runs comparable without persisting source code
    or potentially sensitive diff text into run logs.
    """

    if "worktree_sha256" in _GIT_CACHE:
        return str(_GIT_CACHE["worktree_sha256"])
    if not git_is_dirty():
        _GIT_CACHE["worktree_sha256"] = ""
        return ""
    try:
        diff = _git_output(
            "diff", "--binary", "--no-ext-diff", "HEAD", text=False, timeout=30
        )
        untracked = _git_output(
            "ls-files", "--others", "--exclude-standard", "-z", text=False,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    if diff.returncode != 0 or untracked.returncode != 0:
        return ""
    digest = hashlib.sha256()
    digest.update(diff.stdout)
    for raw_path in sorted(item for item in untracked.stdout.split(b"\0") if item):
        digest.update(b"\0untracked\0")
        digest.update(raw_path)
        path = _REPO_ROOT / os.fsdecode(raw_path)
        try:
            digest.update(b"\0")
            if path.is_symlink():
                digest.update(b"symlink\0")
                digest.update(os.readlink(path).encode("utf-8", "surrogateescape"))
            else:
                digest.update(path.read_bytes())
        except OSError:
            digest.update(b"\0<unreadable>")
    value = digest.hexdigest()
    _GIT_CACHE["worktree_sha256"] = value
    return value


def git_revision() -> str:
    """The sha, marked when the working tree carries uncommitted changes."""

    sha = git_sha()
    if not sha:
        return ""
    return f"{sha}-dirty" if git_is_dirty() else sha


def git_source_revision(worktree_sha256: object = None) -> str:
    """Comparable source identity while preserving ``git_revision`` format."""

    revision = git_revision()
    if not revision or not git_is_dirty():
        return revision
    worktree = (
        git_worktree_sha256()
        if worktree_sha256 is None
        else str(worktree_sha256 or "")
    )
    return f"{revision}.{worktree[:12]}" if worktree else revision


def version_info() -> dict:
    worktree = git_worktree_sha256()
    return {
        "harnessVersion": HARNESS_VERSION,
        "gitSha": git_sha(),
        "gitDirty": git_is_dirty(),
        "gitBranch": git_branch(),
        "gitWorktreeSha256": worktree,
        "gitRevision": git_revision(),
        "gitSourceRevision": git_source_revision(worktree),
    }
