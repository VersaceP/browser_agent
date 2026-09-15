"""Platform-derived shape facts for the Workflow.execute step language.

The harness hand-authors the model-facing JSON Schema in
``harness.tools.browser_tools.schemas`` because its descriptions teach
event-window semantics that the platform contract does not carry.  The property
NAMES, however, are not ours to invent.  The dispatcher validates every step
against a strict union, so a field the platform does not declare rejects the
whole workflow with -32602.

Live evidence (run ``8208ed49``, 2026-09-11): the harness advertised ``timeout``
on Action steps, which the platform's ``workflowActionFields`` does not define.
The model dutifully authored it and burned two turns on
``steps.0 anyOf must satisfy an allowed shape`` before dropping the field on its
own.  Two hand-maintained copies of one contract drift; this module exposes the
platform half so a test can fail the moment they do.

The source is the cached action contract the platform generates from its own Zod
definitions and stamps with a ``catalogRevision``.  ``_bootstrap_schema_cache``
REWRITES that directory at the start of every run, so the facts here are keyed
by the contract file's modification time: a refreshed cache is re-read rather
than served from a cache frozen at import.  The derivation is also lazy, so
importing this module never pins a revision.

What this is NOT: run-scoped.  ``harness.workflow_policy`` has no runtime
config, so it reads the default location (``<worktree parent>/
global_schema_cache/schemas``, which is the repo root under the default
``worktree_dir``).  A caller that knows the configured worktree should pass
``worktree_dir`` so a non-default layout resolves to the directory this run's
bootstrap actually wrote.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, FrozenSet, List, Optional, Tuple

from harness.schema_cache import global_schemas_dir
from harness.utils import JsonDict

EXECUTE_SCHEMA_FILE = "Workflow.execute.json"
#: Fields the generated JSON Schema marks required only because Zod gave them a
#: ``.default()``; omitting them is legal.
_DEFAULTED_STEP_FIELDS = frozenset({"onError"})


class PlatformSchemaUnavailable(RuntimeError):
    """Raised when the cached platform contract is missing or unreadable."""


@dataclass(frozen=True)
class StepShape:
    """One member of the platform's workflow step union.

    ``property_schemas`` carries each property's own constraints, which is what
    lets a drift check compare types and bounds rather than only names. For
    ``store`` — a union with one member per op — the schemas are merged, so a
    property present in any op is described here.

    ``additional_properties`` is recorded but must NOT be compared for
    equality: the platform's generator renders every ``.strict()`` object as
    ``additionalProperties: true``, while the dispatcher demonstrably rejects
    unknown step fields with -32602. The harness's ``false`` is the accurate
    one.
    """

    step_type: str
    properties: FrozenSet[str]
    required: FrozenSet[str]
    property_schemas: Dict[str, JsonDict] = field(default_factory=dict)
    additional_properties: Any = None


@dataclass(frozen=True)
class WorkflowContract:
    """Every structural fact this harness derives from one cached contract."""

    catalog_revision: str
    execute_properties: FrozenSet[str]
    execute_required: FrozenSet[str]
    execute_property_schemas: Dict[str, JsonDict]
    step_shapes: Dict[str, StepShape]
    action_shorthand: Optional[StepShape]
    transform_ops: Dict[str, StepShape]
    condition_operators: Dict[str, FrozenSet[str]]

    def effective_required(self, step_type: str) -> FrozenSet[str]:
        """Fields a step of this type cannot omit under ANY union member.

        The Action union has a typeless shorthand member, so ``type`` is not
        effectively required for Action steps while that member exists.
        """
        shape = self.step_shapes.get(step_type)
        if shape is None:
            return frozenset()
        required = shape.required
        if step_type == "action" and self.action_shorthand is not None:
            required = required & self.action_shorthand.required
        return required - _DEFAULTED_STEP_FIELDS

    def phantom_execute_params(self, candidates: FrozenSet[str]) -> FrozenSet[str]:
        """Candidates ``Workflow.execute`` does not declare.

        Its action schema is not ``.strict()``, so these travel and are dropped
        without an error — unlike a phantom *step* field, which rejects the
        whole workflow with -32602.  A live probe sent ``stepTimeout: 1000``
        against a step that then ran 5005 ms uninterrupted.
        """
        return frozenset(candidates) - self.execute_properties


def default_schemas_dir() -> Path:
    """Where the cache lands under the default ``worktree_dir``."""
    return Path(__file__).resolve().parent.parent / "global_schema_cache" / "schemas"


#: The directory this run's schema bootstrap actually wrote. Bound once, at the
#: end of that bootstrap, so the three consumers of the contract — the
#: model-facing tool schema, `validate_workflow_params`, and the tool-schema
#: cache stamp — all read the same files. Without it each resolves the default
#: location independently, which only coincides with the bootstrap's target
#: under the default ``worktree_dir``.
_ACTIVE_SCHEMAS_DIR: Optional[Path] = None


def bind_schemas_dir(schemas_dir: Optional[Path]) -> None:
    """Point every contract read at the directory this run bootstrapped."""
    global _ACTIVE_SCHEMAS_DIR
    _ACTIVE_SCHEMAS_DIR = Path(schemas_dir) if schemas_dir else None


def active_schemas_dir() -> Optional[Path]:
    return _ACTIVE_SCHEMAS_DIR


def contract_source() -> JsonDict:
    """Where the contract is being read from, and whether that is the run's own.

    A fallback is a degraded mode, not an error: the risk of reading a slightly
    older contract is bounded and loud. A stale contract can only make the
    model-facing schema too permissive (the dispatcher then rejects the step
    with -32602 and a precise path) or too strict (the harness rejects it, also
    with a path). Neither can produce a step that runs and does the wrong
    thing. But it should still be visible rather than inferred.
    """
    bound = _ACTIVE_SCHEMAS_DIR
    resolved = resolve_schemas_dir()
    fell_back = bound is not None and resolved != bound
    source: JsonDict = {
        "schemasDir": str(resolved),
        "boundDir": str(bound) if bound is not None else None,
        "fellBack": fell_back,
    }
    if fell_back:
        source["reason"] = "the run's own cache has no Workflow.execute contract"
    try:
        source["catalogRevision"] = workflow_contract().catalog_revision
    except PlatformSchemaUnavailable:
        source["catalogRevision"] = None
    return source


def resolve_schemas_dir(worktree_dir: Optional[str] = None) -> Path:
    """Where to read the contract from, preferring this run's own cache.

    The binding is a PREFERENCE, not a requirement. It is set before the
    bootstrap writes, and a bootstrap can degrade (no browser, empty
    capabilities, lock timeout) and leave nothing there. Falling back to the
    checked-in copy then is strictly better than failing every workflow schema
    build in the process — the contract is a shape description, and a slightly
    older one still rejects the shapes the dispatcher rejects.
    """
    if worktree_dir:
        return global_schemas_dir(worktree_dir)
    if _ACTIVE_SCHEMAS_DIR is not None:
        if (_ACTIVE_SCHEMAS_DIR / EXECUTE_SCHEMA_FILE).is_file():
            return _ACTIVE_SCHEMAS_DIR
    return default_schemas_dir()


_CONTRACT_CACHE: Dict[Tuple[str, int], WorkflowContract] = {}


def workflow_contract(
    *, worktree_dir: Optional[str] = None, schemas_dir: Optional[Path] = None,
) -> WorkflowContract:
    """Return the derived contract, re-reading whenever the cache file changes."""
    directory = Path(schemas_dir) if schemas_dir else resolve_schemas_dir(worktree_dir)
    path = directory / EXECUTE_SCHEMA_FILE
    try:
        stamp = path.stat().st_mtime_ns
    except OSError as exc:
        raise PlatformSchemaUnavailable(f"cannot stat {path}: {exc}") from exc
    key = (str(path), stamp)
    cached = _CONTRACT_CACHE.get(key)
    if cached is not None:
        return cached
    contract = _derive(path)
    # A rewritten contract makes every earlier key dead; do not accumulate.
    for stale in [k for k in _CONTRACT_CACHE if k[0] == key[0]]:
        _CONTRACT_CACHE.pop(stale, None)
    _CONTRACT_CACHE[key] = contract
    return contract


def _derive(path: Path) -> WorkflowContract:
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:  # pragma: no cover - environment-dependent
        raise PlatformSchemaUnavailable(f"cannot read {path}: {exc}") from exc
    except json.JSONDecodeError as exc:  # pragma: no cover - corrupt cache
        raise PlatformSchemaUnavailable(f"{path} is not valid JSON: {exc}") from exc
    if not isinstance(parsed, dict) or not isinstance(parsed.get("inputSchema"), dict):
        raise PlatformSchemaUnavailable(f"{path} has no inputSchema")
    schema = parsed["inputSchema"]
    defs = schema.get("$defs") or {}
    members = _step_union_members(schema, defs)
    return WorkflowContract(
        catalog_revision=str(parsed.get("catalogRevision") or ""),
        execute_properties=frozenset((schema.get("properties") or {}).keys()),
        execute_required=frozenset(schema.get("required") or ()),
        execute_property_schemas={
            name: value
            for name, value in (schema.get("properties") or {}).items()
            if isinstance(value, dict)
        },
        step_shapes=_step_shapes(members),
        action_shorthand=_action_shorthand(members),
        transform_ops=_transform_ops(members),
        condition_operators=_condition_operators(members, defs),
    )


def _step_union_members(schema: JsonDict, defs: JsonDict) -> List[JsonDict]:
    steps = (schema.get("properties") or {}).get("steps") or {}
    ref = (steps.get("items") or {}).get("$ref") or ""
    union = defs.get(ref.rsplit("/", 1)[-1]) if ref else {}
    members: List[JsonDict] = []

    def walk(node: Any) -> None:
        if not isinstance(node, dict):
            return
        for combinator in ("anyOf", "oneOf", "allOf"):
            branch = node.get(combinator)
            if isinstance(branch, list):
                for item in branch:
                    walk(item)
                return
        members.append(node)

    walk(union or {})
    return members



def _literal_type(member: JsonDict) -> Optional[str]:
    prop = (member.get("properties") or {}).get("type")
    if not isinstance(prop, dict):
        return None
    if isinstance(prop.get("const"), str):
        return prop["const"]
    enum = prop.get("enum")
    if isinstance(enum, list) and len(enum) == 1 and isinstance(enum[0], str):
        return enum[0]
    return None


def _step_shapes(members: List[JsonDict]) -> Dict[str, StepShape]:
    shapes: Dict[str, StepShape] = {}
    for member in members:
        step_type = _literal_type(member)
        if not step_type:
            continue
        raw_properties = member.get("properties") or {}
        properties = frozenset(raw_properties.keys())
        required = frozenset(member.get("required") or ())
        schemas = {
            name: schema
            for name, schema in raw_properties.items()
            if isinstance(schema, dict)
        }
        existing = shapes.get(step_type)
        if existing is None:
            shapes[step_type] = StepShape(
                step_type,
                properties,
                required,
                schemas,
                member.get("additionalProperties"),
            )
            continue
        # `store` arrives as one member per op; a field is legal if any op
        # declares it, and required only when every op requires it. Its `op`
        # literal differs per member, so the merged enum is the union of them.
        merged = dict(existing.property_schemas)
        for name, schema in schemas.items():
            merged[name] = _merge_property(merged.get(name), schema)
        shapes[step_type] = StepShape(
            step_type,
            existing.properties | properties,
            existing.required & required,
            merged,
            existing.additional_properties,
        )
    return shapes


def _merge_property(left: Optional[JsonDict], right: JsonDict) -> JsonDict:
    """Widen two union members' views of one property into their union.

    Only ``const``/``enum`` actually differ between store op members, and a
    field legal under either member is legal for the merged shape.
    """
    if not isinstance(left, dict):
        return right
    merged = dict(left)
    values = _literal_values(left) | _literal_values(right)
    if values:
        merged.pop("const", None)
        merged["enum"] = sorted(values)
    return merged


def _literal_values(schema: JsonDict) -> FrozenSet[str]:
    if isinstance(schema.get("const"), str):
        return frozenset({schema["const"]})
    enum = schema.get("enum")
    if isinstance(enum, list):
        return frozenset(str(item) for item in enum)
    return frozenset()


def _action_shorthand(members: List[JsonDict]) -> Optional[StepShape]:
    """The union's typeless Action member, if the platform still has one.

    ``workflowActionShorthandSchema`` lets a step omit ``type`` entirely; the
    compiler adds it.  The harness tool description teaches this spelling, so a
    drift check that demanded ``type`` on Action steps would be wrong.
    """
    for member in members:
        if _literal_type(member) is not None:
            continue
        properties = member.get("properties") or {}
        if "action" not in properties:
            continue
        return StepShape(
            "action",
            frozenset(properties.keys()),
            frozenset(member.get("required") or ()),
            {k: v for k, v in properties.items() if isinstance(v, dict)},
            member.get("additionalProperties"),
        )
    return None


def _transform_ops(members: List[JsonDict]) -> Dict[str, StepShape]:
    shapes: Dict[str, StepShape] = {}
    for member in members:
        if _literal_type(member) != "transform":
            continue
        items = ((member.get("properties") or {}).get("ops") or {}).get("items") or {}
        for branch in items.get("oneOf") or items.get("anyOf") or []:
            if not isinstance(branch, dict):
                continue
            name = ((branch.get("properties") or {}).get("op") or {}).get("const")
            if not isinstance(name, str):
                continue
            branch_properties = branch.get("properties") or {}
            shapes[name] = StepShape(
                name,
                frozenset(branch_properties.keys()),
                frozenset(branch.get("required") or ()),
                {
                    key: value
                    for key, value in branch_properties.items()
                    if isinstance(value, dict)
                },
                branch.get("additionalProperties"),
            )
    return shapes


def _condition_operators(
    members: List[JsonDict], defs: JsonDict,
) -> Dict[str, FrozenSet[str]]:
    leaf: set = set()
    group: set = set()
    for member in members:
        if _literal_type(member) not in {"if", "loop"}:
            continue
        condition = (member.get("properties") or {}).get("condition") or {}
        for branch in condition.get("anyOf") or condition.get("oneOf") or []:
            resolved = _resolve_ref(branch, defs)
            properties = resolved.get("properties") or {}
            names = (properties.get("operator") or {}).get("enum")
            if not isinstance(names, list):
                continue
            target = group if "conditions" in properties else leaf
            target.update(str(name) for name in names)
    return {"leaf": frozenset(leaf), "group": frozenset(group)}


def _resolve_ref(node: Any, defs: JsonDict) -> JsonDict:
    if not isinstance(node, dict):
        return {}
    ref = node.get("$ref")
    if not isinstance(ref, str) or not ref.startswith("#/$defs/"):
        return node
    return defs.get(ref.rsplit("/", 1)[-1]) or {}


# ---------------------------------------------------------------------------
# Structural comparison against the platform contract.
#
# This lives here rather than in a test because the test directory is not
# tracked: the safety net has to survive a commit. A test asserts that
# `compare_model_schema` reports nothing; the comparison itself is product code.
#
# The rule is DIRECTIONAL. The harness schema may be narrower than the platform
# contract — the event whitelist, the loop bound and the workflow budget all are
# on purpose — but never wider. A wider harness accepts a step the dispatcher
# then rejects with -32602, which costs a model turn: run 8208ed49 lost two that
# way to an Action `timeout` the platform does not declare.
# ---------------------------------------------------------------------------

#: `Number.MAX_SAFE_INTEGER`, which is what a Zod integer with no upper bound
#: renders as. Treating it as a real ceiling would flag every unbounded field.
_UNBOUNDED = 9007199254740991

_LOWER_BOUND_KEYWORDS = ("minimum", "exclusiveMinimum", "minLength", "minItems")
_UPPER_BOUND_KEYWORDS = ("maximum", "exclusiveMaximum", "maxLength", "maxItems")
#: Compared for equality when the platform declares one: a regex or format is
#: not something one side can meaningfully "narrow", and dropping it is the
#: exact hole that let `transform.input: "last.lines"` through the harness and
#: into a -32602.
_EXACT_KEYWORDS = ("pattern", "format", "multipleOf")


@dataclass
class SchemaComparison:
    """What a model-facing schema offers beyond, or short of, the platform."""

    violations: List[str] = field(default_factory=list)
    narrowings: List[str] = field(default_factory=list)
    compared: int = 0
    skipped: List[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.violations


def compare_model_schema(
    model_schema: JsonDict,
    *,
    worktree_dir: Optional[str] = None,
    schemas_dir: Optional[Path] = None,
) -> SchemaComparison:
    """Compare a model-facing Workflow.execute schema against the contract."""
    directory = Path(schemas_dir) if schemas_dir else resolve_schemas_dir(worktree_dir)
    parsed = json.loads(
        (directory / EXECUTE_SCHEMA_FILE).read_text(encoding="utf-8")
    )
    platform = parsed["inputSchema"]
    report = SchemaComparison()
    _compare_node(
        model_schema,
        platform,
        "",
        report,
        model_defs=model_schema.get("$defs") or {},
        platform_defs=platform.get("$defs") or {},
        seen=set(),
    )
    return report


def _deref(node: Any, defs: JsonDict) -> JsonDict:
    if not isinstance(node, dict):
        return {}
    ref = node.get("$ref")
    if isinstance(ref, str) and ref.startswith("#/$defs/"):
        return defs.get(ref.rsplit("/", 1)[-1]) or {}
    return node


def _branches(node: JsonDict, defs: JsonDict) -> Optional[List[JsonDict]]:
    """Flatten a union one level, following $refs and nested combinators.

    The platform nests: its step union carries `store` as a single member that
    is itself a `oneOf` of one object per store op. Left nested, that member has
    no discriminator and the whole `store` subtree goes uncompared.
    """
    collected: List[JsonDict] = []
    for combinator in ("oneOf", "anyOf"):
        raw = node.get(combinator)
        if not isinstance(raw, list) or not raw:
            continue
        for item in raw:
            if not isinstance(item, dict):
                continue
            resolved = _deref(item, defs)
            nested = _branches(resolved, defs)
            if nested:
                collected.extend(nested)
            else:
                collected.append(resolved)
        return collected
    return None


def _discriminator(node: JsonDict, defs: JsonDict) -> Optional[str]:
    """The literal that identifies which union member a node is."""
    resolved = _deref(node, defs)
    properties = resolved.get("properties") or {}
    for key in ("type", "op"):
        values = _values_of(properties.get(key))
        if values and len(values) == 1:
            return f"{key}={next(iter(values))}"
    return None


def _merge_branches(left: JsonDict, right: JsonDict) -> JsonDict:
    """Widen two union members that share a discriminator into their union."""
    merged = dict(left)
    left_properties = left.get("properties") or {}
    right_properties = right.get("properties") or {}
    properties: JsonDict = dict(left_properties)
    for name, schema in right_properties.items():
        properties[name] = _merge_property(properties.get(name), schema)
    merged["properties"] = properties
    merged["required"] = sorted(
        set(left.get("required") or ()) & set(right.get("required") or ())
    )
    return merged


def _property_names(node: JsonDict, defs: JsonDict) -> FrozenSet[str]:
    return frozenset((_deref(node, defs).get("properties") or {}).keys())


#: Jaccard overlap two union members must share before being treated as the
#: same member. Conditions — the real users of shape alignment — score 1.0
#: against their counterpart. A step type the platform has never heard of
#: overlaps every member slightly (they all carry `type`), and must be reported
#: as unmatched rather than compared against whichever one it grazes.
_SHAPE_ALIGNMENT_FLOOR = 0.5


def _align_by_shape(
    branch: JsonDict, candidates: List[JsonDict],
    model_defs: JsonDict, platform_defs: JsonDict,
) -> Optional[JsonDict]:
    """Pair union members that carry no literal discriminator.

    Conditions are such a union: a leaf ({path, operator, value}) and a group
    ({operator, conditions}) tell themselves apart by which properties they
    have, not by a tag. Matching on the property signature keeps the whole
    condition subtree inside the comparison instead of silently skipped.
    """
    mine = _property_names(branch, model_defs)
    if not mine:
        return None
    best: Optional[JsonDict] = None
    best_score = 0.0
    for candidate in candidates:
        theirs = _property_names(candidate, platform_defs)
        if not theirs:
            continue
        overlap = len(mine & theirs)
        if not overlap:
            continue
        score = overlap / len(mine | theirs)
        if score > best_score:
            best, best_score = candidate, score
    return best if best_score >= _SHAPE_ALIGNMENT_FLOOR else None


def _values_of(schema: Any) -> Optional[FrozenSet[str]]:
    if not isinstance(schema, dict):
        return None
    if isinstance(schema.get("const"), str):
        return frozenset({schema["const"]})
    enum = schema.get("enum")
    if isinstance(enum, list):
        return frozenset(str(item) for item in enum)
    return None


def _numeric(value: Any) -> Optional[float]:
    return float(value) if isinstance(value, (int, float)) else None


def _compare_node(
    model: Any, platform: Any, where: str, report: SchemaComparison,
    *, model_defs: JsonDict, platform_defs: JsonDict, seen: set,
) -> None:
    model = _deref(model, model_defs)
    platform = _deref(platform, platform_defs)
    if not isinstance(model, dict) or not isinstance(platform, dict):
        return
    # Keyed on the node PAIR, not the path: `then`/`else`/`body` all point back
    # at the step union, so a path-keyed guard never repeats and recurses until
    # the stack gives out. The same pair reached by a second path is the same
    # comparison, already made.
    key = (id(model), id(platform))
    if key in seen:
        return
    seen.add(key)
    report.compared += 1
    label = where or "<root>"

    _compare_types(model, platform, label, report)
    _compare_literals(model, platform, label, report)
    _compare_exact(model, platform, label, report)
    _compare_bounds(model, platform, label, report)
    _compare_membership(model, platform, label, report)
    _descend(
        model, platform, where, report,
        model_defs=model_defs, platform_defs=platform_defs, seen=seen,
    )


def _compare_types(
    model: JsonDict, platform: JsonDict, label: str, report: SchemaComparison,
) -> None:
    model_type, platform_type = model.get("type"), platform.get("type")
    if platform_type is None:
        return
    if model_type is None:
        report.violations.append(
            f"{label}: platform declares type {platform_type!r}, harness declares none"
        )
    elif model_type != platform_type:
        report.violations.append(
            f"{label}: type {model_type!r} != platform {platform_type!r}"
        )


def _compare_literals(
    model: JsonDict, platform: JsonDict, label: str, report: SchemaComparison,
) -> None:
    platform_values = _values_of(platform)
    if platform_values is None:
        return
    model_values = _values_of(model)
    if model_values is None:
        report.violations.append(
            f"{label}: platform restricts to {len(platform_values)} literal value(s),"
            " harness accepts any"
        )
        return
    extra = model_values - platform_values
    if extra:
        report.violations.append(
            f"{label}: offers values absent upstream {sorted(extra)}"
        )
    elif model_values != platform_values:
        report.narrowings.append(
            f"{label}: {len(model_values)} of {len(platform_values)} values"
        )


def _compare_exact(
    model: JsonDict, platform: JsonDict, label: str, report: SchemaComparison,
) -> None:
    for keyword in _EXACT_KEYWORDS:
        theirs = platform.get(keyword)
        if theirs is None:
            continue
        mine = model.get(keyword)
        if mine is None:
            report.violations.append(
                f"{label}: platform declares {keyword}={theirs!r}, harness declares none"
            )
        elif mine != theirs:
            report.violations.append(
                f"{label}: {keyword} {mine!r} != platform {theirs!r}"
            )


def _compare_bounds(
    model: JsonDict, platform: JsonDict, label: str, report: SchemaComparison,
) -> None:
    mine_low, theirs_low = _lower_bound(model), _lower_bound(platform)
    if theirs_low is not None:
        if mine_low is None:
            report.violations.append(
                f"{label}: platform floor {theirs_low}, harness has none"
            )
        elif mine_low < theirs_low:
            report.violations.append(
                f"{label}: floor {mine_low} below platform {theirs_low}"
            )
        elif mine_low > theirs_low:
            report.narrowings.append(f"{label}: floor {mine_low} vs {theirs_low}")

    mine_high, theirs_high = _upper_bound(model), _upper_bound(platform)
    if theirs_high is None or theirs_high >= _UNBOUNDED:
        return
    if mine_high is None:
        report.violations.append(
            f"{label}: platform ceiling {theirs_high}, harness has none"
        )
    elif mine_high > theirs_high:
        report.violations.append(
            f"{label}: ceiling {mine_high} above platform {theirs_high}"
        )
    elif mine_high < theirs_high:
        report.narrowings.append(f"{label}: ceiling {mine_high} vs {theirs_high}")


def _compare_membership(
    model: JsonDict, platform: JsonDict, label: str, report: SchemaComparison,
) -> None:
    """Compare which properties exist and which cannot be omitted.

    Without this the comparison only checked the CONSTRAINTS of properties both
    sides declare, so a field the platform has never heard of was simply never
    visited. That is the exact shape of the original incident: an Action step
    carrying `timeout`, rejected by the dispatcher with
    `steps.0 anyOf must satisfy an allowed shape`, costing two model turns.
    """
    model_properties = model.get("properties")
    platform_properties = platform.get("properties")
    if not isinstance(model_properties, dict) or not isinstance(platform_properties, dict):
        return

    invented = set(model_properties) - set(platform_properties)
    if invented:
        report.violations.append(
            f"{label}: declares properties the platform does not"
            f" {sorted(invented)}"
        )
    withheld = set(platform_properties) - set(model_properties)
    if withheld:
        report.narrowings.append(
            f"{label}: withholds {len(withheld)} platform propert"
            f"{'y' if len(withheld) == 1 else 'ies'} {sorted(withheld)}"
        )

    required = _effective_required(platform)
    missing = required - set(model.get("required") or ())
    if missing:
        report.violations.append(
            f"{label}: lets the caller omit platform-required {sorted(missing)}"
        )


def _effective_required(schema: JsonDict) -> FrozenSet[str]:
    """Platform-required property names, minus the ones a default fills in.

    Zod's `.default()` renders as `required` in the generated JSON Schema while
    remaining optional at runtime — `onError` and the top-level `timeout` both
    read as mandatory and are not. The generated schema keeps the `default`
    keyword alongside, so the artifact is detectable without naming fields.
    """
    properties = schema.get("properties") or {}
    return frozenset(
        name
        for name in (schema.get("required") or ())
        if "default" not in (properties.get(name) or {})
    )


def _lower_bound(schema: JsonDict) -> Optional[float]:
    """The tightest lower bound a node declares, however it spells it.

    `exclusiveMinimum: 0` on an integer and `minimum: 1` are the same rule; Zod
    renders `.positive()` as the former and the harness writes the latter.
    """
    candidates = []
    for keyword in _LOWER_BOUND_KEYWORDS:
        value = _numeric(schema.get(keyword))
        if value is None:
            continue
        if keyword == "exclusiveMinimum" and schema.get("type") == "integer":
            value += 1
        candidates.append(value)
    return max(candidates) if candidates else None


def _upper_bound(schema: JsonDict) -> Optional[float]:
    candidates = []
    for keyword in _UPPER_BOUND_KEYWORDS:
        value = _numeric(schema.get(keyword))
        if value is None:
            continue
        if keyword == "exclusiveMaximum" and schema.get("type") == "integer":
            value -= 1
        candidates.append(value)
    return min(candidates) if candidates else None


def _descend(
    model: JsonDict, platform: JsonDict, where: str, report: SchemaComparison,
    *, model_defs: JsonDict, platform_defs: JsonDict, seen: set,
) -> None:
    recurse = lambda a, b, path: _compare_node(  # noqa: E731 - local alias
        a, b, path, report,
        model_defs=model_defs, platform_defs=platform_defs, seen=seen,
    )

    model_properties = model.get("properties")
    platform_properties = platform.get("properties")
    if isinstance(model_properties, dict) and isinstance(platform_properties, dict):
        for name, schema in model_properties.items():
            upstream = platform_properties.get(name)
            if upstream is not None:
                recurse(schema, upstream, f"{where}.{name}" if where else name)

    for nested in ("items", "additionalProperties"):
        mine, theirs = model.get(nested), platform.get(nested)
        if isinstance(mine, dict) and isinstance(theirs, dict):
            recurse(mine, theirs, f"{where}.{nested}")

    model_branches = _branches(model, model_defs)
    platform_branches = _branches(platform, platform_defs)
    if model_branches is None or platform_branches is None:
        return
    # Several platform branches can share one discriminator: `store` is four
    # members, all `type=store`, differing only in their `op` literal. Keeping
    # the last one would compare the harness's four-value `op` enum against
    # `delete` alone and report three phantom extras.
    upstream_by_key: Dict[str, JsonDict] = {}
    unkeyed: List[JsonDict] = []
    for branch in platform_branches:
        marker = _discriminator(branch, platform_defs)
        if not marker:
            unkeyed.append(branch)
            continue
        existing = upstream_by_key.get(marker)
        upstream_by_key[marker] = (
            branch if existing is None else _merge_branches(existing, branch)
        )
    # A platform member with no discriminator can still be an alternative
    # spelling of a keyed one: `workflowActionShorthandSchema` is the Action
    # member with `type` omitted. Folding it in is what makes `type` correctly
    # optional on Action steps — the merge intersects the required sets.
    for branch in unkeyed:
        names = _property_names(branch, platform_defs)
        if not names:
            continue
        best_key, best_score = None, 0.0
        for key, candidate in upstream_by_key.items():
            theirs = _property_names(candidate, platform_defs)
            if not theirs:
                continue
            score = len(names & theirs) / len(names | theirs)
            if score > best_score:
                best_key, best_score = key, score
        if best_key is not None and best_score >= _SHAPE_ALIGNMENT_FLOOR:
            upstream_by_key[best_key] = _merge_branches(
                upstream_by_key[best_key], branch,
            )
    for index, branch in enumerate(model_branches):
        marker = _discriminator(branch, model_defs)
        upstream = upstream_by_key.get(marker) if marker else None
        if upstream is None and len(platform_branches) == 1:
            upstream = platform_branches[0]
        if upstream is None:
            upstream = _align_by_shape(
                branch, platform_branches, model_defs, platform_defs,
            )
        if upstream is None:
            report.violations.append(
                f"{where}[{index}]: model union member ({marker or 'no discriminator'})"
                " matches no platform member, so nothing checks it"
            )
            report.skipped.append(f"{where}[{index}] ({marker or 'no discriminator'})")
            continue
        label = marker or f"[{index}]"
        recurse(branch, upstream, f"{where}<{label}>")


def platform_constraints(
    schema: JsonDict, *keywords: str,
) -> JsonDict:
    """Lift the named constraint keywords out of a platform schema node.

    Used where the harness must reproduce a constraint verbatim rather than
    restate it: a hand-copied UUID regex is one edit away from being a regex
    the dispatcher does not share.
    """
    return {key: schema[key] for key in keywords if key in schema}


def contract_stamp(
    *, worktree_dir: Optional[str] = None, schemas_dir: Optional[Path] = None,
) -> str:
    """A cheap identity for the contract currently on disk.

    Anything that caches a schema DERIVED from the contract has to key on this,
    or its cache outlives the refresh. `_bootstrap_schema_cache` rewrites the
    directory at the start of every run, so a cache keyed only on capability
    methods keeps serving a tool schema built against the previous revision.

    The modification time is part of the key, not just the revision: a cache
    that is rewritten with the same catalog revision is still a different file,
    and stat is cheap enough to pay on every lookup.
    """
    directory = Path(schemas_dir) if schemas_dir else resolve_schemas_dir(worktree_dir)
    path = directory / EXECUTE_SCHEMA_FILE
    try:
        stamp = path.stat().st_mtime_ns
    except OSError:
        return "contract-unavailable"
    try:
        revision = workflow_contract(schemas_dir=directory).catalog_revision
    except PlatformSchemaUnavailable:  # pragma: no cover - unreadable cache
        return f"unreadable:{stamp}"
    return f"{revision}:{stamp}"
