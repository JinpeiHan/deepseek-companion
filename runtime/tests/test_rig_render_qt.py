"""Offscreen Qt smoke tests for the rig renderer.

Everything here runs against a *synthetic* rig built in a temp directory rather
than a shipped pack: the real ``assets/pet-*-rig.json`` is produced by a
separate track, and a renderer test that can only run once the art lands is a
test that never catches a renderer regression. The fixture exercises the same
code paths the real rig will -- schema validation via
:func:`runtime.rig_pack.load_rig`, part-path confinement, strips, chains and
alpha masks -- with flat-coloured PNGs whose geometry is known exactly, which is
what makes the anchor assertions below mechanical rather than eyeballed.
"""

from __future__ import annotations

import json
import math
import os
import shutil
import struct
import tempfile
import unittest
import zlib
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6.QtCore import QRectF
    from PySide6.QtGui import QColor, QImage, QPainter, QPixmap
    from PySide6.QtWidgets import QApplication, QWidget

    HAVE_QT = True
except ImportError:  # pragma: no cover - environments without PySide6
    HAVE_QT = False

from runtime.rig_model import hit_test
from runtime.rig_pack import load_rig

if HAVE_QT:
    from runtime.rig_renderer import (
        MASK_ALPHA_THRESHOLD,
        SLOW_PAINT_WINDOW,
        RigRenderer,
        RigRendererError,
    )


APP = None


def setUpModule() -> None:
    """One QApplication for the whole module; Qt allows no second instance."""
    global APP
    if HAVE_QT:
        APP = QApplication.instance() or QApplication([])


# --------------------------------------------------------------------------- #
# Synthetic pack
# --------------------------------------------------------------------------- #

SOURCE = 512
LOGICAL = 260

#: (id, z, bone, rect, colour). Rects are laid out so no two parts overlap,
#: which is what lets a centroid hit test name a unique expected part.
PARTS = (
    ("tail", 5.0, "tail_0", (60.0, 300.0, 120.0, 140.0), "#3fa9f5"),
    ("body", 20.0, "body", (200.0, 260.0, 120.0, 200.0), "#f2c14e"),
    ("head", 40.0, "head", (200.0, 90.0, 130.0, 140.0), "#e5533d"),
)


def _png(width: int, height: int, rgba: tuple[int, int, int, int]) -> bytes:
    """Minimal opaque-rectangle PNG, so the fixture needs no image library."""
    raw = b"".join(
        b"\x00" + bytes(rgba) * width for _ in range(height)
    )

    def chunk(tag: bytes, payload: bytes) -> bytes:
        return (
            struct.pack(">I", len(payload))
            + tag
            + payload
            + struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF)
        )

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )


def build_rig() -> dict:
    parts = []
    for part_id, z, bone, rect, _ in PARTS:
        entry = {
            "id": part_id,
            "z": z,
            "bone": bone,
            "rect": list(rect),
            "file": f"parts/{part_id}.png",
        }
        if part_id == "tail":
            # Strips + a chain: the one part whose draw path differs.
            entry["strips"] = 6
            entry["stripBones"] = ["tail_0", "tail_1", "tail_2"]
            entry["pivot"] = [120.0, 300.0]
        parts.append(entry)

    return {
        "formatVersion": 3,
        "renderer": "rig",
        "maxFrameWidth": SOURCE,
        "maxFrameHeight": SOURCE,
        "logicalWidth": LOGICAL,
        "logicalHeight": LOGICAL,
        "footAnchor": [0.5, 0.87],
        "bubbleAnchor": [0.5, 0.04],
        "overflow": {"left": 40, "top": 30, "right": 40, "bottom": 20},
        "params": {
            "headAngleZ": {"min": -25.0, "max": 25.0, "default": 0.0},
            "breath": {"min": 0.0, "max": 1.0, "default": 0.0},
            "tailSwing": {"min": -1.0, "max": 1.0, "default": 0.0},
        },
        "bones": [
            {"id": "root", "pivot": [256.0, 460.0]},
            {"id": "body", "parent": "root", "pivot": [260.0, 440.0]},
            {"id": "head", "parent": "body", "pivot": [265.0, 230.0]},
            {"id": "tail_0", "parent": "body", "pivot": [180.0, 320.0], "chain": "tail", "chainIndex": 0},
            {"id": "tail_1", "parent": "tail_0", "pivot": [150.0, 370.0], "chain": "tail", "chainIndex": 1},
            {"id": "tail_2", "parent": "tail_1", "pivot": [120.0, 420.0], "chain": "tail", "chainIndex": 2},
        ],
        "parts": parts,
        "bindings": [
            {"param": "headAngleZ", "channel": "rotate", "bone": "head", "gain": 1.0},
            {"param": "breath", "channel": "scaleY", "bone": "body", "gain": 0.02, "bias": 1.0},
        ],
        "chains": {
            "tail": {
                "driver": "tailSwing",
                "amplitudeDeg": 9.0,
                "distribution": [1.0, 0.8, 0.6],
                "bones": ["tail_0", "tail_1", "tail_2"],
                "deform": "strips",
                "spring": {
                    "stiffness": 70.0,
                    "dampingRatio": 0.4,
                    "lagPerSegmentMs": 45.0,
                    "maxDeg": 14.0,
                },
            }
        },
        "clips": {
            "idle": {
                "loop": True,
                "oscillators": [
                    {"param": "breath", "wave": "sin", "periodMs": 4000, "amplitude": 0.5, "offset": 0.5},
                    {"param": "tailSwing", "wave": "sin", "periodMs": 5200, "amplitude": 0.6},
                ],
            },
            "poke": {
                "loop": False,
                "durationMs": 320,
                "envelope": {"attackMs": 60, "holdMs": 60, "decayMs": 200},
                "tracks": [
                    {
                        "param": "headAngleZ",
                        "blend": "add",
                        "keys": [[0, 0.0], [120, -12.0], [320, 0.0]],
                    }
                ],
            },
        },
        "stateMap": {"IDLE": "idle"},
        "hitGroups": {"head": ["head"], "tail": ["tail"], "body": ["body"]},
        "interactions": {
            "head": {"clip": "poke"},
            "tail": {"clip": "poke", "impulse": {"chain": "tail", "chainAngularVel": 240.0}},
        },
    }


class StubDescriptor:
    """Just the attributes ``load_rig`` and ``RigRenderer`` actually read."""

    renderer = "rig"

    def __init__(self, root: Path, rig: dict) -> None:
        self.pack_id = "synthetic"
        self.manifest = rig
        self.asset_root = root
        self.rig_path = root / "rig.json"
        self.foot_anchor = tuple(rig["footAnchor"])
        self.bubble_anchor = tuple(rig["bubbleAnchor"])
        self.logical_width = rig["logicalWidth"]
        self.logical_height = rig["logicalHeight"]

    @property
    def logical_scale(self) -> float:
        return self.logical_width / float(self.manifest["maxFrameWidth"])


def write_pack(root: Path, rig: dict) -> StubDescriptor:
    (root / "parts").mkdir(parents=True, exist_ok=True)
    for part_id, _, _, rect, colour in PARTS:
        colour_q = QColor(colour) if HAVE_QT else None
        rgba = (
            (colour_q.red(), colour_q.green(), colour_q.blue(), 255)
            if colour_q is not None
            else (255, 0, 0, 255)
        )
        (root / "parts" / f"{part_id}.png").write_bytes(
            _png(int(rect[2]), int(rect[3]), rgba)
        )
    (root / "rig.json").write_text(json.dumps(rig), encoding="utf-8")
    return StubDescriptor(root, rig)


@unittest.skipUnless(HAVE_QT, "PySide6 is not installed")
class RigRenderQtTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = Path(tempfile.mkdtemp(prefix="rig-render-"))
        cls.rig = build_rig()
        cls.descriptor = write_pack(cls.tmp, cls.rig)
        # Round-trips the fixture through the real loader, so a schema change
        # that would reject the shipped rig fails here first.
        cls.loaded = load_rig(cls.descriptor)

    @classmethod
    def tearDownClass(cls) -> None:
        shutil.rmtree(cls.tmp, ignore_errors=True)

    # -- helpers -------------------------------------------------------- #

    def make_renderer(self) -> RigRenderer:
        renderer = RigRenderer(self.descriptor, self.loaded)
        renderer.part_pixmaps()
        return renderer

    def solve(self, params: dict | None = None):
        from runtime.rig_model import RigModel

        return RigModel(self.loaded).solve(params or {})

    def render(self, scale: float, params: dict | None = None):
        """Paint into a widget-sized image and return (image, anchor, renderer).

        The widget geometry mirrors ``helper.py``: the logical pet box sits at
        the bottom of the window with the declared overflow reserved around it.
        """
        renderer = self.make_renderer()
        pet_w = round(LOGICAL * scale)
        pet_h = round(LOGICAL * scale)
        over = renderer.overflow_px(scale)
        pad = [max(base, math.ceil(value)) for base, value in zip((25, 18, 25, 8), over)]
        width = pet_w + pad[0] + pad[2]
        height = pet_h + pad[1] + pad[3]

        image = QImage(width, height, QImage.Format.Format_ARGB32_Premultiplied)
        image.fill(0)
        pet_x = pad[0]
        pet_y = height - pet_h - pad[3]
        foot_x, foot_y = renderer.foot_fraction
        anchor = (pet_x + foot_x * pet_w, pet_y + foot_y * pet_h)

        painter = QPainter(image)
        renderer.paint(
            painter,
            self.solve(params),
            anchor_x=anchor[0],
            anchor_y=anchor[1],
            scale=scale,
        )
        painter.end()
        return image, anchor, renderer

    @staticmethod
    def alpha_bbox(image: QImage):
        left, top = image.width(), image.height()
        right = bottom = -1
        for y in range(image.height()):
            for x in range(image.width()):
                if image.pixelColor(x, y).alpha() > 0:
                    left = min(left, x)
                    right = max(right, x)
                    top = min(top, y)
                    bottom = max(bottom, y)
        if right < 0:
            return None
        return left, top, right, bottom

    # -- tests ---------------------------------------------------------- #

    def test_renders_at_every_scale_without_exception(self) -> None:
        for scale in (0.7, 1.0, 1.4):
            with self.subTest(scale=scale):
                image, _, _ = self.render(scale)
                self.assertIsNotNone(
                    self.alpha_bbox(image), "nothing was drawn at all"
                )

    def test_alpha_bbox_stays_strictly_inside_the_widget(self) -> None:
        """The window reserves overflow, so no pose may touch its edge."""
        for scale in (0.7, 1.0, 1.4):
            for params in ({}, {"headAngleZ": 25.0, "breath": 1.0, "tailSwing": 1.0}):
                with self.subTest(scale=scale, params=tuple(sorted(params))):
                    image, _, _ = self.render(scale, params)
                    box = self.alpha_bbox(image)
                    self.assertIsNotNone(box)
                    left, top, right, bottom = box
                    self.assertGreater(left, 0)
                    self.assertGreater(top, 0)
                    self.assertLess(right, image.width() - 1)
                    self.assertLess(bottom, image.height() - 1)

    def test_foot_anchor_row_has_ink_at_every_scale(self) -> None:
        """The mechanised "stands in the same place" guard.

        The anchor is derived from the *rest* logical box, so a deformed pose
        must still put the character over its standing point. Checked against a
        deformed pose too -- that is the case a bbox-derived anchor would fail.
        """
        for scale in (0.7, 1.0, 1.4):
            for params in ({}, {"headAngleZ": 25.0, "tailSwing": 1.0}):
                with self.subTest(scale=scale, params=tuple(sorted(params))):
                    image, anchor, _ = self.render(scale, params)
                    row = int(round(anchor[1]))
                    self.assertTrue(0 <= row < image.height())
                    columns = [
                        x
                        for x in range(image.width())
                        if image.pixelColor(x, row).alpha() > 0
                    ]
                    self.assertTrue(columns, f"no ink on the foot row at {scale}")
                    nearest = min(abs(x - anchor[0]) for x in columns)
                    self.assertLessEqual(
                        nearest,
                        6.0,
                        f"foot row ink is {nearest:.1f}px from the anchor at {scale}",
                    )

    def test_hit_test_finds_a_part_centroid(self) -> None:
        masks = self.make_renderer().alpha_masks()
        transforms = self.solve()
        for part_id, _, _, rect, _ in PARTS:
            with self.subTest(part=part_id):
                cx = rect[0] + rect[2] / 2.0
                cy = rect[1] + rect[3] / 2.0
                self.assertEqual(hit_test(transforms, masks, cx, cy), part_id)

    def test_hit_test_follows_a_rotated_part(self) -> None:
        """Rotate the head 25 degrees; the click must follow the pixels."""
        masks = self.make_renderer().alpha_masks()
        transforms = self.solve({"headAngleZ": 25.0})
        head = next(t for t in transforms if t.part_id == "head")
        rect = next(p[3] for p in PARTS if p[0] == "head")
        centroid = (rect[0] + rect[2] / 2.0, rect[1] + rect[3] / 2.0)
        from runtime.rig_model import a_map

        moved = a_map(head.matrix, *centroid)
        # The head really did move, so this is not an accidental pass.
        self.assertGreater(math.dist(moved, centroid), 5.0)
        self.assertEqual(hit_test(transforms, masks, *moved), "head")

    def test_alpha_masks_are_non_empty_for_every_part(self) -> None:
        masks = self.make_renderer().alpha_masks()
        self.assertEqual(set(masks), {part[0] for part in PARTS})
        for part_id, mask in masks.items():
            with self.subTest(part=part_id):
                self.assertGreater(mask.width, 0)
                self.assertGreater(mask.height, 0)
                self.assertLessEqual(max(mask.width, mask.height), 128)
                self.assertGreater(
                    sum(bin(byte).count("1") for byte in mask.bits),
                    0,
                    "mask has no set bits",
                )
                # Fully opaque fixture parts must be fully covered.
                self.assertTrue(mask.covers(*_mask_centre(mask)))
        self.assertGreaterEqual(MASK_ALPHA_THRESHOLD, 1)

    def test_unreadable_part_is_a_pack_load_error(self) -> None:
        """A broken part must fail loading, not paint a hole."""
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            descriptor = write_pack(root, self.rig)
            (root / "parts" / "head.png").write_bytes(b"not a png")
            renderer = RigRenderer(descriptor, self.rig)
            with self.assertRaises(RigRendererError):
                renderer.part_pixmaps()
            # And it is a ValueError, which is what helper.py falls back on.
            self.assertTrue(issubclass(RigRendererError, ValueError))

    def test_prescale_cache_is_bucketed_and_rebuilt_only_on_change(self) -> None:
        renderer = self.make_renderer()
        renderer._ensure_scaled(0.7)
        first = dict(renderer._scaled)
        bucket = renderer._scaled_bucket
        renderer._ensure_scaled(0.7 + 1e-6)
        self.assertEqual(renderer._scaled_bucket, bucket)
        for part_id, pixmap in first.items():
            self.assertIs(renderer._scaled[part_id], pixmap)
        renderer._ensure_scaled(1.4)
        self.assertNotEqual(renderer._scaled_bucket, bucket)

    def test_degrades_to_a_slower_ticker_and_reports_once(self) -> None:
        renderer = self.make_renderer()
        self.assertEqual(renderer.tick_ms(False), 16)
        self.assertEqual(renderer.tick_ms(True), 33)
        for _ in range(SLOW_PAINT_WINDOW):
            renderer.record_paint_ms(14.0)
        self.assertTrue(renderer.degraded)
        self.assertEqual(renderer.tick_ms(False), 33)
        self.assertTrue(renderer.take_degradation_notice())
        self.assertFalse(renderer.take_degradation_notice())

    def test_fast_paints_never_degrade(self) -> None:
        renderer = self.make_renderer()
        for _ in range(SLOW_PAINT_WINDOW * 3):
            renderer.record_paint_ms(4.0)
        self.assertFalse(renderer.degraded)
        self.assertEqual(renderer.tick_ms(False), 16)

    def test_paint_leaves_the_painter_transform_untouched(self) -> None:
        """paintEvent draws the bubble around this call; it must not leak state."""
        renderer = self.make_renderer()
        image = QImage(400, 400, QImage.Format.Format_ARGB32_Premultiplied)
        image.fill(0)
        painter = QPainter(image)
        painter.translate(37.0, 11.0)
        painter.scale(1.3, 1.3)
        before = painter.transform()
        renderer.paint(
            painter, self.solve(), anchor_x=200.0, anchor_y=300.0, scale=1.0
        )
        after = painter.transform()
        painter.end()
        self.assertEqual(before, after)

    def test_paint_ignores_the_painters_incoming_transform(self) -> None:
        """The rig branch sets an absolute transform, so a leftover bubble
        translate cannot shift the pet."""
        renderer = self.make_renderer()
        plain = QImage(400, 400, QImage.Format.Format_ARGB32_Premultiplied)
        plain.fill(0)
        painter = QPainter(plain)
        renderer.paint(painter, self.solve(), anchor_x=200.0, anchor_y=300.0, scale=1.0)
        painter.end()

        shifted = QImage(400, 400, QImage.Format.Format_ARGB32_Premultiplied)
        shifted.fill(0)
        painter = QPainter(shifted)
        painter.translate(50.0, -20.0)
        painter.scale(2.0, 2.0)
        renderer.paint(painter, self.solve(), anchor_x=200.0, anchor_y=300.0, scale=1.0)
        painter.end()

        self.assertEqual(plain, shifted)

    def test_strip_part_draws_every_slice(self) -> None:
        """A chain-bent tail must cover at least as much as its flat draw."""
        from runtime.rig_model import RigModel

        model = RigModel(self.loaded)
        transforms = model._solve(
            {}, {("bone", "tail_1", "rotate"): 10.0, ("bone", "tail_2", "rotate"): 10.0}
        )
        tail = next(t for t in transforms if t.part_id == "tail")
        self.assertEqual(tail.strips, 6)
        self.assertEqual(len(tail.strip_matrices), 6)

        renderer = self.make_renderer()
        image = QImage(400, 500, QImage.Format.Format_ARGB32_Premultiplied)
        image.fill(0)
        painter = QPainter(image)
        renderer.paint(
            painter, [tail], anchor_x=200.0, anchor_y=420.0, scale=1.0
        )
        painter.end()
        painted = sum(
            1
            for y in range(image.height())
            for x in range(image.width())
            if image.pixelColor(x, y).alpha() > 0
        )
        self.assertGreater(painted, 0, "strip drawing produced no pixels")

    def test_widget_paint_event_path(self) -> None:
        """Sanity check that a real QWidget paint works, not just a QImage."""
        renderer = self.make_renderer()
        widget = QWidget()
        widget.resize(400, 400)
        pixmap = QPixmap(400, 400)
        pixmap.fill(QColor(0, 0, 0, 0))
        painter = QPainter(pixmap)
        painter.drawRect(QRectF(0, 0, 1, 1))
        renderer.paint(painter, self.solve(), anchor_x=200.0, anchor_y=340.0, scale=1.0)
        painter.end()
        self.assertGreater(renderer.last_paint_ms, 0.0)


def _mask_centre(mask) -> tuple[float, float]:
    x, y, w, h = mask.rect
    return (x + w / 2.0, y + h / 2.0)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
