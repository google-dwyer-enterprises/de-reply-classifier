"""Category & title booking analysis — live data for the in-app /analytics/category-booking page.

Answers Reggie's #1 ("which categories / titles book calls?") from live tables on
every page load, so it's always current. Read-only.

Method (grounded in the data audit):
  * Booking signal = headline status coalesce(manual_status, auto_status) = 'booked'
    (so the Instantly booked/interested tag promotion counts, not just status1).
  * Denominator = CLEAN / ENGAGED leads — excludes the ~75% noise (unsubscribe /
    OOF / customer-service / wrong-person / no-longer-there). Noise share varies
    68-84% by category, so "rate among all repliers" is biased; engaged removes
    it. A booked lead always counts as engaged (a tag-booked-but-misclassified
    reply can't be dropped).
  * Category recovery: own lead_contacts.industry -> most common industry of
    other contacts on the same company domain (free-email domains excluded) ->
    SmartScout. Lifts categorized bookings ~264 -> ~353 of 483. Title is
    person-specific so it is never borrowed (own record only).
  * Support floor (MIN_ENGAGED) + Wilson 95% intervals; thin groups are counts-only.
"""
from __future__ import annotations

import math

import psycopg2.extras

from db import connect

GENERIC_DOMAINS = [
    "gmail.com", "yahoo.com", "hotmail.com", "outlook.com", "aol.com",
    "icloud.com", "me.com", "live.com", "comcast.net", "msn.com",
]
NOISE_LABELS = [
    "unsubscribe", "oof", "customer_service", "no_longer_there", "wrong_person",
]
MIN_ENGAGED = 30  # support floor: below this a group isn't ranked

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
         case when lc.industry is not null        then 'direct'
              when dt.industry is not null         then 'domain'
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


# Curated bridge: the 12 Prospeo/BetterContact scrape industries (what the
# scraper actually targets) -> the Amazon booking categories they correspond to.
# Needed because the scraper's vocabulary differs from the booking categories and
# only ~5 of 483 booked leads carry a scrape industry, so we can't tie bookings
# to scrape industries directly. A category may feed more than one industry.
SCRAPE_INDUSTRY_MAP = {
    "Cosmetics": ["Beauty & Personal Care"],
    "Personal Care Product Manufacturing": ["Beauty & Personal Care", "Health & Household"],
    "Retail Health and Personal Care Products": ["Health & Household", "Beauty & Personal Care", "Supplements"],
    "Alternative Medicine": ["Supplements", "Health & Household"],
    "Food and Beverage Manufacturing": ["Grocery & Gourmet Food"],
    "Retail Groceries": ["Grocery & Gourmet Food"],
    "Furniture and Home Furnishings Manufacturing": ["Home & Kitchen", "Tools & Home Improvement"],
    "Pet Services": ["Pet Supplies"],
    "Sporting Goods Manufacturing": ["Sports & Outdoors"],
    "Retail Apparel and Fashion": ["Apparel & Fashion", "Clothing, Shoes & Jewelry"],
    "Apparel Manufacturing": ["Apparel & Fashion", "Clothing, Shoes & Jewelry"],
    "Consumer Goods": ["Toys & Games", "Baby Products", "Electronics", "Automotive"],
}


def _scrape_priority(cats: list[dict]) -> list[dict]:
    """Rank the scrape industries by the pooled book rate of the booking
    categories they target. Tiers: >=20% scrape MORE, 14-20% MAINTAIN, <14% LESS."""
    by_label = {c["label"]: c for c in cats}
    out = []
    for industry, amazon_cats in SCRAPE_INDUSTRY_MAP.items():
        booked = engaged = 0
        used = []
        for ac in amazon_cats:
            c = by_label.get(ac)
            if c:
                booked += c["booked"]
                engaged += c["engaged"]
                used.append(ac)
        rate = round(100.0 * booked / engaged, 1) if engaged else None
        lo, hi = wilson(booked, engaged)
        tier = ("more" if rate is not None and rate >= 20
                else "maintain" if rate is not None and rate >= 14
                else "less" if rate is not None else "unknown")
        out.append({"industry": industry, "booked": booked, "engaged": engaged,
                    "rate": rate, "ci_lo": round(lo * 100), "ci_hi": round(hi * 100),
                    "tier": tier, "categories": ", ".join(used) or "—"})
    return sorted(out, key=lambda r: (r["rate"] is not None, r["rate"] or 0), reverse=True)


def wilson(pos: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """Wilson score 95% interval for a proportion (sane on small n)."""
    if n == 0:
        return (0.0, 0.0)
    pos = min(pos, n)  # a rate can't exceed 100%
    phat = pos / n
    denom = 1 + z * z / n
    center = (phat + z * z / (2 * n)) / denom
    margin = z * math.sqrt(phat * (1 - phat) / n + z * z / (4 * n * n)) / denom
    return (max(0.0, center - margin), min(1.0, center + margin))


def _enrich(rows: list[dict]) -> tuple[list[dict], list[dict]]:
    """Attach rate + Wilson CI + share; split by the support floor."""
    total_booked = sum(r["booked"] for r in rows) or 1
    for r in rows:
        eng = r["engaged"] or 0
        r["rate"] = round(100.0 * r["booked"] / eng, 1) if eng else None
        lo, hi = wilson(r["booked"], eng)
        r["ci_lo"], r["ci_hi"] = round(lo * 100), round(hi * 100)
        r["share"] = round(100.0 * r["booked"] / total_booked)
        # Translate the confidence-interval width into a plain reliability badge
        # (non-technical readers don't parse "95% 10-29%"). The exact range goes
        # in the hover tooltip for anyone who wants it.
        width = r["ci_hi"] - r["ci_lo"]
        if width <= 8:
            r["conf"], r["conf_label"] = "reliable", "Reliable"
        elif width <= 15:
            r["conf"], r["conf_label"] = "fair", "Fairly reliable"
        else:
            r["conf"], r["conf_label"] = "rough", "Rough — few leads"
    ranked = sorted([r for r in rows if r["engaged"] >= MIN_ENGAGED],
                    key=lambda r: r["rate"], reverse=True)
    thin = sorted([r for r in rows if r["engaged"] < MIN_ENGAGED and r["booked"] >= 1],
                  key=lambda r: r["booked"], reverse=True)[:30]
    # relative bar widths within the ranked set
    mx = max((r["rate"] or 0) for r in ranked) if ranked else 1
    for r in ranked:
        r["bar"] = round((r["rate"] or 0) / (mx or 1) * 100)
    return ranked, thin


def fetch_category_booking() -> dict:
    conn = connect()
    params = {"generic": GENERIC_DOMAINS, "noise": NOISE_LABELS}
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(RESOLVE + """
              select count(*) filter (where status = 'booked') as booked_total,
                     count(*) filter (where status = 'booked' and category is not null) as booked_cat,
                     count(*) filter (where status = 'booked' and cat_source = 'domain') as via_domain
                from resolved
            """, params)
            cov = dict(cur.fetchone())

            cur.execute(RESOLVE + """
              select category as label,
                     count(*) filter (where status1 <> all(%(noise)s) or status = 'booked') as engaged,
                     count(*) filter (where status = 'booked') as booked
                from resolved where category is not null group by 1
            """, params)
            cats = [dict(r) for r in cur.fetchall()]

            cur.execute(RESOLVE + f"""
              select {TITLE_BUCKET} as label,
                     count(*) filter (where status1 <> all(%(noise)s) or status = 'booked') as engaged,
                     count(*) filter (where status = 'booked') as booked
                from resolved group by 1
            """, params)
            titles = [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()

    return _assemble(cur_cov=cov, cats=cats, titles=titles)


def fetch_scrape_priority() -> list[dict]:
    """Just the 12-industry scrape-priority ranking (no titles/coverage) — for
    the submit form so the next batch targets the best-performing industries.
    Cheaper than fetch_category_booking()."""
    conn = connect()
    params = {"generic": GENERIC_DOMAINS, "noise": NOISE_LABELS}
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(RESOLVE + """
              select category as label,
                     count(*) filter (where status1 <> all(%(noise)s) or status = 'booked') as engaged,
                     count(*) filter (where status = 'booked') as booked
                from resolved where category is not null group by 1
            """, params)
            cats = [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()
    return _scrape_priority(cats)


def _titles_signal_strong(ranked: list[dict]) -> bool:
    """True only when the top RELIABLE title bucket clearly separates from the
    other reliable ones — its Wilson CI floor sits above their CI ceilings.
    Until then the title ranking is within-noise (all bands overlap ~18-28%),
    so the submit form shows a caveat instead of acting on it. Flip to True
    happens automatically once more data pulls a winner clear."""
    reliable = [r for r in ranked if r.get("conf") != "rough"]
    if len(reliable) < 2:
        return False
    top = max(reliable, key=lambda r: r["rate"] or 0)
    return all(top["ci_lo"] > r["ci_hi"] for r in reliable if r is not top)


def fetch_title_priority() -> dict:
    """Title buckets ranked by booking performance — ADVISORY panel for the
    submit form. Titles are a weak discriminator (buckets book within
    overlapping CIs and ~30% of booked leads carry no title), so this is
    informational only and does NOT drive the scrape — BetterContact's tuned
    decision-maker list does. `signal_strong` reports whether the data has
    matured enough to separate a clear winner. Cheaper than
    fetch_category_booking() (title query only)."""
    conn = connect()
    params = {"generic": GENERIC_DOMAINS, "noise": NOISE_LABELS}
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(RESOLVE + f"""
              select {TITLE_BUCKET} as label,
                     count(*) filter (where status1 <> all(%(noise)s) or status = 'booked') as engaged,
                     count(*) filter (where status = 'booked') as booked
                from resolved group by 1
            """, params)
            titles = [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()
    # '(no title given)' is the coverage gap, not an actionable role — surface
    # it as the missing-% caveat, keep it out of the ranking.
    missing = next((t for t in titles if t["label"] == "(no title given)"), None)
    ranked, _thin = _enrich([t for t in titles if t["label"] != "(no title given)"])
    total = sum(t["booked"] for t in titles) or 1
    return {
        "ranked": ranked,
        "signal_strong": _titles_signal_strong(ranked),
        "missing_pct": round(100.0 * (missing["booked"] if missing else 0) / total),
    }


def _assemble(cur_cov, cats, titles) -> dict:
    cov = cur_cov
    cat_ranked, cat_thin = _enrich(cats)
    # '(no title given)' isn't an actionable role — it's the title-coverage gap.
    # Pull it out of the ranking and surface it as a note instead.
    title_missing = next((t for t in titles if t["label"] == "(no title given)"), None)
    title_ranked, title_thin = _enrich([t for t in titles if t["label"] != "(no title given)"])
    booked_total = cov["booked_total"] or 0
    return {
        "booked_total": booked_total,
        "booked_cat": cov["booked_cat"],
        "via_domain": cov["via_domain"],
        "uncat": booked_total - (cov["booked_cat"] or 0),
        "cov_pct": round(100.0 * (cov["booked_cat"] or 0) / (booked_total or 1)),
        "cat_ranked": cat_ranked, "cat_thin": cat_thin,
        "scrape_priority": _scrape_priority(cats),
        "title_ranked": title_ranked, "title_thin": title_thin,
        "title_missing": title_missing,
        "title_missing_pct": round(100.0 * (title_missing["booked"] if title_missing else 0) / (booked_total or 1)),
        "min_engaged": MIN_ENGAGED,
    }
