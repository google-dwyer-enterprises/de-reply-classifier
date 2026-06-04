"""Lead Scrape Automation worker.

Single long-running process. Polls `scrape_requests` every POLL_INTERVAL_S
seconds and drives two state transitions:

  1. status='pending'  → run BC scraper → status='ready' (send email)
  2. status='ready' + approval='approved'
                       → copy rows from prospeo_new_leads into lead_contacts
                       → status='moved'

Both selects use `FOR UPDATE SKIP LOCKED` so multiple workers can coexist
without double-processing the same row.

On startup the worker also sweeps any `status='running'` rows older than
RUNNING_STUCK_THRESHOLD_S back to 'pending' so a crashed mid-run is auto-
recovered on the next loop.

Designed to run as a Railway service. See LEAD_AUTOMATION.md for the deploy
steps and env-var checklist.
"""
from __future__ import annotations

import os
import sys
import time
import traceback
from datetime import datetime, timezone

from dotenv import load_dotenv

load_dotenv()

import bettercontact_sync
from bettercontact_sync import BC_INDUSTRIES, InsufficientCreditsError
from db import connect
import notifier


POLL_INTERVAL_S = int(os.environ.get("WORKER_POLL_INTERVAL_S", "60"))
RUNNING_STUCK_THRESHOLD_S = 60 * 60   # 1 hour: anything older is presumed dead
# Safety margin for the credit budget. observed rate is ~2 credits / accepted
# lead; 3 gives a cushion without letting a runaway burn the account.
CREDITS_PER_LEAD_BUDGET = 3
# Default page size for the BC scraper. 50 balances diversity (cycle covers
# all industries) against round-trip overhead.
DEFAULT_PAGE_LIMIT = 50


# ---------------------------------------------------------------------------
# Startup recovery
# ---------------------------------------------------------------------------

def sweep_stuck_running(conn) -> None:
    """At startup, reset any rows stuck in 'running' for > threshold.

    A worker crashing mid-scrape (OOM, redeploy, etc.) would otherwise leave
    rows pinned in 'running' forever. We treat anything older than
    RUNNING_STUCK_THRESHOLD_S as crashed and demote it back to 'pending' so
    the next poll re-picks it. Idempotent.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            update scrape_requests
               set status        = 'pending',
                   started_at    = null,
                   error_message = coalesce(error_message, '') ||
                                   '[recovered from stuck running at '
                                   || now()::text || ']'
             where status     = 'running'
               and started_at < now() - make_interval(secs => %s)
            returning id
            """,
            (RUNNING_STUCK_THRESHOLD_S,),
        )
        recovered = [r[0] for r in cur.fetchall()]
    conn.commit()
    if recovered:
        log(f"sweep: recovered {len(recovered)} stuck-running rows: {recovered}")


# ---------------------------------------------------------------------------
# pending → ready
# ---------------------------------------------------------------------------

def claim_pending_request(conn) -> dict | None:
    """Atomically pick the oldest pending request and mark it running.

    Returns the row (as a dict) or None if nothing pending.
    Uses FOR UPDATE SKIP LOCKED so other workers don't see it.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            with claimed as (
              select id from scrape_requests
              where status = 'pending'
              order by id
              for update skip locked
              limit 1
            )
            update scrape_requests r
               set status     = 'running',
                   started_at = now()
              from claimed
             where r.id = claimed.id
            returning r.id, r.requested_leads, r.industries, r.skip_industries,
                      r.countries, r.notes
            """
        )
        row = cur.fetchone()
    conn.commit()
    if not row:
        return None
    return {
        "id": row[0],
        "requested_leads": row[1],
        "industries": row[2] or [],
        "skip_industries": row[3] or [],
        "countries": row[4] or [],
        "notes": row[5],
    }


def compute_skip_industries(chosen: list[str], explicit_skip: list[str]) -> list[str]:
    """Translate Jam's selection into the `--skip-industries` arg.

    If `chosen` is empty, she wants ANY industry → skip only the explicit
    excludes. Otherwise skip everything not chosen, plus the explicit excludes.
    """
    chosen_set = set(chosen)
    skip = set(explicit_skip)
    if chosen_set:
        skip.update(set(BC_INDUSTRIES) - chosen_set)
    return sorted(skip)


def collect_stats(conn, request_id: int) -> dict:
    """Sum the scrape's outcome from prospeo_new_leads tagged with this id."""
    with conn.cursor() as cur:
        cur.execute(
            """
            select count(*) filter (where not rejected) as accepted,
                   count(*) filter (where rejected)     as rejected
            from prospeo_new_leads
            where scrape_request_id = %s
            """,
            (request_id,),
        )
        accepted, rejected = cur.fetchone()

        # Top 5 industries by accepted count, for the email body.
        cur.execute(
            """
            select source_industry, count(*) as cnt
            from prospeo_new_leads
            where scrape_request_id = %s and not rejected
            group by source_industry
            order by cnt desc
            limit 5
            """,
            (request_id,),
        )
        by_industry = [(name or "(unknown)", n) for name, n in cur.fetchall()]
    return {"accepted": accepted, "rejected": rejected, "by_industry": by_industry}


def run_scrape(req: dict) -> None:
    """Run the BC scraper for one claimed request.

    Calls bettercontact_sync.main with scrape_request_id set; everything that
    lands in prospeo_new_leads during this run will be tagged.
    """
    skip = compute_skip_industries(req["industries"], req["skip_industries"])
    log(f"req #{req['id']}: scraping target={req['requested_leads']}, "
        f"countries={req['countries']}, skip={skip}")
    bettercontact_sync.main(
        mode="category",
        target_leads=req["requested_leads"],
        country=req["countries"] or None,
        skip_industries=skip,
        page_limit=DEFAULT_PAGE_LIMIT,
        max_credits=req["requested_leads"] * CREDITS_PER_LEAD_BUDGET,
        scrape_request_id=req["id"],
    )


def mark_ready(conn, request_id: int, stats: dict) -> None:
    """Move row to status='ready' after a successful scrape."""
    with conn.cursor() as cur:
        cur.execute(
            """
            update scrape_requests
               set status        = 'ready',
                   ready_at      = now(),
                   scraped_count = %s,
                   credits_spent = coalesce(
                     (select sum(total_credits_spent)
                        from bettercontact_scrape_state),
                     credits_spent)
             where id = %s
            """,
            (stats["accepted"], request_id),
        )
    conn.commit()


def mark_failed(conn, request_id: int, error: str) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            update scrape_requests
               set status        = 'failed',
                   failed_at     = now(),
                   error_message = %s
             where id = %s
            """,
            (error[:8000], request_id),
        )
    conn.commit()


def send_email_and_log(conn, request_id: int, req_dict: dict, stats: dict) -> None:
    """Best-effort: send the ready email, log on failure, never raise."""
    payload = {
        "id": request_id,
        "requested_leads": req_dict["requested_leads"],
        "scraped_count": stats["accepted"],
        "credits_spent": req_dict.get("credits_spent", 0),
        "by_industry": stats["by_industry"],
    }
    ok = notifier.send_batch_ready_email(payload)
    if ok:
        with conn.cursor() as cur:
            cur.execute(
                "update scrape_requests set email_sent_at = now() where id = %s",
                (request_id,),
            )
        conn.commit()
        log(f"req #{request_id}: email sent")
    else:
        log(f"req #{request_id}: email send failed (status still 'ready')")


def process_one_pending_request(conn) -> bool:
    """Pick one pending request, scrape it, mark ready, send email.
    Returns True if work was done, False if the queue was empty."""
    req = claim_pending_request(conn)
    if not req:
        return False
    rid = req["id"]
    log(f"req #{rid}: claimed, status -> running")
    try:
        run_scrape(req)
    except InsufficientCreditsError as e:
        mark_failed(conn, rid, f"BetterContact INSUFFICIENT_CREDITS: {e}")
        notifier.send_failure_email(rid, str(e))
        log(f"req #{rid}: FAILED — insufficient credits")
        return True
    except Exception as e:
        tb = traceback.format_exc()
        mark_failed(conn, rid, tb)
        notifier.send_failure_email(rid, str(e))
        log(f"req #{rid}: FAILED — {e}")
        return True

    stats = collect_stats(conn, rid)
    log(f"req #{rid}: scrape done, accepted={stats['accepted']}, "
        f"rejected={stats['rejected']}")
    mark_ready(conn, rid, stats)
    send_email_and_log(conn, rid, req, stats)
    return True


# ---------------------------------------------------------------------------
# approved → moved
# ---------------------------------------------------------------------------

def claim_approved_request(conn) -> dict | None:
    """Pick the oldest ready+approved request. SKIP LOCKED on contention."""
    with conn.cursor() as cur:
        cur.execute(
            """
            select id from scrape_requests
            where status = 'ready' and approval = 'approved'
            order by id
            for update skip locked
            limit 1
            """
        )
        row = cur.fetchone()
        if not row:
            return None
        request_id = row[0]
    conn.commit()
    return {"id": request_id}


def move_request_to_contacts(conn, request_id: int) -> int:
    """Copy accepted prospeo_new_leads rows for this request into
    lead_contacts. Returns the number of rows actually inserted (after
    ON CONFLICT dedup).

    The mapping intentionally lists columns explicitly so we get a loud
    error if lead_contacts gains a NOT NULL column later (rather than
    silently dropping data).
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            insert into lead_contacts (
              lead_email, first_name, last_name, title, company_name,
              website, industry, lead_list_source, imported_at
            )
            select p.email, p.first_name, p.last_name, p.title, p.company_name,
                   p.company_website, p.source_industry,
                   'BetterContact', now()
            from prospeo_new_leads p
            where p.scrape_request_id = %s
              and p.rejected = false
            on conflict (lead_email) do nothing
            """,
            (request_id,),
        )
        inserted = cur.rowcount

        cur.execute(
            """
            update scrape_requests
               set status      = 'moved',
                   moved_at    = now(),
                   moved_count = %s
             where id = %s
            """,
            (inserted, request_id),
        )
    conn.commit()
    return inserted


def process_one_approved_request(conn) -> bool:
    """Pick one approved request and move it. Returns True if work done."""
    req = claim_approved_request(conn)
    if not req:
        return False
    rid = req["id"]
    log(f"req #{rid}: approved, moving into lead_contacts")
    try:
        moved = move_request_to_contacts(conn, rid)
    except Exception as e:
        tb = traceback.format_exc()
        # Mark failed but leave the rows in prospeo_new_leads so they can be
        # moved manually after the engineer fixes the mapping.
        mark_failed(conn, rid, "move-to-lead_contacts failed:\n" + tb)
        log(f"req #{rid}: MOVE FAILED — {e}")
        return True
    log(f"req #{rid}: moved {moved} rows -> status=moved")
    return True


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def log(msg: str) -> None:
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


def main() -> None:
    log(f"worker starting (poll interval = {POLL_INTERVAL_S}s)")
    conn = connect()
    sweep_stuck_running(conn)
    log("entering poll loop")
    while True:
        try:
            did_work = process_one_pending_request(conn)
            did_work = process_one_approved_request(conn) or did_work
        except Exception as e:
            # Defensive: a transient DB error shouldn't kill the worker.
            log(f"poll error: {e}\n{traceback.format_exc()}")
            try:
                conn.rollback()
            except Exception:
                pass
            try:
                conn.close()
            except Exception:
                pass
            time.sleep(5)
            conn = connect()
            continue
        time.sleep(POLL_INTERVAL_S)


if __name__ == "__main__":
    main()
