"""Amazon presence check — the higher-leverage piece of the Amazon Revenue QA bot.

Purpose (see AMAZON_REVENUE_BOT_PROPOSAL.html): SmartScout only name-matches ~20% of
our leads. For the other ~80%, the decisive question is usually NOT the exact revenue
but simply **is this company even selling on Amazon as a brand?** — which sorts them into:
  * ON AMAZON  -> a real brand; fetch revenue (SmartScout/Keepa) or send to review
  * NOT ON AMAZON -> out of ICP for an Amazon-brand play -> drop/deprioritize

HOW: search Amazon for the brand, look at the organic results, and count listings whose
BRAND BYLINE matches the company (strict, via token_sort_ratio — so "Blue Apple Co."
doesn't get credited to "Apple"). >=1 branded listing => present.

PROVIDERS (cascade, fail-safe to None):
  1. managed API (Rainforest) — PRODUCTION: reliable at scale, handles proxies/CAPTCHA.
     Needs RAINFOREST_API_KEY. (The team already failed at raw Amazon scraping, so
     managed API is the production route.)
  2. Playwright — PROTOTYPE / small-scale only: works for a handful of brands but
     hits Amazon CAPTCHAs at volume. Needs the `playwright` pkg + Chromium + US geo.
None of these is wired to run unattended yet; both fail safe to None (-> REVIEW).
"""
from __future__ import annotations

import os
import re

from rapidfuzz import fuzz

from smartscout_upload import normalize_brand

BRAND_MATCH_MIN = 85   # a result's brand byline must score >= this vs the target brand
AMAZON_DOMAIN = "amazon.com"


def _is_branded(nb: str, byline: str | None, title: str | None) -> bool:
    """Does one search result belong to brand `nb` (normalized)? Strict, but
    SPACE-INSENSITIVE: 'Nano Bebe' must match listings titled 'Nanobebe ...'
    (bylines are often missing, and brands concatenate their own name). Safety:
    the spaceless compare is EXACT against the title's first-k-words
    concatenations, so 'Acme' can never match 'Acmes'."""
    rb = normalize_brand(byline or "")
    rt = normalize_brand(title or "")
    nbs = nb.replace(" ", "")
    if rb:
        rbs = rb.replace(" ", "")
        if rb == nb or rbs == nbs or fuzz.token_sort_ratio(rb, nb) >= BRAND_MATCH_MIN:
            return True
    if rt.startswith(nb + " ") or rt == nb:
        return True
    words = rt.split()
    return any("".join(words[:k]) == nbs for k in range(1, min(len(words), 4) + 1))


def brand_present(brand: str, results: list[dict]) -> dict:
    """Shared heuristic. `results` = [{'brand': <byline>, 'title': <title>}, ...].
    Returns {on_amazon, branded_hits, total_results}."""
    nb = normalize_brand(brand)
    if not nb:
        return {"on_amazon": None, "branded_hits": 0, "total_results": len(results)}
    hits = sum(1 for r in results if _is_branded(nb, r.get("brand"), r.get("title")))
    return {"on_amazon": hits >= 1, "branded_hits": hits, "total_results": len(results)}


# --------------------------------------------------------------------------- #
# Provider 1 — managed API (Rainforest) — PRODUCTION
# --------------------------------------------------------------------------- #
def _rainforest_raw(search_term: str) -> dict | None:
    """One Rainforest search call (1 credit). Returns the raw payload, or None
    on any failure (no key / suspended / quota / network) so callers fail safe."""
    key = os.environ.get("RAINFOREST_API_KEY")
    if not key:
        return None
    try:
        import requests
        r = requests.get("https://api.rainforestapi.com/request", params={
            "api_key": key, "type": "search", "amazon_domain": AMAZON_DOMAIN,
            "search_term": search_term,
        }, timeout=60)
        data = r.json()
        info = data.get("request_info") or {}
        if not info.get("success", True):
            # suspended / bad key / quota -> fail safe, never a fake verdict. Log it
            # to the admin feed + fire a throttled renew-reminder if it's credit.
            msg = str(info.get("message") or "")
            try:
                import api_events
                api_events.record_error("Rainforest", r.status_code, msg, context="rainforest_search")
            except Exception:
                pass
            try:
                import credit_alerts
                credit_alerts.maybe_alert("Rainforest", msg)
            except Exception:
                pass
            return None
        return data
    except Exception as e:
        try:
            import api_events
            api_events.record_error("Rainforest", None, str(e), context="rainforest_search")
        except Exception:
            pass
        return None


def _rainforest_results(brand: str) -> list[dict] | None:
    data = _rainforest_raw(brand)
    if data is None:
        return None
    return [{"brand": x.get("brand"), "title": x.get("title")}
            for x in (data.get("search_results") or [])]


def parse_recent_sales(label: str | None) -> int:
    """'400+ bought in past month' -> 400 · '1K+ bought in past month' -> 1000.
    Conservative: unparseable -> 0, so the revenue floor only ever under-counts."""
    if not label:
        return 0
    m = re.search(r"([\d,.]+)\s*([km]?)\s*\+?\s*bought", label, re.I)
    if not m:
        return 0
    try:
        n = float(m.group(1).replace(",", ""))
    except ValueError:
        return 0
    mult = {"k": 1_000, "m": 1_000_000}.get(m.group(2).lower(), 1)
    return int(n * mult)


def score_search_results(brand: str, results: list[dict]) -> dict:
    """The scorer (provider-agnostic — needs {brand,title,price,recent_sales,
    ratings_total} per result). From one page of search results compute:
      * branded_hits          — listings whose byline/title match the brand
                                (strict, same rule as brand_present)
      * revenue_floor_annual  — sum over branded listings of bought-past-month
                                x price x 12. A FLOOR: only listings Amazon
                                labels are counted, so we only ever under-count
                                => a KEEP made on it is never a false positive.
      * ratings_total         — cumulative ratings on branded listings (size
                                proxy when the sales label is absent).
    """
    nb = normalize_brand(brand)
    hits, monthly_rev, ratings = 0, 0.0, 0
    bylines: dict[str, int] = {}
    for x in results or []:
        if not nb or not _is_branded(nb, x.get("brand"), x.get("title")):
            continue
        hits += 1
        units = parse_recent_sales(x.get("recent_sales"))
        price = ((x.get("price") or {}).get("value")
                 if isinstance(x.get("price"), dict) else x.get("price")) or 0
        monthly_rev += units * float(price or 0)
        ratings += int(x.get("ratings_total") or 0)
        bl = (x.get("brand") or "").strip()
        if not bl:
            # Byline often missing in search results — recover Amazon's spelling
            # of the brand from the matched TITLE prefix instead
            # ("ScentSationals Wax Cubes..." -> "ScentSationals").
            nbs = nb.replace(" ", "")
            norm_words = normalize_brand(x.get("title") or "").split()
            raw_words = (x.get("title") or "").split()
            for k in range(1, min(len(norm_words), 4) + 1):
                if "".join(norm_words[:k]) == nbs and len(raw_words) >= k:
                    bl = " ".join(raw_words[:k]).strip(" ,.-:;|®™")
                    break
        if bl:
            bylines[bl] = bylines.get(bl, 0) + 1
    return {
        "brand": brand,
        "on_amazon": hits >= 1,
        "branded_hits": hits,
        "revenue_floor_annual": round(monthly_rev * 12),
        "ratings_total": ratings,
        "total_results": len(results or []),
        # Amazon's own spelling of the brand (most common byline among branded
        # hits) — lets the caller re-query with the CANONICAL name when the
        # company's spelling gave a poor result set ("Scents Ational S" vs
        # "ScentSationals", "Anchor Electronics Inc" vs "Anchor").
        "top_byline": max(bylines, key=bylines.get) if bylines else None,
    }


def rainforest_score(brand: str) -> dict | None:
    """ONE search call (1 credit) -> presence + revenue floor + ratings.
    None on any API failure (caller -> REVIEW, never a silent verdict)."""
    data = _rainforest_raw(brand)
    if data is None:
        return None
    results = [{
        "brand": x.get("brand"), "title": x.get("title"),
        "price": x.get("price"), "recent_sales": x.get("recent_sales"),
        "ratings_total": x.get("ratings_total"),
    } for x in (data.get("search_results") or [])]
    s = score_search_results(brand, results)
    s["source"] = "rainforest"
    s["credits_remaining"] = (data.get("request_info") or {}).get("credits_remaining")
    return s


# --------------------------------------------------------------------------- #
# Provider 2 — Playwright — PROTOTYPE / small-scale
# --------------------------------------------------------------------------- #
def _playwright_results(brand: str) -> list[dict] | None:
    try:
        from playwright.sync_api import sync_playwright
    except Exception:
        return None  # pkg not installed -> caller falls through
    try:
        with sync_playwright() as p:
            b = p.chromium.launch(headless=True)
            pg = b.new_page()
            pg.goto(f"https://www.{AMAZON_DOMAIN}/s?k={brand.replace(' ', '+')}", timeout=45000)
            if "captcha" in pg.content().lower() or "not a robot" in pg.content().lower():
                b.close(); return None  # blocked -> caller falls through / REVIEW
            rows = pg.eval_on_selector_all(
                "div[data-component-type='s-search-result']",
                """els => els.slice(0,20).map(e => ({
                    brand: (e.querySelector('h2 .a-size-base-plus, .a-row .a-size-base')||{}).innerText || '',
                    title: (e.querySelector('h2 span')||{}).innerText || ''
                }))""")
            b.close()
            return rows
    except Exception:
        return None


def check_presence(brand: str) -> dict:
    """Cascade: managed API -> Playwright -> unknown. Returns a verdict dict."""
    results = _rainforest_results(brand)
    src = "rainforest"
    if results is None:
        results = _playwright_results(brand)
        src = "playwright"
    if results is None:
        return {"brand": brand, "on_amazon": None, "source": None,
                "reason": "no presence provider available -> REVIEW"}
    v = brand_present(brand, results)
    v.update(brand=brand, source=src)
    return v


if __name__ == "__main__":
    import sys
    print(check_presence(sys.argv[1] if len(sys.argv) > 1 else "OLAPLEX"))
