#!/usr/bin/env python3
"""Compatibility wrapper for the 1688-to-PDD draft skill capture script."""

from pathlib import Path
import runpy


TARGET = (
    Path(__file__).resolve().parents[3]
    / "skills"
    / "1688-to-pdd-draft"
    / "scripts"
    / "abcp_1688_capture.py"
)

if __name__ == "__main__":
    runpy.run_path(str(TARGET), run_name="__main__")
