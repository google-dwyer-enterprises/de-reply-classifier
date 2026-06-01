"""One-shot migration runner for FOLLOWUP_ANALYSIS_PLAN.md Phase 1.1 + 1.2.

Applies the additive DDL block from migrations.sql (the "Follow-up Tracker v3"
section) against production Supabase, then verifies the columns/tables exist.

Safe to re-run — all statements use `if not exists`.

Usage:
    python scripts/apply_v3_migrations.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from db import connect


DDL = """
-- Phase 1.1 — extend sent_messages
alter table sent_messages add column if not exists ue_type smallint;
alter table sent_messages add column if not exists step text;
alter table sent_messages add column if not exists send_kind text
  generated always as (case
    when step is null then 'unibox_manual'
    when ue_type = 1 then 'campaign_auto'
    else 'unknown'
  end) stored;
alter table sent_messages add column if not exists thread_id text;
create index if not exists sent_messages_send_kind_idx on sent_messages (send_kind);
create index if not exists sent_messages_thread_id_idx on sent_messages (thread_id);

-- Phase 1.2 — lead_outcomes table
create table if not exists lead_outcomes (
  lead_email text not null,
  client text not null,
  campaign text not null default '',
  leadlist_source text,
  status_raw text,
  qualified text,
  note text,
  call_ffup text,
  source text not null default 'manual_tracker_csv',
  updated_at timestamptz default now(),
  primary key (lead_email, client, campaign)
);
create index if not exists lead_outcomes_lead_idx on lead_outcomes (lead_email);
"""


def main() -> None:
    print("Connecting to production Supabase...")
    conn = connect()
    conn.autocommit = True

    print("Applying Phase 1.1 + 1.2 DDL (idempotent)...")
    with conn.cursor() as cur:
        cur.execute(DDL)
    print("  DDL executed (no error == success).\n")

    print("Verifying sent_messages new columns...")
    with conn.cursor() as cur:
        cur.execute("""
            select attname, format_type(atttypid, atttypmod), attgenerated
            from pg_attribute
            where attrelid = 'public.sent_messages'::regclass
              and attnum > 0 and not attisdropped
              and attname in ('ue_type', 'step', 'send_kind', 'thread_id')
            order by attname
        """)
        rows = cur.fetchall()
    for name, dtype, generated in rows:
        gen_tag = " (GENERATED stored)" if generated == "s" else ""
        print(f"  [OK] sent_messages.{name}  {dtype}{gen_tag}")

    print("\nVerifying sent_messages indexes...")
    with conn.cursor() as cur:
        cur.execute("""
            select indexname from pg_indexes
            where schemaname = 'public' and tablename = 'sent_messages'
              and indexname in ('sent_messages_send_kind_idx', 'sent_messages_thread_id_idx')
            order by indexname
        """)
        rows = cur.fetchall()
    for (name,) in rows:
        print(f"  [OK] {name}")

    print("\nVerifying lead_outcomes table...")
    with conn.cursor() as cur:
        cur.execute("""
            select attname, format_type(atttypid, atttypmod)
            from pg_attribute
            where attrelid = 'public.lead_outcomes'::regclass
              and attnum > 0 and not attisdropped
            order by attnum
        """)
        rows = cur.fetchall()
    for name, dtype in rows:
        print(f"  [OK] lead_outcomes.{name}  {dtype}")

    print("\nVerifying lead_outcomes index...")
    with conn.cursor() as cur:
        cur.execute("""
            select indexname from pg_indexes
            where schemaname = 'public' and tablename = 'lead_outcomes'
              and indexname = 'lead_outcomes_lead_idx'
        """)
        rows = cur.fetchall()
    for (name,) in rows:
        print(f"  [OK] {name}")

    print("\nVerifying sent_messages is still empty (sanity check)...")
    with conn.cursor() as cur:
        cur.execute("select count(*) from sent_messages")
        sm_count = cur.fetchone()[0]
        cur.execute("select count(*) from lead_outcomes")
        lo_count = cur.fetchone()[0]
    print(f"  sent_messages rows: {sm_count}  (expect 0 today)")
    print(f"  lead_outcomes rows: {lo_count}  (expect 0 today)")

    conn.close()
    print("\nDone. Phase 1.1 + 1.2 migrations applied successfully.")


if __name__ == "__main__":
    main()
