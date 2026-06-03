"""Probe current ABCP detail page structure for TAAFT tabs."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from abcp_client import ABCPClient  # noqa: E402
from taaft_abcp_extract.extract_trending_25_35 import (  # noqa: E402
    DEFAULT_AGENT_ID,
    eval_json_via_title,
    load_browser_config,
    register_and_inventory,
    reusable_pages,
)


JS = r"""
(function() {
  function norm(text) {
    return String(text || '').replace(/\s+/g, ' ').trim();
  }
  const items = [];
  for (const el of document.querySelectorAll('a[href],button,[role="tab"],summary')) {
    const text = norm(el.innerText || el.textContent || el.getAttribute('aria-label') || '');
    if (!text) continue;
    if (!/Pros|Cons|Reviews|Q&A|Overview|Pricing|Alternatives|Prompts|Discussion/i.test(text)) continue;
    const r = el.getBoundingClientRect();
    items.push({
      tag: el.tagName,
      role: el.getAttribute('role'),
      text,
      href: el.getAttribute('href') || '',
      cls: String(el.className || ''),
      ariaSelected: el.getAttribute('aria-selected'),
      ariaExpanded: el.getAttribute('aria-expanded'),
      y: Math.round(r.top + window.scrollY),
      visible: r.width > 0 && r.height > 0
    });
  }
  return {
    url: location.href,
    title: document.title,
    items
  };
})()
"""


async def main() -> int:
    async with ABCPClient(load_browser_config()) as client:
        inventory = await register_and_inventory(client, DEFAULT_AGENT_ID)
        pages = reusable_pages(inventory.pages)
        if not pages:
            print("No reusable pages for", DEFAULT_AGENT_ID)
            return 2
        page_id = pages[0].get("pageId") or pages[0].get("id")
        data = await eval_json_via_title(
            client,
            str(page_id),
            JS,
            purpose="probe detail tab structure",
            chunk_chars=900,
        )
        print(json.dumps(data, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
