"""Recheck persisted phase deliveries without dispatch or side-effect replay."""
import hashlib
import io
import json
from pathlib import Path
from harness.utils import read_task_file_text
from harness.storage.virtual_fs import db_authoritative_for


def accepted_revalidation(state, phase_state, latest):
    receipt = phase_state.get("artifactRevalidation") or {}
    return bool(
        phase_state.get("status") == "validated_done"
        and receipt.get("decision") == "accept"
        and receipt.get("validationStatus") == "done"
        and receipt.get("planVersion") == state.get("plan_version")
        and receipt.get("planHash") == state.get("plan_hash")
        and receipt.get("sourceWorkerId") == latest.get("workerId")
    )


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
    already_accepted = accepted_revalidation(state, current, latest)
    if not already_accepted and current.get("status") not in {"validation_failed", "partial", "done"}:
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
              "phaseId": phase_id, "validation": validation, "businessActionsReplayed": 0,
              "next_instruction": "Review the original goal, existing delivery and semanticObservations. Accept with a reason only when these satisfy the goal; no worker is needed for revalidation."}
    if decision != "accept" or validation.get("status") != "done":
        return result
    if already_accepted:
        return {**result, "status": "done", "decision": "already_accepted"}
    receipt = {"decision": "accept", "reason": reason, "planVersion": state.get("plan_version"),
               "planHash": state.get("plan_hash"), "sourceWorkerId": latest.get("workerId"),
               "validationStatus": "done", "validation": validation, "reviewedAt": tc.utc_now_iso(),
               "priorFailure": current.get("last_failure"), "authority": "lead_semantic_judgment"}
    current["artifactRevalidation"] = receipt
    current["status"] = "validated_done"
    current["validated_artifacts"] = validation.get("artifacts") or []
    current["last_failure"] = None
    current["last_failure_classification"] = None
    for path in current["validated_artifacts"]:
        if path not in state.setdefault("artifacts", []):
            state["artifacts"].append(path)
        # Extraction resources may live in DB; native deliveries are bytes.
        try:
            resource_text = read_task_file_text(logger, path) if db_authoritative_for(logger) else None
        except UnicodeError:
            resource_text = None
        digest = (hashlib.sha256(resource_text.encode()).hexdigest()
                  if resource_text is not None else _artifact_sha256(Path(path), logger))
        if digest:
            state.setdefault("artifact_digests", {})[str(Path(path).resolve())] = digest
    state["current_phase"] = tc._first_active_phase_id(agent.task_plan, state["phases"])
    tc.write_task_state(logger, state)
    logger.write("task_phase.artifacts_revalidated", {"phaseId": phase_id, **receipt})
    return {**result, "status": "done", "decision": "accept", "next_instruction": "Existing artifacts were accepted after revalidation and Lead review. Continue the accepted plan; prior worker history and budgets are unchanged."}
