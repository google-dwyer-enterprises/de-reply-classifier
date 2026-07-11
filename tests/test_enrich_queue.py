"""Tier-3 decoupled revenue-first — the enrichment queue (gate/drain split).

The GATE phase persists gated survivors into revenue_first_enrich_queue; the
DRAIN phase enriches pending rows and writes the accepted leads. These lock the
pure, high-regression bits: the contact key (dedup + result matching) and the
drain's per-row outcome mapping (deliverable→enriched, no-usable-email→skipped,
chunk-timeout→stays pending, budget-cap→no spend). All BC calls are mocked and
the DB is a tiny in-memory fake, so this stays a fast stdlib-unittest unit test.
"""
import unittest
from unittest import mock

import bettercontact_sync as bc


class TestContactKey(unittest.TestCase):
    def test_normalizes_and_joins(self):
        lead = {"first_name": " Jane ", "last_name": "DOE",
                "company_domain": "Brand.COM"}
        self.assertEqual(bc._contact_key(lead), "jane|doe|brand.com")

    def test_missing_fields_are_empty_segments(self):
        self.assertEqual(bc._contact_key({"first_name": "Sam"}), "sam||")

    def test_matches_across_case(self):
        a = bc._contact_key({"first_name": "A", "last_name": "B",
                             "company_domain": "x.com"})
        b = bc._contact_key({"first_name": "a", "last_name": "b",
                             "company_domain": "X.com"})
        self.assertEqual(a, b)


class _FakeCursor:
    """Minimal cursor: routes the handful of SQL statements drain_enrich_queue
    issues to canned results / recorded side effects on the shared state."""

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
    def _norm(sql):
        return " ".join(sql.lower().split())

    def execute(self, sql, params=None):
        s = self._norm(sql)
        if "count(*)" in s and "status = 'pending'" in s:
            self._one = (self.state.get("pending_after", 0),)
        elif "count(*)" in s and "status = 'failed'" in s:
            self._one = (self.state.get("failed_after", 0),)
        elif "select id, lead_json" in s and "status = 'pending'" in s:
            self._all = list(self.state["pending"])
        elif "set status='skipped'" in s:
            self.state.setdefault("skipped_ids", []).extend(params[0])
        elif "attempts = attempts + 1" in s:
            self.state.setdefault("pending_bumped", []).extend(params[-1])

    def executemany(self, sql, rows):
        s = self._norm(sql)
        rows = list(rows)
        if "insert into prospeo_new_leads" in s:
            self.state["inserted"] = self.state.get("inserted", 0) + len(rows)
            self.rowcount = len(rows)
        elif "set status='enriched'" in s:
            self.state["enriched_updates"] = rows

    def fetchone(self):
        return self._one

    def fetchall(self):
        return self._all


class _FakeConn:
    def __init__(self, state):
        self.state = state

    def cursor(self):
        return _FakeCursor(self.state)

    def commit(self):
        pass


def _pending_rows(*leads):
    return [(i + 1, dict(lead)) for i, lead in enumerate(leads)]


class TestDrainOutcomeMapping(unittest.TestCase):
    def _lead(self, first, last="x", domain="brand.com"):
        return {"first_name": first, "last_name": last,
                "company_domain": domain, "company_name": "Brand"}

    def test_deliverable_personal_email_is_enriched(self):
        state = {"pending": _pending_rows(self._lead("jane")), "pending_after": 0}
        conn = _FakeConn(state)

        def fake_enrich(part, key, enrich_phones=False, timeout_s=None):
            return {"credits_consumed": 1, "data": [{
                "contact_first_name": "jane", "contact_last_name": "x",
                "company_domain": "brand.com",
                "contact_email_address": "jane@brand.com",
                "contact_email_address_status": "deliverable"}]}

        with mock.patch.object(bc, "enrich_contacts", side_effect=fake_enrich):
            r = bc.drain_enrich_queue(conn, "k", scrape_request_id=7)
        self.assertEqual(r["enriched"], 1)
        self.assertEqual(r["skipped"], 0)
        self.assertEqual(state.get("inserted"), 1)          # written to prospeo
        self.assertEqual(len(state["enriched_updates"]), 1)  # queue row flipped
        self.assertEqual(r["credits_spent"], 1)

    def test_undeliverable_is_skipped_not_lost(self):
        state = {"pending": _pending_rows(self._lead("no")), "pending_after": 0}
        conn = _FakeConn(state)

        def fake_enrich(part, key, enrich_phones=False, timeout_s=None):
            return {"credits_consumed": 0, "data": [{
                "contact_first_name": "no", "contact_last_name": "x",
                "company_domain": "brand.com",
                "contact_email_address": "", "contact_email_address_status": "unknown"}]}

        with mock.patch.object(bc, "enrich_contacts", side_effect=fake_enrich):
            r = bc.drain_enrich_queue(conn, "k", scrape_request_id=7)
        self.assertEqual(r["enriched"], 0)
        self.assertEqual(r["skipped"], 1)
        self.assertEqual(state.get("skipped_ids"), [1])
        self.assertIsNone(state.get("inserted"))

    def test_generic_mailbox_is_skipped(self):
        state = {"pending": _pending_rows(self._lead("info")), "pending_after": 0}
        conn = _FakeConn(state)

        def fake_enrich(part, key, enrich_phones=False, timeout_s=None):
            return {"credits_consumed": 1, "data": [{
                "contact_first_name": "info", "contact_last_name": "x",
                "company_domain": "brand.com",
                "contact_email_address": "info@brand.com",   # role mailbox
                "contact_email_address_status": "deliverable"}]}

        with mock.patch.object(bc, "enrich_contacts", side_effect=fake_enrich):
            r = bc.drain_enrich_queue(conn, "k", scrape_request_id=7)
        self.assertEqual(r["enriched"], 0)
        self.assertEqual(r["skipped"], 1)

    def test_chunk_timeout_leaves_rows_pending(self):
        state = {"pending": _pending_rows(self._lead("jane")),
                 "pending_after": 1}    # still pending after the failed drain
        conn = _FakeConn(state)

        with mock.patch.object(bc, "enrich_contacts",
                               side_effect=RuntimeError("BC enrich poll timeout")):
            r = bc.drain_enrich_queue(conn, "k", scrape_request_id=7)
        self.assertEqual(r["enriched"], 0)
        self.assertEqual(r["skipped"], 0)
        self.assertEqual(state.get("pending_bumped"), [1])   # attempts++ for retry
        self.assertEqual(r["still_pending"], 1)

    def test_insufficient_credits_aborts_and_keeps_rows(self):
        state = {"pending": _pending_rows(self._lead("jane")), "pending_after": 1}
        conn = _FakeConn(state)

        with mock.patch.object(bc, "enrich_contacts",
                               side_effect=bc.InsufficientCreditsError("no creds")):
            r = bc.drain_enrich_queue(conn, "k", scrape_request_id=7)
        self.assertIn("no creds", r["aborted_reason"])
        self.assertEqual(r["enriched"], 0)

    def test_budget_cap_pulls_nothing(self):
        # credits_already_spent >= max_credits -> no rows pulled, no BC call.
        state = {"pending": _pending_rows(self._lead("jane")), "pending_after": 1}
        conn = _FakeConn(state)
        with mock.patch.object(bc, "enrich_contacts") as m:
            r = bc.drain_enrich_queue(conn, "k", scrape_request_id=7,
                                      max_credits=10, credits_already_spent=10)
        m.assert_not_called()
        self.assertIn("budget cap", r["aborted_reason"].lower())
        self.assertEqual(r["enriched"], 0)


if __name__ == "__main__":
    unittest.main()
