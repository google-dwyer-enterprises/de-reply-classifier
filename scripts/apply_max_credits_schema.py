"""Migration: add scrape_requests.max_credits column.

When NULL the worker uses the auto-computed budget
  max(requested_leads * CREDITS_PER_LEAD_BUDGET, page_limit * 3 + 5)
When set Jam (or whoever submits) explicitly overrides the cap. Useful
when a filter is sparse and she wants more headroom, or when she wants
a hard ceiling regardless of how the hit rate plays out.

Safe to re-run.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from db import connect


DDL = """
alter table scrape_requests
  add column if not exists max_credits integer
    check (max_credits is null or max_credits between 1 and 100000);
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
            select column_name, data_type, column_default
              from information_schema.columns
             where table_schema='public' and table_name='scrape_requests'
               and column_name='max_credits'
        """)
        for name, dtype, default in cur.fetchall():
            print(f"  [OK] scrape_requests.{name}: {dtype}  default={default}")

    conn.close()


if __name__ == "__main__":
    main()
