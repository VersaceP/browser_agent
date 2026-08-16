"""
harness.tools.browser_tools.validation - Parameter validation and response normalization guards.
"""

import re
from typing import Any
from typing import List
from typing import Optional
from typing import Tuple
from pathlib import Path
from harness.screenshot_policy import normalize_screenshot_output_params
from harness.utils import JsonDict
from .axtree_state import AXTREE_ID_RE

def _bt():
    import harness.tools.browser_tools as bt

    return bt

def _non_empty_param(params: JsonDict, key: str) -> bool:
    value = params.get(key)
    if isinstance(value, str):
        return bool(value.strip())
    return value is not None

def _non_negative_numeric_param(params: JsonDict, key: str) -> bool:
    value = params.get(key)
    if isinstance(value, (int, float)):
        return value >= 0
    if isinstance(value, str) and value.strip():
        try:
            return float(value) >= 0
        except ValueError:
            return False
    return False

def _check_select_param_requirements(
    method: str,
    params: JsonDict,
) -> Optional[JsonDict]:
    """Fail early on malformed Input.select selection envelopes.

    The live schema now requires EXACTLY ONE locator per item — id, value,
    label, or path — with `path` exclusive and every path segment following the
    same rule. An earlier revision of this guard deliberately accepted several
    coexisting fields because the schema of the day allowed it; that is now the
    opposite of the contract, and combining them is rejected by the platform.

    Multiple direct choices mean "this is the final selection set", not "append
    one more", and are only valid on a confirmed multi-select control — which
    the harness cannot know before dispatch, so that stays the platform's call.
    """

    if method != "Input.select":
        return None
    selections = params.get("selections")
    if not isinstance(selections, list) or not selections:
        return {
            "method": method,
            "params": params,
            "status": "invalid_params",
            "error": "Input.select requires a non-empty params.selections array.",
            "invalidParam": "selections",
            "missingAnyOf": [["selections"]],
            "tool_was_executed": False,
            "next_instruction": (
                "Call DOM.inspectSelect when choices are unknown, then pass"
                " selections as an array even for one choice. Copy only the"
                " id/value/label or complete path fields returned for the"
                " intended option, preferring exact value or label when present;"
                " Input.select manages the popup atomically."
            ),
        }

    canonical_id = re.compile(r"^\d+:\d+:\d+$")

    def invalid(path: str, detail: str) -> JsonDict:
        return {
            "method": method,
            "params": params,
            "status": "invalid_params",
            "error": detail,
            "invalidParam": path,
            "tool_was_executed": False,
            "next_instruction": (
                "Every selections item must carry EXACTLY ONE locator: id,"
                " exact value, exact label, or path. path is exclusive, and"
                " each path segment follows the same one-locator rule. Copy"
                " only option descriptor fields returned by DOM.inspectSelect;"
                " do not synthesize identifiers or operate the popup manually."
            ),
        }

    def present_locators(choice: JsonDict, *, allow_path: bool) -> List[str]:
        names = ["id", "value", "label"] + (["path"] if allow_path else [])
        present: List[str] = []
        for name in names:
            value = choice.get(name)
            if value is None:
                continue
            if isinstance(value, str) and not value.strip() and name != "value":
                # An empty value IS a legitimate option value; an empty
                # id/label is just an unfilled field.
                continue
            present.append(name)
        return present

    def validate_choice(choice: Any, path: str, *, allow_path: bool) -> Optional[JsonDict]:
        if not isinstance(choice, dict):
            return invalid(path, f"Input.select {path} must be an object.")
        raw_id = choice.get("id")
        if raw_id is not None and (
            not isinstance(raw_id, str) or canonical_id.fullmatch(raw_id.strip()) is None
        ):
            return invalid(f"{path}.id", f"Input.select {path}.id is not a canonical option id.")
        if not allow_path and choice.get("path") is not None:
            return invalid(f"{path}.path", "Nested Input.select cascade paths are not supported.")
        locators = present_locators(choice, allow_path=allow_path)
        if not locators:
            return invalid(
                path,
                f"Input.select {path} requires exactly one of id, value, label"
                + (", or path." if allow_path else "."),
            )
        if len(locators) > 1:
            return invalid(
                path,
                f"Input.select {path} carries {len(locators)} locators"
                f" ({', '.join(locators)}); the schema accepts exactly one.",
            )
        cascade = choice.get("path")
        if cascade is not None:
            if not isinstance(cascade, list) or len(cascade) < 2:
                return invalid(
                    f"{path}.path",
                    f"Input.select {path}.path must contain at least two ordered choices.",
                )
            for index, step in enumerate(cascade):
                error = validate_choice(step, f"{path}.path[{index}]", allow_path=False)
                if error is not None:
                    return error
        return None

    for index, selection in enumerate(selections):
        error = validate_choice(selection, f"selections[{index}]", allow_path=True)
        if error is not None:
            return error
    return None

def _check_nested_id_format(method: str, params: JsonDict) -> Optional[JsonDict]:
    """Same canonical-id check for locators that do not sit at the top level.

    Input.scroll's `target`/`container` and Input.drag's destination carry ids
    the describeAction schema describes inline, so the top-level `params.id`
    lookup below finds no spec and validates nothing. A truncated id there
    still reaches the browser as an opaque -32602.
    """
    for path, locator, key in (
        ("target", params.get("target"), "id"),
        ("container", params.get("container"), "id"),
        ("", params, "toId"),
    ):
        if not isinstance(locator, dict):
            continue
        raw = locator.get(key)
        if not isinstance(raw, str) or not raw.strip():
            continue
        if AXTREE_ID_RE.match(raw.strip()):
            continue
        param_path = f"{path}.{key}" if path else key
        return {
            "method": method,
            "params": params,
            "status": "invalid_params",
            "error": (
                f"{method} params.{param_path} is not a valid canonical element"
                " id (expected frameId:axNodeId:domNodeId)."
            ),
            "tool_was_executed": False,
            "invalidParam": param_path,
            "next_instruction": (
                "Re-read the active page with DOM.getAXTree and copy a current"
                " canonical id verbatim, or drop the id and locate by selector."
            ),
        }
    return None

def _check_id_param_format(
    method: str,
    params: JsonDict,
    method_schemas: Optional[dict],
) -> Optional[JsonDict]:
    """Validate a supplied canonical element `id` against the describeAction
    schema pattern. A truncated/fabricated id (e.g. "2:5367" where the schema
    requires "^\\d+:\\d+:\\d+$") is caught here with an actionable error
    instead of reaching the browser and returning a raw -32602 Invalid params.
    Only fires when an `id` is actually supplied; a missing id is handled by the
    selector/id presence check. Returns None when no pattern is available
    (nothing to validate against) so this never over-rejects."""
    if not isinstance(params, dict) or not isinstance(method_schemas, dict):
        return None
    nested = _check_nested_id_format(method, params)
    if nested is not None:
        return nested
    raw_id = params.get("id")
    if not isinstance(raw_id, str) or not raw_id.strip():
        return None
    schema = method_schemas.get(method)
    if not isinstance(schema, dict):
        return None
    spec_params = schema.get("params")
    if not isinstance(spec_params, dict):
        return None
    id_spec = spec_params.get("id")
    if not isinstance(id_spec, dict):
        return None
    pattern = id_spec.get("pattern")
    if not isinstance(pattern, str) or not pattern.strip():
        return None
    try:
        matched = re.search(pattern, raw_id.strip()) is not None
    except re.error:
        return None
    if matched:
        return None
    return {
        "method": method,
        "params": params,
        "status": "invalid_params",
        "error": (
            f"{method} params.id is not a valid canonical element id"
            f" (schema pattern: {pattern})."
        ),
        "tool_was_executed": False,
        "invalidParam": "id",
        "pattern": pattern,
        "next_instruction": (
            "Re-read the active page with DOM.getAXTree and copy a current"
            " canonical id verbatim. Do not truncate ids, reuse stale ids from"
            " a prior page/navigation, or fabricate one. The id must match the"
            f" schema pattern: {pattern}."
        ),
    }

DOM_GET_IMG_MAX_TARGETS = 32

_SCROLL_MODE_INSTRUCTION = (
    "Input.scroll has three modes and no top-level locator. Target mode:"
    " target={id?,selector?} (plus optional container) with amount as the"
    " per-step cap and NO direction — the browser derives it and success means"
    " targetVisible=true. Container mode: container={id?,selector?} with"
    " direction and amount, for a container that is already visible. Viewport"
    " mode: neither locator, just direction and amount. Read layers[].delta for"
    " the real movement, and do not repeat the same direction after"
    " completedReason=boundary-reached."
)

def _check_scroll_param_requirements(
    method: str,
    params: JsonDict,
) -> Optional[JsonDict]:
    """Reject Input.scroll shapes the platform's three-mode union will refuse.

    The union is strict, so a flat `id`/`selector` — the pre-frame-graph shape
    and the one most models reach for — matches no variant and comes back as a
    bare -32602 with nothing to act on. Catching it here costs one round trip
    less and says which mode was meant.
    """
    if method != "Input.scroll":
        return None

    def invalid(detail: str, invalid_param: str) -> JsonDict:
        return {
            "method": method,
            "params": params,
            "status": "invalid_params",
            "error": detail,
            "invalidParam": invalid_param,
            "tool_was_executed": False,
            "next_instruction": _SCROLL_MODE_INSTRUCTION,
        }

    for key in ("id", "selector", "nodeId", "targetId"):
        if _non_empty_param(params, key):
            return invalid(
                f"Input.scroll does not accept a top-level {key}; put the"
                " locator in target={id?,selector?} or container={id?,selector?}.",
                key,
            )

    target = params.get("target")
    container = params.get("container")
    for key, locator in (("target", target), ("container", container)):
        if locator is None:
            continue
        if not isinstance(locator, dict):
            return invalid(f"Input.scroll params.{key} must be an object.", key)
        if not (_non_empty_param(locator, "id") or _non_empty_param(locator, "selector")):
            return invalid(
                f"Input.scroll params.{key} requires id or selector.", key
            )

    amount = params.get("amount")
    numeric_amount = (
        float(amount)
        if isinstance(amount, (int, float)) and not isinstance(amount, bool)
        else None
    )
    if target is not None:
        if _non_empty_param(params, "direction"):
            return invalid(
                "Input.scroll target mode derives its own direction; drop"
                " params.direction or switch to container/viewport mode.",
                "direction",
            )
        if numeric_amount is not None and numeric_amount <= 0:
            return invalid(
                "Input.scroll target mode needs a positive amount (the cap on"
                " each smooth-scroll step). amount=0 reads state and is valid"
                " only for container or viewport mode.",
                "amount",
            )
    if numeric_amount is not None and numeric_amount < 0:
        return invalid("Input.scroll amount must not be negative.", "amount")
    return None

def _check_target_param_requirements(
    method: str,
    params: JsonDict,
    method_schemas: Optional[dict] = None,
) -> Optional[JsonDict]:
    if not isinstance(params, dict):
        return None
    scroll_error = _check_scroll_param_requirements(method, params)
    if scroll_error is not None:
        return scroll_error
    has_selector_or_id = _non_empty_param(params, "selector") or _non_empty_param(params, "id")
    batch_methods = {"DOM.getText", "DOM.getAttribute", "DOM.getImg"}
    raw_targets = params.get("targets")
    has_batch_targets = isinstance(raw_targets, list) and bool(raw_targets)
    if method in batch_methods and has_batch_targets:
        schema = method_schemas.get(method) if isinstance(method_schemas, dict) else None
        schema_params = schema.get("params") if isinstance(schema, dict) else None
        if isinstance(schema_params, dict) and "targets" not in schema_params:
            return {
                "method": method,
                "params": params,
                "status": "capability_not_supported",
                "error": f"The connected ABCP schema for {method} does not expose params.targets.",
                "tool_was_executed": False,
                "next_instruction": (
                    "Use the single-target selector/id shape for this server version,"
                    " or upgrade ABCP before using native batch reads."
                ),
            }
        for index, target in enumerate(raw_targets):
            if not isinstance(target, dict) or not (
                _non_empty_param(target, "selector") or _non_empty_param(target, "id")
            ):
                return {
                    "method": method,
                    "params": params,
                    "status": "invalid_params",
                    "error": f"{method} params.targets[{index}] requires selector or id.",
                    "invalidParam": f"targets[{index}]",
                    "tool_was_executed": False,
                }
            id_error = _check_id_param_format(method, target, method_schemas)
            if id_error is not None:
                id_error["invalidParam"] = f"targets[{index}].id"
                return id_error
        if method == "DOM.getImg":
            if len(raw_targets) > DOM_GET_IMG_MAX_TARGETS:
                return {
                    "method": method,
                    "params": params,
                    "status": "invalid_params",
                    "error": (
                        f"DOM.getImg accepts at most {DOM_GET_IMG_MAX_TARGETS}"
                        f" targets per call; {len(raw_targets)} were supplied."
                    ),
                    "invalidParam": "targets",
                    "tool_was_executed": False,
                    "next_instruction": (
                        "Split the export into batches of"
                        f" {DOM_GET_IMG_MAX_TARGETS} or fewer targets, keeping"
                        " each batch on one page, and read every batch's"
                        " response.data.items independently."
                    ),
                }
            options = params.get("options")
            path = options.get("path") if isinstance(options, dict) else None
            if not isinstance(path, str) or not path.strip():
                return {
                    "method": method,
                    "params": params,
                    "status": "invalid_params",
                    "error": "DOM.getImg requires params.options.path as an output directory.",
                    "invalidParam": "options.path",
                    "tool_was_executed": False,
                }
        return None
    if method == "DOM.getImg" and not has_batch_targets:
        return {
            "method": method,
            "params": params,
            "status": "invalid_params",
            "error": "DOM.getImg requires a non-empty params.targets array.",
            "tool_was_executed": False,
            "missingAnyOf": [["targets"]],
        }
    if method in {
        "DOM.getText",
        "DOM.getAttribute",
        "DOM.inspectSelect",
        "Input.select",
        "Input.type",
    } and not has_selector_or_id:
        return {
            "method": method,
            "params": params,
            "status": "invalid_params",
            "error": f"{method} requires either params.selector or params.id.",
            "tool_was_executed": False,
            "missingAnyOf": [["selector"], ["id"]],
            "next_instruction": (
                "Use DOM.getAXTree to locate a canonical AX id, or provide a"
                " concrete CSS selector. Do not call this method with only"
                " pageId/purpose or without a target element."
            ),
        }
    select_error = _check_select_param_requirements(method, params)
    if select_error is not None:
        return select_error
    if method == "Input.click" and not has_selector_or_id:
        has_coordinates = (
            _non_negative_numeric_param(params, "x")
            and _non_negative_numeric_param(params, "y")
        )
        if not has_coordinates:
            return {
                "method": method,
                "params": params,
                "status": "invalid_params",
                "error": (
                    "Input.click requires selector/id or both non-negative x and y"
                    " coordinates."
                ),
                "tool_was_executed": False,
                "missingAnyOf": [["selector"], ["id"], ["x", "y"]],
                "next_instruction": (
                    "Prefer a current DOM.getAXTree id for Input.click. Use x/y"
                    " only for a verified coordinate fallback such as a backdrop"
                    " click."
                ),
            }
    # Canonical id format guard: catch a malformed id here (clear, actionable
    # error) rather than letting it reach the browser as a -32602 Invalid params.
    id_format_error = _check_id_param_format(method, params, method_schemas)
    if id_format_error is not None:
        return id_format_error
    return None

def _annotate_dom_batch_response(method: str, response: Any) -> Any:
    """Add a compact receipt without changing the native ordered item envelope."""

    if method not in {"DOM.getText", "DOM.getAttribute", "DOM.getImg"}:
        return response
    if not isinstance(response, dict):
        return response
    data = response.get("data")
    items = data.get("items") if isinstance(data, dict) else None
    if not isinstance(items, list):
        return response
    succeeded = sum(
        1 for item in items
        if isinstance(item, dict)
        and item.get("error") is None
        and isinstance(item.get("info"), dict)
    )
    failed = len(items) - succeeded
    # Only the outer envelope and data mapping change. Keep the potentially
    # large native item payload shared instead of recursively copying it.
    copied = dict(response)
    copied_data = dict(data)
    copied["data"] = copied_data
    copied_data["batchSummary"] = {
        "total": len(items),
        "succeeded": succeeded,
        "failed": failed,
        "partialFailure": bool(succeeded and failed),
        "targetOrderPreserved": True,
    }
    return copied

def _check_screenshot_misuse(
    method: str,
    params: JsonDict,
    reason: str = "",
) -> Optional[JsonDict]:
    if method != "Page.screenshot":
        return None
    text = " ".join(
        str(value or "")
        for value in (
            reason,
            params.get("purpose") if isinstance(params, dict) else "",
        )
    )
    if _bt().SCREENSHOT_ALLOWED_PURPOSE_RE.search(text):
        return None
    if not _bt().SCREENSHOT_MISUSE_RE.search(text):
        return None
    return {
        "status": "rejected",
        "reason": "page_screenshot_not_model_visible",
        "method": method,
        "tool_was_executed": False,
        "next_instruction": (
            "Page.screenshot returns only a savedPath; the model cannot inspect"
            " that image from this tool result. Use DOM.getAXTree,"
            " DOM.getText, DOM.getAttribute, or"
            " visual_verify for bounded visual arbitration."
        ),
    }

def _default_semantic_tree_shadow_dom(
    method: str,
    params: JsonDict,
    method_schemas: Any,
) -> Tuple[JsonDict, bool]:
    """Include shadow content unless the caller explicitly opts out.

    An omitted flag makes a rendered custom-element host look like an empty
    subtree, which led workers to classify tall v-detail-* hosts as a platform
    limitation and skip exportable images. This default is applied only when
    the connected schema advertises the parameter, so older ABCP versions do
    not receive an invented argument. Explicit false remains an escape hatch
    for a deliberately light diagnostic.
    """
    if method != "DOM.getSemanticTree" or "includeShadowDom" in params:
        return params, False
    schema = (
        method_schemas.get(method)
        if isinstance(method_schemas, dict) else None
    )
    schema_params = schema.get("params") if isinstance(schema, dict) else None
    if not isinstance(schema_params, dict) or "includeShadowDom" not in schema_params:
        return params, False
    normalized = dict(params)
    normalized["includeShadowDom"] = True
    return normalized, True

def _normalize_screenshot_output(
    method: str,
    params: JsonDict,
) -> Tuple[JsonDict, Optional[JsonDict]]:
    """Force Page.screenshot to return a file handle, never image bytes.

    Image payload stripping happens only after the WebSocket response arrives,
    which is too late for a large full-page base64 frame.  ABCP owns the output
    path; model-provided path/quality/encoding options are intentionally not
    forwarded because Page.screenshot is a savedPath-only harness primitive.
    """
    return normalize_screenshot_output_params(method, params)

def _normalize_dom_get_img_output(
    agent: Any,
    method: str,
    params: JsonDict,
) -> Tuple[JsonDict, Optional[JsonDict]]:
    """Resolve model-relative image export directories inside this task.

    ABCP resolves relative paths in its own service working directory, not in
    the harness worktree.  The resulting ENOENT was surfaced only as generic
    JSON-RPC -32005.  Path resolution is mechanical adapter work: preserve an
    explicit absolute path, and bind a relative one to this run's artifacts
    directory before dispatch.
    """
    if method != "DOM.getImg" or not isinstance(params, dict):
        return params, None
    options = params.get("options")
    raw_path = options.get("path") if isinstance(options, dict) else None
    if not isinstance(raw_path, str) or not raw_path.strip():
        return params, None
    candidate = Path(raw_path.strip()).expanduser()
    if candidate.is_absolute():
        return params, None
    task_dir = getattr(getattr(agent, "logger", None), "task_dir", None)
    if task_dir is None:
        return params, None
    task_root = Path(task_dir).resolve()
    artifacts_root = (task_root / "artifacts").resolve()
    parts = candidate.parts
    if parts and parts[0] == "artifacts":
        resolved = (task_root / candidate).resolve()
    else:
        cwd_candidate = (Path.cwd() / candidate).resolve()
        try:
            cwd_candidate.relative_to(task_root)
        except ValueError:
            resolved = (artifacts_root / candidate).resolve()
        else:
            resolved = cwd_candidate
    try:
        resolved.relative_to(artifacts_root)
    except ValueError:
        # Do not let a model-relative export escape the run directory.
        resolved = artifacts_root / "images"
    resolved.mkdir(parents=True, exist_ok=True)
    normalized = dict(params)
    normalized_options = dict(options)
    normalized_options["path"] = str(resolved)
    normalized["options"] = normalized_options
    receipt = {
        "field": "params.options.path",
        "from": raw_path,
        "to": str(resolved),
        "basis": "task_artifacts_directory",
    }
    logger = getattr(agent, "logger", None)
    if logger is not None and hasattr(logger, "write"):
        logger.write("browser.call.dom_get_img_output_normalized", receipt)
    return normalized, receipt

def _attach_normalized_handles(result: JsonDict) -> JsonDict:
    if not isinstance(result, dict):
        return result
    data = _bt()._response_data(result)
    handles = {
        key: str(data.get(key))
        for key in ("fleetId", "pageId", "downloadId", "bookmarkId")
        if data.get(key) is not None and str(data.get(key)).strip()
    }
    if handles:
        result = dict(result)
        result["normalizedHandles"] = handles
    return result
