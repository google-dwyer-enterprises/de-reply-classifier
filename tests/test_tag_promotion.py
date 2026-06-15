"""Unit tests for excel_writer.promote_status — the Gap 2 rank-based, promote-only
headline-status promotion. The invariant under test: an Instantly booked/interested
tag can RAISE a lead's status but never lower it. Run: python -m unittest discover -s tests
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from excel_writer import promote_status, STATUS_RANK


class TestPromoteStatus(unittest.TestCase):
    def test_promotes_when_tag_outranks_classifier(self):
        self.assertEqual(promote_status("not_interested", "Epic - Booked"), ("booked", True))
        self.assertEqual(promote_status("interested", "Booked - DE SALES"), ("booked", True))
        self.assertEqual(promote_status("other", "Epic - interested"), ("interested", True))
        self.assertEqual(promote_status("not_now", "EC - Interested"), ("interested", True))

    def test_never_demotes(self):
        # The whole point: a tagged-'interested' lead the classifier already booked stays booked.
        self.assertEqual(promote_status("booked", "interested - DE SALES"), ("booked", False))
        self.assertEqual(promote_status("booked", "Not interested"), ("booked", False))

    def test_equal_rank_is_noop(self):
        self.assertEqual(promote_status("interested", "EC - Interested"), ("interested", False))
        self.assertEqual(promote_status("booked", "Epic - Booked"), ("booked", False))

    def test_none_classifier_is_promoted_by_tag(self):
        # status1 None -> rank 999, so any booked/interested tag promotes (lead with only a tag).
        self.assertEqual(promote_status(None, "Epic - Booked"), ("booked", True))
        self.assertEqual(promote_status(None, "Interested"), ("interested", True))

    def test_none_classifier_with_negative_tag_stays_none(self):
        self.assertEqual(promote_status(None, "Not interested"), (None, False))
        self.assertEqual(promote_status(None, None), (None, False))

    def test_no_tag_is_noop(self):
        self.assertEqual(promote_status("not_now", None), ("not_now", False))
        self.assertEqual(promote_status("not_now", "Lead"), ("not_now", False))

    def test_rank_assumptions(self):
        # Guards the priority order the promotion relies on (booked beats interested beats negatives).
        self.assertLess(STATUS_RANK["booked"], STATUS_RANK["interested"])
        self.assertLess(STATUS_RANK["interested"], STATUS_RANK["not_interested"])
        self.assertEqual(STATUS_RANK.get("", 999), 999)


if __name__ == "__main__":
    unittest.main()
