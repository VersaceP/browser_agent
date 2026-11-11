"""Opt-in stage descriptions and explicit report composition; no business verdicts."""
from __future__ import annotations

import json
import subprocess

from harness.workflow_mode.evidence import pointer


def validate_declarations(contract):
    stage = contract.get("stageProtocol")
    if stage is not None and (not isinstance(stage, dict) or stage.get("version") != 1
            or not isinstance(stage.get("entry"), str)):
        raise ValueError("stageProtocol requires version=1 and a source entry")
    report = contract.get("report", {})
    if not isinstance(report, dict):
        raise ValueError("report must be an object")
    parts = report.get("parts")
    if parts is None:
        return
    if not isinstance(parts, list):
        raise ValueError("report.parts must be an array")
    seen = set()
    for part in parts:
        if (not isinstance(part, dict) or not isinstance(part.get("id"), str)
                or not part["id"] or part["id"] in seen or not isinstance(part.get("pointer"), str)):
            raise ValueError("report.parts requires unique ids and output-relative JSON Pointers")
        address = part["pointer"]
        if address and not address.startswith("/"):
            raise ValueError("report.parts pointers must be empty or start with /")
        import re
        if re.search(r"~(?![01])", address):
            raise ValueError("report.parts pointers contain an invalid RFC 6901 escape")
        seen.add(part["id"])


def check_stages(root, contract, files):
    declaration = contract.get("stageProtocol")
    if declaration is None:
        return {"status": "legacy", "interpretation": "Stage dispatch is not mechanically verified"}
    if (not isinstance(declaration, dict) or declaration.get("version") != 1
            or not isinstance(declaration.get("entry"), str) or declaration["entry"] not in files):
        return {"status": "invalid", "error": "stageProtocol needs version=1 and an existing source entry"}
    from harness.workflow.offline_runtime import reachable, run_jobs
    runner = "__harness_stage_description.js"
    while runner in files:
        runner = "_" + runner
    sources = reachable({**files, runner:
        "import {describeStages} from " + json.dumps("./" + declaration["entry"]) + ";\n"
        "export default function main(){return describeStages();}"}, runner)
    job = {"workflow": {"schemaVersion": 1, "name": "stage-description", "entry": runner,
                        "timeoutMs": 5000, "files": sources}, "input": {}}
    try:
        value = run_jobs([job], timeout_s=15)[0]
    except (OSError, RuntimeError, ValueError, TimeoutError, subprocess.TimeoutExpired) as exc:
        return {"status": "unavailable", "error": str(exc)}
    if value.get("status") != "succeeded" or value.get("output") != contract.get("stages", []):
        return {"status": "invalid", "error": "Declared stages differ from the pure runtime description",
                "described": value.get("output"), "runtimeError": value.get("error")}
    return {"status": "valid", "stages": value["output"],
            "interpretation": "Declaration equality only; behavior/effect boundaries need separate verification"}


def report_parts(receipt, contract):
    """Compose only explicitly declared output addresses; never guess business keys."""
    output = ((receipt.get("response") or {}).get("data") or {}).get("output")
    report = contract.get("report") or {}
    declarations = report.get("parts")
    if declarations is None:
        declarations = [{"id": "output", "pointer": ""}]
    if not isinstance(declarations, list):
        raise ValueError("report.parts must be an array")
    rows, seen = [], set()
    for part in declarations:
        if (not isinstance(part, dict) or not isinstance(part.get("id"), str)
                or not part["id"] or part["id"] in seen or not isinstance(part.get("pointer"), str)):
            raise ValueError("report.parts needs unique ids and output-relative JSON Pointers")
        seen.add(part["id"])
        address = part["pointer"]
        try:
            selected = pointer(output, address)
            summary = selected.get("summary") if isinstance(selected, dict) else None
            rows.append({"id": part["id"], "pointer": "/response/data/output" + address,
                         "present": True, "summary": summary if isinstance(summary, dict) else None})
        except (ValueError, TypeError, KeyError, IndexError):
            rows.append({"id": part["id"], "pointer": "/response/data/output" + address, "present": False})
    return rows


def report_view(receipt, ref, contract):
    from harness.workflow_mode.tools import compact
    # Root summary already has a dedicated first view. Composition is opt-in.
    if "parts" not in (contract.get("report") or {}):
        return []
    return [{**part, "summary": compact(part.get("summary"), 6000),
             "evidenceScope": {"ref": ref, "pointer": part["pointer"]}}
            for part in report_parts(receipt, contract)]


def report_facts(output):
    """Structure/arithmetic facts for opted-in reports, never goal acceptance."""
    if not isinstance(output, dict) or output.get('protocolVersion') != 1:
        return {'status': 'legacy', 'issues': []}
    from collections import Counter
    issues = []
    summary = output.get('summary')
    delivery = output.get('delivery')
    if not isinstance(summary, dict):
        issues.append('summary must be an object')
        summary = {}
    if 'outcome' in summary and 'outcome' in output and summary['outcome'] != output['outcome']:
        issues.append('summary.outcome contradicts output.outcome')
    if not isinstance(delivery, dict) or type(delivery.get('essentialComplete')) is not bool:
        issues.append('delivery.essentialComplete must be a boolean')
    elif delivery['essentialComplete'] is False and output.get('outcome') == 'complete':
        issues.append('outcome=complete contradicts unavailable essential evidence')
    rows = output.get('items')
    coverage = None
    if rows is not None:
        if not isinstance(rows, list):
            issues.append('items must be an array when supplied')
        else:
            identities = [row.get('id') for row in rows if isinstance(row, dict)]
            if len(identities) != len(rows) or any(not isinstance(key, str) or not key for key in identities):
                issues.append('each item needs a nonempty string id')
            else:
                duplicate = sorted(key for key, count in Counter(identities).items() if count > 1)
                if duplicate:
                    issues.append('duplicate item ids: ' + str(duplicate))
                requested = output.get('requestedItemIds')
                if isinstance(requested, list) and all(isinstance(key, str) for key in requested):
                    coverage = {'missing': sorted(set(requested) - set(identities)),
                                'unrequested': sorted(set(identities) - set(requested)),
                                'duplicateResults': duplicate,
                                'duplicateRequests': sorted(key for key, count in Counter(requested).items() if count > 1)}
            if 'itemCount' in summary and (type(summary['itemCount']) is not int or summary['itemCount'] != len(rows)):
                issues.append('summary.itemCount contradicts the returned item array')
            counts = Counter(row.get('status') for row in rows if isinstance(row, dict) and isinstance(row.get('status'), str))
            if 'itemCounts' in summary and summary['itemCounts'] != dict(counts):
                issues.append('summary.itemCounts contradicts the returned item statuses')
    return {'status': 'issues' if issues else 'valid', 'issues': issues, 'identityCoverage': coverage,
            'interpretation': 'Structural consistency and declared-identity coverage only; classify relevance and business completion against the original goal.'}
