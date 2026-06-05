"""Migration: per-batch NocoDB review view tracking.

Adds review_view_id, review_share_uuid, review_url columns to scrape_requests
so the worker can create a NocoDB grid view scoped to each batch at
mark_ready time, store its identifiers for cleanup later, and embed the
public share URL in Jam's email.

The per-batch view gives Jam structural cross-batch isolation: the view
filters on scrape_request_id = N, so header-checkbox / multi-select / bulk
edits inside it can ONLY touch this batch's leads.

Safe to re-run.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from db import connect


DDL = """
alter table scrape_requests
  add column if not exists review_view_id text,
  add column if not exists review_share_uuid text,
  add column if not exists review_url text;
"""


def main() -> None:
    print("Connecting to production Supabase...")
    conn = connect()
    conn.autocommit = True

    with conn.cursor() as cur:
        cur.execute(DDL)
    print("DDL applied.\n")

    with conn.cursor() as cur:
        cur.execute("""
            select column_name, data_type
              from information_schema.columns
             where table_schema='public' and table_name='scrape_requests'
               and column_name in ('review_view_id','review_share_uuid','review_url')
             order by column_name
        """)
        for name, dtype in cur.fetchall():
            print(f"  [OK] scrape_requests.{name}: {dtype}")

    conn.close()


if __name__ == "__main__":
    main()
