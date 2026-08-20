import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path

from runtime.asset_pack import load_pack_descriptor
from runtime.helper import resolve_pack


def _write_bundle(root: Path, *, standard_manifest: bool) -> None:
    assets = root / "assets"
    (assets / "pet").mkdir(parents=True)
    (assets / "pet-packs.json").write_text(json.dumps({
        "formatVersion": 1,
        "defaultPack": "chibi",
        "packs": {
            "chibi": {"manifest": "pet-manifest.json", "root": "pet"},
            "standard": {"manifest": "pet-standard-manifest.json", "root": "pet-standard"},
        },
    }), encoding="utf-8")
    (assets / "pet-manifest.json").write_text(json.dumps({
        "formatVersion": 1,
        "maxFrameWidth": 238,
        "maxFrameHeight": 260,
        "clips": {"idle": {"frames": ["idle.png"], "frameMs": 180, "loop": True}},
        "stateMap": {"IDLE": "idle"},
        "workingActivityMap": {},
        "idleMicroClips": [],
    }), encoding="utf-8")
    if standard_manifest:
        (assets / "pet-standard-manifest.json").write_text(json.dumps({
            "formatVersion": 2,
            "maxFrameWidth": 512,
            "maxFrameHeight": 512,
            "logicalWidth": 260,
            "logicalHeight": 260,
            "clips": {"idle": {"frames": ["idle.png"], "frameMs": 180, "loop": True}},
            "stateMap": {"IDLE": "idle"},
            "workingActivityMap": {},
            "idleMicroClips": [],
        }), encoding="utf-8")


class PackFallbackTests(unittest.TestCase):
    def test_missing_pack_root_falls_back_to_chibi_with_one_warning(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _write_bundle(root, standard_manifest=False)
            stderr = io.StringIO()
            with redirect_stderr(stderr):
                descriptor = resolve_pack(root, "standard")
            self.assertEqual(descriptor.pack_id, "chibi")
            lines = stderr.getvalue().strip().splitlines()
            self.assertEqual(len(lines), 1)
            self.assertIn("proportion pack 'standard' unavailable", lines[0])
            self.assertIn("falling back to chibi", lines[0])

    def test_frame_loss_in_the_selected_pack_falls_back_wholesale(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _write_bundle(root, standard_manifest=True)

            def loader(bundle_root: Path, pack_id: str):
                descriptor = load_pack_descriptor(bundle_root, pack_id)
                if descriptor.pack_id != "chibi":
                    raise ValueError("1 frame(s) unreadable, first 'idle.png'")
                return descriptor

            stderr = io.StringIO()
            with redirect_stderr(stderr):
                descriptor = resolve_pack(root, "standard", loader=loader)
            self.assertEqual(descriptor.pack_id, "chibi")
            self.assertIn("unreadable", stderr.getvalue())

    def test_unknown_pack_id_never_warns(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _write_bundle(root, standard_manifest=False)
            stderr = io.StringIO()
            with redirect_stderr(stderr):
                descriptor = resolve_pack(root, "does-not-exist")
            self.assertEqual(descriptor.pack_id, "chibi")
            self.assertEqual(stderr.getvalue(), "")

    def test_broken_chibi_stays_fatal(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "assets").mkdir(parents=True)
            with self.assertRaises(OSError):
                resolve_pack(root, "chibi")

    def test_second_failure_after_fallback_is_not_swallowed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _write_bundle(root, standard_manifest=False)
            (root / "assets" / "pet-manifest.json").unlink()
            with redirect_stderr(io.StringIO()):
                with self.assertRaises(OSError):
                    resolve_pack(root, "standard")


if __name__ == "__main__":
    unittest.main()
