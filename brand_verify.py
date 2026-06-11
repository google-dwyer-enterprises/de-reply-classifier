"""Reseller detection — per-domain brand-vs-reseller verdicts.

Phase 1 of RESELLER_DETECTION_PLAN.md: the free deterministic layer plus
LLM arbitration of Shopify-probe flags. Called from bettercontact_sync
after the per-company contact cap, before _insert_leads.

Funnel implemented here:
  Stage 0  per-domain dedup + domain_brand_verdicts cache lookup
  Stage 1b Shopify /products.json probe (share rule; runs before SmartScout
           so structural evidence can't be overridden by a name collision)
           - reseller flags are NOT final: they go to a vendor-list LLM
             arbitration (Phase 0 finding: vendor fields contain OEM factory
             names and internal codes that no rule can recognize)
  Stage 1a SmartScout/Amazon confirm (token_sort_ratio + domain corroboration
           + retailer-vocab guard — Phase 0 fixes)
  Stage 2  homepage fetch + signal extraction + one site-LLM verdict
           (prompts/brand_verify.txt, bv1 — Phase 0 measured: 0 false
           reseller flags on the accepted set across two runs).
           Confidence gating is asymmetric: 'reseller' only acts on HIGH
           confidence (a wrong rejection is a paid-for lead lost);
           'brand' acts on high or medium. Everything else -> unknown.
  (Stage 3 web-search fallback lands in Phase 3; until then unresolved
   domains get verdict 'unknown' and pass through flagged, never
   auto-rejected.)

Cache policy: only decisive verdicts (brand/reseller) are written to
domain_brand_verdicts; 'unknown' is re-derivable and caching it would stop
later stages from re-judging.

Fetch policy (Phase 0 finding): Shopify's shared CDN rate-limits per IP, so
probes run with low concurrency and a one-shot retry on 429. A 429 that
persists resolves to 'unknown', never to a verdict.
"""
from __future__ import annotations

import json
import re
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import requests
from bs4 import BeautifulSoup
from rapidfuzz import fuzz, process

from smartscout_upload import normalize_brand

PROMPT_VERSION = "bv3"
MODEL = "claude-haiku-4-5"
ARBITRATE_PROMPT = Path(__file__).parent / "prompts" / "brand_verify_vendor_arbitrate.txt"
SITE_PROMPT = Path(__file__).parent / "prompts" / "brand_verify.txt"
AGENTIC_PROMPT = Path(__file__).parent / "prompts" / "brand_verify_agentic.txt"
OWNERSHIP_PROMPT = Path(__file__).parent / "prompts" / "brand_verify_vendor_ownership.txt"
OWNER_SIZE_PROMPT = Path(__file__).parent / "prompts" / "brand_verify_ownership_size.txt"

# Verdicts that reject a lead (everything else passes through; 'unknown'
# passes flagged for review). Maps verdict -> agency_filter_reason prefix.
# Regression-tuned (2026-06-11): only evidence-documented classes auto-reject.
# no_dtc_store / foreign_no_usca / too_large are REVIEW flags, not rejects —
# the bv2 regression showed they wrongly rejected retail-channel brands (BDI,
# Seachem: real manufacturers selling via dealers), brands with off-site US
# presence (Wild: in all US Target stores, invisible on its UK site), and
# debatable sizes (Nixon). Those resolve to 'unknown' with the reason in the
# evidence so the reviewer decides.
REJECT_VERDICTS = {
    "reseller": "reseller_site",
    "mlm": "mlm_direct_sales",
    "banned_category": "banned_category",
    "out_of_scope": "out_of_scope_category",
    # Policy decisions 2026-06-11:
    # - A foreign brand without real US/CA MARKET PRESENCE (dedicated US
    #   storefront / USD-native site aimed at the US / US retail like
    #   Ulta-Target-Amazon US) is rejected. Merely shipping to the US does
    #   not count. Only ever set via the ownership-search confirmation,
    #   never from the site read alone (Wild's Target presence was
    #   invisible on its UK site).
    "foreign_no_usca": "foreign_no_usca",
    # - A brand owned by a MAJOR corporate parent (public company,
    #   household conglomerate, large brand house: Mars/Henkel/Wella/
    #   Barilla class) is rejected — budget authority sits with corporate,
    #   breaking the founder-led ICP. Small parents (small holdings,
    #   founder/PE-controlled standalone ops) keep the brand verdict.
    "corporate_owned": "corporate_parent",
}

# Anthropic server-side web search tool (Stage 3). ~$10 per 1,000 searches,
# billed on the same API key the worker already uses — no extra credential.
WEB_SEARCH_TOOL = {"type": "web_search_20250305", "name": "web_search",
                   "max_uses": 2}

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
FETCH_TIMEOUT = 12
PROBE_WORKERS = 4          # polite: Shopify CDN throttles bursty IPs
# LLM/search concurrency. Bounded by OUR API tier, not target sites, so it
# can be higher than the fetch pool. Per-entry work is independent; results
# are identical to serial — this is pure wall-clock at 1.7k companies/week.
LLM_WORKERS = 6
RETRY_429_SLEEP_S = 20


def _pmap(fn, items):
    """Run fn over items with the LLM worker pool (order not preserved)."""
    if not items:
        return
    if len(items) == 1:
        fn(items[0])
        return
    with ThreadPoolExecutor(min(LLM_WORKERS, len(items))) as ex:
        list(ex.map(fn, items))

# Same thresholds as smartscout_resolve.py / the Phase 0 diagnostic.
FUZZY_HIGH = 92.0
MIN_BRAND_LEN = 3
MIN_LEN_RATIO = 0.4

# Shopify app/service pseudo-vendors that are not real product makers
# (normalized form: lowercase alnum).
VENDOR_NOISE = {
    "route", "redo", "xcover", "shippingprotection", "shipinsure",
    "navidium", "corso", "seel", "extend", "clydetechnologiesinc",
    "giftcard", "giftcards", "shopifycollective", "savedby",
}

# Stage 2 deterministic features fed to the site-LLM alongside the page text.
RESELLER_PHRASES = [
    "authorized dealer", "authorised dealer", "authorized retailer",
    "official stockist", "official retailer of", "we carry brands",
    "brands we carry", "shop all brands", "shop by brand", "our brands a-z",
    "top brands", "browse brands", "all brands",
]
BRAND_PHRASES = [
    "we make", "we manufacture", "we design", "our formula", "we craft",
    "handcrafted by", "made by us", "we created", "our founder",
    "we developed", "family-owned and operated",
]
SHOP_BY_BRAND_NAV = re.compile(
    r"shop\s+by\s+brand|our\s+brands|brands\s+a\s*-\s*z|all\s+brands|by\s+brand",
    re.IGNORECASE)
# MLM structure language ("ambassador" deliberately excluded — influencer
# programs are not MLMs; the prompt enforces the same distinction).
MLM_PAT = re.compile(
    r"become\s+a\s+consultant|find\s+(your|a)\s+consultant|join\s+(as\s+a\s+)?"
    r"(consultant|distributor)|income\s+disclosure|host\s+rewards|"
    r"downline|direct\s+selling|independent\s+(consultant|distributor)",
    re.IGNORECASE)
# Non-US/CA home-market hints in page text (currency-first; .ca is fine).
GEO_PAT = re.compile(
    r"\.com\.au|\.co\.uk|\.co\.nz|\bAUD\b|\bNZD\b|\bGBP\b|\bEUR\b|\bINR\b|"
    r"£|€|₹|VAT\s+includ", re.IGNORECASE)

# Company names built from retailer vocabulary collide with same-named Amazon
# brands ("Epic Sports"); a name match alone can't prove brand ownership.
RETAILER_NAME_WORDS = {
    "sports", "country", "outlet", "warehouse", "depot", "store", "shop",
    "shoppe", "mart", "emporium", "supply", "supplies", "gear", "equipment",
    "wholesale", "distributing", "distributors", "trading", "imports",
}


def norm_domain(d: str | None) -> str | None:
    if not d:
        return None
    d = d.strip().lower()
    d = re.sub(r"^https?://", "", d)
    d = re.sub(r"^www\.", "", d).split("/")[0].strip()
    return d or None


# ---------------------------------------------------------------------------
# Stage 0 — cache
# ---------------------------------------------------------------------------

def _cache_lookup(conn, domains: list[str]) -> dict[str, dict]:
    if not domains:
        return {}
    with conn.cursor() as cur:
        cur.execute(
            """select domain, verdict, method, confidence, evidence
               from domain_brand_verdicts where domain = any(%s)""",
            (domains,),
        )
        return {r[0]: {"verdict": r[1], "method": f"cache:{r[2]}",
                       "confidence": r[3], "evidence": r[4]}
                for r in cur.fetchall()}


def _cache_write(conn, verdicts: dict[str, dict]) -> None:
    # Cache every decisive verdict (brand + all reject verdicts); 'unknown'
    # is re-derivable and caching it would block later re-judgment.
    rows = [(d, v["verdict"], v["method"], v.get("confidence"),
             (v.get("evidence") or "")[:2000], v.get("vendor_count"),
             PROMPT_VERSION if "llm" in v["method"] or "search" in v["method"]
             else None,
             v.get("parent_company"), v.get("size_estimate"))
            for d, v in verdicts.items()
            if v["verdict"] != "unknown"
            and not v["method"].startswith("cache:")]
    if not rows:
        return
    with conn.cursor() as cur:
        cur.executemany(
            """insert into domain_brand_verdicts
               (domain, verdict, method, confidence, evidence,
                shopify_vendor_count, prompt_version,
                parent_company, size_estimate)
               values (%s,%s,%s,%s,%s,%s,%s,%s,%s)
               on conflict (domain) do update set
                 verdict = excluded.verdict, method = excluded.method,
                 confidence = excluded.confidence, evidence = excluded.evidence,
                 shopify_vendor_count = excluded.shopify_vendor_count,
                 prompt_version = excluded.prompt_version,
                 parent_company = excluded.parent_company,
                 size_estimate = excluded.size_estimate,
                 decided_at = now()""",
            rows,
        )
    conn.commit()


# ---------------------------------------------------------------------------
# Stage 1b — Shopify vendor probe
# ---------------------------------------------------------------------------

def _real_vendors(vendors: Counter, company: str, domain: str) -> list[tuple[str, int]]:
    """Drop app-noise vendors and same-brand variants; return real ones."""
    comp_norm = normalize_brand(company or "")
    dom_norm = normalize_brand(domain.split(".")[0])
    comp_raw = (company or "").lower()
    real = []
    for v, n in vendors.items():
        vn = normalize_brand(v or "")
        if not vn or vn in VENDOR_NOISE:
            continue
        if comp_norm and fuzz.token_set_ratio(vn, comp_norm) >= 80:
            continue
        if dom_norm and fuzz.token_set_ratio(vn, dom_norm) >= 80:
            continue
        # Raw-token comparison merges sub-labels the concatenated norms miss
        # ("Cliff Keen Wrestling" vendor on the Cliff Keen Athletic site).
        if comp_raw and fuzz.token_set_ratio((v or "").lower(), comp_raw) >= 85:
            continue
        real.append((v, n))
    return sorted(real, key=lambda t: -t[1])


def _shopify_probe(entry: dict) -> None:
    """Probe one domain; sets entry['verdict'/'flag'] in place.

    Decisions (share rule, Phase 0):
      <=1 third-party vendor                  -> brand (final)
      >=2 vendors but <30% of products        -> brand (accessory side-shelf)
      >=4 vendors and >=50% of products       -> 'flag' (LLM arbitration)
      otherwise / not Shopify / fetch trouble -> undecided
    """
    dom = entry["domain"]
    for attempt in (1, 2):
        try:
            r = requests.get(f"https://{dom}/products.json?limit=250",
                             headers={"User-Agent": UA}, timeout=FETCH_TIMEOUT)
        except requests.RequestException as e:
            entry["probe_status"] = f"error:{type(e).__name__}"
            return
        if r.status_code == 429 and attempt == 1:
            time.sleep(RETRY_429_SLEEP_S)
            continue
        break
    if r.status_code != 200:
        entry["probe_status"] = f"http_{r.status_code}"
        return
    try:
        products = r.json().get("products")
    except ValueError:
        entry["probe_status"] = "not_json"
        return
    if products is None:
        entry["probe_status"] = "not_shopify"
        return
    if not products:
        entry["probe_status"] = "empty_catalog"
        return

    entry["probe_status"] = "ok"
    vendors = Counter((p.get("vendor") or "").strip() for p in products)
    vendors.pop("", None)
    real = _real_vendors(vendors, entry["company"], dom)
    entry["vendor_count"] = len(real)
    third = sum(n for _, n in real)
    share = third / max(len(products), 1)
    top = ", ".join(f"{v}({n})" for v, n in real[:10])
    detail = (f"Shopify catalog: {len(products)} products, {len(real)} "
              f"third-party vendor(s), {share:.0%} third-party share. {top}")
    if len(real) <= 1:
        entry.update(verdict="brand", method="shopify_probe",
                     confidence="high", evidence=detail)
    elif len(real) >= 2 and share < 0.3:
        entry.update(verdict="brand", method="shopify_probe", confidence="high",
                     evidence="accessory side-shelf, own brand dominates. " + detail)
    elif len(real) >= 4 and share >= 0.5:
        # NOT final — vendor fields can hold OEM factories / internal codes.
        entry["flag"] = detail


_client = None


def _llm_client():
    global _client
    if _client is None:
        import anthropic
        _client = anthropic.Anthropic()
    return _client


def _arbitrate_flags(flags: list[dict], on_log) -> None:
    """One Haiku call per probe-flagged domain, judging the vendor list.

    A 'brand' label is final (the false-flag causes — OEM factory names,
    internal codes — are recognizable from the names alone). A 'reseller'
    label is PROVISIONAL: vendor names can also be the company's own
    sub-brands or category labels (Vetnique's Glandex, MKC's category names —
    found in the 2026-06-10 retro run), which only an ownership lookup can
    settle. Provisional flags go to _confirm_reseller_flags.
    """
    if not flags:
        return
    client = _llm_client()
    system = ARBITRATE_PROMPT.read_text(encoding="utf-8")

    def one(entry):
        payload = {"company_name": entry["company"],
                   "domain": entry["domain"],
                   "catalog": entry["flag"]}
        out = None
        try:
            resp = client.messages.create(
                model=MODEL, max_tokens=200, temperature=0, system=system,
                messages=[{"role": "user", "content": json.dumps(payload)}],
            )
            out = _parse_verdict_json(resp.content[0].text)
        except Exception as e:
            on_log(f"    brand_verify: arbitration failed for "
                   f"{entry['domain']}: {e}")
        if not out:
            out = {"label": "unknown", "confidence": "low",
                   "reason": "arbitration unparseable"}
        label = out["label"]
        reason = out.get("reason") or out.get("evidence_quote") or ""
        if label == "reseller":
            entry["reseller_claim"] = f"{reason} | {entry['flag']}"[:2000]
        else:
            entry.update(
                verdict=label, method="vendor_llm",
                confidence=out.get("confidence", "low"),
                evidence=f"{reason} | {entry['flag']}"[:2000],
            )

    _pmap(one, flags)


def _confirm_reseller_flags(entries: list[dict], on_log) -> None:
    """Web-search ownership check on provisional reseller flags.

    Asks specifically whether the catalog's major vendor names are
    independent third-party brands or the company's own sub-brands/labels.
    Whatever the outcome, the domain is RESOLVED here — undecidable means
    'unknown' (human review), never a pass into later stages where a name
    match or marketing copy could override structural evidence.
    """
    if not entries:
        return
    on_log(f"    brand_verify: confirming {len(entries)} reseller flag(s) "
           f"via vendor-ownership search")
    client = _llm_client()
    system = OWNERSHIP_PROMPT.read_text(encoding="utf-8")

    def one(entry):
        payload = {"company_name": entry["company"],
                   "domain": entry["domain"],
                   "catalog_and_arbitration": entry["reseller_claim"]}
        messages = [{"role": "user",
                     "content": json.dumps(payload, ensure_ascii=False)}]
        out = None
        try:
            for _ in range(2):
                resp = client.messages.create(
                    model=MODEL, max_tokens=800, temperature=0,
                    system=system, tools=[WEB_SEARCH_TOOL], messages=messages,
                )
                if resp.stop_reason == "pause_turn":
                    messages = messages[:1] + [
                        {"role": "assistant", "content": resp.content}]
                    continue
                break
            for b in reversed(resp.content):
                if b.type == "text" and b.text.strip():
                    out = _parse_verdict_json(b.text)
                    break
        except Exception as e:
            on_log(f"    brand_verify: ownership check failed for "
                   f"{entry['domain']}: {e}")
        if not out:
            out = {"label": "unknown", "confidence": "low",
                   "evidence_quote": "ownership check failed"}
        label, conf = out["label"], out.get("confidence", "low")
        evidence = (f"vendor-ownership search: {out.get('evidence_quote', '')} "
                    f"| {entry['reseller_claim']}")[:2000]
        # Hybrid guard (50-lead acceptance audit, babymori.com): a brand whose
        # complementary gift-shelf dominates catalog ITEM COUNT isn't a
        # reseller — own identity can carry the store. Auto-reject only when
        # third-party share is overwhelming (>=75%); the 50-75% band is a
        # genuine hybrid -> review with both pieces of evidence.
        m = re.search(r"(\d+)%\s*third-party share", entry.get("flag", ""))
        share = int(m.group(1)) if m else 100
        if label == "reseller" and conf in ("high", "medium") and share < 75:
            entry.update(verdict="unknown", method="vendor_llm+search",
                         confidence="low",
                         evidence=(f"hybrid: {share}% third-party catalog with "
                                   f"verified independent brands — own brand "
                                   f"may still be primary, review. " + evidence)[:2000])
        elif label == "reseller" and conf in ("high", "medium"):
            entry.update(verdict="reseller", method="vendor_llm+search",
                         confidence=conf, evidence=evidence)
        elif label == "brand" and conf in ("high", "medium"):
            entry.update(verdict="brand", method="vendor_llm+search",
                         confidence=conf, evidence=evidence)
        else:
            entry.update(verdict="unknown", method="vendor_llm+search",
                         confidence="low",
                         evidence=("conflicting: multi-vendor catalog but "
                                   "ownership unclear — review. " + evidence)[:2000])

    _pmap(one, entries)


# ---------------------------------------------------------------------------
# Stage 2 — homepage fetch + signal extraction + site-LLM verdict
# ---------------------------------------------------------------------------

def _fetch_homepage(entry: dict) -> None:
    """Fetch the homepage HTML into entry['_html']; polite 429 handling."""
    dom = entry["domain"]
    last_status = "error:unknown"
    for scheme in ("https", "http"):
        for attempt in (1, 2):
            try:
                r = requests.get(f"{scheme}://{dom}/",
                                 headers={"User-Agent": UA},
                                 timeout=FETCH_TIMEOUT, allow_redirects=True)
            except requests.RequestException as e:
                last_status = f"error:{type(e).__name__}"
                break
            if r.status_code == 429 and attempt == 1:
                time.sleep(RETRY_429_SLEEP_S)
                continue
            last_status = f"http_{r.status_code}"
            if r.status_code == 200 and r.text:
                entry["fetch_status"] = "ok"
                entry["_html"] = r.text[:600_000]
                return
            break
    entry["fetch_status"] = last_status


def _extract_signals(entry: dict) -> None:
    """Build entry['homepage'] + entry['features'] from the fetched HTML."""
    html = entry.pop("_html", None)
    if not html:
        return
    try:
        soup = BeautifulSoup(html, "html.parser")
    except Exception:
        entry["fetch_status"] = "parse_error"
        return
    for tag in soup(["script", "style", "noscript", "svg", "iframe"]):
        tag.decompose()

    title = (soup.title.string or "").strip() if soup.title else ""
    meta = ""
    md = soup.find("meta", attrs={"name": "description"})
    if md:
        meta = (md.get("content") or "").strip()
    nav_texts = []
    for nav in soup.find_all(["nav", "header"]):
        nav_texts += [a.get_text(" ", strip=True) for a in nav.find_all("a")]
    nav_str = " | ".join(t for t in dict.fromkeys(nav_texts) if t)[:1500]
    footer = soup.find("footer")
    footer_str = footer.get_text(" ", strip=True)[:800] if footer else ""
    body = re.sub(r"\s+", " ", soup.get_text(" ", strip=True))[:8000]

    if len(body) < 300:
        entry["fetch_status"] = "empty_body"
        return

    lower_all = f"{nav_str} {body}".lower()
    page_and_desc = f"{nav_str} {footer_str} {body} {entry.get('description') or ''}"
    entry["homepage"] = {"title": title, "meta_description": meta,
                         "nav": nav_str, "footer": footer_str,
                         "body_excerpt": body[:4000]}
    entry["features"] = {
        "shopify_vendor_count": entry.get("vendor_count"),
        "nav_has_shop_by_brand": bool(SHOP_BY_BRAND_NAV.search(nav_str)),
        "reseller_phrase_hits": sum(lower_all.count(p) for p in RESELLER_PHRASES),
        "brand_phrase_hits": sum(lower_all.count(p) for p in BRAND_PHRASES),
        "mlm_signal_hits": len(MLM_PAT.findall(page_and_desc)),
        "geo_signals": sorted(set(m.strip().lower()
                                  for m in GEO_PAT.findall(page_and_desc)))[:6],
    }


def _apply_icp_checks(entry: dict, out: dict, conf: str, evidence: str) -> bool:
    """Apply the bv2 ICP fields (MLM, category, DTC store, US/CA market).

    Returns True if a reject verdict was set. Reject only on HIGH confidence;
    medium/unclear ICP concerns become review notes, never rejections.
    """
    reject_checks = [
        (out.get("is_mlm") == "yes", "mlm", "MLM/direct-sales structure"),
        (out.get("category_status") == "banned", "banned_category",
         f"banned category: {out.get('category', '?')}"),
        (out.get("category_status") == "out_of_scope", "out_of_scope",
         f"out-of-scope category: {out.get('category', '?')}"),
    ]
    # Review-only concerns: real brands legitimately sell via retail channels
    # (no site cart), and US presence can live off-site (Target, Amazon) —
    # the regression proved auto-rejecting these burns good leads.
    review_checks = [
        (out.get("sells_online") == "no", "no_dtc_store",
         "no consumer store on site (catalog/dealer/service only)"),
    ]
    for hit, verdict, why in reject_checks:
        if hit and conf == "high":
            entry.update(verdict=verdict, method="site_llm", confidence=conf,
                         evidence=f"{why}. {evidence}"[:2000])
            return True
        if hit:  # medium/low-confidence concern -> flag for review
            entry["site_llm_note"] = (f"{verdict}?/{conf}: {why}. "
                                      f"{evidence}")[:400]
    for hit, verdict, why in review_checks:
        if hit and conf == "high":
            entry.update(verdict="unknown", method="site_llm",
                         confidence="low",
                         evidence=f"{verdict} — {why} — review. {evidence}"[:2000])
            return True
        if hit:
            entry["site_llm_note"] = (f"{verdict}?/{conf}: {why}. "
                                      f"{evidence}")[:400]
    # Foreign signals with sells_us_ca 'no' OR 'unclear': not a pass, but
    # resolvable — US presence often lives off the main site (us.*
    # storefronts, Ulta, Target; Wild's was invisible on its UK site). Mark
    # for the ownership search to settle: search-confirmed 'no' rejects
    # (policy 2026-06-11: ships-only doesn't count), 'yes' keeps, weaker
    # evidence goes to review.
    if (out.get("hq_foreign_signals") == "yes"
            and out.get("sells_us_ca") in ("no", "unclear")):
        entry["site_llm_note"] = (f"foreign signals, site US/CA sales: "
                                  f"{out.get('sells_us_ca')}. {evidence}")[:400]
        entry["needs_usca_search"] = True
    # Name mismatch is never an auto-reject (can be a parent brand) — review.
    if out.get("name_match") == "mismatch":
        entry["site_llm_note"] = (f"name mismatch: site brand differs from "
                                  f"company/domain. {evidence}")[:400]
        entry["force_review"] = True
    return False


def _site_llm_verdicts(entries: list[dict], on_log) -> None:
    """One Haiku call per fetched homepage, asymmetric confidence gating.

    Entries that already carry a deterministic brand verdict (Shopify probe /
    SmartScout) are ICP-checked only: a high-confidence MLM/category/store/
    market failure overrides the brand confirm, but the site read's own
    brand/reseller label never does — structural catalog evidence wins.
    """
    if not entries:
        return
    client = _llm_client()
    system = SITE_PROMPT.read_text(encoding="utf-8")

    def one(entry):
        payload = {
            "company_name": entry["company"],
            "domain": entry["domain"],
            "third_party_description": (entry.get("description") or "")[:1200],
            "homepage": entry["homepage"],
            "features": entry["features"],
        }
        try:
            resp = client.messages.create(
                model=MODEL, max_tokens=450, temperature=0, system=system,
                messages=[{"role": "user",
                           "content": json.dumps(payload, ensure_ascii=False)}],
            )
            out = _parse_verdict_json(resp.content[0].text)
            assert out, "unparseable verdict"
            label = out["label"]
        except Exception as e:
            on_log(f"    brand_verify: site-LLM failed for {entry['domain']}: {e}")
            return
        conf = out.get("confidence", "low")
        evidence = (f"[{out.get('primary_signal', '?')}] "
                    f"{out.get('evidence_quote', '')}")[:2000]
        entry["icp_out"] = {k: out.get(k) for k in
                            ("category", "category_status", "sells_online",
                             "name_match", "is_mlm", "hq_foreign_signals",
                             "sells_us_ca")}

        had_verdict = "verdict" in entry          # deterministic brand confirm
        if had_verdict:
            prior = (entry["verdict"], entry["method"],
                     entry["confidence"], entry["evidence"])
            del entry["verdict"]
            if _apply_icp_checks(entry, out, conf, evidence):
                return                            # ICP reject overrides
            if entry.get("force_review"):
                # Unresolvable ICP concern (name mismatch) demotes a
                # catalog-confirmed brand to review — the 50-lead acceptance
                # audit caught taippe.com passing because flags were dropped.
                entry.update(verdict="unknown", method=prior[1],
                             confidence="low",
                             evidence=(f"{entry.get('site_llm_note', 'icp concern')} "
                                       f"| catalog: {prior[3]}")[:2000])
                return
            # needs_usca_search rides along: the verdict is tentative until
            # the ownership search confirms US/CA sales.
            entry.update(verdict=prior[0], method=prior[1],
                         confidence=prior[2], evidence=prior[3])
            return

        if _apply_icp_checks(entry, out, conf, evidence):
            return
        # Brand/reseller axis (unchanged from the measured bv1 behavior).
        # A brand verdict with needs_usca_search stays tentative — the
        # ownership search (which runs on all brand verdicts) resolves it.
        if label == "reseller" and conf == "high":
            entry.update(verdict="reseller", method="site_llm",
                         confidence=conf, evidence=evidence)
        elif (label == "brand" and conf in ("high", "medium")
              and not entry.get("force_review")):
            entry.update(verdict="brand", method="site_llm",
                         confidence=conf, evidence=evidence)
        else:
            entry.setdefault("site_llm_note", f"{label}/{conf}: {evidence[:300]}")

    _pmap(one, entries)


# ---------------------------------------------------------------------------
# Stage 3 — web-search fallback (site unreadable or site-LLM unsure)
# ---------------------------------------------------------------------------

def _parse_verdict_json(text: str) -> dict | None:
    """Parse a verdict JSON object, with a regex fallback.

    Haiku occasionally emits an unescaped quote inside evidence_quote, which
    breaks strict JSON parsing (8/54 calls in the 2026-06-10 retro run). The
    enum-valued fields are still trivially extractable, so fall back to
    field-level regex rather than dropping the verdict.
    """
    text = re.sub(r"^```(json)?|```$", "", text.strip(), flags=re.MULTILINE).strip()
    try:
        out = json.loads(text)
        if out.get("label") in ("brand", "reseller", "unknown"):
            return out
    except (json.JSONDecodeError, AttributeError):
        pass
    label = re.search(r'"label"\s*:\s*"(brand|reseller|unknown)"', text)
    if not label:
        return None
    conf = re.search(r'"confidence"\s*:\s*"(high|medium|low)"', text)
    ev = re.search(
        r'"(?:evidence_quote|reason)"\s*:\s*"(.*?)"\s*,?\s*[\r\n]', text, re.S)
    sig = re.search(r'"primary_signal"\s*:\s*"([a-z_]+)"', text)
    return {"label": label.group(1),
            "confidence": conf.group(1) if conf else "low",
            "evidence_quote": ev.group(1)[:1000] if ev else "",
            "reason": ev.group(1)[:1000] if ev else "",
            "primary_signal": sig.group(1) if sig else "?"}


def _agentic_verdicts(entries: list[dict], on_log) -> None:
    """One web-search + verdict call per domain whose site couldn't be judged.

    Judges the company from AROUND its website (LinkedIn, Amazon, press), so
    it works for unreachable/bot-blocked sites. Same asymmetric confidence
    gating as Stage 2.
    """
    if not entries:
        return
    client = _llm_client()
    system = AGENTIC_PROMPT.read_text(encoding="utf-8")

    def one(entry):
        payload = {
            "company_name": entry["company"],
            "domain": entry["domain"],
            "third_party_description": (entry.get("description") or "")[:800],
            "why_escalated": entry.get("site_llm_note")
                or f"site fetch failed: {entry.get('fetch_status', '-')}",
        }
        messages = [{"role": "user",
                     "content": json.dumps(payload, ensure_ascii=False)}]
        out = None
        try:
            # The server runs the search loop; pause_turn means it wants to
            # be re-invoked to continue. One continuation is plenty for a
            # max_uses=2 search budget.
            for _ in range(2):
                resp = client.messages.create(
                    model=MODEL, max_tokens=800, temperature=0,
                    system=system, tools=[WEB_SEARCH_TOOL], messages=messages,
                )
                if resp.stop_reason == "pause_turn":
                    messages = messages[:1] + [
                        {"role": "assistant", "content": resp.content}]
                    continue
                break
            text = next((b.text for b in resp.content
                         if b.type == "text" and b.text.strip()), "")
            # The verdict JSON is in the LAST text block (text blocks before
            # tool use are narration).
            for b in reversed(resp.content):
                if b.type == "text" and b.text.strip():
                    text = b.text
                    break
            out = _parse_verdict_json(text)
        except Exception as e:
            on_log(f"    brand_verify: agentic failed for {entry['domain']}: {e}")
        if not out:
            entry.setdefault(
                "site_llm_note",
                f"agentic: no parseable verdict")
            return
        conf = out.get("confidence", "low")
        evidence = (f"[{out.get('primary_signal', '?')}] "
                    f"{out.get('evidence_quote', '')}")[:2000]
        label = out["label"]
        if label == "reseller" and conf == "high":
            entry.update(verdict="reseller", method="agentic",
                         confidence=conf, evidence=evidence)
        elif label == "brand" and conf in ("high", "medium"):
            entry.update(verdict="brand", method="agentic",
                         confidence=conf, evidence=evidence)
        else:
            entry["site_llm_note"] = f"agentic {label}/{conf}: {evidence[:300]}"

    _pmap(one, entries)


# ---------------------------------------------------------------------------
# Ownership & true-size check (corporate-parent / enterprise detection)
# ---------------------------------------------------------------------------

def _ownership_size_check(entries: list[dict], on_log) -> None:
    """One web search per brand-verdict company: parent ownership + real size.

    Scraped headcount lies (Pura Vida: listed small, ~1,000 employees), and
    ownership isn't in scraped data at all. Policy (asymmetric):
      enterprise + high confidence            -> reject 'too_large'
      subsidiary of a major parent + high     -> 'unknown' (review) — the
        acquired-but-independent line is Victor's policy call, default review
      anything else                           -> pass; parent/size stamped
    """
    if not entries:
        return
    on_log(f"    brand_verify: ownership/size check on {len(entries)} "
           f"brand-verdict domain(s)")
    client = _llm_client()
    system = OWNER_SIZE_PROMPT.read_text(encoding="utf-8")

    def one(entry):
        payload = {"company_name": entry["company"],
                   "domain": entry["domain"],
                   "scraped_description": (entry.get("description") or "")[:400]}
        # Site-read MLM suspicion steers the search: rebranded MLMs hide the
        # structure on-site ("ambassadors") but are documented off-site.
        site_mlm = (entry.get("icp_out") or {}).get("is_mlm")
        if site_mlm in ("unclear", "yes"):
            payload["note"] = ("The website shows possible direct-sales/"
                               "ambassador structure — explicitly search "
                               "whether this company is an MLM.")
        if entry.get("needs_usca_search"):
            payload["check_us_ca_sales"] = (
                "Foreign-based brand, US/CA sales not visible on its main "
                "site — verify whether it sells to US/Canada.")
        messages = [{"role": "user", "content": json.dumps(payload)}]
        out = None
        try:
            for _ in range(2):
                resp = client.messages.create(
                    model=MODEL, max_tokens=600, temperature=0,
                    system=system, tools=[WEB_SEARCH_TOOL], messages=messages)
                if resp.stop_reason == "pause_turn":
                    messages = messages[:1] + [
                        {"role": "assistant", "content": resp.content}]
                    continue
                break
            for b in reversed(resp.content):
                if b.type == "text" and b.text.strip():
                    txt = re.sub(r"^```(json)?|```$", "", b.text.strip(),
                                 flags=re.MULTILINE).strip()
                    try:
                        out = json.loads(txt)
                    except json.JSONDecodeError:
                        ind = re.search(
                            r'"independence"\s*:\s*"(independent|subsidiary|unknown)"', txt)
                        sz = re.search(
                            r'"size_estimate"\s*:\s*"(micro|smb|mid|enterprise|unknown)"', txt)
                        cf = re.search(r'"confidence"\s*:\s*"(high|medium|low)"', txt)
                        pc = re.search(r'"parent_company"\s*:\s*"([^"]{1,80})"', txt)
                        pt = re.search(r'"parent_type"\s*:\s*"(major|minor|unknown)"', txt)
                        bm = re.search(r'"business_model"\s*:\s*"(mlm|standard|unknown)"', txt)
                        us = re.search(r'"sells_us_ca"\s*:\s*"(yes|no|unclear)"', txt)
                        if ind:
                            out = {"independence": ind.group(1),
                                   "size_estimate": sz.group(1) if sz else "unknown",
                                   "confidence": cf.group(1) if cf else "low",
                                   "parent_company": pc.group(1) if pc else None,
                                   "parent_type": pt.group(1) if pt else "unknown",
                                   "business_model": bm.group(1) if bm else "unknown",
                                   "sells_us_ca": us.group(1) if us else "unclear",
                                   "evidence_quote": txt[:250]}
                    break
        except Exception as e:
            on_log(f"    brand_verify: ownership check failed for "
                   f"{entry['domain']}: {e}")
        if not out:
            return                         # check failed -> brand verdict stands
        conf = out.get("confidence", "low")
        parent = out.get("parent_company") or None
        size = out.get("size_estimate", "unknown")
        ev = out.get("evidence_quote", "")
        entry["parent_company"] = parent
        entry["size_estimate"] = size
        if out.get("business_model") == "mlm" and conf in ("high", "medium"):
            # MLMs hide their structure on rebranded sites (Stella & Dot's
            # "ambassadors") but are plainly documented off-site.
            entry.update(verdict="mlm", method="ownership_search",
                         confidence=conf,
                         evidence=f"documented MLM/direct-sales company. {ev}"[:2000])
        elif size == "enterprise" and conf == "high":
            # Review, not reject: size data is third-party and the 5-50 line
            # is a policy call (regression: Nixon flagged enterprise debatably).
            entry.update(verdict="unknown", method="ownership_search",
                         confidence="low",
                         evidence=f"too_large? enterprise-size company — review. {ev}"[:2000])
        elif out.get("independence") == "subsidiary" and conf == "high":
            # Policy 2026-06-11: major corporate parent -> reject (budget
            # authority is corporate; founder-led ICP broken). Small parent
            # -> brand stands, parent stamped. Unknown parent type -> review.
            ptype = out.get("parent_type", "unknown")
            if ptype == "major":
                entry.update(verdict="corporate_owned",
                             method="ownership_search", confidence=conf,
                             evidence=(f"owned by major corporate parent "
                                       f"{parent or '(unnamed)'}. {ev}")[:2000])
            elif ptype == "minor":
                entry["evidence"] = (f"{entry.get('evidence', '')} | owned by "
                                     f"small parent {parent or '(unnamed)'} — "
                                     f"operates independently, kept. {ev}")[:2000]
            else:
                owner = parent or "a corporate parent"
                entry.update(verdict="unknown", method="ownership_search",
                             confidence="low",
                             evidence=(f"owned by {owner} of unclear scale "
                                       f"— review. {ev}")[:2000])
        if entry.get("verdict") == "brand" and entry.get("needs_usca_search"):
            usca = out.get("sells_us_ca", "unclear")
            # Policy 2026-06-11: market presence required; passive shipping
            # is a documented 'no'. High-confidence either way acts;
            # anything weaker goes to review.
            if usca == "yes" and conf == "high":
                entry["evidence"] = (f"{entry.get('evidence', '')} | foreign "
                                     f"HQ but real US/CA market presence "
                                     f"(search-confirmed): {ev}")[:2000]
            elif usca == "no" and conf == "high":
                entry.update(verdict="foreign_no_usca",
                             method="ownership_search", confidence=conf,
                             evidence=(f"foreign brand without US/CA market "
                                       f"presence (ships-only does not "
                                       f"count). {ev}")[:2000])
            else:
                entry.update(verdict="unknown", method="ownership_search",
                             confidence="low",
                             evidence=(f"foreign brand, US/CA market presence "
                                       f"{usca} — review. {ev}")[:2000])

    _pmap(one, entries)


# ---------------------------------------------------------------------------
# Stage 1a — SmartScout / Amazon confirm
# ---------------------------------------------------------------------------

_brand_norms_cache: list[str] | None = None


def _smartscout_confirm(conn, entries: list[dict]) -> None:
    global _brand_norms_cache
    todo = [e for e in entries if "verdict" not in e]
    if not todo:
        return
    if _brand_norms_cache is None:
        with conn.cursor() as cur:
            cur.execute("select brand_norm from smartscout_brands")
            _brand_norms_cache = [r[0] for r in cur.fetchall()
                                  if r[0] and len(r[0]) >= MIN_BRAND_LEN]
    for e in todo:
        norm = normalize_brand(e["company"] or "")
        if not norm or len(norm) < MIN_BRAND_LEN:
            continue
        # token_sort_ratio, not token_set_ratio: set-ratio scores token
        # subsets as 100 ("704 supply" vs "supply") — fine for attaching
        # market data, not for a brand-PASS gate.
        result = process.extractOne(norm, _brand_norms_cache,
                                    scorer=fuzz.token_sort_ratio,
                                    score_cutoff=FUZZY_HIGH)
        if not result:
            continue
        matched, score, _ = result
        if len(matched) / max(len(norm), 1) < MIN_LEN_RATIO:
            continue
        # Retailer-vocabulary names never auto-pass on a name match alone.
        tokens = set(re.findall(r"[a-z]+", (e["company"] or "").lower()))
        if tokens & RETAILER_NAME_WORDS:
            continue
        # The domain must corroborate the matched brand, so a brand named
        # 'Ayla' can't vouch for aylabeauty.com the retailer.
        dom_norm = normalize_brand(e["domain"].split(".")[0])
        if fuzz.token_sort_ratio(dom_norm, matched) < 85 and dom_norm != norm:
            continue
        e.update(verdict="brand", method="smartscout", confidence="high",
                 evidence=f"Amazon-registered brand match: '{matched}' "
                          f"(score {score:.0f})")


# ---------------------------------------------------------------------------
# Public entry
# ---------------------------------------------------------------------------

def verify_domains(conn, leads: list[dict], on_log=print,
                   force: bool = False) -> dict[str, dict]:
    """Verdict per unique company domain in `leads`.

    Returns {domain: {verdict: brand|<reject verdict>|unknown, method,
    confidence, evidence}} — reject verdicts are the REJECT_VERDICTS keys.
    Decisive verdicts are cached in domain_brand_verdicts; 'unknown' passes
    through for later stages / human review. force=True bypasses the cache
    READ (still writes) — used by regression runs and scheduled re-audits.
    """
    entries: dict[str, dict] = {}
    for lead in leads:
        dom = norm_domain(lead.get("company_domain"))
        if dom and dom not in entries:
            entries[dom] = {"domain": dom,
                            "company": lead.get("company_name") or "",
                            "description": lead.get("company_description")}
    if not entries:
        return {}

    # Stage 0 — cache.
    cached = {} if force else _cache_lookup(conn, list(entries))
    for dom, v in cached.items():
        entries[dom].update(v)
    fresh = [e for e in entries.values() if "verdict" not in e]
    on_log(f"    brand_verify: {len(entries)} domains "
           f"({len(cached)} cached, {len(fresh)} to judge)")
    if not fresh:
        return entries

    # Stage 1b — Shopify probe first: structural evidence beats name matches.
    with ThreadPoolExecutor(PROBE_WORKERS) as ex:
        list(ex.map(_shopify_probe, fresh))
    _arbitrate_flags([e for e in fresh if "flag" in e and "verdict" not in e],
                     on_log)
    # Provisional reseller flags get a vendor-ownership search; resolved
    # fully here (reseller / brand / unknown) — they skip the later stages.
    _confirm_reseller_flags(
        [e for e in fresh if "reseller_claim" in e and "verdict" not in e],
        on_log)

    # Stage 1a — SmartScout confirm on the remainder.
    _smartscout_confirm(conn, fresh)

    # Stage 2 — homepage fetch + site-LLM for ALL fresh domains (bv2):
    # undecided ones get the full brand/reseller + ICP judgment; domains the
    # free layer already brand-confirmed still get the ICP checks (MLM,
    # category, DTC store, US/CA market) — a high-confidence ICP failure
    # overrides a catalog confirm, the site's brand/reseller label does not.
    todo = [e for e in fresh
            if e.get("verdict") in (None, "brand") or "verdict" not in e]
    if todo:
        with ThreadPoolExecutor(PROBE_WORKERS) as ex:
            list(ex.map(_fetch_homepage, todo))
        for e in todo:
            _extract_signals(e)
        judgeable = [e for e in todo if e.get("homepage")]
        on_log(f"    brand_verify: stage 2 — {len(judgeable)}/{len(todo)} "
               f"homepages fetched, judging via {MODEL} ({PROMPT_VERSION})")
        _site_llm_verdicts(judgeable, on_log)

    # Stage 3 — web-search fallback for what Stage 2 couldn't judge
    # (fetch-failed sites and low-confidence verdicts). force_review
    # entries are NOT sent here — their open question is an ICP concern
    # the reseller-focused agentic check can't clear.
    todo = [e for e in fresh
            if "verdict" not in e and not e.get("force_review")]
    if todo:
        on_log(f"    brand_verify: stage 3 — web-search fallback for "
               f"{len(todo)} domain(s)")
        _agentic_verdicts(todo, on_log)

    # Ownership & true-size check on every fresh brand verdict (corporate
    # parents and enterprise size are invisible to scraped data and sites).
    _ownership_size_check(
        [e for e in fresh if e.get("verdict") == "brand"], on_log)

    # Still unresolved -> unknown; passes through flagged, never auto-rejected.
    for e in fresh:
        if "verdict" not in e:
            note = e.get("site_llm_note") or (
                f"fetch: {e.get('fetch_status', '-')}, "
                f"probe: {e.get('probe_status', '-')}")
            e.update(verdict="unknown", method="none", confidence=None,
                     evidence=f"unresolved (stages 0-3): {note}"[:1000])

    _cache_write(conn, entries)
    counts = Counter(e["verdict"] for e in entries.values())
    on_log(f"    brand_verify: verdicts {dict(counts)}")
    return entries
