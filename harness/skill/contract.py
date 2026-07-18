"""harness.skill.contract - skill selection/enrichment for worker contracts.

LeadAgent has the phase rows, detail URLs, and validators, so skill selection
belongs at spawn time. The worker-level dispatch still owns execution and
fallback: explicit skill_id wins, selected variables are validated, and failed
fast paths fall through to the BrowserAgent slow path.
"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from harness.extraction_artifacts import field_names_from_specs
from harness.pacing import parse_utc_timestamp


# Minimum soft-recall score required to interrupt the Lead with a selection
# request. Paired with skill_selection_signal (structural gate) so neither a
# bare domain match (+8) nor keyword-only overlap can trigger on its own.
_MIN_SELECTION_SCORE = 4

_SUITE_ROUTE_SOURCE_KEY = "_skill_route_source"
_SUITE_ROUTE_SOURCE_VALUE = "suite_routed"


def is_suite_routed(worker_contract: Dict[str, Any]) -> bool:
    """Whether a suite selected the skill by an exact phase match.

    This internal provenance is intentionally distinct from a direct, human
    force: suite-routed runs participate in health accounting and autoheal.
    """
    return (
        isinstance(worker_contract, dict)
        and str(worker_contract.get(_SUITE_ROUTE_SOURCE_KEY) or "")
        == _SUITE_ROUTE_SOURCE_VALUE
    )


def _health_entry(provider: Any, skill_id: str) -> Dict[str, Any]:
    if provider is None or not hasattr(provider, "entry"):
        return {}
    try:
        entry = provider.entry(skill_id)
        return entry if isinstance(entry, dict) else {}
    except Exception:
        return {}


def _updated_timestamp(value: Any) -> float:
    parsed = parse_utc_timestamp(value)
    if parsed is None:
        return float("-inf")
    try:
        return parsed.timestamp()
    except (OverflowError, OSError):
        return float("-inf")


def _skill_create_report(skill: Any) -> Dict[str, Any]:
    directory = getattr(skill, "directory", None)
    if directory is None:
        return {}
    try:
        from harness.skill.registry import load_create_report
        report = load_create_report(directory)
        return report if isinstance(report, dict) else {}
    except Exception:
        return {}


def _truthy_report_flag(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"true", "yes", "1", "on"}


def _cold_start_eligible(skill: Any, entry: Dict[str, Any], report: Dict[str, Any]) -> bool:
    """A quality-gated workflow with no runtime health gets one suite trial.

    `--recheck` is a dry-run, so it may grant eligibility but must not fabricate
    a successful health run. The first real suite-routed fast-path outcome
    creates the health entry and naturally consumes this state.
    """
    if getattr(skill, "is_hints_only", False) or entry:
        return False
    if bool(getattr(skill, "is_draft", False)) or not bool(
        getattr(skill, "is_tested", False)
    ):
        return False
    if "cold_start_eligible" in report:
        return _truthy_report_flag(report.get("cold_start_eligible"))
    # Backward compatibility for skills generated before the explicit flag was
    # introduced. Both statuses mean the persisted-contract dry-run passed.
    return str(report.get("status") or "") in {"created", "recheck_passed"}


def _skill_updated(skill: Any, report: Dict[str, Any]) -> str:
    """Return skill-content freshness, deliberately separate from health time."""
    frontmatter = getattr(skill, "frontmatter", {}) or {}
    if isinstance(frontmatter, dict):
        explicit = str(
            frontmatter.get("updated_at") or frontmatter.get("updated") or ""
        ).strip()
        if explicit:
            return explicit
    candidates = [
        str(report.get(key) or "").strip()
        for key in ("updated_at", "created_at")
    ]
    candidates = [value for value in candidates if value]
    if not candidates:
        return ""
    return max(candidates, key=_updated_timestamp)


def _suite_rank_evidence(
    skill: Any,
    *,
    workflow_health: Any = None,
    guidance_health: Any = None,
) -> Dict[str, Any]:
    provider = guidance_health if getattr(skill, "is_hints_only", False) else workflow_health
    entry = _health_entry(provider, str(getattr(skill, "skill_id", "") or ""))
    report = _skill_create_report(skill)
    disabled = False
    if not getattr(skill, "is_hints_only", False):
        if provider is not None and hasattr(provider, "is_disabled"):
            try:
                # SkillHealth.is_disabled also covers the maintenance-policy
                # consecutive-failure threshold, not just persisted disabled.
                disabled = bool(provider.is_disabled(skill))
            except Exception:
                disabled = bool(entry.get("disabled", False))
        else:
            disabled = bool(entry.get("disabled", False))
    total_runs = int(entry.get("total_runs", 0) or 0)
    total_failures = int(entry.get("total_failures", 0) or 0)
    health_updated = str(entry.get("updated") or "")
    skill_updated = _skill_updated(skill, report)
    cold_start_pending = _cold_start_eligible(skill, entry, report)
    evidence = {
        "skill_id": str(getattr(skill, "skill_id", "") or ""),
        "disabled": disabled,
        "eligible": not disabled,
        "draft_false": not bool(getattr(skill, "is_draft", False)),
        "tested_true": bool(getattr(skill, "is_tested", False)),
        "cold_start_pending": cold_start_pending,
        "total_runs": total_runs,
        "total_failures": total_failures,
        "successful_runs": total_runs - total_failures,
        "health_updated": health_updated,
        "skill_updated": skill_updated,
    }
    evidence["score"] = (
        int(evidence["draft_false"]),
        int(evidence["tested_true"]),
        int(evidence["cold_start_pending"]),
        int(evidence["successful_runs"]),
        _updated_timestamp(skill_updated),
    )
    return evidence


def _route_forced_collection(
    skills: List[Any],
    phase: Optional[Dict[str, Any]],
    worker_contract: Dict[str, Any],
    *,
    workflow_health: Any = None,
    guidance_health: Any = None,
) -> "tuple[Optional[Any], str, List[str], List[Dict[str, Any]]]":
    """A 方案：一组强制 skill 按 THIS phase 的四维路由到唯一成员。

    区分维是 stage_hint + fields（domain/task_type 在同一技能组内通常一致、不
    区分——已验证 collection.fields ⊆ detail_phase.fields 的子集关系让 fields
    单独会误命中，靠 stage_hint 精确相等拆开）。用 s.matches(domain=s.domain,…)
    让 domain 自匹配（跳过组内 domain 区分——用户选 suite 已隐含 domain scope）。
    返回 (chosen|None, reason, matched_ids, ranking)：workflow 已禁用时先排除
    （guidance 没有禁用语义）；恰好 1 个有效命中→选中；≥2 个有效命中再按
    non-draft → tested → cold-start → 成功次数 → skill.updated 排序，唯一
    胜者→ranked；cold-start 只给质量门通过、尚无真实 health 的 workflow 一次
    suite 试跑机会，绝不把 dry-run 伪装成成功记录；
    0 或最终仍并列才不盖章走慢路径。ranking 已按实际竞选顺序排列，可直接
    用于日志，避免重复读取 health。"""
    phase = phase or {}
    task_type = str(worker_contract.get("task_type") or phase.get("task_type") or "")
    stage_hint = str(phase.get("stage_hint") or worker_contract.get("stage_hint") or "")
    expected = phase.get("expected_artifact")
    if not isinstance(expected, dict):
        expected = worker_contract.get("expected_artifact")
    fields = set()
    if isinstance(expected, dict) and isinstance(expected.get("fields"), list):
        # task_plan supports both compact string fields and typed field specs
        # such as {"name": "rank", "type": "integer"}. Stringifying the
        # latter turns the entire dict into a bogus field name and makes every
        # suite member miss before health ranking even starts.
        fields = set(field_names_from_specs(expected.get("fields")))
    matched = [
        s for s in skills
        if s.matches(domain=s.domain, task_type=task_type,
                     stage_hint=stage_hint, fields=fields or None)
    ]
    ranked = [
        (s, _suite_rank_evidence(
            s,
            workflow_health=workflow_health,
            guidance_health=guidance_health,
        ))
        for s in matched
    ]
    ranked.sort(
        key=lambda item: (int(item[1]["eligible"]), item[1]["score"]),
        reverse=True,
    )
    ranking = [
        {key: value for key, value in evidence.items() if key != "score"}
        for _, evidence in ranked
    ]
    eligible = [(s, evidence) for s, evidence in ranked if evidence["eligible"]]
    matched_ids = [s.skill_id for s in matched]
    if len(eligible) == 1:
        reason = "routed" if len(matched) == 1 else "ranked"
        return eligible[0][0], reason, matched_ids, ranking
    if len(eligible) > 1:
        ranked = eligible
        best_score = max(item[1]["score"] for item in ranked)
        winners = [item[0] for item in ranked if item[1]["score"] == best_score]
        if len(winners) == 1:
            return winners[0], "ranked", matched_ids, ranking
        return None, "ambiguous", matched_ids, ranking
    if matched:
        return None, "disabled", matched_ids, ranking
    return None, "no_match", matched_ids, ranking


def apply_forced_skill(
    worker_contract: Dict[str, Any],
    *,
    registry: Any,
    forced_skill_id: str,
    phase: Optional[Dict[str, Any]] = None,
    logger: Any = None,
    workflow_health: Any = None,
    guidance_health: Any = None,
) -> bool:
    """Human/operator override: stamp a forced skill onto the worker_contract.

    `forced_skill_id` may be a SINGLE id OR a comma-separated COLLECTION (a
    /skill <suite> expansion). Single id → unconditional stamp (07-07 semantics:
    the skill runs wherever its vars derive; the LLM/contract self-arbitrate on a
    partial-fit phase). Collection (≥2) → route by Skill.matches four dims against
    THIS phase; multiple matches use the deterministic quality/health ranking in
    `_route_forced_collection`. Only an exact suite route receives the internal
    `suite_routed` marker used by outcome accounting. Direct single force does not.

    Returns True iff a skill was stamped. A stamped skill wins over Lead
    auto-match / decline (skill_selection cleared). Unknown ids are dropped
    (logged); an all-unknown / unrouted collection returns False, leaving normal
    selection intact.
    """
    forced_raw = str(forced_skill_id or "").strip()
    if not forced_raw or registry is None or not isinstance(worker_contract, dict):
        return False
    worker_contract.pop(_SUITE_ROUTE_SOURCE_KEY, None)
    ids = [s.strip() for s in forced_raw.split(",") if s.strip()]
    skills: List[Any] = []
    unknown: List[str] = []
    for sid in ids:
        try:
            sk = registry.get(sid)
        except Exception:  # pragma: no cover - registry lookup must never break spawn
            sk = None
        (skills.append(sk) if sk is not None else unknown.append(sid))
    if unknown and logger is not None and hasattr(logger, "write"):
        try:
            logger.write("skill.forced.unknown", {"skill_id": unknown})
        except Exception:  # pragma: no cover
            pass
    if not skills:
        return False

    if len(skills) == 1:
        chosen, reason, matched_ids, ranking = (
            skills[0], "single", [skills[0].skill_id], []
        )
    else:
        chosen, reason, matched_ids, ranking = _route_forced_collection(
            skills,
            phase,
            worker_contract,
            workflow_health=workflow_health,
            guidance_health=guidance_health,
        )
        if chosen is None:
            if logger is not None and hasattr(logger, "write"):
                try:
                    logger.write(f"skill.forced.{reason}", {
                        "candidates": [s.skill_id for s in skills],
                        "matched": matched_ids,
                        "phaseId": str((phase or {}).get("id") or ""),
                        "ranking": ranking,
                    })
                except Exception:  # pragma: no cover
                    pass
            return False  # 0 or ambiguous → slow path

        worker_contract[_SUITE_ROUTE_SOURCE_KEY] = _SUITE_ROUTE_SOURCE_VALUE
        if reason == "ranked" and logger is not None and hasattr(logger, "write"):
            try:
                logger.write("skill.forced.ranked", {
                    "phaseId": str((phase or {}).get("id") or ""),
                    "chosen": chosen.skill_id,
                    "ranking": ranking,
                    "priority": [
                        "not_disabled", "draft_false", "tested_true",
                        "cold_start_pending", "successful_runs", "skill_updated",
                    ],
                })
            except Exception:  # pragma: no cover
                pass

    worker_contract["skill_id"] = chosen.skill_id
    worker_contract.pop("skill_selection", None)  # operator force overrides decline
    if logger is not None and hasattr(logger, "write"):
        try:
            logger.write("skill.forced", {
                "skill_id": chosen.skill_id, "reason": reason,
                "from_collection": len(skills) > 1,
            })
        except Exception:  # pragma: no cover
            pass
    return True


def skill_selection_declined(worker_contract: Dict[str, Any]) -> bool:
    selection = worker_contract.get("skill_selection")
    if not isinstance(selection, dict):
        return False
    if selection.get("use_skill") is False:
        return True
    status = str(selection.get("status") or "").strip().lower()
    return status in {"declined", "skip", "skipped", "none"}


def _selection_skill_id(worker_contract: Dict[str, Any]) -> str:
    skill_id = str(worker_contract.get("skill_id") or "").strip()
    if skill_id:
        return skill_id
    selection = worker_contract.get("skill_selection")
    if isinstance(selection, dict) and selection.get("use_skill") is not False:
        return str(selection.get("skill_id") or "").strip()
    return ""


def enrich_worker_contract_with_skill(
    worker_contract: Dict[str, Any],
    *,
    registry: Any,
    phase: Optional[Dict[str, Any]] = None,
    task: str = "",
    context: str = "",
    logger: Any = None,
    mode: str = "manual",
) -> Optional[Dict[str, Any]]:
    """Stamp skill_id (+ skill_variables) onto worker_contract when a skill matches.

    Returns enrichment info or None. Mutates worker_contract in place. In
    mode="manual" (default) enrichment only fills variables for an EXPLICITLY
    selected skill (forced /skill or Lead-set skill_id) — no auto-stamping.
    """
    if registry is None or not isinstance(worker_contract, dict):
        return None
    selected_skill_id = _selection_skill_id(worker_contract)
    if selected_skill_id and not worker_contract.get("skill_id"):
        worker_contract["skill_id"] = selected_skill_id
    if skill_selection_declined(worker_contract):
        return None
    if worker_contract.get("skill_id") and isinstance(worker_contract.get("skill_variables"), dict):
        return None
    try:
        from harness.skill.dispatch import (
            _find_url,
            derive_variables,
            required_filled,
            select_skill,
        )
    except Exception:  # pragma: no cover
        return None

    try:
        target_url = _find_url("", [worker_contract, phase, task, context]) or ""
        skill = select_skill(registry, worker_contract, target_url=target_url, mode=mode)
        if skill is None:
            return None

        info: Dict[str, Any] = {"skill": skill.skill_id}
        if not worker_contract.get("skill_id"):
            worker_contract["skill_id"] = skill.skill_id
            info["set_skill_id"] = True

        if not isinstance(worker_contract.get("skill_variables"), dict):
            variables = derive_variables(skill, worker_contract, phase or {}, task, context)
            if required_filled(skill, variables):
                worker_contract["skill_variables"] = variables
                info["set_skill_variables"] = sorted(variables.keys())
            else:
                info["skill_variables_unfilled"] = True

        if logger is not None and hasattr(logger, "write"):
            try:
                logger.write("skill.contract.enriched", info)
            except Exception:  # pragma: no cover
                pass
        return info
    except Exception as exc:  # never break spawning
        if logger is not None and hasattr(logger, "write"):
            try:
                logger.write("skill.contract.enrich_error", {"error": str(exc)})
            except Exception:  # pragma: no cover
                pass
        return None


def build_skill_selection_request(
    worker_contract: Dict[str, Any],
    *,
    registry: Any,
    phase: Optional[Dict[str, Any]] = None,
    task: str = "",
    context: str = "",
    logger: Any = None,
    max_candidates: int = 3,
    mode: str = "manual",
) -> Optional[Dict[str, Any]]:
    """Return a LeadAgent-facing skill selection request, or None.

    mode="manual" (default): never fires — skill use is a user decision
    (/skill <id>), the Lead is not asked to pick. mode="auto" restores the
    soft-recall gate: keywords/field aliases/domain/stage rank candidates, and
    the LeadAgent's follow-up call chooses or declines.
    """
    if mode != "auto":
        return None
    if registry is None or not isinstance(worker_contract, dict):
        return None
    if _selection_skill_id(worker_contract) or skill_selection_declined(worker_contract):
        return None
    try:
        from harness.skill.dispatch import (
            _contract_fields,
            _find_url,
            derive_variables,
            required_filled,
        )
    except Exception:  # pragma: no cover
        return None

    try:
        fields = _contract_fields(worker_contract)
        target_url = _find_url("", [worker_contract, phase, task, context]) or ""
        domain = _host_of_url(target_url) or str(worker_contract.get("domain") or "")
        text = " ".join([
            str(task or ""),
            str(context or ""),
            json.dumps(phase or {}, ensure_ascii=False, default=str),
            json.dumps(worker_contract or {}, ensure_ascii=False, default=str),
        ])
        stage_hint = str(worker_contract.get("stage_hint") or "")
        candidates = registry.soft_candidates(
            domain=domain,
            task_type=str(worker_contract.get("task_type") or ""),
            stage_hint=stage_hint,
            fields=fields,
            text=text,
            limit=max_candidates,
        )
        # Gate before interrupting the Lead: pure keyword overlap (common tokens
        # vs a SKILL.md body) or a bare domain match would otherwise raise a
        # skill_selection_required round-trip on nearly every browser phase, and
        # surface single-detail skills over unrelated collection phases. Require
        # a structural applicability signal (exact stage_hint or a non-generic
        # output-field overlap) plus a minimum total score.
        try:
            from harness.skill.registry import skill_selection_signal
            candidates = [
                (skill, score, reasons)
                for (skill, score, reasons) in candidates
                if score >= _MIN_SELECTION_SCORE
                and skill_selection_signal(
                    skill, domain=domain, stage_hint=stage_hint, fields=fields
                )
            ]
        except Exception:  # pragma: no cover - never block spawning on gate import
            pass
        if not candidates:
            return None

        payload_candidates: List[Dict[str, Any]] = []
        for skill, score, reasons in candidates:
            variables = derive_variables(skill, worker_contract, phase or {}, task, context)
            payload_candidates.append(_skill_candidate_payload(
                skill,
                score=score,
                reasons=reasons,
                variables=variables,
                required_filled=required_filled(skill, variables),
            ))

        request = {
            "status": "skill_selection_required",
            "reason": (
                "Reusable skill candidates were found. Read each candidate's"
                " skillMarkdown before deciding whether to use one."
            ),
            "phaseId": str((phase or {}).get("id") or worker_contract.get("phase_id") or ""),
            "targetUrl": target_url,
            "candidates": payload_candidates,
            "next_instruction": (
                "Read the candidate skillMarkdown. If a skill is suitable, call"
                " spawn_browser_agent again with worker_contract.skill_id set to"
                " the chosen skill_id and worker_contract.skill_variables filled"
                " for the target row/page — or, for a batch of detail URLs,"
                " worker_contract.skill_rows=[one dict per row using the skill's"
                " variables, filled from the upstream collection artifact]. When"
                " a validated upstream artifact already exactly identifies the"
                " phase slice, you may omit skill_rows and let the harness build"
                " them; explicit rows are enriched only after an exact validated"
                " identity-set match. The"
                " fast path runs the workflow once per row on one warm tab with"
                " zero LLM steps, so PREFER skill_rows over declining a matching"
                " single-detail skill for a batch. If none is suitable, call"
                " spawn_browser_agent again with worker_contract.skill_selection="
                "{\"use_skill\": false, \"reason\": \"...\","
                " \"considered_skill_ids\": [...]}. Never run a single-detail"
                " skill once over a whole batch; accept with skill_rows, split"
                " per row, or explicitly decline."
            ),
        }
        if logger is not None and hasattr(logger, "write"):
            try:
                logger.write(
                    "skill.selection.required",
                    {
                        "phaseId": request["phaseId"],
                        "candidateIds": [c["skill_id"] for c in payload_candidates],
                    },
                )
            except Exception:  # pragma: no cover
                pass
        return request
    except Exception as exc:
        if logger is not None and hasattr(logger, "write"):
            try:
                logger.write("skill.selection.error", {"error": str(exc)})
            except Exception:  # pragma: no cover
                pass
        return None


def selected_skill_context(
    registry: Any,
    worker_contract: Dict[str, Any],
    *,
    max_markdown_chars: int = 6000,
) -> str:
    """Context block for BrowserAgent fallback slow path.

    This is task context, not system prompt. If the fast path falls back, the
    worker can still follow the selected skill's recipe.
    """
    if registry is None or not isinstance(worker_contract, dict):
        return ""
    skill_id = _selection_skill_id(worker_contract)
    if not skill_id or skill_selection_declined(worker_contract):
        return ""
    try:
        skill = registry.get(skill_id)
    except Exception:
        skill = None
    if skill is None:
        return ""

    variables = worker_contract.get("skill_variables")
    variables = variables if isinstance(variables, dict) else {}
    # Guidance 层（07-07）：hints-only skill 定向注入 hints 小节（不整份灌
    # SKILL.md——正文的校准清单/版本史是噪音，6000 字预算要花在干货上）；
    # 带 hints 小节的 workflow skill 保持整份注入（兜底契约等仍是慢路径要
    # 读的菜谱），但同样附上探针协议。
    is_guidance = bool(getattr(skill, "is_hints_only", False))
    hints_section = ""
    protocol = ""
    try:
        from harness.skill.guidance import extract_hints_section, guidance_protocol_text
        hints_section = extract_hints_section(skill.skill_md)
        if is_guidance or hints_section:
            protocol = guidance_protocol_text()
    except Exception:  # pragma: no cover - guidance layer must never break spawn
        pass
    # A blocked draft the user forced anyway: surface its generation-failure
    # record so the slow-path LLM knows the skill's known gaps and compensates
    # instead of rediscovering them (07-06 user requirement).
    quality_report: Dict[str, Any] = {}
    try:
        from harness.skill.registry import load_create_report
        report = load_create_report(getattr(skill, "directory", None))
        if str(report.get("status") or "") in (
            "draft_blocked", "revision_blocked", "recheck_failed",
        ):
            quality_report = {
                "status": report.get("status"),
                "known_gaps": report.get("failure_human") or report.get("failed_checks") or [],
                "guidance": (
                    "该 skill 质量门未过：其 workflow 无法自行产出上述字段。"
                    "慢路径执行时必须自行补齐这些字段的来源（页面提取或行级输入），"
                    "不要假设 skill 快路径已经覆盖它们。"
                ),
            }
    except Exception:  # pragma: no cover - context enrichment must never break spawn
        pass
    block = {
        "skill_id": skill.skill_id,
        "kind": "guidance" if is_guidance else "workflow",
        "sourcePath": str(skill.directory / "SKILL.md") if skill.directory else "",
        "frontmatter": {
            "domain": skill.domain,
            "task_type": skill.task_type,
            "stage_hint": skill.stage_hint,
            "fields": sorted(skill.fields),
            "description": skill.description,
            "allow_auto_captcha": bool(skill.frontmatter.get("allow_auto_captcha", False)),
        },
        "skill_variables": variables,
        "workflowVariables": skill.variable_template,
        "rowContract": skill.row_contract,
        **({"create_quality_report": quality_report} if quality_report else {}),
        **({"guidance_protocol": protocol} if protocol else {}),
        "fallback_guidance": (
            "This is a GUIDANCE skill: it has NO workflow fast path — you (the"
            " worker) perform the task yourself. The hints in skill_markdown are"
            " shortcuts distilled from past successful runs on this site; apply"
            " them under the guidance_protocol (probe first, discard on"
            " mismatch, report guidance_stale)."
        ) if is_guidance else (
            "If the zero-LLM skill fast path did not complete, execute the frozen"
            " selected recipe with the execute_selected_skill harness tool."
            " Do NOT search for workflow.json, copy steps into browser_call, or"
            " reconstruct the workflow from this markdown. Supply only live"
            " page/fleet handles plus variables or rows allowed by rowContract;"
            " then persist accepted rows with record_extraction. Derive missing"
            " values only from worker input, page evidence, or validated"
            " record_extraction artifacts."
        ),
    }
    markdown = (hints_section if is_guidance and hints_section
                else str(skill.skill_md or ""))
    if len(markdown) > max_markdown_chars:
        markdown = markdown[:max_markdown_chars] + "\n...[truncated]"
    return (
        "<selected_skill>\n"
        f"{json.dumps(block, ensure_ascii=False, indent=2, default=str)}\n\n"
        "<skill_markdown>\n"
        f"{markdown}\n"
        "</skill_markdown>\n"
        "</selected_skill>"
    )


def build_known_skills_digest(registry: Any, *, max_skills: int = 12) -> str:
    """Planning-time digest of reusable skills for the LeadAgent system prompt.

    The granularity mismatch (batch phases vs single-detail skills) is decided at
    PLAN time — by spawn time the phases are already shaped and the Lead can only
    decline or replan. This digest lets the planner shape detail phases at skill
    granularity up front: 1 row per phase, or a batch phase carrying skill_rows.
    Data only — the usage rule lives in the Lead prompt text. Empty string when
    there is nothing to say (no registry / no skills / any error). Drafts are
    omitted: the planner must not shape phases around an uncalibrated scaffold
    (the user can still force one via /skill <id>)."""
    if registry is None:
        return ""
    try:
        skills = [s for s in registry.all() if not getattr(s, "is_draft", False)]
    except Exception:
        return ""
    if not skills:
        return ""
    try:
        from harness.skill.dispatch import _referenced_vars, _url_variable
    except Exception:  # pragma: no cover
        return ""
    entries: List[Dict[str, Any]] = []
    for skill in skills[:max_skills]:
        try:
            url_var = _url_variable(skill)
            is_guidance = bool(getattr(skill, "is_hints_only", False))
            entries.append({
                "skill_id": skill.skill_id,
                # guidance skill 不自产 artifact：worker 仍是执行主体，skill 只
                # 提供 hints——planner 不能把它当零 LLM 快路径去塑形 phase。
                "kind": "guidance" if is_guidance else "workflow",
                "domain": skill.domain,
                "task_type": skill.task_type,
                "stage_hint": skill.stage_hint,
                "fields": sorted(skill.fields),
                "input_variables": sorted(_referenced_vars(skill)),
                "row_variables": sorted(skill.variable_template.keys()),
                "row_contract": skill.row_contract,
                "granularity": (
                    "advisory hints; the worker performs the task itself (no"
                    " workflow fast path)" if is_guidance
                    else f"one {url_var or 'target'} per workflow run"
                ),
                "description": str(skill.description or "").strip(),
            })
        except Exception:  # one malformed skill must not sink the digest
            continue
    if not entries:
        return ""
    return (
        "<known_skills>\n"
        + json.dumps(entries, ensure_ascii=False, indent=2, default=str)
        + "\n</known_skills>"
    )


def _skill_candidate_payload(
    skill: Any,
    *,
    score: int,
    reasons: List[str],
    variables: Dict[str, Any],
    required_filled: bool,
) -> Dict[str, Any]:
    steps = []
    for step in list(getattr(skill, "steps", []) or [])[:8]:
        if not isinstance(step, dict):
            continue
        steps.append({
            "action": step.get("action"),
            "purpose": step.get("purpose"),
            "extract": step.get("extract"),
        })
    markdown = str(getattr(skill, "skill_md", "") or "")
    return {
        "skill_id": skill.skill_id,
        "kind": "guidance" if getattr(skill, "is_hints_only", False) else "workflow",
        "score": score,
        "reasons": reasons,
        "sourcePath": str(skill.directory / "SKILL.md") if skill.directory else "",
        "frontmatter": {
            "domain": skill.domain,
            "task_type": skill.task_type,
            "stage_hint": skill.stage_hint,
            "fields": sorted(skill.fields),
            "description": skill.description,
            "allow_auto_captcha": bool(skill.frontmatter.get("allow_auto_captcha", False)),
        },
        "workflowVariables": skill.variable_template,
        "rowContract": skill.row_contract,
        "derivedVariables": variables,
        "requiredVariablesFilled": required_filled,
        "workflowStepSummary": steps,
        "skillMarkdown": markdown[:12000] + ("\n...[truncated]" if len(markdown) > 12000 else ""),
    }


def _host_of_url(url: str) -> str:
    text = str(url or "")
    if "://" not in text:
        return ""
    host = text.split("://", 1)[1].split("/", 1)[0]
    return host.lower().lstrip("www.")
