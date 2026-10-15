# Tau agent core provenance

- Upstream: https://github.com/huggingface/tau
- Commit: `c66fb879c1058f7b3d8514fb7f92c919d3c3e3b3`
- License: MIT; copied in `LICENSE`.
- Source: `src/tau_agent/{events,harness,loop,messages,provider,provider_events,tool_history,tools,types}.py`.
- Local import patch: `tau_agent.*` → `harness._vendor.tau_agent.*`.
- Local loop patch: stop on provider `length`, honor trusted tool `terminate`, pair skipped calls as synthetic results, map explicit domain errors, run the declared argument preparer once, and emit bounded live tool progress. Application policy stays outside this directory.
- Local message patch: preserve opaque thinking, stop reason and usage-availability fields; `populate_by_name` keeps this core compatible with the project's Pydantic 2.10 baseline.
- Local context patch: an optional `before_model` transform runs at completed tool-turn boundaries; applications persist and compact active context. Compaction messages retain checkpoint identity and structured facts. Raw event transcripts remain separate.
- Local steering patch: queued steering is checked between tool dispatches. Unsent calls receive paired `user_input_pending` results, and pending steering prevents a stale terminal tool from ending the loop. Input persistence, permissions and goal reconciliation stay in the application layer.

The Tau application (`tau_coding`) and its shell tool are not vendored.
