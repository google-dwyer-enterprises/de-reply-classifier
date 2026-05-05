"""Backfill `tags` column on replies and sent_messages from campaign tag mappings.

Reuses fetch_campaign_tags() from instantly_sync. Idempotent — running it
again on already-tagged rows is a no-op write of the same array.
"""

from __future__ import annotations

import os
import sys

from dotenv import load_dotenv
from supabase import create_client

from instantly_sync import (
    DEFAULT_MIN_INTERVAL_S,
    RateLimiter,
    fetch_campaign_tags,
    get_env,
    make_session,
)

TABLES = ("replies", "sent_messages")


def backfill_table(supabase, table: str, tags_map: dict[str, list[str]]) -> int:
    total_updated = 0
    for cid, tags in tags_map.items():
        resp = (
            supabase.table(table)
            .update({"tags": tags})
            .eq("campaign_id", cid)
            .execute()
        )
        n = len(resp.data or [])
        total_updated += n
    return total_updated


def main() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    load_dotenv()
    instantly_key = get_env("INSTANTLY_API_KEY")
    supabase_url = get_env("SUPABASE_URL")
    supabase_key = get_env("SUPABASE_KEY")

    supabase = create_client(supabase_url, supabase_key)
    session = make_session(instantly_key)
    limiter = RateLimiter(DEFAULT_MIN_INTERVAL_S)

    tags_map = fetch_campaign_tags(session, limiter)
    total_links = sum(len(v) for v in tags_map.values())
    print(f"Loaded {total_links} campaign-tag links across {len(tags_map)} campaigns")

    if not tags_map:
        print("No tag mappings found; nothing to backfill.")
        return

    for table in TABLES:
        n = backfill_table(supabase, table, tags_map)
        print(f"  {table}: updated {n} rows")


if __name__ == "__main__":
    main()
