"""Workflow-owned local processes, with real OS-enforced file scopes.

This is separate from WebCross's guest QuickJS runtime. macOS sandbox-exec
enforces explicit task READ/WRITE grants, including for child processes. If
the backend cannot start, return the error; never retry without the sandbox.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import shutil
import signal as signals
import sys
import tempfile
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from harness.tools import path_authorization as auth
from harness.workflow_mode.evidence import evidence_locations

_FIXED_OPERATION = object()  # host-created argv, never accepted in model arguments
_CAPTURE_BYTES = 4 * 1024 * 1024  # per stream; stop rather than silently drop evidence
_MAX_STDIN_BYTES = 2 * 1024 * 1024
_MAX_TIMEOUT_SECONDS = 3600
_SYSTEM_ROOTS = ("/System", "/Library/Apple", "/usr/lib", "/usr/libexec", "/usr/share",
                 "/usr/bin", "/usr/sbin", "/bin", "/sbin", "/private/var/db/dyld",
                 "/private/var/db/oah", "/private/var/db/timezone")


def _quoted(value: str | Path) -> str:
    return json.dumps(str(value), ensure_ascii=False)


def _subpath(path: str | Path) -> str:
    return f"(subpath {_quoted(path)})"


def _casefold_pattern(text: str) -> str:
    # SBPL regex is POSIX ERE; inline Python flags are not supported.
    return "".join(f"[{c.lower()}{c.upper()}]" if c.isascii() and c.isalpha()
                   else re.escape(c) for c in text)


def _protected_filter(path: str | Path) -> str:
    return f'(regex {_quoted("^" + _casefold_pattern(str(path)) + "(/|$)")})'


def runtime_roots(executable: Path) -> list[str]:
    roots = [Path(p) for p in _SYSTEM_ROOTS] + [Path(sys.prefix), Path(sys.base_prefix)]
    # Homebrew binaries use dylibs in other formulae. Grant code/runtime trees,
    # not the Homebrew etc/ configuration directory or the user's home.
    for prefix in (Path("/opt/homebrew"), Path("/usr/local")):
        if executable.is_relative_to(prefix):
            roots.extend(prefix / part for part in ("Cellar", "opt", "lib", "bin"))
    return sorted({str(p.resolve()) for p in roots if p.exists()})


def sandbox_profile(session: Any, *, reads: list[Path], writes: list[Path],
                    runtime: list[str], scratch: Path, executable: Path) -> str:
    task = auth.task_root(session)
    outputs = [task / part for part in ("observations", "deliverables", "scratchpad")]
    rules = ["(version 1)", "(deny default)", '(import "system.sb")',
             # Imported OS rules bootstrap dyld/Rosetta, not browser access.
             "(deny network*)", "(allow process-exec process-fork)",
             "(allow signal (target self) (target children))", "(allow sysctl-read)",
             "(allow file-read-metadata)",
             # posix_spawn can set a new session in the kernel without calling
             # setsid in userspace. Keep fork/exec, but deny this detach bypass.
             "(deny syscall-unix (syscall-number SYS_setsid SYS_setpgid SYS_posix_spawn))"]
    readable = list(dict.fromkeys([*runtime, *(str(p) for p in reads), str(scratch)]))
    rules.append("(allow file-read* file-map-executable " + " ".join(_subpath(p) for p in readable)
                 + f" (literal {_quoted(executable)}))")
    rules.append("(allow file-write* " + " ".join(_subpath(p) for p in [*writes, scratch]) + ")")
    # Protected roots take precedence even when an approved ancestor includes
    # them. Canonical paths + OS checks also prevent symlink scope escapes.
    protected = sorted(auth.state(session)["protected"])
    if protected:
        rules.append("(deny file-read* file-write* file-map-executable " +
                     " ".join(_protected_filter(p) for p in protected) + ")")
    names = [_casefold_pattern(name) for name in sorted(auth._SENSITIVE)]
    names.append(_casefold_pattern(".env") + r"\.[^/]*")
    sensitive = "(^|/)(" + "|".join(names) + ")(/|$)"
    rules.append(f"(deny file-read* file-write* file-map-executable (regex {_quoted(sensitive)}))")
    installs = [Path(__file__).resolve().parents[2]]
    if task.parent.name == "worktree":
        installs.append(task.parent.parent)
    for install in dict.fromkeys(installs):
        rules.append("(deny file-read* file-write* file-map-executable (require-all " +
                     _protected_filter(install) + f" (require-not {_subpath(task)})))")
    rules.append("(deny file-write* (require-all " + _subpath(task) + " " +
                 " ".join(f"(require-not {_subpath(p)})" for p in [*outputs, scratch]) + "))")
    for raw, mode in auth.state(session)["denied"]:
        operation = "file-read* file-map-executable" if mode == "read" else "file-write*"
        rules.append(f"(deny {operation} {_protected_filter(raw)})")
    return "\n".join(rules) + "\n"


def _command(arguments: dict[str, Any]) -> list[str]:
    argv = arguments.get("argv")
    if (not isinstance(argv, list) or not argv or
            any(not isinstance(a, str) or "\x00" in a for a in argv) or not argv[0]):
        raise ValueError("argv must be a non-empty array of NUL-free strings")
    first = sys.executable if argv[0] in {"python", "python3"} else argv[0]
    if os.sep in first and not Path(first).is_absolute():
        raise ValueError("use an absolute executable path or a name on the host PATH")
    # Never search a model-controlled cwd or relative PATH component.
    search = os.pathsep.join(p for p in os.get_exec_path() if Path(p).is_absolute())
    resolved = shutil.which(first, path=search) if not Path(first).is_absolute() else first
    if not resolved or not Path(resolved).is_file() or not os.access(resolved, os.X_OK):
        raise ValueError(f"executable not found or not executable: {argv[0]}")
    return [str(Path(resolved).resolve()), *argv[1:]]


async def _end_group(process: asyncio.subprocess.Process) -> None:
    # Always clean children, including after the leader has exited. Descendant
    # setsid/setpgid is denied by the profile, so the group remains authoritative.
    try:
        os.killpg(process.pid, signals.SIGTERM)
    except ProcessLookupError:
        pass
    else:
        await asyncio.sleep(0.15)
        try:
            os.killpg(process.pid, signals.SIGKILL)
        except ProcessLookupError:
            pass
    await process.wait()


class LocalScriptRunner:
    def __init__(self, session: Any):
        self.session = session
        self.active: dict[str, asyncio.subprocess.Process] = {}
        self.closing = False

    def _save(self, facts: dict[str, Any]) -> dict[str, Any]:
        session = self.session
        facts["durationMs"] = round((time.monotonic() - facts.pop("_begun")) * 1000)
        ref = session.store.receipt(session.reload(), "workflow.local_execution.result", facts)
        session.state.setdefault("localExecutions", []).append({
            k: facts.get(k) for k in ("executionId", "status", "exitCode", "durationMs", "purpose")})
        session.state["localExecutions"][-1]["receiptRef"] = ref
        session.save()
        view = {k: v for k, v in facts.items() if k not in {"sandboxProfile", "input", "argv"}}
        view.update(stdout=facts.get("stdout", "")[-4000:], stderr=facts.get("stderr", "")[-4000:],
                    outputViewTruncated=any(len(facts.get(k, "")) > 4000 for k in ("stdout", "stderr")),
                    executable=(facts.get("argv") or [None])[0], receiptRef=ref,
                    outputPointers={"stdout": "/stdout", "stderr": "/stderr"},
                    evidenceLocations=evidence_locations(ref, facts, {
                        "stdout": "/stdout", "stderr": "/stderr", "scope": "/scope",
                        "input": "/input", "result": ""}))
        return view

    async def run(self, arguments: dict[str, Any], *, signal=None, _authority=None) -> dict[str, Any]:
        session = self.session
        begun = time.monotonic()
        facts = {"executionId": uuid.uuid4().hex, "tool": "run_local", "input": arguments, "_begun": begun,
                 "startedAt": datetime.now(timezone.utc).isoformat(),
                 "purpose": arguments.get("purpose"), "status": "rejected", "exitCode": None,
                 "tool_was_executed": False, "stdout": "", "stderr": ""}
        try:
            if session.profile != "author" and _authority is not _FIXED_OPERATION:
                raise PermissionError("Run sessions execute fixed local operations only; "
                                      "arbitrary commands/scripts belong to workflow-create/edit")
            timeout = arguments.get("timeout_seconds", 120)
            if isinstance(timeout, bool) or not isinstance(timeout, int) or not 1 <= timeout <= _MAX_TIMEOUT_SECONDS:
                raise ValueError(f"timeout_seconds must be 1..{_MAX_TIMEOUT_SECONDS}")
            if not isinstance(arguments.get("purpose"), str) or not arguments["purpose"].strip():
                raise ValueError("purpose must describe this local operation")
            for key in ("read_paths", "write_paths"):
                if not isinstance(arguments.get(key), list) or any(not isinstance(p, str) or not p.strip()
                                                                 for p in arguments[key]):
                    raise ValueError(f"{key} must be an explicit array of paths")
            stdin = arguments.get("stdin", "")
            if not isinstance(stdin, str) or len(stdin.encode()) > _MAX_STDIN_BYTES:
                raise ValueError("stdin must be a string of at most 2 MiB")
            argv = _command(arguments)
            executable = Path(argv[0])
            if auth.protected(session, executable):
                raise PermissionError("protected executable path")
            if sys.platform != "darwin" or not Path("/usr/bin/sandbox-exec").is_file():
                facts.update(status="unavailable", error="OS sandbox backend unavailable; no process started")
                return self._save(facts)
            with session.waiting("local_authorization"):
                denied = await auth.authorize_tool_call(session, {"name": "run_local", "input": arguments})
            if denied:
                facts.update(status="rejected", authorization=denied)
                return self._save(facts)
            cwd = auth.resolve_authorized_path(session, arguments["cwd"], mode="read")
            if not cwd.is_dir():
                raise ValueError("cwd must be an existing directory; create it with local_fs_batch first")
            reads = list(dict.fromkeys([cwd, *(auth.resolve_authorized_path(session, p, mode="read")
                                              for p in arguments["read_paths"])]))
            writes = list(dict.fromkeys(auth.resolve_authorized_path(session, p, mode="write")
                                        for p in arguments["write_paths"]))
            runtime = runtime_roots(executable)
            facts.update(argv=argv, cwd=str(cwd), sandbox="macos-seatbelt", timeoutSeconds=timeout,
                         scope={"readPaths": list(map(str, reads)), "writePaths": list(map(str, writes)),
                                "runtimeReadRoots": runtime, "network": "denied"},
                         commandSha256=hashlib.sha256(json.dumps(argv, ensure_ascii=False).encode()).hexdigest())
            if self.closing or (signal is not None and signal.is_cancelled()) or session._stop_flag():
                facts.update(status="cancelled")
                return self._save(facts)
            await self._execute(facts, argv, cwd, reads, writes, runtime, stdin, timeout, signal)
        except asyncio.CancelledError:
            facts.update(status="cancelled")
            self._save(facts)
            raise
        except (OSError, ValueError, PermissionError) as exc:
            facts.update(status="failed" if facts["tool_was_executed"] else "rejected",
                         error=f"{type(exc).__name__}: {exc}")
        return self._save(facts)

    async def _execute(self, facts, argv, cwd, reads, writes, runtime, stdin, timeout, signal):
        # Host-only profile lives outside the guest's readable/writable temp dir.
        host_root = auth.task_root(self.session) / "workflow" / "local-processes"
        host_root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="exec-", dir=host_root) as directory:
            private = Path(directory).resolve()
            scratch = private / "tmp"
            scratch.mkdir()
            profile = sandbox_profile(self.session, reads=reads, writes=writes, runtime=runtime,
                                      scratch=scratch, executable=Path(argv[0]))
            policy_file = private / "scope.sb"
            policy_file.write_text(profile, encoding="utf-8")
            facts["sandboxProfile"] = profile
            env = {"PATH": os.pathsep.join(dict.fromkeys([str(Path(argv[0]).parent), "/usr/bin", "/bin",
                                                       "/opt/homebrew/bin", "/usr/local/bin"])),
                   "LANG": "en_US.UTF-8", "TMPDIR": str(scratch), "PYTHONDONTWRITEBYTECODE": "1",
                   "PYTHONNOUSERSITE": "1", "PYTHONSAFEPATH": "1", "PYTHONUTF8": "1",
                   "OPENSSL_CONF": "/dev/null"}
            process = await asyncio.create_subprocess_exec("/usr/bin/sandbox-exec", "-f", str(policy_file),
                *argv, cwd=cwd, env=env, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE, start_new_session=True, close_fds=True)
            key = facts["executionId"]
            self.active[key] = process
            facts.update(tool_was_executed=True, pid=process.pid)
            self.session.logger.write("workflow.local_execution.started", {
                "executionId": key, "pid": process.pid, "argv": argv, "cwd": str(cwd), "scope": facts["scope"]})
            buffers = {"stdout": bytearray(), "stderr": bytearray()}
            exceeded = asyncio.Event()

            async def drain(name, stream):
                while chunk := await stream.read(65536):
                    remaining = _CAPTURE_BYTES - len(buffers[name])
                    buffers[name].extend(chunk[:remaining])
                    if len(chunk) > remaining:
                        exceeded.set()

            async def feed():
                try:
                    process.stdin.write(stdin.encode())
                    await process.stdin.drain()
                except (BrokenPipeError, ConnectionResetError):
                    pass
                finally:
                    process.stdin.close()

            io = [asyncio.create_task(drain("stdout", process.stdout)),
                  asyncio.create_task(drain("stderr", process.stderr)), asyncio.create_task(feed())]
            waited = asyncio.create_task(process.wait())
            deadline = time.monotonic() + timeout
            try:
                while process.returncode is None:
                    if exceeded.is_set():
                        facts.update(status="output_limit", outputTruncated=True)
                        break
                    if self.closing or (signal is not None and signal.is_cancelled()) or self.session._stop_flag():
                        facts["status"] = "cancelled"
                        break
                    if time.monotonic() >= deadline:
                        facts["status"] = "timed_out"
                        break
                    await asyncio.wait({waited}, timeout=0.1)
                else:
                    facts["status"] = "cancelled" if self.closing else (
                        "completed" if process.returncode == 0 else "failed")
            finally:
                await _end_group(process)
                await asyncio.gather(waited, *io)
                self.active.pop(key, None)
                facts.update(exitCode=process.returncode,
                             **{name: data.decode("utf-8", "replace") for name, data in buffers.items()},
                             outputBytes={name: len(data) for name, data in buffers.items()})
                if exceeded.is_set():
                    facts.update(status="output_limit", outputTruncated=True)

    async def close(self):
        self.closing = True
        await asyncio.gather(*(_end_group(p) for p in list(self.active.values())), return_exceptions=True)
