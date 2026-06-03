"""
harness.vl - Narrow visual verification helper for screenshot verdicts.
"""

from __future__ import annotations

import asyncio
import base64
import json
import mimetypes
import os
import re
from pathlib import Path
from typing import Any, Optional

from harness.config import VLConfig
from harness.utils import JsonDict


VISUAL_VERIFY_SYSTEM = (
    "You are a visual verification component inside a browser automation harness. "
    "Use the screenshot only to verify page state or action outcome. Do not extract "
    "long tables/lists or invent unseen data. Return JSON only."
)


def build_visual_verify_prompt(
    *,
    expected: JsonDict,
    mode: str,
    question: str,
) -> str:
    if mode == "challenge_detection":
        return (
            "Decide whether this browser screenshot is blocked by an anti-bot,"
            " CAPTCHA, Cloudflare/security verification, login wall, or other"
            " page that requires human action before automation can continue.\n"
            f"question: {question or '(none)'}\n"
            f"expected: {json.dumps(expected or {}, ensure_ascii=False, default=str)}\n\n"
            "Return exactly one JSON object with keys:\n"
            "- verdict: one of confirmed_challenge, normal_loading, unrelated_block, uncertain\n"
            "- confidence: number from 0 to 1\n"
            "- visible_evidence: short array of visible screenshot observations\n"
            "- recommended_recovery: one of hitl, continue, retry_navigation, use_dom\n"
            "- reason: one short sentence\n"
        )
    return (
        "Verify the browser screenshot against this expected state.\n"
        f"mode: {mode or 'action_outcome'}\n"
        f"question: {question or '(none)'}\n"
        f"expected: {json.dumps(expected or {}, ensure_ascii=False, default=str)}\n\n"
        "Return exactly one JSON object with keys:\n"
        "- verdict: one of match, mismatch, blocked, uncertain\n"
        "- confidence: number from 0 to 1\n"
        "- visible_evidence: short array of visible screenshot observations\n"
        "- recommended_recovery: one of continue, retry_click, close_overlay, hitl, use_dom, retry_navigation\n"
        "- reason: one short sentence\n"
    )


async def visual_verify_image(
    *,
    config: VLConfig,
    image_path: str,
    expected: JsonDict,
    mode: str = "action_outcome",
    question: str = "",
) -> JsonDict:
    if not config.enabled:
        return {"status": "disabled", "reason": "vl.enabled is false"}
    if not config.model_id:
        return {"status": "failed", "error": "vl.model_id is required"}

    path = Path(image_path)
    if not path.exists() or not path.is_file():
        return {"status": "failed", "error": "screenshot file is missing", "path": image_path}

    image_b64 = base64.b64encode(path.read_bytes()).decode("ascii")
    mime_type = mimetypes.guess_type(str(path))[0] or "image/png"
    prompt = build_visual_verify_prompt(
        expected=expected,
        mode=mode,
        question=question,
    )

    provider = (config.provider or "openai").strip().lower()
    try:
        if provider == "openai":
            raw_text, usage = await _call_openai_compatible(
                config=config,
                image_b64=image_b64,
                mime_type=mime_type,
                prompt=prompt,
            )
        elif provider == "anthropic":
            raw_text, usage = await _call_anthropic_compatible(
                config=config,
                image_b64=image_b64,
                mime_type=mime_type,
                prompt=prompt,
            )
        else:
            return {
                "status": "failed",
                "error": "vl.provider must be openai or anthropic",
                "provider": config.provider,
            }
    except Exception as exc:
        return {
            "status": "failed",
            "error": str(exc),
            "provider": provider,
            "model": config.model_id,
        }

    parsed = _parse_json_object(raw_text)
    if not isinstance(parsed, dict):
        return {
            "status": "failed",
            "error": "VL response was not valid JSON",
            "raw": raw_text[:2000],
            "usage": usage,
        }

    verdict = str(parsed.get("verdict") or "uncertain").strip().lower()
    allowed_verdicts = (
        {"confirmed_challenge", "normal_loading", "unrelated_block", "uncertain"}
        if mode == "challenge_detection"
        else {"match", "mismatch", "blocked", "uncertain"}
    )
    if verdict not in allowed_verdicts:
        verdict = "uncertain"
    recovery = str(parsed.get("recommended_recovery") or "use_dom").strip().lower()
    allowed_recoveries = {
        "continue",
        "retry_click",
        "close_overlay",
        "hitl",
        "use_dom",
        "retry_navigation",
    }
    if recovery not in allowed_recoveries:
        recovery = "use_dom"
    try:
        confidence = float(parsed.get("confidence", 0.0))
    except (TypeError, ValueError):
        confidence = 0.0
    confidence = max(0.0, min(confidence, 1.0))

    evidence = parsed.get("visible_evidence")
    if not isinstance(evidence, list):
        evidence = []

    return {
        "status": "done",
        "verdict": verdict,
        "confidence": confidence,
        "visible_evidence": [str(item)[:300] for item in evidence[:8]],
        "recommended_recovery": recovery,
        "reason": str(parsed.get("reason") or "")[:500],
        "usage": usage,
    }


async def _call_openai_compatible(
    *,
    config: VLConfig,
    image_b64: str,
    mime_type: str,
    prompt: str,
) -> tuple[str, JsonDict]:
    try:
        from openai import AsyncOpenAI
    except ImportError as exc:
        raise RuntimeError("openai SDK is required for vl.provider=openai") from exc

    api_key = config.api_key or os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError("VL OpenAI-compatible api_key is missing")
    client = AsyncOpenAI(api_key=api_key, base_url=config.base_url)
    params: JsonDict = {
        "model": config.model_id,
        "messages": [
            {"role": "system", "content": VISUAL_VERIFY_SYSTEM},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:{mime_type};base64,{image_b64}",
                        },
                    },
                ],
            },
        ],
        "temperature": 0,
        "max_tokens": 800,
    }
    params.update(config.extra_params or {})
    response = await asyncio.wait_for(
        client.chat.completions.create(**params),
        timeout=config.default_timeout_seconds,
    )
    text = response.choices[0].message.content or ""
    usage = getattr(response, "usage", None)
    return text, {
        "provider": "openai",
        "model": config.model_id,
        "prompt_tokens": int(getattr(usage, "prompt_tokens", 0) or 0) if usage else 0,
        "completion_tokens": int(getattr(usage, "completion_tokens", 0) or 0) if usage else 0,
    }


async def _call_anthropic_compatible(
    *,
    config: VLConfig,
    image_b64: str,
    mime_type: str,
    prompt: str,
) -> tuple[str, JsonDict]:
    try:
        from anthropic import AsyncAnthropic
    except ImportError as exc:
        raise RuntimeError("anthropic SDK is required for vl.provider=anthropic") from exc

    api_key = config.api_key or os.getenv("ANTHROPIC_AUTH_TOKEN")
    if not api_key:
        raise RuntimeError("VL Anthropic-compatible api_key is missing")
    client = AsyncAnthropic(api_key=api_key, base_url=config.base_url)
    response = await asyncio.wait_for(
        client.messages.create(
            model=config.model_id,
            system=VISUAL_VERIFY_SYSTEM,
            max_tokens=int((config.extra_params or {}).get("max_tokens", 800)),
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": mime_type,
                                "data": image_b64,
                            },
                        },
                    ],
                }
            ],
        ),
        timeout=config.default_timeout_seconds,
    )
    text = ""
    for block in response.content:
        if getattr(block, "type", None) == "text":
            text += getattr(block, "text", "") or ""
    usage = getattr(response, "usage", None)
    return text, {
        "provider": "anthropic",
        "model": config.model_id,
        "input_tokens": int(getattr(usage, "input_tokens", 0) or 0) if usage else 0,
        "output_tokens": int(getattr(usage, "output_tokens", 0) or 0) if usage else 0,
    }


def _parse_json_object(text: str) -> Optional[JsonDict]:
    text = (text or "").strip()
    if not text:
        return None
    try:
        parsed = json.loads(text)
        return parsed if isinstance(parsed, dict) else None
    except json.JSONDecodeError:
        pass
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, flags=re.S)
    if fence:
        try:
            parsed = json.loads(fence.group(1))
            return parsed if isinstance(parsed, dict) else None
        except json.JSONDecodeError:
            pass
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        try:
            parsed = json.loads(text[start:end + 1])
            return parsed if isinstance(parsed, dict) else None
        except json.JSONDecodeError:
            return None
    return None
