"""Contact-coverage fix for the client-facing lead view (pending task: coverage).

lead_status_mv is built `FROM lead_contacts LEFT JOIN leads`, so a classified
lead only appears if it also has an Apollo enrichment row. Result: 2,292
classified leads -- including ~191 BOOKED and ~254 INTERESTED -- are invisible
in the client's NocoDB view. This is the single biggest contributor to the
"booked count looks too low" problem (bigger than the #1/#2 residuals combined).

Fix (pure SQL, no API keys, no new columns -> no NocoDB meta-sync needed):
drive the MV's `lc_with_domain` CTE from the UNION of all lead emails
(lead_contacts + leads) and LEFT JOIN lead_contacts for enrichment. Every
classified lead then shows up with its real status / clients / reason /
repeat-booker flag; the Apollo columns are simply NULL for the un-enriched
ones. A booked lead with no company name beats an invisible booked lead.

Surfaces the documented MV drop+recreate dance (modeled on debug/_mv_view_swap2.py).
Run AFTER `run.py update-status` so the surfaced leads carry their v4 labels.
Preserves the #2 repeat-booker columns (operates on the live definition).
"""
import re
import sys
import time

sys.path.insert(0, ".")
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
from dotenv import load_dotenv

load_dotenv(".env")
from db import connect

# (1) drive lc_with_domain from the union of all lead emails, LEFT JOIN contacts for enrichment.
FROM_PAT = (r"FROM\s+lead_contacts\s+lc_1,\s*"
            r"LATERAL\s*\(\s*SELECT\s+string_to_array\(lower\(split_part\(lc_1\.lead_email,\s*'@'::text,\s*2\)\),\s*"
            r"'\.'::text\)\s+AS\s+arr\)\s+parts")
FROM_REPL = (
    "FROM (( SELECT lead_contacts.lead_email FROM lead_contacts\n"
    "                  UNION\n"
    "                  SELECT leads.lead_email FROM leads) ae\n"
    "             LEFT JOIN lead_contacts lc_1 ON ((lc_1.lead_email = ae.lead_email))),\n"
    "            LATERAL ( SELECT string_to_array(lower(split_part(ae.lead_email, '@'::text, 2)), '.'::text) AS arr) parts"
)
# (2) the CTE's projected lead_email must come from the union driver, not the (now-nullable) contact row.
SELECT_PAT = r"SELECT\s+lc_1\.lead_email,"
SELECT_REPL = "SELECT ae.lead_email,"


def visible_booked(cur):
    cur.execute("select count(*) from lead_status where status='booked'")
    return cur.fetchone()[0]


def main():
    conn = connect()
    cur = conn.cursor()
    conn.autocommit = True

    cur.execute("select definition from pg_matviews where matviewname='lead_status_mv'")
    mv = cur.fetchone()[0]
    cur.execute("select definition from pg_views where viewname='lead_status'")
    wrap = cur.fetchone()[0]

    if "UNION" in mv and "ae.lead_email" in mv:
        print("lead_status_mv already coverage-fixed -- nothing to do.")
        conn.close()
        return

    new_mv, n1 = re.subn(FROM_PAT, FROM_REPL, mv, count=0)
    assert n1 == 1, f"FROM/LATERAL anchor: expected 1 match, got {n1}"
    new_mv, n2 = re.subn(SELECT_PAT, SELECT_REPL, new_mv, count=0)
    assert n2 == 1, f"CTE lead_email anchor: expected 1 match, got {n2}"
    new_mv = new_mv.rstrip().rstrip(";")
    new_wrap = wrap.rstrip().rstrip(";")

    cur.execute(f"select * from ({new_mv}) probe limit 1")
    print("MV probe ok")
    cur.execute("select count(*) from lead_status_mv")
    before = cur.fetchone()[0]
    before_booked = visible_booked(cur)
    print(f"rows before: {before} | visible booked before: {before_booked}")

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
    after_booked = visible_booked(cur)
    print(f"rows after: {after} (+{after - before})")
    print(f"visible booked after: {after_booked} (+{after_booked - before_booked})")
    # guard: no lead should have been dropped
    if after < before:
        print("WARNING: row count decreased -- investigate before trusting the view.")
    conn.close()


if __name__ == "__main__":
    main()
