"""Fuzzy-match leads to SmartScout brands.

Pipeline:
  1. Pull lead companies (using same logic as MV's "Use this company") + brand list.
  2. Fuzzy match (rapidfuzz token_set_ratio); ≥FUZZY_HIGH → 'fuzzy'.
  3. Below FUZZY_HIGH → 'none' with the best score recorded (so the LLM
     pass can target a specific score band later).
  4. Upsert results into lead_smartscout_match.

For the LLM second pass on grey-zone leads, see smartscout_llm_resolve.py.

CLI: python run.py resolve-smartscout [--rerun] [--limit N]
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timezone

from dotenv import load_dotenv
from psycopg2.extras import execute_values
from rapidfuzz import fuzz, process

from db import connect, refresh_lead_status
from smartscout_upload import normalize_brand


FUZZY_HIGH = 92.0       # ≥ this → accept directly
MIN_BRAND_LEN = 3       # drop SmartScout brands ≤ 2 chars after normalization (e.g. 'r', 'u')
MIN_LEN_RATIO = 0.4     # reject match if brand_norm is much shorter than lead_norm


def fetch_brands(conn) -> list[tuple[str, str]]:
    with conn.cursor() as cur:
        cur.execute("select brand_norm, brand_original from smartscout_brands")
        return [(r[0], r[1]) for r in cur.fetchall()]


def fetch_lead_companies(conn, only_unresolved: bool, limit: int | None) -> list[dict]:
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
            lc.lead_email,
            case
                when nullif(trim(lc.apollo_company_name), '') is null then lc.company_name
                when nullif(trim(lc.company_name), '') is null then lc.apollo_company_name
                when length(lc.apollo_norm) > 0 and length(lc.domain_norm) > 0
                     and (lc.apollo_norm like '%' || lc.domain_norm || '%' or lc.domain_norm like '%' || lc.apollo_norm || '%')
                then lc.apollo_company_name
                else lc.company_name
            end as use_this_company
        from lc_with_domain lc
    """
    if only_unresolved:
        sql += """
            where not exists (
                select 1 from lead_smartscout_match m
                where m.lead_email = lc.lead_email
            )
        """
    if limit:
        sql += f" limit {int(limit)}"
    with conn.cursor() as cur:
        cur.execute(sql)
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]


def write_matches(conn, rows: list[dict]) -> None:
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


def chunked(items: list, size: int):
    for i in range(0, len(items), size):
        yield items[i : i + size]


def main(rerun: bool = False, limit: int | None = None) -> None:
    load_dotenv()
    conn = connect()

    brands = fetch_brands(conn)
    if not brands:
        conn.close()
        sys.exit("smartscout_brands is empty. Run upload-smartscout first.")
    all_brand_norms = [bn for bn, _ in brands]
    brand_norms = [bn for bn in all_brand_norms if len(bn) >= MIN_BRAND_LEN]
    dropped = len(all_brand_norms) - len(brand_norms)
    print(f"Loaded {len(brand_norms)} brands ({dropped} dropped for being <{MIN_BRAND_LEN} chars).")

    leads = fetch_lead_companies(conn, only_unresolved=not rerun, limit=limit)
    if not leads:
        print("No leads to resolve.")
        conn.close()
        return
    print(f"Resolving {len(leads)} leads (rerun={rerun}).")

    fuzzy_hits: list[dict] = []
    unmatched: list[dict] = []
    now_iso = datetime.now(timezone.utc).isoformat()

    # Dedup: group leads by normalized company so we only fuzzy-match each
    # unique company once, then fan the result out to every lead sharing it.
    unique_companies: dict[str, list[tuple[str, str]]] = {}
    empty_emails: list[str] = []
    for lead in leads:
        raw_company = (lead.get("use_this_company") or "").strip() or None
        norm = normalize_brand(raw_company or "")
        if not norm:
            empty_emails.append(lead["lead_email"])
        else:
            unique_companies.setdefault(norm, []).append((lead["lead_email"], raw_company))

    print(f"  {len(unique_companies)} unique normalized companies "
          f"(deduped from {len(leads)} leads, {len(empty_emails)} empty)")

    for email in empty_emails:
        unmatched.append({
            "lead_email": email,
            "brand_norm": None,
            "match_score": None,
            "match_method": "none",
            "resolved_at": now_iso,
            "use_this_company": None,
        })

    total_unique = len(unique_companies)
    for i, (norm, lead_pairs) in enumerate(unique_companies.items(), 1):
        if i % 2000 == 0 or i == total_unique:
            print(f"  fuzzy progress: {i}/{total_unique} unique "
                  f"({len(fuzzy_hits)} hit rows / {len(unmatched)} unmatched rows)")

        result = process.extractOne(norm, brand_norms, scorer=fuzz.token_set_ratio)
        if not result:
            for email, raw_company in lead_pairs:
                unmatched.append({
                    "lead_email": email,
                    "brand_norm": None,
                    "match_score": None,
                    "match_method": "none",
                    "resolved_at": now_iso,
                    "use_this_company": raw_company,
                })
            continue

        best_norm, best_score, _ = result
        # Length-ratio guard: reject if brand is much shorter than lead.
        # Catches "amazon" matching "Inboostr ... | Amazon Solutions Partner" etc.
        if len(norm) > 0 and (len(best_norm) / len(norm)) < MIN_LEN_RATIO:
            for email, raw_company in lead_pairs:
                unmatched.append({
                    "lead_email": email,
                    "brand_norm": None,
                    "match_score": float(best_score),
                    "match_method": "none",
                    "resolved_at": now_iso,
                    "use_this_company": raw_company,
                })
            continue

        if best_score >= FUZZY_HIGH:
            for email, raw_company in lead_pairs:
                fuzzy_hits.append({
                    "lead_email": email,
                    "brand_norm": best_norm,
                    "match_score": float(best_score),
                    "match_method": "fuzzy",
                    "resolved_at": now_iso,
                    "use_this_company": raw_company,
                })
        else:
            for email, raw_company in lead_pairs:
                unmatched.append({
                    "lead_email": email,
                    "brand_norm": None,
                    "match_score": float(best_score),
                    "match_method": "none",
                    "resolved_at": now_iso,
                    "use_this_company": raw_company,
                })

    print(f"  fuzzy ≥{FUZZY_HIGH}: {len(fuzzy_hits)}")
    print(f"  unmatched:        {len(unmatched)}")

    all_rows = fuzzy_hits + unmatched
    print(f"Writing {len(all_rows)} match rows...")
    write_matches(conn, all_rows)

    conn.close()
    print("Refreshing lead_status materialized view...")
    refresh_lead_status()
    print("Done.")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--rerun", action="store_true",
                   help="Re-resolve all leads, not just unresolved ones")
    p.add_argument("--limit", type=int, default=None,
                   help="Cap number of leads (for testing)")
    a = p.parse_args()
    main(rerun=a.rerun, limit=a.limit)
