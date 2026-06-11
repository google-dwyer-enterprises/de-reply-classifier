"""Finalize the reseller cleanup on the BetterContact pool and re-cut the
combined export in the standard format (same as the 2026-06-09 cut).

  1. Quarantine the funnel-confirmed reseller leads: rejected=true,
     agency_filter_result='reseller',
     agency_filter_reason='reseller_site: <evidence>'.
     (Identified by brand_verify_result='reseller' on accepted BC rows.)
  2. Re-export provider='bettercontact' through prospeo_sync.write_csv /
     write_xlsx — identical columns, sheet names, and styling to
     bettercontact_{accepted,all}_combined_20260609_*.

Usage: python scripts/reseller_finalize_export.py
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from dotenv import load_dotenv
load_dotenv()

from db import connect
from prospeo_sync import write_csv, write_xlsx


def main() -> None:
    conn = connect()

    # 1. Quarantine confirmed resellers still sitting in the accepted pool.
    with conn.cursor() as cur:
        cur.execute("""
            update prospeo_new_leads
               set rejected = true,
                   agency_filter_result = 'reseller',
                   agency_filter_reason =
                     left('reseller_site: ' || coalesce(brand_verify_evidence, ''), 500)
             where provider = 'bettercontact'
               and not rejected
               and brand_verify_result = 'reseller'
            returning email, company_domain
        """)
        moved = cur.fetchall()
    conn.commit()
    print(f"quarantined {len(moved)} reseller lead(s):")
    for email, dom in moved:
        print(f"  {email}  ({dom})")

    # 2. Re-export in the standard combined format.
    accepted: list[dict] = []
    rejected: list[dict] = []
    with conn.cursor() as cur:
        cur.execute("""
            select email, mobile, first_name, last_name, title, company_name,
                   company_website, source_domain, agency_filter_result,
                   mobile_status, agency_filter_method, agency_filter_reason,
                   scrape_mode, source_industry, rejected
            from prospeo_new_leads
            where provider = 'bettercontact'
            order by rejected, scraped_at desc
        """)
        for r in cur.fetchall():
            lead = {
                "email": r[0], "mobile": r[1],
                "first_name": r[2], "last_name": r[3], "title": r[4],
                "company_name": r[5], "company_website": r[6],
                "source_domain": r[7], "agency_filter_result": r[8],
                "mobile_status": r[9], "agency_filter_method": r[10],
                "agency_filter_reason": r[11],
                "scrape_mode": r[12], "source_industry": r[13],
            }
            (rejected if r[14] else accepted).append(lead)
    conn.close()

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    csv_path = f"exports/bettercontact_accepted_combined_{stamp}.csv"
    xlsx_path = f"exports/bettercontact_all_combined_{stamp}.xlsx"
    write_csv(accepted, csv_path)
    write_xlsx(accepted, rejected, xlsx_path)
    print(f"\naccepted: {len(accepted)}  rejected: {len(rejected)}")
    print(f"CSV  (accepted only):   {csv_path}")
    print(f"XLSX (both sheets):     {xlsx_path}")


if __name__ == "__main__":
    main()
