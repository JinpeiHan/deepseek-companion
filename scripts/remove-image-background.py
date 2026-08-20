"""Cut generated frames to transparent 512x512 RGBA assets.

Two problems make the naive path wrong:

1. gpt-image-2 rejects `background=transparent`, so frames arrive on a flat white
   plate. `u2netp` cannot tell the white apron, frilled hem and white socks from
   that plate and eats them, so `isnet-anime` is used instead and its mask is
   hardened above a low threshold. RGB is taken from the untouched source, which
   keeps hair gaps transparent without leaving black fringes.
2. The endpoint ignores `size`, returning anything from 995x1580 to 1254x1254.
   Scaling each frame by its own bounding box would make a clip breathe, so one
   scale factor is shared across the whole group (default: the entire run).
"""

from __future__ import annotations

import argparse
import shutil
import sys
import tempfile
from pathlib import Path

import numpy as np
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent))

# One matting implementation, shared with normalize-pack-scale.py.
from remove_image_background_lib import (  # noqa: E402
    FOOT_ANCHOR_Y,
    MAX_HEIGHT_RATIO,
    MAX_WIDTH_RATIO,
    SOLID_ALPHA,
    TARGET,
    cut_alpha,
    fit,
    new_matting_session,
)


def group_key(path: Path, source_root: Path, mode: str) -> str:
    if mode == "pack":
        return "*"
    if mode == "clip":
        return path.relative_to(source_root).parent.as_posix()
    return path.as_posix()


def main() -> int:
    parser = argparse.ArgumentParser(description="Normalise generated frames to transparent 512x512 RGBA")
    parser.add_argument("--input", required=True, help="input PNG file or directory")
    parser.add_argument("--output", required=True, help="output PNG file or directory")
    parser.add_argument("--model", default="isnet-anime", help="rembg model name (default: isnet-anime)")
    parser.add_argument(
        "--preserve-vertical",
        action="store_true",
        help="keep each frame's height above the group's lowest foot instead of bottom-anchoring every frame",
    )
    parser.add_argument(
        "--group",
        default="pack",
        choices=("pack", "clip", "frame"),
        help="which frames share one scale factor (default: pack)",
    )
    args = parser.parse_args()

    source = Path(args.input)
    target = Path(args.output)
    if source.is_dir():
        files = sorted(p for p in source.rglob("*.png") if not p.name.endswith(".tmp.png"))
        pairs = [(p, target / p.relative_to(source)) for p in files]
        source_root = source
    else:
        pairs = [(source, target / source.name if target.is_dir() else target)]
        source_root = source.parent
    if not pairs:
        raise SystemExit(f"no PNG input found at {source}")

    session = new_matting_session(args.model)
    staging = Path(tempfile.mkdtemp(prefix="dsh-matte-"))
    try:
        # Pass 1: matte once, remember every bounding box.
        boxes: dict[Path, tuple[int, int, int, int]] = {}
        groups: dict[str, list[Path]] = {}
        group_of: dict[Path, str] = {}
        for index, (path, _) in enumerate(pairs):
            with Image.open(path) as handle:
                cut = cut_alpha(handle.convert("RGBA"), session)
            box = cut.getbbox()
            if box is None:
                raise SystemExit(f"{path.name}: image is fully transparent after matting")
            staged = staging / f"{index:04d}.png"
            cut.save(staged, format="PNG")
            boxes[staged] = box
            key = group_key(path, source_root, args.group)
            groups.setdefault(key, []).append(staged)
            group_of[staged] = key
            print(f"{path.name}: matted with {args.model}, bbox {box[2] - box[0]}x{box[3] - box[1]}")

        # One scale per group, driven by its tallest and widest frame, so relative
        # pose height differences inside a clip survive.
        scales: dict[Path, float] = {}
        for key, staged_files in groups.items():
            tallest = max(boxes[f][3] - boxes[f][1] for f in staged_files)
            widest = max(boxes[f][2] - boxes[f][0] for f in staged_files)
            scale = min((TARGET[1] * MAX_HEIGHT_RATIO) / tallest, (TARGET[0] * MAX_WIDTH_RATIO) / widest)
            for staged in staged_files:
                scales[staged] = scale
            print(f"group {key}: {len(staged_files)} frames, tallest {tallest}px, scale {scale:.4f}")

        # Pass 2: apply the shared transform and write the final assets.
        for index, (path, destination) in enumerate(pairs):
            staged = staging / f"{index:04d}.png"
            with Image.open(staged) as handle:
                lift = 0.0
                if args.preserve_vertical:
                    # The group's lowest foot is the ground; everything else
                    # keeps however far above it the model drew the character.
                    ground = max(boxes[f][3] for f in groups[group_of[staged]])
                    lift = (ground - boxes[staged][3]) * scales[staged]
                fitted = fit(handle.convert("RGBA"), boxes[staged], scales[staged], lift)
            assert fitted.mode == "RGBA", f"{path.name}: expected RGBA, got {fitted.mode}"
            assert fitted.size == TARGET, f"{path.name}: expected {TARGET}, got {fitted.size}"
            low, _ = fitted.getchannel("A").getextrema()
            assert low == 0, f"{path.name}: no fully transparent pixel after post-processing"
            destination.parent.mkdir(parents=True, exist_ok=True)
            temporary = destination.with_suffix(".tmp.png")
            fitted.save(temporary, format="PNG")
            temporary.replace(destination)
            print(f"{path.name} -> {destination}")
    finally:
        shutil.rmtree(staging, ignore_errors=True)

    print(f"post-processed {len(pairs)} images")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
