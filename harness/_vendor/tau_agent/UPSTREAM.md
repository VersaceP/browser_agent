# Tau agent core provenance

- Upstream: https://github.com/huggingface/tau
- Commit: `c66fb879c1058f7b3d8514fb7f92c919d3c3e3b3`
- License: MIT; copied in `LICENSE`.
- Source: `src/tau_agent/{events,harness,loop,messages,provider,provider_events,tool_history,tools,types}.py`.
- Local import patch: `tau_agent.*` → `harness._vendor.tau_agent.*`.
- Local loop patch: stop on provider `length`, honor trusted tool `terminate`, pair skipped calls as synthetic results, map explicit domain errors, run the declared argument preparer once, and emit bounded live tool progress. Application policy stays outside this directory.
- Local message patch: preserve opaque thinking, stop reason and usage-availability fields; `populate_by_name` keeps this core compatible with the project's Pydantic 2.10 baseline.

The Tau application (`tau_coding`) and its shell tool are not vendored.
