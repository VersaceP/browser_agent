"""One Agent, a bounded set of deterministic executions, task-owned receipts."""
from __future__ import annotations
import asyncio
import copy
import json
import time
import uuid
from harness.workflow_mode import package as pkg
from harness.workflow_mode.concurrency import lease, parallelism
from harness.workflow_mode.input_reference import resolve_input, bind_input
from harness.workflow_mode.platform import WorkflowPlatform, OBSERVE_METHODS
from harness.workflow_mode.tools import result, compact


def recover_interrupted(state):
    changed = False
    for batch in state.get('executionBatches', []):
        if batch.get('status') != 'running': continue
        batch['status'] = 'interrupted'
        for item in batch['items']:
            if item['status'] == 'running':
                item.update(status='unknown', error='Previous process ended without a confirmed item result')
            elif item['status'] == 'pending': item['status'] = 'not_dispatched'
        changed = True
    return changed


async def execute_batch(session, args):
    session._check_stop()
    if session.profile not in {'run', 'author'}:
        raise PermissionError('Batch execution is available only to run/author sessions')
    items = args['items']
    if not isinstance(items, list) or not items:
        raise ValueError('Supply explicit independent work items')
    ids = [item.get('id') for item in items]
    if any(not isinstance(i, str) or not i.strip() for i in ids) or len(set(ids)) != len(ids):
        raise ValueError('Each batch item requires a distinct nonempty id')
    count = args.get('concurrency', parallelism(session.runtime))
    if type(count) is not int or not 1 <= count <= parallelism(session.runtime):
        raise ValueError('concurrency must be within the configured workflow_parallelism')
    known = session.known_refs()
    base = resolve_input(args, known_refs=known, read=session.store.read_ref)
    digest, root = session._pinned()
    document = pkg.execution_document(root)
    prepared, inventories = [], {}
    # Validate the complete dispatch description before issuing any package action.
    for item in items:
        fleet, page = item['fleetId'], item['pageId']
        if fleet not in session.state.get('authorizedFleets', []):
            raise PermissionError('Batch Fleet must already be authorized by bind_page/create_page')
        if fleet not in inventories:
            inventory = await session.platform.call('Page.list', {'fleetId': fleet}, allowed=OBSERVE_METHODS)
            if not inventory.get('ok') or not isinstance(inventory.get('data'), list):
                raise ValueError('Live page membership could not be confirmed')
            inventories[fleet] = {p.get('pageId') for p in inventory['data'] if isinstance(p, dict)}
        if page not in inventories[fleet]:
            raise ValueError('Batch page is absent from the selected Fleet')
        overrides = item.get('input_overrides', {})
        if not isinstance(overrides, dict):
            raise ValueError('input_overrides must be an object')
        business = bind_input({**copy.deepcopy(base), **copy.deepcopy(overrides)},
                              item.get('input_bindings', []), known_refs=known, read=session.store.read_ref)
        files, missing = session._input_files(business)
        if missing:
            raise PermissionError('Batch input files require existing authorization: '+str(missing[:10]))
        resources = item.get('resource_keys', [])
        if not isinstance(resources, list) or any(not isinstance(k, str) or not k.strip() for k in resources):
            raise ValueError('resource_keys must be explicit nonempty strings')
        prepared.append((item, business, files, resources))
    owner = session.runs.claim_pages(session.logger.task_id, [item['pageId'] for item in items])
    if owner:
        raise PermissionError('Batch page is bound by live task '+owner)
    batch_id = uuid.uuid4().hex
    record = {'batchId': batch_id, 'contentHash': digest, 'status': 'running',
              'reason': args['reason'], 'concurrency': count,
              'items': [{'id': i['id'], 'pageId': i['pageId'], 'fleetId': i['fleetId'],
                         'resource_keys': keys, 'input': business, 'status': 'pending'}
                        for i, business, _, keys in prepared]}
    plan_ref = session.store.receipt(session.reload(), 'workflow.batch.plan', record)
    for index, row in enumerate(record['items']):
        row.pop('input')
        row['inputEvidence'] = {'ref': plan_ref, 'pointer': '/items/'+str(index)+'/input'}
    record['planRef'] = plan_ref
    session.state.setdefault('executionBatches', []).append(record)
    session.save()
    started = time.monotonic()
    queue = asyncio.Queue()
    for index in range(len(prepared)): queue.put_nowait(index)
    blocked = set()

    def persist():
        session.reload()
        entries = session.state.setdefault('executionBatches', [])
        entries[:] = [record if r['batchId'] == batch_id else r for r in entries]
        session.save()

    async def worker():
        platform = WorkflowPlatform(session.runtime, session.logger, session.store, label=session.label)
        platform.schema_bootstrap = dict(session.platform.schema_bootstrap)
        platform.on_receipt = session._receipt_added
        session._batch_platforms.add(platform)
        try:
            # Each worker has its own connection and one in-flight execution.
            while not queue.empty():
                index = queue.get_nowait()
                item, business, files, resources = prepared[index]
                row = record['items'][index]
                if session._stop_flag():
                    row['status'] = 'not_dispatched'; persist(); continue
                try:
                    keys = ['declared:'+k for k in resources]
                    # Hold the batch page claim through result classification;
                    # an uncertain predecessor cannot release work onto that page.
                    keys.append('batch-page:'+item['fleetId']+':'+item['pageId'])
                    async with lease(session.runtime, keys, stop_check=session._stop_flag):
                        if blocked.intersection(keys):
                            row.update(status='not_dispatched', error='Shared resource has unconfirmed prior effects')
                            persist(); continue
                        session._check_stop()
                        row['status'] = 'running'; persist()
                        def started(workflow_id):
                            row['workflowId'] = workflow_id
                            row['platformAgentId'] = platform.agent_id
                            session._execution_platforms[workflow_id] = platform
                            persist()
                        response = await session._execute_workflow(
                            {'reason': args['reason']+'; item '+item['id'], 'experiment': item.get('experiment')},
                            _prepared=(digest, root, document, business, files),
                            _binding={'pageId': item['pageId'], 'fleetId': item['fleetId']}, _platform=platform,
                            _on_started=started)
                        view = json.loads(response.content[0].text)
                        row.update({k: view.get(k) for k in ('status', 'workflowId', 'receiptRef', 'facts',
                                                            'platformError', 'stopped')})
                        if row['status'] not in {'succeeded', 'failed', 'cancelled', 'completed', 'not_dispatched'}:
                            blocked.update(keys)
                except asyncio.CancelledError:
                    row.update(status='not_dispatched' if row['status']=='pending' else 'unknown',
                               error='Interrupted; reconcile owned execution before replay')
                    persist(); raise
                except Exception as exc:
                    blocked.update(keys)
                    row.update(status='not_dispatched' if session._stop_flag() and row['status']=='pending' else 'unknown',
                               error=str(exc), errorType=type(exc).__name__)
                persist()
        finally:
            try:
                await platform.close()
            finally:
                session._batch_platforms.discard(platform)

    tasks = [asyncio.create_task(worker()) for _ in range(min(count, len(items)))]
    completed = False
    try:
        await asyncio.gather(*tasks)
        completed = True
    finally:
        for task in tasks:
            if not task.done(): task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        for row in record['items']:
            if row['status'] == 'pending': row['status'] = 'not_dispatched'
            elif row['status'] == 'running': row['status'] = 'unknown'
        record.update(status='returned' if completed else 'interrupted',
                      durationMs=round((time.monotonic()-started)*1000))
        persist()
        ref = session.store.receipt(session.reload(), 'workflow.batch.result', record)
        session.save()
        # Unconfirmed pages remain claimed until their owning task ends.
        uncertain = {row['pageId'] for row in record['items']
                     if row['status'] not in {'succeeded','failed','cancelled','completed','not_dispatched'}}
        session.runs.release_pages(session.logger.task_id,
            sorted({row['pageId'] for row in record['items']} - uncertain))
    rows = [{k:v for k,v in row.items() if k!='input'} for row in record['items']]
    return result({'batchId': batch_id, 'contentHash': digest, 'receiptRef': ref,
                   'items': compact(rows, 14000), 'itemCount': len(rows), 'durationMs': record['durationMs'],
                   'next': 'Judge each item against the original goal. Returned is not business completion. '
                           'Inspect unknown effects before retrying; reuse exact input from this receipt.'})
