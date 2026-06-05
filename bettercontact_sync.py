"""BetterContact category-mode lead scraper.

Pulls decision-maker leads from BetterContact's Lead Finder API (parallel to
prospeo_sync.py but for the BetterContact provider). Writes accepted leads
into the existing `prospeo_new_leads` table with `provider='bettercontact'`,
so downstream exports / dedup / NocoDB views keep working unchanged.

CLI:
  python run.py scrape-leads --provider bettercontact --mode category \
    --target-leads N --max-credits M [--country "United States,Canada"] \
    [--skip-industries "..."] [--dry-run]

API shape (verified 2026-06-01 via debug/bettercontact_verify_all.py):

  POST https://app.bettercontact.rocks/api/v2/lead_finder/async   (submit)
  GET  https://app.bettercontact.rocks/api/v2/lead_finder/async/{id}   (poll)
  Header: X-API-Key: ${BETTERCONTACT_API_KEY}

  Submit body:
  {
    "filters": {
      "company_industry": {"include": ["Cosmetics"]},
      "lead_location": {"include": ["United States","Canada"]},
      "lead_seniority": {"include": ["owner","founder","cxo"]},
      "company_headcount_min": 5
    },
    "limit": 200,
    "offset": 0,
    "enrich_email_address": true
  }

  Pricing: 0.1 credit per limit slot when results exist + 1 credit per
  *deliverable* email returned. undeliverable / not_found = free.

Layer 1 only — no SmartScout revenue cross-check (BetterContact has no
revenue filter so we use `company_headcount_min=5` as a coarse proxy).
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

import requests
from dotenv import load_dotenv

# Reuse Prospeo's exporter + industries list + decision-maker title fallback.
# Industries are identical (verified — every Prospeo enum string resolves in
# BetterContact's enum). DECISION_MAKER_TITLES is the post-filter we apply if
# BC returns a non-owner-level title slipping past `lead_seniority`.
from prospeo_sync import (
    PROSPEO_INDUSTRIES as BC_INDUSTRIES,
    DECISION_MAKER_TITLES,
    rule_classify,           # agency-token + marketplace-domain rule filter
    write_csv,
    write_xlsx,
)
from db import connect


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

BC_BASE = "https://app.bettercontact.rocks/api/v2"

# Title strategy (revised after 2026-06-01 smoke tests):
#   - `lead_seniority` enum is multi-lingual + overloaded (returns Stockholders,
#     salon-owners, Cargill "data product owners", "Arbeidsgiver" etc.)
#   - `lead_job_title` substring match is fuzzy/semantic (matches "VP marketing"
#     when querying "President" — BC treats them as related)
#   - "President" in the include list draws VPs we have to throw away
# Net: substring-match owner-level titles (no President), pay for the VP noise
# BC slips through, post-filter (DECISION_MAKER_TITLES) drops it.
BC_TITLE_KEYWORDS = ["CEO", "Founder", "Owner"]

# Headcount min as revenue-floor proxy. Verified: 5 and 10 are the same
# bracket in BC's data; 20 cuts ~44%. 5 excludes only solo-founder shops.
BC_HEADCOUNT_MIN = 5

BC_PAGE_LIMIT = 200            # API max per submit
BC_REQUEST_TIMEOUT_S = 30      # HTTP timeout for submit + poll calls
BC_POLL_INTERVAL_S = 5         # how often to poll for terminate status
BC_POLL_TIMEOUT_S = 600        # give up on a request after this long. Bumped
                                # from 180s after batch 1 (2026-06-02) — most
                                # >180s waits did complete and BC was still
                                # billing for them.
BC_SUBMIT_MAX_RETRIES = 5      # network-level retries on submit
BC_POLL_MAX_RETRIES = 5        # network-level retries on each poll GET


class InsufficientCreditsError(Exception):
    """Raised when BetterContact rejects with a no-credit error."""


# ---------------------------------------------------------------------------
# Low-level API calls
# ---------------------------------------------------------------------------

def _submit_search(filters: dict, limit: int, offset: int, api_key: str) -> str:
    """POST a Lead Finder search; returns the async request_id."""
    body = {
        "filters": filters,
        "limit": limit,
        "offset": offset,
        "enrich_email_address": True,
    }
    headers = {"X-API-Key": api_key, "Content-Type": "application/json"}
    url = f"{BC_BASE}/lead_finder/async"

    last_err = None
    for attempt in range(BC_SUBMIT_MAX_RETRIES):
        try:
            r = requests.post(url, json=body, headers=headers,
                              timeout=BC_REQUEST_TIMEOUT_S)
        except (requests.exceptions.ConnectionError,
                requests.exceptions.Timeout,
                requests.exceptions.ChunkedEncodingError) as e:
            last_err = e
            time.sleep(2 * (attempt + 1))
            continue

        if r.status_code in (200, 201, 202):
            data = r.json() or {}
            rid = data.get("request_id") or data.get("id")
            if not rid:
                raise RuntimeError(f"BC submit returned no request_id: {r.text[:200]}")
            return rid

        # Permanent errors — surface them
        if r.status_code == 401:
            raise RuntimeError(f"BC submit 401 (bad API key): {r.text[:200]}")
        if r.status_code == 402:
            # Anecdote: docs don't name a code for insufficient credits, but 402
            # is the standard HTTP semantic.
            raise InsufficientCreditsError(
                f"BC reports insufficient credits: {r.text[:200]}"
            )
        if r.status_code == 400:
            raise RuntimeError(f"BC submit 400 (bad filters?): {r.text[:300]}")

        # 429 / 5xx — back off and retry
        last_err = RuntimeError(f"BC submit {r.status_code}: {r.text[:200]}")
        time.sleep(2 * (attempt + 1))

    raise RuntimeError(f"BC submit exhausted {BC_SUBMIT_MAX_RETRIES} retries: {last_err}")


def _poll_for_result(request_id: str, api_key: str,
                      timeout_s: int = BC_POLL_TIMEOUT_S) -> dict:
    """Poll GET /lead_finder/async/{id} until terminated; return the result."""
    headers = {"X-API-Key": api_key}
    url = f"{BC_BASE}/lead_finder/async/{request_id}"
    start = time.time()
    fail_streak = 0

    while time.time() - start < timeout_s:
        time.sleep(BC_POLL_INTERVAL_S)
        try:
            r = requests.get(url, headers=headers, timeout=BC_REQUEST_TIMEOUT_S)
        except (requests.exceptions.ConnectionError,
                requests.exceptions.Timeout,
                requests.exceptions.ChunkedEncodingError):
            fail_streak += 1
            if fail_streak >= BC_POLL_MAX_RETRIES:
                raise RuntimeError(f"BC poll {request_id}: transport failures")
            continue
        fail_streak = 0

        if r.status_code != 200 and r.status_code != 202:
            # 406 / 401 / 5xx — log and continue (transient)
            continue

        data = r.json() or {}
        status = (data.get("status") or "").lower()
        if status in ("terminated", "completed", "done", "finished"):
            return data

    raise RuntimeError(f"BC poll {request_id} timeout after {timeout_s}s")


def _industry_filters(industry: str, countries: list[str] | None) -> dict:
    filters: dict = {
        "company_industry": {"include": [industry]},
        "lead_job_title": {"include": BC_TITLE_KEYWORDS},
        "company_headcount_min": BC_HEADCOUNT_MIN,
    }
    if countries:
        filters["lead_location"] = {"include": list(countries)}
    return filters


def _search_industry(industry: str, countries: list[str] | None,
                      offset: int, limit: int, api_key: str) -> dict:
    """One full submit+poll cycle for one industry page (serial version)."""
    request_id = _submit_search(_industry_filters(industry, countries),
                                 limit, offset, api_key)
    return _poll_for_result(request_id, api_key)


def _poll_many(request_ids: list[str], api_key: str,
                timeout_s: int = BC_POLL_TIMEOUT_S) -> dict[str, dict]:
    """Concurrently poll many in-flight BC request_ids until all terminate
    (or timeout). Returns {request_id: result_dict}. Request_ids that never
    terminate are absent from the returned dict — caller must reconcile.

    Strategy: single-threaded round-robin polling (each GET is ~50-100ms)
    rather than a thread pool. BC's per-IP rate-limit makes massive
    parallelism a 429-magnet; sequential GETs every 5s is plenty for
    real-world wait times of 30-300s.
    """
    headers = {"X-API-Key": api_key}
    pending = set(request_ids)
    results: dict[str, dict] = {}
    start = time.time()
    fail_streak = 0

    while pending and time.time() - start < timeout_s:
        time.sleep(BC_POLL_INTERVAL_S)
        done = []
        for rid in list(pending):
            url = f"{BC_BASE}/lead_finder/async/{rid}"
            try:
                r = requests.get(url, headers=headers,
                                 timeout=BC_REQUEST_TIMEOUT_S)
            except (requests.exceptions.ConnectionError,
                    requests.exceptions.Timeout,
                    requests.exceptions.ChunkedEncodingError):
                fail_streak += 1
                continue
            fail_streak = 0
            if r.status_code != 200 and r.status_code != 202:
                continue
            data = r.json() or {}
            status = (data.get("status") or "").lower()
            if status in ("terminated", "completed", "done", "finished"):
                results[rid] = data
                done.append(rid)
        for rid in done:
            pending.discard(rid)

    return results


# ---------------------------------------------------------------------------
# Lead parsing + acceptance
# ---------------------------------------------------------------------------

def _parse_bc_lead(bc: dict, industry: str) -> dict | None:
    """Convert one BC lead JSON into a prospeo_new_leads row dict.

    Returns None if the lead is unusable (no email, missing critical fields).
    """
    email = (bc.get("contact_email_address") or "").lower().strip()
    if not email:
        return None
    email_status = bc.get("contact_email_address_status")
    if email_status not in ("deliverable",):
        # Skip undeliverable / catch_all_not_safe / unknown.
        return None

    # BC populates `company_domain` but leaves `company_website` null. Synthesize
    # the URL form so Jam's CSV column matches the Prospeo-sourced rows.
    domain = bc.get("company_domain")
    website = bc.get("company_website") or (f"https://{domain}" if domain else None)
    return {
        "email": email,
        "mobile": bc.get("contact_phone_number"),
        "first_name": bc.get("contact_first_name"),
        "last_name": bc.get("contact_last_name"),
        "title": bc.get("contact_job_title"),
        "company_name": bc.get("company_name"),
        "company_website": website,
        "company_domain": domain,
        "source_domain": domain,
        "source_industry": industry,
        "scrape_mode": "category",
        "provider": "bettercontact",
        "mobile_status": "verified" if bc.get("contact_phone_number") else None,
        "agency_filter_result": "accepted",   # we trust BC's filter + our post-checks
        "agency_filter_method": "bettercontact_title_substring",
        "agency_filter_reason": None,
        "bettercontact_raw": bc,
    }


def _post_filter(lead: dict) -> tuple[bool, str | None]:
    """Apply our own quality gates on top of BC's filtering.

    Three layers (mirrors Prospeo's accept/reject pipeline):
      1. rule_classify — marketplace domain (amazon.com etc.) or agency token
         in company name/website ("agency", "marketing", "solutions" etc.)
      2. company_name must be present
      3. title must contain a DECISION_MAKER_TITLES keyword (CEO/Founder/Owner/
         CMO/Head of E-com etc.) — drops VPs that BC's substring match slips in

    Returns (accept, reject_reason).
    """
    # Layer 1: Prospeo-style agency + marketplace rule filter (reused intact)
    rule_result = rule_classify(lead)
    if rule_result is not None:
        result, _method, reason = rule_result
        return False, f"{result}:{reason}"

    # Layer 2: must have company name
    company = (lead.get("company_name") or "").strip()
    if not company:
        return False, "no_company_name"

    # Layer 3: title must be a decision-maker keyword
    title = (lead.get("title") or "").lower()
    if not title:
        return False, "no_title"
    if not any(kw in title for kw in DECISION_MAKER_TITLES):
        return False, f"title_not_decision_maker:{title[:60]}"

    return True, None


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

def _load_existing_emails(conn) -> set[str]:
    """Pull all emails already in prospeo_new_leads (any provider) for dedup."""
    with conn.cursor() as cur:
        cur.execute("select email from prospeo_new_leads")
        return {r[0] for r in cur.fetchall() if r[0]}


def _load_state(conn) -> dict[str, dict]:
    """Read bettercontact_scrape_state into a dict keyed by industry."""
    state: dict[str, dict] = {}
    with conn.cursor() as cur:
        cur.execute("""
          select industry, last_offset_consumed, total_leads_estimated,
                 exhausted, total_credits_spent
          from bettercontact_scrape_state
        """)
        for ind, lo, tle, ex, cr in cur.fetchall():
            state[ind] = {
                "last_offset_consumed": lo or 0,
                "total_leads_estimated": tle,
                "exhausted": bool(ex),
                "total_credits_spent": float(cr or 0),
            }
    return state


def _update_state(conn, industry: str, *, new_offset: int,
                  leads_found: int | None, credits_spent_delta: float,
                  exhausted: bool, countries: list[str] | None) -> None:
    """Upsert per-industry state."""
    with conn.cursor() as cur:
        cur.execute("""
          insert into bettercontact_scrape_state
            (industry, countries, last_offset_consumed, total_leads_estimated,
             exhausted, last_scraped_at, total_credits_spent)
          values (%s, %s, %s, %s, %s, now(), %s)
          on conflict (industry) do update set
            countries = excluded.countries,
            last_offset_consumed = excluded.last_offset_consumed,
            total_leads_estimated = coalesce(excluded.total_leads_estimated,
                                              bettercontact_scrape_state.total_leads_estimated),
            exhausted = excluded.exhausted,
            last_scraped_at = now(),
            total_credits_spent =
              bettercontact_scrape_state.total_credits_spent + %s
        """, (
            industry, countries or [], new_offset, leads_found, exhausted,
            credits_spent_delta, credits_spent_delta,
        ))


def _insert_leads(conn, leads: list[dict],
                   scrape_request_id: int | None = None) -> int:
    """Bulk-insert leads (accepted or rejected). Caller sets each row's
    `rejected` field explicitly. When `scrape_request_id` is set, every row
    is tagged so the lead-scrape-automation worker can later move just the
    rows from a specific request into `lead_contacts`.

    For worker-tagged rows, `lead_approval` is seeded:
      - BC-accepted (rejected=false) -> 'pending' (Jam reviews in NocoDB)
      - BC-auto-rejected (rejected=true) -> 'rejected' (no manual review)
    Non-worker rows (CLI) leave lead_approval NULL, which the worker's
    move query excludes by definition.

    Returns rows actually inserted."""
    if not leads:
        return 0
    cols = ["email", "first_name", "last_name", "title", "company_name",
            "company_domain", "company_website", "source_domain",
            "source_industry", "scrape_mode", "provider",
            "mobile", "mobile_status",
            "agency_filter_result", "agency_filter_method",
            "agency_filter_reason", "rejected", "bettercontact_raw",
            "scrape_request_id", "lead_approval"]

    def _approval_for(lead: dict) -> str | None:
        if scrape_request_id is None:
            return None
        return "rejected" if lead.get("rejected") else "pending"

    rows = [
        [l.get(c) if c != "bettercontact_raw" else json.dumps(l.get(c) or {})
         for c in cols[:-2]] + [scrape_request_id, _approval_for(l)]
        for l in leads
    ]
    placeholders = ",".join(["%s"] * len(cols))
    sql = (f"insert into prospeo_new_leads ({','.join(cols)}) "
           f"values ({placeholders}) on conflict do nothing")
    with conn.cursor() as cur:
        cur.executemany(sql, rows)
        return cur.rowcount


# ---------------------------------------------------------------------------
# Round-robin runner
# ---------------------------------------------------------------------------

def _run_category(conn, api_key: str, *,
                   target_leads: int | None,
                   country: list[str] | None,
                   skip_industries: list[str] | None = None,
                   page_limit: int = BC_PAGE_LIMIT,
                   dry_run: bool, max_credits: int | None,
                   scrape_request_id: int | None = None) -> dict:
    """Round-robin over BC_INDUSTRIES, advancing each industry's offset by
    BC_PAGE_LIMIT each cycle until target/budget/exhaustion.

    Returns a run summary dict so callers (the Lead Scrape Automation worker)
    can record per-request stats without re-querying the lifetime-aggregated
    bettercontact_scrape_state table:
        {
          "accepted": int,
          "rejected": int,
          "credits_spent": float,
          "csv_path": str | None,
          "xlsx_path": str | None,
          "aborted_reason": str | None,
          "rejected_counts": dict[str, int],
        }
    Dry-run / all-exhausted early-returns get a zero-stats dict (still a dict).
    """
    countries = country
    skip_set = set(skip_industries or [])

    # Build active queue, respecting skips + exhausted state.
    state = _load_state(conn)
    queue: list[str] = []
    print(f"\ncategory mode (BetterContact) — countries={countries or '(any)'}, "
          f"headcount_min={BC_HEADCOUNT_MIN}")
    for ind in BC_INDUSTRIES:
        if ind in skip_set:
            print(f"  [skip] {ind!r} (--skip-industries)")
            continue
        s = state.get(ind, {})
        if s.get("exhausted"):
            print(f"  [skip] {ind!r} exhausted (offset={s.get('last_offset_consumed')})")
            continue
        queue.append(ind)

    if not queue:
        print("All industries exhausted. Reset state to re-scan from top.")
        return {"accepted": 0, "rejected": 0, "credits_spent": 0.0,
                "csv_path": None, "xlsx_path": None,
                "aborted_reason": "all_industries_exhausted",
                "rejected_counts": {}}

    print(f"  {len(queue)} industries active.")

    if dry_run:
        for ind in queue:
            s = state.get(ind, {})
            print(f"  would scrape {ind!r} starting at offset={s.get('last_offset_consumed', 0)}")
        return {"accepted": 0, "rejected": 0, "credits_spent": 0.0,
                "csv_path": None, "xlsx_path": None,
                "aborted_reason": "dry_run",
                "rejected_counts": {}}

    # Per-industry consecutive transport-failure counter (drop after N).
    consec_failures: dict[str, int] = {ind: 0 for ind in queue}
    MAX_CONSEC_FAILURES = 3

    existing_emails = _load_existing_emails(conn)
    total_existing_before_run = len(existing_emails)
    print(f"  existing emails in DB: {total_existing_before_run:,}")

    accepted: list[dict] = []
    rejected: list[dict] = []
    rejected_counts: dict[str, int] = {}
    # Credit accounting: pre-charged budget. `credits_spent` is the running
    # confirmed cost (from completed calls). `in_flight_credits` is the
    # worst-case reservation for submitted-but-unread calls. We check the cap
    # against `credits_spent + in_flight_credits` BEFORE submitting, so we
    # never overshoot even when BC is slow to process. On timeout, we
    # conservatively assume BC charged us the full reservation (worst case).
    credits_spent: float = 0.0
    in_flight_credits: float = 0.0
    aborted_reason: str | None = None
    cycle = 0

    while queue:
        cycle += 1
        print(f"\n--- cycle {cycle} ({len(queue)} active industries) ---")
        next_queue: list[str] = []

        # ------------------------------------------------------------------
        # Phase A — submit all queue industries in parallel.
        # Each submit is a ~50-100ms POST so doing them serially is still
        # ~1s for the whole queue. We track per-rid metadata so when results
        # come back we know which industry / offset to credit.
        # ------------------------------------------------------------------
        submitted: dict[str, tuple[str, int]] = {}   # rid -> (industry, offset)
        submit_failures: dict[str, Exception] = {}   # industry -> exception
        for ind in queue:
            if target_leads is not None and len(accepted) >= target_leads:
                aborted_reason = f"target_leads reached ({len(accepted)}/{target_leads})"
                break
            if (max_credits is not None
                    and credits_spent + in_flight_credits + page_limit > max_credits):
                aborted_reason = (
                    f"budget cap hit (spent={credits_spent:.1f} + "
                    f"in_flight={in_flight_credits:.1f} + next_reserve={page_limit} "
                    f"> {max_credits})"
                )
                break
            s = state.get(ind, {})
            offset = s.get("last_offset_consumed", 0)
            in_flight_credits += page_limit
            try:
                rid = _submit_search(_industry_filters(ind, countries),
                                     page_limit, offset, api_key)
                submitted[rid] = (ind, offset)
            except InsufficientCreditsError as e:
                in_flight_credits -= page_limit
                print(f"  !!! {e}", file=sys.stderr)
                aborted_reason = "BetterContact INSUFFICIENT_CREDITS"
                break
            except Exception as e:
                in_flight_credits -= page_limit
                submit_failures[ind] = e

        if not submitted:
            # Nothing made it through — either we hit a hard abort during
            # submission, or every industry failed at submit time.
            if aborted_reason:
                break
            for ind, e in submit_failures.items():
                consec_failures[ind] = consec_failures.get(ind, 0) + 1
                if consec_failures[ind] >= MAX_CONSEC_FAILURES:
                    print(f"  ! {ind!r} submit failed: {e} "
                          f"[dropping after {MAX_CONSEC_FAILURES} consecutive failures]",
                          file=sys.stderr)
                else:
                    print(f"  ! {ind!r} submit failed: {e} "
                          f"[{consec_failures[ind]}/{MAX_CONSEC_FAILURES}]",
                          file=sys.stderr)
                    next_queue.append(ind)
            queue = next_queue
            continue

        print(f"  submitted {len(submitted)} industries concurrently, polling...")

        # ------------------------------------------------------------------
        # Phase B — poll all submitted request_ids concurrently. Returns
        # only the ones that terminated; unterminated rids are treated as
        # timeouts.
        # ------------------------------------------------------------------
        results = _poll_many(list(submitted.keys()), api_key)

        # ------------------------------------------------------------------
        # Phase C — process each industry's result, update state, write to DB.
        # ------------------------------------------------------------------
        for rid, (ind, offset) in submitted.items():
            if rid not in results:
                # Poll timeout: BC very likely still charged us. Treat
                # reservation as spent (mirrors batch 1 recovery semantic).
                in_flight_credits -= page_limit
                credits_spent += page_limit
                consec_failures[ind] = consec_failures.get(ind, 0) + 1
                if consec_failures[ind] >= MAX_CONSEC_FAILURES:
                    print(f"  ! {ind!r} offset={offset}: poll timeout "
                          f"[dropping after {MAX_CONSEC_FAILURES} consecutive failures; "
                          f"assumed {page_limit} credits charged]",
                          file=sys.stderr)
                else:
                    print(f"  ! {ind!r} offset={offset}: poll timeout "
                          f"[{consec_failures[ind]}/{MAX_CONSEC_FAILURES}; "
                          f"assumed {page_limit} credits charged]",
                          file=sys.stderr)
                    next_queue.append(ind)
                continue

            consec_failures[ind] = 0
            result = results[rid]

            cc = float(result.get("credits_consumed") or 0)
            in_flight_credits -= page_limit
            credits_spent += cc
            leads_found = ((result.get("summary") or {}).get("leads_found") or 0)
            bc_leads = result.get("leads") or []

            if state.get(ind, {}).get("total_leads_estimated") is None and leads_found:
                state.setdefault(ind, {})["total_leads_estimated"] = leads_found

            batch_accepted: list[dict] = []
            batch_rejected: list[dict] = []
            for bc in bc_leads:
                lead = _parse_bc_lead(bc, ind)
                if not lead:
                    rejected_counts["no_deliverable_email"] = (
                        rejected_counts.get("no_deliverable_email", 0) + 1)
                    continue
                if lead["email"] in existing_emails:
                    lead["agency_filter_result"] = "rejected"
                    lead["agency_filter_reason"] = "dedup_existing_db"
                    lead["rejected"] = True
                    batch_rejected.append(lead)
                    rejected_counts["dedup"] = rejected_counts.get("dedup", 0) + 1
                    continue
                ok, reason = _post_filter(lead)
                if not ok:
                    lead["agency_filter_result"] = "rejected"
                    lead["agency_filter_reason"] = reason
                    lead["rejected"] = True
                    batch_rejected.append(lead)
                    rejected_counts[reason] = rejected_counts.get(reason, 0) + 1
                    continue
                lead["rejected"] = False
                batch_accepted.append(lead)
                existing_emails.add(lead["email"])

            if batch_accepted or batch_rejected:
                _insert_leads(conn, batch_accepted + batch_rejected,
                              scrape_request_id=scrape_request_id)
                conn.commit()

            new_offset = offset + page_limit
            est = state.get(ind, {}).get("total_leads_estimated") or leads_found or 0
            exhausted = bool(est) and new_offset >= est

            _update_state(conn, ind, new_offset=new_offset,
                          leads_found=leads_found,
                          credits_spent_delta=cc,
                          exhausted=exhausted,
                          countries=countries or [])
            conn.commit()
            state.setdefault(ind, {})["last_offset_consumed"] = new_offset
            state[ind]["exhausted"] = exhausted

            accepted.extend(batch_accepted)
            rejected.extend(batch_rejected)

            print(f"  [{ind}] offset={offset}→{new_offset} of est={est:,}: "
                  f"+{len(batch_accepted)} accepted / "
                  f"+{len(batch_rejected)} rejected "
                  f"(credits this call: {cc:.1f}, run total: {credits_spent:.1f}"
                  f"{'/' + str(max_credits) if max_credits else ''})")

            if not exhausted:
                next_queue.append(ind)
            else:
                print(f"    [{ind!r} exhausted at offset {new_offset}]")

        # Submit-time failures (no rid was ever issued) → also count toward
        # consec_failures and potentially re-queue.
        for ind, e in submit_failures.items():
            consec_failures[ind] = consec_failures.get(ind, 0) + 1
            if consec_failures[ind] >= MAX_CONSEC_FAILURES:
                print(f"  ! {ind!r} submit failed: {e} "
                      f"[dropping after {MAX_CONSEC_FAILURES} consecutive failures]",
                      file=sys.stderr)
            else:
                print(f"  ! {ind!r} submit failed: {e} "
                      f"[{consec_failures[ind]}/{MAX_CONSEC_FAILURES}]",
                      file=sys.stderr)
                next_queue.append(ind)

        if aborted_reason:
            break
        queue = next_queue

    if aborted_reason:
        print(f"\n!!! Run aborted: {aborted_reason}", file=sys.stderr)

    # --- Final export -------------------------------------------------------
    os.makedirs("exports", exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    csv_path = f"exports/bettercontact_new_leads_{stamp}.csv"
    xlsx_path = f"exports/bettercontact_new_leads_{stamp}.xlsx"
    # Accepted-only CSV (what Jam loads into Instantly).
    write_csv(accepted, csv_path)
    # Two-sheet XLSX: Accepted + Rejected (audit).
    write_xlsx(accepted, rejected, xlsx_path)

    print(f"\n=== summary ===")
    if aborted_reason:
        print(f"  ABORTED:               {aborted_reason}")
    print(f"  countries:             {countries or '(any)'}")
    print(f"  industries scanned:    {len(BC_INDUSTRIES)}")
    print(f"  accepted (brand):      {len(accepted)}")
    print(f"  rejected:              {len(rejected)}")
    print(f"  total credits spent:   {credits_spent:.1f}")
    print(f"  CSV (accepted):        {csv_path}")
    print(f"  XLSX (both sheets):    {xlsx_path}")
    if rejected_counts:
        print(f"  rejection breakdown:")
        for reason, n in sorted(rejected_counts.items(), key=lambda x: -x[1])[:10]:
            print(f"    {reason}: {n}")

    return {
        "accepted": len(accepted),
        "rejected": len(rejected),
        "credits_spent": float(credits_spent),
        "csv_path": csv_path,
        "xlsx_path": xlsx_path,
        "aborted_reason": aborted_reason,
        "rejected_counts": dict(rejected_counts),
    }


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main(*, mode: str = "category", target_leads: int | None = None,
         country: list[str] | None = None,
         skip_industries: list[str] | None = None,
         page_limit: int = BC_PAGE_LIMIT,
         dry_run: bool = False, max_credits: int | None = None,
         scrape_request_id: int | None = None) -> dict:
    """Entry point for `run.py scrape-leads --provider bettercontact`.

    Domain mode is NOT supported — BetterContact's Lead Finder is criteria-
    based, not domain-list based. If you want enrichment of an existing
    domain list, that's a different endpoint (separate implementation).

    `scrape_request_id`, when set, tags every inserted row in
    `prospeo_new_leads` so the Lead Scrape Automation worker (see worker.py)
    can later move the request's rows into `lead_contacts` on approval.
    Callers from the CLI leave this as None — only the worker uses it.
    """
    if mode != "category":
        raise SystemExit(f"BetterContact only supports --mode category (got {mode!r})")

    load_dotenv()
    api_key = (os.environ.get("BETTERCONTACT_API_KEY") or "").strip()
    if not api_key:
        sys.exit("BETTERCONTACT_API_KEY not set in .env")

    conn = connect()
    try:
        return _run_category(conn, api_key,
                             target_leads=target_leads, country=country,
                             skip_industries=skip_industries,
                             page_limit=page_limit,
                             dry_run=dry_run, max_credits=max_credits,
                             scrape_request_id=scrape_request_id)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
