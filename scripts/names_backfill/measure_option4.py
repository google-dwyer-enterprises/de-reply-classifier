"""Read-only: measure END-TO-END Option 4 yield + cost on the live pool.

The full pipeline (steps 1–7) lives in `name_extraction.py` and is shared
with the production feature `backfill_names_from_replies.py`. This script
is a thin wrapper that calls `run_pipeline()` and formats a measurement
report — funnel counts, token cost, source breakdown, audit samples.

Usage:
    python scripts/names_backfill/measure_option4.py
    python scripts/names_backfill/measure_option4.py --limit 100
    python scripts/names_backfill/measure_option4.py --skip-extractor
"""

from __future__ import annotations

import argparse
import os
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from anthropic import Anthropic
from dotenv import load_dotenv
from supabase import create_client

import name_extraction as ne
from instantly_sync import (
    DEFAULT_MIN_INTERVAL_S,
    RateLimiter,
    get_env,
    make_session,
)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=None,
                        help="Cap candidates after role-account filter (smaller test run)")
    parser.add_argument("--skip-c", action="store_true",
                        help="Skip technique C; measure A + extractor only")
    parser.add_argument("--skip-extractor", action="store_true",
                        help="Skip the residual extractor; measure A + C + verifier only")
    args = parser.parse_args()

    load_dotenv()
    supabase = create_client(get_env("SUPABASE_URL"), get_env("SUPABASE_KEY"))
    session = make_session(get_env("INSTANTLY_API_KEY"))
    limiter = RateLimiter(DEFAULT_MIN_INTERVAL_S)
    api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        sys.exit("FATAL: ANTHROPIC_API_KEY missing from .env")
    anthropic = Anthropic(api_key=api_key)

    print("Read-only Option 4 end-to-end measurement. No writes.\n")

    candidates, state, stats = ne.run_pipeline(
        supabase, session, limiter, anthropic,
        limit=args.limit, skip_c=args.skip_c, skip_extractor=args.skip_extractor,
    )
    if not candidates:
        return

    pool = stats["pool"]
    sources = Counter(state[em]["source"] for em in candidates)
    filled_fast = stats["fast_accept_A"] + stats["fast_accept_C"]
    filled_llm = stats["verifier_accept_A"] + stats["verifier_accept_C"]
    filled_extractor = stats["extractor_successes"]
    total_filled = filled_fast + filled_llm + filled_extractor

    # --- Yield report ---------------------------------------------------- #
    print()
    print("=" * 78)
    print("OPTION 4 END-TO-END YIELD")
    print("=" * 78)
    print(f"Pool (unique post-role-filter candidates):           {pool:>5}")
    print(f"  Heuristic fills:")
    print(f"    A (local-part):                                  {stats['a_fills']:>5}")
    print(f"    C (Instantly display-name, A-misses only):       {stats['c_raw_fills']:>5}")
    print(f"  Verification:")
    print(f"    fast-path accept (clean local match, no brand):  {filled_fast:>5}  "
          f"(A={stats['fast_accept_A']}  C={stats['fast_accept_C']})")
    print(f"    LLM verifier accept:                             {filled_llm:>5}  "
          f"(A={stats['verifier_accept_A']}  C={stats['verifier_accept_C']})")
    print(f"    LLM verifier reject:                             {stats['verifier_reject']:>5}")
    print(f"    no-match + no-body (skipped):                    {stats['no_match_no_body']:>5}")
    print(f"    verifier failed batches:                         {stats['verifier_failed_batches']:>5}")
    if not args.skip_extractor:
        print(f"  Residual LLM extractor:")
        print(f"    extracted:                                       {stats['extractor_successes']:>5}")
        print(f"    null:                                            {stats['extractor_null']:>5}")
        print(f"    post-filter rejected:                            {stats['extractor_filtered']:>5}")
        print(f"    failed batches:                                  {stats['extractor_failed_batches']:>5}")
    unfilled_total = sum(v for k, v in sources.items() if k.startswith("unfilled"))
    print(f"  Unfillable:                                        {unfilled_total:>5}")
    print()
    print(f"TOTAL FILLED:                                        {total_filled:>5}  "
          f"({100*total_filled/pool:5.1f}% of pool)")
    print(f"  via fast-path:                                     {filled_fast:>5}  "
          f"({100*filled_fast/pool:5.1f}%)  — no LLM cost")
    print(f"  via LLM verifier:                                  {filled_llm:>5}  "
          f"({100*filled_llm/pool:5.1f}%)")
    print(f"  via residual extractor:                            {filled_extractor:>5}  "
          f"({100*filled_extractor/pool:5.1f}%)")

    # --- Cost ------------------------------------------------------------ #
    print()
    print("=" * 78)
    print("TOKEN SPEND (Haiku 4.5: $1/M in, $5/M out)")
    print("=" * 78)
    v_cost = ne.cost_dollars(stats["verifier_in_tokens"], stats["verifier_out_tokens"])
    e_cost = ne.cost_dollars(stats["extractor_in_tokens"], stats["extractor_out_tokens"])
    print(f"  Verifier            in={stats['verifier_in_tokens']:>8,}  "
          f"out={stats['verifier_out_tokens']:>8,}  ${v_cost:.4f}")
    print(f"  Residual extractor  in={stats['extractor_in_tokens']:>8,}  "
          f"out={stats['extractor_out_tokens']:>8,}  ${e_cost:.4f}")
    print(f"  TOTAL                                              ${v_cost + e_cost:.4f}")

    # --- Source breakdown ------------------------------------------------ #
    print()
    print("=" * 78)
    print("SOURCE BREAKDOWN")
    print("=" * 78)
    for src in ("A_fast", "A_llm", "C_fast", "C_llm", "LLM_extracted",
                "unfilled_no_reply", "unfilled_empty_body", "unfilled_llm_null",
                "unfilled_llm_filtered", "unfilled_llm_failed"):
        n = sources.get(src, 0)
        if n:
            print(f"  {src:24s}  {n:>5}  ({100*n/pool:5.1f}%)")

    # --- Audit samples --------------------------------------------------- #
    print()
    print("=" * 78)
    print("AUDIT SAMPLES")
    print("=" * 78)

    def sample(predicate, label, limit=15):
        print(f"\n--- {label} ---")
        shown = 0
        for em in candidates:
            if predicate(em):
                s = state[em]
                origin_extra = ""
                if s["heuristic_origin"]:
                    origin_extra = (f"  (raw {s['heuristic_origin']}: "
                                    f"{s['heuristic_first']!r}/{s['heuristic_last']!r})")
                print(f"  {em:55s}  first={s['first']!r:18s}  last={s['last']!r}{origin_extra}")
                shown += 1
                if shown >= limit:
                    break
        if shown == 0:
            print("  (none)")

    sample(lambda em: state[em]["source"] == "A_fast",
           "A fast-path accepts (deterministic)")
    sample(lambda em: state[em]["source"] == "C_fast",
           "C fast-path accepts (deterministic)")
    sample(lambda em: state[em]["source"] == "A_llm",
           "A LLM-verified accepts")
    sample(lambda em: state[em]["source"] == "C_llm",
           "C LLM-verified accepts")
    sample(lambda em: state[em]["verifier_path"] == "llm_reject"
                   and state[em]["heuristic_first"] is not None,
           "Verifier rejects (with raw heuristic output, to audit FPs)")
    sample(lambda em: state[em]["source"] == "LLM_extracted",
           "Residual extractor fills")


if __name__ == "__main__":
    main()
