"""Unit tests for the interest follow-up A/B pure logic (no DB / no LLM):
deterministic arm assignment, AI-draft JSON parsing, template token-fill, and the
Wilson interval used for the winner verdict. Run: python -m unittest discover -s tests
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from followup_experiments_data import assign_arm, _parse_drafts, N_VARS
from followup_experiments_attrib import wilson
from followup_templates_data import fill_tokens


class TestAssignArm(unittest.TestCase):
    def test_deterministic(self):
        self.assertEqual(assign_arm(12345), assign_arm(12345))

    def test_valid_values(self):
        self.assertIn(assign_arm(7), ("static", "ai"))

    def test_both_arms_appear_and_roughly_balanced(self):
        split = [assign_arm(i) for i in range(400)]
        self.assertTrue(150 <= split.count("ai") <= 250)   # ~50/50, tolerant
        self.assertTrue(150 <= split.count("static") <= 250)


class TestParseDrafts(unittest.TestCase):
    def test_fenced_json(self):
        self.assertEqual(_parse_drafts('```json\n["a","b"]\n```'), ["a", "b"])

    def test_caps_at_n(self):
        self.assertEqual(len(_parse_drafts('["a","b","c","d","e"]')), N_VARS)

    def test_drops_empty_and_trims(self):
        self.assertEqual(_parse_drafts('[" hi ", "", "  "]'), ["hi"])

    def test_bad_input(self):
        self.assertEqual(_parse_drafts("not json at all"), [])
        self.assertEqual(_parse_drafts(""), [])


class TestFillTokens(unittest.TestCase):
    def test_replaces(self):
        self.assertEqual(fill_tokens("Hi {first_name} at {company}",
                                     first_name="Sam", company="Acme"),
                         "Hi Sam at Acme")

    def test_blank_fallback(self):
        self.assertEqual(fill_tokens("Hi {first_name}"), "Hi there")
        self.assertEqual(fill_tokens("at {company}"), "at your team")

    def test_no_tokens_passthrough(self):
        self.assertEqual(fill_tokens("plain text"), "plain text")


class TestWilson(unittest.TestCase):
    def test_zero(self):
        self.assertEqual(wilson(0, 0), (0.0, 0.0))

    def test_bounds_ordered_and_in_range(self):
        lo, hi = wilson(15, 50)
        self.assertLessEqual(0.0, lo)
        self.assertLess(lo, hi)
        self.assertLessEqual(hi, 100.0)

    def test_all_positive_high_ceiling(self):
        lo, hi = wilson(20, 20)
        self.assertGreater(lo, 80.0)   # strong lower bound when 20/20
        self.assertEqual(hi, 100.0)


if __name__ == "__main__":
    unittest.main()
