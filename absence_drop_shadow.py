"""Absence-drop — SHADOW measurement (read-only, zero credits, zero pipeline change).

Absence-drop = free-drop a company that MISSES SmartScout instead of paying
Rainforest to confirm it's sub-floor (see the #57 test: ~99% of misses are
sub-floor). Before we ENFORCE that, we measure the false-drop rate it WOULD cause.

We don't need to instrument the pipeline: brand_revenue_cache already IS the
shadow ledger. The revenue cascade only pays Rainforest when SmartScout has no
usable revenue, so every `rainforest`-sourced cache row is a company that reached
the paid check. The TRUE absence-drop targets — companies with NO SmartScout
match — are exactly the cached rows whose brand_norm is NOT in smartscout_brands
(a matched company caches under the SmartScout brand, which IS in that table; a
no-match company caches under its own normalized name, which isn't). Re-applying
the gate's own floor_verdict to each target tells us:

  DROP   -> absence-drop is RIGHT (Rainforest also says sub-floor)
  KEEP   -> FALSE-DROP (a real, at/above-floor brand we'd have thrown away)
  REVIEW -> borderline (under-counted floor; a human would decide)

The false-drop rate here is measured vs what Rainforest WOULD have kept — exactly
the "can we replace the paid check with a free drop?" question. Enforce only once
this rate is proven low across a few batches.
"""
from __future__ import annotations

import argparse

from amazon_revenue_qa import floor_verdict, REVENUE_FLOOR_ANNUAL
from db import connect

# Column order the query returns / bucket_rows expects.
#   (brand_norm, annual_revenue, on_amazon, source, branded_hits, ratings_total, annual_units, fetched_at)
_R_ANN, _R_HITS, _R_RATINGS, _R_UNITS = 1, 4, 5, 6


def bucket_rows(rows, floor_line: float = REVENUE_FLOOR_ANNUAL) -> dict:
    """Pure aggregation over absence-drop target rows -> shadow stats.
    Re-applies floor_verdict (the gate's own logic) so shadow == enforce."""
    tally = {"DROP": 0, "REVIEW": 0, "KEEP": 0}
    zero_presence = 0
    for r in rows:
        hits = r[_R_HITS] or 0
        rev = {"annual_revenue": float(r[_R_ANN]) if r[_R_ANN] is not None else 0.0,
               "branded_hits": hits,
               "ratings_total": r[_R_RATINGS] or 0,
               "annual_units": r[_R_UNITS] or 0}
        if hits == 0:
            zero_presence += 1
        verdict, _ = floor_verdict(rev, floor_line)
        tally[verdict] += 1
    total = sum(tally.values())
    return {
        "floor_line": floor_line,
        "total_misses": total,            # companies absence-drop WOULD drop
        "correct_drops": tally["DROP"],   # Rainforest agrees: sub-floor
        "review": tally["REVIEW"],        # borderline / under-counted
        "false_drops": tally["KEEP"],     # real KEEPs we'd wrongly discard
        "zero_presence": zero_presence,   # $0 on Amazon (clearest drops)
        "false_drop_rate": (tally["KEEP"] / total) if total else 0.0,
        # upper bound: also count the ambiguous REVIEWs as lost
        "false_drop_rate_upper": ((tally["KEEP"] + tally["REVIEW"]) / total) if total else 0.0,
    }


def shadow_stats(cur, since_days: int | None = None,
                 floor_line: float = REVENUE_FLOOR_ANNUAL) -> dict:
    """Query the true absence-drop targets (rainforest-sourced cache rows with NO
    SmartScout match) and bucket them. Optional recency window."""
    where = ["c.source like '%%rainforest%%'",
             "not exists (select 1 from smartscout_brands s where s.brand_norm = c.brand_norm)"]
    params: list = []
    if since_days is not None:
        where.append("c.fetched_at > now() - make_interval(days => %s)")
        params.append(since_days)
    cur.execute(f"""
        select c.brand_norm, c.annual_revenue, c.on_amazon, c.source,
               c.branded_hits, c.ratings_total, c.annual_units, c.fetched_at
          from brand_revenue_cache c
         where {' and '.join(where)}
    """, params)
    s = bucket_rows(cur.fetchall(), floor_line)
    s["since_days"] = since_days
    return s


def report(since_days: int | None = None, floor_line: float = REVENUE_FLOOR_ANNUAL) -> dict:
    conn = connect()
    try:
        cur = conn.cursor()
        s = shadow_stats(cur, since_days, floor_line)
    finally:
        conn.close()
    win = f"last {since_days}d" if since_days else "all-time"
    t = s["total_misses"]
    print(f"=== Absence-drop SHADOW ({win}, floor ${floor_line:,.0f}/yr) ===")
    print(f"  SmartScout-misses (would free-drop): {t}")
    if not t:
        print("  (no misses in window — nothing to measure)")
        return s
    print(f"    $0 Amazon presence      : {s['zero_presence']:4d}  ({100*s['zero_presence']/t:5.1f}%)")
    print(f"    correct DROP (< floor)  : {s['correct_drops']:4d}  ({100*s['correct_drops']/t:5.1f}%)")
    print(f"    REVIEW (borderline)     : {s['review']:4d}  ({100*s['review']/t:5.1f}%)")
    print(f"    FALSE-DROP (real KEEP)  : {s['false_drops']:4d}  ({100*s['false_drops']/t:5.1f}%)")
    print(f"\n  FALSE-DROP RATE: {100*s['false_drop_rate']:.1f}%"
          f"   (upper bound incl. REVIEW: {100*s['false_drop_rate_upper']:.1f}%)")
    print(f"  Rainforest calls absence-drop would SKIP this window: ~{t} "
          f"(~{t}-{round(t*1.6)} credits at ~1-1.6/company)")
    return s


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Absence-drop shadow measurement (read-only)")
    ap.add_argument("--days", type=int, default=None, help="Only count misses cached in the last N days")
    ap.add_argument("--floor", type=int, default=REVENUE_FLOOR_ANNUAL,
                    help=f"Keep/drop line in $/yr (default {REVENUE_FLOOR_ANNUAL})")
    a = ap.parse_args()
    report(since_days=a.days, floor_line=a.floor)
