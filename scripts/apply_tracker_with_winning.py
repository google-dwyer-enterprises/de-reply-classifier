"""Update followup_tracker_mv to include the 3 winning-reply columns.

Adds (left-joined from followup_winning_selection):
  - "Winning Ffup #"        — which follow-up number won (1, 2, 3, ...)
  - "Confidence"             — high / medium / low / fallback
  - "Why We Think It Won"    — Haiku's rationale

Null for leads without a selection (not booked, or backfill hasn't
covered their outbounds yet). Re-runnable (drop + create).
"""

import sys
import time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from db import connect


DDL = """
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
    s.id, s.lead_email, s.sent_timestamp, s.body,
    row_number() over (
      partition by s.lead_email order by s.sent_timestamp asc
    ) as ffup_n
  from sent_messages s
  where s.send_kind = 'unibox_manual'
),
ffup_counts as (
  select lead_email, count(*) as cnt
  from sent_messages
  where send_kind = 'unibox_manual'
  group by lead_email
),
latest_selection as (
  select distinct on (lead_email) *
  from followup_winning_selection
  order by lead_email, selected_at desc
),
winning_ffup_n as (
  select ls.lead_email, ro.ffup_n
  from latest_selection ls
  join ranked_outbounds ro on ro.id = ls.winning_sent_message_id
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
  coalesce(fc.cnt, 0)                                        as "Total Follow-ups",
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
  max(case when ro.ffup_n = 9  then ro.sent_timestamp end)   as "Email ffup 9 Date",
  max(case when ro.ffup_n = 9  then ro.body end)             as "Sent ff 9",
  max(case when ro.ffup_n = 10 then ro.sent_timestamp end)   as "Email ffup 10 Date",
  max(case when ro.ffup_n = 10 then ro.body end)             as "Sent ff 10",
  max(case when ro.ffup_n = 11 then ro.sent_timestamp end)   as "Email ffup 11 Date",
  max(case when ro.ffup_n = 11 then ro.body end)             as "Sent ff 11",
  max(case when ro.ffup_n = 12 then ro.sent_timestamp end)   as "Email ffup 12 Date",
  max(case when ro.ffup_n = 12 then ro.body end)             as "Sent ff 12",
  max(case when ro.ffup_n = 13 then ro.sent_timestamp end)   as "Email ffup 13 Date",
  max(case when ro.ffup_n = 13 then ro.body end)             as "Sent ff 13",
  max(case when ro.ffup_n = 14 then ro.sent_timestamp end)   as "Email ffup 14 Date",
  max(case when ro.ffup_n = 14 then ro.body end)             as "Sent ff 14",
  max(case when ro.ffup_n = 15 then ro.sent_timestamp end)   as "Email ffup 15 Date",
  max(case when ro.ffup_n = 15 then ro.body end)             as "Sent ff 15",
  max(case when ro.ffup_n = 16 then ro.sent_timestamp end)   as "Email ffup 16 Date",
  max(case when ro.ffup_n = 16 then ro.body end)             as "Sent ff 16",
  max(case when ro.ffup_n = 17 then ro.sent_timestamp end)   as "Email ffup 17 Date",
  max(case when ro.ffup_n = 17 then ro.body end)             as "Sent ff 17",
  max(case when ro.ffup_n = 18 then ro.sent_timestamp end)   as "Email ffup 18 Date",
  max(case when ro.ffup_n = 18 then ro.body end)             as "Sent ff 18",
  max(case when ro.ffup_n = 19 then ro.sent_timestamp end)   as "Email ffup 19 Date",
  max(case when ro.ffup_n = 19 then ro.body end)             as "Sent ff 19",
  max(case when ro.ffup_n = 20 then ro.sent_timestamp end)   as "Email ffup 20 Date",
  max(case when ro.ffup_n = 20 then ro.body end)             as "Sent ff 20",
  lo.note                                                    as "NOTE (JOYCE)",
  lr.reply_timestamp                                         as "Last Reply At",
  left(lr.body, 500)                                         as "Last reply from Instantly",
  wn.ffup_n                                                  as "Winning Ffup #",
  ls.confidence                                              as "Confidence",
  ls.winning_subject                                         as "Winning Subject",
  left(ls.winning_body, 500)                                 as "Winning Message",
  left(ls.booking_reply_body, 300)                           as "What Lead Said (Booking)",
  ls.rationale                                               as "Why We Think It Won"
from lead_outcomes lo
left join leads l                  on l.lead_email = lo.lead_email
left join first_reply fr           on fr.lead_email = lo.lead_email
left join last_reply lr            on lr.lead_email = lo.lead_email
left join ffup_counts fc           on fc.lead_email = lo.lead_email
left join ranked_outbounds ro      on ro.lead_email = lo.lead_email
left join latest_selection ls      on ls.lead_email = lo.lead_email
left join winning_ffup_n wn        on wn.lead_email = lo.lead_email
group by
  lo.client, lo.lead_email, lo.campaign, lo.leadlist_source,
  lo.status_raw, l.auto_status, lo.qualified,
  fr.reply_timestamp, fr.body,
  lo.call_ffup, lo.note,
  lr.reply_timestamp, lr.body,
  fc.cnt,
  wn.ffup_n, ls.confidence, ls.rationale,
  ls.winning_subject, ls.winning_body, ls.booking_reply_body;
"""


def main():
    conn = connect()
    conn.autocommit = True

    print("Dropping + recreating followup_tracker_mv (with winning-reply columns)...")
    t0 = time.monotonic()
    with conn.cursor() as cur:
        cur.execute(DDL)
    print(f"  [OK] {(time.monotonic() - t0)*1000:.0f}ms")

    # Verify
    with conn.cursor() as cur:
        cur.execute("""
            select count(*) from pg_attribute
            where attrelid = 'public.followup_tracker_mv'::regclass
              and attnum > 0 and not attisdropped
        """)
        col_count = cur.fetchone()[0]

        cur.execute("select count(*) from followup_tracker_mv")
        row_count = cur.fetchone()[0]

        cur.execute('''
            select count(*) from followup_tracker_mv
            where "Winning Ffup #" is not null
        ''')
        with_winner = cur.fetchone()[0]

    print(f"  Columns: {col_count}")
    print(f"  Rows: {row_count}")
    print(f"  With winning reply: {with_winner}")

    # Show the winning-reply rows
    if with_winner:
        with conn.cursor() as cur:
            cur.execute('''
                select "Email Address", "Winning Ffup #", "Confidence",
                       "Why We Think It Won"
                from followup_tracker_mv
                where "Winning Ffup #" is not null
            ''')
            print(f"\n  Leads with winning reply identified:")
            for em, ffup_n, conf, why in cur.fetchall():
                print(f"    {em!r}  ffup#{ffup_n}  conf={conf!r}")
                print(f"      {why!r}")

    conn.close()
    print("\nDone. Trigger NocoDB meta-sync to surface the 3 new columns.")


if __name__ == "__main__":
    main()
