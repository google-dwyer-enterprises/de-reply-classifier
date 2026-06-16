"""Repeat-booker columns for the client-facing lead view (pending task #2).

Adds two computed columns to lead_status_mv (+ its wrapper view lead_status):
  - "# Clients Engaged": distinct count of clients this lead replied to,
    normalized (lower+trim) and excluding the junk token 'other' so the
    Epic/EPIC casing split no longer inflates the count.
  - "Repeat Booker": TRUE when the lead is booked AND engaged >= 2 clients
    -- Victor's "one dude books calls across multiple clients" signal.

Both are derived purely from the existing `leads.clients` string, so no
recompute-pipeline change is needed. Surfaces the MV-swap dance documented in
CLAUDE.md (drop wrapper -> drop MV -> recreate MV -> recreate unique index ->
recreate wrapper -> re-grant), modeled on debug/_mv_view_swap2.py. After running,
the user must trigger a NocoDB meta-sync to pick up the two new columns.

Run AFTER `run.py update-status` so `leads.clients` reflects the latest reclassify.
"""
import sys
import time

sys.path.insert(0, ".")
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
from dotenv import load_dotenv

load_dotenv(".env")
from db import connect

# distinct, normalized, 'other'-excluded client count off leads.clients ("; "-joined)
CNT = ("( SELECT count(DISTINCT lower(btrim(tok)))\n"
       "  FROM unnest(string_to_array(COALESCE(l.clients, ''::text), ';')) AS tok\n"
       "  WHERE lower(btrim(tok)) <> ''::text AND lower(btrim(tok)) <> 'other'::text )")

COL_ANCHOR = "    lc.lead_email\n"
NEW_COLS = (f"    COALESCE({CNT}, 0) AS \"# Clients Engaged\",\n"
            f"    (COALESCE(l.manual_status, l.auto_status) = 'booked'::text\n"
            f"     AND COALESCE({CNT}, 0) >= 2) AS \"Repeat Booker\",\n")

WRAP_ANCHOR = "    lead_email\n"
WRAP_NEW = '    "# Clients Engaged",\n    "Repeat Booker",\n'


def main():
    conn = connect()
    cur = conn.cursor()
    conn.autocommit = True

    cur.execute("select definition from pg_matviews where matviewname='lead_status_mv'")
    mv = cur.fetchone()[0]
    cur.execute("select definition from pg_views where viewname='lead_status'")
    wrap = cur.fetchone()[0]

    if '"Repeat Booker"' in mv:
        print("lead_status_mv already has the repeat-booker columns -- nothing to do.")
        conn.close()
        return
    assert mv.count(COL_ANCHOR) == 1, "MV lead_email anchor not unique"
    assert wrap.count(WRAP_ANCHOR) == 1, "wrap lead_email anchor not unique"

    new_mv = mv.replace(COL_ANCHOR, NEW_COLS + COL_ANCHOR).rstrip().rstrip(";")
    new_wrap = wrap.replace(WRAP_ANCHOR, WRAP_NEW + WRAP_ANCHOR).rstrip().rstrip(";")

    cur.execute(f"select * from ({new_mv}) probe limit 1")
    print("MV probe ok")
    cur.execute("select count(*) from lead_status_mv")
    before = cur.fetchone()[0]
    print(f"rows before: {before}")

    conn.autocommit = False
    for attempt in range(6):
        try:
            cur.execute("set lock_timeout = '5s'")
            cur.execute("drop view lead_status")
            cur.execute("drop materialized view lead_status_mv")
            cur.execute(f"create materialized view lead_status_mv as {new_mv}")
            cur.execute("create unique index lead_status_mv_lead_email_idx on lead_status_mv (lead_email)")
            cur.execute(f"create view lead_status as {new_wrap}")
            cur.execute("grant select, insert, update, delete, truncate, references, trigger "
                        "on lead_status to anon, authenticated, service_role, postgres")
            conn.commit()
            print("swap committed")
            break
        except Exception as e:
            conn.rollback()
            print(f"  attempt {attempt + 1} failed ({type(e).__name__}: {str(e)[:90]}), retrying in 10s")
            time.sleep(10)
    else:
        raise SystemExit("could not swap")

    conn.autocommit = True
    cur.execute("select count(*) from lead_status_mv")
    after = cur.fetchone()[0]
    cur.execute('select "Repeat Booker", count(*) from lead_status group by 1 order by 1')
    flag_dist = cur.fetchall()
    cur.execute('select "# Clients Engaged", count(*) from lead_status group by 1 order by 1')
    cnt_dist = cur.fetchall()
    print(f"rows after: {after} (match: {after == before})")
    print(f"Repeat Booker distribution: {flag_dist}")
    print(f"# Clients Engaged distribution: {cnt_dist}")
    conn.close()


if __name__ == "__main__":
    main()
