"""
harness.diagnostics.selector_audit - Post-run audit of element targeting.

For every method that takes an element handle (Input.click / Input.type /
Input.drag / DOM.getText / DOM.getAttribute), classify what the model
actually passed (axtree_id, css selector, fake jQuery-style pseudo, or
an "other_string" the model invented) and pair each call with its outcome.

Also reports the find-chain cost: how often a targeted call was preceded
by DOM.getAXTree / local_fs_search / local_fs_read in
the prior few tool calls, and the median number of model steps spent
between a DOM.getAXTree and the next call that consumed an element handle.

Output answers three questions:
    1. Which addressing style is the model actually choosing?
    2. Which style fails most often?
    3. How expensive is the "find" half of find-then-act?

Run as:
    python -m harness.diagnostics.selector_audit
    python -m harness.diagnostics.selector_audit /path/to/worktree
    python -m harness.diagnostics.selector_audit --task <task_id>
    python -m harness.diagnostics.selector_audit --json

Programmatic use:
    from harness.diagnostics.selector_audit import audit_worktree, audit_run
"""

from __future__ import annotations

import argparse
import json
import re
import statistics
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple


ELEMENT_TARGETED_METHODS = frozenset({
    "Input.click",
    "Input.press",
    "Input.select",
    "Input.type",
    "Input.drag",
    "DOM.inspectSelect",
    "DOM.getText",
    "DOM.getAttribute",
})
INTERACTION_METHODS = frozenset({
    "Input.click", "Input.select", "Input.type", "Input.press",
})
FIND_TOOLS = frozenset({"local_fs_search", "local_fs_read"})
AXTREE_METHODS = frozenset({"DOM.getAXTree", "DOM.getSemanticTree"})
RENDER_RECOVERY_EVENT_TYPES = frozenset({"browser.call.recovery_result"})

AXTREE_ID_RE = re.compile(r"^\d+:-?\d+:-?\d+$")
FAKE_PSEUDO_RE = re.compile(r":(text|contains|has)\b")
XPATH_RE = re.compile(r"\bxpath\b", re.I)
CSS_TAG_RE = re.compile(r"^([a-z][a-z0-9-]*)(?:[#.:\[\s>+~]|$)")

HTML_TAGS = frozenset({
    "a", "article", "aside", "button", "canvas", "div", "footer", "form",
    "h1", "h2", "h3", "h4", "h5", "h6", "header", "iframe", "img",
    "input", "label", "li", "main", "nav", "ol", "option", "p", "section",
    "select", "span", "table", "tbody", "td", "textarea", "th", "thead",
    "tr", "ul",
})

TARGET_PARAM_KEYS = ("id", "nodeId", "target", "selector", "elementId")

# Observation substrings that mean "we tried but the element wasn't found /
# the call failed". Matched case-insensitively against the observation field.
FAILURE_MARKERS = (
    "could not", "no such", "not found", "failed to", "is not visible",
    "is detached", "stale", "invalid node", "blank page",
    "element not found", "no element matched", "selector did not match",
)

# Window used for the "find-chain" check: how many tool_calls before each
# targeted call we look at when counting find/getAXTree occurrences.
FIND_CHAIN_LOOKBACK = 3


def classify_selector(params: Any) -> str:
    """Classify the element-handle string the model passed.

    Returns one of:
      axtree_id      — matches the ABCP "<frameId>:<backendId>:<x>" format
      fake_pseudo    — contains :text() / :contains() / :has() (not valid CSS)
      css            — starts like a CSS selector (#, ., [, or tag>...)
      xpath          — looks like an XPath
      other_string   — some other string (often invented free text or a guess)
      no_target_arg  — no element-handle param at all (e.g. Input.scroll)
    """
    if not isinstance(params, dict):
        return "no_target_arg"
    for key in TARGET_PARAM_KEYS:
        value = params.get(key)
        if not isinstance(value, str):
            continue
        s = value.strip()
        if not s:
            continue
        if AXTREE_ID_RE.match(s):
            return "axtree_id"
        if FAKE_PSEUDO_RE.search(s):
            return "fake_pseudo"
        if XPATH_RE.search(s) or s.startswith("/"):
            return "xpath"
        if _looks_like_css_selector(s):
            return "css"
        return "other_string"
    return "no_target_arg"


def _looks_like_css_selector(text: str) -> bool:
    """Heuristic, not a parser: classify likely CSS without treating labels as CSS."""
    s = text.strip()
    if not s:
        return False
    if "," in s:
        parts = [part.strip() for part in s.split(",") if part.strip()]
        return bool(parts) and all(_looks_like_css_selector(part) for part in parts)
    if s[0] in "#.[":
        return True
    if any(token in s for token in (">", "+", "~")):
        return bool(CSS_TAG_RE.match(s) or s[0] in "#.[")
    if "[" in s and "]" in s:
        return bool(CSS_TAG_RE.match(s) or s[0] in "#.[")
    if "." in s or "#" in s or ":" in s:
        return bool(CSS_TAG_RE.match(s) or s[0] in "#.[")
    if re.search(r"\s", s):
        parts = [part for part in re.split(r"\s+", s) if part]
        return bool(parts) and all(_looks_like_css_selector(part) for part in parts)

    match = CSS_TAG_RE.match(s)
    if not match:
        return False
    tag = match.group(1).lower()
    return tag in HTML_TAGS or "-" in tag


# ---------- Data model ----------------------------------------------------

@dataclass
class FailureExample:
    method: str
    selector_kind: str
    observation: Optional[str]
    chain: List[Dict[str, Optional[str]]] = field(default_factory=list)


@dataclass
class RunReport:
    task_id: str
    click_total: int = 0
    click_ok: int = 0
    click_fail: int = 0
    render_recovery_events: int = 0
    # selector kind distribution per method
    selector_kind_per_method: Dict[str, Dict[str, int]] = field(default_factory=dict)
    # (method, kind) -> (ok, fail)
    outcome_per_kind: Dict[str, Dict[str, Tuple[int, int]]] = field(default_factory=dict)
    # method -> {hits_in_prior_3: count}
    find_chain_distribution: Dict[str, Dict[int, int]] = field(default_factory=dict)
    # samples of steps and find-tool counts between a getAXTree and the next
    # targeted call (within the same agent's tool_call sequence).
    steps_between_axtree_and_target: List[int] = field(default_factory=list)
    finds_between_axtree_and_target: List[int] = field(default_factory=list)
    failure_examples: List[FailureExample] = field(default_factory=list)


@dataclass
class AggregateReport:
    runs: List[RunReport]

    def fold_selector_kind(self) -> Dict[str, Counter]:
        out: Dict[str, Counter] = defaultdict(Counter)
        for r in self.runs:
            for method, dist in r.selector_kind_per_method.items():
                for kind, n in dist.items():
                    out[method][kind] += n
        return dict(out)

    def fold_outcome(self) -> Dict[str, Dict[str, Tuple[int, int]]]:
        out: Dict[str, Dict[str, List[int]]] = defaultdict(lambda: defaultdict(lambda: [0, 0]))
        for r in self.runs:
            for method, per_kind in r.outcome_per_kind.items():
                for kind, (ok, fail) in per_kind.items():
                    out[method][kind][0] += ok
                    out[method][kind][1] += fail
        return {m: {k: (v[0], v[1]) for k, v in d.items()} for m, d in out.items()}

    def fold_find_chain(self) -> Dict[str, Counter]:
        out: Dict[str, Counter] = defaultdict(Counter)
        for r in self.runs:
            for method, dist in r.find_chain_distribution.items():
                for hits, n in dist.items():
                    out[method][hits] += n
        return dict(out)

    def all_steps_between(self) -> List[int]:
        return [v for r in self.runs for v in r.steps_between_axtree_and_target]

    def all_finds_between(self) -> List[int]:
        return [v for r in self.runs for v in r.finds_between_axtree_and_target]


# ---------- Core walk -----------------------------------------------------

def _coerce_dict(value: Any) -> Dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _iter_events(run_jsonl_path: Path) -> Iterable[Dict[str, Any]]:
    with run_jsonl_path.open() as fh:
        for line in fh:
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


def _observation_failed(observation: Optional[str]) -> bool:
    if not isinstance(observation, str):
        return False
    lowered = observation.lower()
    return any(marker in lowered for marker in FAILURE_MARKERS)


def audit_run(run_jsonl_path: Path) -> RunReport:
    """Walk a single run.jsonl and emit a RunReport."""
    task_id = run_jsonl_path.parent.name
    report = RunReport(task_id=task_id)

    # Flat tool-call sequence used to look "back N steps" from a result event.
    tool_calls: List[Dict[str, Any]] = []
    last_axtree_tc_idx: Optional[int] = None
    finds_since_last_axtree = 0

    # Per-method per-kind accumulators
    sel_kinds: Dict[str, Counter] = defaultdict(Counter)
    outcomes: Dict[str, Dict[str, List[int]]] = defaultdict(
        lambda: defaultdict(lambda: [0, 0])
    )
    chain_dist: Dict[str, Counter] = defaultdict(Counter)

    for event in _iter_events(run_jsonl_path):
        event_type = event.get("type", "")
        payload = _coerce_dict(event.get("payload"))

        if event_type in ("agent.model", "lead.model"):
            for tc in payload.get("tool_calls") or []:
                if not isinstance(tc, dict):
                    continue
                tool = tc.get("name")
                tc_input = tc.get("input") if isinstance(tc.get("input"), dict) else {}
                method = tc_input.get("method") if tool == "browser_call" else None
                params = tc_input.get("params") if isinstance(tc_input.get("params"), dict) else {}
                tool_calls.append({"tool": tool, "method": method, "params": params})

                if method in AXTREE_METHODS:
                    last_axtree_tc_idx = len(tool_calls)
                    finds_since_last_axtree = 0
                elif tool in FIND_TOOLS:
                    finds_since_last_axtree += 1

                if method in ELEMENT_TARGETED_METHODS:
                    kind = classify_selector(params)
                    sel_kinds[method][kind] += 1

                    # Count find/getAXTree hits in the prior FIND_CHAIN_LOOKBACK
                    # tool_calls (excluding the targeted call itself).
                    prior = tool_calls[-1 - FIND_CHAIN_LOOKBACK : -1]
                    hits = sum(
                        1 for prev in prior
                        if prev["tool"] in FIND_TOOLS or prev["method"] in AXTREE_METHODS
                    )
                    chain_dist[method][hits] += 1

                    if last_axtree_tc_idx is not None:
                        steps = len(tool_calls) - last_axtree_tc_idx
                        report.steps_between_axtree_and_target.append(steps)
                        report.finds_between_axtree_and_target.append(finds_since_last_axtree)

        elif event_type == "browser.call.result":
            method = payload.get("method")
            response = _coerce_dict(payload.get("response"))
            error = response.get("error") or response.get("err") or response.get("rpc_error")
            observation = response.get("observation")
            ok = error is None and not _observation_failed(observation)

            if method in INTERACTION_METHODS:
                report.click_total += 1
                if ok:
                    report.click_ok += 1
                else:
                    report.click_fail += 1

            if method in ELEMENT_TARGETED_METHODS:
                params = payload.get("params") if isinstance(payload.get("params"), dict) else {}
                kind = classify_selector(params)
                chain_view: List[Dict[str, Optional[str]]] = []
                for prev in reversed(tool_calls):
                    chain_view.append({"tool": prev["tool"], "method": prev["method"]})
                    if len(chain_view) >= FIND_CHAIN_LOOKBACK + 1:
                        break

                if ok:
                    outcomes[method][kind][0] += 1
                else:
                    outcomes[method][kind][1] += 1
                    if len(report.failure_examples) < 5:
                        obs_str = (
                            observation[:240] if isinstance(observation, str) else None
                        )
                        report.failure_examples.append(FailureExample(
                            method=method,
                            selector_kind=kind,
                            observation=obs_str,
                            chain=list(reversed(chain_view)),
                        ))

        elif event_type in RENDER_RECOVERY_EVENT_TYPES or "render_recovery" in event_type:
            report.render_recovery_events += 1

    report.selector_kind_per_method = {m: dict(c) for m, c in sel_kinds.items()}
    report.outcome_per_kind = {
        m: {k: (v[0], v[1]) for k, v in d.items()}
        for m, d in outcomes.items()
    }
    report.find_chain_distribution = {m: dict(c) for m, c in chain_dist.items()}
    return report


def audit_worktree(
    worktree_dir: Path,
    *,
    task_filter: Optional[str] = None,
) -> AggregateReport:
    runs: List[RunReport] = []
    for run_jsonl in sorted(worktree_dir.glob("*/run.jsonl")):
        task_id = run_jsonl.parent.name
        if task_filter and task_filter not in task_id:
            continue
        runs.append(audit_run(run_jsonl))
    return AggregateReport(runs=runs)


# ---------- Rendering ----------------------------------------------------

def _fmt_percent(num: int, denom: int) -> str:
    return f"{num/denom:.1%}" if denom else "—"


def format_text(report: AggregateReport) -> str:
    lines: List[str] = []
    lines.append("=== Per-task ===")
    for r in report.runs:
        lines.append(
            f"\n[{r.task_id[:8]}]  Input.click/type/press: {r.click_total}  "
            "(Input.drag tracked in selector tables)  "
            f"ok:{r.click_ok}  fail:{r.click_fail}  render_rec:{r.render_recovery_events}"
        )
        for ex in r.failure_examples[:3]:
            lines.append(f"    FAIL {ex.method} (kind={ex.selector_kind})")
            lines.append(f"      obs: {ex.observation}")
            lines.append(f"      chain: {ex.chain}")

    sel = report.fold_selector_kind()
    lines.append("\n=== Selector-kind distribution per method ===")
    for method, counter in sel.items():
        total = sum(counter.values())
        lines.append(f"\n  {method}  (total {total})")
        for kind, n in counter.most_common():
            lines.append(f"    {kind:15s}: {n:4d}  ({_fmt_percent(n, total)})")

    outcomes = report.fold_outcome()
    lines.append("\n=== Outcome per (method, selector-kind) ===")
    for method, per_kind in outcomes.items():
        lines.append(f"\n  {method}")
        for kind, (ok, fail) in per_kind.items():
            total = ok + fail
            lines.append(
                f"    {kind:15s}: ok={ok:4d}  fail={fail:4d}  "
                f"({_fmt_percent(fail, total)} fail)"
            )

    chains = report.fold_find_chain()
    lines.append(
        f"\n=== Find-chain occurrence in prior {FIND_CHAIN_LOOKBACK} tool_calls ==="
    )
    for method, counter in chains.items():
        total = sum(counter.values())
        any_hit = sum(v for k, v in counter.items() if k >= 1)
        lines.append(
            f"  {method:20s}  any-hit: {any_hit}/{total} "
            f"({_fmt_percent(any_hit, total)})   dist: {dict(counter)}"
        )

    steps = report.all_steps_between()
    finds = report.all_finds_between()
    lines.append("\n=== getAXTree → next targeted call ===")
    if steps:
        lines.append(
            f"  samples: {len(steps)}    "
            f"steps  median={statistics.median(steps):.0f}  "
            f"mean={statistics.mean(steps):.1f}  max={max(steps)}"
        )
        lines.append(
            f"                 "
            f"finds  median={statistics.median(finds):.0f}  "
            f"mean={statistics.mean(finds):.1f}  max={max(finds)}"
        )
    else:
        lines.append("  no targeted calls recorded")
    return "\n".join(lines)


def to_json(report: AggregateReport) -> Dict[str, Any]:
    return {
        "runs": [
            {
                "task_id": r.task_id,
                "click_total": r.click_total,
                "click_ok": r.click_ok,
                "click_fail": r.click_fail,
                "render_recovery_events": r.render_recovery_events,
                "selector_kind_per_method": r.selector_kind_per_method,
                "outcome_per_kind": {
                    m: {k: list(v) for k, v in d.items()}
                    for m, d in r.outcome_per_kind.items()
                },
                "find_chain_distribution": r.find_chain_distribution,
                "steps_between_axtree_and_target": r.steps_between_axtree_and_target,
                "finds_between_axtree_and_target": r.finds_between_axtree_and_target,
                "failure_examples": [
                    {
                        "method": ex.method,
                        "selector_kind": ex.selector_kind,
                        "observation": ex.observation,
                        "chain": ex.chain,
                    }
                    for ex in r.failure_examples
                ],
            }
            for r in report.runs
        ],
        "aggregate": {
            "selector_kind_per_method": {
                m: dict(c) for m, c in report.fold_selector_kind().items()
            },
            "outcome_per_kind": {
                m: {k: list(v) for k, v in d.items()}
                for m, d in report.fold_outcome().items()
            },
            "find_chain_distribution": {
                m: dict(c) for m, c in report.fold_find_chain().items()
            },
            "steps_between_axtree_and_target_samples": report.all_steps_between(),
            "finds_between_axtree_and_target_samples": report.all_finds_between(),
        },
    }


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m harness.diagnostics.selector_audit",
        description="Post-run selector-targeting audit",
    )
    parser.add_argument(
        "worktree_dir",
        nargs="?",
        default="worktree",
        help="path to worktree directory (default: ./worktree)",
    )
    parser.add_argument(
        "--task",
        dest="task_filter",
        default=None,
        help="substring match on task id; only audit matching tasks",
    )
    parser.add_argument(
        "--json",
        dest="emit_json",
        action="store_true",
        help="emit machine-readable JSON instead of text",
    )
    args = parser.parse_args(argv)

    worktree = Path(args.worktree_dir)
    if not worktree.is_dir():
        print(f"worktree not found: {worktree}", file=sys.stderr)
        return 2

    report = audit_worktree(worktree, task_filter=args.task_filter)
    if not report.runs:
        msg = f"no run.jsonl found under {worktree}"
        if args.task_filter:
            msg += f" matching --task {args.task_filter!r}"
        print(msg, file=sys.stderr)
        return 1

    if args.emit_json:
        json.dump(to_json(report), sys.stdout, ensure_ascii=False, indent=2)
        sys.stdout.write("\n")
    else:
        print(format_text(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
