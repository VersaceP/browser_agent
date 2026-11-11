"""One deployment switch for independent task review across all runtimes."""
from __future__ import annotations


def reviewer_enabled(runtime) -> bool:
    # Schema-only consumers and older embedded callers may have no runtime
    # or supply a partial configuration object. Both retain the default.
    return getattr(getattr(runtime, "harness", None), "reviewer_enabled", True)
