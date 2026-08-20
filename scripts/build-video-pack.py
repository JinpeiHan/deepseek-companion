"""Assemble a frame-sequence pack from generated clips.

The slender proportion has no rig: layer extraction needs a hand-authored
annotations.json, and slender is a six-and-a-half-head skeleton that cannot
reuse the four-head or two-head geometry. So slender ships as what it already
is -- a frame pack -- with every action supplied by generated video instead of
by solved geometry.

This writes a *separate* pack rather than editing pet-slender-manifest.json.
That manifest is validated against a fixed file list, and folding baked clips
into it would make every generated frame an "unexpected PNG" and every
still-missing keyframe a failure. A separate pack keeps both contracts intact
and lets the two coexist while the keyframe pack is still incomplete.

Clip timing and looping come from the rig template, so the same action behaves
the same way whichever proportion the user picks.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent))

import rig_template as T  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
TARGET = (512, 512)


def main() -> int:
    parser = argparse.ArgumentParser(description="Build a frame-sequence pack from generated clips")
    parser.add_argument("--pack", default="slender-video", help="pack id to register")
    parser.add_argument("--root", default="pet-slender-video", help="asset directory under assets/")
    parser.add_argument("--character-id", default="whale-girl-slender-video")
    parser.add_argument("--source", required=True, help="directory holding <clip>/ frame folders")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    source_root = Path(args.source)
    clip_dirs = sorted(d for d in source_root.iterdir() if d.is_dir() and any(d.glob("*.png")))
    if not clip_dirs:
        raise SystemExit(f"no clip folders with PNGs under {source_root}")

    clips: dict[str, dict] = {}
    planned: list[tuple[Path, Path]] = []
    for clip_dir in clip_dirs:
        clip = clip_dir.name
        frames = sorted(clip_dir.glob("*.png"))
        for source in frames:
            with Image.open(source) as handle:
                if handle.size != TARGET:
                    raise SystemExit(f"{source}: expected {TARGET}, got {handle.size}")
        names = [f"{clip}/{clip}_{index:02d}.png" for index in range(len(frames))]
        template = T.CLIPS.get(clip, {})
        entry: dict = {
            "frames": names,
            "frameMs": 110,
            "loop": bool(template.get("loop", True)),
        }
        if template.get("motion"):
            entry["motion"] = template["motion"]
        clips[clip] = entry
        planned.extend(zip(frames, (Path(args.root) / name for name in names)))

    # Only route a state to a clip that exists, or the loader dies on startup.
    state_map = {state: clip for state, clip in T.STATE_MAP.items() if clip in clips}
    if "IDLE" not in state_map:
        raise SystemExit("no idle clip: the pack has nothing to fall back to")
    for state in T.STATE_MAP:
        state_map.setdefault(state, state_map["IDLE"])
    working = {k: v for k, v in T.WORKING_ACTIVITY_MAP.items() if v in clips}
    micro = [c for c in T.IDLE_MICRO_CLIPS if c in clips]

    manifest = {
        "formatVersion": 2,
        "characterId": args.character_id,
        "sourceWidth": TARGET[0],
        "sourceHeight": TARGET[1],
        "maxFrameWidth": TARGET[0],
        "maxFrameHeight": TARGET[1],
        "logicalWidth": 260,
        "logicalHeight": 260,
        "footAnchor": [0.5, 0.97],
        "bubbleAnchor": [0.5, 0.04],
        "clips": clips,
        "stateMap": state_map,
        "workingActivityMap": working,
        "idleMicroClips": micro,
    }

    print(f"{args.pack}: {len(clips)} clips, {len(planned)} frames")
    if args.dry_run:
        for clip in sorted(clips):
            print(f"  {clip}: {len(clips[clip]['frames'])} frames loop={clips[clip]['loop']}")
        return 0

    out_root = ROOT / "assets" / args.root
    if out_root.exists():
        shutil.rmtree(out_root)
    for source, relative in planned:
        target = ROOT / "assets" / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(source.read_bytes())

    manifest_name = f"pet-{args.pack}-manifest.json"
    manifest_path = ROOT / "assets" / manifest_name
    temp = manifest_path.with_suffix(".tmp.json")
    temp.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    temp.replace(manifest_path)

    registry_path = ROOT / "assets" / "pet-packs.json"
    registry = json.loads(registry_path.read_text(encoding="utf-8"))
    registry["packs"][args.pack] = {"manifest": manifest_name, "root": args.root}
    temp = registry_path.with_suffix(".tmp.json")
    temp.write_text(json.dumps(registry, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    temp.replace(registry_path)

    print(f"wrote assets/{manifest_name} and assets/{args.root}/ ({len(planned)} frames)")
    print(f"remember: runtime/asset_pack.py PACK_IDS must include '{args.pack}'")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
