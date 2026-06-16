"""Backfill leads.first_name / leads.last_name from Instantly.

Names live nowhere in our DB except lead_contacts (Apollo) -- which is exactly
what the ~2,292 un-enriched (but classified) leads lack, so the coverage-fixed
lead_status_mv would surface them nameless. Instantly itself holds the lead's
first/last name (from the campaign upload), so we pull it from /leads/list (our
own account's key) and store it on `leads`. The MV then COALESCEs Apollo name
first, Instantly name second.

Pages /leads/list exactly like backfill_lead_status.fetch_lead_status_map,
capturing first_name/last_name, then bulk-updates `leads` via execute_values
(only rows we actually have, only where a non-empty name is returned).
Idempotent: re-running just overwrites with the latest Instantly values.
"""
from __future__ import annotations

import sys

from dotenv import load_dotenv
from psycopg2.extras import execute_values

from instantly_sync import (
    DEFAULT_MIN_INTERVAL_S,
    PAGE_LIMIT,
    RateLimiter,
    extract_items,
    get_env,
    make_session,
)
from backfill_lead_status import LIST_LEADS_URL, post_with_backoff
from db import connect


def fetch_name_map(session, limiter) -> dict[str, tuple[str | None, str | None]]:
    out: dict[str, tuple[str | None, str | None]] = {}
    cursor = None
    page = 0
    while True:
        page += 1
        body = {"limit": PAGE_LIMIT}
        if cursor:
            body["starting_after"] = cursor
        print(f"Fetching leads page {page}" + (f" (cursor={cursor})" if cursor else ""))
        resp = post_with_backoff(session, LIST_LEADS_URL, body, limiter)
        items, next_cursor = extract_items(resp.json())
        for it in items:
            email = (it.get("email") or "").strip().lower()
            if not email:
                continue
            fn = (it.get("first_name") or "").strip() or None
            ln = (it.get("last_name") or "").strip() or None
            if fn or ln:
                out[email] = (fn, ln)
        if next_cursor and next_cursor != cursor:
            cursor = next_cursor
        elif len(items) >= PAGE_LIMIT:
            last_id = items[-1].get("id") if items else None
            if last_id and last_id != cursor:
                cursor = last_id
                continue
            return out
        else:
            return out


def main() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    load_dotenv()

    session = make_session(get_env("INSTANTLY_API_KEY"))
    limiter = RateLimiter(DEFAULT_MIN_INTERVAL_S)
    name_map = fetch_name_map(session, limiter)
    print(f"Instantly returned a name for {len(name_map)} leads")

    conn = connect()
    cur = conn.cursor()
    # only the emails we actually track in `leads`
    cur.execute("select lead_email from leads")
    ours = {r[0] for r in cur.fetchall()}
    rows = [(e, fn, ln) for e, (fn, ln) in name_map.items() if e in ours]
    print(f"of those, {len(rows)} match a row in leads")

    if rows:
        execute_values(
            cur,
            "update leads l set first_name = v.fn, last_name = v.ln "
            "from (values %s) as v(email, fn, ln) where l.lead_email = v.email",
            rows,
        )
        conn.commit()
    cur.execute("select count(*) from leads where first_name is not null or last_name is not null")
    print(f"leads with a name now: {cur.fetchone()[0]}")
    # how many of the un-enriched (coverage-surfaced) leads got a name
    cur.execute("""select count(*) from leads l
        where (l.first_name is not null or l.last_name is not null)
          and not exists (select 1 from lead_contacts lc where lc.lead_email=l.lead_email)""")
    print(f"  ...of which un-enriched (would've been nameless): {cur.fetchone()[0]}")
    cur.close(); conn.close()


if __name__ == "__main__":
    main()
