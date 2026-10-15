"""Versioned optional source helpers, installed through normal workspace CAS."""
from pathlib import Path


def install(builder, *, path="src/harness_helpers.js", expected_hash=None):
    source = Path(__file__).resolve().parents[1] / "workflow/sdk/response_helpers.js"
    body = source.read_text()
    result = builder._write_file(path, body, expected_hash)
    return {**result, "helperVersion": "1", "scope": "WebCross Workflow SDK action values; not host RPC envelopes",
        "next": "Read this source and include it in build_workflow_package files. pageRows expects Page.list's array value; errors stay explicit. Recheck live schemas after platform changes. No selectors, business policies or automatic retries."}
