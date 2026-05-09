---
name: taaft_aitools
target_websites:
  - theresanaiforthat.com
keywords:
  - taaft
  - trending ai
  - theresanaiforthat
description: TAAFT (theresanaiforthat.com) 站点抓取策略 — URL 模式、详情页 selector、最近一次踩坑教训
---

# TAAFT (theresanaiforthat.com) 抓取策略

## 关键 URL 模式 — **不要弄错**

| 入口 | URL 模式 | 用途 |
|---|---|---|
| Trending 列表页 | `https://theresanaiforthat.com/trending/` | 拿到 50 个 slug |
| **TAAFT 详情页** | `https://theresanaiforthat.com/ai/<slug>/` | 评分 / 评论 / pros / cons 在这里 |
| 产品官网 | `https://<product-domain>/?ref=taaft&utm_source=taaft` | **不要去这里抓**,这是 affiliate 链接,产品官网没有 taaft 评分数据 |

⚠️ **task_1778062783 真实踩坑**:worker 从 trending 列表拿到 `<a class="visit_website_btn">` 的 href(产品官网),直接 navigate 过去抓数据 → ratings/category 全空。**应该**用 `/ai/<slug>/` 拼出 taaft 详情页。

## 列表页 — 提取 slug

trending 列表项的 className 是 `.double` (出现 ~50 次,在 `<li>` 标签里)。每个卡片含产品名 + 链接到 `/ai/<slug>/`。

探索代码:
```python
goto("https://theresanaiforthat.com/trending/")
classes = dom_classes(min_count=20)        # 找出现 >= 20 次的 className
print([c for c in classes if c['tag_sample'] == 'li'][:5])
# 应该看到 .double 51× <li>

# 提取前 N 个 slug
items = js("""
    return [...document.querySelectorAll('li.double a[href*="/ai/"]')]
        .slice(0, 50)
        .map(a => {
            const href = a.getAttribute('href') || '';
            const m = href.match(/\\/ai\\/([^/]+)\\//);
            return m ? {slug: m[1], name: a.textContent?.trim() ?? null} : null;
        })
        .filter(Boolean);
""")
```

## 详情页 selector(基于 task_1778062783 实测有效)

| 字段 | selector | 解析 |
|---|---|---|
| views | `.stats_opens` | textContent |
| rating | `.rating_top` | textContent.match(/[\d.]+/)[0] |
| type | `a.task_label:not(.company_label)` | textContent.replace(/^[A-Z]{2},\s*/, '') |
| tool_link | `.visit_website_btn` | href.split('?')[0] |
| bookmark | `.save_button_text` | textContent |
| description | `meta[name="description"]` | content |
| pros | `.pac-info-item-pros .pac-elem` | 全部 textContent |
| cons | `.pac-info-item-cons .pac-elem` | 全部 textContent |

## 推荐工作流

```python
# 1. 列表页拿 slug
goto("https://theresanaiforthat.com/trending/")
slugs = js("""
    return [...document.querySelectorAll('li.double a[href*="/ai/"]')]
        .slice(0, 10).map(a => {
            const m = (a.getAttribute('href') || '').match(/\\/ai\\/([^/]+)\\//);
            return m ? m[1] : null;
        }).filter(Boolean);
""")
print(f"got {len(slugs)} slugs:", slugs[:3])

# 2. 逐个详情页抓
results = []
for slug in slugs:
    goto(f"https://theresanaiforthat.com/ai/{slug}/", wait=2)
    rec = js("""
        return {
            slug: location.pathname.match(/\\/ai\\/([^/]+)\\//)?.[1] ?? null,
            views: document.querySelector('.stats_opens')?.textContent?.trim() ?? null,
            rating: document.querySelector('.rating_top')?.textContent?.match(/[\\d.]+/)?.[0] ?? null,
            type: document.querySelector('a.task_label:not(.company_label)')?.textContent?.trim().replace(/^[A-Z]{2},\\s*/, '') ?? null,
            tool_link: document.querySelector('.visit_website_btn')?.href?.split('?')[0] ?? null,
            description: document.querySelector('meta[name="description"]')?.content ?? null,
            pros: [...document.querySelectorAll('.pac-info-item-pros .pac-elem')].map(e => e.textContent?.trim() ?? null).filter(Boolean),
            cons: [...document.querySelectorAll('.pac-info-item-cons .pac-elem')].map(e => e.textContent?.trim() ?? null).filter(Boolean)
        };
    """)
    rec['name'] = slug  # 占位,真名在列表页拿
    results.append(rec)

# 3. 增量 publish(每 5 个一次,防 max_turns 砍掉丢数据)
publish_artifact(name='ai_tools_top10', content=results, description=f'{len(results)} TAAFT details')
```

## 反爬注意

- TAAFT 没有强反爬,普通 navigate 就行
- 详情页加载稍慢,navigate 用 wait=2-3
- 如果出现 Cloudflare 页(标题含 "Just a moment"),helpers.goto 会自动抛 HumanInterventionRequired,worker 应汇报给 lead 而不是重试
