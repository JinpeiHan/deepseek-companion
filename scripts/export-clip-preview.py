"""Export one pet clip as a shareable animated image (WebP / APNG / GIF).

Pillow does all three formats natively, which matters because ffmpeg, apngasm,
magick, gifski and gifsicle are all absent here and the art pipeline already has
to run under `py -3` on Windows. At 512x512 and <=24 frames a system binary buys
nothing.

Three format-specific traps decide whether the output is watchable:

1. GIF has 1-bit alpha, so the anti-aliased hair and skirt edges land on
   whatever the decoder puts behind them -- usually black, which reads as a
   dark halo. The fix is to composite every partially transparent pixel onto an
   opaque matte *before* quantising, and only then to cut a hard transparency
   key at alpha < 128.
2. Quantising each GIF frame on its own gives each frame its own palette, and
   the animation shimmers. One palette is built from a montage of all frames and
   every frame is mapped into it. `optimize=True` would renumber that palette and
   silently detach the reserved transparent index, so it stays off.
3. APNG defaults to dispose=NONE/blend=OVER, which leaves a smear behind a limb
   that moves out of a region. `disposal=1` (clear to background) plus
   `blend=0` (replace, do not composite) is the only combination that plays
   back frame-exactly.

WebP is the README default: true 8-bit alpha, and on this art it lands around
0.6x the APNG of the same clip (measured 741 KB vs 1.14 MB for standard/head_pat
at 512x512 -- flat cel shading compresses well in both, so do not expect the 3-6x
that photographic content gets). APNG is the fallback for viewers that will not
animate WebP; GIF exists only for maximum compatibility, and is the *smallest* of
the three here because 255 colours are plenty for this palette.

    python scripts/export-clip-preview.py --source clip:chibi/blink
    python scripts/export-clip-preview.py --source clip:standard/head_pat \
        --formats webp,gif --scale 0.5 --out docs/images

The `--source` scheme is deliberately open: `rig:<pack>/<motion>` will render
frames through scripts/render-rig-clip.py once the rig renderer lands, and
everything downstream of `load_frames()` stays the same.
"""

from __future__ import annotations

import argparse
import json
import math
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
PACK_REGISTRY = ROOT / "assets" / "pet-packs.json"
FORMATS = ("webp", "apng", "gif")
DEFAULT_BUDGETS = {"gif": 2_000_000, "webp": 800_000, "apng": 3_000_000}
# Below this the pixel is keyed out of the GIF entirely; above it, it is opaque
# matte-composited colour. 128 keeps soft edges attached to the silhouette.
GIF_ALPHA_KEY = 128
# GIF palette index reserved for transparency. Frames may only use 0..254.
GIF_TRANSPARENT_INDEX = 255


@dataclass(frozen=True)
class ClipPlan:
    """Everything needed to write the outputs, resolved before any file is read."""

    source: str
    pack: str
    clip: str
    frame_paths: tuple[Path, ...]
    frame_ms: int
    loop: bool
    durations: tuple[int, ...]
    scale: float


def parse_budgets(raw: str) -> dict[str, int]:
    budgets = dict(DEFAULT_BUDGETS)
    for item in (chunk.strip() for chunk in raw.split(",") if chunk.strip()):
        key, _, value = item.partition("=")
        if key not in FORMATS or not value.isdigit():
            raise SystemExit(f"--max-bytes: expected <format>=<bytes> from {FORMATS}, got {item!r}")
        budgets[key] = int(value)
    return budgets


def parse_matte(raw: str) -> tuple[int, int, int]:
    text = raw.strip().lstrip("#")
    if len(text) == 3:
        text = "".join(ch * 2 for ch in text)
    if len(text) != 6:
        raise SystemExit(f"--gif-matte: expected #rrggbb, got {raw!r}")
    try:
        return tuple(int(text[i : i + 2], 16) for i in (0, 2, 4))  # type: ignore[return-value]
    except ValueError as error:
        raise SystemExit(f"--gif-matte: expected #rrggbb, got {raw!r}") from error


def resolve_clip_source(reference: str) -> tuple[str, str, tuple[Path, ...], int, bool]:
    """Resolve `<pack>/<clip>` against assets/pet-packs.json and its manifest."""
    pack_id, _, clip_id = reference.partition("/")
    if not pack_id or not clip_id:
        raise SystemExit(f"--source clip: expected <pack>/<clip>, got {reference!r}")
    registry = json.loads(PACK_REGISTRY.read_text(encoding="utf-8"))
    packs = registry["packs"]
    if pack_id not in packs:
        raise SystemExit(f"unknown pack {pack_id!r}; known packs: {', '.join(sorted(packs))}")
    manifest_path = ROOT / "assets" / packs[pack_id]["manifest"]
    pack_root = ROOT / "assets" / packs[pack_id]["root"]
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    clips = manifest["clips"]
    if clip_id not in clips:
        raise SystemExit(f"{pack_id}: unknown clip {clip_id!r}; known clips: {', '.join(sorted(clips))}")
    clip = clips[clip_id]
    paths = []
    for frame in clip["frames"]:
        path = (pack_root / frame).resolve()
        # A manifest is data, so treat it as untrusted: never read outside the pack.
        if not path.is_relative_to(pack_root.resolve()):
            raise SystemExit(f"{pack_id}/{clip_id}: frame {frame!r} escapes the pack root")
        if not path.is_file():
            raise SystemExit(f"{pack_id}/{clip_id}: missing frame {path}")
        paths.append(path)
    return pack_id, clip_id, tuple(paths), int(clip["frameMs"]), bool(clip["loop"])


def build_plan(args: argparse.Namespace) -> ClipPlan:
    scheme, _, reference = args.source.partition(":")
    if scheme == "rig":
        raise SystemExit(
            "--source rig: not implemented yet; it will render frames via "
            "scripts/render-rig-clip.py once the rig renderer lands"
        )
    if scheme != "clip":
        raise SystemExit(f"--source: expected clip:<pack>/<clip>, got {args.source!r}")

    pack, clip, paths, manifest_ms, loop = resolve_clip_source(reference)
    if args.fps is not None:
        frame_ms = max(1, round(1000 / args.fps))
    elif args.frame_ms is not None:
        frame_ms = args.frame_ms
    else:
        frame_ms = manifest_ms

    durations = [frame_ms] * len(paths)
    # A hold on a looping clip stutters the cycle, so it only applies to
    # one-shot clips, where it gives the reader a beat on the final pose.
    if args.hold_last_ms > 0 and not loop:
        durations[-1] += args.hold_last_ms
    return ClipPlan(
        source=args.source,
        pack=pack,
        clip=clip,
        frame_paths=paths,
        frame_ms=frame_ms,
        loop=loop,
        durations=tuple(durations),
        scale=args.scale,
    )


def load_frames(plan: ClipPlan) -> list[Image.Image]:
    frames = []
    for path in plan.frame_paths:
        with Image.open(path) as handle:
            frame = handle.convert("RGBA")
        if plan.scale != 1.0:
            size = (max(1, round(frame.width * plan.scale)), max(1, round(frame.height * plan.scale)))
            frame = frame.resize(size, Image.Resampling.LANCZOS)
        frames.append(frame)
    size = frames[0].size
    for path, frame in zip(plan.frame_paths, frames):
        assert frame.size == size, f"{path.name}: frame size {frame.size} != {size}"
        assert frame.mode == "RGBA", f"{path.name}: expected RGBA, got {frame.mode}"
    return frames


def matte_composite(frame: Image.Image, matte: tuple[int, int, int]) -> np.ndarray:
    """Return HxWx3 uint8 with every non-opaque pixel blended onto `matte`.

    Fully transparent pixels come out as the matte colour itself, so they cost
    the palette one entry instead of scattering undefined RGB (usually black)
    through the quantiser.
    """
    pixels = np.asarray(frame, dtype=np.uint16)
    alpha = pixels[:, :, 3:4]
    rgb = pixels[:, :, :3]
    plate = np.asarray(matte, dtype=np.uint16).reshape(1, 1, 3)
    blended = (rgb * alpha + plate * (255 - alpha) + 127) // 255
    return np.where(alpha == 255, rgb, blended).astype(np.uint8)


def build_gif_frames(
    frames: list[Image.Image], matte: tuple[int, int, int], colors: int
) -> list[Image.Image]:
    composited = [matte_composite(frame, matte) for frame in frames]
    # One palette for the whole clip: quantise a montage of every frame, then map
    # each frame into that result. Per-frame palettes make the animation shimmer.
    montage = Image.fromarray(np.concatenate(composited, axis=0), mode="RGB")
    master = montage.quantize(colors=colors, method=Image.Quantize.MEDIANCUT, dither=Image.Dither.NONE)
    palette = master.getpalette()[: colors * 3]
    palette = palette + [0] * (GIF_TRANSPARENT_INDEX * 3 - len(palette)) + list(matte)
    assert len(palette) == 256 * 3, f"palette has {len(palette) // 3} entries, expected 256"

    out = []
    for frame, plate in zip(frames, composited):
        mapped = Image.fromarray(plate, mode="RGB").quantize(palette=master, dither=Image.Dither.NONE)
        indices = np.asarray(mapped, dtype=np.uint8)
        assert indices.max() < GIF_TRANSPARENT_INDEX, "quantiser used the reserved transparent index"
        keyed = np.where(np.asarray(frame)[:, :, 3] < GIF_ALPHA_KEY, GIF_TRANSPARENT_INDEX, indices)
        indexed = Image.fromarray(keyed.astype(np.uint8), mode="P")
        indexed.putpalette(palette)
        out.append(indexed)
    return out


def atomic_save(image: Image.Image, destination: Path, **options) -> int:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(".tmp" + destination.suffix)
    image.save(temporary, **options)
    temporary.replace(destination)
    return destination.stat().st_size


def write_webp(frames: list[Image.Image], destination: Path, durations, loop: int) -> int:
    return atomic_save(
        frames[0],
        destination,
        format="WEBP",
        save_all=True,
        append_images=frames[1:],
        duration=list(durations),
        loop=loop,
        lossless=True,
        quality=100,
        method=6,
        minimize_size=True,
        allow_mixed=False,
    )


def write_apng(frames: list[Image.Image], destination: Path, durations, loop: int) -> int:
    return atomic_save(
        frames[0],
        destination,
        format="PNG",
        save_all=True,
        append_images=frames[1:],
        duration=list(durations),
        loop=loop,
        disposal=1,
        blend=0,
    )


def write_gif(frames: list[Image.Image], destination: Path, durations, loop: int) -> int:
    return atomic_save(
        frames[0],
        destination,
        format="GIF",
        save_all=True,
        append_images=frames[1:],
        duration=list(durations),
        loop=loop,
        disposal=2,
        transparency=GIF_TRANSPARENT_INDEX,
        optimize=False,
    )


def coalesced_frame_count(frames: list[Image.Image]) -> int:
    """How many frames survive an encoder that merges identical neighbours.

    The GIF and WebP encoders drop a frame that is pixel-identical to the one
    before it and add its delay to the predecessor instead. `idle_glance` has two
    such pairs, so a flat `n_frames == len(frames)` check fails on a file that is
    in fact correct. The playback timeline is what must not change.
    """
    runs = 1
    for previous, frame in zip(frames, frames[1:]):
        if frame.tobytes() != previous.tobytes():
            runs += 1
    return runs


def verify(
    destination: Path, kind: str, expected_frames: int, min_frames: int, total_ms: int, budget: int
) -> int:
    """Re-open the written file and assert the properties the format can lose.

    Returns the number of frames the encoder actually stored.
    """
    size = destination.stat().st_size
    with Image.open(destination) as handle:
        stored = handle.n_frames
        assert min_frames <= handle.n_frames <= expected_frames, (
            f"{destination.name}: {handle.n_frames} frames written, expected {expected_frames} "
            f"({min_frames} after merging identical neighbours)"
        )
        # Frame 0 first: seeking through a GIF replaces `info` with the current
        # frame's, and only the first frame carries the global transparency key.
        if kind == "gif":
            assert handle.info.get("transparency") == GIF_TRANSPARENT_INDEX, (
                f"{destination.name}: transparency index lost (info={handle.info.get('transparency')!r})"
            )
        else:
            alpha = np.asarray(handle.convert("RGBA"))[:, :, 3]
            soft = int(np.count_nonzero((alpha > 0) & (alpha < 255)))
            assert soft > 0, f"{destination.name}: frame 0 has no partial alpha, 8-bit alpha was flattened"
        # WebP is excluded: Pillow 11.1 decodes animated WebP without exposing
        # per-frame durations, so there is nothing to read back.
        if kind != "webp":
            played = 0
            for index in range(handle.n_frames):
                handle.seek(index)
                played += int(handle.info.get("duration", 0))
            # GIF stores delays in centiseconds, so a 135ms frame plays as 130ms.
            slack = 10 * stored if kind == "gif" else 0
            assert abs(played - total_ms) <= slack, (
                f"{destination.name}: timeline {played}ms, expected {total_ms}ms (+/-{slack}ms)"
            )
    if size > budget:
        # Delete rather than leave it: an over-budget preview that survives on
        # disk is exactly the file that gets committed by accident.
        destination.unlink()
        raise SystemExit(
            f"{destination.name}: {size} bytes exceeds the {kind} budget of {budget} bytes "
            f"(file removed); lower --scale or export fewer frames rather than raising the budget"
        )
    return stored


WRITERS = {"webp": write_webp, "apng": write_apng, "gif": write_gif}
SUFFIXES = {"webp": ".webp", "apng": ".png", "gif": ".gif"}


def main() -> int:
    parser = argparse.ArgumentParser(description="Export a pet clip as WebP/APNG/GIF")
    parser.add_argument("--source", required=True, help="clip:<pack>/<clip> (rig:<pack>/<motion> reserved)")
    parser.add_argument("--formats", default="webp,apng,gif", help="comma separated subset of webp,apng,gif")
    parser.add_argument("--out", default="docs/images", help="output directory (default: docs/images)")
    parser.add_argument("--fps", type=float, default=None, help="override timing as frames per second")
    parser.add_argument("--frame-ms", type=int, default=None, help="override timing as milliseconds per frame")
    parser.add_argument("--scale", type=float, default=1.0, help="resample factor applied to every frame")
    parser.add_argument("--loop", type=int, default=0, help="loop count, 0 means forever (default: 0)")
    parser.add_argument(
        "--hold-last-ms",
        type=int,
        default=400,
        help="extra time on the final frame of a one-shot clip (default: 400)",
    )
    parser.add_argument("--gif-matte", default="#ffffff", help="colour behind semi-transparent GIF pixels")
    parser.add_argument("--gif-colors", type=int, default=255, help="palette entries before the transparent one")
    parser.add_argument(
        "--max-bytes",
        default="",
        help="per-format byte budgets, e.g. gif=2000000,webp=800000,apng=3000000",
    )
    parser.add_argument("--dry-run", action="store_true", help="resolve and print the plan without writing")
    args = parser.parse_args()

    formats = [item.strip() for item in args.formats.split(",") if item.strip()]
    for name in formats:
        if name not in FORMATS:
            raise SystemExit(f"--formats: unknown format {name!r}, expected a subset of {FORMATS}")
    if not formats:
        raise SystemExit("--formats: nothing to export")
    if args.fps is not None and args.frame_ms is not None:
        raise SystemExit("--fps and --frame-ms are mutually exclusive")
    if args.fps is not None and args.fps <= 0:
        raise SystemExit("--fps must be positive")
    if args.scale <= 0:
        raise SystemExit("--scale must be positive")
    if not 2 <= args.gif_colors <= 255:
        raise SystemExit("--gif-colors must be between 2 and 255 (index 255 is reserved for transparency)")

    budgets = parse_budgets(args.max_bytes)
    matte = parse_matte(args.gif_matte)
    plan = build_plan(args)
    out_dir = Path(args.out) if Path(args.out).is_absolute() else ROOT / args.out
    stem = f"{plan.pack}-{plan.clip}"
    total_ms = sum(plan.durations)

    print(f"source {plan.source}: {len(plan.frame_paths)} frames, {plan.frame_ms}ms/frame, loop={plan.loop}")
    print(f"timeline {total_ms}ms total, scale {plan.scale:g}, matte #{'%02x%02x%02x' % matte}")
    for name in formats:
        target = out_dir / (stem + SUFFIXES[name])
        shown = target.relative_to(ROOT) if out_dir.is_relative_to(ROOT) else target
        print(f"  -> {shown} (budget {budgets[name]} bytes)")
    if args.dry_run:
        print("dry run: nothing written")
        return 0

    frames = load_frames(plan)
    gif_frames = build_gif_frames(frames, matte, args.gif_colors) if "gif" in formats else []
    width, height = frames[0].size

    for name in formats:
        destination = out_dir / (stem + SUFFIXES[name])
        payload = gif_frames if name == "gif" else frames
        # Counted on the payload, not the source frames: quantisation can make
        # two nearly identical RGBA frames byte-identical in the GIF palette.
        size = WRITERS[name](payload, destination, plan.durations, args.loop)
        stored = verify(
            destination, name, len(frames), coalesced_frame_count(payload), total_ms, budgets[name]
        )
        merged = "" if stored == len(frames) else f" (from {len(frames)}, identical neighbours merged)"
        print(f"{name}: {destination} {width}x{height} {stored} frames{merged} {size} bytes "
              f"({size / 1024:.1f} KiB, {math.ceil(size / stored)} B/frame)")

    print(f"exported {len(formats)} file(s) for {plan.source}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
