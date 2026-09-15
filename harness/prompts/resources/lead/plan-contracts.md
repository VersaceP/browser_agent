---
id: lead.plan-contracts
audience: lead
version: "2026-09-15"
description: Complete task-plan examples, file validator parameters, independent dependencies and minimal contract repairs.
sources:
  - harness/tools/lead_tools.py
  - harness/task_control/plan_validation.py
  - harness/task_control/validators.py
emitter_sources:
  - harness/tools/lead_tools.py
related_tools:
  - emit_task_plan
  - begin_task_plan_draft
  - append_task_plan_draft
  - submit_task_plan_draft
  - repair_task_plan
error_codes:
  - task_plan_schema_invalid
  - mechanical_invalid
topics:
  - planning examples
  - output_contract
  - additional_checks
  - file_integrity
  - path_pattern
  - depends_on
aliases:
  - 计划合同示例
  - few shot
  - validator 参数
---
# Plan contracts: worked examples

Examples teach syntax and reasoning, not additional user requirements. Use the
live tool schema as authority. Copy only constraints justified by the original
request. Prefer one compact output_contract over duplicating expected_artifact
and validators. Use emit_direct_task_plan for a single coherent deliverable;
the emit_task_plan examples below demonstrate phase contracts. Large plans use
the same phase objects through the draft tools.

## Collection: count, identity and evidence

User: On the results page at https://example.org collect exactly the second
and fourth cards' titles and URLs in visible card order.

Correct emit_task_plan arguments:

```json
{"plan":{"goal":"Collect exactly cards 2 and 4 with page evidence.","phases":[{"id":"collect","task_type":"web_scrape","stage_hint":"collection","task":"Open https://example.org and identify cards 2 and 4 from the rendered card sequence. Record rank, title and URL with page evidence. Do not substitute unrelated links.","depends_on":[],"output_contract":{"name":"cards","rows":{"exact":2,"identity":{"field":"rank","values":[2,4]}},"fields":{"rank":{"type":"integer","required":true,"empty":"forbid","provenance":true},"title":{"type":"string","required":true,"empty":"forbid","provenance":true},"url":{"type":"url","required":true,"empty":"forbid","provenance":true}}}}]}}
```

Wrong: rows.exact=2 alone does not identify which two cards. Fix: declare the
rank identity and evidence. The compiler derives cardinality, set and
uniqueness validators. A schema-valid plan can still lack semantic coverage;
do not claim that mechanical approval proves the requested identities.
For “up to two”, use rows.max=2 instead; for a query-driven search phase use
web_search. URLs in prose should be separated from surrounding words and
sentence punctuation by whitespace; never include explanatory text in a URL.

## Download: validator type versus parameter

User: Download the supplied file https://example.org/manual.pdf into the
specified /tmp/example-delivery directory and verify it exists and is nonempty.
The directory here is an example user requirement, not a default destination.

Correct emit_task_plan arguments:

```json
{"plan":{"goal":"Save the supplied PDF in the requested directory.","phases":[{"id":"download","task_type":"file_download","stage_hint":"generic","task":"Download https://example.org/manual.pdf to /tmp/example-delivery/manual.pdf . Prepare the parent directory with available file tools, verify the physical file and record savedPath. Do not report an internal artifact path as the delivered file.","depends_on":[],"output_contract":{"name":"files","rows":{"exact":1},"fields":{"savedPath":{"type":"string","required":true,"empty":"forbid"}}},"additional_checks":[{"type":"file_integrity","path_fields":["savedPath"],"min_files":1,"min_bytes":1,"path_pattern":"^/tmp/example-delivery/"}]}]}}
```

Wrong: `{"type":"path_pattern","pattern":"..."}`. The mechanical
validator rejects the unknown type with an error naming the phase and
validator index and listing valid types. `path_pattern` belongs inside
file_integrity. `field_pattern` instead checks a row field's text using
`field` and `pattern`; it does not prove that a physical file exists.

Minimal repair: use the rejected candidateHash with repair_task_plan, replacing
the offending existing object via `op:"set"` at its actual reported JSON
pointer with the complete file_integrity object above. Keep other constraints.
If a file_integrity object already exists, set its path_pattern property using
`add` when absent. Removing an extra array element is structural: submit the
revised complete plan. Never invent a hash or assume a compiled validator index
is the original additional_checks index. Use repairIssues paths where present.
Successful repair still needs review/approval; an approval is followed by
execution, not another emit of the same plan.

## Independent branches: data dependency versus wave

User: Read one title from each of two supplied pages. The pages can be read
independently and neither requires shared mutable page state.

Correct emit_task_plan arguments:

```json
{"plan":{"goal":"Read both supplied pages independently.","output_contracts":{"title":{"rows":{"exact":1},"fields":{"title":{"type":"string","required":true,"empty":"forbid","provenance":true}}}},"phases":[{"id":"a","task_type":"web_scrape","stage_hint":"detail_sections","task":"Read the title from https://example.org/a with page evidence.","depends_on":[],"dispatch_wave":1,"output_ref":"title","output_contract":{"name":"title_a"}},{"id":"b","task_type":"web_scrape","stage_hint":"detail_sections","task":"Read the title from https://example.org/b with page evidence.","depends_on":[],"dispatch_wave":1,"output_ref":"title","output_contract":{"name":"title_b"}}]}}
```

Wrong: omit b.depends_on and expect parallel execution. Omission creates serial
dependencies, even with the same wave. Minimal repair: set depends_on=[] when
independence is justified; use the receipt's candidateHash or accepted plan's
basePlanVersion and replan_reason as appropriate. Runtime max_browser_agents
still limits concurrent workers.

For discovered pages, each consumer declares inputs.artifact with the actual
producer phase_id/artifact_name and depends_on:[producer]. A download consuming
only detail_a depends on detail_a, not detail_b. Put independent delivery and
detail branches in the same wave when the user has not requested a stage
barrier. Shared mutable state or real dependencies can require serialization;
matching task types alone do not justify parallelism.

## Stop confusing internal exceptions with contract feedback

A structured schema error identifies a field and permits a targeted correction.
An opaque tool_exception does not identify an invalid contract. In particular,
phase.worker_contract.reuse_scope is documented and valid; do not remove it
merely because a spawn raised ValueError. Follow lead.worker-status for
replayForbidden receipts. Paths and worker prose are not proof of artifact
contents; follow lead.artifact-validation for evidence and completion.

## Less common authoring choices

Nested data belongs inside its outer field. For example, the legacy shape
{"name":"reviews","type":"array","items":{"required":["reviewText","date"]}}
requires those keys inside each review. In compact fields syntax use
"reviews":{"type":"array","items":{"required":["reviewText","date"]}}.
Do not describe nested item fields as top-level artifact fields. Set minItems
or maxItems only from the requested collection size; required/empty policies
are decided separately (lead.artifact-validation).

Choose the phase task_type from its effects and live capabilities. Native
DOM.getImg export can stay in the page-owning phase if that task_type exposes
it, with image_exported and file_integrity evidence. Download.* saving needs
file_download; file_upload handles chooser work. Do not split a visual export
just to manufacture a URL-download stage, or force native image export when
it cannot deliver the requested asset. Batch sizes come from the live schema.

content_completeness regions/markers collect observations, not business
verdicts. Do not put a route mode, recovery policy, or retry count in
content_completeness; the worker interprets the observations against the goal.
Do not invent a missing collection size. Avoid hand-authored allowed-method
lists; task_type supplies the capability boundary. Extra restrictions, if
needed, must use canonical method names.

Declare the user-specified entity groups in the initial plan. If discovery is
needed to identify the entities and no existing phase can cover the result,
revise the plan after validated discovery, preserving completed work and
lineage. Lead declares phases; Harness may dispatch those already approved,
but does not invent a split. Default spawn is {"phase_id":"a"}; the task and
compiled contract are inherited. A continuation adds only new evidence and
remaining work in context, preserving the same phase and approved contract.

## Evidence-backed empty fields and file verification

`empty: "with_evidence"` plus `allow_empty_with_outcome: ["confirmed_absent"]`
compiles to a nonempty check WITH an exception. These are one conditional rule,
not a contradiction. Do not remove the nonempty rule and leave only its exception:
that makes emptiness pass without the evidence the declaration promises.

For an optional video-file array, reason about three cases:
- Files exist: record the observed paths and verify actual files with file_integrity.
- No video exists: use only the contract's complete absence evidence; no invented paths.
- Nothing was collected and absence is unproven: incomplete, not confirmed_absent.

File integrity checks declared paths, existence, size and hashes. min_files is
an explicit quantity constraint (default 0); an explicitly empty path_fields
population never borrows unrelated screenshots. Use a separate explicit count
when the user requires files. Every claimed file must still exist.

Conditional absence uses <field>Absence with outcome=confirmed_absent and
evidenceText. It records a worker judgment; no fixed proof flags or visual
ritual is required. Review adequacy against the original goal. allowed_domain
is retired; legacy declarations and ordinary business URL/field patterns are
advisory. field_pattern/url_pattern/cross_field_contains can use
enforcement="literal" only for an unambiguous literal user/protocol requirement,
never as a substitute domain affiliation gate.
