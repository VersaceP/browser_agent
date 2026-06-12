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


if __name__ == "__main__":
    unittest.main()
