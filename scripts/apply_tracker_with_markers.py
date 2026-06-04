"""Replace followup_tracker_mv with a version that fills the
'Email FF 1 what we sent' column for booked leads with zero follow-ups,
explaining WHY the column is empty.

Categorization (only applied when Status='booked' AND Total Follow-ups=0):
  Cat A — auto-send fired before reply, no manual sent:
          "No follow-up needed — replied to initial campaign in this workspace"
  Cat B — first reply pre-dates sent_messages coverage (Aug 7 2025):
          "Pre-backfill (replies before Aug 7 2025; outbound history not captured)"
  Cat C — no campaign_auto AND reply within coverage:
          "Outreach sent via different Instantly workspace — outbound history not synced here"
  Cat D — no reply at all:
          "Booking via external channel — no reply tracked in Instantly"

This is idempotent — `CREATE OR REPLACE VIEW`. Rollback uses
debug/followup_tracker_mv_current.sql.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from db import connect


NEW_VIEW = """
CREATE OR REPLACE VIEW public.followup_tracker_mv AS
 WITH first_reply AS (
         SELECT DISTINCT ON (replies.lead_email) replies.lead_email,
            replies.reply_timestamp,
            replies.body
           FROM replies
          ORDER BY replies.lead_email, replies.reply_timestamp
        ), last_reply AS (
         SELECT DISTINCT ON (replies.lead_email) replies.lead_email,
            replies.reply_timestamp,
            replies.body
           FROM replies
          ORDER BY replies.lead_email, replies.reply_timestamp DESC
        ), ranked_outbounds AS (
         SELECT s.id,
            s.lead_email,
            s.sent_timestamp,
            s.body,
            row_number() OVER (PARTITION BY s.lead_email ORDER BY s.sent_timestamp) AS ffup_n
           FROM sent_messages s
          WHERE s.send_kind = 'unibox_manual'::text
        ), ffup_counts AS (
         SELECT sent_messages.lead_email,
            count(*) AS cnt
           FROM sent_messages
          WHERE sent_messages.send_kind = 'unibox_manual'::text
          GROUP BY sent_messages.lead_email
        ), latest_selection AS (
         SELECT DISTINCT ON (followup_winning_selection.lead_email) followup_winning_selection.lead_email,
            followup_winning_selection.winning_sent_message_id,
            followup_winning_selection.booking_reply_id,
            followup_winning_selection.candidate_message_ids,
            followup_winning_selection.confidence,
            followup_winning_selection.rationale,
            followup_winning_selection.model,
            followup_winning_selection.prompt_version,
            followup_winning_selection.selected_at,
            followup_winning_selection.winning_subject,
            followup_winning_selection.winning_body,
            followup_winning_selection.booking_reply_body
           FROM followup_winning_selection
          ORDER BY followup_winning_selection.lead_email, followup_winning_selection.selected_at DESC
        ), winning_ffup_n AS (
         SELECT ls_1.lead_email,
            ro_1.ffup_n
           FROM latest_selection ls_1
             JOIN ranked_outbounds ro_1 ON ro_1.id = ls_1.winning_sent_message_id
        ), booked_empty_marker AS (
         -- Emit a marker explaining WHY follow-ups are empty for booked leads
         -- with zero follow-ups. The expensive "did campaign_auto fire first?"
         -- check is an EXISTS subquery scoped to just these ~200 leads — uses
         -- the (lead_email) index, no full-table scan over 294K campaign_auto
         -- rows. See scripts/apply_tracker_with_markers.py docstring for the
         -- category definitions.
         SELECT lo.lead_email,
            CASE
                -- Cat D: no reply tracked. Booking was set via some channel
                -- outside Instantly (manual entry, call, in-person, etc.).
                WHEN fr.reply_timestamp IS NULL
                    THEN 'Booking via external channel — no reply tracked in Instantly'
                -- Cat B: reply predates our sent_messages coverage. Outbound
                -- + follow-ups (if any) were sent before Aug 7 2025 and aren't
                -- in our database.
                WHEN fr.reply_timestamp < timestamp '2025-08-07'
                    THEN 'Pre-backfill (replies before Aug 7 2025; outbound history not captured)'
                -- Cat A: campaign_auto reached them BEFORE they replied. They
                -- responded to the initial cold outreach; no follow-up was
                -- needed.
                WHEN EXISTS (
                    SELECT 1 FROM sent_messages s
                    WHERE s.lead_email = lo.lead_email
                      AND s.send_kind = 'campaign_auto'::text
                      AND s.sent_timestamp < fr.reply_timestamp
                )
                    THEN 'No follow-up needed — replied to initial campaign in this workspace'
                -- Cat C: no campaign_auto to this lead but they replied within
                -- coverage. Reply bodies frequently quote outbounds from other
                -- Dwyer-Enterprises sending workspaces (gotscal..., outreachecomm.com,
                -- etc.) — we only sync ONE workspace's outbound but inbound
                -- consolidates to the support inbox.
                ELSE 'Outreach sent via different Instantly workspace — outbound history not synced here'
            END AS marker_text
           FROM lead_outcomes lo
             LEFT JOIN leads l ON l.lead_email = lo.lead_email
             LEFT JOIN first_reply fr ON fr.lead_email = lo.lead_email
             LEFT JOIN ffup_counts fc ON fc.lead_email = lo.lead_email
          WHERE COALESCE(fc.cnt, 0::bigint) = 0
            AND lower(COALESCE(lo.status_raw, l.auto_status)) = 'booked'
        )
 SELECT lo.client AS "Client",
    lo.lead_email AS "Email Address",
    lo.campaign AS "Campaign",
    lo.leadlist_source AS "Leadlist Source",
    COALESCE(lo.status_raw, l.auto_status) AS "Status",
    lo.qualified AS "Qualified",
    fr.reply_timestamp AS "Initial Reply Date",
    fr.body AS "What was their initial reply",
    COALESCE(fc.cnt, 0::bigint) AS "Total Follow-ups",
    max(
        CASE
            WHEN ro.ffup_n = 1 THEN ro.sent_timestamp
            ELSE NULL::timestamp with time zone
        END) AS "Email ffup 1 Date",
    COALESCE(
        max(
            CASE
                WHEN ro.ffup_n = 1 THEN ro.body
                ELSE NULL::text
            END),
        bem.marker_text
    ) AS "Email FF 1 what we sent",
    max(
        CASE
            WHEN ro.ffup_n = 2 THEN ro.sent_timestamp
            ELSE NULL::timestamp with time zone
        END) AS "Email ffup 2 Date",
    max(
        CASE
            WHEN ro.ffup_n = 2 THEN ro.body
            ELSE NULL::text
        END) AS "Sent ff 2",
    max(
        CASE
            WHEN ro.ffup_n = 3 THEN ro.sent_timestamp
            ELSE NULL::timestamp with time zone
        END) AS "Email ffup 3 Date",
    max(
        CASE
            WHEN ro.ffup_n = 3 THEN ro.body
            ELSE NULL::text
        END) AS "Sent ff 3",
    max(
        CASE
            WHEN ro.ffup_n = 4 THEN ro.sent_timestamp
            ELSE NULL::timestamp with time zone
        END) AS "Email ffup 4 Date",
    max(
        CASE
            WHEN ro.ffup_n = 4 THEN ro.body
            ELSE NULL::text
        END) AS "Sent ff 4",
    max(
        CASE
            WHEN ro.ffup_n = 5 THEN ro.sent_timestamp
            ELSE NULL::timestamp with time zone
        END) AS "Email ffup 5 Date",
    max(
        CASE
            WHEN ro.ffup_n = 5 THEN ro.body
            ELSE NULL::text
        END) AS "Sent ff 5",
    max(
        CASE
            WHEN ro.ffup_n = 6 THEN ro.sent_timestamp
            ELSE NULL::timestamp with time zone
        END) AS "Email ffup 6 Date",
    max(
        CASE
            WHEN ro.ffup_n = 6 THEN ro.body
            ELSE NULL::text
        END) AS "Sent ff 6",
    max(
        CASE
            WHEN ro.ffup_n = 7 THEN ro.sent_timestamp
            ELSE NULL::timestamp with time zone
        END) AS "Email ffup 7 Date",
    max(
        CASE
            WHEN ro.ffup_n = 7 THEN ro.body
            ELSE NULL::text
        END) AS "Sent ff 7",
    max(
        CASE
            WHEN ro.ffup_n = 8 THEN ro.sent_timestamp
            ELSE NULL::timestamp with time zone
        END) AS "Email ffup 8 Date",
    max(
        CASE
            WHEN ro.ffup_n = 8 THEN ro.body
            ELSE NULL::text
        END) AS "Sent ff 8",
    lo.call_ffup AS "Call ffup",
    max(
        CASE
            WHEN ro.ffup_n = 9 THEN ro.sent_timestamp
            ELSE NULL::timestamp with time zone
        END) AS "Email ffup 9 Date",
    max(
        CASE
            WHEN ro.ffup_n = 9 THEN ro.body
            ELSE NULL::text
        END) AS "Sent ff 9",
    max(
        CASE
            WHEN ro.ffup_n = 10 THEN ro.sent_timestamp
            ELSE NULL::timestamp with time zone
        END) AS "Email ffup 10 Date",
    max(
        CASE
            WHEN ro.ffup_n = 10 THEN ro.body
            ELSE NULL::text
        END) AS "Sent ff 10",
    max(
        CASE
            WHEN ro.ffup_n = 11 THEN ro.sent_timestamp
            ELSE NULL::timestamp with time zone
        END) AS "Email ffup 11 Date",
    max(
        CASE
            WHEN ro.ffup_n = 11 THEN ro.body
            ELSE NULL::text
        END) AS "Sent ff 11",
    max(
        CASE
            WHEN ro.ffup_n = 12 THEN ro.sent_timestamp
            ELSE NULL::timestamp with time zone
        END) AS "Email ffup 12 Date",
    max(
        CASE
            WHEN ro.ffup_n = 12 THEN ro.body
            ELSE NULL::text
        END) AS "Sent ff 12",
    max(
        CASE
            WHEN ro.ffup_n = 13 THEN ro.sent_timestamp
            ELSE NULL::timestamp with time zone
        END) AS "Email ffup 13 Date",
    max(
        CASE
            WHEN ro.ffup_n = 13 THEN ro.body
            ELSE NULL::text
        END) AS "Sent ff 13",
    max(
        CASE
            WHEN ro.ffup_n = 14 THEN ro.sent_timestamp
            ELSE NULL::timestamp with time zone
        END) AS "Email ffup 14 Date",
    max(
        CASE
            WHEN ro.ffup_n = 14 THEN ro.body
            ELSE NULL::text
        END) AS "Sent ff 14",
    max(
        CASE
            WHEN ro.ffup_n = 15 THEN ro.sent_timestamp
            ELSE NULL::timestamp with time zone
        END) AS "Email ffup 15 Date",
    max(
        CASE
            WHEN ro.ffup_n = 15 THEN ro.body
            ELSE NULL::text
        END) AS "Sent ff 15",
    max(
        CASE
            WHEN ro.ffup_n = 16 THEN ro.sent_timestamp
            ELSE NULL::timestamp with time zone
        END) AS "Email ffup 16 Date",
    max(
        CASE
            WHEN ro.ffup_n = 16 THEN ro.body
            ELSE NULL::text
        END) AS "Sent ff 16",
    max(
        CASE
            WHEN ro.ffup_n = 17 THEN ro.sent_timestamp
            ELSE NULL::timestamp with time zone
        END) AS "Email ffup 17 Date",
    max(
        CASE
            WHEN ro.ffup_n = 17 THEN ro.body
            ELSE NULL::text
        END) AS "Sent ff 17",
    max(
        CASE
            WHEN ro.ffup_n = 18 THEN ro.sent_timestamp
            ELSE NULL::timestamp with time zone
        END) AS "Email ffup 18 Date",
    max(
        CASE
            WHEN ro.ffup_n = 18 THEN ro.body
            ELSE NULL::text
        END) AS "Sent ff 18",
    max(
        CASE
            WHEN ro.ffup_n = 19 THEN ro.sent_timestamp
            ELSE NULL::timestamp with time zone
        END) AS "Email ffup 19 Date",
    max(
        CASE
            WHEN ro.ffup_n = 19 THEN ro.body
            ELSE NULL::text
        END) AS "Sent ff 19",
    max(
        CASE
            WHEN ro.ffup_n = 20 THEN ro.sent_timestamp
            ELSE NULL::timestamp with time zone
        END) AS "Email ffup 20 Date",
    max(
        CASE
            WHEN ro.ffup_n = 20 THEN ro.body
            ELSE NULL::text
        END) AS "Sent ff 20",
    lo.note AS "NOTE (JOYCE)",
    lr.reply_timestamp AS "Last Reply At",
    "left"(lr.body, 500) AS "Last reply from Instantly",
    wn.ffup_n AS "Winning Ffup #",
    ls.confidence AS "Confidence",
    ls.winning_subject AS "Winning Subject",
    "left"(ls.winning_body, 500) AS "Winning Message",
    "left"(ls.booking_reply_body, 300) AS "What Lead Said (Booking)",
    ls.rationale AS "Why We Think It Won"
   FROM lead_outcomes lo
     LEFT JOIN leads l ON l.lead_email = lo.lead_email
     LEFT JOIN first_reply fr ON fr.lead_email = lo.lead_email
     LEFT JOIN last_reply lr ON lr.lead_email = lo.lead_email
     LEFT JOIN ffup_counts fc ON fc.lead_email = lo.lead_email
     LEFT JOIN ranked_outbounds ro ON ro.lead_email = lo.lead_email
     LEFT JOIN latest_selection ls ON ls.lead_email = lo.lead_email
     LEFT JOIN winning_ffup_n wn ON wn.lead_email = lo.lead_email
     LEFT JOIN booked_empty_marker bem ON bem.lead_email = lo.lead_email
  GROUP BY lo.client, lo.lead_email, lo.campaign, lo.leadlist_source, lo.status_raw, l.auto_status, lo.qualified, fr.reply_timestamp, fr.body, lo.call_ffup, lo.note, lr.reply_timestamp, lr.body, fc.cnt, wn.ffup_n, ls.confidence, ls.rationale, ls.winning_subject, ls.winning_body, ls.booking_reply_body, bem.marker_text;
"""


def main() -> None:
    conn = connect()
    conn.autocommit = True
    cur = conn.cursor()

    print("Replacing public.followup_tracker_mv...")
    cur.execute(NEW_VIEW)
    print("[OK] view replaced")

    # Smoke-test: verify the four categories show their markers
    print("\nSmoke-test: marker text distribution for booked + 0 follow-ups")
    cur.execute("""
      select "Email FF 1 what we sent" as ff1, count(*)
      from followup_tracker_mv
      where lower("Status") = 'booked' and "Total Follow-ups" = 0
      group by ff1
      order by count(*) desc
    """)
    for ff1, c in cur.fetchall():
        text = (ff1 or "(NULL)")[:75]
        print(f"  {c:>4} : {text}")

    # Verify rows with actual follow-ups are still untouched (their ff 1 text is
    # the real send body, not a marker)
    print("\nSanity: 5 leads WITH follow-ups (should show real send body, not marker)")
    cur.execute("""
      select "Email Address", left("Email FF 1 what we sent", 80) as ff1_snippet
      from followup_tracker_mv
      where "Total Follow-ups" > 0
      limit 5
    """)
    for email, ff1 in cur.fetchall():
        print(f"  {email}: {ff1!r}")

    cur.close()
    conn.close()
    print("\nDone.")


if __name__ == "__main__":
    main()
