"""Phase 0 readiness check for the descriptive follow-up-effectiveness analysis.

Read-only. Confirms the plan's verified assumptions against live data and prints
the numbers that feed the support thresholds + the HTML caveat banner:

  - the power funnel (manual sends -> any in-window reply -> positive -> booked)
  - quoted-thread boundary detection rate + new-text length distribution
  - reverse-causality exposure (positives before any manual follow-up)
  - client/campaign positive-rate spread + largest-client volume share
  - the gold-winner set (followup_winning_selection) joinability
  - coverage (booked leads with zero manual follow-ups; blank tracker rows)

The windowed last-touch attribution SQL here is the SAME logic the feature
extractor (followup_features.py) uses, so Phase 0 and Phase 1 stay consistent.

Usage:  python scripts/check_followup_effectiveness_readiness.py
"""
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from db import connect
from followup_features import extract_new_text  # canonical quoted-thread stripper


def new_text(body: str, subject: str | None = None) -> tuple[str, bool]:
    return extract_new_text(subject, body)


# Windowed last-touch attribution: each manual follow-up -> the first reply that
# landed before the next manual send to the same lead, labelled by latest class.
ATTRIB_SQL = """
with manual as (
  select id, lead_email, sent_timestamp
  from sent_messages
  where send_kind = 'unibox_manual'
),
withnext as (
  select m.*,
         lead(sent_timestamp) over (partition by lead_email order by sent_timestamp, id) as next_out
  from manual m
),
credit as (
  select w.id, w.lead_email, w.sent_timestamp, w.next_out,
         (select r.id from replies r
           where r.lead_email = w.lead_email
             and r.reply_timestamp > w.sent_timestamp
             and (w.next_out is null or r.reply_timestamp < w.next_out)
           order by r.reply_timestamp asc limit 1) as credit_reply_id
  from withnext w
),
labeled as (
  select c.id, c.lead_email, c.sent_timestamp, c.credit_reply_id,
         (select cl.label from classifications cl
           where cl.reply_id = c.credit_reply_id
           order by cl.classified_at desc limit 1) as reply_label
  from credit c
)
select * from labeled
"""


def main() -> None:
    conn = connect()
    cur = conn.cursor()

    print("=" * 70)
    print("PHASE 0 — Follow-up effectiveness readiness (read-only)")
    print("=" * 70)

    # ---- Power funnel ----
    cur.execute(f"""
        with l as ({ATTRIB_SQL})
        select count(*) total,
               count(credit_reply_id) had_reply,
               count(*) filter (where reply_label in ('booked','interested')) positive,
               count(*) filter (where reply_label = 'booked') booked
        from l
    """)
    total, had, pos, booked = cur.fetchone()
    print("\n## Power funnel (manual follow-ups)")
    print(f"  manual sends ............ {total}")
    print(f"  any in-window reply ..... {had}  ({100*had/total:.1f}%)")
    print(f"  positive (booked|inter).. {pos}  ({100*pos/total:.1f}%)")
    print(f"  booked .................. {booked}  ({100*booked/total:.1f}%)")

    # ---- Quoted-thread boundary detection ----
    cur.execute("select id, body from sent_messages where send_kind='unibox_manual'")
    rows = cur.fetchall()
    has_wrote = sum(1 for _, b in rows if b and "wrote:" in b.lower())
    detected = 0
    full_len = []
    nt_len = []
    no_boundary_long = 0
    for _id, b in rows:
        nt, found = new_text(b or "")
        if found:
            detected += 1
        full_len.append(len(b or ""))
        nt_len.append(len(nt))
        if not found and len(b or "") > 800:
            no_boundary_long += 1
    nrows = len(rows)
    avg = lambda xs: sum(xs) / len(xs) if xs else 0
    print("\n## Quoted-thread contamination")
    print(f"  bodies containing 'wrote:' .......... {has_wrote}/{nrows}  ({100*has_wrote/nrows:.1f}%)")
    print(f"  boundary DETECTED (analyzable) ...... {detected}/{nrows}  ({100*detected/nrows:.1f}%)")
    print(f"  mean full body len .................. {avg(full_len):.0f} chars")
    print(f"  mean extracted new-text len ......... {avg(nt_len):.0f} chars")
    print(f"  no boundary AND >800 chars (review).. {no_boundary_long}")

    # ---- Reverse-causality exposure ----
    cur.execute(f"""
        with l as ({ATTRIB_SQL}),
        pos as (select * from l where reply_label in ('booked','interested')),
        firstman as (select lead_email, min(sent_timestamp) ts
                     from sent_messages where send_kind='unibox_manual' group by lead_email)
        select
          (select count(*) from pos p join firstman f on f.lead_email=p.lead_email
             where exists (select 1 from replies r
                join classifications c on c.reply_id=r.id
                where r.lead_email=p.lead_email and r.reply_timestamp < p.sent_timestamp
                  and c.label in ('booked','interested')
                  and c.classified_at = (select max(c2.classified_at) from classifications c2 where c2.reply_id=r.id)
             )) as credited_pos_with_prior_positive
    """)
    rev = cur.fetchone()[0]
    print("\n## Reverse-causality exposure")
    print(f"  credited positives where lead was ALREADY positive before the send: {rev}")
    print(f"  (these get prior_positive_exists=true and drop from the primary rate)")

    # ---- Client spread + concentration ----
    cur.execute(f"""
        with l as ({ATTRIB_SQL})
        select coalesce(s.client,'(none)') client,
               count(*) sends,
               count(*) filter (where l.reply_label in ('booked','interested')) pos
        from l join sent_messages s on s.id=l.id
        group by 1 order by sends desc
    """)
    cli = cur.fetchall()
    print("\n## Client mix (positive-rate spread + volume share)")
    for c, s, p in cli[:8]:
        print(f"  {c[:28]:28s} sends={s:5d}  pos%={100*p/s:5.1f}")
    top = cli[0]
    print(f"  largest client share of all sends: {100*top[1]/total:.0f}% ({top[0]})")
    rates = [100*p/s for _, s, p in cli if s >= 30]
    if rates:
        print(f"  positive-rate spread (clients >=30 sends): {min(rates):.1f}% .. {max(rates):.1f}%  ({max(rates)/max(min(rates),0.1):.1f}x)")

    # ---- Gold winners joinable ----
    cur.execute("select count(*) from followup_winning_selection")
    fws = cur.fetchone()[0]
    cur.execute("""select count(*) from followup_winning_selection w
                   join sent_messages s on s.id=w.winning_sent_message_id
                   where s.send_kind='unibox_manual'""")
    fws_join = cur.fetchone()[0]
    print("\n## Gold winners (validation overlay)")
    print(f"  followup_winning_selection rows ........ {fws}")
    print(f"  joinable to a unibox_manual send ....... {fws_join}")

    # ---- Coverage ----
    cur.execute("""
        select count(*) from (
          select distinct l.lead_email
          from classifications c
          join replies r on r.id=c.reply_id
          join leads l on l.lead_email=r.lead_email
          where c.label='booked'
            and c.classified_at=(select max(c2.classified_at) from classifications c2 where c2.reply_id=c.reply_id)
        ) bl
        where not exists (select 1 from sent_messages s
                          where s.lead_email=bl.lead_email and s.send_kind='unibox_manual')
    """)
    booked_no_ffup = cur.fetchone()[0]
    try:
        cur.execute("""select count(*) from followup_tracker_mv
                       where "Email ffup 1 Date" is null""")
        blank_tracker = cur.fetchone()[0]
    except Exception:
        conn.rollback()
        blank_tracker = "n/a (column name differs)"
    print("\n## Coverage (live-derived; cite with snapshot date)")
    print(f"  booked-labelled leads with ZERO manual follow-ups: {booked_no_ffup}")
    print(f"  tracker rows with blank first follow-up ..........: {blank_tracker}")

    print("\n" + "=" * 70)
    print("Readiness summary: use these numbers for thresholds + the HTML caveat banner.")
    conn.close()


if __name__ == "__main__":
    main()
