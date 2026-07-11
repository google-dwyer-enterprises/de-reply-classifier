"""Tier-3 revenue-first enrichment queue — ASYNC submit/collect drain.

The gate persists survivors into revenue_first_enrich_queue; the drain SUBMITS
pending rows to BetterContact (fast POST -> bc_request_id) and COLLECTS results
on later polls (quick status GET), so a slow/hung BC enrich never blocks the
worker loop. These lock the pure, high-regression bits: the contact key, and the
drain's state machine (pending -> submitted -> enriched/skipped, aged-out ->
pending/failed, budget stop). BC network calls are mocked and the DB is a small
in-memory fake that actually applies the status transitions, so the counts the
drain returns are real. Fast stdlib-unittest, no network, no DB.
"""
import unittest
from unittest import mock
from datetime import datetime, timezone, timedelta

import bettercontact_sync as bc


class TestContactKey(unittest.TestCase):
    def test_normalizes_and_joins(self):
        lead = {"first_name": " Jane ", "last_name": "DOE",
                "company_domain": "Brand.COM"}
        self.assertEqual(bc._contact_key(lead), "jane|doe|brand.com")

    def test_missing_fields_are_empty_segments(self):
        self.assertEqual(bc._contact_key({"first_name": "Sam"}), "sam||")

    def test_matches_across_case(self):
        a = bc._contact_key({"first_name": "A", "last_name": "B", "company_domain": "x.com"})
        b = bc._contact_key({"first_name": "a", "last_name": "b", "company_domain": "X.com"})
        self.assertEqual(a, b)


# --- tiny in-memory queue-table fake -------------------------------------

class _State:
    def __init__(self, rows):
        # rows: list of dicts with id/status/lead_json/bc_request_id/submitted_at/attempts
        self.rows = {r["id"]: r for r in rows}
        self.inserted = 0


class _Cursor:
    def __init__(self, state):
        self.state = state
        self.rowcount = 0
        self._one = None
        self._all = []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    @staticmethod
    def _n(sql):
        return " ".join(sql.lower().split())

    def execute(self, sql, params=None):
        s = self._n(sql)
        rows = self.state.rows
        self._one, self._all = None, []
        if "count(*) filter (where status='pending')" in s:
            p = sum(1 for r in rows.values() if r["status"] == "pending")
            sub = sum(1 for r in rows.values() if r["status"] == "submitted")
            self._one = (p, sub)
        elif "select id, lead_json, bc_request_id, submitted_at" in s:      # COLLECT
            self._all = [(r["id"], r["lead_json"], r["bc_request_id"], r["submitted_at"])
                         for r in rows.values() if r["status"] == "submitted"]
        elif "select id, lead_json from revenue_first_enrich_queue" in s and "status = 'pending'" in s:
            limit = params[-1]
            pend = sorted((r for r in rows.values() if r["status"] == "pending"),
                          key=lambda r: r["id"])
            self._all = [(r["id"], r["lead_json"]) for r in pend[:limit]]
        elif "set status='skipped'" in s:
            for i in params[0]:
                rows[i]["status"] = "skipped"
        elif "set attempts = attempts + 1" in s:                            # aged-out reset
            mx, ids = params[0], params[1]
            for i in ids:
                rows[i]["attempts"] += 1
                rows[i]["status"] = "failed" if rows[i]["attempts"] >= mx else "pending"
                rows[i]["bc_request_id"] = None
                rows[i]["submitted_at"] = None
        elif "set status='submitted'" in s:
            rid, ids = params[0], params[1]
            for i in ids:
                rows[i]["status"] = "submitted"
                rows[i]["bc_request_id"] = rid
                rows[i]["submitted_at"] = datetime.now(timezone.utc)
        elif "and status = 'failed'" in s:
            ids = params[0]
            self._one = (sum(1 for i in ids if rows.get(i, {}).get("status") == "failed"),)

    def executemany(self, sql, argslist):
        s = self._n(sql)
        argslist = list(argslist)
        if "insert into prospeo_new_leads" in s:
            self.state.inserted += len(argslist)
            self.rowcount = len(argslist)
        elif "set status='enriched'" in s:      # (lead_json, id) pairs
            for _lj, i in argslist:
                self.state.rows[i]["status"] = "enriched"
                self.state.rows[i]["bc_request_id"] = None

    def fetchone(self):
        return self._one

    def fetchall(self):
        return self._all


class _Conn:
    def __init__(self, state):
        self.state = state

    def cursor(self):
        return _Cursor(self.state)

    def commit(self):
        pass


def _row(rid, status, first, last="x", domain="brand.com",
         bc_request_id=None, submitted_at=None, attempts=0):
    return {"id": rid, "status": status, "attempts": attempts,
            "bc_request_id": bc_request_id, "submitted_at": submitted_at,
            "lead_json": {"first_name": first, "last_name": last,
                          "company_domain": domain, "company_name": "Brand"}}


class TestAsyncDrain(unittest.TestCase):
    def test_submit_marks_pending_rows_submitted(self):
        state = _State([_row(1, "pending", "jane"), _row(2, "pending", "sam")])
        conn = _Conn(state)
        with mock.patch.object(bc, "enrich_submit", return_value="req-1") as sub, \
             mock.patch.object(bc, "enrich_poll_once") as poll:
            d = bc.drain_enrich_queue(conn, "k", scrape_request_id=7)
        sub.assert_called_once()
        poll.assert_not_called()               # nothing in flight to collect yet
        self.assertEqual(d["submitted"], 2)
        self.assertEqual(d["in_flight"], 2)
        self.assertEqual(d["still_pending"], 0)
        self.assertEqual(d["enriched"], 0)
        self.assertTrue(all(r["bc_request_id"] == "req-1" for r in state.rows.values()))

    def test_collect_terminated_maps_outcomes(self):
        now = datetime.now(timezone.utc)
        state = _State([
            _row(1, "submitted", "jane", domain="a.com", bc_request_id="R", submitted_at=now),
            _row(2, "submitted", "info", domain="b.com", bc_request_id="R", submitted_at=now),
            _row(3, "submitted", "no",   domain="c.com", bc_request_id="R", submitted_at=now),
        ])
        conn = _Conn(state)
        result = {"credits_consumed": 2, "data": [
            {"contact_first_name": "jane", "contact_last_name": "x", "company_domain": "a.com",
             "contact_email_address": "jane@a.com", "contact_email_address_status": "deliverable"},
            {"contact_first_name": "info", "contact_last_name": "x", "company_domain": "b.com",
             "contact_email_address": "info@b.com", "contact_email_address_status": "deliverable"},
            {"contact_first_name": "no", "contact_last_name": "x", "company_domain": "c.com",
             "contact_email_address": "", "contact_email_address_status": "unknown"},
        ]}
        with mock.patch.object(bc, "enrich_poll_once", return_value=result), \
             mock.patch.object(bc, "enrich_submit") as sub:
            d = bc.drain_enrich_queue(conn, "k", scrape_request_id=7)
        sub.assert_not_called()                       # nothing pending to submit
        self.assertEqual(d["enriched"], 1)            # jane
        self.assertEqual(d["skipped"], 2)             # info@ generic + undeliverable
        self.assertEqual(state.inserted, 1)           # only jane written to prospeo
        self.assertEqual(d["in_flight"], 0)
        self.assertEqual(d["still_pending"], 0)
        self.assertEqual(state.rows[1]["status"], "enriched")
        self.assertEqual(state.rows[2]["status"], "skipped")
        self.assertEqual(state.rows[3]["status"], "skipped")

    def test_not_terminated_recent_stays_in_flight(self):
        now = datetime.now(timezone.utc)
        state = _State([_row(1, "submitted", "jane", bc_request_id="R", submitted_at=now)])
        conn = _Conn(state)
        with mock.patch.object(bc, "enrich_poll_once", return_value=None), \
             mock.patch.object(bc, "enrich_submit"):
            d = bc.drain_enrich_queue(conn, "k", scrape_request_id=7)
        self.assertEqual(d["in_flight"], 1)           # left submitted
        self.assertEqual(d["enriched"], 0)
        self.assertEqual(state.rows[1]["status"], "submitted")

    def test_aged_out_in_flight_resets_to_pending(self):
        old = datetime.now(timezone.utc) - timedelta(seconds=bc.ENRICH_INFLIGHT_MAX_AGE_S + 60)
        state = _State([_row(1, "submitted", "jane", bc_request_id="R", submitted_at=old)])
        conn = _Conn(state)
        with mock.patch.object(bc, "enrich_poll_once", return_value=None), \
             mock.patch.object(bc, "enrich_submit", return_value="R2") as sub:
            d = bc.drain_enrich_queue(conn, "k", scrape_request_id=7)
        # aged out -> reset to pending -> resubmitted in the same call's SUBMIT phase
        self.assertEqual(state.rows[1]["attempts"], 1)
        sub.assert_called_once()
        self.assertEqual(d["in_flight"], 1)           # resubmitted under R2

    def test_budget_cap_blocks_submit(self):
        state = _State([_row(1, "pending", "jane")])
        conn = _Conn(state)
        with mock.patch.object(bc, "enrich_submit") as sub, \
             mock.patch.object(bc, "enrich_poll_once"):
            d = bc.drain_enrich_queue(conn, "k", scrape_request_id=7,
                                      max_credits=5, credits_already_spent=5)
        sub.assert_not_called()
        self.assertIn("budget cap", d["aborted_reason"].lower())
        self.assertEqual(state.rows[1]["status"], "pending")

    def test_insufficient_credits_on_submit_aborts(self):
        state = _State([_row(1, "pending", "jane")])
        conn = _Conn(state)
        with mock.patch.object(bc, "enrich_submit",
                               side_effect=bc.InsufficientCreditsError("no creds")), \
             mock.patch.object(bc, "enrich_poll_once"):
            d = bc.drain_enrich_queue(conn, "k", scrape_request_id=7)
        self.assertIn("no creds", d["aborted_reason"])
        self.assertEqual(state.rows[1]["status"], "pending")   # not submitted


if __name__ == "__main__":
    unittest.main()
