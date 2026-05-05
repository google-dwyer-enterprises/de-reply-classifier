"""One-time backfill: populate classifications.reason from raw_response.

No API calls. Parses the already-stored Claude response JSON and
writes the `reason` field to its own column. Safe to re-run (only
updates rows where reason IS NULL).
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv
from supabase import create_client


def _paginate(query_builder, page_size: int = 1000) -> list[dict]:
    out = []
    start = 0
    while True:
        resp = query_builder.range(start, start + page_size - 1).execute()
        batch = resp.data or []
        out.extend(batch)
        if len(batch) < page_size:
            return out
        start += page_size


def extract_reason(raw_response, target_reply_id) -> str | None:
    if raw_response is None:
        return None
    try:
        obj = json.loads(raw_response) if isinstance(raw_response, str) else raw_response
    except Exception:
        return None

    if isinstance(obj, list):
        for item in obj:
            if not isinstance(item, dict):
                continue
            if item.get("id") == target_reply_id and item.get("reason"):
                return str(item["reason"])[:500]
        for item in obj:
            if isinstance(item, dict) and item.get("reason"):
                return str(item["reason"])[:500]
    elif isinstance(obj, dict):
        if obj.get("reason"):
            return str(obj["reason"])[:500]
    return None


def main() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    load_dotenv()
    supabase = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_KEY"])

    print("Fetching classifications with NULL reason...", flush=True)
    rows = _paginate(
        supabase.table("classifications")
        .select("id, reply_id, raw_response")
        .is_("reason", "null")
    )
    print(f"  {len(rows)} rows to backfill", flush=True)

    updated = 0
    skipped_no_reason = 0
    for i, row in enumerate(rows, 1):
        reason = extract_reason(row.get("raw_response"), row.get("reply_id"))
        if not reason:
            skipped_no_reason += 1
            continue
        supabase.table("classifications").update({"reason": reason}).eq("id", row["id"]).execute()
        updated += 1
        if updated % 500 == 0:
            print(f"  updated {updated}/{len(rows)}...", flush=True)

    print()
    print(f"Backfill complete. updated={updated}  skipped_no_reason={skipped_no_reason}")


if __name__ == "__main__":
    main()
