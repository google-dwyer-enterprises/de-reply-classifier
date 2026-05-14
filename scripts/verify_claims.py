"""READ-ONLY verification of claims about the Prospeo implementation.
Runs a series of SELECT queries against Supabase and prints findings.
Never writes — uses set_session(readonly=True)."""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from db import connect


def section(title):
    print("\n" + "=" * 70)
    print(title)
    print("=" * 70)


def q(cur, sql, params=None, max_rows=20):
    cur.execute(sql, params or ())
    rows = cur.fetchall()
    cols = [d[0] for d in cur.description] if cur.description else []
    if cols:
        print(" | ".join(cols))
        print("-" * 70)
    for r in rows[:max_rows]:
        print(" | ".join(str(v) if v is not None else "" for v in r))
    if len(rows) > max_rows:
        print(f"... ({len(rows) - max_rows} more rows)")
    return rows


def main():
    conn = connect()
    conn.set_session(readonly=True)
    with conn.cursor() as cur:

        section("1. CLAIM: There are 'over 500 interested records' / 150 booked calls / "
                "300+ booked replies in the DB (user's concern)")
        # Total replies and breakdown by latest classification label
        q(cur, """
          with latest as (
            select distinct on (reply_id) reply_id, lead_email, label
            from classifications
            order by reply_id, classified_at desc
          )
          select label, count(*) as n
          from latest
          group by label
          order by n desc;
        """)

        section("1b. Distinct lead_emails per label (so 'booked calls' counts unique people, "
                "not repeat replies)")
        q(cur, """
          with latest as (
            select distinct on (reply_id) reply_id, lead_email, label
            from classifications
            order by reply_id, classified_at desc
          )
          select label, count(distinct lead_email) as distinct_leads
          from latest
          group by label
          order by distinct_leads desc;
        """)

        section("2. CLAIM: The 22/60 + 1/15 title analysis only counted replies whose "
                "senders had a populated lead_contacts.title")
        # Total replies with a classification
        q(cur, """
          select
            (select count(*) from (
                select distinct on (reply_id) reply_id, lead_email
                from classifications
                where prompt_version='v3'
                order by reply_id, classified_at desc
            ) x) as v3_classified_replies,
            (select count(*) from (
                select distinct on (reply_id) reply_id, lead_email
                from classifications
                order by reply_id, classified_at desc
            ) x) as all_classified_replies,
            (select count(*) from lead_contacts where coalesce(title,'') <> '') as lead_contacts_with_title,
            (select count(*) from lead_contacts) as lead_contacts_total;
        """)

        section("2b. How many classified replies join to a lead_contact WITH a title? "
                "vs without — this is the gap")
        q(cur, """
          with latest as (
            select distinct on (reply_id) reply_id, lead_email, label, prompt_version
            from classifications
            order by reply_id, classified_at desc
          )
          select
            count(*) as total_classified,
            count(*) filter (where lc.lead_email is not null and coalesce(lc.title,'') <> '') as with_title,
            count(*) filter (where lc.lead_email is not null and coalesce(lc.title,'') = '') as joined_but_no_title,
            count(*) filter (where lc.lead_email is null) as no_lead_contact_join
          from latest l
          left join lead_contacts lc on lc.lead_email = l.lead_email;
        """)

        section("2c. The actual title-family analysis used in ARCHITECTURE.html §5 — "
                "re-running it now")
        q(cur, """
          with latest as (
            select distinct on (reply_id) reply_id, lead_email, label
            from classifications where prompt_version='v3'
            order by reply_id, classified_at desc
          ),
          titled as (
            select l.label, lower(trim(lc.title)) as title
            from latest l
            join lead_contacts lc on lc.lead_email = l.lead_email
            where coalesce(lc.title,'') <> ''
          ),
          family as (
            select
              case
                when title ~ '(^|\s)(ceo|chief executive|founder|owner|president|chairman|managing director)' then 'Founder/CEO/Owner/President'
                when title ~ '(cmo|chief marketing|head of (marketing|e-?commerce)|marketing director|vp.*marketing|director.*marketing|director.*e-?commerce|vp.*e-?commerce)' then 'CMO/Marketing/HeadEcom'
                when title ~ '(coo|cfo|cto|chief|vp|vice president|director|head of)' then 'OtherChief/VP/Director'
                else 'Manager/Specialist/Other'
              end as family,
              label
            from titled
          )
          select family,
            sum(case when label in ('booked','interested','interested_past') then 1 else 0 end) as positive,
            sum(case when label in ('not_interested','not_now') then 1 else 0 end) as soft_neg,
            sum(case when label = 'wrong_person' then 1 else 0 end) as wrong_person,
            sum(case when label = 'no_longer_there' then 1 else 0 end) as no_longer_there,
            sum(case when label = 'oof' then 1 else 0 end) as oof,
            count(*) as total,
            round(100.0 * sum(case when label in ('booked','interested','interested_past') then 1 else 0 end) / count(*), 1) as pos_pct
          from family
          group by family
          order by positive desc;
        """)

        section("3. CLAIM (user): there's a richer signal in replies than the title-join captures. "
                "Check replies.lead_status (the Instantly tag) — does that have more booked data?")
        q(cur, """
          select lead_status, count(*) as n
          from replies
          where lead_status is not null
          group by lead_status
          order by n desc
          limit 25;
        """)

        section("3b. Booked-ish lead_status totals (the 150-booked-calls number user cited)")
        q(cur, """
          select count(distinct lead_email) as distinct_leads_with_booked_status
          from replies
          where lead_status is not null
            and (lead_status ilike '%%booked%%' or lead_status ilike '%%meeting%%');
        """)

        section("3c. Of those booked-status leads, how many ALSO have a title in lead_contacts? "
                "(this is the lever — using lead_status broadens the sample)")
        q(cur, """
          with booked_leads as (
            select distinct lead_email
            from replies
            where lead_status is not null
              and (lead_status ilike '%%booked%%' or lead_status ilike '%%meeting%%')
          )
          select
            count(*) as total_booked_leads,
            count(*) filter (where lc.lead_email is not null) as joined_to_lead_contacts,
            count(*) filter (where lc.lead_email is not null and coalesce(lc.title,'') <> '') as with_title
          from booked_leads b
          left join lead_contacts lc on lc.lead_email = b.lead_email;
        """)

        section("4. CLAIM: domain_inclusion_list is ~52k domains (architecture claim)")
        q(cur, """
          select
            count(*) as total,
            count(*) filter (where last_scraped_at is not null) as scraped,
            count(*) filter (where last_scraped_at is null) as never_scraped
          from domain_inclusion_list;
        """)

        section("5. CLAIM: skip_decision_maker rule blocks CMO-fetching at brands "
                "where CEO already exists. Verify the SQL it uses, then count impact")
        # The exact SQL from prospeo_sync.py:243-254
        q(cur, """
          select count(distinct lower(split_part(lead_email,'@',2))) as domains_with_decision_maker
          from lead_contacts
          where lead_email is not null
            and lower(coalesce(title, '')) ~ '(ceo|founder|owner|president|chief|cmo|head of (marketing|e-?commerce))';
        """)

        section("5b. How many of those domains have ONLY a CEO/Founder and no CMO/Marketing — "
                "i.e. where adding CMO would be valuable")
        q(cur, """
          with by_domain as (
            select lower(split_part(lead_email,'@',2)) as domain,
              max(case when lower(coalesce(title,'')) ~ '(ceo|founder|owner|president|chief executive|managing director|chairman)' then 1 else 0 end) as has_ceo,
              max(case when lower(coalesce(title,'')) ~ '(cmo|chief marketing|head of (marketing|e-?commerce)|marketing director|vp.*marketing|director.*marketing|director.*e-?commerce)' then 1 else 0 end) as has_marketing
            from lead_contacts
            where lead_email is not null
            group by 1
          )
          select
            count(*) filter (where has_ceo=1 and has_marketing=1) as both,
            count(*) filter (where has_ceo=1 and has_marketing=0) as ceo_only,
            count(*) filter (where has_ceo=0 and has_marketing=1) as marketing_only,
            count(*) filter (where has_ceo=0 and has_marketing=0) as neither
          from by_domain;
        """)

        section("6. CLAIM: yield is ~1-3% verified leads per domain. Verify against "
                "actual prospeo_new_leads data")
        q(cur, """
          select
            (select count(*) from prospeo_new_leads) as leads_total,
            (select count(*) from prospeo_new_leads where rejected = false) as accepted,
            (select count(*) from prospeo_new_leads where rejected = true) as rejected,
            (select count(distinct source_domain) from prospeo_new_leads where source_domain is not null) as distinct_source_domains;
        """)

        section("6b. Accepted-by-source-domain — if multiple accepted leads per domain, "
                "the bot is already returning more than one decision-maker per brand")
        q(cur, """
          select n_leads_per_domain, count(*) as n_domains
          from (
            select source_domain, count(*) as n_leads_per_domain
            from prospeo_new_leads
            where rejected = false and source_domain is not null
            group by source_domain
          ) x
          group by 1 order by 1;
        """)

        section("7. CLAIM: implementation uses websites filter (not category). Confirm in code.")
        print("Code reference: prospeo_sync.py lines 305-312 (_search_people)")
        print("  filters = {")
        print("      'company': {'websites': {'include': domains}},")
        print("      'person_job_title': {'include': PROSPEO_TITLES, 'match': 'smart'}")
        print("  }")
        print("=> Confirmed: domain-only, no category filter.")

    conn.close()


if __name__ == "__main__":
    main()
