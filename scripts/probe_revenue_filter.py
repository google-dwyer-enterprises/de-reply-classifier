"""Probe Prospeo's revenue filter shape.

Goal: figure out the exact field name + value format Prospeo accepts for
"minimum annual revenue" before we ship it in prospeo_sync.py.

Why this exists: Prospeo silently ignores wrong shapes (the codebase has a
506-credit burn scar from exactly this). FINDINGS.html §6 verified the
industry-filter shape this same way.

Strategy:
  1. Baseline call: title + 1 narrow industry (Cosmetics) -> record total_count.
  2. For each candidate revenue shape, repeat with the extra filter and
     compare total_count.
        400 INVALID_FILTERS / filter_error -> wrong shape, FREE
        200 with same total_count        -> silently ignored, 1 credit wasted
        200 with smaller total_count     -> WORKING, 1 credit (the answer)
        200 with 0 / very small          -> shape right, value too narrow

Worst case cost: ~12 candidates that all parse = 12 credits ($0.24).

Run:
    python scripts/probe_revenue_filter.py --dry-run    # show plan, no calls
    python scripts/probe_revenue_filter.py              # asks y/N before spend
    python scripts/probe_revenue_filter.py --yes        # skip confirmation
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import requests
from dotenv import load_dotenv

from prospeo_sync import PROSPEO_BASE, PROSPEO_TITLES


# A narrow but real baseline so total_count is interpretable (a global
# baseline like "all titles" returns tens of millions and small revenue
# floors barely move the needle). Cosmetics + owner titles ~ a few hundred K.
BASELINE_FILTERS: dict = {
    "company_industry": {"include": ["Cosmetics"]},
    "person_job_title": {
        "include": PROSPEO_TITLES,
        "match": "smart",
        "match_strictness": "normal",
    },
}

# Candidate shapes. Each is the EXTRA block we add to BASELINE_FILTERS.
#
# Round 1 (committed history) established: field name `company_revenue` is
# correct (Prospeo's 400 said "Invalid value for filter 'company_revenue'"
# specifically, vs other names that 400'd as unknown field). The remaining
# question is what VALUE format Prospeo wants under it.
CANDIDATES: list[tuple[str, dict]] = [
    # --- string shorthand under .min (Prospeo uses LinkedIn-style enums for industry) ---
    ("company_revenue.min='1M'",
     {"company_revenue": {"min": "1M"}}),
    ("company_revenue.min='500K'",
     {"company_revenue": {"min": "500K"}}),
    ("company_revenue.min='$1M'",
     {"company_revenue": {"min": "$1M"}}),
    # --- bare value (no min wrapper) ---
    ("company_revenue=500000 (bare int)",
     {"company_revenue": 500000}),
    ("company_revenue='1M' (bare string shorthand)",
     {"company_revenue": "1M"}),
    ("company_revenue='$1M+' (bare string with comparator)",
     {"company_revenue": "$1M+"}),
    # --- include with various bucket-string formats ---
    ("company_revenue.include=['$1M-$10M', '$10M-$50M', ...]",
     {"company_revenue": {"include": [
         "$1M-$10M", "$10M-$50M", "$50M-$100M",
         "$100M-$500M", "$500M-$1B", "$1B+",
     ]}}),
    ("company_revenue.include=['1M-10M', '10M-50M', ...] (no $)",
     {"company_revenue": {"include": [
         "1M-10M", "10M-50M", "50M-100M",
         "100M-500M", "500M-1B", "1B+",
     ]}}),
    ("company_revenue.include=['$1M to $10M', ...] (with 'to')",
     {"company_revenue": {"include": [
         "$1M to $10M", "$10M to $50M", "$50M to $100M",
         "$100M to $500M", "$500M to $1B", "$1B+",
     ]}}),
    ("company_revenue.include=LinkedIn enum-style strings",
     {"company_revenue": {"include": [
         "ONE_TO_TEN_MILLION_USD_REVENUE",
         "TEN_TO_FIFTY_MILLION_USD_REVENUE",
         "FIFTY_TO_HUNDRED_MILLION_USD_REVENUE",
         "HUNDRED_TO_FIVE_HUNDRED_MILLION_USD_REVENUE",
         "FIVE_HUNDRED_MILLION_TO_ONE_BILLION_USD_REVENUE",
         "ONE_TO_TEN_BILLION_USD_REVENUE",
         "MORE_THAN_TEN_BILLION_USD_REVENUE",
     ]}}),
    # --- min+max tuple ---
    ("company_revenue.{min:500000,max:null}",
     {"company_revenue": {"min": 500000, "max": None}}),
    ("company_revenue=[500000,null] (array tuple)",
     {"company_revenue": [500000, None]}),
]


def call(filters: dict, api_key: str) -> tuple[int, dict]:
    """Single search-person call, page 1. Returns (http_status, body)."""
    url = f"{PROSPEO_BASE}/search-person"
    r = requests.post(
        url, json={"filters": filters, "page": 1},
        headers={"X-KEY": api_key, "Content-Type": "application/json"},
        timeout=20,
    )
    try:
        body = r.json()
    except ValueError:
        body = {}
    return r.status_code, body


def classify(http_status: int, body: dict, baseline_total: int) -> tuple[str, int | None, str]:
    """Decide what the response means.

    Returns (verdict, total_count, message).
    verdict in {"working", "ignored", "rejected", "no_results", "error"}.
    """
    if http_status == 200:
        tc = (body.get("pagination") or {}).get("total_count")
        if tc is None:
            return ("error", None, "200 but no pagination.total_count")
        if tc == baseline_total:
            return ("ignored", tc, "total_count matches baseline -> silently ignored")
        if tc == 0:
            return ("no_results", tc, "0 results -> shape may be right, value too narrow")
        if tc < baseline_total:
            return ("working", tc, f"reduced from {baseline_total} -> filter applied")
        # tc > baseline shouldn't happen for an ADD-on filter; flag it
        return ("error", tc, f"total_count={tc} GREATER than baseline={baseline_total}")
    if http_status == 400:
        # error_code may be INVALID_FILTERS / NO_RESULTS / etc.
        ec = body.get("error_code") or ""
        if ec == "NO_RESULTS":
            return ("no_results", 0, "400 NO_RESULTS (free)")
        msg = (body.get("filter_error")
               or body.get("error_toast")
               or body.get("message")
               or ec
               or json.dumps(body)[:200])
        return ("rejected", None, f"{ec or '400'}: {msg}")
    return ("error", None, f"HTTP {http_status}: {json.dumps(body)[:200]}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true",
                        help="List what would be probed. No API calls.")
    parser.add_argument("--yes", action="store_true",
                        help="Skip confirmation prompt.")
    args = parser.parse_args()

    load_dotenv()
    api_key = os.environ.get("PROSPEO_API_KEY", "").strip()

    print(f"Baseline filters: {json.dumps(BASELINE_FILTERS, indent=2)}")
    print(f"\nCandidate shapes to probe ({len(CANDIDATES)}):")
    for label, extra in CANDIDATES:
        print(f"  - {label}")

    print(f"\nWorst-case credit cost: {len(CANDIDATES) + 1} "
          f"(1 baseline + up to {len(CANDIDATES)} candidates). "
          f"Rejections (400) are FREE.")

    if args.dry_run:
        return

    if not api_key:
        sys.exit("PROSPEO_API_KEY not set in .env")

    if not args.yes:
        resp = input(f"Proceed (worst case ~${(len(CANDIDATES) + 1) * 0.02:.2f})? [y/N] ").strip().lower()
        if resp != "y":
            print("Aborted.")
            return

    # 1. Baseline
    print("\n=== baseline ===")
    status, body = call(BASELINE_FILTERS, api_key)
    if status != 200:
        sys.exit(f"Baseline failed (HTTP {status}): {json.dumps(body)[:300]}")
    baseline_total = (body.get("pagination") or {}).get("total_count")
    if baseline_total is None:
        sys.exit(f"Baseline 200 but no total_count: {json.dumps(body)[:300]}")
    print(f"  baseline total_count = {baseline_total} (1 credit spent)")

    # 2. Each candidate
    print(f"\n=== candidates (baseline = {baseline_total}) ===")
    results: list[dict] = []
    for label, extra in CANDIDATES:
        merged = {**BASELINE_FILTERS, **extra}
        status, body = call(merged, api_key)
        verdict, tc, msg = classify(status, body, baseline_total)
        results.append({
            "label": label, "http_status": status,
            "verdict": verdict, "total_count": tc, "message": msg,
        })
        print(f"  [{verdict:9s}] {label}")
        print(f"             http={status} tc={tc} :: {msg}")

    # 3. Summary
    print("\n=== summary ===")
    working = [r for r in results if r["verdict"] == "working"]
    no_results = [r for r in results if r["verdict"] == "no_results"]
    ignored = [r for r in results if r["verdict"] == "ignored"]
    rejected = [r for r in results if r["verdict"] == "rejected"]
    errored = [r for r in results if r["verdict"] == "error"]

    print(f"  working ({len(working)})  <-- USE THESE")
    for r in working:
        print(f"    {r['label']}  (tc={r['total_count']}, baseline={baseline_total})")
    if no_results:
        print(f"  no_results ({len(no_results)})  <-- shape may be right, value too narrow")
        for r in no_results:
            print(f"    {r['label']}")
    if ignored:
        print(f"  ignored ({len(ignored)})  <-- DO NOT USE (filter does nothing)")
        for r in ignored:
            print(f"    {r['label']}")
    print(f"  rejected ({len(rejected)})  <-- wrong shape, free")
    for r in rejected:
        print(f"    {r['label']}")
        print(f"      -> {r['message']}")
    if errored:
        print(f"  errored ({len(errored)})")
        for r in errored:
            print(f"    {r['label']}: {r['message']}")


if __name__ == "__main__":
    main()
