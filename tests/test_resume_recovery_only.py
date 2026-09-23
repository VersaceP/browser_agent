import argparse
import pytest
from main import _handle_resume_command, _validate_resume_mode
from harness.runtime.resume_state import ResumeStateError


def test_instruction_rejected_without_changing_request():
    args = argparse.Namespace()
    assert _handle_resume_command('/resume task new goal', args) is None
    assert not hasattr(args, 'resume')


def test_plain_resume_is_accepted():
    args = argparse.Namespace()
    assert _handle_resume_command('/resume task', args) == ''
    assert args.resume == 'task'
    assert args.resume_instruction == ''


@pytest.mark.parametrize('mode', ['browser', 'lead'])
def test_mode_bound_to_manifest(mode):
    manifest = {'startup_args': {'agent_mode': mode}}
    assert _validate_resume_mode(manifest, {}, mode) == mode
    with pytest.raises(ResumeStateError):
        _validate_resume_mode(manifest, {}, 'lead' if mode == 'browser' else 'browser')


def test_legacy_mode_falls_back_to_plan():
    assert _validate_resume_mode(None, {'execution_mode': 'direct_worker'}, 'browser') == 'browser'
    assert _validate_resume_mode(None, {}, 'lead') == 'lead'
