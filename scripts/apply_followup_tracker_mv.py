"""Create followup_tracker_mv per FOLLOWUP_ANALYSIS_PLAN.md Phase 1.3.

Drops + recreates the MV (idempotent — `drop if exists` + `create`). Safe to
re-run when the MV definition changes.

After applying:
  - Run `refresh materialized view concurrently followup_tracker_mv;` to populate.
  - Trigger NocoDB meta-sync so the new columns appear in the UI.

Usage:
    python scripts/apply_followup_tracker_mv.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from db import connect


DDL = """
drop materialized view if exists followup_tracker_mv;
create materialized view followup_tracker_mv as
with first_reply as (
  select distinct on (lead_email) lead_email, reply_timestamp, body
  from replies order by lead_email, reply_timestamp asc
),
last_reply as (
  select distinct on (lead_email) lead_email, reply_timestamp, body
  from replies order by lead_email, reply_timestamp desc
),
ranked_outbounds as (
  select
    s.lead_email, s.sent_timestamp, s.body,
    row_number() over (
      partition by s.lead_email order by s.sent_timestamp asc
    ) as ffup_n
  from sent_messages s
  where s.send_kind = 'unibox_manual'
)
select
  lo.client                                                  as "Client",
  lo.lead_email                                              as "Email Address",
  lo.campaign                                                as "Campaign",
  lo.leadlist_source                                         as "Leadlist Source",
  coalesce(lo.status_raw, l.auto_status)                     as "Status",
  lo.qualified                                               as "Qualified",
  fr.reply_timestamp                                         as "Initial Reply Date",
  fr.body                                                    as "What was their initial reply",
  max(case when ro.ffup_n = 1  then ro.sent_timestamp end)   as "Email ffup 1 Date",
  max(case when ro.ffup_n = 1  then ro.body end)             as "Email FF 1 what we sent",
  max(case when ro.ffup_n = 2  then ro.sent_timestamp end)   as "Email ffup 2 Date",
  max(case when ro.ffup_n = 2  then ro.body end)             as "Sent ff 2",
  max(case when ro.ffup_n = 3  then ro.sent_timestamp end)   as "Email ffup 3 Date",
  max(case when ro.ffup_n = 3  then ro.body end)             as "Sent ff 3",
  max(case when ro.ffup_n = 4  then ro.sent_timestamp end)   as "Email ffup 4 Date",
  max(case when ro.ffup_n = 4  then ro.body end)             as "Sent ff 4",
  max(case when ro.ffup_n = 5  then ro.sent_timestamp end)   as "Email ffup 5 Date",
  max(case when ro.ffup_n = 5  then ro.body end)             as "Sent ff 5",
  max(case when ro.ffup_n = 6  then ro.sent_timestamp end)   as "Email ffup 6 Date",
  max(case when ro.ffup_n = 6  then ro.body end)             as "Sent ff 6",
  max(case when ro.ffup_n = 7  then ro.sent_timestamp end)   as "Email ffup 7 Date",
  max(case when ro.ffup_n = 7  then ro.body end)             as "Sent ff 7",
  max(case when ro.ffup_n = 8  then ro.sent_timestamp end)   as "Email ffup 8 Date",
  max(case when ro.ffup_n = 8  then ro.body end)             as "Sent ff 8",
  lo.call_ffup                                               as "Call ffup",
  max(case when ro.ffup_n = 9  then ro.sent_timestamp end)   as "Email ffup 9",
  max(case when ro.ffup_n = 10 then ro.sent_timestamp end)   as "Email ffup 10",
  lo.note                                                    as "NOTE (JOYCE)",
  lr.reply_timestamp                                         as "Last Reply At",
  left(lr.body, 500)                                         as "Last reply from Instantly"
from lead_outcomes lo
left join leads l                  on l.lead_email = lo.lead_email
left join first_reply fr           on fr.lead_email = lo.lead_email
left join last_reply lr            on lr.lead_email = lo.lead_email
left join ranked_outbounds ro      on ro.lead_email = lo.lead_email
group by
  lo.client, lo.lead_email, lo.campaign, lo.leadlist_source,
  lo.status_raw, l.auto_status, lo.qualified,
  fr.reply_timestamp, fr.body,
  lo.call_ffup, lo.note,
  lr.reply_timestamp, lr.body;

create unique index if not exists followup_tracker_mv_pk
  on followup_tracker_mv ("Email Address", "Client", "Campaign");
"""


def main() -> None:
    print("Connecting to production Supabase...")
    conn = connect()
    conn.autocommit = True

    print("Dropping + recreating followup_tracker_mv...")
    with conn.cursor() as cur:
        cur.execute(DDL)
    print("  DDL executed (MV created and populated on first SELECT).\n")

    # Verify columns
    print("Verifying MV columns:")
    with conn.cursor() as cur:
        cur.execute("""
            select attname, format_type(atttypid, atttypmod)
            from pg_attribute
            where attrelid = 'public.followup_tracker_mv'::regclass
              and attnum > 0 and not attisdropped
            order by attnum
        """)
        cols = cur.fetchall()
    for name, dtype in cols:
        print(f"  [OK] {name}  {dtype}")
    print(f"  Total: {len(cols)} columns")

    print("\nVerifying unique index:")
    with conn.cursor() as cur:
        cur.execute("""
            select indexname from pg_indexes
            where schemaname = 'public' and tablename = 'followup_tracker_mv'
        """)
        for (name,) in cur.fetchall():
            print(f"  [OK] {name}")

    print("\nMV rowcount + sample stats:")
    with conn.cursor() as cur:
        cur.execute("select count(*) from followup_tracker_mv")
        total = cur.fetchone()[0]
        print(f"  total rows: {total}")

        cur.execute("""select count(*) from followup_tracker_mv
                       where "Last Reply At" is not null""")
        with_last = cur.fetchone()[0]
        print(f"  with Last Reply At populated: {with_last}")

        cur.execute("""select count(*) from followup_tracker_mv
                       where "Email FF 1 what we sent" is not null""")
        with_ffup1 = cur.fetchone()[0]
        print(f"  with ffup 1 populated: {with_ffup1}")

        cur.execute("""select count(*) from followup_tracker_mv
                       where "Sent ff 5" is not null""")
        with_ffup5 = cur.fetchone()[0]
        print(f"  with ffup 5 populated: {with_ffup5}")

        cur.execute("""select "Status", count(*) from followup_tracker_mv
                       group by 1 order by 2 desc limit 8""")
        print("  Status breakdown (top 8):")
        for status, n in cur.fetchall():
            print(f"    {status!r}: {n}")

    conn.close()
    print("\nDone. Trigger NocoDB meta-sync to surface the MV in the UI.")


if __name__ == "__main__":
    main()
