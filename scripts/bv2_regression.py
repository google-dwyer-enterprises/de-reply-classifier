"""bv2 regression against the audit ground truth (qa_audit_labels).

Re-judges every labeled company through the full bv2 funnel (force=True,
cache bypassed on read) and scores against the human-verified labels:

  label 'fail'   -> caught if verdict is a reject verdict OR 'unknown'
                    (review queue counts as caught: a human sees it)
  label 'review' -> good if verdict is 'unknown' or a reject; 'brand' = soft miss
  label 'pass'   -> verdict must NOT be a reject verdict ('brand' or
                    'unknown' both fine). A reject here is a FALSE REJECTION
                    — the hard gate is ZERO of these.

GATE (ROADMAP_IMPLEMENTATION_PLAN.md): >=90% of fails caught, 0 false
rejections on passes.

Usage: python scripts/bv2_regression.py [--limit N] [--buckets fail,review,pass]
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
from dotenv import load_dotenv
load_dotenv(".env")

from db import connect
import brand_verify


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int)
    ap.add_argument("--buckets", default="fail,review,pass")
    args = ap.parse_args()
    buckets = args.buckets.split(",")

    conn = connect()
    with conn.cursor() as cur:
        cur.execute(r"""
            with acc as (
              select distinct on (lower(regexp_replace(company_domain,'^www\.','')))
                     lower(regexp_replace(company_domain,'^www\.','')) as d,
                     company_name,
                     bettercontact_raw->>'company_description' as descr
              from prospeo_new_leads
              where provider='bettercontact'
            )
            select l.domain, l.verdict, l.issue_group,
                   coalesce(acc.company_name, l.domain), acc.descr
            from qa_audit_labels l
            left join acc on acc.d = l.domain
            where l.verdict = any(%s)
            order by l.verdict, l.domain
        """, (buckets,))
        rows = cur.fetchall()
    if args.limit:
        rows = rows[:args.limit]
    print(f"regressing {len(rows)} labeled companies (buckets: {buckets})")

    leads = [{"company_name": name, "company_domain": dom,
              "company_description": descr} for dom, _, _, name, descr in rows]
    labels = {dom: (lv, ig) for dom, lv, ig, _, _ in rows}

    # Chunk to keep progress visible; force=True so bv2 actually re-judges.
    verdicts: dict[str, dict] = {}
    CH = 25
    for i in range(0, len(leads), CH):
        chunk = leads[i:i + CH]
        verdicts.update(brand_verify.verify_domains(conn, chunk, force=True))
        print(f"  progress {min(i + CH, len(leads))}/{len(leads)}")
    conn.close()

    rejects = set(brand_verify.REJECT_VERDICTS)
    outcome: Counter = Counter()
    false_rejections, missed_fails, soft_review_miss = [], [], []
    for dom, (lab, issue) in labels.items():
        v = verdicts.get(dom, {}).get("verdict", "missing")
        outcome[(lab, "reject" if v in rejects else v)] += 1
        if lab == "pass" and v in rejects:
            false_rejections.append((dom, v, verdicts[dom].get("evidence", "")))
        elif lab == "fail" and v not in rejects and v != "unknown":
            missed_fails.append((dom, v, issue))
        elif lab == "review" and v == "brand":
            soft_review_miss.append((dom, issue))

    print("\n=== outcome matrix (label -> bv2 verdict class) ===")
    for (lab, vc), n in sorted(outcome.items()):
        print(f"  {lab:7s} -> {vc:10s} {n:4d}")

    n_fail = sum(1 for l, _ in labels.values() if l == "fail")
    caught = n_fail - len(missed_fails)
    print(f"\nFAIL catch rate: {caught}/{n_fail}")
    print(f"FALSE REJECTIONS on passes: {len(false_rejections)}  <-- hard gate: 0")
    for dom, v, ev in false_rejections:
        print(f"  !! {dom}: {v} — {ev[:140]}")
    print(f"missed fails ({len(missed_fails)}):")
    for dom, v, issue in missed_fails:
        print(f"  {dom}: got {v}  (label issue: {issue[:80]})")
    print(f"review labels passed as brand ({len(soft_review_miss)}):")
    for dom, issue in soft_review_miss:
        print(f"  {dom}  ({issue[:80]})")

    out = {"matrix": {f"{k[0]}->{k[1]}": n for k, n in outcome.items()},
           "false_rejections": false_rejections, "missed_fails": missed_fails,
           "review_passed": soft_review_miss}
    Path("debug/_bv2_regression_results.json").write_text(
        json.dumps(out, indent=1), encoding="utf-8")
    print("\nresults -> debug/_bv2_regression_results.json")


if __name__ == "__main__":
    main()
