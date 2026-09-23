"""Interactive consent holds the task until an explicit decision or cancellation."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from harness.diagnostics import WorkerDiagnostics, classify_terminal_status, status_category
from harness.tools import path_authorization as policy
from harness.tools.browser_tools.dispatch import build_browser_tool_dispatcher
from harness.utils import RunLogger


def test_wait_holds_sibling_tools_and_resumes_original_call(tmp_path):
    agent = SimpleNamespace(logger=RunLogger(str(tmp_path / 'tasks'), task_id='wait'))
    target = tmp_path / 'output'

    async def run():
        entered, answer = asyncio.Event(), asyncio.Event()

        async def unanswered(prompt):
            entered.set()
            await answer.wait()
            return 'yes'

        with patch('harness.runtime.hitl_input.read_terminal_input', new=unanswered):
            pending = asyncio.create_task(policy.authorize_path(agent, str(target)))
            await entered.wait()
            sibling = asyncio.create_task(policy.authorize_tool_call(agent, {
                'name': 'browser_call', 'input': {'method': 'Page.navigate', 'params': {}}}))
            model_turn = asyncio.create_task(policy.wait_for_local_authorization(agent))
            await asyncio.sleep(0.03)
            assert not pending.done() and not sibling.done() and not model_turn.done()
            assert not policy.allowed(agent, target, 'read')
            answer.set()
            assert await pending is None
            assert await sibling is None
            await model_turn
        assert policy.allowed(agent, target, 'read')

    asyncio.run(run())


def test_export_options_path_cannot_bypass_denied_mkdir(tmp_path):
    agent = SimpleNamespace(logger=RunLogger(str(tmp_path / 'tasks'), task_id='export'))
    target = tmp_path / 'images'
    call = {'name': 'browser_call', 'input': {'method': 'DOM.getImg',
            'params': {'options': {'path': str(target)}}}}
    with patch('harness.runtime.hitl_input.read_terminal_input', new=AsyncMock(return_value='no')) as prompt:
        assert asyncio.run(policy.authorize_path(agent, str(target), 'write'))['code'] == 'external_path_access_denied'
        assert asyncio.run(policy.authorize_tool_call(agent, call))['code'] == 'external_path_access_denied'
        assert prompt.await_count == 1
    assert not target.exists()


def test_unrecognized_input_keeps_waiting(tmp_path):
    agent = SimpleNamespace(logger=RunLogger(str(tmp_path / 'tasks'), task_id='input'))
    with patch('harness.runtime.hitl_input.read_terminal_input', new=AsyncMock(side_effect=['continue', 'yes'])) as prompt:
        assert asyncio.run(policy.authorize_path(agent, str(tmp_path / 'input'))) is None
        assert prompt.await_count == 2


def test_cancellation_does_not_become_consent(tmp_path):
    agent = SimpleNamespace(logger=RunLogger(str(tmp_path / 'tasks'), task_id='cancel'))
    target = tmp_path / 'output'

    async def run():
        entered = asyncio.Event()

        async def unanswered(prompt):
            entered.set()
            await asyncio.Future()

        with patch('harness.runtime.hitl_input.read_terminal_input', new=unanswered):
            task = asyncio.create_task(policy.authorize_path(agent, str(target)))
            await entered.wait()
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            else:
                raise AssertionError('cancellation swallowed')
        assert not policy.allowed(agent, target, 'read')
        await asyncio.wait_for(policy.wait_for_local_authorization(agent), 1)

    asyncio.run(run())


def test_dispatch_stops_attempt_and_reports_human_blocker(tmp_path):
    agent = SimpleNamespace(
        logger=RunLogger(str(tmp_path / 'tasks'), task_id='dispatch'),
        diagnostics=WorkerDiagnostics(),
    )
    call = {'id': 'consent', 'name': 'local_fs_batch', 'input': {
        'operations': [{'op': 'list', 'path': str(tmp_path / 'output'), 'overwrite': False}]}}
    with patch('harness.runtime.hitl_input.read_terminal_input', new=AsyncMock(return_value=None)), patch(
        'harness.tools.browser_tools.dispatch.execute_browser_tool', new=AsyncMock()
    ) as execute:
        result, stop = asyncio.run(build_browser_tool_dispatcher(agent)(call, 30))
    assert stop and result['status'] == 'hitl_required', result
    assert result['tool_was_executed'] is False
    execute.assert_not_awaited()
    status, reason = classify_terminal_status(
        diagnostics=agent.diagnostics, model_reported_status=result['status'],
        reached_step_cap=False,
    )
    assert status_category(status) == 'needs_human'
    assert 'local file authorization' in reason
    assert agent.diagnostics.to_log_payload()['local_path_authorization_pending']['path'] == str(tmp_path / 'output')
    assert not agent.diagnostics.hitl_wait_entered
    from harness.tools.lead_tools import _wait_result_lead_reason
    assert _wait_result_lead_reason(agent, {'completed': [{
        **result, 'workerId': 'browser-005', 'phaseId': 'detail_rank5',
    }]}) == 'worker_incomplete_without_dispatched_continuation'
