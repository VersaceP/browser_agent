"""Recheck persisted phase deliveries without dispatch or side-effect replay."""
import io
import json
from pathlib import Path
from harness.utils import read_task_file_text
from harness.storage.virtual_fs import db_authoritative_for


def _artifact_ledger_key(path, logger):
    location = Path(path).expanduser()
    if not location.is_absolute():
        location = Path(logger.task_dir) / location
    return str(location.resolve(strict=False))


def accepted_revalidation(state, phase_state, latest, phase, logger):
    from harness.task_control import evidence_contract_fingerprint
    from harness.task_control.phase_lifecycle import _artifact_sha256
    receipt = phase_state.get("artifactRevalidation") or {}
    digests = receipt.get("artifactDigests")
    if not isinstance(digests, dict):
        return False
    if not bool(
        phase_state.get("status") == "validated_done"
        and receipt.get("decision") == "accept"
        and receipt.get("validationStatus") == "done"
        and receipt.get("evidenceContractHash") in {
            evidence_contract_fingerprint(phase),
            evidence_contract_fingerprint(phase, legacy_task_type=True),
        }
        and receipt.get("sourceWorkerId") == latest.get("workerId")
        and set(digests) == set(phase_state.get("validated_artifacts") or [])
    ):
        return False
    ledger = state.get("artifact_digests") or {}
    for path, expected in digests.items():
        actual = _artifact_sha256(Path(path), logger)
        if not actual or actual != expected or ledger.get(_artifact_ledger_key(path, logger)) != expected:
            return False
    return True


def revalidate_phase_artifacts(agent, *, phase_id, plan_version, decision="inspect", reason=""):
    from harness import task_control as tc
    from harness.task_control.phase_lifecycle import _artifact_sha256
    logger = agent.logger
    state = tc.load_task_state(logger)
    phase = tc.find_phase(getattr(agent, "task_plan", None), phase_id)
    current = (state.get("phases") or {}).get(phase_id)
    if not isinstance(phase, dict) or not isinstance(current, dict):
        return {"status": "rejected", "error": "unknown_phase"}
    if plan_version != state.get("plan_version"):
        return {"status": "rejected", "error": "plan_version_changed"}
    attempts = current.get("attempts") or []
    latest = attempts[-1] if attempts else {}
    if decision not in {"inspect", "accept"} or (decision == "accept" and not reason.strip()):
        return {"status": "rejected", "error": "explicit_semantic_decision_required"}
    already_accepted = accepted_revalidation(state, current, latest, phase, logger)
    if (decision == "accept" and not already_accepted
            and current.get("status") not in {"validation_failed", "partial", "done", "validated_done"}):
        return {"status": "rejected", "error": "phase_not_revalidatable", "phaseStatus": current.get("status")}
    paths, worker_ids = [], set()
    for attempt in attempts:
        worker_ids.add(attempt.get("workerId"))
        validation = attempt.get("validation") or {}
        for key in ("artifacts", "allExtractionArtifacts", "fileArtifacts", "priorFileArtifacts"):
            for path in validation.get(key) or []:
                if path not in paths:
                    paths.append(path)
    # Only actual stored browser responses, scoped to this phase's workers.
    # Missing receipts remain missing; no historic success boolean is forged.
    evidence = []
    log_path = Path(logger.task_dir) / "run.jsonl"
    stream = (log_path.open(encoding="utf-8")
              if not db_authoritative_for(logger) and log_path.is_file()
              else io.StringIO(read_task_file_text(logger, log_path) or ""))
    with stream:
        for line in stream:
            try:
                event = json.loads(line)
            except ValueError:
                continue
            payload = event.get("payload") or {}
            if event.get("type") == "browser.call.result" and payload.get("workerId") in worker_ids and payload.get("phaseId") == phase_id:
                method = str(payload.get("method") or "")
                if method.startswith(("Download.", "File.")) or method == "DOM.getImg":
                    evidence.append({k: payload[k] for k in ("method", "params", "response") if k in payload})
    validation = tc.validate_worker_artifacts(contract=tc.phase_contract(phase), artifacts=paths,
        file_evidence=evidence, task_dir=logger.task_dir, logger=logger)
    result = {"status": "review_required" if validation.get("status") == "done" else "failed",
              "phaseId": phase_id, "phaseStatus": current.get("status"),
              "decision": decision, "validation": validation,
              "businessActionsReplayed": 0,
              "next_instruction": (
                  "Read-only inspection does not change this phase. Acceptance"
                  " requires an eligible finished status and a reason grounded"
                  " in the original goal, existing delivery and semanticObservations."
                  if current.get("status") not in {"validation_failed", "partial", "done", "validated_done"}
                  else "Review the original goal, existing delivery and semanticObservations. Accept with a reason only when these satisfy the goal; no worker is needed for revalidation."
              )}
    if decision != "accept" or validation.get("status") != "done":
        return result
    if already_accepted:
        return {**result, "status": "done", "decision": "already_accepted"}
    validated_paths = validation.get("artifacts") or []
    artifact_digests = {}
    for path in validated_paths:
        digest = _artifact_sha256(Path(path), logger)
        if not digest:
            return {**result, "status": "failed", "error": "artifact_integrity_unavailable"}
        artifact_digests[path] = digest
    receipt = {"decision": "accept", "reason": reason, "planVersion": state.get("plan_version"),
               "planHash": state.get("plan_hash"), "sourceWorkerId": latest.get("workerId"),
               "evidenceContractHash": tc.evidence_contract_fingerprint(phase),
               "artifactDigests": artifact_digests,
               "validationStatus": "done", "validation": validation, "reviewedAt": tc.utc_now_iso(),
               "priorFailure": current.get("last_failure"), "authority": "lead_semantic_judgment"}
    current["artifactRevalidation"] = receipt
    current["status"] = "validated_done"
    current["validated_artifacts"] = validated_paths
    current["last_failure"] = None
    current["last_failure_classification"] = None
    for path in current["validated_artifacts"]:
        if path not in state.setdefault("artifacts", []):
            state["artifacts"].append(path)
        state.setdefault("artifact_digests", {})[_artifact_ledger_key(path, logger)] = artifact_digests[path]
    state["current_phase"] = tc._first_active_phase_id(agent.task_plan, state["phases"])
    tc.write_task_state(logger, state)
    logger.write("task_phase.artifacts_revalidated", {"phaseId": phase_id, **receipt})
    return {**result, "status": "done", "decision": "accept", "next_instruction": "Existing artifacts were accepted after revalidation and Lead review. Continue the accepted plan; prior worker history and budgets are unchanged."}
