#!/usr/bin/env python3
"""devtools.event_catalog - Generate docs/event-catalog.md from the source tree.

The catalog is mechanically derived, never hand-edited: a hand-written
inventory of 350+ call sites is stale the day after it is written. Run with
``--check`` in review to fail on drift.

Three producer families are extracted by AST, not regex, so a call spanning
several lines or nested in a comprehension is still seen:

- ``<anything named *logger*>.write("event.type", ...)``   -> run event
- ``self._write_agent_event("event.type", ...)``           -> run event
- ``<anything named *trace*>.append({"type": "...", ...})`` -> trace entry

Consumers are located by a second pass over the same files looking for the
read side (``read_events``, ``.trace``, ``worker_trace_events``, ``on_event``),
because the point of the catalog is to prove which producer has a reader.
"""

from __future__ import annotations

import argparse
import ast
import subprocess
import sys
from collections import defaultdict
from pathlib import Path
from typing import Dict, List, NamedTuple, Optional, Set, Tuple

REPO_ROOT = Path(__file__).resolve().parent.parent
OUTPUT_PATH = REPO_ROOT / "docs" / "event-catalog.md"

# Emitted verbatim so a reader of the file knows not to edit it by hand.
HEADER = """<!-- GENERATED FILE - do not edit by hand.
     Regenerate: python3 devtools/event_catalog.py
     Verify:     python3 devtools/event_catalog.py --check -->

# 事件与 trace 目录（自动生成）

本文件由 `devtools/event_catalog.py` 从源码 AST 提取，用于重构前证明"哪些事件
真的被产生、哪些真的被消费"。手工维护的清单会在第二天过期，所以这里只放机械
可判定的事实；语义判断留在实施计划里。
"""


class Producer(NamedTuple):
    event_type: str
    path: str
    line: int
    kind: str          # "run_event" | "trace"
    dynamic: bool      # event type is not a plain literal


def tracked_python_files() -> List[Path]:
    """Git-tracked .py files only.

    A bare filesystem walk pulls in venv/, abcp-platform/ and the ignored
    tests/ tree, which would make the counts meaningless.
    """
    out = subprocess.run(
        ["git", "ls-files", "-z", "*.py"],
        cwd=REPO_ROOT, capture_output=True, text=True, check=True,
    ).stdout
    # Skip this file. Its own aggregation loops read `.event_type` off local
    # variables, which the producer scan below reads as an event named `item`.
    # A catalog of the catalog generator is never the intent.
    self_path = Path(__file__).resolve()
    return [
        path for name in out.split("\0") if name
        for path in [REPO_ROOT / name]
        if path.resolve() != self_path
    ]


def _receiver_name(node: ast.AST) -> str:
    try:
        return ast.unparse(node)
    except Exception:  # pragma: no cover - unparse handles every real node
        return ""


def _literal(node: Optional[ast.AST]) -> Tuple[str, bool]:
    """Return (event_type, dynamic). Dynamic types are kept, not dropped:
    an f-string event name is exactly the kind of thing a typed event system
    has to account for."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value, False
    if isinstance(node, ast.JoinedStr):
        parts: List[str] = []
        for value in node.values:
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                parts.append(value.value)
            else:
                parts.append("{}")
        return "".join(parts), True
    if node is None:
        return "", True
    return _receiver_name(node), True


def _trace_type(node: Optional[ast.AST]) -> Tuple[str, bool]:
    """Read {"type": "..."} out of a trace.append() dict argument."""
    if not isinstance(node, ast.Dict):
        return _receiver_name(node) if node is not None else "", True
    for key, value in zip(node.keys, node.values):
        if isinstance(key, ast.Constant) and key.value == "type":
            return _literal(value)
    return "(no type key)", True


def collect_producers(paths: List[Path]) -> List[Producer]:
    producers: List[Producer] = []
    for path in paths:
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError):
            continue
        rel = str(path.relative_to(REPO_ROOT))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            first = node.args[0] if node.args else None

            if isinstance(func, ast.Attribute) and func.attr == "write":
                receiver = _receiver_name(func.value).lower()
                if "logger" not in receiver:
                    continue
                event_type, dynamic = _literal(first)
                producers.append(
                    Producer(event_type, rel, node.lineno, "run_event", dynamic)
                )
            elif (
                isinstance(func, ast.Attribute) and func.attr == "_write_agent_event"
            ) or (
                isinstance(func, ast.Name) and func.id == "_write_agent_event"
            ):
                event_type, dynamic = _literal(first)
                producers.append(
                    Producer(event_type, rel, node.lineno, "run_event", dynamic)
                )
            elif isinstance(func, ast.Attribute) and func.attr == "append":
                receiver = _receiver_name(func.value).lower()
                if "trace" not in receiver:
                    continue
                event_type, dynamic = _trace_type(first)
                producers.append(
                    Producer(event_type, rel, node.lineno, "trace", dynamic)
                )
    return producers


CONSUMER_PATTERNS: Dict[str, str] = {
    "read_events(": "读取持久化 run event",
    ".trace": "读取 agent.trace 列表",
    "worker_trace_events": "读取 worker trace 表",
    "on_event": "事件回调（Console/transport）",
    "iter_events(": "遍历事件流",
}


def collect_consumers(paths: List[Path]) -> Dict[str, List[Tuple[str, int, str]]]:
    found: Dict[str, List[Tuple[str, int, str]]] = defaultdict(list)
    for path in paths:
        rel = str(path.relative_to(REPO_ROOT))
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except UnicodeDecodeError:
            continue
        for index, line in enumerate(lines, start=1):
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            for pattern in CONSUMER_PATTERNS:
                if pattern not in line:
                    continue
                # Definition sites are not consumers.
                if stripped.startswith(("def ", "async def ", "class ")):
                    continue
                found[pattern].append((rel, index, stripped[:110]))
    return found


def render(producers: List[Producer], consumers) -> str:
    run_events = [item for item in producers if item.kind == "run_event"]
    traces = [item for item in producers if item.kind == "trace"]

    by_type: Dict[str, List[Producer]] = defaultdict(list)
    for item in run_events:
        by_type[item.event_type].append(item)
    by_file: Dict[str, int] = defaultdict(int)
    for item in run_events:
        by_file[item.path] += 1

    trace_by_type: Dict[str, List[Producer]] = defaultdict(list)
    for item in traces:
        trace_by_type[item.event_type].append(item)

    prefixes: Dict[str, int] = defaultdict(int)
    for item in run_events:
        prefixes[item.event_type.split(".", 1)[0] or "(dynamic)"] += 1

    lines: List[str] = [HEADER, ""]
    lines.append("## 1. 汇总")
    lines.append("")
    lines.append("| 指标 | 数量 |")
    lines.append("|---|---:|")
    lines.append(f"| run event 产生点 | {len(run_events)} |")
    lines.append(f"| 不同 run event 类型 | {len(by_type)} |")
    lines.append(f"| 动态（非字面量）事件名 | {sum(1 for i in run_events if i.dynamic)} |")
    lines.append(f"| trace 产生点 | {len(traces)} |")
    lines.append(f"| 不同 trace type | {len(trace_by_type)} |")
    lines.append("")

    lines.append("## 2. 事件命名空间分布")
    lines.append("")
    lines.append("| 前缀 | 产生点 |")
    lines.append("|---|---:|")
    for prefix, count in sorted(prefixes.items(), key=lambda kv: (-kv[1], kv[0])):
        lines.append(f"| `{prefix}` | {count} |")
    lines.append("")

    lines.append("## 3. 产生点最多的文件")
    lines.append("")
    lines.append("| 文件 | run event 产生点 |")
    lines.append("|---|---:|")
    for path, count in sorted(by_file.items(), key=lambda kv: (-kv[1], kv[0]))[:20]:
        lines.append(f"| `{path}` | {count} |")
    lines.append("")

    lines.append("## 4. run event 类型全表")
    lines.append("")
    lines.append("| 事件类型 | 产生点 | 位置 |")
    lines.append("|---|---:|---|")
    for event_type, items in sorted(by_type.items()):
        where = ", ".join(
            f"`{item.path}:{item.line}`" for item in sorted(items)[:4]
        )
        if len(items) > 4:
            where += f" 等 {len(items)} 处"
        label = f"`{event_type}`" if event_type else "*(空)*"
        if items[0].dynamic:
            label += " ⚠动态"
        lines.append(f"| {label} | {len(items)} | {where} |")
    lines.append("")

    lines.append("## 5. trace type 全表")
    lines.append("")
    lines.append("| trace type | 产生点 | 位置 |")
    lines.append("|---|---:|---|")
    for event_type, items in sorted(trace_by_type.items()):
        where = ", ".join(
            f"`{item.path}:{item.line}`" for item in sorted(items)[:4]
        )
        if len(items) > 4:
            where += f" 等 {len(items)} 处"
        lines.append(f"| `{event_type}` | {len(items)} | {where} |")
    lines.append("")

    lines.append("## 6. 消费端")
    lines.append("")
    lines.append(
        "机械匹配，可能含误报；用于证明某条产生链是否真的有读者。"
    )
    lines.append("")
    for pattern, description in CONSUMER_PATTERNS.items():
        hits = consumers.get(pattern, [])
        lines.append(f"### `{pattern}` — {description}（{len(hits)} 处）")
        lines.append("")
        if not hits:
            lines.append("*无*")
            lines.append("")
            continue
        lines.append("| 位置 | 代码 |")
        lines.append("|---|---|")
        for rel, line_no, text in hits[:40]:
            escaped = text.replace("|", "\\|")
            lines.append(f"| `{rel}:{line_no}` | `{escaped}` |")
        if len(hits) > 40:
            lines.append(f"| … | 另有 {len(hits) - 40} 处 |")
        lines.append("")

    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check", action="store_true",
        help="fail if the checked-in catalog differs from a fresh render",
    )
    args = parser.parse_args()

    paths = tracked_python_files()
    rendered = render(collect_producers(paths), collect_consumers(paths))

    if args.check:
        current = OUTPUT_PATH.read_text(encoding="utf-8") if OUTPUT_PATH.exists() else ""
        if current != rendered:
            print(
                f"event catalog is stale: regenerate with "
                f"`python3 {Path(__file__).relative_to(REPO_ROOT)}`",
                file=sys.stderr,
            )
            return 1
        print("event catalog up to date")
        return 0

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(rendered, encoding="utf-8")
    print(f"wrote {OUTPUT_PATH.relative_to(REPO_ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
