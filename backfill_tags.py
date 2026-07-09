"""Backfill `tags` column on replies and sent_messages from campaign tag mappings.

Reuses fetch_campaign_tags() from instantly_sync. Idempotent — running it again
on already-tagged rows is a no-op write.

Hardened 2026-07-09 (cron OOM/kill fix — see project-railway-deploy-state): the
previous version fired one PostgREST UPDATE per campaign per table (~643 × 2 =
~1,286 round-trips) and pulled every updated row back into memory via
`return=representation`. That was the heavy, slow step whose completion the daily
cron kept dying right after (starving the sync). This version collapses it into
ONE server-side, set-based SQL UPDATE per table via psycopg2 — no per-campaign
round-trips, no row payloads copied into Python — so it's fast and memory-flat.
The `is distinct from` guard + the campaign_id index keep each UPDATE to only the
rows whose tags genuinely changed (near-zero after the first pass; new rows are
already tagged at insert time by instantly_sync.parse_email).
"""

from __future__ import annotations

import sys

from dotenv import load_dotenv
from psycopg2.extras import execute_values

from db import connect
from instantly_sync import (
    DEFAULT_MIN_INTERVAL_S,
    RateLimiter,
    fetch_campaign_tags,
    get_env,
    make_session,
)

TABLES = ("replies", "sent_messages")


def backfill_table(cur, table: str, tags_map: dict[str, list[str]]) -> int:
    """One set-based UPDATE for the whole table: join the (campaign_id -> tags)
    map in as a VALUES list and write only rows whose tags actually differ.
    Returns the number of rows updated (cur.rowcount)."""
    if not tags_map:
        return 0
    rows = [(cid, list(tags)) for cid, tags in tags_map.items()]
    # table name is a fixed constant from TABLES, never user input.
    sql = (
        f"update {table} t "
        f"   set tags = v.tags "
        f"  from (values %s) as v(campaign_id, tags) "
        f" where t.campaign_id = v.campaign_id "
        f"   and t.tags is distinct from v.tags"
    )
    execute_values(cur, sql, rows, template="(%s, %s::text[])", page_size=1000)
    return cur.rowcount


def main() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    load_dotenv()
    instantly_key = get_env("INSTANTLY_API_KEY")

    session = make_session(instantly_key)
    limiter = RateLimiter(DEFAULT_MIN_INTERVAL_S)

    tags_map = fetch_campaign_tags(session, limiter)
    total_links = sum(len(v) for v in tags_map.values())
    print(f"Loaded {total_links} campaign-tag links across {len(tags_map)} campaigns")

    if not tags_map:
        print("No tag mappings found; nothing to backfill.")
        return

    conn = connect()
    conn.autocommit = True
    try:
        cur = conn.cursor()
        for table in TABLES:
            n = backfill_table(cur, table, tags_map)
            print(f"  {table}: updated {n} rows")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
