"""Amazon Revenue QA — batch harness + cost/quality report (the July-6 deliverable).

Sweeps N random lead_contacts companies through the full verified cascade
(cache -> SmartScout -> Rainforest floor) with a HARD credit cap, then writes
Victor's report: verdict split, cost per lead, coverage, source breakdown, and
the KEEP hand-review sheet (closes the generic-name edge).

Safety rails (learned the hard way):
  * --dry-run costs ZERO credits: computes brand-match / SmartScout / cache
    coverage and projects exactly how many Rainforest credits a live run needs.
  * Live runs stop calling Rainforest at --max-credits; undecidable leads are
    marked PENDING_CREDITS (never silently dropped) and cost nothing to finish
    later — every Rainforest result is cached (90-day TTL).
  * Results streamed to JSON after every lead — a crash loses nothing.

Usage:
  python amazon_revenue_qa_batch.py --dry-run --limit 1000          # free preview
  python amazon_revenue_qa_batch.py --limit 1000 --max-credits 450  # live (asks y/N)
  python amazon_revenue_qa_batch.py --report-only                   # re-render HTML
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import psycopg2.extras

import amazon_presence as ap
import amazon_revenue_qa as aq
from amazon_brand_match import match_brand
from db import connect
from smartscout_upload import normalize_brand

RESULTS_PATH = Path("debug/amazon_qa_batch_results.json")
REPORT_PATH = Path("docs/scraping/AMAZON_QA_1K_REPORT.html")


def sample_companies(cur, limit: int) -> list[str]:
    """Random-but-reproducible sample of distinct company names."""
    cur.execute("""
        select company from (
            select distinct coalesce(resolved_company_name, company_name) as company
              from lead_contacts
             where coalesce(resolved_company_name, company_name) is not null
        ) t
        order by md5(company)
        limit %s
    """, (limit,))
    return [r[0] for r in cur.fetchall()]


# --------------------------------------------------------------------------- #
# Dry run — zero credits
# --------------------------------------------------------------------------- #
def dry_run(limit: int) -> None:
    conn = connect(); conn.autocommit = True
    cur = conn.cursor()
    aq.ensure_cache_schema(cur)
    companies = sample_companies(cur, limit)
    n = len(companies)
    junk = smartscout = cached = need_rf = 0
    grey = 0
    for co in companies:
        if len(normalize_brand(co or "")) < 3:
            junk += 1
            continue
        m = match_brand(cur, co)
        if m:
            if aq._cache_get(cur, m["brand"]):
                cached += 1
                continue
            rev = aq.smartscout_revenue(cur, m["brand"])
            if rev is not None:
                ann = rev["annual_revenue"]
                if aq.GREY_LOW <= ann < aq.GREY_HIGH:
                    grey += 1
                    need_rf += 1      # grey band escalates to Rainforest
                else:
                    smartscout += 1   # clear keep/drop, free
                continue
        # no match, or match without revenue -> check the company-name cache
        key = normalize_brand(co)
        if aq._cache_get(cur, key) or (m and aq._cache_get(cur, m["brand"])):
            cached += 1
        else:
            need_rf += 1
    conn.close()
    print(f"=== DRY RUN over {n} random leads (0 credits spent) ===")
    print(f"  junk names (free REVIEW)      : {junk}")
    print(f"  SmartScout clear (free)       : {smartscout}")
    print(f"  already cached (free)         : {cached}")
    print(f"  need a Rainforest credit      : {need_rf}   (incl. {grey} SmartScout grey-band confirms)")
    print(f"\n  -> a live run of these {n} needs ~{need_rf} credits")


# --------------------------------------------------------------------------- #
# Live run — hard credit cap
# --------------------------------------------------------------------------- #
def live_run(limit: int, max_credits: int, yes: bool) -> None:
    spent = 0
    real_score = ap.rainforest_score

    def guarded(term):
        nonlocal spent
        if spent >= max_credits:
            return None
        s = real_score(term)
        if s is not None:
            spent += 1
        return s

    ap.rainforest_score = guarded
    aq.rainforest_score = guarded

    if not yes:
        ok = input(f"Live run: up to {max_credits} Rainforest credits on {limit} leads. Proceed? [y/N] ")
        if ok.strip().lower() != "y":
            print("aborted"); return

    conn = connect(); conn.autocommit = True
    cur = conn.cursor()
    aq.ensure_cache_schema(cur)
    companies = sample_companies(cur, limit)

    out: list[dict] = []
    for i, co in enumerate(companies, 1):
        cap_was_hit = spent >= max_credits
        r = aq.evaluate(cur, co)
        # distinguish "cap exhausted" from a real API failure/review
        if cap_was_hit and r["verdict"] == "REVIEW" and "unavailable" in r["reason"]:
            r["verdict"], r["reason"] = "PENDING_CREDITS", "credit cap reached — finish next cycle (cached, costs nothing extra)"
        r["credits_spent_so_far"] = spent
        out.append(r)
        RESULTS_PATH.write_text(json.dumps(out, indent=0, default=str), encoding="utf-8")
        if i % 25 == 0 or i == len(companies):
            print(f"  {i}/{len(companies)} done | credits {spent}/{max_credits}")
    conn.close()
    print(f"\nfinished: {len(out)} leads, {spent} credits")
    render_report(out, max_credits)


# --------------------------------------------------------------------------- #
# Report
# --------------------------------------------------------------------------- #
def render_report(out: list[dict], max_credits: int | None = None) -> None:
    n = len(out) or 1
    tally: dict[str, int] = {}
    srcs: dict[str, int] = {}
    for r in out:
        tally[r["verdict"]] = tally.get(r["verdict"], 0) + 1
        s = (r.get("source") or "none").replace("+cache", "")
        srcs[s] = srcs.get(s, 0) + 1
    spent = max((r.get("credits_spent_so_far") or 0) for r in out) if out else 0
    keeps = [r for r in out if r["verdict"] == "KEEP"]
    reviews = [r for r in out if r["verdict"] in ("REVIEW",)]
    pending = [r for r in out if r["verdict"] == "PENDING_CREDITS"]
    # $ math: Rainforest ~$0.166/credit on the 500-for-$83 tier equivalent
    usd = spent * (83.0 / 500.0)

    def row(r):
        rev = f"${r['annual_revenue']:,.0f}" if r.get("annual_revenue") else "—"
        return (f"<tr><td>{(r['company'] or '')[:48]}</td><td>{r['verdict']}</td>"
                f"<td>{rev}</td><td>{(r.get('source') or '—')}</td>"
                f"<td>{(r.get('reason') or '')[:110]}</td><td></td></tr>")

    html = f"""<!doctype html><html><head><meta charset="utf-8">
<title>Amazon Revenue QA — batch report</title>
<style>
 body{{font:15px/1.5 -apple-system,Segoe UI,Roboto,Arial,sans-serif;color:#1a1d23;max-width:980px;margin:30px auto;padding:0 20px}}
 h1{{font-size:22px;margin:0 0 4px}} h2{{font-size:16px;margin:24px 0 6px;border-bottom:1px solid #e5e7eb;padding-bottom:4px}}
 table{{border-collapse:collapse;width:100%;font-size:13px;margin:8px 0}} th,td{{border:1px solid #e5e7eb;padding:6px 8px;text-align:left}}
 th{{background:#f8fafc}} .big{{font-size:22px;font-weight:800}} .muted{{color:#6b7280;font-size:12.5px}}
 .tiles{{display:flex;gap:12px;flex-wrap:wrap;margin:12px 0}} .tile{{border:1px solid #e5e7eb;border-radius:10px;padding:12px 16px;min-width:130px}}
 .amber{{background:#fffbeb;border:1px solid #fde68a;color:#78350f;border-radius:8px;padding:10px 12px;font-size:13px;margin:12px 0}}
</style></head><body>
<h1>Amazon Revenue QA — batch cost &amp; quality report</h1>
<p class="muted">Cascade: SmartScout first (free) → Rainforest fallback (1 credit) · floor ${aq.REVENUE_FLOOR_ANNUAL:,}/yr · verdicts KEEP / DROP / REVIEW · nothing silently dropped.</p>
<div class="tiles">
 <div class="tile"><div class="big">{n}</div>leads evaluated</div>
 <div class="tile"><div class="big">{tally.get('KEEP',0)}</div>KEEP (in ICP)</div>
 <div class="tile"><div class="big">{tally.get('DROP',0)}</div>DROP (out of ICP)</div>
 <div class="tile"><div class="big">{len(reviews)}</div>REVIEW (human)</div>
 <div class="tile"><div class="big">{len(pending)}</div>pending credits</div>
 <div class="tile"><div class="big">{spent}</div>credits (≈${usd:,.2f})</div>
 <div class="tile"><div class="big">${usd / max(n - len(pending), 1):,.3f}</div>cost / decided lead</div>
</div>
<h2>Sources</h2>
<table><tr><th>Decided by</th><th>Leads</th></tr>
{''.join(f'<tr><td>{k}</td><td>{v}</td></tr>' for k, v in sorted(srcs.items(), key=lambda x: -x[1]))}
</table>
<h2>KEEP hand-review sheet ({len(keeps)} rows — spot-check each, ~30s/row)</h2>
<p class="muted">A KEEP's revenue floor is measured from Amazon's own sales labels and only under-counts — but generic-word company names can in theory match someone else's listings. This sheet closes that edge with a human pass.</p>
<table><tr><th>Company</th><th>Verdict</th><th>Revenue (floor/est.)</th><th>Source</th><th>Reason</th><th>✓ checked</th></tr>
{''.join(row(r) for r in keeps)}
</table>
<h2>REVIEW queue ({len(reviews)} rows)</h2>
<table><tr><th>Company</th><th>Verdict</th><th>Revenue</th><th>Source</th><th>Reason</th><th></th></tr>
{''.join(row(r) for r in reviews)}
</table>
{f'<div class="amber"><b>{len(pending)} leads hit the {max_credits}-credit cap</b> and are marked PENDING_CREDITS — re-running after a plan upgrade finishes ONLY these (all prior results are cached; nothing is re-spent).</div>' if pending else ''}
<h2>Scale economics</h2>
<p>Observed Rainforest need: <b>{spent}</b> credits for <b>{n - len(pending)}</b> decided leads
(≈{(spent / max(n - len(pending), 1)):.2f} credits/lead after SmartScout &amp; cache).
At 20,000 leads/month that projects to ≈<b>{int(20000 * spent / max(n - len(pending), 1)):,} credits/month</b> —
use this to size the Rainforest plan (cache + SmartScout refreshes reduce it over time).</p>
</body></html>"""
    REPORT_PATH.write_text(html, encoding="utf-8")
    print(f"report written: {REPORT_PATH}")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--limit", type=int, default=1000)
    p.add_argument("--max-credits", type=int, default=450)
    p.add_argument("--dry-run", action="store_true", help="ZERO credits: project the credit need")
    p.add_argument("--report-only", action="store_true", help="re-render HTML from saved results")
    p.add_argument("--yes", action="store_true", help="skip the y/N confirm")
    a = p.parse_args()
    if a.report_only:
        render_report(json.loads(RESULTS_PATH.read_text(encoding="utf-8")), a.max_credits)
    elif a.dry_run:
        dry_run(a.limit)
    else:
        live_run(a.limit, a.max_credits, a.yes)
