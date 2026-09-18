import sys,json,tempfile,hashlib
from pathlib import Path
from types import SimpleNamespace
sys.path.insert(0,str(Path.cwd()))
from harness.workflow.workflow_policy import validate_workflow_params
from harness.results.numeric_facts import build_numeric_fact_index,resolve_numeric_claim,reconcile_numeric_claims
from harness.tools.browser_tools.downloads import _download_operation_key,_reusable_download_response,_download_receipt_store
from harness.tools.loop_guard import check_tool_call_loop
from harness.storage import create_storage
from harness.utils import RunLogger,read_task_file_text
from harness.evidence.extraction_artifacts import save_extraction_artifact
from harness.task_control.phase_lifecycle import _resume_artifact_integrity_error
from harness.results.completion_receipt import _artifact_row_count

def out(name,value):print(name,json.dumps(value,ensure_ascii=False,default=str))
# No AX handles: navigation followed by state and a selector-based text read.
steps=[{'action':'Page.navigate','params':{'url':'https://example.org'}},
       {'type':'readEvents','focus':['Page.loaded']},
       {'action':'Page.getState'},
       {'action':'DOM.getText','params':{'selector':'body'}}]
for enforce in (True,False):
 _,error=validate_workflow_params({'steps':steps},capability_methods=['Page.navigate','Page.getState','DOM.getText'],task_type='web_scrape',enforce_lifecycle=enforce)
 out('workflow_ax_'+str(enforce),error)
_,error=validate_workflow_params({'steps':[{'type':'waitEvent','focus':['Fleet.ready'],'timeout':1000}]},capability_methods=[],task_type='web_scrape')
out('event_static_exclusion',error)
with tempfile.TemporaryDirectory(prefix='gates-') as tmp:
 root=Path(tmp); d=root/'artifacts'/'extractions';d.mkdir(parents=True)
 old=d/'raw.json';new=d/'final.json'
 old.write_text(json.dumps({'name':'raw','rows':[{'id':'item-A','values':['a','a','b','c']}]}))
 new.write_text(json.dumps({'name':'final','rows':[{'id':'item-A','values':['a','b','c']}]}))
 state={'artifacts':[str(new)],'phases':{'p':{'status':'validated_done','validated_artifacts':[str(new)]}}}
 index=build_numeric_fact_index(state,task_dir=root)
 claim={'claimId':'c','text':'3','subject':'item-A','field':'values','metric':'count','unit':'field_entries','scope':'row','value':3}
 out('correct_dedup_claim',resolve_numeric_claim(claim,index))
 out('items_unit',resolve_numeric_claim({**claim,'unit':'items'},index))
 # Cached receipt survives destruction of the file it claims was delivered.
 missing=root/'deleted.bin'; missing.write_bytes(b'ok'); missing.unlink()
 agent=SimpleNamespace();params={'url':'https://example.org/a','savePath':str(missing)}
 _download_receipt_store(agent)[_download_operation_key(params)]={**params,'downloadId':'d1','state':'completed'}
 out('deleted_file_download_reuse',{'exists':missing.exists(),'receipt':_reusable_download_response(agent,params)})
 # Explicit overall budget remains, but duplicate-call policy ignores results.
 agent=SimpleNamespace(max_steps=100)
 for step in range(1,22):
  result=check_tool_call_loop(agent,name='browser_call',tool_input={'method':'Page.wheel','params':{'scrollY':600}},step=step)
 out('repeat_21',result)
with tempfile.TemporaryDirectory(prefix='gates-db-') as tmp:
 root=Path(tmp)/'worktree';logger=RunLogger(str(root),task_id='t1',run_id='run-1');store=create_storage(backend='db',worktree_dir=str(root));logger.attach_storage(store)
 try:
  store.create_task(task_id='t1',harness_version='v');store.start_run(task_id='t1',harness_version='v',run_id='run-1')
  saved=save_extraction_artifact(logger=logger,runtime=SimpleNamespace(harness=SimpleNamespace(runs_dir='')),artifacts=[],name='rows',rows=[{'id':'a'},{'id':'b'}])
  path=Path(saved['savedPath']);text=read_task_file_text(logger,str(path));digest=hashlib.sha256(text.encode()).hexdigest()
  state={'artifacts':[str(path)],'phases':{'p':{'status':'validated_done','validated_artifacts':[str(path)]}}}
  out('db_no_file',{'actualRows':len(json.loads(text)['rows']),'numericPlanFacts':build_numeric_fact_index(state,task_dir=logger.task_dir)['planFacts'],'completionRowCount':_artifact_row_count([str(path)]),'resumeError':_resume_artifact_integrity_error(logger,{str(path):digest},path)})
  path.write_text('{"rows":[]}')
  out('db_stale_file',{'readApiRows':len(json.loads(read_task_file_text(logger,str(path)))['rows']),'numericPlanFacts':build_numeric_fact_index(state,task_dir=logger.task_dir)['planFacts'],'resumeError':_resume_artifact_integrity_error(logger,{str(path):digest},path)})
 finally:store.close()
from harness.task_control import validate_worker_artifacts
from harness.tools.browser_tools.record_extraction import _record_extraction_content_warnings
with tempfile.TemporaryDirectory(prefix='gates-warning-') as tmp:
 root=Path(tmp);logger=RunLogger(str(root),'t');path=logger.artifacts_dir/'extractions'/'old.json';path.parent.mkdir(parents=True,exist_ok=True)
 rows=[{'id':'one','observed':'loading...'}]
 warnings=_record_extraction_content_warnings(rows)
 if not warnings:
  rows=[{'id':'one','observed':'placeholder'}];warnings=_record_extraction_content_warnings(rows)
 contract={'expected_artifact':{'name':'old','fields':['id','observed']},'validators':[{'type':'exact_rows','value':1}]}
 for include in (True,False):
  payload={'name':'old','rows':rows}
  if include:payload['schemaWarnings']=warnings
  path.write_text(json.dumps(payload))
  r=validate_worker_artifacts(contract=contract,artifacts=[str(path)],logger=logger,task_dir=logger.task_dir)
  out('historic_warnings_'+str(include),{'warnings':warnings,'status':r['status'],'failures':r['failures']})
