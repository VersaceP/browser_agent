"""Workshop package files, content identity, packing and installed-runtime checks.

A package directory holds the WebCross metadata (workflow.json), the Harness
contract (contract.json), optional business-input schema, sources and offline
tests. ``contentHash`` covers exactly these managed entries; generated
exports and draft/history metadata are excluded. Source and metadata
changes, including a Workflow name change, produce a new version identity.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import uuid
from pathlib import Path, PurePosixPath
from typing import Any

MANAGED_FILES = ("workflow.json", "contract.json", "params.schema.json", "coverage.json", "README.md")
MANAGED_DIRS = ("src", "tests")
GENERATED_FILES = ("final.workshop.json", "publish.json")
SOURCE_SUFFIXES = (".js", ".ts", ".mjs", ".cjs")
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,62}[a-z0-9]$")
DECLARABLE_CAPABILITIES = ("File.write", "Runtime.evaluate")
_SKIPPED_PARTS = {"node_modules", "__pycache__"}


class PackageError(ValueError):
    """The package violates its file or contract format."""


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _visible(relative: str) -> bool:
    return not any(part.startswith(".") or part in _SKIPPED_PARTS for part in relative.split("/"))


def managed_files(root: Path) -> list[str]:
    """Sorted package-relative managed paths; symbolic links are rejected."""
    root = Path(root)
    found: list[str] = []
    for name in MANAGED_FILES:
        path = root / name
        if path.is_symlink():
            raise PackageError(f"{name} must not be a symbolic link")
        if path.is_file():
            found.append(name)
    for directory in MANAGED_DIRS:
        base = root / directory
        if base.is_symlink():
            raise PackageError(f"{directory}/ must not be a symbolic link")
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*")):
            relative = path.relative_to(root).as_posix()
            if not _visible(relative):
                continue
            if path.is_symlink():
                raise PackageError(f"{relative} must not be a symbolic link")
            if path.is_file():
                found.append(relative)
    return sorted(found)


def content_hash(root: Path) -> str:
    root = Path(root)
    entries = [{"path": name, "sha256": _sha((root / name).read_bytes())} for name in managed_files(root)]
    return _sha(json.dumps(entries, sort_keys=True, separators=(",", ":")).encode())


def file_inventory(root: Path) -> list[dict[str, Any]]:
    root = Path(root)
    return [{"path": name, "bytes": (root / name).stat().st_size,
             "sha256": _sha((root / name).read_bytes())} for name in managed_files(root)]


def read_json(path: Path) -> Any:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise PackageError(f"{Path(path).name} is missing") from None
    except ValueError as exc:
        raise PackageError(f"{Path(path).name} is not valid JSON: {exc}") from None


def load_metadata(root: Path) -> dict[str, Any]:
    meta = read_json(Path(root) / "workflow.json")
    if not isinstance(meta, dict):
        raise PackageError("workflow.json must be an object")
    if meta.get("schemaVersion") != 1:
        raise PackageError("workflow.json schemaVersion must be 1")
    for key in ("name", "entry"):
        if not isinstance(meta.get(key), str) or not meta[key].strip():
            raise PackageError(f"workflow.json {key} must be a nonempty string")
    if "files" in meta or "kind" in meta:
        raise PackageError("workflow.json holds metadata only; sources live in src/")
    timeout = meta.get("timeoutMs")
    if timeout is not None and (isinstance(timeout, bool) or not isinstance(timeout, int)
                                or not 1000 <= timeout <= 3600000):
        raise PackageError("workflow.json timeoutMs must be an integer between 1000 and 3600000")
    entry = PurePosixPath(meta["entry"])
    if entry.parts[:1] != ("src",) or ".." in entry.parts or not meta["entry"].endswith(SOURCE_SUFFIXES):
        raise PackageError("workflow.json entry must name a JS/TS module under src/")
    if not (Path(root) / meta["entry"]).is_file():
        raise PackageError(f"workflow.json entry {meta['entry']} does not exist")
    return meta


def load_contract(root: Path) -> dict[str, Any]:
    contract = read_json(Path(root) / "contract.json")
    if not isinstance(contract, dict) or contract.get("schemaVersion") != 1:
        raise PackageError("contract.json must be an object with schemaVersion 1")
    try:
        uuid.UUID(str(contract.get("templateId")))
    except ValueError:
        raise PackageError("contract.json templateId must be a UUID") from None
    slug = contract.get("slug")
    if not isinstance(slug, str) or not SLUG_RE.fullmatch(slug):
        raise PackageError("contract.json slug must be 3-64 lowercase letters, digits or hyphens")
    capabilities = contract.get("capabilities", {})
    if not isinstance(capabilities, dict) or set(capabilities) - set(DECLARABLE_CAPABILITIES):
        raise PackageError("contract.json capabilities may declare only " + ", ".join(DECLARABLE_CAPABILITIES))
    stages = contract.get("stages", [])
    if not isinstance(stages, list) or any(not isinstance(s, dict) or not isinstance(s.get("id"), str)
                                           for s in stages):
        raise PackageError("contract.json stages must be a list of objects with an id")
    if len({s["id"] for s in stages}) != len(stages):
        raise PackageError("contract.json stage ids must be unique")
    from harness.workflow_mode.protocol import validate_declarations
    try:
        validate_declarations(contract)
    except ValueError as exc:
        raise PackageError(str(exc)) from exc
    stage_entry = (contract.get("stageProtocol") or {}).get("entry")
    if stage_entry is not None:
        path = safe_relative(root, stage_entry)
        if not stage_entry.startswith("src/") or not path.is_file():
            raise PackageError("stageProtocol.entry must be an existing source module under src/")
    preflight = contract.get("preflight")
    if preflight is not None:
        if not isinstance(preflight, dict) or not isinstance(preflight.get("entry"), str):
            raise PackageError("contract.json preflight needs an entry under src/")
        name = preflight["entry"]
        path = safe_relative(root, name)
        if not name.startswith("src/") or not name.endswith((".js", ".ts")) or not path.is_file():
            raise PackageError("contract.json preflight.entry must name an existing JS/TS file under src/")
    schema_path = Path(root) / "params.schema.json"
    if schema_path.exists() and not isinstance(read_json(schema_path), dict):
        raise PackageError("params.schema.json must be a JSON Schema object")
    from harness.workflow_mode import coverage
    try:
        coverage.read(root)
    except (OSError, ValueError) as exc:
        raise PackageError(str(exc)) from exc
    return contract


def source_files(root: Path) -> dict[str, str]:
    """Package-relative sources that the platform compiles (src/ only)."""
    root = Path(root)
    files: dict[str, str] = {}
    for name in managed_files(root):
        if name.startswith("src/"):
            try:
                files[name] = (root / name).read_text(encoding="utf-8")
            except UnicodeError as exc:
                raise PackageError(f"Source module {name} is not UTF-8 text") from exc
    if not files:
        raise PackageError("src/ contains no JS/TS sources")
    return files


def execution_document(root: Path) -> dict[str, Any]:
    """The Workflow.execute document; capabilities are Harness admission data."""
    meta = load_metadata(root)
    contract = load_contract(root)
    document = {key: meta[key] for key in ("schemaVersion", "name", "description", "entry", "timeoutMs")
                if key in meta}
    document["files"] = source_files(root)
    if contract.get("capabilities"):
        document["capabilities"] = json.loads(json.dumps(contract["capabilities"]))
    return document


def workshop_document(root: Path) -> dict[str, Any]:
    """Flat webcross-workshop document, matching ``webcross workflow pack``."""
    meta = load_metadata(root)
    return {"kind": "webcross-workshop", **meta, "files": source_files(root)}


def write_workshop(root: Path, destination: Path) -> dict[str, Any]:
    document = workshop_document(root)
    text = json.dumps(document, ensure_ascii=False, indent=2) + "\n"
    Path(destination).write_text(text, encoding="utf-8")
    return {"path": str(destination), "modules": len(document["files"]), "bytes": len(text.encode()),
            "sha256": _sha(text.encode())}


def check_structure(root: Path) -> dict[str, Any]:
    """Format facts; never proof that the program works on a page."""
    from harness.workflow_mode import coverage
    try:
        meta = load_metadata(root)
        contract = load_contract(root)
        files = source_files(root)
    except PackageError as exc:
        return {"status": "invalid", "error": str(exc)}
    from harness.workflow.offline_runtime import check_limits, reachable
    reached = reachable(files, meta["entry"])
    problem = check_limits(reached, {})
    unreferenced = sorted(set(files) - set(reached))
    return {"status": "invalid" if problem else "valid", "error": problem, "entry": meta["entry"],
            "modules": len(files), "reachableModules": len(reached),
            "unreferencedSources": unreferenced, "templateId": contract["templateId"],
            "slug": contract["slug"], "stages": [s["id"] for s in contract.get("stages", [])],
            "capabilities": sorted(contract.get("capabilities", {})), "coverage": coverage.summary(root)}


_CHECK_RUNNER = r"""
import fs from 'node:fs'; import os from 'node:os'; import path from 'node:path';
import {pathToFileURL} from 'node:url';
const runtime = process.env.WEBCROSS_RUNTIME_MODULES;
const doc = JSON.parse(fs.readFileSync(process.argv[1], 'utf8'));
const {parseWorkshopManifest} = await import(pathToFileURL(path.join(runtime, 'workshop/dist/index.js')));
const manifest = parseWorkshopManifest(doc);
const {kind, availability, ...workflow} = manifest;
const previous = process.cwd(), temporary = fs.mkdtempSync(path.join(os.tmpdir(), 'workflow-compile-'));
try {
  process.chdir(temporary);
  const {compileWorkflow} = await import(pathToFileURL(path.join(runtime, 'dispatcher/dist/runtime/workflowCompiler.js')));
  const compiled = await compileWorkflow(workflow);
  process.stdout.write(JSON.stringify({workshopValidated: true, entry: workflow.entry,
    modules: Object.keys(workflow.files).length, compiledBytes: compiled.code.length}));
} finally { process.chdir(previous); fs.rmSync(temporary, {recursive: true, force: true}); }
"""

DEFAULT_RUNTIME_MODULES = "/Applications/WebCross.app/Contents/Resources/app.asar.unpacked/dist/node_modules/@abcp"


def installed_runtime() -> dict[str, Any] | None:
    modules = Path(os.environ.get("WEBCROSS_RUNTIME_MODULES") or DEFAULT_RUNTIME_MODULES)
    node = shutil.which("node")
    if not node or not (modules / "workshop" / "dist" / "index.js").is_file():
        return None
    return {"node": node, "modules": str(modules)}


def check_compile(root: Path, *, timeout_s: float = 120.0) -> dict[str, Any]:
    """Validate and compile with the installed WebCross Workshop validator."""
    runtime = installed_runtime()
    if runtime is None:
        return {"status": "unavailable",
                "error": "Installed WebCross Workshop validator or node not found; set WEBCROSS_RUNTIME_MODULES."}
    try:
        document = workshop_document(root)
    except PackageError as exc:
        return {"status": "invalid", "error": str(exc)}
    with tempfile.TemporaryDirectory(prefix="workflow-check-") as directory:
        path = Path(directory) / "package.workshop.json"
        path.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")
        env = {"PATH": os.environ.get("PATH", ""), "HOME": os.environ.get("HOME", ""),
               "WEBCROSS_RUNTIME_MODULES": runtime["modules"], "NODE_OPTIONS": ""}
        try:
            done = subprocess.run([runtime["node"], "--input-type=module", "-e", _CHECK_RUNNER, str(path)],
                                  capture_output=True, text=True, timeout=timeout_s, env=env)
        except subprocess.TimeoutExpired:
            return {"status": "invalid", "error": f"Installed validator did not finish within {timeout_s}s"}
    if done.returncode != 0:
        return {"status": "invalid", "error": (done.stderr or done.stdout)[-4000:]}
    try:
        return {"status": "valid", **json.loads(done.stdout)}
    except ValueError:
        return {"status": "invalid", "error": "Validator returned unreadable output: " + done.stdout[-500:]}


def check(root: Path) -> dict[str, Any]:
    structure = check_structure(root)
    if structure["status"] != "valid":
        return {"status": "invalid", "structure": structure}
    compiled = check_compile(root)
    from harness.workflow_mode.protocol import check_stages
    stages = check_stages(root, load_contract(root), source_files(root))
    status = compiled["status"]
    if status == "valid" and stages["status"] not in {"valid", "legacy"}:
        status = stages["status"]
    return {"status": status, "structure": structure, "compile": compiled, "stageProtocol": stages,
            "contentHash": content_hash(root)}


def copy_managed(source: Path, destination: Path) -> list[str]:
    """Copy managed entries only; destination must not already hold them."""
    names = managed_files(source)
    for name in names:
        target = Path(destination) / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(Path(source) / name, target)
    return names


def safe_relative(root: Path, raw: str, *, writable: bool = False) -> Path:
    """Resolve a package-relative path inside the managed set."""
    text = str(raw or "").strip().replace("\\", "/")
    relative = PurePosixPath(text)
    if (not text or relative.is_absolute() or ".." in relative.parts
            or any(part.startswith(".") for part in relative.parts)):
        raise PackageError("Use a package-relative path without '..' or hidden parts")
    name = relative.as_posix()
    if name in GENERATED_FILES:
        raise PackageError(f"{name} is generated by /workflow-publish")
    if writable and name not in MANAGED_FILES and relative.parts[0] not in MANAGED_DIRS:
        raise PackageError("Write workflow.json, contract.json, params.schema.json, coverage.json, README.md, src/** or tests/**")
    path = Path(root) / name
    resolved_root = Path(root).resolve()
    if path.exists() and not path.resolve().is_relative_to(resolved_root):
        raise PackageError("Path escapes the package directory")
    return path


def new_contract(slug: str, *, summary: str = "") -> dict[str, Any]:
    if not SLUG_RE.fullmatch(slug):
        raise PackageError("slug must be 3-64 lowercase letters, digits or hyphens")
    return {"schemaVersion": 1, "templateId": str(uuid.uuid4()), "slug": slug, "summary": summary,
            "inputs": {"description": "Describe business input fields, or ship params.schema.json."},
            "stages": [], "report": {"description": "Describe returned facts, unresolved items and resume data."},
            "capabilities": {}, "verifiedScope": [], "knownLimitations": []}
