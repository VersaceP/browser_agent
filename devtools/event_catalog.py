#!/usr/bin/env python3
"""devtools.event_catalog - Generate docs/event-catalog.md from the source tree.

The catalog is mechanically derived, never hand-edited: a hand-written
inventory of 350+ call sites is stale the day after it is written. Run with
``--check`` in review to fail on drift.

Two directions are kept strictly apart, because conflating them is how an
event inventory turns into fiction:

OUTBOUND - events this harness writes itself. Four producer families are
extracted by AST, not regex, so a call spanning several lines or nested in a
comprehension is still seen:

- ``<anything named *logger*>.write("event.type", ...)``    -> run event
- ``self._write_agent_event("event.type", ...)``            -> run event
- ``_log(<logger>, "event.type", ...)``                     -> run event
  (seven modules wrap ``logger.write`` in a private ``_log`` helper; without
  this family ~66 event types, incl. the whole Layer-0 platform-event bridge,
  would be invisible)
- ``<anything named *trace*>.append({"type": "...", ...})``  -> trace entry

INBOUND - events the WebCross platform emits and this harness only consumes
(§7-§9). Their names come from the platform's own catalog, read from two
independent pieces of evidence: the vendored source snapshot under
``abcp-platform/`` and the shipped, minified catalog inside the installed
``app.asar``. A name in one but not the other is drift worth failing on
(WebCross 0.9.3 dropped ``DOM.axTreeUpdated``).

Consumers are located by a second pass over the same files looking for the
read side (``read_events``, ``.trace``, ``worker_trace_events``, ``on_event``),
because the point of the catalog is to prove which producer has a reader.
"""

from __future__ import annotations

import argparse
import ast
import json
import os
import re
import subprocess
import sys
import time
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

两个方向分开统计，不要混读：

- **出站**（§2-§6）：本 harness 自己写的 run event 与 trace，落在 run log /
  SQLite，读者是 Console、replay 和审计。
- **入站**（§7-§9）：WebCross 平台通过 `System.notification` 推给 harness 的
  事件。harness 只消费不产生；名字属于平台的目录，不属于本仓库。事件名与
  Action 名同形（`Page.navigate` 两者都是），所以入站侧一律以平台目录为准，
  不按字符串形状猜。
"""


class Producer(NamedTuple):
    event_type: str
    path: str
    line: int
    kind: str          # "run_event" | "trace"
    dynamic: bool = False   # event type is not a plain literal
    channel: str = ""       # which call form produced it


# Not product source: tests/ holds doubles that call logger.write too, and
# docs/ holds one-off audit repro scripts kept as evidence. Both are in
# .gitignore but a few files are staged anyway, so they are dropped by name.
NON_PRODUCT_PREFIXES = ("tests/", "docs/")


def candidate_names(raw: str) -> List[str]:
    """Dedupe ``git ls-files`` output and drop the non-product trees."""
    return [
        name for name in dict.fromkeys(raw.split("\0"))
        if name and not name.startswith(NON_PRODUCT_PREFIXES)
    ]


def tracked_python_files() -> List[Path]:
    """Git-tracked .py files, plus new sources that are not yet committed.

    A bare filesystem walk pulls in venv/, abcp-platform/ and the ignored
    tests/ tree, which would make the counts meaningless. ``--others
    --exclude-standard`` adds the files a running refactor introduced but has
    not staged yet - they are real producer sites, and .gitignore still keeps
    venv/, abcp-platform/ and tests/ out.

    Files deleted in the worktree but still present in the index are skipped:
    they produce no event at runtime, and reading them raises FileNotFoundError.
    """
    try:
        out = subprocess.run(
            ["git", "ls-files", "-z", "--cached", "--others",
             "--exclude-standard", "*.py"],
            cwd=REPO_ROOT, capture_output=True, text=True, check=True,
        ).stdout
    except subprocess.CalledProcessError as exc:
        # Seen in practice: an x86_64 interpreter invoking /usr/bin/git under
        # Rosetta dies in xcrun before git runs. The traceback says nothing
        # about that, so say it here.
        raise SystemExit(
            "git ls-files failed: " + (exc.stderr or "").strip() +
            "\nIf this mentions xcrun/libxcrun, the interpreter and git have "
            "different architectures - run this tool with a native "
            "interpreter (e.g. /usr/bin/python3)."
        )
    # Skip this file. Its own aggregation loops read `.event_type` off local
    # variables, which the producer scan below reads as an event named `item`.
    # A catalog of the catalog generator is never the intent.
    self_path = Path(__file__).resolve()
    return [
        path for name in candidate_names(out)
        for path in [REPO_ROOT / name]
        if path.is_file() and path.resolve() != self_path
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
    if isinstance(node, ast.IfExp):
        # logger.write("a.ok" if ok else "a.fallback", ...): both names are
        # real events and the choice is only made at runtime. Keep them both,
        # joined, instead of dumping the raw expression into the table.
        body, _ = _literal(node.body)
        alternative, _ = _literal(node.orelse)
        return f"{body}|{alternative}", True
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


def _wrapper_event_name(node: ast.Call) -> Optional[ast.AST]:
    """The event-name argument of a ``_log(...)`` wrapper call.

    Two shapes exist: ``self._log("name", payload)`` and the module-level
    ``_log(logger, "name", payload)``. The name is therefore the first string
    argument; when the caller passes a variable instead, the first argument
    whose source mentions ``event`` is the name (``log_event``).
    """
    literal = next(
        (arg for arg in node.args
         if isinstance(arg, (ast.Constant, ast.JoinedStr, ast.IfExp))),
        None,
    )
    if literal is not None:
        return literal
    return next(
        (arg for arg in node.args
         if "event" in _receiver_name(arg).lower()),
        None,
    )


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
            callee = (
                func.attr if isinstance(func, ast.Attribute)
                else func.id if isinstance(func, ast.Name)
                else ""
            )

            if isinstance(func, ast.Attribute) and func.attr == "write":
                receiver = _receiver_name(func.value).lower()
                if "logger" not in receiver:
                    continue
                event_type, dynamic = _literal(first)
                producers.append(
                    Producer(event_type, rel, node.lineno, "run_event",
                             dynamic, "logger.write")
                )
            elif callee == "_write_agent_event":
                event_type, dynamic = _literal(first)
                producers.append(
                    Producer(event_type, rel, node.lineno, "run_event",
                             dynamic, "_write_agent_event")
                )
            elif callee == "_log":
                name_node = _wrapper_event_name(node)
                if name_node is None:
                    continue
                event_type, dynamic = _literal(name_node)
                producers.append(
                    Producer(event_type, rel, node.lineno, "run_event",
                             dynamic, "_log")
                )
                continue
            elif isinstance(func, ast.Attribute) and func.attr == "append":
                receiver = _receiver_name(func.value).lower()
                if "trace" not in receiver:
                    continue
                event_type, dynamic = _trace_type(first)
                producers.append(
                    Producer(event_type, rel, node.lineno, "trace",
                             dynamic, "trace.append")
                )
            for keyword in node.keywords:
                # `_apply_reducer(..., log_event="page.dialog.ledger")`: the
                # event name travels as a keyword into a _log wrapper.
                if keyword.arg != "log_event":
                    continue
                event_type, dynamic = _literal(keyword.value)
                producers.append(
                    Producer(event_type, rel, node.lineno, "run_event",
                             dynamic, "log_event=")
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


# --- INBOUND: the WebCross platform's own event catalog -----------------
#
# Two independent evidence sources, because neither alone can answer "what
# does the runtime I am talking to actually emit":
#
# * the vendored platform source under ``abcp-platform/`` - readable
#   definitions with file and line, but a snapshot that can lag the build;
# * the installed ``app.asar`` - the shipped catalog. It is minified, yet
#   every entry survives as string literals, so it describes the runtime
#   that is actually on this machine.

PLATFORM_ROOT = REPO_ROOT / "abcp-platform"
PLATFORM_CATALOG_SOURCES = ("packages/events/src/catalog.ts",)
PLATFORM_DOMAIN_GLOB = "packages/events/src/domains/*/events.ts"
PLATFORM_ACTION_GLOB = "packages/actions/src/domains/*/*/def.ts"
DEFAULT_BUNDLE = Path("/Applications/WebCross.app/Contents/Resources/app.asar")
BUNDLE_ENV = "WEBCROSS_APP_ASAR"

# One catalog entry: `event: 'Page.loaded', category: 'navigation',
# severity: 'info', audience: 'both'` - identical in the TS source and in the
# minified bundle, which is what makes the cross-check mechanical.
EVENT_ENTRY_RE = re.compile(
    r"event:\s*['\"](?P<name>[A-Z][A-Za-z0-9]*\.[A-Za-z][A-Za-z0-9]*)['\"]\s*,"
    r"\s*category:\s*['\"](?P<category>[a-z]+)['\"]\s*,"
    r"\s*severity:\s*['\"](?P<severity>[a-z]+)['\"]\s*,"
    r"\s*audience:\s*['\"](?P<audience>[a-z]+)['\"]"
)
# A platform-shaped name. Used only to find literals that are in NEITHER
# catalog, so prose like `System.getCapabilities` in a prompt can be listed
# for a human instead of silently counted as an event.
PLATFORM_NAME_RE = re.compile(r"^[A-Z][A-Za-z0-9]*\.[A-Za-z][A-Za-z0-9]*$")
# JSON-RPC method names the harness builds itself. They look like platform
# names but are neither an Action nor an event, so §9.4 labels them instead of
# calling them unknown.
PROTOCOL_METHODS = frozenset({"System.notification"})
NON_NAME_SUFFIXES = (
    ".md", ".json", ".py", ".ts", ".js", ".db", ".txt", ".csv", ".html",
    ".png", ".jpg", ".yml", ".yaml", ".log", ".asar", ".jsonl",
)


class PlatformEvent(NamedTuple):
    name: str
    category: str
    severity: str
    audience: str
    agent_visible: bool   # has an `agent:` projection -> an Agent can receive it


def _catalog_entries(text: str, with_lines: bool = True) -> Dict[str, Tuple[PlatformEvent, int]]:
    found: Dict[str, Tuple[PlatformEvent, int]] = {}
    for match in EVENT_ENTRY_RE.finditer(text):
        name = match.group("name")
        if name in found:
            continue
        # `agent: {...}` follows the four scalars within the same entry.
        tail = text[match.end():match.end() + 600]
        agent = re.search(r"\bagent:\s*\{", tail) is not None
        line = text.count("\n", 0, match.start()) + 1 if with_lines else 0
        found[name] = (
            PlatformEvent(name, match.group("category"), match.group("severity"),
                          match.group("audience"), agent),
            line,
        )
    return found


def vendored_event_catalog() -> Dict[str, Tuple[PlatformEvent, str, int]]:
    """name -> (event, source path relative to the repo, line)."""
    catalog: Dict[str, Tuple[PlatformEvent, str, int]] = {}
    if not PLATFORM_ROOT.is_dir():
        return catalog
    sources = [PLATFORM_ROOT / rel for rel in PLATFORM_CATALOG_SOURCES]
    sources.extend(sorted(PLATFORM_ROOT.glob(PLATFORM_DOMAIN_GLOB)))
    for path in sources:
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for name, (event, line) in _catalog_entries(text).items():
            catalog.setdefault(name, (event, str(path.relative_to(REPO_ROOT)), line))
    return catalog


def vendored_action_names() -> Set[str]:
    """Action names from the same platform snapshot.

    Needed because `Page.navigate` is both an Action and an event: without the
    Action list the overlap in §8 cannot be shown, only asserted.
    """
    names: Set[str] = set()
    if not PLATFORM_ROOT.is_dir():
        return names
    for path in sorted(PLATFORM_ROOT.glob(PLATFORM_ACTION_GLOB)):
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        domain = re.search(r"domain:\s*['\"]([A-Za-z0-9]+)['\"]", text)
        action = re.search(r"action:\s*['\"]([A-Za-z0-9]+)['\"]", text)
        if domain and action:
            names.add(f"{domain.group(1)}.{action.group(1)}")
    return names


def bundle_path() -> Optional[Path]:
    override = os.environ.get(BUNDLE_ENV)
    path = Path(override).expanduser() if override else DEFAULT_BUNDLE
    return path if path.is_file() else None


def installed_bundle(path: Path) -> Tuple[Dict[str, PlatformEvent], str]:
    """(catalog shipped in the installed bundle, decoded bundle text).

    The text is returned so a caller can test further names for presence
    without reading 50 MB twice.
    """
    try:
        text = path.read_bytes().decode("utf-8", "replace")
    except OSError:
        return {}, ""
    return (
        {event.name: event
         for event, _ in _catalog_entries(text, with_lines=False).values()},
        text,
    )


def bundle_presence(text: str, candidates: Set[str]) -> Set[str]:
    """Which candidate names occur anywhere in the installed bundle.

    One pass with a literal alternation: a name-by-name scan of a 50 MB bundle
    would cost seconds per name. Presence proves the string ships in the build;
    it does not prove the runtime emits or accepts it - that is why §7 uses the
    parsed catalog instead, and this is only used for the anomaly table §9.4.
    """
    if not text or not candidates:
        return set()
    # Longest name first: a plain sorted alternation would match the `DOM.get`
    # prefix of `DOM.getAXTree` and report the longer name as absent.
    ordered = sorted(candidates, key=lambda name: (-len(name), name))
    pattern = re.compile("|".join(re.escape(name) for name in ordered))
    return {match.group(0) for match in pattern.finditer(text)}


def _run(cmd: List[str], cwd: Optional[Path] = None, timeout: int = 20) -> str:
    try:
        out = subprocess.run(
            cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return out.stdout.strip() if out.returncode == 0 else ""


def platform_provenance(bundle: Optional[Path]) -> List[str]:
    """Identity of every piece of platform evidence, so no row is read as a
    version claim it cannot support."""
    lines: List[str] = []
    if PLATFORM_ROOT.is_dir():
        head = _run(["git", "rev-parse", "--short", "HEAD"], cwd=PLATFORM_ROOT)
        describe = _run(["git", "describe", "--tags", "--always"], cwd=PLATFORM_ROOT)
        date = _run(["git", "log", "-1", "--format=%ad", "--date=short"], cwd=PLATFORM_ROOT)
        lines.append(
            f"- 平台源码快照 `abcp-platform/`：commit `{head or '?'}`"
            f"（`{describe or '?'}`，{date or '?'}）→ §7 的定义、行号与 Agent 投影"
        )
    else:
        lines.append("- 平台源码快照 `abcp-platform/`：本机未提供 → §7 只剩安装包证据")
    raw = _run(["webcross", "--version"])
    version = ""
    try:
        version = str(json.loads(raw).get("data", "")) if raw else ""
    except ValueError:
        version = raw
    lines.append(f"- 已安装 CLI：`{version}`" if version else "- 已安装 CLI：未检测到 `webcross`")
    if bundle is not None:
        try:
            stat = bundle.stat()
            stamp = time.strftime("%Y-%m-%d %H:%M", time.localtime(stat.st_mtime))
            size = f"{stat.st_size / 1e6:.1f} MB"
        except OSError:
            stamp, size = "?", "?"
        lines.append(
            f"- 已安装运行时包：`{bundle}`（{size}，mtime {stamp}）"
            f"→ §7 `安装包` 列的证据来源"
        )
    else:
        lines.append(
            f"- 已安装运行时包：未找到（可用 `{BUNDLE_ENV}=<app.asar>` 指定）"
            "→ §7 `安装包` 列为 `未检测`"
        )
    return lines


def collect_platform_references(
    paths: List[Path], known: Set[str]
) -> Tuple[Dict[str, List[Tuple[str, int]]], Dict[str, List[Tuple[str, int]]]]:
    """(catalog event name -> sites, non-catalog platform-shaped name -> sites).

    Only names that appear in a platform catalog are counted as event
    references. Guessing from the shape of a string would read the Action call
    `Page.navigate` and the prompt prose `System.getCapabilities` as events.
    """
    referenced: Dict[str, List[Tuple[str, int]]] = defaultdict(list)
    unknown: Dict[str, List[Tuple[str, int]]] = defaultdict(list)
    for path in paths:
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (OSError, SyntaxError, UnicodeDecodeError):
            continue
        rel = str(path.relative_to(REPO_ROOT))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
                continue
            value = node.value
            if value in known:
                referenced[value].append((rel, node.lineno))
            elif (
                PLATFORM_NAME_RE.match(value)
                and not value.endswith(NON_NAME_SUFFIXES)
            ):
                unknown[value].append((rel, node.lineno))
    return dict(referenced), dict(unknown)


def _cell(text: str) -> str:
    """Escape a value for use inside a Markdown table cell.

    A conditional event name renders as ``a.ok|a.fallback``; the bare pipe
    would split the cell in two.
    """
    return text.replace("|", "\\|")


class Platform(NamedTuple):
    """Everything §7-§9 need about the INBOUND side."""

    vendored: Dict[str, Tuple[PlatformEvent, str, int]]
    installed: Dict[str, PlatformEvent]
    actions: Set[str]
    references: Dict[str, List[Tuple[str, int]]]
    unknown: Dict[str, List[Tuple[str, int]]]
    provenance: List[str]
    bundle: Optional[Path]
    shipped: Set[str]          # candidate names found in the bundle text

    @property
    def names(self) -> List[str]:
        return sorted(set(self.vendored) | set(self.installed))

    @property
    def known_names(self) -> Set[str]:
        """Every platform name in either catalog: events and Actions."""
        return set(self.vendored) | set(self.installed) | self.actions

    @property
    def shippable(self) -> List[str]:
        """Catalog events the installed runtime actually ships."""
        if self.bundle is None:
            return self.names
        return [name for name in self.names if name in self.installed]

    def meta(self, name: str) -> PlatformEvent:
        """Installed metadata wins: it describes the runtime on this machine."""
        return self.installed.get(name) or self.vendored[name][0]

    def in_bundle(self, name: str) -> str:
        if self.bundle is None:
            return "未检测"
        return "✅" if name in self.installed else "❌"

    def sites(self, name: str, limit: int = 2) -> str:
        hits = sorted(self.references.get(name, []))
        if not hits:
            return "*无*"
        where = ", ".join(f"`{path}:{line}`" for path, line in hits[:limit])
        if len(hits) > limit:
            where += f" 等 {len(hits)} 处"
        return where


def _where(items: List[Producer], limit: int = 4) -> str:
    where = ", ".join(f"`{item.path}:{item.line}`" for item in sorted(items)[:limit])
    if len(items) > limit:
        where += f" 等 {len(items)} 处"
    return where


def render(producers: List[Producer], consumers, platform: Platform,
           scanned: int) -> str:
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

    channels: Dict[str, int] = defaultdict(int)
    for item in run_events:
        channels[item.channel or "?"] += 1

    referenced = [name for name in platform.names if platform.references.get(name)]
    agent_visible = [
        name for name in platform.shippable if platform.meta(name).agent_visible
    ]
    unread = [name for name in agent_visible if not platform.references.get(name)]

    lines: List[str] = [HEADER, ""]
    lines.append("## 1. 汇总")
    lines.append("")
    lines.append("| 指标 | 数量 |")
    lines.append("|---|---:|")
    lines.append(f"| 出站 run event 产生点 | {len(run_events)} |")
    lines.append(f"| 出站 run event 类型 | {len(by_type)} |")
    lines.append(f"| 动态（非字面量）事件名 | {sum(1 for i in run_events if i.dynamic)} |")
    lines.append(f"| 出站 trace 产生点 | {len(traces)} |")
    lines.append(f"| 出站 trace type | {len(trace_by_type)} |")
    lines.append(f"| 入站平台事件（目录内） | {len(platform.names)} |")
    lines.append(f"| 入站平台事件（安装包内） | {len(platform.shippable)} |")
    lines.append(f"| 入站平台事件（Agent 可见） | {len(agent_visible)} |")
    lines.append(f"| 入站平台事件（harness 有引用） | {len(referenced)} |")
    lines.append(f"| 入站平台事件（Agent 可见但无引用） | {len(unread)} |")
    lines.append("")

    lines.append("### 1.1 事件族与方向")
    lines.append("")
    lines.append("| 族 | 方向 | 谁产生 | 谁消费 | 章节 |")
    lines.append("|---|---|---|---|---|")
    lines.append(
        "| run event | 出站：harness → run log / SQLite | "
        "`logger.write`、`_write_agent_event`、`_log` 包装器 | "
        "`read_events`、Console、replay、审计 | §2-§4、§6 |"
    )
    lines.append(
        "| trace | 出站：harness → `agent.trace` / `worker_trace_events` | "
        "`*.trace.append({\"type\": ...})` | judge、worker trace 表读者 | §5、§6 |"
    )
    lines.append(
        "| WebCross 平台事件 | 入站：Dispatcher → harness（`System.notification`） | "
        "平台（不在本仓库） | `event_observer`、reducers、`fleet.runtime`、"
        "`page_lifecycle` | §7-§9 |"
    )
    lines.append("")
    lines.append(
        "入站事件不会产生 §4 里的任何一行；它们被消费后**才**可能触发一条出站 "
        "run event（已核实的两条链：`Hitl.paused` → "
        "`workflow.hitl_barrier.claimed_by_event_observer`；`Page.dialogOpened` → "
        "`page.dialog.ledger`）。这条因果只能靠 §7 的引用点与 §4 的位置对照读出，"
        "机械提取不会替它编造联系。"
    )
    lines.append("")

    lines.append("### 1.2 证据来源与扫描范围")
    lines.append("")
    lines.extend(platform.provenance)
    lines.append(
        f"- 出站扫描范围：`git ls-files --cached --others --exclude-standard '*.py'`"
        f" 去掉 `{'`、`'.join(NON_PRODUCT_PREFIXES)}` 与本生成器，共 {scanned} 个文件"
    )
    lines.append("")
    lines.append("| run event 产生通道 | 产生点 |")
    lines.append("|---|---:|")
    for channel, count in sorted(channels.items(), key=lambda kv: (-kv[1], kv[0])):
        lines.append(f"| `{channel}` | {count} |")
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

    lines.append("## 4. run event 类型全表（出站）")
    lines.append("")
    lines.append("| 事件类型 | 产生点 | 通道 | 位置 |")
    lines.append("|---|---:|---|---|")
    for event_type, items in sorted(by_type.items()):
        label = f"`{_cell(event_type)}`" if event_type else "*(空)*"
        if items[0].dynamic:
            label += " ⚠动态"
        used = sorted({item.channel or "?" for item in items})
        channel = ", ".join(f"`{_cell(name)}`" for name in used)
        lines.append(f"| {label} | {len(items)} | {channel} | {_where(items)} |")
    lines.append("")

    lines.append("## 5. trace type 全表（出站）")
    lines.append("")
    lines.append("| trace type | 产生点 | 位置 |")
    lines.append("|---|---:|---|")
    for event_type, items in sorted(trace_by_type.items()):
        lines.append(f"| `{_cell(event_type)}` | {len(items)} | {_where(items)} |")
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
            lines.append(f"| `{rel}:{line_no}` | `{_cell(text)}` |")
        if len(hits) > 40:
            lines.append(f"| … | 另有 {len(hits) - 40} 处 |")
        lines.append("")

    lines.append("## 7. WebCross 平台事件目录（入站）")
    lines.append("")
    lines.append(
        "平台自己发出的事件，harness 只消费不产生。名字与元数据取自 §1.2 的两处"
        "证据：`安装包` 列是已安装运行时包里能否找到该事件的目录定义；`引用点` 是"
        "产品源码里按这个名字消费它的位置数（字符串常量精确匹配，不按形状猜）。"
        "`Agent 可见` 指平台目录里该事件带 Agent 投影，即 Agent 订阅得到的那一部分；"
        "标 `—` 的只投给 User 界面，harness 永远收不到。"
    )
    lines.append("")
    lines.append(
        "注意：`引用点` 对 §8 里事件名与 Action 名同形的三行是**混合计数**（同一"
        "字符串既可能是等待的事件，也可能是调用的 Action）；机械提取不替它做语义"
        "判断，要分辨得看位置上的代码。"
    )
    lines.append("")
    if not platform.names:
        lines.append("*两处平台证据都不可用：`abcp-platform/` 与安装包均未找到。*")
        lines.append("")
    else:
        lines.append(
            "| 事件 | category | severity | audience | Agent 可见 | 安装包 | 引用点 | 位置 |"
        )
        lines.append("|---|---|---|---|---|---|---:|---|")
        for name in platform.names:
            meta = platform.meta(name)
            source = platform.installed.get(name) or platform.vendored[name][0]
            differs = (
                name in platform.installed and name in platform.vendored
                and source != platform.vendored[name][0]
            )
            label = f"`{name}`" + (" ⚠两处不一致" if differs else "")
            lines.append(
                f"| {label} | `{meta.category}` | `{meta.severity}` | `{meta.audience}` "
                f"| {'✅' if meta.agent_visible else '—'} | {platform.in_bundle(name)} "
                f"| {len(platform.references.get(name, []))} | {platform.sites(name)} |"
            )
        lines.append("")

    lines.append("## 8. 事件名与 Action 名同形（区分用）")
    lines.append("")
    lines.append(
        "同一个名字既是平台 Action（可以调用）又是平台事件（只能等待）。指南里的"
        "“事件名不是 Action”说的就是这一类：`引用点` 是字符串出现次数，两侧混计。"
    )
    lines.append("")
    overlap = sorted(set(platform.names) & platform.actions)
    if not overlap:
        lines.append("*无重叠，或平台目录不可用。*")
    else:
        lines.append("| 名字 | 事件目录 | Action 目录 | 引用点（两侧混计） | 位置 |")
        lines.append("|---|---|---|---:|---|")
        for name in overlap:
            lines.append(
                f"| `{name}` | ✅ | ✅ | {len(platform.references.get(name, []))} "
                f"| {platform.sites(name)} |"
            )
    lines.append("")

    lines.append("## 9. 平台事件漂移与覆盖差")
    lines.append("")

    lines.append("### 9.1 源码快照有、安装包没有（该运行时已移除）")
    lines.append("")
    gone = [name for name in platform.names
            if name in platform.vendored and platform.bundle is not None
            and name not in platform.installed]
    if platform.bundle is None:
        lines.append("*安装包不可用，无法判定。*")
    elif not gone:
        lines.append("*无。*")
    else:
        lines.append(
            "引用点大于 0 的行是**死分支**：代码在等一个该运行时不会再发的事件。"
        )
        lines.append("")
        lines.append("| 事件 | Agent 可见 | 源码定义 | 引用点 | 位置 |")
        lines.append("|---|---|---|---:|---|")
        for name in gone:
            event, source, line = platform.vendored[name]
            lines.append(
                f"| `{name}` | {'✅' if event.agent_visible else '—'} | `{source}:{line}` "
                f"| {len(platform.references.get(name, []))} | {platform.sites(name)} |"
            )
    lines.append("")

    lines.append("### 9.2 安装包有、源码快照没有（本地快照落后）")
    lines.append("")
    added = [name for name in platform.names
             if name in platform.installed and name not in platform.vendored]
    if platform.bundle is None or not platform.vendored:
        lines.append("*证据不全，无法判定。*")
    elif not added:
        lines.append("*无。*")
    else:
        lines.append("| 事件 | category | severity | audience | Agent 可见 | 引用点 |")
        lines.append("|---|---|---|---|---|---:|")
        for name in added:
            meta = platform.installed[name]
            lines.append(
                f"| `{name}` | `{meta.category}` | `{meta.severity}` | `{meta.audience}` "
                f"| {'✅' if meta.agent_visible else '—'} "
                f"| {len(platform.references.get(name, []))} |"
            )
    lines.append("")

    lines.append("### 9.3 Agent 可见但 harness 没有任何引用（无人消费）")
    lines.append("")
    if not unread:
        lines.append("*无。*")
    else:
        lines.append("| 事件 | category | severity | 安装包 |")
        lines.append("|---|---|---|---|")
        for name in unread:
            meta = platform.meta(name)
            lines.append(
                f"| `{name}` | `{meta.category}` | `{meta.severity}` "
                f"| {platform.in_bundle(name)} |"
            )
    lines.append("")

    lines.append("### 9.4 harness 里出现的非事件平台形名")
    lines.append("")
    lines.append(
        "形如 `Domain.name` 的字符串常量，但不在事件目录里：绝大多数是 Action 名（正"
        "常，调用侧）。`类别` 为 `⚠未知` 且 `安装包字符串` 为 `❌` 的行排在最前："
        "代码引用的名字既不属于任何目录，已安装运行时包里也找不到（已删除的 Action"
        "或旧版事件名）。`前缀` 类是故意的前缀匹配常量，不是名字。"
    )
    lines.append("")
    if not platform.unknown:
        lines.append("*无。*")
    else:
        def kind_of(name: str) -> str:
            if name in platform.actions:
                return "Action"
            if name in PROTOCOL_METHODS:
                return "协议方法"
            if any(other != name and other.startswith(name)
                   for other in platform.known_names):
                # `_STABLE_BROWSER_METHOD_PREFIXES` and friends match on a
                # prefix, so the literal is a prefix, not a missing name.
                return "前缀"
            return "⚠未知"

        # Anomalies first: a name that is neither an Action, a protocol method
        # nor a prefix outranks the ordinary call sites that fill this table,
        # and one the installed bundle does not contain at all outranks both.
        def _rank(name: str) -> Tuple[int, int, str]:
            odd = 1 if kind_of(name) == "⚠未知" else 0
            missing = 0 if (platform.bundle is not None
                            and name not in platform.shipped) else 1
            return (-odd, missing, name)

        lines.append("| 名字 | 类别 | 安装包字符串 | 出现次数 | 位置 |")
        lines.append("|---|---|---|---:|---|")
        for name in sorted(platform.unknown, key=_rank):
            hits = sorted(platform.unknown[name])
            first = ", ".join(f"`{path}:{line}`" for path, line in hits[:2])
            if len(hits) > 2:
                first += f" 等 {len(hits)} 处"
            shipped = (
                "未检测" if platform.bundle is None
                else ("✅" if name in platform.shipped else "❌")
            )
            lines.append(
                f"| `{_cell(name)}` | {kind_of(name)} | {shipped} | {len(hits)} | {first} |"
            )
    lines.append("")

    return "\n".join(lines) + "\n"


def collect_platform(paths: List[Path]) -> Platform:
    bundle = bundle_path()
    vendored = vendored_event_catalog()
    installed, bundle_text = (
        installed_bundle(bundle) if bundle is not None else ({}, "")
    )
    actions = vendored_action_names()
    known = set(vendored) | set(installed)
    references, unknown = collect_platform_references(paths, known)
    return Platform(
        vendored=vendored,
        installed=installed,
        actions=actions,
        references=references,
        unknown=unknown,
        provenance=platform_provenance(bundle),
        bundle=bundle,
        shipped=bundle_presence(bundle_text, set(unknown) | actions),
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check", action="store_true",
        help="fail if the checked-in catalog differs from a fresh render",
    )
    args = parser.parse_args()

    paths = tracked_python_files()
    rendered = render(
        collect_producers(paths), collect_consumers(paths),
        collect_platform(paths), len(paths),
    )

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
