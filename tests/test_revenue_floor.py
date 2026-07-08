"""The per-request revenue floor threads from the worker to the scraper.

Pure (no DB/network): worker.run_scrape must pass the request's `revenue_floor`
through to bettercontact_sync.main (None -> the $300k default is applied inside
main). The DB + HTTP round-trip is exercised by a live integration check at
build time; this locks the worker-side plumbing so a refactor can't drop it.
"""
import unittest
from unittest import mock

import worker


def _req(floor):
    return {"id": 1, "requested_leads": 10, "industries": [], "skip_industries": [],
            "countries": ["United States"], "notes": None, "max_credits": 100,
            "enrichment": "email", "revenue_floor": floor}


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


if __name__ == "__main__":
    unittest.main()
