"""Unit tests for config.tag_to_label — the Gap 2 Instantly-tag -> taxonomy mapper.

This mapper feeds the rank-based headline-status promotion (excel_writer.promote_status),
so a wrong mapping silently inflates/under-counts the client-facing booked/interested
count. Run: python -m unittest discover -s tests
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import tag_to_label


class TestTagToLabel(unittest.TestCase):
    def test_booked_positives(self):
        # Every real production booked tag (case/spacing variants) must map to booked.
        for tag in ["Epic - Booked", "Booked - DE SALES", "EC - Booked", "Navira - Booked",
                    "Lumian - Booked", "BG - Booked", "SOKO - Booked", "MOD Booked +ve",
                    "booked", "BOOKED", "  Booked  "]:
            with self.subTest(tag=tag):
                self.assertEqual(tag_to_label(tag), "booked")

    def test_interested_positives(self):
        for tag in ["Epic - interested", "Interested", "interested - DE SALES",
                    "EC - Interested", "Navira - Interested", "Interested - Ripple",
                    "  Interested  "]:
            with self.subTest(tag=tag):
                self.assertEqual(tag_to_label(tag), "interested")

    def test_negatives_and_neutral(self):
        for tag in ["Not interested", "not interested", "NOT INTERESTED",
                    "Navira - Not Qualified", "Not Qualified", "Lead", "Unsubscribe",
                    "Out of office", "Autoreply", "Support Email", "referral",
                    "DE - Contact soon", "", "   ", None]:
            with self.subTest(tag=tag):
                self.assertIsNone(tag_to_label(tag))

    def test_meeting_completed_is_booked(self):
        # Post-booking Instantly built-in: at least as strong as booked.
        self.assertEqual(tag_to_label("Meeting completed"), "booked")
        self.assertEqual(tag_to_label("Epic - Meeting Completed"), "booked")

    def test_word_boundary_no_false_positive_booked(self):
        # Word-boundary, not raw substring: these must NOT promote to booked.
        for tag in ["overbooked", "unbooked", "Rebooked"]:  # no separator -> no \bbooked\b
            with self.subTest(tag=tag):
                self.assertIsNone(tag_to_label(tag))

    def test_cancelled_booking_not_promoted(self):
        for tag in ["Booked - cancelled", "Booked then cancelled", "Meeting cancelled"]:
            with self.subTest(tag=tag):
                self.assertIsNone(tag_to_label(tag))

    def test_word_boundary_no_false_positive_interested(self):
        # 'not interested' guard is robust to hyphen/extra-space; dis-/un-/-interested don't match.
        for tag in ["not-interested", "not  interested", "Uninterested", "Disinterested",
                    "reinterested"]:
            with self.subTest(tag=tag):
                self.assertIsNone(tag_to_label(tag))

    def test_won_not_matched(self):
        # 'won' is intentionally NOT matched (would false-positive on "won't ...").
        self.assertIsNone(tag_to_label("won't buy"))


if __name__ == "__main__":
    unittest.main()
