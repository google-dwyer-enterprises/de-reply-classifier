"""credit_alerts.maybe_low_balance_alert — proactive low-credit warning.

Pure threshold logic (email + throttle + api_event mocked): fire only when
remaining <= the provider's threshold, never above, never on None/unknown, and
stay classified-as-low even when the throttle suppresses the email.
"""
import unittest
from unittest import mock

import credit_alerts as ca


class TestLowBalanceAlert(unittest.TestCase):
    def test_no_fire_above_threshold(self):
        with mock.patch.object(ca, "_send_low_balance_email") as m:
            self.assertFalse(ca.maybe_low_balance_alert("Rainforest", 5000))
            m.assert_not_called()

    def test_fires_at_or_below_threshold(self):
        with mock.patch.object(ca, "_should_send", return_value=True), \
             mock.patch("api_events.record"), \
             mock.patch.object(ca, "_send_low_balance_email") as m:
            self.assertTrue(ca.maybe_low_balance_alert("Rainforest", 900))
            m.assert_called_once()

    def test_none_remaining_no_fire(self):
        with mock.patch.object(ca, "_send_low_balance_email") as m:
            self.assertFalse(ca.maybe_low_balance_alert("Rainforest", None))
            m.assert_not_called()

    def test_unknown_provider_has_no_threshold(self):
        with mock.patch.object(ca, "_send_low_balance_email") as m:
            self.assertFalse(ca.maybe_low_balance_alert("Nope", 1))
            m.assert_not_called()

    def test_explicit_threshold_override(self):
        with mock.patch.object(ca, "_should_send", return_value=True), \
             mock.patch("api_events.record"), \
             mock.patch.object(ca, "_send_low_balance_email") as m:
            self.assertTrue(ca.maybe_low_balance_alert("Rainforest", 50, threshold=100))
            m.assert_called_once()

    def test_throttle_suppresses_email_but_still_flagged(self):
        with mock.patch.object(ca, "_should_send", return_value=False), \
             mock.patch("api_events.record"), \
             mock.patch.object(ca, "_send_low_balance_email") as m:
            self.assertTrue(ca.maybe_low_balance_alert("Rainforest", 10))
            m.assert_not_called()

    def test_bettercontact_has_a_threshold(self):
        # BetterContact was previously uncovered (only Rainforest had a
        # threshold), so an empty BC balance never warned — the gap that lost a
        # day. It must now fire below threshold and stay quiet above.
        self.assertIn("BetterContact", ca.LOW_BALANCE_THRESHOLDS)
        with mock.patch.object(ca, "_should_send", return_value=True), \
             mock.patch("api_events.record"), \
             mock.patch.object(ca, "_send_low_balance_email") as m:
            self.assertTrue(ca.maybe_low_balance_alert("BetterContact", 50))
            m.assert_called_once()
        with mock.patch.object(ca, "_send_low_balance_email") as m:
            self.assertFalse(ca.maybe_low_balance_alert("BetterContact", 5000))
            m.assert_not_called()


if __name__ == "__main__":
    unittest.main()
