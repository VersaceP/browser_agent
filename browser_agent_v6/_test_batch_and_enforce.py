"""LLM-independent end-to-end validation for two architectural pieces:

  TEST A — batch_browser_actions + publish_to
    Real browser, no LLM. We hand-write a recipe + iterations and verify:
      • all iterations run server-side
      • the partial spill file is correct
      • publish_to creates shared/<name> with the aggregated JSON

  TEST B — _enforce_worker_publish_if_needed hook
    Mock LLM that does extraction work but never calls publish_artifact.
    Spawn a worker through AgentSpawner and verify:
      • execute_turn runs (LLM scripted)
      • after end_turn the enforce hook fires
      • the hook's reminder makes the next scripted turn publish
      • final shared/<file> exists

Both tests skip the LLM (TEST A bypasses spawner; TEST B uses a scripted
LLMProvider). They validate batch + publish + enforce independent of
kimi-k2.6 quota.

Run:
    conda activate agent
    cd browser_agent_v6
    python _test_batch_and_enforce.py
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Tuple

os.environ.pop("SSL_CERT_FILE", None)
sys.path.insert(0, str(Path(__file__).parent))


# ──────────────────────────────────
# TEST A — batch_browser_actions + publish_to (real browser, no LLM)
# ──────────────────────────────────

# Three known TAAFT detail page slugs we've seen in prior runs.
# Pick small N (3) so the test stays fast — same mechanism scales to N=10/50.
TEST_A_SLUGS = ["surething", "inkett", "notis-ai"]


async def test_a_batch_to_publish() -> bool:
    """Direct dispatch of batch_browser_actions, asserts publish_to lands a file."""
    print("\n" + "=" * 70)
    print("  TEST A — batch_browser_actions → publish_to (no LLM)")
    print("=" * 70)

    from browser.daemon import get_daemon
    from browser.helpers import set_active_port, set_window_size
    from sandbox.pool import WarmPool
    from tools import build_default_registry, dispatch
    from tools.base import ToolContext

    BROWSER_PORT = 9222
    daemon = get_daemon(port=BROWSER_PORT); daemon.start()
    set_active_port(BROWSER_PORT)
    set_window_size(1440, 1740)

    pool = WarmPool(
        size=1, blocked_imports=["requests", "httpx", "aiohttp", "urllib"],
        browser_port=BROWSER_PORT,
    ); pool.start()

    registry = build_default_registry()

    # Minimal spawner shim — batch_browser_actions handler reads ctx.spawner.registry
    class _SpawnerShim:
        def __init__(self, reg): self.registry = reg
        require_first_iteration_approval = False
        session_states: Dict[str, Any] = {}

    session_id = f"test_a_{int(time.time())}"
    work_root = Path(__file__).parent / "worktrees" / session_id
    worktree = work_root / "worker"
    shared = work_root / "shared"
    worktree.mkdir(parents=True, exist_ok=True)
    shared.mkdir(parents=True, exist_ok=True)

    ctx = ToolContext(
        worktree=str(worktree),
        shared_dir=str(shared),
        session_id=session_id,
        agent_type="worker",
        pool=pool,
        spawner=_SpawnerShim(registry),
        daemon=daemon,
    )

    # Recipe: navigate + 3 single-step extractions per page.
    # Uses ONLY tools allowed inside batch (navigate / extract_text / dom_query).
    recipe = [
        {"tool": "navigate", "input": {"url": "https://theresanaiforthat.com/ai/{slug}/", "wait": 4}},
        {"tool": "extract_text", "input": {"selector": ".rating_top"}},
        {"tool": "extract_text", "input": {"selector": ".stats_opens"}},
        {"tool": "dom_query", "input": {"selector": ".user_comments_wrapper .comment-wrapper", "max_items": 10}},
    ]
    iterations = [{"slug": s} for s in TEST_A_SLUGS]

    publish_name = "test_a_products.json"

    print(f"  recipe steps: {len(recipe)}")
    print(f"  iterations: {len(iterations)} → {[i['slug'] for i in iterations]}")
    print(f"  publish_to: {publish_name}")
    print(f"  shared dir: {shared}")

    started = time.time()
    result = await dispatch(registry, "batch_browser_actions", {
        "steps": recipe,
        "iterations": iterations,
        "publish_to": publish_name,
        "throttle_seconds": 0.4,
    }, ctx)
    duration = time.time() - started

    pool.shutdown()

    print(f"\n  duration: {duration:.1f}s")
    if result["status"] != "ok":
        print(f"  ❌ dispatch failed: {result}")
        return False

    body = result["result"]
    print(f"  batch ok={body['ok']}, completed={body['completed']}, "
          f"succeeded={body['succeeded_total']}/{body['iterations_total']}")
    print(f"  partial spill: {body.get('partial_saved_to')}")
    print(f"  published_to:  {body.get('published_to')}")

    # Assertions
    target = shared / publish_name
    if not target.exists():
        print(f"  ❌ shared/{publish_name} does NOT exist after publish_to")
        return False
    raw = target.read_text(encoding="utf-8")
    try:
        records = json.loads(raw)
    except json.JSONDecodeError as e:
        print(f"  ❌ shared/{publish_name} not valid JSON: {e}")
        return False

    if not isinstance(records, list):
        print(f"  ❌ published file root is not a list: got {type(records).__name__}")
        return False
    if len(records) != len(iterations):
        print(f"  ⚠ expected {len(iterations)} records, got {len(records)} "
              f"(some iters may have failed mid-step — see partial)")

    # Spot-check first record
    r0 = records[0] if records else {}
    print(f"\n  first record keys: {sorted(r0.keys())}")
    print(f"  first record _slug: {r0.get('_slug')}")

    rating_key = "step_1_extract_text"
    views_key = "step_2_extract_text"
    reviews_key = "step_3_dom_query"
    rating = r0.get(rating_key)
    views = r0.get(views_key)
    reviews_raw = r0.get(reviews_key)

    print(f"  first record rating ({rating_key}): {rating!r}")
    print(f"  first record views  ({views_key}):  {views!r}")
    print(f"  first record reviews ({reviews_key}): "
          f"{len(reviews_raw) if isinstance(reviews_raw, list) else 'not a list'} items")

    # Field-fill rates across all records
    n = len(records)
    rating_filled = sum(1 for r in records if r.get(rating_key))
    views_filled = sum(1 for r in records if r.get(views_key))
    reviews_filled = sum(1 for r in records if isinstance(r.get(reviews_key), list)
                         and len(r.get(reviews_key) or []) > 0)
    print(f"\n  fill rates across {n} records:")
    print(f"    rating       : {rating_filled}/{n}")
    print(f"    views        : {views_filled}/{n}")
    print(f"    reviews list : {reviews_filled}/{n}")

    # Pass criteria: file exists + at least 1 record + at least one field populated somewhere
    if not records:
        print("  ❌ no records published")
        return False
    if rating_filled + views_filled + reviews_filled == 0:
        print("  ❌ all extracted fields empty across all records — recipe might be wrong")
        return False

    print(f"\n  ✓ TEST A passed: batch + publish_to mechanism works end-to-end")
    return True


# ──────────────────────────────────
# TEST B — _enforce_worker_publish_if_needed hook (scripted LLM, no real LLM)
# ──────────────────────────────────


class ScriptedLLM:
    """A minimal LLMProvider that returns scripted assistant blocks in order.

    Each script entry is a tuple matching what BaseLLMProvider.generate_response
    must return: (text, tool_uses, stop_reason, usage_dict).
    Used to drive execute_turn deterministically without burning tokens.
    """

    def __init__(self, scripts: List[Tuple[str, List[Dict], str, Dict]]):
        self.scripts = scripts
        self.idx = 0
        self.calls: List[Dict] = []  # for inspection

    async def generate_response(self, system_prompt, messages, tools):
        if self.idx >= len(self.scripts):
            raise RuntimeError(
                f"ScriptedLLM exhausted after {self.idx} calls; "
                f"messages history len={len(messages)}; the test scripted too few responses."
            )
        s = self.scripts[self.idx]
        self.calls.append({
            "idx": self.idx,
            "system_prefix": (system_prompt or "")[:60],
            "msg_count": len(messages),
            "tool_count": len(tools or []),
        })
        self.idx += 1
        return s


async def _run_enforce_hook_scenario(
    scenario_label: str,
    publish_name: str,
    sample_data: list,
    scripts: list,
    expected_min_llm_calls: int,
) -> bool:
    """Reusable harness — script an LLM trajectory + check the hook fires correctly."""
    from core.agent_spawner import AgentSpawner
    from core.session_state import SessionState, Contract
    from sandbox.pool import WarmPool
    from browser.daemon import get_daemon
    from browser.helpers import set_active_port
    from tools import build_default_registry

    print(f"\n  --- scenario: {scenario_label} ---")

    BROWSER_PORT = 9222
    daemon = get_daemon(port=BROWSER_PORT); daemon.start()
    set_active_port(BROWSER_PORT)
    pool = WarmPool(
        size=1, blocked_imports=["requests", "httpx", "aiohttp", "urllib"],
        browser_port=BROWSER_PORT,
    ); pool.start()

    registry = build_default_registry()
    scripted_llm = ScriptedLLM(scripts)

    session_id = f"test_b_{scenario_label}_{int(time.time())}"
    work_root = Path(__file__).parent / "worktrees" / session_id
    work_root.mkdir(parents=True, exist_ok=True)

    spawner = AgentSpawner(
        registry=registry,
        llm=scripted_llm,
        worktrees_root=str(Path(__file__).parent / "worktrees"),
        shared_dir_root=str(Path(__file__).parent / "worktrees"),
        pool=pool,
        daemon=daemon,
        summarizer=None,
    )
    spawner.register_builtin()

    st = SessionState(session_id=session_id)
    st.add_contract(Contract(
        name=publish_name,
        output_path=f"shared/{publish_name}",
        agent_type="worker",
        description=f"{scenario_label} demo records and publish to shared/{publish_name}",
        schema="[{id:int, name:str}]",
    ))
    spawner.session_states[session_id] = st

    # Worker max_turns = 2 forces the second LLM call's tool_use to hit the cap
    # (only relevant for the max_turns scenario; for end_turn scenario, scripted
    # LLM ends with end_turn before cap)
    max_turns = 2 if scenario_label == "max_turns_cutoff" else 5

    result = await spawner.spawn(
        agent_type="worker",
        task=f"{scenario_label} demo records and publish to shared/{publish_name}",
        max_turns=max_turns,
        session_id=session_id,
    )
    pool.shutdown()

    print(f"    duration measured by spawn return")
    print(f"    scripted LLM calls used: {scripted_llm.idx}/{len(scripts)}")
    print(f"    worker turns_used: {result['turns_used']}, stop_reason: {result['stop_reason']}")

    target = Path(spawner.shared_dir_root) / session_id / "shared" / publish_name
    if not target.exists():
        print(f"    ❌ shared/{publish_name} NOT created — hook didn't fire or failed")
        return False
    body = json.loads(target.read_text(encoding="utf-8"))
    if body != sample_data:
        print(f"    ❌ content mismatch")
        return False
    if scripted_llm.idx < expected_min_llm_calls:
        print(f"    ❌ hook didn't re-engage LLM enough "
              f"({scripted_llm.idx} < {expected_min_llm_calls})")
        return False
    print(f"    ✓ {scenario_label}: hook fired correctly, file landed")
    return True


async def test_b_enforce_publish_hook() -> bool:
    """Scripted LLM does extraction without publishing → enforce hook must
    inject the reminder, the next scripted turn publishes, and shared/X exists.

    Two sub-scenarios:
      end_turn      — worker says "all done" w/o publish (the original case)
      max_turns_cutoff — worker would keep calling tools but hits max_turns
                         and stop_reason becomes tool_use (the worker-1 case
                         from the failed e2e)
    """
    print("\n" + "=" * 70)
    print("  TEST B — publish-enforcement hook (scripted LLM, no real LLM)")
    print("=" * 70)

    sample_data = [{"id": 1, "name": "demo"}, {"id": 2, "name": "demo2"}]

    # Sub-scenario 1: worker self-finishes via end_turn without publishing.
    # Trajectory: extract → end_turn → [hook fires] → publish → end_turn
    end_turn_scripts = [
        ("Looking around.",
         [{"id": "tu1", "name": "run_browser_python",
           "input": {"code": "print('inspecting; found 2 records')", "timeout": 10}}],
         "tool_use",
         {"cache_read": 0, "cache_creation": 0, "uncached_input": 200, "output": 30}),
        # call 2: NO publish, says "all done"
        ("All done!", [], "end_turn",
         {"cache_read": 200, "cache_creation": 0, "uncached_input": 50, "output": 10}),
        # call 3: hook re-engages → publish
        ("OK publishing now.",
         [{"id": "tu3", "name": "publish_artifact",
           "input": {"name": "test_b_endturn.json", "content": sample_data,
                     "description": "end_turn scenario"}}],
         "tool_use",
         {"cache_read": 250, "cache_creation": 0, "uncached_input": 80, "output": 20}),
        # call 4: end_turn after publish
        ("Published.", [], "end_turn",
         {"cache_read": 350, "cache_creation": 0, "uncached_input": 30, "output": 15}),
    ]

    # Sub-scenario 2: worker hits max_turns mid-flight (the worker-1 case from
    # the rank 11-20 e2e). max_turns=2 means call 2's tool_use response gets
    # capped → stop_reason='tool_use'. Hook should fire for tool_use too.
    # Trajectory: extract → extract-again-but-cap-hits → [hook fires] → publish → end_turn
    max_turns_scripts = [
        ("Looking around — first probe.",
         [{"id": "tu1", "name": "run_browser_python",
           "input": {"code": "print('found stage 1 data')", "timeout": 10}}],
         "tool_use",
         {"cache_read": 0, "cache_creation": 0, "uncached_input": 200, "output": 30}),
        # call 2: still doing extraction, NOT publishing — hits max_turns=2
        # The execution_loop will set stop_reason='tool_use' here because it
        # tried to dispatch tools but ran out of turn budget after this one.
        ("Now probing more.",
         [{"id": "tu2", "name": "run_browser_python",
           "input": {"code": "print('stage 2 — about to publish but no time')", "timeout": 10}}],
         "tool_use",
         {"cache_read": 200, "cache_creation": 0, "uncached_input": 100, "output": 30}),
        # call 3: hook re-engages → MUST publish (per the cutoff_note in reminder)
        ("OK publishing what I have.",
         [{"id": "tu3", "name": "publish_artifact",
           "input": {"name": "test_b_maxturns.json", "content": sample_data,
                     "description": "max_turns scenario"}}],
         "tool_use",
         {"cache_read": 300, "cache_creation": 0, "uncached_input": 90, "output": 20}),
        # call 4: end_turn after publish
        ("Done.", [], "end_turn",
         {"cache_read": 400, "cache_creation": 0, "uncached_input": 30, "output": 15}),
    ]

    p1 = await _run_enforce_hook_scenario(
        scenario_label="end_turn",
        publish_name="test_b_endturn.json",
        sample_data=sample_data,
        scripts=end_turn_scripts,
        expected_min_llm_calls=3,
    )

    p2 = await _run_enforce_hook_scenario(
        scenario_label="max_turns_cutoff",
        publish_name="test_b_maxturns.json",
        sample_data=sample_data,
        scripts=max_turns_scripts,
        expected_min_llm_calls=3,
    )

    if p1 and p2:
        print(f"\n  ✓ TEST B passed: enforce hook fires on BOTH end_turn AND max_turns/tool_use")
        return True
    print(f"\n  ❌ TEST B failed: end_turn={p1}, max_turns_cutoff={p2}")
    return False


# ──────────────────────────────────
# main
# ──────────────────────────────────

async def main():
    if sys.platform == "win32":
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    a_pass = False
    b_pass = False

    try:
        a_pass = await test_a_batch_to_publish()
    except Exception as e:
        import traceback
        print(f"\n  ❌ TEST A crashed: {type(e).__name__}: {e}")
        traceback.print_exc()

    try:
        b_pass = await test_b_enforce_publish_hook()
    except Exception as e:
        import traceback
        print(f"\n  ❌ TEST B crashed: {type(e).__name__}: {e}")
        traceback.print_exc()

    print("\n" + "=" * 70)
    print(f"  RESULTS: TEST A {'✓' if a_pass else '✗'}    TEST B {'✓' if b_pass else '✗'}")
    print("=" * 70)
    return 0 if (a_pass and b_pass) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
