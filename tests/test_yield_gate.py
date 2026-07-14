"""Yield-gate: park a thinning industry mid-run so the gate rotates to fresher
shallow pages instead of drilling deep (deep pages burn Rainforest on sub-floor
small brands that fail the revenue floor). Two parts:
  1. the pure should_park_industry decision (threshold behaviour), and
  2. a REPLAY over batch #56's real page data — proving the gate would have
     skipped the wasteful deep pages (esp. the 0-survivor one) it actually ran.
No network, no credits.
"""
import unittest
from unittest import mock
from datetime import datetime, timezone, timedelta

import bettercontact_sync as bc


class TestShouldPark(unittest.TestCase):
    def test_cheap_page_never_parks(self):
        # A page that spent little RF (cache/SmartScout hits) is never parked on,
        # even with 0 survivors — it wasn't the expensive kind.
        self.assertFalse(bc.should_park_industry(0, 0))
        self.assertFalse(bc.should_park_industry(0, bc.YIELD_GATE_MIN_RF_TO_JUDGE - 1))

    def test_zero_survivors_with_real_spend_parks(self):
        self.assertTrue(bc.should_park_industry(0, 50))

    def test_high_rf_per_survivor_parks(self):
        # 29 RF for 1 survivor = 29/survivor > 15 ceiling -> park
        self.assertTrue(bc.should_park_industry(1, 29))
        self.assertTrue(bc.should_park_industry(4, 101))     # 25/survivor

    def test_healthy_yield_keeps(self):
        self.assertFalse(bc.should_park_industry(11, 55))    # 5/survivor
        self.assertFalse(bc.should_park_industry(15, 124))   # 8.3/survivor
        self.assertFalse(bc.should_park_industry(8, 75))     # 9.4/survivor

    def test_toggle_off_never_parks(self):
        with mock.patch.object(bc, "YIELD_GATE_ENABLED", False):
            self.assertFalse(bc.should_park_industry(0, 999))


class TestReplayBatch56(unittest.TestCase):
    """Replay the 9 gate pages batch #56 actually ran (industry, survivors,
    per-page RF delta from the live logs) and confirm the yield-gate parks the
    right industries and skips the wasteful deep pages."""
    # (industry, survivors_this_page, rf_delta_this_page) — from #56's logs.
    PAGES = [
        ("PersonalCare", 11, 55),
        ("FoodBev",       9, 33),
        ("PetServices",   1, 29),   # thin -> should park PetServices here
        ("RetailHealth",  8, 0),    # cheap (cache) -> keep
        ("PersonalCare", 14, 75),
        ("FoodBev",       4, 101),  # thin -> should park FoodBev here
        ("PetServices",   0, 50),   # would be SKIPPED (PetServices already parked)
        ("RetailHealth", 15, 124),
        ("PersonalCare",  8, 75),
    ]

    def test_gate_parks_thin_industries_and_avoids_waste(self):
        parked, avoided_rf, avoided_pages = set(), 0, 0
        for ind, surv, rf in self.PAGES:
            if ind in parked:                 # gate would not have run this page
                avoided_rf += rf
                avoided_pages += 1
                continue
            if bc.should_park_industry(surv, rf):
                parked.add(ind)
        self.assertEqual(parked, {"PetServices", "FoodBev"})
        # The gate would have skipped exactly the 0-survivor Pet Services p2
        # (50 RF for 0 leads) — pure waste it actually spent.
        self.assertEqual(avoided_pages, 1)
        self.assertEqual(avoided_rf, 50)


class TestAdaptivePageSize(unittest.TestCase):
    def test_full_page_in_shallow_zone(self):
        self.assertEqual(bc.page_limit_for_offset(0, 200), 200)
        self.assertEqual(bc.page_limit_for_offset(bc.DEEP_OFFSET_THRESHOLD - 1, 200), 200)

    def test_small_page_at_deep_offset(self):
        # deep offsets thin out -> use a small page so a bad page costs less RF
        self.assertEqual(bc.page_limit_for_offset(bc.DEEP_OFFSET_THRESHOLD, 200), bc.DEEP_PAGE_LIMIT)
        self.assertEqual(bc.page_limit_for_offset(2000, 200), bc.DEEP_PAGE_LIMIT)

    def test_never_exceeds_requested_full(self):
        self.assertEqual(bc.page_limit_for_offset(9999, 30), 30)   # full<floor -> full


class TestParkStatus(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 7, 14, 12, 0, tzinfo=timezone.utc)

    def test_never_parked_is_available(self):
        self.assertEqual(bc.industry_park_status(None, self.now), (True, False))

    def test_parked_within_cooldown_is_skipped(self):
        recent = self.now - timedelta(days=1)
        self.assertEqual(bc.industry_park_status(recent, self.now), (False, False))

    def test_cooldown_expired_available_and_resets_to_shallow(self):
        old = self.now - timedelta(days=bc.PARK_COOLDOWN_DAYS + 1)
        self.assertEqual(bc.industry_park_status(old, self.now), (True, True))

    def test_boundary_just_under_cooldown_still_skipped(self):
        edge = self.now - timedelta(days=bc.PARK_COOLDOWN_DAYS, hours=-1)  # just under
        self.assertEqual(bc.industry_park_status(edge, self.now), (False, False))


if __name__ == "__main__":
    unittest.main()
