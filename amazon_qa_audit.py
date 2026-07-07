"""Amazon Revenue QA — audit + re-score tool.

Victor's ask (2026-07-07 Loom): "do an audit, A-B test it, look at a certain
number of companies each time and cross-reference." He'd found "Scott's Protein
Balls" reported at ~$44M when it makes ~$1k/month — the floor was crediting a
brand with competitors' listings (fixed in amazon_presence._is_branded +
amazon_revenue_qa._is_respelling; commit 5308004).

This tool re-runs Rainforest under the FIXED logic for a set of brands and, for
each, shows EXACTLY which listings were counted toward the floor vs which
competitors were excluded — so the number is auditable against Helium 10. It
also corrects the stale cached value.

  python amazon_qa_audit.py --suspect --dry-run              # 0 credits: list what would run
  python amazon_qa_audit.py --suspect --max-credits 80 --yes # live: re-score, 1 credit/brand (+1 if requery)
  python amazon_qa_audit.py --names "Scott's Protein Balls" "Waggin"   # audit specific names
  python amazon_qa_audit.py --report-only                    # rebuild the HTML from the last run

Writes debug/amazon_qa_audit_results.json + docs/scraping/AMAZON_QA_AUDIT.html.
Budget-gated + y/N confirmed; --dry-run and --report-only spend nothing.
"""
from __future__ import annotations

import argparse
import json
import os

import psycopg2.extras

import amazon_presence as ap
from amazon_revenue_qa import (
    REVENUE_FLOOR_ANNUAL, _cache_put, _is_respelling, ensure_cache_schema,
    floor_verdict,
)
from db import connect
from smartscout_upload import normalize_brand

RESULTS_JSON = "debug/amazon_qa_audit_results.json"
REPORT_HTML = "docs/scraping/AMAZON_QA_AUDIT.html"
SUSPECT_MIN_HITS = 10   # rainforest entries with >= this many branded_hits are the
                        # fix's prime targets (competitor over-attribution)


# --------------------------------------------------------------------------- #
# Data selection
# --------------------------------------------------------------------------- #
def load_suspects(cur) -> list[dict]:
    """Cache entries most likely inflated by the old over-attribution: requery-
    sourced OR many branded_hits. Ordered worst-first (highest revenue)."""
    cur.execute("""
        select brand_norm, annual_revenue::float ar, branded_hits, ratings_total, source
          from brand_revenue_cache
         where source like '%%requery%%'
            or (source like 'rainforest%%' and coalesce(branded_hits,0) >= %s)
         order by annual_revenue desc nulls last
    """, (SUSPECT_MIN_HITS,))
    return [dict(r) for r in cur.fetchall()]


def _results_from_raw(data: dict) -> list[dict]:
    return [{
        "brand": x.get("brand"), "title": x.get("title"),
        "price": x.get("price"), "recent_sales": x.get("recent_sales"),
        "ratings_total": x.get("ratings_total"), "asin": x.get("asin"),
    } for x in (data.get("search_results") or [])]


# --------------------------------------------------------------------------- #
# Re-score one brand under the FIXED logic (mirrors rainforest_floor, but keeps
# the per-listing breakdown for the audit). 1 credit, +1 if a guarded requery.
# --------------------------------------------------------------------------- #
def rescore(search_name: str, budget: dict) -> dict | None:
    if budget["spent"] >= budget["max"]:
        return None
    data = ap._rainforest_raw(search_name)
    if data is None:
        return None
    budget["spent"] += 1
    results = _results_from_raw(data)
    s = ap.score_search_results(search_name, results)
    listings = ap.audit_listings(search_name, results)
    source = "rainforest"

    # spaceless retry (same rule as production rainforest_floor)
    nbn = normalize_brand(search_name)
    spaceless = nbn.replace(" ", "")
    if s["branded_hits"] == 0 and " " in nbn and spaceless and spaceless != nbn and budget["spent"] < budget["max"]:
        data_s = ap._rainforest_raw(spaceless)
        if data_s is not None:
            budget["spent"] += 1
            rs = _results_from_raw(data_s)
            ss = ap.score_search_results(spaceless, rs)
            if ss["branded_hits"] > 0:
                s, listings, source = ss, ap.audit_listings(spaceless, rs), "rainforest_spaceless"

    # guarded canonical-name requery (same rule as production)
    byline = (s.get("top_byline") or "").strip()
    if (s["on_amazon"] and s["revenue_floor_annual"] < REVENUE_FLOOR_ANNUAL
            and byline and byline.lower() != search_name.strip().lower()
            and _is_respelling(search_name, byline)
            and budget["spent"] < budget["max"]):
        data2 = ap._rainforest_raw(byline)
        if data2 is not None:
            budget["spent"] += 1
            r2 = _results_from_raw(data2)
            s2 = ap.score_search_results(byline, r2)
            if (s2["revenue_floor_annual"] > s["revenue_floor_annual"]
                    or s2["branded_hits"] > s["branded_hits"]):
                s, listings, source = s2, ap.audit_listings(byline, r2), "rainforest_requery"

    v, why = floor_verdict({"branded_hits": s["branded_hits"],
                            "annual_revenue": s["revenue_floor_annual"],
                            "ratings_total": s["ratings_total"],
                            "annual_units": s.get("annual_units")})
    return {
        "annual_revenue": s["revenue_floor_annual"], "branded_hits": s["branded_hits"],
        "ratings_total": s["ratings_total"], "annual_units": s.get("annual_units"),
        "on_amazon": s["on_amazon"], "source": source, "verdict": v, "reason": why,
        "matched": [x for x in listings if x["matched"]],
        "excluded": [x for x in listings if not x["matched"] and not x.get("duplicate") and x["byline"]],
        "duplicates": [x for x in listings if x.get("duplicate")],
    }


# --------------------------------------------------------------------------- #
# Report
# --------------------------------------------------------------------------- #
def _fmt(v):
    if v is None:
        return "—"
    return f"${v/1e6:.2f}M" if v >= 1e6 else f"${v/1e3:.0f}K"


def write_report(rows: list[dict]) -> None:
    def esc(s):
        return (str(s or "")).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    def listing_rows(items, cls):
        out = []
        for x in items[:12]:
            out.append(
                f"<tr class='{cls}'><td>{esc(x['byline'] or '—')}</td>"
                f"<td>{esc(x['title'][:70])}</td><td>{x['units']:,}</td>"
                f"<td>${x['price']:,.2f}</td><td>{x['ratings']:,}</td>"
                f"<td>{_fmt(x['monthly_contrib']*12) if x['matched'] else '—'}</td></tr>")
        return "".join(out)

    cards = []
    for r in rows:
        old, new = r.get("old_revenue"), r.get("new_revenue")
        drop = (old and new is not None and new < old)
        delta = f"<span style='color:{'#166534' if drop else '#991b1b'}'>{_fmt(old)} &rarr; {_fmt(new)}</span>"
        cards.append(f"""
        <div class="card">
          <h3>{esc(r['brand'])} &nbsp; {delta} &nbsp;
             <span class="v v-{r.get('new_verdict','')}">{esc(r.get('new_verdict',''))}</span></h3>
          <p class="muted">old: {_fmt(old)} ({r.get('old_hits','?')} hits, {r.get('old_source','?')}) ·
             new: {_fmt(new)} ({r.get('new_hits','?')} hits, {r.get('new_source','?')}) · {esc(r.get('reason',''))}</p>
          <table><tr><th>Byline</th><th>Title</th><th>Units/mo</th><th>Price</th><th>Ratings</th><th>Annual</th></tr>
            {listing_rows(r.get('matched', []), 'ok')}
            {('<tr><td colspan=6 class=muted>Excluded competitors (foreign byline):</td></tr>' + listing_rows(r.get('excluded', []), 'ex')) if r.get('excluded') else ''}
          </table>
        </div>""")

    html = f"""<!doctype html><html><head><meta charset="utf-8">
<title>Amazon Revenue QA — Audit</title><style>
body{{font:14px/1.5 -apple-system,Segoe UI,Roboto,sans-serif;max-width:1000px;margin:28px auto;padding:0 18px;color:#1a1d23}}
h1{{font-size:22px}} h3{{font-size:15px;margin:18px 0 4px}}
.card{{border:1px solid #e5e7eb;border-radius:10px;padding:12px 16px;margin:14px 0}}
table{{border-collapse:collapse;width:100%;font-size:12.5px;margin:6px 0}}
th,td{{border:1px solid #eef1f4;padding:4px 8px;text-align:left}} th{{background:#f8fafc}}
tr.ok td{{background:#f0fdf4}} tr.ex td{{background:#fef2f2;color:#7f1d1d}}
.muted{{color:#6b7280;font-size:12px}}
.v{{font-weight:700;font-size:11px;border-radius:5px;padding:1px 7px}}
.v-KEEP{{background:#dcfce7;color:#166534}} .v-DROP{{background:#fee2e2;color:#991b1b}} .v-REVIEW{{background:#fef3c7;color:#92400e}}
</style></head><body>
<h1>Amazon Revenue QA — Audit &amp; re-score</h1>
<p class="muted">Re-scored under the fixed brand-attribution logic (competitors with a foreign byline
no longer counted; requery guarded). Green rows were counted toward the floor; red rows are excluded competitors.
Floor line = ${REVENUE_FLOOR_ANNUAL:,}/yr. Cross-check the green listings against Helium 10.</p>
<p class="muted">{len(rows)} brands re-scored.</p>
{''.join(cards)}
</body></html>"""
    os.makedirs(os.path.dirname(REPORT_HTML), exist_ok=True)
    with open(REPORT_HTML, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"  report -> {REPORT_HTML}")


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def main() -> None:
    ap_ = argparse.ArgumentParser()
    g = ap_.add_mutually_exclusive_group(required=True)
    g.add_argument("--suspect", action="store_true", help="audit the inflated cache entries")
    g.add_argument("--names", nargs="+", help="audit specific brand/company names")
    g.add_argument("--report-only", action="store_true", help="rebuild HTML from last JSON")
    ap_.add_argument("--limit", type=int, default=None)
    ap_.add_argument("--max-credits", type=int, default=80)
    ap_.add_argument("--dry-run", action="store_true", help="0 credits: list what would run")
    ap_.add_argument("--yes", action="store_true", help="skip the spend confirmation")
    a = ap_.parse_args()

    if a.report_only:
        with open(RESULTS_JSON, encoding="utf-8") as f:
            write_report(json.load(f))
        return

    conn = connect(); conn.autocommit = True
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    ensure_cache_schema(cur)

    if a.suspect:
        suspects = load_suspects(cur)
        if a.limit:
            suspects = suspects[:a.limit]
        targets = [{"name": s["brand_norm"], "old_revenue": s["ar"],
                    "old_hits": s["branded_hits"], "old_source": s["source"]} for s in suspects]
    else:
        names = a.names[:a.limit] if a.limit else a.names
        targets = []
        for n in names:
            cur.execute("select annual_revenue::float ar, branded_hits, source from brand_revenue_cache where brand_norm=%s",
                        (normalize_brand(n),))
            old = cur.fetchone()
            targets.append({"name": n, "old_revenue": (old or {}).get("ar"),
                            "old_hits": (old or {}).get("branded_hits"), "old_source": (old or {}).get("source")})

    print(f"Audit: {len(targets)} brand(s) | floor ${REVENUE_FLOOR_ANNUAL:,} | "
          f"est. cost 1 credit/brand (+1 if requery), cap {a.max_credits}")
    for t in targets[:60]:
        print(f"  {_fmt(t['old_revenue']):>9} ({t['old_hits']} hits, {t['old_source']}) | {t['name']}")

    if a.dry_run:
        print("\n--dry-run: no credits spent, no cache writes.")
        conn.close()
        return
    if not a.yes:
        if input(f"\nSpend up to {a.max_credits} Rainforest credits to re-score these? [y/N] ").strip().lower() != "y":
            print("aborted."); conn.close(); return

    budget = {"max": a.max_credits, "spent": 0}
    rows = []
    for t in targets:
        r = rescore(t["name"], budget)
        if r is None:
            print(f"  SKIP (budget/API): {t['name']}")
            if budget["spent"] >= budget["max"]:
                print("  budget reached — stopping."); break
            continue
        _cache_put(cur, normalize_brand(t["name"]), r)   # correct the stale cache
        rows.append({
            "brand": t["name"], "old_revenue": t["old_revenue"], "old_hits": t["old_hits"],
            "old_source": t["old_source"], "new_revenue": r["annual_revenue"],
            "new_hits": r["branded_hits"], "new_source": r["source"], "new_verdict": r["verdict"],
            "reason": r["reason"], "matched": r["matched"], "excluded": r["excluded"],
        })
        print(f"  {_fmt(t['old_revenue']):>9} -> {_fmt(r['annual_revenue']):>9}  {r['verdict']:6}  {t['name']}  (credits: {budget['spent']})")

    os.makedirs(os.path.dirname(RESULTS_JSON), exist_ok=True)
    with open(RESULTS_JSON, "w", encoding="utf-8") as f:
        json.dump(rows, f, indent=2, default=str)
    write_report(rows)
    print(f"\nDone. {len(rows)} re-scored, {budget['spent']} credits spent. Cache corrected.")
    conn.close()


if __name__ == "__main__":
    main()
