"""One-time CSV ingest for Jam's manual follow-up tracker.

Loads original_data/followup_tracker_2026-05-19.csv into the lead_outcomes
table, capturing the columns that the Instantly API doesn't give us:
Client, Campaign, Leadlist Source, Status, Qualified, NOTE (JOYCE), Call ffup.

Does NOT load any message bodies/dates — those come from sent_messages
(Phase 2 sync) and replies (already exists). The CSV is a one-time
snapshot; after this runs, lead_outcomes is updated only when new manual
fields are needed for specific leads.

CLI:
    python run.py upload-followup-tracker original_data/followup_tracker_2026-05-19.csv

Reference: FOLLOWUP_ANALYSIS_PLAN.md §Phase 3.
"""

from __future__ import annotations

import csv
import os
import sys
from collections import Counter
from pathlib import Path

from dotenv import load_dotenv
from supabase import create_client

# Column index map for the 2026-05-19 CSV. Hard-coded because the source
# file is a snapshot; the column order has been stable across iterations.
# Indices are 0-based here (Python convention), 1-based in the plan docs.
COL_CLIENT          = 0
COL_EMAIL           = 1
COL_CAMPAIGN        = 2
COL_LEADLIST_SOURCE = 3
COL_STATUS          = 4
COL_QUALIFIED       = 5
COL_CALL_FFUP       = 24   # "Call ffup"
COL_NOTE            = 27   # "NOTE (JOYCE)"

UPSERT_BATCH = 200


def is_valid_email(s: str) -> bool:
    """Mirror the filter in probe_outbound_v4.py:pick_probes() so we skip
    the same malformed rows."""
    if not s:
        return False
    if "\n" in s or "," in s:
        return False
    if "@" not in s:
        return False
    return True


def parse_csv(path: Path) -> tuple[list[dict], dict]:
    """Returns (rows_to_upsert, counters).

    counters tracks: total seen, skipped per-reason, qualified breakdown,
    status breakdown — for the summary print at the end.
    """
    rows: list[dict] = []
    counters = {
        "total": 0,
        "skipped_short_row": 0,
        "skipped_bad_email": 0,
        "skipped_empty_client": 0,
        "qualified": Counter(),
        "status": Counter(),
        "with_note": 0,
        "with_call_ffup": 0,
    }
    seen_keys: set[tuple[str, str, str]] = set()

    with open(path, "r", encoding="utf-8", errors="replace", newline="") as f:
        reader = csv.reader(f)
        header = next(reader)
        if len(header) < 36:
            sys.exit(f"FATAL: CSV header has {len(header)} cols; expected >= 36. "
                     f"Wrong file?")
        for raw in reader:
            counters["total"] += 1
            if len(raw) < 36:
                counters["skipped_short_row"] += 1
                continue
            email = raw[COL_EMAIL].strip().lower()
            if not is_valid_email(email):
                counters["skipped_bad_email"] += 1
                continue
            client = raw[COL_CLIENT].strip()
            if not client:
                counters["skipped_empty_client"] += 1
                continue
            campaign = raw[COL_CAMPAIGN].strip()
            key = (email, client, campaign)
            if key in seen_keys:
                # Same lead/client/campaign appearing twice in the sheet —
                # keep first (matches Postgres on conflict do nothing semantics).
                continue
            seen_keys.add(key)

            qualified = raw[COL_QUALIFIED].strip() or None
            status_raw = raw[COL_STATUS].strip() or None
            note = raw[COL_NOTE].strip() if len(raw) > COL_NOTE else ""
            call_ffup = raw[COL_CALL_FFUP].strip() if len(raw) > COL_CALL_FFUP else ""

            counters["qualified"][qualified or "(null)"] += 1
            if status_raw:
                counters["status"][status_raw] += 1
            if note:
                counters["with_note"] += 1
            if call_ffup:
                counters["with_call_ffup"] += 1

            rows.append({
                "lead_email": email,
                "client": client,
                "campaign": campaign,
                "leadlist_source": raw[COL_LEADLIST_SOURCE].strip() or None,
                "status_raw": status_raw,
                "qualified": qualified,
                "note": note or None,
                "call_ffup": call_ffup or None,
                "source": "manual_tracker_csv",
            })

    return rows, counters


def chunk(seq, n):
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


def upsert_rows(supabase, rows: list[dict]) -> int:
    """Upsert into lead_outcomes; conflict key is the composite PK
    (lead_email, client, campaign). Returns count of rows touched."""
    if not rows:
        return 0
    total = 0
    for batch in chunk(rows, UPSERT_BATCH):
        resp = (
            supabase.table("lead_outcomes")
            .upsert(batch, on_conflict="lead_email,client,campaign")
            .execute()
        )
        total += len(resp.data or [])
    return total


def main(csv_path: str) -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    path = Path(csv_path)
    if not path.exists():
        sys.exit(f"FATAL: file not found: {csv_path}")

    load_dotenv()
    supabase_url = os.environ.get("SUPABASE_URL", "").strip()
    supabase_key = os.environ.get("SUPABASE_KEY", "").strip()
    if not supabase_url or not supabase_key:
        sys.exit("FATAL: SUPABASE_URL or SUPABASE_KEY missing in .env")
    supabase = create_client(supabase_url, supabase_key)

    print(f"Reading {path}...")
    rows, counters = parse_csv(path)
    print(f"  parsed {len(rows)} valid rows of {counters['total']} total")
    if counters["skipped_short_row"]:
        print(f"  skipped {counters['skipped_short_row']} short rows (< 36 cols)")
    if counters["skipped_bad_email"]:
        print(f"  skipped {counters['skipped_bad_email']} rows with malformed email")
    if counters["skipped_empty_client"]:
        print(f"  skipped {counters['skipped_empty_client']} rows with empty client")

    print(f"\nUpserting into lead_outcomes (batch={UPSERT_BATCH})...")
    upserted = upsert_rows(supabase, rows)
    print(f"  upserted {upserted} rows")

    print("\n=== summary ===")
    print(f"  Total parsed:      {len(rows)}")
    print(f"  With NOTE:         {counters['with_note']}")
    print(f"  With Call ffup:    {counters['with_call_ffup']}")
    print(f"\n  Qualified breakdown:")
    for q, n in counters["qualified"].most_common():
        print(f"    {q!r}: {n}")
    print(f"\n  Top 10 Status values:")
    for s, n in counters["status"].most_common(10):
        print(f"    {s!r}: {n}")

    # Verify DB rowcount
    try:
        verify = supabase.table("lead_outcomes").select("*", count="exact", head=True).execute()
        print(f"\nlead_outcomes total rowcount after upload: {verify.count}")
    except Exception as e:
        print(f"\n  ! verify query failed: {e}", file=sys.stderr)


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit("Usage: python followup_tracker_upload.py <path-to-csv>")
    main(sys.argv[1])
