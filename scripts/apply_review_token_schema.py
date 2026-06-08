"""Migration: add scrape_requests.review_token for the lead-reviewer page.

Adds a UUID column with a server-side default of gen_random_uuid() so every
new scrape_requests row gets a random unguessable token without the worker
having to generate one. The lead-reviewer static page uses this token in
its /batch/<token> URL — same security model as NocoDB share UUIDs.

Backfills existing rows that don't have a token yet (running this against
a populated DB will give each historical row a token, though only future
rows are reachable from emails — historical rows can still be opened via
their token from a direct query).

Safe to re-run.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from db import connect


DDL = """
alter table scrape_requests
  add column if not exists review_token uuid default gen_random_uuid();

-- Backfill any rows that pre-date the column. The default only fires on
-- INSERT; existing rows need an explicit UPDATE.
update scrape_requests set review_token = gen_random_uuid()
 where review_token is null;

create unique index if not exists scrape_requests_review_token_idx
  on scrape_requests (review_token);
"""


def main() -> None:
    print("Connecting to production Supabase...")
    conn = connect()
    conn.autocommit = True

    with conn.cursor() as cur:
        cur.execute(DDL)
    print("DDL applied + existing rows backfilled.\n")

    with conn.cursor() as cur:
        cur.execute("""
            select column_name, data_type, column_default
              from information_schema.columns
             where table_schema='public' and table_name='scrape_requests'
               and column_name='review_token'
        """)
        for name, dtype, default in cur.fetchall():
            print(f"  [OK] scrape_requests.{name}: {dtype}  default={default}")

        cur.execute("select count(*), count(review_token) from scrape_requests")
        total, with_token = cur.fetchone()
    print(f"\n  rows total       : {total}")
    print(f"  rows with token  : {with_token}  (should equal total)")

    conn.close()


if __name__ == "__main__":
    main()
