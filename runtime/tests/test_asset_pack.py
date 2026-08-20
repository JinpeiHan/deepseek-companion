import json
import tempfile
import unittest
from pathlib import Path

from runtime.asset_pack import (
    PackDescriptor,
    load_pack_descriptor,
    load_pack_pixmaps,
    normalise_pack_id,
)


class FakePixmap:
    """Stands in for QPixmap so pack loading stays testable without Qt."""

    def __init__(self, path: str) -> None:
        self.path = path
        self._null = not Path(path).exists()

    def isNull(self) -> bool:
        return self._null


def _descriptor(asset_root: Path, frames: list[str], **manifest: object) -> PackDescriptor:
    payload = {
        "formatVersion": 1,
        "maxFrameWidth": 238,
        "maxFrameHeight": 260,
        "clips": {"idle": {"frames": frames, "frameMs": 180, "loop": True}},
        **manifest,
    }
    return PackDescriptor(
        "chibi", payload, asset_root, 238, 260, (0.5, 1.0), (0.5, 0.0),
    )


class AssetPackTests(unittest.TestCase):
    def test_pack_id_is_allowlisted(self) -> None:
        self.assertEqual(normalise_pack_id("standard"), "standard")
        self.assertEqual(normalise_pack_id("slender"), "slender")
        self.assertEqual(normalise_pack_id("unknown"), "chibi")
        self.assertEqual(normalise_pack_id(True), "chibi")

    def test_descriptor_uses_logical_metrics_and_confined_paths(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "assets" / "pet").mkdir(parents=True)
            (root / "assets" / "pet-packs.json").write_text(json.dumps({
                "formatVersion": 1,
                "defaultPack": "chibi",
                "packs": {"chibi": {"manifest": "pet-manifest.json", "root": "pet"}},
            }), encoding="utf-8")
            (root / "assets" / "pet-manifest.json").write_text(json.dumps({
                "formatVersion": 1,
                "maxFrameWidth": 238,
                "maxFrameHeight": 260,
                "clips": {"idle": {"frames": ["idle.png"], "frameMs": 180, "loop": True}},
                "stateMap": {"IDLE": "idle"},
                "workingActivityMap": {},
                "idleMicroClips": [],
            }), encoding="utf-8")
            descriptor = load_pack_descriptor(root, "chibi")
            self.assertEqual(descriptor.logical_width, 238)
            self.assertEqual(descriptor.logical_height, 260)
            self.assertEqual(descriptor.asset_root, root / "assets" / "pet")

    def test_pack_root_escaping_assets_root_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "assets").mkdir(parents=True)
            (root / "assets" / "pet-packs.json").write_text(json.dumps({
                "formatVersion": 1,
                "defaultPack": "chibi",
                "packs": {"chibi": {"manifest": "pet-manifest.json", "root": "../../outside"}},
            }), encoding="utf-8")
            with self.assertRaises(ValueError) as caught:
                load_pack_descriptor(root, "chibi")
            self.assertIn("escapes assets root", str(caught.exception))

    def test_renderer_defaults_to_frames_and_v3_requires_a_rig(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            assets = root / "assets"
            (assets / "pet").mkdir(parents=True)
            (assets / "pet-packs.json").write_text(json.dumps({
                "formatVersion": 1,
                "defaultPack": "chibi",
                "packs": {"chibi": {"manifest": "pet-manifest.json", "root": "pet"}},
            }), encoding="utf-8")
            manifest_path = assets / "pet-manifest.json"

            def write(**extra: object) -> None:
                manifest_path.write_text(json.dumps({
                    "formatVersion": 1,
                    "maxFrameWidth": 238,
                    "maxFrameHeight": 260,
                    "clips": {"idle": {"frames": ["idle.png"], "frameMs": 180, "loop": True}},
                    "stateMap": {"IDLE": "idle"},
                    **extra,
                }), encoding="utf-8")

            write()
            descriptor = load_pack_descriptor(root, "chibi")
            self.assertEqual(descriptor.renderer, "frames")
            self.assertIsNone(descriptor.rig_path)
            self.assertEqual(descriptor.logical_scale, 1.0)

            write(formatVersion=2, logicalWidth=260, logicalHeight=260, maxFrameWidth=512, maxFrameHeight=512)
            self.assertAlmostEqual(load_pack_descriptor(root, "chibi").logical_scale, 260 / 512)

            write(formatVersion=3, renderer="rig")
            rig = load_pack_descriptor(root, "chibi")
            self.assertEqual(rig.renderer, "rig")
            self.assertEqual(rig.rig_path, manifest_path.resolve())

            write(formatVersion=3)
            with self.assertRaises(ValueError):
                load_pack_descriptor(root, "chibi")

            write(formatVersion=4)
            with self.assertRaises(ValueError):
                load_pack_descriptor(root, "chibi")

    def test_strict_loading_raises_on_the_first_unreadable_frame(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "present.png").write_bytes(b"")
            descriptor = _descriptor(root, ["present.png", "gone.png"])
            with self.assertRaises(ValueError) as caught:
                load_pack_pixmaps(descriptor, FakePixmap)
            self.assertIn("unable to load frame", str(caught.exception))

    def test_non_strict_loading_reports_missing_frames(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "present.png").write_bytes(b"")
            descriptor = _descriptor(root, ["present.png", "gone.png", "present.png"])
            pixmaps, missing = load_pack_pixmaps(descriptor, FakePixmap, strict=False)
            self.assertEqual(sorted(pixmaps), ["present.png"])
            self.assertEqual(missing, ["gone.png"])

    def test_frames_escaping_the_pack_root_are_rejected_even_when_lenient(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "pack"
            root.mkdir()
            descriptor = _descriptor(root, ["../outside.png"])
            with self.assertRaises(ValueError) as caught:
                load_pack_pixmaps(descriptor, FakePixmap, strict=False)
            self.assertIn("escapes pack root", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
