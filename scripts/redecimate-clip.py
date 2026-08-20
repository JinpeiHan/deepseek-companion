"""Re-pick the kept frames of an already-generated clip, avoiding off-plate frames.

The clips in this repo were decimated uniformly across the whole 124-frame
video, which reliably picked up the head and tail frames. Those come back on a
different plate colour than the body of the clip -- usually pure black where the
rest is white -- and matting bakes that plate into the silhouette edge, so the
finished animation flickers between a dark rim and a light one.

The full-resolution frames are still on disk, so this is a re-selection, not a
regeneration: no GPU, no model, no new pixels. It rewrites the decimated
directory in place, after which the normal matte -> detrend -> bake chain runs
unchanged.
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent))

from remove_image_background_lib import plate_colour_of, plate_consistent_run  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Re-pick clip frames from the plate-consistent run")
    parser.add_argument("--raw", required=True, help="directory of full-resolution generated frames")
    parser.add_argument("--output", required=True, help="decimated directory to rewrite")
    parser.add_argument("--keep", type=int, default=16)
    # Plate consistency is measurable; a stray human hand reaching into the
    # frame, or the model drawing "???" over the character's head, is not. Those
    # occupy a contiguous stretch of the video, so the window is narrowed by
    # hand for the handful of clips that need it rather than by a heuristic that
    # would fire on the character's own props.
    parser.add_argument("--from", dest="start", type=int, help="earliest raw frame to consider")
    parser.add_argument("--to", dest="stop", type=int, help="latest raw frame to consider (exclusive)")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    frames = sorted(Path(args.raw).glob("*.png"))
    offset = 0
    if args.start is not None or args.stop is not None:
        offset = args.start or 0
        frames = frames[offset : args.stop if args.stop is not None else len(frames)]
        print(f"{Path(args.raw).name}: manual window {offset}..{offset + len(frames)}")
    if len(frames) < args.keep:
        raise SystemExit(f"{args.raw}: only {len(frames)} frames, need {args.keep}")

    plates = [plate_colour_of(Image.open(path)) for path in frames]
    start, stop = plate_consistent_run(plates)
    usable = frames[start:stop]
    dropped = len(frames) - len(usable)
    if len(usable) < args.keep:
        raise SystemExit(
            f"{args.raw}: only {len(usable)} of {len(frames)} frames share one plate colour"
        )

    print(f"{Path(args.raw).name}: plate run {offset + start}..{offset + stop}, "
          f"{dropped} off-plate frames dropped")
    if args.dry_run:
        return 0

    out = Path(args.output)
    out.mkdir(parents=True, exist_ok=True)
    for existing in out.glob("*.png"):
        existing.unlink()
    step = len(usable) / args.keep
    order = [usable[int(slot * step)] for slot in range(args.keep)]
    for slot, source in enumerate(order):
        shutil.copy(source, out / f"{slot:02d}.png")

    written = sorted(out.glob("*.png"))
    assert len(written) == args.keep, f"wrote {len(written)} frames, expected {args.keep}"
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
