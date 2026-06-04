"""Export today's session's accepted+rejected prospeo_new_leads (CSV + XLSX).

"Today's session" = rows scraped after the pre-batch1 snapshot timestamp
(reads it from the snapshot JSON). Reuses prospeo_sync.write_csv / write_xlsx.

CLI:
  python scripts/export_today.py --since-snapshot snapshots/snapshot_YYYYMMDD_HHMM_pre_batch1.json
  python scripts/export_today.py --since "2026-05-31T23:07:00+00:00"
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from db import connect
from prospeo_sync import write_csv, write_xlsx


def main() -> None:
    p = argparse.ArgumentParser()
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--since", help="ISO timestamp; only rows scraped at or after this are exported.")
    g.add_argument("--since-snapshot", metavar="PATH",
                   help="Read the `ts` field from a snapshot JSON and use it as --since.")
    p.add_argument("--out-dir", default="exports")
    p.add_argument("--mode", choices=["domain", "category"], default="category",
                   help="Filter by scrape_mode (default: category)")
    p.add_argument("--provider", choices=["prospeo", "bettercontact"], default=None,
                   help="Filter by provider. Omit to include all providers.")
    args = p.parse_args()

    if args.since_snapshot:
        snap = json.loads(Path(args.since_snapshot).read_text(encoding="utf-8"))
        since_iso = snap["ts"]
        print(f"since (from snapshot): {since_iso}")
    else:
        since_iso = args.since
        print(f"since: {since_iso}")

    accepted: list[dict] = []
    rejected: list[dict] = []

    conn = connect()
    try:
        with conn.cursor() as cur:
            sql = """
              select email, mobile, first_name, last_name, title, company_name,
                     company_website, source_domain, agency_filter_result,
                     mobile_status, agency_filter_method, agency_filter_reason,
                     scrape_mode, source_industry, rejected, scraped_at
              from prospeo_new_leads
              where scrape_mode = %s and scraped_at >= %s
            """
            params: list = [args.mode, since_iso]
            if args.provider:
                sql += " and provider = %s"
                params.append(args.provider)
            sql += " order by rejected, scraped_at desc"
            cur.execute(sql, params)
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
                if r[14]:
                    rejected.append(lead)
                else:
                    accepted.append(lead)
    finally:
        conn.close()

    os.makedirs(args.out_dir, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    suffix = f"_{args.provider}" if args.provider else ""
    csv_path = f"{args.out_dir}/today_{args.mode}{suffix}_{stamp}.csv"
    xlsx_path = f"{args.out_dir}/today_{args.mode}{suffix}_{stamp}.xlsx"
    write_csv(accepted, csv_path)
    write_xlsx(accepted, rejected, xlsx_path)

    print(f"Exported {len(accepted)} accepted + {len(rejected)} rejected "
          f"(mode={args.mode}, provider={args.provider or 'any'}, since={since_iso}):")
    print(f"  CSV  : {csv_path}")
    print(f"  XLSX : {xlsx_path}")


if __name__ == "__main__":
    main()
