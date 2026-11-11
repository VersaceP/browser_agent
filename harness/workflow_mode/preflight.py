"""Local input facts and optional package-owned pure checks; no browser actions.

The Harness supplies authorized file metadata and checks the report protocol.
The package owns applicable requirements; the Agent owns semantic decisions.
"""
from __future__ import annotations

import hashlib
import json
import os
import stat
import struct
import subprocess
import warnings
from pathlib import Path
from typing import Any

from harness.workflow import offline_runtime
from harness.workflow_mode import package as pkg


def _header_metadata(path: Path) -> dict[str, Any] | None:
    """Bounded header observations for common formats; never decode pixels."""
    with path.open("rb") as handle:
        header = handle.read(32)
        fmt, width, height = None, 0, 0
        if header.startswith(b"\x89PNG\r\n\x1a\n") and header[8:16] == b"\x00\x00\x00\rIHDR" and len(header) >= 24:
            fmt, (width, height) = "PNG", struct.unpack(">II", header[16:24])
        elif header[:6] in {b"GIF87a", b"GIF89a"} and len(header) >= 10:
            fmt, (width, height) = "GIF", struct.unpack("<HH", header[6:10])
        elif header[:4] == b"RIFF" and header[8:12] == b"WEBP" and len(header) >= 30:
            fmt = "WEBP"
            if header[12:16] == b"VP8X":
                width = 1 + int.from_bytes(header[24:27], "little")
                height = 1 + int.from_bytes(header[27:30], "little")
            elif header[12:16] == b"VP8 " and header[23:26] == b"\x9d\x01\x2a":
                width, height = (v & 0x3fff for v in struct.unpack("<HH", header[26:30]))
            elif header[12:16] == b"VP8L" and header[20] == 0x2f:
                bits = int.from_bytes(header[21:25], "little")
                width, height = 1 + (bits & 0x3fff), 1 + ((bits >> 14) & 0x3fff)
        elif header.startswith(b"\xff\xd8"):
            handle.seek(2)
            # Large EXIF/ICC headers are not an invalid-image verdict at this budget.
            while handle.tell() < 131072:
                if handle.read(1) != b"\xff":
                    break
                marker = handle.read(1)
                while marker == b"\xff" and handle.tell() < 131072:
                    marker = handle.read(1)
                if not marker or marker in {b"\xda", b"\xd9"}:
                    break
                if marker[0] in {0x01, *range(0xd0, 0xd9)}:
                    continue
                raw = handle.read(2)
                if len(raw) != 2:
                    break
                length = int.from_bytes(raw, "big")
                if length < 2 or handle.tell() + length - 2 > 131072:
                    break
                if marker[0] in {0xc0, 0xc1, 0xc2, 0xc3, 0xc5, 0xc6, 0xc7,
                                 0xc9, 0xca, 0xcb, 0xcd, 0xce, 0xcf} and length >= 8:
                    raw = handle.read(5)
                    if len(raw) == 5:
                        height, width = struct.unpack(">HH", raw[1:5])
                        fmt = "JPEG"
                    break
                handle.seek(length - 2, 1)
    if not fmt or not width or not height:
        return None
    mime = {"PNG": "image/png", "JPEG": "image/jpeg", "GIF": "image/gif", "WEBP": "image/webp"}
    return {"status": "observed", "format": fmt, "width": width, "height": height,
            "mimeType": mime[fmt], "coverage": "header_only", "reader": "bounded_header"}


def _image_metadata(path: Path) -> dict[str, Any]:
    header = _header_metadata(path)
    if header:
        return header
    try:
        from PIL import Image
    except ImportError:
        return {"status": "unconfirmed", "error": "Header not recognized within budget; optional Pillow reader unavailable"}
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(path) as image:
                return {"status": "observed", "format": image.format, "width": image.width,
                        "height": image.height, "mimeType": Image.MIME.get(image.format),
                        "coverage": "header_only", "reader": "Pillow"}
    except (OSError, ValueError, SyntaxError, Image.DecompressionBombWarning,
            Image.DecompressionBombError) as exc:
        return {"status": "unconfirmed", "error": str(exc),
                "note": "Image metadata unavailable; this is not a business verdict."}


def file_facts(owner: Any, files: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Inspect every explicitly named input, without granting access or guessing roles."""
    from harness.tools.path_authorization import resolve_authorized_path
    facts = []
    for item in files:
        fact: dict[str, Any] = {"path": item["path"], "kind": item.get("kind", "file"),
                                **{key: item[key] for key in ("role", "source") if key in item}}
        facts.append(fact)
        try:
            path = resolve_authorized_path(owner, item["path"], mode="read")
        except (OSError, ValueError) as exc:
            fact.update(status="unconfirmed", error=str(exc))
            continue  # Never stat/read a denied path, even to test existence.
        fact["canonicalPath"] = str(path)
        try:
            info = path.stat()
            if not stat.S_ISREG(info.st_mode):
                fact.update(status="not_file")
                continue
            fact.update(status="observed", bytes=info.st_size, modifiedNs=info.st_mtime_ns)
            if fact["kind"] == "image":
                # Read common headers, optionally other formats; never infer MIME from a suffix.
                fact["image"] = _image_metadata(path)
            after = path.stat()
            if (after.st_size, after.st_mtime_ns) != (info.st_size, info.st_mtime_ns):
                fact.update(status="unconfirmed", error="File changed while reading metadata")
        except FileNotFoundError:
            fact.update(status="missing")
        except OSError as exc:
            fact.update(status="unconfirmed", error=str(exc))
    return facts


_FILE_FACT_FIELDS = ("status", "canonicalPath", "bytes", "modifiedNs")
_IMAGE_FACT_FIELDS = ("status", "format", "width", "height", "mimeType", "coverage", "reader")


def compare_file_facts(previous: Any, current: list[dict[str, Any]],
                       previous_ref: str | None) -> dict[str, Any]:
    """Metadata deltas only; the Agent judges whether derived business input is stale."""
    fields = [*_FILE_FACT_FIELDS, "image"]
    if (not isinstance(previous, list) or len(previous) != len(current)
            or any(not isinstance(old, dict) or old.get("path") != new.get("path")
                   for old, new in zip(previous, current))):
        return {"status": "unavailable", "changedCount": None, "items": [],
                "reason": "No comparable saved file facts for the same declared manifest", "fieldsCompared": fields}
    def values(fact):
        value = {key: fact.get(key) for key in _FILE_FACT_FIELDS}
        image = fact.get("image")
        value["image"] = ({key: image.get(key) for key in _IMAGE_FACT_FIELDS}
                          if isinstance(image, dict) else None)
        return value
    changed = []
    for index, (old, new) in enumerate(zip(previous, current)):
        before, after = values(old), values(new)
        delta = {key: {"before": before[key], "after": after[key]}
                 for key in fields if before[key] != after[key]}
        if delta:
            changed.append({"path": new["path"], "changes": delta,
                            "previous": {"ref": previous_ref, "pointer": f"/files/{index}"},
                            "currentPointer": f"/files/{index}"})
    return {"status": "compared", "changedCount": len(changed), "items": changed,
            "fieldsCompared": fields,
            "interpretation": "Observed metadata changes only; no input rewrite or business verdict. Unchanged metadata does not prove identical file content."}


_MAX_MANIFEST_BYTES = 1024 * 1024


def read_material_manifest(owner: Any, source: dict[str, Any]) -> tuple[Any, dict[str, Any]]:
    """Read an explicitly supplied manifest after authorization, with bounded bytes."""
    from harness.tools.path_authorization import resolve_authorized_path
    path = resolve_authorized_path(owner, source["path"], mode="read")
    # No existence, size or content probe before the permission check above.
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
    with os.fdopen(descriptor, "rb") as stream:
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode):
            raise ValueError("Material manifest must be a regular file")
        data = stream.read(_MAX_MANIFEST_BYTES + 1)
        after = os.fstat(stream.fileno())
    current = path.stat(follow_symlinks=False)
    identity = lambda value: (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns)
    if identity(before) != identity(after) or identity(after) != identity(current):
        raise ValueError("Material manifest changed while reading; acquire a current copy")
    if len(data) > _MAX_MANIFEST_BYTES:
        raise ValueError("Material manifest exceeds the 1 MiB read limit")
    digest = hashlib.sha256(data).hexdigest()
    if "sha256" in source and digest != source["sha256"]:
        raise ValueError("Material manifest does not match its SHA-256")
    try:
        value = json.loads(data.decode("utf-8"))
    except (UnicodeError, ValueError) as exc:
        raise ValueError("Material manifest is not UTF-8 JSON") from exc
    return value, {"path": str(path), "sha256": digest, "bytes": len(data),
                   "modifiedNs": after.st_mtime_ns, "coverage": "manifest read only; files rechecked separately"}


def run(root: Path, business_input: dict[str, Any], files: list[dict[str, Any]],
        *, timeout_s: float = 30.0) -> dict[str, Any]:
    """Run a declared pure checker in WebCross's offline VM with no action host."""
    entry = (pkg.load_contract(root).get("preflight") or {}).get("entry")
    if not entry:
        return {"status": "not_declared", "items": []}
    runner = "__harness_preflight.js"
    source = ("import check from " + json.dumps("./" + entry) + ";\n"
              "export default async function main(api, value) {\n"
              "  return await check(value.input, {files: value.files});\n}\n")
    sources = offline_runtime.reachable({**pkg.source_files(root), runner: source}, runner)
    inputs = {"input": business_input, "files": files}
    problem = offline_runtime.check_limits(sources, inputs)
    if problem:
        return {"status": "unconfirmed", "error": problem, "items": []}
    job = {"workflow": {"schemaVersion": 1, "name": "local-preflight", "entry": runner,
                        "timeoutMs": 20000, "files": sources}, "input": inputs}
    try:
        outcome = offline_runtime.run_jobs([job], timeout_s=timeout_s)[0]
    except (OSError, RuntimeError, ValueError, subprocess.TimeoutExpired) as exc:
        return {"status": "unconfirmed", "error": str(exc), "items": []}
    if outcome.get("status") != "succeeded":
        return {"status": "unconfirmed", "error": outcome.get("error"), "items": []}
    output = outcome.get("output")
    items = output.get("items") if isinstance(output, dict) else None
    if not isinstance(items, list) or any(
        not isinstance(item, dict) or not isinstance(item.get("key"), str) or not item["key"]
        or item.get("status") not in {"pass", "fail", "unknown"} for item in items
    ):
        return {"status": "unconfirmed", "error": "preflight must return {items:[{key,status:pass|fail|unknown,...}]}",
                "items": [], "reported": output}
    if len({item["key"] for item in items}) != len(items):
        return {"status": "unconfirmed", "error": "preflight item keys must be unique", "items": items}
    counts = {name: sum(item["status"] == name for item in items) for name in ("pass", "fail", "unknown")}
    return {"status": "issues" if counts["fail"] else "unconfirmed" if counts["unknown"] or not items else "passed",
            "counts": counts, "items": items,
            "interpretation": "Package check claims, not business acceptance or root-cause classification."}


STARTER = '''// Pure, package-owned checks. Use the same rules before browser side effects.
// No fs/network/browser API here. context.files contains authorized local metadata.
export default function preflight(input, context = {}) {
  return {items: [{key: 'requirements', status: 'unknown',
    reason: 'Replace this placeholder with checks derived from the goal and verified platform requirements.'}]};
}
'''
