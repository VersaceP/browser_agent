"""
harness.schema_loader - Capability discovery and per-method schema loading.

System.getCapabilities returns method names and descriptions but NOT the
full parameter schema (paramsSchema is null for every method on current
ABCP builds). To know which methods require `purpose`, what fields are
required, and what hints to use when auto-filling, we must enumerate the
capability list and call System.describeAction per method.

Current servers return ``agentGuide`` plus catalog/guide revisions from
System.getCapabilities. The earlier ``skillsGuide`` response and
``System.skillsDoc`` pseudo-capability remain readable for compatibility.

This module is the single source of truth for that two-call bootstrap.
"""

import asyncio
import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, FrozenSet, Iterable, List, Optional, Set

from abcp_client import ABCPClient, ABCPTransportError
from harness.utils import JsonDict, RunLogger


SKILLS_DOC_CAPABILITY = "System.skillsDoc"


@dataclass
class CapabilityBundle:
    """All capability/schema info derived at bootstrap. Passed by reference
    to BrowserAgent and prompt builders."""

    capabilities: List[JsonDict] = field(default_factory=list)
    capability_methods: Set[str] = field(default_factory=set)
    method_schemas: Dict[str, JsonDict] = field(default_factory=dict)
    methods_requiring_purpose: Set[str] = field(default_factory=set)
    purpose_hints: Dict[str, str] = field(default_factory=dict)
    skills_doc: str = ""
    catalog_revision: str = ""
    guide_revision: str = ""


async def load_capability_bundle(
    browser: ABCPClient,
    *,
    logger: RunLogger,
    blocked_methods: Iterable[str] = (),
    schemas_dir: Optional[Path] = None,
    schema_cache_dir: Optional[Path] = None,
    describe_concurrency: int = 8,
    caps_response: Optional[JsonDict] = None,
    prune_schema_cache: bool = True,
) -> CapabilityBundle:
    """Discover all server capabilities and load their schemas.

    Steps:
      1. System.getCapabilities — enumerate methods, harvest the skillsDoc
         markdown shipped via the System.skillsDoc pseudo-capability.
      2. For every real method (skipping the pseudo-capability and any
         entry in `blocked_methods`), load cached System.describeAction output
         from `schema_cache_dir` when available, otherwise call
         System.describeAction.
      3. Build the indices the rest of the harness consumes.
      4. If `schemas_dir` is set, persist one JSON per method for offline
         debugging and ad-hoc local_fs_search recall.
    """
    started = time.monotonic()
    describe_elapsed_ms = 0
    bundle = CapabilityBundle()
    blocked: FrozenSet[str] = frozenset(blocked_methods)

    # A long-lived slot has already completed its connection-level capability
    # handshake before it may issue Fleet/Page business actions.  Reuse that
    # exact response here so the worker bundle loader does not send a second,
    # delayed System.getCapabilities request after the slot is already active.
    if caps_response is None:
        caps_response = await browser.call(
            "System.getCapabilities", {"guide": "omit"}
        )
    raw_capabilities = _capability_actions_from_response(caps_response)
    bundle.skills_doc = _skills_doc_from_capabilities_response(caps_response)
    revisions = _capability_revisions_from_response(caps_response)
    bundle.catalog_revision = revisions["catalogRevision"]
    bundle.guide_revision = revisions["guideRevision"]
    if not raw_capabilities:
        return bundle

    methods_to_describe: List[str] = []
    for cap in raw_capabilities:
        if not isinstance(cap, dict):
            continue
        method = str(cap.get("method") or "").strip()
        if not method:
            continue
        if method == SKILLS_DOC_CAPABILITY:
            # Pseudo-capability: the description IS the markdown manual.
            description = str(cap.get("description") or "")
            if description.strip():
                bundle.skills_doc = description
            continue
        if method in blocked:
            continue
        bundle.capabilities.append(cap)
        bundle.capability_methods.add(method)
        methods_to_describe.append(method)

    pruned_schema_cache_files = (
        _prune_stale_cached_schemas(
            schema_cache_dir,
            bundle.capability_methods,
        )
        if prune_schema_cache
        else 0
    )

    cache_read_started = time.monotonic()
    methods_missing_cache: List[str] = []
    capabilities_by_method = {
        str(cap.get("method") or "").strip(): cap
        for cap in bundle.capabilities
        if isinstance(cap, dict) and str(cap.get("method") or "").strip()
    }
    for method in methods_to_describe:
        cached_schema = _read_cached_schema(schema_cache_dir, method)
        live_capability = capabilities_by_method.get(method) or {}
        live_revision = str(live_capability.get("actionRevision") or "").strip()
        cached_revision = str(
            (cached_schema.get("actionRevision") or "")
            if isinstance(cached_schema, dict)
            else ""
        ).strip()
        # A revision mismatch is a targeted cache miss. When the live catalog
        # does not advertise revisions (legacy Dispatcher), retain the old
        # digest/generation-based cache behavior.
        if cached_schema is None or (
            live_revision and cached_revision != live_revision
        ):
            methods_missing_cache.append(method)
            continue
        _ingest_method_schema(bundle, method, cached_schema)
    cache_read_elapsed_ms = int((time.monotonic() - cache_read_started) * 1000)

    if methods_missing_cache:
        describe_started = time.monotonic()
        semaphore = asyncio.Semaphore(max(1, describe_concurrency))

        async def describe(method: str) -> None:
            async with semaphore:
                try:
                    resp = await browser.call(
                        "System.describeAction", {"method": method}
                    )
                except ABCPTransportError as exc:
                    logger.write(
                        "schema.describeAction.error",
                        {"method": method, "error": str(exc)},
                    )
                    return
            data = resp.get("data") if isinstance(resp, dict) else None
            if not isinstance(data, dict):
                return
            live_revision = str(
                (capabilities_by_method.get(method) or {}).get("actionRevision")
                or ""
            ).strip()
            if live_revision and not data.get("actionRevision"):
                data = dict(data)
                data["actionRevision"] = live_revision
            _ingest_method_schema(bundle, method, data)

        # describeAction is read-only; describe in parallel (bounded) so
        # bootstrap latency scales with the slowest method, not the total.
        await asyncio.gather(*(describe(m) for m in methods_missing_cache))
        describe_elapsed_ms = int((time.monotonic() - describe_started) * 1000)

    if schemas_dir is not None and bundle.method_schemas:
        schemas_dir.mkdir(parents=True, exist_ok=True)
        for method, schema in bundle.method_schemas.items():
            safe = method.replace("/", "_")
            (schemas_dir / f"{safe}.json").write_text(
                json.dumps(schema, ensure_ascii=False, indent=2, default=str),
                encoding="utf-8",
            )

    logger.write(
        "schema.bundle.loaded",
        {
            "capability_count": len(bundle.capability_methods),
            "schema_count": len(bundle.method_schemas),
            "requires_purpose_count": len(bundle.methods_requiring_purpose),
            "skills_doc_chars": len(bundle.skills_doc),
            "catalogRevision": bundle.catalog_revision or None,
            "guideRevision": bundle.guide_revision or None,
            "schemas_dir": str(schemas_dir) if schemas_dir else None,
            "schema_cache_dir": str(schema_cache_dir) if schema_cache_dir else None,
            "schema_cache_hits": len(methods_to_describe) - len(methods_missing_cache),
            "schema_cache_misses": len(methods_missing_cache),
            "schema_cache_pruned": pruned_schema_cache_files,
            "cache_read_elapsed_ms": cache_read_elapsed_ms,
            "describe_elapsed_ms": describe_elapsed_ms,
            "elapsed_ms": int((time.monotonic() - started) * 1000),
        },
    )
    return bundle


def _capability_actions_from_response(response: Any) -> List[JsonDict]:
    """Return callable capability entries from old and new ABCP shapes.

    Older builds returned ``data`` as a bare list of capability dictionaries.
    Current builds return ``data.actions`` and put the skills guide beside it
    as ``data.skillsGuide``. The harness must accept both shapes; otherwise the
    schema bundle is empty and purpose auto-fill / method discovery silently
    degrade.
    """
    if not isinstance(response, dict):
        return []
    data = response.get("data")
    raw_actions: Any
    if isinstance(data, list):
        raw_actions = data
    elif isinstance(data, dict):
        raw_actions = data.get("actions")
    else:
        raw_actions = []
    if not isinstance(raw_actions, list):
        return []
    return [item for item in raw_actions if isinstance(item, dict)]


def _skills_doc_from_capabilities_response(response: Any) -> str:
    if not isinstance(response, dict):
        return ""
    data = response.get("data")
    if isinstance(data, dict):
        guide = data.get("agentGuide")
        if guide is None:
            guide = data.get("skillsGuide")
        if isinstance(guide, dict):
            value = guide.get("value")
            if isinstance(value, str) and value.strip():
                return value
        if isinstance(guide, str) and guide.strip():
            return guide
    return ""


def _capability_revisions_from_response(response: Any) -> JsonDict:
    data = response.get("data") if isinstance(response, dict) else None
    if not isinstance(data, dict):
        return {"catalogRevision": "", "guideRevision": ""}
    return {
        "catalogRevision": str(data.get("catalogRevision") or "").strip(),
        "guideRevision": str(data.get("guideRevision") or "").strip(),
    }


def _schema_cache_path(schema_cache_dir: Optional[Path], method: str) -> Optional[Path]:
    if schema_cache_dir is None:
        return None
    safe = method.replace("/", "_")
    return schema_cache_dir / f"{safe}.json"


def _prune_stale_cached_schemas(
    schema_cache_dir: Optional[Path],
    live_methods: Set[str],
) -> int:
    if schema_cache_dir is None:
        return 0
    if not schema_cache_dir.exists() or not schema_cache_dir.is_dir():
        return 0
    pruned = 0
    for path in schema_cache_dir.glob("*.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            data = {}
        method = str(data.get("method") or path.stem).strip()
        if method and method in live_methods:
            continue
        try:
            path.unlink()
            pruned += 1
        except OSError:
            pass
    return pruned


def _read_cached_schema(schema_cache_dir: Optional[Path], method: str) -> Optional[JsonDict]:
    path = _schema_cache_path(schema_cache_dir, method)
    if path is None or not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _ingest_method_schema(
    bundle: CapabilityBundle,
    method: str,
    schema: JsonDict,
) -> None:
    bundle.method_schemas[method] = schema
    if schema.get("requiresPurpose") is True:
        bundle.methods_requiring_purpose.add(method)
    hint = schema.get("purposeHint")
    if isinstance(hint, str) and hint.strip():
        bundle.purpose_hints[method] = hint.strip()


def _schema_object_variants(schema: JsonDict) -> List[JsonDict]:
    """Object-variant views of a describeAction schema.

    Two describeAction generations exist:

    - Legacy agent view: a flat ``params`` map of per-name specs where each
      spec carries its own ``required`` boolean.
    - JSON-Schema views (``inputSchema`` or a bare schema): either a plain
      object schema (``properties`` + ``required`` list) or a union whose top
      level is ``anyOf``/``oneOf`` over object branches (e.g. Download.start's
      direct-URL vs page-reservation variants).

    Returns one ``{properties, required}`` dict per object branch; empty for
    shapes that expose neither.
    """
    if not isinstance(schema, dict):
        return []
    source = schema.get("inputSchema") if isinstance(schema.get("inputSchema"), dict) else schema

    def variants_of(node: JsonDict) -> List[JsonDict]:
        properties = node.get("properties")
        if isinstance(properties, dict):
            required_raw = node.get("required")
            required = {
                str(name)
                for name in (required_raw if isinstance(required_raw, list) else [])
                if isinstance(name, str)
            }
            return [{"properties": properties, "required": required}]
        for keyword in ("anyOf", "oneOf"):
            branches = node.get(keyword)
            if isinstance(branches, list):
                collected: List[JsonDict] = []
                for branch in branches:
                    if isinstance(branch, dict):
                        collected.extend(variants_of(branch))
                if collected:
                    return collected
        return []

    return variants_of(source)


def schema_param_specs(schema: JsonDict) -> Dict[str, JsonDict]:
    """Normalized per-parameter specs across schema generations.

    Returns ``{name: spec}`` where every spec carries a boolean ``required``.
    For union schemas a name is required only when EVERY branch requires it
    (mirroring the platform's own agent-view merge), and differing branch
    specs are preserved under ``spec["anyOf"]``.
    """
    if not isinstance(schema, dict):
        return {}
    params = schema.get("params")
    if isinstance(params, dict) and params:
        return {
            str(name): dict(spec)
            for name, spec in params.items()
            if isinstance(spec, dict)
        }
    variants = _schema_object_variants(schema)
    if not variants:
        return {}
    # Two passes: names must be collected across ALL branches first, so a
    # name absent from an earlier branch still receives a not-required flag
    # for that branch ("required" = required in every branch, mirroring the
    # platform's merged agent view).
    all_names: List[str] = []
    seen_names: Set[str] = set()
    for variant in variants:
        for name in variant["properties"]:
            if name not in seen_names:
                seen_names.add(name)
                all_names.append(str(name))
    branch_specs: Dict[str, List[JsonDict]] = {}
    branch_required: Dict[str, List[bool]] = {}
    for variant in variants:
        properties = variant["properties"]
        for name in all_names:
            spec = properties.get(name)
            branch_specs.setdefault(name, []).append(
                spec if isinstance(spec, dict) else {}
            )
            branch_required.setdefault(name, []).append(
                isinstance(spec, dict) and name in variant["required"]
            )
    merged: Dict[str, JsonDict] = {}
    for name in all_names:
        name_specs = branch_specs.get(name) or [{}]
        distinct = []
        seen = set()
        for spec in name_specs:
            key = json.dumps(spec, sort_keys=True, default=str)
            if key not in seen:
                seen.add(key)
                distinct.append(spec)
        spec = dict(distinct[0])
        if len(distinct) > 1:
            spec["anyOf"] = distinct
        spec["required"] = all(branch_required.get(name) or [False])
        merged[name] = spec
    return merged


def schema_param_spec(schema: JsonDict, name: str) -> Optional[JsonDict]:
    """One parameter spec by name, or None. Union branches are searched."""
    specs = schema_param_specs(schema)
    spec = specs.get(name)
    if spec is not None:
        return spec
    return None


def required_param_names(schema: JsonDict) -> List[str]:
    """Names of required parameters from a describeAction schema, in
    declaration order. Required means required in EVERY union branch, which
    matches the platform's merged agent view for union actions."""
    return [
        name
        for name, spec in schema_param_specs(schema).items()
        if spec.get("required") is True
    ]


def build_capability_digest(bundle: CapabilityBundle) -> str:
    """One-line-per-method digest for the system prompt.

    Format: `- <method> (requires: a, b, c): <description>`. Required
    fields come from the describeAction schema; description falls back to
    the bare capability entry if describeAction didn't return for this
    method. The full schema is on disk for the agent to recall via
    local_fs_search/read or via tool_result error annotations.
    """
    description_by_method = {
        str(cap.get("method") or ""): str(cap.get("description") or "").strip()
        for cap in bundle.capabilities
        if isinstance(cap, dict)
    }
    lines: List[str] = []
    for method in sorted(bundle.capability_methods):
        schema: dict[str, Any] | None = bundle.method_schemas.get(method)
        description = ""
        required: List[str] = []
        if isinstance(schema, dict):
            description = str(schema.get("description") or "").strip()
            required = required_param_names(schema)
        if not description:
            description = description_by_method.get(method, "")
        if required:
            lines.append(
                f"- {method} (requires: {', '.join(required)}): {description}"
            )
        else:
            lines.append(f"- {method}: {description}")
    return "\n".join(lines)
