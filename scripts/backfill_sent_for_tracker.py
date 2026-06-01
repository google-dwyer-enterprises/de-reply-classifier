"""Targeted backfill of sent_messages for the follow-up tracker cohort.

WHY THIS EXISTS
---------------
The bulk sent-message sync (`instantly_sync.py --type sent`) was only ever run
for a ~50-day window, so `sent_messages` covers 2026-04-12 -> present. But the
follow-up tracker's leads are overwhelmingly HISTORICAL — 85% of booked leads
last replied before 2026-04-12 — so their manual follow-ups were sent before the
synced window and never landed in `sent_messages`. Result: `followup_tracker_mv`
shows blank follow-up columns for 77% of rows.

A full bulk historical backfill would pull millions of campaign auto-sends to
non-tracker leads at 20 req/min (the live limit). Instead, this script pulls
outbound ONLY for the ~829 leads that appear in `lead_outcomes` (the tracker
cohort), via the per-lead endpoint `GET /v2/emails?lead=<email>`. Verified
(scripts/diag_probe_historical.py) that Instantly still serves these leads'
outbound back to at least 2025-09.

WHAT IT DOES
------------
For each distinct lead_email in lead_outcomes:
  1. GET /v2/emails?lead=<email>  (paginated, 429-aware via request_with_backoff)
  2. Keep outbound (ue_type in (1, 3)); parse with the SAME parse_email() the
     production sync uses (email_type="sent") so rows are byte-identical.
  3. Upsert into sent_messages (on conflict instantly_message_id do nothing).
send_kind is the stored generated column, so manual vs auto is derived in-DB.

Idempotent + resumable: completed lead emails are checkpointed to
debug/backfill_sent_tracker_done.txt; re-running skips them. Upserts ignore
duplicates, so a partial lead is safe to re-pull.

Does NOT touch sync_state (this is a targeted historical fill, not the
incremental cursor). No explicit refresh needed — followup_tracker_mv and
followup_messages_mv are regular views now (converted for NocoDB), so they
auto-recompute against the latest sent_messages on next query.

Usage:
    python scripts/backfill_sent_for_tracker.py [--limit N] [--reset]
      --limit N   process only the first N pending leads (smoke test)
      --reset     ignore the checkpoint file and reprocess everyone
"""
from __future__ import annotations

import argparse
import sys
import time
from collections import Counter
from pathlib import Path

from dotenv import load_dotenv
from supabase import create_client

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import instantly_sync as isync
from db import connect

CHECKPOINT = isync.DEBUG_DIR / "backfill_sent_tracker_done.txt"
# Live limit observed at 20 req/min on the per-lead endpoint. 3.2s floor keeps
# us under it even before the ratelimit headers kick in. request_with_backoff
# also self-adjusts from x-ratelimit-* headers and retries 429s.
PER_LEAD_INTERVAL_S = 3.2


def load_done() -> set[str]:
    if not CHECKPOINT.exists():
        return set()
    return {
        line.strip().lower()
        for line in CHECKPOINT.read_text(encoding="utf-8").splitlines()
        if line.strip()
    }


def mark_done(email: str) -> None:
    with open(CHECKPOINT, "a", encoding="utf-8") as f:
        f.write(email + "\n")


def get_tracker_leads() -> list[str]:
    """Distinct lead_email from lead_outcomes — the tracker cohort."""
    conn = connect()
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute(
                "select distinct lead_email from lead_outcomes "
                "where lead_email is not null and trim(lead_email) <> '' "
                "order by lead_email"
            )
            return [r[0] for r in cur.fetchall()]
    finally:
        conn.close()


def fetch_lead_outbound(session, limiter, email: str) -> list[dict]:
    """All emails for one lead via ?lead=<email>, returning the raw outbound
    items (ue_type 1 = campaign auto, 3 = manual unibox). Inbound (ue_type 2)
    is dropped — it already lives in `replies`."""
    items: list[dict] = []
    cursor: str | None = None
    for _ in range(40):  # hard pagination cap per lead
        params = {"lead": email, "limit": isync.PAGE_LIMIT}
        if cursor:
            params["starting_after"] = cursor
        resp = isync.request_with_backoff(
            session, isync.LIST_EMAILS_URL, params, limiter
        )
        payload = resp.json()
        page_items, next_cursor = isync.extract_items(payload)
        items.extend(page_items)
        if next_cursor and next_cursor != cursor:
            cursor = next_cursor
            continue
        if len(page_items) >= isync.PAGE_LIMIT and page_items:
            last_id = page_items[-1].get("id")
            if last_id and last_id != cursor:
                cursor = last_id
                continue
        break
    return [it for it in items if it.get("ue_type") in (1, 3)]


def main() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=None,
                    help="Process only the first N pending leads (smoke test).")
    ap.add_argument("--reset", action="store_true",
                    help="Ignore checkpoint; reprocess all leads.")
    args = ap.parse_args()

    load_dotenv()
    instantly_key = isync.get_env("INSTANTLY_API_KEY")
    supabase = create_client(
        isync.get_env("SUPABASE_URL"), isync.get_env("SUPABASE_KEY")
    )

    session = isync.make_session(instantly_key)
    limiter = isync.RateLimiter(PER_LEAD_INTERVAL_S)

    print("Fetching campaign / tag / label maps...")
    campaigns_map = isync.fetch_all_campaigns(session, limiter)
    tags_map = isync.fetch_campaign_tags(session, limiter)
    lead_labels = isync.fetch_lead_labels(session, limiter)
    print(f"  {len(campaigns_map)} campaigns, {len(lead_labels)} lead labels")

    leads = get_tracker_leads()
    done = set() if args.reset else load_done()
    if args.reset and CHECKPOINT.exists():
        CHECKPOINT.unlink()
    pending = [e for e in leads if e.lower() not in done]
    if args.limit:
        pending = pending[: args.limit]

    print(f"\nTracker leads: {len(leads)} | already done: {len(done)} | "
          f"processing now: {len(pending)}")
    if not pending:
        print("Nothing to do.")
        return

    isync.DEBUG_DIR.mkdir(exist_ok=True)

    totals = Counter()
    leads_with_outbound = 0
    leads_empty = 0
    new_rows = 0
    start = time.monotonic()

    for i, email in enumerate(pending, 1):
        try:
            outbound = fetch_lead_outbound(session, limiter, email)
        except SystemExit:
            print(f"  ! giving up on {email} after retries; leaving unmarked "
                  f"for a later resume", file=sys.stderr)
            continue

        rows = []
        for it in outbound:
            row = isync.parse_email(it, campaigns_map, tags_map, lead_labels,
                                    email_type="sent")
            if not row["instantly_message_id"] or not row["lead_email"]:
                continue
            rows.append(row)
            k = "manual" if it.get("ue_type") == 3 else "auto"
            totals[k] += 1

        inserted = isync.upsert_rows(supabase, rows, "sent")
        new_rows += inserted
        if rows:
            leads_with_outbound += 1
        else:
            leads_empty += 1

        mark_done(email)

        if i % 10 == 0 or i == len(pending):
            elapsed = time.monotonic() - start
            rate = i / elapsed * 60 if elapsed else 0
            eta_min = (len(pending) - i) / max(rate, 0.01)
            print(f"  [{i}/{len(pending)}] {email[:40]:40s} "
                  f"out={len(rows):3d} new={inserted:3d} | "
                  f"manual={totals['manual']} auto={totals['auto']} "
                  f"newrows={new_rows} | {rate:.1f} leads/min ETA {eta_min:.0f}m")

    print("\n=== backfill summary ===")
    print(f"  leads processed:        {len(pending)}")
    print(f"  leads with outbound:    {leads_with_outbound}")
    print(f"  leads empty (no API):   {leads_empty}")
    print(f"  outbound parsed:        manual={totals['manual']} auto={totals['auto']}")
    print(f"  NEW sent_messages rows: {new_rows}")
    print("\nDone — followup_tracker_mv / followup_messages_mv are regular views, "
          "so the new rows will show up on next query (no refresh needed).")


if __name__ == "__main__":
    main()
