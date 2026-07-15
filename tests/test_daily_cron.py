"""daily-cron runs the whole daily sequence behind ONE Railway start command.

A multi-command `;`-chain start command silently runs only its FIRST step on
Railway (the Dockerfile's exec-form CMD means the start command isn't shell-
parsed), which is why the follow-up steps never ran from cron. `run.py daily-cron`
sequences every step inside one command instead. Guard the invariants:
  1. the cron step set == job_monitor.DAILY_JOBS — every step pages on failure,
     and every paging job is actually run (catches future drift between the two);
  2. refresh leads (it feeds every downstream step's data);
  3. --dry-run executes nothing;
  4. a failing step does NOT abort the rest (continue-on-error, better than `;`).
No network, no DB, no real subprocess.
"""
import unittest
from unittest import mock

import run
import job_monitor


class TestDailyCronSteps(unittest.TestCase):
    def test_step_set_matches_daily_jobs(self):
        cron = {s[0] for s in run.DAILY_CRON_STEPS}
        self.assertEqual(
            cron, set(job_monitor.DAILY_JOBS),
            "daily-cron steps and job_monitor.DAILY_JOBS have drifted — "
            "every cron step should page on failure and vice-versa",
        )

    def test_refresh_runs_first(self):
        self.assertEqual(run.DAILY_CRON_STEPS[0], ["refresh"])

    def test_dry_run_executes_nothing(self):
        args = mock.Mock(dry_run=True)
        with mock.patch.object(run.subprocess, "run") as sp:
            run.cmd_daily_cron(args)
        sp.assert_not_called()

    def test_live_runs_every_step_and_continues_past_failure(self):
        args = mock.Mock(dry_run=False)
        calls = []

        def fake_run(cmd, timeout=None):
            calls.append(cmd)
            rc = 1 if "backfill-tags" in cmd else 0   # one step fails
            return mock.Mock(returncode=rc)

        with mock.patch.object(run.subprocess, "run", side_effect=fake_run):
            run.cmd_daily_cron(args)                   # must NOT raise

        # cmd == [sys.executable, "run.py", <step>, ...] -> index 2 is the step
        ran = [c[2] for c in calls]
        self.assertEqual(len(calls), len(run.DAILY_CRON_STEPS))
        self.assertEqual(ran[0], "refresh")
        self.assertIn("backfill-tags", ran)           # reached despite earlier success/failure mix


if __name__ == "__main__":
    unittest.main()
