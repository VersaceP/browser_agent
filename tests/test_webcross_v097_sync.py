"""Source-package protocol, per-call permission and consumer-mode regressions."""
import asyncio
import json
import subprocess
from pathlib import Path

import pytest

from harness.workflow.script_runtime import scope_package, input_files, script_tool_schema
from harness.workflow.workflow_wire import to_platform_execute_params
from harness.workflow.workflow_definitions import save_workflow_definition, load_workflow_definition
from harness.workflow.workflow_projection import workflow_execution_facts
from harness.observation.page_observation import hydrate_page_observation_response
from harness.skill_builder.resources import workflow_entries
from harness.tools.browser_tools.dispatch import build_browser_agent_tool_specs
from harness.workflow.workflow_schema_source import bind_schemas_dir

FIXTURES = Path(__file__).parent / "fixtures" / "webcross_v097"


def package(source="export default async function main(api, input) { return input; }"):
    return {"schemaVersion": 1, "name": "fixture", "entry": "main.js", "files": {"main.js": source}}


@pytest.fixture(autouse=True)
def live_contract():
    bind_schemas_dir(FIXTURES)
    yield
    bind_schemas_dir(None)


def run_guest(tmp_path, source, business_input, *, methods=None, files=None):
    original = package(source)
    guarded, receipt = scope_package(original, page_id="page", fleet_id="fleet",
        methods=methods or {"Page.getState", "File.handleChooser", "Network.readApi"}, files=files or [])
    for name, text in guarded["files"].items():
        target = tmp_path / name; target.parent.mkdir(parents=True, exist_ok=True); target.write_text(text)
    code = '''import main from ENTRY;
let calls = [];
const raw = { context: {fleetId:'fleet'},
  actions: {Page:{getState:async p => {calls.push(p); return {title:'fixture'};},
                 list:async p => {calls.push(p); return [];}},
            File:{handleChooser:async p => {calls.push(p); return {accepted:true};}},
            Network:{readApi:async p => {calls.push(p); return {secret:'unreachable'};}}},
  readText:async () => 'text', sleep:async () => {}, waitFor:async options => {calls.push({waitOptions:options});return {event:{pageId:'new-page'}};}};
try {const output = await main(raw, INPUT); console.log(JSON.stringify({output,calls}));}
catch(e) {console.log(JSON.stringify({error:e.message,code:e.code,calls}));}
'''.replace("ENTRY", json.dumps((tmp_path / guarded["entry"]).as_uri())).replace("INPUT", json.dumps(business_input))
    executed = subprocess.run(["node", "--input-type=module", "-e", code], check=True, text=True, capture_output=True)
    assert original["files"]["main.js"] == source
    return json.loads(executed.stdout), receipt


def test_wire_separates_source_input_and_binding():
    doc = package()
    wire = {"workflow": doc, "input": {"sizes": ["S", "L"]}, "binding": {"pageId": "page"}}
    assert to_platform_execute_params(wire) == wire
    assert to_platform_execute_params({"_platformWorkflow": doc, "_platformInput": [1, 2], "pageId": "page"})["input"] == [1, 2]
    assert "steps" not in script_tool_schema()["properties"]


def test_variable_lengths_reuse_identical_source(tmp_path):
    source = "export default async function main(api, input) { return input.values.map(x => x * 2); }"
    first, a = run_guest(tmp_path, source, {"values": [1]})
    second, b = run_guest(tmp_path, source, {"values": [2, 3, 4]})
    assert first["output"] == [2] and second["output"] == [4, 6, 8]
    assert a["sourceHash"] == b["sourceHash"] and a["executionPackageHash"] == b["executionPackageHash"]


@pytest.mark.parametrize("domain,method,params", [
    ("Page", "getState", {"pageId": "other"}),
    ("Page", "getState", {"fleetId": "other"}),
    ("Network", "readApi", {}),
    ("File", "handleChooser", {"files": ["/not-authorized.txt"]}),
])
def test_computed_calls_cannot_expand_scope(tmp_path, domain, method, params):
    source = "export default async function main({actions}, input) { const call = actions[input.domain][input.method]; return call(input.params); }"
    result, _ = run_guest(tmp_path, source, {"domain": domain, "method": method, "params": params})
    assert result["code"] == "harness-scope-denied" and result["calls"] == []


def test_authorized_file_can_be_assigned(tmp_path):
    result, _ = run_guest(tmp_path, "export default async function main({actions}, input) { return actions.File.handleChooser({files:input.files}); }", {"files": ["/allowed.txt"]}, files=["/allowed.txt"])
    assert result["output"] == {"accepted": True}


def test_fleet_events_do_not_expand_page_action_scope(tmp_path):
    source = '''export default async function main({actions,waitFor,context}) {
      const event = await waitFor({events:['Page.open'],fleetId:context.fleetId});
      await actions.Page.getState({});
      try {await actions.Page.getState({pageId:event.event.pageId});}
      catch(e) {return {pageId:context.pageId,denied:e.code};}
    }'''
    result, receipt = run_guest(tmp_path, source, {})
    assert result['output'] == {'pageId':'page','denied':'harness-scope-denied'}
    assert result['calls'] == [{'waitOptions':{'events':['Page.open'],'fleetId':'fleet'}}, {'pageId':'page'}]
    assert receipt['platformBinding'] == {'fleetId':'fleet'}


def test_events_and_page_inventory_cannot_escape_fleet(tmp_path):
    source = "export default async function main({waitFor}) {return waitFor({events:['Page.open'],fleetId:'other'});}"
    result, _ = run_guest(tmp_path, source, {})
    assert result['code'] == 'harness-scope-denied' and not result['calls']
    source = "export default async function main({actions}) {return actions.Page.list({});}"
    result, _ = run_guest(tmp_path, source, {}, methods={'Page.list'})
    assert result['calls'] == [{'fleetId':'fleet'}]


def test_host_scoped_package_uses_explicit_fleet_binding_on_wire():
    wire = to_platform_execute_params({'_platformWorkflow':package(), '_platformInput':{},
        'pageId':'page', 'fleetId':'fleet', '_platformBinding':{'fleetId':'fleet'}})
    assert wire['binding'] == {'fleetId':'fleet'}


def test_source_path_cannot_import_outside_package():
    bad = package(); bad["files"]["../outside.js"] = "export default 1;"
    with pytest.raises(ValueError):
        scope_package(bad, page_id="page", fleet_id="fleet", methods=set(), files=[])


def test_formal_entry_list_excludes_inputs_and_probes(tmp_path):
    for path in ["workflow.json", "workflows/other.json", "params/input.json", "trials/probe.workflow.json"]:
        target = tmp_path / path; target.parent.mkdir(parents=True, exist_ok=True); target.write_text(json.dumps(package()))
    assert [e["path"] for e in workflow_entries(tmp_path)] == ["workflow.json", "workflows/other.json"]


def test_selected_resource_tool_is_browser_mode_only():
    args = dict(workflow_enabled=True, selected_skill_available=True)
    browser = build_browser_agent_tool_specs({"Workflow.execute"}, standalone_review_enabled=True, **args)
    lead = build_browser_agent_tool_specs({"Workflow.execute"}, standalone_review_enabled=False, **args)
    assert "read_selected_skill_resource" in {t["name"] for t in browser}
    assert "read_selected_skill_resource" not in {t["name"] for t in lead}


def test_skill_preference_prompt_is_browser_mode_only():
    from tests.test_browser_prompt_consistency import make_agent
    browser = make_agent(True, False); browser.runtime.harness.forced_skill_id = "demo"; browser.standalone_browser_mode = True
    lead = make_agent(True, False); lead.runtime.harness.forced_skill_id = "demo"; lead.standalone_browser_mode = False
    browser_prompt = browser._build_system_prompt()
    lead_prompt = lead._build_system_prompt()
    # Skill orchestration belongs to Skill Runtime; Browser keeps only the
    # generic WebCross Workflow contract and must not receive the old Skill
    # execution policy based on forced_skill_id.
    assert "Prefer execute_published_skill_workflow" not in browser_prompt
    assert "read_selected_skill_resource" not in browser_prompt
    assert "Prefer execute_published_skill_workflow" not in lead_prompt
    assert "native variables" in browser_prompt


def detail(source="live"):
    return {"data": {"pageId": "page", "mode": "detail", "source": source, "documentEpoch": "epoch",
        "summary": {"total": 1, "succeeded": 1, "failed": 0}, "details": [
            {"index": 0, "ok": True, "target": {"id": "node"}, "view": "state", "value": {"value": "sample"}}]}}


def test_inline_details_preserve_facts_and_check_identity():
    raw = detail(); got = hydrate_page_observation_response({"pageId": "page", "query": {}}, raw, method="DOM.getDetails")
    assert got["data"]["records"][0]["value"] == {"value": "sample"}
    assert "records" not in raw["data"]
    bad = hydrate_page_observation_response({"pageId": "different", "query": {}}, raw)
    assert bad["data"]["observationError"]["code"] == "detail-identity-mismatch"
    raw["data"]["summary"]["failed"] = 1
    assert "observationError" in hydrate_page_observation_response({"pageId": "page"}, raw)["data"]


def test_source_error_and_cancelled_are_not_business_success():
    result = {"response": {"data": {"workflowId": "wf", "taskId": "task", "status": "cancelled",
        "output": None, "results": [{"status": "unknown"}], "error": {"code": "workflow-cancelled", "source": {"file": "main.ts", "line": 3}}}}}
    facts = workflow_execution_facts(result)
    assert facts["status"] == "cancelled" and facts["tool_was_executed"]
    assert facts["source"] == {"file": "main.ts", "line": 3}


def test_mutating_intrinsics_cannot_expand_scope(tmp_path):
    source = """export default async function main({actions}) {
      try {Set.prototype.has = () => true;} catch(e) {}
      try {globalThis.Object = {create:() => ({})};} catch(e) {}
      return actions.Network.readApi({});
    }"""
    result, _ = run_guest(tmp_path, source, {})
    assert result["code"] == "harness-scope-denied" and result["calls"] == []


def test_file_manifest_requires_existing_permission_and_canonical_path(tmp_path):
    from types import SimpleNamespace
    from harness.tools.path_authorization import state
    root=tmp_path / "task"; root.mkdir()
    own=root / "own.txt"; own.write_text("owned")
    external=tmp_path / "external.txt"; external.write_text("external")
    owner=SimpleNamespace(logger=SimpleNamespace(task_dir=root))
    assert input_files(owner,{"files":[str(own),str(external)]}) == [str(own)]
    state(owner)["readRoots"].append(str(external))
    assert input_files(owner,{"files":[str(external)]}) == [str(external)]


def test_projection_preserves_output_and_complete_call_count(tmp_path):
    from harness.utils import RunLogger
    from harness.workflow.workflow_projection import project_workflow_receipt
    result={"response":{"data":{"workflowId":"wf","taskId":"task","status":"succeeded",
        "output":{"business":"kept"},"results":[{"callId":str(i),"kind":"action","action":"Page.getState","status":"succeeded","value":{"title":"x"}} for i in range(100)]}}}
    projection=project_workflow_receipt(logger=RunLogger(str(tmp_path)),result=result)
    assert projection["projected"]
    assert result["response"]["data"]["output"] == {"business":"kept"}
    assert workflow_execution_facts(result)["completedCalls"] == 100
    assert result["response"]["data"]["callRecordsReference"]["total"] == 100


def test_compile_source_and_uncertain_delivery_are_preserved():
    facts=workflow_execution_facts({"error":"compile error","rpcData":{"details":{"code":"workflow-compile-failed","source":{"file":"main.ts","line":3}}}})
    assert facts["status"] == "failed" and facts["tool_was_executed"] is False
    assert facts["source"] == {"file":"main.ts","line":3}
    facts=workflow_execution_facts({"error":"timeout","transportCode":"ABCP_TRANSPORT_CALL_TIMEOUT","requestSent":True,"executionTrace":{"workflowId":"live"}})
    assert facts["status"] == "unknown" and facts["workflowId"] == "live"


def test_source_definition_reuses_source_and_rebinds_input(tmp_path):
    from harness.utils import RunLogger
    from harness.workflow.workflow_definitions import apply_workflow_definition_patch
    logger=RunLogger(str(tmp_path))
    original={"workflow":package(),"input":{"values":[1]}}
    receipt=save_workflow_definition(logger,original)
    loaded,error=load_workflow_definition(logger,definition_ref=receipt["definitionRef"],expected_hash=receipt["definitionHash"])
    assert error is None and loaded == original
    updated,error=apply_workflow_definition_patch(loaded,[{"op":"set","path":"/input/values","value":[2,3]}])
    assert error == [] and updated["workflow"] == original["workflow"]
    assert updated["input"] == {"values":[2,3]}


def test_client_waits_for_declared_script_deadline_without_replay():
    from abcp_client import ABCPClient
    from runtime_config import ABCPClientConfig
    from unittest.mock import patch
    observed=[]
    async def case():
        client=ABCPClient(ABCPClientConfig(call_timeout_seconds=1))
        class Socket:
            count=0
            async def send(self,payload):
                self.count+=1
                request=json.loads(payload)
                client._pending[request['id']].future.set_result({'result':{'data':{'status':'succeeded','output':{},'results':[]}}})
        socket=Socket();client._ws=socket
        real=asyncio.wait_for
        async def wait(future,timeout):
            observed.append(timeout)
            return await real(future,timeout)
        doc=package();doc['timeoutMs']=70000
        with patch('abcp_client.asyncio.wait_for',side_effect=wait):
            await client.call('Workflow.execute',{'workflow':doc,'input':{}})
        assert socket.count==1
    asyncio.run(case())
    assert observed==[80]




def test_model_projection_omits_source_but_keeps_audit_identity():
    from harness.workflow.script_runtime import script_params_for_model
    doc=package()
    guarded,scope=scope_package(doc,page_id='page',fleet_id='fleet',methods={'Page.getState'},files=[])
    raw={'_platformWorkflow':guarded,'_platformInput':{'name':'actual'},'pageId':'page','fleetId':'fleet','timeout':30000}
    shown=script_params_for_model(raw,scope,"task-resource:complete-request")
    assert shown['input']=={'name':'actual'} and shown['binding']=={'pageId':'page','fleetId':'fleet'}
    assert shown['workflow']['entry']=='main.js'
    assert shown['workflow']['sourceHash']==scope['sourceHash']
    assert 'files' not in shown['workflow'] and '_platformWorkflow' not in shown
    assert raw['_platformWorkflow']==guarded
    assert shown['sourceRecordedIn']=='task-resource:complete-request'
