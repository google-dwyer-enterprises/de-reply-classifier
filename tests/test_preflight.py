"""preflight.check — provider health gate before a scrape spends.

Pure aggregation (the actual provider probes mocked): BetterContact is always
required; Rainforest only for revenue-first. All-healthy iff every required
provider is up.
"""
import unittest
from unittest import mock

import preflight


class TestPreflightCheck(unittest.TestCase):
    def test_classic_checks_anthropic_and_bettercontact_not_rainforest(self):
        with mock.patch.object(preflight, "_anthropic", return_value=(True, "Anthropic OK")), \
             mock.patch.object(preflight, "_bettercontact", return_value=(True, "BC OK")), \
             mock.patch.object(preflight, "_rainforest", return_value=(False, "RF down")) as rf:
            ok, msgs = preflight.check(revenue_first=False, use_cache=False)
        self.assertTrue(ok)                 # Rainforest not required for classic
        rf.assert_not_called()
        self.assertEqual(msgs, ["Anthropic OK", "BC OK"])

    def test_revenue_first_requires_all_three(self):
        with mock.patch.object(preflight, "_anthropic", return_value=(True, "Anthropic OK")), \
             mock.patch.object(preflight, "_bettercontact", return_value=(True, "BC OK")), \
             mock.patch.object(preflight, "_rainforest", return_value=(True, "RF OK")):
            ok, _ = preflight.check(revenue_first=True, use_cache=False)
        self.assertTrue(ok)

    def test_down_anthropic_blocks_even_classic(self):
        with mock.patch.object(preflight, "_anthropic", return_value=(False, "Anthropic down")), \
             mock.patch.object(preflight, "_bettercontact", return_value=(True, "BC OK")):
            ok, msgs = preflight.check(revenue_first=False, use_cache=False)
        self.assertFalse(ok)
        self.assertIn("Anthropic down", msgs)

    def test_down_bettercontact_blocks(self):
        with mock.patch.object(preflight, "_anthropic", return_value=(True, "Anthropic OK")), \
             mock.patch.object(preflight, "_bettercontact", return_value=(False, "BC hung")):
            ok, msgs = preflight.check(revenue_first=False, use_cache=False)
        self.assertFalse(ok)
        self.assertIn("BC hung", msgs)

    def test_down_rainforest_blocks_revenue_first(self):
        with mock.patch.object(preflight, "_anthropic", return_value=(True, "Anthropic OK")), \
             mock.patch.object(preflight, "_bettercontact", return_value=(True, "BC OK")), \
             mock.patch.object(preflight, "_rainforest", return_value=(False, "RF out of credits")):
            ok, msgs = preflight.check(revenue_first=True, use_cache=False)
        self.assertFalse(ok)
        self.assertIn("RF out of credits", msgs)

    def test_bettercontact_probe_blocks_when_out_of_credits(self):
        # The gap that lost a day: a near-zero BC balance parks enrich jobs
        # 'on hold' silently. The probe must catch it from the balance.
        import bettercontact_sync, credit_alerts
        with mock.patch.dict("os.environ", {"BETTERCONTACT_API_KEY": "x"}), \
             mock.patch.object(bettercontact_sync, "account_credits", return_value=0.9), \
             mock.patch.object(credit_alerts, "maybe_low_balance_alert"):
            ok, msg = preflight._bettercontact()
        self.assertFalse(ok)
        self.assertIn("out of credits", msg.lower())

    def test_bettercontact_probe_ok_with_credits(self):
        import bettercontact_sync, credit_alerts
        with mock.patch.dict("os.environ", {"BETTERCONTACT_API_KEY": "x"}), \
             mock.patch.object(bettercontact_sync, "account_credits", return_value=500), \
             mock.patch.object(credit_alerts, "maybe_low_balance_alert") as warn:
            ok, msg = preflight._bettercontact()
        self.assertTrue(ok)
        self.assertIn("500", msg)
        warn.assert_called_once()             # always feeds the low-balance check

    def test_revenue_first_also_gates_on_bettercontact_credits(self):
        with mock.patch.object(preflight, "_anthropic", return_value=(True, "Anthropic OK")), \
             mock.patch.object(preflight, "_rainforest", return_value=(True, "RF OK")), \
             mock.patch.object(preflight, "_bettercontact",
                               return_value=(False, "BetterContact is out of credits (0.9 left)")):
            ok, msgs = preflight.check(revenue_first=True, use_cache=False)
        self.assertFalse(ok)
        self.assertTrue(any("out of credits" in m.lower() for m in msgs))

    def test_cache_reuses_result(self):
        preflight._cache.clear()
        calls = {"n": 0}

        def flaky():
            calls["n"] += 1
            return True, "BC OK"

        with mock.patch.object(preflight, "_anthropic", return_value=(True, "Anthropic OK")), \
             mock.patch.object(preflight, "_bettercontact", side_effect=flaky):
            preflight.check(revenue_first=False, use_cache=True)
            preflight.check(revenue_first=False, use_cache=True)
        self.assertEqual(calls["n"], 1)     # second call served from cache
        preflight._cache.clear()


if __name__ == "__main__":
    unittest.main()
