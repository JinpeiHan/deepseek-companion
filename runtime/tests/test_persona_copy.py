import json
import tempfile
import unittest
from pathlib import Path

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


if __name__ == "__main__":
    unittest.main()
