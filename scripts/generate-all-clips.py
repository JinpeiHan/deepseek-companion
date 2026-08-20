"""Generate every rig clip for every proportion with the local MiniMax-H3.

Runs unattended: for each pack and clip it picks the best available first frame,
generates, decimates, mattes per frame and bakes the result into the rig pack.
Already-baked clips are skipped, so the run is resumable after an interruption.

Two details decide whether the output is usable.

**The first frame is chosen, not defaulted.** A clip whose pose already exists in
the shipped frame pack starts from that frame, so the model is animating the
approved pose rather than inventing one. Only clips with no matching frame fall
back to the idle master. chibi's frames are 195x260, far below what a video
model treats as a detailed reference, so they are upscaled first with the same
RealESRGAN pass used for the rig master.

**Drift is divided out, not normalised away.** H3 slides the camera during a
clip -- measured across 124 frames, the fitted trend is 3px on the standard
sweep but 38px on the chibi one. Normalising every frame independently would
remove that, and would also remove the crouch in `poke` and the feet-off-ground
lift in `dragging`, because height is not a constant of a good performance. So
frames are matted on a SHARED scale, which keeps both effects, and
detrend-clip.py then fits the slow component and divides only that out.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent))

from remove_image_background_lib import plate_colour_of, plate_consistent_run  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
ART_PYTHON = "/home/jinpei/anaconda3/envs/dsh-art/bin/python"

PACKS = {
    "standard-rig": {
        "frames": "assets/pet-standard",
        "suffix": "_512",
        "proportion": "a four-head-tall chibi anime maid girl with a slightly oversized head and short rounded limbs",
        "upscale": False,
    },
    "chibi-rig": {
        "frames": "assets/pet",
        "suffix": "_238",
        "proportion": "a super-deformed two-head-tall chibi anime maid girl with a hugely oversized head and a tiny body",
        "upscale": True,
    },
    # slender is played older and gentler on purpose: same character, further
    # along. The tone words sit in the proportion string because they have to
    # reach every clip -- a calm reading of "angry" is a different drawing, not
    # the same drawing at a different size.
    "slender-rig": {
        "frames": "assets/pet-slender",
        "suffix": "_512",
        "proportion": (
            "a six-and-a-half-head-tall slender young woman with long legs and a mature silhouette, "
            "an elegant and composed grown-up maid with a gentle, kind, softly smiling demeanour, "
            "calm and unhurried in every movement, poised rather than childish or exaggerated"
        ),
        "upscale": False,
        "emojiFrames": "slender",
        "tone": (
            "Play the action calmly and gracefully rather than energetically: smaller, slower, "
            "more restrained motion, a warm and gentle expression, no chibi-style exaggeration, "
            "no super-deformed proportions."
        ),
    },
}

IDENTITY = (
    "long blue gradient hair, a white frilled maid headdress, whale ear fins on both sides of her head, "
    "a navy maid dress with a white frilled apron bearing a small white whale emblem, white socks, "
    "navy Mary Jane shoes and a forked whale tail behind her"
)

# clip -> (source frame directory in the frame pack or None, action description)
CLIPS: dict[str, tuple[str | None, str]] = {
    "idle": ("idle_front", "stands quietly at rest, arms relaxed at her sides, breathing gently, hair and tail swaying a little"),
    "thinking": ("idle_think", "thinks, one hand near her chin, eyes drifting upward, head tilting slowly"),
    "working": ("sweep", "sweeps the floor with a broom, pushing it left and right in a steady rhythm, leaning into each stroke"),
    "working_search": ("idle_front", "walks in place looking around for something, head turning side to side, arms swinging"),
    "working_command": ("idle_front", "types busily on an invisible keyboard in front of her, hands moving quickly, focused expression"),
    "waiting": ("talk", "waits and speaks, one hand raised in a small gesture, mouth opening and closing"),
    "success": ("happy", "celebrates happily, raising both hands, eyes curving into a delighted smile, bouncing lightly"),
    "error": ("angry", "pouts angrily, hands on hips, cheeks puffed, eyebrows lowered"),
    "error_dizzy": ("dizzy", "is dizzy, swaying unsteadily, eyes spinning into spirals, body tilting"),
    "dragging": ("dragging", "is lifted into the air and dangles, arms raised, hair and skirt hanging down, feet off the ground, wriggling"),
    "blink": ("idle_blink", "stands still and blinks, both eyes closing together and opening again, repeatedly"),
    "glance": ("idle_front", "glances left and right, only her head and eyes turning, body still"),
    "head_pat": ("head_pat", "is being patted on the head, shoulders drawing up, eyes narrowing happily, no hand visible in frame"),
    "poke": ("poke_react", "reacts to being poked, flinching back with a surprised expression, then relaxing"),
    "tail": ("idle_front", "notices her whale tail being touched, twisting to look back at it while the tail swings"),
    "eat_token": (None, "happily nibbles a small glowing golden coin token held in both hands near her mouth, cheeks puffing, eyes squeezed shut with delight"),
}


# Emoji actions. Their first frame is a prepared sticker under
# art-references/emoji-first/, already matted and padded so the feet cannot be
# pushed out of frame, rather than a frame from the pack -- the pack has no
# pose for any of these.
EMOJI_CLIPS: dict[str, tuple[str, str]] = {
    "taunt_token_jab": ("taunt-token-jab", "jabs a finger at the viewer with a smug grin, taunting, other hand on her hip"),
    "smug_zako": ("smug-zako", "covers her mouth with one hand and smirks mockingly, leaning forward slightly"),
    "slack_off": ("slack-off", "slumps on the floor having given up, limbs sprawled, face blank"),
    "love_heart_hands": ("love-heart-hands", "makes a heart shape with both hands, eyes closed happily, hearts floating around her"),
    "dash_run": ("dash-run", "sprints forward at full speed, arms pumping, hair and tail streaming behind"),
    "sulk_pout": ("sulk-pout", "pouts and sulks, brow furrowed, arms drawn in close, looking away"),
    "soul_leaving": ("soul-leaving", "slumps exhausted as a little ghost drifts out of her mouth"),
    "salute_roger": ("salute-roger", "snaps a crisp salute, standing straight, bright confident expression"),
    "plead_kneel": ("plead-kneel", "kneels with both hands clasped, pleading upward with teary eyes"),
    "confused_question": ("confused-question", "tilts her head in confusion, finger to her chin, question marks above her"),
    "relax_armchair": ("relax-armchair", "sits back relaxed holding a warm mug, sipping contentedly"),
    "cry_wail": ("cry-wail", "wails with her eyes squeezed shut, tears streaming, arms down"),
    "idea_lightbulb": ("idea-lightbulb", "raises one finger as an idea strikes, eyes lighting up"),
}


def edge_report(frames: "list[Path]", samples: int = 12) -> "dict[str, int]":
    """How many sampled frames run off each edge of the raw generation.

    Checked on the *raw* frames, not the finished asset. Matting crops to the
    character's bounding box and re-centres it, so a clip whose arms left the
    top of frame comes out looking tidy with clear margin all round -- the crop
    is baked into the artwork, not into the canvas, and no amount of inspecting
    the asset can find it. standard/dragging shipped with the hands cut off
    exactly that way.

    The background is whatever the model chose -- measured over the shipped
    set, 39 clips came back on black and 41 on white -- so "is there ink at the
    edge" is decided against the frame's own corners rather than against an
    assumed black plate.
    """
    from PIL import Image
    import numpy as np

    counts = {"top": 0, "bottom": 0, "left": 0, "right": 0}
    step = max(1, len(frames) // samples)
    for path in frames[::step]:
        pixels = np.asarray(Image.open(path).convert("RGB")).astype(int)
        corners = np.concatenate([
            pixels[:10, :10].reshape(-1, 3), pixels[:10, -10:].reshape(-1, 3),
            pixels[-10:, :10].reshape(-1, 3), pixels[-10:, -10:].reshape(-1, 3),
        ])
        background = corners.mean(axis=0)
        ink = np.abs(pixels - background).sum(axis=2) > 90
        if ink[:4, :].sum() > 60: counts["top"] += 1
        if ink[-4:, :].sum() > 60: counts["bottom"] += 1
        if ink[:, :4].sum() > 60: counts["left"] += 1
        if ink[:, -4:].sum() > 60: counts["right"] += 1
    return counts


def run(cmd: list[str], **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=ROOT, text=True, capture_output=True, **kwargs)


def first_frame_for(pack: str, clip: str, work: Path) -> Path:
    meta = PACKS[pack]
    if clip in EMOJI_CLIPS:
        stem, _ = EMOJI_CLIPS[clip]
        # The stickers are drawn at roughly four-head proportions, and
        # image-to-video matches the proportion of the frame it is pinned to --
        # the prompt asking for six and a half heads does not override it. A
        # slender-specific redraw of each pose therefore has its own directory;
        # generating slender from the shared frames produced a clip whose every
        # frame measured 0.61 wide-to-tall against slender's own 0.55.
        emoji_dir = "emoji-first-slender" if meta.get("emojiFrames") == "slender" else "emoji-first"
        prepared = ROOT / "art-references" / emoji_dir / f"{stem}.png"
        if not prepared.exists():
            # RuntimeError, not SystemExit: SystemExit derives from
            # BaseException and slips past the per-clip guard, so one missing
            # file would abandon every clip after it.
            raise RuntimeError(f"no prepared first frame at {prepared}")
        return prepared
    directory, _ = CLIPS[clip]
    frames_root = ROOT / meta["frames"]
    candidate = None
    if directory:
        matches = sorted((frames_root / directory).glob("*.png")) if (frames_root / directory).exists() else []
        if matches:
            candidate = matches[0]
    if candidate is None:
        fallback = sorted((frames_root / "idle_front").glob("*.png"))
        if not fallback:
            raise RuntimeError(f"{pack}: no idle_front frame to fall back on")
        candidate = fallback[0]
    if not meta["upscale"]:
        return candidate
    upscaled = work / f"{pack}-{clip}-first.png"
    if not upscaled.exists():
        result = run([ART_PYTHON, "scripts/upscale-rig-master.py", "--input", str(candidate), "--output", str(upscaled)])
        if result.returncode != 0:
            raise RuntimeError(f"upscale failed: {result.stderr[-500:]}")
    return upscaled


def already_baked(pack: str, clip: str) -> bool:
    registry = json.loads((ROOT / "assets" / "pet-packs.json").read_text(encoding="utf-8"))
    entry = registry["packs"].get(pack)
    if entry is None:
        return False
    manifest = json.loads((ROOT / "assets" / entry["manifest"]).read_text(encoding="utf-8"))
    return bool(manifest.get("clips", {}).get(clip, {}).get("frames"))


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate and bake every rig clip for every proportion")
    parser.add_argument("--packs", default="standard-rig,chibi-rig")
    parser.add_argument("--clips", default="", help="comma separated subset; default is every clip")
    parser.add_argument("--length", type=int, default=124)
    parser.add_argument("--keep", type=int, default=16, help="frames kept after decimation")
    parser.add_argument("--frame-ms", type=int, default=110)
    parser.add_argument("--work", default="/tmp/petclip/batch")
    # Pinning the last frame to the first is how a loop clip was made to end
    # where it began, but MiniMax responds by barely moving: measured across the
    # shipped set, 11 clips came back under 2.0 mean inter-frame difference.
    # --free-motion drops the pinned end and lets the model act.
    # The seed is derived from the clip's position in the run so a whole batch
    # is reproducible. Retrying one clip that the edge guard rejected needs a
    # different draw, and moving it in the list would change every other seed.
    parser.add_argument("--seed-base", type=int, default=1000)
    parser.add_argument("--regenerate-video", action="store_true",
                        help="discard any cached raw video and sample a new one")
    parser.add_argument("--free-motion", action="store_true",
                        help="do not pin the last frame; let the model move")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    work = Path(args.work)
    work.mkdir(parents=True, exist_ok=True)
    all_clips = {**CLIPS, **{k: (None, v[1]) for k, v in EMOJI_CLIPS.items()}}
    wanted = [c.strip() for c in args.clips.split(",") if c.strip()] or list(all_clips)
    unknown = [c for c in wanted if c not in all_clips]
    if unknown:
        raise SystemExit(f"unknown clip(s): {', '.join(unknown)}")

    todo = []
    for pack in [p.strip() for p in args.packs.split(",") if p.strip()]:
        if pack not in PACKS:
            raise SystemExit(f"unknown pack {pack}")
        for clip in wanted:
            if not args.force and already_baked(pack, clip):
                print(f"skip {pack}/{clip} (already baked)")
                continue
            todo.append((pack, clip))

    print(f"{len(todo)} clip(s) to generate")
    if args.dry_run:
        for pack, clip in todo:
            print(f"  {pack}/{clip}")
        return 0

    failures: list[str] = []
    for index, (pack, clip) in enumerate(todo, start=1):
        meta = PACKS[pack]
        _, action = all_clips[clip]
        prompt = (
            f"{meta['proportion']} with {IDENTITY}. She {action}. "
            "Keep the exact head-to-body proportion of the reference. Full body, feet visible, "
            "character stays centred and the same size throughout, consistent character design, "
            "simple plain background. The character stays fully visible and fully opaque in "
            "every single frame: no fade to black, no dissolve, no cut away, no camera "
            "transition, and she never leaves the frame. Her whole body including both "
            "hands, the top of her hair and both feet stays inside the frame at all times, "
            "with clear margin on every side. Nothing but the character appears: no human "
            "hand, no arm, no prop held by anyone else, no text, no speech bubble, no "
            "border. " + meta.get("tone", "")
        )
        raw = work / f"{pack}-{clip}"
        small = work / f"{pack}-{clip}-16"
        rgba = work / f"{pack}-{clip}-rgba"
        print(f"[{index}/{len(todo)}] {pack}/{clip}", flush=True)
        try:
            first = first_frame_for(pack, clip, work)
            # Reusing a complete raw video is what makes a re-bake cheap, but it
            # silently ignores any flag that changes how the video is generated
            # -- --free-motion in particular would re-decimate the very clip it
            # was meant to replace. --regenerate-video forces a new sampling.
            have_video = raw.exists() and len(list(raw.glob("*.png"))) >= args.length
            if args.regenerate_video and have_video:
                shutil.rmtree(raw)
                have_video = False
            if not have_video:
                result = run([
                    sys.executable, "scripts/generate-video-clip.py", "--mode", "i2v",
                    "--first-frame", str(first),
                    *([] if args.free_motion else ["--last-frame", str(first)]),
                    "--prompt", prompt, "--length", str(args.length),
                    "--width", "512", "--height", "768", "--seed", str(args.seed_base + index),
                    "--prefix", f"petclip/{pack}-{clip}", "--out", str(raw),
                ])
                if result.returncode != 0:
                    raise RuntimeError(result.stdout[-500:] + result.stderr[-800:])

            frames = sorted(raw.glob("*.png"))
            if len(frames) < args.keep:
                raise RuntimeError(f"only {len(frames)} frames generated")
            edges = edge_report(frames)
            crossed = {side: n for side, n in edges.items() if n > 0}
            if crossed:
                raise RuntimeError(
                    f"the character leaves the frame ({crossed}); regenerate rather than "
                    "reprocess -- matting re-centres the crop and hides it"
                )
            small.mkdir(parents=True, exist_ok=True)
            for existing in small.glob("*.png"):
                existing.unlink()
            # Decimate from the plate-consistent interior, not the whole video.
            # The head and tail frames come back on a different plate colour and
            # their silhouette edges are contaminated with it; see
            # plate_consistent_run in remove_image_background_lib.
            plates = [plate_colour_of(Image.open(path)) for path in frames]
            start, stop = plate_consistent_run(plates)
            usable = frames[start:stop]
            if len(usable) < args.keep:
                raise RuntimeError(
                    f"only {len(usable)} of {len(frames)} frames share one plate colour"
                )
            if len(usable) < len(frames):
                print(f"  {pack}/{clip}: keeping frames {start}..{stop} "
                      f"({len(frames) - len(usable)} off-plate frames dropped)")
            step = len(usable) / args.keep
            order = [usable[int(slot * step)] for slot in range(args.keep)]
            for slot, source in enumerate(order):
                shutil.copy(source, small / f"{slot:02d}.png")

            shared = work / f"{pack}-{clip}-shared"
            result = run([sys.executable, "scripts/remove-image-background.py",
                          "--input", str(small), "--output", str(shared), "--group", "pack"])
            if result.returncode != 0:
                raise RuntimeError(result.stderr[-800:])
            result = run([sys.executable, "scripts/detrend-clip.py",
                          "--input", str(shared), "--output", str(rgba)])
            if result.returncode != 0:
                raise RuntimeError(result.stderr[-800:])
            print("   " + result.stdout.strip().splitlines()[0], flush=True)

            result = run([sys.executable, "scripts/bake-clip-into-rig.py",
                          "--pack", pack, "--clip", clip, "--frames", str(rgba),
                          "--frame-ms", str(args.frame_ms)])
            if result.returncode != 0:
                raise RuntimeError(result.stdout[-300:] + result.stderr[-800:])
            print("   " + result.stdout.strip(), flush=True)
        except Exception as error:  # noqa: BLE001
            print(f"   FAILED: {error}", flush=True)
            failures.append(f"{pack}/{clip}")

    print(f"\ndone: {len(todo) - len(failures)} baked, {len(failures)} failed")
    for name in failures:
        print(f"  failed: {name}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
