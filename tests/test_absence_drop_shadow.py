"""Absence-drop shadow measurement — the pure bucketing over cache rows.

bucket_rows re-applies the gate's own floor_verdict to each SmartScout-miss so the
shadow measurement can never drift from what enforcement would actually do. These
lock the classification + the false-drop-rate math. No DB, no network.

Row tuple layout (as the query returns it):
  (brand_norm, annual_revenue, on_amazon, source, branded_hits, ratings_total, annual_units, fetched_at)
"""
import unittest

import absence_drop_shadow as ads


def row(brand, ann, hits, ratings=0, units=0):
    return (brand, ann, hits > 0, "rainforest", hits, ratings, units, None)


class TestBucketRows(unittest.TestCase):
    def setUp(self):
        # one of each outcome class:
        self.rows = [
            row("zero", 0, 0),                       # 0 listings          -> DROP (+ $0 presence)
            row("small", 50_000, 1, 50, 500),        # tiny presence        -> DROP
            row("keep", 500_000, 3, 2_000, 10_000),  # >= floor, corroborated -> KEEP (false-drop)
            row("review", 100_000, 3, 1_500, 5_000), # below floor but established -> REVIEW
        ]

    def test_buckets_and_rate(self):
        s = ads.bucket_rows(self.rows, floor_line=300_000)
        self.assertEqual(s["total_misses"], 4)
        self.assertEqual(s["correct_drops"], 2)      # zero + small
        self.assertEqual(s["review"], 1)
        self.assertEqual(s["false_drops"], 1)        # keep
        self.assertEqual(s["zero_presence"], 1)
        self.assertAlmostEqual(s["false_drop_rate"], 0.25)
        self.assertAlmostEqual(s["false_drop_rate_upper"], 0.50)  # (keep + review)/4

    def test_floor_override_flips_keep_to_review(self):
        # At a $1M floor the $500k brand is no longer a keep — it's below the line
        # but established -> REVIEW, so the strict false-drop count goes to 0.
        s = ads.bucket_rows(self.rows, floor_line=1_000_000)
        self.assertEqual(s["false_drops"], 0)
        self.assertEqual(s["review"], 2)             # the 500k brand joins REVIEW

    def test_empty_is_safe(self):
        s = ads.bucket_rows([], floor_line=300_000)
        self.assertEqual(s["total_misses"], 0)
        self.assertEqual(s["false_drop_rate"], 0.0)
        self.assertEqual(s["false_drop_rate_upper"], 0.0)

    def test_null_revenue_and_units_do_not_crash(self):
        s = ads.bucket_rows([("x", None, True, "rainforest", 2, None, None, None)], floor_line=300_000)
        self.assertEqual(s["total_misses"], 1)       # None revenue -> 0 -> a drop, no crash


if __name__ == "__main__":
    unittest.main()
