#!/usr/bin/env python3
"""Wrapper for the 1688 capture and PDD draft-fill phases."""

import argparse
import subprocess
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPT_DIR = Path(__file__).resolve().parent
CAPTURE = SCRIPT_DIR / "abcp_1688_capture.py"
PDD = SCRIPT_DIR / "abcp_pdd_publish.py"


def run(cmd):
    print("+ " + " ".join(str(part) for part in cmd), flush=True)
    return subprocess.run(cmd, cwd=REPO_ROOT, check=True)


def capture(args) -> int:
    cmd = [sys.executable, str(CAPTURE), "--keyword", args.keyword, "--ranks", args.ranks]
    if args.page_id:
        cmd += ["--page-id", args.page_id]
    if args.fleet_id:
        cmd += ["--fleet-id", args.fleet_id]
    run(cmd)
    return 0


def fill(args) -> int:
    targets = list(args.product_dir or [])
    targets += list(args.manifest or [])
    if not targets:
        raise SystemExit("Provide at least one --product-dir or --manifest")
    for target in targets:
        cmd = [sys.executable, str(PDD), "fill-draft", "--page-id", args.page_id]
        path = Path(target).expanduser()
        if path.is_dir():
            cmd += ["--product-dir", str(path)]
        else:
            cmd += ["--manifest", str(path)]
        if args.skip_detail:
            cmd.append("--skip-detail")
        run(cmd)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Run reusable 1688 to PDD draft phases.")
    sub = parser.add_subparsers(dest="phase", required=True)

    capture_parser = sub.add_parser("capture")
    capture_parser.add_argument("--keyword", default="汉服女装")
    capture_parser.add_argument("--ranks", default="6,7")
    capture_parser.add_argument("--page-id", default=None)
    capture_parser.add_argument("--fleet-id", default=None)

    fill_parser = sub.add_parser("fill")
    fill_parser.add_argument("--page-id", required=True)
    fill_parser.add_argument("--product-dir", action="append", default=[])
    fill_parser.add_argument("--manifest", action="append", default=[])
    fill_parser.add_argument("--skip-detail", action="store_true")

    args = parser.parse_args()
    if args.phase == "capture":
        return capture(args)
    if args.phase == "fill":
        return fill(args)
    raise SystemExit(f"Unknown phase: {args.phase}")


if __name__ == "__main__":
    raise SystemExit(main())
