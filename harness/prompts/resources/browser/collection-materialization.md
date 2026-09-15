---
id: browser.collection-materialization
audience: browser
version: "2026-09-15"
description: Collect repeated page records with bounded evidence cycles and distinguish an incomplete surface from a confirmed absence.
sources:
  - harness/observation/content_completeness.py
  - harness/tools/browser_tools/composites/collect_items.py
  - harness/tools/browser_tools/record_extraction.py
emitter_sources:
  - harness/observation/content_completeness.py
  - harness/tools/browser_tools/record_extraction.py
related_tools:
  - browser_call
  - record_extraction
  - visual_verify
related_methods:
  - DOM.getAXTree
  - DOM.getText
  - DOM.getAttribute
  - Input.scroll
error_codes:
  - route_recovery_required
  - blocked_content_suppression
  - marker_declaration_suspect
  - repair_fallback_required
  - repair_contract_conflict
topics:
  - collection
  - enumeration
  - pagination
  - loading shell
  - absence
  - materialization
aliases:
  - 列表采集
  - 分页
  - 加载中
  - 数据没出来
  - 空结果
  - 确认不存在
---
# Collection materialization

Use this guide for listings, tables, comments, reviews and other repeated
records when the initial surface is partial, a loader persists, or the task
requires evidence for an absence/shortfall claim.

A section heading, drawer shell, loading skeleton or preview rows does not
satisfy a repeated-record target. Identify one scroll container or load-more
control, refresh AX evidence, enumerate row/field ids, batch native text and
attribute reads, deduplicate locally and persist observed rows. Decide whether to repeat from
remaining coverage, changed observations and the last experiment; zero growth
is a diagnostic fact, not an automatic stop verdict. Nested lists and multiple scroll layers need a bounded
decomposition rather than blind repeated scrolls.

A miss on one surface means only "not observed here." Choose additional reads,
scrolling or visual checks only when they can resolve a concrete uncertainty.
No fixed sequence of materialization, overlay clearing, enumeration or peer
calibration is required. Preserve conflicting observations and report blockers.

contentCompleteness is attributed observation, not a terminal verdict. Compare
its markers, counts, exhaustion receipts and attempted actions with other live
evidence, then choose a falsifiable next experiment. Persist the relevant
observation through record_extraction before handing it to Lead.

## Empty values and validation order

record_extraction persists observed rows and returns validation feedback; do
not wait for an external validation step before calling it. Read its receipt:
persistence alone does not mean the phase contract passed. Correct reported
shape or evidence issues from actual observations rather than rescraping all
trusted rows or inventing missing values.

Required keys, non-empty values and absence outcomes come from the approved
worker_contract. An allowed empty value is not a placeholder by itself. Where
allow_empty_with_outcome permits confirmed_absent, record the judgment directly:

```json
{"attachments": [], "attachmentsAbsence": {"outcome": "confirmed_absent", "evidenceText": "The fully read attachments section explicitly says no attachments."}}
```

This is a worker semantic judgment, not a mechanical proof. No eight-flag
checklist, positive epoch or required visual tool call applies. Optional
`evidenceRefs` must name existing evidence files; the harness checks those
references, while you and Lead judge whether they support the claim. A bare
empty value or a blocked/unreadable region does not declare confirmed_absent.
A displayed value such as "N/A" is page data when supported by provenance;
your own "could not retrieve" narrative is not a field value.

A region missing on one peer page may reflect different content, rendering,
entry route or an unresolved failure. Re-entry using an observed card/href is
a candidate experiment, not a mandatory prerequisite for every absence claim.
Choose checks that can distinguish the live hypotheses, and honor any explicit
route_recovery_required receipt. Screenshots, when available, answer a bounded
visual question; they are neither mandatory for every empty value nor proof of
absence by themselves. Never interpret an AX target-resolution failure as
proof that the business content is absent.
