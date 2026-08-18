import contextlib
import importlib.util
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from runtime import helper
from runtime.persona_copy import interaction_copy, load_persona_copy


class PersonaCopyTests(unittest.TestCase):
    def test_loads_character_and_deterministic_interactions(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "copy.json"
            path.write_text(json.dumps({
                "characterName": "小鲸鱼",
                "status": {"idle": ["待命"]},
                "interaction": {
                    "headPat": ["摸摸头"],
                    "tail": ["摇尾巴"],
                    "poke": ["第一句", "第二句"],
                    "doubleClick": ["双击啦"],
                },
            }, ensure_ascii=False), encoding="utf-8")
            copy = load_persona_copy(path)
            self.assertEqual(copy["characterName"], "小鲸鱼")
            self.assertEqual(interaction_copy(copy, "poke", 1), "第二句")
            self.assertEqual(interaction_copy(copy, "poke", 1), "第二句")

    def test_rejects_missing_interaction_group(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "copy.json"
            path.write_text('{"characterName":"小鲸鱼","status":{},"interaction":{}}', encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "headPat"):
                load_persona_copy(path)


class _ClosedFlagRecorder:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


@unittest.skipUnless(importlib.util.find_spec("PySide6"), "PySide6 is required for visual helper mode")
class HelperPersonaCopyTests(unittest.TestCase):
    def test_run_visual_persona_copy_load_error_returns_2_closes_recorder_and_prints_stderr(self) -> None:
        for error in (ValueError("broken persona copy"), OSError("missing persona copy")):
            with self.subTest(error=type(error).__name__):
                recorder = _ClosedFlagRecorder()
                stderr = io.StringIO()
                with mock.patch.object(helper, "load_persona_copy", side_effect=error):
                    with contextlib.redirect_stderr(stderr):
                        code = helper.run_visual(recorder)
                self.assertEqual(code, 2)
                self.assertTrue(recorder.closed)
                self.assertIn("Unable to load whale persona copy:", stderr.getvalue())
                self.assertIn(str(error), stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
