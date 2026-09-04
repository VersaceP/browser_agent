"""Prompt assembly and LLM-pullable Harness operating guides.

The system prompt contains only the stable, safety-critical operating
contract.  Detailed, implementation-facing recovery guidance lives in this
package and is exposed to the model through the ``read_harness_guide`` tool.
"""

from .guides import (
    clear_guide_registry_cache,
    guide_manifest,
    guide_registry_errors,
    guides_for_audience,
    read_harness_guide,
    search_harness_guides,
)

__all__ = [
    "clear_guide_registry_cache",
    "guide_manifest",
    "guide_registry_errors",
    "guides_for_audience",
    "read_harness_guide",
    "search_harness_guides",
]
