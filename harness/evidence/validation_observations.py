"""Model-visible validation facts. Content meaning is not a mechanical gate."""
from pathlib import Path
from urllib.parse import urlsplit
from harness.results.row_ledger import absence_field_name, field_absence_accepted
from harness.utils import task_file_exists


def validation_observations(rows, expected, validators, *, task_dir=None, logger=None):
    observations, failures = [], []
    fields = expected.get("allow_empty_with_outcome") or {}
    fields = dict(fields) if isinstance(fields, dict) else {}
    for spec in expected.get("fields") or []:
        if isinstance(spec, dict) and spec.get("allow_empty_with_outcome"):
            fields[str(spec.get("name") or "")] = spec["allow_empty_with_outcome"]
    for i, row in enumerate(rows):
        for field, outcomes in fields.items():
            if field not in row or row[field] not in (None, "", [], {}):
                continue
            verdict = field_absence_accepted(row, field, allowed_outcomes=outcomes)
            declaration = row.get(absence_field_name(field))
            declaration = declaration if isinstance(declaration, dict) else {}
            observation = {"type": "empty_field_judgment", "row": i, "field": field,
                           "authority": "worker_semantic_judgment", "mechanicallyProven": False,
                           "declarationAccepted": verdict["accepted"],
                           "outcome": declaration.get("outcome", row.get(f"{field}Outcome")),
                           "evidenceText": str(declaration.get("evidenceText") or row.get(f"{field}EvidenceText") or "")[:2000]}
            refs = declaration.get("evidenceRefs", [])
            if not isinstance(refs, list) or any(not isinstance(ref, str) or not ref.strip() for ref in refs):
                failures.append({"type": "evidence_reference_invalid", "row": i, "field": field})
            else:
                checked = []
                for ref in refs:
                    path = Path(ref).expanduser()
                    if not path.is_absolute() and task_dir is not None:
                        path = Path(task_dir) / path
                    exists = path.is_file()
                    if not exists and logger is not None:
                        exists = task_file_exists(logger, str(path))
                    checked.append({"path": ref, "exists": exists})
                    if not exists:
                        failures.append({"type": "evidence_reference_missing", "row": i, "field": field, "path": ref})
                observation["evidenceReferences"] = checked
            observations.append(observation)
        for rule in validators:
            kind = rule.get("type")
            if kind not in {"allowed_domain", "url_pattern", "field_pattern", "cross_field_contains"}:
                continue
            if kind != "allowed_domain" and rule.get("enforcement") == "literal":
                continue
            field = str(rule.get("field") or "url")
            value = row.get(field)
            fact = {"type": "semantic_constraint_observation", "row": i, "field": field,
                    "rule": rule, "value": str(value)[:1000], "blocking": False}
            if kind in {"allowed_domain", "url_pattern"}:
                try:
                    fact["host"] = urlsplit(str(value or "")).hostname
                except ValueError:
                    fact["host"] = None
            observations.append(fact)
    return observations, failures
