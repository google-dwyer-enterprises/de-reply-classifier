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


def main() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    load_dotenv()

    session = make_session(get_env("INSTANTLY_API_KEY"))
    limiter = RateLimiter(DEFAULT_MIN_INTERVAL_S)

    conn = connect()
    cur = conn.cursor()
    cur.execute("select lead_email from leads")
    ours = {r[0] for r in cur.fetchall()}
    print(f"{len(ours)} leads to match against Instantly", flush=True)

    buf: dict[str, tuple[str | None, str | None]] = {}
    written = 0

    def flush():
        """Write the accumulated names to `leads` and clear the buffer. Called
        every N pages so progress is DURABLE — a kill loses at most one chunk,
        and re-running (idempotent) continues. Without this, the whole multi-
        thousand-page pull is lost if the process dies before the end."""
        nonlocal buf, written
        rows = [(e, fn, ln) for e, (fn, ln) in buf.items() if e in ours]
        if rows:
            execute_values(
                cur,
                "update leads l set first_name = v.fn, last_name = v.ln "
                "from (values %s) as v(email, fn, ln) "
                "where l.lead_email = v.email "
                "and (l.first_name is distinct from v.fn or l.last_name is distinct from v.ln)",
                rows,
            )
            conn.commit()
            written += cur.rowcount
        buf = {}

    cursor = None
    page = 0
    while True:
        page += 1
        body = {"limit": PAGE_LIMIT}
        if cursor:
            body["starting_after"] = cursor
        resp = post_with_backoff(session, LIST_LEADS_URL, body, limiter)
        items, next_cursor = extract_items(resp.json())
        for it in items:
            email = (it.get("email") or "").strip().lower()
            if not email:
                continue
            fn = (it.get("first_name") or "").strip() or None
            ln = (it.get("last_name") or "").strip() or None
            if fn or ln:
                buf[email] = (fn, ln)
        if page % 25 == 0:
            flush()
            print(f"page {page}: matched-name updates so far {written}", flush=True)
        if next_cursor and next_cursor != cursor:
            cursor = next_cursor
        elif len(items) >= PAGE_LIMIT:
            last_id = items[-1].get("id") if items else None
            if last_id and last_id != cursor:
                cursor = last_id
                continue
            break
        else:
            break

    flush()
    cur.execute("select count(*) from leads where first_name is not null or last_name is not null")
    print(f"done. leads with a name: {cur.fetchone()[0]}", flush=True)
    cur.execute("""select count(*) from leads l
        where (l.first_name is not null or l.last_name is not null)
          and not exists (select 1 from lead_contacts lc where lc.lead_email=l.lead_email)""")
    print(f"  ...of which un-enriched (would've been nameless): {cur.fetchone()[0]}", flush=True)
    cur.close(); conn.close()


if __name__ == "__main__":
    main()
