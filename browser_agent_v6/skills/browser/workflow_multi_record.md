---
name: workflow_multi_record
description: Explore→distill→batch 3-phase pattern for extracting the same fields from N≥3 similar pages. Read when your task needs uniform extraction across multiple detail pages, search results, or paginated lists.
---

# Multi-record extraction — explore → distill → batch (3 PHASES)

When extracting the same set of fields from N similar pages (N ≥ 3), DO NOT
process each page in a separate LLM round. That wastes tokens linearly with N
and re-runs vision per page. Use this 3-phase pattern instead — vision and
selector discovery happen ONCE, then a tool runs the rest server-side.

## ⚠ Special case: contract file ALREADY exists with partial data

If you `read_file('shared/<contract>')` and find the file already has N records
but most fields are empty / null (typical: `rank` / `name` / `slug` are filled
but `views` / `rating` / `pricing` / `reviews` etc. are blank), **DO NOT
switch to "fill in the blanks one record at a time" mode**. That single-step
loop is the failure mode this whole 3-phase pattern exists to prevent.

The correct interpretation: a previous worker delivered the listing skeleton,
and your job is to ENRICH it. This is the standard batch input — not an
exception. Steps:

  1. Read the partial file → extract the slug / url list as your `iterations`.
  2. Run PHASE 1 normally (pick a DATA-RICH slug, explore one detail page
     to find selectors for the missing fields).
  3. Run PHASE 2 normally (distill recipe + validate on a second slug).
  4. Run PHASE 3:
       batch_browser_actions(
         steps      = <recipe that emits views / rating / pricing / etc.>,
         iterations = <the N slug dicts from the partial file>,
         publish_to = '<contract basename>'   # OVERWRITES the partial file
       )
  5. The batch's auto-publish overwrites the partial file with the fully
     enriched version. The basic fields from the partial (rank/name/slug) are
     either:
     - already in your recipe (re-extracted per page; fine, cheap), OR
     - merged client-side: read the batch result + read the original partial,
       zip by index, then publish_artifact the merged form.

DO NOT navigate to each detail page separately and call extract_text per
field. That's O(N) LLM round-trips and burns out on iteration ~5 of 10.

## PHASE 1 — EXPLORE (one product, full vision-first workflow)

### ⚠ Choose a DATA-RICH sample, NOT the first item in order

Do NOT blindly pick the first record (rank 1 / index 0) for PHASE 1. Brand-new
or low-engagement entries often have empty fields (no reviews, no pricing,
missing description) and your selectors will appear to "miss" when in reality
the page just has nothing to show. That contaminates the recipe with bogus
"selector failed" signals and wastes the entire phase.

Instead, before picking the PHASE 1 target:
1. From the listing page, `dom_query(selector='<list-item class>', max_items=N+5)`
   to fetch the candidate set with their text/href/dataset.
2. Sort or scan candidates by signals of "field density":
   - longer `text` (more visible content suggests filled-in description)
   - higher view count / rating (popular = more reviews, more pricing detail)
   - `data-released` / `data-version` not too recent (older = had time to
     accumulate reviews + community content)
3. Pick the densest-looking candidate as PHASE 1 sample. The first-in-order
   record can be processed later by PHASE 3 batch like any other.

If you accidentally picked a sparse product and many selectors return empty,
the safest move is NOT to assume your selectors are wrong — re-pick a denser
candidate first, re-run PHASE 1 there, and confirm. Selectors that work on a
data-rich sample will degrade gracefully on sparse ones (just emit nulls).
The reverse is not true.

### After picking the right sample

- `navigate(first_url, wait=3)`
- Run the full vision-first workflow on this single page (`read_skill('workflow_vision_first')`)
  to discover where each task field lives in the DOM.
- Extract all required fields from this ONE product. `print()` them so you can
  see the result, OR call `publish_artifact` for the partial single-record
  file if you want a checkpoint.
- Mentally note: which steps actually produced each field, and which steps
  were dead-ends (failed `dom_query` attempts, exploratory `dom_outline` calls,
  etc.). The dead ends are throw-away.

## PHASE 2 — DISTILL & VALIDATE (one more product)

Build a CLEAN recipe expressible as `batch_browser_actions` steps. Allowed
step tools (NOT `run_browser_python`, NOT vision tools):
```
navigate, click, fill, extract_text, screenshot, scroll,
accept_dialog, dismiss_dialog, dom_query, list_skills, read_skill,
read_file, list_files
```

The recipe is a small ordered list, e.g.:
```
[{"tool": "navigate", "input": {"url": "{url}", "wait": 3}},
 {"tool": "extract_text", "input": {"selector": "h1.product-title"}},
 {"tool": "dom_query", "input": {"selector": ".price-tag", "max_items": 1}},
 {"tool": "dom_query", "input": {"selector": ".review-card", "max_items": 50}}]
```
where `"{url}"` is a placeholder substituted per iteration.

Run the recipe MANUALLY against a SECOND product URL — by calling each step
single-step (one navigate, one extract_text, etc.). Confirm the same fields
populate cleanly. If a step fails, refine it BEFORE PHASE 3.

**Why a manual second pass**: `batch_browser_actions` stops on the first
failure; one upfront manual validation saves N iterations of cascading retries.

## PHASE 3 — BATCH (remaining N-2 products in one call)

Call `batch_browser_actions` ONCE:
```
batch_browser_actions(
  steps = <distilled recipe>,
  iterations = [{"url": "<url_3>"}, {"url": "<url_4>"}, ...],   # N-2 entries
  publish_to = "<final_output_basename>",   # auto-publishes to shared/
  require_first_approval = false,
  throttle_seconds = 0.5,
)
```

The tool runs all iterations server-side, NO LLM round-trip per iteration.

The first 2 records (PHASE 1 + PHASE 2) you already have — merge them with
the batch result before calling `publish_artifact`, OR include their URLs in
`iterations` and let batch re-fetch them (simpler, slightly redundant — pick
whichever).

On a step failure, batch returns `stop_reason` + `suggested_action`. See
`read_skill('workflow_batch_recovery')` for the recovery map.

## WHY THIS PATTERN

- LLM tokens scale O(1) with N, not O(N): vision + selector discovery happen
  once, batch runs the rest.
- Selector validation BEFORE committing to N-2 iterations.
- The aggregated result auto-publishes via batch's `publish_to` parameter,
  which means you cannot forget the final delivery step.
- Skip this pattern only when N < 3 (the overhead exceeds the savings).
