"""Snapshot + diff helper for iterative category-mode scrape loop.

Modes:
  --save <path>           Write current category-mode snapshot to JSON at <path>.
  --diff <before> [after] Diff two snapshots. If <after> is omitted, takes a
                          fresh snapshot in-memory. Prints per-industry deltas
                          and a recommended --skip-industries list for the next
                          batch.

The snapshot covers:
  - prospeo_new_leads totals + per-industry accepted/rejected counts
    (filtered to scrape_mode='category')
  - category_scrape_state pagination cursor + cumulative credits per industry

Skip heuristic (per batch):
  - state.exhausted flipped to true       (cursor reached end — scraper already
                                           auto-skips these next run, listed for
                                           visibility only, NOT included in the
                                           --skip-industries CLI list)
  - delta_credits > 0 and delta_accepted == 0  (burned credits, got nothing)
  - delta_credits >= 20 and accept_rate < 0.30 and delta_accepted < 10
                                          (bad fit — costs too much per yield)
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from db import connect


def take_snapshot() -> dict:
    conn = connect()
    cur = conn.cursor()

    cur.execute("select count(*) from prospeo_new_leads where scrape_mode='category'")
    total = cur.fetchone()[0]
    cur.execute("select count(*) from prospeo_new_leads where scrape_mode='category' and not rejected")
    accepted = cur.fetchone()[0]
    cur.execute("select count(*) from prospeo_new_leads where scrape_mode='category' and rejected")
    rejected = cur.fetchone()[0]

    per_industry: dict[str, dict] = {}
    cur.execute("""
        select coalesce(source_industry, '(null)'),
               count(*) filter (where not rejected),
               count(*) filter (where rejected),
               count(*)
        from prospeo_new_leads
        where scrape_mode='category'
        group by source_industry
    """)
    for ind, acc, rej, tot in cur.fetchall():
        per_industry[ind] = {"accepted": acc, "rejected": rej, "total": tot}

    state: dict[str, dict] = {}
    cur.execute("""
        select industry, last_page_consumed, total_pages, exhausted, total_credits_spent
        from category_scrape_state
    """)
    for ind, lp, tp, ex, cr in cur.fetchall():
        state[ind] = {
            "last_page": lp or 0,
            "total_pages": tp,
            "exhausted": bool(ex),
            "credits": cr or 0,
        }

    conn.close()
    return {
        "ts": datetime.now(timezone.utc).isoformat(),
        "totals": {"total": total, "accepted": accepted, "rejected": rejected},
        "per_industry": per_industry,
        "state": state,
    }


def save_snapshot(path: str) -> None:
    snap = take_snapshot()
    Path(path).write_text(json.dumps(snap, indent=2), encoding="utf-8")
    print(f"snapshot saved: {path}")
    print(f"  total={snap['totals']['total']}  "
          f"accepted={snap['totals']['accepted']}  "
          f"rejected={snap['totals']['rejected']}")


def diff_snapshots(before: dict, after: dict) -> None:
    bt = before["totals"]
    at = after["totals"]
    d_total = at["total"] - bt["total"]
    d_acc = at["accepted"] - bt["accepted"]
    d_rej = at["rejected"] - bt["rejected"]

    print(f"=== batch delta ===")
    print(f"  before: {before['ts']}")
    print(f"  after:  {after['ts']}")
    print(f"  total:    {bt['total']:5d} -> {at['total']:5d}  (Δ {d_total:+d})")
    print(f"  accepted: {bt['accepted']:5d} -> {at['accepted']:5d}  (Δ {d_acc:+d})")
    print(f"  rejected: {bt['rejected']:5d} -> {at['rejected']:5d}  (Δ {d_rej:+d})")

    industries = sorted(set(before["per_industry"]) | set(after["per_industry"])
                        | set(before["state"]) | set(after["state"]))

    rows = []
    for ind in industries:
        b_ind = before["per_industry"].get(ind, {"accepted": 0, "rejected": 0, "total": 0})
        a_ind = after["per_industry"].get(ind, {"accepted": 0, "rejected": 0, "total": 0})
        b_st = before["state"].get(ind, {"credits": 0, "exhausted": False, "last_page": 0})
        a_st = after["state"].get(ind, {"credits": 0, "exhausted": False, "last_page": 0})

        d_acc_i = a_ind["accepted"] - b_ind["accepted"]
        d_rej_i = a_ind["rejected"] - b_ind["rejected"]
        d_tot_i = a_ind["total"] - b_ind["total"]
        d_cr_i = a_st["credits"] - b_st["credits"]
        d_pg_i = a_st["last_page"] - b_st["last_page"]
        accept_rate = (d_acc_i / d_tot_i) if d_tot_i > 0 else None
        newly_exhausted = a_st["exhausted"] and not b_st["exhausted"]

        rows.append({
            "industry": ind,
            "d_accepted": d_acc_i,
            "d_rejected": d_rej_i,
            "d_total": d_tot_i,
            "d_credits": d_cr_i,
            "d_pages": d_pg_i,
            "accept_rate": accept_rate,
            "newly_exhausted": newly_exhausted,
            "exhausted_now": a_st["exhausted"],
        })

    rows.sort(key=lambda r: r["d_credits"], reverse=True)

    print()
    print(f"per-industry (sorted by credits spent this batch):")
    print(f"  {'industry':<50s} {'Δacc':>5s} {'Δrej':>5s} {'Δtot':>5s} "
          f"{'Δcr':>5s} {'Δpg':>4s} {'rate':>6s}  flags")
    for r in rows:
        if r["d_credits"] == 0 and r["d_total"] == 0:
            continue
        rate_str = f"{r['accept_rate']*100:5.1f}%" if r["accept_rate"] is not None else "  n/a"
        flags = []
        if r["newly_exhausted"]:
            flags.append("EXHAUSTED")
        if r["d_credits"] > 0 and r["d_accepted"] == 0:
            flags.append("ZERO-YIELD")
        if (r["d_credits"] >= 20 and r["accept_rate"] is not None
                and r["accept_rate"] < 0.30 and r["d_accepted"] < 10):
            flags.append("BAD-FIT")
        flag_str = " ".join(flags)
        print(f"  {r['industry']:<50s} {r['d_accepted']:>5d} {r['d_rejected']:>5d} "
              f"{r['d_total']:>5d} {r['d_credits']:>5d} {r['d_pages']:>4d} {rate_str}  {flag_str}")

    recommended_skip = sorted([
        r["industry"] for r in rows
        if not r["newly_exhausted"]
        and not r["exhausted_now"]
        and (
            (r["d_credits"] > 0 and r["d_accepted"] == 0)
            or (r["d_credits"] >= 20 and r["accept_rate"] is not None
                and r["accept_rate"] < 0.30 and r["d_accepted"] < 10)
        )
    ])
    newly_exhausted = sorted([r["industry"] for r in rows if r["newly_exhausted"]])

    print()
    if newly_exhausted:
        print("newly exhausted (scraper auto-skips, no action needed):")
        for ind in newly_exhausted:
            print(f"  - {ind}")
    if recommended_skip:
        print()
        print("recommended --skip-industries for next batch (low-yield):")
        for ind in recommended_skip:
            print(f"  - {ind}")
        print()
        skip_arg = ",".join(recommended_skip)
        print(f"  CLI form:  --skip-industries \"{skip_arg}\"")
    else:
        print()
        print("no recommended skips — all credited industries delivered yield this batch.")


def main() -> None:
    p = argparse.ArgumentParser()
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--save", metavar="PATH",
                   help="Write current snapshot to PATH as JSON.")
    g.add_argument("--diff", nargs="+", metavar="SNAPSHOT",
                   help="Diff two snapshots: --diff <before.json> [<after.json>]. "
                        "If only one path given, takes a fresh snapshot for 'after'.")
    args = p.parse_args()

    if args.save:
        save_snapshot(args.save)
    else:
        paths = args.diff
        if len(paths) not in (1, 2):
            sys.exit("--diff expects 1 or 2 paths.")
        before = json.loads(Path(paths[0]).read_text(encoding="utf-8"))
        if len(paths) == 2:
            after = json.loads(Path(paths[1]).read_text(encoding="utf-8"))
        else:
            after = take_snapshot()
        diff_snapshots(before, after)


if __name__ == "__main__":
    main()
