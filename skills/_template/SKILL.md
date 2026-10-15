---
name: replace-with-skill-name
version: "1"
description: Explain the reusable task and when this Skill applies.
domain: example.com
fields: []
allow_auto_captcha: false
---

## Goal and inputs

Describe the user goal, runtime inputs, page preconditions, and expected outputs.
Keep business values in execution input. Verify representative value, array-length,
optional-input and mapping changes; do not copy sample values into source.

## Skill orchestration

Maintain orchestration.json with ordered Workflow, Browser and decision stages.
The Skill Orchestrator binds this run's inputs; the Harness executes decided
stages continuously. Define factual continueWhen comparisons only where they
establish the chosen transition; omission yields to the owner. Whole-object
$binding and backward $result references carry values. Keep action loops in
Workflow source. Describe judgment, authorization and recovery entry points.
Resolve page and element identities at run time.
Keep execution binding separate from business input: binding.pageId may reference
a previous stage's /output/newPageId. Observe Fleet events or Page.list in source;
the host admits the next execution page through inventory and lease checks.
Use the current JS/TS source package (entry/files), default-export main(api,input),
and return compact JSON evidence. Parameter examples are supporting resources;
development probes remain in the Builder task worktree.

## Evidence and recovery

State which receipt fields establish progress. A failed Workflow may already
have changed the page; inspect the returned call records, current page
state and source locations before deciding how to continue. Persist task data with Harness tools
after browser execution when the user goal requires an artifact.
Use the actual consumer contract and run_skill_trial for newly authored paths.
Existing successful Skill Runtime records may supply validation for the exact
version and path; success produced by Browser recovery does not validate the
original source. Record the executed version, inputs and actual operations.
