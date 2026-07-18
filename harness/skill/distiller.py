"""Cached loader for the standalone trace distiller."""

from __future__ import annotations

import importlib.util
from functools import lru_cache
from pathlib import Path
from typing import Any


DISTILLER_PATH = (
    Path(__file__).resolve().parents[2] / "skills" / "_tools" / "distill_trace.py"
)


@lru_cache(maxsize=1)
def load_distiller() -> Any:
    spec = importlib.util.spec_from_file_location("_skill_distiller", DISTILLER_PATH)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load distiller from {DISTILLER_PATH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module
