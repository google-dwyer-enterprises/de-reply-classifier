# Interest Follow-up A/B — Implementation Plan

**Status:** pre-implementation (awaiting sign-off). No code written yet.
**Author:** drafted 2026-06-18.
**Scope:** a *new operational* feature (drafting + experiment), built on top of the existing
read-only reply pipeline and the descriptive follow-up-effectiveness analysis.

---

## 1. Goal & framing

After a lead replies with **interest**, give Jam ready-to-send follow-up variations and
**A/B test** two ways of writing them:

- **Arm A — static templates:** a small, curated, Jam/Victor-approved template library.
- **Arm B — AI-generated:** Haiku drafts 1–3 follow-ups tailored to *that specific reply*.

…then measure which arm gets more **positive next replies / bookings**.

This is for **follow-up after interest, NOT the first reply**. Entry condition = a lead whose
**latest** classification is in the interest set (see §3).

Two deliverables (the user's two asks):

1. **Phase 1 — "Best replies, use this":** a simple, no-jargon page of curated winning
   follow-ups for Jam to copy. The deep-dive `/analytics` page stays separate. This curated
   set *is* Arm A.
2. **Phase 2 — the A/B tool:** pull today's interest replies per client → 1–3 variations each,
   randomized A vs B → Jam sends → measure the winner.

### Why this is better than the existing analysis
`/analytics` is **descriptive** and fights the **warm-lead confound** (many follow-ups went to
already-positive leads). This feature **randomizes** the arm per reply, so the confound is
balanced across arms → the result is **causal** ("AI beats templates by X pts"), which the
descriptive view cannot deliver.

---

## 2. Hard constraint that shapes the design

**The pipeline is read-only — there is no "send via Instantly" code** (verified: `instantly_sync.py`
only pulls; no POST/send path anywhere). Jam sends follow-ups *manually* in the Instantly Unibox;
they come back to us via `sent_messages` (`send_kind='unibox_manual'`).

Consequences (confirmed direction with stakeholder):
- The tool **generates + presents** variations; Jam copies into Instantly and sends.
- To measure the A/B we **record which arm/variation Jam used** (she clicks "Mark sent").
- We do **not** build auto-send (much larger scope: live send API, deliverability, throttling).

---

## 3. Verified building blocks (reuse, don't rebuild)

| Need | Exists | Where |
|---|---|---|
| Today's replies per client | ✅ | `replies(client, reply_timestamp, subject, body, lead_email)` |
| "Interest" detection | ✅ | `classifications(label, classified_at)`; latest-per-reply; `POSITIVE_LABELS=("booked","interested")` |
| Manual follow-ups (what Jam sent) | ✅ | `sent_messages` where `send_kind='unibox_manual'` |
| Outcome attribution (windowed last-touch) | ✅ | `followup_features.ATTRIB_SQL` (first inbound reply after the touch → label → positive/booked, with prior-positive guard) |
| LLM generation idiom | ✅ | `classify.py` / `followup_llm_features.py` batch pattern + `prompts/*.txt`; `ANTHROPIC_API_KEY` present |
| Web app + roles | ✅ | Flask; `scraper`=Jam (`/submit`,`/batches`,`/batch`), `analyst`=`/analytics` |
| Reply-generation / A-B scaffolding | ❌ greenfield | (nothing exists — design clean) |

**Interest entry set (decision needed, §9):** default `{interested, booked}`. `interested_past`
is re-engagement (different message intent) — propose **excluded** from v1. `not_now` excluded.

---

## 4. Data model (new)

### 4.1 `followup_templates` (Arm A library — Phase 1)
```
id              bigserial pk
scenario_key    text not null        -- coarse bucket, e.g. 'interested', 'pricing_ask', 'booked_nudge'
title           text                 -- short label Jam sees
body            text not null        -- the template (supports {first_name},{company} tokens)
subject         text                 -- optional
is_active       boolean default true
approved_by     text                 -- who signed off (Victor/Jam)
source_note     text                 -- e.g. "derived from sent_message 12345, 31% positive"
version         int default 1
created_at      timestamptz default now()
```
Seeding: a read-only script surfaces top-positive-rate real follow-ups (from
`followup_message_features`) **as candidates**; Jam/Victor approve/edit into this table via a
simple admin view (§6). Nothing auto-promotes.

### 4.2 `followup_experiments` (assignments + outcomes — Phase 2)
```
id                   bigserial pk
source_reply_id      bigint references replies(id)   -- the interest reply being followed up
lead_email           text not null
client               text
arm                  text not null check (arm in ('static','ai'))
variations           jsonb not null                  -- [{idx, text, template_id?}] the 1-3 shown
chosen_variation_idx int                             -- which Jam used (null until sent)
chosen_text          text
status               text not null default 'assigned'-- assigned|sent|attributed|skipped
assigned_at          timestamptz default now()
sent_marked_at       timestamptz                     -- when Jam clicked "Mark sent"
sent_message_id      text                            -- linked sent_messages row (confirms real send)
-- outcome (filled by the attribution job)
had_reply            boolean
responded_positive   boolean
responded_booked     boolean
outcome_reply_id     bigint
attributed_at        timestamptz
unique (source_reply_id)                             -- one experiment per interest reply
```

### 4.3 `followup_ab_results` (plain view)
Aggregates `followup_experiments` where `sent_message_id is not null` (per-protocol): per arm →
n_sent, positive-rate, booked-rate, Wilson CI, lift, and a "insufficient data" flag below the
support floor. Same statistical discipline as the analytics report.

DDL lives at the bottom of `migrations.sql`; the view in a `scripts/apply_*` script, consistent
with current conventions.

---

## 5. End-to-end flow

```
interest reply (replies + latest classification ∈ interest set, no experiment yet)
      │  (per-client, per-day filter on the page)
      ▼
[1] ASSIGN arm  — deterministic per source_reply_id (stable hash, 50/50) → insert experiment row
      ▼
[2] GENERATE 1–3 variations (once, persisted in experiment.variations)
      ├─ arm 'static': top N active templates for the scenario (+ {first_name}/{company} fill)
      └─ arm 'ai':     Haiku drafts from the reply text + lead/company + distilled "what works"
      ▼
[3] Jam UI: shows the assigned arm's variations + copy buttons + "Mark sent (this one)"
      ▼
[4] Jam pastes into Instantly & sends  → later syncs into sent_messages (unibox_manual)
      ▼
[5] LINK sent → experiment (daily job): match by lead_email + sent_timestamp ≥ sent_marked_at
      (nearest after; optional fuzzy body match to chosen_text) → set sent_message_id, status='sent'
      ▼
[6] ATTRIBUTE outcome (daily job): reuse windowed last-touch → had_reply / responded_positive /
      responded_booked → status='attributed'
      ▼
[7] RESULTS view/page: arm A vs B positive-rate + booked-rate + Wilson CI + lift + significance
```

Generation is **on-assignment and persisted** (each reply's variations generated once, cached in
the row) — not regenerated on every page load (bounds LLM cost, keeps the UI stable).

Steps [5]/[6] run on the **daily cron** (chained after `run.py refresh`, like the analytics
refresh) via a new `run.py attribute-followup-experiments`.

---

## 6. Web app surface (Flask, `scraper` role = Jam)

- `GET  /followups`                 — the tool: client + date filter; lists interest replies, each
                                       with its assigned arm + 1–3 variations + copy + "Mark sent".
- `POST /followups/<id>/sent`       — record chosen_variation_idx/chosen_text, status='sent'.
- `POST /followups/<id>/skip`       — Jam dismisses (status='skipped', excluded from results).
- `GET  /followups/best`            — Phase 1 "best replies, use this" (curated templates, copy-ready).
- `GET  /followups/templates` +POST — curation admin (approve/edit/activate). Gate to Victor/Jam.
- `GET  /followups/results`         — A/B results. **Analyst role** (it's analysis), or expose to both.

Templates rendered like existing pages (reuse `base.html`, the analytics styling). No new auth
model — reuse `require_role`.

---

## 7. Prompts (Arm B)

`prompts/followup_generate.txt` — system prompt. Inputs per reply: the interest reply's new text,
lead first name, company, client, and a **distilled "what works" block** (concise tone/hook/CTA
guidance pulled from the effectiveness findings). Output: JSON array of 1–3 short drafts, each with
one clear CTA, matched length/tone constraints. Versioned (`FOLLOWUP_GEN_PROMPT_VERSION`), batched
via the existing `classify` idiom. Coercion/validation like `followup_llm_features.coerce_features`.

---

## 8. Measurement & integrity

- **Primary metric:** booked-rate (the real goal). **Secondary:** positive-rate (booked|interested) —
  higher volume, reported alongside since booked is rare.
- **Per-protocol:** only experiments with `sent_message_id` linked count (i.e. actually sent).
- **Randomization** balances the warm-lead confound across arms → causal read. The interest entry
  condition is shared by both arms (controlled), not a confound.
- **Stats:** two-proportion comparison + Wilson CIs; show "insufficient data" until ≥ N per arm
  (support floor, decision §9). No winner declared early.
- **Known limitations (documented in the UI):**
  - Low volume (~489 manual follow-ups/mo) → significance may take **weeks–months**.
  - Lead-replies-from-a-different-email identity mismatch (existing tracker issue) can miss some
    outcomes — same caveat as the current tracker.
  - "Mark sent" depends on Jam; unmarked-but-sent items are recovered by the fuzzy match in [5],
    and unmatched assignments are surfaced as "unconfirmed" (excluded from results).

---

## 9. Open decisions to confirm at sign-off

1. **Interest entry set:** `{interested, booked}` only (proposed), or also `interested_past`?
2. **Primary success metric:** booked-rate (proposed) vs positive-rate as the headline.
3. **Minimum sample / stopping rule:** e.g. ≥30 sent per arm AND ≥15 positives before showing a
   verdict (mirrors the analytics support floor) — confirm thresholds.
4. **Results page audience:** analyst-only, or visible to Jam too?
5. **Template curation UX:** in-app approve/edit page (proposed) vs a one-time reviewed seed +
   edit-in-DB. How many templates / scenario buckets to start with?
6. **Number of variations:** fixed 3 per arm, or 1–3 (proposed: up to 3; static may have fewer if
   the library is small).
7. **Token fill for static arm:** support `{first_name}`/`{company}` substitution? (Recommended.)

---

## 10. Build order (once signed off)

1. **Phase 1:** `followup_templates` table + seed-candidate script + `/followups/best` +
   curation admin. (Ships value fast; seeds Arm A.)
2. **Phase 2a:** `followup_experiments` table + assignment + generation (prompt + Haiku) +
   `/followups` tool + "Mark sent".
3. **Phase 2b:** sent→experiment linking + outcome attribution job (`run.py attribute-followup-
   experiments`, chained on the daily cron) + `followup_ab_results` view + `/followups/results`.
4. Unit tests for the pure logic (arm assignment determinism, template token-fill, variation
   coercion) — per the repo's `tests/` convention.

---

## 11. Cost

Arm B only: Haiku, 1–3 short drafts per interest reply. Interest replies are low-volume → a few
cents/day. Arm A and all measurement are zero-LLM. No new third-party costs.
