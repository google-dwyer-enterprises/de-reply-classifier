"""Adversarial audit of the accepted BC pool's brand verdicts.

Every accepted domain whose verdict came from a single LLM judgment or a
name match (site_llm, agentic, smartscout, vendor_llm) gets an INDEPENDENT
second opinion via the web-search channel — evidence the original verdict
didn't use. Deterministic probe verdicts (single-vendor / share-rule) are
out of scope. READ-ONLY: writes nothing to the DB; prints agreements,
disagreements, and inconclusives.

Usage: python scripts/reseller_accept_audit.py [--methods m1,m2] [--limit N]
"""
from __future__ import annotations

import argparse
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from dotenv import load_dotenv
load_dotenv()

from db import connect
import brand_verify

AUDIT_METHODS = ("site_llm", "agentic", "smartscout", "vendor_llm")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--methods", default=",".join(AUDIT_METHODS))
    ap.add_argument("--limit", type=int)
    args = ap.parse_args()
    methods = tuple(args.methods.split(","))

    conn = connect()
    with conn.cursor() as cur:
        cur.execute(r"""
            with acc as (
              select distinct on (lower(regexp_replace(company_domain,'^www\.','')))
                     lower(regexp_replace(company_domain,'^www\.','')) as d,
                     company_name,
                     bettercontact_raw->>'company_description' as descr
              from prospeo_new_leads
              where provider='bettercontact' and not rejected
            )
            select v.domain, acc.company_name, acc.descr, v.method,
                   v.confidence, left(v.evidence, 200)
            from domain_brand_verdicts v
            join acc on acc.d = v.domain
            where v.verdict = 'brand' and v.method = any(%s)
            order by v.method, v.domain
        """, (list(methods),))
        rows = cur.fetchall()
    conn.close()
    if args.limit:
        rows = rows[:args.limit]
    print(f"auditing {len(rows)} accepted-domain brand verdicts "
          f"(methods: {', '.join(methods)})")

    entries = []
    for dom, comp, descr, method, conf, ev in rows:
        entries.append({
            "domain": dom, "company": comp or "", "description": descr,
            "site_llm_note": (f"AUDIT second-opinion. Original verdict: brand "
                              f"via {method}/{conf}: {ev}"),
            "_orig_method": method,
        })

    # Independent web-search judgment; sets verdict only when confident.
    brand_verify._agentic_verdicts(entries, print)

    agree, disagree, inconclusive = [], [], []
    for e in entries:
        v = e.get("verdict")
        if v == "brand":
            agree.append(e)
        elif v == "reseller":
            disagree.append(e)
        else:
            inconclusive.append(e)

    print(f"\n=== audit result over {len(entries)} domains ===")
    by = Counter((e["_orig_method"], e.get("verdict") or "inconclusive")
                 for e in entries)
    for (m, v), n in sorted(by.items()):
        print(f"  {m:12s} -> second opinion {v:12s} {n:4d}")

    print(f"\nDISAGREEMENTS — second opinion says reseller ({len(disagree)}):")
    for e in disagree:
        print(f"  {e['company']}  {e['domain']}  (orig: {e['_orig_method']})")
        print(f"      {str(e.get('evidence'))[:180]}")

    print(f"\nINCONCLUSIVE — second opinion couldn't decide ({len(inconclusive)}):")
    for e in inconclusive:
        print(f"  {e['company']}  {e['domain']}  (orig: {e['_orig_method']})  "
              f"{str(e.get('site_llm_note'))[:100]}")


if __name__ == "__main__":
    main()
