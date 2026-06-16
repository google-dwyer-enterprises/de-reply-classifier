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

### Phase-3 auto-refresh cron — ⏸ BLOCKED on an LLM cost comparison
Wire `refresh-followup-patterns` into the daily job so the `/analytics` dashboard self-updates (today it's
manual; `update-status` for the NocoDB lead view is already in the daily `run.py refresh` cron).
**Why not yet:** the recurring refresh would include the paid LLM follow-up tagging
(`llm-followup-features`), so we're first doing a **cost comparison across OpenAI / Anthropic / Gemini**
to pick the provider before committing to a recurring spend. Build the cron once that decision is made.

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
