import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from harness.schema_loader import load_capability_bundle


class _Logger:
    def __init__(self):
        self.events = []

    def write(self, event_type, payload):
        self.events.append((event_type, payload))


class _FakeCapabilityBrowser:
    def __init__(self):
        self.calls = []

    async def call(self, method, params):
        self.calls.append((method, params))
        if method == "System.getCapabilities":
            return {
                "data": {
                    "actions": [
                        {"method": "Page.getState", "description": "Read state"},
                    ]
                }
            }
        if method == "System.describeAction":
            name = str(params.get("method") or "")
            return {
                "data": {
                    "method": name,
                    "description": f"Schema for {name}",
                    "params": {},
                    "requiresPurpose": False,
                }
            }
        raise RuntimeError(f"unexpected method: {method}")


class SchemaCachePruneTests(unittest.TestCase):
    def test_load_capability_bundle_prunes_removed_cached_methods(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache_dir = Path(tmp) / "schemas"
            cache_dir.mkdir()
            live_path = cache_dir / "Page.getState.json"
            stale_path = cache_dir / "Removed.method.json"
            live_path.write_text(
                json.dumps(
                    {
                        "method": "Page.getState",
                        "description": "cached",
                        "params": {},
                        "requiresPurpose": False,
                    }
                ),
                encoding="utf-8",
            )
            stale_path.write_text(
                json.dumps(
                    {
                        "method": "Removed.method",
                        "description": "stale",
                        "params": {},
                        "requiresPurpose": False,
                    }
                ),
                encoding="utf-8",
            )

            browser = _FakeCapabilityBrowser()
            logger = _Logger()
            bundle = asyncio.run(
                load_capability_bundle(
                    browser,
                    logger=logger,
                    schema_cache_dir=cache_dir,
                )
            )

            self.assertEqual(bundle.capability_methods, {"Page.getState"})
            self.assertTrue(live_path.exists())
            self.assertFalse(stale_path.exists())
            self.assertNotIn("Removed.method", bundle.method_schemas)
            self.assertFalse(
                any(
                    call == ("System.describeAction", {"method": "Removed.method"})
                    for call in browser.calls
                )
            )
            loaded_events = [
                payload
                for event_type, payload in logger.events
                if event_type == "schema.bundle.loaded"
            ]
            self.assertEqual(loaded_events[-1]["schema_cache_pruned"], 1)


class CapabilityHashPolicyTests(unittest.TestCase):
    """The capability hash must fold in the method-policy fingerprint so that
    un-banning a method (e.g. DOM.getSemanticTree leaving the blocked set)
    invalidates a cache built while it was blocked, even though the raw
    System.getCapabilities response is unchanged."""

    def test_policy_fingerprint_changes_digest(self) -> None:
        from harness.schema_cache import capability_hash

        caps = [{"method": "DOM.getAXTree"}, {"method": "DOM.getSemanticTree"}]
        base = capability_hash(caps)
        blocked = capability_hash(caps, policy_fingerprint={"DOM.getSemanticTree"})
        unblocked = capability_hash(caps, policy_fingerprint=set())

        # Legacy capability-only hash is unaffected by policy.
        self.assertNotEqual(base, blocked)
        # Un-banning (different blocked set) yields a different cache key.
        self.assertNotEqual(blocked, unblocked)
        # Stable + order-independent for the same policy set.
        self.assertEqual(
            blocked,
            capability_hash(list(reversed(caps)), policy_fingerprint=["DOM.getSemanticTree"]),
        )


class SchemaBootstrapDegradedTests(unittest.TestCase):
    """When THIS run's bootstrap failed (no browser / empty caps / lock timeout /
    exception), a stale on-disk cache must NOT drive the strict unknown-method
    check — otherwise a disconnected run wrongly rejects now-valid methods (e.g.
    a freshly un-banned DOM.getSemanticTree)."""

    def _fake_lead(self, worktree_dir: str, degraded: bool):
        from types import SimpleNamespace

        return SimpleNamespace(
            _schema_bootstrap_degraded=degraded,
            runtime=SimpleNamespace(
                harness=SimpleNamespace(worktree_dir=worktree_dir)
            ),
        )

    def test_degraded_flag_skips_strict_check_despite_stale_cache(self) -> None:
        from agent_harness import LeadAgent, SchemaCacheStatus
        from harness.schema_cache import (
            global_schema_cache_dir,
            global_schemas_dir,
            write_cached_capability_hash,
        )

        with tempfile.TemporaryDirectory() as tmp:
            worktree = str(Path(tmp) / "worktree")
            cache_dir = global_schema_cache_dir(worktree)
            schemas_dir = global_schemas_dir(worktree)
            schemas_dir.mkdir(parents=True, exist_ok=True)
            (schemas_dir / "Page.getState.json").write_text(
                json.dumps(
                    {
                        "method": "Page.getState",
                        "description": "cached",
                        "params": {},
                        "requiresPurpose": False,
                    }
                ),
                encoding="utf-8",
            )
            write_cached_capability_hash(cache_dir, digest="abc", capability_count=1)

            # Healthy bootstrap -> the on-disk cache is authoritative.
            status, methods = LeadAgent._schema_cache_status(
                self._fake_lead(worktree, False)
            )
            self.assertEqual(status, SchemaCacheStatus.LOADED_OK)
            self.assertIn("Page.getState", methods)

            # Degraded bootstrap -> ignore the stale cache, skip strict check.
            status, methods = LeadAgent._schema_cache_status(
                self._fake_lead(worktree, True)
            )
            self.assertEqual(status, SchemaCacheStatus.NOT_LOADED)
            self.assertEqual(methods, set())


if __name__ == "__main__":
    unittest.main()
