"""Shared artifact validator taxonomy and row-hygiene helpers.

This module contains production validation primitives used by task control and
record_extraction.  It deliberately has no observational completeness ledger,
shadow mode, terminal projection, or model-facing receipt.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Dict, List, Optional

from harness.evidence.extraction_artifacts import field_name_from_spec, field_names_from_specs
from harness.utils import JsonDict


VALIDATOR_SCOPE: Dict[str, str] = {
    "artifact_required": "meta",
    "required_fields": "row",
    "field_nonempty": "row",
    "field_pattern": "row",
    "field_provenance": "row",
    "url_pattern": "row",
    "allowed_domain": "row",
    "cross_field_contains": "row",
    "action_outcome": "row",
    "range": "row",
    "min_rows": "aggregate",
    "max_rows": "aggregate",
    "exact_rows": "aggregate",
    "unique": "aggregate",
    "set_equals": "aggregate",
    "download_completed": "file",
    "file_integrity": "file",
    "upload_selected": "file",
    "upload_confirmed": "file",
    "image_exported": "file",
}


def _types_with_scope(scope: str) -> frozenset:
    return frozenset(
        name for name, value in VALIDATOR_SCOPE.items() if value == scope
    )


VALIDATOR_TYPES = frozenset(VALIDATOR_SCOPE)
FILE_VALIDATOR_TYPES = _types_with_scope("file")


DIAGNOSTIC_FAILURE_LIMIT = 20


def _capped(items: List[JsonDict], limit: Optional[int]) -> List[JsonDict]:
    return items if limit is None else items[:limit]


_CONTROL_FIELD_NAMES = frozenset({
    "blocker", "blockers", "status", "reason", "error", "errors",
    "collectionstatus", "extractionstatus", "gatetype", "gateevidence",
    "authrequired", "authenticationrequired", "loginrequired",
    "requireslogin", "requiresauth", "authsurface", "loginsurface",
    "authevidence", "loginevidence", "nextphaserequireshitl",
})
_STRUCTURED_BLOCKER_STATUS_RE = re.compile(
    r"^(?:login|signin|auth|authentication|captcha|challenge|"
    r"human_verification|verification|access)[_-]"
    r"(?:blocked|required|gated|failed|unavailable)$",
    re.IGNORECASE,
)
_BLOCKER_TEMPLATE_PATTERN = (
    r"(?:(?:login|sign[ -]?in|auth(?:entication)?|captcha|challenge|"
    r"human verification|verification|access)"
    r"(?:\s+(?:wall|gate|check|challenge|verification))?"
    r"|(?:登录|登陆)(?:墙|门禁)?|验证码|人机验证|身份验证|访问验证|风控)"
    r"\s*(?:was\s+|is\s+|已)?(?:blocked|prevented|denied|stopped|"
    r"unavailable|阻止|拦截|未通过|导致无法)"
)
_BLOCKER_TEMPLATE_RE = re.compile(
    rf"^\s*{_BLOCKER_TEMPLATE_PATTERN}",
    re.IGNORECASE,
)
_BLOCKER_TEMPLATE_SEARCH_RE = re.compile(
    _BLOCKER_TEMPLATE_PATTERN,
    re.IGNORECASE,
)
_PLACEHOLDER_LITERAL_RE = re.compile(
    r"(?:['\"]\s*(?:n/?a|unknown|unavailable|null|none)\s*['\"]"
    r"|\b(?:login|auth|captcha|challenge)[_-](?:blocked|required|failed)\b)",
    re.IGNORECASE,
)


PLACEHOLDER_TEXT_PATTERNS = tuple(
    re.compile(pattern, re.I)
    for pattern in (
        r"^\s*loading(?:\.\.\.)?\s*$",
        r"^\s*(?:<\s*)?placeholder(?:\s*>)?\s*$",
        r"\bplaceholder\b",
        r"^\s*be the first to\b.*$",
        r"^\s*sign in to (?:view|continue|see)\b.*$",
        r"^\s*ask a question\s*$",
        r"^\s*no data(?: available)?\s*$",
        r"^\s*no reviews? yet\s*$",
        r"^\s*no comments? yet\s*$",
        r"^\s*coming soon\s*$",
        r"^\s*nothing (?:here|found)\s*$",
        r"^\s*(?:n/?a|none|null|nil|unknown|unavailable|not (?:found|available|provided|specified|shown|displayed|captured|obtained|extracted))\s*\.?\s*$",
        r"(?:located in|present in|inside|within)\s+(?:an?\s+)?iframe",
        r"位于\s*iframe|iframe\s*(?:中|内|里)|嵌(?:套|入)在?\s*iframe",
        r"主\s*dom\s*(?:未|中未|没有|不包含)|not (?:directly )?(?:in|present in|contained in) the (?:main )?dom",
        r"^\s*(?:未(?:获取|提供|找到|明确|展示|提取|包含|显示|抓取|呈现)|无法(?:获取|提取|访问|抓取|读取)|暂无(?:数据|内容|信息)?|无(?:数据|内容|此信息|相关信息)|未知|不适用|页面(?:未|没有)(?:明确|直接|展示))",
    )
)


def _normalized_semantic_token(value: Any) -> str:
    return re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "", str(value or "").lower())


def _business_fields_from_expected(expected: JsonDict) -> List[str]:
    fields = field_names_from_specs(expected.get("fields") or [])
    for field in field_names_from_specs(expected.get("required_fields") or []):
        if field not in fields:
            fields.append(field)
    out: List[str] = []
    for field in fields:
        normalized = _normalized_semantic_token(field)
        if not normalized:
            continue
        if normalized in _CONTROL_FIELD_NAMES:
            continue
        if normalized.endswith("evidence") or normalized.endswith("evidencetext"):
            continue
        if normalized in {"sourcetool", "sourceselectororaxid", "pageurl"}:
            continue
        out.append(field)
    return out


def _has_field_specific_evidence(row: JsonDict, field: str) -> bool:
    field_token = _normalized_semantic_token(field)
    wanted = {f"{field_token}evidence", f"{field_token}evidencetext"}
    for key, value in row.items():
        if _normalized_semantic_token(key) not in wanted:
            continue
        if isinstance(value, str) and value.strip():
            return True
        if isinstance(value, (list, dict)) and value:
            return True
    return False


def _expected_array_fields(expected_artifact: JsonDict) -> List[str]:
    raw_fields = expected_artifact.get("fields")
    if not isinstance(raw_fields, list):
        return []
    out: List[str] = []
    for field in raw_fields:
        if not isinstance(field, dict):
            continue
        name = field_name_from_spec(field)
        field_type = str(field.get("type") or "").strip().lower()
        if name and field_type == "array":
            out.append(name)
    return out


def _row_has_blocker_explanation(row: JsonDict) -> bool:
    for key in (
        "_note",
        "note",
        "notes",
        "blocker",
        "blockers",
        "status",
        "reason",
        "absence_reason",
        "missing_reason",
    ):
        value = row.get(key)
        if isinstance(value, str) and value.strip():
            return True
        if isinstance(value, list) and value:
            return True
        if isinstance(value, dict) and value:
            return True
    absence_markers = (
        "absent",
        "empty",
        "missing",
        "no ",
        "none",
        "not visible",
        "not shown",
        "unavailable",
        "未显示",
        "没有",
        "无",
    )
    for key, value in row.items():
        if not str(key).endswith("EvidenceText"):
            continue
        text = str(value or "").strip().lower()
        if text and any(marker in text for marker in absence_markers):
            return True
    return False


def _row_self_reports_placeholder(row: JsonDict) -> bool:
    for key in (
        "placeholderDetected",
        "placeholder_detected",
        "isPlaceholder",
        "is_placeholder",
        "dataPlaceholder",
        "data_placeholder",
    ):
        if row.get(key) is True:
            return True
        value = row.get(key)
        if isinstance(value, str) and value.strip().lower() in {"true", "yes", "1"}:
            return True
    return False


def _placeholder_pattern(text: str) -> str:
    for pattern in PLACEHOLDER_TEXT_PATTERNS:
        if pattern.search(text):
            return pattern.pattern
    return ""


def detect_blocker_data_rows(
    rows: List[JsonDict],
    expected_artifact: Optional[JsonDict],
    *,
    limit: Optional[int] = DIAGNOSTIC_FAILURE_LIMIT,
) -> List[JsonDict]:
    """Reject control-plane failure explanations embedded as business data."""
    expected = expected_artifact if isinstance(expected_artifact, dict) else {}
    business_fields = _business_fields_from_expected(expected)
    if not business_fields:
        return []
    failures: List[JsonDict] = []
    for index, row in enumerate(rows):
        matched: List[JsonDict] = []
        for field in business_fields:
            value = row.get(field)
            if isinstance(value, str):
                text = value.strip()
                if text and _STRUCTURED_BLOCKER_STATUS_RE.fullmatch(text):
                    matched.append({
                        "field": field,
                        "value": text[:120],
                        "reason": "structured_blocker_status_in_business_field",
                    })
                continue
            if not isinstance(value, list):
                continue
            texts = [
                item.strip() for item in value
                if isinstance(item, str) and item.strip()
            ]
            exact_tokens = [
                text for text in texts
                if _STRUCTURED_BLOCKER_STATUS_RE.fullmatch(text)
            ]
            if exact_tokens:
                matched.append({
                    "field": field,
                    "value": exact_tokens[0][:120],
                    "reason": "structured_blocker_status_in_business_array",
                })
                continue
            has_other_meaningful_content = any(
                not isinstance(item, str)
                and item is not None
                and bool(item)
                for item in value
            )
            all_text_is_blocker = bool(texts) and all(
                _BLOCKER_TEMPLATE_RE.search(text) is not None
                for text in texts
            )
            if (
                all_text_is_blocker
                and not has_other_meaningful_content
                and not _has_field_specific_evidence(row, field)
            ):
                matched.append({
                    "field": field,
                    "value": " | ".join(texts[:2])[:120],
                    "reason": "blocker_only_array_without_field_evidence",
                })
        if matched:
            failures.append({
                "type": "data_placeholder",
                "row": index,
                "reason": "blocker_as_business_data",
                "fields": matched[:5],
            })
    return _capped(failures, limit)


def detect_placeholder_rows(
    rows: List[JsonDict],
    *,
    limit: Optional[int] = DIAGNOSTIC_FAILURE_LIMIT,
) -> List[JsonDict]:
    bad: List[JsonDict] = []
    for index, row in enumerate(rows):
        if _row_self_reports_placeholder(row):
            bad.append({
                "type": "data_placeholder",
                "row": index,
                "reason": "row_self_reported_placeholder",
            })
            continue
        matched_fields: List[JsonDict] = []
        for field, value in row.items():
            if not isinstance(value, str):
                continue
            text = value.strip()
            if not text or len(text) > 120:
                continue
            pattern = _placeholder_pattern(text)
            if pattern:
                matched_fields.append({
                    "field": str(field),
                    "value": text[:120],
                    "pattern": pattern,
                })
        if matched_fields:
            bad.append({
                "type": "data_placeholder",
                "row": index,
                "fields": matched_fields[:5],
            })
    return _capped(bad, limit)


def detect_stub_rows(
    rows: List[JsonDict],
    expected_artifact: JsonDict,
    *,
    limit: Optional[int] = DIAGNOSTIC_FAILURE_LIMIT,
) -> List[JsonDict]:
    array_fields = _expected_array_fields(expected_artifact)
    if not array_fields:
        return []
    bad: List[JsonDict] = []
    for index, row in enumerate(rows):
        present = [field for field in array_fields if field in row]
        if not present:
            continue
        empty = [
            field for field in present
            if isinstance(row.get(field), list) and len(row.get(field) or []) == 0
        ]
        if len(empty) < max(2, len(present)):
            continue
        if _row_has_blocker_explanation(row):
            continue
        bad.append({
            "type": "data_stub",
            "row": index,
            "emptyArrayFields": empty,
            "reason": "detail-like row has empty arrays without blocker or absence note",
        })
    return _capped(bad, limit)


def detect_near_stub_rows(
    rows: List[JsonDict],
    expected_artifact: JsonDict,
    *,
    limit: Optional[int] = DIAGNOSTIC_FAILURE_LIMIT,
) -> List[JsonDict]:
    array_fields = _expected_array_fields(expected_artifact)
    if len(array_fields) < 3:
        return []
    warnings: List[JsonDict] = []
    for index, row in enumerate(rows):
        present = [field for field in array_fields if field in row]
        if len(present) < 3:
            continue
        empty = [
            field for field in present
            if isinstance(row.get(field), list) and len(row.get(field) or []) == 0
        ]
        if len(empty) < max(2, len(present) - 1):
            continue
        if len(empty) >= len(present):
            continue
        if _row_has_blocker_explanation(row):
            continue
        warnings.append({
            "type": "near_stub_row",
            "row": index,
            "emptyArrayFields": empty,
            "nonEmptyArrayFields": [
                field for field in present if field not in empty
            ],
            "reason": (
                "most detail array fields are empty; verify this is real page"
                " absence, not padding"
            ),
        })
    return _capped(warnings, limit)


def cumulative_row_key(row: JsonDict, expected: JsonDict) -> str:
    fields = field_names_from_specs(
        expected.get("required_fields") or expected.get("fields") or []
    )
    priority_fields = [
        "url",
        "href",
        "detailUrl",
        "detail_url",
        "productUrl",
        "product_url",
        "rank",
        "position",
        "name",
        "title",
    ]
    for field in [*priority_fields, *fields]:
        value = row.get(field)
        text = str(value or "").strip()
        if text:
            return f"{field}:{text.lower()}"
    canonical = json.dumps(
        row,
        ensure_ascii=False,
        sort_keys=True,
        default=str,
        separators=(",", ":"),
    )
    return "rowhash:" + hashlib.sha256(
        canonical.encode("utf-8", errors="replace")
    ).hexdigest()[:16]
