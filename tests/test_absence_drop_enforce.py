"""Absence-drop ENFORCEMENT + audit sampling in amazon_revenue_qa.evaluate.

When ABSENCE_DROP_ENFORCE is on, a SmartScout-miss is free-dropped WITHOUT paying
Rainforest — except a deterministic ~ABSENCE_DROP_SAMPLE_PCT% audit slice that still
gets checked (keeps the shadow monitor fed). Lock: the drop skips Rainforest, the
sample doesn't, the flag defaults behaviour-neutral, and sampling is deterministic.
No DB, no network — match_brand + rainforest_floor are mocked.
"""
import unittest
from unittest import mock

import amazon_revenue_qa as qa


class TestInAuditSample(unittest.TestCase):
    def test_zero_pct_never_samples(self):
        with mock.patch.object(qa, "ABSENCE_DROP_SAMPLE_PCT", 0):
            self.assertFalse(qa._in_audit_sample("Any Brand Co"))

    def test_hundred_pct_always_samples(self):
        with mock.patch.object(qa, "ABSENCE_DROP_SAMPLE_PCT", 100):
            self.assertTrue(qa._in_audit_sample("Any Brand Co"))

    def test_deterministic_same_name_same_side(self):
        with mock.patch.object(qa, "ABSENCE_DROP_SAMPLE_PCT", 10):
            self.assertEqual(qa._in_audit_sample("Consistent Brand LLC"),
                             qa._in_audit_sample("Consistent Brand LLC"))

    def test_roughly_target_rate_over_many_names(self):
        with mock.patch.object(qa, "ABSENCE_DROP_SAMPLE_PCT", 10):
            hits = sum(qa._in_audit_sample(f"brand number {i} co") for i in range(2000))
            self.assertGreater(hits, 120)   # ~200 expected (10% of 2000); generous slack
            self.assertLess(hits, 300)


class TestEvaluateAbsenceDrop(unittest.TestCase):
    def _run(self, enforce, sample_pct):
        """evaluate() on a SmartScout-miss; returns (result, rainforest_mock)."""
        rf_return = {"on_amazon": True, "annual_revenue": 0, "source": "rainforest",
                     "branded_hits": 0, "ratings_total": 0, "annual_units": 0}
        with mock.patch.object(qa, "match_brand", return_value=None), \
             mock.patch.object(qa, "ABSENCE_DROP_SAMPLE_PCT", sample_pct), \
             mock.patch.object(qa, "rainforest_floor", return_value=rf_return) as rf:
            res = qa.evaluate(mock.Mock(), "Some Miss Company", absence_enforce=enforce)
        return res, rf

    def test_enforce_drops_without_paying_rainforest(self):
        res, rf = self._run(enforce=True, sample_pct=0)     # 0% -> always absence-drop
        self.assertEqual(res["verdict"], "DROP")
        self.assertEqual(res["source"], "absence_drop")
        rf.assert_not_called()                               # the whole point: no credit

    def test_audit_sample_still_checks_rainforest(self):
        _res, rf = self._run(enforce=True, sample_pct=100)   # 100% -> always audited
        rf.assert_called_once()

    def test_flag_off_is_behaviour_neutral(self):
        _res, rf = self._run(enforce=False, sample_pct=0)    # unchanged legacy path
        rf.assert_called_once()

    def test_default_uses_module_flag_off(self):
        # absence_enforce=None -> read module ABSENCE_DROP_ENFORCE, which defaults off
        with mock.patch.object(qa, "match_brand", return_value=None), \
             mock.patch.object(qa, "ABSENCE_DROP_ENFORCE", False), \
             mock.patch.object(qa, "rainforest_floor",
                               return_value={"on_amazon": False, "annual_revenue": 0,
                                             "source": "rainforest", "branded_hits": 0,
                                             "ratings_total": 0, "annual_units": 0}) as rf:
            qa.evaluate(mock.Mock(), "Some Miss Company")
        rf.assert_called_once()


if __name__ == "__main__":
    unittest.main()
