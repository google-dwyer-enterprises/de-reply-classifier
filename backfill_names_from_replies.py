"""Fill missing first_name / last_name / full_name on lead_contacts
using the Option 4 extraction pipeline (see BACKFILL_NAMES_FROM_REPLIES_PLAN.md).

Pipeline lives in `name_extraction.py` and is shared with the read-only
measurement script `scripts/names_backfill/measure_option4.py`. This file
adds: fetch current state, build update payloads (fill-NULLs-only),
PLAN audit, dry-run guard, chunked upsert, matview refresh.

Update rules (locked):
  - first_name: write only if currently NULL.
  - last_name:  write only if currently NULL.
  - full_name:  compose '<first> <last>' only if currently NULL AND
                effective first + last both present. "Effective" = current
                Apollo value OR newly extracted value.
  - updated_at: bumped on any modified row.
  - Skip row entirely if no fields would change.
  - manual_status / manual_status_set_at / notes: never touched.

CLI:
  python run.py backfill-names-from-replies              # full live run
  python run.py backfill-names-from-replies --dry-run    # PLAN only, no writes
  python run.py backfill-names-from-replies --limit 50   # cap candidates

Adoption sequence:
  1. --dry-run --limit 50    sanity check on a slice
  2. --dry-run               full audit; verify the PLAN list
  3. (real run)              writes + matview refresh
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from collections import Counter
from datetime import datetime, timezone

from anthropic import Anthropic
from dotenv import load_dotenv
from supabase import create_client

import name_extraction as ne
from db import refresh_lead_status
from instantly_sync import (
    DEFAULT_MIN_INTERVAL_S,
    RateLimiter,
    get_env,
    make_session,
)


# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #

UPSERT_CHUNK = 200
UPSERT_RETRIES = 5
UPSERT_RETRY_BASE_DELAY = 2.0

PLAN_SAMPLE_LIMIT = 20      # how many proposed updates to print in the audit


# --------------------------------------------------------------------------- #
# Current-state fetcher (needed to know which fields are fillable)
# --------------------------------------------------------------------------- #

def fetch_current_state(supabase, emails: list[str]) -> dict[str, dict]:
    """{email: {first_name, last_name, full_name}} for each candidate.
    Used to decide which fields are NULL and therefore fillable."""
    out: dict[str, dict] = {}
    for i in range(0, len(emails), ne.LOOKUP_CHUNK):
        chunk = emails[i:i + ne.LOOKUP_CHUNK]
        rows = ne._supabase_retry(
            lambda: supabase.table("lead_contacts")
            .select("lead_email, first_name, last_name, full_name")
            .in_("lead_email", chunk)
            .execute()
            .data or [],
            label=f"lead_contacts.in_(chunk@{i})",
        )
        for r in rows:
            em = (r.get("lead_email") or "").lower()
            if em:
                out[em] = {
                    "first_name": r.get("first_name"),
                    "last_name": r.get("last_name"),
                    "full_name": r.get("full_name"),
                }
    return out


# --------------------------------------------------------------------------- #
# Update payload builder — applies the locked fill-NULLs-only rules
# --------------------------------------------------------------------------- #

def build_update_payload(
    email: str,
    extracted: dict,
    current: dict,
    now_iso: str,
) -> dict | None:
    """Compose the partial upsert row, or None if no fields would change.

    extracted: state[email] from the pipeline — {first, last, source, ...}
    current:   {first_name, last_name, full_name} from lead_contacts today
    """
    ext_first = extracted.get("first")
    ext_last = extracted.get("last")
    if not (ext_first or ext_last):
        return None

    cur_first = current.get("first_name")
    cur_last = current.get("last_name")
    cur_full = current.get("full_name")

    payload: dict = {"lead_email": email}
    changed = False

    if ext_first and cur_first is None:
        payload["first_name"] = ext_first
        changed = True
    if ext_last and cur_last is None:
        payload["last_name"] = ext_last
        changed = True

    # full_name: compose only if currently NULL AND effective first+last both present.
    # Effective = current Apollo value OR newly extracted value.
    eff_first = cur_first or payload.get("first_name") or None
    eff_last = cur_last or payload.get("last_name") or None
    if cur_full is None and eff_first and eff_last:
        payload["full_name"] = f"{eff_first} {eff_last}"
        changed = True

    if not changed:
        return None

    payload["updated_at"] = now_iso
    return payload


# --------------------------------------------------------------------------- #
# Chunked upsert with retry
# --------------------------------------------------------------------------- #

def upsert_with_retry(supabase, chunk: list[dict]) -> None:
    last_exc: Exception | None = None
    for attempt in range(1, UPSERT_RETRIES + 1):
        try:
            supabase.table("lead_contacts").upsert(
                chunk, on_conflict="lead_email"
            ).execute()
            return
        except Exception as exc:
            last_exc = exc
            if attempt == UPSERT_RETRIES:
                break
            delay = UPSERT_RETRY_BASE_DELAY * (2 ** (attempt - 1))
            print(f"    retry {attempt}/{UPSERT_RETRIES - 1} after error: "
                  f"{type(exc).__name__}: {exc} (sleeping {delay:.1f}s)")
            time.sleep(delay)
    raise last_exc  # type: ignore[misc]


# --------------------------------------------------------------------------- #
# PLAN report — printed in both dry-run and full-run modes
# --------------------------------------------------------------------------- #

def print_plan(
    candidates: list[str],
    state: dict[str, dict],
    stats: dict,
    current_state: dict[str, dict],
    updates: list[dict],
) -> None:
    pool = stats["pool"]
    sources = Counter(state[em]["source"] for em in candidates)
    filled_fast = stats["fast_accept_A"] + stats["fast_accept_C"]
    filled_llm = stats["verifier_accept_A"] + stats["verifier_accept_C"]
    filled_extractor = stats["extractor_successes"]
    total_filled = filled_fast + filled_llm + filled_extractor

    fills_first = sum(1 for u in updates if "first_name" in u)
    fills_last = sum(1 for u in updates if "last_name" in u)
    fills_full = sum(1 for u in updates if "full_name" in u)

    print()
    print("=" * 78)
    print("PLAN")
    print("=" * 78)
    print(f"Visible in leads (raw rows):                         {stats['visible_raw']:>5}")
    print(f"  ...unique after dedup:                             {stats['visible_unique']:>5}")
    print(f"  ...skipped (role-account local-part):              {stats['skipped_role']:>5}")
    print(f"  Pool: post-role-filter candidates:                 {pool:>5}")
    print()
    print(f"Pipeline:")
    print(f"  A heuristic fills:                                 {stats['a_fills']:>5}")
    print(f"  C raw fills (Instantly display-name, A-misses):    {stats['c_raw_fills']:>5}")
    print(f"  Fast-path accepts (no LLM):                        {filled_fast:>5}  "
          f"(A={stats['fast_accept_A']} C={stats['fast_accept_C']})")
    print(f"  LLM verifier accepts:                              {filled_llm:>5}  "
          f"(A={stats['verifier_accept_A']} C={stats['verifier_accept_C']})")
    print(f"  LLM verifier rejects:                              {stats['verifier_reject']:>5}")
    print(f"  Residual extractor fills:                          {filled_extractor:>5}")
    print(f"  Pipeline total filled:                             {total_filled:>5}  "
          f"({100*total_filled/pool:5.1f}% of pool)")
    print()
    print(f"Update payload (fill-NULLs-only rules applied):")
    print(f"  Rows that will be updated:                         {len(updates):>5}")
    print(f"  ...with first_name newly filled:                   {fills_first:>5}")
    print(f"  ...with last_name newly filled:                    {fills_last:>5}")
    print(f"  ...with full_name newly composed:                  {fills_full:>5}")
    pipeline_dropped = total_filled - len(updates)
    if pipeline_dropped > 0:
        print(f"  (pipeline-filled rows where no field was NULL: {pipeline_dropped})")

    print()
    print("=" * 78)
    print(f"SAMPLE — first {min(PLAN_SAMPLE_LIMIT, len(updates))} of {len(updates)} proposed updates")
    print("=" * 78)
    if not updates:
        print("  (no updates to make — pool is fully populated or pipeline produced no fills)")
        return
    for u in updates[:PLAN_SAMPLE_LIMIT]:
        em = u["lead_email"]
        cur = current_state.get(em, {})
        cur_first = cur.get("first_name")
        cur_last = cur.get("last_name")
        cur_full = cur.get("full_name")
        new_first = u.get("first_name", cur_first)
        new_last = u.get("last_name", cur_last)
        new_full = u.get("full_name", cur_full)
        s = state[em]
        print(f"  {em}")
        print(f"    via {s['source']:14s}  raw={s['heuristic_origin'] or '-':1s} "
              f"hf={s['heuristic_first']!r}/hl={s['heuristic_last']!r}")
        print(f"    current: first={cur_first!r:18s} last={cur_last!r:18s} full={cur_full!r}")
        print(f"    new:     first={new_first!r:18s} last={new_last!r:18s} full={new_full!r}")


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #

def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="backfill_names_from_replies",
        description="Fill missing first/last/full names on lead_contacts using "
                    "Option 4 (A → C → verifier → extractor) pipeline.",
    )
    parser.add_argument("--dry-run", action="store_true",
                        help="Print PLAN and proposed updates, then exit. No writes, no matview refresh.")
    parser.add_argument("--limit", type=int, default=None,
                        help="Cap candidates after the role-account filter (smaller test run).")
    parser.add_argument("--skip-c", action="store_true",
                        help="Skip technique C (Instantly API). Useful for offline sanity checks.")
    parser.add_argument("--skip-extractor", action="store_true",
                        help="Skip the residual LLM extractor.")
    args = parser.parse_args(argv)

    load_dotenv()
    supabase_url = get_env("SUPABASE_URL")
    supabase_key = get_env("SUPABASE_KEY")
    instantly_key = get_env("INSTANTLY_API_KEY")
    anth_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not anth_key:
        sys.exit("FATAL: ANTHROPIC_API_KEY missing from .env")

    supabase = create_client(supabase_url, supabase_key)
    session = make_session(instantly_key)
    limiter = RateLimiter(DEFAULT_MIN_INTERVAL_S)
    anthropic = Anthropic(api_key=anth_key)

    mode = "DRY-RUN" if args.dry_run else "LIVE RUN"
    print(f"backfill_names_from_replies — {mode}")
    print(f"  ({'no writes will be made' if args.dry_run else 'will write to lead_contacts and refresh matview'})")
    print()

    # Run the shared pipeline (steps 1–7)
    candidates, state, stats = ne.run_pipeline(
        supabase, session, limiter, anthropic,
        limit=args.limit, skip_c=args.skip_c, skip_extractor=args.skip_extractor,
    )

    if not candidates:
        print("\nNothing to do.")
        return

    # Fetch current lead_contacts state to decide what's fillable
    print(f"\nFetching current lead_contacts state for {len(candidates)} candidates...")
    current_state = fetch_current_state(supabase, candidates)
    print(f"  fetched {len(current_state)} current rows.")

    # Build update payloads (fill-NULLs-only)
    now_iso = datetime.now(timezone.utc).isoformat()
    updates: list[dict] = []
    for em in candidates:
        if not (state[em]["first"] or state[em]["last"]):
            continue
        current = current_state.get(em, {"first_name": None, "last_name": None, "full_name": None})
        payload = build_update_payload(em, state[em], current, now_iso)
        if payload is not None:
            updates.append(payload)

    # PLAN — always print, in both dry-run and live mode
    print_plan(candidates, state, stats, current_state, updates)

    # Cost report
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

    if args.dry_run:
        print()
        print("--dry-run: no writes performed, matview not refreshed.")
        return

    if not updates:
        print("\nNothing to write. Skipping matview refresh.")
        return

    # Live run — chunked upsert
    print()
    print("=" * 78)
    print(f"WRITING {len(updates)} updates to lead_contacts (chunks of {UPSERT_CHUNK})")
    print("=" * 78)
    upserted = 0
    n_chunks = (len(updates) + UPSERT_CHUNK - 1) // UPSERT_CHUNK
    for i, chunk in enumerate(ne.chunked(updates, UPSERT_CHUNK), start=1):
        upsert_with_retry(supabase, chunk)
        upserted += len(chunk)
        print(f"  chunk {i}/{n_chunks}: {upserted}/{len(updates)} upserted")

    print(f"\nDone. {upserted} rows upserted into lead_contacts.")

    # Matview refresh
    print("\nRefreshing lead_status materialized view...")
    refresh_lead_status()
    print("Refreshed.")


if __name__ == "__main__":
    main()
