"""Lead Scrape Automation worker.

Single long-running process. Polls every POLL_INTERVAL_S seconds and drives:

  1. status='pending'  → run BC scraper → status='ready' (send email).
  2. For each 'ready' request with leads that have lead_approval='approved'
     and lead_moved_at IS NULL: copy those leads into lead_contacts and
     stamp lead_moved_at=now(). Jam approves/rejects leads one-at-a-time
     in the NocoDB per-batch grid; the worker keeps moving newly-approved
     leads until the batch is fully decided.
  3. When every lead for a 'ready' request has a decision (lead_approval !=
     'pending') AND every approved lead has been moved, the worker auto-
     finalizes the request to status='moved'.

All claims use `FOR UPDATE SKIP LOCKED` so multiple workers can coexist
without double-processing the same row.

On startup the worker also sweeps any `status='running'` rows older than
RUNNING_STUCK_THRESHOLD_S back to 'pending' so a crash mid-scrape is auto-
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

# bettercontact_sync prints unicode arrows in its progress output; on Windows
# the default cp1252 stdout would raise UnicodeEncodeError and crash the
# pending-cycle. Railway / Linux already runs UTF-8 so this is a no-op there.
for stream in (sys.stdout, sys.stderr):
    try:
        stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

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


def run_scrape(req: dict) -> dict:
    """Run the BC scraper for one claimed request.

    Calls bettercontact_sync.main with scrape_request_id set; everything that
    lands in prospeo_new_leads during this run will be tagged.

    Returns the per-run summary dict from bettercontact_sync (accepted,
    credits_spent, csv_path, xlsx_path, aborted_reason, ...) so the caller
    can persist per-request stats without re-querying lifetime totals.
    """
    skip = compute_skip_industries(req["industries"], req["skip_industries"])
    # Scale page size to the request: a target=3 ask shouldn't burn 50 credits
    # to satisfy it. 10 is the practical floor; DEFAULT_PAGE_LIMIT (50) the
    # ceiling. Anything in between scales ~5 raw leads per requested lead so
    # the post-filter survival rate has slack.
    page_limit = max(10, min(DEFAULT_PAGE_LIMIT, req["requested_leads"] * 5))
    # Budget cap is a runaway guard, not the primary stop. Floor it at THREE
    # pages so the first cycle can fan out across enough industries to make
    # progress — one tapped-out industry (high offset / heavy dedup) shouldn't
    # be able to swallow the whole budget. For target=3, page=15, this gives
    # max_credits=50: 3 industries get a fair shot at finding the 3 leads.
    max_credits = max(req["requested_leads"] * CREDITS_PER_LEAD_BUDGET,
                      page_limit * 3 + 5)
    log(f"req #{req['id']}: scraping target={req['requested_leads']}, "
        f"countries={req['countries']}, skip={skip}, "
        f"page_limit={page_limit}, max_credits={max_credits}")
    return bettercontact_sync.main(
        mode="category",
        target_leads=req["requested_leads"],
        country=req["countries"] or None,
        skip_industries=skip,
        page_limit=page_limit,
        max_credits=max_credits,
        scrape_request_id=req["id"],
    )


def mark_ready(conn, request_id: int, *, scraped_count: int,
               credits_spent: float, csv_path: str | None,
               xlsx_path: str | None) -> None:
    """Move row to status='ready' after a successful scrape.

    credits_spent is the per-request delta returned by bettercontact_sync,
    NOT a lifetime aggregate.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            update scrape_requests
               set status           = 'ready',
                   ready_at         = now(),
                   scraped_count    = %s,
                   credits_spent    = %s,
                   export_csv_path  = %s,
                   export_xlsx_path = %s
             where id = %s
            """,
            (scraped_count, credits_spent, csv_path, xlsx_path, request_id),
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


def send_email_and_log(conn, request_id: int, req_dict: dict,
                       stats: dict, run_summary: dict) -> None:
    """Best-effort: send the ready email, log on failure, never raise."""
    payload = {
        "id": request_id,
        "requested_leads": req_dict["requested_leads"],
        "scraped_count": stats["accepted"],
        "credits_spent": run_summary.get("credits_spent", 0),
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
        run_summary = run_scrape(req)
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
        f"rejected={stats['rejected']}, "
        f"credits_spent={run_summary.get('credits_spent', 0):.1f}")
    mark_ready(
        conn, rid,
        scraped_count=stats["accepted"],
        credits_spent=float(run_summary.get("credits_spent") or 0),
        csv_path=run_summary.get("csv_path"),
        xlsx_path=run_summary.get("xlsx_path"),
    )
    send_email_and_log(conn, rid, req, stats, run_summary)
    return True


# ---------------------------------------------------------------------------
# Per-lead approval → granular move (Flavor C)
# ---------------------------------------------------------------------------
#
# Jam reviews leads inside the NocoDB per-batch grid and toggles each row's
# `lead_approval` to 'approved' or 'rejected'. The worker keeps moving newly-
# approved leads into lead_contacts on every poll until the batch is fully
# decided, then auto-finalizes the request.
#
# Two functions:
#   - process_pending_lead_moves(conn) — finds ready requests with leads
#     that are 'approved' but not yet moved; copies those into lead_contacts.
#   - finalize_completed_requests(conn) — flips status='moved' on ready
#     requests where no lead is still 'pending' and every approved lead has
#     been moved.

def find_requests_with_pending_moves(conn) -> list[int]:
    """Return scrape_request ids that have at least one lead with
    lead_approval='approved' AND lead_moved_at IS NULL.

    Uses the prospeo_new_leads_pending_move_idx partial index so this is
    cheap even though prospeo_new_leads has tens of thousands of rows.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            select distinct p.scrape_request_id
              from prospeo_new_leads p
              join scrape_requests r on r.id = p.scrape_request_id
             where p.lead_approval = 'approved'
               and p.lead_moved_at is null
               and r.status = 'ready'
             order by p.scrape_request_id
            """
        )
        return [r[0] for r in cur.fetchall()]


def move_approved_leads_for_request(conn, request_id: int) -> int:
    """Copy this request's approved-but-not-yet-moved leads into
    lead_contacts and stamp lead_moved_at on the source rows.

    Returns the number of NEW rows inserted into lead_contacts (the ON
    CONFLICT path silently drops dups; all approved-but-not-moved source
    rows still get lead_moved_at stamped regardless of conflict).

    The mapping intentionally lists columns explicitly so we get a loud
    error if lead_contacts gains a NOT NULL column later (rather than
    silently dropping data).
    """
    with conn.cursor() as cur:
        # Lock the source rows first so concurrent workers can't double-move.
        cur.execute(
            """
            select email
              from prospeo_new_leads
             where scrape_request_id = %s
               and lead_approval = 'approved'
               and lead_moved_at is null
             order by id
             for update skip locked
            """,
            (request_id,),
        )
        emails = [r[0] for r in cur.fetchall()]
        if not emails:
            return 0

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
               and p.lead_approval = 'approved'
               and p.lead_moved_at is null
               and p.email = any(%s)
            on conflict (lead_email) do nothing
            """,
            (request_id, emails),
        )
        inserted = cur.rowcount

        cur.execute(
            """
            update prospeo_new_leads
               set lead_moved_at = now()
             where scrape_request_id = %s
               and lead_approval = 'approved'
               and lead_moved_at is null
               and email = any(%s)
            """,
            (request_id, emails),
        )

        # moved_count on the scrape_requests row is a running total of leads
        # actually copied (NEW rows only — dedup-conflict rows aren't counted).
        cur.execute(
            """
            update scrape_requests
               set moved_count = moved_count + %s
             where id = %s
            """,
            (inserted, request_id),
        )
    conn.commit()
    return inserted


def finalize_request_if_done(conn, request_id: int) -> bool:
    """Auto-flip status='moved' when this request is fully decided.

    "Fully decided" = no lead_approval='pending' rows remain AND every
    'approved' lead has lead_moved_at set. moved_at gets stamped (idempotent
    via coalesce).

    Returns True if the request was finalized this call.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            update scrape_requests
               set status   = 'moved',
                   moved_at = coalesce(moved_at, now())
             where id     = %s
               and status = 'ready'
               and not exists (
                 select 1 from prospeo_new_leads
                  where scrape_request_id = %s
                    and lead_approval = 'pending'
               )
               and not exists (
                 select 1 from prospeo_new_leads
                  where scrape_request_id = %s
                    and lead_approval = 'approved'
                    and lead_moved_at is null
               )
            returning id
            """,
            (request_id, request_id, request_id),
        )
        finalized = cur.fetchone() is not None
    conn.commit()
    return finalized


def process_pending_lead_moves(conn) -> bool:
    """Across all ready requests, move newly-approved leads into lead_contacts.

    Loops over every request id that has pending moves; for each, locks
    + moves + finalizes (if applicable). Returns True if any work was done
    this cycle.
    """
    request_ids = find_requests_with_pending_moves(conn)
    if not request_ids:
        return False

    any_work = False
    for rid in request_ids:
        try:
            moved = move_approved_leads_for_request(conn, rid)
        except Exception as e:
            tb = traceback.format_exc()
            mark_failed(conn, rid, "move-to-lead_contacts failed:\n" + tb)
            log(f"req #{rid}: MOVE FAILED — {e}")
            continue
        if moved or True:  # always check finalize, even when ON CONFLICT skipped everything
            log(f"req #{rid}: moved {moved} approved lead(s) into lead_contacts")
            any_work = True
        if finalize_request_if_done(conn, rid):
            log(f"req #{rid}: all leads decided — status=moved")
    return any_work


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
            did_work = process_pending_lead_moves(conn) or did_work
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
