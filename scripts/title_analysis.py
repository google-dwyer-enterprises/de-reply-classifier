"""READ-ONLY analysis of title-family conversion rates.

Three passes:
  A1. Reproduce the architecture's original numbers (v3 classifications only,
      reply-level, requires lead_contacts.title).
  A2. Widen to all prompt_versions, still reply-level.
  A3. Lead-level (distinct lead_email), combines classifier label + lead_status
      from replies (the Instantly tag) for a much bigger signal set.

For each pass, compute Wilson 95% confidence intervals on the positive rate.
"""

import math
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from db import connect


def wilson_ci(positives, n, z=1.96):
    """Two-sided Wilson score 95% CI for a binomial proportion.
    Returns (lower, upper) as percentages."""
    if n == 0:
        return (0.0, 0.0)
    p = positives / n
    denom = 1 + z * z / n
    center = (p + z * z / (2 * n)) / denom
    spread = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return ((center - spread) * 100, (center + spread) * 100)


FAMILY_CASE = """
  case
    when coalesce(title,'') = '' then '(no title)'
    when title ~ '(coo|chief operating)' then 'COO/Operations'
    when title ~ '(cmo|chief marketing|head of marketing|head of e-?commerce|marketing director|vp.*marketing|director.*marketing|director.*e-?commerce|vp.*e-?commerce|head of growth|head of digital)'
      then 'CMO/Marketing/HeadEcom'
    when title ~ '(ceo|chief executive|founder|co-?founder|owner|co-?owner|president|chairman|managing director)'
      then 'Founder/CEO/Owner/President'
    when title ~ '(chief|vp|vice president|director|head of)' then 'OtherChief/VP/Director'
    else 'Manager/Specialist/Other'
  end
"""


def section(t):
    print("\n" + "=" * 78)
    print(t)
    print("=" * 78)


def print_table(rows, header):
    print(" | ".join(header))
    print("-" * 78)
    for row in rows:
        print(" | ".join(str(v) for v in row))


def analyze(cur, label_source_sql, title_table_alias="lc", where_extra="", title="Pass"):
    """Run the title-family aggregation against a CTE that yields
    (label_source, lead_email_or_reply_key, title). Builds families,
    computes positive (booked/interested/interested_past) totals,
    plus Wilson 95% CIs."""
    sql = f"""
      with src as (
        {label_source_sql}
      ),
      family as (
        select {FAMILY_CASE} as family, label, lead_email
        from src
        where 1=1 {where_extra}
      )
      select family,
        count(*) as total,
        sum(case when label in ('booked','interested','interested_past') then 1 else 0 end) as positive,
        sum(case when label = 'booked' then 1 else 0 end) as booked,
        sum(case when label = 'interested' then 1 else 0 end) as interested,
        sum(case when label = 'wrong_person' then 1 else 0 end) as wrong_person,
        sum(case when label = 'no_longer_there' then 1 else 0 end) as no_longer_there,
        sum(case when label = 'oof' then 1 else 0 end) as oof
      from family
      group by family
      order by positive desc;
    """
    cur.execute(sql)
    rows = cur.fetchall()
    print(f"{'family':<32}{'total':>7}{'pos':>5}{'pos%':>7}{'CI95%':>16}"
          f"{'booked':>8}{'inter':>7}{'wrong':>7}{'NLT':>6}{'oof':>6}")
    print("-" * 110)
    for family, total, pos, booked, interested, wrong, nlt, oof in rows:
        lo, hi = wilson_ci(pos, total)
        ci = f"[{lo:5.1f}, {hi:5.1f}]"
        pct = (100.0 * pos / total) if total else 0
        print(f"{family:<32}{total:>7}{pos:>5}{pct:>6.1f}%{ci:>16}"
              f"{booked:>8}{interested:>7}{wrong:>7}{nlt:>6}{oof:>6}")
    print()


def main():
    conn = connect()
    conn.set_session(readonly=True)
    with conn.cursor() as cur:

        # ---------- PASS A1: reproduce ARCHITECTURE.html §5 methodology ----------
        section("PASS A1 — Reproduce architecture's methodology "
                "(v3 only, reply-level, requires title)")
        a1_sql = """
          select
            lower(trim(lc.title)) as title,
            l.label,
            l.lead_email
          from (
            select distinct on (reply_id) reply_id, lead_email, label
            from classifications
            where prompt_version = 'v3'
            order by reply_id, classified_at desc
          ) l
          join lead_contacts lc on lc.lead_email = l.lead_email
          where coalesce(lc.title,'') <> ''
        """
        analyze(cur, a1_sql, title="A1: v3, reply-level, titled")

        # ---------- PASS A2: same shape but all prompt versions ----------
        section("PASS A2 — All prompt versions, reply-level, requires title")
        a2_sql = """
          select
            lower(trim(lc.title)) as title,
            l.label,
            l.lead_email
          from (
            select distinct on (reply_id) reply_id, lead_email, label
            from classifications
            order by reply_id, classified_at desc
          ) l
          join lead_contacts lc on lc.lead_email = l.lead_email
          where coalesce(lc.title,'') <> ''
        """
        analyze(cur, a2_sql, title="A2: all versions, reply-level, titled")

        # ---------- PASS A3: lead-level, combine classifier + lead_status ----------
        section("PASS A3 — Lead-level (distinct lead_email), combines "
                "classifier label + Instantly lead_status as positive signal")
        # Derive one 'effective label' per lead:
        #   booked if any reply was classified booked OR lead_status contains 'booked'/'meeting'
        #   interested if any reply was classified interested OR lead_status contains 'interested'
        #   else fall back to the dominant classifier label
        a3_sql = """
          with latest as (
            select distinct on (reply_id) reply_id, lead_email, label
            from classifications
            order by reply_id, classified_at desc
          ),
          per_lead as (
            select
              r.lead_email,
              bool_or(l.label = 'booked'
                      or r.lead_status ilike '%%booked%%'
                      or r.lead_status ilike '%%meeting%%') as is_booked,
              bool_or(l.label = 'interested'
                      or r.lead_status ilike '%%interested%%') as is_interested,
              bool_or(l.label = 'interested_past') as is_interested_past,
              bool_or(l.label = 'wrong_person') as is_wp,
              bool_or(l.label = 'no_longer_there') as is_nlt,
              bool_or(l.label = 'oof') as is_oof,
              bool_or(l.label = 'unsubscribe') as is_unsub,
              bool_or(l.label = 'not_interested') as is_ni,
              bool_or(l.label = 'not_now') as is_nn
            from replies r
            left join latest l on l.reply_id = r.id
            group by r.lead_email
          )
          select
            lower(trim(lc.title)) as title,
            case
              when pl.is_booked then 'booked'
              when pl.is_interested then 'interested'
              when pl.is_interested_past then 'interested_past'
              when pl.is_ni then 'not_interested'
              when pl.is_nn then 'not_now'
              when pl.is_wp then 'wrong_person'
              when pl.is_nlt then 'no_longer_there'
              when pl.is_oof then 'oof'
              when pl.is_unsub then 'unsubscribe'
              else 'other'
            end as label,
            pl.lead_email
          from per_lead pl
          left join lead_contacts lc on lc.lead_email = pl.lead_email
        """
        analyze(cur, a3_sql, title="A3: lead-level, classifier+lead_status")

        # ---------- PASS A4: same as A3 but include leads with NO classification but tagged in Instantly ----------
        section("PASS A4 — Same as A3, but also count leads whose ONLY positive "
                "signal is the Instantly lead_status (no classification yet)")
        # The same query A3 actually already includes them (left join from replies).
        # Show the size of that subset for transparency.
        cur.execute("""
          select count(distinct r.lead_email) as no_class_but_booked
          from replies r
          left join classifications c on c.reply_id = r.id
          where c.reply_id is null
            and (r.lead_status ilike '%%booked%%' or r.lead_status ilike '%%meeting%%');
        """)
        n = cur.fetchone()[0]
        print(f"Leads with NO classification row but a Booked-ish Instantly tag: {n}")

        # ---------- Title coverage summary ----------
        section("Coverage check — what fraction of the positive-family leads have a title?")
        cur.execute("""
          with latest as (
            select distinct on (reply_id) reply_id, lead_email, label
            from classifications
            order by reply_id, classified_at desc
          ),
          pos_leads as (
            select distinct r.lead_email
            from replies r
            left join latest l on l.reply_id = r.id
            where (l.label in ('booked','interested','interested_past'))
               or r.lead_status ilike '%%booked%%'
               or r.lead_status ilike '%%meeting%%'
               or r.lead_status ilike '%%interested%%'
          )
          select
            count(*) as total_positive_leads,
            count(*) filter (where lc.lead_email is not null) as joined_to_lc,
            count(*) filter (where coalesce(lc.title,'') <> '') as with_title,
            count(*) filter (where coalesce(lc.title,'') = '' and lc.lead_email is not null) as joined_no_title
          from pos_leads pl
          left join lead_contacts lc on lc.lead_email = pl.lead_email;
        """)
        cols = [d[0] for d in cur.description]
        row = cur.fetchone()
        for c, v in zip(cols, row):
            print(f"  {c}: {v}")

    conn.close()


if __name__ == "__main__":
    main()
