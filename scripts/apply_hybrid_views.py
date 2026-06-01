"""Apply the hybrid follow-up tracker architecture.

Two views, both derived from the same source (sent_messages):

  followup_messages_mv  — LONG-FORM, one row per (lead_email * manual outbound).
                          NO CAP. ffup_n grows unbounded per lead.
                          Source of truth for "every follow-up we sent."

  followup_tracker_mv   — WIDE PIVOT, one row per lead.
                          Shows ffup 1-20 columns (cap for the UI).
                          Anything past ffup 20 is in messages_mv but not here.
                          + "Total Follow-ups" count column derived from
                            sent_messages directly (NOT capped) so consumers
                            know the true count.

Both views update live (no manual refresh needed). NocoDB sees them as
linked tables via Lead Email.

Re-runnable: drops + recreates both views via `drop view if exists`.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from db import connect


DDL_MESSAGES = """
drop view if exists followup_messages_mv;
create view followup_messages_mv as
select
  s.lead_email                                              as "Lead Email",
  row_number() over (
    partition by s.lead_email
    order by s.sent_timestamp asc
  )                                                         as "Ffup #",
  s.sent_timestamp                                          as "Sent At",
  s.subject                                                 as "Subject",
  s.body                                                    as "Body",
  s.send_kind                                               as "Send Kind",
  s.campaign_name                                           as "Campaign",
  s.client                                                  as "Client",
  s.instantly_message_id                                    as "Message ID"
from sent_messages s
where s.send_kind = 'unibox_manual';
"""

DDL_TRACKER = """
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
),
ffup_counts as (
  -- True count per lead, NOT capped. Drives the "Total Follow-ups" column.
  select lead_email, count(*) as cnt
  from sent_messages
  where send_kind = 'unibox_manual'
  group by lead_email
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
  -- ffup 1-20 (wide pivot for the UI; longer threads continue in followup_messages_mv)
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
  left(lr.body, 500)                                         as "Last reply from Instantly"
from lead_outcomes lo
left join leads l                  on l.lead_email = lo.lead_email
left join first_reply fr           on fr.lead_email = lo.lead_email
left join last_reply lr            on lr.lead_email = lo.lead_email
left join ffup_counts fc           on fc.lead_email = lo.lead_email
left join ranked_outbounds ro      on ro.lead_email = lo.lead_email
group by
  lo.client, lo.lead_email, lo.campaign, lo.leadlist_source,
  lo.status_raw, l.auto_status, lo.qualified,
  fr.reply_timestamp, fr.body,
  lo.call_ffup, lo.note,
  lr.reply_timestamp, lr.body,
  fc.cnt;
"""


def main() -> None:
    conn = connect()
    conn.autocommit = True

    # The tracker view references the messages view's NAME only indirectly
    # (through the same source query), so order doesn't matter — both are
    # built from sent_messages directly. But we drop tracker first since
    # NocoDB has a registration on it.
    print("Step 1: drop + create followup_messages_mv (long-form, no cap)")
    t0 = time.monotonic()
    with conn.cursor() as cur:
        cur.execute(DDL_MESSAGES)
    print(f"  [OK] {(time.monotonic() - t0)*1000:.0f}ms\n")

    print("Step 2: drop + create followup_tracker_mv (wide, ffup 1-20 + Total)")
    t0 = time.monotonic()
    with conn.cursor() as cur:
        cur.execute(DDL_TRACKER)
    print(f"  [OK] {(time.monotonic() - t0)*1000:.0f}ms\n")

    # Verify
    print("=== Verification ===\n")
    with conn.cursor() as cur:
        # 1. messages_mv columns
        cur.execute("""
            select attname from pg_attribute
            where attrelid = 'public.followup_messages_mv'::regclass
              and attnum > 0 and not attisdropped
            order by attnum
        """)
        msg_cols = [r[0] for r in cur.fetchall()]
        print(f"followup_messages_mv: {len(msg_cols)} cols")
        for c in msg_cols:
            print(f"  - {c}")
        print()

        # 2. tracker_mv column count
        cur.execute("""
            select count(*) from pg_attribute
            where attrelid = 'public.followup_tracker_mv'::regclass
              and attnum > 0 and not attisdropped
        """)
        track_cnt = cur.fetchone()[0]
        print(f"followup_tracker_mv: {track_cnt} cols (expect 51: 8 lead-level + Total + 20*2 ffup + Call ffup + NOTE + Last Reply At + Last reply)")
        print()

        # 3. rowcounts
        cur.execute("select count(*) from followup_messages_mv")
        msg_n = cur.fetchone()[0]
        cur.execute("select count(*) from followup_tracker_mv")
        track_n = cur.fetchone()[0]
        print(f"Row counts:")
        print(f"  followup_messages_mv: {msg_n}  (one row per manual outbound)")
        print(f"  followup_tracker_mv:  {track_n}  (one row per lead in lead_outcomes)")
        print()

        # 4. max ffup_n in messages (proves dynamic)
        cur.execute('select max("Ffup #") from followup_messages_mv')
        max_n = cur.fetchone()[0]
        print(f"Max ffup_n in messages_mv: {max_n}  (NO CAP — grows with data)")
        print()

        # 5. Verify Total Follow-ups matches the actual count
        cur.execute("""
            with leads_with_manuals as (
                select lead_email, count(*) as cnt
                from sent_messages
                where send_kind = 'unibox_manual'
                group by lead_email
            ),
            tracker_counts as (
                select "Email Address" as lead_email, "Total Follow-ups" as cnt
                from followup_tracker_mv
                where "Total Follow-ups" > 0
            )
            select count(*) as mismatches
            from leads_with_manuals lwm
            join tracker_counts tc
              on tc.lead_email = lwm.lead_email
             and tc.cnt <> lwm.cnt
        """)
        mismatches = cur.fetchone()[0]
        print(f"Mismatches between sent_messages count and tracker_mv 'Total Follow-ups': {mismatches}  (expect 0)")
        print()

        # 6. Sample lead with most ffups
        cur.execute('''
            select "Email Address", "Total Follow-ups"
            from followup_tracker_mv
            order by "Total Follow-ups" desc
            limit 3
        ''')
        print("Top 3 leads by Total Follow-ups:")
        for em, n in cur.fetchall():
            print(f"  {em}: {n} follow-ups")

    conn.close()
    print("\nDone. Re-register both views in NocoDB next.")


if __name__ == "__main__":
    main()
