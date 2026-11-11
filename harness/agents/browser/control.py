"""Standalone task policy: TODO, review triggers and context restoration.

The shared BrowserAgent engine invokes these hooks. Independent review is
controlled by the common runtime policy; otherwise the task owner judges delivery.
"""
from __future__ import annotations
import copy
import json
from harness.runtime.review_policy import reviewer_enabled
from harness.utils import JsonDict


class StandaloneTaskControl:
    def __init__(self, agent):
        self.agent = agent
        self.pending = False
        self.question = ""
        self.source_reads = {}

    @property
    def review_enabled(self) -> bool:
        return reviewer_enabled(self.agent.runtime)

    def begin_turn(self):
        self.pending = False
        self.question = ""

    def compaction_continuity(self):
        from harness.agents.browser.review.task import compaction_continuity
        return compaction_continuity(self.agent)

    def append_prompt(self, system_prompt):
        agent = self.agent
        todo_start_guidance = (
            " Before continuing execution, reconcile the restored checklist"
            " with the ordered user amendments and save it with update_task_todo."
            " Add or reopen affected items; if no checklist is available,"
            " recreate a short draft with unverified details marked."
            if getattr(agent, "standalone_resuming", False) else
            " Start by saving a short draft from the original user request"
            " with update_task_todo. Mark source-dependent details as unverified,"
            " then read the needed materials and refine the checklist before"
            " acting on them."
        )
        ready_guidance = (
            " If the whole request is ready for final review, set"
            " ready_for_final_review=true on update_task_todo and propose"
            " final_answer(done) next. The final review covers that update."
            if self.review_enabled else
            " Independent review is disabled. Judge completion against the"
            " original request and observed results, then call final_answer."
            " No reviewer will verify your completion decision."
        )
        review_guidance = (
            " Checklist writes save progress without triggering review. Once"
            " needed sources are inspected and the initial execution plan is"
            " formed, use request_goal_review once to check that plan against"
            " the goal before carrying it out. During execution request a targeted review for a"
            " material evidence conflict, doubtful direction or changed plan;"
            " combine related concerns. Routine progress updates need no review."
            " Reuse prior review history on resume/compaction; ask again when"
            " the changed goal, plan or evidence warrants it. A done final_answer"
            " is a completion proposal and ends only after independent verification."
            " Do not treat a review opinion as authority to change the user goal or permissions."
            if self.review_enabled else
            " Checklist writes save progress without triggering review."
        )
        system_prompt += (
            "\n\nStandalone /browser task: the original user request"
            " and attributed later user instructions define the goal."
            " You own this whole task and its completion."
            " Do not end the task for a turn count, context compaction,"
            " or a saved partial checkpoint. Recheck the original"
            " request against observed results as you work; correct"
            " your own course when evidence disagrees. Reuse applicable"
            " observed results for this complete goal check; refresh"
            " only facts still missing, conflicting or affected by later"
            " changes. A text-only"
            " turn does not complete the task. Use final_answer(done)"
            " after checking the entire requested outcome; use"
            " final_answer(incomplete) for a concrete blocker."
            " Ask through the existing pause channel only when"
            " information from the user is necessary."
            " Maintain a short Markdown checklist at"
            f" {agent.logger.task_dir}/scratchpad/todo.md."
            f"{todo_start_guidance}"
            " Call update_task_todo with the full Markdown content for"
            " each update. Check off an item only after verifying it."
            f"{ready_guidance}"
            " Keep source details and unresolved items visible; add"
            " discovered requirements and reopen mistaken checks."
            " For each stage, briefly keep its intended outcome, verified"
            " progress with receipt references, remaining or uncertain work,"
            " and the next execution approach in this same todo. Update at"
            " meaningful stage/batch boundaries or changed evidence, combining"
            " related changes rather than rewriting after every action. Record"
            " sources for choices that affect the result; when different"
            " records need different materials or values, include their"
            " associations as needed. Mark unresolved choices as uncertain"
            " and inspect relevant sources before claiming no basis exists."
            " After execution reconcile actual results and remaining work;"
            " preserve useful decisions and references on update or resume."
            " For artifact or source capture times, cite the actual tool"
            " receipt or media metadata; do not estimate a timestamp."
            " The original request, attributed later user instructions"
            " and designated materials, not your checklist, define"
            " completion. On resume, add or reopen checklist items"
            " affected by the new instruction."
            f"{review_guidance}"
            " Preserve unresolved requirements when rewriting todo.md;"
            " explain removed or reinterpreted items in the Markdown."
            " When new work invalidates a completed check, reopen the"
            " check and record the new work before continuing."
            " If a material scope or acceptance disagreement cannot be"
            " resolved from the original request, designated materials"
            " and real operator replies, ask the user through"
            " Hitl.requestPause with hitl_assistance_kind=information_request."
            " State the competing interpretations, evidence, options"
            " and consequence; do not change scope while awaiting a decision."
            " A timeout is not approval; preserve the question as unresolved."
            " For this standalone mode final_answer.answer is concise"
            " user-facing text in the user's language. State the actual"
            " result first, then only material unfinished work, needed"
            " user action and useful artifact links. Usually use one to"
            " three short sentences; add detail only when omitting it"
            " would misrepresent the result or the next action."
            " Do not narrate tool calls, review rounds, checklist upkeep"
            " or routine verification, and do not enumerate every"
            " field or variant already present in an artifact."
            " The worker JSON report format"
            " does not apply. Keep technical receipts and exhaustive"
            " field inventories in persisted artifacts, not the answer."
            " For structured data captures, persist at the source with browser_call"
            ' Runtime.evaluate and runtime_policy={"record_name":"<dataset name>"}.'
            " Return an array of row objects or {rows:[...]} directly from"
            " the expression, without JSON.stringify. Nested arrays/objects"
            " inside rows are preserved. Inspect recordExtraction and reuse"
            " its savedPath instead of retranscribing recorded rows. Recording"
            " does not prove source coverage or task completion."
            " Choose Runtime.evaluate world from the expression's dependencies:"
            " prefer isolated for shared DOM and standard Web APIs; use main"
            " when page-defined globals, framework state or page-owned object"
            " identity are required. Choose auto only when either world works"
            " and a possible retry is safe. Isolation still shares the DOM and"
            " is not a guarantee against automation detection."
        )
        if getattr(agent, "standalone_resuming", False):
            system_prompt += (
                "\nThis is a resumed task. Re-observe the live page"
                " and persisted results before repeating any action"
                " with side effects. A previous attempt's unknown"
                " outcome is not permission to replay it."
            )

        return system_prompt

    def configure_tools(self, tools):
        completion_guidance = (
            "Done proposes completion and requires independent evidence review"
            " against the original goal and todo.md."
            if self.review_enabled else
            "Independent review is disabled; done ends the task based on your"
            " evidence-grounded completion decision."
        )
        for tool_spec in tools:
            # Registry schemas are shared; per-task policy must not change
            # another runtime's tool descriptions.
            tool_spec["input_schema"] = copy.deepcopy(tool_spec["input_schema"])
            if tool_spec.get("name") == "browser_call":
                policy = tool_spec["input_schema"]["properties"]["runtime_policy"]
                policy["description"] = (
                    "Optional Harness JSON recording for Runtime.evaluate;"
                    " never forwarded to ABCP and does not authorize or classify"
                    " the expression. Set record_name (automatically enables JSON) to"
                    " persist returned rows through record_extraction, without"
                    " regenerating the data in a second tool call. Return a"
                    " row-object array or {rows:[...]} directly, not a JSON string."
                    " The result's recordExtraction contains status and savedPath."
                )
            if tool_spec.get("name") == "final_answer":
                tool_spec["description"] = (
                    "Report the standalone browser task's explicit"
                    " terminal outcome. Done requires a saved, readable, non-empty"
                    f" task checklist. {completion_guidance} Use incomplete for"
                    " a concrete blocker. Partial saves a checkpoint"
                    " and the same BrowserAgent continues."
                )
                tool_spec["input_schema"]["properties"]["answer"]["description"] = (
                    "Usually one to three short user-facing sentences: actual result first,"
                    " then only material gaps, needed user action and useful artifact links."
                    " Keep field inventories and routine verification in artifacts."
                    " Do not embed the worker receipt JSON."
                )
            if tool_spec.get("name") == "update_task_todo" and not self.review_enabled:
                tool_spec["input_schema"]["properties"]["ready_for_final_review"]["description"] = (
                    "Ignored while independent review is disabled; the checklist is still saved."
                )

    def restore_initial_context(self, messages):
        agent = self.agent
        from harness.agents.browser.review.task import review_advisory, todo_snapshot
        initial_todo = todo_snapshot(agent)
        if initial_todo["exists"]:
            messages[0]["content"] += (
                "\n\n<restored_browser_todo>\n"
                + initial_todo["content"][:24000]
                + "\n</restored_browser_todo>"
            )
        elif initial_todo.get("error"):
            messages[0]["content"] += (
                "\n\n<browser_todo_storage_error>"
                + str(initial_todo["error"])
                + "</browser_todo_storage_error>"
            )
        prior_review = review_advisory(agent)
        if prior_review:
            messages[0]["content"] += (
                "\n\n<unresolved_browser_review>\n"
                + json.dumps(prior_review, ensure_ascii=False)
                + "\n</unresolved_browser_review>"
            )
        messages[0]["content"] += self._review_checkpoint(initial_todo)
        from harness.agents.browser.resume import restore_resume_messages
        restore_resume_messages(agent, messages)

    def restore_compacted_context(self, messages):
        agent = self.agent
        from harness.agents.browser.review.task import review_advisory, todo_snapshot
        restored_todo = todo_snapshot(agent)
        pending_review = review_advisory(agent)
        messages.append({"role": "user", "content": (
            "<browser_todo_after_compaction>\n"
            + restored_todo["content"][:24000]
            + "\n</browser_todo_after_compaction>\n"
            + ("<browser_todo_storage_error>"
               + str(restored_todo["error"])
               + "</browser_todo_storage_error>\n"
               if restored_todo.get("error") else "")
            + ("<unresolved_browser_review>\n"
               + json.dumps(pending_review, ensure_ascii=False)
               + "\n</unresolved_browser_review>\n"
               if pending_review else "")
            + self._review_checkpoint(restored_todo)
            + "Recheck this progress against the original user goal."
        )})

    def _review_checkpoint(self, todo):
        from harness.agents.browser.review.task import read_review_state
        previous = read_review_state(self.agent) or {}
        if previous.get("status") != "reviewed":
            return ""
        checkpoint = {key: previous[key] for key in (
            "phase", "verdict", "goalHash", "todoHash",
        ) if key in previous}
        checkpoint["currentTodoHash"] = todo["hash"]
        return ("\n<browser_review_checkpoint>\n"
                + json.dumps(checkpoint, ensure_ascii=False)
                + "\n</browser_review_checkpoint>\n"
                "This is a prior review, not approval of later work. Hashes"
                " identify saved versions; judge goal and evidence applicability.\n")

    async def completion_review(self, proposed_answer):
        agent = self.agent
        from harness.agents.browser.review.task import todo_snapshot
        current_todo = todo_snapshot(agent)
        if (current_todo.get("error") or not current_todo["exists"]
                or not current_todo["content"].strip()):
            todo_reason = str(current_todo.get("error") or (
                "todo_empty" if current_todo["exists"] else "todo_missing"))
            review = {"status": "unavailable",
                      "requirement": "persisted_task_todo",
                      "reason": todo_reason,
                      "suggestedNextAction": (
                          "Inspect the task storage and recover"
                          " the persisted checklist before finalizing."
                          if current_todo.get("error") else
                          "Save a non-empty scratchpad/todo.md with"
                          " update_task_todo from the"
                          " original goal and designated sources;"
                          " verify outcomes before checking items."
                      )}
            agent._write_agent_event("browser.task_todo.required", {
                "phase": "final", "step": agent._current_step,
                "reason": todo_reason,
            })
        elif not self.review_enabled:
            review = await self.review(phase="final", final_answer=proposed_answer)
        else:
            review = await agent._review_standalone_task(
                phase="final", final_answer=proposed_answer, question=self.question,
            )
        self.pending = False
        self.question = ""
        return review

    def observe_tool_result(self, tool_call, result):
        from harness.agents.browser.resume import source_read_fact
        fact = source_read_fact(tool_call, result)
        if fact:
            key = tuple(fact.get(k) for k in ("path", "lineOffset", "linesRead", "readContentSha256"))
            self.source_reads[key] = fact
        if not self.review_enabled:
            return
        if (tool_call.get("name") == "request_goal_review"
                and isinstance(result, dict)
                and result.get("status") == "requested"):
            self.pending = True
            self.question = str(result.get("question") or "")

    async def review_boundary(self):
        if not self.review_enabled:
            self.pending = False
            return []
        agent = self.agent
        tool_results = []
        if (self.pending
                and getattr(agent, "standalone_hitl_timeout", None)):
            self.pending = False
            agent._write_agent_event("browser.task_review.skipped", {
                "phase": "progress", "step": agent._current_step,
                "reason": "hitl_wait_awaiting_user",
            })
            tool_results.append({"type": "text", "text": (
                "HITL wait ended without a confirmed page resume. Your todo was saved."
                " Preserve any recorded user answer; an interrupted wait adds no decision or permission."
                " Do not write to the paused page."
                " Report the concrete blocker briefly with final_answer(incomplete)."
            )})
        elif self.pending:
            self.pending = False
            review = await agent._review_standalone_task(
                phase="progress", question=self.question,
            )
            from harness.agents.browser.review.task import review_needs_attention
            if review_needs_attention(review):
                tool_results.append({"type": "text", "text": (
                    "<browser_task_review>\n"
                    + json.dumps(review, ensure_ascii=False)
                    + "\n</browser_task_review>\n"
                    "Compare this finding with the original request"
                    " and actual evidence. Update todo.md and fix"
                    " the result where warranted."
                )})
        return tool_results

    async def review(
        self, *, phase: str, final_answer: str = "", question: str = "",
    ) -> JsonDict:
        """Continue the task's independent Browser review without task owner planning."""
        agent = self.agent
        if not self.review_enabled:
            skipped = {"status": "skipped", "reason": "reviewer_disabled"}
            agent._write_agent_event("browser.task_review.skipped", {
                "phase": phase, "step": agent._current_step, **skipped,
            })
            return skipped
        from harness.agents.browser.review.task import (
            persist_review_state, review_browser_task, review_for_executor,
        )
        from harness.planning.context import user_context

        source = getattr(agent, "authoritative_user_context", {})
        original_goal = (
            source.get("originalUserTask") if isinstance(source, dict) else None
        ) or getattr(agent, "task_memory_root_task", "")
        original_goal = str(original_goal or "")
        context = user_context(agent.logger, original_goal)
        agent._write_agent_event("browser.task_review.start", {
            "phase": phase, "step": agent._current_step,
        })
        try:
            review = await review_browser_task(
                agent=agent, original_goal=original_goal,
                operator_inputs=context.get("operatorInputs", []),
                phase=phase, final_answer=final_answer, question=question,
            )
        except Exception as exc:
            review = {"status": "unavailable", "reason": "reviewer_execution_failed",
                      "errorType": type(exc).__name__}
        agent._write_agent_event("browser.task_review", {
            "phase": phase, "step": agent._current_step, **review,
        })
        persist_review_state(agent, review)
        return review_for_executor(review)
