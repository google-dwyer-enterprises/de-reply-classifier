"""Build followup_ab_results — the per-arm A/B scoreboard (plain view).

One row per arm (static / ai) over ATTRIBUTED experiments (per-protocol: only
those with a confirmed real send, sent_message_id not null). Wilson CIs + the
winner verdict are layered on in Python (followup_experiments_attrib.fetch_results).

Plain view = auto-recomputes on query. Usage: python scripts/apply_followup_ab_results_view.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from db import connect

VIEW = """
drop view if exists followup_ab_results;
create view followup_ab_results as
select
  arm,
  count(*) filter (where status = 'attributed')                              as decided,
  count(*) filter (where status = 'attributed' and responded_positive)       as positives,
  count(*) filter (where status = 'attributed' and responded_booked)         as booked,
  count(*) filter (where status = 'sent')                                    as pending_outcome,
  count(*) filter (where status in ('assigned'))                             as awaiting_send,
  round(100.0 * avg((responded_positive)::int)
        filter (where status = 'attributed'), 1)                             as positive_pct,
  round(100.0 * avg((responded_booked)::int)
        filter (where status = 'attributed'), 1)                             as booked_pct
from followup_experiments
where arm in ('static', 'ai')
group by arm;
"""


def main() -> None:
    conn = connect()
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute(VIEW)
        cur.execute("select arm, decided, positives, booked from followup_ab_results order by arm")
        rows = cur.fetchall()
    conn.close()
    print("followup_ab_results ready.")
    for r in rows:
        print(f"  {r[0]:7} decided={r[1]} positives={r[2]} booked={r[3]}")
    if not rows:
        print("  (no arms yet — populates as experiments are attributed)")


if __name__ == "__main__":
    main()
