"""Small regression tests for semantic applicability and packed state 3."""
from __future__ import annotations

import unittest
from types import SimpleNamespace

from engine import Engine
from migrate_not_applicable import _restore_columns
from question_policy import (STATE_NOT_APPLICABLE, STATE_UNKNOWN,
                             condition_value, encode_not_applicable_matrix)


class QuestionPolicyTests(unittest.TestCase):
    def test_restore_columns_recovers_pre_policy_states(self) -> None:
        meta = {"books": 1, "questions": 3, "bytes_per_row": 1}
        questions = [{"id": "fiction"}, {"id": "child"}, {"id": "war"}]
        current = bytes([1 | (3 << 2) | (3 << 4)])
        baseline = bytes([1 | (0 << 2) | (2 << 4)])
        restored = _restore_columns(
            current, baseline, meta, questions, {"child", "war"})
        self.assertEqual(restored, baseline)

    def test_probable_answer_opens_semantic_branch(self) -> None:
        condition = {"question": "fiction", "answer": "yes"}
        self.assertTrue(Engine._condition_satisfied(
            condition, {"fiction": "probably_yes"}))
        self.assertFalse(Engine._condition_satisfied(
            condition, {"fiction": "unknown"}))

    def test_logical_skip_requires_a_firm_answer(self) -> None:
        condition = {"question": "veryold", "answer": "yes"}
        self.assertTrue(Engine._condition_satisfied(
            condition, {"veryold": "yes"}, allow_probable=False))
        self.assertFalse(Engine._condition_satisfied(
            condition, {"veryold": "probably_yes"}, allow_probable=False))

        engine = Engine.__new__(Engine)
        engine.m = SimpleNamespace(
            question_policy={"alive": {"skip_if": condition}},
            dependency_rules_by_child={})
        engine.answers = [("veryold", "yes")]
        self.assertTrue(engine._dependency_blocked("alive"))
        engine.answers = [("veryold", "probably_yes")]
        self.assertFalse(engine._dependency_blocked("alive"))

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
