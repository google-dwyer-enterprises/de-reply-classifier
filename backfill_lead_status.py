"""Backfill `lead_status` and `lead_status_code` on replies from Instantly leads.

Pages through /leads/list, builds (email -> lt_interest_status) and resolves
to label names via /lead-labels. Updates replies grouped by status code so
each status only triggers one round-trip per chunk of emails.
"""

from __future__ import annotations

import argparse
import os
import sys

import requests
from dotenv import load_dotenv
from supabase import create_client

from instantly_sync import (
    API_BASE,
    DEFAULT_MIN_INTERVAL_S,
    PAGE_LIMIT,
    RateLimiter,
    extract_items,
    fetch_lead_labels,
    get_env,
    make_session,
    request_with_backoff,
)

LIST_LEADS_URL = f"{API_BASE}/leads/list"
UPDATE_CHUNK = 200


def post_with_backoff(session, url, body, limiter):
    """Mirror request_with_backoff but for POST. Inlined since /leads/list is POST."""
    import random
    import time
    for attempt in range(8):
        limiter.wait()
        try:
            resp = session.post(url, json=body, timeout=60)
        except (requests.exceptions.ReadTimeout, requests.exceptions.ConnectionError) as exc:
            sleep_s = (2 ** attempt) + random.random()
            print(f"  network error ({type(exc).__name__}); retrying in {sleep_s:.1f}s")
            time.sleep(sleep_s)
            continue
        limiter.update_from_headers(resp.headers)
        if resp.status_code == 429:
            sleep_s = float(resp.headers.get("retry-after") or min(60.0, max(15.0, (2 ** attempt) + random.random())))
            print(f"  429; backing off {sleep_s:.1f}s")
            time.sleep(sleep_s)
            continue
        if 500 <= resp.status_code < 600:
            time.sleep((2 ** attempt) + random.random())
            continue
        resp.raise_for_status()
        return resp
    sys.exit(f"FATAL: exhausted retries on {url}")


def fetch_lead_status_map(session, limiter) -> dict[str, int]:
    """Returns {lead_email: lt_interest_status} for every lead with one set."""
    out: dict[str, int] = {}
    cursor = None
    page = 0
    while True:
        page += 1
        body = {"limit": PAGE_LIMIT}
        if cursor:
            body["starting_after"] = cursor
        print(f"Fetching leads page {page}" + (f" (cursor={cursor})" if cursor else ""))
        resp = post_with_backoff(session, LIST_LEADS_URL, body, limiter)
        payload = resp.json()
        items, next_cursor = extract_items(payload)
        for it in items:
            email = (it.get("email") or "").strip().lower()
            code = it.get("lt_interest_status")
            if email and code is not None:
                out[email] = int(code)
        if next_cursor and next_cursor != cursor:
            cursor = next_cursor
        elif len(items) >= PAGE_LIMIT:
            last_id = items[-1].get("id") if items else None
            if last_id and last_id != cursor:
                cursor = last_id
                continue
            return out
        else:
            return out


def chunked(seq, n):
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


def update_replies(supabase, status_map: dict[str, int], labels: dict[int, str]) -> int:
    by_code: dict[int, list[str]] = {}
    for email, code in status_map.items():
        by_code.setdefault(code, []).append(email)

    total = 0
    for code, emails in by_code.items():
        label = labels.get(code)
        payload = {"lead_status_code": code, "lead_status": label}
        for batch in chunked(emails, UPDATE_CHUNK):
            resp = (
                supabase.table("replies")
                .update(payload)
                .in_("lead_email", batch)
                .execute()
            )
            total += len(resp.data or [])
    return total


def relabel_only(supabase, labels: dict[int, str]) -> int:
    """Re-resolve lead_status (label string) from existing lead_status_code
    in replies. Used after adding new entries to BUILTIN_LEAD_STATUSES or
    lead-labels in Instantly. No /leads/list pagination."""
    total = 0
    for code, label in labels.items():
        resp = (
            supabase.table("replies")
            .update({"lead_status": label})
            .eq("lead_status_code", code)
            .execute()
        )
        n = len(resp.data or [])
        if n:
            print(f"  code={code} -> {label!r}: {n} rows")
        total += n
    return total


def main(argv: list[str] | None = None) -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    parser = argparse.ArgumentParser(prog="backfill_lead_status.py")
    parser.add_argument("--relabel", action="store_true",
                        help="Skip /leads/list pagination; only re-resolve lead_status from existing lead_status_code")
    args = parser.parse_args(argv)

    load_dotenv()
    instantly_key = get_env("INSTANTLY_API_KEY")
    supabase_url = get_env("SUPABASE_URL")
    supabase_key = get_env("SUPABASE_KEY")

    supabase = create_client(supabase_url, supabase_key)
    session = make_session(instantly_key)
    limiter = RateLimiter(DEFAULT_MIN_INTERVAL_S)

    labels = fetch_lead_labels(session, limiter)
    print(f"Loaded {len(labels)} lead labels (built-in + custom)")

    if args.relabel:
        n = relabel_only(supabase, labels)
        print(f"replies: relabeled {n} rows")
        return

    status_map = fetch_lead_status_map(session, limiter)
    print(f"Loaded interest status for {len(status_map)} leads")

    if not status_map:
        print("No lead statuses to apply.")
        return

    n = update_replies(supabase, status_map, labels)
    print(f"replies: updated {n} rows with lead_status")


if __name__ == "__main__":
    main()
