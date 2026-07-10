"""The per-request revenue floor threads from the worker to the scraper.

Pure (no DB/network): worker.run_scrape must pass the request's `revenue_floor`
through to bettercontact_sync.main (None -> the $300k default is applied inside
main). The DB + HTTP round-trip is exercised by a live integration check at
build time; this locks the worker-side plumbing so a refactor can't drop it.
"""
import unittest
from unittest import mock

import worker


def _req(floor, revenue_first=False, requested_leads=10, amazon_qa_max_credits=None):
    return {"id": 1, "requested_leads": requested_leads, "industries": [],
            "skip_industries": [], "countries": ["United States"], "notes": None,
            "max_credits": 100, "enrichment": "email", "revenue_floor": floor,
            "revenue_first": revenue_first, "amazon_qa_max_credits": amazon_qa_max_credits}


class TestRunScrapeThreadsFloor(unittest.TestCase):
    def test_explicit_floor_threaded(self):
        with mock.patch.object(worker.bettercontact_sync, "main", return_value={}) as m:
            worker.run_scrape(_req(1_000_000))
        self.assertEqual(m.call_args.kwargs.get("revenue_floor"), 1_000_000)

    def test_missing_floor_passes_none(self):
        # a request with no floor set passes None -> main() coalesces to $300k
        with mock.patch.object(worker.bettercontact_sync, "main", return_value={}) as m:
            worker.run_scrape(_req(None))
        self.assertIsNone(m.call_args.kwargs.get("revenue_floor"))

    def test_classic_request_does_not_set_revenue_first(self):
        # a normal batch must NOT flip into the revenue-first flow
        with mock.patch.object(worker.bettercontact_sync, "main", return_value={}) as m:
            worker.run_scrape(_req(None))
        self.assertNotIn("revenue_first", m.call_args.kwargs)
        self.assertNotIn("amazon_qa_max_credits", m.call_args.kwargs)

    def test_revenue_first_threaded_with_capped_rainforest(self):
        # revenue_first=True -> threads the flag + a per-batch Rainforest cap
        # (~6 credits/target lead, floor 150). 50 leads -> 300.
        with mock.patch.object(worker.bettercontact_sync, "main", return_value={}) as m:
            worker.run_scrape(_req(None, revenue_first=True, requested_leads=50))
        self.assertTrue(m.call_args.kwargs.get("revenue_first"))
        self.assertEqual(m.call_args.kwargs.get("amazon_qa_max_credits"), 300)

    def test_explicit_rainforest_cap_overrides_formula(self):
        # an explicit per-request cap (e.g. 2000 for a big validation run) wins
        # over the ~6/target-lead formula
        with mock.patch.object(worker.bettercontact_sync, "main", return_value={}) as m:
            worker.run_scrape(_req(None, revenue_first=True, requested_leads=50,
                                   amazon_qa_max_credits=2000))
        self.assertEqual(m.call_args.kwargs.get("amazon_qa_max_credits"), 2000)


if __name__ == "__main__":
    unittest.main()
