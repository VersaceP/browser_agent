"""Extract TAAFT weekly trending ranks 25-35 through ABCP Browser.

This script intentionally lives outside the existing recon/ folder. It uses
ABCP atomic methods only (Page.*, DOM.*, Input.*, Runtime.*) and keeps a hard
cap of two owned ABCP pages for the shared agent.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import csv
import json
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple


ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from abcp_client import ABCPClient, ABCPClientConfig  # noqa: E402


LIST_URL = "https://theresanaiforthat.com/trending/week/"
TARGET_RANKS = list(range(25, 36))
MAX_ABCP_INSTANCES = 2
DEFAULT_AGENT_ID = "recon-shared"
OUTPUT_ROOT = Path(__file__).resolve().parent / "output"
TITLE_PREFIX = "TAAFTX"


JsonDict = Dict[str, Any]


@dataclass
class PageInventory:
    registration: JsonDict
    fleets: List[JsonDict]
    pages: List[JsonDict]


def load_browser_config() -> ABCPClientConfig:
    cfg_path = ROOT / "config.json"
    if not cfg_path.exists():
        raise FileNotFoundError(f"Missing config file: {cfg_path}")
    raw = json.loads(cfg_path.read_text(encoding="utf-8"))
    return ABCPClientConfig.from_dict(raw.get("browser", {}))


def now_stamp() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")


def iso_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )


def one_line(value: Any, limit: int = 240) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."


def extract_page_id(response: JsonDict) -> Optional[str]:
    for key in ("pageId", "id"):
        value = response.get(key)
        if isinstance(value, str) and value:
            return value
    data = response.get("data")
    if isinstance(data, dict):
        for key in ("pageId", "id"):
            value = data.get(key)
            if isinstance(value, str) and value:
                return value
    return None


async def register_and_inventory(
    client: ABCPClient,
    agent_id: str,
) -> PageInventory:
    registration = await client.call("System.register", {"agentId": agent_id})
    fleets_raw = []
    data = registration.get("data")
    if isinstance(data, dict):
        fleets_raw = data.get("fleets") or []

    fleets: List[JsonDict] = []
    for item in fleets_raw:
        if isinstance(item, dict):
            fleets.append(item)
        elif isinstance(item, str):
            fleets.append({"fleetId": item})

    pages: List[JsonDict] = []
    for fleet in fleets:
        fleet_id = fleet.get("fleetId") or fleet.get("id")
        if not fleet_id:
            continue
        try:
            response = await client.call("Page.list", {"fleetId": fleet_id})
        except Exception as exc:
            print(f"[inventory] Page.list({fleet_id}) failed: {exc}")
            continue
        page_data = response.get("data")
        if isinstance(page_data, list):
            page_items = page_data
        elif isinstance(page_data, dict):
            page_items = page_data.get("pages") or page_data.get("items") or []
        else:
            page_items = []
        for page in page_items:
            if not isinstance(page, dict):
                continue
            row = dict(page)
            row.setdefault("fleetId", fleet_id)
            pages.append(row)

    return PageInventory(registration=registration, fleets=fleets, pages=pages)


def page_sort_key(page: JsonDict) -> Tuple[int, str]:
    created = page.get("createdAt") or page.get("updatedAt") or 0
    try:
        created_int = int(created)
    except (TypeError, ValueError):
        created_int = 0
    return created_int, str(page.get("pageId") or page.get("id") or "")


def reusable_pages(pages: Iterable[JsonDict]) -> List[JsonDict]:
    idle_statuses = {"idle", "navigated", "loaded", "complete", "ready"}
    candidates = [
        page for page in pages
        if page.get("pageId") or page.get("id")
    ]
    preferred = [
        page for page in candidates
        if str(page.get("status") or "").lower() in idle_statuses
    ]
    fallback = [
        page for page in candidates
        if str(page.get("status") or "").lower() not in {"crashed", "closed"}
    ]
    selected = preferred or fallback
    return sorted(selected, key=page_sort_key, reverse=True)


def is_safely_closable(page: JsonDict) -> bool:
    status = str(page.get("status") or "").lower()
    # Restrict automatic cleanup to pages that are clearly idle or already bad.
    return status in {"idle", "navigated", "loaded", "complete", "ready", "crashed"}


async def enforce_page_cap(
    client: ABCPClient,
    *,
    agent_id: str,
    inventory: PageInventory,
    max_instances: int,
) -> PageInventory:
    """Close extra idle pages for this shared agent so page count stays <= cap."""
    pages = list(inventory.pages)
    if len(pages) <= max_instances:
        return inventory

    keep_ids = {
        str(page.get("pageId") or page.get("id"))
        for page in reusable_pages(pages)[:max_instances]
        if page.get("pageId") or page.get("id")
    }
    close_candidates = [
        page for page in sorted(pages, key=page_sort_key)
        if str(page.get("pageId") or page.get("id")) not in keep_ids
    ]
    for page in close_candidates:
        page_id = str(page.get("pageId") or page.get("id") or "")
        if not page_id:
            continue
        if not is_safely_closable(page):
            print(
                f"[resource] cannot auto-close page {page_id} "
                f"(status={page.get('status')}); cap still exceeded"
            )
            continue
        print(
            f"[resource] closing extra idle page {page_id} "
            f"(status={page.get('status')}) to enforce cap={max_instances}"
        )
        try:
            await client.call(
                "Page.close",
                {
                    "pageId": page_id,
                    "purpose": (
                        "close extra idle recon-shared page so the extractor "
                        "keeps at most two ABCP pages"
                    ),
                },
            )
            await asyncio.sleep(0.5)
        except Exception as exc:
            print(f"[resource] Page.close({page_id}) failed: {exc}")

    refreshed = await register_and_inventory(client, agent_id)
    if len(refreshed.pages) > max_instances:
        unsafe = [
            {
                "pageId": page.get("pageId") or page.get("id"),
                "status": page.get("status"),
                "url": page.get("url"),
            }
            for page in refreshed.pages
        ]
        raise RuntimeError(
            "Unable to enforce ABCP page cap without closing non-idle pages: "
            + json.dumps(unsafe, ensure_ascii=False)
        )
    return refreshed


async def acquire_page(
    client: ABCPClient,
    *,
    agent_id: str,
    url: str,
    purpose: str,
    max_instances: int,
    wait_rounds: int = 6,
) -> Tuple[str, JsonDict]:
    """Return a pageId, never creating more than max_instances owned pages."""
    attempts: List[JsonDict] = []
    for round_index in range(wait_rounds):
        inventory = await register_and_inventory(client, agent_id)
        inventory = await enforce_page_cap(
            client,
            agent_id=agent_id,
            inventory=inventory,
            max_instances=max_instances,
        )
        pages = inventory.pages
        attempts.append({
            "round": round_index + 1,
            "fleetCount": len(inventory.fleets),
            "pageCount": len(pages),
            "pages": [
                {
                    "pageId": page.get("pageId") or page.get("id"),
                    "status": page.get("status"),
                    "url": page.get("url"),
                    "fleetId": page.get("fleetId"),
                }
                for page in pages
            ],
        })
        owned_instance_count = max(len(inventory.fleets), len(pages))
        print(
            f"[resource] round={round_index + 1} "
            f"fleets={len(inventory.fleets)} pages={len(pages)} "
            f"ownedCount={owned_instance_count}"
        )

        candidates = reusable_pages(pages)
        if candidates:
            page_id = str(candidates[0].get("pageId") or candidates[0].get("id"))
            print(
                f"[resource] reusing page {page_id} "
                f"(status={candidates[0].get('status')})"
            )
            await client.call(
                "Page.navigate",
                {
                    "pageId": page_id,
                    "url": url,
                    "purpose": purpose,
                },
            )
            await asyncio.sleep(3.0)
            await wait_for_url(
                client,
                page_id,
                "/trending/week",
                purpose="verify reused page reached trending list",
            )
            return page_id, {
                "mode": "reused",
                "pageId": page_id,
                "attempts": attempts,
            }

        if owned_instance_count < max_instances:
            print("[resource] no reusable page; creating one within cap")
            response = await client.call(
                "Page.create",
                {
                    "url": url,
                    "purpose": purpose,
                },
            )
            page_id = extract_page_id(response)
            if not page_id:
                raise RuntimeError(
                    "Page.create returned no pageId: "
                    + json.dumps(response, ensure_ascii=False)[:500]
                )
            await asyncio.sleep(3.0)
            await wait_for_url(
                client,
                page_id,
                "/trending/week",
                purpose="verify created page reached trending list",
            )
            return page_id, {
                "mode": "created",
                "pageId": page_id,
                "attempts": attempts,
                "createResponse": response,
            }

        print(
            f"[resource] cap reached ({owned_instance_count}/{max_instances}); "
            "waiting for an idle page"
        )
        await asyncio.sleep(4.0)

    raise RuntimeError(
        f"No idle ABCP page available and cap={max_instances} prevents Page.create"
    )


async def get_state(client: ABCPClient, page_id: str, purpose: str) -> JsonDict:
    return await client.call(
        "Page.getState",
        {"pageId": page_id, "purpose": purpose},
    )


async def wait_for_url(
    client: ABCPClient,
    page_id: str,
    expected_fragment: str,
    *,
    purpose: str,
    attempts: int = 10,
    sleep_seconds: float = 1.5,
) -> JsonDict:
    last_state: JsonDict = {}
    for attempt in range(1, attempts + 1):
        last_state = await get_state(
            client,
            page_id,
            f"{purpose} (url check {attempt}/{attempts})",
        )
        url = str((last_state.get("data") or {}).get("url") or "")
        status = str((last_state.get("data") or {}).get("status") or "")
        if expected_fragment in url:
            return last_state
        print(
            f"[state] waiting for URL fragment {expected_fragment!r}; "
            f"current={url!r} status={status!r}"
        )
        await asyncio.sleep(sleep_seconds)
    raise RuntimeError(
        f"Page did not reach expected URL fragment {expected_fragment!r}: "
        + json.dumps(last_state.get("data") or last_state, ensure_ascii=False)[:800]
    )


async def scroll_down(
    client: ABCPClient,
    page_id: str,
    *,
    amount: int,
    purpose: str,
    sleep_seconds: float = 1.0,
) -> None:
    await client.call(
        "Input.scroll",
        {
            "pageId": page_id,
            "direction": "down",
            "amount": amount,
            "purpose": purpose,
        },
    )
    await asyncio.sleep(sleep_seconds)


async def run_js(client: ABCPClient, page_id: str, expression: str, purpose: str) -> JsonDict:
    return await client.call(
        "Runtime.evaluate",
        {
            "pageId": page_id,
            "expression": expression,
            "returnByValue": True,
            "purpose": purpose,
        },
    )


async def eval_json_via_title(
    client: ABCPClient,
    page_id: str,
    payload_expression: str,
    *,
    purpose: str,
    chunk_chars: int = 700,
    max_chunks: int = 300,
) -> Any:
    """Evaluate JS and retrieve JSON through document.title chunks.

    Some ABCP Runtime.evaluate deployments do not reliably return complex
    values by value. The title side-channel has been proven stable in local
    recon scripts, so this helper uses it as the main path.
    """
    safe_prefix = TITLE_PREFIX.replace("|", "")
    setup = f"""
(() => {{
  try {{
    window.__taaftPayload = ({payload_expression});
    window.__taaftText = JSON.stringify(window.__taaftPayload ?? null);
  }} catch (err) {{
    window.__taaftText = JSON.stringify({{
      error: String(err && err.message || err),
      stack: String(err && err.stack || "")
    }});
  }}
  const bytes = new TextEncoder().encode(window.__taaftText);
  let binary = "";
  const step = 0x8000;
  for (let i = 0; i < bytes.length; i += step) {{
    binary += String.fromCharCode.apply(null, bytes.subarray(i, i + step));
  }}
  window.__taaftTextB64 = btoa(binary);
  window.__taaftOffset = 0;
  document.title = "{safe_prefix}|READY|" + String(window.__taaftTextB64.length);
}})()
"""
    await run_js(client, page_id, setup, purpose)
    ready = await get_state(client, page_id, "read JSON side-channel ready marker")
    title = str((ready.get("data") or {}).get("title") or "")
    if not title.startswith(f"{safe_prefix}|READY|"):
        raise RuntimeError(f"JSON side-channel did not initialize: {title[:200]!r}")
    try:
        total_len = int(title.rsplit("|", 1)[-1])
    except ValueError as exc:
        raise RuntimeError(f"Invalid side-channel ready title: {title!r}") from exc

    chunks: List[str] = []
    while sum(len(chunk) for chunk in chunks) < total_len:
        if len(chunks) >= max_chunks:
            raise RuntimeError(
                f"Too many JSON chunks ({len(chunks)}); expected {total_len} chars"
            )
        chunk_expr = f"""
(() => {{
  const text = String(window.__taaftTextB64 || "bnVsbA==");
  const start = Number(window.__taaftOffset || 0);
  const chunk = text.slice(start, start + {int(chunk_chars)});
  window.__taaftOffset = start + chunk.length;
  document.title = "{safe_prefix}|CHUNK|" + String(start) + "|" + chunk;
}})()
"""
        await run_js(client, page_id, chunk_expr, "emit JSON side-channel chunk")
        state = await get_state(client, page_id, "read JSON side-channel chunk")
        title = str((state.get("data") or {}).get("title") or "")
        parts = title.split("|", 3)
        if len(parts) != 4 or parts[0] != safe_prefix or parts[1] != "CHUNK":
            raise RuntimeError(f"Invalid side-channel chunk title: {title[:200]!r}")
        chunks.append(parts[3])

    encoded = "".join(chunks)
    if len(encoded) != total_len:
        raise RuntimeError(
            f"Side-channel length mismatch: got {len(encoded)}, expected {total_len}"
        )
    text = base64.b64decode(encoded.encode("ascii")).decode("utf-8")
    return json.loads(text)


LIST_EXTRACT_JS = r"""
(function() {
  function norm(text) {
    return String(text || '').replace(/\s+/g, ' ').trim();
  }
  function visibleBox(el) {
    const style = window.getComputedStyle(el);
    if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') return false;
    const r = el.getBoundingClientRect();
    return r.width > 0 && r.height > 0;
  }
  function productHrefInfo(href) {
    try {
      if (!href) return null;
      if (/^https?:/i.test(href)) {
        const u = new URL(href);
        if (u.host !== location.host) return null;
        href = u.pathname;
      }
      if (!href.startsWith('/')) return null;
      const path = href.split('#')[0].split('?')[0].replace(/\/+$/, '');
      const parts = path.split('/').filter(Boolean);
      if (parts.length !== 2 || parts[0] !== 'ai') return null;
      const slug = parts[1];
      if (!slug || slug.length < 2) return null;
      return { slug, url: location.origin + '/ai/' + slug + '/' };
    } catch (_) {
      return null;
    }
  }
  function productImageName(root) {
    for (const img of root.querySelectorAll('img[alt]')) {
      const alt = norm(img.getAttribute('alt'));
      if (!alt) continue;
      if (/profile picture|video demo|TAAFT|logo/i.test(alt)) continue;
      return alt;
    }
    return '';
  }
  function plausibleName(text) {
    if (!text) return false;
    if (text.length > 100) return false;
    if (/^#\d+\s+in\s+Trending$/i.test(text)) return false;
    if (/^[\d,\s.]+$/.test(text)) return false;
    if (/^(free|paid|freemium|\$|from\s+\$)/i.test(text)) return false;
    if (/karma$/i.test(text) || /profile picture/i.test(text)) return false;
    if (/^www\.|\.com$|\.ai$|\.io$/i.test(text)) return false;
    return true;
  }
  function nearestCard(a) {
    let node = a;
    for (let depth = 0; node && depth < 10; depth++, node = node.parentElement) {
      if (!visibleBox(node)) continue;
      const text = norm(node.innerText || node.textContent);
      if (text.length < 20) continue;
      if (text.length > 6000) continue;
      if (node.querySelector('a[href*="/ai/"]') && productImageName(node)) return node;
      if (node.querySelector('a[href*="/ai/"]') && /#\d+\s+in\s+Trending/i.test(text)) return node;
    }
    return a;
  }
  function productName(a, card) {
    const img = productImageName(card);
    if (img) return img;
    const own = norm(a.textContent);
    if (plausibleName(own)) return own;
    const names = Array.from(card.querySelectorAll('a[href*="/ai/"], h2, h3, h4, strong, b'))
      .map(el => norm(el.textContent))
      .filter(plausibleName);
    return names[0] || '';
  }
  function categoryText(card) {
    return Array.from(card.querySelectorAll('a, span'))
      .map(x => norm(x.textContent))
      .find(x => /[\u{1F300}-\u{1FAFF}]/u.test(x) && x.length < 80) || '';
  }
  function rankBadge(card) {
    const text = norm(card.innerText || card.textContent);
    const match = text.match(/#\d+\s+in\s+Trending/i);
    return match ? match[0] : '';
  }

  const products = [];
  const seen = new Set();
  const anchors = Array.from(document.querySelectorAll('a[href]'));
  anchors.forEach((a, domOrder) => {
    if (!visibleBox(a)) return;
    const info = productHrefInfo(a.getAttribute('href') || a.href || '');
    if (!info || seen.has(info.slug)) return;
    const card = nearestCard(a);
    if (!visibleBox(card)) return;
    const name = productName(a, card);
    if (!name) return;
    seen.add(info.slug);
    const rect = card.getBoundingClientRect();
    const cardText = norm(card.innerText || card.textContent);
    products.push({
      status: 'found',
      product_name: name,
      product_url: info.url,
      slug: info.slug,
      category: categoryText(card),
      trendingBadge: rankBadge(card),
      cardText: cardText.slice(0, 3000),
      pageY: Math.round((rect.top || 0) + window.scrollY),
      x: Math.round(rect.left || 0),
      domOrder,
      collectedAt: new Date().toISOString()
    });
  });
  products.sort((a, b) => (a.pageY - b.pageY) || (a.x - b.x) || (a.domOrder - b.domOrder));
  return {
    url: location.href,
    title: document.title,
    scrollY: window.scrollY,
    documentHeight: Math.max(document.body.scrollHeight, document.documentElement.scrollHeight),
    productCount: products.length,
    products
  };
})()
"""


DETAIL_EXTRACT_JS = r"""
(function() {
  function norm(text) {
    return String(text || '').replace(/\s+/g, ' ').trim();
  }
  function visible(el) {
    const style = window.getComputedStyle(el);
    return style.display !== 'none' && style.visibility !== 'hidden' && style.opacity !== '0';
  }
  function uniq(items) {
    const out = [];
    const seen = new Set();
    for (const raw of items) {
      const text = norm(raw);
      if (!text || seen.has(text)) continue;
      seen.add(text);
      out.push(text);
    }
    return out;
  }
  function textOf(el, maxLen = 8000) {
    return norm(el ? (el.innerText || el.textContent || '') : '').slice(0, maxLen);
  }
  function headingText(el) {
    return norm(el.innerText || el.textContent || el.getAttribute('aria-label') || '');
  }
  function findHeadings(pattern) {
    const nodes = Array.from(document.querySelectorAll(
      'h1,h2,h3,h4,h5,[role="heading"],summary,button,a,span,div'
    ));
    return nodes.filter(el => {
      const text = headingText(el);
      if (!text || text.length > 120) return false;
      return pattern.test(text);
    });
  }
  function nearestSection(el, maxLen = 12000) {
    let node = el;
    let best = el;
    for (let depth = 0; node && depth < 8; depth++, node = node.parentElement) {
      const text = textOf(node, maxLen + 2000);
      if (text.length > 30 && text.length <= maxLen) best = node;
      if (/^(SECTION|ARTICLE|MAIN|ASIDE)$/i.test(node.tagName || '')) break;
    }
    return best;
  }
  function sectionByHeading(pattern, maxLen = 12000) {
    const heads = findHeadings(pattern);
    const sections = [];
    for (const head of heads) {
      const sec = nearestSection(head, maxLen);
      const text = textOf(sec, maxLen);
      if (text) sections.push({ heading: headingText(head), text });
    }
    return sections;
  }
  function listItemsInSections(sections, rejectPattern) {
    const items = [];
    for (const sec of sections) {
      const candidates = Array.from(sec.element ? sec.element.querySelectorAll('li,p,div,span') : []);
      for (const el of candidates) {
        const text = textOf(el, 800);
        if (text.length < 8 || text.length > 700) continue;
        if (rejectPattern && rejectPattern.test(text)) continue;
        items.push(text);
      }
    }
    return uniq(items);
  }
  function sectionsWithElements(pattern, maxLen = 12000) {
    const heads = findHeadings(pattern);
    const sections = [];
    for (const head of heads) {
      const element = nearestSection(head, maxLen);
      const text = textOf(element, maxLen);
      if (text) sections.push({ heading: headingText(head), text, element });
    }
    return sections;
  }
  function extractProsCons() {
    const proSections = sectionsWithElements(/\b(pros?|advantages?|what.*like|benefits?)\b/i, 10000);
    const conSections = sectionsWithElements(/\b(cons?|disadvantages?|limitations?|what.*(dislike|better)|drawbacks?)\b/i, 10000);
    const combined = sectionsWithElements(/pros?\s*(?:&|and|\/)\s*cons?|strengths?\s*(?:&|and|\/)\s*weaknesses?/i, 14000);
    const reject = /^(pros?|cons?|advantages?|disadvantages?|reviews?|questions?|q&a)$/i;
    let pros = listItemsInSections(proSections, reject);
    let cons = listItemsInSections(conSections, reject);
    const combinedText = uniq(combined.map(x => x.text)).join('\n\n').slice(0, 12000);
    if ((!pros.length || !cons.length) && combinedText) {
      const lines = combinedText.split(/(?:\n|•|- |\u2022)/).map(norm).filter(Boolean);
      let bucket = '';
      for (const line of lines) {
        if (/^pros?\b/i.test(line)) {
          bucket = 'pros';
          continue;
        }
        if (/^cons?\b/i.test(line)) {
          bucket = 'cons';
          continue;
        }
        if (line.length < 8 || line.length > 500) continue;
        if (bucket === 'pros') pros.push(line);
        if (bucket === 'cons') cons.push(line);
      }
    }
    return {
      pros: uniq(pros).slice(0, 30),
      cons: uniq(cons).slice(0, 30),
      rawText: combinedText || uniq([...proSections, ...conSections].map(x => x.text)).join('\n\n').slice(0, 12000)
    };
  }
  function extractReviews() {
    const sections = sectionsWithElements(/\b(reviews?|ratings?|comments?|testimonials?)\b/i, 18000);
    const containers = [];
    for (const section of sections) {
      for (const el of section.element.querySelectorAll('article,li,blockquote,[class*="review" i],[class*="comment" i],p,div')) {
        const text = textOf(el, 1800);
        if (text.length < 30 || text.length > 1600) continue;
        if (/^(reviews?|ratings?|comments?)$/i.test(text)) continue;
        if (/^(log in|sign up|search|home)$/i.test(text)) continue;
        containers.push(text);
      }
    }
    const rawText = uniq(sections.map(x => x.text)).join('\n\n').slice(0, 18000);
    return {
      countText: (document.body.innerText.match(/\b\d+\s+reviews?\b/i) || [null])[0],
      items: uniq(containers).slice(0, 25),
      rawText
    };
  }
  function extractQA() {
    const sections = sectionsWithElements(/\b(q\s*&\s*a|questions?|answers?|faq|frequently asked)\b/i, 18000);
    const pairs = [];
    for (const section of sections) {
      for (const details of section.element.querySelectorAll('details')) {
        const q = textOf(details.querySelector('summary'), 600);
        const a = textOf(details, 1800).replace(q, '').trim();
        if (q || a) pairs.push({ question: q, answer: a });
      }
      const questionNodes = Array.from(section.element.querySelectorAll('h3,h4,summary,button,p,div'))
        .filter(el => {
          const text = textOf(el, 500);
          return text.endsWith('?') && text.length >= 8 && text.length <= 500;
        });
      for (const qNode of questionNodes) {
        let answer = '';
        let sib = qNode.nextElementSibling;
        for (let i = 0; sib && i < 3; i++, sib = sib.nextElementSibling) {
          const text = textOf(sib, 1200);
          if (text && text !== textOf(qNode, 500)) {
            answer = text;
            break;
          }
        }
        pairs.push({ question: textOf(qNode, 500), answer });
      }
    }
    const seen = new Set();
    const cleanPairs = [];
    for (const pair of pairs) {
      const q = norm(pair.question);
      const a = norm(pair.answer);
      const key = q + '|' + a;
      if ((!q && !a) || seen.has(key)) continue;
      seen.add(key);
      cleanPairs.push({ question: q, answer: a });
    }
    return {
      items: cleanPairs.slice(0, 30),
      rawText: uniq(sections.map(x => x.text)).join('\n\n').slice(0, 18000)
    };
  }
  function leafTexts(root, maxLen = 700, requireVisible = false) {
    const nodes = Array.from(root.querySelectorAll('*'))
      .filter(el => !requireVisible || visible(el));
    const out = [];
    for (const el of nodes) {
      const text = textOf(el, maxLen + 20);
      if (!text || text.length > maxLen) continue;
      const children = Array.from(el.children || []);
      if (children.some(child => textOf(child, maxLen + 20) === text)) continue;
      out.push(text);
    }
    return uniq(out);
  }
  function sectionById(id, maxLen = 50000) {
    const element = document.getElementById(id);
    if (!element) return null;
    const text = textOf(element, maxLen);
    if (!text) return null;
    return { element, text };
  }
  function smallestSectionContaining(pattern, requiredPattern, maxLen = 40000) {
    const candidates = [];
    for (const el of document.querySelectorAll('main,section,article,div')) {
      if (!visible(el)) continue;
      const text = textOf(el, maxLen + 1000);
      if (text.length < 40 || text.length > maxLen) continue;
      if (!pattern.test(text)) continue;
      if (requiredPattern && !requiredPattern.test(text)) continue;
      candidates.push({ element: el, text });
    }
    candidates.sort((a, b) => a.text.length - b.text.length);
    return candidates[0] || null;
  }
  function extractStrictProsCons() {
    const section = sectionById('pros-and-cons', 70000) || smallestSectionContaining(
      /pros\s*(?:&|and)?\s*cons/i,
      /\bPros\b[\s\S]*\bCons\b/i,
      50000
    );
    if (!section) return { pros: [], cons: [], rawText: '' };
    const leaves = leafTexts(section.element, 500, false);
    let mode = '';
    const pros = [];
    const cons = [];
    const stop = /^(reviews?|q\s*&\s*a|questions?|prompts?|pricing|alternatives?|overview|releases?|author|related topics|featured alternatives)$/i;
    for (const line of leaves) {
      if (/^pros?\b/i.test(line)) {
        mode = 'pros';
        continue;
      }
      if (/^cons?\b/i.test(line)) {
        mode = 'cons';
        continue;
      }
      if (stop.test(line)) {
        if (mode) break;
        continue;
      }
      if (!mode) continue;
      if (line.length < 4 || line.length > 450) continue;
      if (/view \d+ more/i.test(line)) continue;
      if (mode === 'pros') pros.push(line);
      if (mode === 'cons') cons.push(line);
    }
    return {
      pros: uniq(pros).slice(0, 80),
      cons: uniq(cons).slice(0, 80),
      rawText: section.text.slice(0, 30000)
    };
  }
  function extractStrictQA() {
    const section = sectionById('faq', 90000) || smallestSectionContaining(
      /\bQ\s*&\s*A\b|frequently asked|What is|How does/i,
      /\?/,
      70000
    );
    if (!section) return { items: [], rawText: '' };
    const leaves = leafTexts(section.element, 1800, false);
    const pairs = [];
    for (let i = 0; i < leaves.length; i++) {
      const q = leaves[i];
      if (!/\?$/.test(q) || q.length < 8 || q.length > 700) continue;
      if (/profile picture|karma|newsletter|sign up/i.test(q)) continue;
      let answer = '';
      for (let j = i + 1; j < Math.min(i + 5, leaves.length); j++) {
        const candidate = leaves[j];
        if (/\?$/.test(candidate)) break;
        if (/^\+?\s*show \d+ more/i.test(candidate)) continue;
        if (/^(ask a question|submit|q\s*&\s*a)$/i.test(candidate)) continue;
        if (candidate.length >= 20) {
          answer = candidate;
          break;
        }
      }
      pairs.push({ question: q, answer });
    }
    return {
      items: uniq(pairs.map(pair => JSON.stringify(pair))).map(text => JSON.parse(text)).slice(0, 80),
      rawText: section.text.slice(0, 50000)
    };
  }
  function extractStrictReviews() {
    const section = sectionById('rw_cont', 70000)
      || sectionById('discussion', 70000)
      || smallestSectionContaining(
      /\b(reviews?|discussion|comments?)\b/i,
      /karma|🙏|\b\d+\s+\d+\b|review/i,
      60000
    );
    if (!section) return { countText: null, items: [], rawText: '' };
    const leaves = leafTexts(section.element, 1500, false);
    const items = [];
    for (const line of leaves) {
      if (line.length < 20 || line.length > 1400) continue;
      if (/^(reviews?|discussion|comments?|overview|pricing|pros|cons|q\s*&\s*a)$/i.test(line)) continue;
      if (/^(free mode|home search|tools \d|investors \d|featured alternatives)/i.test(line)) continue;
      if (/karma|🙏|review|good|great|useful|easy|waste|brilliant|concern|recommend|experience|love|like|better/i.test(line)) {
        items.push(line);
      }
    }
    const body = textOf(document.body, 80000);
    return {
      countText: (body.match(/\b\d+\s*(?:reviews?|comments?|discussion)\b/i) || [null])[0],
      items: uniq(items).slice(0, 80),
      rawText: section.text.slice(0, 50000)
    };
  }

  for (const detail of document.querySelectorAll('details')) {
    try { detail.open = true; } catch (_) {}
  }

  const productName = norm((document.querySelector('h1') || {}).innerText || '');
  const strictProsCons = extractStrictProsCons();
  const looseProsCons = extractProsCons();
  const prosCons = (strictProsCons.pros.length || strictProsCons.cons.length)
    ? strictProsCons
    : looseProsCons;
  const strictReviews = extractStrictReviews();
  const looseReviews = extractReviews();
  const reviews = strictReviews.items.length ? strictReviews : looseReviews;
  const strictQA = extractStrictQA();
  const looseQA = extractQA();
  const qa = strictQA.items.length ? strictQA : looseQA;
  const bodyText = textOf(document.body, 100000);
  return {
    url: location.href,
    title: document.title,
    productName,
    ratingText: (bodyText.match(/\b\d(?:\.\d)?\s*(?:\/\s*5|stars?)\b/i) || [null])[0],
    reviews,
    pros: prosCons.pros,
    cons: prosCons.cons,
    prosConsRawText: prosCons.rawText,
    qa,
    sectionHeadings: uniq(Array.from(document.querySelectorAll('h1,h2,h3,h4,[role="heading"]')).map(headingText)).slice(0, 80),
    extractedAt: new Date().toISOString()
  };
})()
"""


def merge_products(current: Dict[str, JsonDict], rows: Iterable[JsonDict]) -> None:
    for row in rows:
        if not isinstance(row, dict):
            continue
        slug = str(row.get("slug") or "").strip()
        if not slug:
            continue
        previous = current.get(slug)
        if previous is None:
            current[slug] = row
            continue
        prev_y = previous.get("pageY")
        row_y = row.get("pageY")
        try:
            better_position = int(row_y) < int(prev_y)
        except (TypeError, ValueError):
            better_position = False
        better_name = (
            len(str(row.get("product_name") or ""))
            > len(str(previous.get("product_name") or ""))
        )
        if better_position or better_name:
            current[slug] = {**previous, **row}


async def collect_target_products(client: ABCPClient, page_id: str) -> List[JsonDict]:
    print("[list] warming up list page")
    await asyncio.sleep(8.0)
    await wait_for_url(
        client,
        page_id,
        "/trending/week",
        purpose="confirm trending page state before extraction",
    )
    current: Dict[str, JsonDict] = {}
    last_height = 0
    stagnant_rounds = 0

    for round_index in range(1, 18):
        payload = await eval_json_via_title(
            client,
            page_id,
            LIST_EXTRACT_JS,
            purpose=f"extract product cards from list round {round_index}",
        )
        products = payload.get("products") if isinstance(payload, dict) else []
        if isinstance(products, list):
            merge_products(current, products)
        height = int(payload.get("documentHeight") or 0) if isinstance(payload, dict) else 0
        print(
            f"[list] round={round_index} products={len(current)} "
            f"visibleProducts={payload.get('productCount') if isinstance(payload, dict) else '?'} "
            f"height={height} scrollY={payload.get('scrollY') if isinstance(payload, dict) else '?'}"
        )
        if height and height == last_height:
            stagnant_rounds += 1
        else:
            stagnant_rounds = 0
        last_height = height
        if len(current) >= max(TARGET_RANKS) and stagnant_rounds >= 2:
            print("[list] collected enough products and page height is stable")
            break
        if stagnant_rounds >= 5:
            print("[list] document height stopped changing; ending scroll search")
            break
        await scroll_down(
            client,
            page_id,
            amount=1600,
            purpose=f"load more trending products for visual positions 25-35 round {round_index}",
            sleep_seconds=1.4,
        )

    ordered = sorted(
        current.values(),
        key=lambda row: (
            int(row.get("pageY") or 0),
            int(row.get("x") or 0),
            int(row.get("domOrder") or 0),
        ),
    )
    rows: List[JsonDict] = []
    for position in TARGET_RANKS:
        index = position - 1
        if 0 <= index < len(ordered):
            row = dict(ordered[index])
            row["rank"] = position
            row["visual_position"] = position
            row["status"] = "found"
        else:
            row = {
                "rank": position,
                "visual_position": position,
                "status": "missing_on_list",
                "product_name": None,
                "product_url": None,
            }
        rows.append(row)
    return rows


async def prepare_detail_page(client: ABCPClient, page_id: str, url: str, rank: int) -> JsonDict:
    print(f"[detail] rank={rank} navigate {url}")
    await client.call(
        "Page.navigate",
        {
            "pageId": page_id,
            "url": url,
            "purpose": f"open product detail page for rank {rank}",
        },
    )
    await asyncio.sleep(5.0)
    expected = "/" + "/".join(str(url).split("/", 3)[3:]).strip("/")
    state = await wait_for_url(
        client,
        page_id,
        expected,
        purpose=f"snapshot product detail rank {rank}",
        attempts=8,
    )
    for i in range(5):
        await scroll_down(
            client,
            page_id,
            amount=1300,
            purpose=f"load lower detail sections for rank {rank} pass {i + 1}",
            sleep_seconds=0.8,
        )
    await asyncio.sleep(1.0)
    return state


def section_status(items: Any, raw_text: Any) -> str:
    if isinstance(items, list) and items:
        return "found"
    if isinstance(raw_text, str) and raw_text.strip():
        return "found_raw_text"
    return "not_present"


def normalize_detail(product: JsonDict, detail: JsonDict, state: JsonDict) -> JsonDict:
    reviews = detail.get("reviews") if isinstance(detail.get("reviews"), dict) else {}
    qa = detail.get("qa") if isinstance(detail.get("qa"), dict) else {}
    pros = detail.get("pros") if isinstance(detail.get("pros"), list) else []
    cons = detail.get("cons") if isinstance(detail.get("cons"), list) else []

    return {
        "rank": product.get("rank"),
        "product_name": product.get("product_name") or detail.get("productName"),
        "product_url": product.get("product_url"),
        "slug": product.get("slug"),
        "list_status": product.get("status"),
        "detail_status": "found",
        "page_state": {
            "url": (state.get("data") or {}).get("url") if isinstance(state, dict) else None,
            "title": (state.get("data") or {}).get("title") if isinstance(state, dict) else None,
            "status": (state.get("data") or {}).get("status") if isinstance(state, dict) else None,
        },
        "rating_text": detail.get("ratingText"),
        "reviews": {
            "status": section_status(reviews.get("items"), reviews.get("rawText")),
            "count_text": reviews.get("countText"),
            "items": reviews.get("items") or [],
            "raw_text": reviews.get("rawText") or "",
        },
        "pros": {
            "status": section_status(pros, detail.get("prosConsRawText")),
            "items": pros,
        },
        "cons": {
            "status": section_status(cons, detail.get("prosConsRawText")),
            "items": cons,
        },
        "pros_cons_raw_text": detail.get("prosConsRawText") or "",
        "qa": {
            "status": section_status(qa.get("items"), qa.get("rawText")),
            "items": qa.get("items") or [],
            "raw_text": qa.get("rawText") or "",
        },
        "section_headings": detail.get("sectionHeadings") or [],
        "extracted_at": detail.get("extractedAt") or iso_now(),
    }


async def collect_details(
    client: ABCPClient,
    page_id: str,
    products: List[JsonDict],
) -> List[JsonDict]:
    results: List[JsonDict] = []
    for product in products:
        rank = product.get("rank")
        url = product.get("product_url")
        if product.get("status") != "found" or not url:
            results.append({
                "rank": rank,
                "product_name": product.get("product_name"),
                "product_url": url,
                "list_status": product.get("status"),
                "detail_status": "skipped_missing_list_url",
                "reviews": {"status": "not_collected", "items": [], "raw_text": ""},
                "pros": {"status": "not_collected", "items": []},
                "cons": {"status": "not_collected", "items": []},
                "qa": {"status": "not_collected", "items": [], "raw_text": ""},
            })
            continue
        try:
            state = await prepare_detail_page(client, page_id, str(url), int(rank))
            detail = await eval_json_via_title(
                client,
                page_id,
                DETAIL_EXTRACT_JS,
                purpose=f"extract reviews pros cons Q&A for rank {rank}",
                chunk_chars=650,
            )
            if not isinstance(detail, dict) or detail.get("error"):
                raise RuntimeError(json.dumps(detail, ensure_ascii=False)[:500])
            normalized = normalize_detail(product, detail, state)
            results.append(normalized)
            print(
                f"[detail] rank={rank} done "
                f"reviews={normalized['reviews']['status']} "
                f"pros={len(normalized['pros']['items'])} "
                f"cons={len(normalized['cons']['items'])} "
                f"qa={normalized['qa']['status']}"
            )
        except Exception as exc:
            print(f"[detail] rank={rank} failed: {exc}")
            results.append({
                "rank": rank,
                "product_name": product.get("product_name"),
                "product_url": url,
                "list_status": product.get("status"),
                "detail_status": "failed",
                "error": str(exc),
                "reviews": {"status": "failed", "items": [], "raw_text": ""},
                "pros": {"status": "failed", "items": []},
                "cons": {"status": "failed", "items": []},
                "qa": {"status": "failed", "items": [], "raw_text": ""},
            })
    return results


def write_csv(path: Path, rows: List[JsonDict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = [
        "rank",
        "product_name",
        "product_url",
        "detail_status",
        "reviews_status",
        "review_count",
        "pros_count",
        "cons_count",
        "qa_status",
        "qa_count",
        "rating_text",
    ]
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            reviews = row.get("reviews") if isinstance(row.get("reviews"), dict) else {}
            pros = row.get("pros") if isinstance(row.get("pros"), dict) else {}
            cons = row.get("cons") if isinstance(row.get("cons"), dict) else {}
            qa = row.get("qa") if isinstance(row.get("qa"), dict) else {}
            writer.writerow({
                "rank": row.get("rank"),
                "product_name": row.get("product_name"),
                "product_url": row.get("product_url"),
                "detail_status": row.get("detail_status"),
                "reviews_status": reviews.get("status"),
                "review_count": len(reviews.get("items") or []),
                "pros_count": len(pros.get("items") or []),
                "cons_count": len(cons.get("items") or []),
                "qa_status": qa.get("status"),
                "qa_count": len(qa.get("items") or []),
                "rating_text": row.get("rating_text"),
            })


def validate_results(products: List[JsonDict], details: List[JsonDict]) -> JsonDict:
    found_ranks = [row.get("rank") for row in products if row.get("status") == "found"]
    detail_done = [row.get("rank") for row in details if row.get("detail_status") == "found"]
    missing = [rank for rank in TARGET_RANKS if rank not in found_ranks]
    failed_details = [
        {
            "rank": row.get("rank"),
            "product_url": row.get("product_url"),
            "status": row.get("detail_status"),
            "error": row.get("error"),
        }
        for row in details
        if row.get("detail_status") != "found"
    ]
    urls = [row.get("product_url") for row in products if row.get("product_url")]
    return {
        "expectedRanks": TARGET_RANKS,
        "listFoundRanks": found_ranks,
        "detailDoneRanks": detail_done,
        "missingListRanks": missing,
        "uniqueUrlCount": len(set(urls)),
        "urlCount": len(urls),
        "failedDetails": failed_details,
        "complete": not missing and not failed_details and len(set(urls)) == len(TARGET_RANKS),
    }


def validate_list_quality(products: List[JsonDict]) -> None:
    problems = []
    for row in products:
        url = str(row.get("product_url") or "")
        name = str(row.get("product_name") or "").strip()
        if row.get("status") != "found":
            problems.append(f"rank {row.get('rank')} missing")
        if not url.startswith("https://theresanaiforthat.com/ai/"):
            problems.append(f"rank {row.get('rank')} invalid url {url!r}")
        if not name:
            problems.append(f"rank {row.get('rank')} missing name")
        text = str(row.get("cardText") or "")
        if "Featured alternatives" in text or "People also viewed" in text:
            problems.append(
                f"rank {row.get('rank')} looks like a detail/sidebar card, not trending"
            )
    if problems:
        raise RuntimeError("List extraction quality check failed: " + "; ".join(problems))


async def run(args: argparse.Namespace) -> int:
    out_dir = OUTPUT_ROOT / now_stamp()
    out_dir.mkdir(parents=True, exist_ok=True)
    print(f"[output] {out_dir}")

    cfg = load_browser_config()
    resource_log: JsonDict = {
        "agentId": args.agent_id,
        "maxAbcpInstances": MAX_ABCP_INSTANCES,
        "startedAt": iso_now(),
    }

    async with ABCPClient(cfg) as client:
        page_id, acquire_log = await acquire_page(
            client,
            agent_id=args.agent_id,
            url=LIST_URL,
            purpose="open TAAFT weekly trending list for ranks 25-35 extraction",
            max_instances=MAX_ABCP_INSTANCES,
        )
        resource_log["acquire"] = acquire_log
        resource_log["workingPageId"] = page_id
        write_json(out_dir / "resource_log.json", resource_log)

        products = await collect_target_products(client, page_id)
        for product in products:
            print(
                f"[list] rank={product.get('rank')} "
                f"status={product.get('status')} "
                f"name={one_line(product.get('product_name'), 80)} "
                f"url={product.get('product_url')}"
            )
        validate_list_quality(products)
        write_json(out_dir / "list_products_25_35.json", products)

        details = await collect_details(client, page_id, products)
        validation = validate_results(products, details)
        final_payload = {
            "source": LIST_URL,
            "scrapedAt": iso_now(),
            "agentId": args.agent_id,
            "maxAbcpInstances": MAX_ABCP_INSTANCES,
            "workingPageId": page_id,
            "resource": resource_log,
            "validation": validation,
            "products": details,
        }
        write_json(out_dir / "products_25_35_details.json", final_payload)
        write_csv(out_dir / "products_25_35_summary.csv", details)
        print(f"[done] JSON: {out_dir / 'products_25_35_details.json'}")
        print(f"[done] CSV:  {out_dir / 'products_25_35_summary.csv'}")
        print(f"[done] validation: {json.dumps(validation, ensure_ascii=False)}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Extract TAAFT weekly trending ranks 25-35 through ABCP Browser."
    )
    parser.add_argument(
        "--agent-id",
        default=os.environ.get("ABCP_AGENT_ID", DEFAULT_AGENT_ID),
        help=(
            "Shared ABCP agent id. Defaults to recon-shared so idle recon pages "
            "are reused instead of creating more instances."
        ),
    )
    return parser


def main() -> int:
    return asyncio.run(run(build_parser().parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
