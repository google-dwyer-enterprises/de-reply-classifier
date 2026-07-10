"""enrich_contacts_resilient — chunked, retrying BC enrichment.

BetterContact's async enrich is intermittently slow (600s poll hangs killed
batch #47, stalled #48/#49). The resilient wrapper chunks survivors, uses a
short per-attempt timeout, retries a timed-out chunk, keeps the chunks that
succeed, and only signals failure (RuntimeError) if EVERY chunk failed — so the
runner's abort guard fires on a real BC outage but a single hang doesn't lose a
whole page. These lock that behavior (pure, all BC calls mocked).
"""
import unittest
from unittest import mock

import bettercontact_sync as bc


class TestEnrichResilient(unittest.TestCase):
    def test_chunks_and_aggregates(self):
        calls = []

        def fake(part, key, enrich_phones=False, timeout_s=None):
            calls.append(len(part))
            return {"data": [{"contact_email_address": "a@b.com"} for _ in part],
                    "credits_consumed": len(part)}

        with mock.patch.object(bc, "enrich_contacts", side_effect=fake):
            r = bc.enrich_contacts_resilient([{"first_name": str(i)} for i in range(12)], "k")
        self.assertEqual(calls, [5, 5, 2])          # chunked by BC_ENRICH_CHUNK=5
        self.assertEqual(len(r["data"]), 12)
        self.assertEqual(r["credits_consumed"], 12)

    def test_retries_a_timed_out_chunk_then_succeeds(self):
        n = {"i": 0}

        def fake(part, key, enrich_phones=False, timeout_s=None):
            n["i"] += 1
            if n["i"] == 1:
                raise RuntimeError("BC enrich poll timeout after 90s")
            return {"data": [{"x": 1}], "credits_consumed": 1}

        with mock.patch.object(bc, "enrich_contacts", side_effect=fake):
            r = bc.enrich_contacts_resilient([{"first_name": "a"}], "k")
        self.assertEqual(n["i"], 2)                 # retried once, then ok
        self.assertEqual(r["credits_consumed"], 1)

    def test_all_chunks_fail_raises_for_abort_guard(self):
        with mock.patch.object(bc, "enrich_contacts",
                               side_effect=RuntimeError("timeout")):
            with self.assertRaises(RuntimeError):
                bc.enrich_contacts_resilient([{"first_name": "a"}], "k")

    def test_partial_success_does_not_raise(self):
        # chunk 1 ok, chunk 2 always times out -> keep chunk 1, no raise
        def fake(part, key, enrich_phones=False, timeout_s=None):
            if part[0]["first_name"] == "bad":
                raise RuntimeError("timeout")
            return {"data": [{"x": 1}], "credits_consumed": 1}

        leads = [{"first_name": "ok"}] * 5 + [{"first_name": "bad"}] * 5
        with mock.patch.object(bc, "enrich_contacts", side_effect=fake):
            r = bc.enrich_contacts_resilient(leads, "k")
        self.assertEqual(r["credits_consumed"], 1)   # only the good chunk

    def test_insufficient_credits_propagates(self):
        with mock.patch.object(bc, "enrich_contacts",
                               side_effect=bc.InsufficientCreditsError("no creds")):
            with self.assertRaises(bc.InsufficientCreditsError):
                bc.enrich_contacts_resilient([{"first_name": "a"}], "k")


if __name__ == "__main__":
    unittest.main()
