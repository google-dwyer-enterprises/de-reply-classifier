"""Recover follow-ups for tracker leads whose lead_outcomes email is NOT the
address Instantly campaigned them under (the identity-mismatch cohort).

BACKGROUND
----------
After the full per-lead sent backfill (scripts/backfill_sent_for_tracker.py),
~195 tracker leads still showed no follow-ups. They DO appear in `replies`
(they replied), but `/v2/emails?lead=<their tracker email>` returns nothing —
because Jam's CSV recorded a different address than the one Instantly indexes
the lead under. Example: tracker `a.samad@skinchemists.com`, but the campaign
lead is `mk2@skinchemists.com` (the reply just came FROM a.samad@).

RECOVERY (verified by scripts/diag_thread_probe.py)
---------------------------------------------------
The raw reply object exposes the real lead address in its `lead` field:
  GET /v2/emails/<reply_id>  ->  obj["lead"] == campaigned address
(`?thread_id=` is silently ignored by Instantly, so it can't be used.)

For each mismatched tracker lead:
  1. Take one of its reply `instantly_message_id`s from `replies`.
  2. GET /v2/emails/<id> -> campaigned_email = obj["lead"].
  3. If campaigned_email differs, fetch /v2/emails?lead=<campaigned_email>
     (the lead's full outbound, incl. manual ue_type=3 follow-ups), parse with
     the production parse_email(), but OVERRIDE lead_email = tracker_email so
     `followup_tracker_mv` (which joins on lead_outcomes.lead_email) picks them
     up with NO view change.
  4. Upsert into sent_messages (on conflict instantly_message_id do nothing).

Storing under the tracker email is safe: these campaigned addresses are NOT in
lead_outcomes (so the normal cohort backfill never pulls them), and the
campaigned thread IS this lead's conversation, so its manual sends ARE this
lead's follow-ups.

Idempotent + resumable via debug/reconcile_tracker_done.txt. A per-lead mapping
report is written to debug/reconcile_tracker_map.csv.

Usage:
    python scripts/reconcile_tracker_identities.py [--limit N] [--reset]
"""
from __future__ import annotations

import argparse
import csv
import sys
import time
from collections import Counter
from pathlib import Path

import requests
from dotenv import load_dotenv
from supabase import create_client

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import instantly_sync as isync
from db import connect

CHECKPOINT = isync.DEBUG_DIR / "reconcile_tracker_done.txt"
MAP_CSV = isync.DEBUG_DIR / "reconcile_tracker_map.csv"
PER_LEAD_INTERVAL_S = 3.2


def load_done() -> set[str]:
    if not CHECKPOINT.exists():
        return set()
    return {l.strip().lower() for l in CHECKPOINT.read_text(encoding="utf-8").splitlines() if l.strip()}


def mark_done(email: str) -> None:
    with open(CHECKPOINT, "a", encoding="utf-8") as f:
        f.write(email + "\n")


def get_mismatch_cohort() -> list[tuple[str, str]]:
    """Tracker leads with no sent_messages under their email but WITH a reply
    we can look up. Returns [(tracker_email, a_reply_message_id), ...]."""
    conn = connect()
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute("""
                select distinct on (lo.lead_email)
                       lo.lead_email, r.instantly_message_id
                from lead_outcomes lo
                join replies r on r.lead_email = lo.lead_email
                where not exists (
                    select 1 from sent_messages s where s.lead_email = lo.lead_email)
                  and r.instantly_message_id is not null
                order by lo.lead_email, r.reply_timestamp desc
            """)
            return [(e, mid) for e, mid in cur.fetchall()]
    finally:
        conn.close()


def fetch_email_by_id(session, limiter, message_id: str) -> dict | None:
    """GET one email by id. Returns None on 404 (Instantly no longer serves
    that message — deleted/expired) rather than crashing the run."""
    url = f"{isync.LIST_EMAILS_URL}/{message_id}"
    try:
        resp = isync.request_with_backoff(session, url, {}, limiter)
    except requests.exceptions.HTTPError as e:
        sc = getattr(e.response, "status_code", None)
        if sc == 404:
            return None
        raise
    try:
        return resp.json()
    except Exception:
        return None


def same_domain(a: str, b: str) -> bool:
    return a.split("@")[-1].lower() == b.split("@")[-1].lower()


def fetch_lead_outbound(session, limiter, email: str) -> list[dict]:
    """All outbound (ue_type 1/3) for one lead via ?lead=. Same pagination as
    backfill_sent_for_tracker.fetch_lead_outbound."""
    items: list[dict] = []
    cursor: str | None = None
    for _ in range(40):
        params = {"lead": email, "limit": isync.PAGE_LIMIT}
        if cursor:
            params["starting_after"] = cursor
        resp = isync.request_with_backoff(session, isync.LIST_EMAILS_URL, params, limiter)
        page_items, next_cursor = isync.extract_items(resp.json())
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
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--reset", action="store_true")
    ap.add_argument("--dry-run", action="store_true",
                    help="Resolve + classify every lead's campaigned identity, "
                         "write debug/reconcile_tracker_dryrun.csv, but DO NOT "
                         "fetch outbound or write to the DB.")
    ap.add_argument("--same-domain-only", action="store_true",
                    help="When applying, skip cross-domain remaps (only attribute "
                         "follow-ups when the campaigned address shares the lead's "
                         "domain — high confidence).")
    args = ap.parse_args()

    load_dotenv()
    instantly_key = isync.get_env("INSTANTLY_API_KEY")
    supabase = create_client(isync.get_env("SUPABASE_URL"), isync.get_env("SUPABASE_KEY"))

    session = isync.make_session(instantly_key)
    limiter = isync.RateLimiter(PER_LEAD_INTERVAL_S)

    print("Fetching campaign / tag / label maps...")
    campaigns_map = isync.fetch_all_campaigns(session, limiter)
    tags_map = isync.fetch_campaign_tags(session, limiter)
    lead_labels = isync.fetch_lead_labels(session, limiter)

    cohort = get_mismatch_cohort()
    if args.dry_run:
        pending = list(cohort)  # classify everyone; never touches checkpoint/DB
    else:
        done = set() if args.reset else load_done()
        if args.reset and CHECKPOINT.exists():
            CHECKPOINT.unlink()
        pending = [(e, mid) for e, mid in cohort if e.lower() not in done]
    if args.limit:
        pending = pending[: args.limit]

    print(f"Mismatch cohort: {len(cohort)} | processing: {len(pending)}"
          + ("  [DRY RUN]" if args.dry_run else ""))
    if not pending:
        print("Nothing to do.")
        return

    isync.DEBUG_DIR.mkdir(exist_ok=True)
    out_csv = (isync.DEBUG_DIR / "reconcile_tracker_dryrun.csv") if args.dry_run else MAP_CSV
    map_exists = out_csv.exists() and not args.dry_run
    map_f = open(out_csv, "w" if args.dry_run else "a", newline="", encoding="utf-8")
    map_w = csv.writer(map_f)
    if args.dry_run:
        map_w.writerow(["tracker_email", "campaigned_email", "resolution"])
    elif not map_exists:
        map_w.writerow(["tracker_email", "campaigned_email", "manual", "auto", "new_rows"])

    totals = Counter()
    resolved = same = unresolved = cross = new_rows_total = 0
    skipped_cross = 0
    start = time.monotonic()

    for i, (tracker_email, reply_id) in enumerate(pending, 1):
        try:
            obj = fetch_email_by_id(session, limiter, reply_id)
            campaigned = ((obj or {}).get("lead") or "").strip().lower()

            # ---- classify resolution ----
            if not campaigned:
                resolution = "unresolved"
                unresolved += 1
            elif campaigned == tracker_email:
                resolution = "same_email"     # ?lead= already tried it; nothing to recover
                same += 1
            elif same_domain(tracker_email, campaigned):
                resolution = "same_domain"
                resolved += 1
            else:
                resolution = "cross_domain"
                resolved += 1
                cross += 1

            if args.dry_run:
                map_w.writerow([tracker_email, campaigned, resolution])
                map_f.flush()
                if i % 10 == 0 or i == len(pending):
                    el = time.monotonic() - start
                    rate = i / el * 60 if el else 0
                    print(f"  [{i}/{len(pending)}] same_domain={resolved-cross} "
                          f"cross_domain={cross} same_email={same} "
                          f"unresolved={unresolved} | {rate:.1f}/min")
                continue  # dry-run: no DB writes, no checkpoint

            # ---- apply ----
            rows, n_manual, n_auto, inserted = [], 0, 0, 0
            recover = resolution in ("same_domain", "cross_domain")
            if recover and args.same_domain_only and resolution == "cross_domain":
                skipped_cross += 1
                recover = False
            if recover:
                outbound = fetch_lead_outbound(session, limiter, campaigned)
                for it in outbound:
                    row = isync.parse_email(it, campaigns_map, tags_map, lead_labels,
                                            email_type="sent")
                    if not row["instantly_message_id"]:
                        continue
                    row["lead_email"] = tracker_email  # attribute to tracker row
                    rows.append(row)
                    if it.get("ue_type") == 3:
                        n_manual += 1
                        totals["manual"] += 1
                    else:
                        n_auto += 1
                        totals["auto"] += 1
                inserted = isync.upsert_rows(supabase, rows, "sent")
                new_rows_total += inserted

            map_w.writerow([tracker_email, campaigned, n_manual, n_auto, inserted])
            map_f.flush()
        except SystemExit:
            print(f"  ! giving up on {tracker_email} (retries exhausted); will resume later",
                  file=sys.stderr)
            continue
        except Exception as e:
            print(f"  ! error on {tracker_email}: {type(e).__name__}: {str(e)[:120]}; skipping",
                  file=sys.stderr)
            continue

        mark_done(tracker_email)

        if i % 10 == 0 or i == len(pending):
            el = time.monotonic() - start
            rate = i / el * 60 if el else 0
            eta = (len(pending) - i) / max(rate, 0.01)
            print(f"  [{i}/{len(pending)}] resolved={resolved} same={same} "
                  f"unresolved={unresolved} skipped_cross={skipped_cross} | "
                  f"manual={totals['manual']} newrows={new_rows_total} | "
                  f"{rate:.1f}/min ETA {eta:.0f}m")

    map_f.close()
    if args.dry_run:
        print("\n=== DRY-RUN summary (no DB writes) ===")
        print(f"  processed:        {len(pending)}")
        print(f"  same_domain:      {resolved - cross}  (safe to auto-apply)")
        print(f"  cross_domain:     {cross}  (needs review — may attribute wrong lead)")
        print(f"  same_email:       {same}  (no recoverable outbound)")
        print(f"  unresolved:       {unresolved}  (reply 404 / no lead field)")
        print(f"  mapping -> {out_csv}")
        return
    print("\n=== reconciliation summary ===")
    print(f"  processed:            {len(pending)}")
    print(f"  resolved (remapped):  {resolved}")
    print(f"  already-correct:      {same}")
    print(f"  unresolved (no lead): {unresolved}")
    print(f"  outbound: manual={totals['manual']} auto={totals['auto']}")
    print(f"  NEW sent_messages rows: {new_rows_total}")
    print(f"  mapping -> {MAP_CSV}")


if __name__ == "__main__":
    main()
