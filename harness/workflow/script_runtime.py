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


def is_script_package(value: Any) -> bool:
    return isinstance(value, dict) and isinstance(value.get("entry"), str) and isinstance(value.get("files"), dict)


def script_contract_available() -> bool:
    from harness.workflow.workflow_schema_source import workflow_contract
    return "workflow" in workflow_contract().execute_properties


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
                  methods: set[str], files: list[str]) -> tuple[dict, dict]:
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
    permitted = sorted(m for m in methods
                       if (m.startswith(("Page.", "DOM.", "Input.")) or m == "File.handleChooser")
                       and m not in {"Page.create", "Page.switchTo", "Page.close", "Page.screenshot", "DOM.getImg"}
                       and not disabled_reason_for_method(m))
    scope = {"pageId": page_id, "fleetId": fleet_id, "methods": permitted, "files": files}
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
const parse = JSON.parse, stringify = JSON.stringify;
const create = Object.create, keys = Object.keys, freeze = Object.freeze;
const isArray = Array.isArray;
const some = Function.prototype.call.bind(Array.prototype.some);
const NativeError = Error;
const deny = message => { const error = new NativeError(message); error.code = "harness-scope-denied"; throw error; };
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
        if (method === "Page.list") {
          params.fleetId = scope.fleetId;
          delete params.pageId;
        } else {
          // Execution binds only the Fleet so event waits can observe new tabs.
          // Every admitted action still targets this exact authorized page.
          params.pageId = scope.pageId;
        }
        if (method === "File.handleChooser" && (!isArray(params.files) || some(params.files, file => !filesHas(file)))) {
          deny("File is outside the authorized input file manifest; read or authorize the files and supply canonical paths in input");
        }
        return raw.actions[domain][name](params);
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
    result = copy.deepcopy(document)
    result["files"][guard] = code
    result["files"][wrapper] = (
        "import {scopedApi} from " + json.dumps("./" + guard) + ";\n"
        "import main from " + json.dumps("./" + entry) + ";\n"
        "export default async function guarded(raw, input) { return main(scopedApi(raw), input); }\n"
    )
    result["entry"] = wrapper
    return result, {"sourceHash": source_hash(document), "executionPackageHash": source_hash(result),
                    "scopeEntry": wrapper, "scopeModule": guard, "businessEntry": entry,
                    "sourceFiles": sorted(document["files"]), "authorizedFileCount": len(files),
                    "pageId": page_id, "fleetId": fleet_id, "allowedMethods": permitted,
                    "platformBinding": {"fleetId": fleet_id}}


def script_result_facts(result: Any) -> dict:
    """Execution state is objective; output still needs semantic interpretation."""
    response = result.get("response", {}) if isinstance(result, dict) else {}
    data = response.get("data", {}) if isinstance(response, dict) else {}
    data = data if isinstance(data, dict) else {}
    error = data.get("error") or {}
    return {"status": data.get("status", "unknown"), "workflowId": data.get("workflowId"),
            "tool_was_executed": bool(data.get("taskId")), "source": error.get("source"),
            "failedActionCode": error.get("actionCode"), "failedAction": error.get("action"),
            "failedErrorCode": error.get("code"), "completedCalls": (data.get("callRecordsReference") or {}).get("total", len(data.get("results") or []))}


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
