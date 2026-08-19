"""Install a generated frame sequence into a rig pack as a baked clip.

A bone rig can only deform what its master contains. Sweeping needs a broom and
eating needs a token, and neither exists in the master, so those two actions
have nothing to deform -- rendered from the rig they read as swaying and
head-bobbing. They ship instead as real frames inside the rig pack, played
through the same AnimationModel, so state routing, overlay timing and the pack
switch are unchanged and only the paint path differs.

Frames arrive from MiniMax-H3, which drifts the camera: the character grew
noticeably across a 124-frame sweep even with the prompt asking for a constant
size. Normalising each frame's character height independently removes that drift
-- correct here precisely because the size change is an artefact rather than
part of the performance, which is the opposite of the frame packs, where one
scale is shared so that ducking still reads as ducking.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent))

ROOT = Path(__file__).resolve().parent.parent
TARGET = (512, 512)


def main() -> int:
    parser = argparse.ArgumentParser(description="Install a frame sequence into a rig pack as a baked clip")
    parser.add_argument("--pack", required=True, help="rig pack id, e.g. standard-rig")
    parser.add_argument("--clip", required=True, help="clip name in the rig manifest, e.g. working")
    parser.add_argument("--frames", required=True, help="directory of matted 512x512 RGBA frames")
    parser.add_argument("--frame-ms", type=int, default=110)
    parser.add_argument("--loop", action="store_true", default=True)
    parser.add_argument("--no-loop", dest="loop", action="store_false")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    registry = json.loads((ROOT / "assets" / "pet-packs.json").read_text(encoding="utf-8"))
    if args.pack not in registry["packs"]:
        raise SystemExit(f"{args.pack} is not registered in assets/pet-packs.json")
    entry = registry["packs"][args.pack]
    manifest_path = ROOT / "assets" / entry["manifest"]
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("renderer") != "rig":
        raise SystemExit(f"{args.pack} is not a rig pack")
    if args.clip not in manifest["clips"]:
        raise SystemExit(f"{args.clip} is not a clip in {entry['manifest']}")

    sources = sorted(Path(args.frames).glob("*.png"))
    if not sources:
        raise SystemExit(f"no PNG frames in {args.frames}")

    heights = []
    for source in sources:
        with Image.open(source) as handle:
            image = handle.convert("RGBA")
        if image.size != TARGET:
            raise SystemExit(f"{source.name}: expected {TARGET}, got {image.size}")
        box = image.split()[3].getbbox()
        if box is None:
            raise SystemExit(f"{source.name}: frame is fully transparent")
        heights.append(box[3] - box[1])
    spread = max(heights) - min(heights)
    # A baked clip that still drifts would make the pet visibly zoom mid-action.
    if spread > 12:
        raise SystemExit(
            f"character height varies by {spread}px across the clip; "
            "re-run the matting with --group frame to remove the camera drift"
        )

    rel_dir = f"baked/{args.clip}"
    names = [f"{rel_dir}/{args.clip}_{index:02d}.png" for index in range(len(sources))]
    if args.dry_run:
        print(f"{args.pack}/{args.clip}: {len(sources)} frames, height spread {spread}px")
        for name in names[:3]:
            print(f"  {name}")
        print("  …")
        return 0

    out_dir = ROOT / "assets" / entry["root"] / rel_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    for existing in out_dir.glob("*.png"):
        existing.unlink()
    for source, name in zip(sources, names):
        shutil.copy(source, ROOT / "assets" / entry["root"] / name)

    clip = manifest["clips"][args.clip]
    # Drop the oscillators: a clip is either solved or baked, never both, and
    # leaving them would imply the rig still drives this action.
    clip.pop("oscillators", None)
    clip["frames"] = names
    clip["frameMs"] = args.frame_ms
    clip["loop"] = bool(args.loop)

    temp = manifest_path.with_suffix(".tmp.json")
    temp.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    temp.replace(manifest_path)
    print(f"{args.pack}/{args.clip}: baked {len(names)} frames at {args.frame_ms}ms, spread {spread}px")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
