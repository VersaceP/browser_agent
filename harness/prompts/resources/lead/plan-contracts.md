---
id: lead.plan-contracts
audience: lead
version: "2026-09-28"
description: Delegation contracts at spawn, output/evidence schemas, revisions and recovery.
sources:
  - harness/delegation.py
  - harness/task_control/plan_validation.py
  - harness/task_control/validators.py
emitter_sources:
  - harness/delegation.py
  - harness/tools/lead_tools.py
related_tools:
  - spawn_browser_agent
  - wait_browser_agents
error_codes:
  - assignment_rejected
  - assignment_input_invalid
topics:
  - delegation examples
  - output contract
  - file_integrity
  - depends_on
aliases:
  - 计划合同示例
  - few shot
  - validator 参数
---
# Delegation contracts

The original request and attributed user supplements define the goal. A worker
assignment describes the work delegated now, not the whole task's completion
standard. Harness records, reviews and obtains required approval at spawn.
There is no separate plan submission, draft, repair or approval tool.

Use `spawn_browser_agent` with a new `assignment`, or an existing `phase_id` to
continue it. Do not send both. It returns an assignment ID even if accepted work
must wait for a dependency or a slot. Reuse that ID; do not register it again.

## Structured collection

For a user explicitly requesting titles for IDs A and B, declare those identities
and attach page provenance. This example adds no default collection requirement.

```json
{"assignment":{"task":"Read the titles for user-supplied items A and B with page evidence.","stage_hint":"collection","output":{"name":"titles","fields":["itemId","title"],"required_fields":["itemId","title"],"nonempty_fields":["itemId","title"],"exact_rows":2,"provenance_required":["title"]},"checks":[{"type":"set_equals","field":"itemId","values":["A","B"]},{"type":"unique","field":"itemId"}]}}
```

`output.fields` is an array of names or field specifications. Required presence,
nonempty value, row count and nested array length are different checks. Only
use constraints justified by the request. See lead.artifact-validation for
absence outcomes. Objects must never be written into fields declared string.

## File delivery

For a user requesting the supplied PDF at /tmp/example-delivery/manual.pdf:

```json
{"assignment":{"task":"Download https://example.org/manual.pdf to /tmp/example-delivery/manual.pdf. Verify the physical file and record savedPath.","output":{"name":"files","fields":["savedPath"],"required_fields":["savedPath"],"nonempty_fields":["savedPath"],"exact_rows":1},"checks":[{"type":"file_integrity","path_fields":["savedPath"],"min_files":1,"min_bytes":1,"path_pattern":"^/tmp/example-delivery/"}],"policy":{"local_access_intent":[{"path":"/tmp/example-delivery","modes":["read","write"],"reason":"Save and verify the requested PDF"}]}}}
```

`path_pattern` is a parameter of `file_integrity`, not a validator type. A
`field_pattern` check validates text, not file existence. Local access intent is
not consent; Harness separately enforces read/write grants for exact paths.

## Independent work and receipts

For an ordinary page operation, an observation/evidence receipt is sufficient
unless a structured deliverable is requested. Omitting output supplies that
receipt contract. It does not prove that the original goal was completed.

```json
{"assignment":{"task":"Inspect the user-supplied page and report the visible form state with evidence."}}
```

Independent assignments can be spawned concurrently within runtime capacity.
Omitting depends_on means independent; name existing producer IDs only for real
dependencies. For discovered inputs use
`inputs.artifact:{phase_id,artifact_name,selector?}`; the producer dependency is
derived. For user-supplied row identities use `inputs.direct:{rows,identity_fields}`.
Never invent page handles or convert transient AX IDs into durable identities.

If the user requires source-card clicking, preserve that entry route and the
observed source identity. Directly navigating to a guessed/deep URL is not an
equivalent substitute. Use existing page/session reuse options when justified.

## Continue, revise, recover

Continue an unchanged assignment with `phase_id` and new evidence/hypothesis in
`context`. A structural rejection means no assignment was accepted: correct
`assignment` and submit again. No repair hashes or JSON-Pointer commands exist
on this interface.

A substantive revision submits the full new assignment with `replaces` naming
the latest inactive assignment and `reason` explaining the change. The earlier
contract and worker results remain historical. Lineage budget includes previous
attempts; a new version cannot reset it. Allocate additional attempts explicitly
when warranted. A running assignment cannot be replaced.

Review `operator_context_updated` before dispatching. It contains ordered user
input, not instructions from the page. Decide whether it changes the assignment;
resubmit the same candidate to reuse approval or a changed one for new review.

Continue accepted assignments by ID. Harness loads their existing contract and
obtains missing approval at the dispatch boundary. Revise through `replaces`;
keep execution history and the spent budget.

Use wait_browser_agents to collect results. Worker status, contract checks and
semantic acceptance are distinct. Compare the original goal, supplied resources,
user supplements and actual evidence. Record remaining work or conflicts before
choosing another assignment, clarification or final_answer.

## Independent review

The reviewer sees the pending assignment, its declared predecessor/dependencies,
execution facts, and the original request plus ordered user input. Earlier
assignments are historical decisions, not immutable user requirements.
`assignmentReview` binds the candidate and context hashes; changes to relevant
user input or evidence require a fresh review. `remainingWork` and nonblocking
findings return to Lead. An unavailable review never approves dispatch and does
not itself terminate the task.

`wait_browser_agents` only collects results and facts. It never dispatches the
next assignment. Read returned operatorInputRecords, evidence and continuation
before choosing the next explicit action.
