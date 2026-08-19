"""Render a rig pack in motion, offscreen, using the real runtime modules.

A still frame proves the layers composite; it proves nothing about whether the
rig actually animates. This drives `RigDriver` and `RigModel` exactly as
`helper.py` does -- same solver, same driver, same renderer -- and writes a PNG
sequence, so what you look at is the shipping code path rather than a mock.

`QT_QPA_PLATFORM` is forced to `offscreen` before PySide6 is imported, so this
needs no display server.

Motions are declarative so a preview is reproducible and reviewable:
    idle      breathing and the tail settling under its own spring
    blink     the idle-micro overlay
    look      the pointer circling the pet, head and eyes following
    poke      a poke impulse on the tail, decaying on its own
    drag      the window thrown left and right, hair and tail lagging

Run with the repo interpreter (needs PySide6):
    python3 scripts/render-rig-clip.py --pack standard-rig --motion look
"""

from __future__ import annotations

import argparse
import math
import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from PySide6.QtCore import QRectF, Qt  # noqa: E402
from PySide6.QtGui import QGuiApplication, QImage, QPainter  # noqa: E402

from runtime.asset_pack import load_pack_descriptor  # noqa: E402
from runtime.rig_driver import RigDriver  # noqa: E402
from runtime.rig_model import RigModel  # noqa: E402
from runtime.rig_pack import load_rig  # noqa: E402
from runtime.rig_renderer import RigRenderer  # noqa: E402

MOTIONS = ("idle", "blink", "look", "poke", "drag")


def drive(driver: RigDriver, motion: str, frame: int, total: int, now_ms: int) -> None:
    """Apply this motion's inputs for one frame, before the driver advances."""
    phase = frame / max(1, total)
    if motion == "look":
        angle = phase * 2.0 * math.pi
        driver.set_pointer(math.cos(angle), math.sin(angle) * 0.6, True, now_ms)
    elif motion == "drag":
        # Throw the window left, then right, then release two thirds through.
        if phase < 0.66:
            velocity = 900.0 * math.sin(phase * 3.0 * math.pi)
            driver.set_root_motion(velocity, 0.0, True)
        elif frame == int(total * 0.66):
            driver.set_root_motion(0.0, 0.0, False)
    elif motion == "poke" and frame == 6:
        for chain in ("tail",):
            driver.apply_impulse({"chain": chain, "chainAngularVel": 320.0})


def main() -> int:
    parser = argparse.ArgumentParser(description="Render a rig pack in motion, offscreen")
    parser.add_argument("--pack", required=True)
    parser.add_argument("--motion", default="idle", choices=MOTIONS)
    parser.add_argument("--frames", type=int, default=36)
    parser.add_argument("--fps", type=int, default=18)
    parser.add_argument("--scale", type=float, default=1.0)
    parser.add_argument("--out", required=True, help="directory for the PNG sequence")
    args = parser.parse_args()

    app = QGuiApplication.instance() or QGuiApplication([])  # noqa: F841

    descriptor = load_pack_descriptor(ROOT, args.pack)
    if descriptor.renderer != "rig":
        raise SystemExit(f"{args.pack} is a {descriptor.renderer} pack, not a rig")
    rig = load_rig(descriptor)
    model = RigModel(rig)
    driver = RigDriver(rig)
    renderer = RigRenderer(descriptor, rig)

    base_clip = rig["stateMap"]["IDLE"]
    overlay = "blink" if args.motion == "blink" else None

    left, top, right, bottom = renderer.pet_rect(args.scale)
    width, height = int(math.ceil(right - left)), int(math.ceil(bottom - top))
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    step_ms = int(round(1000 / args.fps))
    now_ms = 0
    for frame in range(args.frames):
        now_ms += step_ms
        # Re-trigger the one-shot overlay so a short preview shows the whole blink.
        driver.sync_model(base_clip, None, overlay if frame % 12 == 0 else None, now_ms)
        drive(driver, args.motion, frame, args.frames, now_ms)
        params = driver.advance(step_ms, now_ms)
        transforms = model.solve(params)

        image = QImage(width, height, QImage.Format.Format_RGBA8888_Premultiplied)
        image.fill(Qt.GlobalColor.transparent)
        painter = QPainter(image)
        try:
            renderer.paint(
                painter,
                transforms,
                anchor_x=-left,
                anchor_y=-top,
                scale=args.scale,
            )
        finally:
            painter.end()
        path = out_dir / f"{args.motion}_{frame:03d}.png"
        if not image.save(str(path), "PNG"):
            raise SystemExit(f"failed to write {path}")

    print(f"{args.pack}/{args.motion}: {args.frames} frames {width}x{height} -> {out_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
