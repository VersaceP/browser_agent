#!/usr/bin/env python3
"""Start a fresh production run from a historical task's original wording.

This is a thin wrapper around ``main.py``: it reads the ``<user_task>`` text out
of a past run's Lead context and starts a new run from it, so a regression can
be driven by exactly the request that produced the run being compared against.
It changes no harness behavior and adds no runtime or configuration switch.

It used to carry an A/B shadow-bypass variant, where the B arm let the progress
and duplicate-call detectors count while suppressing their enforcement. The
old per-tool gates are retired: progress is observation-only, and duplicate
calls are observed until the one global 20-call spend limit stops the 21st
byte-identical request. The former A/B arms therefore no longer model the
current harness. Baseline reports produced before that change describe a
harness that enforced those gates; read them against that version, not this one.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import sys
from typing import List, Optional


REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import main as harness_main  # noqa: E402


def historical_task(run_id: str) -> str:
    """Read only the original user task from a historical Lead context."""
    normalized = str(run_id or "").strip()
    if not re.fullmatch(r"[0-9a-f]{32}", normalized):
        raise ValueError("historical task id must be a 32-character lowercase hex id")
    context_path = (
        REPO_ROOT / "worktree" / normalized / "contexts" / "lead_agent-final-context.json"
    )
    payload = json.loads(context_path.read_text(encoding="utf-8"))
    messages = payload.get("messages") if isinstance(payload.get("messages"), list) else []
    first = messages[0].get("content") if messages and isinstance(messages[0], dict) else ""
    text = str(first or "")
    match = re.search(r"<user_task>\s*(.*?)\s*</user_task>", text, re.S)
    if not match or not match.group(1).strip():
        raise ValueError(f"historical user task not found in {context_path}")
    return match.group(1).strip()


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Replay a historical task's original wording through main.py"
    )
    parser.add_argument(
        "--historical-task-id",
        default="",
        help="Start a fresh run using only the original task text from this worktree id",
    )
    parser.add_argument("harness_args", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    forwarded = list(args.harness_args)
    if forwarded[:1] == ["--"]:
        forwarded = forwarded[1:]
    if args.historical_task_id:
        if any(item == "--resume" or item.startswith("--resume=") for item in forwarded):
            parser.error("--historical-task-id cannot be combined with --resume")
        forwarded.extend(["--task", historical_task(args.historical_task_id)])
    return harness_main.main(forwarded)


if __name__ == "__main__":
    raise SystemExit(main())
