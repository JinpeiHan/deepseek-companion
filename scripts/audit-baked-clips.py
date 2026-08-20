"""Report the three defects that made the shipped clip set look wrong.

These checks existed only as throwaway snippets while the set was being fixed,
which is exactly why the problems survived so long: nothing in the repo could
tell you a clip was bad. Each measure here corresponds to a real defect found
in the shipped assets:

**motion** -- mean absolute difference between consecutive frames. The pipeline
used to pin MiniMax's last frame to its first so a loop clip would close, and
the model answered by barely moving: 11 clips came back under 2.0, two of them
were 16 copies of one frame. Generating with a free end and folding the result
back on itself (--free-motion --pingpong) is the fix.

**loop seam** -- difference between the last frame and the first. For a clip
declared loop=true this should be about one frame step; much larger means the
animation pops every cycle.

**rim spread** -- how much the colour of the silhouette edge varies across the
frames of one clip. The character and its outline are the same in every frame,
so this should be near zero. It was not, because MiniMax renders the head and
tail frames of a video on a different plate colour than the body, and matting
bakes whatever is behind the character into its anti-aliased edge. Decimating
from the plate-consistent run is the fix; see plate_consistent_run.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
from PIL import Image
from scipy import ndimage

ROOT = Path(__file__).resolve().parent.parent
# A clip below this barely moves; the regenerated ones land around 6.
MOTION_FLOOR = 2.0
# Grey levels of edge-colour variation within one clip before it reads as
# flickering between a dark rim and a light one.
RIM_SPREAD_MAX = 20.0


def rim_colour(path: Path) -> float | None:
    array = np.asarray(Image.open(path).convert("RGBA")).astype(int)
    mask = array[:, :, 3] > 128
    if mask.sum() < 200:
        return None
    edge = mask & ~ndimage.binary_erosion(mask, iterations=1)
    if edge.sum() < 50:
        return None
    return float(array[:, :, :3][edge].mean())


def audit_clip(directory: Path) -> dict | None:
    frames = sorted(directory.glob("*.png"))
    if len(frames) < 2:
        return None
    arrays = [np.asarray(Image.open(f).convert("RGBA")).astype(np.int16) for f in frames]
    steps = [float(np.abs(arrays[i] - arrays[i - 1]).mean()) for i in range(1, len(arrays))]
    rims = [r for r in (rim_colour(f) for f in frames) if r is not None]
    return {
        "frames": len(frames),
        "motion": float(np.mean(steps)),
        "seam": float(np.abs(arrays[-1] - arrays[0]).mean()),
        "rim_spread": (max(rims) - min(rims)) if len(rims) >= 4 else 0.0,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Audit baked clip quality across every rig pack")
    parser.add_argument("--packs", default="standard,chibi,slender")
    parser.add_argument("--json", help="also write the full table here")
    parser.add_argument("--strict", action="store_true",
                        help="exit non-zero if any clip trips a threshold")
    args = parser.parse_args()

    rows = []
    for pack in [p.strip() for p in args.packs.split(",") if p.strip()]:
        root = ROOT / "assets" / f"pet-{pack}-rig" / "baked"
        if not root.is_dir():
            raise SystemExit(f"no baked clips for pack {pack!r} at {root}")
        for directory in sorted(root.iterdir()):
            if not directory.is_dir():
                continue
            result = audit_clip(directory)
            if result is None:
                continue
            rows.append({"clip": f"{pack}/{directory.name}", **result})

    if not rows:
        raise SystemExit("no baked clips found")

    static = [r for r in rows if r["motion"] < MOTION_FLOOR]
    edgy = [r for r in rows if r["rim_spread"] > RIM_SPREAD_MAX]
    motions = np.array([r["motion"] for r in rows])
    spreads = np.array([r["rim_spread"] for r in rows])

    print(f"{len(rows)} baked clips\n")
    print(f"  motion      mean {motions.mean():6.2f}  median {np.median(motions):6.2f}  "
          f"min {motions.min():6.2f}")
    print(f"  rim spread  mean {spreads.mean():6.2f}  median {np.median(spreads):6.2f}  "
          f"max {spreads.max():6.2f}")
    print(f"\n  near-static (motion < {MOTION_FLOOR}): {len(static)}")
    for row in sorted(static, key=lambda r: r["motion"]):
        print(f"     {row['clip']:34s} {row['motion']:6.2f}")
    print(f"  inconsistent edges (rim spread > {RIM_SPREAD_MAX}): {len(edgy)}")
    for row in sorted(edgy, key=lambda r: -r["rim_spread"])[:10]:
        print(f"     {row['clip']:34s} {row['rim_spread']:6.1f}")

    if args.json:
        Path(args.json).write_text(json.dumps(rows, indent=1), encoding="utf-8")
        print(f"\nwrote {args.json}")

    if args.strict and (static or edgy):
        print(f"\n{len(static)} static and {len(edgy)} edge-inconsistent clip(s)")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
