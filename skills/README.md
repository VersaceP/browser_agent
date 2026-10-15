# Skills

Published Skills live in `skills/<skill-id>/`. `SKILL.md` describes the goal, inputs,
outputs, execution conditions and recovery. Formal entries under `workflows/` are
WebCross JavaScript/TypeScript source packages (`schemaVersion`, `name`, `entry`,
`files`, optional `timeoutMs`); invocation variables are passed separately as
`input`. Trials under `trials/` are development probes, not formal entries.
Existing frontmatter fields and extensions are preserved.

## References and matching

Execute, publish and delete by Skill name or Skill ID. Resolution prefers an exact
Skill ID, then a case-insensitive exact name, then case-insensitive name containment.
A single match is selected automatically. Multiple matches print every matching
name and ID and require an exact selection; no edit-distance guess or recency choice
is used. `--skill` uses the same name resolver. Quote names containing spaces.

Skill IDs identify Skills across versions. A full task ID identifies an execution
or one Builder authoring conversation. `/skill-create` requires `@` followed by the
full 32-character source task ID. `/resume` requires the saved task ID (an optional
`@` prefix is accepted). Neither command uses fuzzy task IDs.

## Commands

Enter `/skill` to display published Skills and Builder drafts. The input prompt is
`[skill]>`. User task text and raw evidence retain their original language.

| Operation | Command |
| --- | --- |
| Select / execute a published Skill | `/<Skill-name-or-ID> [@Fleet-ID] [task]` |
| Create or update from execution evidence | `/skill-create [Skill-name-or-ID] @<task-id> [request]` |
| Publish a saved draft | `/skill-publish <Skill-name-or-ID>` |
| Delete a published Skill or draft | `/skill-delete <Skill-name-or-ID>` |
| Continue an execution or Builder draft | `/resume <task-id> [instruction]` |

Fleet selection is optional. A Skill-only command selects it and waits for the goal.
A command containing only a Skill and Fleet retains that Fleet for the next goal.
`/<Skill>` always executes; task IDs in the Fleet position never route to repair.
Mode navigation remains `/skill`, `/browser` and `/workflow [task]`.

Publish and delete resolve a Skill before selecting a version. If several drafts
match, publication lists their task IDs, versions and statuses and prompts for a
number. Deletion does the same when published files and drafts, or multiple drafts,
are present. Enter cancels. Non-interactive ambiguity returns the candidates and
exit code 3 without mutation. Multiple Skill-name matches require re-entering an
exact name or ID before any version choice. Hashes are read and checked internally;
users do not need to copy a content hash or specify `--draft` / `--published`.
Saved draft validation and publication conflict checks still apply. Draft deletion
checks current file identity, ownership and a content snapshot even for incomplete
drafts. Active task locks prevent publication, deletion and another resume from
changing the same owned draft concurrently. Task-owned execution sources survive
publication and deletion.

Omit the Skill name to create a new Skill; include a published name or ID to update
that Skill from the named task's evidence. The update copies the task's pinned
executed version when available, and binds the current published hash for conflict
checking. Multiple invocations of identical source bytes share one version binding
and retain all task evidence. If the task used different hashes, the CLI lists the
source invocations for explicit version selection. Non-interactive or cancelled
selection returns exit code 3 without starting a Builder. Updating creates a fresh
draft; `/resume <builder-task-id>` continues an existing authoring conversation.
Publication remains a separate command.

Answering `no` to file, test-page or Fleet authorization refuses that operation and
returns a denial to the Agent without terminating its model loop. The permission
is not granted. The Agent can continue with authorized operations or report the
current task incomplete if its goal cannot be met without that permission.

The old `/skill <name>`, `/skill-edit`, `/skill-repair`, `/skill-draft` and
`/skill-show` command forms are removed. Use `/skill` for inventory and the printed
work directory to inspect files. Continue editing with `/resume <builder-task-id>`;
reference a new source task in the instruction to authorize reading that run.

```text
/pdd-mms-publish-product @<fleet-id> Upload this product folder
/publish-product Upload this product folder
/skill-create @<source-task-id> Package the recorded workflow
/skill-create pdd-mms-publish-product @<new-task-id> Update from this run
/resume <builder-task-id> Improve using @<new-source-task-id>
/skill-publish pdd-mms-publish-product
/skill-delete pdd-mms-publish-product
```

## Execution and repair

`/skill` opens an independent mode alongside `/workflow` and `/browser`. All commands
above are available from its prompt. Selecting a published Skill with a task
starts its Orchestrator; selecting without a task waits for input. `--skill`
starts the same mode. Python 3.12+ is required.

### Develop once, package, then reuse

```text
/workflow @<fleet-id> Complete this task and leave a reusable program
/resume <workflow-task-id> Continue from the recorded effects
/skill-create @<workflow-task-id> Package the executed program as <skill-name>
/resume <builder-task-id> Improve the saved draft
/skill-publish <skill-name>
/<skill-name> @<fleet-id> Run with the new product and asset mappings
```

Workflow mode has one author who writes source, executes business work, and tests
that same workspace on a separate owned page. Browser may execute planned semantic
stages or bounded recovery. Equal URLs do not establish equal starting conditions;
test-page checkpoints never complete business-page work. There is no Builder
subagent, parallel source copy or merge wait.

Source lives in `worktree/<task-id>/coding/workflows/<workspace-id>/builder/`.
Independent trials keep their own durable task state and SQLite evidence indexes.
`/skill-create` and Builder `/resume` use the shared agent loop and execution services
while retaining draft identities, metadata and user-controlled publication.
Historical Builder logs remain readable as authorized source evidence; resuming an
old Workflow task does not restart or merge the removed subagent.


The Builder imports the original source bytes, then documents/parameterizes them;
it should not reconstruct a second implementation from the conversation. Actual
executed hashes and paths determine what has been verified. The user still
publishes explicitly. `/<Skill>` runs the pinned
published files with new input, continuing without model calls while the declared
facts hold. Existing broken templates are not automatically repaired by this mode.

For repeated rows, inputs should map business keys to assets, for example
`{key:{color:"red",size:"M"},files:["/approved/red.jpg"]}`. A Workflow discovers the
current row and its upload control, so row order, count and transient node IDs are
not fixed inputs. The model resolves the meaning of image assignments; the program
performs known matching, iteration, waiting and verification. Conflicts and missing
matches return evidence instead of assigning every file to the first control.

Task startup pins the published content hash. The Orchestrator reads `SKILL.md`
and `orchestration.json`, binds this run's values, and asks the Harness to execute
consecutive decided Workflow stages without a model call between them. Formal
entries remain WebCross JS/TS source packages. The contract uses workflow,
browser and decision stages; declared factual `continueWhen` comparisons permit
automatic continuation. Missing conditions, decisions and unexpected receipts
return to the owner. Historical JS Skills without orchestration can use an
explicit task-local plan; legacy steps DSL requires rebuilding.

During `/skill` reuse, Browser handles bounded judgment or recovery. Code defects
and deviations remain in the execution package for a later
`/resume <builder-task-id> Improve using @<task-id> <advice>`. Reuse does not launch background
replay or a Coding repair loop; its completion review assesses the business goal
and discloses program limitations. Separate Builder edit/trial sessions retain
files, conversation and raw receipts across rounds, compacting complete turns
when needed. Publication remains a user command. No shared shell
execution is introduced. Child completion never finishes the owner.

A failed Workflow may already have side effects. Check the raw receipt and page
before retrying. Interrupted running steps resume as uncertain, never as implicit
replay. Completed execution still requires independent whole-goal review. Drafts
require explicit publication before ordinary execution.

`/<Skill-name-or-ID> [@Fleet-ID] [task]` executes a published Skill. Fleet IDs
accept a full UUID or an 8-character hexadecimal prefix. Omitted Fleet bindings
follow the existing task browser-context policy.

Builder work files live in `worktree/<builder-task-id>/coding/builder/` and have a
SQLite index. Builder requires Python 3.12 or newer. Logs are readable only for
tasks explicitly referenced by the user. Referencing another source task in
`/resume` advice permits the model to select it as the analysis source while
preserving the original source provenance. `/resume` identifies Builders before
routing execution tasks to their saved Skill/Browser/Workflow mode, regardless of
the current prompt mode. Startup prints
the execution or Builder task ID. Skill tasks retain their plan, inputs, receipts,
checkpoints and conversation.

Builder can read the actual consumer contract with `describe_skill_runtime`,
observe authorized execution and Coding rounds with `observe_skill_run`, and run
the exact draft through the shared executor with `run_skill_trial`, without a
nested Orchestrator. Trials have independent IDs, indexed source snapshots and
receipts. On the same authorized Fleet, code and input changes continue the
owned trial; changed completed checkpoints yield for `resolve_trial_step`
reconciliation. `investigate_skill_trial` can request bounded Browser investigation
or read-only review. `read_draft_execution` compares current code to actual runs.
Only exact approved READ scopes transfer from Builder to its owned trial.

Browser task IDs remain valid `/skill-create` sources. Builder develops and tests
the new program; Workflow/Skill sources can import saved code and receipts.
Metadata-only changes preserve byte-identical program evidence; code changes
need affected-path verification. Task completion alone does not verify a program.
During explicit Skill execution, `recover_skill_step` records the owner's
semantic diagnosis and selects investigation, rebinding or deferral. Completion
of a business checkpoint does not certify an unexecuted program revision;
`resolve_skill_recovery` preserves the evidenced outcome. Use `/resume <builder-task-id>` for
subsequent program optimization, explicitly referencing the recorded run.

Terminal output groups startup, role messages, tool results and final outcomes.
During an active task, plain text or `/guide <text>` supplies user guidance to
the owner, active Browser workers and Reviewer. Reply to HITL/authorization with
`/answer <question-id> <answer>`; ordinary guidance is never an implicit answer.
`/messages` shows delivery status, `/pause` stops new dispatch and requests a
platform Workflow pause, and `/continue` lets the owner reconcile and resume.
In-flight effects retain their receipts. See
[runtime user steering](../docs/runtime-user-steering-plan.md).
Builder prints the draft/version, execution facts and publish/resume commands;
its complete result is indexed as `coding/builder/result.json`.

Execution owners read the exact active Skill files with `read_skill_resource`.
They prepare local business input using `local_fs_search` and `local_fs_read`.
External paths trigger the existing terminal READ authorization, shared by the
same task's Browser workers and restored from the task permission ledger. A
business answer in `request_skill_input` does not grant file access. File receipts
remain indexed for input provenance; read permission does not grant write access.
Step `input` contains business values; `binding.pageId` can reference a prior
Workflow output. Workflow Fleet events and `Page.list` can discover a new page,
but page actions remain bound to the authorized execution page. The next stage
checks Fleet inventory and the existing lease before acting on the new page.

Publication checks the saved draft hash and the bound published hash. A Builder
can publish successive edits by replacing `skills/<name>/`; an external change
produces a conflict. Publication creates no `.versions` archive or new
`skill_versions` rows and does not require a version-number increment. Existing
frontmatter fields (including an optional human version label) remain intact.
Before a task uses a Skill, its exact files are pinned under
`worktree/<task-id>/skill/sources/<hash>/` and indexed in `skill_task_sources`.
Publishing or deleting a Skill cannot change those task-owned bytes. Resume and
repair use the original task's source, not the newly published files.
Legacy publication snapshots are migrated for invocation, manifest/session and
Builder references before cleanup; a failed preparation keeps the old archive.
Stop old Harness processes before first running the new version, because an old
process still expects `.versions`. Deleting a draft removes only its work files.
Builder IDs, content hashes and recovery commands appear in inventories.
A generated draft is not evidence of business success. Mechanical checks cover
identity, permissions, file structure and protocol contracts; business path and
completion judgments remain with the Agent using the original goal and evidence.
