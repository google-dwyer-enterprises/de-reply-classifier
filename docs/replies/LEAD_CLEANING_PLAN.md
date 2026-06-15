# Lead Cleaning and Scoring — Implementation Plan

Source: Meeting 1 (assignment, ~46:00–52:34), Meeting 3 (follow-up demo, ~28:00–46:30).
Owner: Hassan. Target completion: **2026-05-22**.

---

## Task statement (verbatim from action items)

> **Lead Cleaning and Scoring — Hassan | Due May 22**
> Booked-call count under-reporting (139 vs 200+ for Epic alone). Reading email
> content instead of Instantly tags.
> → Hassan to switch to Instantly tags + add first/last name + reply tracking
> by Thu May 15.

Three concrete deliverables packed in:

1. **Stop under-reporting bookings.** Current pipeline reads reply *content*
   via Haiku LLM. Victor wants it driven off Instantly's per-lead **tags**
   (`Epic Booked`, `Epic Interested`, etc.) which are the source of truth.
2. **First/last name population** on `leads`, with sensible fallback.
3. **Reply tracking** — capture our outbound replies so the follow-up
   analysis ("which of our replies actually converts") can run.

---

## Decisions already locked in

| Question | Decision | Reasoning |
|---|---|---|
| Tag vs LLM precedence | **Tags always win.** LLM is fallback when no tag exists. | Matches Victor's instruction in Meeting 1 ([49:35]). LLM was the source of the under-counting bug. |
| Reply tracking shape | **Separate `lead_replies` table** (or view, depending on Phase 1 audit). | Supports one-lead-many-clients case (the "cactus guy"). Doesn't conflate inbound + outbound semantics in the existing `replies` table. Matches what Victor + Hassan agreed in Meeting 3 ([37:09]–[38:39]). |
| Name source | **Instantly first, Apollo fallback.** No LLM extraction. | Deterministic, free, fast. Long-tail signature extraction deferred. |
| Order of attack | **Tags first, names second, reply tracking last.** | Highest-visibility fix first. Tag fix is independent of the others. Reply tracking has the most unknowns. |

---

## Phase 1 — Investigation (no code yet, ~1–2 hours)

Before writing anything, answer three questions by querying Supabase directly.
Post findings in the channel before starting Phase 2.

### 1a. How big is the 139-vs-200 gap, really?

```sql
-- How many leads does the LLM say are "booked"?
select count(*) from leads where auto_status = 'booked';

-- How many leads does Instantly tag as booked (across all clients)?
select count(*) from replies where lead_status ilike '%booked%';

-- Breakdown by client (to compare against the "Epic 200+" claim)
select
  c.client,
  count(*) filter (where r.lead_status ilike '%booked%') as instantly_booked,
  count(*) filter (where l.auto_status = 'booked')      as llm_booked
from replies r
left join leads l on l.lead_email = r.lead_email
group by client
order by instantly_booked desc;
```

**Blocker check:** if `replies.lead_status` is mostly NULL, `backfill_lead_status.py`
needs to run first (it pulls tags from Instantly's `/leads/list`).

### 1b. Is `sent_messages` already capturing Unibox manual replies?

**RESOLVED (2026-05-19).** Verified empirically:

- `select count(*) from sent_messages` returns **0**. The table exists in `migrations.sql:20` but **nothing writes to it** — `instantly_sync.py` never inserts there, only `backfill_tags.py` references it via UPDATE-by-`campaign_id` (no-op on empty).
- Probed Instantly v2 API: `GET /v2/emails?lead=<email>&limit=100` returns manual outbound sends (`ue_type=3, step=null`) with full HTML bodies. The API **has** the manual-reply data we need.
- Coverage on the 5 booked leads probed: API returned **37 manual outbound sends** vs. **16 logged in Jam's CSV** — API has more.

**Implication:** Branch A is **not** viable as-written (it assumed `sent_messages` already had data). Branch B (build the sync) is now the path — but the work is shared with the separate Follow-up Analysis project. See `FOLLOWUP_ANALYSIS_PLAN.md` Phase 2 — it owns the `sent_messages` sync. Phase 4 here just needs to wait for or coordinate with that work.

### 1c. Where do names actually live?

```sql
-- How many leads need name backfill?
select
  count(*)                                            as total_leads,
  count(*) filter (where l.first_name is not null)    as already_has_name,
  count(*) filter (where l.first_name is null
                    and lc.first_name is not null)    as needs_apollo_fallback,
  count(*) filter (where l.first_name is null
                    and lc.first_name is null)        as no_name_anywhere
from leads l
left join lead_contacts lc on lc.lead_email = l.lead_email;
```

**Phase 1 deliverable:** 5-bullet writeup posted to the channel covering 1a, 1b, 1c results.

---

## Phase 2 — Tag-driven status (the main fix)

The 139→200+ count fix. Highest priority.

### What changes
Insert a tag-driven layer that wins over the LLM in the per-lead aggregation.

### New aggregation rule

For each lead, compute effective status as the most "positive" among:

1. **`replies.lead_status` (Instantly tag)** — highest priority.
2. **`classifications.label` (LLM)** — fallback only when no tag exists.

Priority order, highest to lowest:
`booked > interested > interested_past > not_now > customer_service > not_interested > wrong_person > no_longer_there > unsubscribe > oof > other`

### Multi-status columns

Redefine the existing `status1`–`status4` columns on `leads`:

| Column | Meaning |
|---|---|
| `status1` | Best Instantly tag for this lead (NULL if no tag exists) |
| `status2` | Best LLM classification (NULL if all replies are tagged — tag wins) |
| `status3` | Second-best across both signals (for cross-client visibility) |
| `status4` | Instantly lead-status code — leave as-is (already used per CLAUDE.md) |

This keeps both signals visible when they disagree — that's the actual diagnostic
value Victor wants when an LLM call looks wrong.

### Files to touch

- **`leads_status_update.py`** — modify the per-lead aggregation. Reads from `replies.lead_status` AND `classifications.label`, applies the priority rule.
- **`excel_writer.py`** — `fetch_per_lead_summary` is the shared helper. Update once, both Excel + leads update path benefit.
- **`config.py`** — add `STATUS_PRIORITY` ordered list so the rule isn't hardcoded in three places.

### Invariant to preserve

CLAUDE.md rule: *"Do NOT filter by a single `prompt_version`."* The fix is about
which **signal** wins (tag vs LLM), not about which `prompt_version` to look at.
Continue to take latest classification across all prompt versions for the LLM fallback.

### Verification gate

- Before/after count: `select count(*) from leads where status1 = 'booked' and campaigns ilike '%epic%'` — must come up materially (target: ≥200, vs current 139).
- Spot-check 5 leads where LLM said `other` but Instantly tagged `booked` — confirm they now show `status1='booked'`.
- Spot-check 5 leads where LLM said `booked` and there's no Instantly tag — confirm they still show `status1=NULL, status2='booked'`.
- Spot-check 5 leads where both agree — both `status1` and `status2` populated.

---

## Phase 3 — First/last name population

Smaller scope. Largely deterministic.

### What changes

- **Sync path:** check whether `instantly_sync.py` captures `first_name` / `last_name`. Almost certainly not on `replies` (message-level). May need a new pull from Instantly `/leads/list` (similar to how `backfill_lead_status.py` already does).
- **Aggregation:** `leads_status_update.py` populates `leads.first_name` / `leads.last_name` from:
  1. Instantly lead record (preferred — what the operator actually typed/loaded)
  2. `lead_contacts.first_name` / `lead_contacts.last_name` (Apollo)
  3. NULL (do **not** trigger LLM signature extraction per locked-in decision)

### Edge cases

- Generic prefixes (`info@`, `sales@`, `hello@`, `support@`, `contact@`) — never auto-fill a name. Better blank than wrong.
- Excluded senders (per `config.is_excluded_sender`) — same, leave blank.

### Verification

- `select count(*) from leads where first_name is null` — should drop materially.
- Spot-check 10 leads with names — confirm correct source (Instantly vs Apollo).

---

## Phase 4 — Reply tracking

**Updated 2026-05-19.** Branch A is dead (Phase 1.1b confirmed `sent_messages` is empirically empty). The path forward shares infrastructure with the separate **Follow-up Reply Effectiveness** project (`FOLLOWUP_ANALYSIS_PLAN.md`).

### What that other project builds (and we reuse)

`FOLLOWUP_ANALYSIS_PLAN.md` Phase 2 extends `instantly_sync.py` with an outbound pass:
- Pulls `email_type=sent` from Instantly's v2 API.
- Writes to the existing `sent_messages` table (additive columns: `ue_type`, `step`, `send_kind`, `thread_id`, `in_reply_to_id`).
- Distinguishes campaign auto-sends (`send_kind='campaign_auto'`) from Unibox manual replies (`send_kind='unibox_manual'`).

Once that lands, `sent_messages` will have the data this Phase 4 always needed. **No separate `lead_replies` table required.** The other plan's choice to reuse `sent_messages` is the same call this plan would have made now that we know the table is empty.

### What this phase still has to do

Once `sent_messages` is populated:

1. **Build the joined thread view** `lead_thread_view` for NocoDB:

   ```sql
   create view lead_thread_view as
   select lead_email, sent_timestamp as ts, 'inbound' as direction,
          subject, body, campaign_name, client
   from replies
   union all
   select lead_email, sent_timestamp as ts, 'outbound' as direction,
          subject, body, campaign_name, client
   from sent_messages
   order by lead_email, ts;
   ```

2. **Expose to NocoDB** (trigger meta-sync per `README.md:512–514` guidance).

3. **Verify the "cactus guy" case** — one email replying to multiple clients should produce separate threads per client when grouped by `(lead_email, client)`.

### Coordination with the other plan

| What | Owned by |
|---|---|
| `sent_messages` schema additions (`ue_type`, `step`, …) | `FOLLOWUP_ANALYSIS_PLAN.md` Phase 1.1 |
| Outbound sync (`instantly_sync.py` extension) | `FOLLOWUP_ANALYSIS_PLAN.md` Phase 2 |
| `lead_thread_view` for the NocoDB conversation UI | **This plan, Phase 4** |
| Per-message LLM scoring (`followup_scores`) | `FOLLOWUP_ANALYSIS_PLAN.md` Phase 4 (not needed for Lead Cleaning) |

Order of work: that plan's Phase 2 must land before this plan's Phase 4 can ship. Either Hassan owns both (recommended — same data plumbing), or coordinate sequencing.

**Estimate:** 2 hours **once `sent_messages` is populated** by the other plan.

### NocoDB exposure

A new "Conversation" column per lead expands to show the full thread
(their reply → our reply → their reply → …). Victor's use case: answer
*"which of our replies actually got the booking?"* across all leads at once.

---

## Phase 5 — Integration & validation

### Wire it up

1. Run `update-status` end-to-end. Confirm no regressions on existing flows.
2. Refresh `lead_status_mv`. Trigger NocoDB meta-sync (so Hassan's UI picks up new columns).
3. Compare Epic booked count side-by-side: before vs after.
4. Show Victor the new view in NocoDB.

### Validation gates (in order)

1. **DB-level:** booked-count query returns ≥200 for Epic. Pass/fail.
2. **Cross-client:** "cactus guy" case — lead booking with multiple clients shows correctly in new view. Pass/fail.
3. **Reply tracking:** pick 3 booked leads, manually verify full thread is captured. Pass/fail.
4. **Name coverage:** % leads with non-NULL `first_name` should jump materially. Soft target — agree with Victor what's acceptable.

---

## Sequencing & timeline

```
Phase 1 (Investigation)
   ↓ unblocks
Phase 2 (Tag-driven status) ────────────► main fix
   ↓
Phase 3 (Names) ──────────────────────── independent of Phase 2, parallel-safe
   ↓
Phase 4 (Reply tracking) ──────────── biggest unknown until Phase 1 done
   ↓
Phase 5 (Integration + validation)
```

Phase 1 is the gating item. Phase 2 + Phase 3 could land before Phase 4 if reply tracking turns out to need new infrastructure.

### Realistic estimate (updated 2026-05-19)

Phase 4 now depends on `FOLLOWUP_ANALYSIS_PLAN.md` Phase 2 (the `sent_messages` sync). Two scenarios:

**Scenario A — Phase 4 + Follow-up Analysis plan land together (Hassan owns both):**

| Day | Work |
|---|---|
| 1 | Phase 1 investigation + writeup (this plan) |
| 1–2 | Phase 2 implementation + validation (this plan) |
| 2 | Phase 3 — names (this plan) |
| 3 | `sent_messages` schema + outbound sync (other plan's Phase 1+2) |
| 3 | Phase 4 view + NocoDB exposure (this plan) |
| 4 | Phase 5 integration + demo (this plan) |

Total: **4 working days.** Inside May 22 deadline.

**Scenario B — Phase 4 deferred until other plan ships separately:**

| Day | Work |
|---|---|
| 1 | Phase 1 investigation + writeup |
| 1–2 | Phase 2 implementation + validation |
| 2 | Phase 3 (names) |
| 3 | Phase 5 partial — ship Phase 2+3 without reply-tracking view |

Total: **3 working days** for the tag-fix + names. Reply tracking deferred.

---

## Things to watch out for

- **The `prompt_version` rule (CLAUDE.md key invariant):** don't accidentally filter by a single version in the new aggregation. Take the latest classification across all versions for LLM fallback.
- **Excluded senders:** keep `config.is_excluded_sender` filtering active in the aggregation — don't surface bots / no-reply / internal addresses just because they have tags.
- **NocoDB schema cache:** any DDL change to `lead_status_mv` requires the user to trigger meta-sync in NocoDB. Pure data refreshes don't. (CLAUDE.md.)
- **MV definition lives in Supabase, not in `migrations.sql`:** fetch from `pg_matviews` before any DDL on the MV.
- **The "cactus guy" case:** one email replying to multiple clients. Make sure the per-lead aggregation handles this — `clients` column on `leads` should accumulate all clients where this lead has a tagged status.

---

## Quick links to relevant existing code

- `replies.lead_status` / `replies.lead_status_code` — already on the table (see `migrations.sql`).
- `backfill_lead_status.py` — pulls Instantly per-lead `interest_status` into `replies.lead_status`. May need to run before Phase 2 if Phase 1 shows `replies.lead_status` is sparse.
- `leads_status_update.py` — current per-lead aggregator. The main edit point for Phase 2.
- `excel_writer.fetch_per_lead_summary` — shared helper used by both Excel export and `leads_status_update`. Updating it once propagates.
- `name_extraction.py` / `backfill_names_from_replies.py` — LLM signature extraction. Out of scope per locked-in decision but available if scope changes.
- `instantly_sync.py` — message pull from Instantly. May need extension for Phase 4 Branch B.
- `migrations.sql` — bottom of file is the natural place for any new DDL.

---

## Status

| Phase | Status | Notes |
|---|---|---|
| 1a — Booked count gap | ⏭️ Not started | Run the audit queries; post findings |
| 1b — `sent_messages` audit | ✅ Resolved 2026-05-19 | Empirically empty (0 rows); API has the data. See `FOLLOWUP_ANALYSIS_PLAN.md` |
| 1c — Name source audit | ⏭️ Not started | Run the audit query; post findings |
| 2 — Tag-driven status | ⏭️ Blocked by Phase 1a | The main fix for 139→200+ |
| 3 — Names | ⏭️ Blocked by Phase 1c | Independent of Phase 2 once 1c done |
| 4 — Reply tracking (view) | ⏭️ Blocked by `FOLLOWUP_ANALYSIS_PLAN.md` Phase 2 | Needs `sent_messages` populated first |
| 5 — Integration + validation | ⏭️ Blocked by Phases 2–4 | Final gate before demoing to Victor |
