"""Behavioral coverage for external material access and task-scoped consent."""
import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from harness.utils import RunLogger
from harness.tools import path_authorization as policy
from harness.tools.file_tools import local_fs_batch
from harness.tools.local_fs import local_fs_read, local_fs_search, local_fs_list


@pytest.fixture
def agent(tmp_path):
    return SimpleNamespace(logger=RunLogger(str(tmp_path / 'tasks'), task_id='task'),
                           runtime=SimpleNamespace(harness=SimpleNamespace(protected_local_roots=[])),
                           artifacts=[], worker_contract={}, worker_id='one')


def approve(agent, path, mode='read'):
    with patch('harness.runtime.hitl_input.read_terminal_input', new=AsyncMock(return_value='yes')):
        assert asyncio.run(policy.authorize_path(agent, str(path), mode)) is None


def test_external_directory_authorization_covers_multiple_nested_subdirectories(agent, tmp_path):
    materials = tmp_path / 'materials'
    (materials / 'one' / 'deep').mkdir(parents=True)
    (materials / 'two').mkdir()
    approve(agent, materials)

    # A directory grant is recursive for the same permission mode. Each child
    # must be checked independently, but none may trigger another prompt.
    with patch(
        'harness.runtime.hitl_input.read_terminal_input',
        new=AsyncMock(side_effect=AssertionError('child directory prompted again')),
    ):
        for child in (materials / 'one', materials / 'one' / 'deep', materials / 'two'):
            assert asyncio.run(policy.authorize_path(agent, str(child), 'read')) is None
            assert policy.allowed(agent, child.resolve(), 'read')


def test_external_directory_discovery_read_search_copy_and_resume(agent, tmp_path):
    materials = tmp_path / 'materials'
    materials.mkdir()
    (materials / '商品信息.txt').write_text('needle 汉服')
    assert local_fs_read(agent.logger, agent=agent, path=str(materials / '商品信息.txt'))['status'] == 'failed'
    approve(agent, materials)
    assert len(local_fs_list(agent, str(materials))['entries']) == 1
    assert local_fs_read(agent.logger, agent=agent, path=str(materials / '商品信息.txt'))['content'] == 'needle 汉服'
    assert local_fs_search(agent.logger, agent=agent, path=str(materials), glob_pattern='*.txt', pattern='needle')['count'] == 1
    sibling = SimpleNamespace(**vars(agent))
    sibling.worker_id = 'two'
    with patch('harness.runtime.hitl_input.read_terminal_input', new=AsyncMock(side_effect=AssertionError('duplicate prompt'))):
        assert asyncio.run(policy.authorize_path(sibling, str(materials))) is None
    policy._STATES.pop(str(policy.task_root(agent)))
    assert policy.allowed(sibling, materials.resolve(), 'read')
    copied = local_fs_batch(sibling, [{'op': 'copy', 'source': str(materials / '商品信息.txt'),
                                      'path': 'scratchpad/info.txt', 'overwrite': False}])
    assert copied['status'] == 'done'
    assert not policy.allowed(agent, materials.resolve(), 'write')


def test_denial_and_noninteractive_do_not_grant(agent, tmp_path):
    path = tmp_path / 'materials'
    path.mkdir()
    with patch('harness.runtime.hitl_input.read_terminal_input', new=AsyncMock(return_value=None)):
        assert asyncio.run(policy.authorize_path(agent, str(path)))['code'] == 'external_path_confirmation_required'
    with patch('harness.runtime.hitl_input.read_terminal_input', new=AsyncMock(return_value='no')) as prompt:
        assert asyncio.run(policy.authorize_path(agent, str(path)))['code'] == 'external_path_access_denied'
        assert asyncio.run(policy.authorize_path(agent, str(path)))['code'] == 'external_path_access_denied'
        assert prompt.await_count == 1
    assert not policy.allowed(agent, path.resolve(), 'read')


def test_protected_nested_path_and_symlink_never_leak(agent, tmp_path):
    materials = tmp_path / 'materials'
    materials.mkdir()
    secret = materials / 'webcross'
    secret.mkdir()
    (secret / 'source.py').write_text('secret-code')
    (materials / 'link').symlink_to(secret, target_is_directory=True)
    (materials / 'good.txt').write_text('public')
    agent.runtime.harness.protected_local_roots = [str(secret)]
    approve(agent, materials)
    listing = local_fs_list(agent, str(materials), recursive=True)
    assert [x['name'] for x in listing['entries']] == ['good.txt']
    for path in (secret / 'source.py', materials / 'link' / 'source.py'):
        assert local_fs_read(agent.logger, agent=agent, path=str(path))['status'] == 'failed'
    search = local_fs_search(agent.logger, agent=agent, path=str(materials), glob_pattern='**/*', pattern='secret-code')
    assert search['count'] == 0
    with patch('harness.runtime.hitl_input.read_terminal_input', new=AsyncMock(side_effect=AssertionError('must not prompt'))):
        assert asyncio.run(policy.authorize_path(agent, str(secret)))['code'] == 'protected_path_denied'


def test_parent_grant_does_not_cross_siblings_and_child_grant_stays_narrow(agent, tmp_path):
    materials = tmp_path / '素材'
    product_a = materials / '商品A'
    product_b = materials / '商品B'
    product_a.mkdir(parents=True)
    product_b.mkdir()

    approve(agent, product_a)
    assert policy.allowed(agent, product_a.resolve(), 'read')
    assert not policy.allowed(agent, product_b.resolve(), 'read')
    assert not policy.allowed(agent, materials.resolve(), 'read')

    # A separate parent authorization is the deliberate way to cover siblings.
    approve(agent, materials)
    assert policy.allowed(agent, product_a.resolve(), 'read')
    assert policy.allowed(agent, product_b.resolve(), 'read')


def test_read_write_and_task_scope_remain_independent(agent, tmp_path):
    source = tmp_path / '素材'
    source.mkdir()
    approve(agent, source, 'read')
    assert policy.allowed(agent, source.resolve(), 'read')
    assert not policy.allowed(agent, source.resolve(), 'write')

    other_root = tmp_path / 'other-task'
    other_root.mkdir()
    other = SimpleNamespace(
        logger=RunLogger(str(other_root), task_id='other-task'),
        runtime=agent.runtime, artifacts=[], worker_contract={}, worker_id='other',
    )
    with patch('harness.runtime.hitl_input.read_terminal_input', new=AsyncMock(return_value='no')) as prompt:
        result = asyncio.run(policy.authorize_path(other, str(source), 'read'))
    assert result['code'] == 'external_path_access_denied'
    assert prompt.await_count == 1


def test_symlink_outside_parent_grant_requires_its_own_scope(agent, tmp_path):
    materials = tmp_path / 'materials'
    outside = tmp_path / 'outside'
    materials.mkdir()
    outside.mkdir()
    (outside / 'secret.txt').write_text('secret')
    link = materials / 'link'
    link.symlink_to(outside, target_is_directory=True)
    approve(agent, materials)

    with patch('harness.runtime.hitl_input.read_terminal_input', new=AsyncMock(return_value='no')) as prompt:
        result = asyncio.run(policy.authorize_path(agent, str(link / 'secret.txt'), 'read'))
    assert result['code'] == 'external_path_access_denied'
    assert prompt.await_count == 1


def test_authorization_prompt_states_scope_boundaries(agent, tmp_path):
    folder = tmp_path / '素材' / '商品A'
    folder.mkdir(parents=True)
    with patch('harness.runtime.hitl_input.read_terminal_input', new=AsyncMock(return_value='no')) as prompt:
        result = asyncio.run(policy.authorize_path(agent, str(folder), 'read'))
    assert result['code'] == 'external_path_access_denied'
    text = prompt.call_args.args[0]
    assert '当前任务' in text
    assert '当前权限' in text
    assert '不包含父目录或兄弟目录' in text
    assert '符号链接' in text


def test_file_consent_does_not_grant_parent(agent, tmp_path):
    source = tmp_path / 'one.txt'
    source.write_text('one')
    approve(agent, source)
    assert policy.allowed(agent, source.resolve(), 'read')
    assert not policy.allowed(agent, tmp_path.resolve(), 'read')
    assert not policy.allowed(agent, (tmp_path / 'two.txt').resolve(), 'read')


def test_external_write_requires_independent_consent(agent, tmp_path):
    output = tmp_path / 'out'
    approve(agent, output, 'write')
    result = local_fs_batch(agent, [{'op': 'write_text', 'path': str(output / 'deep' / 'result.txt'),
                                    'content': 'delivered', 'overwrite': False}])
    assert result['status'] == 'done', result
    assert (output / 'deep' / 'result.txt').read_text() == 'delivered'
    assert not policy.allowed(agent, output.resolve(), 'read')


def test_concurrent_workers_share_one_confirmation(agent, tmp_path):
    source = tmp_path / 'materials'
    source.mkdir()
    async def run():
        with patch('harness.runtime.hitl_input.read_terminal_input', new=AsyncMock(return_value='yes')) as prompt:
            result = await asyncio.gather(*(policy.authorize_path(agent, str(source)) for _ in range(4)))
            assert result == [None] * 4
            assert prompt.await_count == 1
    asyncio.run(run())


def test_browser_nested_paths_are_checked(agent, tmp_path):
    call = {'name': 'browser_call', 'input': {'method': 'Workflow.execute', 'params': {'steps': [
        {'action': 'Download.start', 'params': {'savePath': str(tmp_path / 'video.mp4')}},
        {'action': 'File.handleChooser', 'params': {'filePaths': [str(tmp_path / 'input.txt')]}}
    ]}}}
    paths = list(policy.requests(call))
    assert (str(tmp_path / 'video.mp4'), 'write', None) in paths
    assert (str(tmp_path / 'input.txt'), 'read', None) in paths
