"""Unit tests for followup_llm_features.coerce_features — the closed-enum validator
that guards what the LLM may write into the V2 tag columns. A bad value must fall
back, never reach the database. Run: python -m unittest discover -s tests
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from followup_llm_features import coerce_features, build_feature_block
from config import FOLLOWUP_FEATURE_SPEC, FOLLOWUP_FEATURE_FALLBACK


class TestCoerceFeatures(unittest.TestCase):
    def test_valid_values_pass_through(self):
        item = {"id": 1, "hook_type": "question", "tone": "casual",
                "cta_style": "direct", "personalization": "deep"}
        self.assertEqual(coerce_features(item),
                         {"hook_type": "question", "tone": "casual",
                          "cta_style": "direct", "personalization": "deep"})

    def test_unknown_values_fall_back(self):
        item = {"hook_type": "banana", "tone": "angry", "cta_style": "??", "personalization": "max"}
        self.assertEqual(coerce_features(item), FOLLOWUP_FEATURE_FALLBACK)

    def test_missing_keys_fall_back(self):
        self.assertEqual(coerce_features({}), FOLLOWUP_FEATURE_FALLBACK)

    def test_none_values_fall_back(self):
        item = {"hook_type": None, "tone": None, "cta_style": None, "personalization": None}
        self.assertEqual(coerce_features(item), FOLLOWUP_FEATURE_FALLBACK)

    def test_case_and_whitespace_normalized(self):
        item = {"hook_type": " Question ", "tone": "CASUAL",
                "cta_style": "Direct", "personalization": "LIGHT"}
        out = coerce_features(item)
        self.assertEqual(out["hook_type"], "question")
        self.assertEqual(out["tone"], "casual")
        self.assertEqual(out["cta_style"], "direct")
        self.assertEqual(out["personalization"], "light")

    def test_every_output_is_a_legal_enum_value(self):
        # Whatever the model returns, the result is always inside the closed enum.
        out = coerce_features({"hook_type": "x", "tone": "y", "cta_style": "z", "personalization": "w"})
        for dim, vals in FOLLOWUP_FEATURE_SPEC.items():
            self.assertIn(out[dim], vals)

    def test_feature_block_lists_every_value(self):
        block = build_feature_block()
        for dim, vals in FOLLOWUP_FEATURE_SPEC.items():
            self.assertIn(dim.upper(), block)
            for v in vals:
                self.assertIn(v, block)


if __name__ == "__main__":
    unittest.main()
