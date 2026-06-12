"""
harness.strategy_bank - Strategy guidance for common browser tasks.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from datetime import datetime
from typing import Any, List, Optional, Tuple
from urllib.parse import urlparse

from harness.utils import JsonDict, trim_large_strings


def resolve_strategy_bank_path(raw_path: str) -> Path:
    path = Path(raw_path or "strategy_bank/strategy_bank.json").expanduser()
    if not path.is_absolute():
        path = Path.cwd() / path
    return path.resolve(strict=False)


def learned_strategy_bank_path(raw_path: str) -> Path:
    path = resolve_strategy_bank_path(raw_path)
    return (path.parent / "learned_strategies.json").resolve(strict=False)


def load_strategy_bank(raw_path: str) -> JsonDict:
    path = resolve_strategy_bank_path(raw_path)
    if not path.exists():
        data: JsonDict = {"version": 1, "strategies": [], "path": str(path)}
        return _attach_learned_strategies(data, path)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        data = {
            "version": 1,
            "strategies": [],
            "path": str(path),
            "load_error": str(exc),
        }
        return _attach_learned_strategies(data, path)
    if not isinstance(data, dict):
        data = {
            "version": 1,
            "strategies": [],
            "path": str(path),
            "load_error": "strategy bank root must be an object",
        }
        return _attach_learned_strategies(data, path)
    data.setdefault("version", 1)
    data.setdefault("strategies", [])
    data["path"] = str(path)
    return _attach_learned_strategies(data, path)


def record_learned_strategy(
    raw_path: str,
    *,
    worker_contract: JsonDict,
    result: JsonDict,
    phase: Optional[JsonDict] = None,
    logger: Optional[Any] = None,
) -> Optional[JsonDict]:
    validation = (
        result.get("artifactValidation")
        if isinstance(result.get("artifactValidation"), dict)
        else {}
    )
    if validation.get("status") != "done":
        return None
    status = str(result.get("status") or "").strip()
    status_category = str(result.get("statusCategory") or "").strip()
    blocked_statuses = {
        "blocked_by_challenge",
        "hitl_required",
        "hitl_waiting",
        "hitl_timeout",
        "page_settled_after_hitl",
        "stale_pause_deadlock",
        "browser_api_contract_error",
        "context_limit_exceeded",
        "page_crashed",
        "failed",
        "cancelled",
    }
    if status in blocked_statuses or status_category in {"needs_human", "fatal"}:
        return None
    phase = phase or {}
    task_type = str(
        worker_contract.get("task_type")
        or phase.get("task_type")
        or "web_scrape"
    )
    stage = str(
        worker_contract.get("stage_hint")
        or phase.get("stage_hint")
        or "generic"
    )
    domains, entry_urls = _extract_success_domains_and_urls(
        worker_contract=worker_contract,
        result=result,
        phase=phase,
        logger=logger,
    )
    if not domains:
        return None
    expected = (
        worker_contract.get("expected_artifact")
        if isinstance(worker_contract.get("expected_artifact"), dict)
        else {}
    )
    expected_name = str(expected.get("name") or "").strip()
    seed = "|".join([",".join(domains), task_type, stage, expected_name])
    digest = hashlib.sha256(seed.encode("utf-8", errors="replace")).hexdigest()[:10]
    strategy_id = f"learned.{domains[0]}.{stage}.{digest}"
    now = datetime.utcnow().isoformat(timespec="seconds") + "Z"
    methods = []
    trace_summary = result.get("traceSummary")
    if isinstance(trace_summary, dict) and isinstance(trace_summary.get("methods"), dict):
        methods = [
            method for method, count in trace_summary.get("methods", {}).items()
            if count and method != "final_answer"
        ][:8]
    if "record_extraction" not in methods:
        methods.append("record_extraction")
    strategy: JsonDict = {
        "id": strategy_id,
        "learned": True,
        "task_types": [task_type],
        "stage": stage,
        "applies_to_stages": [stage],
        "fallback": False,
        "cross_cutting": False,
        "site_domains": domains[:5],
        "site_entry_urls": entry_urls[:5],
        "phase_keywords": _learned_phase_keywords(worker_contract, phase, domains),
        "applies_when": [
            "A future phase targets the same site/domain and stage.",
            "The current task can reuse the validated entry points, page flow, or extraction shape from this successful attempt.",
        ],
        "preferred_tools": methods,
        "cautioned_tools": [],
        "avoid_tools": [],
        "procedure": _learned_procedure(stage, expected_name, entry_urls),
        "success_criteria": [
            "record_extraction row keys match the worker contract exactly",
            "validated artifacts satisfy the phase validators",
            "source URLs and provenance are persisted for handoff",
        ],
        "failure_signatures": [
            "same-domain task ignores the learned successful entry point",
            "re-scrapes already validated rows instead of continuing from trusted artifacts",
            "creates repeated fresh pages where same-tab navigation would reuse the working session",
        ],
        "stats": {"success_count": 1, "fail_count": 0},
        "first_seen_at": now,
        "last_seen_at": now,
    }
    path = learned_strategy_bank_path(raw_path)
    bank = _load_learned_file(path)
    strategies = bank.setdefault("strategies", [])
    if not isinstance(strategies, list):
        strategies = []
        bank["strategies"] = strategies
    existing = None
    for item in strategies:
        if isinstance(item, dict) and item.get("id") == strategy_id:
            existing = item
            break
    if existing is None:
        strategies.append(strategy)
        saved = strategy
    else:
        existing.update({
            "site_domains": _unique_strings([*(existing.get("site_domains") or []), *strategy["site_domains"]]),
            "site_entry_urls": _unique_strings([*(existing.get("site_entry_urls") or []), *strategy["site_entry_urls"]])[:10],
            "phase_keywords": _unique_strings([*(existing.get("phase_keywords") or []), *strategy["phase_keywords"]])[:20],
            "preferred_tools": _unique_strings([*(existing.get("preferred_tools") or []), *strategy["preferred_tools"]])[:12],
            "procedure": strategy["procedure"],
            "last_seen_at": now,
        })
        stats = existing.setdefault("stats", {})
        if isinstance(stats, dict):
            stats["success_count"] = int(stats.get("success_count") or 0) + 1
        saved = existing
    bank["version"] = 1
    bank["updated_at"] = now
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(bank, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        return None
    return trim_large_strings(saved, 2000)


def _attach_learned_strategies(data: JsonDict, path: Path) -> JsonDict:
    learned_path = (path.parent / "learned_strategies.json").resolve(strict=False)
    learned = _load_learned_file(learned_path)
    learned_strategies = (
        learned.get("strategies")
        if isinstance(learned.get("strategies"), list)
        else []
    )
    base_strategies = (
        data.get("strategies")
        if isinstance(data.get("strategies"), list)
        else []
    )
    data["learned_strategy_path"] = str(learned_path)
    data["learned_strategies"] = learned_strategies
    data["strategies"] = _merge_strategy_lists(base_strategies, learned_strategies)
    if learned.get("load_error"):
        data["learned_load_error"] = learned.get("load_error")
    return data


def _load_learned_file(path: Path) -> JsonDict:
    if not path.exists():
        return {"version": 1, "strategies": []}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return {"version": 1, "strategies": [], "load_error": str(exc)}
    if not isinstance(data, dict):
        return {
            "version": 1,
            "strategies": [],
            "load_error": "learned strategy bank root must be an object",
        }
    data.setdefault("version", 1)
    data.setdefault("strategies", [])
    return data


def _merge_strategy_lists(base: List[Any], learned: List[Any]) -> List[JsonDict]:
    out: List[JsonDict] = []
    seen = set()
    for item in [*base, *learned]:
        if not isinstance(item, dict):
            continue
        strategy_id = str(item.get("id") or "").strip()
        if strategy_id and strategy_id in seen:
            continue
        if strategy_id:
            seen.add(strategy_id)
        out.append(item)
    return out


def _extract_success_domains_and_urls(
    *,
    worker_contract: JsonDict,
    result: JsonDict,
    phase: JsonDict,
    logger: Optional[Any],
) -> Tuple[List[str], List[str]]:
    urls: List[str] = []
    for value in (worker_contract, phase, result.get("answer")):
        urls.extend(_urls_from_value(value))
    validation = (
        result.get("artifactValidation")
        if isinstance(result.get("artifactValidation"), dict)
        else {}
    )
    artifact_paths = validation.get("artifacts")
    if not isinstance(artifact_paths, list):
        artifact_paths = []
    task_dir = getattr(logger, "task_dir", None)
    for raw_path in artifact_paths[:5]:
        payload = _load_artifact_payload(raw_path, task_dir)
        if not payload:
            continue
        urls.extend(_urls_from_value(payload.get("pageUrl")))
        rows = payload.get("rows")
        if isinstance(rows, list):
            for row in rows[:50]:
                if isinstance(row, dict):
                    for key in ("pageUrl", "url", "href", "detailUrl", "productUrl"):
                        urls.extend(_urls_from_value(row.get(key)))
    entry_urls = _unique_strings(
        url for url in urls
        if _domain_from_url(url)
    )
    domains = _unique_strings(
        _domain_from_url(url)
        for url in entry_urls
        if _domain_from_url(url)
    )
    return domains, entry_urls


def _learned_phase_keywords(
    worker_contract: JsonDict,
    phase: JsonDict,
    domains: List[str],
) -> List[str]:
    values = [
        worker_contract.get("phase_id"),
        worker_contract.get("objective"),
        worker_contract.get("stage_hint"),
        phase.get("objective"),
        phase.get("worker_task"),
        *domains,
    ]
    keywords: List[str] = []
    for value in values:
        text = str(value or "").strip()
        if not text:
            continue
        keywords.append(text)
    return _unique_strings(keywords)[:20]


def _learned_procedure(stage: str, expected_name: str, entry_urls: List[str]) -> List[str]:
    procedure: List[str] = []
    if entry_urls:
        procedure.append(
            "Start from the validated same-site entry URL when the task scope matches: "
            + entry_urls[0]
        )
    if stage == "detail_sections":
        procedure.append(
            "Prefer one browser page and Page.navigate/Page.getState between detail URLs instead of opening a fresh Page.create for every item."
        )
    elif stage == "collection":
        procedure.append(
            "Use the previously validated listing/leaderboard surface before broad search or alternate pages."
        )
    else:
        procedure.append(
            "Reuse the previously validated same-site flow before exploring a new surface."
        )
    if expected_name:
        procedure.append(
            f"Persist the handoff as record_extraction name {expected_name!r} with exact contract fields."
        )
    else:
        procedure.append(
            "Persist the handoff with record_extraction using exact contract fields."
        )
    procedure.append(
        "Include pageUrl, sourceTool, sourceSelectorOrAxId, and canonical <field>EvidenceText provenance for sensitive fields."
    )
    return procedure


def _load_artifact_payload(raw_path: Any, task_dir: Optional[Any]) -> JsonDict:
    try:
        path = Path(str(raw_path or ""))
        if not path.is_absolute() and task_dir is not None:
            path = Path(task_dir) / path
        payload = json.loads(path.resolve(strict=False).read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


URL_RE = re.compile(r"https?://[^\s\"'<>]+")


def _urls_from_value(value: Any) -> List[str]:
    if isinstance(value, str):
        return URL_RE.findall(value)
    if isinstance(value, dict):
        urls: List[str] = []
        for item in value.values():
            urls.extend(_urls_from_value(item))
        return urls
    if isinstance(value, list):
        urls: List[str] = []
        for item in value:
            urls.extend(_urls_from_value(item))
        return urls
    return []


def _domain_from_url(url: str) -> str:
    host = urlparse(str(url or "")).netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    return host


def _unique_strings(values: Any) -> List[str]:
    out: List[str] = []
    seen = set()
    for value in values:
        text = str(value or "").strip()
        if text and text not in seen:
            out.append(text)
            seen.add(text)
    return out


def compact_strategy_bank(bank: JsonDict, *, max_strategies: int = 8) -> JsonDict:
    strategies = bank.get("strategies") if isinstance(bank.get("strategies"), list) else []
    compact: List[JsonDict] = []
    for item in strategies[:max_strategies]:
        if not isinstance(item, dict):
            continue
        compact.append({
            "id": item.get("id"),
            "task_types": item.get("task_types") or [],
            "stage": item.get("stage"),
            "applies_to_stages": item.get("applies_to_stages") or [],
            "fallback": bool(item.get("fallback", False)),
            "cross_cutting": bool(item.get("cross_cutting", False)),
            "learned": bool(item.get("learned", False)),
            "site_domains": item.get("site_domains") or [],
            "site_entry_urls": item.get("site_entry_urls") or [],
            "applies_when": item.get("applies_when") or [],
            "preferred_tools": item.get("preferred_tools") or [],
            "cautioned_tools": item.get("cautioned_tools") or [],
            "avoid_tools": item.get("avoid_tools") or [],
            "procedure": item.get("procedure") or [],
            "success_criteria": item.get("success_criteria") or [],
            "failure_signatures": item.get("failure_signatures") or [],
        })
    payload: JsonDict = {
        "version": bank.get("version", 1),
        "path": bank.get("path"),
        "strategies": compact,
    }
    if bank.get("load_error"):
        payload["load_error"] = bank.get("load_error")
    return trim_large_strings(payload, 2000)


def _keyword_hits(text: str, values: Any) -> int:
    if not isinstance(values, list):
        return 0
    lowered = text.lower()
    return sum(1 for value in values if str(value or "").lower() in lowered)


def _site_hits(text: str, item: JsonDict) -> int:
    domains = item.get("site_domains")
    if not isinstance(domains, list) or not domains:
        return 0
    lowered = text.lower()
    return sum(1 for domain in domains if str(domain or "").lower() in lowered)


def _task_type_matches(task_type: Optional[str], item: JsonDict) -> bool:
    task_types = item.get("task_types") if isinstance(item.get("task_types"), list) else []
    if not task_type or not task_types:
        return True
    return str(task_type) in {str(value) for value in task_types}


def _stage_matches(stage_hint: str, item: JsonDict) -> bool:
    stages = item.get("applies_to_stages")
    if not isinstance(stages, list) or not stages:
        stage = str(item.get("stage") or "").strip()
        stages = [stage] if stage else []
    return stage_hint in {str(value).strip() for value in stages}


def _render_strategy(item: JsonDict) -> JsonDict:
    return {
        "id": item.get("id"),
        "stage": item.get("stage"),
        "applies_to_stages": item.get("applies_to_stages") or [],
        "fallback": bool(item.get("fallback", False)),
        "cross_cutting": bool(item.get("cross_cutting", False)),
        "learned": bool(item.get("learned", False)),
        "site_domains": item.get("site_domains") or [],
        "site_entry_urls": item.get("site_entry_urls") or [],
        "preferred_tools": item.get("preferred_tools") or [],
        "cautioned_tools": item.get("cautioned_tools") or [],
        "avoid_tools": item.get("avoid_tools") or [],
        "procedure": item.get("procedure") or [],
        "success_criteria": item.get("success_criteria") or [],
        "failure_signatures": item.get("failure_signatures") or [],
    }


def select_strategies_for_phase(
    bank: JsonDict,
    *,
    task_type: Optional[str],
    phase: JsonDict,
    limit: int = 3,
) -> List[JsonDict]:
    strategies = bank.get("strategies") if isinstance(bank.get("strategies"), list) else []
    stage_hint = str(phase.get("stage_hint") or "").strip()
    phase_text = " ".join(
        str(phase.get(key) or "")
        for key in ("id", "objective", "worker_task", "type", "stage_hint_reason")
    )
    selected: List[Tuple[int, JsonDict]] = []
    fallback: List[Tuple[int, JsonDict]] = []
    for item in strategies:
        if not isinstance(item, dict):
            continue
        if not _task_type_matches(task_type, item):
            continue
        site_score = _site_hits(phase_text, item)
        if item.get("learned") and item.get("site_domains") and site_score <= 0:
            continue
        rendered = _render_strategy(item)
        stage_match = bool(stage_hint and _stage_matches(stage_hint, item))
        keyword_score = _keyword_hits(phase_text, item.get("phase_keywords"))
        failure_score = _keyword_hits(phase_text, item.get("failure_signatures"))
        score = keyword_score + failure_score + (site_score * 3)
        if bool(item.get("fallback", False)):
            item_stage = str(item.get("stage") or "").strip()
            cross_cutting = bool(item.get("cross_cutting", False))
            include_fallback = (
                score > 0
                or (bool(stage_hint) and stage_hint == item_stage)
                or (stage_match and not cross_cutting)
            )
            if include_fallback:
                fallback.append((score, rendered))
            continue
        if stage_match:
            selected.append((score, rendered))
    if not selected:
        fallback.sort(key=lambda item: item[0], reverse=True)
        return [rendered for _score, rendered in fallback[:limit]]
    selected.sort(key=lambda item: item[0], reverse=True)
    positive = [(score, rendered) for score, rendered in selected if score > 0]
    if positive:
        max_score = positive[0][0]
        close_matches = [
            rendered for score, rendered in positive
            if score >= max_score - 1
        ]
    else:
        close_matches = [rendered for _score, rendered in selected[:limit]]
    remaining = max(0, limit - len(close_matches))
    if remaining:
        fallback.sort(key=lambda item: item[0], reverse=True)
        close_matches.extend(rendered for _score, rendered in fallback[:remaining])
    return close_matches[:limit]


def render_strategy_guidance(strategies: List[JsonDict]) -> str:
    if not strategies:
        return ""
    return (
        "<strategy_bank_guidance>\n"
        + json.dumps(strategies, ensure_ascii=False, indent=2, default=str)
        + "\n</strategy_bank_guidance>"
    )
