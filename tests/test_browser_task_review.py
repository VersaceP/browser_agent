"""Standalone Browser review: source coverage and non-mutating evidence access."""

import asyncio
import base64
import hashlib
import json
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from harness.agents.browser.review.task import (
    _browser_read, _query_observation, _read_tool, _review_result, _video_metadata,
    persist_review_state, read_review_session, review_advisory, review_browser_task,
    successful_todo_write, todo_path, todo_snapshot,
    write_todo,
)
from harness.tools.file_tools import local_fs_batch
from harness.tools.browser_tools.dispatch import build_browser_agent_tool_specs
from harness.storage.factory import create_storage
from harness.storage.virtual_fs import virtual_fs_for
from harness.tools.local_fs import local_fs_read
from harness.utils import RunLogger


class FakeBrowser:
    def __init__(self):
        self.calls = []

    async def call(self, method, params, **kwargs):
        self.calls.append((method, params))
        return {"data": {"pageId": params.get("pageId"),
                         "nodes": [{"name": "color", "value": "red"}]}}


class ScriptedReviewer:
    def __init__(self):
        self.calls = 0
        self.initial = None

    async def generate_response(self, system_prompt, messages, tools):
        self.calls += 1
        if self.calls == 1:
            self.initial = json.loads(messages[0]["content"])
            return "", [{"id": "read-1", "name": "review_read_trace",
                         "input": {"evidence_id": self.initial["recentReceipts"][0]["evidenceId"]}}], "tool_use", {}
        result = json.loads(messages[-1]["content"][0]["content"])
        if result.get("evidenceId"):
            self.receipt = result["evidenceId"]
        return "", [{"id": "submit-1", "name": "submit_browser_task_review",
                     "input": {
                         "issueRevision": self.initial["issueRevision"],
                         "verdict": "complete", "reason": "Observed source and result.",
                         "issues": [], "evidenceIds": [self.receipt],
                         "suggestedNextAction": ""}}], "tool_use", {}


def make_agent(tmp_path):
    logger = RunLogger(str(tmp_path), task_id="review-task")
    runtime = SimpleNamespace(harness=SimpleNamespace(
        browser_agent_max_multimodal_image_bytes=4_194_304,
    ))
    agent = SimpleNamespace(
        logger=logger, runtime=runtime, browser=FakeBrowser(),
        capability_methods={"DOM.getAXTree", "Page.getState"},
        method_schemas={"DOM.getAXTree": {"inputSchema": {
            "type": "object", "properties": {
                "pageId": {"type": "string"}, "purpose": {"type": "string"}},
            "required": ["pageId", "purpose"]}}},
        allowed_page_ids={"page-1"}, pinned_page_id="",
        trace=[{"type": "local_fs_read", "result": {
            "status": "done", "path": "source.md", "content": "color: red"}}],
        effective_model_config=SimpleNamespace(provider="fake", model_id="fake"),
        _current_step=4,
        standalone_browser_mode=True,
    )
    return agent


def test_review_control_tools_are_visible_only_to_standalone_browser():
    regular = {item["name"] for item in build_browser_agent_tool_specs(set())}
    standalone = {item["name"] for item in build_browser_agent_tool_specs(
        set(), standalone_review_enabled=True)}
    assert {"update_task_todo", "request_goal_review"}.isdisjoint(regular)
    assert {"update_task_todo", "request_goal_review"} <= standalone


def test_reviewer_contract_uses_evidence_ids_and_harness_owned_bindings():
    from harness.agents.browser.review.task import _TOOLS
    read = next(tool for tool in _TOOLS if tool["name"] == "review_read_trace")
    submit = next(tool for tool in _TOOLS if tool["name"] == "submit_browser_task_review")
    assert read["input_schema"]["required"] == ["evidence_id"]
    assert "index" not in read["input_schema"]["properties"]
    assert not {"goalHash", "todoHash", "proposalHash"} & set(
        submit["input_schema"]["properties"])


def test_todo_write_event_uses_successful_fixed_resource_only(tmp_path):
    agent = make_agent(tmp_path)
    path = todo_path(agent)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("- [ ] Verify colors\n", encoding="utf-8")
    internal = write_todo(agent, "- [x] Verify colors\n")
    assert successful_todo_write(agent, {"name": "update_task_todo"}, internal)
    assert str(todo_path(agent)) not in getattr(agent, "artifacts", [])
    assert Path(internal["versionPath"]).read_text() == "- [x] Verify colors\n"
    assert not successful_todo_write(agent, {"name": "local_fs_batch"},
        {"results": [{"op": "write_text", "status": "done", "path": str(path)}]})
    bypass = local_fs_batch(agent, [{"op": "write_text", "path": str(path),
                                     "content": "- [x] Fake\n", "overwrite": True}])
    assert bypass["results"][0]["status"] == "failed"
    session_bypass = local_fs_batch(agent, [{"op": "write_text",
        "path": str(path.parent / "review-session.json"),
        "content": "{}", "overwrite": True}])
    assert session_bypass["results"][0]["status"] == "failed"
    assert path.read_text() == "- [x] Verify colors\n"
    assert todo_snapshot(agent)["hash"] == hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.mark.parametrize("backend", ["db", "dual"])
def test_todo_is_a_task_resource_outside_model_context(tmp_path, backend):
    agent = make_agent(tmp_path)
    agent.logger.run_id = "run-1"
    store = create_storage(backend=backend, worktree_dir=str(tmp_path),
                           sqlite_path="harness.db")
    try:
        store.create_task(task_id=agent.logger.task_id, harness_version="test")
        store.start_run(task_id=agent.logger.task_id,
                        harness_version="test", run_id="run-1")
        agent.logger.attach_storage(store)
        saved = write_todo(agent, "- [ ] Verify colors\r\n- [x] Check source\r\n")
        assert saved["status"] == "done"
        assert saved["storagePath"]
        assert todo_snapshot(agent)["content"] == (
            "- [ ] Verify colors\n- [x] Check source\n")
        assert local_fs_read(agent.logger, agent=agent,
            path=str(todo_path(agent)))["content"].splitlines() == (
                todo_snapshot(agent)["content"].splitlines())
        view = virtual_fs_for(agent.logger)
        assert "".join(view.iter_lines("scratchpad/todo.md")) == todo_snapshot(agent)["content"]
        assert "".join(view.iter_lines("scratchpad/todo_versions/" + saved["sha256"] + ".md")) == todo_snapshot(agent)["content"]
        db = store.secondary if backend == "dual" else store
        assert db.connection.execute(
            "SELECT COUNT(*) FROM task_resources WHERE task_id = ? AND logical_path = ?",
            (agent.logger.task_id, "scratchpad/todo.md"),
        ).fetchone()[0] == 1
        assert todo_path(agent).is_file() is (backend == "dual")
        if backend == "db":
            todo_path(agent).parent.mkdir(parents=True, exist_ok=True)
            todo_path(agent).write_text("stale disk copy", encoding="utf-8")
            assert todo_snapshot(agent)["content"] != "stale disk copy"
            listing, _ = asyncio.run(_read_tool(agent, agent.trace,
                "review_list_files", {"path": str(todo_path(agent).parent)}, "r1"))
            assert listing["status"] == "done"
            assert next(entry for entry in listing["entries"]
                        if entry["name"] == "todo.md")["storage"] == "database"
        resumed = make_agent(tmp_path)
        resumed.logger.run_id = "run-1"
        resumed.logger.attach_storage(store)
        assert todo_snapshot(resumed)["hash"] == saved["sha256"]
    finally:
        store.close()


def test_unresolved_review_survives_context_rebuild(tmp_path):
    agent = make_agent(tmp_path)
    review = {"status": "reviewed", "verdict": "needs_work",
              "issues": ["One source requirement is missing"]}
    persist_review_state(agent, review)
    assert review_advisory(agent) == review
    persist_review_state(agent, {"status": "reviewed", "verdict": "progress_ok"})
    assert review_advisory(agent) is None


def test_final_review_reads_attributed_receipt_and_binds_verdict(tmp_path):
    agent = make_agent(tmp_path)
    path = todo_path(agent)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("- [x] Verify colors\n", encoding="utf-8")
    agent.provider = ScriptedReviewer()
    answer = asyncio.run(review_browser_task(agent=agent,
        original_goal="Verify colors.", operator_inputs=[], phase="final",
        final_answer="Colors verified."))
    assert answer["status"] == "reviewed"
    assert answer["verdict"] == "complete"
    assert answer["modelCalls"] == 2
    assert answer["toolCalls"] == 1
    assert answer["modelDurationMs"] >= 0
    assert answer["toolDurationMs"] >= 0
    assert agent.provider.calls == 2
    assert agent.provider.initial["originalUserGoal"] == "Verify colors."
    assert agent.provider.initial["todoHash"] == todo_snapshot(agent)["hash"]


def test_quota_failure_preserves_completed_review_reads_for_resume(tmp_path):
    agent = make_agent(tmp_path)
    write_todo(agent, "- [x] Verify colors\n")

    class InterruptedReviewer:
        def __init__(self):
            self.calls = 0

        async def generate_response(self, system_prompt, messages, tools):
            self.calls += 1
            if self.calls == 1:
                return "", [{"id": "read-before-quota", "name": "review_read_text",
                    "input": {"path": str(todo_path(agent))}}], "tool_use", {}
            raise RuntimeError("AccountQuotaExceeded")

    agent.provider = InterruptedReviewer()
    first = asyncio.run(review_browser_task(agent=agent,
        original_goal="Verify colors", operator_inputs=[], phase="progress"))
    assert first["status"] == "unavailable"
    assert first["sessionPersisted"]
    saved = read_review_session(agent)
    assert saved["checkpoint"]["phase"] == "progress"
    assert any("read-before-quota" in str(item) for item in saved["messages"])
    assert saved["evidence"]

    resumed = make_agent(tmp_path)

    class ResumedReviewer:
        async def generate_response(self, system_prompt, messages, tools):
            request = json.loads(messages[-1]["content"])
            assert request["interruptedReview"]["phase"] == "progress"
            assert any("read-before-quota" in str(item) for item in messages[:-1])
            assert request["reusableFileEvidence"] == []
            prior_read = next(json.loads(block["content"])
                for item in messages[:-1] if isinstance(item.get("content"), list)
                for block in item["content"]
                if block.get("type") == "tool_result" and block.get("tool_use_id") == "read-before-quota")
            evidence_id = prior_read["evidenceId"]
            return "", [{"id": "resume-submit", "name": "submit_browser_task_review",
                "input": {"issueRevision": request["issueRevision"], "goalHash": request["goalHash"],
                    "todoHash": request["todoHash"], "proposalHash": "",
                    "verdict": "progress_ok", "reason": "Read source retained",
                    "issues": [], "evidenceIds": [evidence_id],
                    "suggestedNextAction": ""}}], "tool_use", {}

    resumed.provider = ResumedReviewer()
    second = asyncio.run(review_browser_task(agent=resumed,
        original_goal="Verify colors", operator_inputs=[], phase="progress"))
    assert second["status"] == "reviewed", second
    assert read_review_session(resumed)["checkpoint"] is None


def test_fatal_browser_transport_stops_review_without_repeated_reads(tmp_path):
    agent = make_agent(tmp_path)
    write_todo(agent, "- [ ] Verify live page\n")
    agent.browser.connection_usable = False

    class FatalReadReviewer:
        def __init__(self):
            self.calls = 0

        async def generate_response(self, system_prompt, messages, tools):
            self.calls += 1
            assert self.calls == 1
            return "", [{"id": f"browser-{index}",
                "name": "review_browser_read", "input": {
                    "method": "DOM.getAXTree", "params": {"pageId": "page-1",
                        "purpose": "Verify live page"}}} for index in range(3)], "tool_use", {}

    agent.provider = FatalReadReviewer()
    result = asyncio.run(review_browser_task(agent=agent,
        original_goal="Verify live page", operator_inputs=[], phase="progress"))
    assert result["reason"] == "browser_transport_disconnected"
    assert result["sessionPersisted"]
    assert agent.provider.calls == 1
    assert agent.browser.calls == []
    assert len(read_review_session(agent)["messages"][-1]["content"]) == 3


def test_final_review_rejects_unbound_or_uncited_completion():
    base = {"issueRevision": 0, "goalHash": "g", "todoHash": "t", "proposalHash": "p",
            "verdict": "complete", "reason": "done", "issues": [],
            "suggestedNextAction": ""}
    assert _review_result({**base, "evidenceIds": []}, phase="final",
        goal_hash="g", todo_hash="t", proposal_hash="p",
        evidence_ids={"trace:0"})["status"] == "unavailable"
    assert _review_result({**base, "evidenceIds": ["trace:99"]}, phase="final",
        goal_hash="g", todo_hash="t", proposal_hash="p",
        evidence_ids={"trace:0"})["status"] == "unavailable"
    assert _review_result({**base, "evidenceIds": ["trace:0"]}, phase="final",
        goal_hash="g", todo_hash="t", proposal_hash="p",
        evidence_ids={"trace:0"}, inspected_evidence_ids=set())["status"] == "unavailable"
    assert _review_result({**base, "evidenceIds": ["trace:0"]}, phase="final",
        goal_hash="g", todo_hash="t", proposal_hash="p",
        evidence_ids={"trace:0"}, inspected_evidence_ids={"trace:0"})["status"] == "reviewed"


def test_prior_findings_cannot_disappear_on_complete_or_reclassification():
    base = {"issueRevision": 0, "goalHash": "g", "todoHash": "t", "proposalHash": "p",
            "verdict": "complete", "reason": "done", "issues": [],
            "evidenceIds": ["trace:0"], "suggestedNextAction": ""}
    bound = dict(phase="final", goal_hash="g", todo_hash="t", proposal_hash="p",
                 evidence_ids={"trace:0"}, fresh_evidence_ids={"trace:0"},
                 issues_state={"version": 2, "revision": 0, "items": {"issue:colors": {
                     "issueId": "issue:colors", "text": "Source colors remain uncreated", "status": "open"}}})
    dropped = _review_result(base, **bound)
    assert dropped["status"] == "unavailable"
    assert any("unresolved issue state" in error for error in dropped["errors"])
    still_open = _review_result({**base, "issueUpdates": [{
        "issueId": "issue:colors", "status": "open", "reason": "Not yet done",
        "evidenceIds": ["trace:0"]}]}, **bound)
    assert still_open["status"] == "unavailable"
    progress = _review_result({**base, "verdict": "progress_ok", "issueUpdates": [{
        "issueId": "issue:colors", "status": "open", "reason": "Not yet done",
        "evidenceIds": ["trace:0"]}]}, **{**bound, "phase": "progress"})
    assert progress["issues"] == ["Source colors remain uncreated"]
    need_input = _review_result({**base, "verdict": "needs_user", "issueUpdates": [{
        "issueId": "issue:colors", "status": "needs_user", "reason": "Source scope is ambiguous",
        "evidenceIds": ["trace:0"]}]}, **bound)
    assert need_input["issues"] == ["Source colors remain uncreated"]


def test_protocol_repair_keeps_findings_from_invalid_complete_submission(tmp_path):
    class ClearingReviewer:
        def __init__(self):
            self.calls = 0
            self.saw_required_disposition = False

        async def generate_response(self, _system_prompt=None, messages=None, tools=None,
                                    **_kwargs):
            self.calls += 1
            request = json.loads(messages[0]["content"])
            if self.calls == 1:
                return "", [{"id": "read", "name": "review_read_trace",
                             "input": {"evidence_id": request["recentReceipts"][0]["evidenceId"]}}], "tool_use", {}
            self.saw_required_disposition |= "issueRevision" in next(
                tool for tool in tools if tool["name"] == "submit_browser_task_review"
            )["input_schema"]["required"]
            return "", [{"id": f"submit-{self.calls}",
                "name": "submit_browser_task_review", "input": {
                    "issueRevision": request["issueRevision"], "goalHash": request["goalHash"], "todoHash": request["todoHash"],
                    "proposalHash": request["proposalHash"], "verdict": "complete",
                    "reason": "done", "newIssues": ([{"clientId": "sizes", "text": "Sizes omitted",
                        "reason": "Missing", "evidenceIds": [request["recentReceipts"][0]["evidenceId"]]}] if self.calls == 2 else []),
                    "evidenceIds": [request["recentReceipts"][0]["evidenceId"]], "suggestedNextAction": "",
                }}], "tool_use", {}

    agent = make_agent(tmp_path)
    todo_path(agent).parent.mkdir(parents=True, exist_ok=True)
    todo_path(agent).write_text("- [x] Product\n", encoding="utf-8")
    agent.provider = ClearingReviewer()
    result = asyncio.run(review_browser_task(agent=agent, original_goal="Create sizes",
        operator_inputs=[], phase="final", final_answer="done"))
    assert result["status"] == "unavailable"
    assert "Sizes omitted" in result["issues"]
    assert agent.provider.saw_required_disposition
    assert agent.provider.calls == 4


@pytest.mark.parametrize("backend", ["file", "db"])
def test_reviewer_searches_one_authorized_file_without_directory_glob(tmp_path, backend):
    agent = make_agent(tmp_path)
    store = None
    try:
        if backend == "db":
            agent.logger.run_id = "run-1"
            store = create_storage(backend="db", worktree_dir=str(tmp_path),
                                   sqlite_path="harness.db")
            store.create_task(task_id=agent.logger.task_id, harness_version="test")
            store.start_run(task_id=agent.logger.task_id,
                            harness_version="test", run_id="run-1")
            agent.logger.attach_storage(store)
            assert write_todo(agent, "- [ ] 颜色 红色\n- [ ] 尺码 XXL\n")["status"] == "done"
        else:
            todo_path(agent).parent.mkdir(parents=True, exist_ok=True)
            todo_path(agent).write_text("- [ ] 颜色 红色\n- [ ] 尺码 XXL\n", encoding="utf-8")
        result, _ = asyncio.run(_read_tool(agent, agent.trace,
            "review_search_text", {"path": str(todo_path(agent)),
                                   "pattern": "尺码|颜色"}, "r1"))
        assert result["status"] == "done"
        assert [hit["line"] for hit in result["results"]] == [1, 2]
        assert result["evidenceId"].startswith("file:")
        denied, _ = asyncio.run(_read_tool(agent, agent.trace,
            "review_search_text", {"path": str(tmp_path / "outside.md"),
                                   "pattern": "尺码"}, "r1"))
        assert denied["status"] != "done"
    finally:
        if store is not None:
            store.close()


def test_reviewer_single_file_search_pages_and_directory_search_still_works(tmp_path):
    agent = make_agent(tmp_path)
    source = Path(agent.logger.task_dir) / "sources" / "details.md"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text("other\n" * 1100 + "size XXL\n", encoding="utf-8")
    match, _ = asyncio.run(_read_tool(agent, agent.trace, "review_search_text",
        {"path": str(source), "pattern": "XXL"}, "r1"))
    assert match["status"] == "done"
    assert [item["line"] for item in match["results"]] == [1101]
    directory, _ = asyncio.run(_read_tool(agent, agent.trace, "review_search_text",
        {"path": str(source.parent), "glob": "*.md", "pattern": "XXL"}, "r1"))
    assert directory["status"] == "done"
    assert directory["count"] == 1


def test_reviewer_lists_file_names_with_pagination(tmp_path):
    agent = make_agent(tmp_path)
    source_dir = Path(agent.logger.task_dir) / "sources"
    source_dir.mkdir(parents=True)
    for index in range(5):
        (source_dir / f"part-{index}.txt").write_text("private contents", encoding="utf-8")
    first, _ = asyncio.run(_read_tool(agent, agent.trace, "review_list_files",
        {"path": str(source_dir), "limit": 2}, "r1"))
    second, _ = asyncio.run(_read_tool(agent, agent.trace, "review_list_files",
        {"path": str(source_dir), "limit": 2,
         "offset": first["nextOffset"]}, "r1"))
    assert first["status"] == second["status"] == "done"
    assert first["count"] == second["count"] == 2
    assert first["nextOffset"] == 2 and second["nextOffset"] == 4
    assert first["entries"][0]["name"] == "part-0.txt"
    assert "private contents" not in json.dumps(first)


@pytest.mark.parametrize("backend", ["file", "db"])
def test_reviewer_file_search_continues_without_repeating_hits(tmp_path, backend):
    agent = make_agent(tmp_path)
    store = None
    try:
        if backend == "db":
            agent.logger.run_id = "run-1"
            store = create_storage(backend="db", worktree_dir=str(tmp_path),
                                   sqlite_path="harness.db")
            store.create_task(task_id=agent.logger.task_id, harness_version="test")
            store.start_run(task_id=agent.logger.task_id,
                            harness_version="test", run_id="run-1")
            agent.logger.attach_storage(store)
        content = "".join(f"size {index}\n" for index in range(53))
        assert write_todo(agent, content)["status"] == "done"
        offset = 0
        source_hash = ""
        found = []
        for expected in (20, 20, 13):
            result, _ = asyncio.run(_read_tool(agent, agent.trace, "review_search_text",
                {"path": str(todo_path(agent)), "pattern": "size",
                 "line_offset": offset, "source_sha256": source_hash}, "r1"))
            assert result["status"] == "done"
            assert result["count"] == expected
            found.extend(hit["line"] for hit in result["results"])
            source_hash = result["sourceSha256"]
            offset = result["nextLineOffset"]
        assert found == list(range(1, 54))
        assert offset is None
        unbound, _ = asyncio.run(_read_tool(agent, agent.trace, "review_search_text",
            {"path": str(todo_path(agent)), "pattern": "size",
             "line_offset": 20}, "r1"))
        assert unbound["reason"] == "source_sha256_required_for_pagination"
        assert write_todo(agent, content + "size 53\n")["status"] == "done"
        stale, _ = asyncio.run(_read_tool(agent, agent.trace, "review_search_text",
            {"path": str(todo_path(agent)), "pattern": "size",
             "line_offset": 20, "source_sha256": source_hash}, "r1"))
        assert stale["reason"] == "source_changed_during_pagination"
    finally:
        if store is not None:
            store.close()


@pytest.mark.parametrize("backend", ["file", "db"])
def test_reviewer_context_restores_and_expires_changed_file_evidence(tmp_path, backend):
    agent = make_agent(tmp_path)
    store = None
    try:
        if backend == "db":
            agent.logger.run_id = "run-1"
            store = create_storage(backend="db", worktree_dir=str(tmp_path),
                                   sqlite_path="harness.db")
            store.create_task(task_id=agent.logger.task_id, harness_version="test")
            store.start_run(task_id=agent.logger.task_id,
                            harness_version="test", run_id="run-1")
            agent.logger.attach_storage(store)
        write_todo(agent, "- [ ] Verify color\n")

        class ContinuingReviewer:
            def __init__(self):
                self.seen_previous = False
                self.expired = False

            async def generate_response(self, system_prompt, messages, tools):
                requests = [json.loads(item["content"]) for item in messages
                            if isinstance(item.get("content"), str)
                            and item["content"].startswith("{\"originalUserGoal\"")]
                request = requests[-1]
                first = isinstance(messages[-1]["content"], str)
                if request["phase"] == "final" and first:
                    self.seen_previous = len(requests) == 2 and any(
                        "sourceSha256" in str(item) for item in messages[:-1])
                    self.expired = bool(request["expiredEvidenceIds"])
                    assert request["reusableFileEvidence"] == []
                if first:
                    return "", [{"id": "read-source", "name": "review_read_text",
                        "input": {"path": str(todo_path(agent))}}], "tool_use", {}
                last = json.loads(messages[-1]["content"][-1]["content"])
                return "", [{"id": "submit", "name": "submit_browser_task_review",
                    "input": {"issueRevision": request["issueRevision"], "goalHash": request["goalHash"],
                              "todoHash": request["todoHash"],
                              "proposalHash": request["proposalHash"],
                              "verdict": "complete" if request["phase"] == "final" else "progress_ok",
                              "reason": "Source was read", "issues": [],
                              "evidenceIds": [last["evidenceId"]],
                              "suggestedNextAction": ""}}], "tool_use", {}

        provider = ContinuingReviewer()
        agent.provider = provider
        progress = asyncio.run(review_browser_task(agent=agent,
            original_goal="Verify color", operator_inputs=[], phase="progress"))
        assert progress["status"] == "reviewed"
        assert progress["sessionPersisted"]
        assert read_review_session(agent)["messages"]
        write_todo(agent, "- [x] Verify color\n")
        resumed = make_agent(tmp_path)
        resumed.provider = provider
        if store is not None:
            store.start_run(task_id=agent.logger.task_id,
                            harness_version="test", run_id="run-2")
            resumed.logger.run_id = "run-2"
            resumed.logger.attach_storage(store)
        result = asyncio.run(review_browser_task(agent=resumed,
            original_goal="Verify color", operator_inputs=[], phase="final",
            final_answer="Verified"))
        assert result["status"] == "reviewed", result
        assert result["verdict"] == "complete"
        assert provider.seen_previous and provider.expired
    finally:
        if store is not None:
            store.close()


def test_reviewer_reuses_only_unchanged_trace_receipts(tmp_path):
    agent = make_agent(tmp_path)

    class TraceReviewer:
        def __init__(self):
            self.seen = []
            self.receipt = ""

        async def generate_response(self, system_prompt, messages, tools):
            request = next(json.loads(item["content"]) for item in reversed(messages)
                if isinstance(item.get("content"), str)
                and item["content"].startswith("{\"originalUserGoal\""))
            if isinstance(messages[-1]["content"], str):
                self.seen.append((request["reusableTraceEvidence"],
                                  request["expiredEvidenceIds"]))
                if request["recentReceipts"]:
                    self.receipt = request["recentReceipts"][0]["evidenceId"]
                    return "", [{"id": "trace-read", "name": "review_read_trace",
                        "input": {"evidence_id": self.receipt}}], "tool_use", {}
                assert self.receipt in str(messages[:-1])
            return "", [{"id": "trace-submit", "name": "submit_browser_task_review",
                "input": {"issueRevision": request["issueRevision"], "goalHash": request["goalHash"],
                    "todoHash": request["todoHash"],
                    "proposalHash": request["proposalHash"],
                    "verdict": "progress_ok", "reason": "Receipt checked",
                    "issues": [], "evidenceIds": [self.receipt],
                    "suggestedNextAction": ""}}], "tool_use", {}

    agent.provider = TraceReviewer()
    first = asyncio.run(review_browser_task(agent=agent,
        original_goal="Inspect receipt", operator_inputs=[], phase="progress"))
    assert first["status"] == "reviewed"
    original_ref = agent._browser_review_receipts.live_ids[0]
    assert read_review_session(agent)["evidence"][original_ref]["kind"] == "receipt"
    second = asyncio.run(review_browser_task(agent=agent,
        original_goal="Inspect receipt", operator_inputs=[], phase="progress"))
    assert second["status"] == "reviewed"
    assert agent.provider.seen[1][0] == []
    assert original_ref in read_review_session(agent)["evidence"]
    assert second["modelCalls"] == 1 and second["toolCalls"] == 0
    agent.trace[0]["result"]["content"] = "changed receipt"
    third = asyncio.run(review_browser_task(agent=agent,
        original_goal="Inspect receipt", operator_inputs=[], phase="progress"))
    assert third["status"] == "reviewed"
    assert agent.provider.seen[2][0] == []
    assert agent.provider.seen[2][1] == [original_ref]


def test_reviewer_browser_read_does_not_enter_action_dispatch(tmp_path):
    agent = make_agent(tmp_path)
    agent.worker_contract = {"forbidden_methods": ["Network.*"]}
    blocked = asyncio.run(_browser_read(agent, {"method": "Input.click",
        "params": {"pageId": "page-1"}}, "r1"))
    contract_blocked = asyncio.run(_browser_read(agent, {
        "method": "Network.readApi", "params": {"pageId": "page-1"}}, "r1"))
    foreign = asyncio.run(_browser_read(agent, {"method": "DOM.getAXTree",
        "params": {"pageId": "foreign", "purpose": "verify"}}, "r1"))
    observed = asyncio.run(_browser_read(agent, {"method": "DOM.getAXTree",
        "params": {"pageId": "page-1", "purpose": "verify"}}, "r1"))
    assert blocked["status"] == "rejected"
    assert contract_blocked["reason"] == "method_forbidden_by_contract"
    assert foreign["status"] == "rejected"
    assert agent.browser.calls == [("DOM.getAXTree", {
        "pageId": "page-1", "purpose": "verify"})]
    assert observed["status"] == "done"
    assert Path(observed["resourceUri"]).is_file()
    assert observed["sourceSha256"] == hashlib.sha256(
        Path(observed["resourceUri"]).read_bytes()).hexdigest()
    invalid = asyncio.run(_browser_read(agent, {"method": "DOM.getAXTree",
        "params": {"pageId": "page-1"}}, "r1"))
    assert invalid["status"] == "rejected"
    assert len(agent.browser.calls) == 1
    denied, _image = asyncio.run(_read_tool(agent, agent.trace, "browser_call",
        {"method": "Input.click", "params": {"pageId": "page-1"}}, "r1"))
    assert denied["status"] == "rejected"


@pytest.mark.parametrize("mode", ["full", "detail"])
def test_reviewer_consumes_already_hydrated_axtree(tmp_path, mode):
    agent = make_agent(tmp_path)
    response = {"observation": "Page view is available at /external/leased-view.",
        "suggested_prompt": "Read artifact.path first.",
        "warnings": ["A field is truncated; query detail for the complete value."],
        "data": {"mode": mode, "pageId": "page-1", "version": "v_current_2",
                 "freshness": "current", "pendingChanges": False,
                 "completeness": "complete",
                 "artifact": {"kind": mode, "sha256": "verified"}}}
    response["data"].update({"lines": ['1 [n_0000000000000001] button "color: red"']}
        if mode == "full" else {"records": [{"nodeId": "n_0000000000000001",
                                             "name": "color: red"}]})
    original = json.dumps(response, ensure_ascii=False, indent=2)
    async def hydrated(method, params, **kwargs):
        # Real ABCPClient removes the host path after validating the artifact.
        return response
    agent.browser.call = hydrated
    observed = asyncio.run(_browser_read(agent, {"method": "DOM.getAXTree",
        "params": {"pageId": "page-1", "purpose": "verify"}}, "r1"))
    assert observed["status"] == "done"
    assert "color: red" in observed["content"]
    assert "/external/leased-view" not in observed["content"]
    assert "Read artifact.path" not in observed["content"]
    assert "no external file read is needed" in observed["notice"]
    assert response == json.loads(original)
    stored = agent.logger.storage.read_resource(current_task_id=agent.logger.task_id,
                                                resource_uri=observed["resourceUri"])
    assert stored["content_text"] == original
    assert observed["sourceSha256"] == hashlib.sha256(original.encode()).hexdigest()
    metadata = json.loads(observed["content"].split("\n\nAX lines:\n")[0])
    assert metadata["warnings"] == response["warnings"]
    for key in ("pageId", "version", "freshness", "pendingChanges", "completeness", "artifact"):
        assert metadata["data"][key] == response["data"][key]
    if mode == "full":
        assert observed["content"].split("\n\nAX lines:\n")[1] == response["data"]["lines"][0]
    else:
        assert metadata["data"]["records"] == response["data"]["records"]


def test_review_ax_query_matches_unescaped_quotes_and_pages_saved_lines(tmp_path):
    agent = make_agent(tmp_path)
    lines = [f'1 [n_{i:016x}] button "Action {i}"' for i in range(13)]
    lines.append('1 [n_000000000000000e] textFieldWithComboBox "请输入"')
    async def response(_method, _params, **_kwargs):
        agent.browser.calls.append((_method, _params))
        return {"data": {"mode": "full", "lines": lines, "freshness": "current"}}
    agent.browser.call = response
    observed = asyncio.run(_browser_read(agent, {"method": "DOM.getAXTree",
        "params": {"pageId": "page-1", "purpose": "verify"}}, "r1"))
    fact = {"kind": "page", "resourceUri": observed["resourceUri"],
        "logicalPath": observed["logicalPath"], "sourceSha256": observed["sourceSha256"],
        "resourceVersion": observed["resourceVersion"], "runId": observed["runId"]}
    evidence = {observed["evidenceId"]: fact}
    query = _query_observation(agent, {"evidence_id": observed["evidenceId"],
        "patterns": ['button "', '请输入"'], "keys": ["freshness"], "limit": 80}, evidence)
    assert query["status"] == "done" and query["searchSource"] == "data.lines"
    assert query["limit"] == 10 and not query["coverageTruncated"]
    buttons, input_field, freshness = query["results"]
    assert len(buttons["matches"]) == 10 and buttons["nextOffset"] == 10
    assert len(input_field["matches"]) == 1
    assert freshness["matches"][0]["value"] == '"current"'
    ax_text = "\n".join(lines)
    hit = input_field["matches"][0]
    assert ax_text[hit["charOffset"]:hit["charOffset"] + hit["matchLength"]] == '请输入"'
    continued = _query_observation(agent, {"evidence_id": observed["evidenceId"],
        "patterns": ['button "'], "offset": buttons["nextOffset"], "limit": 10}, evidence)
    assert len(continued["results"][0]["matches"]) == 3
    assert continued["results"][0]["nextOffset"] is None
    rejected = _query_observation(agent, {"evidence_id": observed["evidenceId"],
        "patterns": [str(i) for i in range(10)]}, evidence)
    assert rejected["reason"] == "observation_query_invalid"
    assert "at most 8" in rejected["detail"] and "patterns=10" in rejected["detail"]
    from harness.agents.browser.review.task import review_tools
    schema = next(tool for tool in review_tools(agent)
                  if tool["name"] == "review_query_observation")["input_schema"]["properties"]
    assert schema["patterns"]["maxItems"] == 8
    assert schema["limit"]["maximum"] == query["limit"]
    assert len(agent.browser.calls) == 1


@pytest.mark.parametrize("backend", ["file", "db"])
def test_review_observation_is_task_resource_with_batched_nested_json_query(tmp_path, backend):
    agent = make_agent(tmp_path)
    agent.logger.run_id = "run-1"
    store = create_storage(backend=backend, worktree_dir=str(tmp_path),
                           sqlite_path="harness.db")
    store.create_task(task_id=agent.logger.task_id, harness_version="test")
    store.start_run(task_id=agent.logger.task_id, harness_version="test", run_id="run-1")
    agent.logger.attach_storage(store)
    agent.capability_methods.add("Network.readApi")
    agent.method_schemas["Network.readApi"] = agent.method_schemas["DOM.getAXTree"]
    async def response(_method, _params, **_kwargs):
        return {"data": {"responseBody": json.dumps({"specs": [
            {"color": "red", "size": "XL"}, {"color": "blue", "size": "XXL"}
        ]})}}
    agent.browser.call = response
    try:
        observed = asyncio.run(_browser_read(agent, {"method": "Network.readApi",
            "params": {"pageId": "page-1", "purpose": "verify"}}, "r1"))
        assert observed["status"] == "done"
        assert observed["resourceUri"].startswith("sqlite://") is (backend == "db")
        if backend == "db":
            assert not Path(observed["resourceUri"]).exists()
        fact = {"kind": "page", "resourceUri": observed["resourceUri"],
            "logicalPath": observed["logicalPath"],
            "sourceSha256": observed["sourceSha256"],
            "resourceVersion": observed["resourceVersion"],
            "capturedAt": observed["capturedAt"], "runId": "run-1"}
        query = _query_observation(agent, {"evidence_id": observed["evidenceId"],
            "patterns": ["red", "blue", "XXL"], "keys": ["color", "size"]},
            {observed["evidenceId"]: fact})
        assert query["status"] == "done"
        assert query["searchSource"] == "response"
        assert len(query["results"]) == 5
        colors = next(item for item in query["results"]
                      if item["type"] == "jsonKey" and item["query"] == "color")
        assert [hit["value"] for hit in colors["matches"]] == ['"red"', '"blue"']
        assert all("/$json/" in hit["path"] for hit in colors["matches"])
        if backend == "db":
            fact["resourceUri"] = str(agent.logger.task_dir / observed["logicalPath"])
            rebound = _query_observation(agent, {
                "evidence_id": observed["evidenceId"], "keys": ["color"],
            }, {observed["evidenceId"]: fact})
            assert rebound["status"] == "done"
            assert fact["resourceUri"].startswith("sqlite://")
        fact["sourceSha256"] = "0" * 64
        assert _query_observation(agent, {"evidence_id": observed["evidenceId"],
            "keys": ["color"]}, {observed["evidenceId"]: fact})["status"] == "unavailable"
        fact["sourceSha256"] = observed["sourceSha256"]
        foreign = make_agent(tmp_path)
        foreign.logger.task_id = "foreign-task"
        foreign.logger.attach_storage(store)
        assert _query_observation(foreign, {"evidence_id": observed["evidenceId"],
            "keys": ["color"]}, {observed["evidenceId"]: fact})["status"] == "unavailable"
    finally:
        store.close()


@pytest.mark.parametrize("backend", ["file", "db"])
def test_reviewer_search_pages_multiple_matches_on_one_line(tmp_path, backend):
    agent = make_agent(tmp_path)
    store = None
    try:
        if backend == "db":
            agent.logger.run_id = "run-1"
            store = create_storage(backend="db", worktree_dir=str(tmp_path),
                                   sqlite_path="harness.db")
            store.create_task(task_id=agent.logger.task_id, harness_version="test")
            store.start_run(task_id=agent.logger.task_id,
                            harness_version="test", run_id="run-1")
            agent.logger.attach_storage(store)
        assert write_todo(agent, " ".join(f"size{i}" for i in range(53)))["status"] == "done"
        line_offset = match_offset = 0
        source_hash = ""
        columns = []
        for expected in (20, 20, 13):
            result, _ = asyncio.run(_read_tool(agent, agent.trace,
                "review_search_text", {"path": str(todo_path(agent)),
                    "pattern": r"size\d+", "line_offset": line_offset,
                    "match_offset": match_offset,
                    "source_sha256": source_hash}, "r1"))
            assert result["status"] == "done" and result["count"] == expected
            columns.extend(hit["matchColumn"] for hit in result["results"])
            line_offset, match_offset = result["nextLineOffset"], result["nextMatchOffset"]
            source_hash = result["sourceSha256"]
        assert len(set(columns)) == 53
        assert line_offset is None and match_offset is None
    finally:
        if store is not None:
            store.close()


@pytest.mark.parametrize("backend", ["file", "db"])
def test_final_review_can_cite_validated_prior_page_capture(tmp_path, backend):
    agent = make_agent(tmp_path)
    agent.logger.run_id = "run-1"
    store = create_storage(backend=backend, worktree_dir=str(tmp_path),
                           sqlite_path="harness.db")
    store.create_task(task_id=agent.logger.task_id, harness_version="test")
    store.start_run(task_id=agent.logger.task_id, harness_version="test", run_id="run-1")
    agent.logger.attach_storage(store)
    assert write_todo(agent, "- [x] Verify color\n")["status"] == "done"

    class ReusePageReviewer:
        def __init__(self):
            self.page_id = ""
            self.final_request = None
            self.read_requested = False

        async def generate_response(self, system_prompt, messages, tools):
            request = next(json.loads(item["content"]) for item in reversed(messages)
                if isinstance(item.get("content"), str)
                and item["content"].startswith('{"originalUserGoal"'))
            if request["phase"] == "progress" and not self.read_requested:
                self.read_requested = True
                return "", [{"id": "capture", "name": "review_browser_read",
                    "input": {"method": "DOM.getAXTree", "params": {
                        "pageId": "page-1", "purpose": "verify"}}}], "tool_use", {}
            if request["phase"] == "progress":
                result = json.loads(messages[-1]["content"][-1]["content"])
                self.page_id = result["evidenceId"]
            else:
                self.final_request = request
                assert self.page_id in str(messages[:-1])
            return "", [{"id": "verdict", "name": "submit_browser_task_review",
                "input": {"goalHash": request["goalHash"], "todoHash": request["todoHash"],
                    "proposalHash": request["proposalHash"],
                    "issueRevision": request["issueRevision"],
                    "verdict": "complete" if request["phase"] == "final" else "progress_ok",
                    "reason": "Captured page establishes the observed color.",
                    "evidenceIds": [self.page_id], "suggestedNextAction": ""}}], "tool_use", {}

    provider = ReusePageReviewer()
    agent.provider = provider
    try:
        progress = asyncio.run(review_browser_task(agent=agent,
            original_goal="Verify color", operator_inputs=[], phase="progress"))
        assert progress["status"] == "reviewed"
        final = asyncio.run(review_browser_task(agent=agent,
            original_goal="Verify color", operator_inputs=[], phase="final",
            final_answer="Color verified"))
        assert final["status"] == "reviewed" and final["verdict"] == "complete"
        assert final["modelCalls"] == 1 and final["toolCalls"] == 0
        assert provider.final_request["reusablePageEvidence"] == []
        assert provider.page_id in read_review_session(agent)["evidence"]
        assert provider.final_request["expiredEvidenceIds"] == []
        assert len(agent.browser.calls) == 1
    finally:
        store.close()


@pytest.mark.parametrize("response,reason", [
    ({"error": {"code": "page-not-ready", "message": "loading"}}, "browser_read_failed"),
    ({"data": {"mode": "full", "observationError": {
        "code": "artifact-unavailable", "message": "expired"}}}, "axtree_artifact_unavailable"),
])
def test_reviewer_does_not_credit_failed_browser_read(tmp_path, response, reason):
    agent = make_agent(tmp_path)
    async def failed(_method, _params, **_kwargs):
        return response
    agent.browser.call = failed
    observed = asyncio.run(_browser_read(agent, {"method": "DOM.getAXTree",
        "params": {"pageId": "page-1", "purpose": "verify"}}, "r1"))
    assert observed["status"] == "unavailable"
    assert observed["reason"] == reason
    assert "evidenceId" not in observed and "notice" not in observed


def test_reviewer_trace_search_excludes_worker_claims(tmp_path):
    agent = make_agent(tmp_path)
    agent.trace.append({"type": "browser_completion_proposal",
                        "result": {"reason": "claimed-only marker"}})
    found, _ = asyncio.run(_read_tool(agent, agent.trace,
        "review_search_trace", {"term": "claimed-only marker"}, "r1"))
    rejected, _ = asyncio.run(_read_tool(agent, agent.trace,
        "review_read_trace", {"index": 1}, "r1"))
    assert found["matches"] == []
    assert rejected["status"] == "unavailable"


def test_reviewer_image_uses_authorized_bounded_attachment(tmp_path):
    agent = make_agent(tmp_path)
    image_path = Path(agent.logger.task_dir) / "deliverables" / "sample.png"
    image_path.parent.mkdir(parents=True, exist_ok=True)
    image_path.write_bytes(base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9WlT"))
    result, image = asyncio.run(_read_tool(agent, agent.trace,
        "review_view_image", {"path": str(image_path)}, "r1"))
    assert result["status"] == "done"
    assert image["type"] == "image"
    assert image["source"]["media_type"] == "image/png"
    assert base64.b64decode(image["source"]["data"]) == image_path.read_bytes()
    assert result["evidenceId"]
    external = tmp_path / "unapproved.png"
    external.write_bytes(image_path.read_bytes())
    denied, no_image = asyncio.run(_read_tool(agent, agent.trace,
        "review_view_image", {"path": str(external)}, "r1"))
    assert denied["status"] == "unavailable"
    assert no_image is None


def test_review_request_that_cannot_fit_context_fails_closed(tmp_path, monkeypatch):
    agent = make_agent(tmp_path)
    agent.provider = ScriptedReviewer()
    monkeypatch.setattr("harness.agents.browser.review.task.estimate_prompt_tokens",
                        lambda *_args, **_kwargs: 1000000)
    answer = asyncio.run(review_browser_task(agent=agent,
        original_goal="Inspect page.", operator_inputs=[], phase="progress"))
    assert answer["reason"] == "review_request_exceeds_context"
    assert agent.provider.calls == 0


def test_video_tool_reports_metadata_without_frames(tmp_path):
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        pytest.skip("ffmpeg/ffprobe not available")
    agent = make_agent(tmp_path)
    video = Path(agent.logger.task_dir) / "scratchpad" / "clip.mp4"
    video.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i",
                    "color=c=red:s=16x16:d=0.2", "-pix_fmt", "yuv420p",
                    "-y", str(video)], check=True)
    result = asyncio.run(_video_metadata(agent, str(video)))
    assert result["status"] == "done"
    assert float(result["metadata"]["duration"]) > 0
    assert result["metadata"]["streams"][0]["width"] == 16
    assert "frames" not in result
