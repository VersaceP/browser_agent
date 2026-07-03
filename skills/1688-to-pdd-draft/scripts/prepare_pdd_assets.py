#!/usr/bin/env python3
"""Prepare PDD-ready carousel images for an existing 1688 product directory."""

import argparse
import importlib.util
import json
import shutil
import subprocess
from pathlib import Path
from typing import List, Optional, Tuple


CAPTURE_SCRIPT = Path(__file__).resolve().with_name("abcp_1688_capture.py")


def load_capture_module():
    spec = importlib.util.spec_from_file_location("abcp_1688_capture_for_manifest", CAPTURE_SCRIPT)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Could not load capture script: {CAPTURE_SCRIPT}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def infer_keyword(product_dir: Path, raw: dict) -> str:
    if product_dir.name.startswith("1688_") and "_第" in product_dir.name:
        return product_dir.name[len("1688_"):].split("_第", 1)[0]
    return str(raw.get("source", {}).get("keyword") or "汉服女装")


def image_size(path: Path) -> Optional[Tuple[int, int]]:
    try:
        result = subprocess.run(
            ["/usr/bin/sips", "-g", "pixelWidth", "-g", "pixelHeight", str(path)],
            check=True,
            text=True,
            capture_output=True,
        )
    except Exception:
        return None
    width = height = None
    for line in result.stdout.splitlines():
        line = line.strip()
        if line.startswith("pixelWidth:"):
            width = int(line.split(":", 1)[1].strip())
        elif line.startswith("pixelHeight:"):
            height = int(line.split(":", 1)[1].strip())
    if width is None or height is None:
        return None
    return width, height


def is_product_main_image(path: Path) -> bool:
    size = image_size(path)
    if size is None:
        return True
    width, height = size
    return width >= 400 and height >= 400


def prepare(product_dir: Path, max_main: int) -> List[str]:
    output_dir = product_dir / "pdd_upload"
    output_dir.mkdir(exist_ok=True)
    for old in output_dir.glob("main_image_*.jpg"):
        old.unlink()
    sources = sorted(
        path for path in product_dir.glob("main_image_*")
        if path.is_file()
        and path.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp"}
        and is_product_main_image(path)
    )
    saved: List[str] = []
    for index, source in enumerate(sources[:max_main], start=1):
        target = output_dir / f"main_image_{index:02d}.jpg"
        if source.suffix.lower() in {".jpg", ".jpeg"}:
            shutil.copyfile(source, target)
        else:
            subprocess.run(
                ["/usr/bin/sips", "-s", "format", "jpeg", str(source), "--out", str(target)],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        saved.append(str(target))
    return saved


def update_manifest(product_dir: Path, images: List[str], refresh: bool = False) -> None:
    manifest_path = product_dir / "product_manifest.json"
    if manifest_path.exists() and not refresh:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest.setdefault("pdd", {}).setdefault("assets", {})["pddMainImages"] = images
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        return

    raw_path = product_dir / "raw_product_data.json"
    if not raw_path.exists():
        return
    raw = json.loads(raw_path.read_text(encoding="utf-8"))
    capture = load_capture_module()
    capture.KEYWORD = infer_keyword(product_dir, raw)
    saved_main = sorted(str(path) for path in product_dir.glob("main_image_*") if path.is_file())
    saved_detail = sorted(str(path) for path in product_dir.glob("detail_image_*") if path.is_file())
    saved_video = sorted(
        str(path) for path in product_dir.glob("video_*")
        if path.is_file() and path.suffix.lower() in {".mp4", ".mov", ".m4v", ".flv", ".webm"}
    )
    capture.write_product_manifest(product_dir, raw, saved_main, saved_detail, saved_video)


def main() -> int:
    parser = argparse.ArgumentParser(description="Prepare PDD carousel images under pdd_upload/.")
    parser.add_argument("product_dir")
    parser.add_argument("--max-main", type=int, default=10)
    parser.add_argument("--refresh-manifest", action="store_true")
    args = parser.parse_args()

    product_dir = Path(args.product_dir).expanduser()
    if not product_dir.exists():
        raise SystemExit(f"Product directory not found: {product_dir}")
    images = prepare(product_dir, args.max_main)
    update_manifest(product_dir, images, refresh=args.refresh_manifest)
    print(json.dumps({"productDir": str(product_dir), "pddMainImages": images}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
