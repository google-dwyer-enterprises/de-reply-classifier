"""BetterContact category-mode lead scraper.

Pulls decision-maker leads from BetterContact's Lead Finder API (parallel to
prospeo_sync.py but for the BetterContact provider). Writes accepted leads
into the existing `prospeo_new_leads` table with `provider='bettercontact'`,
so downstream exports / dedup / NocoDB views keep working unchanged.

CLI:
  python run.py scrape-leads --provider bettercontact --mode category \
    --target-leads N --max-credits M [--country "United States,Canada"] \
    [--skip-industries "..."] [--dry-run]

API shape (verified 2026-06-01 via debug/bettercontact_verify_all.py):

  POST https://app.bettercontact.rocks/api/v2/lead_finder/async   (submit)
  GET  https://app.bettercontact.rocks/api/v2/lead_finder/async/{id}   (poll)
  Header: X-API-Key: ${BETTERCONTACT_API_KEY}

  Submit body:
  {
    "filters": {
      "company_industry": {"include": ["Cosmetics"]},
      "lead_location": {"include": ["United States","Canada"]},
      "lead_seniority": {"include": ["owner","founder","cxo"]},
      "company_headcount_min": 5
    },
    "limit": 200,
    "offset": 0,
    "enrich_email_address": true
  }

  Pricing (best-fit model across all measured runs, 2026-06-12): 1 credit per
  FOUND email (deliverable AND catch-all — catch-alls are billed but dropped
  at parse), 10 credits per delivered phone, ~0.1 credit only for returned
  leads with no billable email; nothing-found slots are free. NB: BC's docs
  claim a flat 0.1/slot search fee, but 44 logged production pages contradict
  it (max observed 0.92 cr/slot; integer credit totals reconcile exactly as
  found-email counts). Billing-semantics support ticket still open — credit
  reservations carry 10% headroom for the worse model until BC confirms.

Quality gates (BETTERCONTACT_LEAD_QUALITY_PLAN.md):
  - P1: "Alternative Medicine" industry dropped from the shared industry list.
  - P2: prohibited-category blocklist (cannabis/alcohol/firearms) via
        rule_classify, matched on name + domain + keywords + description.
  - P3: LLM brand/agency/reseller/marketplace gate on rule-passed leads —
        only "brand" (sells its own product) is kept. Mirrors Prospeo.
  - P4 (pending): no usable revenue/size floor. BetterContact returns no
        revenue and its size fields are 0% populated; `company_headcount_min=5`
        is the only server-side proxy. SmartScout can't be a hard floor (it
        only covers Amazon sellers, so a non-match ≠ too small).
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Iterable

import anthropic
import requests
from dotenv import load_dotenv
from langdetect import DetectorFactory, LangDetectException, detect_langs

# langdetect is non-deterministic by default; pin the seed so the same
# description always yields the same verdict (reproducible filtering).
DetectorFactory.seed = 0

# Reuse Prospeo's exporter + industries list + shared rule/LLM helpers.
# Industries are identical (verified — every Prospeo enum string resolves in
# BetterContact's enum). Decision-maker title gating is BC-specific now
# (bc_title_rank); the LLM gate uses the BC-specific BC_ICP_PROMPT, not
# Prospeo's agency_filter.txt.
from prospeo_sync import (
    PROSPEO_INDUSTRIES as BC_INDUSTRIES,
    rule_classify,           # prohibited-category + service + agency + marketplace rule
    llm_classify_batch,      # LLM gate (driven here by BC_ICP_PROMPT)
    write_csv,
    write_xlsx,
)
from db import connect
import brand_verify
import amazon_revenue_qa

# Amazon Revenue QA (Rainforest) — SHADOW mode: verdicts are stamped on every
# accepted lead (amazon_verdict / amazon_revenue_annual / ...) but do NOT
# reject until AMAZON_QA_ENFORCE is flipped. Cost-optimal slot = last
# company-level gate, after brand_verify (measured on batch #44 — see
# docs/scraping/RAINFOREST_VERIFICATION.html). Hard per-RUN credit budget.
# Env-toggleable (default OFF/shadow): set AMAZON_QA_ENFORCE=true on the worker
# to auto-drop DROP-verdict leads — no code change/redeploy needed to flip.
AMAZON_QA_ENFORCE = (os.environ.get("AMAZON_QA_ENFORCE", "").strip().lower()
                     in ("1", "true", "yes", "on"))
AMAZON_QA_MAX_CREDITS = 150


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

BC_BASE = "https://app.bettercontact.rocks/api/v2"

# BC-specific ICP brand-gate prompt (manufacturer/private-label only, excluded
# categories, no blogs). Separate from Prospeo's shared agency_filter.txt so
# Prospeo's gate is unaffected.
BC_ICP_PROMPT = Path(__file__).parent / "prompts" / "bettercontact_icp_filter.txt"

# Title strategy (revised after 2026-06-01 smoke tests):
#   - `lead_seniority` enum is multi-lingual + overloaded (returns Stockholders,
#     salon-owners, Cargill "data product owners", "Arbeidsgiver" etc.)
#   - `lead_job_title` substring match is fuzzy/semantic (matches "VP marketing"
#     when querying "President" — BC treats them as related)
#   - "President" in the include list draws VPs we have to throw away
# Net: substring-match the decision-maker titles, pay for the VP/junior noise
# BC slips through, post-filter (bc_title_rank) drops it.
BC_TITLE_KEYWORDS = [
    "CEO", "Founder", "Owner", "President",
    "CMO", "Marketing Manager", "Head of Marketing",
    "Ecommerce Director", "Ecommerce Manager",
]

# Cost reseq R1 (probe-verified 2026-06-11: the exclude param demonstrably
# changes the result set server-side). BC matches include-keywords loosely —
# "President" matches "Vice President of Sales" — so these excludes stop us
# being billed for title classes our local bc_title_rank rejects anyway.
# EXCLUDE-only by design: an include-allowlist could silently drop valid
# decision-makers; an exclude-list of known-bad titles cannot lose a lead
# the local filter would have kept (it stays as the backstop).
BC_TITLE_EXCLUDE = [
    "Vice President of Sales", "VP of Sales", "SVP of Sales",
    "Vice President of Finance", "VP of Finance", "VP Finance",
    "Vice President of Operations", "VP of Operations",
    "VP of Business Development", "Vice President of Business Development",
    "Sales Manager", "Account Manager", "Account Executive",
    "Human Resources", "HR Manager", "Recruiter",
    # Observed in the 6/11 A/B run's paid-then-rejected titles:
    "VP of Product", "Vice President of Product", "Project Manager",
]

# Headcount min as revenue-floor proxy. Verified: 5 and 10 are the same
# bracket in BC's data; 20 cuts ~44%. 5 excludes only solo-founder shops.
BC_HEADCOUNT_MIN = 5

# Server-side MAX headcount. CRITICAL cost lever — verified 2026-06-09 via
# debug/_probe_headcount.py: with only headcount_min set, BC's default ordering
# surfaces the BIGGEST companies first (offset 0 of Cosmetics = 100% 5k-10k-
# employee firms), so we were paying to enrich enterprise emails and discarding
# them all via the client-side ≤100 filter. `company_headcount_max` IS honored
# (max=50 → only 11-50 buckets, leads_found 15,127→7,618). Setting it to 50
# matches our effective ICP (the 51-200 bucket was already rejected client-side)
# and stops us paying for oversized leads at the source. enrich-free probe = 0
# credits. NB: changing this changes BC's result ordering, so the saved
# bettercontact_scrape_state offsets must be reset when you flip it on.
BC_HEADCOUNT_MAX = 50

# ICP: company must be 1-100 employees. BC returns LinkedIn-style size BUCKETS
# (company_employees_range_start/_end), not exact counts. The clean sub-100
# buckets are 1-10 and 11-50; 51-200 straddles 100 so STRICT mode rejects it.
# A lead passes only if its range_end is a number ≤ 100. Kept as a backstop even
# with BC_HEADCOUNT_MAX server-side (defends against bucket-edge surprises).
BC_MAX_HEADCOUNT = 100

# Decision-maker titles in PRIORITY order (best first). Used both to gate (a
# lead's title must match a tier) and to rank which contacts to keep under the
# per-company cap. Owner-level outranks marketing, which outranks e-commerce.
BC_TITLE_TIERS: list[tuple[str, tuple[str, ...]]] = [
    ("owner", ("chief executive", "ceo", "founder", "co-founder", "cofounder",
               "owner", "co-owner", "president", "managing director", "principal")),
    ("cmo", ("cmo", "chief marketing")),
    ("marketing", ("head of marketing", "vp marketing", "vp of marketing",
                   "marketing director", "director of marketing", "marketing manager",
                   "head of growth", "growth manager")),
    ("ecom", ("head of e-commerce", "head of ecommerce", "director of e-commerce",
              "director of ecommerce", "ecommerce director", "e-commerce director",
              "vp e-commerce", "vp of ecommerce", "ecommerce manager",
              "e-commerce manager", "chief e-commerce", "chief digital",
              "digital director", "head of digital")),
]

# Per-company contact cap (keep the 3 highest-priority decision-makers).
BC_MAX_CONTACTS_PER_COMPANY = 3

# Generic / role-based mailbox local-parts — we want a real person's work email
# (victor@brand.com), never a shared inbox (info@brand.com).
BC_GENERIC_EMAIL_PREFIXES = frozenset({
    "info", "sales", "support", "hello", "contact", "admin", "team", "office",
    "orders", "order", "help", "service", "services", "marketing", "careers",
    "jobs", "press", "media", "newsletter", "noreply", "no-reply", "donotreply",
    "mail", "email", "enquiries", "enquiry", "inquiries", "inquiry", "general",
    "accounts", "accounting", "billing", "finance", "hr", "legal", "privacy",
    "webmaster", "postmaster", "shop", "store", "wholesale", "customerservice",
    "customercare", "care", "hi", "hey", "ask", "connect", "reception",
})

# Deterministic out-of-scope categories matched on name + keywords (HIGH
# PRECISION only — phrases that almost never appear in a real product brand).
# The broad/contextual ones (apparel, food, grocery, electronics, toys,
# software, education) are left to the BC ICP LLM gate, which has full context.
# Merged from the BetterContact spec + the team's documented exclusions
# (real estate, insurance, education, orgs, books, toys, software).
BC_EXCLUDED_CATEGORY_TOKENS: dict[str, tuple[str, ...]] = {
    # NB: apparel/food/grocery/electronics/toys are ALLOWED (valid e-commerce
    # products). Excluded: the dangerous blocklist (cannabis/alcohol/firearms via
    # prohibited_category, + adult here), books/publishing, and non-product
    # categories (real estate/insurance, + software/education/memberships via
    # the LLM gate).
    "adult": ("sex toy", "sex toys", "adult toy", "adult toys", "dildo",
              "vibrator", "lingerie", "adult novelty", "pleasure product",
              "pleasure products", "bdsm"),
    "books": ("bookstore", "book store", "book publisher", "publishing house",
              "publisher of books"),
    "real_estate": ("real estate", "realty", "realtor", "realtors",
                    "property management", "brokerage firm"),
    "insurance": ("insurance agency", "insurance broker", "insurance brokerage",
                  "insurance company", "life insurance", "health insurance"),
}

# Domain / TLD filter: the ICP is US + Canada Amazon e-commerce brands, so we
# keep US/global-neutral commerce TLDs plus Canada (.ca) and drop other foreign
# ccTLDs (.au, .co.uk, .de, …) and malformed domains. The final dot-segment is
# the registrable TLD, so "brand.com.au" → "au" (dropped) while "brand.ca" →
# "ca" (kept). Tunable: add a TLD here if a legit brand is wrongly dropped.
BC_ALLOWED_TLDS: frozenset[str] = frozenset({
    "com", "co", "net", "us", "ca", "shop", "store",
})

# Min description length before we trust langdetect, and min probability before
# we treat a non-English verdict as real. Short/ambiguous text → keep (don't
# over-reject). Verified against 610 accepted: 5 non-English (fr/it), 0 false
# positives, 0 detect errors.
BC_LANG_MIN_CHARS = 40
BC_LANG_MIN_PROB = 0.85


def _local_part(email: str | None) -> str:
    return (email or "").split("@", 1)[0].lower().strip()


def is_generic_email(email: str | None) -> bool:
    """True for shared/role mailboxes (info@, sales@…), False for personal."""
    local = _local_part(email)
    if not local:
        return True
    base = local.split("+", 1)[0]
    return base in BC_GENERIC_EMAIL_PREFIXES


def bc_size_ok(bc: dict) -> bool:
    """True if the company is <= BC_MAX_HEADCOUNT employees (strict: range_end
    must be a number <= 100, which keeps only the 1-10 and 11-50 buckets)."""
    try:
        end = int(bc.get("company_employees_range_end"))
    except (TypeError, ValueError):
        return False  # open-ended (10001+) or unknown → can't confirm ≤100
    return end <= BC_MAX_HEADCOUNT


def bc_language_ok(bc: dict) -> bool:
    """True unless the company_description is confidently non-English. BC returns
    the description in the site's own language, so this is our proxy for site
    language (Victor's 'different language' rule). Short/ambiguous/undetectable
    text → True (don't over-reject)."""
    desc = (bc.get("company_description") or "").strip()
    if len(desc) < BC_LANG_MIN_CHARS:
        return True
    try:
        langs = detect_langs(desc)
    except LangDetectException:
        return True
    if not langs:
        return True
    top = langs[0]
    return not (top.lang != "en" and top.prob >= BC_LANG_MIN_PROB)


def bc_title_rank(title: str | None) -> int | None:
    """Return the priority tier index (0 = best) if the title is a target
    decision-maker, else None."""
    t = (title or "").lower()
    if not t:
        return None
    for i, (_name, kws) in enumerate(BC_TITLE_TIERS):
        if any(kw in t for kw in kws):
            return i
    return None


def bc_excluded_category(name: str | None, keywords) -> str | None:
    """Deterministic out-of-scope category match (high-precision). Returns the
    category if matched, else None. Broad categories go to the LLM gate."""
    kw = " ".join(str(k) for k in keywords) if isinstance(keywords, (list, tuple)) else (keywords or "")
    blob = f"{name or ''} {kw}".lower()
    for cat, toks in BC_EXCLUDED_CATEGORY_TOKENS.items():
        if any(t in blob for t in toks):
            return cat
    return None


def bc_domain_ok(domain: str | None) -> bool:
    """True if the domain is a US/global-neutral commerce TLD (BC_ALLOWED_TLDS)
    and well-formed. Drops foreign ccTLDs (.au, .co.uk, .ca…) and malformed
    domains. The final dot-segment is the TLD checked."""
    d = (domain or "").strip().lower()
    # strip any scheme / path / www that slipped through
    for pre in ("https://", "http://"):
        if d.startswith(pre):
            d = d[len(pre):]
    if d.startswith("www."):
        d = d[4:]
    d = d.split("/", 1)[0].split("?", 1)[0]
    if not d or "." not in d or " " in d:
        return False
    return d.rsplit(".", 1)[1] in BC_ALLOWED_TLDS

BC_PAGE_LIMIT = 200            # API max per submit
WORKER_PAGE_LIMIT = 200        # worker's per-page cap (= BC API max; see effective_page_limit)
BC_REQUEST_TIMEOUT_S = 30      # HTTP timeout for submit + poll calls
BC_POLL_INTERVAL_S = 5         # how often to poll for terminate status
BC_POLL_TIMEOUT_S = 600        # give up on a request after this long. Bumped
                                # from 180s after batch 1 (2026-06-02) — most
                                # >180s waits did complete and BC was still
                                # billing for them.
BC_SUBMIT_MAX_RETRIES = 5      # network-level retries on submit
BC_POLL_MAX_RETRIES = 5        # network-level retries on each poll GET
# Enrichment resilience (2026-07-10): BC's async enrich is intermittently slow —
# a batch poll can hang the full 600s (killed batch #47; stalled #48/#49). So the
# revenue-first enrich runs in small chunks with a SHORT per-attempt timeout and
# retries: a hang costs one chunk-attempt (~90s) and re-submits, instead of a
# 10-min dead wait, and one bad chunk doesn't lose the others.
BC_ENRICH_POLL_TIMEOUT_S = 90  # per-attempt enrich poll timeout (vs 600 for search)
BC_ENRICH_CHUNK = 5            # enrich survivors this many at a time
BC_ENRICH_ATTEMPTS = 3         # re-submit a chunk this many times on timeout


class InsufficientCreditsError(Exception):
    """Raised when BetterContact rejects with a no-credit error."""


# ---------------------------------------------------------------------------
# Budget math — shared by the worker's page-sizing and the submit-form
# validation so the two can never drift. A run aborts before scraping anything
# if max_credits can't cover even one page's worst-case reservation.
# ---------------------------------------------------------------------------

def effective_page_limit(requested_leads: int) -> int:
    """Page size the worker will use for a given target (mirrors run_scrape)."""
    return max(10, min(WORKER_PAGE_LIMIT, requested_leads * 5))


def reserve_for_page(page_limit: int, enrichment: str = "email") -> int:
    """Worst-case credit reservation for one page. Phones bill ~10 cr each on
    top of 1/email, so 'both' reserves ~11.1x vs ~1.1x for email."""
    factor_tenths = 111 if enrichment in ("both", "phone") else 11
    return (page_limit * factor_tenths + 9) // 10


def min_credits_for(requested_leads: int, enrichment: str = "email") -> int:
    """Smallest max_credits that won't abort before scraping anything.
    For 'both' the worker shrinks the page down to a floor of 5 to fit a small
    budget, so its floor is reserve(5); email runs the full target-scaled page."""
    page_limit = 5 if enrichment in ("both", "phone") else effective_page_limit(requested_leads)
    return reserve_for_page(page_limit, enrichment)


# ---------------------------------------------------------------------------
# Low-level API calls
# ---------------------------------------------------------------------------

def _submit_search(filters: dict, limit: int, offset: int, api_key: str,
                   enrich_phones: bool = False,
                   ambiguous_attempts: list | None = None,
                   enrich_email_address: bool = True) -> str:
    """POST a Lead Finder search; returns the async request_id.

    enrich_email_address (default True = classic behavior): set False for
    EMAIL-FREE discovery — probe-verified 2026-07-08 that BC returns the person
    + full company firmographics with `credits_consumed: 0.0` when email
    enrichment is off. This is the discovery step of the revenue-first flow
    (enrich only survivors via the standalone /api/v2/async endpoint).

    enrich_phones (probe-verified 2026-06-12): the Lead Finder accepts
    enrich_phone_number and bills 10 credits per delivered phone on top of
    1 per email — callers must scale their credit reservations accordingly.

    ambiguous_attempts: caller-owned list. Every retried failure where BC may
    have ACCEPTED the POST before the transport died (read-timeout, reset,
    gateway 5xx) appends one entry — each such attempt may have created a
    server-side search that bills but whose request_id we never saw. The
    caller must book those against the budget the same way poll timeouts are
    booked (BC demonstrably keeps billing for requests we abandon — see
    BC_POLL_TIMEOUT_S note). Attempts that provably never reached BC
    (connect-timeout, 429 rejection) are not counted.
    """
    body = {
        "filters": filters,
        "limit": limit,
        "offset": offset,
        "enrich_email_address": bool(enrich_email_address),
        "enrich_phone_number": bool(enrich_phones),
    }
    headers = {"X-API-Key": api_key, "Content-Type": "application/json"}
    url = f"{BC_BASE}/lead_finder/async"

    last_err = None
    for attempt in range(BC_SUBMIT_MAX_RETRIES):
        try:
            r = requests.post(url, json=body, headers=headers,
                              timeout=BC_REQUEST_TIMEOUT_S)
        except requests.exceptions.ConnectTimeout as e:
            # Never connected — BC cannot have created the search.
            last_err = e
            time.sleep(2 * (attempt + 1))
            continue
        except (requests.exceptions.ConnectionError,
                requests.exceptions.Timeout,
                requests.exceptions.ChunkedEncodingError) as e:
            # POST may have landed before the transport died — the search
            # could exist and bill server-side. Record it for booking.
            if ambiguous_attempts is not None:
                ambiguous_attempts.append(type(e).__name__)
            last_err = e
            time.sleep(2 * (attempt + 1))
            continue

        if r.status_code in (200, 201, 202):
            data = r.json() or {}
            rid = data.get("request_id") or data.get("id")
            if not rid:
                raise RuntimeError(f"BC submit returned no request_id: {r.text[:200]}")
            return rid

        # Permanent errors — surface them
        if r.status_code == 401:
            raise RuntimeError(f"BC submit 401 (bad API key): {r.text[:200]}")
        if r.status_code == 402:
            # Anecdote: docs don't name a code for insufficient credits, but 402
            # is the standard HTTP semantic.
            raise InsufficientCreditsError(
                f"BC reports insufficient credits: {r.text[:200]}"
            )
        if r.status_code == 400:
            raise RuntimeError(f"BC submit 400 (bad filters?): {r.text[:300]}")

        # 5xx — a gateway error can mask an accepted request; count it.
        # 429 means BC rejected the request outright — free, not counted.
        if r.status_code >= 500 and ambiguous_attempts is not None:
            ambiguous_attempts.append(f"http_{r.status_code}")
        last_err = RuntimeError(f"BC submit {r.status_code}: {r.text[:200]}")
        time.sleep(2 * (attempt + 1))

    raise RuntimeError(f"BC submit exhausted {BC_SUBMIT_MAX_RETRIES} retries: {last_err}")


def _poll_for_result(request_id: str, api_key: str,
                      timeout_s: int = BC_POLL_TIMEOUT_S) -> dict:
    """Poll GET /lead_finder/async/{id} until terminated; return the result."""
    headers = {"X-API-Key": api_key}
    url = f"{BC_BASE}/lead_finder/async/{request_id}"
    start = time.time()
    fail_streak = 0

    while time.time() - start < timeout_s:
        time.sleep(BC_POLL_INTERVAL_S)
        try:
            r = requests.get(url, headers=headers, timeout=BC_REQUEST_TIMEOUT_S)
        except (requests.exceptions.ConnectionError,
                requests.exceptions.Timeout,
                requests.exceptions.ChunkedEncodingError):
            fail_streak += 1
            if fail_streak >= BC_POLL_MAX_RETRIES:
                raise RuntimeError(f"BC poll {request_id}: transport failures")
            continue
        fail_streak = 0

        if r.status_code != 200 and r.status_code != 202:
            # 406 / 401 / 5xx — log and continue (transient)
            continue

        data = r.json() or {}
        status = (data.get("status") or "").lower()
        if status in ("terminated", "completed", "done", "finished"):
            return data

    raise RuntimeError(f"BC poll {request_id} timeout after {timeout_s}s")


def enrich_contacts(leads: list[dict], api_key: str,
                    enrich_phones: bool = False,
                    timeout_s: int = BC_POLL_TIMEOUT_S) -> dict | None:
    """Standalone enrichment (POST /api/v2/async): name+company -> email.

    The "enrich only survivors" step of the revenue-first flow. Bills 1 credit
    per FOUND email (0 if not found). `leads` = dicts with first_name/last_name
    + company and/or company_domain. Returns the BC result dict (keys
    `data` [rows with contact_email_address + contact_email_address_status],
    `credits_consumed`, ...) or None on failure.

    Round-trip probe-verified 2026-07-08: submit -> {id}; poll GET /async/{id}
    until terminated; result carries data[] + credits_consumed (1 for a found
    email). Distinct from _submit_search, which is the Lead Finder SEARCH."""
    if not leads:
        return {"data": [], "credits_consumed": 0}
    payload = [{
        "first_name": l.get("first_name") or l.get("contact_first_name"),
        "last_name": l.get("last_name") or l.get("contact_last_name"),
        "company": l.get("company_name") or l.get("company"),
        "company_domain": l.get("company_domain"),
        "linkedin_url": l.get("contact_linkedin_profile_url") or l.get("linkedin_url"),
    } for l in leads]
    headers = {"X-API-Key": api_key, "Content-Type": "application/json"}
    body = {"data": payload, "enrich_email_address": True,
            "enrich_phone_number": bool(enrich_phones)}
    r = requests.post(f"{BC_BASE}/async", json=body, headers=headers,
                      timeout=BC_REQUEST_TIMEOUT_S)
    if r.status_code == 402:
        raise InsufficientCreditsError(f"BC enrich insufficient credits: {r.text[:200]}")
    if r.status_code not in (200, 201, 202):
        raise RuntimeError(f"BC enrich submit {r.status_code}: {r.text[:200]}")
    eid = (r.json() or {}).get("id")
    if not eid:
        raise RuntimeError(f"BC enrich returned no id: {r.text[:200]}")
    url = f"{BC_BASE}/async/{eid}"
    start = time.time()
    while time.time() - start < timeout_s:
        time.sleep(BC_POLL_INTERVAL_S)
        try:
            g = requests.get(url, headers={"X-API-Key": api_key},
                             timeout=BC_REQUEST_TIMEOUT_S)
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout,
                requests.exceptions.ChunkedEncodingError):
            continue
        if g.status_code not in (200, 202):
            continue
        d = g.json() or {}
        if (d.get("status") or "").lower() in ("terminated", "completed", "done", "finished"):
            return d
    raise RuntimeError(f"BC enrich poll {eid} timeout after {timeout_s}s")


def enrich_contacts_resilient(leads: list[dict], api_key: str,
                              enrich_phones: bool = False) -> dict | None:
    """Chunked, short-timeout, retrying enrich (see BC_ENRICH_* constants).

    BetterContact's async enrich is intermittently slow. Enriching all survivors
    in one request means a single hung poll wastes 10 min AND loses the whole
    page. This runs small chunks with a short per-attempt timeout and re-submits
    on timeout, so a hang costs ~90s and is retried, one bad chunk doesn't lose
    the others, and results are aggregated. Propagates InsufficientCreditsError;
    raises RuntimeError only if EVERY chunk failed (BC unhealthy) so the caller's
    abort guard fires."""
    if not leads:
        return {"data": [], "credits_consumed": 0}
    data: list[dict] = []
    credits = 0.0
    chunks = [leads[i:i + BC_ENRICH_CHUNK]
              for i in range(0, len(leads), BC_ENRICH_CHUNK)]
    failed = 0
    for ci, part in enumerate(chunks, 1):
        ok = False
        for attempt in range(1, BC_ENRICH_ATTEMPTS + 1):
            try:
                r = enrich_contacts(part, api_key, enrich_phones=enrich_phones,
                                    timeout_s=BC_ENRICH_POLL_TIMEOUT_S)
                if r:
                    data.extend(r.get("data") or [])
                    credits += float(r.get("credits_consumed") or 0)
                ok = True
                break
            except InsufficientCreditsError:
                raise
            except RuntimeError as e:
                if attempt < BC_ENRICH_ATTEMPTS:
                    print(f"      enrich chunk {ci}/{len(chunks)} attempt {attempt} "
                          f"failed ({str(e)[:50]}) — retrying")
                else:
                    print(f"      enrich chunk {ci}/{len(chunks)} gave up after "
                          f"{BC_ENRICH_ATTEMPTS} attempts")
        if not ok:
            failed += 1
    if failed and failed == len(chunks):
        raise RuntimeError(f"all {failed} enrich chunk(s) timed out — BC enrichment unhealthy")
    return {"data": data, "credits_consumed": credits}


def _industry_filters(industry: str, countries: list[str] | None,
                      exclude_domains: list[str] | None = None) -> dict:
    filters: dict = {
        "company_industry": {"include": [industry]},
        "lead_job_title": {"include": BC_TITLE_KEYWORDS,
                           "exclude": BC_TITLE_EXCLUDE},
        "company_headcount_min": BC_HEADCOUNT_MIN,
        "company_headcount_max": BC_HEADCOUNT_MAX,
    }
    if countries:
        filters["lead_location"] = {"include": list(countries)}
    if exclude_domains:
        # Cost reseq R2 (probe-verified incl. positive control: an excluded
        # domain present in baseline results disappears; 500-entry lists
        # accepted): suppress companies we'd reject or cap anyway, BEFORE
        # BetterContact bills us for their emails.
        filters["company"] = {"exclude": exclude_domains[:500]}
    return filters


def _build_suppression_list(conn, limit: int = 500) -> list[str]:
    """Company domains to exclude from Lead Finder searches (cost reseq R2).

    Only company-LEVEL exclusions — never domains rejected for contact-level
    reasons (a bad title on one contact must not block the company's other,
    possibly valid, contacts):
      1. companies already at the contact cap (their emails are pure dedup
         waste — the single biggest measured waste class, 22-57% of paid
         rejects), newest first;
      2. companies rejected for company-level reasons (reseller / MLM /
         service / prohibited / corporate / banned category).
    """
    with conn.cursor() as cur:
        cur.execute(r"""
          with d as (
            select lower(regexp_replace(company_domain,'^www\.','')) as dom,
                   bool_or(not rejected) as has_accepted,
                   count(*) filter (where not rejected) as n_accepted,
                   max(scraped_at) as latest,
                   bool_or(rejected and (
                     agency_filter_reason ~ '^(reseller|mlm_|banned_|out_of_scope|too_large|corporate_|prohibited)'
                     or agency_filter_reason like 'QA: prohibited%%'
                     or agency_filter_reason like 'QA: service%%'
                     or agency_filter_reason like 'LLM: reseller%%'
                     or agency_filter_reason like 'LLM: service%%'
                     or agency_filter_reason like 'LLM: agency%%'
                     or agency_filter_reason like 'LLM: marketplace%%'
                   )) as company_level_reject
            from prospeo_new_leads
            where provider = 'bettercontact' and company_domain is not null
            group by 1
          )
          select dom from d
          where n_accepted >= %s or (company_level_reject and not has_accepted)
          order by latest desc
          limit %s
        """, (BC_MAX_CONTACTS_PER_COMPANY, limit))
        return [r[0] for r in cur.fetchall()]


def _search_industry(industry: str, countries: list[str] | None,
                      offset: int, limit: int, api_key: str) -> dict:
    """One full submit+poll cycle for one industry page (serial version)."""
    request_id = _submit_search(_industry_filters(industry, countries),
                                 limit, offset, api_key)
    return _poll_for_result(request_id, api_key)


def _poll_many(request_ids: list[str], api_key: str,
                timeout_s: int = BC_POLL_TIMEOUT_S) -> dict[str, dict]:
    """Concurrently poll many in-flight BC request_ids until all terminate
    (or timeout). Returns {request_id: result_dict}. Request_ids that never
    terminate are absent from the returned dict — caller must reconcile.

    Strategy: single-threaded round-robin polling (each GET is ~50-100ms)
    rather than a thread pool. BC's per-IP rate-limit makes massive
    parallelism a 429-magnet; sequential GETs every 5s is plenty for
    real-world wait times of 30-300s.
    """
    headers = {"X-API-Key": api_key}
    pending = set(request_ids)
    results: dict[str, dict] = {}
    start = time.time()
    fail_streak = 0

    while pending and time.time() - start < timeout_s:
        time.sleep(BC_POLL_INTERVAL_S)
        done = []
        for rid in list(pending):
            url = f"{BC_BASE}/lead_finder/async/{rid}"
            try:
                r = requests.get(url, headers=headers,
                                 timeout=BC_REQUEST_TIMEOUT_S)
            except (requests.exceptions.ConnectionError,
                    requests.exceptions.Timeout,
                    requests.exceptions.ChunkedEncodingError):
                fail_streak += 1
                continue
            fail_streak = 0
            if r.status_code != 200 and r.status_code != 202:
                continue
            data = r.json() or {}
            status = (data.get("status") or "").lower()
            if status in ("terminated", "completed", "done", "finished"):
                results[rid] = data
                done.append(rid)
        for rid in done:
            pending.discard(rid)

    return results


# ---------------------------------------------------------------------------
# Lead parsing + acceptance
# ---------------------------------------------------------------------------

def _parse_bc_lead(bc: dict, industry: str) -> dict | None:
    """Convert one BC lead JSON into a prospeo_new_leads row dict.

    Returns None if the lead is unusable (no email, missing critical fields).
    """
    email = (bc.get("contact_email_address") or "").lower().strip()
    if not email:
        return None
    email_status = bc.get("contact_email_address_status")
    if email_status not in ("deliverable",):
        # Skip undeliverable / catch_all_not_safe / unknown.
        return None

    # BC populates `company_domain` but leaves `company_website` null. Synthesize
    # the URL form so Jam's CSV column matches the Prospeo-sourced rows.
    domain = bc.get("company_domain")
    website = bc.get("company_website") or (f"https://{domain}" if domain else None)
    return {
        "email": email,
        "mobile": bc.get("contact_phone_number"),
        "first_name": bc.get("contact_first_name"),
        "last_name": bc.get("contact_last_name"),
        "title": bc.get("contact_job_title"),
        "company_name": bc.get("company_name"),
        "company_website": website,
        "company_domain": domain,
        # Surfaced for the prohibited-category blocklist (rule_classify reads
        # these). BetterContact returns rich keywords + description that catch
        # coy cannabis brands whose names give nothing away. Not DB columns —
        # _insert_leads / write_csv pick a fixed column set and ignore extras.
        "company_description": bc.get("company_description"),
        "company_keywords": bc.get("company_keywords") or [],
        # Size bucket (range_start/_end) drives the ≤100-employee ICP filter.
        "company_size_start": bc.get("company_employees_range_start"),
        "company_size_end": bc.get("company_employees_range_end"),
        "source_domain": domain,
        "source_industry": industry,
        "scrape_mode": "category",
        "provider": "bettercontact",
        "mobile_status": "verified" if bc.get("contact_phone_number") else None,
        "agency_filter_result": "accepted",   # we trust BC's filter + our post-checks
        "agency_filter_method": "bettercontact_title_substring",
        "agency_filter_reason": None,
        "bettercontact_raw": bc,
    }


def _parse_bc_person(bc: dict, industry: str) -> dict | None:
    """Email-FREE variant of _parse_bc_lead for the revenue-first flow.

    Same row shape but WITHOUT requiring an email (discovery ran with
    enrich_email_address=false, so contact_email_address is empty). Returns a
    lead dict with email=None for the company/ICP/revenue gates to run on; the
    email is filled later by enrich_contacts for survivors only. Returns None
    only if the person/company is structurally unusable (no name or no domain)."""
    domain = bc.get("company_domain")
    first = bc.get("contact_first_name")
    last = bc.get("contact_last_name")
    if not domain or not (first or last):
        return None
    website = bc.get("company_website") or (f"https://{domain}" if domain else None)
    return {
        "email": None,
        "mobile": None,
        "first_name": first,
        "last_name": last,
        "title": bc.get("contact_job_title"),
        "company_name": bc.get("company_name"),
        "company_website": website,
        "company_domain": domain,
        "company_description": bc.get("company_description"),
        "company_keywords": bc.get("company_keywords") or [],
        "company_size_start": bc.get("company_employees_range_start"),
        "company_size_end": bc.get("company_employees_range_end"),
        "source_domain": domain,
        "source_industry": industry,
        "scrape_mode": "category",
        "provider": "bettercontact",
        "mobile_status": None,
        "contact_linkedin_profile_url": bc.get("contact_linkedin_profile_url"),
        "agency_filter_result": "accepted",
        "agency_filter_method": "bettercontact_title_substring",
        "agency_filter_reason": None,
        "bettercontact_raw": bc,
    }


def _post_filter(lead: dict) -> tuple[bool, str | None]:
    """Deterministic ICP gates on top of BC's filtering (the LLM brand/category
    gate runs afterward in _run_category). Order is cheapest-reject-first.

    Returns (accept, reject_reason).
    """
    # Prohibited (cannabis/alcohol/firearms) + service + agency + marketplace
    rule_result = rule_classify(lead)
    if rule_result is not None:
        result, _method, reason = rule_result
        return False, f"{result}:{reason}"

    # Out-of-scope category (adult/books/real-estate/insurance — deterministic)
    cat = bc_excluded_category(lead.get("company_name"), lead.get("company_keywords"))
    if cat:
        return False, f"excluded_category:{cat}"

    # Domain must be a US/global-neutral commerce TLD (drop .au/.co.uk/etc.)
    if not bc_domain_ok(lead.get("company_domain")):
        return False, f"bad_domain:{lead.get('company_domain')}"

    # Company must be <= 100 employees (1-10 / 11-50 buckets only)
    raw = lead.get("bettercontact_raw") or {
        "company_employees_range_end": lead.get("company_size_end")}
    if not bc_size_ok(raw):
        return False, f"size_over_100:{lead.get('company_size_start')}-{lead.get('company_size_end')}"

    # Site must be English (Victor's 'different language' rule) — detected from
    # BC's native-language company_description. NB: foreign companies whose BC
    # description is in English (e.g. a UK/AU brand) are intentionally KEPT — the
    # rule is language, not geography (decision 2026-06-09).
    if not bc_language_ok(raw):
        return False, "non_english_site"

    # Work email only — no shared/role mailboxes (info@, sales@…).
    # Guard on presence: the revenue-first flow gates BEFORE enrichment, so
    # `email` is None here — an empty email must NOT read as "generic" (that
    # wrongly rejected ~84% in the first validation). The check is re-applied on
    # the real email post-enrichment in _run_category_revenue_first; the classic
    # path always has an email so its behavior is unchanged.
    if lead.get("email") and is_generic_email(lead.get("email")):
        return False, f"generic_email:{_local_part(lead.get('email'))}"

    # Must have a company name
    if not (lead.get("company_name") or "").strip():
        return False, "no_company_name"

    # Title must be a target decision-maker (owner / CMO / marketing / e-com)
    if bc_title_rank(lead.get("title")) is None:
        return False, f"title_not_decision_maker:{(lead.get('title') or '')[:60]}"

    return True, None


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

def _load_existing_emails(conn) -> set[str]:
    """Pull all emails already in prospeo_new_leads (any provider) for dedup."""
    with conn.cursor() as cur:
        cur.execute("select email from prospeo_new_leads")
        return {r[0] for r in cur.fetchall() if r[0]}


def _load_company_counts(conn) -> dict[str, int]:
    """How many accepted BetterContact contacts each company domain already has,
    to enforce the per-company cap across runs."""
    with conn.cursor() as cur:
        cur.execute(
            "select lower(company_domain), count(*) from prospeo_new_leads "
            "where provider='bettercontact' and not rejected "
            "and company_domain is not null group by 1"
        )
        return {d: c for d, c in cur.fetchall() if d}


def _load_state(conn) -> dict[str, dict]:
    """Read bettercontact_scrape_state into a dict keyed by industry."""
    state: dict[str, dict] = {}
    with conn.cursor() as cur:
        cur.execute("""
          select industry, last_offset_consumed, total_leads_estimated,
                 exhausted, total_credits_spent, parked_at
          from bettercontact_scrape_state
        """)
        for ind, lo, tle, ex, cr, parked in cur.fetchall():
            state[ind] = {
                "last_offset_consumed": lo or 0,
                "total_leads_estimated": tle,
                "exhausted": bool(ex),
                "total_credits_spent": float(cr or 0),
                "parked_at": parked,
            }
    return state


def _update_state(conn, industry: str, *, new_offset: int,
                  leads_found: int | None, credits_spent_delta: float,
                  exhausted: bool, countries: list[str] | None,
                  accepted_delta: int = 0) -> None:
    """Upsert per-industry state.

    Cost reseq R3 (segment health): a segment that burns >=10 credits on a
    call yielding 0 accepted leads twice IN A ROW gets parked (parked_at
    set). Parked segments are skipped at queue-build for 30 days, then
    auto-retried (BC's inventory refreshes). One good call resets the
    counter. This is what actually caused the 11.6-credits/accepted run —
    exhausted segments, not deep offsets (measured: healthy segments were
    flat 3.3-5.1 cr/accepted at any depth).
    """
    bad_call = accepted_delta == 0 and credits_spent_delta >= 10
    with conn.cursor() as cur:
        cur.execute("""
          insert into bettercontact_scrape_state
            (industry, countries, last_offset_consumed, total_leads_estimated,
             exhausted, last_scraped_at, total_credits_spent,
             consecutive_zero_yield)
          values (%s, %s, %s, %s, %s, now(), %s, %s)
          on conflict (industry) do update set
            countries = excluded.countries,
            last_offset_consumed = excluded.last_offset_consumed,
            total_leads_estimated = coalesce(excluded.total_leads_estimated,
                                              bettercontact_scrape_state.total_leads_estimated),
            exhausted = excluded.exhausted,
            last_scraped_at = now(),
            total_credits_spent =
              bettercontact_scrape_state.total_credits_spent + %s,
            consecutive_zero_yield = case when %s
              then bettercontact_scrape_state.consecutive_zero_yield + 1
              else 0 end,
            parked_at = case
              when %s and bettercontact_scrape_state.consecutive_zero_yield + 1 >= 2
              then now()
              when not %s then null
              else bettercontact_scrape_state.parked_at end
        """, (
            industry, countries or [], new_offset, leads_found, exhausted,
            credits_spent_delta, 1 if bad_call else 0,
            credits_spent_delta, bad_call, bad_call, bad_call,
        ))


def _insert_leads(conn, leads: list[dict],
                   scrape_request_id: int | None = None) -> int:
    """Bulk-insert leads (accepted or rejected). Caller sets each row's
    `rejected` field explicitly. When `scrape_request_id` is set, every row
    is tagged so the lead-scrape-automation worker can later move just the
    rows from a specific request into `lead_contacts`.

    For worker-tagged rows, `lead_approval` is seeded:
      - BC-accepted (rejected=false) -> 'pending' (Jam reviews in NocoDB)
      - BC-auto-rejected (rejected=true) -> 'rejected' (no manual review)
    Non-worker rows (CLI) leave lead_approval NULL, which the worker's
    move query excludes by definition.

    Returns rows actually inserted."""
    if not leads:
        return 0
    cols = ["email", "first_name", "last_name", "title", "company_name",
            "company_domain", "company_website", "source_domain",
            "source_industry", "scrape_mode", "provider",
            "mobile", "mobile_status",
            "agency_filter_result", "agency_filter_method",
            "agency_filter_reason", "rejected",
            "brand_verify_result", "brand_verify_method",
            "brand_verify_evidence", "amazon_presence",
            "amazon_verdict", "amazon_revenue_annual",
            "amazon_revenue_source", "amazon_reason", "bettercontact_raw",
            "scrape_request_id", "lead_approval"]

    def _approval_for(lead: dict) -> str | None:
        if scrape_request_id is None:
            return None
        return "rejected" if lead.get("rejected") else "pending"

    rows = [
        [l.get(c) if c != "bettercontact_raw" else json.dumps(l.get(c) or {})
         for c in cols[:-2]] + [scrape_request_id, _approval_for(l)]
        for l in leads
    ]
    placeholders = ",".join(["%s"] * len(cols))
    sql = (f"insert into prospeo_new_leads ({','.join(cols)}) "
           f"values ({placeholders}) on conflict do nothing")
    with conn.cursor() as cur:
        cur.executemany(sql, rows)
        return cur.rowcount


# ---------------------------------------------------------------------------
# Round-robin runner
# ---------------------------------------------------------------------------

_norm_dom = brand_verify.norm_domain


def _gate_per_domain(conn, client, system, leads: list[dict]) -> dict:
    """ICP-gate verdicts keyed by normalized domain (cost reseq R8).

    One Haiku judgment per unique company domain instead of one per lead
    (measured 42% duplicates), cached across runs in icp_gate_cache, fanned
    through a small thread pool. Domain-less leads are judged individually
    under a 'lead:<email>' key and never cached.

    Quality note: this is strictly MORE coherent than the old per-lead loop
    — previously each contact of a company got an independent (non-zero
    temperature) draw, so companies could be half-accepted; now one verdict
    applies to the whole company, and the website-verification funnel still
    re-judges every accepted company from primary evidence.
    """
    from concurrent.futures import ThreadPoolExecutor

    reps: dict[str, dict] = {}
    keyed: dict[str, tuple[str, str]] = {}
    for lead in leads:
        dom = _norm_dom(lead.get("company_domain"))
        key = dom or f"lead:{lead['email']}"
        if key not in reps:
            reps[key] = lead

    domains = [k for k in reps if not k.startswith("lead:")]
    if domains:
        with conn.cursor() as cur:
            cur.execute("select domain, result, reason from icp_gate_cache "
                        "where domain = any(%s)", (domains,))
            for d, res, why in cur.fetchall():
                keyed[d] = (res, f"cache: {why}")

    todo = [k for k in reps if k not in keyed]

    def judge(key: str) -> None:
        result, reason = llm_classify_batch(client, system, [reps[key]])[0]
        keyed[key] = (result, reason)

    if todo:
        with ThreadPoolExecutor(min(6, len(todo))) as ex:
            list(ex.map(judge, todo))

    fresh = [(k, *keyed[k]) for k in todo if not k.startswith("lead:")
             and k in keyed]
    if fresh:
        with conn.cursor() as cur:
            cur.executemany(
                """insert into icp_gate_cache (domain, result, reason)
                   values (%s, %s, %s)
                   on conflict (domain) do update
                     set result = excluded.result, reason = excluded.reason,
                         decided_at = now()""",
                [(d, r, (w or "")[:400]) for d, r, w in fresh])
        conn.commit()
    return keyed


def _run_category(conn, api_key: str, *,
                   target_leads: int | None,
                   country: list[str] | None,
                   skip_industries: list[str] | None = None,
                   page_limit: int = BC_PAGE_LIMIT,
                   dry_run: bool, max_credits: int | None,
                   skip_llm: bool = False,
                   skip_brand_verify: bool = False,
                   skip_amazon_qa: bool = False,
                   amazon_qa_max_credits: int = AMAZON_QA_MAX_CREDITS,
                   revenue_floor: float = amazon_revenue_qa.REVENUE_FLOOR_ANNUAL,
                   enrichment: str = "email",
                   scrape_request_id: int | None = None) -> dict:
    """Round-robin over BC_INDUSTRIES, advancing each industry's offset by
    BC_PAGE_LIMIT each cycle until target/budget/exhaustion.

    Returns a run summary dict so callers (the Lead Scrape Automation worker)
    can record per-request stats without re-querying the lifetime-aggregated
    bettercontact_scrape_state table:
        {
          "accepted": int,
          "rejected": int,
          "credits_spent": float,
          "csv_path": str | None,
          "xlsx_path": str | None,
          "aborted_reason": str | None,
          "rejected_counts": dict[str, int],
        }
    Dry-run / all-exhausted early-returns get a zero-stats dict (still a dict).
    """
    countries = country
    skip_set = set(skip_industries or [])

    # Build active queue, respecting skips + exhausted state.
    state = _load_state(conn)
    queue: list[str] = []
    print(f"\ncategory mode (BetterContact) — countries={countries or '(any)'}, "
          f"headcount_min={BC_HEADCOUNT_MIN}")
    for ind in BC_INDUSTRIES:
        if ind in skip_set:
            print(f"  [skip] {ind!r} (--skip-industries)")
            continue
        s = state.get(ind, {})
        if s.get("exhausted"):
            print(f"  [skip] {ind!r} exhausted (offset={s.get('last_offset_consumed')})")
            continue
        parked = s.get("parked_at")
        if parked is not None:
            if datetime.now(timezone.utc) - parked < timedelta(days=30):
                print(f"  [skip] {ind!r} parked since {parked:%Y-%m-%d} "
                      f"(2+ zero-yield calls; auto-retries after 30d)")
                continue
            print(f"  [retry] {ind!r} park expired — probing segment again")
        queue.append(ind)

    if not queue:
        print("All industries exhausted. Reset state to re-scan from top.")
        return {"accepted": 0, "rejected": 0, "credits_spent": 0.0,
                "csv_path": None, "xlsx_path": None,
                "aborted_reason": "all_industries_exhausted",
                "rejected_counts": {}}

    print(f"  {len(queue)} industries active.")

    if dry_run:
        for ind in queue:
            s = state.get(ind, {})
            print(f"  would scrape {ind!r} starting at offset={s.get('last_offset_consumed', 0)}")
        return {"accepted": 0, "rejected": 0, "credits_spent": 0.0,
                "csv_path": None, "xlsx_path": None,
                "aborted_reason": "dry_run",
                "rejected_counts": {}}

    # Per-industry consecutive transport-failure counter (drop after N).
    consec_failures: dict[str, int] = {ind: 0 for ind in queue}
    MAX_CONSEC_FAILURES = 3

    # Phones bill 10 credits each on top of 1/email (probe-verified), so
    # the worst-case credit reservation per page scales 11x when enabled.
    # The extra 10% headroom covers BC's documented-but-unconfirmed
    # 0.1 cr/slot search fee: production logs (44 pages, max 0.92 cr/slot)
    # contradict that fee, but the billing-semantics support ticket is
    # still open, so reserve for the worse model — over-reserving aborts
    # earlier, under-reserving could settle past the cap.
    enrich_phones = enrichment in ("both", "phone")
    # ceil(page * 11.1) / ceil(page * 1.1) — shared with the submit-form
    # validation via reserve_for_page() so the two never drift.
    reserve_per_page = reserve_for_page(page_limit, enrichment)
    if enrich_phones:
        print(f"  phone enrichment ON — 10 cr/phone; per-page reservation {reserve_per_page}")

    existing_emails = _load_existing_emails(conn)
    total_existing_before_run = len(existing_emails)
    print(f"  existing emails in DB: {total_existing_before_run:,}")

    # Cost reseq R2: server-side suppression of companies whose emails we'd
    # pay for and then discard (capped companies + company-level rejects).
    # Local dedup/cap stay as backstops — this only reduces what BC bills.
    suppression = _build_suppression_list(conn)
    print(f"  suppression list: {len(suppression)} company domains excluded server-side")

    # Per-company contact cap: seed with how many accepted BC contacts each
    # domain already has, so we never exceed BC_MAX_CONTACTS_PER_COMPANY across
    # runs. Incremented as we accept; survivors are sorted by title priority
    # per page so the highest-value contacts win the slots.
    company_counts = _load_company_counts(conn)

    accepted: list[dict] = []
    rejected: list[dict] = []
    rejected_counts: dict[str, int] = {}
    # one Rainforest budget for the WHOLE run (not per page)
    amazon_qa_budget = {"max": amazon_qa_max_credits, "spent": 0}
    # P3: lazy LLM brand-gate client (only created on first grey lead). Mirrors
    # Prospeo — leads that clear the rule layer are run through the
    # brand/agency/reseller/marketplace classifier; only "brand" is kept.
    llm_client: anthropic.Anthropic | None = None
    llm_system: str | None = None
    # Credit accounting: pre-charged budget. `credits_spent` is the running
    # confirmed cost (from completed calls). `in_flight_credits` is the
    # worst-case reservation for submitted-but-unread calls. We check the cap
    # against `credits_spent + in_flight_credits` BEFORE submitting, so we
    # never overshoot even when BC is slow to process. On timeout, we
    # conservatively assume BC charged us the full reservation (worst case).
    credits_spent: float = 0.0
    in_flight_credits: float = 0.0
    aborted_reason: str | None = None
    cycle = 0

    while queue:
        cycle += 1
        print(f"\n--- cycle {cycle} ({len(queue)} active industries) ---")
        next_queue: list[str] = []

        # ------------------------------------------------------------------
        # Phase A — submit all queue industries in parallel.
        # Each submit is a ~50-100ms POST so doing them serially is still
        # ~1s for the whole queue. We track per-rid metadata so when results
        # come back we know which industry / offset to credit.
        # ------------------------------------------------------------------
        submitted: dict[str, tuple[str, int]] = {}   # rid -> (industry, offset)
        submit_failures: dict[str, Exception] = {}   # industry -> exception
        for ind in queue:
            if target_leads is not None and len(accepted) >= target_leads:
                aborted_reason = f"target_leads reached ({len(accepted)}/{target_leads})"
                break
            if (max_credits is not None
                    and credits_spent + in_flight_credits + reserve_per_page > max_credits):
                if credits_spent == 0 and in_flight_credits == 0:
                    # Couldn't even start: the credit budget is below one page's cost.
                    aborted_reason = (
                        f"Budget too low to start. One page of {page_limit} leads needs "
                        f"~{reserve_per_page} credits, but the credit budget was set to "
                        f"{max_credits}. Raise the credit budget to at least {reserve_per_page} "
                        f"and resubmit."
                    )
                else:
                    aborted_reason = (
                        f"Budget cap reached. Spent {credits_spent:.0f} credits "
                        f"(+{in_flight_credits:.0f} in flight); the next page would need "
                        f"~{reserve_per_page} more, which exceeds the {max_credits} budget. "
                        f"Stopping with the leads gathered so far."
                    )
                break
            s = state.get(ind, {})
            offset = s.get("last_offset_consumed", 0)
            in_flight_credits += reserve_per_page
            # Retried submits whose POST may have reached BC before the
            # transport died can leave ORPHANED server-side searches that
            # bill but never settle. Book each one as spent at the full
            # reservation — the submit-side mirror of the poll-timeout
            # booking below (BC keeps billing for abandoned requests).
            ambiguous: list = []
            try:
                rid = _submit_search(
                    _industry_filters(ind, countries,
                                      exclude_domains=suppression),
                    page_limit, offset, api_key,
                    enrich_phones=enrich_phones,
                    ambiguous_attempts=ambiguous)
                submitted[rid] = (ind, offset)
            except InsufficientCreditsError as e:
                in_flight_credits -= reserve_per_page
                print(f"  !!! {e}", file=sys.stderr)
                aborted_reason = "BetterContact INSUFFICIENT_CREDITS"
                break
            except Exception as e:
                in_flight_credits -= reserve_per_page
                submit_failures[ind] = e
            finally:
                if ambiguous:
                    credits_spent += reserve_per_page * len(ambiguous)
                    print(f"  ! {ind!r}: {len(ambiguous)} ambiguous submit "
                          f"attempt(s) ({', '.join(ambiguous)}) — booked "
                          f"{reserve_per_page * len(ambiguous)} credits as "
                          f"possibly billed by orphaned searches",
                          file=sys.stderr)

        if not submitted:
            # Nothing made it through — either we hit a hard abort during
            # submission, or every industry failed at submit time.
            if aborted_reason:
                break
            for ind, e in submit_failures.items():
                consec_failures[ind] = consec_failures.get(ind, 0) + 1
                if consec_failures[ind] >= MAX_CONSEC_FAILURES:
                    print(f"  ! {ind!r} submit failed: {e} "
                          f"[dropping after {MAX_CONSEC_FAILURES} consecutive failures]",
                          file=sys.stderr)
                else:
                    print(f"  ! {ind!r} submit failed: {e} "
                          f"[{consec_failures[ind]}/{MAX_CONSEC_FAILURES}]",
                          file=sys.stderr)
                    next_queue.append(ind)
            queue = next_queue
            continue

        print(f"  submitted {len(submitted)} industries concurrently, polling...")

        # ------------------------------------------------------------------
        # Phase B — poll all submitted request_ids concurrently. Returns
        # only the ones that terminated; unterminated rids are treated as
        # timeouts.
        # ------------------------------------------------------------------
        results = _poll_many(list(submitted.keys()), api_key)

        # ------------------------------------------------------------------
        # Phase C — process each industry's result, update state, write to DB.
        # ------------------------------------------------------------------
        for rid, (ind, offset) in submitted.items():
            if rid not in results:
                # Poll timeout: BC very likely still charged us. Treat
                # reservation as spent (mirrors batch 1 recovery semantic).
                in_flight_credits -= reserve_per_page
                credits_spent += reserve_per_page
                consec_failures[ind] = consec_failures.get(ind, 0) + 1
                if consec_failures[ind] >= MAX_CONSEC_FAILURES:
                    print(f"  ! {ind!r} offset={offset}: poll timeout "
                          f"[dropping after {MAX_CONSEC_FAILURES} consecutive failures; "
                          f"assumed {reserve_per_page} credits charged]",
                          file=sys.stderr)
                else:
                    print(f"  ! {ind!r} offset={offset}: poll timeout "
                          f"[{consec_failures[ind]}/{MAX_CONSEC_FAILURES}; "
                          f"assumed {reserve_per_page} credits charged]",
                          file=sys.stderr)
                    next_queue.append(ind)
                continue

            consec_failures[ind] = 0
            result = results[rid]

            # Missing (not zero) credits_consumed books the full reservation:
            # the whole ledger trusts this one field, and a silent API shape
            # change must fail toward early abort, not an uncapped run.
            cc_raw = result.get("credits_consumed")
            cc = float(cc_raw) if cc_raw is not None else float(reserve_per_page)
            in_flight_credits -= reserve_per_page
            credits_spent += cc
            leads_found = ((result.get("summary") or {}).get("leads_found") or 0)
            bc_leads = result.get("leads") or []

            if state.get(ind, {}).get("total_leads_estimated") is None and leads_found:
                state.setdefault(ind, {})["total_leads_estimated"] = leads_found

            batch_accepted: list[dict] = []
            batch_rejected: list[dict] = []
            for bc in bc_leads:
                lead = _parse_bc_lead(bc, ind)
                if not lead:
                    rejected_counts["no_deliverable_email"] = (
                        rejected_counts.get("no_deliverable_email", 0) + 1)
                    continue
                if lead["email"] in existing_emails:
                    lead["agency_filter_result"] = "rejected"
                    lead["agency_filter_reason"] = "dedup_existing_db"
                    lead["rejected"] = True
                    batch_rejected.append(lead)
                    rejected_counts["dedup"] = rejected_counts.get("dedup", 0) + 1
                    continue
                ok, reason = _post_filter(lead)
                if not ok:
                    lead["agency_filter_result"] = "rejected"
                    lead["agency_filter_reason"] = reason
                    lead["rejected"] = True
                    batch_rejected.append(lead)
                    rejected_counts[reason] = rejected_counts.get(reason, 0) + 1
                    continue
                lead["rejected"] = False
                batch_accepted.append(lead)
                existing_emails.add(lead["email"])

            # Per-company cap FIRST (cost reseq R7): the cap is free and
            # deterministic, the LLM gate costs tokens — previously up to 5
            # contacts per company were gate-judged and only 3 survived the
            # cap. Safe to reorder: the gate's verdict is company-level, so
            # capping never changes which companies get judged. Keep at most
            # BC_MAX_CONTACTS_PER_COMPANY contacts per domain (highest title
            # priority first), counting contacts already accepted in earlier
            # pages/runs.
            if batch_accepted:
                capped: list[dict] = []
                for lead in sorted(batch_accepted,
                                   key=lambda l: bc_title_rank(l.get("title")) or 99):
                    dom = (lead.get("company_domain") or "").lower()
                    if dom and company_counts.get(dom, 0) >= BC_MAX_CONTACTS_PER_COMPANY:
                        lead["agency_filter_result"] = "rejected"
                        lead["agency_filter_reason"] = "company_contact_cap"
                        lead["rejected"] = True
                        batch_rejected.append(lead)
                        rejected_counts["company_cap"] = rejected_counts.get("company_cap", 0) + 1
                    else:
                        if dom:
                            company_counts[dom] = company_counts.get(dom, 0) + 1
                        capped.append(lead)
                batch_accepted = capped

            # ICP LLM brand-gate, per-DOMAIN + cached + parallel (cost reseq
            # R8): measured 1,013 judgments for 591 companies (42% duplicate)
            # under the old per-lead serial loop. The verdict is company-
            # level (verified: only 2/178 multi-lead companies ever got
            # accept-vs-reject divergence, both documented gate errors), so
            # judge each domain once, cache in icp_gate_cache across runs,
            # and fan uncached judgments through a small thread pool. Only
            # an in-scope manufacturer/private-label "brand" survives;
            # LLM-rejected rows are still inserted (as rejected) for audit.
            if batch_accepted and not skip_llm:
                if llm_client is None:
                    llm_client = anthropic.Anthropic()
                    llm_system = BC_ICP_PROMPT.read_text(encoding="utf-8")
                verdicts = _gate_per_domain(conn, llm_client, llm_system,
                                            batch_accepted)
                survivors = []
                for lead in batch_accepted:
                    dom = _norm_dom(lead.get("company_domain"))
                    # Fail CLOSED: a missing verdict key must NOT default to
                    # "brand" (accept) — an unexpected gap should reject the
                    # paid lead for review, never silently pass it as in-ICP.
                    result, reason = verdicts.get(
                        dom or f"lead:{lead['email']}", ("unknown", "no verdict — fail-closed"))
                    lead["agency_filter_method"] = "llm"
                    lead["agency_filter_result"] = result
                    if result == "brand":
                        lead["agency_filter_reason"] = reason
                        survivors.append(lead)
                    else:
                        lead["agency_filter_reason"] = f"{result}: {reason}"
                        lead["rejected"] = True
                        batch_rejected.append(lead)
                        key = f"llm_{result}"
                        rejected_counts[key] = rejected_counts.get(key, 0) + 1
                batch_accepted = survivors

            # Website verification (RESELLER_DETECTION_PLAN.md + the bv2
            # gap-fixes): per-domain verdicts — reseller, MLM, banned /
            # out-of-scope category, no DTC store, foreign-not-selling-US/CA,
            # corporate/enterprise size — from the layered funnel. Any
            # REJECT_VERDICTS verdict rejects with evidence; 'brand'/'unknown'
            # pass through stamped — unknowns are never auto-rejected.
            if batch_accepted and not skip_brand_verify:
                verdicts = brand_verify.verify_domains(conn, batch_accepted)
                survivors = []
                for lead in batch_accepted:
                    v = verdicts.get(
                        brand_verify.norm_domain(lead.get("company_domain")) or "")
                    if not v:
                        survivors.append(lead)
                        continue
                    lead["brand_verify_result"] = v["verdict"]
                    lead["brand_verify_method"] = v["method"]
                    lead["brand_verify_evidence"] = (v.get("evidence") or "")[:1000]
                    lead["amazon_presence"] = v.get("amazon_presence")
                    reason = brand_verify.REJECT_VERDICTS.get(v["verdict"])
                    if reason:
                        lead["agency_filter_result"] = v["verdict"]
                        lead["agency_filter_reason"] = (
                            f"{reason}: {v.get('evidence', '')}"[:500])
                        lead["rejected"] = True
                        batch_rejected.append(lead)
                        rejected_counts[reason] = (
                            rejected_counts.get(reason, 0) + 1)
                    else:
                        survivors.append(lead)
                batch_accepted = survivors

            # Amazon Revenue QA — LAST company-level gate (cheapest-first
            # ordering: this is the priciest check, so everything above has
            # already rejected for free/cheaper). Shadow mode stamps verdicts
            # for Jam; enforce mode also rejects DROPs. REVIEW / PENDING /
            # API-failure NEVER reject (mirrors bv3's unknowns-pass rule).
            if batch_accepted and not skip_amazon_qa:
                try:
                    amazon_revenue_qa.qa_companies(
                        conn, batch_accepted, budget=amazon_qa_budget,
                        floor_line=revenue_floor)
                    if AMAZON_QA_ENFORCE:
                        survivors = []
                        for lead in batch_accepted:
                            if lead.get("amazon_verdict") == "DROP":
                                lead["agency_filter_result"] = "amazon_qa_drop"
                                lead["agency_filter_reason"] = (
                                    f"amazon_qa: {lead.get('amazon_reason', '')}"[:500])
                                lead["rejected"] = True
                                batch_rejected.append(lead)
                                rejected_counts["amazon_qa"] = (
                                    rejected_counts.get("amazon_qa", 0) + 1)
                            else:
                                survivors.append(lead)
                        batch_accepted = survivors
                except Exception as e:
                    # QA must never sink a batch — leads pass unstamped.
                    print(f"    amazon-qa error (leads pass unstamped): {e}")

            if batch_accepted or batch_rejected:
                _insert_leads(conn, batch_accepted + batch_rejected,
                              scrape_request_id=scrape_request_id)
                conn.commit()

            new_offset = offset + page_limit
            est = state.get(ind, {}).get("total_leads_estimated") or leads_found or 0
            exhausted = bool(est) and new_offset >= est

            _update_state(conn, ind, new_offset=new_offset,
                          leads_found=leads_found,
                          credits_spent_delta=cc,
                          exhausted=exhausted,
                          countries=countries or [],
                          accepted_delta=len(batch_accepted))
            conn.commit()
            state.setdefault(ind, {})["last_offset_consumed"] = new_offset
            state[ind]["exhausted"] = exhausted

            accepted.extend(batch_accepted)
            rejected.extend(batch_rejected)

            print(f"  [{ind}] offset={offset}→{new_offset} of est={est:,}: "
                  f"+{len(batch_accepted)} accepted / "
                  f"+{len(batch_rejected)} rejected "
                  f"(credits this call: {cc:.1f}, run total: {credits_spent:.1f}"
                  f"{'/' + str(max_credits) if max_credits else ''})")

            if not exhausted:
                next_queue.append(ind)
            else:
                print(f"    [{ind!r} exhausted at offset {new_offset}]")

        # Submit-time failures (no rid was ever issued) → also count toward
        # consec_failures and potentially re-queue.
        for ind, e in submit_failures.items():
            consec_failures[ind] = consec_failures.get(ind, 0) + 1
            if consec_failures[ind] >= MAX_CONSEC_FAILURES:
                print(f"  ! {ind!r} submit failed: {e} "
                      f"[dropping after {MAX_CONSEC_FAILURES} consecutive failures]",
                      file=sys.stderr)
            else:
                print(f"  ! {ind!r} submit failed: {e} "
                      f"[{consec_failures[ind]}/{MAX_CONSEC_FAILURES}]",
                      file=sys.stderr)
                next_queue.append(ind)

        if aborted_reason:
            break
        queue = next_queue

    if aborted_reason:
        print(f"\n!!! Run aborted: {aborted_reason}", file=sys.stderr)

    # P5: independent QA scan of this run's accepted leads (warn-only here —
    # the enforcing gate is `run.py export-leads`). Catches anything that
    # slipped the scrape-time filters before it reaches the per-run CSV.
    try:
        import lead_qa
        qa = lead_qa.scan(accepted)
        if qa["flagged"]:
            lead_qa.print_report(qa)
            print("  ! QA flagged leads in this run — review before loading; "
                  "`run.py qa-leads --fix` will quarantine them.", file=sys.stderr)
    except Exception as e:  # QA must never break a scrape run
        print(f"  ! QA scan skipped: {e}", file=sys.stderr)

    # --- Final export -------------------------------------------------------
    os.makedirs("exports", exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    csv_path = f"exports/bettercontact_new_leads_{stamp}.csv"
    xlsx_path = f"exports/bettercontact_new_leads_{stamp}.xlsx"
    # Accepted-only CSV (what Jam loads into Instantly).
    write_csv(accepted, csv_path)
    # Two-sheet XLSX: Accepted + Rejected (audit).
    write_xlsx(accepted, rejected, xlsx_path)

    print(f"\n=== summary ===")
    if aborted_reason:
        print(f"  ABORTED:               {aborted_reason}")
    print(f"  countries:             {countries or '(any)'}")
    print(f"  industries scanned:    {len(BC_INDUSTRIES)}")
    print(f"  accepted (brand):      {len(accepted)}")
    print(f"  rejected:              {len(rejected)}")
    print(f"  total credits spent:   {credits_spent:.1f}")
    print(f"  CSV (accepted):        {csv_path}")
    print(f"  XLSX (both sheets):    {xlsx_path}")
    if rejected_counts:
        print(f"  rejection breakdown:")
        for reason, n in sorted(rejected_counts.items(), key=lambda x: -x[1])[:10]:
            print(f"    {reason}: {n}")

    print(f"  amazon-qa: {amazon_qa_budget['spent']}/{amazon_qa_budget['max']} "
          f"Rainforest credits this run (shadow={'off' if AMAZON_QA_ENFORCE else 'on'})")
    return {
        "accepted": len(accepted),
        "rejected": len(rejected),
        "credits_spent": float(credits_spent),
        "csv_path": csv_path,
        "xlsx_path": xlsx_path,
        "aborted_reason": aborted_reason,
        "rejected_counts": dict(rejected_counts),
        "amazon_qa_credits": amazon_qa_budget["spent"],
    }


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def _run_category_revenue_first(conn, api_key: str, *,
                                target_leads: int | None,
                                country: list[str] | None,
                                skip_industries: list[str] | None = None,
                                page_limit: int = BC_PAGE_LIMIT,
                                dry_run: bool, max_credits: int | None,
                                skip_llm: bool = False,
                                skip_brand_verify: bool = False,
                                skip_amazon_qa: bool = False,
                                amazon_qa_max_credits: int = AMAZON_QA_MAX_CREDITS,
                                revenue_floor: float = amazon_revenue_qa.REVENUE_FLOOR_ANNUAL,
                                scrape_request_id: int | None = None) -> dict:
    """REVENUE-FIRST category scrape (opt-in, experimental).

    Discover email-free (credits_consumed=0) -> ICP/brand/revenue-gate the
    company (no email needed) -> enrich ONLY survivors via the standalone
    endpoint (1 cr/found email). Shifts spend from BetterContact emails to
    (cheap) Rainforest. Pre-enrich rejects are never paid for or written; their
    verdicts live in the brand/revenue caches. Needs a Phase-4 live validation
    before it defaults — see docs/cost/REVENUE_FIRST_PIPELINE_PLAN.md."""
    countries = country
    skip_set = set(skip_industries or [])
    state = _load_state(conn)
    existing_emails = _load_existing_emails(conn)
    company_counts = _load_company_counts(conn)
    amazon_qa_budget = {"max": amazon_qa_max_credits, "spent": 0}

    queue = [ind for ind in BC_INDUSTRIES
             if ind not in skip_set and not state.get(ind, {}).get("exhausted")]
    print(f"\ncategory REVENUE-FIRST — countries={countries or '(any)'}, "
          f"target={target_leads or '(all)'}, BC-enrich cap={max_credits}, "
          f"Rainforest cap={amazon_qa_max_credits}")
    zero = {"accepted": 0, "rejected": 0, "credits_spent": 0.0, "csv_path": None,
            "xlsx_path": None, "rejected_counts": {}, "amazon_qa_credits": 0}
    if not queue:
        return {**zero, "aborted_reason": "all_industries_exhausted"}
    if dry_run:
        print("--dry-run: no paid calls, no writes.")
        return {**zero, "aborted_reason": "dry_run"}

    llm_client = llm_system = None
    accepted: list[dict] = []
    rejected_counts: dict[str, int] = {}
    credits_spent = 0.0            # BC ENRICH credits only — discovery is free
    n_discovered = n_gated_out = 0
    enrich_failures = 0            # consecutive transient BC-enrich failures
    aborted_reason = None

    def _revfirst_queue():
        # Round-robin the live (non-exhausted) industries, cycling to DEEPER
        # offsets each pass, until every industry is exhausted. Previously this
        # was a single pass (one page/industry) so a target-based run stopped
        # after ~one page each and never used its credit budget. Caps
        # (target / Rainforest / BC) are enforced by the break checks below.
        while True:
            live = [i for i in BC_INDUSTRIES
                    if i not in skip_set and not state.get(i, {}).get("exhausted")]
            if not live:
                return
            for i in live:
                yield i

    for ind in _revfirst_queue():
        if target_leads and len(accepted) >= target_leads:
            break
        if amazon_qa_budget["spent"] >= amazon_qa_budget["max"]:
            aborted_reason = (f"Rainforest cap hit "
                              f"({amazon_qa_budget['spent']}/{amazon_qa_budget['max']})")
            break
        offset = state.get(ind, {}).get("last_offset_consumed", 0)
        filters = _industry_filters(ind, countries)
        # 1. email-free discovery (0 credits)
        try:
            rid = _submit_search(filters, page_limit, offset, api_key,
                                 enrich_email_address=False)
            result = _poll_for_result(rid, api_key)
        except Exception as e:
            print(f"  ! {ind!r} discovery failed: {e}", file=sys.stderr)
            continue
        leads_found = ((result.get("summary") or {}).get("leads_found") or 0)
        bc_leads = result.get("leads") or []
        n_discovered += len(bc_leads)

        # 2. parse (no email) + deterministic ICP gates
        survivors: list[dict] = []
        for bc in bc_leads:
            lead = _parse_bc_person(bc, ind)
            if not lead:
                continue
            ok, reason = _post_filter(lead)
            if not ok:
                rejected_counts[reason] = rejected_counts.get(reason, 0) + 1
                continue
            survivors.append(lead)
        # 3. per-company cap (free, deterministic — before the paid/LLM steps)
        capped = []
        for lead in sorted(survivors, key=lambda l: bc_title_rank(l.get("title")) or 99):
            dom = (lead.get("company_domain") or "").lower()
            if dom and company_counts.get(dom, 0) >= BC_MAX_CONTACTS_PER_COMPANY:
                rejected_counts["company_cap"] = rejected_counts.get("company_cap", 0) + 1
            else:
                if dom:
                    company_counts[dom] = company_counts.get(dom, 0) + 1
                capped.append(lead)
        survivors = capped
        # 4. ICP LLM gate (per-domain, cached)
        if survivors and not skip_llm:
            if llm_client is None:
                llm_client = anthropic.Anthropic()
                llm_system = BC_ICP_PROMPT.read_text(encoding="utf-8")
            verdicts = _gate_per_domain(conn, llm_client, llm_system, survivors)
            kept = []
            for lead in survivors:
                dom = _norm_dom(lead.get("company_domain"))
                res, rsn = verdicts.get(dom or "", ("unknown", "no verdict — fail-closed"))
                if res == "brand":
                    lead["agency_filter_method"] = "llm"
                    lead["agency_filter_result"] = "brand"
                    lead["agency_filter_reason"] = rsn
                    kept.append(lead)
                else:
                    rejected_counts[f"llm_{res}"] = rejected_counts.get(f"llm_{res}", 0) + 1
            survivors = kept
        # 5. brand_verify (domain)
        if survivors and not skip_brand_verify:
            vd = brand_verify.verify_domains(conn, survivors)
            kept = []
            for lead in survivors:
                v = vd.get(brand_verify.norm_domain(lead.get("company_domain")) or "")
                if v and brand_verify.REJECT_VERDICTS.get(v["verdict"]):
                    rejected_counts[v["verdict"]] = rejected_counts.get(v["verdict"], 0) + 1
                    continue
                if v:
                    lead["brand_verify_result"] = v["verdict"]
                    lead["brand_verify_method"] = v["method"]
                    lead["brand_verify_evidence"] = (v.get("evidence") or "")[:1000]
                    lead["amazon_presence"] = v.get("amazon_presence")
                kept.append(lead)
            survivors = kept
        # 6. Amazon revenue QA — the revenue gate (reject DROP; REVIEW/PENDING pass)
        if survivors and not skip_amazon_qa:
            try:
                amazon_revenue_qa.qa_companies(conn, survivors, budget=amazon_qa_budget,
                                               floor_line=revenue_floor)
                kept = []
                for lead in survivors:
                    if lead.get("amazon_verdict") == "DROP":
                        rejected_counts["amazon_qa"] = rejected_counts.get("amazon_qa", 0) + 1
                    else:
                        kept.append(lead)
                survivors = kept
            except Exception as e:
                print(f"    amazon-qa error (leads pass unstamped): {e}")
        n_gated_out += len(bc_leads) - len(survivors)

        # 7. enrich ONLY survivors (paid; respect BC budget)
        page_accepted: list[dict] = []
        if survivors and (max_credits is None or credits_spent < max_credits):
            try:
                enr = enrich_contacts_resilient(survivors, api_key)
                enrich_failures = 0
            except InsufficientCreditsError as e:
                aborted_reason = str(e)          # credits genuinely out -> stop
                enr = None
            except RuntimeError as e:
                # Transient BC enrich failure (poll timeout / 5xx / transport):
                # skip THIS page and keep cycling — one slow BC poll must not
                # abort the whole run (batch #47 died on a 600s poll timeout).
                # Bail only if BC enrichment fails repeatedly (it's down).
                enrich_failures += 1
                print(f"    enrich failed for [{ind}] (#{enrich_failures}), "
                      f"skipping this page: {str(e)[:110]}")
                rejected_counts["enrich_error"] = (
                    rejected_counts.get("enrich_error", 0) + len(survivors))
                enr = None
                if enrich_failures >= 3:
                    aborted_reason = (f"BC enrichment failing repeatedly "
                                      f"({enrich_failures}x) — stopping")
            if enr:
                credits_spent += float(enr.get("credits_consumed") or 0)
                idx = {}
                for row in (enr.get("data") or []):
                    k = ((row.get("contact_first_name") or "").lower(),
                         (row.get("contact_last_name") or "").lower(),
                         (row.get("company_domain") or "").lower())
                    idx[k] = row
                for lead in survivors:
                    k = ((lead.get("first_name") or "").lower(),
                         (lead.get("last_name") or "").lower(),
                         (lead.get("company_domain") or "").lower())
                    row = idx.get(k) or {}
                    email = (row.get("contact_email_address") or "").lower().strip()
                    if not email or row.get("contact_email_address_status") != "deliverable":
                        continue
                    # Now that we have the real email, apply the generic/role
                    # mailbox check that _post_filter deferred pre-enrichment.
                    if is_generic_email(email):
                        rejected_counts["generic_email"] = rejected_counts.get("generic_email", 0) + 1
                        continue
                    if email in existing_emails:
                        continue
                    existing_emails.add(email)
                    lead["email"] = email
                    lead["mobile"] = row.get("contact_phone_number")
                    lead["rejected"] = False
                    page_accepted.append(lead)
        elif survivors:
            aborted_reason = f"BC enrich budget cap hit ({credits_spent}/{max_credits})"

        # 8. write accepted survivors (they now have a deliverable email)
        if page_accepted:
            _insert_leads(conn, page_accepted, scrape_request_id=scrape_request_id)
            conn.commit()
            accepted.extend(page_accepted)

        new_offset = offset + page_limit
        est = state.get(ind, {}).get("total_leads_estimated") or leads_found or 0
        # a page that returns nothing => pool end (guards the cycle loop from
        # spinning forever on an industry whose estimated total is unknown/0).
        exhausted = (bool(est) and new_offset >= est) or len(bc_leads) == 0
        _update_state(conn, ind, new_offset=new_offset, leads_found=leads_found,
                      credits_spent_delta=0.0, exhausted=exhausted,
                      countries=countries or [], accepted_delta=len(page_accepted))
        conn.commit()
        state.setdefault(ind, {})["last_offset_consumed"] = new_offset
        state[ind]["exhausted"] = exhausted   # keep the cycle queue in sync
        print(f"  [{ind}] discovered {len(bc_leads)} | survived gates {len(survivors)} | "
              f"accepted {len(page_accepted)} | BC-cr {credits_spent:.0f}"
              f"{f'/{max_credits}' if max_credits else ''} | RF-cr {amazon_qa_budget['spent']}")
        if aborted_reason:
            break

    os.makedirs("exports", exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    csv_path = f"exports/bettercontact_revfirst_{stamp}.csv"
    xlsx_path = f"exports/bettercontact_revfirst_{stamp}.xlsx"
    write_csv(accepted, csv_path)
    write_xlsx(accepted, [], xlsx_path)

    print(f"\n=== revenue-first summary ===")
    if aborted_reason:
        print(f"  ABORTED:              {aborted_reason}")
    print(f"  discovered (free):    {n_discovered}")
    print(f"  gated out (unpaid):   {n_gated_out}  <- would have been enriched in the classic flow")
    print(f"  enriched + accepted:  {len(accepted)}")
    print(f"  BC enrich credits:    {credits_spent:.0f}")
    print(f"  Rainforest credits:   {amazon_qa_budget['spent']}")
    print(f"  CSV (accepted):       {csv_path}")
    for r, n in sorted(rejected_counts.items(), key=lambda x: -x[1])[:10]:
        print(f"    {r}: {n}")
    return {"accepted": len(accepted), "rejected": n_gated_out,
            "credits_spent": float(credits_spent), "csv_path": csv_path,
            "xlsx_path": xlsx_path, "aborted_reason": aborted_reason,
            "rejected_counts": dict(rejected_counts),
            "amazon_qa_credits": amazon_qa_budget["spent"]}


def main(*, mode: str = "category", target_leads: int | None = None,
         country: list[str] | None = None,
         skip_industries: list[str] | None = None,
         page_limit: int = BC_PAGE_LIMIT,
         dry_run: bool = False, max_credits: int | None = None,
         skip_llm: bool = False, skip_brand_verify: bool = False,
         skip_amazon_qa: bool = False,
         amazon_qa_max_credits: int = AMAZON_QA_MAX_CREDITS,
         revenue_floor: float | None = None,
         enrichment: str = "email",
         scrape_request_id: int | None = None,
         revenue_first: bool = False) -> dict:
    """Entry point for `run.py scrape-leads --provider bettercontact`.

    Domain mode is NOT supported — BetterContact's Lead Finder is criteria-
    based, not domain-list based. If you want enrichment of an existing
    domain list, that's a different endpoint (separate implementation).

    `scrape_request_id`, when set, tags every inserted row in
    `prospeo_new_leads` so the Lead Scrape Automation worker (see worker.py)
    can later move the request's rows into `lead_contacts` on approval.
    Callers from the CLI leave this as None — only the worker uses it.
    """
    # Use ValueError (not sys.exit / SystemExit) so the Lead Scrape Automation
    # worker can catch config errors via `except Exception` and mark the
    # request `failed` with a clear message — instead of dying mid-poll and
    # leaving the request stuck in `status='running'`. The CLI surfaces these
    # as a traceback, same as any other invalid-input error.
    if mode != "category":
        raise ValueError(f"BetterContact only supports --mode category (got {mode!r})")
    # None -> the default $300k floor (lets CLI/worker pass through unset cleanly).
    revenue_floor = revenue_floor or amazon_revenue_qa.REVENUE_FLOOR_ANNUAL
    # Enforced HERE (not just in run.py's arg parsing) so programmatic callers
    # can't reach the scraper with paid phone enrichment and no budget cap —
    # phones bill 10 cr each and the budget guard is inert when max_credits
    # is None. Covers 'both' and the (unused) 'phone' value alike.
    if enrichment != "email" and max_credits is None:
        raise ValueError(
            f"enrichment={enrichment!r} requires an explicit max_credits "
            f"(phones bill 10 credits each; uncapped phone runs are refused)")

    load_dotenv()
    api_key = (os.environ.get("BETTERCONTACT_API_KEY") or "").strip()
    if not api_key:
        raise ValueError("BETTERCONTACT_API_KEY not set in env")

    conn = connect()
    try:
        runner = (_run_category_revenue_first if revenue_first else _run_category)
        kwargs = dict(target_leads=target_leads, country=country,
                      skip_industries=skip_industries, page_limit=page_limit,
                      dry_run=dry_run, max_credits=max_credits, skip_llm=skip_llm,
                      skip_brand_verify=skip_brand_verify, skip_amazon_qa=skip_amazon_qa,
                      amazon_qa_max_credits=amazon_qa_max_credits,
                      revenue_floor=revenue_floor,
                      scrape_request_id=scrape_request_id)
        if not revenue_first:
            kwargs["enrichment"] = enrichment   # revenue-first is email-only by design
        return runner(conn, api_key, **kwargs)
    finally:
        conn.close()


if __name__ == "__main__":
    # Deliberately NOT runnable directly: a bare `python bettercontact_sync.py`
    # used to launch an immediate, uncapped, untargeted scrape of every
    # industry at page_limit=200 — real credits with a single keystroke.
    # All invocations go through run.py (CLI) or worker.py (automation),
    # both of which handle budget caps.
    sys.exit("bettercontact_sync.py is not a standalone script. Use:\n"
             "  python run.py scrape-leads --provider bettercontact "
             "--mode category --target-leads N --max-credits M")
