"""job_monitor.py — record cron/CLI job runs so failures surface in the admin panel.

The daily cron runs each step as a separate `python run.py <cmd>`. Before this,
a crash in any step (e.g. the backfill-tags statement-timeout on 2026-07-07, or
the verify_supabase count timeout on 2026-07-06) left NO trace in the admin
panel — `api_events` only logs errors from provider API call sites (Anthropic,
Rainforest), not database timeouts and not whether a job ran at all.

`job_run(name)` wraps run.py's dispatch. It records a row per invocation
(running → success/failure + duration + traceback) into `job_run_log`, so the
admin panel can show:
  * did each daily job run, and when (a *missing* run is as visible as a failed one);
  * which jobs are failing, with the traceback;
  * DB timeouts — the failure traceback is classified (a statement timeout →
    kind 'timeout') and also written to `api_event_log`, so it shows in the
    existing tiles/feed alongside API problems.

On a DAILY_JOBS failure it also fires a throttled email (reusing credit_alerts'
Resend sender, which falls back to onboarding@resend.dev so a mis-set NOTIFY_FROM
can't defeat the alert).

Best-effort by design: logging never breaks the wrapped command. The command's
own exception ALWAYS propagates (so the process still exits non-zero and Railway
still marks the deploy crashed) — the monitor only observes.
"""
from __future__ import annotations

import os
import sys
import time
import traceback
from contextlib import contextmanager

# The steps the daily cron runs (see railway.json start command). Only these
# escalate a failure to an api_event + email; other (manual) run.py commands are
# still logged to job_run_log for visibility, but don't page anyone.
DAILY_JOBS = (
    "backfill-tags", "refresh", "refresh-followup-patterns",
    "generate-followup-experiments", "attribute-followup-experiments",
    "resolve-companies", "llm-followup-features",
)

ALERT_THROTTLE_HOURS = 6
RESEND_ENDPOINT = "https://api.resend.com/emails"


# --------------------------------------------------------------------------- #
# Pure logic (unit-tested — no I/O)
# --------------------------------------------------------------------------- #
def outcome_for(exc: BaseException | None) -> tuple[str, bool]:
    """Map a raised exception (or None for a clean exit) to (status, alert).

    - None                       -> ('success', False)
    - SystemExit(0 / None)       -> ('success', False)   (clean sys.exit)
    - KeyboardInterrupt          -> ('failure', False)   (operator aborted; don't page)
    - SystemExit(non-zero) / any -> ('failure', True)
    """
    if exc is None:
        return ("success", False)
    if isinstance(exc, KeyboardInterrupt):
        return ("failure", False)
    if isinstance(exc, SystemExit):
        return ("success", False) if exc.code in (0, None) else ("failure", True)
    return ("failure", True)


# --------------------------------------------------------------------------- #
# Schema
# --------------------------------------------------------------------------- #
def ensure_schema(cur) -> None:
    cur.execute("""
        create table if not exists job_run_log (
            id          bigserial primary key,
            job         text not null,
            started_at  timestamptz not null default now(),
            finished_at timestamptz,
            status      text not null default 'running',
            duration_ms int,
            detail      text,
            host        text
        )
    """)
    cur.execute("create index if not exists job_run_log_started_idx on job_run_log (started_at desc)")
    cur.execute("create index if not exists job_run_log_job_idx on job_run_log (job, started_at desc)")


# --------------------------------------------------------------------------- #
# Write side — all best-effort, never raise
# --------------------------------------------------------------------------- #
def _record_start(job: str) -> int | None:
    try:
        from db import connect
        conn = connect(); conn.autocommit = True
        cur = conn.cursor()
        ensure_schema(cur)
        cur.execute(
            "insert into job_run_log (job, status, host) values (%s,'running',%s) returning id",
            (job, (os.environ.get("RAILWAY_SERVICE_NAME") or os.environ.get("HOSTNAME") or "")[:120] or None),
        )
        rid = cur.fetchone()[0]
        conn.close()
        return rid
    except Exception as e:
        print(f"job_monitor: could not record start for {job}: {e}", file=sys.stderr)
        return None


def _record_finish(run_id: int | None, status: str, detail: str | None, duration_ms: int) -> None:
    if run_id is None:
        return
    try:
        from db import connect
        conn = connect(); conn.autocommit = True
        cur = conn.cursor()
        cur.execute(
            "update job_run_log set status=%s, finished_at=now(), duration_ms=%s, detail=%s where id=%s",
            (status, duration_ms, (detail or "")[:4000] or None, run_id),
        )
        conn.close()
    except Exception as e:
        print(f"job_monitor: could not record finish for run {run_id}: {e}", file=sys.stderr)


def _should_alert(job: str) -> bool:
    """Throttle to one failure email per job per ALERT_THROTTLE_HOURS. Fail-OPEN:
    if the state store is unreachable we still send (a dupe beats silence)."""
    try:
        from db import connect
        conn = connect(); conn.autocommit = True
        cur = conn.cursor()
        cur.execute("""create table if not exists job_alert_state (
                         job text primary key, last_sent_at timestamptz not null)""")
        cur.execute("""select last_sent_at > now() - make_interval(hours => %s)
                         from job_alert_state where job = %s""",
                    (ALERT_THROTTLE_HOURS, job))
        row = cur.fetchone()
        if row and row[0]:
            conn.close()
            return False
        cur.execute("""insert into job_alert_state (job, last_sent_at) values (%s, now())
                       on conflict (job) do update set last_sent_at = now()""", (job,))
        conn.close()
        return True
    except Exception as e:
        print(f"job_monitor: alert throttle unavailable ({e}); sending anyway", file=sys.stderr)
        return True


def _send_failure_email(job: str, detail: str) -> bool:
    import requests
    api_key = (os.environ.get("RESEND_API_KEY") or "").strip()
    to = (os.environ.get("NOTIFY_EMAIL") or "").strip()
    try:
        from credit_alerts import _resolve_sender
        sender = _resolve_sender()
    except Exception:
        sender = "Pipeline Monitor <onboarding@resend.dev>"
    if not (api_key and to):
        print(f"job_monitor: RESEND_API_KEY/NOTIFY_EMAIL not set — would alert about "
              f"failed job '{job}': {detail[:120]}", file=sys.stderr)
        return False
    tail = (detail or "").strip()[-1200:]
    subject = f"❌ Daily cron job failed: {job}"
    html = f"""\
<div style="font-family:-apple-system,Segoe UI,Roboto,sans-serif;max-width:620px;
            margin:0 auto;padding:24px;line-height:1.55;color:#1a1a1a;">
  <p>The daily-cron step <strong>{job}</strong> failed on its last run.</p>
  <pre style="background:#fef2f2;border:1px solid #fecaca;padding:12px;border-radius:6px;
            font-size:12px;white-space:pre-wrap;overflow-x:auto;">{tail}</pre>
  <p style="color:#6b7280;font-size:13px;">See the admin panel → Daily cron health for the
     full history. (You won't get another '{job}' alert for {ALERT_THROTTLE_HOURS}h.)</p>
  <p style="color:#6b7280;font-size:13px;">— Pipeline monitor</p>
</div>"""
    try:
        r = requests.post(RESEND_ENDPOINT,
                          json={"from": sender, "to": [to], "subject": subject, "html": html},
                          headers={"Authorization": f"Bearer {api_key}",
                                   "Content-Type": "application/json"},
                          timeout=30)
        if 200 <= r.status_code < 300:
            print(f"job_monitor: emailed {to} about failed job '{job}'")
            return True
        print(f"job_monitor: Resend {r.status_code}: {r.text[:200]}", file=sys.stderr)
        return False
    except Exception as e:
        print(f"job_monitor: failure-email send failed: {e}", file=sys.stderr)
        return False


def _on_failure(job: str, detail: str) -> None:
    """Escalate a DAILY_JOBS failure: log to api_event_log (classified) + email.
    Manual (non-daily) commands are logged in job_run_log only, no page."""
    if job not in DAILY_JOBS:
        return
    # 1) surface in the existing api-health feed/tiles, classified (timeout/etc)
    try:
        import api_events
        kind = api_events.classify_error(None, detail)
        api_events.record("Cron", kind, detail=detail, context=job)
    except Exception as e:
        print(f"job_monitor: could not record api_event for {job}: {e}", file=sys.stderr)
    # 2) email (throttled)
    try:
        if _should_alert(job):
            _send_failure_email(job, detail)
    except Exception as e:
        print(f"job_monitor: alert path error for {job}: {e}", file=sys.stderr)


@contextmanager
def job_run(job: str):
    """Wrap a job invocation. Records start/finish; escalates daily-job failures.
    The wrapped block's exception ALWAYS propagates unchanged."""
    run_id = _record_start(job)
    t0 = time.time()
    exc: BaseException | None = None
    try:
        yield
    except BaseException as e:   # noqa: BLE001 — observe everything, then re-raise
        exc = e
        raise
    finally:
        status, alert = outcome_for(exc)
        detail = None
        if status == "failure":
            detail = "".join(traceback.format_exception(exc)) if exc else "unknown failure"
        _record_finish(run_id, status, detail, int((time.time() - t0) * 1000))
        if alert:
            _on_failure(job, detail or "")


# --------------------------------------------------------------------------- #
# Read side (admin panel)
# --------------------------------------------------------------------------- #
def recent_runs(limit: int = 60) -> list[dict]:
    import psycopg2.extras
    from db import connect
    conn = connect()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        ensure_schema(cur); conn.commit()
        cur.execute("""select job, started_at, finished_at, status, duration_ms, detail
                         from job_run_log order by started_at desc limit %s""", (limit,))
        return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()


def daily_health(stale_hours: int = 26) -> list[dict]:
    """One row per DAILY_JOBS entry: its last run (any status) + last success +
    a `stale` flag (no success within stale_hours, which also catches a job that
    never ran today). Ordered by worst-first (failing/stale, then ok)."""
    import psycopg2.extras
    from db import connect
    conn = connect()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        ensure_schema(cur); conn.commit()
        jobs = list(DAILY_JOBS)
        cur.execute("""
            select distinct on (job) job, status, started_at, finished_at, duration_ms, detail
              from job_run_log where job = any(%s) order by job, started_at desc
        """, (jobs,))
        last = {r["job"]: dict(r) for r in cur.fetchall()}
        cur.execute("""
            select job, max(started_at) last_success
              from job_run_log where job = any(%s) and status = 'success' group by job
        """, (jobs,))
        last_success = {r["job"]: r["last_success"] for r in cur.fetchall()}
        cur.execute("select now() - make_interval(hours => %s) as cutoff", (stale_hours,))
        cutoff = cur.fetchone()["cutoff"]
    finally:
        conn.close()

    out = []
    for job in jobs:
        lr = last.get(job)
        ls = last_success.get(job)
        stale = (ls is None) or (ls < cutoff)
        out.append({
            "job": job,
            "last_status": (lr or {}).get("status") or "never run",
            "last_started": (lr or {}).get("started_at"),
            "last_duration_ms": (lr or {}).get("duration_ms"),
            "last_detail": (lr or {}).get("detail"),
            "last_success": ls,
            "stale": stale,
            "ok": (not stale) and (lr or {}).get("status") == "success",
        })
    # worst first: not-ok before ok
    out.sort(key=lambda r: (r["ok"], r["job"]))
    return out


if __name__ == "__main__":
    # Manual smoke test: `python job_monitor.py [--fail]`
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--fail", action="store_true")
    ap.add_argument("--job", default="backfill-tags")
    a = ap.parse_args()
    from dotenv import load_dotenv; load_dotenv()
    try:
        with job_run(a.job):
            if a.fail:
                raise RuntimeError("TEST — simulated statement timeout")
            print("ok")
    except RuntimeError:
        print("(expected test failure propagated)")
    print("recent:", recent_runs(3))
