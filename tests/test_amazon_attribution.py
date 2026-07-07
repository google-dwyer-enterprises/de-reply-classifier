"""Tests for the Amazon revenue-floor brand-attribution fix (2026-07-07).

Victor flagged that "Scott's Protein Balls" was reported at ~$44M (from 38
listings / 340k ratings) when the brand makes ~$1k/month. Root cause: a search
for a generic/category-like name returns COMPETITORS (e.g. "Protein Balls by
Orgain"), and the old matcher credited them via the title even though their
byline was a different brand; the canonical-name requery then escalated a small
brand into a full category search.

These guard the two fixes, both pure (no network/DB):
  1. amazon_presence._is_branded — a PRESENT, FOREIGN byline is a competitor and
     is excluded regardless of title; a byline that's a short sub-form of our own
     name ('Scott's' for 'Scott's Protein Balls') is still ours; a MISSING byline
     still falls back to the title (concatenated own-brand recovery, 'Nanobebe').
  2. amazon_revenue_qa._is_respelling — the requery only fires for a genuine
     re-spelling of the searched name, not a truncation to a generic/common term.
"""
import unittest

import amazon_presence as ap
from amazon_revenue_qa import _is_respelling


class TestIsBranded(unittest.TestCase):
    def nb(self, term):
        return ap.normalize_brand(term)

    def test_foreign_byline_competitor_excluded(self):
        # "Protein Balls by Orgain" ranks for a generic search but is Orgain's
        self.assertFalse(ap._is_branded(self.nb("Protein Balls"),
                                        "Orgain", "Protein Balls by Orgain"))
        self.assertFalse(ap._is_branded(self.nb("Scott's Protein Balls"),
                                        "GoMacro", "GoMacro Protein Balls"))

    def test_own_short_byline_subform_kept_when_title_confirms(self):
        # a brand bylines itself with its core name AND the title confirms the
        # full name -> ours. (A subform byline falls through to title
        # confirmation; this is what also blocks 'Apple' -> 'Apple Rubber'.)
        self.assertTrue(ap._is_branded(self.nb("Scott's Protein Balls"),
                                       "Scott's", "Scott's Protein Balls Original"))
        self.assertTrue(ap._is_branded(self.nb("Anchor Electronics"),
                                       "Anchor", "Anchor Electronics 12ft Cable"))

    def test_subform_byline_without_title_confirmation_excluded(self):
        # conservative (floor under-counts): 'Apple' byline on an 'Apple Rubber'
        # search whose title doesn't confirm 'rubber' is NOT credited to us.
        self.assertFalse(ap._is_branded(self.nb("Apple Rubber"),
                                        "Apple", "Apple AirPods Pro"))

    def test_missing_byline_falls_back_to_title(self):
        # byline absent (common in Rainforest) -> concatenated own-brand recovery
        self.assertTrue(ap._is_branded(self.nb("Nano Bebe"),
                                       None, "Nanobebe Silicone Bottle"))
        self.assertTrue(ap._is_branded(self.nb("ScentSationals"),
                                       "", "ScentSationals Wax Cubes Variety"))

    def test_missing_byline_unrelated_title_not_matched(self):
        self.assertFalse(ap._is_branded(self.nb("Nano Bebe"),
                                        None, "Dr Browns Baby Bottle"))

    def test_exact_byline_match(self):
        self.assertTrue(ap._is_branded(self.nb("Olaplex"), "OLAPLEX", "Olaplex No.3"))


class TestScoreExcludesCompetitors(unittest.TestCase):
    def test_generic_search_does_not_sum_competitors(self):
        results = [
            {"brand": "GoMacro", "title": "GoMacro Protein Balls",
             "price": 25, "recent_sales": "2K+ bought in past month", "ratings_total": 50000},
            {"brand": "Orgain", "title": "Protein Balls by Orgain",
             "price": 22, "recent_sales": "3K+ bought in past month", "ratings_total": 80000},
            {"brand": "Scott's", "title": "Scott's Protein Balls Original",
             "price": 15, "recent_sales": "50+ bought in past month", "ratings_total": 5},
        ]
        s = ap.score_search_results("Scott's Protein Balls", results)
        # only Scott's own listing counts -> tiny floor, not the competitors' millions
        self.assertEqual(s["branded_hits"], 1)
        self.assertEqual(s["ratings_total"], 5)
        self.assertLess(s["revenue_floor_annual"], 50_000)


class TestRequeryRespellingGuard(unittest.TestCase):
    def test_legit_respellings_allowed(self):
        self.assertTrue(_is_respelling("Scents Ational S", "ScentSationals"))
        self.assertTrue(_is_respelling("Nano Bebe", "Nanobebe"))

    def test_truncation_to_common_or_category_rejected(self):
        self.assertFalse(_is_respelling("Scott's Protein Balls", "Scott's"))
        self.assertFalse(_is_respelling("Scott's Protein Balls", "Protein Balls"))
        self.assertFalse(_is_respelling("Anchor Electronics Inc", "Anchor"))

    def test_empty_inputs_safe(self):
        self.assertFalse(_is_respelling("", "Anything"))
        self.assertFalse(_is_respelling("Something", ""))


if __name__ == "__main__":
    unittest.main()
