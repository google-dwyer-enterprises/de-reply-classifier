"""Cost-resequencing probes P1+P2 against the live BetterContact API.

Pure API calls: no DB writes, no category_scrape_state changes, no inserts.
Three small searches (limit=10/10/5 ≈ ~$4-6 total):

  A  baseline — exact production filters (Cosmetics, US/CA, 5-50, titles)
  B  A + lead_job_title.exclude (known-bad title classes)  -> P1
  C  A + company.exclude (up to 500 known domains)         -> P2

Reports per search: credits_consumed, leads_found, emails delivered,
title distribution, exclusion violations. Settles:
  - does exclude filtering work server-side?
  - does billing follow delivered emails (and is there a per-slot fee)?
  - company.exclude max list size (try 500, fall back 200/50)
"""
import json
import sys
import os
sys.path.insert(0, '.')
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
from dotenv import load_dotenv
load_dotenv(".env")

from db import connect
import bettercontact_sync as bc

API_KEY = os.environ["BETTERCONTACT_API_KEY"].strip()
INDUSTRY = "Cosmetics"
COUNTRIES = ["United States", "Canada"]
# Fresh offset: beyond the production cursor so results are mostly unseen.
conn = connect()
cur = conn.cursor()
cur.execute("select last_page_consumed from category_scrape_state where industry = %s", (INDUSTRY,))
row = cur.fetchone()
page = (row[0] if row else 0) + 4          # well past the cursor
OFFSET = page * 50

TITLE_EXCLUDE = [
    "Vice President of Sales", "VP of Sales", "SVP of Sales",
    "VP of Finance", "VP of Business Development", "VP of Operations",
    "Vice President of Finance", "Vice President of Operations",
    "Sales Manager", "Account Manager", "Human Resources",
]

# company.exclude candidates: capped companies + company-level rejects.
cur.execute(r"""
  select distinct lower(regexp_replace(company_domain,'^www\.','')) as d
  from prospeo_new_leads
  where provider='bettercontact' and company_domain is not null
    and (not rejected
         or agency_filter_reason ~ '^(reseller|mlm_|banned_|out_of_scope|too_large|corporate_|prohibited|QA: prohibited|QA: service|LLM: (reseller|service|agency|marketplace))')
  limit 500
""")
EXCLUDE_DOMAINS = [r[0] for r in cur.fetchall()]
conn.close()
print(f"probe setup: industry={INDUSTRY} offset={OFFSET} "
      f"exclude_pool={len(EXCLUDE_DOMAINS)} domains")


def run_search(label, extra_filters, limit):
    filters = bc._industry_filters(INDUSTRY, COUNTRIES)
    filters.update(extra_filters)
    rid = bc._submit_search(filters, limit, OFFSET, API_KEY)
    res = bc._poll_for_result(rid, API_KEY)
    leads = res.get("leads") or []
    summary = res.get("summary") or {}
    titles = [(l.get("contact_job_title") or "?") for l in leads]
    domains = [(l.get("company_domain") or "?").lower().replace("www.", "")
               for l in leads]
    emails = sum(1 for l in leads
                 if l.get("contact_email_address_status") == "deliverable")
    print(f"\n--- {label} (limit={limit}) ---")
    print(f"  credits_consumed: {res.get('credits_consumed')}  "
          f"leads_found(total est): {summary.get('leads_found')}  "
          f"returned: {len(leads)}  deliverable_emails: {emails}")
    for t in titles:
        print(f"    title: {t}")
    return {"credits": res.get("credits_consumed"), "returned": len(leads),
            "emails": emails, "titles": titles, "domains": domains}


a = run_search("A baseline (production filters)", {}, 10)

b = run_search("B + lead_job_title.exclude",
               {"lead_job_title": {"include": bc.BC_TITLE_KEYWORDS,
                                   "exclude": TITLE_EXCLUDE}}, 10)

c = None
for size in (500, 200, 50):
    try:
        c = run_search(f"C + company.exclude[{size}]",
                       {"company": {"exclude": EXCLUDE_DOMAINS[:size]}}, 5)
        print(f"  -> company.exclude accepted at size {size}")
        break
    except Exception as e:
        print(f"  company.exclude size {size} FAILED: {type(e).__name__}: "
              f"{str(e)[:140]}")

print("\n=== VERDICTS ===")
bad = [t for t in b["titles"] if any(x.lower() in t.lower()
       for x in ("sales", "finance", "human resources", "account manager"))]
print(f"P1 title-exclude: baseline had "
      f"{sum(1 for t in a['titles'] if 'sales' in t.lower() or 'finance' in t.lower())} "
      f"sales/finance titles; excluded-search has {len(bad)}: {bad}")
if c:
    viol = [d for d in c["domains"] if d in set(EXCLUDE_DOMAINS)]
    print(f"P2 company-exclude violations (returned despite exclusion): "
          f"{len(viol)} {viol[:5]}")
for label, r in (("A", a), ("B", b)) + ((("C", c),) if c else ()):
    if r and r.get("credits") is not None and r["returned"]:
        print(f"billing {label}: {r['credits']} credits for {r['returned']} "
              f"returned / {r['emails']} emails -> "
              f"{r['credits']/max(r['emails'],1):.2f} cr/email, "
              f"{r['credits']/r['returned']:.2f} cr/returned-lead")
