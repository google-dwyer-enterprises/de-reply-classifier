"""Use Claude to pick the real company name when apollo_company_name and
company_name disagree. Writes result + reason into lead_contacts.

Idempotent: only processes rows where resolved_company_name IS NULL AND the
two source columns disagree (case-insensitive). New uploads only resolve the
new/changed rows; existing decisions are cached forever.

Easy cases (one column empty, both equal, both empty) are NOT sent to Claude
— the view's COALESCE fallback chain handles them deterministically.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time

from anthropic import Anthropic
from dotenv import load_dotenv

from db import connect

MODEL = "claude-haiku-4-5"
BATCH_SIZE = 25
MAX_TOKENS = 2000

SYSTEM_PROMPT = """You disambiguate noisy company-name data for a sales CRM.

For each lead you get:
- email_domain: the email's domain root (e.g. "shiseido" from emea.shiseido.com)
- apollo_name: candidate from Apollo enrichment
- company_name: candidate from a different enrichment source

Pick the real company. Rules:
1. Prefer the candidate that matches the email_domain — that's the company that owns the domain.
2. If email_domain is a known ISP / webmail provider (gmail, yahoo, comcast, shaw, libero, orange, free, t-online, btinternet, ntlworld, etc.), the email tells you nothing about the company. Pick whichever candidate looks like a real business name (a noun/brand, not a country code or single word like "Support" or "BR" or "Emea").
3. If one candidate is obviously a subdomain artifact (geographic codes "BR", "US", "EMEA", or generic words "Support", "Shop", "Sales", "Info"), pick the other.
4. If both look reasonable, prefer the one that's longer/more descriptive.
5. If both look wrong, output the email_domain capitalized as the picked name.

Respond with a JSON array, one object per lead, in input order:
[{"id": 1, "picked": "Shiseido", "reason": "domain emea.shiseido.com matches"}, ...]

`picked` must be a non-empty string. `reason` is one short clause (under 80 chars).
Output ONLY the JSON array, no preamble, no code fences."""


def find_unresolved(conn, limit: int | None = None) -> list[dict]:
    sql = """
        select
          lead_email,
          apollo_company_name,
          company_name,
          (string_to_array(lower(split_part(lead_email, '@', 2)), '.'))[
            array_length(string_to_array(lower(split_part(lead_email, '@', 2)), '.'), 1) - 1
          ] as domain_root
        from lead_contacts
        where resolved_company_name is null
          and nullif(trim(apollo_company_name), '') is not null
          and nullif(trim(company_name), '') is not null
          and lower(trim(apollo_company_name)) <> lower(trim(company_name))
    """
    if limit:
        sql += f" limit {int(limit)}"
    with conn.cursor() as cur:
        cur.execute(sql)
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]


def format_user_message(batch: list[dict]) -> str:
    lines = [f"Resolve these {len(batch)} leads:", ""]
    for i, lead in enumerate(batch, 1):
        domain = lead.get("domain_root") or ""
        apollo = (lead.get("apollo_company_name") or "").strip()
        company = (lead.get("company_name") or "").strip()
        lines.append(f"[{i}] email_domain={domain!r} apollo_name={apollo!r} company_name={company!r}")
    return "\n".join(lines)


def parse_response(text: str) -> list[dict]:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    return json.loads(text)


def chunked(items: list, size: int):
    for i in range(0, len(items), size):
        yield items[i : i + size]


def write_results(conn, results: list[tuple[str, str, str]]) -> None:
    """results = list of (lead_email, picked_name, reason)"""
    sql = """
        update lead_contacts
        set resolved_company_name = %s,
            resolved_company_reason = %s,
            resolved_at = now()
        where lead_email = %s
    """
    with conn.cursor() as cur:
        cur.executemany(sql, [(p, r, e) for (e, p, r) in results])
    conn.commit()


def main(limit: int | None = None) -> None:
    load_dotenv()
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        sys.exit("ANTHROPIC_API_KEY missing from .env")

    client = Anthropic(api_key=api_key)
    conn = connect()

    todo = find_unresolved(conn, limit=limit)
    if not todo:
        print("No unresolved rows. Nothing to do.")
        conn.close()
        return

    print(f"Resolving {len(todo)} ambiguous company names "
          f"({len(todo) // BATCH_SIZE + 1} batches of {BATCH_SIZE})...")

    total_resolved = 0
    total_failed = 0
    for batch_idx, batch in enumerate(chunked(todo, BATCH_SIZE), start=1):
        try:
            resp = client.messages.create(
                model=MODEL,
                max_tokens=MAX_TOKENS,
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": format_user_message(batch)}],
            )
            text = resp.content[0].text
            parsed = parse_response(text)
        except Exception as exc:
            print(f"  batch {batch_idx}: API/parse error: {type(exc).__name__}: {exc}")
            total_failed += len(batch)
            time.sleep(2)
            continue

        results = []
        for item, lead in zip(parsed, batch):
            picked = (item.get("picked") or "").strip()
            reason = (item.get("reason") or "").strip()[:200]
            if picked:
                results.append((lead["lead_email"], picked, reason))

        if results:
            write_results(conn, results)
            total_resolved += len(results)

        if batch_idx % 10 == 0 or batch_idx * BATCH_SIZE >= len(todo):
            print(f"  batch {batch_idx}: {total_resolved} resolved, {total_failed} failed")

    conn.close()
    print(f"Done. Resolved {total_resolved}, failed {total_failed}.")

    if total_resolved:
        print("Refreshing lead_status materialized view...")
        from db import refresh_lead_status
        refresh_lead_status()
        print("Refreshed.")


if __name__ == "__main__":
    lim = int(sys.argv[1]) if len(sys.argv) > 1 else None
    main(limit=lim)
