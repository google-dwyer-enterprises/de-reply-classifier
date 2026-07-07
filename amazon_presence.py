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


def _byline_matches(nb: str, byline: str | None) -> bool:
    """Does a PRESENT byline identify brand `nb`? Space-insensitive."""
    rb = normalize_brand(byline or "")
    if not rb:
        return False
    return (rb == nb or rb.replace(" ", "") == nb.replace(" ", "")
            or fuzz.token_sort_ratio(rb, nb) >= BRAND_MATCH_MIN)


def _byline_is_subform(nb: str, rb: str) -> bool:
    """Is byline `rb` a shorter form of OUR brand `nb` (both normalized)? e.g.
    byline 'Scott's' (-> 'scotts') for company 'Scott's Protein Balls'. Brands
    routinely byline themselves with just the core name, so such a listing is
    still ours — unlike a foreign byline ('Orgain') that must be rejected."""
    nbs, rbs = nb.replace(" ", ""), rb.replace(" ", "")
    if not rbs:
        return False
    if nbs.startswith(rbs) or rbs.startswith(nbs):
        return True
    rbt, nbt = set(rb.split()), set(nb.split())
    return bool(rbt) and rbt.issubset(nbt)


def _is_branded(nb: str, byline: str | None, title: str | None) -> bool:
    """Does one search result belong to brand `nb` (normalized)?

    The BYLINE is authoritative when present. A listing whose byline names a
    DIFFERENT, FOREIGN brand is a competitor ranking for the search term — never
    ours, no matter what its title says. This is the fix for the category-term
    over-attribution that credited "Scott's Protein Balls" with other brands'
    listings (e.g. "Protein Balls by Orgain", byline 'Orgain' -> excluded).

    Exceptions where we still trust the title:
      * byline matches our brand (incl. a short sub-form like 'Scott's' for
        'Scott's Protein Balls'), or
      * byline is MISSING (common in Rainforest results) — then fall back to the
        title SPACE-INSENSITIVELY to recover a brand that concatenates its own
        name ('Nano Bebe' -> 'Nanobebe ...'). The spaceless compare is EXACT
        against the title's first-k-words concatenations, so 'Acme' never
        matches 'Acmes'."""
    rb = normalize_brand(byline or "")
    if rb and not _byline_matches(nb, byline) and not _byline_is_subform(nb, rb):
        return False   # present, foreign byline -> competitor, not ours
    if _byline_matches(nb, byline):
        return True
    rt = normalize_brand(title or "")
    nbs = nb.replace(" ", "")
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


def _price_of(x: dict) -> float:
    p = ((x.get("price") or {}).get("value")
         if isinstance(x.get("price"), dict) else x.get("price")) or 0
    return float(p or 0)


def _dedupe_key(x: dict):
    """Identify one product so the same listing appearing twice (sponsored +
    organic placement, a real Rainforest behaviour) is counted once. Prefer the
    ASIN; fall back to (title, price) when ASIN is absent (e.g. the Playwright
    path or hand-built test data — distinct products keep distinct keys)."""
    return x.get("asin") or (x.get("title"), _price_of(x))


def score_search_results(brand: str, results: list[dict]) -> dict:
    """The scorer (provider-agnostic — needs {brand,title,price,recent_sales,
    ratings_total,asin} per result). From one page of search results compute:
      * branded_hits          — UNIQUE listings whose byline/title match the
                                brand (strict, same rule as brand_present),
                                deduped by ASIN so a product listed twice
                                (sponsored + organic) isn't double-counted.
      * revenue_floor_annual  — sum over UNIQUE branded listings of
                                bought-past-month x price x 12. A FLOOR: only
                                listings Amazon labels are counted, so we only
                                ever under-count => a KEEP on it is never a
                                false positive.
      * annual_units          — sum of bought-past-month x 12 over the same
                                listings (sanity signal vs ratings_total).
      * ratings_total         — cumulative ratings on branded listings (size
                                proxy when the sales label is absent).
    """
    nb = normalize_brand(brand)
    hits, monthly_rev, monthly_units, ratings = 0, 0.0, 0, 0
    bylines: dict[str, int] = {}
    seen: set = set()
    for x in results or []:
        if not nb or not _is_branded(nb, x.get("brand"), x.get("title")):
            continue
        key = _dedupe_key(x)
        if key in seen:
            continue   # same product listed twice (sponsored + organic) -> count once
        seen.add(key)
        hits += 1
        units = parse_recent_sales(x.get("recent_sales"))
        price = _price_of(x)
        monthly_rev += units * price
        monthly_units += units
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
        "annual_units": monthly_units * 12,
        "ratings_total": ratings,
        "total_results": len(results or []),
        # Amazon's own spelling of the brand (most common byline among branded
        # hits) — lets the caller re-query with the CANONICAL name when the
        # company's spelling gave a poor result set ("Scents Ational S" vs
        # "ScentSationals", "Anchor Electronics Inc" vs "Anchor").
        "top_byline": max(bylines, key=bylines.get) if bylines else None,
    }


def audit_listings(brand: str, results: list[dict]) -> list[dict]:
    """Transparency for the audit tool: per-listing match decision + revenue
    contribution, so a human can see EXACTLY which listings were counted toward
    the floor (and which competitors were excluded) and cross-check them."""
    nb = normalize_brand(brand)
    rows = []
    seen: set = set()
    for x in results or []:
        matched = bool(nb) and _is_branded(nb, x.get("brand"), x.get("title"))
        # a matched product listed twice counts once (mirror score_search_results);
        # mark the duplicate so the audit report shows why it wasn't summed.
        dup = False
        if matched:
            key = _dedupe_key(x)
            dup = key in seen
            seen.add(key)
        units = parse_recent_sales(x.get("recent_sales"))
        price = _price_of(x)
        rows.append({
            "matched": matched and not dup,
            "duplicate": dup,
            "byline": (x.get("brand") or "").strip() or None,
            "title": (x.get("title") or "").strip(),
            "units": units,
            "price": price,
            "ratings": int(x.get("ratings_total") or 0),
            "monthly_contrib": round(units * price) if (matched and not dup) else 0,
        })
    return rows


def rainforest_score(brand: str) -> dict | None:
    """ONE search call (1 credit) -> presence + revenue floor + ratings.
    None on any API failure (caller -> REVIEW, never a silent verdict)."""
    data = _rainforest_raw(brand)
    if data is None:
        return None
    results = [{
        "brand": x.get("brand"), "title": x.get("title"),
        "price": x.get("price"), "recent_sales": x.get("recent_sales"),
        "ratings_total": x.get("ratings_total"), "asin": x.get("asin"),
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
