"""One-shot migration runner for the per-lead approval workflow (Flavor C).

Adds `prospeo_new_leads.lead_approval` (nullable text with CHECK) and
`prospeo_new_leads.lead_moved_at` (timestamptz). Per-lead approval drives
the worker's move-to-lead_contacts logic so Jam can approve/reject leads
one-by-one inside a batch and re-open the batch later to continue.

Safe to re-run — all statements use `if not exists`.

Usage:
    python scripts/apply_lead_approval_schema.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from db import connect


DDL = """
-- Per-lead approval state.
-- NULL on rows that came from the CLI scraper (no scrape_request_id).
-- For worker-tagged rows: starts 'pending' for BC-accepted leads, 'rejected'
-- for BC-auto-rejected ones; Jam moves them to 'approved' or 'rejected'
-- inside the NocoDB per-batch grid.
alter table prospeo_new_leads
  add column if not exists lead_approval text
    check (lead_approval is null or
           lead_approval in ('pending', 'approved', 'rejected'));

-- Stamp when the worker actually copies the lead into lead_contacts.
-- NULL means "not moved yet" — lets the worker distinguish "approved but
-- still queued" from "approved and already in the 200k pool".
alter table prospeo_new_leads
  add column if not exists lead_moved_at timestamptz;

-- Index targeted at the worker's hot path: find approved-but-not-yet-moved
-- leads. Partial so the index stays tiny even though prospeo_new_leads has
-- 30k+ rows.
create index if not exists prospeo_new_leads_pending_move_idx
  on prospeo_new_leads (scrape_request_id)
  where lead_approval = 'approved' and lead_moved_at is null;
"""


def main() -> None:
    print("Connecting to production Supabase...")
    conn = connect()
    conn.autocommit = True

    print("Applying lead_approval / lead_moved_at DDL (idempotent)...")
    with conn.cursor() as cur:
        cur.execute(DDL)
    print("  DDL executed (no error == success).\n")

    print("Verifying prospeo_new_leads new columns...")
    with conn.cursor() as cur:
        cur.execute("""
            select attname, format_type(atttypid, atttypmod), attnotnull
            from pg_attribute
            where attrelid = 'public.prospeo_new_leads'::regclass
              and attnum > 0 and not attisdropped
              and attname in ('lead_approval', 'lead_moved_at')
            order by attname
        """)
        rows = cur.fetchall()
    if not rows:
        sys.exit("FAIL: neither column was added")
    for name, dtype, notnull in rows:
        nntag = " NOT NULL" if notnull else ""
        print(f"  [OK] prospeo_new_leads.{name}  {dtype}{nntag}")

    print("\nVerifying CHECK constraint on lead_approval...")
    with conn.cursor() as cur:
        cur.execute("""
            select pg_get_constraintdef(c.oid)
              from pg_constraint c
              join pg_class t on t.oid = c.conrelid
             where t.relname = 'prospeo_new_leads'
               and c.contype = 'c'
               and pg_get_constraintdef(c.oid) like '%lead_approval%'
        """)
        rows = cur.fetchall()
    for (defn,) in rows:
        print(f"  [OK] {defn}")
    if not rows:
        print("  ! WARN: no CHECK constraint found for lead_approval")

    print("\nVerifying partial index on (scrape_request_id) where "
          "lead_approval='approved' and lead_moved_at is null...")
    with conn.cursor() as cur:
        cur.execute("""
            select indexname, indexdef
              from pg_indexes
             where schemaname = 'public'
               and tablename = 'prospeo_new_leads'
               and indexname = 'prospeo_new_leads_pending_move_idx'
        """)
        rows = cur.fetchall()
    for name, defn in rows:
        print(f"  [OK] {name}\n       {defn}")

    print("\nSanity counts (current state)...")
    with conn.cursor() as cur:
        cur.execute("""
            select
              count(*) filter (where scrape_request_id is not null) as worker_rows,
              count(*) filter (where lead_approval is null)        as approval_null,
              count(*) filter (where lead_approval = 'pending')    as approval_pending,
              count(*) filter (where lead_approval = 'approved')   as approval_approved,
              count(*) filter (where lead_approval = 'rejected')   as approval_rejected,
              count(*) filter (where lead_moved_at is not null)    as moved
              from prospeo_new_leads
        """)
        r = cur.fetchone()
    print(f"  worker-tagged rows : {r[0]:,}")
    print(f"  lead_approval NULL : {r[1]:,}  (all pre-migration rows)")
    print(f"  ...        pending : {r[2]:,}")
    print(f"  ...       approved : {r[3]:,}")
    print(f"  ...       rejected : {r[4]:,}")
    print(f"  lead_moved_at set  : {r[5]:,}")

    conn.close()
    print("\nDone.")


if __name__ == "__main__":
    main()
