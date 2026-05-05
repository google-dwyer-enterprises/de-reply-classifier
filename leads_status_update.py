"""Materialize per-lead status + score + reason + clients + campaigns onto `leads`.

Reuses excel_writer.fetch_per_lead_summary so NocoDB rows match the Excel
deliverable exactly. Manual columns (manual_status, manual_status_set_at, notes)
are never touched. Idempotent.

CLI: python run.py update-status
"""

from __future__ import annotations

import os
import sys
import time
from datetime import datetime, timezone

from dotenv import load_dotenv
from supabase import create_client

from excel_writer import fetch_per_lead_summary


CHUNK_SIZE = 500
MAX_RETRIES = 5
RETRY_BASE_DELAY = 2.0


def upsert_with_retry(supabase, chunk: list[dict]) -> None:
    last_exc: Exception | None = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            supabase.table("leads").upsert(chunk, on_conflict="lead_email").execute()
            return
        except Exception as exc:
            last_exc = exc
            if attempt == MAX_RETRIES:
                break
            delay = RETRY_BASE_DELAY * (2 ** (attempt - 1))
            print(f"    retry {attempt}/{MAX_RETRIES - 1} after error: "
                  f"{type(exc).__name__}: {exc} (sleeping {delay:.1f}s)")
            time.sleep(delay)
    raise last_exc  # type: ignore[misc]


def chunked(items: list, size: int):
    for i in range(0, len(items), size):
        yield items[i : i + size]


def main() -> None:
    load_dotenv()
    url = os.environ.get("SUPABASE_URL")
    key = os.environ.get("SUPABASE_KEY")
    if not url or not key:
        sys.exit("SUPABASE_URL and SUPABASE_KEY must be set in .env")

    supabase = create_client(url, key)

    summary = fetch_per_lead_summary(supabase)
    if not summary:
        print("No leads to update.")
        return

    now_iso = datetime.now(timezone.utc).isoformat()

    rows = []
    for email, s in summary.items():
        status1 = s.get("status1") or None
        status4 = s.get("status4") or None
        if not status1 and not status4:
            continue
        rows.append({
            "lead_email": email,
            "status1": status1,
            "status2": s.get("status2") or None,
            "status3": s.get("status3") or None,
            "status4": status4,
            "auto_status": status1,  # back-compat alias
            "auto_confidence": s.get("status_confidence"),
            "last_reply_at": s.get("last_reply_date"),
            "auto_status_updated_at": now_iso,
            "score": s.get("score"),
            "reason": s.get("reason") or None,
            "clients": s.get("clients") or None,
            "campaigns": s.get("campaigns") or None,
        })

    total = len(rows)
    print(f"Upserting {total} leads...")
    upserted = 0
    for i, chunk in enumerate(chunked(rows, CHUNK_SIZE), start=1):
        upsert_with_retry(supabase, chunk)
        upserted += len(chunk)
        if i % 5 == 0 or upserted == total:
            print(f"  chunk {i}: {upserted}/{total}")

    print(f"Done. {upserted} leads upserted.")

    print("Refreshing lead_status materialized view...")
    from db import refresh_lead_status
    refresh_lead_status()
    print("Refreshed.")


if __name__ == "__main__":
    main()
