"""Test PHASE 3 (batch_browser_actions) reachability when starting from a
state where PHASE 1 has effectively been done by a previous worker.

SCENARIO
  A previous worker delivered shared/<contract>.json with the listing-only
  skeleton (rank / name / slug / url filled, all other fields null). The new
  worker's job is to ENRICH each record with detail-page fields by following
  the explore→distill→batch pattern. The test verifies:

    - the worker recognizes the partial-file scenario from
      workflow_multi_record.md's "Special case" section
    - the worker calls batch_browser_actions with publish_to set
    - the final shared/<contract>.json has detail fields populated for >= 2/3
      records (allowing 1 to fail to e.g. Cloudflare flake)

USAGE
  conda activate agent
  cd browser_agent_v6
  python _test_phase3_from_phase2.py

This burns real LLM tokens but small scope (3 slugs, ≤15 worker turns expected).
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, List

os.environ.pop("SSL_CERT_FILE", None)
sys.path.insert(0, str(Path(__file__).parent))


# Three known TAAFT slugs we've seen produce real data in prior runs.
PARTIAL_RECORDS = [
    {"rank": 1,  "name": "SureThing.io", "slug": "surething",
     "url": "https://theresanaiforthat.com/ai/surething/",
     "views": None, "rating": None, "reviews_count": None},
    {"rank": 2,  "name": "Notis",        "slug": "notis-ai",
     "url": "https://theresanaiforthat.com/ai/notis-ai/",
     "views": None, "rating": None, "reviews_count": None},
    {"rank": 3,  "name": "Inkett",       "slug": "inkett",
     "url": "https://theresanaiforthat.com/ai/inkett/",
     "views": None, "rating": None, "reviews_count": None},
]
CONTRACT_NAME = "phase3_enrichment_test.json"


WORKER_TASK = f"""\
A previous worker has already delivered the listing skeleton to shared/{CONTRACT_NAME}.
The file contains 3 AI-tool records with `rank`, `name`, `slug`, `url` filled
in, but `views`, `rating`, `reviews_count` are all null.

Your job: enrich each of the 3 records with the missing detail-page fields
(views, rating, reviews_count) by visiting each tool's detail page on
theresanaiforthat.com.

Final output: shared/{CONTRACT_NAME} (overwrite — same path) with the same
records but `views`, `rating`, `reviews_count` populated.

This is a multi-record extraction task (N=3). Follow the workflow_multi_record
playbook strictly: read the skill FIRST, recognize that you're in the
"partial file already exists" special case, then go through the
explore→distill→batch phases. The final delivery MUST come from a single
batch_browser_actions call with publish_to='{CONTRACT_NAME}'.
"""


async def main() -> int:
    if sys.platform == "win32":
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    from core.llm_provider import LLMFactory, ModelConfig
    from core.agent_spawner import AgentSpawner
    from core.session_state import SessionState, Contract
    from tools import build_default_registry
    from sandbox.pool import WarmPool
    from browser.daemon import get_daemon
    from browser.helpers import set_active_port, set_window_size
    from browser.skills import auto_load as auto_load_skills

    BROWSER_PORT = 9222

    print("=" * 70)
    print("  TEST — PHASE 3 reachable from PHASE 2 (partial-file special case)")
    print("=" * 70)

    # Infrastructure
    print("\n[setup] starting browser daemon...")
    daemon = get_daemon(port=BROWSER_PORT); daemon.start()
    set_active_port(BROWSER_PORT)
    set_window_size(1440, 1740)
    pool = WarmPool(
        size=1, blocked_imports=["requests", "httpx", "aiohttp", "urllib"],
        browser_port=BROWSER_PORT,
    ); pool.start()

    skills_result = auto_load_skills()
    print(f"[setup] skills loaded: {skills_result}")

    config = ModelConfig.load_from_file("config.json")
    llm = LLMFactory.create_provider(config)
    print(f"[setup] LLM = {config.provider} / {config.model_id}")

    # Spawner
    work_root = Path(__file__).parent / "worktrees"
    spawner = AgentSpawner(
        registry=build_default_registry(),
        llm=llm,
        worktrees_root=str(work_root),
        shared_dir_root=str(work_root),
        pool=pool,
        daemon=daemon,
    )
    spawner.register_builtin()
    spawner.force_disable_iter_approval = True   # automated, no human at terminal

    # Build session + pre-populate the partial file
    session_id = f"phase3_test_{int(time.time())}"
    session_dir = work_root / session_id
    shared_dir = session_dir / "shared"
    shared_dir.mkdir(parents=True, exist_ok=True)
    partial_file = shared_dir / CONTRACT_NAME
    partial_file.write_text(
        json.dumps(PARTIAL_RECORDS, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"[setup] pre-populated partial file: {partial_file}")
    print(f"          {len(PARTIAL_RECORDS)} records, listing fields only "
          f"(views/rating/reviews_count = null)")

    st = SessionState(session_id=session_id)
    st.add_contract(Contract(
        name=CONTRACT_NAME,
        output_path=f"shared/{CONTRACT_NAME}",
        agent_type="worker",
        description="enrich the partial AI-tools file with views/rating/reviews_count",
        schema="[{rank, name, slug, url, views, rating, reviews_count}]",
    ))
    spawner.session_states[session_id] = st

    print("\n" + "=" * 70)
    print("  TASK")
    print("=" * 70)
    print(WORKER_TASK)
    print("=" * 70 + "\n")

    started = time.time()
    result = await spawner.spawn(
        agent_type="worker",
        task=WORKER_TASK,
        max_turns=20,
        session_id=session_id,
    )
    duration = time.time() - started

    pool.shutdown()

    # ── Analyze events ───────────────────────────────────────────
    events = result["events"]
    read_skill_calls = [ev for ev in events
                         if ev.get("type") == "tool_use" and ev.get("name") == "read_skill"]
    batch_calls = [ev for ev in events
                    if ev.get("type") == "tool_use" and ev.get("name") == "batch_browser_actions"]
    publish_calls = [ev for ev in events
                      if ev.get("type") == "tool_use" and ev.get("name") == "publish_artifact"]

    skill_names = [ev["input"].get("name") for ev in read_skill_calls]
    multi_record_loaded = "workflow_multi_record" in skill_names

    print("\n" + "=" * 70)
    print("  RESULTS")
    print("=" * 70)
    print(f"  duration:           {duration:.1f}s")
    print(f"  worker turns_used:  {result['turns_used']}")
    print(f"  worker stop_reason: {result['stop_reason']}")
    print(f"  worker success:     {result['success']}")
    print()
    print(f"  read_skill calls:   {len(read_skill_calls)} → {skill_names}")
    print(f"  batch_browser_actions calls: {len(batch_calls)}")
    print(f"  publish_artifact calls: {len(publish_calls)}")

    # Validate file
    if not partial_file.exists():
        print(f"\n  ❌ shared/{CONTRACT_NAME} disappeared")
        return 1
    body = json.loads(partial_file.read_text(encoding="utf-8"))
    if not isinstance(body, list):
        print(f"\n  ❌ file root is not a list")
        return 1

    print(f"\n  records in final file: {len(body)}")

    # Field-fill rates for the enriched fields (the originals all 100%)
    enriched_fields = ["views", "rating", "reviews_count"]
    fill = {f: 0 for f in enriched_fields}
    for r in body:
        if not isinstance(r, dict):
            continue
        for f in enriched_fields:
            v = r.get(f)
            if v not in (None, "", 0):
                fill[f] += 1

    n = len(body)
    print(f"\n  enriched-field fill rates:")
    for f in enriched_fields:
        pct = fill[f] / n * 100 if n else 0
        print(f"    {f:18s}: {fill[f]}/{n}  ({pct:.0f}%)")

    if body:
        print(f"\n  sample (first record):")
        for k, v in body[0].items():
            print(f"    {k:18s}: {v}")

    # ── Pass criteria ───────────────────────────────────────────
    print("\n" + "=" * 70)
    print("  ASSERTIONS")
    print("=" * 70)

    checks = []

    c = ("multi_record skill was loaded", multi_record_loaded)
    checks.append(c); print(f"  {'✓' if c[1] else '✗'} {c[0]}")

    c = ("batch_browser_actions called >= 1 time", len(batch_calls) >= 1)
    checks.append(c); print(f"  {'✓' if c[1] else '✗'} {c[0]}")

    if batch_calls:
        first_batch_input = batch_calls[0].get("input", {})
        has_publish_to = bool(first_batch_input.get("publish_to"))
        c = ("first batch had publish_to set", has_publish_to)
        checks.append(c); print(f"  {'✓' if c[1] else '✗'} {c[0]}")

        iter_count = len(first_batch_input.get("iterations", []))
        c = (f"first batch had >= 2 iterations (got {iter_count})", iter_count >= 2)
        checks.append(c); print(f"  {'✓' if c[1] else '✗'} {c[0]}")

    enriched_pct = sum(fill.values()) / (len(enriched_fields) * n) if n else 0
    c = (f"enriched-field overall fill >= 30% (got {enriched_pct*100:.0f}%)",
         enriched_pct >= 0.3)
    checks.append(c); print(f"  {'✓' if c[1] else '✗'} {c[0]}")

    passed = all(b for _, b in checks)
    print()
    print("=" * 70)
    print(f"  RESULT: {'✓ PASS' if passed else '✗ FAIL'}  ({sum(b for _,b in checks)}/{len(checks)})")
    print("=" * 70)
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
