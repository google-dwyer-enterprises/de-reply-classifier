# Reply AI Bot — Pending Tasks (meeting-grounded)

**Snapshot:** 2026-06-16 · **Scope:** the cold-reply analysis bot (`de-reply-classifier`) only.
Derived from the Dwyer meeting transcripts + the accuracy audit. Excludes other workstreams
(Atlas cold-calling, Prospeo/waterfall lead scraping, social-video analysis, cold-email copy/strategy bots).

Legend: ✅ shipped · 🔄 in progress · ⏳ pending · 🔮 future

---

## 🔄 In progress — v4 reclassify + update-status
Prompt **v4 merged** (PR #25). Deploying v4 to all ~19k replies (the bulk of live labels were still stale `v2`).
**Bug found + fixed mid-deploy:** `classify.py --reclassify` used a raw `.execute()` (no pagination), and
PostgREST caps one request at 1000 rows — so `--reclassify` had *never* touched more than the first 1000
replies (explains why the bulk stayed `v2` and v3 only reached ~2k). Fixed to use `_paginate_all`
(`classify.py` ~line 808). The corrected full reclassify is **running now** (background).
**Next step when it finishes:** run `python run.py update-status` (materialize v4 labels onto `leads`
+ refresh `lead_status_mv`), then sanity-check the new label distribution and the booked/interested
counts, and confirm the Gap-2 tag promotion still holds. Also **commit the pagination fix**.

---

## ⏳ Pending — these decide whether the numbers are *correct*

### 1. Identity-mismatch / coverage — ✅ SHIPPED (deterministic part)
**Meeting:** Victor described Instantly's cross-campaign opportunity glitch and a booked count that
looked too low. The "~223 blank booked" really = 112 pre-backfill (history gap) + 86 true mismatch
+ 11 no-followup-needed + 10 external-channel.
**Shipped:** `scripts/apply_followup_alias_bridge.py` creates a `lead_email_aliases` view (deterministic
1:1 `thread_id` bridge, restricted to campaigned addresses that are NOT themselves tracked leads → purely
additive, never cannibalizes a displayed row) and rewrites the two `sent_messages` CTEs in
`followup_tracker_mv` (`ranked_outbounds`, `ffup_counts`) to attribute a campaigned address's follow-ups
back to the outcome lead. **25 clean alias pairs; 5 tracked leads recovered (4 booked + 1 not-interested),
0 regressions** (the script aborts+rolls back if any lead loses follow-ups).
**Residual (NOT code-fixable):** the ~112 pre-backfill + ~47 unsynced-workspace leads need the sync
pointed at the other Instantly workspace(s) — operational (needs those API keys), tracked as a follow-up.
**Code:** `scripts/apply_followup_alias_bridge.py`, `lead_email_aliases` (new view), `followup_tracker_mv`.

### 2. Repeat-booker / cross-client detection — ✅ SHIPPED
**Meeting:** Victor — *"one dude… we probably have 20 booked calls from that one dude… track that across everything."*
**Reframe (from investigation):** the signal is NOT one human under many emails (zero named people have 2+
booked emails) — it's one `lead_email` booked across many clients' campaigns. Person-level unification adds
nothing, so it was skipped.
**Shipped:** `scripts/apply_repeat_booker_columns.py` adds two columns to `lead_status_mv` + wrapper:
`"# Clients Engaged"` (distinct, normalized — drops the `Epic`/`EPIC` casing split + the `other` junk token)
and `"Repeat Booker"` (booked + ≥2 clients). Applied: **55 repeat-bookers** flagged (top: `nic@icebeanie.com`
and `jedd@cactusscratcher.com` at 7 clients each), 0 row loss. **Requires a NocoDB meta-sync** to surface
the columns. The flag refreshes with the MV (so it tracks the v4 booked status after `update-status`).
**Known gap:** 30 more booked-multi-client leads have no `lead_contacts` row → invisible in the MV (same
contact-coverage gap as #1's residual; would lift the count to 85).
**Code:** `scripts/apply_repeat_booker_columns.py`, `lead_status_mv` + `lead_status`.

---

## ⏳ Partial — ask mostly satisfied, small gap

### 3. First/last-name population (non-generic mailboxes)
**Meeting:** Victor wanted name-before-email; Hassan's task was to populate names only for non-generic
mailboxes (skip support@/info@). **Current:** Apollo names flow for enriched leads (`lead_contacts` → MV
"First/Last Name"); the reply-lead name-fill was in progress.

### 4. Customer-service dedicated flag
**Meeting:** Victor — *"know if it's a customer service email… separate column."* **Current:**
`customer_service` is a first-class label surfaced in the Status columns (v4 routes support@/info@ acks to
it reliably); there's no standalone boolean column. **What's left (if wanted):** an explicit
`is_customer_service` column in the MV.

### 5. Non-English as a filterable outcome (minor)
Non-English replies fold into `other` (not separately filterable). Would need a distinct label/flag
(taxonomy change). Low priority unless meaningful foreign-language volume.

---

## 🔮 Future / deferred in the meeting

### 6. Live Amazon revenue (Helium10 / Zonbase / Keepa)
**Meeting:** Victor — *"SmartScout data is old… get revenue from Helium 10 or Zonbase."* **Current:**
qualification rides on manually-uploaded (stale) SmartScout exports; no live-revenue integration. Victor
framed this as future.

---

## ⏳ Ops / housekeeping (from our work, not the meetings)

### 7. Phase-3 auto-refresh
Wire `refresh-followup-patterns` (and decide on the paid `llm-followup-features`) into the daily cron so
the follow-up analysis stays current without manual runs.

### 8. Stale-row purge (minor)
`update-status` only upserts (never deletes), so ~21 `lead_status_mv` / ~50 `leads` rows from excluded
senders (sybill.ai, postmaster, etc.) linger. One-time cleanup pass.

### 9. Retire `FOLLOWUP_ANALYSIS_PLAN.md`
Superseded by the shipped deterministic + LLM follow-up-effectiveness work.

---

## 🔮 From the accuracy audit (not the meeting) — optional
### 10. Classifier accuracy ceiling (~82%)
Blind-judge audit (`debug/_acc_audit.py`): v4 ≈ 82% agreement with a Sonnet judge. Remaining
disagreements are gray-zone pairs (`not_now`↔`not_interested`, `wrong_person`↔`no_longer_there`,
`interested_past`). Pushing higher needs a small **human** adjudication pass, not more prompt rounds — do
only if a defensible accuracy figure for Victor is needed.

---

**Priority read:** once the reclassify + `update-status` land, **#1 (coverage)** and **#2 (repeat-booker)**
are the only items that meaningfully move the needle — one makes the numbers complete, the other is the
clearest unbuilt explicit ask. Items 3–10 are polish, ops, and future enrichment.
