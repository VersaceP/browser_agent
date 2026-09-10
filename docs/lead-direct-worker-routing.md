# Lead route: direct worker and orchestration

The Lead still decides the route after reading the user task. It has two
planning tools:

- `emit_direct_task_plan` accepts one goal, one worker instruction, one
  `stage_hint`, one compact `output_contract`, and optional routing details.
  The harness compiles this to a canonical v1 plan containing one
  `browser_worker` phase (`execution_mode: "direct_worker"`).
- `emit_task_plan` remains the route for multiple phases, dependencies,
  parallel cohorts, producer/consumer artifacts, merges, or any decision that
  needs Lead coordination.

Both routes run the same mechanical validation, independent PlanValidator
review, and operator approval. The direct contract is smaller for model output;
the worker still receives the normal phase contract, method policy, session
binding, artifact validators, and lifecycle receipts. The approved direct phase
shows a default `max_attempts: 3` unless the Lead declares another value from
1 through 8.

After approval, the direct handler calls the existing spawn and wait paths. It
does not ask the Lead to copy `phase_id`, routing, or contract arguments. A
worker result can be retried automatically only when all of these control-plane
facts hold: the result is a bounded continuation status (`partial`,
`step_budget_exhausted`, `context_limit_exceeded`, `incomplete`, `page_crashed`,
or `fleet_assignment_lost`); no unresolved HITL/challenge/session blocker is
present; the phase and contract remain the same; the attempt budget remains;
and the result is not a repeat with the same failure signature and no new row
progress. The first safe transient retry may proceed without row progress. A
repeated no-progress signature is returned to Lead for a semantic decision.

`partial`/budget results with usable evidence are finalized through the existing
Lead final-answer checks after the bounded budget is reached. A `done` result is
accepted only when the phase lifecycle is `validated_done`; a raw worker claim
or an artifact file alone cannot terminate the task. Challenge, HITL, session
continuity, validation contradictions, unknown statuses, and repeated stalls
remain Lead or operator decisions. No site, field, or incident-specific rule is
used by this route.
