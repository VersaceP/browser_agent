---
name: workflow_vision_first
description: Vision-first workflow for unfamiliar pages — multi-pass scroll-and-rescan + gated content recovery. Read when DOM probes return [] but a field looks visible, or when entering a new site without a complete site-skill.
---

# Vision-first workflow

DOM-only extraction is fragile: a CSS selector miss is indistinguishable from
"the field doesn't exist on this page". Workers without this workflow have
historically concluded `reviews=[]` / `pros=null` for pages that visually
HAD those sections. The vision-first workflow breaks that single-modality trap.

## When to trigger

Either of:
- **CASE A** — `navigate(list_url)` returned NO `available_skills` (new site, no playbook)
- **CASE B** — `read_skill()` output does NOT mention every field your task contract requires
              (e.g. task asks for `pricing` + `reviews` but skill only documents
              `pros / cons / views / rating`)

Trigger on the FIRST detail page (NOT the listing page).

## STEP A — prime the page so lazy content renders

Many pages render lower sections only when the user scrolls past them. A
fresh `navigate()` leaves them un-rendered, so `scrollHeight` is artificially
small and skeleton scans miss them. ONE-time priming sweep:

```
1. record sh_before = document.documentElement.scrollHeight
2. while not atBottom: scroll(dy=800); sleep(0.4)
3. sleep(1.5)                      # final XHRs / IntersectionObservers
4. record sh_after; if sh_after > sh_before * 1.3 then priming worked
5. js("window.scrollTo({top: 0, behavior: 'instant'})") + sleep(0.5)
```

## STEP B — full-page coverage via scroll-and-rescan

`vision_page_skeleton()` captures ONLY the current viewport. Vision models
downsample very tall full-page screenshots and lose bottom sections. To map
the entire page:

```
accumulated = []                       # collected sections across passes
while True:
    skel = vision_page_skeleton()       # viewport screenshot + section list
    accumulated.extend(skel.sections)
    if skel.viewport.atBottom:
        break
    scroll(dy=int(skel.viewport.innerHeight * 0.85))   # 15% overlap
    sleep(0.4)
# dedupe accumulated by (name, heading_text)
```

At the standardized window (1440×1740 outer → ~1410×1310 inner), most pages
need 4-6 passes. Cap at 8 — beyond that the page is infinite-scrolling and
you've seen enough.

## STEP C — map each task field to a section

For each task-contract field, find a matching section in `accumulated` by
semantic match between the field's purpose and the section's `heading_text`
or `sample_text`. A direct heading_text match is the strongest signal; a
sample_text containing the field's keyword is a weaker but valid signal.

## STEP D — recover gated content (sections behind a tab / button / accordion)

Some sections aren't surfaced by scroll alone — they're gated behind UI state
(a tab not yet activated, an accordion still collapsed, a "Show more" button
not yet clicked, content behind aria-expanded="false"). Pure scroll can't
reveal them.

**Trigger condition**: a task-contract field has NO matching section after STEP B.

Recovery procedure (do NOT hard-code the gating control's text — discover it):

1. Pick a viewport screenshot from `accumulated` where some navigation /
   tabbar / control row was visible (often the page top).
2. Ask vision to find the gating control SEMANTICALLY:
   ```
   analyze_screenshot(filename=<that screenshot>, query=
     "I'm looking for a section about <FIELD_NAME> on this page, but it
      isn't currently visible. Does the screenshot show any clickable UI
      element — a tab, button, link, accordion header, or 'load more'
      control — whose visible text or icon suggests it would reveal
      content related to <FIELD_NAME>? If yes, quote the exact visible
      text on that control and describe its position. If no such control
      exists on this screenshot, say 'no candidate'.")
   ```
3. If vision returned a candidate, locate it in the DOM by its visible text:
   ```
   dom_query('xpath://*[contains(normalize-space(text()), "<QUOTED TEXT>")]', max_items=3)
   ```
   Pick the first clickable result (`a`, `button`, `[role="tab"]`, etc.).
4. `click(<that selector>)` then `sleep(1.0)`
5. `js("window.scrollTo({top: 0, behavior: 'instant'})")` then `sleep(0.5)`
6. Re-run STEP A (priming) + STEP B (skeleton loop) into a FRESH `accumulated`.
7. Re-attempt STEP C. The previously-missing section should now be there.

⚠ Cap gated-recovery at 2 attempts per field. If two distinct candidate
controls both fail to surface the section, the field genuinely doesn't exist
on this page (e.g. a fresh product with zero entries yet).

**Why semantic discovery (not a synonym list)**: different sites name the same
concept differently — "Reviews" / "Comments" / "Discussion" / "Feedback" /
"评价" / "意见" / etc. A baked-in synonym table will always lag the next
site's naming. Asking vision "is anything on screen likely to reveal X?"
generalizes to any vocabulary.

## STEP E — find DOM under each mapped section

For each mapped section, anchor DOM probes on the visible heading text:
```
dom_query(selector='xpath://h2[contains(., "<heading text>")]/parent::*', ...)
```
OR `dom_outline` focused on that approx_y region. Then probe repeating items
from that subtree.

## STEP F — closing the loop

If a section appeared in skeleton but DOM probe returns `[]`:
1. Scroll the section's `approx_y` region into view, `vision_page_skeleton` again.
2. ```
   analyze_screenshot(filename=...,
     query="Where exactly is the <heading> on the page and what HTML
            element wraps each visible item under it? Quote any visible
            class= attribute or repeating element pattern you can see.")
   ```
3. Retry `dom_query` with the refined anchor.

Repeat up to 3 cycles per field.

## Concluding "field absent"

Requires EITHER:
- the section did NOT appear in skeleton AFTER gated-recovery (STEP D), OR
- the section was found but `item_count_visible=0` AND `has_repeating_items=false`
  AND dataset / inline JSON-LD / scripts also return nothing.

NEVER write `null` / `[]` for a field whose section appears in ANY skeleton
pass — that's a DOM-extraction bug, not a real absence. Re-scan and re-probe
until the DOM matches what vision sees, or until vision itself confirms the
section is gone across all passes.
