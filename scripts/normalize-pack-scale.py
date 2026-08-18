"""Give every proportion pack one consistent on-screen character size.

remove-image-background.py shares one scale factor across a generation *run*,
which is right for a single batch and wrong the moment a pack is built from more
than one. Three failures come out of that:

1. Mixed batches. Regenerating a few clips leaves the pack holding finished
   512x512 RGBA frames next to fresh 1254x1254 white-plate responses. Re-running
   the matting script over the whole directory would take its scale from the
   tallest raw frame and shrink the finished ones to about 40%.
2. Scale pop. A frame the model happened to draw larger stays larger, because
   sharing one factor preserves relative sizes on purpose.
   assets/pet-slender/head_pat/head_pat_512_00.png arrived 25% taller than the
   rest of its clip that way.
3. Packs of different heights. chibi, standard and slender were each scaled
   against their own batch, so slender ended up 22% shorter on screen than chibi
   even though the acceptance criterion is 等高.

The fix needs a pose-invariant target, and bounding-box height is not one: a
character legitimately gets shorter when ducking or taller with its feet off the
ground. So the chibi pack -- already shipped and human-approved, and the only
pack with all 19 clips -- supplies the template. For every frame we want

    target_height = REFERENCE_HEIGHT * (chibi_height(clip, frame) / chibi_idle_height)

which transfers chibi's per-frame height *relationships* onto the other packs
while pinning all three to the same idle height. Pose variation survives; batch
accidents and cross-pack drift do not.

Frames still on a white plate are matted first, reusing cut_alpha() from
remove-image-background.py so there is one matting implementation.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent))

from remove_image_background_lib import cut_alpha, new_matting_session  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
TARGET = (512, 512)
FOOT_ANCHOR_Y = 0.97
MAX_HEIGHT_RATIO = 0.90
MAX_WIDTH_RATIO = 0.94
# Every pack's idle frame is scaled to this character height in 512-canvas
# pixels. 445 keeps standard almost exactly where it already sits, so the pack
# that is currently correct barely moves.
REFERENCE_HEIGHT = 445.0


def char_box(image: Image.Image) -> tuple[int, int, int, int]:
    box = image.convert("RGBA").split()[3].getbbox()
    if box is None:
        raise ValueError("frame is fully transparent")
    return box


def is_plate(image: Image.Image) -> bool:
    """True when the frame is still an opaque generator response."""
    alpha = np.asarray(image.convert("RGBA"))[:, :, 3]
    return bool((alpha > 250).all())


def place(image: Image.Image, box: tuple[int, int, int, int], scale: float) -> Image.Image:
    character = image.crop(box)
    size = (max(1, round(character.width * scale)), max(1, round(character.height * scale)))
    character = character.resize(size, Image.Resampling.LANCZOS)
    canvas = Image.new("RGBA", TARGET, (0, 0, 0, 0))
    left = max(0, (TARGET[0] - size[0]) // 2)
    top = max(0, min(round(TARGET[1] * FOOT_ANCHOR_Y) - size[1], TARGET[1] - size[1]))
    canvas.paste(character, (left, top), character)
    return canvas


def chibi_heights(chibi_root: Path, manifest: dict) -> dict[tuple[str, int], float]:
    heights: dict[tuple[str, int], float] = {}
    for clip, definition in manifest["clips"].items():
        for index, frame in enumerate(definition["frames"]):
            box = char_box(Image.open(chibi_root / frame))
            heights[(clip, index)] = float(box[3] - box[1])
    return heights


def main() -> int:
    parser = argparse.ArgumentParser(description="Normalise every pack to one on-screen character size")
    parser.add_argument("--packs", default="standard,slender", help="comma separated pack ids")
    parser.add_argument("--model", default="isnet-anime")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    registry = json.loads((ROOT / "assets" / "pet-packs.json").read_text(encoding="utf-8"))
    chibi_entry = registry["packs"]["chibi"]
    chibi_manifest = json.loads((ROOT / "assets" / chibi_entry["manifest"]).read_text(encoding="utf-8"))
    chibi_root = ROOT / "assets" / chibi_entry["root"]
    template = chibi_heights(chibi_root, chibi_manifest)
    chibi_idle = template[("idle", 0)]
    print(f"chibi template: idle {chibi_idle:.0f}px across {len(template)} frames")

    session = None
    for pack in args.packs.split(","):
        pack = pack.strip()
        if not pack:
            continue
        entry = registry["packs"][pack]
        manifest = json.loads((ROOT / "assets" / entry["manifest"]).read_text(encoding="utf-8"))
        pack_root = ROOT / "assets" / entry["root"]

        planned: list[tuple[Path, Image.Image, tuple[int, int, int, int], float]] = []
        for clip, definition in manifest["clips"].items():
            for index, frame in enumerate(definition["frames"]):
                path = pack_root / frame
                if not path.exists():
                    continue
                key = (clip, index)
                if key not in template:
                    raise SystemExit(f"{pack}/{clip}[{index}] has no chibi template frame")
                image = Image.open(path).convert("RGBA")
                if is_plate(image):
                    if session is None:
                        session = new_matting_session(args.model)
                    image = cut_alpha(image, session)
                    print(f"  matted {frame}")
                box = char_box(image)
                current = float(box[3] - box[1])
                target = REFERENCE_HEIGHT * (template[key] / chibi_idle)
                # Never let a frame exceed the canvas budget the manifests assume.
                scale = min(
                    target / current,
                    (TARGET[1] * MAX_HEIGHT_RATIO) / current,
                    (TARGET[0] * MAX_WIDTH_RATIO) / float(box[2] - box[0]),
                )
                planned.append((path, image, box, scale))

        print(f"{pack}: {len(planned)} frames")
        for path, image, box, scale in planned:
            before = box[3] - box[1]
            after = round(before * scale)
            if args.dry_run:
                print(f"  {path.relative_to(pack_root)}: {before}px -> {after}px (x{scale:.3f})")
                continue
            fitted = place(image, box, scale)
            assert fitted.size == TARGET, f"{path.name}: expected {TARGET}, got {fitted.size}"
            assert fitted.mode == "RGBA", f"{path.name}: expected RGBA"
            assert fitted.split()[3].getextrema()[0] == 0, f"{path.name}: no transparent pixel"
            temp = path.with_suffix(".tmp.png")
            fitted.save(temp, "PNG")
            temp.replace(path)
        if not args.dry_run:
            print(f"  wrote {len(planned)} frames")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
