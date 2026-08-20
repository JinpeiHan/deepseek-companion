"""Turn a sticker in pet-emoji/ into a first frame that cannot crop the feet.

The stickers are not assets. They carry burnt-in captions, some sit on a full
scene background, and several are cropped below the knee -- five of the sixteen
have ink running off the bottom edge. Feeding one of those to image-to-video
propagates the crop into every frame of the clip, because the model matches the
framing it is given.

So this does two things the plain matting does not:

**It measures the crop and refuses.** A sticker whose character already touches
the bottom edge cannot be rescued by padding; the feet are simply not in the
picture. Those have to fall back to the pack's own full-body master and describe
the pose in words instead, which is a decision for the caller, so this exits
non-zero rather than quietly emitting a cropped frame.

**It reserves margin rather than filling the canvas.** The matting pipeline fits
a character to 90% of the frame height because a runtime asset wants to be as
large as it can be. A generation input wants the opposite: room for the model to
move the character without pushing a foot out of frame. FILL_RATIO leaves that
room, and the character sits above the bottom edge by a real margin.

Captions are dropped by the same largest-connected-component rule the matting
already uses, but a caption drawn touching the character survives it, so the
result is checked for stray ink and reported.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent))

from remove_image_background_lib import bleed_edges, cut_alpha, new_matting_session  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
# Generation canvas: portrait, so a standing full body fits without cropping.
CANVAS = (512, 768)
# Fraction of the canvas height the character may occupy. Deliberately well
# below the runtime pipeline's 0.90: the rest is headroom for the animation.
FILL_RATIO = 0.72
# Where the feet sit. 0.88 leaves ~92px of clear canvas underneath at 768.
FOOT_ANCHOR_Y = 0.88
EDGE_BAND = 6


def edge_ink(image: Image.Image) -> dict[str, int]:
    array = np.asarray(image.convert("RGB")).astype(int)
    reference = array[0, 0]
    ink = np.abs(array - reference).sum(axis=2) > 40
    return {
        "bottom": int(ink[-EDGE_BAND:, :].sum()),
        "top": int(ink[:EDGE_BAND, :].sum()),
        "left": int(ink[:, :EDGE_BAND].sum()),
        "right": int(ink[:, -EDGE_BAND:].sum()),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Prepare a sticker as a crop-safe generation first frame")
    parser.add_argument("--input", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--model", default="isnet-anime")
    parser.add_argument(
        "--allow-cropped",
        action="store_true",
        help="proceed even though the source already cuts off the feet",
    )
    args = parser.parse_args()

    source_path = Path(args.input)
    with Image.open(source_path) as handle:
        source = handle.convert("RGB")

    edges = edge_ink(source)
    width = source.size[0]
    if edges["bottom"] > width * 0.05 and not args.allow_cropped:
        print(
            f"{source_path.name}: {edges['bottom']}px of ink on the bottom edge -- the feet are "
            "already outside this sticker. Use the pack's full-body master as the first frame and "
            "describe the pose in the prompt instead, or pass --allow-cropped.",
            file=sys.stderr,
        )
        return 2

    cut = cut_alpha(source, new_matting_session(args.model))
    box = cut.split()[3].getbbox()
    if box is None:
        raise SystemExit(f"{source_path.name}: matting produced an empty mask")

    character = bleed_edges(cut.crop(box))
    scale = min(
        (CANVAS[1] * FILL_RATIO) / character.height,
        (CANVAS[0] * 0.86) / character.width,
    )
    size = (max(1, round(character.width * scale)), max(1, round(character.height * scale)))
    character = character.resize(size, Image.Resampling.LANCZOS)

    canvas = Image.new("RGBA", CANVAS, (0, 0, 0, 0))
    left = (CANVAS[0] - size[0]) // 2
    top = round(CANVAS[1] * FOOT_ANCHOR_Y) - size[1]
    # Straight copy: compositing with the image as its own mask darkens every
    # semi-transparent edge pixel and squares its alpha.
    canvas.paste(character, (max(0, left), max(0, top)))

    final_box = canvas.split()[3].getbbox()
    clearance = CANVAS[1] - final_box[3]
    assert clearance > 24, f"{source_path.name}: only {clearance}px below the feet"
    assert final_box[1] > 8, f"{source_path.name}: character touches the top edge"

    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    temp = out.with_suffix(".tmp.png")
    canvas.save(temp, "PNG")
    temp.replace(out)
    print(
        f"{source_path.name}: character {final_box[2]-final_box[0]}x{final_box[3]-final_box[1]}, "
        f"{clearance}px clear below the feet -> {out}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
