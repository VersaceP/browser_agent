"""
harness.schema_loader - Capability discovery and per-method schema loading.

System.getCapabilities returns one compact summary per Action (method,
description, requiresPurpose) plus the catalog/guide revisions and, on request,
the Agent guide. It does NOT return input schemas, so to know what fields are
required and what hints to use when auto-filling we enumerate the capability
list and call System.describeAction per method.

This module is the single source of truth for that two-call bootstrap.
"""

import asyncio
import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, FrozenSet, Iterable, List, Optional, Set, Tuple

from abcp_client import ABCPClient, ABCPTransportError
from harness.utils import JsonDict, RunLogger


@dataclass
class CapabilityBundle:
    """All capability/schema info derived at bootstrap. Passed by reference
    to BrowserAgent and prompt builders."""

    capabilities: List[JsonDict] = field(default_factory=list)
    capability_methods: Set[str] = field(default_factory=set)
    method_schemas: Dict[str, JsonDict] = field(default_factory=dict)
    methods_requiring_purpose: Set[str] = field(default_factory=set)
    purpose_hints: Dict[str, str] = field(default_factory=dict)
    agent_guide: str = ""
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
      1. System.getCapabilities — enumerate methods and harvest the Agent
         guide markdown.
      2. For every method not in `blocked_methods`, load cached
         System.describeAction output from `schema_cache_dir` when available,
         otherwise call System.describeAction.
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
    bundle.agent_guide = _agent_guide_from_capabilities_response(caps_response)
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
    for method in methods_to_describe:
        cached_schema = _read_cached_schema(schema_cache_dir, method)
        cached_catalog_revision = str(
            (cached_schema.get("catalogRevision") or "")
            if isinstance(cached_schema, dict)
            else ""
        ).strip()
        # The platform publishes one catalogRevision, not per-Action revisions.
        # Once it changes, every cached descriptor belongs to the old contract.
        if cached_schema is None or (
            bundle.catalog_revision
            and cached_catalog_revision != bundle.catalog_revision
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
            returned_revision = str(data.get("catalogRevision") or "").strip()
            if bundle.catalog_revision and returned_revision != bundle.catalog_revision:
                logger.write(
                    "schema.describeAction.stale_catalog",
                    {
                        "method": method,
                        "expectedCatalogRevision": bundle.catalog_revision,
                        "returnedCatalogRevision": returned_revision or None,
                    },
                )
                return
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
            "agent_guide_chars": len(bundle.agent_guide),
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
    """Return the callable capability entries from ``data.actions``."""
    if not isinstance(response, dict):
        return []
    data = response.get("data")
    raw_actions = data.get("actions") if isinstance(data, dict) else []
    if not isinstance(raw_actions, list):
        return []
    return [item for item in raw_actions if isinstance(item, dict)]


def _agent_guide_from_capabilities_response(response: Any) -> str:
    """Read ``data.agentGuide`` — ``{format: 'content'|'path', value}``.

    A bare string is accepted because ``guide: "path"`` and ``guide: "content"``
    both hand back a single value the caller renders the same way.
    """
    if not isinstance(response, dict):
        return ""
    data = response.get("data")
    if not isinstance(data, dict):
        return ""
    guide = data.get("agentGuide")
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
    """Object-variant views of a describeAction ``inputSchema``.

    The schema is either a plain object (``properties`` + ``required`` list) or
    a union whose top level is ``anyOf``/``oneOf`` over object branches (e.g.
    Download.start's direct-URL vs page-reservation variants). A bare schema is
    accepted so a cached branch can be passed in directly.

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
    """Normalized per-parameter specs from a describeAction ``inputSchema``.

    Returns ``{name: spec}`` where every spec carries a boolean ``required``.
    For union schemas a name is required only when EVERY branch requires it,
    and differing branch specs are preserved under ``spec["anyOf"]``.
    """
    if not isinstance(schema, dict):
        return {}
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


# A required parameter whose value is an array or an object cannot be built
# from its name alone, and the digest is all the worker has before its first
# call: naming `selections` without saying it holds `{value}|{id}|{label}`
# items is how a worker spends steps guessing at a shape (task f1da2976).
# Scalars stay bare - their name plus the description already say enough - and
# a rendering longer than this budget is dropped rather than allowed to bloat
# every prompt; the full schema is on disk for those. Sized against the
# connected catalog: its widest parameter is Fleet.setProxy's `config` union at
# 86 characters, and that is exactly the kind of parameter a worker cannot
# guess, so the cap sits above it rather than below. Everything else there is
# 46 or shorter.
_DIGEST_SHAPE_MAX_CHARS = 96

# Second, independent bound: the per-parameter cap above says nothing about how
# MANY parameters a method has, and this text lands in every worker's system
# prompt. The worst method in the connected catalog spends 58 characters here,
# so this leaves roughly 3x headroom while keeping a future catalog from
# growing the prompt without limit. Required parameter NAMES are never dropped
# (a call cannot be built without them) - only shape annotations are, and
# unrendered optional params are counted in a trailing marker so the worker
# knows to go read the full schema rather than concluding they do not exist.
_DIGEST_METHOD_SHAPE_BUDGET = 160


def _raw_param_specs(schema: JsonDict, name: str) -> List[JsonDict]:
    """The parameter's own schema in EVERY top-level branch, in order.

    Two reasons this does not go through ``schema_param_specs``: that view
    overwrites ``required``/``anyOf`` on each spec with its cross-branch
    verdict (which would misread an object parameter's own required-key list),
    and it keeps only the first branch's shape when branches differ. A union
    action can give one name a different shape per branch, and showing the
    first as if it were the only one hides a legal call.
    """
    specs: List[JsonDict] = []
    for variant in _schema_object_variants(schema):
        spec = variant["properties"].get(name)
        if isinstance(spec, dict):
            specs.append(spec)
    return specs


def _digest_object_shape(spec: JsonDict) -> str:
    properties = spec.get("properties")
    if not isinstance(properties, dict) or not properties:
        return "{...}"
    required_raw = spec.get("required")
    required = {
        str(item)
        for item in (required_raw if isinstance(required_raw, list) else [])
    }
    keys = [
        str(name) if str(name) in required else f"{name}?"
        for name in properties
    ]
    return "{" + ",".join(keys) + "}"


def _digest_value_shape(spec: Any) -> str:
    """Compact rendering of one value: `{a,b?}`, `string`, `[...]`, unions."""
    if not isinstance(spec, dict):
        return "..."
    for keyword in ("anyOf", "oneOf"):
        branches = spec.get(keyword)
        if isinstance(branches, list) and branches:
            rendered: List[str] = []
            for branch in branches:
                shape = _digest_value_shape(branch)
                if shape not in rendered:
                    rendered.append(shape)
            return "|".join(rendered)
    kind = spec.get("type")
    if kind == "object":
        return _digest_object_shape(spec)
    if kind == "array":
        return f"[{_digest_value_shape(spec.get('items'))}]"
    if isinstance(kind, str):
        return kind
    return "..."


def _shape_of_branch(spec: Any) -> Optional[str]:
    """One branch's shape, or None when a name alone can carry a legal call.

    A parameter can be a union at its own level (``Fleet.setProxy``'s `config`
    is a bare ``oneOf`` of objects with no top-level ``type``), so reading
    ``type`` alone would call a complex parameter shapeless. Nested unions
    recurse; a single scalar branch anywhere collapses the whole parameter to
    None, because then the name really does suffice for at least one legal
    call and a partial shape would misrepresent the others.
    """
    if not isinstance(spec, dict):
        return None
    for keyword in ("anyOf", "oneOf"):
        branches = spec.get(keyword)
        if isinstance(branches, list) and branches:
            rendered: List[str] = []
            for branch in branches:
                shape = _shape_of_branch(branch)
                if shape is None:
                    return None
                if shape not in rendered:
                    rendered.append(shape)
            return "|".join(rendered) if rendered else None
    kind = spec.get("type")
    if kind == "array":
        return f"[{_digest_value_shape(spec.get('items'))}]"
    if kind == "object":
        return _digest_object_shape(spec)
    return None


def _render_param_shape(specs: Any) -> Optional[str]:
    """The parameter's full shape rendering, or None when it has no shape.

    None and "too long" are different answers and the callers act on them
    differently: a parameter with no shape is one whose name already carries a
    legal call, while a parameter whose shape does not fit is one the worker
    still has to go look up. Length is NOT judged here.
    """
    if isinstance(specs, dict):
        specs = [specs]
    if not isinstance(specs, list):
        return None
    rendered: List[str] = []
    for spec in specs:
        if not isinstance(spec, dict):
            continue
        shape = _shape_of_branch(spec)
        if shape is None:
            return None
        if shape not in rendered:
            rendered.append(shape)
    if not rendered:
        return None
    return "|".join(rendered)


def _digest_param_shape(specs: Any) -> str:
    """Shape suffix for one parameter, or "" when nothing should be shown.

    Accepts one spec or the per-branch list from ``_raw_param_specs``. Branches
    that render differently are joined with `|`, the same way a union of item
    shapes reads; anything past the budget is dropped rather than truncated,
    because a half-rendered shape is worse than none.
    """
    shape = _render_param_shape(specs)
    if shape is None or len(shape) > _DIGEST_SHAPE_MAX_CHARS:
        return ""
    return shape


def _digest_optional_shapes(
    schema: JsonDict, required: Set[str]
) -> Tuple[List[str], int]:
    """Shapes for the OPTIONAL parameters a name alone cannot describe.

    A contract generation can leave its whole payload optional in JSON Schema
    and express "exactly one of these" somewhere the export does not carry -
    ABCP's array-contract ``Input.select`` states it in ``purposeHint`` and in
    a zod ``superRefine``, so ``required`` is just ``[pageId, purpose]``. A
    digest built from required names alone would then name no payload field at
    all, which is the gap that made a worker guess (task f1da2976). Scalars are
    still skipped, so this stays a short list: on the connected build it adds
    eight annotations across the whole catalog.

    Returns the rendered entries plus a count of the optional parameters that
    HAVE a shape but whose rendering was too long to show. Those are counted so
    the line's trailing marker can point at them; optional scalars are omitted
    by design and are deliberately not counted, since nothing about them was
    withheld.
    """
    names: List[str] = []
    for variant in _schema_object_variants(schema):
        for name in variant["properties"]:
            name = str(name)
            if name not in required and name not in names:
                names.append(name)
    shaped: List[str] = []
    oversized = 0
    for name in names:
        shape = _render_param_shape(_raw_param_specs(schema, name))
        if shape is None:
            continue
        if len(shape) > _DIGEST_SHAPE_MAX_CHARS:
            oversized += 1
            continue
        shaped.append(f"{name}{shape}")
    return shaped, oversized


def build_capability_digest(bundle: CapabilityBundle) -> str:
    """One-line-per-method digest for the system prompt.

    Format: `- <method> (requires: a, b[{k}]; optional: c{k?}): <description>`.
    Required fields come from the describeAction schema; the `optional:` clause
    lists only the optional params whose value is an array or object, since a
    generation can leave its whole payload optional. Both carry a compact,
    LOSSY shape - item form and key names, never patterns, lengths, enums, or
    which fields exclude one another. Description falls back to the bare
    capability entry if describeAction didn't return for this method. The full
    schema is on disk for the agent to recall via local_fs_search/read or via
    tool_result error annotations, and remains the constraint source of truth.
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
        optional: List[str] = []
        withheld = 0
        if isinstance(schema, dict):
            description = str(schema.get("description") or "").strip()
            required_names = required_param_names(schema)
            budget = _DIGEST_METHOD_SHAPE_BUDGET
            for name in required_names:
                shape = _digest_param_shape(_raw_param_specs(schema, name))
                if shape and len(shape) <= budget:
                    budget -= len(shape)
                else:
                    shape = ""
                required.append(f"{name}{shape}")
            entries, withheld = _digest_optional_shapes(schema, set(required_names))
            for entry in entries:
                if len(entry) + 2 <= budget:
                    budget -= len(entry) + 2
                    optional.append(entry)
                else:
                    withheld += 1
        if not description:
            description = description_by_method.get(method, "")
        clauses: List[str] = []
        if required:
            clauses.append(f"requires: {', '.join(required)}")
        if optional:
            clauses.append(f"optional: {', '.join(optional)}")
        if withheld:
            clauses.append(f"+{withheld} more optional, read full schema")
        if clauses:
            lines.append(f"- {method} ({'; '.join(clauses)}): {description}")
        else:
            lines.append(f"- {method}: {description}")
    return "\n".join(lines)
