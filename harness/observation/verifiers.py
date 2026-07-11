"""
harness.observation.verifiers - Read-only semantic oracles for micro-loops.

Design contract (micro-loop oracle phase 1):
- Oracle JS comes only from the fixed templates below; the model never writes
  or sees it. Bindings are JSON-encoded before substitution.
- Oracles run through the harness read_only_eval channel, which skips the
  pessimistic AXTree invalidation bookkeeping. Because of that, the templates
  must stay provably read-only: no assignments, no DOM mutation APIs, and no
  title side-channel. If returnByValue fails, the verifier reports
  oracle_unavailable instead of escalating to the (mutating) side-channel.
- Every locator-style template reports matchCount so ambiguity is surfaced
  instead of silently picking the first hit.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, List, Optional, Set

from harness.utils import JsonDict

CONFIDENCE_HIGH = "high"
CONFIDENCE_MEDIUM = "medium"
CONFIDENCE_LOW = "low"

# An oracle takes a rendered JS expression and returns either the parsed JSON
# payload produced by the template, or {"_oracle_error": "..."} on failure.
OracleEval = Callable[[str], Awaitable[Any]]


@dataclass(frozen=True)
class SemanticLocator:
    """A 'how to find it in JS' description rendered from a fixed template.

    Bindings are substituted into __TOKEN__ placeholders; values must already
    be JSON-encoded (use SemanticLocator.bind to get that for free), so
    arbitrary text can never break out of a JS string/array literal.
    """

    js_template: str
    bindings: Dict[str, str] = field(default_factory=dict)
    description: str = ""

    @classmethod
    def bind(
        cls,
        js_template: str,
        *,
        description: str = "",
        **values: Any,
    ) -> "SemanticLocator":
        encoded = {
            key: json.dumps(value, ensure_ascii=False) for key, value in values.items()
        }
        return cls(js_template=js_template, bindings=encoded, description=description)

    def render(self) -> str:
        rendered = self.js_template
        for key, encoded in self.bindings.items():
            rendered = rendered.replace(f"__{key.upper()}__", encoded)
        return rendered


@dataclass
class VerifierResult:
    ok: bool
    confidence: str
    method: str  # "semantic_oracle" | "oracle_unavailable" | "oracle_no_match"
    evidence: JsonDict = field(default_factory=dict)
    reason: str = ""
    # Bulk data for in-process callers only (e.g. the scroll_collect harvest
    # accumulator's full stable-key list). Deliberately excluded from
    # to_payload() so it never inflates the model-facing tool result.
    internal: JsonDict = field(default_factory=dict)

    def to_payload(self) -> JsonDict:
        return {
            "ok": self.ok,
            "confidence": self.confidence,
            "method": self.method,
            "evidence": self.evidence,
            "reason": self.reason,
        }


def build_read_only_oracle(agent: Any, page_id: str, step: int) -> OracleEval:
    """OracleEval bound to one agent/page.

    Data path: the live ABCP panel returns `data: null` for EVERY
    Runtime.evaluate(returnByValue) — confirmed against ws://localhost:9300
    for `1+2`, `document.title`, objects, everything (see
    reports/dismiss_overlay_probe_*.json and the abcp-side-channel-pattern
    memory). The eval's side effects still apply, so the document.title
    side-channel is the only working way to read a value back. We capture and
    restore the original title around the side-channel so challenge_detector
    and PageFingerprint (both consume title) never see the marker traffic.

    read_only_eval=True keeps the AXTree id bookkeeping intact: writing
    document.title does not change canonical DOM ids, so the cached snapshot
    stays valid even though we executed JS. internal=True keeps every marker
    call out of the progress/challenge/diagnostics/trace chain — the side-channel
    is harness plumbing, only the decoded payload is a real observation.

    Lazy-imports browser_tools to avoid an import cycle.
    """

    async def run(expression: str) -> Any:
        from harness.tools.browser_tools import (  # local import: avoid cycle
            _eval_json_via_title,
            _invoke_browser_method,
            _response_data,
        )

        # Capture the current title so we can restore it afterwards.
        state = await _invoke_browser_method(
            agent,
            "Page.getState",
            {"pageId": page_id, "purpose": "verifier oracle: capture title"},
            step,
            count_progress=False,
            read_only_eval=True,
            internal=True,
        )
        original_title = _response_data(state).get("title")

        # Verifier templates evaluate to an object; the side-channel needs a
        # JSON string, so wrap with JSON.stringify.
        json_expression = f"JSON.stringify(({expression}))"
        try:
            payload = await _eval_json_via_title(
                agent,
                page_id,
                json_expression,
                step,
                "read-only verifier oracle (harness template)",
                read_only_eval=True,
                internal=True,
            )
        finally:
            if isinstance(original_title, str):
                await _invoke_browser_method(
                    agent,
                    "Runtime.evaluate",
                    {
                        "pageId": page_id,
                        "expression": f"document.title = {json.dumps(original_title)}; 0",
                        "returnByValue": True,
                        "purpose": "verifier oracle: restore title",
                    },
                    step,
                    count_progress=False,
                    read_only_eval=True,
                    internal=True,
                )

        if payload is None:
            return {"_oracle_error": "title side-channel returned no payload"}
        # _eval_json_via_title reports failures as {"error": ...}; a nested
        # ABCP runtime error must not pass as a valid template result, or a
        # verifier reading absent fields would default to ok.
        if isinstance(payload, dict) and payload.get("error"):
            return {"_oracle_error": str(payload.get("error"))}
        return payload

    return run


def _oracle_error(payload: Any) -> Optional[str]:
    # Treat both the harness-normalized `_oracle_error` and any raw `error`
    # key as a hard failure, so no verifier can mistake an error payload for
    # a structured result with defaulted fields.
    if isinstance(payload, dict):
        if payload.get("_oracle_error"):
            return str(payload.get("_oracle_error"))
        if payload.get("error"):
            return str(payload.get("error"))
    return None


def _unavailable(reason: str) -> VerifierResult:
    return VerifierResult(
        ok=False,
        confidence=CONFIDENCE_LOW,
        method="oracle_unavailable",
        evidence={},
        reason=reason,
    )


# ---------------------------------------------------------------------------
# Templates (read-only by construction: queries, computed style, rect reads)
# ---------------------------------------------------------------------------

OVERLAY_PROBE_JS = r"""
(() => {
  // A freshly created ABCP tab has no layout viewport (innerWidth===0, all
  // rects 0) until a DOM.getAXTree / Page.screenshot forces layout. Report it
  // so the verifier can refuse to judge instead of false-reporting "gone"
  // (rect-based visibility would collapse to 0 = no dialogs = dismissed).
  const viewportReady = window.innerWidth > 0 && window.innerHeight > 0;
  const isShown = (el) => {
    const s = getComputedStyle(el);
    if (s.display === 'none' || s.visibility === 'hidden') return false;
    if (Number(s.opacity || '1') <= 0.05) return false;
    if (!viewportReady) return true;  // can't trust rects; rely on style only
    const r = el.getBoundingClientRect();
    return r.width >= 80 && r.height >= 60;
  };
  const brief = (el) => {
    const r = el.getBoundingClientRect();
    return {
      tag: el.tagName.toLowerCase(),
      role: el.getAttribute('role') || '',
      cls: String(el.className || '').slice(0, 80),
      text: String(el.innerText || '').replace(/\s+/g, ' ').slice(0, 120),
      rect: { x: Math.round(r.x), y: Math.round(r.y), w: Math.round(r.width), h: Math.round(r.height) },
      position: getComputedStyle(el).position,
    };
  };
  const dialogs = Array.from(
    document.querySelectorAll('[role="dialog"],[role="alertdialog"],dialog[open]')
  ).filter(isShown);
  const vw = window.innerWidth, vh = window.innerHeight;
  const centerStack = viewportReady
    ? document.elementsFromPoint(vw / 2, vh / 2).slice(0, 6).map(brief)
    : [];
  const covers = centerStack.filter((item) =>
    (item.position === 'fixed' || item.position === 'absolute')
    && item.rect.w >= vw * 0.85 && item.rect.h >= vh * 0.85
  );
  return {
    viewportReady,
    dialogCount: dialogs.length,
    dialogs: dialogs.slice(0, 3).map(brief),
    fullCoverCount: covers.length,
    centerStack,
  };
})()
"""

OCCLUDER_PROBE_JS = r"""
(() => {
  const x = __X__;
  const y = __Y__;
  const stack = document.elementsFromPoint(x, y).slice(0, 6).map((el) => {
    const r = el.getBoundingClientRect();
    return {
      tag: el.tagName.toLowerCase(),
      role: el.getAttribute('role') || '',
      cls: String(el.className || '').slice(0, 80),
      text: String(el.innerText || '').replace(/\s+/g, ' ').slice(0, 80),
      // Accessible-name sources beyond visible text, so the VL safety gate sees
      // the TRUE identity of an icon-only control (e.g. aria-label="Close
      // position" on a button that only renders an "×").
      ariaLabel: String(el.getAttribute('aria-label') || '').replace(/\s+/g, ' ').slice(0, 80),
      title: String(el.getAttribute('title') || '').replace(/\s+/g, ' ').slice(0, 80),
      value: String((el.value != null ? el.value : '') || '').replace(/\s+/g, ' ').slice(0, 80),
      alt: String(el.getAttribute('alt') || '').replace(/\s+/g, ' ').slice(0, 80),
      rect: { x: Math.round(r.x), y: Math.round(r.y), w: Math.round(r.width), h: Math.round(r.height) },
      position: getComputedStyle(el).position,
    };
  });
  return { x, y, stack };
})()
"""

ITEMS_COUNT_JS = r"""
(() => {
  const selector = __SELECTOR__;
  const limit = __LIMIT__;
  let nodes;
  try {
    nodes = Array.from(document.querySelectorAll(selector));
  } catch (err) {
    return { error: 'invalid selector: ' + String(err && err.message || err) };
  }
  const keys = [];
  for (const el of nodes.slice(0, limit)) {
    const anchor = el.matches && el.matches('a[href]')
      ? el
      : (el.querySelector ? el.querySelector('a[href]') : null);
    const key = (anchor && anchor.href)
      || String(el.innerText || '').replace(/\s+/g, ' ').trim().slice(0, 120);
    if (key) keys.push(key);
  }
  return { count: nodes.length, sampled: keys.length, keys };
})()
"""

FIELD_VALUE_JS = r"""
(() => {
  const keywords = __KEYWORDS__;
  const matchText = (t) => {
    t = String(t || '').toLowerCase();
    return !!t && keywords.some((k) => t.includes(k));
  };
  const seen = new Set();
  const matches = [];
  const push = (el, strategy) => {
    if (!el || seen.has(el)) return;
    seen.add(el);
    matches.push({
      strategy,
      value: ('value' in el) ? String(el.value) : String(el.textContent || ''),
      tag: el.tagName.toLowerCase(),
      type: el.getAttribute('type') || '',
      name: el.getAttribute('name') || '',
    });
  };
  for (const label of document.querySelectorAll('label')) {
    if (!matchText(label.textContent)) continue;
    const target = label.htmlFor
      ? document.getElementById(label.htmlFor)
      : label.querySelector('input,textarea,select');
    push(target, 'label');
  }
  for (const el of document.querySelectorAll('input,textarea,select')) {
    if (
      matchText(el.getAttribute('aria-label'))
      || matchText(el.getAttribute('placeholder'))
      || matchText(el.getAttribute('name'))
    ) {
      push(el, 'attr');
    }
  }
  return { matchCount: matches.length, matches: matches.slice(0, 5) };
})()
"""


# ---------------------------------------------------------------------------
# Verifiers
# ---------------------------------------------------------------------------

async def verify_overlay_gone(*, oracle: OracleEval) -> VerifierResult:
    """True when no visible dialog and no full-viewport fixed cover remains."""
    payload = await oracle(SemanticLocator(OVERLAY_PROBE_JS).render())
    error = _oracle_error(payload)
    if error:
        return _unavailable(error)
    if not isinstance(payload, dict):
        return _unavailable("overlay probe returned non-object payload")

    if payload.get("viewportReady") is False:
        # No layout viewport yet (fresh tab pre-getAXTree). Rect-based visibility
        # is meaningless here; refuse to judge rather than false-report "gone".
        return _unavailable(
            "page has no layout viewport yet; call DOM.getAXTree or"
            " Page.screenshot to force layout before verifying overlay state"
        )

    dialog_count = int(payload.get("dialogCount") or 0)
    cover_count = int(payload.get("fullCoverCount") or 0)
    gone = dialog_count == 0 and cover_count == 0
    return VerifierResult(
        ok=gone,
        confidence=CONFIDENCE_HIGH if gone else CONFIDENCE_MEDIUM,
        method="semantic_oracle",
        evidence={
            "dialogCount": dialog_count,
            "fullCoverCount": cover_count,
            "dialogs": payload.get("dialogs") or [],
        },
        reason="" if gone else "visible dialog or full-viewport cover still present",
    )


async def probe_occluder(*, oracle: OracleEval, x: float, y: float) -> JsonDict:
    """Identify what sits on top at (x, y). Element-level occlusion truth for
    same-document overlays; returns the elementsFromPoint stack digest."""
    locator = SemanticLocator.bind(OCCLUDER_PROBE_JS, x=round(float(x), 1), y=round(float(y), 1))
    payload = await oracle(locator.render())
    error = _oracle_error(payload)
    if error:
        return {"status": "oracle_unavailable", "reason": error}
    if not isinstance(payload, dict):
        return {"status": "oracle_unavailable", "reason": "non-object payload"}
    return {"status": "done", **payload}


VIEWPORT_METRICS_JS = r"""
(() => {
  return {
    width: window.innerWidth,
    height: window.innerHeight,
    dpr: window.devicePixelRatio || 1,
  };
})()
"""


async def probe_viewport_metrics(*, oracle: OracleEval) -> JsonDict:
    """CSS layout viewport (innerWidth/innerHeight) + devicePixelRatio. The VL
    arbiter maps NORMALIZED 0-1000 grounding coordinates to CSS by multiplying
    the fraction by innerWidth/innerHeight, so it needs the live CSS viewport
    (dpr is returned for logging only). A fresh tab with no layout reports
    width/height 0 -> caller treats it as unavailable."""
    payload = await oracle(SemanticLocator(VIEWPORT_METRICS_JS).render())
    error = _oracle_error(payload)
    if error:
        return {"status": "oracle_unavailable", "reason": error}
    if not isinstance(payload, dict):
        return {"status": "oracle_unavailable", "reason": "non-object payload"}
    width = float(payload.get("width") or 0)
    height = float(payload.get("height") or 0)
    if width <= 0 or height <= 0:
        return {"status": "oracle_unavailable", "reason": "viewport not laid out"}
    return {
        "status": "done",
        "width": width,
        "height": height,
        "dpr": float(payload.get("dpr") or 1) or 1.0,
    }


async def verify_items_grew(
    *,
    oracle: OracleEval,
    selector: str,
    before_count: int,
    known_keys: Optional[Set[str]] = None,
    limit: int = 300,
) -> VerifierResult:
    """One RPC returns count + stable keys; harvest callers reuse the keys."""
    locator = SemanticLocator.bind(
        ITEMS_COUNT_JS,
        selector=selector,
        limit=int(limit),
    )
    payload = await oracle(locator.render())
    error = _oracle_error(payload)
    if error:
        return _unavailable(error)
    if not isinstance(payload, dict):
        return _unavailable("items probe returned non-object payload")
    if payload.get("error"):
        return _unavailable(str(payload.get("error")))

    count = int(payload.get("count") or 0)
    keys = [str(key) for key in (payload.get("keys") or [])]
    new_keys = [key for key in keys if key not in (known_keys or set())]
    # Virtualized lists swap rows without growing the count, so new stable
    # keys are growth evidence even when the raw count is flat.
    grew = count > int(before_count) or bool(known_keys is not None and new_keys)
    return VerifierResult(
        ok=grew,
        confidence=CONFIDENCE_HIGH,
        method="semantic_oracle",
        evidence={
            "selector": selector,
            "beforeCount": int(before_count),
            "count": count,
            "newKeyCount": len(new_keys),
            "newKeys": new_keys[:20],
        },
        reason="" if grew else "no count growth and no new stable keys",
        # Full stable-key lists stay here for the harvest accumulator; never
        # forwarded to the model (see VerifierResult.internal).
        internal={"keys": keys, "newKeys": new_keys},
    )


COLLECT_ROWS_JS = r"""
(() => {
  const selector = __SELECTOR__;
  const fields = __FIELDS__;       // { name: spec } e.g. {title:"text", href:"href"}
  const keyField = __KEYFIELD__;   // a field name, or "" to auto-derive
  const offset = __OFFSET__;       // window start, for paging large DOM lists
  const limit = __LIMIT__;
  let nodes;
  try {
    nodes = Array.from(document.querySelectorAll(selector));
  } catch (err) {
    return { error: 'invalid selector: ' + String(err && err.message || err) };
  }
  const norm = (v, max = 600) => String(v == null ? '' : v).replace(/\s+/g, ' ').trim().slice(0, max);
  // Site-agnostic lazy-image resolver (kept in sync with extract_dom_records):
  // many sites keep the real URL in data-src/srcset until the <img> scrolls
  // into view and only put a 1x1/blank placeholder on .src.
  const isPh = (u) => !u
    || /^data:image\/(gif|svg)/i.test(u)
    || /(blank|placeholder|spacer|loading|transparent|grey|gray|1x1|s\.gif)\.(gif|png|svg|webp)/i.test(u);
  const absll = (u) => { try { if (u && !/^(https?:|data:|\/\/)/i.test(u)) u = new URL(u, location.href).href; } catch (e) {} return /^\/\//.test(u) ? location.protocol + u : u; };
  const imgSrc = (img) => {
    if (!img) return '';
    let u = img.currentSrc || img.getAttribute('src') || '';
    if (isPh(u)) u = img.getAttribute('data-src') || img.getAttribute('data-lazy-src')
      || img.getAttribute('data-original') || img.getAttribute('data-ks-lazyload')
      || img.getAttribute('data-url') || img.getAttribute('data-image') || u;
    if (isPh(u)) { const ss = img.getAttribute('srcset') || img.getAttribute('data-srcset') || ''; if (ss) { const first = ss.split(',')[0].trim().split(/\s+/)[0]; if (first) u = first; } }
    return absll(u) || '';
  };
  const read = (el, spec) => {
    spec = String(spec || 'text');
    if (spec === 'text' || spec === 'textContent') return norm(el.innerText || el.textContent || '');
    if (spec === 'href') return el.href || (el.querySelector && el.querySelector('a[href]') ? el.querySelector('a[href]').href : '') || (el.closest && el.closest('a[href]') ? el.closest('a[href]').href : '');
    if (spec === 'src') {
      const img = (el.matches && el.matches('img')) ? el : (el.querySelector && el.querySelector('img'));
      if (img) return imgSrc(img);
      return absll(el.currentSrc || (el.getAttribute && el.getAttribute('src')) || '') || '';
    }
    if (spec === 'imgAlt') { const img = (el.matches && el.matches('img')) ? el : (el.querySelector && el.querySelector('img')); return img ? norm(img.getAttribute('alt') || img.alt || '') : ''; }
    if (spec.startsWith('attr:')) return norm(el.getAttribute(spec.slice(5)) || '');
    return norm(el.innerText || el.textContent || '');
  };
  const deriveKey = (el, row) => {
    if (keyField && row[keyField]) return String(row[keyField]);
    const a = (el.matches && el.matches('a[href]')) ? el : (el.querySelector ? el.querySelector('a[href]') : null);
    if (a && a.href) return a.href;
    return norm(el.innerText || el.textContent || '', 160);
  };
  const rows = [];
  for (const el of nodes.slice(offset, offset + limit)) {
    const row = {};
    for (const k in fields) row[k] = read(el, fields[k]);
    row.__key = deriveKey(el, row);
    rows.push(row);
  }
  return { count: nodes.length, offset, returned: rows.length, rows };
})()
"""


async def collect_rows(
    *,
    oracle: OracleEval,
    selector: str,
    fields: Dict[str, str],
    key_field: str = "",
    offset: int = 0,
    limit: int = 200,
) -> JsonDict:
    """Harvest one snapshot of a repeated DOM collection through the read-only
    oracle: returns {status, count, rows} where each row carries the requested
    fields plus a __key for stable-key dedup. Field reads (innerText/href/attr)
    are layout-independent, so this works before the page has a layout viewport;
    only the count reflects what is currently attached to the DOM. The harvest
    accumulator (collect_items) dedups across rounds by __key, which is how
    virtualized lists that recycle rows still get fully collected."""
    locator = SemanticLocator.bind(
        COLLECT_ROWS_JS,
        selector=selector,
        fields=fields,
        keyfield=key_field or "",
        offset=int(offset),
        limit=int(limit),
    )
    payload = await oracle(locator.render())
    error = _oracle_error(payload)
    if error:
        return {"status": "oracle_unavailable", "reason": error}
    if not isinstance(payload, dict):
        return {"status": "oracle_unavailable", "reason": "non-object payload"}
    if payload.get("error"):
        return {"status": "failed", "reason": str(payload.get("error"))}
    rows = [r for r in (payload.get("rows") or []) if isinstance(r, dict)]
    return {
        "status": "done",
        "count": int(payload.get("count") or 0),
        "offset": int(payload.get("offset") or offset),
        "returned": int(payload.get("returned") or len(rows)),
        "rows": rows,
    }


def _normalize_value(value: Any) -> str:
    return " ".join(str(value or "").split())


def _mask(value: str) -> str:
    return f"<masked len={len(value)}>"


async def verify_field_value(
    *,
    oracle: OracleEval,
    keywords: List[str],
    expected_value: str,
    mask_value: bool = False,
) -> VerifierResult:
    """Locate a field by label/aria/placeholder/name keywords and read back
    its live .value property. Ambiguity (matchCount > 1) lowers confidence
    instead of silently trusting the first hit; zero matches reports
    oracle_no_match so callers can fall back to an AXTree refresh."""
    normalized_keywords = [str(k).strip().lower() for k in keywords if str(k).strip()]
    if not normalized_keywords:
        return _unavailable("no keywords provided for field locator")
    locator = SemanticLocator.bind(FIELD_VALUE_JS, keywords=normalized_keywords)
    payload = await oracle(locator.render())
    error = _oracle_error(payload)
    if error:
        return _unavailable(error)
    if not isinstance(payload, dict):
        return _unavailable("field probe returned non-object payload")

    match_count = int(payload.get("matchCount") or 0)
    matches = payload.get("matches") or []
    expected = _normalize_value(expected_value)

    def render_value(value: str) -> str:
        return _mask(value) if mask_value else value

    if match_count == 0:
        return VerifierResult(
            ok=False,
            confidence=CONFIDENCE_LOW,
            method="oracle_no_match",
            evidence={"keywords": normalized_keywords, "matchCount": 0},
            reason="no field matched the keywords; fall back to AXTree refresh",
        )

    hits = [
        m for m in matches
        if isinstance(m, dict) and _normalize_value(m.get("value")) == expected
    ]
    evidence: JsonDict = {
        "keywords": normalized_keywords,
        "matchCount": match_count,
        "expected": render_value(expected),
        "matches": [
            {
                **{k: v for k, v in m.items() if k != "value"},
                "value": render_value(_normalize_value(m.get("value"))),
            }
            for m in matches
            if isinstance(m, dict)
        ],
    }
    if match_count == 1:
        ok = bool(hits)
        return VerifierResult(
            ok=ok,
            confidence=CONFIDENCE_HIGH,
            method="semantic_oracle",
            evidence=evidence,
            reason="" if ok else "field value does not match expected",
        )
    return VerifierResult(
        ok=bool(hits),
        confidence=CONFIDENCE_LOW,
        method="semantic_oracle",
        evidence=evidence,
        reason=(
            f"{match_count} fields matched the keywords; ambiguous target"
            + ("" if hits else " and none holds the expected value")
        ),
    )
