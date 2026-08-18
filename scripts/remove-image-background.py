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
import tempfile
from pathlib import Path

import numpy as np
from PIL import Image
from rembg import new_session, remove
from scipy import ndimage

TARGET = (512, 512)
# Matches footAnchor/logical sizing in the pack manifests: the character stands
# on 97% of the frame height and never fills more than 90% of it.
FOOT_ANCHOR_Y = 0.97
MAX_HEIGHT_RATIO = 0.90
MAX_WIDTH_RATIO = 0.94
# Anything the matting model gives more than this much opacity is treated as
# character. Kept low on purpose: white-on-white cloth comes back faint.
SOLID_ALPHA = 30


def cut_alpha(source: Image.Image, session) -> Image.Image:
    """Return the source RGB with a hardened alpha channel."""
    mask = np.asarray(remove(source, session=session).convert("RGBA"))[:, :, 3]
    solid = mask > SOLID_ALPHA
    labels, count = ndimage.label(solid)
    if count > 1:
        sizes = ndimage.sum(solid, labels, range(1, count + 1))
        solid = labels == (int(np.argmax(sizes)) + 1)
    if not solid.any():
        raise ValueError("matting produced an empty mask")
    cut = source.copy()
    cut.putalpha(Image.fromarray(np.where(solid, 255, 0).astype(np.uint8)))
    return cut


def fit(image: Image.Image, box: tuple[int, int, int, int], scale: float) -> Image.Image:
    """Place one character into a 512x512 frame using a shared scale factor."""
    character = image.crop(box)
    size = (max(1, round(character.width * scale)), max(1, round(character.height * scale)))
    character = character.resize(size, Image.Resampling.LANCZOS)
    canvas = Image.new("RGBA", TARGET, (0, 0, 0, 0))
    left = max(0, (TARGET[0] - size[0]) // 2)
    top = max(0, min(round(TARGET[1] * FOOT_ANCHOR_Y) - size[1], TARGET[1] - size[1]))
    canvas.paste(character, (left, top), character)
    return canvas


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

    session = new_session(args.model, providers=["CPUExecutionProvider"])
    staging = Path(tempfile.mkdtemp(prefix="dsh-matte-"))
    try:
        # Pass 1: matte once, remember every bounding box.
        boxes: dict[Path, tuple[int, int, int, int]] = {}
        groups: dict[str, list[Path]] = {}
        for index, (path, _) in enumerate(pairs):
            with Image.open(path) as handle:
                cut = cut_alpha(handle.convert("RGBA"), session)
            box = cut.getbbox()
            if box is None:
                raise SystemExit(f"{path.name}: image is fully transparent after matting")
            staged = staging / f"{index:04d}.png"
            cut.save(staged, format="PNG")
            boxes[staged] = box
            groups.setdefault(group_key(path, source_root, args.group), []).append(staged)
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
                fitted = fit(handle.convert("RGBA"), boxes[staged], scales[staged])
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
