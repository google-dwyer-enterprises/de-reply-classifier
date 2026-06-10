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
  (Stage 2 site-fetch + LLM lands in Phase 2; until then unresolved domains
   get verdict 'unknown' and pass through flagged, never auto-rejected.)

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
from rapidfuzz import fuzz, process

from smartscout_upload import normalize_brand

PROMPT_VERSION = "bv1"
MODEL = "claude-haiku-4-5"
ARBITRATE_PROMPT = Path(__file__).parent / "prompts" / "brand_verify_vendor_arbitrate.txt"

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
FETCH_TIMEOUT = 12
PROBE_WORKERS = 4          # polite: Shopify CDN throttles bursty IPs
RETRY_429_SLEEP_S = 20

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
    rows = [(d, v["verdict"], v["method"], v.get("confidence"),
             (v.get("evidence") or "")[:2000], v.get("vendor_count"),
             PROMPT_VERSION if "llm" in v["method"] else None)
            for d, v in verdicts.items()
            if v["verdict"] in ("brand", "reseller")
            and not v["method"].startswith("cache:")]
    if not rows:
        return
    with conn.cursor() as cur:
        cur.executemany(
            """insert into domain_brand_verdicts
               (domain, verdict, method, confidence, evidence,
                shopify_vendor_count, prompt_version)
               values (%s,%s,%s,%s,%s,%s,%s)
               on conflict (domain) do update set
                 verdict = excluded.verdict, method = excluded.method,
                 confidence = excluded.confidence, evidence = excluded.evidence,
                 shopify_vendor_count = excluded.shopify_vendor_count,
                 prompt_version = excluded.prompt_version,
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


def _arbitrate_flags(flags: list[dict], on_log) -> None:
    """One Haiku call per probe-flagged domain, judging the vendor list."""
    if not flags:
        return
    import anthropic
    client = anthropic.Anthropic()
    system = ARBITRATE_PROMPT.read_text(encoding="utf-8")
    for entry in flags:
        payload = {"company_name": entry["company"],
                   "domain": entry["domain"],
                   "catalog": entry["flag"]}
        try:
            resp = client.messages.create(
                model=MODEL, max_tokens=200, temperature=0, system=system,
                messages=[{"role": "user", "content": json.dumps(payload)}],
            )
            text = resp.content[0].text.strip()
            text = re.sub(r"^```(json)?|```$", "", text, flags=re.MULTILINE).strip()
            out = json.loads(text)
            label = out.get("label")
            assert label in ("brand", "reseller", "unknown")
        except Exception as e:
            on_log(f"    brand_verify: arbitration failed for "
                   f"{entry['domain']}: {e}")
            label, out = "unknown", {"confidence": "low",
                                     "reason": f"arbitration error: {e}"}
        entry.update(
            verdict=label, method="vendor_llm",
            confidence=out.get("confidence", "low"),
            evidence=f"{out.get('reason', '')} | {entry['flag']}"[:2000],
        )


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

def verify_domains(conn, leads: list[dict], on_log=print) -> dict[str, dict]:
    """Verdict per unique company domain in `leads`.

    Returns {domain: {verdict: brand|reseller|unknown, method, confidence,
    evidence}}. Decisive verdicts are cached in domain_brand_verdicts;
    'unknown' passes through for later stages / human review.
    """
    entries: dict[str, dict] = {}
    for lead in leads:
        dom = norm_domain(lead.get("company_domain"))
        if dom and dom not in entries:
            entries[dom] = {"domain": dom,
                            "company": lead.get("company_name") or ""}
    if not entries:
        return {}

    # Stage 0 — cache.
    cached = _cache_lookup(conn, list(entries))
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

    # Stage 1a — SmartScout confirm on the remainder.
    _smartscout_confirm(conn, fresh)

    # Unresolved -> unknown (Stage 2 in Phase 2; never auto-rejected).
    for e in fresh:
        if "verdict" not in e:
            e.update(verdict="unknown", method="none", confidence=None,
                     evidence=f"unresolved by free layer "
                              f"(probe: {e.get('probe_status', '-')})")

    _cache_write(conn, entries)
    counts = Counter(e["verdict"] for e in entries.values())
    on_log(f"    brand_verify: verdicts {dict(counts)}")
    return entries
