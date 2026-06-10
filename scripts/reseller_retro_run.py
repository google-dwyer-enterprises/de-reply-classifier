"""Retroactively run the full reseller-detection funnel (Stages 0-3) over the
currently-accepted BetterContact leads — the 558 exported on 2026-06-09.

What it does:
  1. Runs brand_verify.verify_domains over every distinct accepted domain
     (cache -> Shopify probe + vendor arbitration -> SmartScout -> site LLM
     -> web-search fallback). Decisive verdicts land in domain_brand_verdicts.
  2. Stamps the brand_verify_* audit columns on the accepted
     prospeo_new_leads rows (does NOT touch rejected/lead_approval — no lead
     is removed; quarantine is a separate human decision).
  3. Writes an annotated XLSX of all 558 leads + verdict columns to exports/.

Usage: python scripts/reseller_retro_run.py [--limit N]
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from dotenv import load_dotenv
load_dotenv()

from db import connect
import brand_verify


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, help="cap distinct domains (smoke)")
    args = ap.parse_args()

    conn = connect()
    with conn.cursor() as cur:
        cur.execute("""
            select id, email, first_name, last_name, title, company_name,
                   company_domain, source_industry, bettercontact_raw
            from prospeo_new_leads
            where provider='bettercontact' and not rejected
            order by company_domain, id
        """)
        rows = cur.fetchall()
    print(f"accepted leads: {len(rows)}")

    leads = []
    for (rid, email, fn, ln, title, comp, dom, ind, raw) in rows:
        b = raw if isinstance(raw, dict) else json.loads(raw or "{}")
        leads.append({
            "id": rid, "email": email, "first_name": fn, "last_name": ln,
            "title": title, "company_name": comp, "company_domain": dom,
            "source_industry": ind,
            "company_description": b.get("company_description"),
        })

    if args.limit:
        seen, capped = set(), []
        for ld in leads:
            d = brand_verify.norm_domain(ld["company_domain"])
            if d not in seen and len(seen) >= args.limit:
                continue
            seen.add(d)
            capped.append(ld)
        leads = capped

    verdicts = brand_verify.verify_domains(conn, leads)

    # Stamp audit columns per lead (no rejection — audit only).
    stamped = 0
    with conn.cursor() as cur:
        for ld in leads:
            v = verdicts.get(brand_verify.norm_domain(ld["company_domain"]) or "")
            if not v:
                continue
            cur.execute("""
                update prospeo_new_leads
                   set brand_verify_result = %s,
                       brand_verify_method = %s,
                       brand_verify_evidence = %s
                 where id = %s
            """, (v["verdict"], v["method"],
                  (v.get("evidence") or "")[:1000], ld["id"]))
            stamped += cur.rowcount
            ld["_v"] = v
    conn.commit()
    conn.close()
    print(f"stamped brand_verify_* on {stamped} lead rows")

    # Summary
    by = Counter((ld["_v"]["verdict"], ld["_v"]["method"].replace("cache:", ""))
                 for ld in leads if "_v" in ld)
    print("\n=== per-lead verdicts ===")
    for (v, m), n in sorted(by.items()):
        print(f"  {v:9s} via {m:14s} {n:4d}")
    resellers = [ld for ld in leads if ld.get("_v", {}).get("verdict") == "reseller"]
    print(f"\n=== RESELLER leads ({len(resellers)}) ===")
    for ld in resellers:
        print(f"  {ld['email']}  {ld['company_name']}  {ld['company_domain']}")
        print(f"      {str(ld['_v'].get('evidence'))[:160]}")
    unknowns = {brand_verify.norm_domain(ld['company_domain'])
                for ld in leads if ld.get("_v", {}).get("verdict") == "unknown"}
    print(f"\nunknown domains needing eyeball: {len(unknowns)}")

    # Annotated sheet
    from openpyxl import Workbook
    wb = Workbook(); ws = wb.active; ws.title = "leads"
    cols = ["email", "first_name", "last_name", "title", "company_name",
            "company_domain", "source_industry",
            "brand_check", "check_method", "check_confidence", "check_evidence"]
    ws.append(cols)
    for ld in sorted(leads, key=lambda l: (l.get("_v", {}).get("verdict", "zz"),
                                           l["company_domain"] or "")):
        v = ld.get("_v", {})
        ws.append([ld["email"], ld["first_name"], ld["last_name"], ld["title"],
                   ld["company_name"], ld["company_domain"], ld["source_industry"],
                   v.get("verdict"), (v.get("method") or "").replace("cache:", ""),
                   v.get("confidence"), str(v.get("evidence") or "")[:500]])
    for i, w in enumerate([34, 14, 14, 24, 28, 26, 22, 11, 14, 11, 80], 1):
        ws.column_dimensions[chr(64 + i)].width = w
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    out = Path(__file__).resolve().parent.parent / "exports" / \
        f"bettercontact_accepted_brandverify_{ts}.xlsx"
    wb.save(out)
    print(f"\nannotated sheet: {out}")


if __name__ == "__main__":
    main()
