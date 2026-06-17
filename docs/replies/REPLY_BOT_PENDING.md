# Reply AI Bot — Status & Pending Tasks

**Snapshot:** 2026-06-16 · **Scope:** the cold-reply analysis bot + the follow-up analytics dashboard.
Legend: ✅ shipped · ⏳ pending (actionable) · 🚫 blocked · 🟡 optional/minor

---

## ✅ Shipped (merged + applied to production)

- **Classifier prompt v4** + **full reclassify** — all 19,086 replies on v4 (fixed a pagination bug where
  `--reclassify` had only ever done the first 1,000; resumable driver `debug/_finish_v4.py`).
- **Identity-mismatch bridge (#1)** — `lead_email_aliases` thread-id bridge in `followup_tracker_mv`
  (`scripts/apply_followup_alias_bridge.py`); recovered 5 tracked leads, additive-only.
- **Repeat-booker (#2)** — `"# Clients Engaged"` + `"Repeat Booker"` (booked + ≥2 clients) on the MV
  (`scripts/apply_repeat_booker_columns.py`).
- **Contact-coverage fix** — `lead_status_mv` now driven by `leads ∪ lead_contacts`
  (`scripts/apply_lead_coverage_fix.py`); **visible booked 278 → 483** (closed the booked under-count),
  2,242 previously-invisible leads surfaced.
- **Customer-service flag** — `"Customer Service"` boolean column (bundled into the coverage swap).
- **Name population** — `backfill_lead_names.py` pulled first/last names from Instantly into
  `leads.first_name/last_name` (1,855 leads), surfaced via name-COALESCE in the MV.
- **Stale-row purge** — 50 excluded-sender rows removed from `leads`.
- **Follow-up analytics dashboard** — `/analytics` plain-English page (`app.py` + `followup_analytics.py`
  + templates). Reply-rate bars, reliability grading, warm-lead caveat, top-3 real examples per pattern,
  timing strip. Plain-language wording pass + run-on text repair done. Re-extracted against v4 outcomes.
- **Auth overhaul** — real `/login` page + sessions, two strictly-separate roles (`scraper` = Jam,
  `analyst` = follow-up dashboard). Env: `SECRET_KEY`, `ANALYST_USERNAME/PASSWORD`.

---

## ⏳ Pending — actionable

### NocoDB meta-sync (user action)
Surface the three new MV columns in the NocoDB per-lead view: `"# Clients Engaged"`, `"Repeat Booker"`,
`"Customer Service"`. Data is already live; this is only NocoDB's schema cache. (Analytics dashboard
needs nothing.)

### LLM provider cost comparison — ✅ SHIPPED (Phase 1)
Bake-off of the cheap tier (Haiku 4.5 vs GPT-5.4 nano vs Gemini 3.1 Flash-Lite) across every LLM feature,
with cost projected to monthly volume (20k leads/mo, ~1,423 replies/mo, ~489 follow-ups/mo). Report:
**`docs/LLM_COST_COMPARISON.html`** + `docs/llm_cost_comparison.csv`; harness in `scripts/llmbench*.py`
(needs `pip install openai google-genai`). Findings: total Haiku ~$151/mo vs nano ~$28 / Gemini ~$37;
company-name resolution → nano is a free quality win; Prospeo filter → keep Haiku (rivals lose 10–20pts);
reply-side features are <$1/mo either way; **brand-verify is the only big lever (~$122→$23–30/mo) but its
quality was NOT compared cross-provider** (multi-step + Anthropic-specific web search).
**Remaining (Phase 2, optional):** a real brand-verify quality test (rebuild its web-search loop per
provider) before chasing that saving; and act on the clean wins (e.g. switch company resolution to nano).

### Railway cron / scheduling — ⏸ now UNBLOCKED, awaiting user's scheduling decision
The cost comparison is done, so the provider/cadence inputs exist. Two items to schedule when ready:
1. **Free daily analytics refresh** — chain `python run.py refresh-followup-patterns` after the existing
   daily `run.py refresh` cron (`railway.json` `startCommand`) so the `/analytics` dashboard self-updates.
   No LLM cost (deterministic extract + view rebuild + HTML regen). The one-line change was prepared in
   PR #34 and **closed at the user's request** to plan scheduling holistically; re-apply when ready.
2. **Paid `llm-followup-features` re-tagging** — periodically tag NEW follow-ups (provider-dependent cost);
   cadence + provider decided by the cost comparison.
(Note: the daily `run.py refresh` — sync → classify → update-status — already runs and already classifies
new replies on Haiku; only the two items above are unscheduled.)

---

## 🚫 Blocked — need credentials we don't have

- **Unsynced-workspace recovery** — residual ~112 pre-backfill + ~47 leads whose outreach lives in other
  Instantly accounts; needs those accounts' API keys.
- **Live Amazon revenue** (Helium10 / Zonbase / Keepa) — qualification still rides on stale manual
  SmartScout uploads.

---

## 🟡 Optional / minor

- **Classifier accuracy ceiling (~82%)** — a human adjudication pass on gray-zone label pairs, only if a
  defensible accuracy figure for Victor is needed (not more prompt rounds).
- **Non-English** as a separately filterable outcome (currently folds into `other`).
- **Source run-on text** — email bodies were stored flattened (newlines dropped without a space). The
  analytics dashboard repairs this at display time (`humanize_text`); fixing at the source would mean
  re-pulling bodies from Instantly — heavy, low value since the display layer handles the visible surface.

---

**Bottom line:** the feature is functionally complete and live. The only build item left is the Phase-3
auto-refresh cron, which is intentionally on hold pending the OpenAI/Anthropic/Gemini cost comparison.
Everything else is a one-time NocoDB sync, blocked on external credentials, or optional polish.
