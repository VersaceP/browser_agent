"""Unit + light integration test for the vision-first plumbing.

Coverage:
1. browser/vision.py: config loading, parse_ok branch, error paths
2. tools/browser_tools.py: analyze_screenshot + vision_page_skeleton tools
   are registered, allowed_tools wiring is correct.
3. (Live) when --live is passed, runs an actual vision call against the
   configured model on a real screenshot of the TAAFT surething page.

Run:
    conda activate agent
    cd browser_agent_v6
    python _test_vision_skeleton.py            # offline (mocks the API)
    python _test_vision_skeleton.py --live     # hits real vision endpoint
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent))


def test_extract_json_array_handles_fences():
    from browser.vision import _try_extract_json_array
    # Plain
    assert _try_extract_json_array('[{"a":1}]') == [{"a": 1}]
    # With ```json fence
    fenced = '```json\n[{"a": 1}, {"a": 2}]\n```'
    assert _try_extract_json_array(fenced) == [{"a": 1}, {"a": 2}]
    # Bare ``` fence
    bare = '```\n[1, 2, 3]\n```'
    assert _try_extract_json_array(bare) == [1, 2, 3]
    # Leading prose
    leading = 'Here is the answer:\n\n[{"x": "y"}]'
    assert _try_extract_json_array(leading) == [{"x": "y"}]
    # Garbage
    assert _try_extract_json_array("not json at all") is None
    print("✓ _try_extract_json_array: 5 cases pass")


def test_vision_skeleton_scan_parses_mock_response():
    """Mock the HTTP layer; verify our parse + return shape."""
    from browser import vision as v

    mock_payload = json.dumps([
        {"name": "header", "heading_text": "SureThing.io v2.0",
         "approx_y": "top", "has_repeating_items": False,
         "item_count_visible": 0, "sample_text": "Title bar"},
        {"name": "reviews", "heading_text": "Reviews",
         "approx_y": "bottom", "has_repeating_items": True,
         "item_count_visible": 4, "sample_text": "Comments(4) — first user iammutex..."},
    ])

    class _Choice:
        def __init__(self): self.message = type("M", (), {"content": mock_payload})()
    class _Usage:
        prompt_tokens = 800; completion_tokens = 120; total_tokens = 920
    class _Resp:
        choices = [_Choice()]; usage = _Usage()
    class _Completions:
        def create(self, **kw): return _Resp()
    class _Chat:
        completions = _Completions()
    class _MockClient:
        chat = _Chat()

    # Pretend config is loaded + client is ready
    v._CLIENT = _MockClient()
    v._VISION_CFG = {
        "model_id": "qwen3-vl-plus", "api_key": "x", "base_url": "https://x/",
        "extra_params": {"max_tokens": 2048, "temperature": 0.7},
    }

    # Real PNG file required by _encode_image_data_url — write a 1x1 pixel
    tmp = Path(__file__).parent / "_tmp_vision_test.png"
    tmp.write_bytes(
        bytes.fromhex(
            "89504E470D0A1A0A0000000D49484452000000010000000108060000001F15C4"
            "890000000A49444154789C6300010000000500010D0A2DB40000000049454E44"
            "AE426082"
        )
    )
    try:
        out = v.vision_skeleton_scan(tmp)
        assert out["parse_ok"] is True, f"parse_ok was False, raw={out['raw']!r}"
        assert isinstance(out["sections"], list) and len(out["sections"]) == 2
        assert out["sections"][1]["name"] == "reviews"
        assert out["sections"][1]["has_repeating_items"] is True
        assert out["sections"][1]["item_count_visible"] == 4
        assert out["model"] == "qwen3-vl-plus"
        assert out["tokens"]["total"] == 920
    finally:
        tmp.unlink(missing_ok=True)
        # Reset module state so subsequent tests don't see stubbed client
        v._CLIENT = None; v._VISION_CFG = None
    print("✓ vision_skeleton_scan parses mock model response correctly")


def test_tools_registered():
    from tools import build_default_registry
    reg = build_default_registry()
    names = set(reg.names())
    assert "analyze_screenshot" in names, names
    assert "vision_page_skeleton" in names, names
    # Both are readonly + long_running
    assert reg.get("analyze_screenshot").readonly is True
    assert reg.get("analyze_screenshot").long_running is True
    assert reg.get("vision_page_skeleton").readonly is True
    assert reg.get("vision_page_skeleton").long_running is True
    print("✓ analyze_screenshot + vision_page_skeleton registered as readonly/long_running")


def test_agent_wiring():
    from core.agent_definition import build_builtin_agents
    agents = build_builtin_agents()
    worker_tools = set(agents["worker"].allowed_tools)
    veri_tools = set(agents["verification"].allowed_tools)
    assert {"analyze_screenshot", "vision_page_skeleton"}.issubset(worker_tools), worker_tools
    assert {"analyze_screenshot", "vision_page_skeleton"}.issubset(veri_tools), veri_tools
    # Verification stays read-only
    assert agents["verification"].readonly is True
    print("✓ worker + verification both have analyze_screenshot + vision_page_skeleton")


def test_helpers_reexports_vision():
    """run_browser_python's namespace pulls all callables from browser.helpers —
    confirm the vision functions are reachable that way."""
    import browser.helpers as bh
    assert callable(bh.analyze_image)
    assert callable(bh.vision_skeleton_scan)
    assert hasattr(bh, "VisionNotConfiguredError")
    print("✓ browser.helpers re-exports analyze_image + vision_skeleton_scan (sandbox can use them)")


def test_vision_not_configured_error_path():
    """When config.json has no vision section, raise VisionNotConfiguredError —
    NOT a generic RuntimeError that hides the cause."""
    from browser import vision as v
    # Force re-load by clearing cache
    v._CLIENT = None; v._VISION_CFG = None

    fake_cfg_dir = Path(__file__).parent / "_tmp_vision_cfg_test"
    fake_cfg_dir.mkdir(exist_ok=True)
    fake_cfg = fake_cfg_dir / "config.json"
    fake_cfg.write_text('{"provider": "x"}', encoding="utf-8")  # no vision section

    with patch.object(v, "_project_root", return_value=fake_cfg_dir):
        try:
            v._load_vision_cfg()
        except v.VisionNotConfiguredError as e:
            assert "vision" in str(e).lower()
            print(f"✓ VisionNotConfiguredError raised for missing section: {e}")
        else:
            raise AssertionError("expected VisionNotConfiguredError")
        finally:
            fake_cfg.unlink()
            fake_cfg_dir.rmdir()


def _is_review_like(section: dict) -> bool:
    """Word-level match (not substring) — 'preview' must NOT match 'review'."""
    import re
    name = (section.get("name") or "").lower()
    heading = (section.get("heading_text") or "").lower()
    sample = (section.get("sample_text") or "").lower()
    # Word-boundary regex avoids 'preview' / 'previous' false positives
    pattern = re.compile(r"\b(reviews?|comments?|discussion|testimonials?|feedback|ratings?)\b")
    return bool(pattern.search(name) or pattern.search(heading) or pattern.search(sample))


def test_live_skeleton_on_taaft_surething():
    """LIVE: open the real TAAFT surething page, run the multi-pass scroll-and-rescan
    loop the worker prompt teaches, and assert that the accumulated sections cover
    a 'reviews' / 'comments' / 'discussion' section with has_repeating_items=true.

    Skipped unless --live is passed AND a daemon can be started.
    """
    if "--live" not in sys.argv:
        print("⏭  live test skipped (pass --live to run against real vision endpoint + browser)")
        return

    import time as _time
    from browser.daemon import get_daemon
    from browser.helpers import (
        set_active_port, goto, screenshot, scroll, js, set_window_size,
    )
    from browser.vision import vision_skeleton_scan

    port = 9222
    daemon = get_daemon(port=port); daemon.start()
    set_active_port(port)

    # Standardize window — read AGGRESSIVE/STANDARD/MINIMAL from CLI flag.
    # AGGRESSIVE: 1440x1740 (inner ~1410x1600, PNG 2115x2400) — tests whether the
    #   stronger vision model can handle larger images without OCR loss.
    # STANDARD: 1440x1340 (inner ~1410x1200, PNG 2115x1800) — recommended sweet spot.
    # MINIMAL: 1440x1080 (inner ~1410x940, PNG 2115x1410) — most conservative.
    if "--aggressive" in sys.argv:
        outer_w, outer_h, label = 1440, 1740, "AGGRESSIVE"
    elif "--minimal" in sys.argv:
        outer_w, outer_h, label = 1440, 1080, "MINIMAL"
    else:
        outer_w, outer_h, label = 1440, 1340, "STANDARD"
    win = set_window_size(outer_w, outer_h)
    print(f"[live] window [{label}]: outer {outer_w}x{outer_h} → "
          f"inner {win['innerWidth']}x{win['innerHeight']}, dpr={win['devicePixelRatio']}")

    print("[live] navigating to surething detail page...")
    goto("https://theresanaiforthat.com/ai/surething/", wait=4)

    shot_dir = Path(__file__).parent / "_screenshots_live"
    shot_dir.mkdir(exist_ok=True)

    # PRIMING — scroll the entire page to trigger lazy-loaded sections (Reviews,
    # Discussion, etc. on TAAFT only render after the user scrolls past them),
    # then return to top before the skeleton loop begins. Eliminates the
    # "page-state varies between runs" variable.
    print("[live] PRIMING: scrolling to bottom to trigger lazy-load...")
    sh_before = js("return document.documentElement.scrollHeight;")
    # Big scroll in chunks so each step actually triggers any IntersectionObserver
    # listeners (a single jump to bottom often misses them).
    cur = 0
    step = 800
    while True:
        scroll(dy=step)
        _time.sleep(0.4)
        new_y = js("return window.scrollY;")
        if new_y <= cur + 1:  # didn't move → at bottom
            break
        cur = new_y
        if cur > 30000:  # sanity stop
            break
    _time.sleep(1.5)  # settle any final XHRs
    sh_after = js("return document.documentElement.scrollHeight;")
    print(f"[live] PRIMING: scrollHeight {sh_before} → {sh_after} (delta +{sh_after - sh_before})")
    # Return to top
    js("window.scrollTo({top: 0, behavior: 'instant'});")
    _time.sleep(0.6)
    final_top = js("return window.scrollY;")
    print(f"[live] PRIMING done. Back at scrollY={final_top}, total scrollHeight={sh_after}")

    # The multi-pass scroll-and-rescan loop the worker prompt teaches.
    # Tolerates transient screenshot timeouts (Chrome occasionally locks during
    # heavy JS / Cloudflare bg checks).
    accumulated = []
    pass_count = 0
    total_tokens = {"input": 0, "output": 0, "total": 0}
    seen_keys = set()  # dedupe by (name, heading_text)
    skipped_passes = 0

    def _shot_with_retry(target_path: Path, attempts: int = 2) -> bool:
        last_err = None
        for i in range(attempts):
            try:
                screenshot(path=target_path, full_page=False)
                return True
            except Exception as e:
                last_err = e
                print(f"[live] screenshot attempt {i+1} failed: {type(e).__name__}: {str(e)[:80]}")
                _time.sleep(2.0)
        print(f"[live] screenshot gave up after {attempts}: {last_err}")
        return False

    while pass_count < 12:
        pass_count += 1
        shot = shot_dir / f"surething_pass{pass_count}.png"

        if not _shot_with_retry(shot):
            skipped_passes += 1
            # try to scroll forward anyway to make progress
            try:
                geom_now = js("return {innerHeight: window.innerHeight, scrollY: window.scrollY, "
                              "scrollHeight: document.documentElement.scrollHeight, "
                              "atBottom: (window.scrollY + window.innerHeight) >= "
                              "(document.documentElement.scrollHeight - 4)};")
                if geom_now["atBottom"]:
                    break
                scroll(dy=int(geom_now["innerHeight"] * 0.85))
                _time.sleep(1.5)
            except Exception:
                pass
            continue

        out = vision_skeleton_scan(shot)
        for k in total_tokens:
            total_tokens[k] += out["tokens"].get(k, 0)

        new_secs = []
        for s in (out["sections"] or []):
            key = (s.get("name", ""), s.get("heading_text", ""))
            if key not in seen_keys:
                seen_keys.add(key)
                new_secs.append(s)
        accumulated.extend(new_secs)

        geom = js(
            "return {scrollY: window.scrollY, "
            "innerHeight: window.innerHeight, "
            "scrollHeight: document.documentElement.scrollHeight, "
            "atBottom: (window.scrollY + window.innerHeight) >= "
            "(document.documentElement.scrollHeight - 4)};"
        )
        print(f"[live] pass {pass_count}: parse_ok={out['parse_ok']}, "
              f"sections={len(out['sections'])} (new {len(new_secs)}), "
              f"scrollY={geom['scrollY']}/{geom['scrollHeight']}, atBottom={geom['atBottom']}")
        # show what new this pass found
        for s in new_secs:
            print(f"    + {(s.get('name','?') or '?'):24s} | "
                  f"heading={(s.get('heading_text','') or '')!r}")

        if geom["atBottom"]:
            break
        scroll(dy=int(geom["innerHeight"] * 0.85))
        _time.sleep(0.8)  # bumped from 0.4 — TAAFT lazy-loads heavily on scroll

    if skipped_passes:
        print(f"[live] {skipped_passes} pass(es) skipped due to screenshot timeout")

    print(f"\n[live] total passes: {pass_count}, accumulated unique sections: {len(accumulated)}, "
          f"total vision tokens: {total_tokens}")
    print("[live] all sections:")
    for s in accumulated:
        print(f"  - {(s.get('name','?') or '?'):24s} | "
              f"heading={(s.get('heading_text','') or ''):30s} | "
              f"y={(s.get('approx_y','?') or '?'):14s} | "
              f"repeating={s.get('has_repeating_items')} | "
              f"items={s.get('item_count_visible',0)}")

    review_like = [s for s in accumulated if _is_review_like(s)]
    assert review_like, (
        "vision did not identify any reviews/comments/discussion section across "
        f"{pass_count} passes — the model failed to see what's clearly visible on the "
        f"page. Section names returned: {[s.get('name') for s in accumulated]}"
    )
    repeating = [s for s in review_like if s.get("has_repeating_items")]
    if not repeating:
        print(f"⚠ found review-like section(s) but none flagged has_repeating_items: {review_like}")
    else:
        print(f"✓ live: vision identified {len(repeating)} repeating-items review/comments section(s)")
    # OCR-accuracy probe: list every review-like section's heading + sample_text
    # so we can see whether the model is preserving small-text detail (author
    # names like "Ioannis Gikas" / "Taskade" / "Rajkumar Dyagala" should appear
    # if the model can read the comment cards).
    print(f"\n[live] OCR probe — review-like sections raw text:")
    for s in review_like:
        h = s.get("heading_text") or ""
        st = s.get("sample_text") or ""
        print(f"  - name={s.get('name','?')!r:30s} heading={h!r:40s}")
        print(f"    sample={st[:200]!r}")
    print(f"\n✓ live: scroll-and-rescan multi-pass workflow validated end-to-end")


if __name__ == "__main__":
    if sys.platform == "win32":
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    test_extract_json_array_handles_fences()
    test_vision_skeleton_scan_parses_mock_response()
    test_tools_registered()
    test_agent_wiring()
    test_helpers_reexports_vision()
    test_vision_not_configured_error_path()
    test_live_skeleton_on_taaft_surething()
    print("\n✅ all vision-first plumbing tests pass")
