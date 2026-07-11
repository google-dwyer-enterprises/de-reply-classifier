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
import signal
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
import millionverifier
import notifier
import nocodb_views


POLL_INTERVAL_S = int(os.environ.get("WORKER_POLL_INTERVAL_S", "60"))
# 3 hours: anything older is presumed dead. Raised from 1h when reseller
# detection (brand_verify) added per-domain probes + polite pacing to the
# scrape — a large batch can now legitimately run past an hour, and the
# startup sweep would otherwise re-queue a healthy run after a redeploy.
RUNNING_STUCK_THRESHOLD_S = 3 * 60 * 60
# Safety margin for the credit budget. observed rate is ~2 credits / accepted
# lead; 3 gives a cushion without letting a runaway burn the account.
# Note: as of the "flat default cap" change this constant is no longer used
# by run_scrape (replaced by DEFAULT_MAX_CREDITS_WHEN_BLANK below). Kept
# here for backward-reference + in case we ever want a multiplier-driven
# cap mode again.
CREDITS_PER_LEAD_BUDGET = 3
# Flat default cap when the user leaves max_credits blank on the form.
# Read as "if Jam forgets, the worst-case spend is bounded to this many
# credits regardless of how many leads she asked for". Decoupled from the
# target on purpose — the target is the primary stop, this is just the
# runaway guard. Override per-batch by setting max_credits explicitly.
DEFAULT_MAX_CREDITS_WHEN_BLANK = 1000
# Page size policy now lives in bettercontact_sync.effective_page_limit
# (WORKER_PAGE_LIMIT = 50), shared with the submit-form credit validation.


# ---------------------------------------------------------------------------
# Startup recovery
# ---------------------------------------------------------------------------

def sweep_stuck_running(conn) -> None:
    """At startup, fail any rows stuck in 'running' for > threshold.

    A worker crashing mid-scrape (OOM, redeploy, etc.) would otherwise leave
    rows pinned in 'running' forever. Stuck rows go to 'failed', NOT back to
    'pending': the in-memory credit ledger died with the crashed run, so an
    automatic re-run would spend the request's full max_credits a second time
    (and Railway's restart policy could repeat that on every crash — N
    recoveries = (N+1)x the configured cap). It also prevented a healthy run
    slower than the threshold from being claimed a second time concurrently.
    The submitter re-runs deliberately via the portal's re-run button, which
    creates a NEW request with its own budget. Idempotent.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            update scrape_requests
               set status        = 'failed',
                   error_message = coalesce(error_message, '') ||
                                   '[stuck in running > 3h (worker crash or '
                                   'overlong run) — failed at ' || now()::text
                                   || '; NOT auto-retried to protect the '
                                   'credit budget. Re-submit via the portal '
                                   'if still needed.]'
             where status     = 'running'
               and started_at < now() - make_interval(secs => %s)
            returning id
            """,
            (RUNNING_STUCK_THRESHOLD_S,),
        )
        failed = [r[0] for r in cur.fetchall()]
    conn.commit()
    if failed:
        log(f"sweep: failed {len(failed)} stuck-running rows "
            f"(no auto-retry, budget protection): {failed}")


# ---------------------------------------------------------------------------
# pending → ready
# ---------------------------------------------------------------------------

def _csv_to_list(value) -> list[str]:
    """Parse a NocoDB MultiSelect column value into a Python list.

    NocoDB stores MultiSelect as comma-separated text; the older schema
    used Postgres text[] (which psycopg2 returns as a list). Tolerant of
    both formats so the worker survives the migration window where
    Railway might briefly run new code against the old schema or vice
    versa.

    Empty / null / blank -> []. Whitespace around entries is trimmed.
    """
    if value is None:
        return []
    if isinstance(value, list):
        return [str(s).strip() for s in value if str(s).strip()]
    return [s.strip() for s in str(value).split(",") if s.strip()]


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
                      r.countries, r.notes, r.max_credits, r.enrichment,
                      r.revenue_floor, r.revenue_first, r.amazon_qa_max_credits
            """
        )
        row = cur.fetchone()
    conn.commit()
    if not row:
        return None
    return {
        "id": row[0],
        "requested_leads": row[1],
        "industries": _csv_to_list(row[2]),
        "skip_industries": _csv_to_list(row[3]),
        "countries": _csv_to_list(row[4]),
        "notes": row[5],
        "max_credits": row[6],   # None -> worker auto-computes; int -> explicit cap
        "enrichment": row[7] or "email",   # 'email' | 'both' (phones = 10 cr each)
        "revenue_floor": row[8],  # None -> bettercontact_main uses the $300k default
        "revenue_first": bool(row[9]),  # revenue-first flow (discover->gate->enrich survivors)
        "amazon_qa_max_credits": row[10],  # None -> worker derives ~6/target-lead
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
    # to satisfy it. 10 is the practical floor; WORKER_PAGE_LIMIT (50) the
    # ceiling. Anything in between scales ~5 raw leads per requested lead so
    # the post-filter survival rate has slack.
    # Shared with the submit-form validation via bettercontact_sync so the
    # form's minimum-credit check matches the page size we actually use.
    page_limit = bettercontact_sync.effective_page_limit(req["requested_leads"])
    # Budget cap is a runaway guard, not the primary stop.
    # 1. If the submitter set an explicit max_credits, honor it verbatim.
    # 2. Otherwise: flat DEFAULT_MAX_CREDITS_WHEN_BLANK (currently 1000).
    #    Flat default reads as "if Jam forgets, don't burn more than ~$200
    #    worth". A scaling-with-target default (the old behaviour) could
    #    silently burn 5x more on a target=2000 submission. The cap is
    #    decoupled from the target on purpose — the target is the primary
    #    stop; this is just the runaway guard.
    # Floor at page_limit*3+5 so a small target (e.g. target=3 with
    # page_limit=15 -> floor=50) still gets enough budget to fan out
    # across 3 industries in cycle 1.
    if req.get("max_credits") is not None:
        max_credits = int(req["max_credits"])
        cap_src = "user"
    else:
        max_credits = max(DEFAULT_MAX_CREDITS_WHEN_BLANK, page_limit * 3 + 5)
        cap_src = "auto"
    # Phones reserve 11.1x per page, so at the standard page size a 1,000-
    # credit cap fits only ONE in-flight page and the round-robin fan-out
    # would abort after a single industry. Shrink the page instead of raising
    # the cap: reservation (ceil(page_limit * 11.1)) <= ~max_credits / 3
    # keeps >= 3 pages in flight per cycle without authorizing a single
    # extra credit.
    if req.get("enrichment", "email") == "both":
        page_limit = max(5, min(page_limit, max_credits // 34))
    log(f"req #{req['id']}: scraping target={req['requested_leads']}, "
        f"countries={req['countries']}, skip={skip}, "
        f"page_limit={page_limit}, max_credits={max_credits} ({cap_src}), "
        f"enrichment={req.get('enrichment', 'email')}")
    call_kwargs = dict(
        mode="category",
        target_leads=req["requested_leads"],
        country=req["countries"] or None,
        skip_industries=skip,
        page_limit=page_limit,
        max_credits=max_credits,
        enrichment=req.get("enrichment", "email"),
        revenue_floor=req.get("revenue_floor"),   # None -> $300k default
        scrape_request_id=req["id"],
    )
    if req.get("revenue_first"):
        # Revenue-first: discover free -> verify e-commerce -> Rainforest revenue
        # gate -> enrich only survivors. Bound the Rainforest spend per batch: an
        # explicit per-request cap if set, else ~6 credits/target lead (floor 150).
        call_kwargs["revenue_first"] = True
        rf_cap = req.get("amazon_qa_max_credits") or max(150, req["requested_leads"] * 6)
        call_kwargs["amazon_qa_max_credits"] = int(rf_cap)
        log(f"req #{req['id']}: REVENUE-FIRST flow "
            f"(Rainforest cap={int(rf_cap)}, BC enrich cap={max_credits})")
    return bettercontact_sync.main(**call_kwargs)


def mark_ready(conn, request_id: int, *, scraped_count: int,
               credits_spent: float, csv_path: str | None,
               xlsx_path: str | None, amazon_qa_credits: int = 0) -> None:
    """Move row to status='ready' after a successful scrape.

    Per-batch isolation is now structural via the lead-reviewer Flask app
    (route-based: /batch/<review_token> only ever queries leads matching
    that batch's id). NocoDB per-batch view creation has been removed —
    the email button points at the lead-reviewer URL instead.

    The legacy review_view_id / review_share_uuid / review_url columns
    are left as-is on existing rows; the cleanup_review_views sweep will
    pick up any orphans on the next poll, and Phase 5 will drop the
    columns from the schema entirely.

    credits_spent is the per-request delta returned by bettercontact_sync,
    NOT a lifetime aggregate.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            update scrape_requests
               set status                 = 'ready',
                   ready_at               = now(),
                   scraped_count          = %s,
                   credits_spent          = %s,
                   amazon_qa_credits_spent = %s,
                   export_csv_path        = %s,
                   export_xlsx_path       = %s
             where id = %s
            """,
            (scraped_count, credits_spent, amazon_qa_credits, csv_path,
             xlsx_path, request_id),
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
    # Surface every batch failure on the admin panel (best-effort — never raise).
    try:
        import api_events
        api_events.record("Worker", api_events.classify_error(None, error),
                          detail=error[:1000], context=f"scrape batch #{request_id}")
    except Exception:
        pass


def send_email_and_log(conn, request_id: int, req_dict: dict,
                       stats: dict, run_summary: dict) -> None:
    """Best-effort: send the ready email, log on failure, never raise."""
    # Pull the row's review_token; the notifier uses it to build the
    # lead-reviewer URL <LEAD_REVIEWER_BASE_URL>/batch/<review_token>.
    # The token is auto-generated by the gen_random_uuid() column default;
    # it's already on the row when we get here.
    review_token = None
    with conn.cursor() as cur:
        cur.execute(
            "select review_token from scrape_requests where id = %s",
            (request_id,),
        )
        row = cur.fetchone()
        if row and row[0]:
            review_token = str(row[0])

    payload = {
        "id": request_id,
        "requested_leads": req_dict["requested_leads"],
        "scraped_count": stats["accepted"],
        "credits_spent": run_summary.get("credits_spent", 0),
        "by_industry": stats["by_industry"],
        "review_token": review_token,
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
    # Preflight: don't spend if a provider this batch needs is down. Put it back
    # to pending so it runs automatically once the provider recovers — rather than
    # burning credits on a doomed run (as #48-50 did while BC enrichment hung).
    import preflight
    healthy, status = preflight.check(revenue_first=req.get("revenue_first", False))
    if not healthy:
        with conn.cursor() as cur:
            cur.execute("update scrape_requests set status='pending', started_at=null where id=%s", (rid,))
        conn.commit()
        reason = "; ".join(status)
        log(f"req #{rid}: HELD (dependency down) -> back to pending: {reason}")
        try:
            import api_events
            api_events.record("Preflight", "other", detail=reason,
                              context=f"batch #{rid} held (dependency down)")
        except Exception:
            pass
        return False   # sleep the poll loop; re-checks next cycle (probe cached ~5m)
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

    # No leads produced AND the run aborted (budget too low, credits out, or
    # all industries exhausted): don't send a misleading "batch ready" email
    # with zero leads. Mark the request and tell Jam the plain-English reason.
    aborted = run_summary.get("aborted_reason")
    if stats["accepted"] == 0 and aborted:
        mark_failed(conn, rid, f"No leads scraped — {aborted}")
        notifier.send_no_leads_email(rid, aborted)
        log(f"req #{rid}: no leads — {aborted}")
        return True

    mark_ready(
        conn, rid,
        scraped_count=stats["accepted"],
        credits_spent=float(run_summary.get("credits_spent") or 0),
        amazon_qa_credits=int(run_summary.get("amazon_qa_credits") or 0),
        csv_path=run_summary.get("csv_path"),
        xlsx_path=run_summary.get("xlsx_path"),
    )
    send_email_and_log(conn, rid, req, stats, run_summary)
    return True


# ---------------------------------------------------------------------------
# Mass-approve shortcut
# ---------------------------------------------------------------------------
#
# Jam can either review leads one-by-one in the NocoDB per-batch grid, OR
# set scrape_requests.approval='approved' to mass-approve every pending
# lead in the batch in one click. This function flips the pending leads
# to 'approved' so the regular granular move loop picks them up.

def apply_mass_approval(conn) -> int:
    """Mass-approve every pending lead for any request where Jam has set
    scrape_requests.approval='approved' on the parent row.

    Runs every poll, before the move step, so leads flipped here get moved
    on the same cycle. Idempotent — once the rows are flipped to 'approved',
    they no longer match the pending filter.

    Returns the total number of leads flipped to 'approved' this call.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            update prospeo_new_leads p
               set lead_approval = 'approved'
              from scrape_requests r
             where p.scrape_request_id = r.id
               and r.status = 'ready'
               and r.approval = 'approved'
               and p.lead_approval = 'pending'
            returning p.scrape_request_id
            """
        )
        rids = [r[0] for r in cur.fetchall()]
    conn.commit()
    if not rids:
        return 0
    # Count per request for tidier logs.
    counts: dict[int, int] = {}
    for rid in rids:
        counts[rid] = counts.get(rid, 0) + 1
    for rid, n in counts.items():
        log(f"req #{rid}: mass-approve flipped {n} pending lead(s) to approved")
    return len(rids)


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
        # The real gate is the LEAD state (approved + not yet moved), NOT the
        # request status. Previously this required r.status='ready', so approved
        # leads stranded on a request that later became 'failed' or 'moved' (e.g.
        # an MV-retry straggler resolved after finalize, or a mid-batch redeploy
        # that sweep_stuck_running failed) were never auto-moved — recoverable
        # only via manual export-leads. The move itself is idempotent + row-locked,
        # so it's safe to move them whatever the request's current status.
        cur.execute(
            """
            select distinct p.scrape_request_id
              from prospeo_new_leads p
             where p.lead_approval = 'approved'
               and p.lead_moved_at is null
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

        # MillionVerifier gate (tasks #8/#9): verify AFTER human approval,
        # BEFORE the 200k pool. Only result='ok' moves. Definitive bad
        # results flip the lead back to 'rejected' (batch still finalizes,
        # reviewer sees why); transient 'error' leaves the lead approved-
        # unmoved so the next poll retries naturally. With no API key
        # configured the gate is a no-op.
        if millionverifier.enabled():
            cur.execute(
                """
                select email, mv_result from prospeo_new_leads
                 where scrape_request_id = %s and email = any(%s)
                """,
                (request_id, emails),
            )
            known = dict(cur.fetchall())
            n_total = len(emails)
            todo = [e for e in emails if not known.get(e)
                    or known[e] == "error"]
            if todo:
                log(f"req #{request_id}: MillionVerifier ON — verifying {len(todo)} "
                    f"approved email(s) ({n_total - len(todo)} already verified)")
                results = millionverifier.verify_emails(todo, on_log=log)
                for email, res in results.items():
                    if res["result"] == "error":
                        continue              # retry next poll, no stamp
                    known[email] = res["result"]
                    cur.execute(
                        """
                        update prospeo_new_leads
                           set mv_result = %s, mv_checked_at = now()
                         where scrape_request_id = %s and email = %s
                        """,
                        (res["result"], request_id, email),
                    )
            bad = [e for e in emails
                   if known.get(e) in millionverifier.DEFINITIVE_BAD]
            if bad:
                cur.execute(
                    """
                    update prospeo_new_leads
                       set lead_approval = 'rejected'
                     where scrape_request_id = %s and email = any(%s)
                    """,
                    (request_id, bad),
                )
                log(f"req #{request_id}: {len(bad)} approved lead(s) failed "
                    f"MillionVerifier -> rejected: "
                    f"{', '.join(bad[:5])}{'...' if len(bad) > 5 else ''}")
            movable = [e for e in emails
                       if known.get(e) in millionverifier.MOVABLE_RESULTS]
            n_retry = n_total - len(movable) - len(bad)
            log(f"req #{request_id}: MillionVerifier results — {len(movable)} ok "
                f"(moving), {len(bad)} bad (rejected), {n_retry} unresolved "
                f"(retry next poll)")
            emails = movable
            if not emails:
                return 0
        elif (os.environ.get("MILLIONVERIFIER_REQUIRED", "").strip().lower()
              in ("1", "true", "yes", "on")):
            # Mandatory-verification mode: refuse to move unverified leads into
            # the client pool (bounces would hit the client's sending domain).
            # Hold them approved-unmoved; a later poll moves them once a key is
            # configured. Default (flag unset) keeps the optional no-op behavior.
            log(f"req #{request_id}: MILLIONVERIFIER_REQUIRED set but no API key — "
                f"HOLDING {len(emails)} approved lead(s) unmoved (refusing to move "
                f"unverified). Set MILLIONVERIFIER_API_KEY to release them.")
            return 0
        else:
            log(f"req #{request_id}: MillionVerifier DISABLED (no API key) — moving "
                f"{len(emails)} approved lead(s) WITHOUT email verification")

        cur.execute(
            """
            insert into lead_contacts (
              lead_email, first_name, last_name, title, company_name,
              website, industry, lead_list_source, imported_at,
              mv_result, mv_checked_at, mobile, scrape_request_id
            )
            select p.email, p.first_name, p.last_name, p.title, p.company_name,
                   p.company_website, p.source_industry,
                   'BetterContact', now(), p.mv_result, p.mv_checked_at,
                   p.mobile, p.scrape_request_id
              from prospeo_new_leads p
             where p.scrape_request_id = %s
               and p.lead_approval = 'approved'
               and p.lead_moved_at is null
               and p.email = any(%s)
            on conflict (lead_email) do update
               set mv_result = coalesce(excluded.mv_result,
                                        lead_contacts.mv_result),
                   mv_checked_at = coalesce(excluded.mv_checked_at,
                                            lead_contacts.mv_checked_at),
                   mobile = coalesce(excluded.mobile, lead_contacts.mobile)
            returning (xmax = 0) as is_insert
            """,
            (request_id, emails),
        )
        # moved_count semantics unchanged: NEW rows only. With DO UPDATE the
        # rowcount would include dedup-conflict rows, so count true inserts
        # via xmax = 0 (fresh tuple has no updating transaction).
        inserted = sum(1 for (is_insert,) in cur.fetchall() if is_insert)

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


def compute_qa_metrics(conn, request_id: int) -> None:
    """Harvest machine-vs-human agreement for a fully-decided batch.

    Runs once per request when it reaches status='moved' (idempotent via the
    unique constraint). The labels are the approve/reject clicks the reviewer
    already made — machine_pass_human_rejected is the escape count, the
    number that drives threshold tuning. Best-effort: failures are logged,
    never block the finalize path.
    """
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                insert into qa_metrics (scrape_request_id, total_leads,
                    machine_pass_human_approved, machine_pass_human_rejected,
                    machine_flag_human_approved, machine_flag_human_rejected)
                select %s, count(*),
                    count(*) filter (where brand_verify_result = 'brand'
                                     and lead_approval = 'approved'),
                    count(*) filter (where brand_verify_result = 'brand'
                                     and lead_approval = 'rejected'),
                    count(*) filter (where coalesce(brand_verify_result,
                                                    'unknown') <> 'brand'
                                     and lead_approval = 'approved'),
                    count(*) filter (where coalesce(brand_verify_result,
                                                    'unknown') <> 'brand'
                                     and lead_approval = 'rejected')
                from prospeo_new_leads
                where scrape_request_id = %s and not rejected
                on conflict (scrape_request_id) do nothing
                """,
                (request_id, request_id),
            )
        conn.commit()
        log(f"req #{request_id}: qa_metrics computed")
    except Exception as e:
        log(f"req #{request_id}: qa_metrics failed (non-fatal): {e}")
        try:
            conn.rollback()
        except Exception:
            pass


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
        # Always fall through to finalize, even when ON CONFLICT skipped every
        # row (moved == 0) — the batch can still be fully decided.
        if moved:
            log(f"req #{rid}: moved {moved} approved lead(s) into lead_contacts")
        any_work = True
        if finalize_request_if_done(conn, rid):
            log(f"req #{rid}: all leads decided — status=moved")
            compute_qa_metrics(conn, rid)
    return any_work


def cleanup_review_views(conn) -> int:
    """Sweep: delete the per-batch NocoDB view for any scrape_requests row
    where the batch has left the "needs review" state via ANY trigger:
      - status='moved'      (auto-finalized or mass-approve→move chain)
      - approval='approved' (Jam clicked mass-approve)
      - approval='rejected' (Jam clicked mass-reject or audit cancel)

    Calls nocodb_views.delete_review_view best-effort. If the delete fails
    (404, timeout, token revoked etc.), the DB columns are NULLed anyway —
    the URL is unreliable from that point and an orphaned view in NocoDB
    is preferable to a stuck reference in the DB. Idempotent: once the
    columns are NULL the row stops matching the WHERE clause.

    Runs every poll. Returns the number of views cleaned up this call.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            select id, review_view_id
              from scrape_requests
             where review_view_id is not null
               and (status = 'moved' or approval in ('approved', 'rejected'))
            """
        )
        candidates = cur.fetchall()

    if not candidates:
        return 0

    for rid, view_id in candidates:
        nocodb_views.delete_review_view(view_id, on_log=log)
        with conn.cursor() as cur:
            cur.execute(
                """
                update scrape_requests
                   set review_view_id    = null,
                       review_share_uuid = null,
                       review_url        = null
                 where id = %s
                """,
                (rid,),
            )
        conn.commit()
        log(f"req #{rid}: review view cleaned up")
    return len(candidates)


def finalize_complete_requests(conn) -> int:
    """Sweep finalize: flip any 'ready' request with no pending leads AND
    no unmoved approveds to status='moved'.

    process_pending_lead_moves only runs finalize_request_if_done after a
    move actually fires. So when Jam's LAST action on a batch is a rejection
    (no move work triggered), the per-request finalize never re-checks and
    the batch sits in status='ready' forever. This sweep covers that gap by
    running on every poll regardless of move activity. Idempotent — once a
    request flips to 'moved' it stops matching the WHERE clause.

    Returns the number of requests finalized this call.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            update scrape_requests
               set status   = 'moved',
                   moved_at = coalesce(moved_at, now())
             where status = 'ready'
               and not exists (
                 select 1 from prospeo_new_leads
                  where scrape_request_id = scrape_requests.id
                    and lead_approval = 'pending'
               )
               and not exists (
                 select 1 from prospeo_new_leads
                  where scrape_request_id = scrape_requests.id
                    and lead_approval = 'approved'
                    and lead_moved_at is null
               )
            returning id
            """
        )
        finalized = [r[0] for r in cur.fetchall()]
    conn.commit()
    for rid in finalized:
        log(f"req #{rid}: all leads decided — status=moved (finalize sweep)")
        compute_qa_metrics(conn, rid)
    return len(finalized)


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def log(msg: str) -> None:
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


_shutdown = False


def _handle_shutdown(signum, _frame) -> None:
    """On SIGTERM/SIGINT (e.g. a Railway redeploy), stop cleanly after the
    current poll iteration instead of dying mid-work. A scrape in flight is
    per-page resumable, so worst case it resumes at the last committed page on
    the next start; between cycles this gives a clean connection close."""
    global _shutdown
    _shutdown = True
    log(f"received signal {signum} — will exit after the current poll iteration")


def main() -> None:
    log(f"worker starting (poll interval = {POLL_INTERVAL_S}s)")
    if not (os.environ.get("RESEND_API_KEY") and os.environ.get("NOTIFY_EMAIL")):
        log("WARNING: alert delivery is OFF — set RESEND_API_KEY + NOTIFY_EMAIL "
            "to email failure/credit/preflight alerts. (They still record to "
            "/admin, but nobody is paged without these.)")
    signal.signal(signal.SIGTERM, _handle_shutdown)
    signal.signal(signal.SIGINT, _handle_shutdown)
    import heartbeat
    conn = connect()
    sweep_stuck_running(conn)
    last_sweep = time.monotonic()
    log("entering poll loop")
    while not _shutdown:
        try:
            heartbeat.beat()   # worker-alive signal (also beat per page in the scraper)
            # Periodic stuck-batch sweep (not just at startup): recover a batch
            # orphaned by a mid-session crash without waiting for a restart.
            if time.monotonic() - last_sweep > 1800:   # every 30 min
                sweep_stuck_running(conn)
                last_sweep = time.monotonic()
            did_work = process_one_pending_request(conn)
            # Mass-approve runs before the move so leads flipped this poll
            # get moved on the same cycle (no extra 60s wait for Jam).
            did_work = bool(apply_mass_approval(conn)) or did_work
            did_work = process_pending_lead_moves(conn) or did_work
            # Sweep finalize covers the "Jam's last action was a rejection"
            # path: no move fires, so process_pending_lead_moves never invokes
            # the per-request finalize_request_if_done — yet the batch is
            # actually fully decided. Idempotent.
            did_work = bool(finalize_complete_requests(conn)) or did_work
            # Cleanup runs LAST so finalize-this-poll batches get their
            # views deleted on the same cycle. Also picks up the mass-
            # approve / mass-reject paths via the approval column.
            did_work = bool(cleanup_review_views(conn)) or did_work
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
        # Interruptible idle wait: wake immediately on a shutdown signal instead
        # of blocking a full POLL_INTERVAL_S (so we exit within Railway's grace
        # window rather than getting SIGKILLed mid-sleep).
        for _ in range(POLL_INTERVAL_S):
            if _shutdown:
                break
            time.sleep(1)
    log("poll loop exited — closing DB connection")
    try:
        conn.close()
    except Exception:
        pass


if __name__ == "__main__":
    main()
