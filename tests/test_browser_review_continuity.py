"""Cross-run evidence and incremental finding contracts; no live LLM/site needed."""
import asyncio
import json
from types import SimpleNamespace

import pytest

from harness.agents.browser.review.evidence import receipt_index
from harness.agents.browser.review.issues import active_issues, apply_issue_delta, issue_state, stage_findings
from harness.agents.browser.review.task import (
    _review_continuity, persist_review_state, read_review_session,
    review_browser_task, todo_snapshot, write_todo,
)
from harness.storage.factory import create_storage
from tests.test_browser_task_review import make_agent


@pytest.fixture(params=['file', 'db', 'dual'])
def stored_agent(tmp_path, request):
    agent = make_agent(tmp_path)
    agent.worker_id = 'browser-001'
    agent.logger.run_id = 'before'
    store = create_storage(backend=request.param, worktree_dir=str(tmp_path), sqlite_path='harness.db')
    store.create_task(task_id=agent.logger.task_id, harness_version='test')
    for run in ('before', 'after'):
        store.start_run(task_id=agent.logger.task_id, harness_version='test', run_id=run)
    agent.logger.attach_storage(store)
    yield agent, store, request.param
    store.close()


def test_task_history_is_searchable_after_resume_and_indices_do_not_collide(stored_agent):
    agent, store, backend = stored_agent
    agent.trace = [{'type': 'browser_call', 'result': {
        'method': 'Workflow.execute', 'response': {'data': {'stepResults': [
            {'id': 'decline_reuse', 'success': True, 'label': '不使用'}]}}}}]
    old = receipt_index(agent, agent.trace)
    old_ref = old.live_ids[0]
    store.append_worker_trace(task_id=agent.logger.task_id, run_id='before',
                              worker_id=agent.worker_id, entries=agent.trace)
    store.append_event(task_id=agent.logger.task_id, run_id='before', worker_id=agent.worker_id,
                       event_type='browser.call.result', payload=agent.trace[0]['result'])
    # Same worker and local index, entirely different receipt in resumed run.
    agent.logger.run_id = 'after'
    agent.trace = [{'type': 'browser_call', 'result': {'method': 'Page.getState', 'response': {'status': 'ready'}}}]
    current = receipt_index(agent, agent.trace)
    current.load_history()
    assert current.live_ids[0] != old_ref
    found = current.search('decline_reuse')
    assert found['matches']
    assert found['coverage']['archiveLoaded'] and not found['coverage']['problems']
    for match in found['matches']:
        receipt = current.read(match['evidenceId'])
        assert 'decline_reuse' in receipt['content']
        assert receipt['historicalObservation'] is True
        assert match['source']['runId'] != 'after'
    if backend == 'db':
        assert current.read(old_ref)['status'] == 'done'
    else:
        legacy = [m for m in found['matches'] if m['source']['kind'] == 'legacy_trace']
        assert legacy and legacy[0]['source']['runId'] == ''
    # No task-global lookup by a caller-supplied foreign path or task ID.
    other = make_agent(agent.logger.task_dir.parent / 'other')
    other.logger.task_id = 'foreign-task'
    assert receipt_index(other, other.trace).read(old_ref)['status'] == 'unavailable'


def test_archived_trace_exhaustion_and_search_cursor(stored_agent):
    agent, store, _ = stored_agent
    entries = [{'type': 'local_fs_read', 'result': {'content': f'needle {i}'}} for i in range(1005)]
    store.append_worker_trace(task_id=agent.logger.task_id, run_id='before',
                              worker_id=agent.worker_id, entries=entries)
    index = receipt_index(agent, [])
    index.load_history()
    cursor, found = '', set()
    while True:
        result = index.search('needle', cursor)
        refs = {m['evidenceId'] for m in result['matches']}
        assert not refs & found
        found |= refs
        if not result['nextCursor']:
            break
        cursor = result['nextCursor']
    assert len(found) == 1005
    assert index.search('different term', cursor)['reason'] == 'search_cursor_mismatch'
    assert 'needle 1004' in index.read(index.search('needle 1004')['matches'][0]['evidenceId'])['content']


def test_offloaded_browser_response_and_incremental_event_scan(stored_agent):
    agent, store, backend = stored_agent
    payload = {'method': 'DOM.getAXTree', 'response': {'data': {'text': 'large-response-marker' + 'x' * 300000}}}
    store.append_event(task_id=agent.logger.task_id, run_id='before', worker_id=agent.worker_id,
                       event_type='browser.call.result', payload=payload)
    index = receipt_index(agent, [])
    index.load_history()
    assert index.search('large-response-marker')['matches']
    assert not index.problems
    cursor = index.event_cursor
    read_events = store.read_events
    seen = []
    def read(**kwargs):
        seen.append(kwargs['after_event_id'])
        return read_events(**kwargs)
    store.read_events = read
    store.append_event(task_id=agent.logger.task_id, run_id='after', worker_id=agent.worker_id,
                       event_type='browser.call.result', payload={'method': 'Page.getState', 'response': {'tag': 'new-event'}})
    index.load_history()
    assert seen[0] == cursor and index.event_cursor > cursor
    assert index.search('new-event')['matches']
    # A model claim logged as a non-response does not become a tool receipt.
    store.append_event(task_id=agent.logger.task_id, run_id='after',
                       event_type='browser.task_review', payload={'result': {'text': 'unverified-claim'}})
    index.load_history()
    assert not index.search('unverified-claim')['matches']


def test_changed_run_marks_all_current_receipts_new(stored_agent):
    agent, _, _ = stored_agent
    snapshot = todo_snapshot(agent)
    persist_review_state(agent, {'traceCount': 1, 'traceCursor': {
        'runId': 'before', 'workerId': agent.worker_id, 'count': 1}})
    assert not _review_continuity(agent, snapshot, agent.trace)['receiptIndex'][0]['newSinceReview']
    agent.logger.run_id = 'after'
    assert _review_continuity(agent, snapshot, agent.trace)['receiptIndex'][0]['newSinceReview']


def addition(client='one', text='Unverified requirement'):
    return {'clientId': client, 'text': text, 'reason': 'Observed gap', 'evidenceIds': ['e1']}


def apply(state, *, additions=None, updates=None, verdict='needs_work'):
    return apply_issue_delta(state, {'issueRevision': state['revision'], 'verdict': verdict,
        'newIssues': additions or [], 'issueUpdates': updates or []}, {'e1'})


def test_findings_update_in_place_unchanged_findings_need_no_rewrite():
    state, errors = apply(issue_state({}), additions=[addition()])
    assert not errors
    issue_id = next(iter(state['items']))
    for turn in range(12):
        state, errors = apply(state, updates=[{
            'issueId': issue_id, 'status': 'open', 'text': f'New wording {turn}',
            'reason': 'Same unresolved concern', 'evidenceIds': ['e1']}])
        assert not errors and list(state['items']) == [issue_id]
    unchanged, errors = apply(state, verdict='progress_ok')
    assert not errors and unchanged == state
    _, errors = apply(state, verdict='complete')
    assert 'complete contradicts unresolved issue state' in errors


def test_semantic_merge_is_explicit_and_preserves_identity_history():
    state, _ = apply(issue_state({}), additions=[addition(), addition('two', 'Paraphrased concern')])
    first, second = state['items']
    merged, errors = apply(state, updates=[{'issueId': second, 'status': 'merged',
        'mergeInto': first, 'reason': 'These describe the same observed gap', 'evidenceIds': ['e1']}])
    assert not errors
    assert len(merged['items']) == 2 and len(active_issues(merged)) == 1
    assert merged['items'][second]['mergeInto'] == first
    _, errors = apply(merged, updates=[{'issueId': first, 'status': 'merged',
        'mergeInto': second, 'reason': 'Circular merge', 'evidenceIds': ['e1']}])
    assert any('cycle' in error for error in errors)
    done, errors = apply(merged, verdict='complete', updates=[{'issueId': first, 'status': 'resolved',
        'reason': 'Actual receipt establishes the result', 'evidenceIds': ['e1']}])
    assert not errors and not active_issues(done)
    reopened, errors = apply(done, updates=[{'issueId': first, 'status': 'open',
        'reason': 'New evidence contradicts completion', 'evidenceIds': ['e1']}])
    assert not errors and active_issues(reopened)[0]['issueId'] == first


def test_stale_revision_and_invalid_reference_leave_previous_state_untouched():
    state, _ = apply(issue_state({}), additions=[addition()])
    original = json.dumps(state)
    key = next(iter(state['items']))
    result, errors = apply_issue_delta(state, {'issueRevision': 0, 'verdict': 'complete',
        'issueUpdates': [{'issueId': key, 'status': 'resolved', 'reason': 'Claim', 'evidenceIds': ['unknown']}]}, {'e1'})
    assert errors and result == state and json.dumps(state) == original
    staged = stage_findings(state, {'newIssues': [addition('retry', 'Another concern')]}, {'e1'})
    assert stage_findings(staged, {'newIssues': [addition('retry', 'Another concern')]}, {'e1'}) == staged


def test_interrupted_protocol_repair_keeps_ids_across_resume_and_goal_amendment(stored_agent):
    agent, _, _ = stored_agent
    write_todo(agent, '- [ ] Verify result\n')
    class Interrupted:
        calls = 0
        async def generate_response(self, system_prompt, messages, tools):
            self.calls += 1
            if self.calls > 1:
                raise RuntimeError('quota exhausted during repair')
            request = json.loads(messages[-1]['content'])
            return '', [{'id': 'invalid-complete', 'name': 'submit_browser_task_review', 'input': {
                **{k: request[k] for k in ('goalHash', 'todoHash', 'proposalHash', 'issueRevision')},
                'verdict': 'complete', 'reason': 'Claimed done', 'newIssues': [{**addition(),
                    'evidenceIds': [request['userGoalEvidenceId']]}], 'evidenceIds': [], 'suggestedNextAction': ''}}], 'tool_use', {}
    agent.provider = Interrupted()
    result = asyncio.run(review_browser_task(agent=agent, original_goal='Check source', operator_inputs=[], phase='final'))
    assert result['status'] == 'unavailable'
    saved = read_review_session(agent)
    key = next(iter(saved['checkpoint']['issueState']['items']))
    class Resumed:
        async def generate_response(self, system_prompt, messages, tools):
            request = json.loads(messages[-1]['content'])
            assert [item['issueId'] for item in request['priorIssues']] == [key]
            return '', [{'id': 'resume', 'name': 'submit_browser_task_review', 'input': {
                **{k: request[k] for k in ('goalHash', 'todoHash', 'proposalHash', 'issueRevision')},
                'verdict': 'needs_work', 'reason': 'Continue verifying', 'evidenceIds': [], 'suggestedNextAction': 'Verify'}}], 'tool_use', {}
    agent.logger.run_id = 'after'
    agent.provider = Resumed()
    resumed = asyncio.run(review_browser_task(agent=agent, original_goal='Amended source check', operator_inputs=[], phase='progress'))
    assert resumed['status'] == 'reviewed'
    assert list(resumed['issueState']['items']) == [key]


def test_unreadable_event_reports_gap_and_can_recover_without_skipping():
    broken = {'task_id': 'task', 'event_id': 1, 'run_id': 'before',
              'event_type': 'browser.call.result', 'payload_json': '{bad-json'}
    class Store:
        def list_worker_trace(self, **kwargs):
            return []
        def read_events(self, **kwargs):
            return [broken] if kwargs['after_event_id'] < 1 else []
    agent = SimpleNamespace(logger=SimpleNamespace(task_id='task', run_id='after', storage=Store()))
    index = receipt_index(agent, [])
    index.load_history()
    assert index.event_cursor == 0 and index.search('receipt')['coverage']['problems']
    broken['payload_json'] = json.dumps({'method': 'Page.getState', 'response': {'value': 'recovered-receipt'}})
    index.load_history()
    assert not index.problems and index.event_cursor == 1
    assert index.search('recovered-receipt')['matches']


def test_live_receipt_is_a_frozen_observation_and_foreign_rows_are_rejected():
    foreign = {'task_id': 'foreign', 'event_id': 1, 'event_type': 'browser.call.result',
               'payload_json': json.dumps({'response': {'value': 'secret'}})}
    class Store:
        def list_worker_trace(self, **kwargs):
            return []
        def read_events(self, **kwargs):
            return [foreign]
    agent = SimpleNamespace(logger=SimpleNamespace(task_id='task', run_id='run', storage=Store()))
    trace = [{'type': 'local_fs_read', 'result': {'content': 'before'}}]
    index = receipt_index(agent, trace)
    ref = index.live_ids[0]
    trace[0]['result']['content'] = 'after'
    assert 'before' in index.read(ref)['content']
    index.load_history()
    assert index.problems and not index.search('secret')['matches']
    receipt_index(agent, trace)
    assert index.read(ref)['status'] == 'unavailable'


def test_legacy_invalid_submission_cannot_silently_drop_its_finding():
    state = issue_state({})
    raw = {'issueRevision': 0, 'verdict': 'complete', 'issues': ['Unverified result'], 'evidenceIds': ['e1']}
    _, errors = apply_issue_delta(state, raw, {'e1'})
    assert errors
    staged = stage_findings(state, raw, {'e1'})
    assert active_issues(staged)[0]['text'] == 'Unverified result'
    assert stage_findings(staged, raw, {'e1'}) == staged
    _, errors = apply(staged, verdict='complete')
    assert 'complete contradicts unresolved issue state' in errors


def test_disclosed_issue_is_preserved_in_executor_projection():
    from harness.agents.browser.review.task import _review_result, review_for_executor
    state, _ = apply(issue_state({}), additions=[addition()])
    key = next(iter(state['items']))
    output = _review_result({'goalHash': 'g', 'todoHash': 't', 'proposalHash': 'p',
        'issueRevision': state['revision'], 'verdict': 'complete', 'reason': 'Verified with limitation',
        'evidenceIds': ['e1'], 'suggestedNextAction': '', 'issueUpdates': [
            {'issueId': key, 'status': 'disclosure', 'reason': 'Limitation is within user scope', 'evidenceIds': ['e1']}]},
        phase='final', goal_hash='g', todo_hash='t', proposal_hash='p', evidence_ids={'e1'},
        fresh_evidence_ids={'e1'}, issues_state=state)
    assert output['status'] == 'reviewed'
    visible = review_for_executor(output)
    assert 'issueState' not in visible and visible['disclosures'] == ['Unverified requirement']
