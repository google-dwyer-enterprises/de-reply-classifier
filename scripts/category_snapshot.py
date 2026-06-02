"""Snapshot category-mode counts in prospeo_new_leads + pagination state.
Used to capture before/after numbers around a category scrape run.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from db import connect


def main() -> None:
    conn = connect()
    cur = conn.cursor()

    print("=== category-mode snapshot ===\n")

    cur.execute("select count(*) from prospeo_new_leads where scrape_mode = 'category'")
    total = cur.fetchone()[0]
    cur.execute("select count(*) from prospeo_new_leads where scrape_mode = 'category' and not rejected")
    accepted = cur.fetchone()[0]
    cur.execute("select count(*) from prospeo_new_leads where scrape_mode = 'category' and rejected")
    rejected = cur.fetchone()[0]

    print(f"  total rows: {total}")
    print(f"  accepted:   {accepted}")
    print(f"  rejected:   {rejected}\n")

    print("per-industry:")
    cur.execute("""
        select coalesce(source_industry, '(null)'),
               count(*) filter (where not rejected) as accepted,
               count(*) filter (where rejected) as rejected,
               count(*) as total
        from prospeo_new_leads
        where scrape_mode = 'category'
        group by source_industry
        order by total desc
    """)
    rows = cur.fetchall()
    if not rows:
        print("  (no category-mode rows yet)")
    else:
        for ind, acc, rej, tot in rows:
            print(f"  {ind:<55s}  accepted={acc:3d}  rejected={rej:3d}  total={tot:3d}")

    print("\ncategory_scrape_state (pagination cursor):")
    cur.execute("""
        select industry, last_page_consumed, total_pages, exhausted, total_credits_spent
        from category_scrape_state
        order by industry
    """)
    rows = cur.fetchall()
    if not rows:
        print("  (empty — no category run has touched this table yet)")
    else:
        for ind, lp, tp, ex, cr in rows:
            print(f"  {ind:<55s}  page {lp}/{tp or '?'}  exhausted={ex}  credits={cr}")

    conn.close()


if __name__ == "__main__":
    main()
