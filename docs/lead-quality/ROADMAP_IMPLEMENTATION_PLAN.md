# Roadmap Implementation Plan — Closing the Audit Gaps, Fully Automated

**Context:** the QA audit (see `QA_LAYERS_REPORT.html` §6) left six roadmap
items. This document answers two questions for each: **is it doable with zero
manual effort inside the existing deployed worker**, and **exactly how to
build it** in this codebase. The pipeline runs unattended on the deployed
worker (`worker.py` → `bettercontact_sync.main()` → `brand_verify.verify_domains()`),
so every design below must run inside that loop with no human step.

## Feasibility summary (read this first)

| # | Enhancement | Fully automatable? | Effort | Net new monthly cost @20k leads |
|---|---|---|---|---|
| 1 | US/Canada gate | **Yes** — deterministic + existing LLM stages | Small (~half a session) | ~$0–5 |
| 2 | Ownership & true-size check | **Yes** — one web-search call per new company | Small–medium (~1 session) | ~$60–100 |
| 3 | MLM detector | **Yes** — rule layer + existing site read | Small (~half a session) | ~$0 |
| 4 | Widened site check (category, name-match, DTC store) | **Yes** — prompt + schema change to the existing Stage 2 call | Small (~half a session) | ~$0 |
| 5 | Scheduled full-pool audit | **Yes, with porting** — the one item that is NOT automatic today (the 312-site audit ran as a one-off multi-agent session, not pipeline code) | Medium (~2 sessions) | ~$30–200 per run depending on sampling |
| 6 | Learn from review decisions | **Yes** — the labels are produced by clicks Jam/Victor already make; the learning side is queries + a report | Medium (~1 session) | ~$0 |

**Bottom line: all six are doable with zero added manual effort.** Items 1–4
are direct extensions of code that already runs on every batch. Item 5 needs
a real port (honest caveat below). Item 6 requires no new human behavior —
it harvests decisions the reviewer app already records.

One design rule carries through everything, inherited from the measured
phases: **uncertain never auto-rejects.** Every new gate resolves to
accept / reject-with-evidence / `unknown`→review-queue. A wrongly rejected
lead is scraping money burned (~$0.40/lead), so rejection always requires
hard evidence or high confidence.

---

## 1. US/Canada gate

**Gap it closes:** 12 foreign-HQ leads (Ksubi/AU, Manukora/NZ, Wild/UK…).
These passed because the language filter intentionally keeps English-site
foreign brands; criterion #4 is stricter.

**Why it's automatable:** three independent signals are already in data we
hold or fetch, no human judgment required:

1. **BC country fields** (already in `bettercontact_raw`):
   `company_address_country`, `company_head_quarters_country`,
   `contact_location_country`. Caveat measured in the audit: occasionally
   *wrong* (Animals Like Us said "Germany", is NZ) — so this is a signal,
   not a verdict.
2. **The homepage Stage 2 already fetches**: currency symbols/codes (£, €,
   AUD, NZD), footer addresses, `.com.au`/`.co.uk` links, VAT mentions.
   Zero extra fetch cost — the HTML is already in hand.
3. **The Stage 3 / ownership web search** (for the few that stay unclear):
   "Where is <company> headquartered?" is exactly what those searches
   surface.

**Implementation:**

- `_parse_bc_lead` (`bettercontact_sync.py`): lift the three country fields
  onto the lead dict (they're already inside the raw JSON it stores).
- New deterministic pre-check in `_post_filter`: if ALL present country
  fields agree on a non-US/CA country → reject
  (`agency_filter_reason='foreign_hq:<country>'`). If fields are absent or
  conflicting → pass through, let the site signals decide.
- `brand_verify._extract_signals`: add a `geo_signals` feature (regex over
  the already-fetched HTML: currency codes, country TLD links, postal-format
  hints) and pass it to the Stage 2 prompt.
- `prompts/brand_verify.txt` (bump to `bv2`, see item 4 — do these together):
  add a `hq_country_guess` + `hq_confidence` output field.
- Verdict application in `bettercontact_sync`: foreign + high confidence →
  reject; foreign + medium/low → `unknown` flag (reviewer sees "possibly
  foreign — check"). US/CA or undetermined → pass.

**Regression test before go-live:** run against today's labeled set — must
flag ≥10 of the 12 known foreign leads and 0 of the 478 passes.

---

## 2. Ownership & true-size check

**Gap it closes:** 14 corporate-owned/oversized leads (Briogeo→Wella,
PrettyLitter→Mars, Pura Vida ~1,000 employees while BC said "small").
Scraped size data demonstrably lies; ownership isn't in scraped data at all.

**Why it's automatable:** the audit found every one of these with a single
web search ("<company> acquired parent company employees"). The pipeline
already makes web-search calls (Stage 3, ownership confirm) with the same
SDK and key — this is one more call of the same shape.

**Implementation:**

- New function `_ownership_size_check(entries, on_log)` in `brand_verify.py`,
  modeled directly on `_confirm_reseller_flags` (search → strict-JSON
  verdict → asymmetric gate). New prompt
  `prompts/brand_verify_ownership_size.txt`:
  - Output: `{parent_company: str|null, independence: "independent"|"subsidiary"|"unknown",
    employees_estimate: "micro"|"smb"|"mid"|"enterprise"|"unknown", confidence, evidence_quote}`.
- Run it once per **new** domain (cache the result in `domain_brand_verdicts`
  — add columns `parent_company text`, `size_estimate text` via
  `migrations.sql`). Cached domains never re-pay.
- Verdict policy (needs one decision from Victor, encode as config):
  - `subsidiary` of a major parent + high confidence → reject
    (`corporate_owned:<parent>`) **or** route to review — default to
    **review** until Victor sets the line (Back to Nature/Barilla proved
    this is a judgment call).
  - `enterprise` size + high confidence → reject (`too_large`).
  - anything else → pass, evidence stamped.
- Cost control: this is the one new per-company web search (~$0.01–0.015
  each ≈ $60–100/month at 6k new companies). Optional optimization: fold the
  ownership/size question into the Stage 3 agentic prompt for domains that
  already get a search, so only Stage-1/2-resolved domains need the extra
  call (~40% saving).

**Regression test:** must flag ≥12 of the 14 known corporate/oversized leads,
0 of the 478 passes (Vetnique-style sub-brand owners must not trip it).

---

## 3. MLM detector

**Gap it closes:** 7 MLM leads (Seacret, Stella & Dot, L'BRI, BELLAME).

**Why it's automatable:** MLMs self-identify loudly — "join", "become a
consultant", "host rewards", "find a consultant", income-disclosure pages.
Two cheap layers catch them:

**Implementation:**

- **Rule layer** in `bettercontact_sync.rule_classify`-style pre-check (or a
  small function in `brand_verify`): regex over `company_description` +
  `company_keywords` for MLM vocabulary (`consultant`, `distributor
  opportunity`, `downline`, `host rewards`, `income disclosure`,
  `direct selling`). Hit → flag for the site check (don't reject on
  description alone — we proved descriptions lie).
- **Site layer**: add `mlm_signal_hits` to `_extract_signals` (the nav/footer
  links are already extracted — "Join", "Become a Consultant", "Find a
  Consultant" links are unambiguous) and an `is_mlm` output field to the
  `bv2` prompt. MLM + high confidence → reject (`mlm_direct_sales`);
  medium → review.

**Regression test:** 4/4 known MLM companies flagged, 0 false positives on
the 478 (watch ambassador/affiliate programs — Pura Vida-style "brand
ambassador" marketing is NOT an MLM; the prompt must distinguish
sales-downline structure from influencer programs).

---

## 4. Widened site check (category + name-consistency + real DTC store)

**Gap it closes:** service/dealer-only businesses (EverFur, Marge Carson — 6
leads), name-mismatch resellers (Victor's "all three things match" rule),
plus a second category check against the live site instead of scraped data.

**Why it's automatable — and nearly free:** Stage 2 already fetches every
undecided company's homepage and pays for an LLM judgment on it. Widening
what that one call answers costs ~0 extra tokens of input and ~40 extra
output tokens.

**Implementation:**

- `prompts/brand_verify.txt` → **`bv2`** (bump `PROMPT_VERSION` in
  `brand_verify.py`; old verdicts stay diffable via the cache's
  `prompt_version` column — same convention as the classifier prompts).
  Add output fields:
  - `category` + `category_status` (`approved|banned|out_of_scope|unclear`)
    — reuse the audit's category lists verbatim in the prompt.
  - `name_match` (`pass|mismatch|unclear`) — site branding vs company name
    vs domain.
  - `sells_online` (`yes|no|unclear`) — cart/checkout present vs
    catalog-only/dealer-locator/service-booking.
  - (plus `hq_country_guess` from item 1 and `is_mlm` from item 3 — ship all
    four in one `bv2` bump, one regression run.)
- Verdict application: `banned` category + high → reject; `out_of_scope` +
  high → reject; `sells_online=no` + high → reject (`no_dtc_store`);
  `name_match=mismatch` → **review, never auto-reject** (mismatch can mean
  parent-company branding, e.g. Endangered Species Chocolate at
  chocolatebar.com — found in the audit's review pile).
- One structural note: Stage 2 currently runs only for domains the free
  layer didn't decide. A Shopify-confirmed brand skips the site read, so it
  would skip these checks too. Fix: for Layer-1-confirmed brands, still run
  the (cheap) fetch+`bv2` call but apply only the non-reseller fields. Adds
  ~$15–20/month at 20k leads; without it, ~half the pool gets the new checks
  only via the scheduled audit (item 5). Recommended: include it.

**Regression test:** the 6 known not-DTC leads flagged; chocolatebar.com
lands in review (not rejected); 0 new false rejections on the 478.

---

## 5. Scheduled full-pool audit

**Gap it closes:** drift — anything that changes after acceptance (site
pivots, acquisitions) and anything novel that slips every gate.

**The honest caveat:** today's 312-site audit ran as a **one-off multi-agent
session driven interactively** — that exact mechanism is not a deployable,
zero-touch job. To make it automatic it must be ported to pipeline code.
This is fully doable because all the building blocks already exist in
`brand_verify.py` (polite fetch, signal extraction, `bv2` full-criteria
judgment, web-search fallback) — the port is essentially "run the widened
funnel over every accepted domain, compare new verdicts to stamped ones,
report changes."

**Implementation:**

- New module `qa_audit.py`: iterate accepted `prospeo_new_leads` (or
  `lead_contacts`) domains in batches; for each, re-run
  `brand_verify.verify_domains` with a `force_refresh=True` flag (bypass the
  cache read, still write back) and the full `bv2` checks; diff against the
  previous `brand_verify_*` stamps; collect changes.
- Output: a change-report email via the existing `notifier.py` (it already
  sends batch-ready emails) + flagged rows set to `unknown` so they surface
  in the reviewer queue. No human needed unless something changed.
- Scheduling on the existing infra: add a second cron service (the repo
  already has the `railway.json` cron pattern for `python run.py refresh`)
  → `python run.py qa-audit --sample 0.25`. Monthly full pool ≈ $180–200,
  or weekly 25% sample ≈ $50/month. Sampling is fine: the per-domain cache
  means a full pass only re-pays for re-judged domains.
- Cache aging belongs here too: re-judge any domain whose verdict is older
  than N months (add `where decided_at < now() - interval '6 months'`).

**Acceptance:** first scheduled run completes unattended within the worker's
stuck threshold and produces a change report with zero human input.

---

## 6. Learn from review decisions

**Gap it closes:** no measurement loop — today we know the layers' accuracy
only because of one-off audits.

**Why this needs zero NEW manual effort:** the labels already exist as a
by-product of normal operation. Every time Jam/Victor approves or rejects a
lead in the reviewer app, `prospeo_new_leads.lead_approval` records a human
verdict right next to the machine's `brand_verify_result`. Nobody has to
label anything extra — the work they already do IS the training signal.

**Implementation:**

- New table `qa_metrics` (`migrations.sql`): per batch —
  `scrape_request_id, layer, verdicts, human_agreements, human_overrides,
  computed_at`.
- Extend `worker.py`'s `finalize_request_if_done` (or the finalize sweep):
  when a batch reaches `status='moved'` (fully decided), compute the
  confusion matrix: machine said brand & human approved (true accept),
  machine said unknown & human rejected (review caught it), human rejected a
  machine-passed lead (**escape** — the number that matters), etc. Insert
  one `qa_metrics` row. Fully automatic — it triggers off the status
  transition that already happens.
- Surface: add the per-layer accuracy line to the existing batch-ready/done
  email, and (optionally) a small MV for NocoDB so the client sees quality
  trends.
- Acting on it stays semi-automatic by design: when escapes cluster (e.g.
  three foreign-HQ escapes in a month), that's the signal to tune a
  threshold or prompt — a code change with a regression run, not a manual
  review burden.

**Acceptance:** after the next two production batches, `qa_metrics` has rows
and the email reports per-layer agreement — with nobody having done anything
new.

---

## Build order and the regression gate

Ship in two PRs:

1. **PR 1 — the `bv2` bundle (items 1+3+4) + item 2.** One prompt bump, one
   schema migration, one regression run against today's labeled 553
   (478 pass / 39 fail / 36 review — persist these labels into a
   `qa_audit_labels` table first so the ground truth survives the
   quarantines). Gate: catches ≥ 90% of the known fails, 0 false rejections
   on the known passes.
2. **PR 2 — items 5+6.** The audit port + cron + metrics. Gate: one
   unattended scheduled run end-to-end.

Total new steady-state cost: **~$90–140/month** on top of the current
~$25–40 funnel cost (item 5's monthly run included at the sampled rate) —
still under 2% of the scraping spend, with every layer's accuracy now
measured continuously instead of by one-off audit.
