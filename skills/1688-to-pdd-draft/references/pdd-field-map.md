# PDD Field Map

Use `raw_product_data.json` as the source of truth and treat `product_info.txt` as human-readable evidence.

## Basic Fields

| PDD field | Manifest path | 1688 source |
|---|---|---|
| 商品标题 | `pdd.title` | `documentTitle` or detail title, cleaned to 60 chars |
| 品牌 | `pdd.attributes.品牌` | 商品属性 `品牌`; default `其它` |
| 面料俗称 | `pdd.attributes.面料俗称` | 商品属性 `面料名称`, fallback `主面料成分` |
| 适用年龄 | `pdd.attributes.适用年龄` | default `青年（18-25周岁）` |
| 流行元素 | `pdd.attributes.流行元素` | 商品属性 `流行元素`, fallback `元素`, default `绣花` |
| 制式 | `pdd.attributes.制式` | 商品属性 `制式`, fallback `朝代`; `汉朝` maps to `汉制` |
| 上市时节 | `pdd.attributes.上市时节` | 商品属性 `上市年份/季节`; fallback current year + source season |
| 商品货号 | `pdd.attributes.商品货号` | 商品属性 `货号` |

## SKU Fields

| PDD spec | Manifest path | 1688 source |
|---|---|---|
| 尺寸 | `pdd.specs.sizes` | 商品属性 or SKU text `尺码`, split on comma/space/slash |
| 颜色 | `pdd.specs.color` | 商品属性 or SKU text `颜色`, with brackets removed |

## Price Defaults

Use these values unless the user overrides them:

| Field | Value |
|---|---:|
| 库存 | 500 |
| 拼单价 | 59 |
| 单买价 | 69 |
| 商品参考价 | 89 |

## Asset Fields

| PDD target | Manifest path | Local source |
|---|---|---|
| 商品轮播图 | `pdd.assets.pddMainImages` | `pdd_upload/main_image_*.jpg` |
| 商品详情 | `pdd.assets.detailImages` | `detail_image_*` |

Do not map `pdd.assets.pddMainImages[0]` to SKU preview image-space upload in this skill. That is intentionally outside scope.
