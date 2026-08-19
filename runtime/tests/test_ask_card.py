"""The approval card: what it accepts, and what a click on it resolves to."""

from __future__ import annotations

import unittest

from runtime.helper import ask_from_message, ask_hit


class AskFromMessageTests(unittest.TestCase):
    def _message(self, **overrides):
        message = {
            "kind": "ask",
            "id": "approval-1",
            "question": "是否执行数据库迁移？",
            "options": [
                {"value": "yes", "label": "执行迁移"},
                {"value": "no", "label": "取消"},
            ],
        }
        message.update(overrides)
        return message

    def test_accepts_a_well_formed_ask(self):
        ask = ask_from_message(self._message())
        self.assertIsNotNone(ask)
        self.assertEqual(ask["id"], "approval-1")
        self.assertEqual([o["value"] for o in ask["options"]], ["yes", "no"])

    def test_rejects_an_ask_with_no_answerable_option(self):
        # Showing this would take over the bubble and leave no way out of it.
        self.assertIsNone(ask_from_message(self._message(options=[])))
        self.assertIsNone(ask_from_message(self._message(options=[{"label": "no value"}])))
        self.assertIsNone(ask_from_message(self._message(options=[{"value": "v", "label": "  "}])))

    def test_rejects_an_ask_with_nothing_to_answer_against(self):
        self.assertIsNone(ask_from_message(self._message(id="")))
        self.assertIsNone(ask_from_message(self._message(question="   ")))

    def test_caps_the_option_count(self):
        many = [{"value": str(i), "label": f"option {i}"} for i in range(9)]
        ask = ask_from_message(self._message(options=many))
        self.assertEqual(len(ask["options"]), 4, "a speech bubble is not a dialog")


class AskHitTests(unittest.TestCase):
    RECTS = [
        (20, 40, 200, 28, "yes"),
        (20, 74, 200, 28, "dry"),
        (20, 108, 200, 28, "no"),
    ]

    def test_a_click_inside_a_row_resolves_to_its_value(self):
        self.assertEqual(ask_hit(self.RECTS, 100, 50), "yes")
        self.assertEqual(ask_hit(self.RECTS, 100, 85), "dry")
        self.assertEqual(ask_hit(self.RECTS, 100, 120), "no")

    def test_a_click_in_the_gap_between_rows_answers_nothing(self):
        # Falling through to a drag is correct here; guessing an answer is not.
        self.assertIsNone(ask_hit(self.RECTS, 100, 70))

    def test_a_click_outside_the_card_answers_nothing(self):
        self.assertIsNone(ask_hit(self.RECTS, 300, 50))
        self.assertIsNone(ask_hit(self.RECTS, 100, 5))

    def test_edges_are_inclusive_so_a_border_click_still_counts(self):
        self.assertEqual(ask_hit(self.RECTS, 20, 40), "yes")
        self.assertEqual(ask_hit(self.RECTS, 220, 68), "yes")

    def test_no_pending_question_means_no_answer(self):
        self.assertIsNone(ask_hit([], 100, 50))


if __name__ == "__main__":
    unittest.main()
