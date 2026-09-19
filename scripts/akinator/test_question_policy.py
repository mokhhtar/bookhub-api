"""Small regression tests for semantic applicability and packed state 3."""
from __future__ import annotations

import unittest

from question_policy import (STATE_NOT_APPLICABLE, STATE_UNKNOWN,
                             condition_value, encode_not_applicable_matrix)


class QuestionPolicyTests(unittest.TestCase):
    def test_three_valued_any_does_not_turn_unknown_into_false(self) -> None:
        condition = {"any": [
            {"question": "fiction", "answer": "yes"},
            {"question": "nonfiction", "answer": "no"},
        ]}
        self.assertIsNone(condition_value(
            condition, lambda _question: None))
        self.assertFalse(condition_value(
            condition, lambda question: question == "nonfiction"))

    def test_policy_edit_resets_old_na_to_unknown(self) -> None:
        meta = {"books": 1, "questions": 3, "bytes_per_row": 1}
        questions = [{"id": "fiction"}, {"id": "nonfiction"}, {"id": "child"}]
        # fiction=no, nonfiction=yes, child=absent
        matrix = bytes([0 | (1 << 2) | (0 << 4)])
        restrictive = {"questions": {"child": {"applies_if":
            {"question": "fiction", "answer": "yes"}}}}
        encoded, _ = encode_not_applicable_matrix(
            matrix, meta, questions, restrictive, max_books=1)
        self.assertEqual((encoded[0] >> 4) & 3, STATE_NOT_APPLICABLE)

        permissive = {"questions": {"child": {"applies_if": {"any": [
            {"question": "fiction", "answer": "yes"},
            {"question": "nonfiction", "answer": "yes"},
        ]}}}}
        reset, stats = encode_not_applicable_matrix(
            encoded, meta, questions, permissive, max_books=1,
            reset_questions={"child"})
        self.assertEqual((reset[0] >> 4) & 3, STATE_UNKNOWN)
        self.assertEqual(stats["reset"], 1)

    def test_positive_child_is_preserved_as_a_conflict(self) -> None:
        meta = {"books": 1, "questions": 2, "bytes_per_row": 1}
        questions = [{"id": "fiction"}, {"id": "child"}]
        matrix = bytes([0 | (1 << 2)])
        policy = {"questions": {"child": {"applies_if":
            {"question": "fiction", "answer": "yes"}}}}
        encoded, stats = encode_not_applicable_matrix(
            matrix, meta, questions, policy, max_books=1)
        self.assertEqual(encoded, matrix)
        self.assertEqual(stats["conflicts"], 1)

    def test_nested_gates_do_not_depend_on_column_order(self) -> None:
        meta = {"books": 1, "questions": 3, "bytes_per_row": 1}
        questions = [{"id": "fiction"}, {"id": "parent"}, {"id": "child"}]
        policy = {"questions": {
            "parent": {"applies_if": {"question": "fiction", "answer": "yes"}},
            "child": {"applies_if": {"question": "parent", "answer": "yes"}},
        }}
        encoded, stats = encode_not_applicable_matrix(
            bytes([0]), meta, questions, policy, max_books=1)
        self.assertEqual((encoded[0] >> 2) & 3, STATE_NOT_APPLICABLE)
        self.assertEqual((encoded[0] >> 4) & 3, STATE_NOT_APPLICABLE)
        self.assertEqual(stats["unresolved"], 0)


if __name__ == "__main__":
    unittest.main()
