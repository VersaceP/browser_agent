---
id: browser.collection-materialization
audience: browser
version: "2026-09-03"
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
attribute reads, deduplicate locally, persist once, then repeat only while the
collection grows. Nested lists and multiple scroll layers need a bounded
decomposition rather than blind repeated scrolls.

An empty selector result, truncated enumeration, screenshot, or one surface's
miss establishes only "not observed here." Before a confirmed absence, reveal
the actual section/tab, clear overlays, scroll it into view, enumerate the
current region and capture what it shows. Use a peer calibration only where the
artifact contract requires an absence proof. If the surface remains a skeleton
with zero records, classify materialization failure rather than success or
target absence.

contentCompleteness is attributed observation, not a terminal verdict. Compare
its markers, counts, exhaustion receipts and attempted actions with other live
evidence, then choose a falsifiable next experiment. Persist the relevant
observation through record_extraction before handing it to Lead.
