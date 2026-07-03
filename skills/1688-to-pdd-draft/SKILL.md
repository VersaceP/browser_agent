---
name: 1688-to-pdd-draft
description: Download selected 1688 search-result products, save product assets/info, prepare Pinduoduo-ready image files, and fill Pinduoduo merchant product-add drafts from manifests. Use when transferring products from 1688 to Pinduoduo drafts with ABCP Browser, including login-gated 1688/PDD flows, product info capture, carousel/detail image upload, category selection, attributes, SKU specs, stock, and prices. This skill deliberately stops before SKU preview image-space upload and before clicking 提交并上架.
---

# 1688 To PDD Draft

## Scope

Use this skill to reproduce the proven 1688-to-Pinduoduo draft workflow:

1. Search 1688 by keyword and capture selected result ranks.
2. Save each product under Desktop as `1688_<keyword>_第<rank>名/`.
3. Generate `product_info.txt`, `raw_product_data.json`, `product_manifest.json`, media files, and `pdd_upload/main_image_*.jpg`.
4. Open or reuse Pinduoduo merchant backend, select `女装/女士精品 > 汉服 > 汉服套装`, and fill the product-add form from a manifest.
5. Stop after carousel images, detail images, basic attributes, SKU specs, stock, and prices are filled.

Never upload SKU preview images to PDD image space and never click `提交并上架` in this skill.

## Required Companion Skill

Use `docs/abcp-browser-direct/abcp_SKILL.md` for all live browser operations. Keep one ABCP connection when possible, call `System.register`, `System.getCapabilities`, `Page.getState`, and include concrete `purpose` values on every state-changing action.

## Main Commands

Capture 1688 products:

```bash
python3 skills/1688-to-pdd-draft/scripts/abcp_1688_capture.py --keyword "汉服女装" --ranks 6,7
```

Prepare PDD images for an existing product directory:

```bash
python3 skills/1688-to-pdd-draft/scripts/prepare_pdd_assets.py /Users/versace/Desktop/1688_汉服女装_第6名
```

Fill a PDD draft from a product directory:

```bash
python3 skills/1688-to-pdd-draft/scripts/abcp_pdd_publish.py fill-draft \
  --page-id <logged-in-pdd-page-id> \
  --product-dir /Users/versace/Desktop/1688_汉服女装_第6名
```

Or run the wrapper:

```bash
python3 skills/1688-to-pdd-draft/scripts/run_1688_to_pdd_draft.py capture --keyword "汉服女装" --ranks 6,7
python3 skills/1688-to-pdd-draft/scripts/run_1688_to_pdd_draft.py fill --page-id <pdd-page-id> --product-dir /Users/versace/Desktop/1688_汉服女装_第6名
```

## PDD Preconditions

- The merchant account is logged in, or the user is ready for HITL password entry.
- The product-add page is open in the target category, or run:

```bash
python3 skills/1688-to-pdd-draft/scripts/abcp_pdd_publish.py select-category --page-id <pdd-page-id>
```

The category path is fixed by default to `服饰箱包 > 女装/女士精品 > 汉服 > 汉服套装`.

## Manifest Contract

Each product directory should contain `product_manifest.json`:

- `source.keyword`, `source.rank`, `source.url`, `source.title`
- `pdd.title`
- `pdd.attributes`: `品牌`, `面料俗称`, `适用年龄`, `流行元素`, `制式`, `上市时节`, `商品货号`
- `pdd.specs.sizes`, `pdd.specs.color`
- `pdd.price.stock`, `pdd.price.groupPrice`, `pdd.price.singlePrice`, `pdd.price.referencePrice`
- `pdd.assets.pddMainImages`, `pdd.assets.detailImages`

Read `references/pdd-field-map.md` before changing field mapping logic.

## Stop Conditions

A run is complete when the PDD form is filled and the script result contains:

```json
{"stoppedBeforeSkuPreviewAndSubmit": true}
```

Do not continue to `fix-preview`, `upload-image-space`, `direct-material-upload`, `retry-material-create`, `finish-current`, `publish-current`, or `submit_product` unless the user explicitly gives a new approval that includes final submission.

## Compatibility Wrappers

The old paths under `docs/abcp-browser-direct/scripts/` are retained as thin wrappers for allowed draft commands. Treat the scripts under this skill directory as the source of truth. Full publish phases are intentionally not exposed by this skill.
