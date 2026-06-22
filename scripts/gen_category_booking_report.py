"""Generate the Category Booking Analysis (docs/replies/CATEGORY_BOOKING.html).

Answers Reggie's #1: "which categories / titles actually book calls?" — read-only.

Method (grounded in the data audit, see REPLY_BOT_PENDING / this session):
  * Booking signal = the headline status, coalesce(manual_status, auto_status)
    = 'booked' (so the Instantly booked/interested tag promotion counts, not
    just the classifier's status1).
  * Denominator = CLEAN / ENGAGED leads only. ~75% of replies are noise
    (unsubscribe / OOF / customer-service / wrong-person / no-longer-there) and
    that noise share varies a lot by category (68%-84%), so "rate among all
    repliers" is biased. Book rate = booked / engaged removes that bias and
    matches Reggie's "clean the data first."
  * Category recovery: a booked lead's category comes from (1) its own
    lead_contacts.industry, else (2) the most common industry of OTHER contacts
    sharing its company domain (same company -> same category; free-email
    domains excluded), else (3) SmartScout's category for an exact brand match.
    This lifts categorized bookings from 264 -> 353 of 483.
  * Thin categories (< MIN_ENGAGED) are listed separately, not ranked, and every
    rate carries a Wilson 95% interval so small samples read as uncertain.

Descriptive only — it shows where bookings happened, not proven cause. Run:
    python scripts/gen_category_booking_report.py
"""
from __future__ import annotations

import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import psycopg2.extras  # noqa: E402
from db import connect  # noqa: E402

GENERIC_DOMAINS = [
    "gmail.com", "yahoo.com", "hotmail.com", "outlook.com", "aol.com",
    "icloud.com", "me.com", "live.com", "comcast.net", "msn.com",
]
NOISE_LABELS = [
    "unsubscribe", "oof", "customer_service", "no_longer_there", "wrong_person",
]
MIN_ENGAGED = 30  # support floor: below this a category isn't ranked

# Resolve a category per replied lead: own industry -> domain-borrowed industry
# -> SmartScout category. cat_source records which path won (for the coverage
# breakdown). Title is person-specific so it is NEVER borrowed — only the
# lead's own lead_contacts.title is used.
RESOLVE = """
with dom_top as (
  select dom, industry from (
    select split_part(lower(lead_email), '@', 2) dom, industry,
           row_number() over (partition by split_part(lower(lead_email), '@', 2)
                              order by count(*) desc) rn
      from lead_contacts
     where industry is not null
       and split_part(lower(lead_email), '@', 2) <> all(%(generic)s)
     group by 1, 2
  ) z where rn = 1
),
resolved as (
  select l.lead_email,
         coalesce(l.manual_status, l.auto_status) as status,
         l.status1,
         coalesce(lc.industry, dt.industry, sb.primary_category) as category,
         case when lc.industry is not null       then 'direct'
              when dt.industry is not null        then 'domain'
              when sb.primary_category is not null then 'smartscout'
              else 'none' end as cat_source,
         lc.title
    from leads l
    left join lead_contacts lc on lower(lc.lead_email) = lower(l.lead_email)
    left join dom_top dt on dt.dom = split_part(lower(l.lead_email), '@', 2)
    left join lead_smartscout_match lsm on lower(lsm.lead_email) = lower(l.lead_email)
    left join smartscout_brands sb on sb.brand_norm = lsm.brand_norm
)
"""

TITLE_BUCKET = """
  case
    when title is null or btrim(title) = '' then '(no title given)'
    when lower(title) ~ '(owner|founder|co-?founder|ceo|president|principal|proprietor|partner)'
      then 'Owner / Founder / CEO'
    when lower(title) ~ '(e-?commerce|market|brand|digital|growth)'
      then 'Ecommerce / Marketing'
    when lower(title) ~ '(operation|ops|supply|logistic|sales|account)'
      then 'Operations / Sales'
    when lower(title) ~ '(manager|director|head|vp|chief|lead|coordinator|specialist)'
      then 'Other manager / staff'
    else 'Other'
  end
"""


def wilson(pos: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """Wilson score 95% interval for a proportion (handles small n sanely)."""
    if n == 0:
        return (0.0, 0.0)
    pos = min(pos, n)  # defensive: a rate can't exceed 100%
    phat = pos / n
    denom = 1 + z * z / n
    center = (phat + z * z / (2 * n)) / denom
    margin = z * math.sqrt(phat * (1 - phat) / n + z * z / (4 * n * n)) / denom
    return (max(0.0, center - margin), min(1.0, center + margin))


def fetch(cur, params):
    # --- coverage of the booking signal ---
    cur.execute(RESOLVE + """
      select count(*) filter (where status = 'booked') as booked_total,
             count(*) filter (where status = 'booked' and category is not null) as booked_cat,
             count(*) filter (where status = 'booked' and cat_source = 'direct') as via_direct,
             count(*) filter (where status = 'booked' and cat_source = 'domain') as via_domain,
             count(*) filter (where status = 'booked' and cat_source = 'smartscout') as via_ss
        from resolved
    """, params)
    cov = dict(cur.fetchone())

    # --- by category (clean/engaged denominator) ---
    cur.execute(RESOLVE + """
      select category,
             count(*) filter (where status1 <> all(%(noise)s) or status = 'booked') as engaged,
             count(*) filter (where status = 'booked') as booked
        from resolved
       where category is not null
       group by 1
    """, params)
    cats = [dict(r) for r in cur.fetchall()]

    # --- by title bucket (own title only, clean/engaged) ---
    cur.execute(RESOLVE + f"""
      select {TITLE_BUCKET} as bucket,
             count(*) filter (where status1 <> all(%(noise)s) or status = 'booked') as engaged,
             count(*) filter (where status = 'booked') as booked
        from resolved
       group by 1
    """, params)
    titles = [dict(r) for r in cur.fetchall()]
    return cov, cats, titles


def enrich(rows):
    """Attach rate + Wilson CI + share; split by support floor."""
    total_booked = sum(r["booked"] for r in rows) or 1
    for r in rows:
        eng = r["engaged"] or 0
        r["rate"] = (100.0 * r["booked"] / eng) if eng else None
        lo, hi = wilson(r["booked"], eng)
        r["ci_lo"], r["ci_hi"] = round(lo * 100, 1), round(hi * 100, 1)
        r["share"] = round(100.0 * r["booked"] / total_booked, 1)
    ranked = sorted([r for r in rows if r["engaged"] >= MIN_ENGAGED],
                    key=lambda r: r["rate"], reverse=True)
    thin = sorted([r for r in rows if r["engaged"] < MIN_ENGAGED],
                  key=lambda r: r["booked"], reverse=True)
    return ranked, thin


def esc(s):
    return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def bars(rows, label_key):
    if not rows:
        return "<p class='muted'>No groups clear the support floor yet.</p>"
    mx = max((r["rate"] or 0) for r in rows) or 1
    out = []
    for r in rows:
        w = (r["rate"] or 0) / mx * 100
        out.append(
            f"<div class='row'><div class='lab' title='{esc(r[label_key])}'>{esc(r[label_key])}</div>"
            f"<div class='track'><div class='fill' style='width:{w:.0f}%'></div></div>"
            f"<div class='val'>{r['rate']:.0f}%</div>"
            f"<div class='det'>{r['booked']} booked / {r['engaged']} engaged "
            f"· 95% {r['ci_lo']:.0f}–{r['ci_hi']:.0f}% · {r['share']:.0f}% of bookings</div></div>"
        )
    return "\n".join(out)


def thin_table(rows, label_key):
    # Only the small groups that actually produced a booking are worth showing
    # (the rest are a long tail of rare/free-text labels with no bookings).
    scored = sorted([r for r in rows if r["booked"] >= 1],
                    key=lambda r: r["booked"], reverse=True)[:30]
    no_booking = len(rows) - len([r for r in rows if r["booked"] >= 1])
    if not scored:
        return (f"<p class='muted'>{len(rows)} more small {label_key}s, none with a booking yet.</p>"
                if rows else "")
    trs = "".join(
        f"<tr><td>{esc(r[label_key])}</td><td class='r'>{r['booked']}</td>"
        f"<td class='r'>{r['engaged']}</td></tr>" for r in scored
    )
    tail = (f"<p class='muted'>+ {no_booking} more small {label_key}s with no bookings (omitted).</p>"
            if no_booking > 0 else "")
    return (f"<details><summary>Small {label_key}s that booked (under {MIN_ENGAGED} engaged — "
            f"counts only, too few to rank)</summary><table class='thin'>"
            f"<tr><th>Group</th><th class='r'>Booked</th><th class='r'>Engaged</th></tr>"
            f"{trs}</table></details>{tail}")


def main():
    conn = connect()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            params = {"generic": GENERIC_DOMAINS, "noise": NOISE_LABELS}
            cov, cats, titles = fetch(cur, params)
    finally:
        conn.close()

    cat_ranked, cat_thin = enrich(cats)
    title_ranked, title_thin = enrich(titles)
    uncat = cov["booked_total"] - cov["booked_cat"]
    cov_pct = round(100.0 * cov["booked_cat"] / (cov["booked_total"] or 1))

    doc = f"""<!doctype html><html lang=en><head><meta charset=utf-8>
<meta name=viewport content="width=device-width, initial-scale=1">
<title>Category booking analysis · Dwyer Enterprises</title>
<style>
  body {{ font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Helvetica,Arial,sans-serif;
    color:#1a1d23; max-width:920px; margin:0 auto; padding:32px 24px 80px; line-height:1.5; }}
  h1 {{ font-size:26px; margin:0 0 4px; }}
  h2 {{ font-size:19px; margin:34px 0 10px; padding-bottom:6px; border-bottom:1px solid #e1e4e8; }}
  .lede {{ color:#5e6470; margin:0 0 8px; }}
  .tiles {{ display:flex; gap:14px; flex-wrap:wrap; margin:18px 0; }}
  .tile {{ flex:1; min-width:150px; background:#fff; border:1px solid #e1e4e8; border-radius:10px; padding:14px 16px; }}
  .tile .n {{ font-size:26px; font-weight:800; color:#2563eb; }}
  .tile .k {{ color:#5e6470; font-size:12.5px; margin-top:2px; }}
  .row {{ display:grid; grid-template-columns:200px 1fr 46px; gap:10px 12px; align-items:center; margin:7px 0; }}
  .lab {{ font-weight:600; font-size:13.5px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }}
  .track {{ background:#eef1f5; border-radius:5px; height:22px; }}
  .fill {{ background:linear-gradient(90deg,#16a34a,#22c55e); height:100%; border-radius:5px; }}
  .val {{ font-weight:700; text-align:right; font-variant-numeric:tabular-nums; }}
  .det {{ grid-column:2 / 4; color:#8c959f; font-size:12px; margin:-2px 0 4px; }}
  .callout {{ background:#fffbeb; border:1px solid #fde68a; border-left:4px solid #f59e0b; border-radius:8px;
    padding:14px 18px; margin:22px 0; font-size:13.5px; color:#713f12; }}
  .callout b {{ color:#92400e; }}
  details {{ margin:12px 0; }} summary {{ cursor:pointer; color:#2563eb; font-size:13.5px; }}
  table.thin {{ width:100%; border-collapse:collapse; margin-top:8px; font-size:13px; }}
  table.thin th, table.thin td {{ padding:4px 8px; border-bottom:1px solid #f3f4f6; text-align:left; }}
  table.thin td.r, table.thin th.r {{ text-align:right; font-variant-numeric:tabular-nums; }}
  .muted {{ color:#8c959f; font-size:13px; }}
</style></head><body>

<h1>Which categories &amp; people book calls</h1>
<p class=lede>Across every lead that replied, this ranks <b>book rate</b> by product category and by
contact title. "Book rate" = booked &divide; genuinely-engaged leads (it excludes unsubscribe / auto-reply
/ wrong-person / customer-service noise, which otherwise drags some categories down unfairly).</p>

<div class=tiles>
  <div class=tile><div class=n>{cov['booked_total']}</div><div class=k>Booked calls analyzed</div></div>
  <div class=tile><div class=n>{cov['booked_cat']}</div><div class=k>Matched to a category ({cov_pct}%)</div></div>
  <div class=tile><div class=n>{cov['via_domain']}</div><div class=k>Category recovered via company domain</div></div>
  <div class=tile><div class=n>{uncat}</div><div class=k>Still uncategorized (see caveats)</div></div>
</div>

<h2>Categories that book best</h2>
<p class=lede>Ranked by book rate among engaged leads. Bar = rate; the small print shows the raw
counts, the 95% confidence range, and what share of all bookings the category drove.</p>
{bars(cat_ranked, 'category')}
{thin_table(cat_thin, 'category')}

<h2>Who books — by title</h2>
<p class=lede>Same book rate, grouped by the contact's role (titles are free-text, so they're bucketed).
Title comes from the lead's own record only, so coverage is lower than for categories.</p>
{bars(title_ranked, 'bucket')}
{thin_table(title_thin, 'bucket')}

<div class=callout>
  <b>How to read this — and what it can't tell you.</b>
  <ul style="margin:8px 0 0; padding-left:18px;">
    <li><b>Descriptive, not proof.</b> It shows where bookings <i>happened</i>, not that a category
      <i>causes</i> bookings. Volume matters too — check the "share of bookings" before chasing a high rate on a small base.</li>
    <li><b>Coverage:</b> {cov['booked_cat']} of {cov['booked_total']} bookings ({cov_pct}%) have a category.
      {cov['via_domain']} were recovered by borrowing their company-domain's category; the remaining {uncat}
      (mostly leads whose reply address isn't in the enrichment pool) stay uncategorized and are excluded from the ranking.</li>
    <li><b>Confidence:</b> categories under {MIN_ENGAGED} engaged leads aren't ranked (shown as counts only); every
      ranked rate carries a Wilson 95% interval — wide intervals mean "not enough data to trust the order yet."</li>
    <li><b>Domain borrow assumption:</b> leads on the same company domain are treated as the same category — safe for single-brand companies, looser for conglomerates.</li>
    <li><b>Category labels</b> come from enrichment and mix Amazon browse categories (which dominate the ranked list) with a long tail of other industry labels; only categories with real volume are ranked.</li>
  </ul>
</div>
<p class=muted>Generated by scripts/gen_category_booking_report.py — re-run to refresh.</p>
</body></html>"""

    out = Path(__file__).resolve().parent.parent / "docs" / "replies" / "CATEGORY_BOOKING.html"
    out.write_text(doc, encoding="utf-8")
    print(f"wrote {out}")
    print(f"  booked={cov['booked_total']} categorized={cov['booked_cat']} ({cov_pct}%) "
          f"recovered_via_domain={cov['via_domain']} uncategorized={uncat}")
    print(f"  ranked categories={len(cat_ranked)} (thin={len(cat_thin)}); "
          f"ranked titles={len(title_ranked)} (thin={len(title_thin)})")


if __name__ == "__main__":
    main()
