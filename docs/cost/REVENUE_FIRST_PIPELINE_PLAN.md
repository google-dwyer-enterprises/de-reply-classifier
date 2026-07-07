# Revenue-First Pipeline — Implementation Plan (BetterContact)

Operationalizes Track 2 of `COST_RESEQUENCING_PLAN.md` + the Amazon Revenue QA
bot. Prompted by Victor's 2026-07-08 Loom: "the limiting factor is finding
right-revenue ICP leads — check revenue first, enrich only matches, so cost is
Rainforest-heavy not BetterContact-heavy."

**This is buildable on BetterContact ALONE — Prospeo is off the table and not
needed.** An earlier version of this plan used Prospeo (reverted). A live probe
(2026-07-08) proved BetterContact does everything required.

## What "fixed" means
Stop paying to enrich an email on leads we'll reject. Discover the company +
person for **free**, revenue/ICP-gate the company (no email needed), and pay to
enrich the email (~$0.049) **only on survivors** — shifting spend to (cheap)
Rainforest and cutting the measured ~67% enrichment waste.

## Probe evidence (live, 2026-07-08)
1. **Email-free discovery works, unbilled.** `POST /lead_finder/async` with
   `enrich_email_address: false` returned people with full company firmographics +
   `contact_first_name/last_name/job_title/seniority/linkedin` and
   **`credits_consumed: 0.0`** — only `contact_email_address` empty.
2. **Standalone enrichment works.** `POST /api/v2/async` with
   `{data:[{first_name,last_name,company,company_domain}], enrich_email_address:true}`
   → poll `GET /api/v2/async/{id}` → returned `contact_email_address` +
   `contact_email_address_status`, **`credits_consumed: 1`** (0 if not found).
3. **Round-trip verified end-to-end** (discover free → enrich that exact person
   → email returned for 1 credit).

This corrects `COST_RESEQUENCING_PLAN.md` which had claimed "no
search-without-enrichment mode exists (confirmed)" — that was wrong.

## Target order (all BetterContact + existing gates)
Lead Finder `enrich_email_address=false` (free) → parse people/companies →
dedup → `_post_filter` (rule/category/domain/size) → `_gate_per_domain` (ICP LLM)
→ `brand_verify` (domain) → `amazon_revenue_qa` (Rainforest, company) →
**enrich survivors only** via `enrich_contacts` (`/api/v2/async`) → keep
deliverable/movable emails → write → review → MillionVerifier → pool.

## Status
- [DONE] `COST_RESEQUENCING_PLAN.md` correction (§3.1).
- [DONE] **Primitive 1** — `_submit_search(enrich_email_address=True)` param
  (default True = classic; False = free email-free discovery). Backward-compatible.
- [DONE] **Primitive 2** — `enrich_contacts(leads, api_key)` — standalone
  `/api/v2/async` enricher (name+company → email, 1 cr per found), probe-verified.
- [DONE] **Orchestration** — `_run_category_revenue_first` + `_parse_bc_person`
  (email-free parse), behind `--revenue-first` (opt-in, default off). Discover
  (enrich_email_address=false, free) → `_post_filter` → per-company cap →
  `_gate_per_domain` → `brand_verify` → `amazon_revenue_qa` (reject DROP) →
  `enrich_contacts` survivors only → `_insert_leads`. Requires `--max-credits`.
  Offline-validated (syntax/import/flag-guard/dry-run/tests). NOT deployed.
- [IN PROGRESS] **Phase 4 validation** — small live run: measure cr/accepted, email
  match rate (BC ~52% deliverable / ~94% MV-ok measured), quality via the
  standard audit; A/B vs classic `_run_category`; then default + set
  `AMAZON_QA_ENFORCE`.

## Orchestration design (for the next build)
1. Round-robin `BC_INDUSTRIES` (reuse `_load_state`/offsets), `_industry_filters`.
2. Per page: `_submit_search(..., enrich_email_address=False)` → `_poll_for_result`.
3. Parse each BC row into a no-email lead dict (new `_parse_bc_person`: company
   fields + `contact_*` name/title/domain, **no** email requirement).
4. Gate (no email): `_post_filter` → `_gate_per_domain` → `brand_verify.verify_domains`
   → `amazon_revenue_qa.qa_companies` (own Rainforest budget). Reject at each.
5. `enrich_contacts(survivors)` → attach `contact_email_address` + status; keep
   only `deliverable` (and decide on `catch_all_safe`, per MV policy).
6. Write via the existing insert path (`scrape_mode='category'`, provider
   `bettercontact`), mark state, checkpoint per page.
7. Budgets: discovery is free (no reservation needed — simpler than classic);
   cap enrichment (Prospeo-style) + Rainforest separately.

## Cost impact (projected — validate in Phase 4)
Current ~$4,300–4,500/mo at 20k (BC ~90%). Revenue-first enriches only ~⅓
(survivors) → BC email spend drops sharply; Rainforest rises but stays cheap.
`COST_RESEQUENCING_PLAN` modeled ~1.3–1.7 cr/accepted vs 4.16 — measured hybrid
saving was ~25–35% (BC catch-all billing eats part), so set expectations there.
