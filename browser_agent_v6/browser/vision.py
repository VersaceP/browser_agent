"""Vision adapter — bridges saved screenshots to a multimodal model.

Design:
- Reads `config.json -> vision` once (lazy), creates a sync OpenAI-compatible client.
- Public surface is a single function `analyze_image(path, query) -> str`.
- The two LLM-facing tools (`analyze_screenshot`, `vision_page_skeleton`) live in
  tools/browser_tools.py and call into this module.

Why sync (not AsyncOpenAI):
- Tool handlers in tools/base.py run sync handlers via asyncio.to_thread, so a
  blocking sync call doesn't block the event loop and avoids wrapping every
  call in async/await for no benefit.
"""
from __future__ import annotations

import base64
import json
import mimetypes
import os
import threading
from pathlib import Path
from typing import Any, Dict, Optional, Tuple


# ──────────────────────────────────
# Lazy client + config
# ──────────────────────────────────

_CLIENT_LOCK = threading.Lock()
_CLIENT: Any = None
_VISION_CFG: Optional[Dict[str, Any]] = None


class VisionNotConfiguredError(RuntimeError):
    """config.json is missing a `vision` section, or required keys (model_id / api_key)."""


def _project_root() -> Path:
    # browser/vision.py → parent.parent = browser_agent_v6/
    return Path(__file__).resolve().parents[1]


def _load_vision_cfg() -> Dict[str, Any]:
    cfg_path = _project_root() / "config.json"
    if not cfg_path.exists():
        raise VisionNotConfiguredError(f"config.json not found at {cfg_path}")
    with open(cfg_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    vision = data.get("vision")
    if not isinstance(vision, dict):
        raise VisionNotConfiguredError("config.json has no `vision` section")
    if not vision.get("model_id"):
        raise VisionNotConfiguredError("config.json -> vision.model_id missing")
    if not vision.get("api_key"):
        raise VisionNotConfiguredError("config.json -> vision.api_key missing")
    return vision


def _get_client() -> Tuple[Any, Dict[str, Any]]:
    global _CLIENT, _VISION_CFG
    with _CLIENT_LOCK:
        if _CLIENT is None:
            cfg = _load_vision_cfg()
            from openai import OpenAI
            kwargs = {"api_key": cfg["api_key"]}
            if cfg.get("base_url"):
                kwargs["base_url"] = cfg["base_url"]
            _CLIENT = OpenAI(**kwargs)
            _VISION_CFG = cfg
    return _CLIENT, _VISION_CFG  # type: ignore[return-value]


# ──────────────────────────────────
# Encode + call
# ──────────────────────────────────

def _encode_image_data_url(path: str | os.PathLike) -> str:
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"image not found: {p}")
    mime, _ = mimetypes.guess_type(p.name)
    if mime is None:
        mime = "image/png"
    raw = p.read_bytes()
    b64 = base64.b64encode(raw).decode("ascii")
    return f"data:{mime};base64,{b64}"


def analyze_image(
    image_path: str | os.PathLike,
    query: str,
    *,
    system: Optional[str] = None,
    max_tokens: Optional[int] = None,
    temperature: Optional[float] = None,
) -> Dict[str, Any]:
    """Send a saved screenshot + a textual query to the configured vision model.

    Returns:
        {
          "answer": "<text from the model>",
          "model": "<model_id>",
          "tokens": {"input": N, "output": M, "total": N+M},
        }

    Raises:
        VisionNotConfiguredError: config.json -> vision is missing or incomplete.
        FileNotFoundError: image_path doesn't exist.
        RuntimeError: API call failed (wraps the underlying error message).
    """
    client, cfg = _get_client()

    extra = cfg.get("extra_params") or {}
    if max_tokens is None:
        max_tokens = int(extra.get("max_tokens", 2048))
    if temperature is None:
        temperature = float(extra.get("temperature", 0.7))

    data_url = _encode_image_data_url(image_path)

    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({
        "role": "user",
        "content": [
            {"type": "image_url", "image_url": {"url": data_url}},
            {"type": "text", "text": query},
        ],
    })

    try:
        resp = client.chat.completions.create(
            model=cfg["model_id"],
            messages=messages,
            max_tokens=max_tokens,
            temperature=temperature,
        )
    except Exception as e:
        raise RuntimeError(f"vision API call failed: {type(e).__name__}: {e}") from e

    answer = (resp.choices[0].message.content or "").strip()
    usage = getattr(resp, "usage", None)
    tokens = {
        "input": getattr(usage, "prompt_tokens", 0) or 0,
        "output": getattr(usage, "completion_tokens", 0) or 0,
        "total": getattr(usage, "total_tokens", 0) or 0,
    }
    return {"answer": answer, "model": cfg["model_id"], "tokens": tokens}


# ──────────────────────────────────
# Skeleton-scan prompt + parser
# ──────────────────────────────────

_SKELETON_SYSTEM = (
    "You are a visual page analyst. Given a webpage screenshot, you identify "
    "every distinct visible content section on the page in top-to-bottom order. "
    "Output strict JSON only — no prose, no markdown fences."
)

_SKELETON_USER = """Analyze this webpage screenshot and list every visible content section
from top to bottom. A "section" is any visually distinct block separated from
its neighbors by a heading, divider, or whitespace gap (e.g. a header bar,
a stats row, an "Overview" block, a "Pricing" panel, a "Reviews" / "Comments"
list, a footer).

Output a JSON array. Each entry MUST have these exact keys:
- "name": short snake_case identifier for the section (e.g. "header", "overview", "pricing", "pros_cons", "reviews", "footer")
- "heading_text": the visible heading or label of the section (string; "" if no explicit heading)
- "approx_y": one of "top" | "upper_middle" | "middle" | "lower_middle" | "bottom"
- "has_repeating_items": true if the section contains a visible list of similar
  repeating sub-items (e.g. multiple comment cards, multiple product rows,
  multiple FAQ entries); false otherwise.
- "item_count_visible": integer — if has_repeating_items is true, how many items
  are visible in the screenshot; otherwise 0.
- "sample_text": ≤120-char excerpt of representative text from this section
  (what a reader would actually see).

Output ONLY the JSON array, nothing else."""


def vision_skeleton_scan(image_path: str | os.PathLike) -> Dict[str, Any]:
    """High-level helper: ask vision to enumerate the page's visible sections.

    Returns:
        {
          "sections": [...]       # parsed JSON array (empty list on parse failure)
          "raw": "<full text>",   # raw model output (for debugging)
          "model": "...",
          "tokens": {...},
          "parse_ok": True/False,
        }
    """
    out = analyze_image(
        image_path,
        query=_SKELETON_USER,
        system=_SKELETON_SYSTEM,
        # skeleton response is structured but bounded — leave temperature lower
        temperature=0.2,
    )
    raw = out["answer"]

    # Try to parse JSON. Be tolerant of a leading ```json fence even though we asked for none.
    parsed = _try_extract_json_array(raw)
    return {
        "sections": parsed if isinstance(parsed, list) else [],
        "raw": raw,
        "model": out["model"],
        "tokens": out["tokens"],
        "parse_ok": isinstance(parsed, list),
    }


def _try_extract_json_array(text: str) -> Any:
    """Extract a JSON array from text. Tolerates markdown fences and leading prose."""
    s = text.strip()
    # Strip ```json ... ``` or ``` ... ``` fences
    if s.startswith("```"):
        # find first newline, last ```
        first_nl = s.find("\n")
        if first_nl != -1:
            s = s[first_nl + 1:]
        if s.rstrip().endswith("```"):
            s = s.rstrip()[:-3].rstrip()
    # Find first '[' and last matching ']'
    lb = s.find("[")
    rb = s.rfind("]")
    if lb == -1 or rb == -1 or rb <= lb:
        try:
            return json.loads(s)  # last resort
        except Exception:
            return None
    chunk = s[lb : rb + 1]
    try:
        return json.loads(chunk)
    except Exception:
        return None
