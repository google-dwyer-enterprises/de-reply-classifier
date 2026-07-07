# Cost Resequencing Plan — Pay Less for the Same (or Better) Leads

**Status:** Track 1 + Track 3 IMPLEMENTED and A/B-verified (2026-06-11,
branch `feat/cost-reseq`). Probes P1/P2 passed including a positive control
(an excluded domain present in baseline results disappears; 500-entry
exclusion lists accepted; title excludes demonstrably filter server-side).
**A/B acceptance scrape on the worst-case stale segments: 3.65 cr/accepted
vs 11.6 before on the same segments (3.2×), dedup share 57% → 25%, and
better than the 4.16 healthy-segment baseline.** Billing discovery: BC
charges for found-but-undeliverable emails (catch-all pages billed with zero
deliverables) — support ticket required (§6 P6).

R3 segment health: IMPLEMENTED (auto-park after 2 consecutive zero-yield
calls ≥10 credits; 30-day auto-retry; smoke-verified park/reset cycle).
R10: deferred after re-verification — the costmap's `_parse_bc_lead` claim
was false (the status check already precedes dict construction) and the
existing-email load is a 4.5k-row query (trivial).

**Track 2 probes P4/P5 RUN (2026-06-11): the two-stage flow works** — 50
Founder/Owner Cosmetics US/CA people for 2 credits with emails withheld;
free QA killed 23/50 pre-payment (incl. 21 duplicate companies BC would
have billed); Bulk Enrich matched 14/15 for 11 credits. **Quality caveat,
measured: only ~55% of Prospeo's verified emails pass MillionVerifier vs
95% for BC-sourced emails.** → Track 2 pivoted to the HYBRID: Prospeo
discovery + QA-before-payment, BC enrichment API as the enricher.
Implementer notes: Prospeo rejects bad filters loudly (`INVALID_FILTERS`);
seniority enum value is `Founder/Owner`; `company_headcount_range` rejected
both documented shapes — omit it and filter size locally.

**HYBRID PILOT RUN (2026-06-12, ~$15 — `scripts/hybrid_pilot_prospeo_bc.py`,
raw output `docs_evidence/hybrid_pilot_20260612.log`). All three unknowns
measured:**

| Unknown | Measured | Verdict |
|---|---|---|
| Prospeo inventory | **60,998** Founder/Owner US/CA people across our 11 industries (single seniority tier) | sustains 20k/mo |
| BC enrichment find-rate | 74/75 emails returned, but only **39 deliverable (52%)**; 28 catch_all_safe, 7 undeliverable | usable-rate 52% |
| Email quality | **37/39 = 94% MV-ok** (bar: 95%) | waterfall quality preserved |
| Billing reality | **67 credits for 74 emails — BC billed the catch-alls and undeliverables** | confirms the §2 billing leak |

**Revised hybrid economics (measured): ~1.9–2.0 BC cr/accepted** (67cr ÷ 39
deliverable = 1.72 cr/usable email, ×~0.94 MV survival, + ~0.15 Prospeo cr
discovery) vs ~2.5–3.5 for optimized BC-only — **~25–35% additional
saving, NOT the 60–70% the docs-based model projected.** The entire gap is
catch-all billing.

**DECISION (2026-06-12): hybrid DEFERRED, not abandoned.** Rationale: (1)
modest measured ROI vs a new orchestration path + second credit pool + a
reversal of the team's BC-standardization decision; (2) the BC support
ticket on deliverable-only billing is the dominant variable — a favorable
answer roughly doubles the hybrid's savings (~1.1 cr/accepted) and is the
trigger to build it; (3) Track 1's production steady-state should be
measured first as the true baseline. Bonus finding for the future build:
the enrichment API returns full firmographics (employee counts,
organization type — fields that are 100% null in Lead Finder), usable as
free ICP inputs.

**The problem in one sentence:** the pipeline was built outside-in ("make it
work, then make it right"), so today **~67% of every BetterContact credit we
spend is burned on leads our own downstream layers then throw away** — and
the fix is mostly resequencing: do free and cheap rejection *before* the
money is spent, not after.

---

## 0. Confidence statement

The waste anatomy (§1–2) is **measured** from production runs. The provider
capabilities (§3) are **documented** (fetched from primary docs and
adversarially re-verified), with two billing nuances flagged as
needs-empirical-probe. The savings projections (§5) are **modeled** from
those measurements and are presented as ranges with explicit assumptions.
Nothing ships without the §6 probes passing.

---

## 1. Where the money actually goes (measured)

Cost classes, biggest first:

| Cost class | Scale | Evidence |
|---|---|---|
| **BetterContact credits** | 2,868 lifetime credits → 572 accepted = **5.0 cr/accepted**; best healthy run 4.16 | `category_scrape_state`, `debug/_fullrun.log` |
| LLM tokens (ICP gate + brand_verify) | ~$0.004–0.02 per company | run logs, token accounting |
| Web searches (brand_verify stages) | ~$0.01/search; ownership search fires on **96% of companies** (299/312 measured) | `debug/_bv2_regression.log` |
| MillionVerifier | 1 cr per *approved* lead only — already optimally placed | code + prod test |
| Human review time | ~36 decided so far; sample too small to model | DB |

**BC credits dominate by ~50–100×.** All LLM + search costs together are
roughly $90–140/month at 20k leads/month; BC spend at the same scale is
thousands. The plan therefore prioritizes credit waste, then token hygiene.

## 2. The waste anatomy of a real run (yesterday's 343-lead production run)

`debug/_fullrun.log` — 1,428 credits, 2,200 leads returned, 1,055 with
deliverable emails (paid), 343 accepted:

| Outcome of PAID leads | Count | % of paid | Preventable how |
|---|---|---|---|
| **Accepted** | 343 | 33% | — |
| **Duplicate of a lead we already own** | 236 | 22% | `company.exclude` / LinkedIn-URL exclude (§3.1), pre-paid |
| ICP LLM gate rejects (reseller/service/agency…) | ~140 | 13% | partially: better server-side targeting shrinks junk volume |
| **Per-company contact cap** | 67 | 6% | suppression of capped companies (§3.1) |
| Title not decision-maker | ~15+ (top-10 visible) | ~2–4% | **server-side `lead_job_title`/`lead_seniority` filters (§3.1)** |
| Prohibited/category/domain/language rules | ~60+ | ~6% | partially (industry mix; junk volume) |
| Other (caps, unknowns, tail) | ~190 | ~18% | mixed |
| **Total discarded after payment** | **712** | **67%** | |

≈ **960 credits of 1,428 wasted in one run.** At the 20k-accepted/month
target this waste class alone is ~25–40k credits/month.

**Two myths this data kills:**
- *"Deep offsets decay"* — refuted: healthy segments were flat at 3.3–5.1
  cr/accepted across offsets 0–250. The 11.6 cr/accepted disaster on 6/11
  came from **exhausted segments + no country filter**, not offset depth.
  The fix is segment health tracking, not offset resets.
- *"No-deliverable leads are free"* — partially wrong: BC's own API example
  shows `credits_consumed: 5.5` for 55 leads (~0.1 cr per returned lead
  slot), and our measured 1.35 cr per stored lead implies the ~1,145
  no-email results were not free. Pinned for the §6 billing probe.

Also measured:
- **Dedup waste is invisible in the DB** (insert conflicts) — only run logs
  show it. At stale segments it reached **57% of paid rejects**.
- **ICP LLM gate: 1,013 judgments for 591 unique companies** (42%
  duplicates), serial, uncached, per-lead.
- Up to **4 web-search calls per domain** worst case in brand_verify
  (vendor arbitration → agentic → ownership → US/CA).

## 3. What the providers actually support (verified against primary docs)

### 3.1 BetterContact — unused server-side weapons

Fetched from `doc.bettercontact.rocks` (Lead Finder POST schema), then
independently re-verified:

- **`lead_job_title.include/.exclude` (+ `exact_match`) and
  `lead_seniority` enum** (`owner, founder, c_suite, vp, head, director,
  manager, …`), plus `lead_department` / `lead_function`. **We filter
  titles only AFTER paying today** (`bc_title_rank` post-filter).
- **`company.exclude`** — "company domains that must NOT match". A real
  suppression mechanism: we can exclude (a) companies already at the
  3-contact cap, (b) every company domain we've ever rejected for
  reseller/MLM/service/prohibited/corporate reasons. **Neither is used
  today.** (Max list size undocumented → probe.)
- **`lead_linkedin_url.exclude`** — contact-level exclusion (we store
  `contact_linkedin_profile_url` in `bettercontact_raw`) → finer-grained
  dedup lever (probe).
- Billing (documented): 1 credit per verified deliverable email; 0 when
  nothing found; catch-all validation free; **phone = 10 credits** (→ task
  #1, phones, must NOT be enabled at scrape time; see §4 R6).
- **CORRECTION (2026-07-08, API-probed): Lead Finder DOES have an email-free
  mode.** An earlier version of this line claimed "no search-without-enrichment
  mode exists (confirmed)" — that was WRONG. A live probe (`enrich_email_address:
  false`, limit 2) returned **2 people with full company firmographics +
  contact name/title/seniority/LinkedIn, `credits_consumed: 0.0`** — only
  `contact_email_address` is empty. So BC CAN do QA-before-payment on its own:
  discover email-free (free) → gate on company revenue/ICP → enrich only
  survivors. The **standalone enrichment endpoint** (`/api/v2/async`: name+company
  → email, 1 cr per found email, 0 if not found) is the enricher for the
  survivors. This means the revenue-first pipeline is buildable on BetterContact
  alone — no Prospeo required. See docs/cost/REVENUE_FIRST_PIPELINE_PLAN.md.

### 3.2 Prospeo — a documented QA-before-payment pipeline (meeting task #5: FEASIBLE)

Fetched from `prospeo.io/api-docs`, adversarially confirmed:

- **Search Person**: 1 credit per request returning up to **25 people —
  explicitly WITHOUT email/mobile** ("This endpoint does not return the
  email and mobile of the persons"). 30+ server-side filters (seniority,
  title, industry, headcount, US/CA, tech, up-to-500-domain lists).
  Duplicate searches within 30 days free.
- **Enrich Person / Bulk Enrich Person (50/batch)**: 1 credit **per matched
  email** — leads without an email found are free; re-enrich within 90 days
  free; `only_verified_email` toggle; mobile = 10 credits (same caution as
  BC).
- Net: **people for ~0.04 credits each, emails only for survivors.** Our
  entire QA stack (dedup, title, category, size, the full brand_verify
  funnel — which needs only the DOMAIN, not the email) can run **before any
  email is paid for**.

## 4. The resequenced design

Principle (same one the QA layers were built on, now applied to money):
**every lead is rejected by the cheapest stage capable of rejecting it — and
ideally before it is ever paid for.**

### Track 1 — stop buying waste (BC credits; the big money)

- **R1 — Server-side title/seniority targeting.** Add
  `lead_seniority.include = [owner, founder, c_suite, vp, head, director]`
  (mirroring `bc_title_rank` tiers) and a `lead_job_title.exclude` list
  built from our measured reject titles (VP of Sales, finance, HR…). Keep
  the local post-filter as backstop. *Caveat (documented gap): docs don't
  explicitly state filtered-out contacts are unbilled — they're not
  returned, and billing is per email in returned results, but §6 P1 proves
  it empirically before rollout.*
- **R2 — Suppression via `company.exclude`.** Before each search, build the
  exclusion list from: companies at contact cap + all rejected-company
  domains for that industry + recently-scraped domains. Send with every
  Lead Finder call. Kills the dedup class (22–57% of paid rejects) and the
  cap class (6%) at the source. §6 P2 probes the max list size; if capped,
  prioritize by historical dupe frequency per industry segment.
- **R3 — Segment health management.** Persist per-call yield
  (credits/accepted, dedup share) per industry segment in
  `category_scrape_state`; auto-park a segment when cr/accepted exceeds 2×
  the trailing healthy median, resume on inventory refresh (BC data
  refreshes; re-entry probe monthly). Default `--country "United States,
  Canada"` stays (measured ~2× better cr/accepted; foreign-sellers remain
  reachable via the policy-encoded funnel if ever scraped deliberately).
- **R4 — Billing truth.** One support ticket + the §6 P1 micro-probe to
  settle: per-returned-lead slot fees, catch-all billing, and whether title
  filters gate billing. Pure information; shapes R1/R2 tuning.

### Track 2 — QA-before-payment (structural; the end-state)

- **R5 — Prospeo two-stage pilot** (meeting task #5): Search Person with
  ICP filters → for each unique company domain run the EXISTING free+cheap
  QA (dedup vs pool, TLD, category rules, **full brand_verify funnel** —
  domain-only, no email needed) → **Bulk Enrich only survivors** → then the
  normal review → MV → pool path. Pilot: ~500 people (≈20 search credits +
  enrich for survivors only, ~$10–20 total) measuring: person coverage vs
  BC for the same ICP, email match rate, overlap/dedup vs existing pool,
  end-quality via the standard audit. Optional: BC enrichment API as
  fallback enricher for emails Prospeo misses (1 BC credit each).
  - Modeled steady-state cost if pilot passes: **~1.3–1.7 credits per
    accepted lead vs 4.16 today** (search ~0.15 + enrich ~1.1–1.3
    amortized + funnel ~$0.02), i.e. **60–70% cheaper per accepted lead**,
    with bad companies never paid for at all.
- **R6 — Phones (meeting task #1) placed post-approval.** Phone = **10
  credits** on both providers. Never enable at scrape time (would ~11× the
  cost of discarded leads). Correct placement: enrich phones like the MV
  gate — only for approved leads entering the pool, via the enrichment
  API; or only for leads that reply (cheapest, smallest set).

### Track 3 — token & search hygiene (second-order; ~$40–90/month at scale)

- **R7 — Free before paid: move the per-company cap BEFORE the ICP LLM
  gate** (today 5 contacts get judged, 3 survive the cap — measured smell,
  `bettercontact_sync.py:889 vs 914`).
- **R8 — ICP gate per-domain + cached + parallel.** 42% duplicate
  judgments measured (1,013 calls / 591 companies), serial loop. Judge once
  per company domain, cache verdicts (same convention as
  `domain_brand_verdicts`), run through the existing 6-worker pool.
  ≥50% gate-cost cut and faster batches.
- **R9 — Search consolidation in brand_verify.** Worst case today: 4
  web-searches/domain. (a) Skip the ownership/size search when SmartScout
  confirmed the brand AND `mlm_signal_hits == 0` AND description carries no
  corporate/consultant markers — SmartScout's own revenue/dominance fields
  serve as the size check (guard: any flag → search anyway). (b) The
  US/CA-presence question stays merged into the ownership call (already
  done). (c) Cache-hit short-circuits all of it on repeat domains (already
  done — keep).
- **R10 — Hygiene.** `_load_existing_emails` full-table scan → indexed
  per-provider query reused for the R2 exclusion list; skip
  `_parse_bc_lead` dict construction for undeliverable rows; share the
  loaded SmartScout norms with the exclusion builder.
- **R11 — Already optimal, do not touch:** MV gate placement (1 cr only on
  approved leads), brand_verify cheap-first internal order, the domain
  verdict cache, polite fetch pacing.

## 5. Savings model (at 20k accepted leads/month)

Assumptions: BC ≈ 84k credits/month at today's 4.16 cr/accepted; credit
price per your plan (worker code says ~$0.20/cr; **confirm actual plan
pricing** — flagged, savings shown in credits so the % holds either way).

| Scenario | cr/accepted | Monthly credits | vs today |
|---|---|---|---|
| Today (healthy segments, measured) | 4.16 | ~84k | — |
| + Track 1 (R1–R3: suppression, server-side titles, segment health) | **~2.5–3.0** | ~50–60k | **−30–40%** |
| + Track 2 (R5 hybrid, if pilot passes) | **~1.3–1.7** | ~26–34k | **−60–70%** |

Track 3 adds ~$40–90/month of LLM/search savings and meaningfully faster
batches — small money, but free to take and improves the 3-hour worker
window headroom.

**Honest bounds:** Track 1's floor is the irreducible post-payment rejects
(LLM-gate junk that no server-side filter expresses, ~10–15%). Track 2's
numbers depend entirely on Prospeo's person coverage and email match rate
for our ICP — that is exactly what the pilot measures, and BC remains the
fallback path unchanged if it fails.

## 6. Probes before any implementation (test-first, in order)

| # | Probe | Cost | Settles |
|---|---|---|---|
| P1 | Two tiny BC searches (limit=10), identical filters ± `lead_seniority`/`lead_job_title`; compare `credits_consumed`, returned titles | ~2–5 credits | Title filters work server-side AND whether billing is gated by them; the per-slot fee question |
| P2 | BC search with `company.exclude` of 50 / 200 / 500 known domains | ~2–5 credits | Exclusion works, max list size, dedup mechanics |
| P3 | BC search incl. `lead_linkedin_url.exclude` of known contacts | ~2 credits | Contact-level suppression viability |
| P4 | Prospeo Search Person ×2 pages with ICP filters; inspect coverage/fields; NO enrichment | ~2 Prospeo credits | Person quality + filter fidelity for our ICP |
| P5 | Prospeo Bulk Enrich 25 QA-passed people; compare emails vs BC for same companies | ~25 Prospeo credits | Email match rate + quality — the R5 go/no-go input |
| P6 | Support ticket to BC re: billing semantics (slot fees, catch-all) | $0 | R4 |

Gate to implement Track 1: P1+P2 pass. Gate for Track 2: P4+P5 + a 500-person
pilot audited with the standard multi-agent QA audit. Every change lands
behind the same regression discipline as the QA layers (the
`qa_audit_labels` ground truth + a fresh-scrape acceptance run).

## 7. Rollout order

1. **Week 1:** Probes P1–P3 + P6 → implement R1+R2+R7+R8 (one PR; R7/R8 are
   pure code, no probe needed) → measured A/B on one production batch.
2. **Week 2:** R3 segment health + R10 hygiene; Prospeo probes P4–P5.
3. **Week 3:** R5 pilot (500 people end-to-end incl. audit) → go/no-go on
   the hybrid as the default path; R9 search consolidation.
4. Continuous: `qa_metrics` + per-batch credit metrics become the standing
   scorecard (credits/accepted printed in every batch email).

## 8. What we deliberately do NOT change

- The QA layers' asymmetric reject policy (wrongly rejecting a paid lead is
  the one waste this plan must never create while killing the others).
- MV placement, the verdict cache, polite fetching.
- The review queue: humans still see everything uncertain, with evidence.
