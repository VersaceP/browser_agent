"""Run guest JS in the installed WebCross Workflow runtime, without a page.

Offline regression tests must behave exactly like the deployed program: WebCross compiles the package with its own esbuild settings
and runs it in its QuickJS WASM VM (no fs, network, process, timers). This
module drives that same WorkflowService through the WebCross app's bundled
Node (ELECTRON_RUN_AS_NODE) with an empty action host, so guest code has no
platform access at all. The host process only runs WebCross's own modules.

There is no fallback to the system Node: if WebCross is unavailable the
caller reports "unavailable" instead of running with different semantics.
"""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
from pathlib import Path
from typing import Any

DEFAULT_APP = Path("/Applications/WebCross.app")
LIMITS = {"files": 64, "fileBytes": 256 * 1024, "sourceBytes": 2 * 1024 * 1024, "inputBytes": 1024 * 1024}

_RUNNER = r"""
const R = process.env.WEBCROSS_MODULES;
const { WorkflowService } = await import(R + "/@abcp/dispatcher/dist/runtime/workflowService.js");
const { createDatabase } = await import(R + "/@abcp/dispatcher/dist/data/database.js");
const { TaskRepository } = await import(R + "/@abcp/dispatcher/dist/tasks/taskRepository.js");
const { TaskService } = await import(R + "/@abcp/dispatcher/dist/tasks/taskService.js");
const chunks = [];
for await (const chunk of process.stdin) chunks.push(chunk);
const jobs = JSON.parse(Buffer.concat(chunks).toString("utf8"));
const service = new WorkflowService(new TaskService(new TaskRepository(createDatabase(":memory:"))));
// No actions, artifacts or events: guest code gets the QuickJS VM only.
const host = { agentId: "harness-offline", actions: [],
  events: { getCursor: () => 0, subscribeAgent: () => () => {} },
  readArtifact: async () => { throw new Error("No artifacts offline"); },
  invokeAction: async () => { throw new Error("No platform actions offline"); } };
const results = [];
for (const job of jobs) {
  try {
    const r = await service.execute({ workflow: job.workflow, input: job.input ?? {} }, host);
    results.push({ status: r.status, output: r.output ?? null, error: r.error ?? null,
                   timing: r.timing ?? null });
  } catch (e) {
    results.push({ status: "rejected", output: null,
      error: { code: e.code ?? "workflow-rejected", message: String(e.message ?? e).slice(0, 4000),
               details: e.details ?? null } });
  }
}
await service.close();
process.stdout.write(JSON.stringify(results));
"""


def locate() -> dict[str, Any] | None:
    """The installed WebCross runtime, or None. WEBCROSS_APP overrides the app path."""
    app = Path(os.environ.get("WEBCROSS_APP") or DEFAULT_APP)
    binary = app / "Contents" / "MacOS" / "WebCross"
    resources = app / "Contents" / "Resources"
    modules = resources / "app.asar" / "dist" / "node_modules"
    unpacked = resources / "app.asar.unpacked" / "dist" / "node_modules" / "@esbuild"
    esbuild = next(iter(sorted(unpacked.glob("*/bin/esbuild"))), None) if unpacked.is_dir() else None
    if not binary.is_file() or not (resources / "app.asar").is_file() or esbuild is None:
        return None
    version = None
    try:
        version = json.loads((resources / "resources" / "build-info.json").read_text())["appVersion"]
    except (OSError, ValueError, KeyError):
        pass
    return {"binary": str(binary), "modules": str(modules), "esbuild": str(esbuild), "version": version}


def unavailable_reason() -> str:
    return ("WebCross Workflow runtime not found (set WEBCROSS_APP to WebCross.app). "
            "Guest code is never run on a different JS engine.")


def _command(runtime: dict) -> tuple[list[str], dict]:
    env = {"PATH": os.environ.get("PATH", ""), "HOME": os.environ.get("HOME", ""),
           "ELECTRON_RUN_AS_NODE": "1", "ESBUILD_BINARY_PATH": runtime["esbuild"],
           "WEBCROSS_MODULES": runtime["modules"], "NODE_OPTIONS": ""}
    return [runtime["binary"], "--input-type=module", "-e", _RUNNER], env


def _decode(stdout: str, stderr: str, code: int | None, count: int) -> list[dict]:
    if code != 0:
        raise RuntimeError("WebCross runtime failed: " + (stderr or stdout)[-3000:])
    results = json.loads(stdout)
    if not isinstance(results, list) or len(results) != count:
        raise RuntimeError("WebCross runtime returned an unexpected result")
    return results


def run_jobs(jobs: list[dict], *, timeout_s: float = 120.0) -> list[dict]:
    runtime = locate()
    if runtime is None:
        raise RuntimeError(unavailable_reason())
    command, env = _command(runtime)
    completed = subprocess.run(command, input=json.dumps(jobs, ensure_ascii=False), capture_output=True,
                               text=True, timeout=timeout_s, env=env)
    return _decode(completed.stdout, completed.stderr, completed.returncode, len(jobs))


async def run_jobs_async(jobs: list[dict], *, timeout_s: float = 120.0) -> list[dict]:
    runtime = locate()
    if runtime is None:
        raise RuntimeError(unavailable_reason())
    command, env = _command(runtime)
    process = await asyncio.create_subprocess_exec(*command, stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, env=env)
    try:
        stdout, stderr = await asyncio.wait_for(
            process.communicate(json.dumps(jobs, ensure_ascii=False).encode()), timeout_s)
    except asyncio.TimeoutError:
        process.kill()
        await process.wait()
        raise RuntimeError(f"WebCross runtime did not return within {timeout_s}s")
    return _decode(stdout.decode("utf-8", "replace"), stderr.decode("utf-8", "replace"),
                   process.returncode, len(jobs))


def package_sources(root: Path, *, exclude_tests: bool = True) -> dict[str, str]:
    """Package-relative JS/TS sources, excluding hidden paths and node_modules."""
    root = Path(root).resolve()
    files: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if path.is_symlink() or not path.is_file() or path.suffix not in {".js", ".ts"}:
            continue
        relative = path.relative_to(root).as_posix()
        if any(part.startswith(".") or part == "node_modules" for part in relative.split("/")):
            continue
        if exclude_tests and relative.startswith("tests/"):
            continue
        files[relative] = path.read_text(encoding="utf-8")
    return files


import re as _re

_IMPORT = _re.compile(r"""(?:\bimport\s*(?:[\w*{}\s,$]+\s*from\s*)?|\bexport\s*[\w*{}\s,$]*\s*from\s*|\bimport\s*\(\s*)["']([^"']+)["']""")


def reachable(files: dict[str, str], entry: str) -> dict[str, str]:
    """Only modules statically reachable from entry, resolved like the WebCross compiler.

    Unreferenced probes and other workflows never count against package limits.
    Unresolvable specifiers are kept out; the compiler reports them precisely.
    """
    import posixpath
    seen: dict[str, str] = {}
    pending = [entry]
    while pending:
        name = pending.pop()
        if name in seen or name not in files:
            continue
        seen[name] = files[name]
        for spec in _IMPORT.findall(files[name]):
            if not spec.startswith(("./", "../")):
                continue
            target = posixpath.normpath(posixpath.join(posixpath.dirname(name), spec))
            if target not in files and target.endswith(".js"):
                target = target[:-3] + ".ts"
            pending.append(target)
    return seen


def check_limits(files: dict[str, str], business_input: Any) -> str | None:
    if len(files) > LIMITS["files"]:
        return f"package has {len(files)} files; WebCross allows {LIMITS['files']}"
    sizes = {name: len(text.encode()) for name, text in files.items()}
    big = [name for name, size in sizes.items() if size > LIMITS["fileBytes"]]
    if big:
        return f"files exceed 256 KiB: {big[:5]}"
    if sum(sizes.values()) > LIMITS["sourceBytes"]:
        return "package sources exceed 2 MiB"
    if len(json.dumps(business_input, ensure_ascii=False).encode()) > LIMITS["inputBytes"]:
        return "input exceeds 1 MiB"
    return None
