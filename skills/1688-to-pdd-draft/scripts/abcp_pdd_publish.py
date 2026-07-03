#!/usr/bin/env python3
"""Assist publishing products on Pinduoduo merchant backend via ABCP Browser.

The script is intentionally phased. Login password and any ambiguous business
decisions remain human-in-the-loop; browser operations are performed directly
through ABCPClient, not through the harness.
"""

import argparse
import asyncio
import base64
import json
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT))

from abcp_client import ABCPClient, ABCPClientConfig  # noqa: E402


JsonDict = Dict[str, Any]
PDD_LOGIN_URL = "https://mms.pinduoduo.com/login/"
DESKTOP = Path("/Users/versace/Desktop")


PRODUCTS: Dict[int, Dict[str, Any]] = {
    6: {
        "folder": DESKTOP / "1688_汉服女装_第6名",
        "title": "唐制汉服女中国风齐胸襦裙飘逸仙气改良原创夏季古装薄款超仙全套",
        "brand": "玥珺",
        "fallback_brand": "其它",
        "fabric": "TR面料",
        "fabric_fallback": "其它",
        "age": "18-25周岁",
        "age_fallback": "青年（18-25周岁）",
        "popular": "绣花",
        "style": "唐制",
        "season": "2026年夏季",
        "article_no": "429881842435",
        "sizes": ["XS", "S", "M", "L", "XL"],
        "color": "齐胸套装",
    },
    7: {
        "folder": DESKTOP / "1688_汉服女装_第7名",
        "title": "新款汉服小唐风女成人唐制复原齐胸显瘦仙气古装国风日常春夏套装",
        "brand": "其它",
        "fallback_brand": "其它",
        "fabric": "涤纶",
        "fabric_fallback": "涤纶",
        "age": "18-25周岁",
        "age_fallback": "青年（18-25周岁）",
        "popular": "渐变色",
        "style": "唐制",
        "season": "2026年春季",
        "article_no": "202604122127",
        "sizes": ["S", "M", "L", "XL", "2XL"],
        "color": "琉璃上衣+裙子+披帛",
    },
}


def load_browser_config(agent_id_override: Optional[str] = None) -> Tuple[ABCPClientConfig, str]:
    raw = json.loads((REPO_ROOT / "config.json").read_text(encoding="utf-8"))
    browser_raw = raw.get("browser") or {}
    cfg = ABCPClientConfig.from_dict(browser_raw)
    return cfg, agent_id_override or browser_raw.get("agent_id") or "abcp-agent"


def data(resp: JsonDict) -> Any:
    return resp.get("data", resp)


def log(message: str) -> None:
    print(message, flush=True)


def count_current_from_upload_counts(counts: Any, total: int) -> int:
    if not isinstance(counts, dict):
        return 0
    direct = counts.get("detail" if total == 50 else "main")
    rows = counts.get("counts") if isinstance(counts.get("counts"), list) else []
    for item in [direct, *rows]:
        if isinstance(item, dict) and int(item.get("total") or 0) == total:
            return int(item.get("current") or 0)
        if isinstance(item, str):
            marker = f"/{total}张"
            if marker in item:
                digits = "".join(ch for ch in item.split("/", 1)[0] if ch.isdigit())
                if digits:
                    return int(digits)
    return 0


def product_payload(rank: int) -> Dict[str, Any]:
    if rank not in PRODUCTS:
        raise RuntimeError(f"Unsupported product rank: {rank}")
    item = dict(PRODUCTS[rank])
    folder = Path(item["folder"])
    item["main_files"] = sorted(str(p) for p in (folder / "pdd_upload").glob("main_image_*.jpg"))
    item["detail_files"] = sorted(str(p) for p in folder.glob("detail_image_*.jpg"))
    if not item["main_files"]:
        raise RuntimeError(f"No converted main images found for rank {rank}: {folder / 'pdd_upload'}")
    if not item["detail_files"]:
        raise RuntimeError(f"No detail images found for rank {rank}: {folder}")
    return item


def resolve_manifest_path(manifest: Optional[str], product_dir: Optional[str]) -> Path:
    if manifest:
        path = Path(manifest).expanduser()
    elif product_dir:
        path = Path(product_dir).expanduser() / "product_manifest.json"
    else:
        raise RuntimeError("Provide --manifest or --product-dir")
    if not path.exists():
        raise RuntimeError(f"Product manifest not found: {path}")
    return path


def load_manifest_product(manifest: Path) -> Dict[str, Any]:
    raw = json.loads(manifest.read_text(encoding="utf-8"))
    base_dir = manifest.parent
    source = raw.get("source") or {}
    pdd = raw.get("pdd") or {}
    attrs = pdd.get("attributes") or {}
    specs = pdd.get("specs") or {}
    price = pdd.get("price") or {}
    assets = pdd.get("assets") or {}

    def files_from(values: Any, fallback_glob: str = "") -> List[str]:
        rows = []
        if isinstance(values, list):
            for item in values:
                path = Path(str(item)).expanduser()
                if not path.is_absolute():
                    path = base_dir / path
                if path.exists():
                    rows.append(str(path))
        if not rows and fallback_glob:
            rows = sorted(str(path) for path in base_dir.glob(fallback_glob))
        return rows

    product = {
        "rank": int(source.get("rank") or 0),
        "folder": base_dir,
        "manifest": manifest,
        "title": str(pdd.get("title") or source.get("title") or "").strip(),
        "brand": str(attrs.get("品牌") or "其它"),
        "fallback_brand": "其它",
        "fabric": str(attrs.get("面料俗称") or attrs.get("面料名称") or "其它"),
        "fabric_fallback": "其它",
        "age": str(attrs.get("适用年龄") or "青年（18-25周岁）"),
        "age_fallback": "青年（18-25周岁）",
        "popular": str(attrs.get("流行元素") or "绣花"),
        "style": str(attrs.get("制式") or "其他"),
        "season": str(attrs.get("上市时节") or f"{time.strftime('%Y年')}春季"),
        "article_no": str(attrs.get("商品货号") or ""),
        "sizes": list(specs.get("sizes") or ["S", "M", "L", "XL"]),
        "color": str(specs.get("color") or "默认"),
        "stock": str(price.get("stock") or 500),
        "group_price": str(price.get("groupPrice") or 59),
        "single_price": str(price.get("singlePrice") or 69),
        "reference_price": str(price.get("referencePrice") or 89),
        "main_files": files_from(assets.get("pddMainImages"), "pdd_upload/main_image_*.jpg"),
        "detail_files": files_from(assets.get("detailImages"), "detail_image_*"),
    }
    if not product["title"]:
        raise RuntimeError(f"Manifest missing product title: {manifest}")
    if not product["main_files"]:
        raise RuntimeError(f"Manifest has no PDD-ready main images: {manifest}")
    return product


async def call(browser: ABCPClient, method: str, params: Optional[JsonDict] = None) -> JsonDict:
    resp = await browser.call(method, params or {})
    observation = str(resp.get("observation") or "")
    if observation:
        log(f"{method}: {observation[:260]}")
    return resp


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
    return data(resp)


async def get_or_create_page(browser: ABCPClient, page_id: Optional[str]) -> str:
    if page_id:
        await call(
            browser,
            "Page.switchTo",
            {
                "pageId": page_id,
                "purpose": "Use the existing Pinduoduo merchant backend tab for the user-approved publishing workflow.",
            },
        )
        return page_id
    page = await call(browser, "Page.create", {"url": PDD_LOGIN_URL})
    page_data = data(page) or {}
    created_page_id = page_data.get("pageId")
    if not created_page_id:
        raise RuntimeError("Page.create did not return pageId")
    return created_page_id


async def page_state(browser: ABCPClient, page_id: str, purpose: str) -> JsonDict:
    return await call(browser, "Page.getState", {"pageId": page_id, "purpose": purpose})


async def ax_tree(browser: ABCPClient, page_id: str, purpose: str) -> JsonDict:
    return await call(browser, "DOM.getAXTree", {"pageId": page_id, "purpose": purpose})


async def click_center(browser: ABCPClient, page_id: str, rect: JsonDict, purpose: str) -> None:
    x = float(rect["x"]) + float(rect["w"]) / 2
    y = float(rect["y"]) + float(rect["h"]) / 2
    await page_state(browser, page_id, f"Confirm page is stable before clicking: {purpose}")
    await call(
        browser,
        "Input.click",
        {
            "pageId": page_id,
            "x": x,
            "y": y,
            "clickCount": 1,
            "purpose": purpose,
        },
    )


async def click_text(browser: ABCPClient, page_id: str, text: str, purpose: str) -> bool:
    js = f"""
const targetText = {json.dumps(text, ensure_ascii=False)};
const clean = (s) => String(s || "").replace(/\\s+/g, " ").trim();
const visible = (el) => {{
  const r = el.getBoundingClientRect();
  const st = getComputedStyle(el);
  return r.width > 1 && r.height > 1 && st.visibility !== "hidden" && st.display !== "none";
}};
const candidates = Array.from(document.querySelectorAll("button,a,div,span,[role='button'],li"))
  .filter((el) => visible(el))
  .map((el, i) => {{
    const r = el.getBoundingClientRect();
    const t = clean(el.innerText || el.textContent || el.getAttribute("aria-label") || "");
    return {{
      i,
      tag: el.tagName,
      text: t,
      className: String(el.className || ""),
      role: el.getAttribute("role") || "",
      rect: {{x: r.x, y: r.y, w: r.width, h: r.height}},
      exact: t === targetText,
      contains: t.includes(targetText)
    }};
  }})
  .filter((item) => item.exact || item.contains)
  .sort((a, b) => (b.exact - a.exact) || (a.rect.w * a.rect.h - b.rect.w * b.rect.h));
return candidates.slice(0, 10);
"""
    candidates = await eval_js(
        browser,
        page_id,
        js,
        f"Locate visible text {text} because AXTree may expose non-actionable text without its clickable container.",
    )
    if not isinstance(candidates, list) or not candidates:
        log(f"No visible candidate found for text: {text}")
        return False
    log(f"Clicking text candidate for {text}: {candidates[0]}")
    await click_center(browser, page_id, candidates[0]["rect"], purpose)
    return True


async def probe_publish_entries(browser: ABCPClient, page_id: str) -> JsonDict:
    js = r"""
const clean = (s) => String(s || "").replace(/\s+/g, " ").trim();
const visible = (el) => {
  const r = el.getBoundingClientRect();
  const st = getComputedStyle(el);
  return r.width > 1 && r.height > 1 && st.visibility !== "hidden" && st.display !== "none";
};
const cssPath = (el) => {
  const parts = [];
  for (let cur = el; cur && cur.nodeType === 1 && parts.length < 6; cur = cur.parentElement) {
    let part = cur.tagName.toLowerCase();
    if (cur.id) {
      part += "#" + cur.id;
      parts.unshift(part);
      break;
    }
    const role = cur.getAttribute("role");
    if (role) part += `[role="${role}"]`;
    const cls = String(cur.className || "").split(/\s+/).filter(Boolean).slice(0, 2);
    if (cls.length) part += "." + cls.join(".");
    parts.unshift(part);
  }
  return parts.join(" > ");
};
const publishRe = /发布新商品|立即发布|商品发布|发布商品|publish|goods/i;
const rows = [];
Array.from(document.querySelectorAll("a,button,[role='button'],li,div,span")).forEach((el, i) => {
  const text = clean(el.innerText || el.textContent || el.getAttribute("aria-label") || "");
  const href = el.href || el.getAttribute("href") || "";
  const anchor = el.closest("a");
  const anchorHref = anchor ? (anchor.href || anchor.getAttribute("href") || "") : "";
  const cls = String(el.className || "");
  if (!publishRe.test(`${text} ${href} ${anchorHref} ${cls}`)) return;
  const r = el.getBoundingClientRect();
  rows.push({
    i,
    tag: el.tagName,
    role: el.getAttribute("role") || "",
    text: text.slice(0, 180),
    href,
    anchorHref,
    cls: cls.slice(0, 180),
    path: cssPath(el),
    rect: {x: Math.round(r.x), y: Math.round(r.y), w: Math.round(r.width), h: Math.round(r.height)},
    visible: visible(el),
    area: Math.round(r.width * r.height)
  });
});
rows.sort((a, b) => Number(b.visible) - Number(a.visible) || a.area - b.area || a.rect.y - b.rect.y);
return {
  title: document.title,
  url: location.href,
  hash: location.hash,
  pathname: location.pathname,
  viewport: {w: innerWidth, h: innerHeight, sx: scrollX, sy: scrollY},
  candidates: rows.slice(0, 80)
};
"""
    result = await eval_js(
        browser,
        page_id,
        js,
        "Read publish-entry DOM candidates and route attributes because visible shortcut clicks did not navigate.",
    )
    if not isinstance(result, dict):
        raise RuntimeError(f"Unexpected publish-entry probe result: {result}")
    return result


async def open_publish_entry(browser: ABCPClient, page_id: str) -> JsonDict:
    await page_state(
        browser,
        page_id,
        "Confirm Pinduoduo merchant home page is stable before opening the publish-product workflow.",
    )
    await ax_tree(
        browser,
        page_id,
        "Refresh the page accessibility map before selecting the publish-product workflow entry.",
    )
    js = r"""
const clean = (s) => String(s || "").replace(/\s+/g, " ").trim();
const visible = (el) => {
  const r = el.getBoundingClientRect();
  const st = getComputedStyle(el);
  return r.width > 1 && r.height > 1 && st.visibility !== "hidden" && st.display !== "none";
};
const candidates = Array.from(document.querySelectorAll("a,button,[role='button'],li,div,span"))
  .map((el, i) => {
    const text = clean(el.innerText || el.textContent || el.getAttribute("aria-label") || "");
    const anchor = el.closest("a");
    const href = el.href || el.getAttribute("href") || "";
    const anchorHref = anchor ? (anchor.href || anchor.getAttribute("href") || "") : "";
    const r = el.getBoundingClientRect();
    const exactNew = text === "发布新商品";
    const exactNow = text === "立即发布";
    const contains = /发布新商品|立即发布|商品发布|发布商品/.test(text);
    const hrefHint = /publish|goods|product|category/i.test(`${href} ${anchorHref}`);
    const clickableTag = /^(A|BUTTON)$/i.test(el.tagName) || el.getAttribute("role") === "button";
    return {
      el,
      i,
      tag: el.tagName,
      text,
      href,
      anchorHref,
      visible: visible(el),
      rect: {x: Math.round(r.x), y: Math.round(r.y), w: Math.round(r.width), h: Math.round(r.height)},
      score:
        (exactNew ? 80 : 0) +
        (exactNow ? 70 : 0) +
        (contains ? 20 : 0) +
        (hrefHint ? 15 : 0) +
        (clickableTag ? 10 : 0) +
        (r.y >= 0 && r.y <= innerHeight ? 6 : 0) -
        Math.min(30, Math.round((r.width * r.height) / 20000))
    };
  })
  .filter((item) => item.visible && item.score > 0)
  .sort((a, b) => b.score - a.score || a.rect.y - b.rect.y);
const chosen = candidates[0];
if (!chosen) return {ok: false, reason: "no-candidate", url: location.href, candidates: []};
const urlBefore = location.href;
const target = chosen.el.closest("a,button,[role='button']") || chosen.el;
const chosenInfo = {
  i: chosen.i,
  tag: chosen.tag,
  text: chosen.text.slice(0, 120),
  href: chosen.href,
  anchorHref: chosen.anchorHref,
  rect: chosen.rect,
  score: chosen.score,
  targetTag: target.tagName,
  targetHref: target.href || target.getAttribute("href") || ""
};
if (chosenInfo.targetHref && !/^javascript:/i.test(chosenInfo.targetHref)) {
  location.href = chosenInfo.targetHref;
  return {ok: true, mode: "href", urlBefore, chosen: chosenInfo};
}
target.click();
return {ok: true, mode: "dom-click", urlBefore, chosen: chosenInfo, urlAfterImmediate: location.href};
"""
    opened = await eval_js(
        browser,
        page_id,
        js,
        "Open the Pinduoduo publish-product workflow using the most specific visible publish entry after structured clicks did not navigate.",
    )
    await asyncio.sleep(1.5)
    state = await page_state(
        browser,
        page_id,
        "Check the page state after opening the publish-product workflow entry.",
    )
    return {"opened": opened, "state": data(state)}


async def probe_category_page(browser: ABCPClient, page_id: str) -> JsonDict:
    js = r"""
const clean = (s) => String(s || "").replace(/\s+/g, " ").trim();
const visible = (el) => {
  const r = el.getBoundingClientRect();
  const st = getComputedStyle(el);
  return r.width > 1 && r.height > 1 && st.visibility !== "hidden" && st.display !== "none";
};
const rows = [];
Array.from(document.querySelectorAll("input,textarea,button,a,li,div,span,[role='button'],[class*='category'],[class*='Category'],[class*='search'],[class*='Search']")).forEach((el, i) => {
  const t = clean(el.innerText || el.textContent || el.placeholder || el.getAttribute("aria-label") || "");
  const cls = String(el.className || "");
  const href = el.href || el.getAttribute("href") || "";
  const r = el.getBoundingClientRect();
  if (/汉服|女装|服装|分类|类目|搜索|确认|家居|虚拟|数码|美容|请输入|套装/.test(`${t} ${cls} ${href}`)) {
    rows.push({
      i,
      tag: el.tagName,
      type: el.type || "",
      text: t.slice(0, 180),
      value: el.value || "",
      placeholder: el.placeholder || "",
      href,
      cls: cls.slice(0, 180),
      role: el.getAttribute("role") || "",
      visible: visible(el),
      rect: {x: Math.round(r.x), y: Math.round(r.y), w: Math.round(r.width), h: Math.round(r.height)},
      html: el.outerHTML.slice(0, 320)
    });
  }
});
return {
  url: location.href,
  title: document.title,
  active: document.activeElement ? {
    tag: document.activeElement.tagName,
    value: document.activeElement.value || "",
    placeholder: document.activeElement.placeholder || "",
    cls: String(document.activeElement.className || "").slice(0, 160)
  } : null,
  viewport: {w: innerWidth, h: innerHeight, sx: scrollX, sy: scrollY},
  rows: rows.slice(0, 180),
  bodyText: clean(document.body.innerText).slice(0, 5000)
};
"""
    result = await eval_js(
        browser,
        page_id,
        js,
        "Inspect category-selection DOM structure to determine how search results and category paths are represented.",
    )
    if not isinstance(result, dict):
        raise RuntimeError(f"Unexpected category probe result: {result}")
    return result


async def probe_product_form(browser: ABCPClient, page_id: str) -> JsonDict:
    js = r"""
const clean = (s) => String(s || "").replace(/\s+/g, " ").trim();
const visible = (el) => {
  const r = el.getBoundingClientRect();
  const st = getComputedStyle(el);
  return r.width > 1 && r.height > 1 && st.visibility !== "hidden" && st.display !== "none";
};
const around = (el) => clean((el.closest("[class*='form'],[class*='Form'],[class*='item'],[class*='Item'],tr,section,div") || el.parentElement || el).innerText).slice(0, 260);
const inputs = Array.from(document.querySelectorAll("input,textarea,[contenteditable='true']")).map((el, i) => {
  const r = el.getBoundingClientRect();
  return {
    i,
    tag: el.tagName,
    type: el.type || "",
    value: el.value || "",
    placeholder: el.placeholder || "",
    aria: el.getAttribute("aria-label") || "",
    name: el.name || "",
    cls: String(el.className || "").slice(0, 150),
    visible: visible(el),
    rect: {x: Math.round(r.x), y: Math.round(r.y), w: Math.round(r.width), h: Math.round(r.height)},
    context: around(el)
  };
}).filter((row) => row.visible || /商品标题|品牌|请选择|请输入|规格|库存|价格|参考价/.test(`${row.placeholder} ${row.value} ${row.context}`));
const buttons = Array.from(document.querySelectorAll("button,a,[role='button'],span")).map((el, i) => {
  const text = clean(el.innerText || el.textContent || el.getAttribute("aria-label") || "");
  const r = el.getBoundingClientRect();
  return {
    i,
    tag: el.tagName,
    type: el.type || "",
    text: text.slice(0, 120),
    cls: String(el.className || "").slice(0, 150),
    testid: el.getAttribute("data-testid") || "",
    e2e: el.getAttribute("data-e2e-id") || "",
    visible: visible(el),
    rect: {x: Math.round(r.x), y: Math.round(r.y), w: Math.round(r.width), h: Math.round(r.height)},
    context: around(el)
  };
}).filter((row) => /上传|提交|保存|知道了|添加|排序|全屏|图片|视频|装修/.test(`${row.text} ${row.context} ${row.e2e}`));
const fileInputs = Array.from(document.querySelectorAll("input[type='file']")).map((el, i) => {
  const r = el.getBoundingClientRect();
  return {
    i,
    accept: el.accept || "",
    multiple: Boolean(el.multiple),
    name: el.name || "",
    cls: String(el.className || "").slice(0, 150),
    visible: visible(el),
    rect: {x: Math.round(r.x), y: Math.round(r.y), w: Math.round(r.width), h: Math.round(r.height)},
    context: around(el)
  };
});
return {
  url: location.href,
  title: document.title,
  scroll: {x: scrollX, y: scrollY},
  bodyText: clean(document.body.innerText).slice(0, 6000),
  inputs,
  buttons,
  fileInputs
};
"""
    result = await eval_js(
        browser,
        page_id,
        js,
        "Inspect the Pinduoduo product form inputs, buttons, and file inputs before filling listing data.",
    )
    if not isinstance(result, dict):
        raise RuntimeError(f"Unexpected product form probe result: {result}")
    return result


async def click_file_input(browser: ABCPClient, page_id: str, file_input_index: int, purpose: str) -> JsonDict:
    js = f"""
const index = {file_input_index};
const inputs = Array.from(document.querySelectorAll("input[type='file']"));
const el = inputs[index];
if (!el) return {{ok: false, reason: "missing-file-input", count: inputs.length}};
const r = el.getBoundingClientRect();
el.click();
return {{
  ok: true,
  index,
  count: inputs.length,
  accept: el.accept || "",
  multiple: !!el.multiple,
  rect: {{x: Math.round(r.x), y: Math.round(r.y), w: Math.round(r.width), h: Math.round(r.height)}}
}};
"""
    result = await eval_js(browser, page_id, js, purpose)
    if not isinstance(result, dict) or not result.get("ok"):
        raise RuntimeError(f"Could not open file input {file_input_index}: {result}")
    await asyncio.sleep(0.4)
    return result


async def find_image_file_input(browser: ABCPClient, page_id: str) -> int:
    js = r"""
const rows = Array.from(document.querySelectorAll("input[type='file']")).map((el, index) => {
  const r = el.getBoundingClientRect();
  const accept = el.accept || "";
  return {
    index,
    accept,
    image: /image\/(jpeg|jpg|png)|image/i.test(accept),
    rect: {x: r.x, y: r.y, w: r.width, h: r.height, bottom: r.bottom},
    inViewport: r.bottom > 0 && r.right > 0 && r.top < innerHeight && r.left < innerWidth
  };
}).filter((row) => row.image);
rows.sort((a, b) => Number(b.inViewport) - Number(a.inViewport) || b.rect.y - a.rect.y || a.index - b.index);
return rows;
"""
    rows = await eval_js(
        browser,
        page_id,
        js,
        "Locate the current visible image file input as a fallback for Pinduoduo detail image upload.",
    )
    if not isinstance(rows, list) or not rows:
        raise RuntimeError(f"Could not locate any image file input. Result: {rows}")
    return int(rows[0]["index"])


async def upload_files(browser: ABCPClient, page_id: str, file_input_index: int, files: List[str], purpose: str) -> None:
    if not files:
        raise RuntimeError(f"No files to upload for: {purpose}")
    clicked = await click_file_input(browser, page_id, file_input_index, purpose)
    log(f"Opened file input: {clicked}")
    await page_state(browser, page_id, f"Confirm file chooser is pending before uploading {len(files)} files.")
    await call(
        browser,
        "File.handleChooser",
        {
            "pageId": page_id,
            "accept": True,
            "files": files,
            "purpose": purpose,
        },
    )


async def inject_files_into_image_input(browser: ABCPClient, page_id: str, files: List[str], purpose: str) -> JsonDict:
    payload = []
    for file_path in files:
        path = Path(file_path)
        payload.append(
            {
                "name": path.name,
                "type": "image/png" if path.suffix.lower() == ".png" else "image/jpeg",
                "base64": base64.b64encode(path.read_bytes()).decode("ascii"),
            }
        )
    js = f"""
const payload = {json.dumps(payload)};
const imageInputs = Array.from(document.querySelectorAll("input[type='file']")).map((el, index) => {{
  const r = el.getBoundingClientRect();
  return {{
    el,
    index,
    accept: el.accept || "",
    image: /image\\//i.test(el.accept || ""),
    rect: {{x: r.x, y: r.y, w: r.width, h: r.height, bottom: r.bottom}},
    inViewport: r.bottom > 0 && r.right > 0 && r.top < innerHeight && r.left < innerWidth
  }};
}}).filter((row) => row.image);
imageInputs.sort((a, b) => Number(b.inViewport) - Number(a.inViewport) || b.rect.y - a.rect.y || a.index - b.index);
const target = imageInputs[0];
if (!target) return {{ok: false, reason: "no-image-input"}};
const dt = new DataTransfer();
for (const item of payload) {{
  const binary = atob(item.base64);
  const bytes = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);
  dt.items.add(new File([bytes], item.name, {{type: item.type}}));
}}
try {{
  target.el.files = dt.files;
}} catch (err) {{
  Object.defineProperty(target.el, "files", {{value: dt.files, configurable: true}});
}}
target.el.dispatchEvent(new Event("input", {{bubbles: true}}));
target.el.dispatchEvent(new Event("change", {{bubbles: true}}));
return {{
  ok: true,
  inputIndex: target.index,
  fileCount: dt.files.length,
  names: Array.from(dt.files).map((file) => file.name),
  rect: target.rect
}};
"""
    result = await eval_js(browser, page_id, js, purpose)
    if not isinstance(result, dict) or not result.get("ok"):
        raise RuntimeError(f"Could not inject image files into Pinduoduo file input: {result}")
    return result


async def inject_file_into_sku_preview(browser: ABCPClient, page_id: str, file_path: str) -> JsonDict:
    path = Path(file_path)
    payload = {
        "name": path.name,
        "type": "image/png" if path.suffix.lower() == ".png" else "image/jpeg",
        "base64": base64.b64encode(path.read_bytes()).decode("ascii"),
    }
    js = f"""
const item = {json.dumps(payload)};
const cells = Array.from(document.querySelectorAll(".sku-preview-cell"));
const target = cells.map((cell, cellIndex) => {{
  const input = cell.querySelector("input[type='file']");
  const r = cell.getBoundingClientRect();
  return {{cell, cellIndex, input, rect: {{x:r.x,y:r.y,w:r.width,h:r.height}}, text: String(cell.innerText || cell.textContent || "")}};
}}).find((row) => row.input);
if (!target) return {{ok:false, reason:"missing-sku-preview-file-input", cellCount: cells.length}};
const binary = atob(item.base64);
const bytes = new Uint8Array(binary.length);
for (let i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);
const dt = new DataTransfer();
dt.items.add(new File([bytes], item.name, {{type: item.type}}));
try {{
  target.input.files = dt.files;
}} catch (err) {{
  Object.defineProperty(target.input, "files", {{value: dt.files, configurable: true}});
}}
target.input.dispatchEvent(new Event("input", {{bubbles: true}}));
target.input.dispatchEvent(new Event("change", {{bubbles: true}}));
return {{ok:true, cellIndex: target.cellIndex, fileName: item.name, fileCount: dt.files.length, rect: target.rect, previousText: target.text}};
"""
    result = await eval_js(
        browser,
        page_id,
        js,
        "Programmatically provide the prepared main image to the SKU preview file input after native chooser handling failed.",
    )
    if not isinstance(result, dict) or not result.get("ok"):
        raise RuntimeError(f"Could not inject SKU preview image: {result}")
    return result


async def inject_file_into_image_space_upload(browser: ABCPClient, page_id: str, file_path: str) -> JsonDict:
    path = Path(file_path)
    payload = {
        "name": path.name,
        "type": "image/png" if path.suffix.lower() == ".png" else "image/jpeg",
        "base64": base64.b64encode(path.read_bytes()).decode("ascii"),
    }
    js = f"""
const item = {json.dumps(payload)};
const target = Array.from(document.querySelectorAll("input[type='file']"))
  .find((input) => /\\.jpg|\\.jpeg|\\.png|image\\//i.test(input.accept || "") && String((input.closest("div") || input).innerText || "").includes("上传文件夹"));
if (!target) return {{ok:false, reason:"missing-image-space-upload-input", count: document.querySelectorAll("input[type='file']").length}};
const binary = atob(item.base64);
const bytes = new Uint8Array(binary.length);
for (let i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);
const dt = new DataTransfer();
dt.items.add(new File([bytes], item.name, {{type: item.type}}));
try {{
  target.files = dt.files;
}} catch (err) {{
  Object.defineProperty(target, "files", {{value: dt.files, configurable: true}});
}}
target.dispatchEvent(new Event("input", {{bubbles: true}}));
target.dispatchEvent(new Event("change", {{bubbles: true}}));
return {{ok:true, fileName:item.name, fileCount:dt.files.length, accept:target.accept || ""}};
"""
    result = await eval_js(
        browser,
        page_id,
        js,
        "Programmatically provide the prepared main image to the Pinduoduo image-space upload panel file input.",
    )
    if not isinstance(result, dict) or not result.get("ok"):
        raise RuntimeError(f"Could not inject image-space upload file: {result}")
    return result


async def drop_file_into_image_space_upload(browser: ABCPClient, page_id: str, file_path: str) -> JsonDict:
    path = Path(file_path)
    payload = {
        "name": path.name,
        "type": "image/png" if path.suffix.lower() == ".png" else "image/jpeg",
        "base64": base64.b64encode(path.read_bytes()).decode("ascii"),
    }
    js = f"""
const item = {json.dumps(payload)};
window.__codexUploadLog = window.__codexUploadLog || [];
if (!window.__codexUploadPatched) {{
  window.__codexUploadPatched = true;
  const originalFetch = window.fetch;
  window.fetch = function(...args) {{
    try {{ window.__codexUploadLog.push({{kind: "fetch", url: String(args[0] && (args[0].url || args[0])), ts: Date.now()}}); }} catch (err) {{}}
    return originalFetch.apply(this, args);
  }};
  const originalOpen = XMLHttpRequest.prototype.open;
  XMLHttpRequest.prototype.open = function(method, url, ...rest) {{
    try {{
      this.__codexUploadUrl = String(url);
      window.__codexUploadLog.push({{kind: "xhr-open", method: String(method), url: String(url), ts: Date.now()}});
    }} catch (err) {{}}
    return originalOpen.call(this, method, url, ...rest);
  }};
}}
const clean = (s) => String(s || "").replace(/\\s+/g, " ").trim();
const targetInput = Array.from(document.querySelectorAll("input[type='file']"))
  .find((input) => (input.accept || "") === ".jpg,.jpeg,.png");
if (!targetInput) return {{ok:false, reason:"missing-image-space-upload-input", fileInputs: document.querySelectorAll("input[type='file']").length}};
const binary = atob(item.base64);
const bytes = new Uint8Array(binary.length);
for (let i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);
const file = new File([bytes], item.name, {{type: item.type, lastModified: Date.now()}});
const dt = new DataTransfer();
dt.items.add(file);
try {{
  targetInput.files = dt.files;
}} catch (err) {{
  Object.defineProperty(targetInput, "files", {{value: dt.files, configurable: true}});
}}
const dropzone = targetInput.parentElement;
const uploadArea = dropzone && (dropzone.querySelector("[class*='upload--select-file-area']") || dropzone);
const targets = [targetInput, uploadArea, dropzone].filter(Boolean);
const dispatched = [];
const fire = (target, type, drag) => {{
  let event;
  const base = {{bubbles: true, cancelable: true, composed: true}};
  if (drag) {{
    try {{
      event = new DragEvent(type, {{...base, dataTransfer: dt, clientX: 612, clientY: 407}});
    }} catch (err) {{
      event = new Event(type, base);
      Object.defineProperty(event, "dataTransfer", {{value: dt}});
    }}
  }} else {{
    event = new Event(type, base);
  }}
  try {{ Object.defineProperty(event, "target", {{value: targetInput, configurable: true}}); }} catch (err) {{}}
  try {{ Object.defineProperty(event, "currentTarget", {{value: target, configurable: true}}); }} catch (err) {{}}
  const result = target.dispatchEvent(event);
  dispatched.push({{tag: target.tagName, type, result}});
}};
try {{ dropzone && dropzone.focus && dropzone.focus(); }} catch (err) {{}}
for (const target of targets) {{
  for (const type of ["dragenter", "dragover", "drop"]) fire(target, type, true);
}}
for (const type of ["input", "change"]) fire(targetInput, type, false);
const info = (el) => {{
  if (!el) return null;
  const r = el.getBoundingClientRect();
  return {{
    tag: el.tagName,
    text: clean(el.innerText || el.textContent).slice(0, 180),
    cls: String(el.className || "").slice(0, 180),
    rect: {{x: Math.round(r.x), y: Math.round(r.y), w: Math.round(r.width), h: Math.round(r.height)}}
  }};
}};
return {{
  ok: true,
  fileName: file.name,
  fileCount: targetInput.files.length,
  inputAccept: targetInput.accept || "",
  dropzone: info(dropzone),
  uploadArea: info(uploadArea),
  dispatched,
  uploadLog: (window.__codexUploadLog || []).slice(-20),
  bodyTail: clean(document.body.innerText).slice(-1600)
}};
"""
    result = await eval_js(
        browser,
        page_id,
        js,
        "Dispatch the prepared preview image through the Pinduoduo image-space upload input and dropzone after native chooser clicks did not open a browser file chooser.",
    )
    if not isinstance(result, dict) or not result.get("ok"):
        raise RuntimeError(f"Could not drop image-space upload file: {result}")
    return result


async def clean_change_image_space_upload(browser: ABCPClient, page_id: str, file_path: str) -> JsonDict:
    path = Path(file_path)
    payload = {
        "name": path.name,
        "type": "image/png" if path.suffix.lower() == ".png" else "image/jpeg",
        "base64": base64.b64encode(path.read_bytes()).decode("ascii"),
    }
    js = f"""
const item = {json.dumps(payload)};
window.__codexCleanUploadLog = [];
if (!window.__codexCleanUploadPatched) {{
  window.__codexCleanUploadPatched = true;
  const originalFetch = window.fetch;
  window.fetch = function(...args) {{
    try {{ window.__codexCleanUploadLog.push({{kind:"fetch", url:String(args[0] && (args[0].url || args[0])), ts:Date.now()}}); }} catch (err) {{}}
    return originalFetch.apply(this, args);
  }};
  const originalOpen = XMLHttpRequest.prototype.open;
  XMLHttpRequest.prototype.open = function(method, url, ...rest) {{
    try {{ window.__codexCleanUploadLog.push({{kind:"xhr-open", method:String(method), url:String(url), ts:Date.now()}}); }} catch (err) {{}}
    return originalOpen.call(this, method, url, ...rest);
  }};
}}
const input = Array.from(document.querySelectorAll("input[type='file']")).find((el) => (el.accept || "") === ".jpg,.jpeg,.png");
if (!input) return {{ok:false, reason:"missing-image-space-input"}};
const binary = atob(item.base64);
const bytes = new Uint8Array(binary.length);
for (let i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);
const file = new File([bytes], item.name, {{type:item.type, lastModified:Date.now()}});
const dt = new DataTransfer();
dt.items.add(file);
input.value = "";
try {{ input.files = dt.files; }} catch (err) {{ Object.defineProperty(input, "files", {{value: dt.files, configurable: true}}); }}
const change = new Event("change", {{bubbles:true, cancelable:true, composed:true}});
const inputEvent = new Event("input", {{bubbles:true, cancelable:true, composed:true}});
const changeResult = input.dispatchEvent(change);
const inputResult = input.dispatchEvent(inputEvent);
return {{
  ok:true,
  fileCount: input.files.length,
  changeResult,
  inputResult,
  uploadLog: window.__codexCleanUploadLog.slice(-20),
  bodyTail: String(document.body.innerText || "").replace(/\\s+/g, " ").slice(-1600)
}};
"""
    result = await eval_js(
        browser,
        page_id,
        js,
        "Trigger the Pinduoduo image-space upload input with a clean change event after assigning the prepared file.",
    )
    if not isinstance(result, dict) or not result.get("ok"):
        raise RuntimeError(f"Could not clean-change image-space upload file: {result}")
    return result


async def select_uploaded_image_space_preview(browser: ABCPClient, page_id: str) -> JsonDict:
    js = r"""
const clean = (s) => String(s || "").replace(/\s+/g, " ").trim();
const visible = (el) => {
  const r = el.getBoundingClientRect();
  const st = getComputedStyle(el);
  return r.width > 1 && r.height > 1 && r.bottom > 0 && r.right > 0 && r.top < innerHeight && r.left < innerWidth && st.display !== "none" && st.visibility !== "hidden";
};
const rows = [];
Array.from(document.querySelectorAll("img,div,button,span")).forEach((el, index) => {
  const r = el.getBoundingClientRect();
  const text = clean(el.innerText || el.textContent || el.getAttribute("alt") || el.getAttribute("title") || "");
  const src = el.currentSrc || el.src || "";
  if (!visible(el)) return;
  if (!/main_image|上传成功|jpg|jpeg|png|预览|确认|取消|已选|暂无可用图片|全部文件/.test(`${text} ${src}`)) return;
  rows.push({
    index,
    tag: el.tagName,
    text: text.slice(0, 160),
    src: src.slice(0, 240),
    cls: String(el.className || "").slice(0, 160),
    rect: {x: Math.round(r.x), y: Math.round(r.y), w: Math.round(r.width), h: Math.round(r.height)}
  });
});
return {
  uploadLog: (window.__codexUploadLog || []).slice(-30),
  rows,
  bodyTail: clean(document.body.innerText).slice(-2200)
};
"""
    result = await eval_js(
        browser,
        page_id,
        js,
        "Inspect image-space results after dispatching the prepared preview image so it can be selected and confirmed.",
    )
    if not isinstance(result, dict):
        raise RuntimeError(f"Unexpected image-space preview probe: {result}")
    return result


async def upload_image_space_material_direct(browser: ABCPClient, page_id: str, file_path: str) -> JsonDict:
    path = Path(file_path)
    suffix = path.suffix.lower().lstrip(".") or "jpg"
    extension = "jpeg" if suffix in {"jpg", "jpeg"} else suffix
    payload = {
        "name": path.name,
        "createName": f"{path.stem}.{extension}",
        "extension": extension,
        "type": "image/png" if extension == "png" else "image/jpeg",
        "base64": base64.b64encode(path.read_bytes()).decode("ascii"),
    }
    js = f"""
const item = {json.dumps(payload)};
const decode = (b64) => {{
  const binary = atob(b64);
  const bytes = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);
  return bytes;
}};
const parseResponse = async (response) => {{
  const text = await response.text();
  let parsed;
  try {{ parsed = JSON.parse(text); }} catch (err) {{ parsed = {{raw: text.slice(0, 1000)}}; }}
  if (!response.ok) throw new Error(`HTTP ${{response.status}} ${{text.slice(0, 300)}}`);
  return parsed && Object.prototype.hasOwnProperty.call(parsed, "result") ? parsed.result : parsed;
}};
const postJson = async (url, body) => parseResponse(await fetch(url, {{
  method: "POST",
  credentials: "include",
  headers: {{"content-type": "application/json"}},
  body: JSON.stringify(body)
}}));
const bucketTag = "mms-material-img";
const signatureResult = await postJson("/galerie/business/get_signature", {{bucket_tag: bucketTag}});
const signature = signatureResult.signature || signatureResult;
if (!signature || typeof signature !== "string") throw new Error("Missing upload signature");
const endpointResult = await postJson("https://file.pinduoduo.com/api/galerie/get_endpoint", {{bucket_tag: bucketTag}});
const endpoint = endpointResult.endpoint || "file.pinduoduo.com";
const file = new File([decode(item.base64)], item.name, {{type: item.type, lastModified: Date.now()}});
const form = new FormData();
form.append("upload_sign", signature);
form.append("image", file, item.name.toLowerCase());
if (item.extension !== "gif") {{
  const quality = item.extension === "png" ? 90 : 80;
  form.append("pic_operations", JSON.stringify({{rules:[{{rule:`imageMogr2/format/jpeg/quality/${{quality}}|imageView2/2/w/2000/h/2000`}}]}}));
}}
const uploadResult = await parseResponse(await fetch(`https://${{endpoint}}/v3/store_image`, {{
  method: "POST",
  credentials: "include",
  body: form
}}));
const uploadedUrl = uploadResult.url || uploadResult.processed_url || uploadResult.download_url;
if (!uploadedUrl) throw new Error(`Upload did not return url: ${{JSON.stringify(uploadResult).slice(0, 400)}}`);
const normalizedUrl = String(uploadedUrl).replace(/^http:/, location.protocol);
const createResult = await postJson("/garner/mms/file/create", {{
  url: normalizedUrl,
  extension: item.extension,
  name: item.createName
}});
const fileId = createResult.file_id || createResult.id || createResult;
const listResult = await postJson("/garner/mms/file/list", {{
  order_by: "create_time desc",
  page: 1,
  page_size: 10,
  check_status_list: [2]
}});
return {{
  ok: true,
  signaturePrefix: signature.slice(0, 16),
  endpoint,
  uploadResult,
  normalizedUrl,
  createResult,
  fileId,
  listFirst: Array.isArray(listResult.list) ? listResult.list.slice(0, 5) : listResult,
  total: listResult.total
}};
"""
    result = await eval_js(
        browser,
        page_id,
        js,
        "Upload the prepared preview image directly through Pinduoduo's authenticated material APIs after UI file selection could not be triggered.",
    )
    if not isinstance(result, dict) or not result.get("ok"):
        raise RuntimeError(f"Could not direct-upload image-space material: {result}")
    return result


async def retry_image_space_create(browser: ABCPClient, page_id: str, image_url: str, rank: int) -> JsonDict:
    product = PRODUCTS[rank]
    payload = {
        "url": image_url,
        "extension": "jpeg",
        "name": f"main_image_01_{rank}_{int(time.time())}.jpeg",
        "title": product["title"],
    }
    js = f"""
const item = {json.dumps(payload, ensure_ascii=False)};
const parseResponse = async (response) => {{
  const text = await response.text();
  let parsed;
  try {{ parsed = JSON.parse(text); }} catch (err) {{ parsed = {{raw: text.slice(0, 1000)}}; }}
  return {{ok: response.ok, status: response.status, parsed}};
}};
const postJson = async (url, body) => parseResponse(await fetch(url, {{
  method: "POST",
  credentials: "include",
  headers: {{"content-type": "application/json"}},
  body: JSON.stringify(body)
}}));
const createPayloads = [
  {{url: item.url, extension: item.extension, name: item.name}},
  {{url: item.url, extension: item.extension, name: item.name.replace(/\\.jpeg$/, "")}},
];
const creates = [];
for (const payload of createPayloads) {{
  creates.push({{payload, response: await postJson("/garner/mms/file/create", payload)}});
  await new Promise(resolve => setTimeout(resolve, 800));
}}
const listPayloads = [
  {{order_by: "create_time desc", page: 1, page_size: 20, check_status_list: [2]}},
  {{order_by: "create_time desc", page: 1, page_size: 20}},
];
const lists = [];
for (const payload of listPayloads) {{
  lists.push({{payload, response: await postJson("/garner/mms/file/list", payload)}});
}}
const text = String(document.body.innerText || "").replace(/\\s+/g, " ").trim();
return {{
  ok: true,
  item,
  creates,
  lists,
  pageTextSample: text.slice(Math.max(0, text.indexOf("图片空间") - 80), Math.max(0, text.indexOf("图片空间") - 80) + 700)
}};
"""
    result = await eval_js(
        browser,
        page_id,
        js,
        "Retry registering the already-uploaded SKU preview image in Pinduoduo image space and inspect the material list.",
    )
    if not isinstance(result, dict):
        raise RuntimeError(f"Unexpected retry image-space create result: {result}")
    return result


async def body_text(browser: ABCPClient, page_id: str, purpose: str) -> str:
    result = await eval_js(
        browser,
        page_id,
        'return String(document.body.innerText || "").replace(/\\s+/g, " ").trim();',
        purpose,
    )
    return str(result or "")


async def wait_for_text(browser: ABCPClient, page_id: str, needle: str, timeout: float, purpose: str) -> bool:
    end = time.time() + timeout
    last = ""
    while time.time() < end:
        last = await body_text(browser, page_id, purpose)
        if needle in last:
            return True
        await asyncio.sleep(1.0)
    log(f"Timed out waiting for text {needle!r}. Last body sample: {last[:500]}")
    return False


async def upload_trigger_candidates(browser: ABCPClient, page_id: str) -> List[JsonDict]:
    js = r"""
const clean = (s) => String(s || "").replace(/\s+/g, " ").trim();
const rows = [];
Array.from(document.querySelectorAll("div,span,button,a,input,label")).forEach((el, i) => {
  const text = clean(el.innerText || el.textContent || el.placeholder || "");
  const accept = el.accept || "";
  if (!/上传图片|本地上传|图片空间上传/.test(`${text} ${accept}`)) return;
  const r = el.getBoundingClientRect();
  const st = getComputedStyle(el);
  rows.push({
    i,
    tag: el.tagName,
    text: text.slice(0, 140),
    type: el.type || "",
    accept,
    cls: String(el.className || "").slice(0, 160),
    visible: r.width > 0 && r.height > 0 && st.visibility !== "hidden" && st.display !== "none",
    inViewport: r.bottom > 0 && r.right > 0 && r.top < innerHeight && r.left < innerWidth,
    rect: {x: Math.round(r.x), y: Math.round(r.y), w: Math.round(r.width), h: Math.round(r.height)},
    outer: el.outerHTML.slice(0, 240)
  });
});
return rows;
"""
    result = await eval_js(
        browser,
        page_id,
        js,
        "Read upload trigger candidates and coordinates for Pinduoduo image upload controls.",
    )
    if not isinstance(result, list):
        raise RuntimeError(f"Unexpected upload trigger probe result: {result}")
    return result


async def click_upload_trigger(browser: ABCPClient, page_id: str, kind: str) -> JsonDict:
    candidates = await upload_trigger_candidates(browser, page_id)
    if kind == "main":
        filtered = [
            row for row in candidates
            if row.get("visible") and row.get("inViewport") and "上传图片" in row.get("text", "")
            and 150 <= row.get("rect", {}).get("y", 9999) <= 360
        ]
    else:
        await eval_js(
            browser,
            page_id,
            "window.scrollTo(Math.max(window.scrollX, 165), window.scrollY); return {scrollX, scrollY, innerWidth};",
            "Move the product form horizontally so the Pinduoduo detail local-upload button is fully inside the clickable viewport.",
        )
        await asyncio.sleep(0.2)
        candidates = await upload_trigger_candidates(browser, page_id)
        filtered = [
            row for row in candidates
            if row.get("visible") and row.get("inViewport") and row.get("text") == "本地上传"
            and -5 <= row.get("rect", {}).get("y", 9999) <= 180
        ]
        if not filtered:
            await call(
                browser,
                "Input.scroll",
                {
                    "pageId": page_id,
                    "deltaY": 760,
                    "purpose": "Scroll the product form to the 商品详情 quick-edit local upload button.",
                },
            )
            await asyncio.sleep(0.5)
            candidates = await upload_trigger_candidates(browser, page_id)
            filtered = [
                row for row in candidates
                if row.get("visible") and row.get("inViewport") and row.get("text") == "本地上传"
            ]
    filtered.sort(key=lambda row: (row["rect"]["w"] * row["rect"]["h"], row["rect"]["y"]))
    if not filtered:
        raise RuntimeError(f"Could not locate a visible {kind} upload trigger. Candidates: {candidates[:20]}")
    chosen = filtered[0]
    await click_center(
        browser,
        page_id,
        chosen["rect"],
        f"Click the visible Pinduoduo {kind} image upload trigger to open the native file chooser.",
    )
    return chosen


async def upload_files_via_visible_trigger(
    browser: ABCPClient,
    page_id: str,
    kind: str,
    files: List[str],
    purpose: str,
) -> None:
    if not files:
        raise RuntimeError(f"No files to upload for: {purpose}")
    try:
        chosen = await click_upload_trigger(browser, page_id, kind)
        log(f"Clicked visible upload trigger: {chosen}")
    except Exception:
        if kind != "detail":
            raise
        file_input_index = await find_image_file_input(browser, page_id)
        clicked = await click_file_input(
            browser,
            page_id,
            file_input_index,
            f"Open the current Pinduoduo detail image file input after the visible local-upload button could not be clicked reliably. {purpose}",
        )
        log(f"Opened detail file input fallback: {clicked}")
    await asyncio.sleep(0.8)
    await page_state(browser, page_id, f"Confirm file chooser is pending before uploading {len(files)} {kind} image files.")
    try:
        await call(
            browser,
            "File.handleChooser",
            {
                "pageId": page_id,
                "accept": True,
                "files": files,
                "purpose": purpose,
            },
        )
    except Exception:
        if kind != "detail":
            raise
        injected = await inject_files_into_image_input(
            browser,
            page_id,
            files,
            f"Programmatically provide detail image files to the Pinduoduo file input after native file chooser handling failed. {purpose}",
        )
        log(f"Injected detail files into file input fallback: {injected}")


async def mark_input_by_label(browser: ABCPClient, page_id: str, label: str, attr: str) -> str:
    js = f"""
const label = {json.dumps(label, ensure_ascii=False)};
const attr = {json.dumps(attr)};
const clean = (s) => String(s || "").replace(/\\s+/g, " ").trim();
const visible = (el) => {{
  const r = el.getBoundingClientRect();
  const st = getComputedStyle(el);
  return r.width > 1 && r.height > 1 && st.visibility !== "hidden" && st.display !== "none";
}};
const labels = Array.from(document.querySelectorAll("div,span,label"))
  .filter((el) => visible(el) && clean(el.innerText || el.textContent) === label)
  .map((el) => {{
    const r = el.getBoundingClientRect();
    return {{el, rect: {{x:r.x,y:r.y,w:r.width,h:r.height}}}};
  }});
const inputs = Array.from(document.querySelectorAll("input,textarea,[contenteditable='true']"))
  .filter((el) => visible(el) && (el.type || "").toLowerCase() !== "file")
  .map((el) => {{
    const r = el.getBoundingClientRect();
    return {{el, rect: {{x:r.x,y:r.y,w:r.width,h:r.height}}, placeholder: el.placeholder || "", value: el.value || ""}};
  }});
let best = null;
for (const lab of labels) {{
  for (const input of inputs) {{
    if (input.rect.x <= lab.rect.x) continue;
    const dy = Math.abs((input.rect.y + input.rect.h / 2) - (lab.rect.y + lab.rect.h / 2));
    if (dy > 80) continue;
    const score = dy + Math.max(0, input.rect.x - lab.rect.x) / 1000;
    if (!best || score < best.score) best = {{...input, labelRect: lab.rect, score}};
  }}
}}
if (!best) return {{ok:false, label, labels: labels.map(x=>x.rect), inputs: inputs.map(x=>({{rect:x.rect,placeholder:x.placeholder,value:x.value}})).slice(0,40)}};
document.querySelectorAll(`[${{attr}}]`).forEach((el) => el.removeAttribute(attr));
best.el.setAttribute(attr, "1");
best.el.scrollIntoView({{block: "center", inline: "nearest"}});
return {{ok:true, selector:`[${{attr}}='1']`, label, placeholder: best.placeholder, value: best.value, rect: best.rect, labelRect: best.labelRect}};
"""
    result = await eval_js(
        browser,
        page_id,
        js,
        f"Mark the input associated with label {label} so the Pinduoduo form can be filled accurately.",
    )
    if not isinstance(result, dict) or not result.get("ok"):
        raise RuntimeError(f"Could not mark input for label {label}: {result}")
    return str(result["selector"])


async def type_labeled_field(browser: ABCPClient, page_id: str, label: str, value: str) -> None:
    selector = await mark_input_by_label(browser, page_id, label, f"data-codex-field-{abs(hash(label))}")
    await call(
        browser,
        "Input.type",
        {
            "pageId": page_id,
            "selector": selector,
            "text": value,
            "clear": True,
            "delay": 25,
            "purpose": f"Fill Pinduoduo product field {label} with value from the downloaded 1688 product information.",
        },
    )


async def click_visible_option(browser: ABCPClient, page_id: str, text: str) -> bool:
    js = f"""
const text = {json.dumps(text, ensure_ascii=False)};
const clean = (s) => String(s || "").replace(/\\s+/g, " ").trim();
const visible = (el) => {{
  const r = el.getBoundingClientRect();
  const st = getComputedStyle(el);
  return r.width > 1 && r.height > 1 && r.bottom > 0 && r.right > 0 && r.top < innerHeight && r.left < innerWidth && st.visibility !== "hidden" && st.display !== "none";
}};
const rows = Array.from(document.querySelectorAll("li,div,span,[role='option'],[role='menuitem']"))
  .filter((el) => visible(el))
  .map((el, i) => {{
    const r = el.getBoundingClientRect();
    const t = clean(el.innerText || el.textContent || "");
    return {{i, tag: el.tagName, text: t, rect: {{x:r.x,y:r.y,w:r.width,h:r.height}}, exact: t === text, contains: t.includes(text)}};
  }})
  .filter((row) => row.exact || row.contains)
  .sort((a,b) => (b.exact-a.exact) || (a.rect.w*a.rect.h - b.rect.w*b.rect.h) || a.rect.y-b.rect.y);
return rows.slice(0, 10);
"""
    candidates = await eval_js(
        browser,
        page_id,
        js,
        f"Locate visible dropdown option {text} after opening a Pinduoduo attribute selector.",
    )
    if not isinstance(candidates, list) or not candidates:
        return False
    await click_center(
        browser,
        page_id,
        candidates[0]["rect"],
        f"Select the visible Pinduoduo dropdown option {text}.",
    )
    await asyncio.sleep(0.4)
    return True


async def select_labeled_field(browser: ABCPClient, page_id: str, label: str, value: str, fallback: Optional[str] = None) -> None:
    selector = await mark_input_by_label(browser, page_id, label, f"data-codex-select-{abs(hash(label))}")
    selected = False
    candidates = []
    for candidate in [value, fallback]:
        if candidate and candidate not in candidates:
            candidates.append(candidate)
    for index, candidate in enumerate(candidates):
        if not candidate:
            continue
        await call(
            browser,
            "Input.type",
            {
                "pageId": page_id,
                "selector": selector,
                "text": candidate,
                "clear": True,
                "delay": 25,
                "purpose": f"Search/select Pinduoduo attribute {label} using value from the downloaded 1688 product information.",
            },
        )
        await asyncio.sleep(0.8)
        if await click_visible_option(browser, page_id, candidate):
            selected = True
            break
        if index < len(candidates) - 1:
            continue
        await call(
            browser,
            "Input.press",
            {
                "pageId": page_id,
                "key": "Enter",
                "purpose": f"Confirm typed Pinduoduo attribute value {candidate} for {label} when no dropdown option was exposed.",
            },
        )
        await asyncio.sleep(0.5)
        selected = True
        break
    if not selected:
        raise RuntimeError(f"Could not select field {label} value {value}")


async def fill_basic_fields(browser: ABCPClient, page_id: str, rank: int) -> None:
    product = product_payload(rank)
    await type_labeled_field(browser, page_id, "商品标题", str(product["title"]))
    await select_labeled_field(browser, page_id, "品牌", str(product["brand"]), str(product["fallback_brand"]))
    await select_labeled_field(browser, page_id, "面料俗称", str(product["fabric"]), str(product.get("fabric_fallback") or ""))
    await select_labeled_field(browser, page_id, "适用年龄", str(product["age"]), str(product.get("age_fallback") or ""))
    await select_labeled_field(browser, page_id, "流行元素", str(product["popular"]))
    await select_labeled_field(browser, page_id, "制式", str(product["style"]))
    await select_labeled_field(browser, page_id, "上市时节", str(product["season"]))
    await type_labeled_field(browser, page_id, "商品货号", str(product["article_no"]))


async def set_labeled_input_value_direct(browser: ABCPClient, page_id: str, label: str, value: str) -> JsonDict:
    js = f"""
const label = {json.dumps(label, ensure_ascii=False)};
const value = {json.dumps(value, ensure_ascii=False)};
const clean = (s) => String(s || "").replace(/\\s+/g, " ").trim();
const visibleBox = (el) => {{
  const r = el.getBoundingClientRect();
  return r.width > 1 && r.height > 1;
}};
const setValue = (el, next) => {{
  const proto = el.tagName === "TEXTAREA" ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
  const setter = Object.getOwnPropertyDescriptor(proto, "value")?.set;
  if (setter) setter.call(el, next); else el.value = next;
  el.dispatchEvent(new Event("input", {{bubbles: true}}));
  el.dispatchEvent(new Event("change", {{bubbles: true}}));
  el.dispatchEvent(new FocusEvent("blur", {{bubbles: true}}));
}};
const labels = Array.from(document.querySelectorAll("div,span,label"))
  .filter((el) => visibleBox(el) && clean(el.innerText || el.textContent) === label)
  .map((el) => {{
    const r = el.getBoundingClientRect();
    return {{el, rect: {{x:r.x,y:r.y,w:r.width,h:r.height}}}};
  }});
const inputs = Array.from(document.querySelectorAll("input,textarea"))
  .filter((el) => visibleBox(el) && (el.type || "").toLowerCase() !== "file")
  .map((el) => {{
    const r = el.getBoundingClientRect();
    return {{el, rect: {{x:r.x,y:r.y,w:r.width,h:r.height}}, placeholder: el.placeholder || "", oldValue: el.value || ""}};
  }});
let best = null;
for (const lab of labels) {{
  for (const input of inputs) {{
    if (input.rect.x <= lab.rect.x) continue;
    const dy = Math.abs((input.rect.y + input.rect.h / 2) - (lab.rect.y + lab.rect.h / 2));
    if (dy > 80) continue;
    const score = dy + Math.max(0, input.rect.x - lab.rect.x) / 1000;
    if (!best || score < best.score) best = {{...input, labelRect: lab.rect, score}};
  }}
}}
if (!best) return {{ok:false, label, labels: labels.map((item) => item.rect), inputs: inputs.map((item) => ({{rect:item.rect, placeholder:item.placeholder, value:item.oldValue}})).slice(0, 40)}};
setValue(best.el, value);
best.el.setAttribute("data-codex-direct-filled", label);
best.el.scrollIntoView({{block: "nearest", inline: "nearest"}});
return {{ok:true, label, value, previous: best.oldValue, placeholder: best.placeholder, rect: best.rect, labelRect: best.labelRect}};
"""
    result = await eval_js(
        browser,
        page_id,
        js,
        f"Directly set Pinduoduo product field {label} to the downloaded 1688 product value after dropdown interactions proved unreliable.",
    )
    if not isinstance(result, dict) or not result.get("ok"):
        raise RuntimeError(f"Could not directly set field {label}: {result}")
    return result


async def fill_basic_fields_direct(browser: ABCPClient, page_id: str, rank: int) -> JsonDict:
    product = product_payload(rank)
    return await fill_basic_fields_direct_product(browser, page_id, product)


async def fill_basic_fields_direct_product(browser: ABCPClient, page_id: str, product: Dict[str, Any]) -> JsonDict:
    values = {
        "商品标题": str(product["title"]),
        "品牌": str(product["brand"]),
        "面料俗称": str(product["fabric"]),
        "适用年龄": str(product.get("age_fallback") or product["age"]),
        "流行元素": str(product["popular"]),
        "制式": str(product["style"]),
        "上市时节": str(product["season"]),
        "商品货号": str(product["article_no"]),
    }
    results = {}
    for label, value in values.items():
        results[label] = await set_labeled_input_value_direct(browser, page_id, label, value)
        await asyncio.sleep(0.2)
    return results


async def current_upload_counts(browser: ABCPClient, page_id: str) -> JsonDict:
    result = await eval_js(
        browser,
        page_id,
        r"""
const text = String(document.body.innerText || "").replace(/\s+/g, " ").trim();
const counts = Array.from(text.matchAll(/已上传(\d+)\/(\d+)张/g)).map((match) => ({
  text: match[0],
  current: Number(match[1]),
  total: Number(match[2])
}));
return {
  counts,
  main: counts.find((item) => item.total === 10) || null,
  detail: counts.find((item) => item.total === 50) || null
};
""",
        "Read current carousel and detail image upload counts before deciding which Pinduoduo uploads still need to run.",
    )
    if not isinstance(result, dict):
        raise RuntimeError(f"Unexpected upload-count result: {result}")
    return result


async def upload_remaining_detail_images(browser: ABCPClient, page_id: str, rank: int) -> JsonDict:
    product = product_payload(rank)
    counts = await current_upload_counts(browser, page_id)
    current = count_current_from_upload_counts(counts, 50)
    detail_files = list(product["detail_files"])
    if current >= len(detail_files):
        return {"rank": rank, "alreadyUploaded": current, "uploadedNow": 0, "target": len(detail_files)}
    remaining = detail_files[current:]
    await upload_files_via_visible_trigger(
        browser,
        page_id,
        "detail",
        remaining,
        f"Upload remaining rank {rank} detail images from the prepared Desktop product folder without duplicating the {current} images already on the Pinduoduo form.",
    )
    await wait_for_text(
        browser,
        page_id,
        f"已上传{len(detail_files)}/50张",
        60.0,
        f"Wait for Pinduoduo to finish uploading remaining rank {rank} detail images.",
    )
    return {"rank": rank, "alreadyUploaded": current, "uploadedNow": len(remaining), "target": len(detail_files)}


async def set_spec_values(browser: ABCPClient, page_id: str, values: List[str], spec_index: int) -> None:
    for value in values:
        js = f"""
const specIndex = {spec_index};
const nextValue = {json.dumps(value, ensure_ascii=False)};
const typeInputs = Array.from(document.querySelectorAll("input"))
  .filter((el) => /^规格类型/.test(el.placeholder || "") && el.getBoundingClientRect().width > 1)
  .sort((a,b) => a.getBoundingClientRect().y - b.getBoundingClientRect().y);
const start = typeInputs[specIndex];
if (!start) return {{ok:false, reason:"missing-spec-type", typeCount:typeInputs.length}};
const startY = start.getBoundingClientRect().y;
const endY = typeInputs[specIndex + 1] ? typeInputs[specIndex + 1].getBoundingClientRect().y : Infinity;
const inputs = Array.from(document.querySelectorAll("input"))
  .filter((el) => {{
    const r = el.getBoundingClientRect();
    return (el.placeholder || "") === "请输入规格名称" && r.width > 1 && r.y > startY && r.y < endY;
  }})
  .sort((a,b) => a.getBoundingClientRect().x - b.getBoundingClientRect().x);
const existing = inputs.map((node) => String(node.value || "").trim()).filter(Boolean);
if (existing.includes(nextValue)) return {{ok:true, skip:true, existing}};
const el = inputs.find((node) => !String(node.value || "").trim()) || inputs[inputs.length - 1];
if (!el) return {{ok:false, reason:"missing-empty-spec-value", count:inputs.length, startY, endY}};
document.querySelectorAll("[data-codex-spec-input]").forEach((node) => node.removeAttribute("data-codex-spec-input"));
el.setAttribute("data-codex-spec-input", "1");
el.scrollIntoView({{block:"center", inline:"nearest"}});
const r = el.getBoundingClientRect();
return {{ok:true, rect:{{x:r.x,y:r.y,w:r.width,h:r.height}}, count:inputs.length, existing: inputs.map((node) => node.value || "")}};
"""
        marked = await eval_js(
            browser,
            page_id,
            js,
            "Mark the current SKU specification value input before typing size or color values.",
        )
        if not isinstance(marked, dict) or not marked.get("ok"):
            raise RuntimeError(f"Could not mark spec input {spec_index}: {marked}")
        if marked.get("skip"):
            log(f"SKU specification value already exists, skipping duplicate: {value}")
            continue
        await call(
            browser,
            "Input.type",
            {
                "pageId": page_id,
                "selector": "[data-codex-spec-input='1']",
                "text": value,
                "clear": True,
                "delay": 25,
                "purpose": f"Enter SKU specification value {value} from the downloaded 1688 product information.",
            },
        )
        await call(
            browser,
            "Input.press",
            {
                "pageId": page_id,
                "key": "Enter",
                "purpose": f"Confirm SKU specification value {value}.",
            },
        )
        await asyncio.sleep(0.5)


async def upload_sku_preview_images(browser: ABCPClient, page_id: str, files: List[str]) -> JsonDict:
    if not files:
        return {"uploaded": 0, "reason": "no-files"}
    results = []
    while True:
        candidates = await eval_js(
            browser,
            page_id,
            r"""
window.scrollTo(Math.max(window.scrollX, 165), window.scrollY);
const clean = (s) => String(s || "").replace(/\s+/g, " ").trim();
const visible = (el) => {
  const r = el.getBoundingClientRect();
  const st = getComputedStyle(el);
  return r.width > 1 && r.height > 1 && r.bottom > 0 && r.right > 0 && r.top < innerHeight && r.left < innerWidth && st.visibility !== "hidden" && st.display !== "none";
};
const rows = [];
Array.from(document.querySelectorAll(".sku-preview-cell,td")).forEach((cell, cellIndex) => {
  if (!/预览图|本地上传|请上传/.test(clean(cell.innerText || cell.textContent || "")) && !String(cell.className || "").includes("sku-preview-cell")) return;
  Array.from(cell.querySelectorAll("button,span,[role='button'],label")).forEach((el, index) => {
    const text = clean(el.innerText || el.textContent || el.getAttribute("aria-label") || "");
    if (text !== "本地上传" || !visible(el)) return;
    const r = el.getBoundingClientRect();
    rows.push({
      cellIndex,
      index,
      tag: el.tagName,
      text,
      rect: {x: Math.round(r.x), y: Math.round(r.y), w: Math.round(r.width), h: Math.round(r.height)}
    });
  });
});
rows.sort((a,b) => a.rect.y - b.rect.y || a.rect.x - b.rect.x);
return rows;
""",
            "Locate visible SKU preview local-upload buttons that must be filled before Pinduoduo submission.",
        )
        if not isinstance(candidates, list) or not candidates:
            break
        chosen = candidates[0]
        await click_center(
            browser,
            page_id,
            chosen["rect"],
            "Open the SKU preview-image file chooser so the required Pinduoduo SKU preview image can be uploaded.",
        )
        await asyncio.sleep(0.8)
        await page_state(browser, page_id, "Confirm SKU preview file chooser is pending before uploading the prepared main image.")
        try:
            await call(
                browser,
                "File.handleChooser",
                {
                    "pageId": page_id,
                    "accept": True,
                    "files": [files[0]],
                    "purpose": "Upload a prepared main product image as the required Pinduoduo SKU preview image.",
                },
            )
        except Exception:
            injected = await inject_file_into_sku_preview(
                browser,
                page_id,
                files[0],
            )
            log(f"Injected SKU preview image into file input fallback: {injected}")
        results.append(chosen)
        await asyncio.sleep(2.0)
        if len(results) >= 10:
            break
    return {"uploaded": len(results), "targets": results}


async def set_prices_and_stock(
    browser: ABCPClient,
    page_id: str,
    stock: str = "500",
    group_price: str = "59",
    single_price: str = "69",
    reference_price: str = "89",
) -> JsonDict:
    config = json.dumps(
        {
            "stock": str(stock),
            "groupPrice": str(group_price),
            "singlePrice": str(single_price),
            "referencePrice": str(reference_price),
        }
    )
    js = r"""
const fixed = __CONFIG__;
const setValue = (el, value) => {
  const proto = el.tagName === "TEXTAREA" ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
  const setter = Object.getOwnPropertyDescriptor(proto, "value")?.set;
  if (setter) setter.call(el, value); else el.value = value;
  el.dispatchEvent(new Event("input", {bubbles:true}));
  el.dispatchEvent(new Event("change", {bubbles:true}));
  el.dispatchEvent(new KeyboardEvent("keyup", {bubbles:true, key:"0"}));
};
const text = (el) => String(el.innerText || el.textContent || "").replace(/\s+/g, " ").trim();
const priceHeader = Array.from(document.querySelectorAll("div,span")).find((el) => text(el) === "价格及库存");
const startY = priceHeader ? priceHeader.getBoundingClientRect().y : 0;
const ref = Array.from(document.querySelectorAll("input")).find((el) => (el.placeholder || "").includes("应大于商品最大单买价"));
const tableInputs = Array.from(document.querySelectorAll("input"))
  .filter((el) => {
    const r = el.getBoundingClientRect();
    return r.width > 1 && r.height > 1 && r.y > startY && el !== ref && (el.placeholder || "") === "请输入";
  })
  .sort((a,b) => {
    const ar = a.getBoundingClientRect(), br = b.getBoundingClientRect();
    return ar.y === br.y ? ar.x - br.x : ar.y - br.y;
  });
const rows = [];
for (let i = 0; i < tableInputs.length; i += 5) rows.push(tableInputs.slice(i, i + 5));
for (const row of rows) {
  if (row[0]) setValue(row[0], fixed.stock);
  if (row[1]) setValue(row[1], fixed.groupPrice);
  if (row[2]) setValue(row[2], fixed.singlePrice);
}
if (ref) setValue(ref, fixed.referencePrice);
return {rowCount: rows.length, inputCount: tableInputs.length, referenceSet: Boolean(ref), ...fixed};
""".replace("__CONFIG__", config)
    result = await eval_js(
        browser,
        page_id,
        js,
        "Fill all SKU rows with configured stock, group price, single-buy price, and reference price for the Pinduoduo draft.",
    )
    if not isinstance(result, dict):
        raise RuntimeError(f"Unexpected price/stock fill result: {result}")
    return result


async def fill_product_form(browser: ABCPClient, page_id: str, rank: int) -> JsonDict:
    product = product_payload(rank)
    await page_state(browser, page_id, f"Confirm product form is stable before filling rank {rank}.")
    await upload_files_via_visible_trigger(
        browser,
        page_id,
        "main",
        product["main_files"],
        f"Upload rank {rank} carousel images from the prepared Desktop product folder.",
    )
    if not await wait_for_text(browser, page_id, f"已上传{len(product['main_files'])}/10张", 60, "Wait for carousel image upload completion."):
        raise RuntimeError("Carousel image upload did not reach the expected uploaded count")
    await fill_basic_fields_direct(browser, page_id, rank)
    await upload_files_via_visible_trigger(
        browser,
        page_id,
        "detail",
        product["detail_files"],
        f"Upload rank {rank} detail images from the prepared Desktop product folder.",
    )
    await wait_for_text(browser, page_id, f"已上传{len(product['detail_files'])}/50张", 60, "Wait for detail image upload completion.")
    await set_spec_values(browser, page_id, list(product["sizes"]), 0)
    await set_spec_values(browser, page_id, [str(product["color"])], 1)
    preview_result = await upload_sku_preview_images(browser, page_id, product["main_files"])
    price_result = await set_prices_and_stock(browser, page_id)
    return {"rank": rank, "title": product["title"], "mainImageCount": len(product["main_files"]), "detailImageCount": len(product["detail_files"]), "previewResult": preview_result, "priceResult": price_result}


async def fill_product_draft_form(browser: ABCPClient, page_id: str, product: Dict[str, Any], upload_detail: bool = True) -> JsonDict:
    rank = int(product.get("rank") or 0)
    label = f"rank {rank}" if rank else product["title"]
    await page_state(browser, page_id, f"Confirm Pinduoduo product-add form is stable before filling draft for {label}.")

    await upload_files_via_visible_trigger(
        browser,
        page_id,
        "main",
        list(product["main_files"]),
        f"Upload carousel images from the prepared 1688 product manifest for {label}.",
    )
    main_expected = f"已上传{min(len(product['main_files']), 10)}/10张"
    main_observed = await wait_for_text(
        browser,
        page_id,
        main_expected,
        60.0,
        f"Wait for Pinduoduo carousel upload completion for {label}.",
    )

    basic_result = await fill_basic_fields_direct_product(browser, page_id, product)

    detail_result: JsonDict = {"skipped": not upload_detail, "files": list(product.get("detail_files") or [])}
    if upload_detail and product.get("detail_files"):
        await upload_files_via_visible_trigger(
            browser,
            page_id,
            "detail",
            list(product["detail_files"]),
            f"Upload product detail images from the prepared 1688 product manifest for {label}.",
        )
        detail_expected = f"已上传{len(product['detail_files'])}/50张"
        detail_result = {
            "skipped": False,
            "files": list(product["detail_files"]),
            "expectedText": detail_expected,
            "observed": await wait_for_text(
                browser,
                page_id,
                detail_expected,
                60.0,
                f"Wait for Pinduoduo detail image upload completion for {label}.",
            ),
        }

    await set_spec_values(browser, page_id, list(product["sizes"]), 0)
    await set_spec_values(browser, page_id, [str(product["color"])], 1)
    price_result = await set_prices_and_stock(
        browser,
        page_id,
        product.get("stock", "500"),
        product.get("group_price", "59"),
        product.get("single_price", "69"),
        product.get("reference_price", "89"),
    )
    return {
        "rank": rank,
        "title": product["title"],
        "manifest": str(product.get("manifest") or ""),
        "mainUpload": {
            "files": list(product["main_files"]),
            "expectedText": main_expected,
            "observed": main_observed,
        },
        "basicFields": basic_result,
        "detailUpload": detail_result,
        "specs": {"sizes": list(product["sizes"]), "color": product["color"]},
        "priceResult": price_result,
        "stoppedBeforeSkuPreviewAndSubmit": True,
    }


async def finish_current_product_form(browser: ABCPClient, page_id: str, rank: int, skip_detail: bool = False) -> JsonDict:
    product = product_payload(rank)
    await page_state(browser, page_id, f"Confirm current Pinduoduo product form before resuming rank {rank}.")
    basic_result = await fill_basic_fields_direct(browser, page_id, rank)
    detail_result = {"rank": rank, "skipped": True} if skip_detail else await upload_remaining_detail_images(browser, page_id, rank)
    await set_spec_values(browser, page_id, list(product["sizes"]), 0)
    await set_spec_values(browser, page_id, [str(product["color"])], 1)
    preview_result = await upload_sku_preview_images(browser, page_id, product["main_files"])
    price_result = await set_prices_and_stock(browser, page_id)
    submitted = await submit_product(browser, page_id, rank)
    return {
        "rank": rank,
        "title": product["title"],
        "basicFields": basic_result,
        "detailUpload": detail_result,
        "previewResult": preview_result,
        "priceResult": price_result,
        "submitted": submitted,
    }


async def submit_product(browser: ABCPClient, page_id: str, rank: int) -> JsonDict:
    await page_state(browser, page_id, f"Confirm product form is stable before submitting rank {rank} for listing.")
    clicked = await click_text(
        browser,
        page_id,
        "提交并上架",
        f"Submit and list rank {rank} product after user confirmed publishing through 提交并上架.",
    )
    if not clicked:
        raise RuntimeError("Could not locate 提交并上架 button")
    await asyncio.sleep(4.0)
    state = await page_state(browser, page_id, f"Check Pinduoduo response after submitting rank {rank}.")
    text = await body_text(browser, page_id, f"Read page text after submitting rank {rank} to detect success or validation errors.")
    return {"rank": rank, "state": data(state), "bodySample": text[:2000]}


async def navigate_new_product_form(browser: ABCPClient, page_id: str) -> JsonDict:
    await call(
        browser,
        "Page.navigate",
        {
            "pageId": page_id,
            "url": "https://mms.pinduoduo.com/goods/goods_add/index?type=add&from=category&id=194983938261",
            "purpose": "Open a fresh Pinduoduo product-add form for the next Hanfu listing in the same confirmed category.",
        },
    )
    await asyncio.sleep(3.0)
    state = await page_state(browser, page_id, "Confirm fresh Pinduoduo product-add form loaded for next product.")
    return data(state)


async def mark_phone_input(browser: ABCPClient, page_id: str) -> str:
    js = """
const clean = (s) => String(s || "").replace(/\\s+/g, " ").trim();
const visible = (el) => {
  const r = el.getBoundingClientRect();
  const st = getComputedStyle(el);
  return r.width > 1 && r.height > 1 && st.visibility !== "hidden" && st.display !== "none";
};
const inputs = Array.from(document.querySelectorAll("input,textarea,[contenteditable='true']"))
  .filter((el) => visible(el))
  .map((el, i) => {
    const r = el.getBoundingClientRect();
    const score =
      (/phone|mobile|account|username|user|tel/i.test(el.name || "") ? 4 : 0) +
      (/phone|mobile|account|username|user|tel/i.test(el.id || "") ? 4 : 0) +
      (/(手机号|手机|账号|账户)/.test(clean(el.placeholder || el.getAttribute("aria-label") || "")) ? 8 : 0) +
      ((el.type || "").toLowerCase() === "tel" ? 3 : 0) +
      ((el.type || "").toLowerCase() === "text" ? 1 : 0);
    return {
      i,
      tag: el.tagName,
      type: el.type || "",
      name: el.name || "",
      id: el.id || "",
      placeholder: el.placeholder || "",
      aria: el.getAttribute("aria-label") || "",
      rect: {x: r.x, y: r.y, w: r.width, h: r.height},
      score
    };
  })
  .filter((item) => item.score > 0)
  .sort((a, b) => b.score - a.score || a.rect.y - b.rect.y);
if (!inputs.length) return {selector: "", inputs};
const chosen = Array.from(document.querySelectorAll("input,textarea,[contenteditable='true']"))
  .filter((el) => visible(el))[inputs[0].i];
chosen.setAttribute("data-codex-pdd-phone", "1");
return {selector: "[data-codex-pdd-phone='1']", chosen: inputs[0], inputs};
"""
    result = await eval_js(
        browser,
        page_id,
        js,
        "Mark the phone/account input so Input.type can target it with a stable selector.",
    )
    if not isinstance(result, dict) or not result.get("selector"):
        raise RuntimeError(f"Could not locate phone/account input: {result}")
    log(f"Phone input marked: {result.get('chosen')}")
    return str(result["selector"])


async def phase_login_phone(args: argparse.Namespace) -> int:
    cfg, agent_id = load_browser_config(args.agent_id)
    async with ABCPClient(cfg) as browser:
        await call(browser, "System.register", {"agentId": agent_id})
        await call(browser, "System.getCapabilities", {"skillFile": False})
        page_id = await get_or_create_page(browser, args.page_id)
        await page_state(
            browser,
            page_id,
            "Confirm Pinduoduo merchant login page is stable before selecting account login.",
        )
        clicked = await click_text(
            browser,
            page_id,
            "账号登录",
            "Switch from QR-code login to account-login mode so the phone number can be entered.",
        )
        if not clicked:
            raise RuntimeError("Could not find the account login tab")
        await asyncio.sleep(0.8)
        selector = await mark_phone_input(browser, page_id)
        await call(
            browser,
            "Input.type",
            {
                "pageId": page_id,
                "selector": selector,
                "text": args.phone,
                "clear": True,
                "delay": 35,
                "purpose": "Enter the user-provided Pinduoduo merchant account phone number.",
            },
        )
        await call(
            browser,
            "Hitl.requestPause",
            {
                "pageId": page_id,
                "reason": "Phone number has been entered. User must manually enter password and complete login.",
                "purpose": "Pause for the user to enter the password and complete Pinduoduo merchant login without exposing credentials to automation.",
            },
        )
        print(json.dumps({"pageId": page_id, "agentId": agent_id, "status": "waiting_for_user_password"}, ensure_ascii=False, indent=2))
    return 0


async def phase_probe_entry(args: argparse.Namespace) -> int:
    cfg, agent_id = load_browser_config(args.agent_id)
    async with ABCPClient(cfg) as browser:
        await call(browser, "System.register", {"agentId": agent_id})
        await call(browser, "System.getCapabilities", {"skillFile": False})
        page_id = await get_or_create_page(browser, args.page_id)
        await page_state(browser, page_id, "Confirm logged-in Pinduoduo page before probing publish entry candidates.")
        await ax_tree(browser, page_id, "Refresh accessibility data before probing publish entry candidates.")
        result = await probe_publish_entries(browser, page_id)
        print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


async def phase_open_publish(args: argparse.Namespace) -> int:
    cfg, agent_id = load_browser_config(args.agent_id)
    async with ABCPClient(cfg) as browser:
        await call(browser, "System.register", {"agentId": agent_id})
        await call(browser, "System.getCapabilities", {"skillFile": False})
        page_id = await get_or_create_page(browser, args.page_id)
        result = await open_publish_entry(browser, page_id)
        print(json.dumps({"pageId": page_id, **result}, ensure_ascii=False, indent=2))
    return 0


async def phase_probe_category(args: argparse.Namespace) -> int:
    cfg, agent_id = load_browser_config(args.agent_id)
    async with ABCPClient(cfg) as browser:
        await call(browser, "System.register", {"agentId": agent_id})
        await call(browser, "System.getCapabilities", {"skillFile": False})
        page_id = await get_or_create_page(browser, args.page_id)
        await page_state(browser, page_id, "Confirm Pinduoduo category page before probing category DOM.")
        result = await probe_category_page(browser, page_id)
        print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


async def phase_probe_form(args: argparse.Namespace) -> int:
    cfg, agent_id = load_browser_config(args.agent_id)
    async with ABCPClient(cfg) as browser:
        await call(browser, "System.register", {"agentId": agent_id})
        await call(browser, "System.getCapabilities", {"skillFile": False})
        page_id = await get_or_create_page(browser, args.page_id)
        await page_state(browser, page_id, "Confirm Pinduoduo product form before probing form controls.")
        result = await probe_product_form(browser, page_id)
        print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


async def phase_upload_files(args: argparse.Namespace) -> int:
    cfg, agent_id = load_browser_config(args.agent_id)
    if args.manifest or args.product_dir:
        product = load_manifest_product(resolve_manifest_path(args.manifest, args.product_dir))
    else:
        if args.rank is None:
            raise RuntimeError("Provide --rank, --manifest, or --product-dir")
        product = product_payload(args.rank)
    files = product["main_files"] if args.kind == "main" else product["detail_files"]
    rank = int(product.get("rank") or args.rank or 0)
    label = f"rank {rank}" if rank else str(product.get("title") or "manifest product")
    expected_count = min(len(files), 10) if args.kind == "main" else len(files)
    expected = f"已上传{expected_count}/{'10' if args.kind == 'main' else '50'}张"
    async with ABCPClient(cfg) as browser:
        await call(browser, "System.register", {"agentId": agent_id})
        await call(browser, "System.getCapabilities", {"skillFile": False})
        page_id = await get_or_create_page(browser, args.page_id)
        await page_state(browser, page_id, f"Confirm Pinduoduo product form before uploading {args.kind} images for {label}.")
        await upload_files_via_visible_trigger(
            browser,
            page_id,
            args.kind,
            files,
            f"Upload {label} {args.kind} images from the prepared Desktop product folder to the matching Pinduoduo image uploader.",
        )
        ok = await wait_for_text(
            browser,
            page_id,
            expected,
            45.0,
            f"Wait for Pinduoduo to finish uploading {label} {args.kind} images.",
        )
        print(json.dumps({"pageId": page_id, "rank": rank, "kind": args.kind, "files": files, "expectedText": expected, "uploadObserved": ok}, ensure_ascii=False, indent=2))
    return 0


async def phase_probe_upload(args: argparse.Namespace) -> int:
    cfg, agent_id = load_browser_config(args.agent_id)
    async with ABCPClient(cfg) as browser:
        await call(browser, "System.register", {"agentId": agent_id})
        await call(browser, "System.getCapabilities", {"skillFile": False})
        page_id = await get_or_create_page(browser, args.page_id)
        await page_state(browser, page_id, "Confirm page before probing visible upload triggers.")
        result = await upload_trigger_candidates(browser, page_id)
        print(json.dumps({"pageId": page_id, "candidates": result}, ensure_ascii=False, indent=2))
    return 0


async def phase_publish_current(args: argparse.Namespace) -> int:
    cfg, agent_id = load_browser_config(args.agent_id)
    results = []
    ranks = [int(part) for part in args.ranks.split(",") if part.strip()]
    async with ABCPClient(cfg) as browser:
        await call(browser, "System.register", {"agentId": agent_id})
        await call(browser, "System.getCapabilities", {"skillFile": False})
        page_id = await get_or_create_page(browser, args.page_id)
        state_resp = await page_state(browser, page_id, "Confirm current Pinduoduo page before starting the continuous publish flow.")
        state_data = data(state_resp) or {}
        if "goods/goods_add" not in str(state_data.get("url", "")):
            await navigate_new_product_form(browser, page_id)
        for idx, rank in enumerate(ranks):
            if idx > 0:
                await navigate_new_product_form(browser, page_id)
            filled = await fill_product_form(browser, page_id, rank)
            submitted = await submit_product(browser, page_id, rank)
            results.append({"filled": filled, "submitted": submitted})
        print(json.dumps({"pageId": page_id, "results": results}, ensure_ascii=False, indent=2))
    return 0


async def phase_finish_current(args: argparse.Namespace) -> int:
    cfg, agent_id = load_browser_config(args.agent_id)
    async with ABCPClient(cfg) as browser:
        await call(browser, "System.register", {"agentId": agent_id})
        await call(browser, "System.getCapabilities", {"skillFile": False})
        page_id = await get_or_create_page(browser, args.page_id)
        result = await finish_current_product_form(browser, page_id, args.rank, args.skip_detail)
        print(json.dumps({"pageId": page_id, "result": result}, ensure_ascii=False, indent=2))
    return 0


async def phase_fill_draft(args: argparse.Namespace) -> int:
    cfg, agent_id = load_browser_config(args.agent_id)
    product = load_manifest_product(resolve_manifest_path(args.manifest, args.product_dir))
    async with ABCPClient(cfg) as browser:
        await call(browser, "System.register", {"agentId": agent_id})
        await call(browser, "System.getCapabilities", {"skillFile": False})
        page_id = await get_or_create_page(browser, args.page_id)
        state_resp = await page_state(browser, page_id, "Confirm current Pinduoduo page before filling the manifest-driven product draft.")
        state_data = data(state_resp) or {}
        if "goods/goods_add" not in str(state_data.get("url", "")):
            await navigate_new_product_form(browser, page_id)
        result = await fill_product_draft_form(browser, page_id, product, upload_detail=not args.skip_detail)
        print(json.dumps({"pageId": page_id, "result": result}, ensure_ascii=False, indent=2))
    return 0


async def phase_fix_preview(args: argparse.Namespace) -> int:
    cfg, agent_id = load_browser_config(args.agent_id)
    product = product_payload(args.rank)
    async with ABCPClient(cfg) as browser:
        await call(browser, "System.register", {"agentId": agent_id})
        await call(browser, "System.getCapabilities", {"skillFile": False})
        page_id = await get_or_create_page(browser, args.page_id)
        await page_state(browser, page_id, f"Confirm current Pinduoduo form before uploading SKU preview image for rank {args.rank}.")
        result = await upload_sku_preview_images(browser, page_id, product["main_files"])
        print(json.dumps({"pageId": page_id, "rank": args.rank, "result": result}, ensure_ascii=False, indent=2))
    return 0


async def phase_upload_image_space(args: argparse.Namespace) -> int:
    cfg, agent_id = load_browser_config(args.agent_id)
    product = product_payload(args.rank)
    async with ABCPClient(cfg) as browser:
        await call(browser, "System.register", {"agentId": agent_id})
        await call(browser, "System.getCapabilities", {"skillFile": False})
        page_id = await get_or_create_page(browser, args.page_id)
        await page_state(browser, page_id, f"Confirm Pinduoduo image-space upload panel before injecting rank {args.rank} preview image.")
        result = await inject_file_into_image_space_upload(browser, page_id, product["main_files"][0])
        await asyncio.sleep(3.0)
        text = await body_text(browser, page_id, "Read image-space modal text after uploading the prepared preview image.")
        print(json.dumps({"pageId": page_id, "rank": args.rank, "result": result, "bodySample": text[-1600:]}, ensure_ascii=False, indent=2))
    return 0


async def phase_drop_image_space(args: argparse.Namespace) -> int:
    cfg, agent_id = load_browser_config(args.agent_id)
    product = product_payload(args.rank)
    async with ABCPClient(cfg) as browser:
        await call(browser, "System.register", {"agentId": agent_id})
        await call(browser, "System.getCapabilities", {"skillFile": False})
        page_id = await get_or_create_page(browser, args.page_id)
        await page_state(browser, page_id, f"Confirm Pinduoduo image-space upload panel before dropping rank {args.rank} preview image.")
        dropped = await drop_file_into_image_space_upload(browser, page_id, product["main_files"][0])
        await asyncio.sleep(args.wait)
        probed = await select_uploaded_image_space_preview(browser, page_id)
        print(json.dumps({"pageId": page_id, "rank": args.rank, "dropped": dropped, "probed": probed}, ensure_ascii=False, indent=2))
    return 0


async def phase_clean_change_image_space(args: argparse.Namespace) -> int:
    cfg, agent_id = load_browser_config(args.agent_id)
    product = product_payload(args.rank)
    async with ABCPClient(cfg) as browser:
        await call(browser, "System.register", {"agentId": agent_id})
        await call(browser, "System.getCapabilities", {"skillFile": False})
        page_id = await get_or_create_page(browser, args.page_id)
        await page_state(browser, page_id, f"Confirm Pinduoduo image-space upload input before clean-change uploading rank {args.rank} preview image.")
        changed = await clean_change_image_space_upload(browser, page_id, product["main_files"][0])
        await asyncio.sleep(args.wait)
        probed = await select_uploaded_image_space_preview(browser, page_id)
        print(json.dumps({"pageId": page_id, "rank": args.rank, "changed": changed, "probed": probed}, ensure_ascii=False, indent=2))
    return 0


async def phase_direct_material_upload(args: argparse.Namespace) -> int:
    cfg, agent_id = load_browser_config(args.agent_id)
    product = product_payload(args.rank)
    async with ABCPClient(cfg) as browser:
        await call(browser, "System.register", {"agentId": agent_id})
        await call(browser, "System.getCapabilities", {"skillFile": False})
        page_id = await get_or_create_page(browser, args.page_id)
        await page_state(browser, page_id, f"Confirm Pinduoduo page before direct-uploading rank {args.rank} preview image into image space.")
        result = await upload_image_space_material_direct(browser, page_id, product["main_files"][0])
        print(json.dumps({"pageId": page_id, "rank": args.rank, "result": result}, ensure_ascii=False, indent=2))
    return 0


async def phase_select_category(args: argparse.Namespace) -> int:
    cfg, agent_id = load_browser_config(args.agent_id)
    async with ABCPClient(cfg) as browser:
        await call(browser, "System.register", {"agentId": agent_id})
        await call(browser, "System.getCapabilities", {"skillFile": False})
        page_id = await get_or_create_page(browser, args.page_id)
        await page_state(browser, page_id, "Confirm Pinduoduo category page before selecting 女装 > 汉服 > 汉服套装.")
        path = ["服饰箱包", "女装/女士精品", "汉服", "汉服套装"]
        clicked_path: List[str] = []
        for label in path:
            clicked = await click_text(
                browser,
                page_id,
                label,
                f"Select category node {label} while choosing 女装 > 汉服 > 汉服套装 for publishing Hanfu products.",
            )
            if not clicked:
                raise RuntimeError(f"Could not find visible category node: {label}")
            clicked_path.append(label)
            await asyncio.sleep(0.8)
        await page_state(browser, page_id, "Confirm category selection is stable before confirming the publish category.")
        confirmed = await click_text(
            browser,
            page_id,
            "确认发布该类商品",
            "Confirm the selected Pinduoduo category 女装 > 汉服 > 汉服套装 and enter the product publishing form.",
        )
        if not confirmed:
            raise RuntimeError("Could not find the confirm-publish-category button")
        await asyncio.sleep(2.0)
        state = await page_state(browser, page_id, "Check page after confirming the selected product category.")
        print(json.dumps({"pageId": page_id, "clickedPath": clicked_path, "state": data(state)}, ensure_ascii=False, indent=2))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Pinduoduo merchant publishing helper through ABCP Browser.")
    sub = parser.add_subparsers(dest="phase", required=True)

    login = sub.add_parser("login-phone", help="Open/login page, select account login, type phone, then pause.")
    login.add_argument("--agent-id", default="abcp-agent")
    login.add_argument("--page-id", default=None)
    login.add_argument("--phone", default="13302424940")

    probe = sub.add_parser("probe-entry", help="Probe publish-product entry candidates on the current merchant page.")
    probe.add_argument("--agent-id", default="abcp-agent")
    probe.add_argument("--page-id", required=True)

    open_publish = sub.add_parser("open-publish", help="Open the publish-product workflow from the current merchant page.")
    open_publish.add_argument("--agent-id", default="abcp-agent")
    open_publish.add_argument("--page-id", required=True)

    probe_category = sub.add_parser("probe-category", help="Probe category-selection page DOM structure.")
    probe_category.add_argument("--agent-id", default="abcp-agent")
    probe_category.add_argument("--page-id", required=True)

    select_category = sub.add_parser("select-category", help="Select 女装 > 汉服 > 汉服套装 and confirm.")
    select_category.add_argument("--agent-id", default="abcp-agent")
    select_category.add_argument("--page-id", required=True)

    probe_form = sub.add_parser("probe-form", help="Probe product form controls and upload inputs.")
    probe_form.add_argument("--agent-id", default="abcp-agent")
    probe_form.add_argument("--page-id", required=True)

    upload = sub.add_parser("upload-files", help="Upload prepared product main/detail images into the current form.")
    upload.add_argument("--agent-id", default="abcp-agent")
    upload.add_argument("--page-id", required=True)
    upload.add_argument("--rank", type=int, choices=[6, 7], default=None)
    upload.add_argument("--manifest", default=None)
    upload.add_argument("--product-dir", default=None)
    upload.add_argument("--kind", choices=["main", "detail"], required=True)

    probe_upload = sub.add_parser("probe-upload", help="Probe visible upload trigger candidates.")
    probe_upload.add_argument("--agent-id", default="abcp-agent")
    probe_upload.add_argument("--page-id", required=True)

    fill_draft = sub.add_parser("fill-draft", help="Fill the current Pinduoduo product-add form from a 1688 product manifest, stopping before SKU preview upload and submit.")
    fill_draft.add_argument("--agent-id", default="abcp-agent")
    fill_draft.add_argument("--page-id", required=True)
    fill_draft.add_argument("--manifest", default=None)
    fill_draft.add_argument("--product-dir", default=None)
    fill_draft.add_argument("--skip-detail", action="store_true")

    return parser


async def main() -> int:
    args = build_parser().parse_args()
    if args.phase == "login-phone":
        return await phase_login_phone(args)
    if args.phase == "probe-entry":
        return await phase_probe_entry(args)
    if args.phase == "open-publish":
        return await phase_open_publish(args)
    if args.phase == "probe-category":
        return await phase_probe_category(args)
    if args.phase == "select-category":
        return await phase_select_category(args)
    if args.phase == "probe-form":
        return await phase_probe_form(args)
    if args.phase == "upload-files":
        return await phase_upload_files(args)
    if args.phase == "probe-upload":
        return await phase_probe_upload(args)
    if args.phase == "fill-draft":
        return await phase_fill_draft(args)
    raise RuntimeError(f"Unknown phase: {args.phase}")


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(main()))
    except KeyboardInterrupt:
        raise SystemExit(130)
