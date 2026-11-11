"""WebCross source packages and per-call Harness capability admission.

WebCross compiles JS/TS. The Harness never parses business source to guess its
actions: a trusted entry passes only a scoped API to the unmodified entry.
"""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path, PurePosixPath
from typing import Any


class ScopedWorkflowPackage(dict):
    """Host-created capability package; this identity cannot come from JSON."""


def page_scoped_workflow(params: dict) -> bool:
    package = params.get("_platformWorkflow")
    if not isinstance(package, ScopedWorkflowPackage):
        return False
    scope = getattr(package, "page_scope", None)
    return bool(scope and scope == (params.get("pageId"), params.get("fleetId"), source_hash(package)))


def execution_scope_contract() -> dict:
    return {"page": "Every action targets the single bound page. Inventory/events do not grant new pages. Return a discovered pageId and continue in a new bound invocation.",
            "independentTools": ["Page.create", "Page.switchTo", "Page.close"],
            "downloads": ("Download.start, DOM.getImg (file output) and Download.list are available. savePath / "
                          "options.path must be absolute paths the task may write; protected locations (WebCross "
                          "installation and data, sensitive names, the Harness, other tasks) are denied. "
                          "Download.control applies to downloads this execution started. A started download is not "
                          "a saved file: confirm completion with Download.list."),
            "network": "Network.readApi and Network.readBody read the bound page's captured requests.",
            "screenshots": "Page.screenshot is allowed on the bound page and always uses managed file output. A saved image is evidence, not proof the model viewed it; use the image-capable observation tool when visual judgment is needed.",
            "files": "File.handleChooser accepts only authorized canonical paths supplied in input.",
            "capabilities": ("Runtime.evaluate needs a top-level declaration: "
                "{\"Runtime.evaluate\":{\"worlds\":[\"isolated\"]}}. "
                "File.write (bounded Markdown under the managed WebCross export directory) is available "
                "without one; declaring it is accepted for older packages. "
                "Runtime.evaluate is for a read-only, identity-checked fallback after structured DOM "
                "queries could not express the read; it runs only in the declared worlds (default isolated; "
                "auto is rejected). Grants never exceed the caller's own authorized methods. "
                "Publication shows declared capabilities to the user."),
            "output": "Return JSON data; normalize absent error fields to null, not undefined. "
                      "Report convention: outcome done/partial/blocked, omitted[{item,code,reason}], blockers[]. "
                      "Handle known variations in code and disclose them in omitted instead of failing.",
            "recovery": "Execution failure or timeout does not undo earlier actions. Reconcile effects before replay."}


# Runtime.evaluate runs page script and stays opt-in. File.write is admitted by
# default and may still be declared for compatibility with existing packages.
DECLARABLE_CAPABILITIES = frozenset({"File.write", "Runtime.evaluate"})
# Admitted without a declaration. Pages stay bound to the task (Page.create,
# Page.switchTo, Page.close are host decisions). Downloads and image exports
# write only through the path guard; Download control is limited to downloads
# this execution started. Hitl, Memory, Fleet, System and Workflow stay with
# the host, which owns user hand-over, shared state and execution identity.
DEFAULT_ACTIONS = frozenset({"File.handleChooser", "File.write", "Download.start", "Download.list",
                             "Download.control", "Network.readApi", "Network.readBody", "DOM.getImg"})
HOST_OWNED_PAGE_ACTIONS = frozenset({"Page.create", "Page.switchTo", "Page.close"})
WRITE_PATH_ACTIONS = {"Download.start": "savePath", "DOM.getImg": "options.path"}

# WebCross 1.0.0-beta workflowLimits. Pin the effective default into the scoped
# request so engine and transport cannot silently use different defaults.
DEFAULT_EXECUTION_TIMEOUT_MS = 1_800_000
MAX_EXECUTION_TIMEOUT_MS = 3_600_000


def execution_timeout_ms(document: dict) -> int:
    value = document.get("timeoutMs", DEFAULT_EXECUTION_TIMEOUT_MS)
    if not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= MAX_EXECUTION_TIMEOUT_MS:
        raise ValueError("Workflow timeoutMs must be an integer from 1 to 3600000")
    return value


def declared_capabilities(document: Any) -> dict:
    """Validate a package's capability declaration; never infer one from source."""
    value = document.get("capabilities") if isinstance(document, dict) else None
    if value is None:
        return {}
    if not isinstance(value, dict) or set(value) - DECLARABLE_CAPABILITIES:
        raise ValueError("capabilities may declare only " + ", ".join(sorted(DECLARABLE_CAPABILITIES)))
    result = {}
    for method, options in value.items():
        if not isinstance(options, dict):
            raise ValueError(f"capabilities.{method} must be an object")
        if method == "Runtime.evaluate":
            worlds = options.get("worlds", ["isolated"])
            if (set(options) - {"worlds"} or not isinstance(worlds, list) or not worlds
                    or set(worlds) - {"isolated", "main"}):
                raise ValueError("capabilities.Runtime.evaluate.worlds must list isolated and/or main")
            result[method] = {"worlds": sorted(set(worlds))}
        else:
            if options:
                raise ValueError(f"capabilities.{method} has no options")
            result[method] = {}
    return result


def is_script_package(value: Any) -> bool:
    return isinstance(value, dict) and isinstance(value.get("entry"), str) and isinstance(value.get("files"), dict)


def script_contract_available() -> bool:
    from harness.workflow.workflow_schema_source import workflow_contract
    from harness.workflow.workflow_schema_source import PlatformSchemaUnavailable
    forms = workflow_contract().execute_forms
    if "source" not in forms and "legacy" not in forms:
        raise PlatformSchemaUnavailable("Workflow.execute has no supported inline execution form; update the Harness adapter")
    return "source" in forms


def workflow_payload(document: dict, business_input: Any, page_id: str, fleet_id: str) -> dict:
    """Keep source, business input and execution identity independent."""
    return {"workflow": copy.deepcopy(document), "input": copy.deepcopy(business_input),
            "binding": {k: v for k, v in {"pageId": page_id, "fleetId": fleet_id}.items() if v}}


def source_hash(document: dict) -> str:
    return hashlib.sha256(json.dumps(document, ensure_ascii=False, sort_keys=True,
                                    separators=(",", ":")).encode()).hexdigest()


def script_tool_schema() -> dict:
    from harness.workflow.workflow_schema_source import workflow_contract
    contract = workflow_contract()
    bindings = contract.execute_property_schemas.get("binding", {}).get("properties", {})
    schema = {"type": "object", "properties": {
        "workflow": copy.deepcopy(contract.execute_property_schemas["workflow"]),
        "input": copy.deepcopy(contract.execute_property_schemas.get("input", {})),
        "pageId": copy.deepcopy(bindings["pageId"]), "fleetId": copy.deepcopy(bindings["fleetId"])},
        "required": ["workflow", "input", "pageId", "fleetId"], "additionalProperties": False}
    # The JSON input's recursive references belong to the live execute schema.
    from harness.workflow.workflow_schema_source import resolve_schemas_dir
    raw = json.loads((resolve_schemas_dir() / "Workflow.execute.json").read_text())
    if raw["inputSchema"].get("$defs"):
        schema["$defs"] = copy.deepcopy(raw["inputSchema"]["$defs"])
    return schema


def input_files(owner: Any, business_input: Any) -> list[str]:
    """Snapshot canonical, already authorized files named by this execution.

    No new access is granted by a string appearing in input. Computed paths must
    be supplied in the input file manifest or explicitly authorized beforehand.
    """
    from harness.tools.path_authorization import allowed, state
    strings: set[str] = set()
    def walk(value: Any) -> None:
        if isinstance(value, str):
            strings.add(value)
        elif isinstance(value, dict):
            for item in value.values(): walk(item)
        elif isinstance(value, list):
            for item in value: walk(item)
    walk(business_input)
    try:
        strings.update(state(owner)["readRoots"])
    except (AttributeError, TypeError):
        return []
    files = []
    for value in sorted(strings):
        try:
            path = Path(value)
            if path.is_absolute() and path.is_file() and not path.is_symlink():
                canonical = path.resolve()
                if str(path) == str(canonical) and allowed(owner, canonical, "read"):
                    files.append(str(canonical))
        except (OSError, ValueError):
            continue
    return files


def scope_package(document: dict, *, page_id: str, fleet_id: str,
                  methods: set[str], files: list[str], write_policy: dict | None = None) -> tuple[dict, dict]:
    """Inject a capability wrapper, preserving every original source byte.

    This applies equally to aliases and computed method names. Guest code never
    receives the raw SDK, cannot import host modules, and cannot expand grants.
    """
    if not is_script_package(document):
        raise ValueError("Legacy steps Workflow is not supported by this deployment; rebuild it as a JS/TS source package")
    entry = document["entry"]
    for name in document["files"]:
        parts = PurePosixPath(name).parts
        if (not parts or name.startswith("/") or "\\" in name
                or any(p in {".", ".."} for p in name.split("/"))
                or not name.endswith((".js", ".ts"))):
            raise ValueError("Workflow source path must stay inside the package")
    if entry not in document["files"]:
        raise ValueError("Workflow entry source is missing")
    from harness.tools.tool_policy import disabled_reason_for_method
    declared = declared_capabilities(document)
    permitted = sorted(m for m in methods
                       if ((m.startswith(("Page.", "DOM.", "Input.")) and m not in HOST_OWNED_PAGE_ACTIONS)
                           or m in DEFAULT_ACTIONS or m in declared)
                       and not disabled_reason_for_method(m)
                       # Without a write policy nothing may write outside the export directory.
                       and (m not in WRITE_PATH_ACTIONS or write_policy is not None))
    denied = sorted(set(declared) - set(permitted))
    worlds = declared.get("Runtime.evaluate", {}).get("worlds", []) if "Runtime.evaluate" in permitted else []
    scope = {"pageId": page_id, "fleetId": fleet_id, "methods": permitted, "files": files,
             "evaluateWorlds": worlds, "write": write_policy or None}
    wrapper = "harness-scope.js"
    while wrapper in document["files"]:
        wrapper = "harness-" + wrapper
    guard = "harness-capabilities.js"
    while guard in document["files"] or guard == wrapper:
        guard = "harness-" + guard
    # This module evaluates before business imports. Capture intrinsics before
    # guest code can replace prototypes or globals and weaken permission tests.
    code = '''
// Reserve SDK intrinsics before loading untrusted modules. Prototypes or
// globals must not be replaced to alter the SDK's JSON encoding or our checks.
for (const name of ["Object", "Array", "JSON", "Set", "Map", "Error", "Number", "String", "Promise", "RegExp", "Symbol", "Reflect"]) {
  const intrinsic = globalThis[name];
  if (intrinsic.prototype) Object.freeze(intrinsic.prototype);
  Object.freeze(intrinsic);
  Object.defineProperty(globalThis, name, {value: intrinsic, writable: false, configurable: false});
}
const scope = SCOPE;
const permittedHas = Set.prototype.has.bind(new Set(scope.methods));
const filesHas = Set.prototype.has.bind(new Set(scope.files));
const worldsHas = Set.prototype.has.bind(new Set(scope.evaluateWorlds));
const parse = JSON.parse, stringify = JSON.stringify;
const create = Object.create, keys = Object.keys, freeze = Object.freeze;
const isArray = Array.isArray;
const some = Function.prototype.call.bind(Array.prototype.some);
const NativeError = Error;
const deny = message => { const error = new NativeError(message); error.code = "harness-scope-denied"; throw error; };
const lower = Function.prototype.call.bind(String.prototype.toLowerCase);
const startsWith = Function.prototype.call.bind(String.prototype.startsWith);
const split = Function.prototype.call.bind(String.prototype.split);
const inside = (path, root) => { const p = lower(path), r = lower(root); return p === r || startsWith(p, r + "/"); };
const ownDownloads = new Set();
const ownHas = Set.prototype.has.bind(ownDownloads), ownAdd = Set.prototype.add.bind(ownDownloads);
// Literal canonical-path check; mirrors path_authorization.allowed for writes.
const writable = path => {
  const policy = scope.write;
  if (!policy || typeof path !== "string" || !startsWith(path, "/")) return false;
  const parts = split(path, "/");
  if (some(parts, part => part === "." || part === ".." ) || path.indexOf(String.fromCharCode(92)) >= 0 || path.indexOf("//") >= 0) return false;
  if (some(parts, part => some(policy.sensitive, name => lower(part) === name) || startsWith(lower(part), ".env."))) return false;
  if (some(policy.taskOutputs, root => inside(path, root))) return true;
  if (some(policy.denyRoots, root => inside(path, root))) return false;
  if (some(policy.allowRoots, root => inside(path, root))) return true;
  return policy.auto === true;
};
export function scopedApi(raw) {
  const bind = params => {
    if (params.pageId !== undefined && params.pageId !== scope.pageId) deny("Page is outside the task binding");
    if (params.fleetId !== undefined && params.fleetId !== scope.fleetId) deny("Fleet is outside the task binding");
  };
  const actions = create(null);
  for (const domain of keys(raw.actions)) {
    const group = create(null);
    for (const name of keys(raw.actions[domain])) {
      const method = domain + "." + name;
      group[name] = async value => {
        if (!permittedHas(method)) deny("Action requires an independently authorized tool: " + method);
        const params = parse(stringify(value === undefined ? {} : value));
        if (params === null || typeof params !== "object" || isArray(params)) deny("Action params must be an object");
        bind(params);
        if (method === "Page.list" || startsWith(method, "Download.") && method !== "Download.start") {
          params.fleetId = scope.fleetId;
          delete params.pageId;
        } else {
          // Execution binds only the Fleet so event waits can observe new tabs.
          // Every admitted action still targets this exact authorized page.
          params.pageId = scope.pageId;
        }
        if (method === "Download.start") {
          if (typeof params.savePath !== "string" || !writable(params.savePath))
            deny("Download.start savePath must be an absolute path the task may write (not protected): " + params.savePath);
        }
        if (method === "DOM.getImg") {
          const options = params.options;
          if (!options || typeof options !== "object" || !writable(options.path))
            deny("DOM.getImg options.path must be an absolute directory the task may write (not protected)");
          // Inline image data would travel in the execution result frame.
          options.format = "file";
        }
        if (method === "Download.control" && !ownHas(params.downloadId))
          deny("Download.control applies only to downloads started by this execution");
        if (method === "File.handleChooser" && (!isArray(params.files) || some(params.files, file => !filesHas(file)))) {
          deny("File is outside the authorized input file manifest; read or authorize the files and supply canonical paths in input");
        }
        if (method === "Runtime.evaluate") {
          // auto may silently retry in the main world; only declared worlds run.
          const world = params.world === undefined ? "isolated" : params.world;
          if (!worldsHas(world)) deny("Runtime.evaluate world is not declared in the package capabilities: " + world);
          params.world = world;
        }
        // Preserve the file-only transport invariant without discarding a
        // destination already allowed by the task's existing write policy.
        if (method === "Page.screenshot") {
          const path = params.options && typeof params.options === "object" ? params.options.path : undefined;
          if (path !== undefined && !writable(path))
            deny("Page.screenshot options.path must be an absolute path the task may write (not protected): " + path);
          params.options = path === undefined ? {format: "file"} : {format: "file", path};
        }
        const response = await raw.actions[domain][name](params);
        if (method === "Download.start" && response && typeof response === "object") {
          const id = response.downloadId || (response.data && response.data.downloadId);
          if (typeof id === "string") ownAdd(id);
        }
        return response;
      };
    }
    actions[domain] = freeze(group);
  }
  return freeze({
    actions: freeze(actions), context: freeze({...raw.context, pageId: scope.pageId}),
    readText: artifact => raw.readText(artifact), sleep: ms => raw.sleep(ms),
    waitFor: (options, trigger) => {
      if (options.fleetId !== undefined && options.fleetId !== scope.fleetId) deny("Fleet is outside the task binding");
      // Events are observations within the authorized Fleet, not grants to act
      // on their page. New pages are admitted by the host before the next run.
      return raw.waitFor({...options, fleetId: scope.fleetId}, trigger);
    }
  });
}
'''.replace("const scope = SCOPE;", "const scope = " + json.dumps(scope, ensure_ascii=False) + ";", 1)
    result = ScopedWorkflowPackage(copy.deepcopy(document))
    result["timeoutMs"] = execution_timeout_ms(document)
    # Declarations are Harness admission metadata, not platform source.
    result.pop("capabilities", None)
    result["files"][guard] = code
    result["files"][wrapper] = (
        "import {scopedApi} from " + json.dumps("./" + guard) + ";\n"
        "import main from " + json.dumps("./" + entry) + ";\n"
        "export default async function guarded(raw, input) { return main(scopedApi(raw), input); }\n"
    )
    result["entry"] = wrapper
    result.page_scope = (page_id, fleet_id, source_hash(result))
    return result, {"sourceHash": source_hash(document), "executionPackageHash": source_hash(result),
                    "scopeEntry": wrapper, "scopeModule": guard, "businessEntry": entry,
                    "sourceFiles": sorted(document["files"]), "authorizedFileCount": len(files),
                    "pageId": page_id, "fleetId": fleet_id, "allowedMethods": permitted,
                    "declaredCapabilities": declared, "deniedCapabilities": denied,
                    "evaluateWorlds": worlds,
                    "timeoutMs": result["timeoutMs"],
                    "writeMode": None if write_policy is None else ("auto" if write_policy.get("auto") else "granted-roots"),
                    "platformBinding": {"fleetId": fleet_id}}


def script_result_facts(result: Any) -> dict:
    """Execution state is objective; output still needs semantic interpretation."""
    response = result.get("response", {}) if isinstance(result, dict) else {}
    data = response.get("data", {}) if isinstance(response, dict) else {}
    data = data if isinstance(data, dict) else {}
    error = data.get("error") or {}
    from harness.workflow.call_facts import call_facts
    calls = call_facts(data)
    return {"status": data.get("status", "unknown"), "workflowId": data.get("workflowId"),
            "tool_was_executed": bool(data.get("taskId")), "source": error.get("source"),
            "failedActionCode": error.get("actionCode"), "failedAction": error.get("action"),
            "failedErrorCode": error.get("code"), "completedCalls": (data.get("callRecordsReference") or {}).get("total", len(data.get("results") or [])),
            "callFacts": calls}


def script_params_for_model(params: dict, scope: dict, source_reference: str) -> dict:
    """The raw request is logged; avoid echoing business and guard source.

    Caller passes the already redacted display view. This projection changes
    model context only, after the complete call receipt has been recorded.
    """
    package = params.get("_platformWorkflow") or {}
    return {"workflow": {"name": package.get("name"), "entry": scope.get("businessEntry"),
                         "sourceFiles": scope.get("sourceFiles"),
                         "sourceHash": scope.get("sourceHash"),
                         "executionPackageHash": scope.get("executionPackageHash")},
            "input": params.get("_platformInput", {}),
            "binding": {k: params[k] for k in ("pageId", "fleetId") if k in params},
            "timeoutMs": params.get("timeout"),
            "sourceRecordedIn": source_reference}
