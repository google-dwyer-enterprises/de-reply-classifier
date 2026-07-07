"""Amazon Revenue QA — keep only brands that are ON AMAZON and make >= the revenue
floor. Runs after the e-commerce QA (brand_verify), BEFORE email-grab, so we don't
spend enrichment/verification on brands below ICP.

Decision log + full design: docs/scraping/AMAZON_REVENUE_QA_BOT_INSTRUCTIONS.md.

CASCADE (per company) — SmartScout first, Rainforest fallback (2026-07-02 meeting):
  1. strict brand match (amazon_brand_match) -> brand_norm  (kills the loose-match
     false positives that assigned giant brands' revenue to unrelated companies).
  2. revenue, cheap-source-first:
       a. cache (brand_revenue_cache) — a Rainforest credit is spent at most ONCE
          per brand, ever.
       b. SmartScout (estimated_monthly_revenue / trailing_12_months) — free, ~20%
          of companies match. Trusted outside the grey band.
       c. Rainforest (1 credit / 1 search): presence + a REVENUE FLOOR from
          Amazon's own "bought in past month" labels x price. Under-counts by
          construction, so a KEEP on it is never a false positive. Used when
          SmartScout has no match/no revenue, or the SmartScout value is grey.
  3. verdict:
       SmartScout estimate:  >= GREY_HIGH -> KEEP · < GREY_LOW -> DROP ·
                             grey band -> Rainforest confirm.
       Rainforest floor:     0 branded listings -> DROP (not on Amazon) ·
                             floor >= floor line -> KEEP ·
                             established (>= MIN_LISTINGS listings and
                             >= RATINGS_ESTABLISHED ratings) but floor below
                             -> REVIEW (floor under-counts; human decides) ·
                             else -> DROP (small presence).
       API failure -> REVIEW. Nothing is ever silently dropped on an error.

Floor = $300k/yr (Jam 2026-07-07; was $500k from the 2026-06-24 meeting).
All thresholds configurable below.
(Helium 10 UI automation was evaluated and shelved — ToS risk; see
HELIUM10_FEASIBILITY_RESEARCH.md. helium10_revenue.py remains unused.)
"""
from __future__ import annotations

import argparse

import psycopg2.extras
from rapidfuzz import fuzz

from amazon_brand_match import match_brand
from amazon_presence import rainforest_score
from db import connect
from smartscout_upload import normalize_brand

REQUERY_SIM_MIN = 85    # a requery byline must be a SPELLING VARIANT of the search
                        # name (spaceless char similarity), not a truncation to a
                        # shorter/generic/common term — see _is_respelling.


def _is_respelling(search_name: str, byline: str) -> bool:
    """Is `byline` a plausible re-SPELLING of `search_name` (so a canonical-name
    requery is safe), rather than a truncation to a generic/common word?

    Compare the SPACELESS normalized forms char-for-char. 'Scents Ational S' vs
    byline 'ScentSationals' -> ~identical spaceless -> True (the legit case the
    requery exists for). 'Scott's Protein Balls' vs byline 'Scott's' (common) or
    'Protein Balls' (category) -> low ratio -> False, so we DON'T requery into a
    term that would sweep in every 'Scott's'/'Protein Balls' listing on Amazon."""
    a = normalize_brand(search_name).replace(" ", "")
    b = normalize_brand(byline).replace(" ", "")
    if not a or not b:
        return False
    return fuzz.ratio(a, b) >= REQUERY_SIM_MIN

REVENUE_FLOOR_ANNUAL = 300_000      # the keep/drop line (Jam 2026-07-07: lowered
                                    # from $500k to $300k "to be safe" — keep a
                                    # wider net; verdicts are advisory in shadow)
GREY_LOW = 200_000                  # below -> clear drop (SmartScout trusted)
GREY_HIGH = 500_000                 # above -> clear keep (SmartScout trusted)
#  GREY_LOW..GREY_HIGH = escalate to Rainforest (accuracy matters near the line)
MIN_LISTINGS_ESTABLISHED = 2        # listings needed to call a below-floor brand "established"
RATINGS_ESTABLISHED = 1_000         # cumulative ratings needed for the same
CACHE_TTL_DAYS = 90                 # re-fetch a cached Rainforest verdict after this


# --------------------------------------------------------------------------- #
# Revenue providers
# --------------------------------------------------------------------------- #
def _annual(monthly, trailing_12) -> float | None:
    if trailing_12 is not None:
        return float(trailing_12)
    return float(monthly) * 12.0 if monthly is not None else None


def smartscout_revenue(cur, brand_norm: str) -> dict | None:
    cur.execute("""
        select estimated_monthly_revenue, trailing_12_months, dominant_seller_country
          from smartscout_brands where brand_norm = %s
    """, (brand_norm,))
    r = cur.fetchone()
    if not r:
        return None
    ann = _annual(r[0], r[1])
    if ann is None:
        return None
    return {"annual_revenue": ann, "country": r[2], "on_amazon": True, "source": "smartscout"}


def ensure_cache_schema(cur) -> None:
    """Cache columns for the Rainforest floor fields (idempotent)."""
    cur.execute("""
        alter table brand_revenue_cache
          add column if not exists branded_hits int,
          add column if not exists ratings_total int
    """)


def _cache_get(cur, brand_norm: str) -> dict | None:
    cur.execute("""select annual_revenue, dominant_seller_country, on_amazon, source,
                          branded_hits, ratings_total
                     from brand_revenue_cache
                    where brand_norm = %s
                      and fetched_at > now() - make_interval(days => %s)""",
                (brand_norm, CACHE_TTL_DAYS))
    r = cur.fetchone()
    if not r:
        return None
    return {"annual_revenue": float(r[0]) if r[0] is not None else None,
            "country": r[1], "on_amazon": r[2], "source": (r[3] or "") + "+cache",
            "branded_hits": r[4], "ratings_total": r[5]}


def _cache_put(cur, brand_norm: str, rev: dict) -> None:
    cur.execute("""
        insert into brand_revenue_cache (brand_norm, annual_revenue, dominant_seller_country,
                                         on_amazon, source, branded_hits, ratings_total, fetched_at)
        values (%s,%s,%s,%s,%s,%s,%s, now())
        on conflict (brand_norm) do update set
          annual_revenue=excluded.annual_revenue, dominant_seller_country=excluded.dominant_seller_country,
          on_amazon=excluded.on_amazon, source=excluded.source,
          branded_hits=excluded.branded_hits, ratings_total=excluded.ratings_total, fetched_at=now()
    """, (brand_norm, rev.get("annual_revenue"), rev.get("country"), rev.get("on_amazon"),
          rev.get("source"), rev.get("branded_hits"), rev.get("ratings_total")))


def _budget_score(search_name: str, budget: dict | None):
    """rainforest_score behind a hard credit cap. budget = {'max': N, 'spent': n}
    (mutated in place). None budget = uncapped."""
    if budget is not None and budget["spent"] >= budget["max"]:
        return None
    s = rainforest_score(search_name)
    if s is not None and budget is not None:
        budget["spent"] += 1
    return s


def rainforest_floor(cur, search_name: str, budget: dict | None = None) -> dict | None:
    """Rainforest presence + revenue-floor for a brand/company name, cached under
    its normalized form so a credit is spent at most once per name per TTL.

    Canonical-name re-query (1 extra credit, at most once): if the result is
    on-Amazon but below the floor AND the branded listings' dominant byline is
    spelled differently from what we searched, search again with Amazon's own
    spelling and take the stronger result. Fixes the "Scents Ational S" ->
    "ScentSationals" / "Anchor Electronics Inc" -> "Anchor" class of false
    DROPs (verified live 2026-07-03)."""
    key = normalize_brand(search_name)
    if not key:
        return None
    cached = _cache_get(cur, key)
    if cached and "rainforest" in (cached.get("source") or ""):
        return cached
    s = _budget_score(search_name, budget)
    if s is None:
        return None   # API failure / budget exhausted -> caller REVIEWs; never cached
    source = "rainforest"
    byline = (s.get("top_byline") or "").strip()
    if (s["on_amazon"] and s["revenue_floor_annual"] < REVENUE_FLOOR_ANNUAL
            and byline and byline.lower() != (search_name or "").strip().lower()
            and _is_respelling(search_name, byline)):
        s2 = _budget_score(byline, budget)
        if s2 is not None and (s2["revenue_floor_annual"] > s["revenue_floor_annual"]
                               or s2["branded_hits"] > s["branded_hits"]):
            s, source = s2, "rainforest_requery"
    rev = {"annual_revenue": s["revenue_floor_annual"], "country": None,
           "on_amazon": s["on_amazon"], "source": source,
           "branded_hits": s["branded_hits"], "ratings_total": s["ratings_total"]}
    _cache_put(cur, key, rev)
    return rev


def floor_verdict(rev: dict) -> tuple[str, str]:
    """Verdict from a Rainforest floor result. The floor under-counts, so KEEP is
    safe, but a below-floor established brand goes to REVIEW, not DROP."""
    hits = rev.get("branded_hits") or 0
    floor = rev.get("annual_revenue") or 0
    ratings = rev.get("ratings_total") or 0
    if hits == 0:
        return "DROP", "not on Amazon (0 branded listings)"
    if floor >= REVENUE_FLOOR_ANNUAL:
        return "KEEP", f"revenue floor ${floor:,.0f}/yr >= ${REVENUE_FLOOR_ANNUAL:,} (measured, under-counted)"
    if hits >= MIN_LISTINGS_ESTABLISHED and ratings >= RATINGS_ESTABLISHED:
        return "REVIEW", (f"established on Amazon ({hits} listings, {ratings:,} ratings) "
                          f"but measurable floor ${floor:,.0f} < ${REVENUE_FLOOR_ANNUAL:,} — floor under-counts")
    return "DROP", f"small Amazon presence ({hits} listing(s), {ratings:,} ratings, floor ${floor:,.0f})"


# --------------------------------------------------------------------------- #
# Cascade
# --------------------------------------------------------------------------- #
def evaluate(cur, company: str, use_llm: bool = False, budget: dict | None = None) -> dict:
    res = {"company": company, "brand": None, "match_method": None,
           "on_amazon": None, "annual_revenue": None, "source": None, "verdict": "REVIEW",
           "reason": ""}

    def apply_floor(rev, prefix=""):
        if rev is None:
            res["reason"] = prefix + "Rainforest unavailable -> review"
            return
        v, why = floor_verdict(rev)
        res.update(on_amazon=rev["on_amazon"], annual_revenue=rev["annual_revenue"],
                   source=rev["source"], verdict=v, reason=prefix + why)

    if len(normalize_brand(company or "")) < 3:
        # junk/degenerate name ("-", "--", "??") — don't burn a credit searching it
        res.update(verdict="REVIEW", reason="company name too short/junk to search -> review")
        return res

    m = match_brand(cur, company, use_llm=use_llm)
    if not m:
        # No SmartScout brand -> Rainforest presence+floor by COMPANY name.
        apply_floor(rainforest_floor(cur, company, budget), prefix="no SmartScout match; ")
        return res

    res.update(brand=m["brand"], match_method=m["method"])
    # SmartScout FIRST (free, and its estimate must not be shadowed by a cached
    # Rainforest floor — that shadowing silently flipped a grey-zone REVIEW to
    # DROP on re-runs; caught 2026-07-03). Rainforest caching lives inside
    # rainforest_floor.
    rev = smartscout_revenue(cur, m["brand"])
    if rev is not None:
        ann = rev["annual_revenue"]
        res.update(on_amazon=rev["on_amazon"], annual_revenue=ann, source=rev["source"])
        if ann >= GREY_HIGH:
            res.update(verdict="KEEP", reason=f"SmartScout ${ann:,.0f}/yr >= ${GREY_HIGH:,} (clear)")
        elif ann < GREY_LOW:
            res.update(verdict="DROP", reason=f"SmartScout ${ann:,.0f}/yr < ${GREY_LOW:,} (clear)")
        else:
            # borderline -> confirm with the Rainforest floor
            rf = rainforest_floor(cur, m["brand"], budget)
            if rf is None:
                res.update(verdict="REVIEW", reason=f"SmartScout borderline (${ann:,.0f}); Rainforest unavailable -> review")
            elif (rf.get("annual_revenue") or 0) >= REVENUE_FLOOR_ANNUAL:
                res.update(verdict="KEEP", annual_revenue=rf["annual_revenue"], source="smartscout+rainforest",
                           reason=f"borderline SmartScout confirmed: floor ${rf['annual_revenue']:,.0f} >= ${REVENUE_FLOOR_ANNUAL:,}")
            else:
                res.update(verdict="REVIEW", source="smartscout+rainforest",
                           reason=f"SmartScout borderline (${ann:,.0f}) and floor didn't confirm -> review")
        return res

    # SmartScout matched the brand but has no revenue -> Rainforest on the brand
    # (rainforest_floor is cache-first, so repeats cost nothing).
    apply_floor(rainforest_floor(cur, m["brand"], budget), prefix="SmartScout match w/o revenue; ")
    return res


# --------------------------------------------------------------------------- #
# Pipeline integration (bettercontact_sync) — SHADOW-FIRST
# --------------------------------------------------------------------------- #
def ensure_lead_columns(cur) -> None:
    """Verdict columns on prospeo_new_leads (idempotent). Victor's ask: 'that
    number needs to be a column'. NB: also add to migrations.sql at commit time."""
    cur.execute("""
        alter table prospeo_new_leads
          add column if not exists amazon_verdict text,
          add column if not exists amazon_revenue_annual numeric,
          add column if not exists amazon_revenue_source text,
          add column if not exists amazon_reason text
    """)


def qa_companies(conn, leads: list[dict], max_credits: int = 150,
                 budget: dict | None = None) -> dict:
    """Amazon Revenue QA for one accepted batch — the LAST company-level gate
    (after brand_verify; cost-optimal slot measured on batch #44, see
    RAINFOREST_VERIFICATION.html).

    Evaluates each UNIQUE company once (the contact cap means 1 credit covers
    up to 3 leads), under a hard per-run credit budget; budget exhausted ->
    verdict PENDING_CREDITS (finishable later at no extra cost — cached).
    Stamps every lead's amazon_* fields IN PLACE and returns
    {company_lower: result}. SHADOW by design: this function never rejects —
    the caller decides whether DROP verdicts reject (enforce mode) or just
    ride along as columns for Jam (shadow mode)."""
    cur = conn.cursor()
    ensure_cache_schema(cur)
    ensure_lead_columns(cur)
    budget = budget if budget is not None else {"max": max_credits, "spent": 0}
    verdicts: dict[str, dict] = {}
    for lead in leads:
        co = (lead.get("company_name") or "").strip()
        key = co.lower()
        if not key:
            continue
        if key not in verdicts:
            exhausted_before = budget["spent"] >= budget["max"]
            r = evaluate(cur, co, budget=budget)
            if (exhausted_before and r["verdict"] == "REVIEW"
                    and "unavailable" in r["reason"]):
                r["verdict"] = "PENDING_CREDITS"
                r["reason"] = "credit budget reached — decide next run (cached, no re-spend)"
            verdicts[key] = r
        r = verdicts[key]
        lead["amazon_verdict"] = r["verdict"]
        lead["amazon_revenue_annual"] = r["annual_revenue"]
        lead["amazon_revenue_source"] = r["source"]
        lead["amazon_reason"] = (r["reason"] or "")[:500]
    conn.commit()
    return verdicts


def demo(limit: int = 20, use_llm: bool = False):
    conn = connect(); conn.autocommit = True
    cur = conn.cursor()
    ensure_cache_schema(cur)
    rc = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    rc.execute("""
        select distinct coalesce(resolved_company_name, company_name) as company
          from lead_contacts
         where coalesce(resolved_company_name, company_name) is not null
         limit %s
    """, (limit,))
    companies = [r["company"] for r in rc.fetchall()]
    # inject the known false-positive cases to prove they're handled
    companies = ["Blue Apple Co.", "Apple Rubber", "NAF NAF"] + companies
    print(f"Floor ${REVENUE_FLOOR_ANNUAL:,}/yr | grey ${GREY_LOW:,}-${GREY_HIGH:,} | cascade: cache -> SmartScout -> Rainforest floor\n")
    tally = {"KEEP": 0, "DROP": 0, "REVIEW": 0}
    print(f"  {'VERDICT':<7} {'REVENUE/yr':<11} {'MATCH':<6} {'COMPANY':<28} BRAND / reason")
    for co in companies:
        r = evaluate(cur, co, use_llm=use_llm)
        tally[r["verdict"]] += 1
        rev = f"${r['annual_revenue']/1e6:.2f}M" if r["annual_revenue"] and r["annual_revenue"] >= 1e6 else (f"${r['annual_revenue']/1e3:.0f}K" if r["annual_revenue"] else "—")
        info = (r["brand"] or "") if r["verdict"] != "REVIEW" else r["reason"]
        print(f"  {r['verdict']:<7} {rev:<11} {(r['match_method'] or '—'):<6} {(co or '')[:28]:<28} {info}")
    print(f"\n  tally: {tally}")
    conn.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--demo", action="store_true")
    ap.add_argument("--limit", type=int, default=20)
    ap.add_argument("--llm", action="store_true", help="enable Haiku grey-zone brand verification")
    a = ap.parse_args()
    if a.demo:
        demo(limit=a.limit, use_llm=a.llm)
