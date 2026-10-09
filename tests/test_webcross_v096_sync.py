"""Regression checks against verbatim 0.9.6-beta receipts from an isolated page."""
import copy
import json
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest

from harness.observation import observation_channel as channel_module
from harness.observation.observation_channel import ObservationChannel, record_hydrated_response
from harness.observation.page_observation import hydrate_page_observation_response
from harness.observation.scroll_receipt import scroll_delta_magnitude, scroll_state_read_position
from harness.tools.argument_pipeline import apply_required_schema_defaults, validate_schema
from harness.tools.capability_repairs import prepare_capability_arguments, prepare_workflow_literal_arguments
from harness.tools.browser_tools.axtree_state import _observe_axtree_state_after
from harness.tools.browser_tools.capability import _mark_axtree_parse_inconsistency
from harness.vl.capture_geometry import scroll_coverage

FIXTURES = Path(__file__).parent / "fixtures" / "webcross_v096"


@pytest.fixture
def captured(tmp_path):
    manifest = json.loads((FIXTURES / "responses.json").read_text())
    root = tmp_path / manifest["pageId"]
    root.mkdir()
    for response in manifest["responses"].values():
        reference = response.get("data", {}).get("artifact")
        if reference:
            destination = root / reference["path"]
            shutil.copyfile(FIXTURES / reference["path"], destination)
            reference["path"] = str(destination)
    return manifest


def hydrate(captured, label):
    return hydrate_page_observation_response(captured["params"][label], captured["responses"][label])


def schema(method):
    return json.loads((FIXTURES / (method + ".json")).read_text())["inputSchema"]


def test_full_capture_and_four_live_query_views_are_hydrated(captured):
    full = hydrate(captured, "full1")["data"]
    assert full["lines"] and full["frames"]
    assert "diff" not in full and "details" not in full
    assert "path" not in full["artifact"]
    for view in ("state", "text", "attributes", "dom"):
        data = hydrate(captured, "live_" + view)["data"]
        assert "observationError" not in data
        assert data["source"] == data["observation"]["source"] == "live"
        assert len(data["records"]) == data["summary"]["total"] == 1
        assert data["records"][0]["ok"] is True
        assert data["records"][0]["view"] == view
        assert "path" not in data["artifact"]


def test_saved_details_preserve_full_value_and_per_node_failure(captured):
    data = hydrate(captured, "snapshot")["data"]
    assert "observationError" not in data
    assert data["source"] == "snapshot"
    assert data["records"][0]["error"]["code"] == "detail-not-found"
    assert data["records"][1]["ok"] is True
    assert data["records"][1]["node"]["text"] == "long-value-" * 100
    assert data["version"] == captured["params"]["snapshot"]["version"]


def test_saved_details_never_revalidate_an_invalidated_current_snapshot(captured):
    agent = SimpleNamespace(axtree_ids={"current-id"}, axtree_invalidated=True,
                            axtree_epoch=5, axtree_event_serial=0)
    response = hydrate(captured, "snapshot")
    _observe_axtree_state_after(agent, "DOM.getDetails", captured["params"]["snapshot"], {"response": response})
    assert agent.axtree_ids == {"current-id"} and agent.axtree_invalidated is True
    assert agent.axtree_epoch == 5
    assert not getattr(agent, "axtree_detail_targets", {})


def test_only_live_queries_feed_watch_baselines(captured, monkeypatch):
    channel = ObservationChannel()
    monkeypatch.setattr(channel_module, "_CHANNEL", channel)
    page = captured["pageId"]
    record_hydrated_response(captured["params"]["full1"], hydrate(captured, "full1"))
    record_hydrated_response(captured["params"]["live_state"], hydrate(captured, "live_state"))
    baseline = copy.deepcopy(channel._details)
    record_hydrated_response(captured["params"]["snapshot"], hydrate(captured, "snapshot"))
    assert channel._details == baseline and page in baseline


def test_live_queries_from_another_document_retire_prior_watch_context(captured, monkeypatch):
    channel = ObservationChannel()
    monkeypatch.setattr(channel_module, "_CHANNEL", channel)
    record_hydrated_response(captured["params"]["full1"], hydrate(captured, "full1"))
    response = hydrate(captured, "live_state")
    response["data"]["observation"]["documentEpoch"] = "another-document"
    record_hydrated_response(captured["params"]["live_state"], response)
    assert channel.current_lines(captured["pageId"]) is None
    assert captured["pageId"] not in channel._details


def test_detail_success_counts_must_match_verified_records(captured):
    response = copy.deepcopy(captured["responses"]["snapshot"])
    response["data"]["summary"].update(succeeded=2, failed=0)
    data = hydrate_page_observation_response(captured["params"]["snapshot"], response)["data"]
    assert data["observationError"]["code"] == "artifact-record-count-mismatch"
    assert "records" not in data


def test_a_detail_response_cannot_be_used_as_a_full_action_response(captured):
    data = hydrate_page_observation_response(captured["params"]["live_state"],
        captured["responses"]["live_state"], method="DOM.getAXTree")["data"]
    assert data["observationError"]["code"] == "artifact-mode-mismatch"
    assert "records" not in data and "path" not in data["artifact"]


@pytest.mark.parametrize("field,value,code", [
    ("version", "wrong-version", "artifact-version-mismatch"),
    ("pageId", "wrong-page", "artifact-page-mismatch"),
    ("source", "live", "artifact-source-mismatch"),
])
def test_conflicting_detail_identity_is_unavailable_evidence(captured, field, value, code):
    response = copy.deepcopy(captured["responses"]["snapshot"])
    response["data"][field] = value
    data = hydrate_page_observation_response(captured["params"]["snapshot"], response)["data"]
    assert data["observationError"]["code"] == code
    assert "records" not in data and "path" not in data["artifact"]


def test_unreadable_details_fail_the_tool_receipt(captured):
    Path(captured["responses"]["live_state"]["data"]["artifact"]["path"]).unlink()
    result = {"response": hydrate(captured, "live_state")}
    assert _mark_axtree_parse_inconsistency("DOM.getDetails", result, None)
    assert result["status"] == "failed"


def test_live_state_query_keeps_the_full_view_and_records_only_resolved_ids(captured):
    response = hydrate(captured, "live_state")
    agent = SimpleNamespace(axtree_ids={"full-id"}, axtree_epoch=5,
                            axtree_event_serial=0, axtree_page_id=captured["pageId"])
    _observe_axtree_state_after(agent, "DOM.getDetails", captured["params"]["live_state"], {"response": response})
    expected = response["data"]["records"][0]["target"]["resolvedId"]
    assert agent.axtree_ids == {"full-id"} and agent.axtree_epoch == 5
    assert agent.axtree_detail_targets[captured["pageId"]]["ids"] == {expected}


def test_old_query_is_not_silently_dispatched_as_a_full_read():
    params = {"query": {"view": "state", "targets": [{"selector": "#field"}]}}
    fixed, repairs, issues = prepare_capability_arguments("DOM.getAXTree", params, schema("DOM.getAXTree"))
    assert fixed == params and not repairs and issues
    assert "DOM.getDetails" in issues[0].message


def test_live_and_saved_forms_cannot_be_mixed_even_in_workflows():
    params = {"version": "v_0123456789abcdef_1", "nodeIds": ["n_0123456789abcdef"],
              "query": {"view": "state", "targets": [{"id": "n_0123456789abcdef"}]}}
    schemas = {"DOM.getDetails": {"inputSchema": schema("DOM.getDetails")}}
    fixed, repairs, issues = prepare_workflow_literal_arguments(
        {"steps": [{"action": "DOM.getDetails", "params": params}]}, schemas)
    assert not repairs and issues and issues[0].keyword == "conflict"
    assert fixed["steps"][0]["params"] == params


def test_live_schema_defaults_work_for_both_query_and_edge_union_branches(captured):
    for method, params in (
        ("DOM.getDetails", {"pageId": captured["pageId"], "purpose": "Read structure",
                            "query": {"view": "dom", "targets": [{"selector": "#field"}]}}),
        ("Input.scroll", {"pageId": captured["pageId"], "purpose": "Reach boundary", "edge": "end"}),
    ):
        defaulted, changes = apply_required_schema_defaults(params, schema(method))
        assert changes and not validate_schema(defaulted, schema(method))


def test_submitted_scroll_input_is_not_reported_as_actual_movement(captured):
    data = captured["responses"]["scroll_delta"]["data"]
    assert data["input"]["completed"] is True and data["input"]["sentDelta"]["y"] == 300
    assert data["goal"]["status"] == "not-reached"
    assert scroll_delta_magnitude(data) == 0
    coverage = scroll_coverage(data)
    assert coverage["goal"] == data["goal"] and coverage["delta"] == 0


def test_only_a_stable_completed_zero_movement_read_certifies_coordinates(captured):
    data = captured["responses"]["scroll_state"]["data"]
    assert scroll_state_read_position(data) == {"x": 0.0, "y": 0.0}
    for change in ({"input": {"completed": False}},
                   {"observation": {"status": "moving"}},
                   {"observation": {"status": "unavailable"}},
                   {"totalDelta": {"x": 0, "y": 1}}):
        assert scroll_state_read_position({**data, **change}) is None


def test_modern_scroll_without_movement_evidence_does_not_prove_traversal(captured):
    from harness.spawner.spawner_classification import _page_traversal_evidence
    data = copy.deepcopy(captured["responses"]["scroll_delta"]["data"])
    for field in ("totalDelta", "observedDelta", "layers"):
        data.pop(field, None)
    data["observation"] = {"status": "unavailable"}
    data["goal"] = {"status": "unknown"}
    ledger = _page_traversal_evidence([{"method": "Input.scroll", "result": {"response": {"data": data}}}])
    assert ledger["scrolls"] == 0 and ledger["traversed"] is False
