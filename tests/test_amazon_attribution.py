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
from amazon_revenue_qa import _is_respelling, floor_verdict, SUSPECT_UNITS_PER_RATING


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


class TestDedupeByAsin(unittest.TestCase):
    def test_same_asin_counted_once(self):
        # Rainforest returns the same product twice (sponsored + organic)
        res = [
            {"asin": "A1", "brand": "Cata-Kor", "title": "NAD+", "price": 40,
             "recent_sales": "20K+ bought in past month", "ratings_total": 4858},
            {"asin": "A1", "brand": "Cata-Kor", "title": "NAD+", "price": 40,
             "recent_sales": "20K+ bought in past month", "ratings_total": 4858},
            {"asin": "A2", "brand": "Cata-Kor", "title": "NMN", "price": 35,
             "recent_sales": "10K+ bought in past month", "ratings_total": 1088},
        ]
        s = ap.score_search_results("Cata-Kor", res)
        self.assertEqual(s["branded_hits"], 2)                 # not 3
        self.assertEqual(s["revenue_floor_annual"], (20000*40 + 10000*35) * 12)
        self.assertEqual(s["annual_units"], (20000 + 10000) * 12)

    def test_missing_asin_distinct_titles_not_collapsed(self):
        res = [
            {"brand": "X", "title": "Product One", "price": 10,
             "recent_sales": "100+ bought in past month", "ratings_total": 5},
            {"brand": "X", "title": "Product Two", "price": 10,
             "recent_sales": "100+ bought in past month", "ratings_total": 5},
        ]
        # byline 'X' won't match brand 'X Brand'? use exact brand so both count
        s = ap.score_search_results("X", res)
        self.assertEqual(s["branded_hits"], 2)


class TestVerdictAnnualizationGuard(unittest.TestCase):
    def test_big_floor_implausible_units_goes_review(self):
        # Cata-Kor: $19M floor but 26x units-per-rating -> REVIEW, not KEEP
        v, _ = floor_verdict({"branded_hits": 14, "annual_revenue": 19_255_608,
                              "ratings_total": 20_470, "annual_units": 534_000})
        self.assertEqual(v, "REVIEW")

    def test_big_floor_proportionate_units_keeps(self):
        # Waggin' Train: $8M floor, 7x -> KEEP
        v, _ = floor_verdict({"branded_hits": 21, "annual_revenue": 7_966_410,
                              "ratings_total": 67_855, "annual_units": 453_600})
        self.assertEqual(v, "KEEP")

    def test_no_units_data_does_not_downgrade(self):
        # old cache rows without annual_units must still KEEP on a real floor
        v, _ = floor_verdict({"branded_hits": 5, "annual_revenue": 1_000_000,
                              "ratings_total": 5_000})
        self.assertEqual(v, "KEEP")

    def test_zero_ratings_big_floor_goes_review(self):
        # a KEEP-level floor with NO review base is unverifiable -> REVIEW
        v, _ = floor_verdict({"branded_hits": 3, "annual_revenue": 2_000_000,
                              "ratings_total": 0, "annual_units": 40_000})
        self.assertEqual(v, "REVIEW")


class TestPerClientFloor(unittest.TestCase):
    """The keep/drop line is a per-run/per-client parameter (July 8 ask)."""
    def _rev(self, annual, ratings=5_000, units=50_000, hits=5):
        return {"branded_hits": hits, "annual_revenue": annual,
                "ratings_total": ratings, "annual_units": units}

    def test_default_floor_keeps_500k(self):
        # $500k clears the default $300k floor
        self.assertEqual(floor_verdict(self._rev(500_000))[0], "KEEP")

    def test_million_floor_does_not_keep_500k(self):
        # same $500k brand, but a $1M-ICP client -> below floor -> not KEEP
        v, _ = floor_verdict(self._rev(500_000), floor_line=1_000_000)
        self.assertNotEqual(v, "KEEP")

    def test_million_floor_keeps_2M(self):
        v, _ = floor_verdict(self._rev(2_000_000, ratings=50_000, units=500_000),
                             floor_line=1_000_000)
        self.assertEqual(v, "KEEP")


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
