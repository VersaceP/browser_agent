"""Guidance must agree across capability modes and avoid conflicting actions."""
from types import SimpleNamespace
import pytest
from agent_harness import BrowserAgent
from harness.prompts import read_harness_guide, search_harness_guides


def make_agent(workflow, multimodal):
    agent = object.__new__(BrowserAgent)
    agent.runtime = SimpleNamespace(harness=SimpleNamespace(
        workflow_execution_enabled=workflow,
        browser_agent_multimodal_enabled=multimodal,
    ))
    agent.worker_contract = {'task_type': 'general'}
    agent.capability_methods = {'Page.getState', 'DOM.getAXTree', 'Workflow.execute'}
    agent.capabilities = [{'method': name} for name in agent.capability_methods]
    agent.method_schemas = {}
    agent.methods_requiring_purpose = set()
    agent.purpose_hints = {}
    agent.agent_guide = ''
    agent.static_context_block = ''
    return agent


@pytest.mark.parametrize('workflow', [False, True])
@pytest.mark.parametrize('multimodal', [False, True])
def test_guidance_retains_boundaries_without_conflicting_defaults(workflow, multimodal):
    prompt = make_agent(workflow, multimodal)._build_system_prompt()
    assert 'replayForbidden=true' in prompt
    assert 'Empty values follow the approved worker_contract' in prompt
    assert 'inspect its validation receipt' in prompt
    assert 'same pending pause' in prompt
    for obsolete in ('Call record_extraction after validation', 'Reject empty, guessed',
                     'Only after a re-entry still shows the gap',
                     'local_fs_* inspects offloaded evidence'):
        assert obsolete not in prompt
    assert ('Do not call Workflow.execute' in prompt) is (not workflow)
    # Resident segment rule. Current WebCross releases expose leased observation
    # text through $cache.observation/$last and transform when workflows are enabled.
    assert ('artifact text' in prompt) is workflow
    assert ('counts as live inspection' in prompt) is workflow
    assert ('Outside a workflow, use find_in_axtree' in prompt) is workflow
    assert ('target id need not be known in advance' in prompt) is workflow
    assert 'cannot read page content' not in prompt
    assert 'A segment cannot read page content' not in prompt
    assert ('mode=visual_locate' in prompt) is (not multimodal)
    assert 'browser.observation-evidence' in prompt


def test_observation_and_workflow_details_are_reachable():
    matches = search_harness_guides(query='AXTree flags', audience='browser')['matches']
    assert any(m['guideId'] == 'browser.observation-evidence' for m in matches)
    for guide_id in ('browser.observation-evidence', 'browser.workflow-segments'):
        result = read_harness_guide(guide_id=guide_id, audience='browser', line_limit=500)
        assert result['status'] == 'done'
        assert not result['truncated']
    body = read_harness_guide(guide_id='browser.workflow-segments', audience='browser', line_limit=500)['content']
    assert 'Dispatch is not outcome proof' in body
    assert 'definitionRef' in body and 'definitionHash' in body
