"""Reviewer pixels and executor receipt queries without a live site or model."""
import asyncio
import base64
import hashlib
import json
from pathlib import Path

import pytest

from harness.agents.browser.review.evidence import receipt_index
from harness.agents.browser.review.task import (
    _observation_text, _query_observation, _read_tool, read_review_session,
    review_browser_task, write_todo,
)
from tests.test_browser_review_continuity import stored_agent
from tests.test_browser_task_review import make_agent


PIXELS = base64.b64decode(
    'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9WlT')


def screenshot_browser(agent, tmp_path, *, pixels=PIXELS, response_page='page-1'):
    path = tmp_path / 'platform-capture.png'
    path.write_bytes(pixels)
    agent.capability_methods.add('Page.screenshot')
    agent.method_schemas['Page.screenshot'] = {'inputSchema': {
        'type': 'object', 'additionalProperties': False,
        'required': ['pageId', 'purpose', 'options'], 'properties': {
            'pageId': {'type': 'string'}, 'purpose': {'type': 'string'},
            'options': {'type': 'object', 'additionalProperties': False,
                'required': ['format'], 'properties': {'format': {'const': 'file'}}}}}}

    async def call(method, params, **kwargs):
        agent.browser.calls.append((method, params))
        return {'data': {'pageId': response_page, 'savedPath': str(path),
                         'width': 1, 'height': 1}}

    agent.browser.call = call
    return path


def capture(agent, **params):
    return asyncio.run(_read_tool(agent, agent.trace, 'review_browser_read', {
        'method': 'Page.screenshot', 'params': {
            'pageId': 'page-1', 'purpose': 'Resolve visible state', **params}}, 'fresh'))


def screenshot_fact(result):
    return {'kind': 'page', **{key: result[key] for key in (
        'resourceUri', 'logicalPath', 'resourceVersion', 'sourceSha256', 'runId',
        'method', 'imageResourceUri', 'imageLogicalPath', 'imageResourceVersion',
        'imageSha256')}}


def test_capture_returns_pixels_and_durable_bound_evidence_together(stored_agent, tmp_path):
    agent, store, backend = stored_agent
    platform_path = screenshot_browser(agent, tmp_path)
    result, image = capture(agent, options={'format': 'base64', 'path': '/untrusted.png'})
    assert result['status'] == 'done' and result['evidenceId'].startswith('page:')
    assert len(agent.browser.calls) == 1
    assert agent.browser.calls[0][1]['options'] == {'format': 'file'}
    assert base64.b64decode(image['source']['data']) == PIXELS
    assert image['source']['media_type'] == 'image/png'
    assert image['source']['data'] not in json.dumps(result)
    fact = screenshot_fact(result)
    platform_path.unlink()
    assert _observation_text(agent, fact) is not None
    # Only the exact task-owned pixels delivered to the reviewer may survive.
    if backend in {'file', 'dual'}:
        Path(result['imageResourceUri']).write_bytes(b'replaced pixels')
    else:
        db = store.secondary if backend == 'dual' else store
        db.connection.execute('UPDATE task_resources SET content_blob = ? '
            'WHERE task_id = ? AND logical_path = ?',
            (b'replaced pixels', agent.logger.task_id, result['imageLogicalPath']))
        db.connection.commit()
    assert _observation_text(agent, fact) is None


@pytest.mark.parametrize('restriction,reason', [
    ('multimodal', 'review_image_model_disabled'),
    ('page', 'review_page_not_bound'),
    ('contract', 'method_forbidden_by_contract'),
    ('capability', 'method_not_in_live_capabilities'),
    ('schema', 'method_schema_invalid'),
])
def test_screenshot_permission_failures_do_not_call_browser(tmp_path, restriction, reason):
    agent = make_agent(tmp_path)
    screenshot_browser(agent, tmp_path)
    params = {}
    if restriction == 'multimodal':
        agent.runtime.harness.browser_agent_multimodal_enabled = False
    elif restriction == 'page':
        params['pageId'] = 'foreign-page'
    elif restriction == 'contract':
        agent.worker_contract = {'forbidden_methods': ['Page.*']}
    elif restriction == 'capability':
        agent.capability_methods.remove('Page.screenshot')
    else:
        params['unexpected'] = True
    result, image = capture(agent, **params)
    assert result['reason'] == reason and image is None
    assert not agent.browser.calls


@pytest.mark.parametrize('failure,reason', [
    ('page', 'screenshot_page_mismatch'),
    ('size', 'image_too_large'),
    ('invalid', 'invalid_image_file'),
])
def test_unusable_capture_has_no_image_or_evidence(tmp_path, failure, reason):
    agent = make_agent(tmp_path)
    screenshot_browser(agent, tmp_path, pixels=b'not an image' if failure == 'invalid' else PIXELS,
                       response_page='foreign-page' if failure == 'page' else 'page-1')
    if failure == 'size':
        agent.runtime.harness.browser_agent_max_multimodal_image_bytes = 1
    result, image = capture(agent)
    assert result['reason'] == reason and image is None
    assert 'evidenceId' not in result


def test_executor_screenshot_receipt_no_longer_grants_host_image_access(tmp_path):
    agent = make_agent(tmp_path)
    path = tmp_path / 'executor.png'
    path.write_bytes(PIXELS)
    agent.trace = [{'type': 'browser_call', 'result': {'method': 'Page.screenshot',
        'response': {'data': {'pageId': 'page-1', 'savedPath': str(path)}}}}]
    result, image = asyncio.run(_read_tool(agent, agent.trace, 'review_view_image',
                                        {'path': str(path)}, 'review'))
    assert result['status'] == 'unavailable' and image is None


def test_fresh_capture_reaches_reviewer_and_cites_exact_pixels(stored_agent, tmp_path):
    agent, _, _ = stored_agent
    screenshot_browser(agent, tmp_path)
    write_todo(agent, '- [x] Verify visible page\n')

    class Reviewer:
        calls = 0

        async def generate_response(self, system_prompt, messages, tools):
            self.calls += 1
            if self.calls == 1:
                self.request = json.loads(messages[-1]['content'])
                return '', [{'id': 'capture', 'name': 'review_browser_read', 'input': {
                    'method': 'Page.screenshot', 'params': {
                        'pageId': 'page-1', 'purpose': 'Resolve visible state'}}}], 'tool_use', {}
            content = messages[-1]['content'][0]['content']
            assert [block['type'] for block in content] == ['text', 'image']
            assert base64.b64decode(content[1]['source']['data']) == PIXELS
            receipt = json.loads(content[0]['text'])
            self.evidence = receipt['evidenceId']
            return '', [{'id': 'submit', 'name': 'submit_browser_task_review', 'input': {
                'issueRevision': self.request['issueRevision'], 'verdict': 'complete',
                'reason': 'Visible result checked.', 'evidenceIds': [self.evidence],
                'suggestedNextAction': ''}}], 'tool_use', {}

    agent.provider = Reviewer()
    result = asyncio.run(review_browser_task(agent=agent, original_goal='Verify visible page',
        operator_inputs=[], phase='final'))
    assert result['verdict'] == 'complete' and result['modelCalls'] == 2
    assert result['toolCalls'] == 1 and len(agent.browser.calls) == 1
    session = read_review_session(agent)
    assert session['evidence'][agent.provider.evidence]['imageSha256'] == hashlib.sha256(PIXELS).hexdigest()
    assert base64.b64encode(PIXELS).decode() not in json.dumps(session)


def receipt(agent, result):
    agent.trace = [{'type': 'browser_call', 'result': result}]
    index = receipt_index(agent, agent.trace)
    return index, index.live_ids[0]


def test_executor_query_batches_values_decodes_ax_lines_and_preserves_provenance(stored_agent):
    agent, _, _ = stored_agent
    index, ref = receipt(agent, {'method': 'DOM.getAXTree', 'response': {
        'observation': 'Read /external/platform-host-file',
        'data': {'mode': 'full', 'pageId': 'page-1', 'version': 7,
            'documentEpoch': 3, 'capturedAt': 'historical-time',
            'lines': ['input "School" value="广东工业大学"', 'button "保存"'],
            'selected': {'degree': '本科', 'department': '经管学院'}}}})
    result = _query_observation(agent, {'evidence_id': ref,
        'patterns': ['^input "School".*广东工业大学', 'button "保存"'],
        'keys': ['degree', 'department'], 'scope': '/result/response/data'}, {}, index)
    assert result['status'] == 'done' and not result['coverageTruncated']
    assert all(item['matches'] for item in result['results'])
    assert result['results'][2]['matches'][0]['path'] == '/result/response/data/selected/degree'
    assert result['searchSource'] == '/result/response/data/lines'
    assert result['source'] == index.fact(ref)['source']
    assert result['receiptSha256'] == index.fact(ref)['receiptSha256']
    assert result['observation'] == {'pageId': 'page-1', 'version': 7,
        'documentEpoch': 3, 'capturedAt': 'historical-time'}
    assert result['historicalObservation'] is True and not agent.browser.calls
    assert '/external/platform-host-file' not in index.query_source(ref)['content']
    assert '/external/platform-host-file' in index.records[ref]['content']


def complete_receipt(agent):
    logical = 'tool_results/complete.json'
    complete = {'method': 'Workflow.execute', 'response': {'data': {
        'returnValue': {'status': 'saved', 'request': {'verb': 'submit'}},
        'callRecords': [{'response': {'body': json.dumps({'code': 0, 'message': 'saved'})}}],
        'large': 'x' * 26000}}}
    text = json.dumps(complete, ensure_ascii=False, indent=2)
    agent.logger.storage.save_resource(task_id=agent.logger.task_id, run_id=agent.logger.run_id,
        resource_type='tool_result_complete', logical_path=logical, media_type='application/json', content=text)
    path = Path(agent.logger.task_dir) / logical
    index, ref = receipt(agent, {'method': 'Workflow.execute', 'response': {'data': {'large': 'x...'}},
        '_truncation': {'sourceComplete': True, 'projectionTruncated': True,
            'savedPath': str(path), 'persistedPayloadSha256': hashlib.sha256(text.encode()).hexdigest()}})
    return index, ref, path, text


def test_query_resolves_hashed_complete_resource_and_revalidates(stored_agent):
    agent, _, backend = stored_agent
    index, ref, path, text = complete_receipt(agent)
    if backend == 'db':
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('stale disk copy')
    result = _query_observation(agent, {'evidence_id': ref, 'keys': ['code', 'message'],
        'scope': '/result/response/data/callRecords'}, {}, index)
    assert result['status'] == 'done' and not result['coverageTruncated']
    assert result['results'][0]['matches'][0]['value'] == '0'
    assert '/$json/code' in result['results'][0]['matches'][0]['path']
    assert result['resolvedResources'] == [{'path': str(path),
        'sourceSha256': hashlib.sha256(text.encode()).hexdigest()}]
    fact = {**index.fact(ref), 'querySourceSha256': result['sourceSha256'],
        'queryResources': result['resolvedResources']}
    assert index.validates(ref, fact)
    agent.logger.storage.save_resource(task_id=agent.logger.task_id, run_id=agent.logger.run_id,
        resource_type='tool_result_complete', logical_path='tool_results/complete.json',
        media_type='application/json', content='{"changed":true}')
    assert not index.validates(ref, fact)
    changed = _query_observation(agent, {'evidence_id': ref, 'keys': ['code']}, {}, index)
    assert changed['status'] == 'partial' and changed['coverageTruncated']
    assert changed['resolvedResources'] == []


@pytest.mark.parametrize('projection', [
    {'_truncation': {'sourceComplete': False, 'projectionTruncated': True}},
    {'response': {'data': {'callRecordsReference': '/unverified/calls.json'}}},
    {'response': {'data': {'returnValue': {'_projected': True, 'valueReference': '/unverified/value.json'}}}},
])
def test_incomplete_receipts_never_claim_complete_query_coverage(tmp_path, projection):
    agent = make_agent(tmp_path)
    index, ref = receipt(agent, {'method': 'Workflow.execute', **projection})
    result = _query_observation(agent, {'evidence_id': ref, 'keys': ['code']}, {}, index)
    assert result['status'] == 'partial' and result['coverageTruncated']
    assert result['results'][0]['matches'] == []


def test_query_rejects_foreign_receipts_and_changed_source(tmp_path):
    agent = make_agent(tmp_path)
    index, ref = receipt(agent, {'method': 'Page.getState', 'response': {'data': {'state': 'saved'}}})
    other = make_agent(tmp_path / 'other')
    other.logger.task_id = 'foreign-task'
    assert _query_observation(other, {'evidence_id': ref, 'keys': ['state']}, {})['status'] == 'unavailable'
    index.records[ref]['content'] += ' '
    assert _query_observation(agent, {'evidence_id': ref, 'keys': ['state']}, {}, index)['status'] == 'unavailable'


@pytest.mark.parametrize('scope,expected', [
    ('/result/response/data/a~1b/~0items/0', 'done'),
    ('/result/response/data/a~1b/~0items/01', 'unavailable'),
    ('/result/response/data/a~2b', 'unavailable'),
    ('result/response', 'rejected'),
])
def test_query_scope_uses_json_pointer_without_guessing_paths(tmp_path, scope, expected):
    agent = make_agent(tmp_path)
    index, ref = receipt(agent, {'method': 'Page.getState', 'response': {
        'data': {'a/b': {'~items': [{'state': 'saved'}]}}}})
    result = _query_observation(agent, {'evidence_id': ref, 'keys': ['state'], 'scope': scope}, {}, index)
    assert result['status'] == expected
    if expected == 'done':
        assert result['results'][0]['matches'] == [{
            'path': scope + '/state', 'value': '"saved"', 'valueTruncated': False}]


def test_complete_reference_cannot_authorize_an_external_resource(tmp_path):
    agent = make_agent(tmp_path)
    path = tmp_path / 'external.json'
    text = '{"secret":"outside task"}'
    path.write_text(text)
    index, ref = receipt(agent, {'method': 'Page.getState', '_truncation': {
        'sourceComplete': True, 'projectionTruncated': True, 'savedPath': str(path),
        'persistedPayloadSha256': hashlib.sha256(text.encode()).hexdigest()}})
    result = _query_observation(agent, {'evidence_id': ref, 'keys': ['secret']}, {}, index)
    assert result['status'] == 'partial' and result['resolvedResources'] == []
    assert result['results'][0]['matches'] == []


def test_changed_complete_resource_cannot_support_submission(stored_agent):
    agent, _, _ = stored_agent
    _, ref, _, _ = complete_receipt(agent)
    write_todo(agent, '- [x] Verify saved receipt\n')

    class Reviewer:
        calls = 0

        async def generate_response(self, system_prompt, messages, tools):
            self.calls += 1
            if self.calls == 1:
                self.request = json.loads(messages[-1]['content'])
                return '', [{'id': 'query', 'name': 'review_query_observation', 'input': {
                    'evidence_id': ref, 'keys': ['code']}}], 'tool_use', {}
            if self.calls == 2:
                assert json.loads(messages[-1]['content'][0]['content'])['status'] == 'done'
                agent.logger.storage.save_resource(task_id=agent.logger.task_id, run_id=agent.logger.run_id,
                    resource_type='tool_result_complete', logical_path='tool_results/complete.json',
                    media_type='application/json', content='{"changed":true}')
            return '', [{'id': f'submit-{self.calls}', 'name': 'submit_browser_task_review', 'input': {
                'issueRevision': self.request['issueRevision'], 'verdict': 'complete',
                'reason': 'Old source claimed unchanged.', 'evidenceIds': [ref],
                'suggestedNextAction': ''}}], 'tool_use', {}

    agent.provider = Reviewer()
    result = asyncio.run(review_browser_task(agent=agent, original_goal='Verify saved receipt',
        operator_inputs=[], phase='final'))
    assert result['status'] == 'unavailable' and result['reason'] == 'review_binding_invalid'
    assert ref not in read_review_session(agent)['evidence']


def test_receipt_query_can_be_cited_without_paging_or_live_browser(stored_agent):
    agent, _, _ = stored_agent
    index, ref, _, _ = complete_receipt(agent)
    write_todo(agent, '- [x] Verify saved receipt\n')

    class Reviewer:
        calls = 0

        async def generate_response(self, system_prompt, messages, tools):
            self.calls += 1
            if self.calls == 1:
                self.request = json.loads(messages[-1]['content'])
                assert ref in [item['evidenceId'] for item in self.request['recentReceipts']]
                return '', [{'id': 'query', 'name': 'review_query_observation', 'input': {
                    'evidence_id': ref, 'keys': ['code', 'message'],
                    'scope': '/result/response/data/callRecords'}}], 'tool_use', {}
            result = json.loads(messages[-1]['content'][0]['content'])
            assert result['status'] == 'done' and result['results'][0]['matches'][0]['value'] == '0'
            return '', [{'id': 'submit', 'name': 'submit_browser_task_review', 'input': {
                'issueRevision': self.request['issueRevision'], 'verdict': 'complete',
                'reason': 'Actual response checked.', 'evidenceIds': [ref],
                'suggestedNextAction': ''}}], 'tool_use', {}

    agent.provider = Reviewer()
    result = asyncio.run(review_browser_task(agent=agent, original_goal='Verify saved receipt',
        operator_inputs=[], phase='final'))
    assert result['verdict'] == 'complete' and result['modelCalls'] == 2
    assert result['toolCalls'] == 1 and not agent.browser.calls
    fact = read_review_session(agent)['evidence'][ref]
    assert fact['queryResources'] and index.validates(ref, fact)
