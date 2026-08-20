"""Remove camera drift from a generated clip without flattening the performance.

MiniMax-H3 slowly zooms and slides during a clip: the character grew steadily
across a 124-frame sweep even with both ends pinned to the same frame and the
prompt asking for a constant size.

Normalising every frame independently does remove the drift, but it also removes
everything else. Height is not a constant of a good performance -- the character
*should* get shorter crouching in `poke` and taller with her feet off the ground
in `dragging` -- so per-frame normalisation silently deletes exactly the motion
the clip was generated for.

The two effects separate cleanly in the frequency domain. Camera drift is slow
and monotonic across the whole clip; pose change is fast and local. So fit a
low-order polynomial to the measured height and centre, treat that fit as the
camera, and divide it out. The residual -- the frame-to-frame departure from the
trend -- is the performance, and it survives untouched.

Input frames must already be matted RGBA on a shared scale (matte with
``--group pack``, not ``--group frame``, or the drift is gone before this can
measure it).
"""

from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np
from PIL import Image

TARGET = (512, 512)
FOOT_ANCHOR_Y = 0.97
MAX_HEIGHT_RATIO = 0.90
MAX_WIDTH_RATIO = 0.94


def measure(path: Path) -> tuple[float, float, float]:
    with Image.open(path) as handle:
        alpha = handle.convert("RGBA").split()[3]
    box = alpha.getbbox()
    if box is None:
        raise SystemExit(f"{path.name}: frame is fully transparent")
    height = float(box[3] - box[1])
    centre_x = float(box[0] + box[2]) / 2.0
    foot_y = float(box[3])
    return height, centre_x, foot_y


def trend(values: np.ndarray, degree: int) -> np.ndarray:
    """Low-order fit over frame index: the camera, not the character."""
    if len(values) <= degree + 1:
        return np.full_like(values, float(np.mean(values)))
    x = np.arange(len(values), dtype=np.float64)
    return np.polyval(np.polyfit(x, values, degree), x)


def main() -> int:
    parser = argparse.ArgumentParser(description="Divide camera drift out of a matted clip")
    parser.add_argument("--input", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--degree", type=int, default=2, help="polynomial order of the drift model")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    sources = sorted(Path(args.input).glob("*.png"))
    if not sources:
        raise SystemExit(f"no PNG frames in {args.input}")

    measured = [measure(path) for path in sources]
    heights = np.array([m[0] for m in measured])
    centres = np.array([m[1] for m in measured])

    height_trend = trend(heights, args.degree)
    centre_trend = trend(centres, args.degree)
    # Anchor on the trend's own midpoint rather than frame 0, so a clip whose
    # drift runs both ways is not dragged toward one end.
    height_ref = float(np.median(height_trend))
    centre_ref = float(np.median(centre_trend))

    raw_spread = float(heights.max() - heights.min())
    trend_spread = float(height_trend.max() - height_trend.min())
    residual = heights * (height_ref / height_trend)
    residual_spread = float(residual.max() - residual.min())
    print(
        f"{len(sources)} frames: raw height spread {raw_spread:.0f}px, "
        f"drift model {trend_spread:.0f}px, performance kept {residual_spread:.0f}px"
    )
    if args.dry_run:
        for index, path in enumerate(sources):
            print(f"  {path.name}: h {heights[index]:.0f} -> {residual[index]:.0f} "
                  f"(x{height_ref / height_trend[index]:.3f})")
        return 0

    out_dir = Path(args.output)
    out_dir.mkdir(parents=True, exist_ok=True)
    for index, path in enumerate(sources):
        scale = height_ref / float(height_trend[index])
        shift = centre_ref - float(centre_trend[index])
        with Image.open(path) as handle:
            image = handle.convert("RGBA")
        box = image.split()[3].getbbox()
        character = image.crop(box)
        size = (max(1, round(character.width * scale)), max(1, round(character.height * scale)))
        character = character.resize(size, Image.Resampling.LANCZOS)
        # Clamp only if the corrected frame would leave the canvas budget.
        limit = min(
            (TARGET[1] * MAX_HEIGHT_RATIO) / size[1],
            (TARGET[0] * MAX_WIDTH_RATIO) / size[0],
            1.0,
        )
        if limit < 1.0:
            size = (max(1, round(size[0] * limit)), max(1, round(size[1] * limit)))
            character = character.resize(size, Image.Resampling.LANCZOS)
        canvas = Image.new("RGBA", TARGET, (0, 0, 0, 0))
        left = int(round((TARGET[0] - size[0]) / 2.0 + shift * scale))
        left = max(0, min(left, TARGET[0] - size[0]))
        top = max(0, min(round(TARGET[1] * FOOT_ANCHOR_Y) - size[1], TARGET[1] - size[1]))
        # Straight copy: compositing with the image as its own mask would darken
        # every semi-transparent edge pixel and square its alpha.
        canvas.paste(character, (left, top))
        assert canvas.size == TARGET
        assert canvas.split()[3].getextrema()[0] == 0, f"{path.name}: no transparent pixel"
        temp = out_dir / f"{path.stem}.tmp.png"
        canvas.save(temp, "PNG")
        temp.replace(out_dir / path.name)
    print(f"wrote {len(sources)} detrended frames to {out_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
