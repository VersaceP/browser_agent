# Simplification replay baselines

These historical runs are regression fixtures, not production thresholds. The
site-specific counts in their artifacts may be used only as replay oracles.

| Run | Purpose | Accepted plan versions | Rejected plan emits | Reviewer calls | Final calls | Final Lead context | Recorded context hashes |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |
| `06fc0bb4b6c74146ad86c34b6626eb5f` | Active early final and numeric-gate friction | 3 | 5 | 4 | 4 (3 rejected) | 441,189 bytes | 4 |
| `0f75b23c391c4665852ae1160544eeeb` | `0/0/0` receipt followed by a false completion claim | 2 | 6 | 2 | 1 | 415,087 bytes | 2 |
| `5e614adacc7048c7b3b1307c5c48c20c` | Plan ceremony, partial continuation, then provider quota interruption | 3 | 3 | 3 | 0 | 336,964 bytes | 3 |
| `a5a9bc2bb31a4eee82f3cf16f6d6ac45` | Worker duplicate comment-expansion click plus repeated Lead waits | historical measurement | historical measurement | historical measurement | historical measurement | measured by analyzer | historical measurement |
| `48b4d7d71e62405a87db6fa7f1fc1404` | 1688 product/media progress interventions | historical measurement | historical measurement | historical measurement | historical measurement | measured by analyzer | historical measurement |
| `7c90ee49e4b34f67b6c59454c7a81a28` | Toolify listing/detail progress and extraction gates | historical measurement | historical measurement | historical measurement | historical measurement | measured by analyzer | historical measurement |

Source evidence is the corresponding `worktree/<run>/run.jsonl`, final Lead
context, task-plan history, worker traces, and task state. `5e614...` must not be
used as evidence of an early final: it ended on `AccountQuotaExceeded` with no
Lead final answer.

The replay comparison records:

- plan/replan and reviewer invocation counts;
- final-answer retries;
- Lead context bytes (and, when replay instrumentation exposes it, per-step
  context bytes);
- system-plus-tools prefix hashes/context hashes;
- productive browser actions and whether unresolved receipts survived the
  worker-to-Lead handoff.

Progress/loop enforcement is not removed from production by this baseline.
The decision requires a replay-only shadow run that still computes repeated
calls, row/artifact deltas, and progress observations while bypassing their
enforcement. No production configuration flag is introduced for that purpose.

`analyze_runs.py` measures the original (non-resume) segment of each historical
run and reports enforcement events that are candidates for a shadow bypass.
It deliberately leaves `behavioralConclusion` null: a recorded blocked call has
no browser receipt, so historical analysis cannot invent what the model would
have done after that call executed. `docs.replay_shadow` supplies the test-only
A/B gate fixture; neither utility is imported by production code.

For a real model/browser comparison, invoke `live_shadow_runner.py` with the
ordinary `main.py` arguments after `--`: variant `A` delegates unchanged;
variant `B` installs process-local witnesses and attaches
`shadowEnforcementObservations` to the real tool receipt. The patch is restored
when the process exits and no production configuration switch exists. Always
use separate task directories/runs for A and B; historical worktrees remain
read-only evidence. The B variant covers both BrowserAgent gates and the
LeadAgent duplicate-call guard; their facts use distinct `gateKind` values.
