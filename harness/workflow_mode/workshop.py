"""Explicit Workshop commands and version-bound source retrieval.

Only the CLI command host calls publish/unpublish. Runtime model tools do not
receive these methods or persistence grants. No write is automatically retried.
"""
from __future__ import annotations
import asyncio
import copy
import json
import shutil
import uuid
from pathlib import Path
from harness.workflow_mode import package as pkg
from harness.workflow_mode.registry import RegistryError, RegistryNotFound, _write_json, _now
from harness.workflow.script_runtime import source_hash


def data_of(response):
    return response.get('data', response) if isinstance(response,dict) else response


class Workshop:
    def __init__(self, runtime, drafts, *, client=None):
        self.runtime, self.drafts = runtime,drafts
        if client is None:
            from abcp_client import ABCPClient
            # Reuse the selected Harness connection config. Business writes
            # use the native client directly, without a replay/retry wrapper.
            client=ABCPClient(runtime.browser)
        self.client=client
        self.ready=False
        self.schemas={}

    async def __aenter__(self):
        try:
            await self.client.connect()
            await self.client.call('System.register',{})
        except BaseException:
            await self.client.close()
            raise
        self.ready=True
        return self

    async def __aexit__(self,*args):
        await self.client.close()

    def _receipt(self,method,params,*,response=None,error=None):
        identity=uuid.uuid4().hex
        path=self.drafts.root/'.operations'/(identity+'.json')
        if path.parent.is_symlink():raise RegistryError('Workshop receipts directory must not be a symbolic link')
        _write_json(path,{'at':_now(),'method':method,'params':params,'response':response,'error':error})
        return str(path)

    async def call(self,method,params):
        if method not in {'Workflow.list','Workflow.upsert','Workflow.remove'}:
            raise RegistryError('Unsupported Workshop command')
        # The deployed action contract is the authority. Describe once per
        # connection; no global-cache guess and no shell interpolation/argv limit.
        if method not in self.schemas:
            result=data_of(await self.client.call('System.describeAction',{'method':method}))
            schema=result.get('inputSchema') if isinstance(result,dict) else None
            if not isinstance(schema,dict):raise RegistryError('WebCross did not provide the '+method+' contract')
            self.schemas[method]=schema
        from harness.tools.argument_pipeline import validate_schema
        issues=validate_schema(params,self.schemas[method])
        if issues:raise RegistryError('Workshop parameters do not match the live contract: '+str(issues))
        try:
            response=await self.client.call(method,params)
        except Exception as exc:
            failure={'type':type(exc).__name__,'message':str(exc),
                     **{name:getattr(exc,attr,None) for name,attr in
                        [('transportCode','transport_code'),('rpcCode','rpc_code'),('rpcMethod','rpc_method'),
                         ('requestSent','request_sent'),('requestId','request_id'),('details','rpc_data')]}}
            ref=self._receipt(method,params,error=failure)
            state='failed before dispatch' if failure['requestSent'] is False else 'did not return confirmation; reconcile current Workshop state before retrying'
            raise RegistryError(f'{method} {state}. {str(exc)[:200]} Receipt: {ref}') from exc
        ref=self._receipt(method,params,response=response)
        if isinstance(response,dict) and response.get('error'):
            raise RegistryError(f'{method} failed; receipt: {ref}')
        return data_of(response),ref

    async def listing(self):
        data,ref=await self.call('Workflow.list',{'limit':1000})
        if not isinstance(data,dict) or not isinstance(data.get('items'),list):
            raise RegistryError('Workshop did not return a valid inventory; receipt: '+ref)
        data['inventoryLimit']=1000
        data['possiblyTruncated']=len(data['items'])>=1000
        return data,ref

    async def resolve(self,reference):
        text=str(reference).strip()
        try:
            identity=str(uuid.UUID(text)); is_id=True
        except ValueError:is_id=False
        if is_id:
            data,ref=await self.call('Workflow.list',{'workshopId':identity})
        else:
            data,ref=await self.call('Workflow.list',{'query':text,'limit':1000})
        items=data.get('items') if isinstance(data,dict) else None
        if not isinstance(items,list):raise RegistryError('Invalid Workshop lookup; receipt: '+ref)
        if not is_id:
            if len(items)>=1000:raise RegistryError('Workshop search reached its result limit; use an exact workshopId')
            items=[i for i in items if isinstance(i,dict) and i.get('name','').casefold()==text.casefold()]
        if not items:
            error=RegistryNotFound('No Workshop workflow matches '+repr(text));error.lookup_receipt=ref
            raise error
        if len(items)!=1 or not isinstance(items[0],dict):
            raise RegistryError('Workshop name is ambiguous; use an exact workshopId')
        item=items[0]
        if not is_id:return await self.resolve(item['workshopId'])
        if (item.get('workshopId')!=identity or not isinstance(item.get('manifest'),dict)
                or type(item.get('revision')) is not int):
            raise RegistryError('Workshop exact lookup lacks matching identity, revision or complete manifest')
        return {**item,'lookupReceipt':ref}

    def source_snapshot(self,item):
        """A hidden immutable cache is execution evidence, never a local market."""
        manifest=item['manifest'];identity=item['workshopId'];digest=source_hash(manifest)
        # Enrich only a remotely selected, exact manifest with its hash-proven
        # author contract. Never execute the local working copy or discover a
        # workflow from local history instead of the Workshop inventory.
        for local in self.drafts.list_drafts(include_deleted=True,with_hashes=False):
            remote=local.get('remote') or local.get('lastRemote') or {}
            version=remote.get('uploadedPackageHash')
            if remote.get('workshopId')!=identity or remote.get('manifestHash')!=digest or not isinstance(version,str):continue
            if len(version)!=64 or any(c not in '0123456789abcdef' for c in version):continue
            source=Path(local['path'])/'history'/'versions'/version
            if source.is_dir() and pkg.content_hash(source)==version and pkg.workshop_document(source)==manifest:
                return {'origin':'workshop','slug':local['slug'],'templateId':local['templateId'],
                        'displayName':manifest['name'],'contentHash':version,'packagePath':str(source),
                        'workshopId':identity,'revision':item['revision'],'manifestHash':digest,'lookupReceipt':item['lookupReceipt']}
        target=self.drafts.root/'.history'/'remote'/identity/digest
        if any(p.is_symlink() for p in (self.drafts.root/'.history',target.parent.parent,target.parent,target)):
            raise RegistryError('Remote snapshot directories must not be symbolic links')
        if target.exists():
            saved=pkg.read_json(target/'remote.json')
            if saved.get('manifest')!=manifest or pkg.content_hash(target)!=saved.get('packageHash'):
                raise RegistryError('Saved remote execution snapshot changed')
        else:
            if manifest.get('kind')!='webcross-workshop':raise RegistryError('Invalid Workshop manifest kind')
            meta={k:copy.deepcopy(v) for k,v in manifest.items() if k not in {'files','kind'}}
            files=manifest.get('files')
            if not isinstance(files,dict) or not files:raise RegistryError('Workshop manifest contains no source')
            import tempfile
            target.parent.mkdir(parents=True,exist_ok=True)
            staging=Path(tempfile.mkdtemp(prefix='.remote-',dir=target.parent))
            try:
                _write_json(staging/'workflow.json',meta)
                if any(not isinstance(name,str) for name in files):raise RegistryError('Invalid module path')
                prefix='' if all(name.startswith('src/') for name in files) else 'src/workshop/'
                if prefix:
                    meta['entry']=prefix+manifest['entry']
                    _write_json(staging/'workflow.json',meta)
                for name,value in files.items():
                    if not isinstance(name,str) or not isinstance(value,str):
                        raise RegistryError('Workshop source requires textual package-relative modules')
                    # Validate the original path before prefixing it.
                    pkg.safe_relative(staging,name)
                    path=pkg.safe_relative(staging,prefix+name,writable=True);path.parent.mkdir(parents=True,exist_ok=True);path.write_text(value,encoding='utf-8')
                contract=pkg.new_contract('workshop-'+identity[:8]);contract['templateId']=str(uuid.uuid5(uuid.NAMESPACE_URL,'webcross-workshop:'+identity))
                _write_json(staging/'contract.json',contract)
                _write_json(staging/'remote.json',{'manifest':manifest,'workshopId':identity,'revision':item['revision'],'packageHash':pkg.content_hash(staging)})
                if pkg.check_structure(staging)['status']!='valid':raise RegistryError('Remote source is not a valid executable package')
                staging.rename(target)
            finally:shutil.rmtree(staging,ignore_errors=True)
        contract=pkg.load_contract(target)
        return {'origin':'workshop','slug':contract['slug'],'templateId':contract['templateId'],
                'displayName':manifest['name'],'contentHash':pkg.content_hash(target),'packagePath':str(target),
                'workshopId':identity,'revision':item['revision'],'manifestHash':digest,'lookupReceipt':item['lookupReceipt']}

    async def checkout(self,reference,*,request):
        item=await self.resolve(reference)
        existing=self.drafts.resolve_edit_draft(item['workshopId'])
        if existing:return existing
        source=self.source_snapshot(item);task=uuid.uuid4().hex
        slug=source['slug'];target=self.drafts._directory(slug)
        if target.exists():
            record=pkg.read_json(target/'draft.json') if (target/'draft.json').is_file() else {}
            if record.get('status')!='deleted':raise RegistryError('A local directory already uses this Workshop identity; choose the local draft explicitly')
            slug=slug+'-'+task[:8];target=self.drafts._directory(slug)
        pkg.copy_managed(Path(source['packagePath']),target)
        if slug!=source['slug']:
            contract=pkg.load_contract(target);_write_json(target/'contract.json',{**contract,'slug':slug})
        local_hash=pkg.content_hash(target)
        row=self.drafts.write_draft({'taskId':task,'templateId':source['templateId'],'slug':slug,
            'displayName':item['manifest']['name'],'kind':'checkout','status':'saved','createdAt':_now(),
            'request':request,'baseHash':local_hash,'savedHash':local_hash,
            'remote':{'workshopId':item['workshopId'],'revision':item['revision'],'manifestHash':source['manifestHash']}})
        self.drafts.checkpoint(task,reason='checkout',summary=request)
        return row

    async def publish(self,reference,*,target=None):
        row=self.drafts.resolve_draft(reference);root=Path(row['path'])
        digest=self.drafts.checkpoint(row['taskId'],reason='publish-preparation')
        # Pack the exact immutable version, not a concurrently changing draft.
        source=root/'history'/'versions'/digest
        contract=pkg.load_contract(source)
        if contract['templateId']!=row['templateId'] or contract['slug']!=row['slug']:
            raise RegistryError('Upload contract differs from the local draft identity')
        checked=await asyncio.to_thread(pkg.check,source)
        if checked['status']!='valid':raise RegistryError('Package check did not pass: '+json.dumps(checked,ensure_ascii=False))
        artifact=root/'history'/(digest+'.workshop.json')
        from asyncio.subprocess import PIPE
        cli=shutil.which('webcross')
        if not cli:raise RegistryError('webcross CLI is required for workflow pack')
        process=await asyncio.create_subprocess_exec(cli,'workflow','pack',str(source),'--out',str(artifact),stdout=PIPE,stderr=PIPE)
        try:out,err=await asyncio.wait_for(process.communicate(),timeout=120)
        except BaseException:
            if process.returncode is None:process.kill();await process.wait()
            raise
        if process.returncode:raise RegistryError('webcross workflow pack failed: '+(err or out).decode(errors='replace')[-2000:])
        manifest=pkg.read_json(artifact)
        if manifest != pkg.workshop_document(source):raise RegistryError('CLI packed different source/metadata')
        remote=row.get('remote') or {};params={'manifest':manifest}
        if target:
            selected=await self.resolve(target)
            remote={'workshopId':selected['workshopId'],'revision':selected['revision'],
                    'manifestHash':source_hash(selected['manifest'])}
            self.source_snapshot(selected)
        if remote.get('workshopId'):
            try:current=await self.resolve(remote['workshopId'])
            except RegistryNotFound as exc:
                raise RegistryError('Linked Workshop record is absent. Reconcile with /workflow-unpublish '+remote['workshopId']+
                                    ' before uploading a new record; local source is retained.') from exc
            if current['manifest']==manifest:
                # A prior write may have succeeded without its acknowledgement.
                # Reconcile the exact content; do not issue another upsert just
                # to recover local bookkeeping or advance the revision.
                return self._remember_publication(row,digest,manifest,current,
                    current['lookupReceipt'],write_performed=False)
            # Revision also changes for menu enablement/folder management.
            # These fields are omitted from this upsert and preserved by the
            # live contract. Compare the content being replaced to its base.
            if source_hash(current['manifest'])!=remote.get('manifestHash'):
                raise RegistryError('Workshop manifest changed since checkout/publish; review the remote version '
                    'before overwriting. Expected manifest '+str(remote.get('manifestHash'))+
                    ', observed '+source_hash(current['manifest'])+'. Receipt: '+current['lookupReceipt'])
            params['workshopId']=remote['workshopId']
        else:params['enabled']=False
        data,receipt=await self.call('Workflow.upsert',params)
        self.drafts.operation(row,'upload-response',{'contentHash':digest,'receipt':receipt,'result':data})
        identity=(data.get('item') or {}).get('workshopId') if isinstance(data,dict) else None
        if not identity:raise RegistryError('Upload returned no Workshop identity; inspect receipt '+receipt+' before retrying')
        try:identity=str(uuid.UUID(identity))
        except (ValueError,TypeError,AttributeError):raise RegistryError('Upload returned an invalid Workshop identity; receipt: '+receipt) from None
        if params.get('workshopId') and identity!=params['workshopId']:
            raise RegistryError('Upload response identifies a different Workshop record; reconcile receipt '+receipt+' before retrying')
        confirmed=await self.resolve(identity)
        if confirmed['manifest']!=manifest:raise RegistryError('Workshop readback differs from uploaded manifest; receipt: '+receipt)
        return self._remember_publication(row,digest,manifest,confirmed,receipt,write_performed=True)

    def _remember_publication(self,row,digest,manifest,confirmed,receipt,*,write_performed):
        latest=self.drafts.read_draft(row['taskId'])
        mapping={'workshopId':confirmed['workshopId'],'revision':confirmed['revision'],
                 'manifestHash':source_hash(manifest),'uploadedPackageHash':digest,'receipt':receipt,
                 'confirmation':'upload-readback' if write_performed else 'observed-identical'}
        self.drafts.write_draft({**latest,'remote':mapping})
        self.drafts.operation(row,'publish' if write_performed else 'publish-reconciled',
                              {'contentHash':digest,'remote':mapping,'writePerformed':write_performed})
        return {'status':'uploaded' if write_performed else 'already_synced',
                'slug':row['slug'],'contentHash':digest,'writePerformed':write_performed,**mapping}

    async def unpublish(self,reference):
        try:item=await self.resolve(reference)
        except RegistryNotFound as exc:
            try:identity=str(uuid.UUID(reference))
            except (ValueError,TypeError,AttributeError):raise exc
            for row in self.drafts.list_drafts(include_deleted=True,with_hashes=False):
                if (row.get('remote') or {}).get('workshopId')==identity:
                    self.drafts.write_draft({**row,'remote':None,'lastRemote':row['remote']})
                    self.drafts.operation(row,'unpublish-reconciled',{'workshopId':identity,'receipt':exc.lookup_receipt,
                        'note':'Confirmed absent at lookup; no remove was sent and prior operation cause is unknown'})
            return {'status':'already_absent','workshopId':identity,'receipt':exc.lookup_receipt}
        snapshot=self.source_snapshot(item)  # Retain the exact source before removal.
        if self.drafts.resolve_edit_draft(item['workshopId']) is None:
            await self.checkout(item['workshopId'],request='Retained locally before explicit Workshop unpublish')
        identity=item['workshopId']
        data,receipt=await self.call('Workflow.remove',{'workshopId':identity})
        if not isinstance(data,dict) or data.get('deleted') is not True:
            raise RegistryError('Removal was not confirmed; receipt: '+receipt)
        remaining,_=await self.call('Workflow.list',{'workshopId':identity})
        if not isinstance(remaining,dict) or remaining.get('items')!=[]:
            raise RegistryError('Workshop removal readback did not confirm absence; receipt: '+receipt)
        for row in self.drafts.list_drafts(include_deleted=True,with_hashes=False):
            if (row.get('remote') or {}).get('workshopId')==identity:
                self.drafts.write_draft({**row,'remote':None,'lastRemote':row['remote']})
                self.drafts.operation(row,'unpublish',{'workshopId':identity,'snapshot':snapshot,'receipt':receipt})
        return {'status':'unpublished','workshopId':identity,'receipt':receipt,'retainedSnapshot':snapshot['packagePath']}
