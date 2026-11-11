"""Objective call evidence, independent of script and business success."""
from collections import Counter


def call_facts(data):
    cached = data.get("callFacts")
    records = data.get("results")
    if not isinstance(records, list):
        facts = dict(cached) if isinstance(cached, dict) else {
            "recordedCalls": 0, "totalCalls": None, "complete": False,
            "failedCalls": None, "failureGroups": []}
        facts.setdefault("metadataComplete", facts.get("complete", False))
        facts.setdefault("omittedValueCount", None)
        facts.setdefault("valuesComplete", None)
        return facts
    delivery = data.get("delivery") if isinstance(data.get("delivery"), dict) else {}
    reference = delivery.get("reference") if isinstance(delivery.get("reference"), dict) else {}
    total = (data.get("callRecordsReference") or {}).get("total", reference.get("callCount", len(records)))
    metadata_complete = total == len(records) and delivery.get("mode") not in {"reference", "unavailable"}
    omitted = sum(isinstance(record, dict) and record.get("valueOmittedReason") is not None for record in records)
    groups = Counter()
    for record in records:
        if not isinstance(record, dict) or record.get("status") != "failed":
            continue
        error = record.get("error") or {}
        error = error if isinstance(error, dict) else {}
        groups[(record.get("action") or record.get("kind") or "unknown",
                error.get("actionCode") or error.get("code") or "unknown")] += 1
    return {"recordedCalls": len(records), "totalCalls": total,
            "complete": metadata_complete, "metadataComplete": metadata_complete,
            "omittedValueCount": omitted, "valuesComplete": metadata_complete and omitted == 0,
            "failedCalls": sum(groups.values()),
            "failureGroups": [{"action": action, "code": code, "count": count}
                              for (action, code), count in groups.items()],
            "interpretation": "complete/metadataComplete cover call metadata, not retained values or business completion. "
                              "omittedValueCount counts available omission markers (a lower bound for partial records); "
                              "valuesComplete requires all call records and no omitted values. Pagination cannot "
                              "restore values discarded by the execution ledger quota. A handled failure may have recovered."}
