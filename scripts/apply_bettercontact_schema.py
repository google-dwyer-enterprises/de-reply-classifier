"""Apply BetterContact schema additions to Supabase.

Idempotent — uses `if not exists` so re-running is safe. Verifies the new
columns/table exist before reporting success.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from db import connect

DDL = """
alter table prospeo_new_leads
  add column if not exists provider text not null default 'prospeo';

alter table prospeo_new_leads
  add column if not exists bettercontact_raw jsonb;

create table if not exists bettercontact_scrape_state (
  industry text primary key,
  countries text[] not null default '{}',
  last_offset_consumed integer not null default 0,
  total_leads_estimated integer,
  exhausted boolean not null default false,
  last_scraped_at timestamptz,
  total_credits_spent numeric(10,1) not null default 0
);
"""


def main() -> None:
    conn = connect()
    conn.autocommit = True
    cur = conn.cursor()

    print("Applying BetterContact schema DDL...")
    cur.execute(DDL)
    print("[OK] DDL applied")

    # Verify columns
    cur.execute("""
      select column_name from information_schema.columns
      where table_schema='public' and table_name='prospeo_new_leads'
        and column_name in ('provider', 'bettercontact_raw')
      order by column_name
    """)
    cols = [r[0] for r in cur.fetchall()]
    assert cols == ['bettercontact_raw', 'provider'], f"unexpected: {cols}"
    print(f"[OK] prospeo_new_leads has columns: {cols}")

    cur.execute("""
      select column_name from information_schema.columns
      where table_schema='public' and table_name='bettercontact_scrape_state'
      order by ordinal_position
    """)
    state_cols = [r[0] for r in cur.fetchall()]
    expected = ['industry', 'countries', 'last_offset_consumed',
                'total_leads_estimated', 'exhausted', 'last_scraped_at',
                'total_credits_spent']
    assert state_cols == expected, f"state cols mismatch: {state_cols}"
    print(f"[OK] bettercontact_scrape_state created with cols: {state_cols}")

    # Sanity: count provider partitions
    cur.execute("select provider, count(*) from prospeo_new_leads group by provider")
    print("\nLead count per provider:")
    for prov, n in cur.fetchall():
        print(f"  {prov}: {n}")

    cur.close()
    conn.close()
    print("\nDone.")


if __name__ == "__main__":
    main()
