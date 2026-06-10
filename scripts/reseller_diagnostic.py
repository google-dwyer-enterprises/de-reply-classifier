"""Phase 0 reseller-detection diagnostic (RESELLER_DETECTION_PLAN.md §5).

Runs funnel Stages 0-2 READ-ONLY over two populations:

  accepted        — the distinct domains behind currently-accepted BC leads
                    (presumed mostly brands; every 'reseller' verdict here is
                    a flag to hand-check → false-positive side + finds real
                    resellers sitting in the batch)
  known_reseller  — a sample of domains the existing ICP LLM gate rejected as
                    'reseller' (noisy positives; catch-rate side)

Writes NOTHING to the DB. Output: console summary + an evidence XLSX in
exports/ for hand review.

Usage:
  python scripts/reseller_diagnostic.py                  # full run
  python scripts/reseller_diagnostic.py --limit 10 --known-sample 5   # smoke
  python scripts/reseller_diagnostic.py --no-llm         # stages 0-1 only
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from collections import Counter
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from rapidfuzz import fuzz, process

load_dotenv()

from db import connect
from smartscout_upload import normalize_brand

# Same thresholds as smartscout_resolve.py so Stage 1a behaves identically
# to the production fuzzy matcher.
FUZZY_HIGH = 92.0
MIN_BRAND_LEN = 3
MIN_LEN_RATIO = 0.4

PROMPT_PATH = Path(__file__).resolve().parent.parent / "prompts" / "brand_verify.txt"
PROMPT_VERSION = "bv1"
MODEL = "claude-haiku-4-5"

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
FETCH_TIMEOUT = 12
FETCH_WORKERS = 8

# Shopify app/service pseudo-vendors that are not real product makers.
# Matched on normalized form (lowercase alnum).
VENDOR_NOISE = {
    "route", "redo", "xcover", "shippingprotection", "shipinsure",
    "navidium", "corso", "seel", "extend", "clydetechnologiesinc",
    "giftcard", "giftcards", "shopifycollective",
}

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


# ---------------------------------------------------------------------------
# Load populations
# ---------------------------------------------------------------------------

def _norm_domain(d: str | None) -> str | None:
    if not d:
        return None
    d = d.strip().lower()
    d = re.sub(r"^https?://", "", d)
    d = re.sub(r"^www\.", "", d).split("/")[0].strip()
    return d or None


def load_population(conn, where: str, group: str, limit: int | None) -> list[dict]:
    """One entry per distinct domain: name, description, lead count."""
    with conn.cursor() as cur:
        cur.execute(f"""
            select company_domain, company_name, bettercontact_raw,
                   agency_filter_reason
            from prospeo_new_leads
            where provider='bettercontact' and {where}
            order by company_domain, id
        """)
        rows = cur.fetchall()

    by_dom: dict[str, dict] = {}
    for dom_raw, name, raw, gate_reason in rows:
        dom = _norm_domain(dom_raw)
        if not dom:
            continue
        if dom not in by_dom:
            desc = None
            if raw:
                b = raw if isinstance(raw, dict) else json.loads(raw)
                desc = b.get("company_description")
            by_dom[dom] = {"group": group, "domain": dom, "company": name,
                           "description": desc, "n_leads": 0,
                           "gate_reason": gate_reason}
        by_dom[dom]["n_leads"] += 1

    out = sorted(by_dom.values(), key=lambda r: r["domain"])
    return out[:limit] if limit else out


# ---------------------------------------------------------------------------
# Stage 1a — SmartScout / Amazon brand confirm
# ---------------------------------------------------------------------------

def smartscout_confirm(conn, items: list[dict]) -> None:
    with conn.cursor() as cur:
        cur.execute("select brand_norm from smartscout_brands")
        brand_norms = [r[0] for r in cur.fetchall()
                       if r[0] and len(r[0]) >= MIN_BRAND_LEN]
    print(f"Stage 1a: matching against {len(brand_norms):,} SmartScout brands...")
    for it in items:
        norm = normalize_brand(it["company"] or "")
        if not norm or len(norm) < MIN_BRAND_LEN:
            continue
        # token_sort_ratio, NOT the resolver's token_set_ratio: set-ratio
        # scores token SUBSETS as 100 ("704 supply" vs "supply"), which is
        # fine for attaching market data but not for a brand-PASS gate.
        result = process.extractOne(norm, brand_norms,
                                    scorer=fuzz.token_sort_ratio,
                                    score_cutoff=FUZZY_HIGH)
        if not result:
            continue
        matched, score, _ = result
        if len(matched) / max(len(norm), 1) < MIN_LEN_RATIO:
            continue
        it["verdict"] = "brand"
        it["method"] = "smartscout"
        it["confidence"] = "high"
        it["evidence"] = f"Amazon-registered brand match: '{matched}' (score {score:.0f})"


# ---------------------------------------------------------------------------
# Stage 1b — Shopify vendor probe
# ---------------------------------------------------------------------------

def _real_vendors(vendors: Counter, company: str, domain: str) -> list[tuple[str, int]]:
    """Drop app-noise vendors and same-brand variants; return real ones."""
    comp_norm = normalize_brand(company or "")
    dom_norm = normalize_brand(domain.split(".")[0])
    real = []
    for v, n in vendors.items():
        vn = normalize_brand(v or "")
        if not vn or vn in VENDOR_NOISE:
            continue
        if comp_norm and fuzz.token_set_ratio(vn, comp_norm) >= 80:
            continue
        if dom_norm and fuzz.token_set_ratio(vn, dom_norm) >= 80:
            continue
        real.append((v, n))
    return sorted(real, key=lambda t: -t[1])


def shopify_probe_one(it: dict) -> None:
    dom = it["domain"]
    try:
        r = requests.get(f"https://{dom}/products.json?limit=250",
                         headers={"User-Agent": UA}, timeout=FETCH_TIMEOUT)
        if r.status_code != 200:
            it["shopify_status"] = f"http_{r.status_code}"
            return
        products = r.json().get("products")
        if products is None:
            it["shopify_status"] = "not_shopify"
            return
    except requests.RequestException as e:
        it["shopify_status"] = f"error:{type(e).__name__}"
        return
    except ValueError:
        it["shopify_status"] = "not_json"
        return

    it["shopify_status"] = "ok"
    if not products:
        it["shopify_status"] = "empty_catalog"
        return
    vendors = Counter((p.get("vendor") or "").strip() for p in products)
    vendors.pop("", None)
    real = _real_vendors(vendors, it["company"], dom)
    it["vendor_count"] = len(real)
    top = ", ".join(f"{v}({n})" for v, n in real[:10])
    if len(real) <= 1:
        it["verdict"] = "brand"
        it["method"] = "shopify_probe"
        it["confidence"] = "high"
        it["evidence"] = (f"Shopify catalog: {len(products)} products, "
                          f"{len(real)} third-party vendor(s). {top}")
    elif len(real) >= 4:
        it["verdict"] = "reseller"
        it["method"] = "shopify_probe"
        it["confidence"] = "high"
        it["evidence"] = (f"Shopify catalog: {len(real)} distinct third-party "
                          f"vendors: {top}")
    # 2-3 vendors: leave undecided; vendor_count feeds Stage 2.


# ---------------------------------------------------------------------------
# Stage 2 — homepage fetch + signal extraction + LLM verdict
# ---------------------------------------------------------------------------

def fetch_homepage_one(it: dict) -> None:
    dom = it["domain"]
    for scheme in ("https", "http"):
        try:
            r = requests.get(f"{scheme}://{dom}/", headers={"User-Agent": UA},
                             timeout=FETCH_TIMEOUT, allow_redirects=True)
            if r.status_code == 200 and r.text:
                it["fetch_status"] = "ok"
                it["_html"] = r.text[:600_000]
                return
            it["fetch_status"] = f"http_{r.status_code}"
        except requests.RequestException as e:
            it["fetch_status"] = f"error:{type(e).__name__}"
    # fell through both schemes: keep last status


def extract_signals(it: dict) -> None:
    html = it.pop("_html", None)
    if not html:
        return
    try:
        soup = BeautifulSoup(html, "html.parser")
    except Exception:
        it["fetch_status"] = "parse_error"
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
        it["fetch_status"] = "empty_body"
        return

    lower_all = f"{nav_str} {body}".lower()
    it["homepage"] = {"title": title, "meta_description": meta,
                      "nav": nav_str, "footer": footer_str,
                      "body_excerpt": body[:4000]}
    it["features"] = {
        "shopify_vendor_count": it.get("vendor_count"),
        "nav_has_shop_by_brand": bool(SHOP_BY_BRAND_NAV.search(nav_str)),
        "reseller_phrase_hits": sum(lower_all.count(p) for p in RESELLER_PHRASES),
        "brand_phrase_hits": sum(lower_all.count(p) for p in BRAND_PHRASES),
    }


def llm_verdict_one(client, system_blocks, it: dict, usage: Counter) -> None:
    payload = {
        "company_name": it["company"],
        "domain": it["domain"],
        "third_party_description": (it.get("description") or "")[:1200],
        "homepage": it.get("homepage"),
        "features": it.get("features"),
    }
    try:
        resp = client.messages.create(
            model=MODEL, max_tokens=300, temperature=0,
            system=system_blocks,
            messages=[{"role": "user", "content": json.dumps(payload, ensure_ascii=False)}],
        )
    except Exception as e:
        it["verdict"] = "unknown"
        it["method"] = "site_llm"
        it["confidence"] = "low"
        it["evidence"] = f"LLM call failed: {e}"
        return
    usage["in"] += resp.usage.input_tokens
    usage["out"] += resp.usage.output_tokens
    usage["cache_read"] += getattr(resp.usage, "cache_read_input_tokens", 0) or 0
    usage["cache_write"] += getattr(resp.usage, "cache_creation_input_tokens", 0) or 0

    text = resp.content[0].text.strip()
    text = re.sub(r"^```(json)?|```$", "", text, flags=re.MULTILINE).strip()
    try:
        out = json.loads(text)
        label = out.get("label")
        assert label in ("brand", "reseller", "unknown")
    except Exception:
        it["verdict"] = "unknown"
        it["method"] = "site_llm"
        it["confidence"] = "low"
        it["evidence"] = f"unparseable LLM output: {text[:200]}"
        return
    it["verdict"] = label
    it["method"] = "site_llm"
    it["confidence"] = out.get("confidence", "low")
    it["evidence"] = (f"[{out.get('primary_signal', '?')}] "
                      f"{out.get('evidence_quote', '')}")[:500]


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

def write_xlsx(items: list[dict], path: Path) -> None:
    from openpyxl import Workbook
    wb = Workbook()
    ws = wb.active
    ws.title = "verdicts"
    cols = ["group", "domain", "company", "n_leads", "verdict", "method",
            "confidence", "evidence", "vendor_count", "shopify_status",
            "fetch_status", "gate_reason", "description"]
    ws.append(cols)
    for it in sorted(items, key=lambda r: (r["group"],
                                           r.get("verdict") or "zz",
                                           r["domain"])):
        ws.append([str(it.get(c, "") or "")[:1000] if c != "n_leads"
                   else it.get(c, 0) for c in cols])
    for i, w in enumerate([12, 28, 28, 8, 10, 14, 10, 80, 12, 14, 16, 30, 60], 1):
        ws.column_dimensions[chr(64 + i)].width = w
    wb.save(path)


def summarize(items: list[dict], group: str) -> None:
    sub = [it for it in items if it["group"] == group]
    if not sub:
        return
    print(f"\n=== {group} ({len(sub)} domains) ===")
    by = Counter((it.get("verdict") or "undecided", it.get("method") or "-")
                 for it in sub)
    for (v, m), n in sorted(by.items()):
        print(f"  {v:10s} via {m:15s} {n:4d}  ({n / len(sub) * 100:.0f}%)")
    if group == "accepted":
        flags = [it for it in sub if it.get("verdict") == "reseller"]
        print(f"\n  -- reseller flags in ACCEPTED set ({len(flags)}) — hand-check these --")
        for it in flags:
            print(f"  [{it['method']}/{it.get('confidence')}] "
                  f"{it['company']}  {it['domain']}")
            print(f"      {str(it.get('evidence'))[:160]}")
    if group == "known_reseller":
        caught = sum(1 for it in sub if it.get("verdict") == "reseller")
        passed = [it for it in sub if it.get("verdict") == "brand"]
        print(f"\n  catch rate vs existing-gate reseller verdicts: "
              f"{caught}/{len(sub)} = {caught / len(sub) * 100:.0f}%")
        print(f"  'brand' disagreements ({len(passed)}) — hand-check "
              f"(existing gate may be the wrong one):")
        for it in passed[:15]:
            print(f"  [{it['method']}/{it.get('confidence')}] "
                  f"{it['company']}  {it['domain']}")
            print(f"      {str(it.get('evidence'))[:160]}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, help="cap accepted-domain count (smoke test)")
    ap.add_argument("--known-sample", type=int, default=60,
                    help="how many known-reseller domains to include (default 60)")
    ap.add_argument("--no-llm", action="store_true", help="stages 0-1 only")
    args = ap.parse_args()

    conn = connect()
    accepted = load_population(conn, "not rejected", "accepted", args.limit)
    known = load_population(
        conn, "rejected and agency_filter_result = 'reseller'",
        "known_reseller", args.known_sample)
    items = accepted + known
    print(f"populations: accepted={len(accepted)} known_reseller={len(known)}")

    # Stage 1a — SmartScout confirm (only meaningful as PASS; run on both
    # groups so we also measure whether it would wrongly pass a reseller).
    smartscout_confirm(conn, items)
    conn.close()
    n = sum(1 for it in items if it.get("method") == "smartscout")
    print(f"Stage 1a resolved: {n}")

    # Stage 1b — Shopify probe on everything unresolved.
    todo = [it for it in items if "verdict" not in it]
    print(f"Stage 1b: probing {len(todo)} domains for Shopify catalogs...")
    with ThreadPoolExecutor(FETCH_WORKERS) as ex:
        list(ex.map(shopify_probe_one, todo))
    n = sum(1 for it in items if it.get("method") == "shopify_probe")
    print(f"Stage 1b resolved: {n}")

    # Stage 2 — homepage fetch + extract + LLM.
    todo = [it for it in items if "verdict" not in it]
    print(f"Stage 2: fetching {len(todo)} homepages...")
    with ThreadPoolExecutor(FETCH_WORKERS) as ex:
        list(ex.map(fetch_homepage_one, todo))
    for it in todo:
        extract_signals(it)
    fetched = [it for it in todo if it.get("homepage")]
    failed = [it for it in todo if not it.get("homepage")]
    for it in failed:
        it["verdict"] = "unknown"
        it["method"] = "fetch_fail"
        it["confidence"] = "low"
        it["evidence"] = f"homepage fetch failed: {it.get('fetch_status')}"
    print(f"  fetched ok: {len(fetched)}, failed/empty: {len(failed)} "
          f"(failures -> Stage 3 in production)")

    if args.no_llm:
        print("--no-llm: stopping before LLM verdicts")
    else:
        import anthropic
        client = anthropic.Anthropic()
        system_blocks = [{"type": "text",
                          "text": PROMPT_PATH.read_text(encoding="utf-8"),
                          "cache_control": {"type": "ephemeral"}}]
        usage: Counter = Counter()
        print(f"Stage 2 LLM: {len(fetched)} verdicts via {MODEL} "
              f"(prompt {PROMPT_VERSION})...")
        for i, it in enumerate(fetched, 1):
            llm_verdict_one(client, system_blocks, it, usage)
            if i % 25 == 0:
                print(f"  {i}/{len(fetched)}")
        cost = (usage["in"] - usage["cache_read"]) / 1e6 * 1.0 \
            + usage["cache_read"] / 1e6 * 0.10 \
            + usage["cache_write"] / 1e6 * 0.25 \
            + usage["out"] / 1e6 * 5.0
        print(f"  tokens in={usage['in']:,} (cached {usage['cache_read']:,}) "
              f"out={usage['out']:,}  est cost ${cost:.2f}")

    summarize(items, "accepted")
    summarize(items, "known_reseller")

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    out = Path(__file__).resolve().parent.parent / "exports" / f"reseller_diagnostic_{ts}.xlsx"
    write_xlsx(items, out)
    print(f"\nevidence sheet: {out}")


if __name__ == "__main__":
    main()
