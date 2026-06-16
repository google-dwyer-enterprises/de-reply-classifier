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

# (3) bundled: a dedicated customer-service flag column (Victor's "separate column" ask).
COL_ANCHOR = "    lc.lead_email\n"
CS_COL = "    (COALESCE(l.manual_status, l.auto_status) = 'customer_service'::text) AS \"Customer Service\",\n"
WRAP_ANCHOR = "    lead_email\n"
WRAP_CS = '    "Customer Service",\n'

# (4) bundled: surface Instantly names (leads.first_name/last_name) when Apollo enrichment is absent,
#     so the coverage-surfaced un-enriched leads aren't nameless. Apollo wins when present.
NAME_SUBS = [
    (r'lc\.full_name AS "Full Name",',
     'COALESCE(lc.full_name, NULLIF(btrim(concat_ws(\' \'::text, l.first_name, l.last_name)), \'\'::text)) AS "Full Name",'),
    (r'lc\.first_name AS "First Name",',
     'COALESCE(lc.first_name, l.first_name) AS "First Name",'),
    (r'lc\.last_name AS "Last Name",',
     'COALESCE(lc.last_name, l.last_name) AS "Last Name",'),
]


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
    # bundled customer-service flag (idempotent guard)
    if '"Customer Service"' not in new_mv:
        assert new_mv.count(COL_ANCHOR) == 1, "MV lead_email anchor not unique"
        assert wrap.count(WRAP_ANCHOR) == 1, "wrap lead_email anchor not unique"
        new_mv = new_mv.replace(COL_ANCHOR, CS_COL + COL_ANCHOR)
        wrap = wrap.replace(WRAP_ANCHOR, WRAP_CS + WRAP_ANCHOR)
    # bundled name-coalesce (idempotent: only if not already wrapped in COALESCE)
    if 'COALESCE(lc.first_name' not in new_mv:
        for pat, repl in NAME_SUBS:
            new_mv, nn = re.subn(pat, repl, new_mv, count=0)
            assert nn == 1, f"name anchor {pat!r}: expected 1 match, got {nn}"
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
