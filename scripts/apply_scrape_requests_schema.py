"""Apply the scrape_requests + scrape_request_id schema to Supabase.

Idempotent — every statement uses IF NOT EXISTS / ADD COLUMN IF NOT EXISTS.
Safe to re-run.

See LEAD_AUTOMATION.md for context. Schema definition lives at the bottom of
migrations.sql under "Lead Scrape Automation (2026-06-04)".
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from db import connect


DDL = """
create table if not exists scrape_requests (
  id              bigserial primary key,
  requested_leads integer  not null check (requested_leads between 1 and 5000),
  industries      text[]   not null default '{}',
  skip_industries text[]   not null default '{}',
  countries       text[]   not null default '{United States,Canada}',
  notes           text,
  status          text     not null default 'pending'
                  check (status in ('pending','running','ready','moved','rejected','failed')),
  approval        text     not null default 'pending'
                  check (approval in ('pending','approved','rejected')),
  scraped_count   integer  not null default 0,
  moved_count     integer  not null default 0,
  credits_spent   numeric(10,1) not null default 0,
  created_at      timestamptz not null default now(),
  started_at      timestamptz,
  ready_at        timestamptz,
  moved_at        timestamptz,
  failed_at       timestamptz,
  email_sent_at   timestamptz,
  export_csv_path text,
  export_xlsx_path text,
  error_message   text
);
create index if not exists scrape_requests_status_idx on scrape_requests (status);
create index if not exists scrape_requests_approval_idx on scrape_requests (approval);

alter table prospeo_new_leads
  add column if not exists scrape_request_id bigint references scrape_requests(id);
create index if not exists prospeo_new_leads_scrape_request_id_idx
  on prospeo_new_leads (scrape_request_id) where scrape_request_id is not null;
"""


def main() -> None:
    conn = connect()
    conn.autocommit = True
    cur = conn.cursor()

    print("Applying scrape_requests schema...")
    cur.execute(DDL)
    print("[OK] DDL applied")

    # Verify table exists with the expected columns
    cur.execute("""
      select column_name
      from information_schema.columns
      where table_schema='public' and table_name='scrape_requests'
      order by ordinal_position
    """)
    cols = [r[0] for r in cur.fetchall()]
    print(f"\n[OK] scrape_requests columns ({len(cols)}):")
    for c in cols:
        print(f"  {c}")

    # Verify the new column on prospeo_new_leads
    cur.execute("""
      select column_name
      from information_schema.columns
      where table_schema='public' and table_name='prospeo_new_leads'
        and column_name='scrape_request_id'
    """)
    if cur.fetchone():
        print("\n[OK] prospeo_new_leads.scrape_request_id added")
    else:
        print("\n[FAIL] prospeo_new_leads.scrape_request_id missing")
        sys.exit(1)

    # Sanity: how many scrape_requests exist? (Should be 0 on first run)
    cur.execute("select count(*) from scrape_requests")
    print(f"\nscrape_requests rowcount: {cur.fetchone()[0]}")

    cur.close()
    conn.close()
    print("\nDone.")


if __name__ == "__main__":
    main()
