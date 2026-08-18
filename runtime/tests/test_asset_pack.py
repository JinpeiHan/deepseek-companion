import json
import tempfile
import unittest
from pathlib import Path

from runtime.asset_pack import load_pack_descriptor, normalise_pack_id


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


if __name__ == "__main__":
    unittest.main()
