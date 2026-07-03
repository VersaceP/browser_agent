#!/usr/bin/env python3
"""Capture the 6th and 7th 1688 search results for a keyword.

This driver follows the abcp-browser-direct skill: it talks to ABCP Browser
directly through ABCPClient, uses Page/DOM/Input actions for navigation, and
uses Runtime.evaluate only for DOM relationships and asset URL extraction that
AXTree cannot expose reliably.
"""

import asyncio
import argparse
import json
import mimetypes
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple


REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT))

from abcp_client import ABCPClient, ABCPClientConfig  # noqa: E402


JsonDict = Dict[str, Any]

KEYWORD = "汉服女装"
TARGET_RANKS = [6, 7]
DESKTOP = Path.home() / "Desktop"
PDD_CATEGORY_PATH = ["服饰箱包", "女装/女士精品", "汉服", "汉服套装"]
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)


def load_browser_config(agent_id_override: Optional[str] = None) -> Tuple[ABCPClientConfig, str]:
    raw = json.loads((REPO_ROOT / "config.json").read_text(encoding="utf-8"))
    browser_raw = raw.get("browser") or {}
    cfg = ABCPClientConfig.from_dict(browser_raw)
    agent_id = agent_id_override or browser_raw.get("agent_id") or "codex-abcp-1688-capture"
    return cfg, agent_id


def log(message: str) -> None:
    print(message, flush=True)


def response_data(resp: JsonDict) -> Any:
    return resp.get("data", resp)


def normalize_url(url: str, base: str = "https://www.1688.com/") -> str:
    if not url:
        return ""
    url = str(url).strip()
    if url.startswith("//"):
        url = "https:" + url
    return urllib.parse.urljoin(base, url)


def clean_filename(text: str, fallback: str) -> str:
    text = re.sub(r"[\\/:*?\"<>|\r\n\t]+", "_", text or "").strip(" ._")
    text = re.sub(r"\s+", "_", text)
    return (text or fallback)[:80]


def unique_dir(path: Path) -> Path:
    if not path.exists():
        return path
    stamp = time.strftime("%Y%m%d_%H%M%S")
    candidate = path.with_name(f"{path.name}_{stamp}")
    if not candidate.exists():
        return candidate
    i = 2
    while True:
        next_candidate = path.with_name(f"{path.name}_{stamp}_{i}")
        if not next_candidate.exists():
            return next_candidate
        i += 1


def ext_from_url(url: str, content_type: Optional[str] = None) -> str:
    path = urllib.parse.urlsplit(url).path
    suffix = Path(path).suffix.lower()
    if suffix in {
        ".jpg",
        ".jpeg",
        ".png",
        ".webp",
        ".gif",
        ".bmp",
        ".mp4",
        ".m4v",
        ".mov",
        ".m3u8",
    }:
        return ".jpg" if suffix == ".jpeg" else suffix
    if content_type:
        guessed = mimetypes.guess_extension(content_type.split(";")[0].strip())
        if guessed:
            return ".jpg" if guessed == ".jpe" else guessed
    return ".bin"


def dedupe_urls(urls: Iterable[str]) -> List[str]:
    seen = set()
    result = []
    for raw in urls:
        url = normalize_url(raw)
        if not url or url.startswith("data:") or url.startswith("blob:"):
            continue
        parsed = urllib.parse.urlsplit(url)
        key = urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))
        if key in seen:
            continue
        seen.add(key)
        result.append(url)
    return result


def parse_ranks(raw: str) -> List[int]:
    ranks = []
    for part in str(raw or "").split(","):
        part = part.strip()
        if not part:
            continue
        ranks.append(int(part))
    if not ranks:
        raise ValueError("--ranks must contain at least one rank")
    return ranks


def is_login_state(state: JsonDict) -> bool:
    data = response_data(state) or {}
    url = str(data.get("url") or "")
    title = str(data.get("title") or "")
    return (
        "login.taobao.com" in url
        or "login.1688.com" in url
        or "login_jump" in url
        or "登录" in title and "1688" not in title and "首页" not in title
    )


def is_search_results_state(state: JsonDict) -> bool:
    data = response_data(state) or {}
    url = str(data.get("url") or "")
    title = str(data.get("title") or "")
    return (
        "s.1688.com" in url
        and "selloffer" in url
        and "punish" not in url
        and "login" not in url
        and (KEYWORD in title or "keywords=" in url)
    )


def extract_ax_id(line: str) -> Optional[str]:
    match = re.search(r"\[(\d+:\-?\d+:\-?\d+)\]", line)
    return match.group(1) if match else None


async def call(browser: ABCPClient, method: str, params: Optional[JsonDict] = None) -> JsonDict:
    resp = await browser.call(method, params or {})
    obs = str(resp.get("observation") or "")
    if obs:
        log(f"{method}: {obs[:240]}")
    return resp


async def page_state(browser: ABCPClient, page_id: str, purpose: str) -> JsonDict:
    return await call(browser, "Page.getState", {"pageId": page_id, "purpose": purpose})


async def wait_settled(browser: ABCPClient, page_id: str, purpose: str, timeout: float = 12.0) -> JsonDict:
    def matches(msg: JsonDict) -> bool:
        params = msg.get("params") or {}
        data = params.get("data") or {}
        payload = data.get("payload") or {}
        event = str(data.get("event") or data.get("type") or "")
        return page_id in json.dumps(payload, ensure_ascii=False) and event in {
            "Page.loaded",
            "Page.loadFailed",
            "Page.navigated",
            "Page.recovered",
        }

    try:
        await browser.wait_for_notification(matches, timeout=timeout, replay_window_seconds=1.5)
    except Exception:
        pass
    return await page_state(browser, page_id, purpose)


async def eval_js(browser: ABCPClient, page_id: str, body: str, purpose: str) -> Any:
    resp = await call(
        browser,
        "Runtime.evaluate",
        {
            "pageId": page_id,
            "expression": body,
            "returnByValue": True,
            "purpose": purpose,
        },
    )
    return response_data(resp)


async def get_ax_lines(browser: ABCPClient, page_id: str, purpose: str) -> List[str]:
    resp = await call(browser, "DOM.getAXTree", {"pageId": page_id, "purpose": purpose})
    data = response_data(resp) or {}
    return list(data.get("lines") or [])


async def find_search_input(browser: ABCPClient, page_id: str) -> Optional[str]:
    lines = await get_ax_lines(
        browser,
        page_id,
        "Locate the 1688 search text field before entering the requested keyword.",
    )
    textfields = [line for line in lines if " textfield " in line.lower() or " textfield" in line.lower()]
    for line in textfields:
        if "以图搜款" not in line and "file" not in line.lower():
            ax_id = extract_ax_id(line)
            if ax_id:
                log(f"Selected search textfield from AXTree: {line[:180]}")
                return ax_id
    return None


SEARCH_BUTTON_JS = r"""
const clean = (s) => String(s || "").replace(/\s+/g, " ").trim();
const visible = (el) => {
  const r = el.getBoundingClientRect();
  const st = getComputedStyle(el);
  return r.width > 10 && r.height > 10 && st.visibility !== "hidden" && st.display !== "none";
};
const candidates = Array.from(document.querySelectorAll("button,input[type=submit],a,[role=button],div,span"))
  .filter((el) => visible(el))
  .map((el, i) => {
    const r = el.getBoundingClientRect();
    const text = clean(el.innerText || el.value || el.getAttribute("aria-label") || "");
    const cls = clean(el.className);
    return {
      i,
      tag: el.tagName,
      type: el.type || "",
      role: el.getAttribute("role") || "",
      text,
      id: el.id || "",
      className: cls,
      href: el.href || "",
      rect: {x: r.x, y: r.y, w: r.width, h: r.height},
      score: (text.match(/搜\s*索|搜索/) ? 10 : 0) + (cls.toLowerCase().includes("search") ? 3 : 0)
    };
  })
  .filter((item) => item.score > 0)
  .sort((a, b) => b.score - a.score || a.rect.y - b.rect.y || a.rect.x - b.rect.x);
return candidates.slice(0, 20);
"""


async def submit_search(browser: ABCPClient, page_id: str) -> JsonDict:
    input_id = await find_search_input(browser, page_id)
    if input_id:
        await call(
            browser,
            "Input.type",
            {
                "pageId": page_id,
                "id": input_id,
                "text": KEYWORD,
                "clear": True,
                "delay": 35,
                "purpose": "Enter the requested keyword into the 1688 search field.",
            },
        )
        await call(
            browser,
            "Input.press",
            {
                "pageId": page_id,
                "key": "Enter",
                "purpose": "Submit the 1688 search after entering the requested keyword.",
            },
        )
        state = await wait_settled(
            browser,
            page_id,
            "Check whether pressing Enter opened the 1688 search results page.",
            timeout=5,
        )
        data = response_data(state) or {}
        if "1688.com/" in str(data.get("url") or "") and "www.1688.com/" not in str(data.get("url") or ""):
            return state

    buttons = await eval_js(
        browser,
        page_id,
        SEARCH_BUTTON_JS,
        "Find visible search submit candidates because AXTree did not expose a reliable search button.",
    )
    if isinstance(buttons, list) and buttons:
        button = buttons[0]
        rect = button.get("rect") or {}
        x = float(rect.get("x", 0)) + float(rect.get("w", 0)) / 2
        y = float(rect.get("y", 0)) + float(rect.get("h", 0)) / 2
        log(f"Clicking search candidate: {button}")
        await page_state(
            browser,
            page_id,
            "Confirm the page is stable before clicking the search submit candidate.",
        )
        await call(
            browser,
            "Input.click",
            {
                "pageId": page_id,
                "x": x,
                "y": y,
                "clickCount": 1,
                "purpose": "Click the 1688 search submit control for the requested keyword.",
            },
        )
        state = await wait_settled(
            browser,
            page_id,
            "Check whether clicking the search submit control opened search results.",
            timeout=8,
        )
        data = response_data(state) or {}
        if "www.1688.com/" not in str(data.get("url") or ""):
            return state

    encoded = urllib.parse.quote_from_bytes(KEYWORD.encode("gbk"))
    search_url = f"https://s.1688.com/selloffer/offer_search.htm?keywords={encoded}"
    await call(
        browser,
        "Page.navigate",
        {
            "pageId": page_id,
            "url": search_url,
            "purpose": "Open the official 1688 search results URL after form submission did not navigate.",
        },
    )
    return await wait_settled(
        browser,
        page_id,
        "Check whether the official 1688 search URL loaded or redirected.",
        timeout=12,
    )


SEARCH_PRODUCTS_JS = r"""
const clean = (s) => String(s || "").replace(/\s+/g, " ").trim();
const absUrl = (u) => {
  if (!u) return "";
  if (u.startsWith("//")) return "https:" + u;
  try { return new URL(u, location.href).href; } catch (_) { return ""; }
};
const imgUrl = (img) => absUrl(
  img.getAttribute("src") ||
  img.getAttribute("data-src") ||
  img.getAttribute("data-lazy-src") ||
  img.getAttribute("data-original") ||
  img.getAttribute("data-img") ||
  ""
);
const offerId = (href) => {
  const m = href.match(/offer\/(\d+)\.html|offerId=(\d+)|offerId%3D(\d+)/);
  return m ? (m[1] || m[2] || m[3]) : "";
};
const cardFor = (a) => {
  let best = a;
  let node = a;
  for (let i = 0; i < 8 && node; i++, node = node.parentElement) {
    const r = node.getBoundingClientRect();
    const imgs = node.querySelectorAll ? node.querySelectorAll("img").length : 0;
    const text = clean(node.innerText);
    if (r.width >= 120 && r.height >= 120 && imgs >= 1 && text.length >= 8) best = node;
  }
  return best;
};
const rows = [];
for (const a of Array.from(document.querySelectorAll("a[href]"))) {
  const href = absUrl(a.getAttribute("href"));
  const id = offerId(href);
  if (!id) continue;
  const card = cardFor(a);
  const r = card.getBoundingClientRect();
  if (r.width < 80 || r.height < 80) continue;
  const imgs = Array.from(card.querySelectorAll("img")).map((img) => ({
    src: imgUrl(img),
    alt: clean(img.alt),
    area: img.getBoundingClientRect().width * img.getBoundingClientRect().height
  })).filter((img) => img.src);
  imgs.sort((a, b) => b.area - a.area);
  const textLines = clean(card.innerText).split(" ").filter(Boolean);
  const title = clean(
    a.getAttribute("title") ||
    a.innerText ||
    (imgs[0] && imgs[0].alt) ||
    textLines.find((line) => /[\u4e00-\u9fa5]/.test(line)) ||
    ""
  );
  rows.push({
    offerId: id,
    href,
    title,
    image: imgs[0] ? imgs[0].src : "",
    text: clean(card.innerText).slice(0, 500),
    absY: r.top + window.scrollY,
    absX: r.left + window.scrollX,
    rect: {x: r.x, y: r.y, w: r.width, h: r.height}
  });
}
const byId = new Map();
for (const item of rows) {
  const old = byId.get(item.offerId);
  if (!old || item.absY < old.absY || (item.absY === old.absY && item.absX < old.absX)) {
    byId.set(item.offerId, item);
  }
}
return Array.from(byId.values())
  .sort((a, b) => a.absY - b.absY || a.absX - b.absX)
  .map((item, index) => ({...item, rank: index + 1}))
  .slice(0, 40);
"""


async def collect_search_products(browser: ABCPClient, page_id: str, target_ranks: List[int]) -> List[JsonDict]:
    products: List[JsonDict] = []
    for attempt in range(8):
        await page_state(
            browser,
            page_id,
            f"Confirm search results state before collecting product cards, pass {attempt + 1}.",
        )
        batch = await eval_js(
            browser,
            page_id,
            SEARCH_PRODUCTS_JS,
            "Extract product card links and ordering because result card relationships are not fully represented in AXTree.",
        )
        if isinstance(batch, list):
            products = batch
            log(f"Collected {len(products)} product candidates from search results.")
            if len(products) >= max(target_ranks):
                return products
        await call(
            browser,
            "Input.scroll",
            {
                "pageId": page_id,
                "direction": "down",
                "amount": 900,
                "purpose": "Load more 1688 search result cards until ranks 6 and 7 are visible.",
            },
        )
        await asyncio.sleep(1.0)
    return products


PRODUCT_EXTRACT_JS = r"""
const clean = (s) => String(s || "").replace(/\s+/g, " ").trim();
const absUrl = (u) => {
  if (!u) return "";
  if (u.startsWith("//")) return "https:" + u;
  try { return new URL(u, location.href).href; } catch (_) { return ""; }
};
const urlFromStyle = (style) => {
  const m = String(style || "").match(/url\(["']?([^"')]+)["']?\)/);
  return m ? absUrl(m[1]) : "";
};
const imgCandidates = (root) => Array.from(root.querySelectorAll("img")).map((img) => {
  const r = img.getBoundingClientRect();
  const attrs = [
    img.currentSrc,
    img.src,
    img.getAttribute("src"),
    img.getAttribute("data-src"),
    img.getAttribute("data-lazy-src"),
    img.getAttribute("data-original"),
    img.getAttribute("data-img"),
    img.getAttribute("data-lazyload-src"),
    img.getAttribute("data-ks-lazyload")
  ];
  return {
    url: absUrl(attrs.find(Boolean) || ""),
    alt: clean(img.alt),
    absY: r.top + window.scrollY,
    absX: r.left + window.scrollX,
    w: r.width,
    h: r.height,
    area: r.width * r.height
  };
}).filter((img) => img.url && img.area >= 1600);
const bgImages = Array.from(document.querySelectorAll("*")).map((el) => {
  const r = el.getBoundingClientRect();
  const url = urlFromStyle(getComputedStyle(el).backgroundImage);
  return {url, alt: "", absY: r.top + window.scrollY, absX: r.left + window.scrollX, w: r.width, h: r.height, area: r.width * r.height};
}).filter((img) => img.url && img.area >= 6400);
const images = imgCandidates(document).concat(bgImages);
const dedupe = (items) => {
  const seen = new Set();
  const out = [];
  for (const item of items) {
    const key = item.url.replace(/[?#].*$/, "");
    if (!key || seen.has(key)) continue;
    seen.add(key);
    out.push(item);
  }
  return out;
};
const allImages = dedupe(images).sort((a, b) => a.absY - b.absY || b.area - a.area);
const mainImages = allImages
  .filter((img) => img.absY < window.scrollY + 1200 && img.w >= 80 && img.h >= 80)
  .sort((a, b) => b.area - a.area)
  .slice(0, 12)
  .map((img) => img.url);
const detailImages = allImages
  .filter((img) => img.absY >= 900 && !mainImages.includes(img.url))
  .sort((a, b) => a.absY - b.absY || a.absX - b.absX)
  .slice(0, 80)
  .map((img) => img.url);
const videos = [];
for (const v of Array.from(document.querySelectorAll("video, source"))) {
  const src = absUrl(v.currentSrc || v.src || v.getAttribute("src") || "");
  if (src) videos.push(src);
}
const html = document.documentElement.innerHTML;
for (const m of html.matchAll(/https?:\\\/\\\/[^"'\\\s]+?\\.(?:mp4|m3u8)(?:\\?[^"'\\\s]*)?/g)) {
  videos.push(m[0].replace(/\\\//g, "/"));
}
for (const m of html.matchAll(/\/\/[^"'\\\s]+?\\.(?:mp4|m3u8)(?:\\?[^"'\\\s]*)?/g)) {
  videos.push("https:" + m[0].replace(/\\\//g, "/"));
}
const bodyText = clean(document.body.innerText || "");
const lines = bodyText.split(/ (?=[\u4e00-\u9fa5A-Za-z0-9])/).map(clean).filter(Boolean);
const titleCandidates = [
  ...Array.from(document.querySelectorAll("h1,[class*=title],[class*=Title]")).map((el) => clean(el.innerText || el.textContent)),
  clean(document.title || "")
].filter((s) => s && !/1688|登录|采购批发/.test(s));
const title = titleCandidates.sort((a, b) => b.length - a.length)[0] || clean(document.title || "");
const sectionAround = (keywords, radius = 5) => {
  const out = [];
  for (let i = 0; i < lines.length; i++) {
    if (keywords.some((kw) => lines[i].includes(kw))) {
      out.push(lines.slice(Math.max(0, i - 1), Math.min(lines.length, i + radius)).join("\n"));
    }
  }
  return Array.from(new Set(out)).slice(0, 12);
};
const labelValuePairs = [];
for (const tr of Array.from(document.querySelectorAll("tr, li, dl, .prop, [class*=prop], [class*=Prop], [class*=attribute], [class*=Attribute]"))) {
  const text = clean(tr.innerText || tr.textContent);
  if (text && text.length <= 300 && /[\u4e00-\u9fa5]/.test(text)) labelValuePairs.push(text);
}
const sizeTexts = sectionAround(["尺码", "尺寸", "大小", "规格"], 8);
const packagingTexts = sectionAround(["包装", "装箱", "箱规", "包规", "包装方式"], 8);
const attrTexts = Array.from(new Set(labelValuePairs.concat(sectionAround(["产品属性", "商品属性", "属性", "参数", "材质", "风格", "货号", "品牌"], 8))))
  .filter((s) => s.length <= 500)
  .slice(0, 80);
return {
  url: location.href,
  title,
  documentTitle: document.title,
  bodySample: bodyText.slice(0, 2000),
  sizes: sizeTexts,
  attributes: attrTexts,
  packaging: packagingTexts,
  mainImages,
  detailImages,
  videos: Array.from(new Set(videos)).slice(0, 20)
};
"""


async def open_product_and_extract(
    browser: ABCPClient,
    item: JsonDict,
    fleet_id: Optional[str] = None,
    fallback_page_id: Optional[str] = None,
) -> JsonDict:
    href = item["href"]
    page_id = ""
    create_attempts: List[JsonDict] = []
    if fleet_id:
        create_attempts.append({"fleetId": fleet_id, "url": href})
    create_attempts.append({"url": href})

    for create_params in create_attempts:
        try:
            page = await call(browser, "Page.create", create_params)
            pdata = response_data(page) or {}
            page_id = pdata.get("pageId") or ""
            if page_id:
                break
        except Exception as exc:
            log(f"Page.create attempt failed for rank {item.get('rank')}: {exc}")

    if not page_id:
        try:
            page = await call(browser, "Page.create", {})
            pdata = response_data(page) or {}
            page_id = pdata.get("pageId") or ""
            if page_id:
                await call(
                    browser,
                    "Page.navigate",
                    {
                        "pageId": page_id,
                        "url": href,
                        "purpose": f"Navigate a blank page to the 1688 product detail URL for search result rank {item.get('rank')}.",
                    },
                )
        except Exception as exc:
            log(f"Blank Page.create plus navigate failed for rank {item.get('rank')}: {exc}")

    if not page_id and fallback_page_id:
        page_id = fallback_page_id
        await call(
            browser,
            "Page.navigate",
            {
                "pageId": page_id,
                "url": href,
                "purpose": f"Reuse the existing search-result page to open the 1688 product detail URL for rank {item.get('rank')} after new-tab creation failed.",
            },
        )

    if not page_id:
        raise RuntimeError(f"Could not open product detail page for {href}")
    state = await wait_settled(
        browser,
        page_id,
        f"Confirm product detail page for rank {item['rank']} finished loading.",
        timeout=18,
    )
    if is_login_state(state):
        return {"rank": item["rank"], "href": item["href"], "blocked": "login", "state": response_data(state)}

    await get_ax_lines(
        browser,
        page_id,
        f"Inspect the product detail page structure for rank {item['rank']} before extracting information.",
    )
    for i in range(8):
        try:
            await call(
                browser,
                "Input.scroll",
                {
                    "pageId": page_id,
                    "direction": "down",
                    "amount": 900,
                    "purpose": "Load lazy product detail images and specification sections before extraction.",
                },
            )
            await asyncio.sleep(0.8)
        except Exception as exc:
            log(f"Input.scroll failed while lazy-loading rank {item.get('rank')}; continuing extraction with currently loaded content: {exc}")
            break
    data = await eval_js(
        browser,
        page_id,
        PRODUCT_EXTRACT_JS,
        "Extract product title, specification text, packaging text, and media URLs that require DOM relationship traversal.",
    )
    if not isinstance(data, dict):
        data = {}
    data.update({"rank": item["rank"], "searchCard": item, "pageId": page_id})
    if not data.get("title"):
        data["title"] = item.get("title") or response_data(state).get("title") or ""
    data["mainImages"] = dedupe_urls(data.get("mainImages") or [])
    data["detailImages"] = dedupe_urls(data.get("detailImages") or [])
    data["videos"] = dedupe_urls(data.get("videos") or [])
    return data


async def browser_download(browser: ABCPClient, page_id: str, url: str, path: Path, purpose: str) -> bool:
    try:
        await call(
            browser,
            "File.download",
            {
                "pageId": page_id,
                "url": url,
                "savePath": str(path),
                "purpose": purpose,
            },
        )
        return path.exists() and path.stat().st_size > 0
    except Exception as exc:
        log(f"File.download failed for {url}: {exc}")
        return False


def urllib_download(url: str, path_without_ext: Path, referer: str) -> Optional[Path]:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Referer": referer,
            "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            content_type = resp.headers.get("Content-Type")
            ext = ext_from_url(url, content_type)
            path = path_without_ext.with_suffix(ext)
            path.write_bytes(resp.read())
            if path.stat().st_size > 0:
                return path
    except Exception as exc:
        log(f"urllib download failed for {url}: {exc}")
    return None


async def save_asset_group(
    browser: ABCPClient,
    page_id: str,
    urls: List[str],
    directory: Path,
    prefix: str,
    referer: str,
    max_count: int,
) -> List[str]:
    saved: List[str] = []
    for index, url in enumerate(urls[:max_count], start=1):
        if prefix == "detail_image" and url.endswith("_sum.jpg"):
            url = url[: -len("_sum.jpg")]
        base = directory / f"{prefix}_{index:02d}"
        ext = ext_from_url(url)
        target = base.with_suffix(ext)
        ok = await browser_download(
            browser,
            page_id,
            url,
            target,
            f"Save {prefix} asset {index} for the requested 1688 product capture.",
        )
        final_path: Optional[Path] = target if ok else urllib_download(url, base, referer)
        if final_path:
            saved.append(str(final_path))
    return saved


def format_list(items: Any) -> str:
    if not items:
        return "未在页面中明确找到"
    if isinstance(items, list):
        return "\n".join(f"- {clean}" for clean in [str(x).strip() for x in items] if clean) or "未在页面中明确找到"
    return str(items).strip() or "未在页面中明确找到"


def clean_title(text: str) -> str:
    text = re.sub(r"\s*-\s*阿里巴巴\s*$", "", text or "").strip()
    text = re.sub(r"\s+\d+(?:\.\d+)?\s+\d+\+人好评.*$", "", text).strip()
    return text[:60]


def compact_text(value: Any) -> str:
    if isinstance(value, list):
        value = "\n".join(str(item) for item in value if item)
    return re.sub(r"\s+", " ", str(value or "")).strip()


def value_after_label(text: str, label: str, stop_labels: Iterable[str]) -> str:
    labels = [label, *stop_labels]
    pattern = "|".join(re.escape(item) for item in labels if item)
    match = re.search(rf"{re.escape(label)}\s+(.+?)(?=\s+(?:{pattern})\s+|$)", text)
    if not match:
        return ""
    value = match.group(1).strip(" ：:，,")
    value = re.sub(r"\s+", " ", value)
    return value


def split_sizes(value: str) -> List[str]:
    values = re.split(r"[,，/、\s]+", value or "")
    allowed = []
    for item in values:
        item = item.strip()
        if not item or len(item) > 8:
            continue
        if re.search(r"^[0-9]*X?S$|^[SML]$|^XL$|^[2-9]XL$|^均码$|^定制$", item, re.I):
            normalized = item.upper() if re.search(r"^[0-9]*x?[sml]$|^xl$|^[2-9]xl$", item, re.I) else item
            if normalized not in allowed:
                allowed.append(normalized)
    return allowed


def normalize_color(value: str) -> str:
    value = re.sub(r"[【】\[\]]", "", value or "").strip()
    value = re.sub(r"\s+", "", value)
    return value[:30] or "默认"


def season_from_source(attr_text: str, title: str = "") -> str:
    season = value_after_label(attr_text, "上市年份/季节", ["主面料成分含量", "主面料成分2", "颜色", "尺码"])
    season = season.replace(" ", "")
    if re.search(r"\d{4}年", season) and re.search(r"春|夏|秋|冬", season):
        return season
    current_year = time.strftime("%Y年")
    if "夏" in title:
        return current_year + "夏季"
    if "秋" in title:
        return current_year + "秋季"
    if "冬" in title:
        return current_year + "冬季"
    if "春" in title:
        return current_year + "春季"
    source_season = value_after_label(attr_text, "适用季节", ["品牌", "上市年份/季节", "主面料成分含量"])
    if "夏" in source_season:
        return current_year + "夏季"
    if "秋" in source_season:
        return current_year + "秋季"
    if "冬" in source_season:
        return current_year + "冬季"
    if "春" in source_season:
        return current_year + "春季"
    return current_year + "春季"


def pdd_fields_from_product(product: JsonDict) -> JsonDict:
    attr_text = compact_text(product.get("attributes"))
    size_text = compact_text(product.get("sizes"))
    title = clean_title(str(product.get("documentTitle") or product.get("title") or product.get("searchCard", {}).get("title") or ""))
    all_text = compact_text([attr_text, size_text, product.get("bodySample")])
    stop_labels = [
        "主面料成分",
        "面料名称",
        "工艺",
        "朝代",
        "款式",
        "适用季节",
        "品牌",
        "上市年份/季节",
        "主面料成分含量",
        "主面料成分2",
        "主面料成分2含量",
        "颜色",
        "尺码",
        "图案",
        "货号",
        "流行元素",
        "元素",
        "适用性别",
        "袖型",
        "货源类型",
        "吊牌",
        "制式",
        "领标",
        "重量(g)",
    ]
    size_value = value_after_label(all_text, "尺码", stop_labels)
    sizes = split_sizes(size_value)
    if not sizes:
        sizes = ["S", "M", "L", "XL"]

    title_style = next((item for item in ["唐制", "明制", "宋制", "汉制", "晋制"] if item in title), "")
    style = value_after_label(attr_text, "制式", stop_labels) or title_style or value_after_label(attr_text, "朝代", stop_labels)
    if style == "汉朝":
        style = "汉制"
    elif not style:
        style = "唐制" if "唐制" in all_text else "其他"

    popular = value_after_label(attr_text, "流行元素", stop_labels) or value_after_label(attr_text, "元素", stop_labels) or "绣花"
    popular = re.split(r"\s+", popular.strip())[0] if popular else "绣花"

    return {
        "title": title,
        "attributes": {
            "品牌": value_after_label(attr_text, "品牌", stop_labels) or "其它",
            "面料俗称": value_after_label(attr_text, "面料名称", stop_labels) or value_after_label(attr_text, "主面料成分", stop_labels) or "其它",
            "适用年龄": "青年（18-25周岁）",
            "流行元素": popular,
            "制式": style,
            "上市时节": season_from_source(attr_text, title),
            "商品货号": value_after_label(attr_text, "货号", stop_labels) or "",
        },
        "specs": {
            "sizeName": "尺寸",
            "sizes": sizes,
            "colorName": "颜色",
            "color": normalize_color(value_after_label(attr_text, "颜色", stop_labels) or value_after_label(size_text, "颜色", stop_labels)),
        },
        "price": {
            "stock": 500,
            "groupPrice": 59,
            "singlePrice": 69,
            "referencePrice": 89,
        },
    }


def image_size(path: Path) -> Optional[Tuple[int, int]]:
    try:
        result = subprocess.run(
            ["/usr/bin/sips", "-g", "pixelWidth", "-g", "pixelHeight", str(path)],
            check=True,
            text=True,
            capture_output=True,
        )
    except Exception:
        return None
    width = height = None
    for line in result.stdout.splitlines():
        line = line.strip()
        if line.startswith("pixelWidth:"):
            width = int(line.split(":", 1)[1].strip())
        elif line.startswith("pixelHeight:"):
            height = int(line.split(":", 1)[1].strip())
    if width is None or height is None:
        return None
    return width, height


def is_large_image(path: Path) -> bool:
    size = image_size(path)
    if size is None:
        return True
    width, height = size
    return width >= 400 and height >= 400


def prepare_pdd_main_images(directory: Path, max_count: int = 10) -> List[str]:
    output_dir = directory / "pdd_upload"
    output_dir.mkdir(exist_ok=True)
    for old in output_dir.glob("main_image_*.jpg"):
        old.unlink()
    saved: List[str] = []
    sources = sorted(
        path for path in directory.glob("main_image_*")
        if path.is_file()
        and path.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp"}
        and is_large_image(path)
    )
    for index, source in enumerate(sources[:max_count], start=1):
        target = output_dir / f"main_image_{index:02d}.jpg"
        suffix = source.suffix.lower()
        try:
            if suffix in {".jpg", ".jpeg"}:
                shutil.copyfile(source, target)
            else:
                subprocess.run(
                    ["/usr/bin/sips", "-s", "format", "jpeg", str(source), "--out", str(target)],
                    check=True,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            saved.append(str(target))
        except Exception as exc:
            log(f"Could not prepare PDD main image {source}: {exc}")
    return saved


def write_product_manifest(directory: Path, product: JsonDict, saved_main: List[str], saved_detail: List[str], saved_video: List[str]) -> JsonDict:
    pdd = pdd_fields_from_product(product)
    manifest = {
        "schema": "1688-to-pdd-draft/v1",
        "generatedAt": time.strftime("%Y-%m-%d %H:%M:%S"),
        "source": {
            "keyword": KEYWORD,
            "rank": int(product.get("rank") or 0),
            "url": str(product.get("url") or product.get("href") or product.get("searchCard", {}).get("href") or ""),
            "title": clean_title(str(product.get("documentTitle") or product.get("title") or "")),
        },
        "pdd": {
            "categoryPath": PDD_CATEGORY_PATH,
            **pdd,
            "assets": {
                "mainImages": saved_main,
                "detailImages": saved_detail,
                "pddMainImages": prepare_pdd_main_images(directory),
                "videos": saved_video,
            },
        },
        "rawText": {
            "sizes": product.get("sizes") or [],
            "attributes": product.get("attributes") or [],
            "packaging": product.get("packaging") or [],
        },
    }
    (directory / "product_manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return manifest


async def save_product(browser: ABCPClient, product: JsonDict) -> JsonDict:
    rank = int(product.get("rank") or 0)
    title = str(product.get("title") or product.get("searchCard", {}).get("title") or f"rank_{rank}")
    dirname = unique_dir(DESKTOP / f"1688_{KEYWORD}_第{rank}名")
    dirname.mkdir(parents=True, exist_ok=False)
    url = str(product.get("url") or product.get("href") or product.get("searchCard", {}).get("href") or "")
    page_id = str(product.get("pageId") or "")

    info = (
        f"搜索关键词：{KEYWORD}\n"
        f"搜索结果名次：第{rank}名\n"
        f"商品链接：{url}\n"
        f"商品标题：{title}\n\n"
        f"尺码：\n{format_list(product.get('sizes'))}\n\n"
        f"商品属性：\n{format_list(product.get('attributes'))}\n\n"
        f"包装信息：\n{format_list(product.get('packaging'))}\n"
    )
    (dirname / "product_info.txt").write_text(info, encoding="utf-8")

    (dirname / "raw_product_data.json").write_text(
        json.dumps(product, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    saved_main = await save_asset_group(
        browser,
        page_id,
        product.get("mainImages") or [],
        dirname,
        "main_image",
        url,
        max_count=12,
    )
    saved_detail = await save_asset_group(
        browser,
        page_id,
        product.get("detailImages") or [],
        dirname,
        "detail_image",
        url,
        max_count=80,
    )
    video_urls = product.get("videos") or []
    saved_video = await save_asset_group(
        browser,
        page_id,
        video_urls,
        dirname,
        "video",
        url,
        max_count=5,
    )
    if video_urls:
        (dirname / "video_links.txt").write_text("\n".join(video_urls), encoding="utf-8")
    manifest = write_product_manifest(dirname, product, saved_main, saved_detail, saved_video)

    return {
        "rank": rank,
        "directory": str(dirname),
        "title": title,
        "url": url,
        "mainImagesSaved": saved_main,
        "detailImagesSaved": saved_detail,
        "videosSaved": saved_video,
        "videoLinks": video_urls,
        "manifest": str(dirname / "product_manifest.json"),
        "pddMainImagesPrepared": manifest.get("pdd", {}).get("assets", {}).get("pddMainImages", []),
    }


async def main() -> int:
    global KEYWORD
    parser = argparse.ArgumentParser(description="Capture 1688 search result products through ABCP Browser.")
    parser.add_argument("--agent-id", default=None, help="ABCP agent id to register and reuse.")
    parser.add_argument("--page-id", default=None, help="Existing ABCP pageId to reuse instead of creating a new home page.")
    parser.add_argument("--fleet-id", default=None, help="Existing ABCP fleetId to use for product detail tabs.")
    parser.add_argument("--keyword", default=KEYWORD, help="1688 search keyword.")
    parser.add_argument("--ranks", default="6,7", help="Comma-separated search result ranks to capture.")
    args = parser.parse_args()

    KEYWORD = str(args.keyword or KEYWORD).strip() or KEYWORD
    target_ranks = parse_ranks(args.ranks)
    cfg, agent_id = load_browser_config(args.agent_id)
    summary: JsonDict = {
        "keyword": KEYWORD,
        "targetRanks": target_ranks,
        "startedAt": time.strftime("%Y-%m-%d %H:%M:%S"),
        "products": [],
    }

    async with ABCPClient(cfg) as browser:
        await call(browser, "System.register", {"agentId": agent_id})
        await call(browser, "System.getCapabilities", {"skillFile": False})

        if args.page_id:
            page_id = args.page_id
            await call(
                browser,
                "Page.switchTo",
                {
                    "pageId": page_id,
                    "purpose": "Use the existing logged-in 1688 browser page selected by the user.",
                },
            )
        else:
            page = await call(browser, "Page.create", {"url": "https://www.1688.com/"})
            pdata = response_data(page) or {}
            page_id = pdata.get("pageId")
            if not page_id:
                raise RuntimeError("Page.create did not return pageId")
        state = await wait_settled(
            browser,
            page_id,
            "Confirm the 1688 home page is loaded before searching.",
            timeout=12,
        )
        if is_login_state(state):
            summary["blocked"] = "login_at_home"
            summary["state"] = response_data(state)
            print(json.dumps(summary, ensure_ascii=False, indent=2))
            return 2

        if is_search_results_state(state):
            log("Current page is already the requested 1688 search results page; skipping a fresh search submission.")
        else:
            state = await submit_search(browser, page_id)
        summary["searchState"] = response_data(state)
        if is_login_state(state):
            summary["blocked"] = "login_required_for_search_results"
            print(json.dumps(summary, ensure_ascii=False, indent=2))
            return 3

        products = await collect_search_products(browser, page_id, target_ranks)
        summary["searchProducts"] = products
        if len(products) < max(target_ranks):
            summary["blocked"] = "not_enough_search_results_visible"
            print(json.dumps(summary, ensure_ascii=False, indent=2))
            return 4

        selected = [item for item in products if int(item.get("rank", 0)) in target_ranks]
        for item in selected:
            log(f"Opening rank {item['rank']}: {item.get('title') or item.get('href')} [{item.get('href')}]")
            product = await open_product_and_extract(browser, item, args.fleet_id, page_id)
            if product.get("blocked"):
                summary["products"].append(product)
                continue
            saved = await save_product(browser, product)
            summary["products"].append(saved)

    summary["finishedAt"] = time.strftime("%Y-%m-%d %H:%M:%S")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(main()))
    except KeyboardInterrupt:
        raise SystemExit(130)
