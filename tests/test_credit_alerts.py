"""Tests for credit_alerts.looks_like_credit_error — the pure matcher that
decides whether a provider error is a credit/quota exhaustion (worth emailing)
vs an unrelated error (stay quiet). No network, no DB."""
import unittest

import credit_alerts as ca


class TestLooksLikeCreditError(unittest.TestCase):
    def test_anthropic_real_message(self):
        # the exact message that broke the cron on 2026-07-06
        msg = ("Error code: 400 - {'type': 'error', 'error': {'type': "
               "'invalid_request_error', 'message': 'Your credit balance is too "
               "low to access the Anthropic API. Please go to Plans & Billing...'}}")
        self.assertTrue(ca.looks_like_credit_error("Anthropic", msg))

    def test_rainforest_suspended(self):
        msg = ("Your account has been temporarily suspended as our systems "
               "detected multiple Free Trial accounts... subscribe to a Plan.")
        self.assertTrue(ca.looks_like_credit_error("Rainforest", msg))

    def test_openai_quota(self):
        self.assertTrue(ca.looks_like_credit_error(
            "OpenAI", "Error: insufficient_quota - You exceeded your current quota"))

    def test_bettercontact_credits(self):
        self.assertTrue(ca.looks_like_credit_error(
            "BetterContact", "BC reports insufficient credits: 402"))

    def test_unrelated_error_stays_quiet(self):
        # a normal 500 / timeout must NOT trigger a credit alert
        self.assertFalse(ca.looks_like_credit_error(
            "Anthropic", "Error code: 529 - overloaded_error: the model is overloaded"))
        self.assertFalse(ca.looks_like_credit_error(
            "Anthropic", "ReadTimeout: request timed out"))

    def test_rate_limit_is_not_credit_error(self):
        self.assertFalse(ca.looks_like_credit_error(
            "Anthropic", "Error code: 429 - rate_limit_error"))

    def test_wrong_provider_signature_no_match(self):
        # a Rainforest suspension text must not match under 'OpenAI'
        self.assertFalse(ca.looks_like_credit_error(
            "OpenAI", "Your account has been temporarily suspended"))

    def test_unknown_provider(self):
        self.assertFalse(ca.looks_like_credit_error("Nope", "credit balance is too low"))

    def test_empty(self):
        self.assertFalse(ca.looks_like_credit_error("Anthropic", ""))
        self.assertFalse(ca.looks_like_credit_error("Anthropic", None))

    def test_case_insensitive(self):
        self.assertTrue(ca.looks_like_credit_error("Anthropic", "CREDIT BALANCE IS TOO LOW"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
