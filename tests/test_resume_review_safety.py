"""Regression tests for HITL replay consent and historical blocker selection."""
import json
from unittest.mock import patch

from main import _confirm_interrupted_replay, _pending_human_interventions


def test_hitl_replay_requires_consent_in_noninteractive_mode():
    report = {'hitlReactivatedPhases': ['direct_worker']}
    with patch('main.sys.stdin.isatty', return_value=False):
        assert not _confirm_interrupted_replay(report, explicitly_allowed=False)
        assert _confirm_interrupted_replay(report, explicitly_allowed=True)


def test_hitl_replay_operator_can_refuse():
    with patch('main.sys.stdin.isatty', return_value=True), patch('builtins.input', return_value='no'):
        assert not _confirm_interrupted_replay(
            {'hitlReactivatedPhases': ['p']}, explicitly_allowed=False)


def test_latest_nonhuman_result_supersedes_old_hitl(tmp_path):
    log = tmp_path / 'run.jsonl'
    log.write_text('\n'.join(json.dumps({'type': 'spawner.browser.result', 'payload': {
        'phaseId': 'p', 'status': status, 'answer': answer,
    }}) for status, answer in [('hitl_required', 'old challenge'), ('failed', 'new reason')]))
    assert _pending_human_interventions(log, ['p']) == []


def test_challenge_without_hitl_keyword_is_historical_only(tmp_path):
    log = tmp_path / 'run.jsonl'
    log.write_text(json.dumps({'type': 'spawner.browser.result', 'payload': {
        'phaseId': 'p', 'status': 'blocked_by_challenge', 'answer': 'challenge',
    }}))
    result = _pending_human_interventions(log, ['p'])
    assert result[0]['requiresLiveVerification']
    assert result[0]['source'] == 'historical_worker_claim_unverified'
