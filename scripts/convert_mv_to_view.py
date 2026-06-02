"""Convert followup_tracker_mv from MATERIALIZED VIEW to a regular VIEW.

NocoDB v2026 auto-syncs tables and regular views, but not materialized views.
At 551 rows, the perf cost of a non-materialized view is negligible (<500ms
per query) and we get NocoDB auto-discovery in exchange.
"""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from db import connect


DDL = """
drop materialized view if exists followup_tracker_mv;
drop view if exists followup_tracker_mv;

create view followup_tracker_mv as
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
"""


def main() -> None:
    conn = connect()
    conn.autocommit = True

    print("Converting followup_tracker_mv: MATERIALIZED VIEW -> VIEW")
    with conn.cursor() as cur:
        cur.execute(DDL)
    print("  [OK] view created")

    # Verify
    with conn.cursor() as cur:
        cur.execute("""
            select 'matview' as kind from pg_matviews
             where schemaname='public' and matviewname='followup_tracker_mv'
            union all
            select 'view' as kind from pg_views
             where schemaname='public' and viewname='followup_tracker_mv'
        """)
        kinds = [r[0] for r in cur.fetchall()]
    print(f"  pg object kinds: {kinds}")

    with conn.cursor() as cur:
        cur.execute("select count(*) from followup_tracker_mv")
        total = cur.fetchone()[0]
    print(f"  rowcount via view: {total}")

    # Time the view query (vs MV cached perf)
    import time
    with conn.cursor() as cur:
        t0 = time.monotonic()
        cur.execute('select count(*) from followup_tracker_mv where "Status" = %s', ("Booked",))
        booked = cur.fetchone()[0]
        elapsed = (time.monotonic() - t0) * 1000
    print(f"  filter-by-Status='Booked' returned {booked} rows in {elapsed:.0f}ms")

    conn.close()
    print("\nDone. Re-run scripts/nocodb_sync.py to register the view in NocoDB.")


if __name__ == "__main__":
    main()
