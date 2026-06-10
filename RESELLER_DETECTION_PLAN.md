# Reseller Detection — Implementation Plan

**Status:** Phases 0–2 DONE (2026-06-10). Phase 0 measured GO (see §10);
Phase 1 (free layer) and Phase 2 (site fetch + LLM) are built, smoke-verified
and wired into `bettercontact_sync.py` on this branch. Next: Phase 3
(web-search fallback for unreachable sites + unknown-flag surfacing in the
lead reviewer).

**Trigger:** Victor's loom review (2026-06-10) — reseller websites are slipping
into the accepted batches. Example: a shop selling Snap Circuits where the
product brand (Elenco) doesn't match the site's own name. His ask: *"somehow we
just need to make sure that they're not resellers."*

**Branch:** `feat/reseller-detection`

---

## 0. Confidence statement (read this first)

The **diagnosis** below is verified against production data. The **solution
design** is verified where marked (Shopify probe, SmartScout match-rate) and
estimated where marked (Stage 2 LLM precision). We do **not** claim a catch
rate to Victor until Phase 0 produces a measured number against his own
verdicts. The plan ends in measurement, not assertion.

---

## 1. Verified diagnosis — why resellers get through today

All four findings verified live on 2026-06-10 against the 558 accepted
BetterContact leads (`provider='bettercontact' and not rejected`):

1. **The LLM brand-gate never sees the website.** `llm_classify_batch` in
   `bettercontact_sync.py` receives only `{company_name, url,
   company_description}`. The discriminating evidence — multi-brand catalog,
   "Shop by Brand" nav, "authorized dealer of X" — lives on the site it never
   fetches.
2. **BC's description text cannot detect resellers.** A reseller-phrase scan
   matched 110/558 accepted leads — almost all false positives (Physicians
   Choice's description literally says "manufactures professional-grade
   supplements"; Strider, Petit Pot, Nudestix matched on incidental words like
   "distribution"). Phrase-matching the description is not a usable signal.
3. **BC provides no structured business-type signal.** `company_type`,
   `company_organization_type`, `company_industry_code` are 100% null across
   all 558. `company_industry` just echoes the search industry. No
   firmographic shortcut exists.
4. **The prompt biases toward keeping.** The ICP gate's "when unsure, prefer
   brand" rule lets borderline resellers through.

**Conclusion:** reliable reseller detection requires evidence from the actual
website (or, where the site is unreachable, from third-party sources). There
is no shortcut in the data we already hold — except the two free confirms in
Stage 1 below.

Side note: Victor's specific example resolves cleanly — `elenco.com` is the
actual *manufacturer* of Snap Circuits, so it is correctly a brand. The
reseller he saw is not in the current accepted set.

---

## 2. Architecture — a 5-stage funnel

Principle: **each company is judged by the cheapest method that can judge it
confidently.** Money is only spent where the cheaper layer couldn't decide.

```
batch finishes post_filter + ICP LLM gate (existing, unchanged)
        │
Stage 0  Per-domain dedup + verdict cache            free
        │   ~3 contacts/company → judge each DOMAIN once;
        │   verdicts persist in domain_brand_verdicts across batches
        ▼
Stage 1  Free deterministic confirms                 free
        │   a) SmartScout/Amazon fuzzy match (≥92) → brand → PASS
        │      (42% hit rate verified: 133/317 accepted companies)
        │   b) Shopify /products.json vendor probe:
        │        ≤1 real vendor  → brand    → PASS
        │        ≥4 real vendors → reseller → REJECT
        │      (verified: every legit brand probed showed exactly 1 real
        │       vendor; app-noise vendors — Route, re:do, XCover, regional
        │       variants — are filtered before counting)
        ▼   (~45-55% of domains resolved here)
Stage 2  Homepage fetch + one Haiku call             ~$0.01/domain
        │   requests fetch → BeautifulSoup extract (title, meta, nav,
        │   footer, visible text) → deterministic features
        │   (shopify_vendor_count, nav_has_shop_by_brand,
        │    reseller_phrase_hits, brand_phrase_hits,
        │    distinct_external_brand_count) → single structured LLM
        │   verdict {label, confidence, evidence_quote}
        │   high-confidence → PASS / REJECT;  low-conf or fetch-fail ↓
        ▼   (~80% of what reaches it resolved)
Stage 3  Lightweight agentic check                   ~$0.03/domain
        │   ONE Anthropic web_search server-tool call (same SDK + API
        │   key already in the worker — no new vendor) surfacing
        │   LinkedIn / Amazon storefront / press → one LLM verdict.
        │   Judges the company from AROUND the website, so it covers
        │   unreachable/JS-only sites too. NO Playwright in v1.
        ▼
Stage 4  Human review                                Victor/Jam
            still-ambiguous → lead_approval stays 'pending' with the
            evidence attached; surfaces in the existing lead-reviewer
            batch page. Never auto-passed.
```

**Explicitly out of scope for v1:** Playwright/headless-Chromium fallback.
Plain-fetch failures route to Stage 3 (which doesn't need the site to load).
Revisit only if monitoring shows a large share of Stage-3 traffic is purely
fetch-failures (it would convert $0.03 calls back into $0.01 calls — a cost
optimization, not a correctness need).

---

## 3. Schema changes (`migrations.sql`)

```sql
-- Reseller detection (RESELLER_DETECTION_PLAN.md)

-- Per-domain verdict cache. One row per company domain ever judged; repeat
-- domains across batches are never re-researched. Source of truth for the
-- verdict; prospeo_new_leads rows carry a denormalized copy for export/audit.
create table if not exists domain_brand_verdicts (
  domain          text primary key,            -- lowercased, no www
  verdict         text not null,               -- 'brand' | 'reseller' | 'unknown'
  method          text not null,               -- 'smartscout' | 'shopify_probe'
                                               -- | 'site_llm' | 'agentic' | 'human'
  confidence      text,                        -- 'high' | 'medium' | 'low' (LLM stages)
  evidence        text,                        -- vendor list / quoted page text / sources
  shopify_vendor_count int,                    -- null when not Shopify
  fetch_status    text,                        -- 'ok' | 'empty' | 'error:<class>'
  decided_at      timestamptz not null default now(),
  prompt_version  text                         -- for LLM verdicts; diffable like classifications
);

-- Denormalized verdict on each lead row (audit + export + reviewer UI).
alter table prospeo_new_leads
  add column if not exists brand_verify_result   text,   -- 'brand'|'reseller'|'unknown'
  add column if not exists brand_verify_method   text,
  add column if not exists brand_verify_evidence text;
```

Rejections reuse the existing convention: `rejected=true`,
`agency_filter_reason = 'reseller_site: <evidence>'` — so the reviewer UI,
`collect_stats`, and the export paths need **no changes** to handle them.

---

## 4. Code changes

| File | Change |
|---|---|
| `brand_verify.py` **(new)** | The whole funnel. Public entry: `verify_domains(conn, leads, *, llm_client, on_log) -> dict[domain, verdict]`. Internals: `_cache_lookup`, `_smartscout_confirm` (reuse `rapidfuzz` ≥92 against `smartscout_brands.brand_norm` — same threshold as `smartscout_resolve.py`), `_shopify_probe` (GET `https://{domain}/products.json?limit=250`, filter `VENDOR_NOISE = {route, re:do, redo, xcover, ...}` + same-brand variants via fuzzy match to the company name, count survivors), `_fetch_homepage` (requests, 10s timeout, UA header, 1 retry), `_extract_signals` (BeautifulSoup: title/meta/nav/footer/body text capped ~3k tokens + the 5 deterministic features), `_site_llm_verdict` (one Haiku call, prompt below), `_agentic_verdict` (one messages call with the `web_search` server tool, max_uses=2). Site fetches run in a small `ThreadPoolExecutor` (8 workers); LLM calls stay serial-batched like the existing gate. |
| `prompts/brand_verify.txt` **(new)** | Stage-2 system prompt. Reseller checklist (dealer-of-X language, Shop-by-Brand nav, many distinct vendors, product brands ≠ site name); brand checklist ("we make/manufacture/formulate", single consistent identity); false-positive guards as explicit rules — *a few complementary third-party items ≠ reseller; "become a dealer / wholesale / find a retailer" (brand-side) ≠ "we are an authorized dealer of X" (reseller-side); private-label = brand*. Output JSON `{label, confidence, evidence_quote, primary_signal}`. **No "when unsure prefer brand" rule** — unsure = low confidence = escalate. Versioned `bv1`, stored in `domain_brand_verdicts.prompt_version`. |
| `prompts/brand_verify_agentic.txt` **(new)** | Stage-3 prompt: judge from search results (LinkedIn, Amazon presence, press); same output schema; same guards. |
| `bettercontact_sync.py` | Insert one call after the per-company cap (line ~928), before `_insert_leads`: collect `batch_accepted` unique domains → `brand_verify.verify_domains(...)` → apply verdicts: `reseller` ⇒ move lead to `batch_rejected` with `agency_filter_reason='reseller_site: …'`; `brand` ⇒ stamp `brand_verify_*` columns; `unknown` ⇒ keep accepted but stamped `unknown` (reviewer sees the flag). Counted in `rejected_counts['reseller_site']`. New kwarg `skip_brand_verify=False` mirroring `skip_llm` for tests/backfills. |
| `worker.py` | `RUNNING_STUCK_THRESHOLD_S`: 1h → **3h**. Verification adds real minutes per batch and the sweep would otherwise re-queue a healthy long run after a redeploy. |
| `requirements.txt` | `beautifulsoup4>=4.12` (pure Python — no Dockerfile change). |
| `migrations.sql` | §3 above, appended at the bottom per repo convention. |
| `scripts/reseller_diagnostic.py` **(new)** | Phase 0: run Stages 0–2 read-only over the 558 already-reviewed accepted leads, compare against Victor/Jam's `lead_approval` verdicts, print precision/recall + a per-domain evidence sheet (XLSX). Writes **nothing** to the DB. |

**Deploy compatibility (verified):** runs inside the existing Railway worker
(`Dockerfile.worker`, `python:3.12-slim`). `ANTHROPIC_API_KEY` is already
provisioned there (the ICP gate uses it). The Anthropic `web_search` server
tool needs no extra credential. No new services, no new env vars.

---

## 5. Rollout phases

### Phase 0 — Retroactive diagnostic (go/no-go gate) — DONE, GO
Ran `scripts/reseller_diagnostic.py` twice over 317 accepted + 60
known-reseller domains. Gate met: 0 site-LLM false flags on the accepted set,
75% catch on judged known resellers (most "misses" were the old gate's own
errors). Full results + the two design amendments in §10.

### Phase 1 — Free layer — DONE (2026-06-10)
`brand_verify.py`: domain cache (`domain_brand_verdicts`, applied to the DB)
+ polite Shopify probe with the share rule + vendor-list LLM arbitration of
probe flags + guarded SmartScout confirm. Wired into `bettercontact_sync.py`
after the per-company cap; `reseller` rejects (`agency_filter_reason =
'reseller_site: …'`), `brand`/`unknown` pass through stamped on the new
`brand_verify_*` columns. CLI opt-out: `--skip-brand-verify`. Worker stuck
threshold raised 1h → 3h. Smoke-verified on real domains: aire.com →
reseller, truelinkswear/tuftandpaw (Phase 0 false flags) → brand via
arbitration, cache short-circuits repeat domains.

### Phase 2 — Site fetch + LLM — DONE (2026-06-10)
`_fetch_homepage` / `_extract_signals` / `_site_llm_verdicts` in
`brand_verify.py`, using `prompts/brand_verify.txt` (bv1, unchanged from the
measured Phase 0 run). Polite fetch (4 workers, retry-on-429). Confidence
gating is **asymmetric**: `reseller` acts only on HIGH confidence (a wrong
rejection is a paid-for lead lost); `brand` acts on high or medium; the rest
stays `unknown`. Smoke-verified: archerycountry.com → reseller/high (footer
brand list quoted), bdiusa.com + stryd.com → brand/high, epicsports.com
(bot-blocked 403) → unknown, never guessed. Remaining acceptance item — a
full scrape batch end-to-end on Railway — happens with the first production
batch after merge.

### Phase 3 — Agentic escalation + review routing — ~1 session
`_agentic_verdict` + `unknown`-flag surfacing in the reviewer UI (evidence
column on the batch page). Acceptance: fetch-failed domains get verdicts via
search; unknowns appear with evidence attached.

### Phase 4 — Measure and tune — ongoing
Per-batch log line: domains by stage, verdicts by method, $ spent, unknown-%.
After ~4 batches decide: widen/narrow Stage-3 escalation, revisit Playwright
(only if fetch-failures dominate Stage 3 traffic).

---

## 6. Cost & throughput at 20k leads/month

| | Value | Basis |
|---|---|---|
| Unique domains/month | ~7,000 | ~3 contacts/company, before cross-batch cache |
| Resolved free (Stages 0-1) | ~45-55% | SmartScout 42% verified + Shopify-probe overlap |
| Stage 2 volume / cost | ~3,500 dom × ~$0.01 | ~3-5k input tokens, Haiku, prompt-cached |
| Stage 3 volume / cost | ~700 dom × ~$0.03 | 1 web_search ($10/1k) + 1 Haiku call |
| **Total** | **~$50-80/month** | worst case (everything → Stage 2) ≈ $200 |
| Added batch wall-clock | ~20-40 min per 5k-lead batch | 8-thread fetch pool, 3-8s/domain |
| Human queue | ~150-300 companies/month | the genuinely ambiguous residue |

Comparators: all-agentic ≈ $600/mo (lightweight) to $2,400-3,600/mo (full
loop); status quo ≈ thousands of manual reviews.

## 7. Expected efficiency (estimates until Phase 0 measures them)

- **Automation rate:** ~96-98% of leads decided without a human.
- **Reseller catch rate:** ~90-95% (Stage 1 verdicts are deterministic and
  near-perfect; the error budget is almost entirely Stage 2's LLM precision —
  the number Phase 0 measures).
- **False rejections:** target <2% — uncertain cases are routed to *review*,
  not rejected, because a wrongly rejected brand is a paid-for lead lost.

## 8. Known limitations (honest)

1. **Clean-storefront resellers** (white-label everything, single-brand look)
   beat every automated layer — and skim-reading humans too. Residual escapes
   concentrate here.
2. **Hybrid companies** (manufacture own line AND resell others') are
   genuinely ambiguous → they inflate the human queue, not the error rate.
3. **No-footprint companies** (dead site, no LinkedIn/Amazon presence) end at
   human review by design.
4. Cache staleness: a domain judged once is never re-judged. Acceptable —
   business models rarely flip; add a `decided_at` age-out later if needed.

## 9. What we tell Victor

Nothing quantitative until Phase 0 produces measured precision/recall against
his own verdicts. Then: the measured catch rate, the measured
false-rejection rate, and the list of any resellers found retroactively in
the current accepted set.

---

## 10. Phase 0 results (2026-06-10) — measured, two runs

`scripts/reseller_diagnostic.py`; evidence sheets in
`exports/reseller_diagnostic_20260610_*.xlsx`. Note: `lead_approval` was null
on 549/558 accepted leads (no per-lead human review of this batch exists), so
ground truth = 317 accepted domains (presumed-brand side) + 60 domains the
existing ICP gate rejected as `reseller` (noisy positives), with hand-checks
on every disagreement.

**Verdict: GO.** The Stage 2 site-LLM (`bv1`) met the gate:

- **0 false reseller flags** on the accepted set across both runs (~50
  domains judged per run); unsure cases went to `unknown`, never guessed.
- Known-reseller set: 24/32 of judged domains caught (75%); of the 8
  "brand" disagreements, hand-check shows ~6 are the OLD gate's errors
  (BDI Furniture, Burry Foods, Butler Specialty, Diamond Cosmetics, Blue
  Peak Creative are real brands it wrongly rejected).
- One genuine reseller found in the current accepted set: **aire.com**
  (54% third-party catalog share — NRS, Canyon Coolers, Sawyer…).
- Cost: ~$0.50 total for both runs.

**Fixes the diagnostic forced (already in the script, carried into the build):**

1. **Stage 1a scorer:** `token_sort_ratio`, not the resolver's
   `token_set_ratio` — set-ratio scores token subsets as 100 ("704 Supply"
   matched Amazon brand "Supply").
2. **Stage 1a guards:** the domain must corroborate the matched brand (brand
   "Ayla" can't vouch for aylabeauty.com the retailer), and retailer-vocab
   names (Epic Sports, Archery Country) never auto-pass — they fall to
   Stage 2. False-passes on known resellers: 10 → 2 (both hand-checked as
   actually-legit brands).
3. **Shopify probe share rule:** reseller requires ≥4 third-party vendors AND
   ≥50% of products; an accessory side-shelf (Ariel Rider's helmets/bags)
   no longer flags. Brand-confirm (≤1 vendor) stayed perfect: 87 confirms
   across both runs, zero issues.

**Design amendments (the two things Phase 0 changed in §2/§4):**

- **Probe reseller-verdicts are NOT final.** Each run produced probe
  false-positives from causes no rule anticipates (run 1: accessory shelves;
  run 2: OEM factory names in the vendor field — truelinkswear.com lists
  "Guangzhou Xinhongcheng Outdoor Shoe Company"; tuftandpaw.com uses internal
  codes "ME/MI/TE"). Probe flags route to **LLM arbitration with the vendor
  list attached** (the LLM trivially recognizes factory names). The probe's
  brand-confirm side stays deterministic.
- **Fetch politely; the web is not the problem.** 142/165 fetch failures were
  HTTP 429 — self-inflicted by burst-fetching 377 domains (Shopify's shared
  CDN throttles per-IP). Refetched slowly: 12/12 succeeded. True unreachable
  rate ≈ **6%, not 36-46%** — so Stage 2 coverage is near-complete and
  Stage 3 stays small. Production fetcher: low concurrency, pacing delay,
  retry-on-429 with backoff.
