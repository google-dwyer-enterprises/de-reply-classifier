"""Identity-mismatch bridge for the follow-up tracker (pending task #1).

Some booked/replied leads reply from a DIFFERENT email address than the one
Instantly actually campaigned (cross-campaign identity mismatch), so their
manual follow-ups -- sent to the *campaigned* address -- never attach to the
outcome row (every join in followup_tracker_mv is lead_email-equality) and the
lead shows blank. The deterministic bridge is `thread_id`: a reply and the
outbound it answers share the Instantly conversation id even when the two email
addresses differ. Thread ids are effectively 1:1 per lead (verified: 3 of 163k
span >1 email), so the bridge is safe.

This script:
  1. (re)creates a plain view `lead_email_aliases` (outcome_email -> campaigned_email),
     restricted to STRICTLY 1:1 pairs in both directions (no fan-out, no ambiguity).
  2. rewrites the two CTEs in `followup_tracker_mv` that read sent_messages
     (`ranked_outbounds`, `ffup_counts`) to attribute a campaigned address's
     unibox follow-ups back to the outcome email via the alias. Replies are
     already FROM the outcome email, so no other CTE changes; `booked_empty_marker`
     auto-corrects (recovered leads gain cnt>0 and drop out of the empty set).

Non-bridged leads are untouched: COALESCE(alias, lead_email) == lead_email when
there's no alias, so their rows are byte-identical to before. The script asserts
this (no lead may LOSE follow-ups) before committing.

Read-only-safe to re-run: idempotent on the alias view; if the bridge is already
applied to followup_tracker_mv it detects that and only refreshes the alias view.
"""
import re
import sys

sys.path.insert(0, ".")
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
from dotenv import load_dotenv

load_dotenv(".env")
from db import connect

ALIAS_VIEW = """
CREATE OR REPLACE VIEW lead_email_aliases AS
WITH bridge AS (
    SELECT DISTINCT r.lead_email AS outcome_email, s.lead_email AS campaigned_email
    FROM replies r
    JOIN sent_messages s ON s.thread_id = r.thread_id
    WHERE r.thread_id IS NOT NULL
      AND s.send_kind = 'unibox_manual'
      AND s.lead_email <> r.lead_email
      -- additive only: never re-attribute follow-ups away from an address that is
      -- itself a tracked lead (would blank out a displayed row -- e.g. forward/colleague threads).
      AND s.lead_email NOT IN (SELECT lead_email FROM lead_outcomes WHERE lead_email IS NOT NULL)
),
oc AS (SELECT outcome_email FROM bridge GROUP BY outcome_email HAVING count(DISTINCT campaigned_email) = 1),
cc AS (SELECT campaigned_email FROM bridge GROUP BY campaigned_email HAVING count(DISTINCT outcome_email) = 1)
SELECT b.outcome_email, b.campaigned_email
FROM bridge b
JOIN oc ON oc.outcome_email = b.outcome_email
JOIN cc ON cc.campaigned_email = b.campaigned_email
"""

# (pattern, replacement, label) -- whitespace-flexible regex, each must match exactly once.
RANKED_SUBS = [
    (r"SELECT\s+s\.id,\s+s\.lead_email,",
     "SELECT s.id,\n            COALESCE(a.outcome_email, s.lead_email) AS lead_email,",
     "ranked_outbounds.select"),
    (r"PARTITION BY\s+s\.lead_email\s+ORDER BY",
     "PARTITION BY COALESCE(a.outcome_email, s.lead_email) ORDER BY",
     "ranked_outbounds.partition"),
    (r"FROM\s+sent_messages\s+s\s+WHERE\s*\(\s*s\.send_kind\s*=\s*'unibox_manual'::text\s*\)",
     "FROM (sent_messages s\n             LEFT JOIN lead_email_aliases a ON ((a.campaigned_email = s.lead_email)))\n          WHERE (s.send_kind = 'unibox_manual'::text)",
     "ranked_outbounds.from"),
    (r"SELECT\s+sent_messages\.lead_email,\s+count\(\*\)\s+AS\s+cnt",
     "SELECT COALESCE(a.outcome_email, sent_messages.lead_email) AS lead_email,\n            count(*) AS cnt",
     "ffup_counts.select"),
    (r"FROM\s+sent_messages\s+WHERE\s*\(\s*sent_messages\.send_kind\s*=\s*'unibox_manual'::text\s*\)\s+GROUP BY\s+sent_messages\.lead_email",
     "FROM (sent_messages\n             LEFT JOIN lead_email_aliases a ON ((a.campaigned_email = sent_messages.lead_email)))\n          WHERE (sent_messages.send_kind = 'unibox_manual'::text)\n          GROUP BY COALESCE(a.outcome_email, sent_messages.lead_email)",
     "ffup_counts.from"),
]


def per_lead_counts(cur):
    cur.execute('SELECT "Email Address", "Total Follow-ups" FROM followup_tracker_mv')
    return {e: c for e, c in cur.fetchall()}


def main():
    conn = connect()
    conn.autocommit = True
    cur = conn.cursor()

    # 1. alias view
    cur.execute(ALIAS_VIEW)
    cur.execute("GRANT SELECT ON lead_email_aliases TO anon, authenticated, service_role")
    cur.execute("SELECT count(*) FROM lead_email_aliases")
    n_alias = cur.fetchone()[0]
    print(f"lead_email_aliases: {n_alias} deterministic 1:1 outcome->campaigned pairs")

    # 2. tracker view
    cur.execute("SELECT definition FROM pg_views WHERE viewname='followup_tracker_mv'")
    defn = cur.fetchone()[0]
    if "lead_email_aliases" in defn:
        print("followup_tracker_mv already bridged -- alias view refreshed, view unchanged.")
        conn.close()
        return

    before = per_lead_counts(cur)

    new_defn = defn
    for pat, repl, label in RANKED_SUBS:
        new_defn, n = re.subn(pat, repl, new_defn, count=0)
        assert n == 1, f"{label}: expected 1 match, got {n}"

    # probe the rewritten view before committing
    cur.execute(f"SELECT * FROM ({new_defn.rstrip().rstrip(';')}) probe LIMIT 1")
    print("rewritten followup_tracker_mv probe ok")

    conn.autocommit = False
    cur.execute("SET lock_timeout = '5s'")
    cur.execute(f"CREATE OR REPLACE VIEW followup_tracker_mv AS {new_defn.rstrip().rstrip(';')}")

    after = per_lead_counts(cur)
    dropped = [(e, before[e], after.get(e, 0)) for e in before if after.get(e, 0) < before[e]]
    recovered = [e for e in after if after[e] > before.get(e, 0)]
    if dropped:
        conn.rollback()
        raise SystemExit(f"ABORT: {len(dropped)} leads LOST follow-ups, e.g. {dropped[:5]}")
    conn.commit()

    gained = sum(after[e] - before.get(e, 0) for e in recovered)
    print(f"committed. leads that gained follow-ups: {len(recovered)} (+{gained} follow-up rows total)")
    print("no lead lost follow-ups (regression check passed).")
    conn.close()


if __name__ == "__main__":
    main()
