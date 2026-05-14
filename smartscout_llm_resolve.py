"""LLM-only second pass for SmartScout matching.

Picks up leads currently marked 'none' in lead_smartscout_match whose fuzzy
score sits in a configurable band (default 85–92), re-runs fuzzy to
regenerate top-5 candidates, then asks Haiku to pick one (or 'none').

Use this AFTER `resolve-smartscout --skip-llm` once you've reviewed the
fuzzy-only results and want to recover the high-confidence grey zone.

CLI: python run.py llm-resolve-smartscout [--min-score N] [--max-score N] [--limit N] [--yes]
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime, timezone

from anthropic import Anthropic
from dotenv import load_dotenv
from psycopg2.extras import execute_values
from rapidfuzz import fuzz, process

from db import connect, refresh_lead_status
from smartscout_upload import normalize_brand


MODEL = "claude-haiku-4-5"
BATCH_SIZE = 25
MAX_TOKENS = 2000
LLM_CANDIDATES = 5
MIN_BRAND_LEN = 3       # match smartscout_resolve.py
MIN_LEN_RATIO = 0.4     # reject candidate if brand much shorter than lead

DEFAULT_MIN = 85.0
DEFAULT_MAX = 92.0


SYSTEM_PROMPT = """You match a lead's company name to one of a few candidate Amazon brand names from SmartScout.

For each lead you get:
- lead_company: the company we have on file for the lead
- candidates: a numbered list of plausible brand matches from SmartScout

Pick the candidate that refers to the SAME company / brand / parent company as lead_company. Rules:
1. If a candidate is the same brand (ignoring punctuation, casing, and corporate suffixes like Inc/LLC/Ltd), pick it.
2. If a candidate is a clear parent or sub-brand of lead_company, pick it ONLY if you are confident.
3. If multiple candidates look plausible but none is a clear match, output "none".
4. If no candidate matches, output "none".

Output a JSON array, one object per lead, in input order:
[{"id": 1, "pick": 2, "reason": "candidate 2 matches brand"}, ...]
or
[{"id": 1, "pick": "none", "reason": "no candidate matches"}, ...]

`pick` is either an integer (the candidate number) or the string "none". `reason` is one short clause under 80 chars.
Output ONLY the JSON array, no preamble, no code fences."""


def fetch_brands(conn) -> dict[str, str]:
    with conn.cursor() as cur:
        cur.execute("select brand_norm, brand_original from smartscout_brands")
        return {r[0]: r[1] for r in cur.fetchall()}


def fetch_grey_zone(conn, min_score: float, max_score: float, limit: int | None) -> list[dict]:
    sql = """
        with lc_with_domain as (
            select
                lc.lead_email,
                lc.apollo_company_name,
                lc.company_name,
                lower(regexp_replace(coalesce(lc.apollo_company_name, ''), '[^a-zA-Z0-9]', '', 'g')) as apollo_norm,
                lower(regexp_replace(split_part(lc.lead_email, '@', 2), '\\..*$', '')) as domain_norm
            from lead_contacts lc
        )
        select
            m.lead_email,
            m.match_score,
            case
                when nullif(trim(lc.apollo_company_name), '') is null then lc.company_name
                when nullif(trim(lc.company_name), '') is null then lc.apollo_company_name
                when length(lc.apollo_norm) > 0 and length(lc.domain_norm) > 0
                     and (lc.apollo_norm like '%%' || lc.domain_norm || '%%' or lc.domain_norm like '%%' || lc.apollo_norm || '%%')
                then lc.apollo_company_name
                else lc.company_name
            end as use_this_company
        from lead_smartscout_match m
        join lc_with_domain lc on lc.lead_email = m.lead_email
        where m.match_method = 'none'
          and m.match_score >= %s
          and m.match_score < %s
    """
    if limit:
        sql += f" limit {int(limit)}"
    with conn.cursor() as cur:
        cur.execute(sql, (min_score, max_score))
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]


def update_match(conn, rows: list[dict]) -> None:
    sql = """
        insert into lead_smartscout_match (lead_email, brand_norm, match_score, match_method, resolved_at, use_this_company)
        values %s
        on conflict (lead_email) do update set
            brand_norm = excluded.brand_norm,
            match_score = excluded.match_score,
            match_method = excluded.match_method,
            resolved_at = excluded.resolved_at,
            use_this_company = excluded.use_this_company
    """
    values = [
        (r["lead_email"], r["brand_norm"], r["match_score"], r["match_method"], r["resolved_at"], r.get("use_this_company"))
        for r in rows
    ]
    with conn.cursor() as cur:
        execute_values(cur, sql, values, page_size=500)
    conn.commit()


def parse_response(text: str) -> list[dict]:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    return json.loads(text)


def chunked(items: list, size: int):
    for i in range(0, len(items), size):
        yield items[i : i + size]


def main(min_score: float = DEFAULT_MIN, max_score: float = DEFAULT_MAX,
         limit: int | None = None, yes: bool = False, dry_run: bool = False) -> None:
    load_dotenv()
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        sys.exit("ANTHROPIC_API_KEY missing from .env")

    conn = connect()
    brands = fetch_brands(conn)
    if not brands:
        conn.close()
        sys.exit("smartscout_brands is empty.")
    brand_norms = [bn for bn in brands.keys() if len(bn) >= MIN_BRAND_LEN]
    print(f"Loaded {len(brand_norms)} brands (filtered to ≥{MIN_BRAND_LEN} chars).")

    leads = fetch_grey_zone(conn, min_score, max_score, limit)
    if not leads:
        conn.close()
        print(f"No leads in score band [{min_score}, {max_score}). Nothing to do.")
        return

    print(f"Found {len(leads)} grey-zone leads (match_score in [{min_score}, {max_score})).")

    # Dedup by normalized company so each unique company is sent to Haiku once.
    unique_companies: dict[str, list[dict]] = {}
    for lead in leads:
        norm = normalize_brand(lead.get("use_this_company") or "")
        if not norm:
            continue
        unique_companies.setdefault(norm, []).append(lead)
    print(f"  → {len(unique_companies)} unique normalized companies (deduped).")

    # Pre-build per-unique-company entries (norm, sample lead_company for prompt, candidates, emails)
    unique_entries = []
    skipped_no_candidates = 0
    for norm, group in unique_companies.items():
        sample = group[0]
        top = process.extract(norm, brand_norms, scorer=fuzz.token_set_ratio, limit=LLM_CANDIDATES)
        # Length-ratio guard: drop candidates much shorter than the lead.
        candidates = [
            (bn, brands[bn]) for bn, _, _ in top
            if len(norm) > 0 and (len(bn) / len(norm)) >= MIN_LEN_RATIO
        ]
        if not candidates:
            skipped_no_candidates += 1
            continue
        unique_entries.append({
            "norm": norm,
            "lead_company": sample.get("use_this_company") or "",
            "emails_with_company": [
                (g["lead_email"], (g.get("use_this_company") or "").strip() or None) for g in group
            ],
            "candidates": candidates,
            "best_score": float(sample["match_score"]) if sample.get("match_score") is not None else None,
        })
    if skipped_no_candidates:
        print(f"  → {skipped_no_candidates} unique companies skipped (no candidate passes length-ratio guard).")

    n_batches = (len(unique_entries) + BATCH_SIZE - 1) // BATCH_SIZE
    est_cost = n_batches * 0.005
    print(f"Will send {n_batches} batches of {BATCH_SIZE} to Haiku (est ~${est_cost:.2f}).")

    if dry_run:
        print("Dry run: no API calls, no DB writes. Exiting.")
        conn.close()
        return

    if not yes:
        ans = input("Proceed? [y/N]: ").strip().lower()
        if ans not in ("y", "yes"):
            print("Aborted.")
            conn.close()
            return

    client = Anthropic(api_key=api_key)
    now_iso = datetime.now(timezone.utc).isoformat()
    results: list[dict] = []
    total_written = 0
    failed = 0

    for batch_idx, batch in enumerate(chunked(unique_entries, BATCH_SIZE), start=1):
        prepared = batch

        lines = [f"Resolve these {len(prepared)} leads:", ""]
        for i, item in enumerate(prepared, 1):
            cand_lines = "; ".join(f"({k}) {c[1]}" for k, c in enumerate(item["candidates"], 1))
            lines.append(f"[{i}] lead_company={item['lead_company']!r} candidates: {cand_lines}")
        user_msg = "\n".join(lines)

        try:
            resp = client.messages.create(
                model=MODEL,
                max_tokens=MAX_TOKENS,
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": user_msg}],
            )
            parsed = parse_response(resp.content[0].text)
        except Exception as exc:
            print(f"  batch {batch_idx}: error {type(exc).__name__}: {exc}")
            failed += len(batch)
            time.sleep(2)
            continue

        batch_results: list[dict] = []
        for parsed_item, item in zip(parsed, prepared):
            pick = parsed_item.get("pick")
            if isinstance(pick, int) and 1 <= pick <= len(item["candidates"]):
                chosen = item["candidates"][pick - 1]
                # fan match out to every lead sharing this normalized company
                for email, raw_company in item["emails_with_company"]:
                    batch_results.append({
                        "lead_email": email,
                        "brand_norm": chosen[0],
                        "match_score": item["best_score"],
                        "match_method": "llm",
                        "resolved_at": now_iso,
                        "use_this_company": raw_company,
                    })
            # 'none' picks → leave untouched (already match_method='none')

        if batch_results:
            try:
                update_match(conn, batch_results)
                total_written += len(batch_results)
                results.extend(batch_results)
            except Exception as exc:
                print(f"  batch {batch_idx}: write failed {type(exc).__name__}: {exc}")
                failed += len(batch)
                continue

        if batch_idx % 10 == 0 or batch_idx == n_batches:
            print(f"  batch {batch_idx}/{n_batches}: {total_written} matched written, {failed} failed")
    conn.close()

    print(f"Done. {len(results)} grey-zone leads upgraded to 'llm' match.")
    if results:
        print("Refreshing lead_status materialized view...")
        refresh_lead_status()
        print("Refreshed.")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--min-score", type=float, default=DEFAULT_MIN,
                   help=f"Minimum fuzzy score band (default {DEFAULT_MIN})")
    p.add_argument("--max-score", type=float, default=DEFAULT_MAX,
                   help=f"Maximum fuzzy score band, exclusive (default {DEFAULT_MAX})")
    p.add_argument("--limit", type=int, default=None, help="Cap number of leads (testing)")
    p.add_argument("--yes", action="store_true", help="Skip confirmation")
    p.add_argument("--dry-run", action="store_true",
                   help="Print cost estimate and exit; no API calls or DB writes")
    a = p.parse_args()
    main(min_score=a.min_score, max_score=a.max_score, limit=a.limit, yes=a.yes,
         dry_run=a.dry_run)
