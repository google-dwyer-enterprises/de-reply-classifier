"""Tests for job_monitor's pure logic — the failure-outcome classifier — and the
statement-timeout classification that makes a DB timeout show up in the admin
panel. No network, no DB.

These guard the two decisions that matter for the monitor's correctness:
  1. outcome_for: which raised conditions count as a failure worth alerting
     (a non-zero sys.exit or any exception) vs a clean/aborted run (exit 0,
     Ctrl-C) that must NOT page anyone.
  2. that the exact production error — 'canceling statement due to statement
     timeout' — is classified as a 'timeout' so it lands in the right bucket.
"""
import unittest

import api_events
import job_monitor as jm


class TestOutcomeFor(unittest.TestCase):
    def test_clean_exit_is_success(self):
        self.assertEqual(jm.outcome_for(None), ("success", False))

    def test_sysexit_zero_is_success(self):
        self.assertEqual(jm.outcome_for(SystemExit(0)), ("success", False))
        self.assertEqual(jm.outcome_for(SystemExit(None)), ("success", False))

    def test_sysexit_nonzero_is_alerting_failure(self):
        # run.py's run_script() does sys.exit("... aborting.") -> code is a str
        self.assertEqual(jm.outcome_for(SystemExit("boom")), ("failure", True))
        self.assertEqual(jm.outcome_for(SystemExit(1)), ("failure", True))

    def test_keyboardinterrupt_is_failure_but_no_alert(self):
        # operator aborted a manual run — record it, don't page
        self.assertEqual(jm.outcome_for(KeyboardInterrupt()), ("failure", False))

    def test_generic_exception_is_alerting_failure(self):
        self.assertEqual(jm.outcome_for(RuntimeError("x")), ("failure", True))
        self.assertEqual(jm.outcome_for(ValueError("x")), ("failure", True))


class TestStatementTimeoutClassified(unittest.TestCase):
    def test_statement_timeout_maps_to_timeout(self):
        # the exact PostgREST/psycopg2 message from the 2026-07-07 cron crash
        msg = ("postgrest.exceptions.APIError: {'message': 'canceling statement "
               "due to statement timeout', 'code': '57014'}")
        self.assertEqual(api_events.classify_error(None, msg), "timeout")


class TestDailyJobsCoverage(unittest.TestCase):
    def test_known_cron_steps_are_daily_jobs(self):
        # the steps in railway.json's cron start command must escalate on failure
        for job in ("backfill-tags", "refresh", "resolve-companies",
                    "llm-followup-features"):
            self.assertIn(job, jm.DAILY_JOBS)


if __name__ == "__main__":
    unittest.main()
