# Follow-up Tracker — Plan v3 (Hybrid: long-form + wide pivot)

**Owner:** Hassan
**Started:** 2026-05-19 · **Revised:** 2026-05-22 (hybrid architecture applied)
**Status:** Shipped. Two-view hybrid architecture live in NocoDB.

> **Hybrid architecture (post-meeting refinement).** The database stores follow-ups in a **fully dynamic** long-form view — `followup_messages_mv`, one row per `(lead_email × manual outbound)`, **no cap**, ffup_n grows unbounded per lead. NocoDB also shows the spreadsheet-shape wide view (`followup_tracker_mv`) with ffup 1–20 columns plus a `Total Follow-ups` count derived from the underlying data (not capped). So: dynamic in the database, capped in the UI. Anything past ffup 20 is queryable in the messages view; the count column on the tracker flags when this is the case.

**Scope:** Replace Jam's manual "DE Email Master Sheet → Follow Up Tracker" spreadsheet with a NocoDB-backed materialized view that has **the same shape** — one row per lead, paired Date + Body columns for each follow-up position 1–10, plus Jam's NOTE column — **augmented with a new "Last reply from Instantly" column** populated automatically from the API. No more manual upkeep.

> **Scope.** Earlier drafts (since removed) explored two other approaches:
> - Pairing each follow-up with its triggering inbound via `in_reply_to_id` — killed by probing (the field doesn't exist in Instantly's response).
> - Option D: identifying the winning reply per booked lead via Haiku-as-judge over 2-3 candidates — table + selection script ship behind a flag (see Phase 5), refresh wiring intentionally absent until product wants it.
>
> This plan matches Jam's existing spreadsheet shape; removes EOM columns; adds an API-sourced "Last reply" column. No Haiku selection in the daily refresh path.

---

## Phase 0 findings (from probe scripts)

`/v2/emails?lead=<email>` works, exposes manual sends, `ue_type` is a reliable discriminator (1=campaign auto, 2=inbound, 3=manual outbound), `step IS NULL ⟺ ue_type=3`. Verified via `scripts/probe_outbound_v4.py`.

## Probe 0.6: no `in_reply_to_id`

Probe v0.6 confirmed Instantly does NOT expose `in_reply_to_id` (0/26 inbound rows had it). This drove the simplification — instead of identifying a single "winning" reply per booked lead, the daily refresh gives Jam the full follow-up history per lead in a spreadsheet shape she already knows, plus the most recent inbound from the API. **The "which reply converted" question lives behind the Option D feature flag.**

---

## What v3 builds (hybrid architecture)

Two views derived from `sent_messages`:

| View | Shape | Cap | Purpose |
|---|---|---|---|
| `followup_messages_mv` | Long-form, one row per manual outbound | **None — fully dynamic** | Source of truth; future analytics; queryable history |
| `followup_tracker_mv` | Wide pivot, one row per lead | ffup 1–20 visible | Jam's daily workflow (same shape as her old spreadsheet) |

The parent view (`followup_tracker_mv`) has a `Total Follow-ups` column that counts the **actual** ffup count from `sent_messages` (uncapped). When a lead has more than 20 follow-ups, the count column flags it and the extra messages remain queryable in `followup_messages_mv`.

Same shape as Jam's spreadsheet, minus the EOM columns, plus one new column from the API.

```
Instantly API
   │
   ├─── /v2/emails?email_type=received   ───►  replies         (already exists, ~17K rows)
   │                                            │
   └─── /v2/emails?email_type=sent       ───►  sent_messages   (Phase 2 fills it)
                                                │
followup_tracker_2026-05-19.csv  ───►  lead_outcomes (one-time CSV ingest)
                                                │
                       ┌────────────────────────┼────────────────────────┐
                       ▼                                                  ▼
       followup_messages_mv  (long-form)                followup_tracker_mv  (wide pivot)
       one row per manual outbound                      one row per lead
       NO CAP — ffup_n grows unbounded                  ffup 1-20 visible (UI cap)
       columns: Lead Email, Ffup #,                     columns: Client, Email, Campaign, Status,
                Sent At, Subject, Body,                          Qualified, Initial Reply + body,
                Send Kind, Campaign, Client,                     Total Follow-ups (UNCAPPED count),
                Message ID                                       ffup 1-20 Date + Body,
                                                                 Call ffup, NOTE,
                                                                 Last Reply At + last reply
                       │                                                  │
                       └────────────────────────┬─────────────────────────┘
                                                ▼
                                              NocoDB
```

Removed from Jam's spreadsheet (8 columns):
- "End of month ffup (non-moving) Joyce" + "Email sent" (×4 cycles)

Added (1 column):
- "Last reply from Instantly" — timestamp + body of the lead's most recent inbound reply. Auto-populated.

---

## Phase 1 — Schema

### 1.1 Extend `sent_messages` (additive only, same as v2)

```sql
alter table sent_messages add column if not exists ue_type smallint;
alter table sent_messages add column if not exists step text;
alter table sent_messages add column if not exists send_kind text
  generated always as (case
    when step is null then 'unibox_manual'
    when ue_type = 1 then 'campaign_auto'
    else 'unknown'
  end) stored;
alter table sent_messages add column if not exists thread_id text;
create index if not exists sent_messages_send_kind_idx on sent_messages (send_kind);
create index if not exists sent_messages_thread_id_idx on sent_messages (thread_id);
-- in_reply_to_id intentionally absent — Probe 0.6 verified Instantly
-- does not expose this field (0/26 inbound rows had it).
```

### 1.2 `lead_outcomes` table (now MANDATORY — was optional in v2)

In v2 this was display-only. In v3 it's load-bearing — it holds the columns the API doesn't give us (Status, Qualified, NOTE, plus Client/Campaign/Leadlist Source if not already on `leads`).

```sql
create table if not exists lead_outcomes (
  lead_email text not null,
  client text not null,
  campaign text not null default '',
  leadlist_source text,                 -- CSV "Leadlist Source" column
  status_raw text,                      -- CSV "Status" column verbatim, e.g. "Booked", "Asking for Proposal"
  qualified text,                       -- 'Qualified' | 'No' | 'Pending' | null
  note text,                            -- CSV "NOTE (JOYCE)"
  call_ffup text,                       -- CSV "Call ffup" column
  source text not null default 'manual_tracker_csv',
  updated_at timestamptz default now(),
  primary key (lead_email, client, campaign)
);
create index if not exists lead_outcomes_lead_idx on lead_outcomes (lead_email);
```

### 1.3 `followup_tracker_mv` — the wide MV

The MV pivots `sent_messages` from long-form (one row per outbound) to wide-form (up to 10 ffup positions per lead) via conditional aggregates. NocoDB sees a single wide table.

```sql
create materialized view followup_tracker_mv as
with first_reply as (
  -- The lead's first reply (the one that put them in the tracker)
  select distinct on (lead_email)
    lead_email, reply_timestamp, body
  from replies
  order by lead_email, reply_timestamp asc
),
last_reply as (
  -- The lead's most recent reply — the new "Last reply from Instantly" column
  select distinct on (lead_email)
    lead_email, reply_timestamp, body, from_address_email
  from replies
  order by lead_email, reply_timestamp desc
),
ranked_outbounds as (
  -- Manual outbounds ranked by chronological order per lead
  select
    s.lead_email,
    s.sent_timestamp,
    s.subject,
    s.body,
    row_number() over (
      partition by s.lead_email
      order by s.sent_timestamp asc
    ) as ffup_n
  from sent_messages s
  where s.send_kind = 'unibox_manual'
)
select
  lo.client                              as "Client",
  lo.lead_email                          as "Email Address",
  lo.campaign                            as "Campaign",
  lo.leadlist_source                     as "Leadlist Source",
  coalesce(lo.status_raw, l.auto_status) as "Status",
  lo.qualified                           as "Qualified",
  fr.reply_timestamp                     as "Initial Reply Date",
  fr.body                                as "What was their initial reply",
  -- ffup 1
  max(case when ro.ffup_n = 1 then ro.sent_timestamp end)  as "Email ffup 1 Date",
  max(case when ro.ffup_n = 1 then ro.body end)            as "Email FF 1 what we sent",
  -- ffup 2
  max(case when ro.ffup_n = 2 then ro.sent_timestamp end)  as "Email ffup 2 Date",
  max(case when ro.ffup_n = 2 then ro.body end)            as "Sent ff 2",
  -- ffup 3
  max(case when ro.ffup_n = 3 then ro.sent_timestamp end)  as "Email ffup 3 Date",
  max(case when ro.ffup_n = 3 then ro.body end)            as "Sent ff 3",
  -- ffup 4
  max(case when ro.ffup_n = 4 then ro.sent_timestamp end)  as "Email ffup 4 Date",
  max(case when ro.ffup_n = 4 then ro.body end)            as "Sent ff 4",
  -- ffup 5
  max(case when ro.ffup_n = 5 then ro.sent_timestamp end)  as "Email ffup 5 Date",
  max(case when ro.ffup_n = 5 then ro.body end)            as "Sent ff 5",
  -- ffup 6
  max(case when ro.ffup_n = 6 then ro.sent_timestamp end)  as "Email ffup 6 Date",
  max(case when ro.ffup_n = 6 then ro.body end)            as "Sent ff 6",
  -- ffup 7
  max(case when ro.ffup_n = 7 then ro.sent_timestamp end)  as "Email ffup 7 Date",
  max(case when ro.ffup_n = 7 then ro.body end)            as "Sent ff 7",
  -- ffup 8
  max(case when ro.ffup_n = 8 then ro.sent_timestamp end)  as "Email ffup 8 Date",
  max(case when ro.ffup_n = 8 then ro.body end)            as "Sent ff 8",
  lo.call_ffup                                              as "Call ffup",
  -- ffup 9 / 10 (date only in source CSV; we surface body too for completeness)
  max(case when ro.ffup_n = 9 then ro.sent_timestamp end)  as "Email ffup 9",
  max(case when ro.ffup_n = 10 then ro.sent_timestamp end) as "Email ffup 10",
  lo.note                                                   as "NOTE (JOYCE)",
  -- ★ NEW column — the headline v3 feature
  lr.reply_timestamp                                        as "Last Reply At",
  case
    -- Tag Calendar/auto-confirmation emails so Jam knows they aren't real replies
    when lr.from_address_email like '%@google.com'
         or lr.from_address_email like '%calendly%'
         or lr.from_address_email like '%calendar%'
    then '[Calendar invite] ' || left(lr.body, 300)
    else left(lr.body, 500)
  end                                                       as "Last reply from Instantly"
from lead_outcomes lo
left join leads l                  on l.lead_email = lo.lead_email
left join first_reply fr           on fr.lead_email = lo.lead_email
left join last_reply lr            on lr.lead_email = lo.lead_email
left join ranked_outbounds ro      on ro.lead_email = lo.lead_email
group by
  lo.client, lo.lead_email, lo.campaign, lo.leadlist_source,
  lo.status_raw, l.auto_status, lo.qualified,
  fr.reply_timestamp, fr.body,
  lo.call_ffup, lo.note,
  lr.reply_timestamp, lr.body, lr.from_address_email;

-- Required for `refresh materialized view concurrently`. Lead email + client +
-- campaign is the PK of lead_outcomes — guaranteed unique.
create unique index followup_tracker_mv_pk
  on followup_tracker_mv ("Email Address", "Client", "Campaign");
```

**Notes on the MV design:**

- **One row per lead** (joined on lead_outcomes.PK). A lead replying to multiple campaigns from the same client gets multiple rows — matches the existing tracker behavior.
- **Up to 10 ffup positions surfaced.** A lead with >10 manual outbounds: only the first 10 appear (chronologically). A lead with <10: empty cells in the higher slots. Same semantic as Jam's spreadsheet.
- **`status_raw` falls back to `leads.auto_status`** when the CSV is silent. So new leads added after the CSV ingest still get a status from the existing classification pipeline.
- **Calendar/auto-confirmation emails** in the "Last reply from Instantly" column get a `[Calendar invite]` prefix so Jam can spot them at a glance.

### 1.4 Validation queries

```sql
-- After Phase 2 sync runs
select send_kind, count(*) from sent_messages group by 1;
-- Expect: unibox_manual ≈ a few thousand, campaign_auto large, unknown ≈ 0

-- After Phase 3 CSV ingest
select count(*) from lead_outcomes;                          -- expect ~1,231
select qualified, count(*) from lead_outcomes group by 1;    -- Qualified=46, No=57, Pending=14
select status_raw, count(*) from lead_outcomes group by 1 order by 2 desc;

-- After MV creation
select count(*) from followup_tracker_mv;                    -- ≈ count(lead_outcomes)
select count(*) from followup_tracker_mv where "Last Reply At" is not null;
-- expect most rows to have a last-reply (every lead in tracker by def replied at least once)

-- Coverage of ffup positions
select
  count(*) filter (where "Email FF 1 what we sent" is not null) as has_ffup_1,
  count(*) filter (where "Sent ff 5" is not null)               as has_ffup_5,
  count(*) filter (where "Email ffup 10" is not null)           as has_ffup_10
from followup_tracker_mv;
```

---

## Phase 2 — Sync

Four edits to `instantly_sync.py` + `run.py` introduce the outbound (`--type sent`) pass alongside the existing inbound (`--type received`) pass. No `in_reply_to_id` parsing. The actual code is already merged — see `instantly_sync.py` and `run.py` `cmd_sync`.

The existing inbound sync already populates `replies`. The "Last reply from Instantly" column reads from `replies` directly via the `last_reply` CTE — no extra sync work needed for the inbound side.

Backfill recipe (unchanged):
1. 7-day smoke test: `python instantly_sync.py --type sent --days 7`. Measure throughput.
2. Extrapolate to 90 or 180 days.
3. Run full backfill once measured.
4. Incremental runs read `sync_state.last_synced_at` — daily delta small and fast.

---

## Phase 3 — CSV ingest (now MANDATORY for v3)

In v2 this was optional. In v3 it's required — `lead_outcomes` provides the Status / Qualified / NOTE / Call ffup / Leadlist Source columns the MV exposes.

### 3.1 Script: `followup_tracker_upload.py`

CLI: `python run.py upload-followup-tracker original_data/followup_tracker_2026-05-19.csv`

What it does:
- Parse 1,231 rows from the CSV.
- For each, upsert into `lead_outcomes` on `(lead_email, client, campaign)`:
  - `client` (col 1)
  - `campaign` (col 3, stripped)
  - `leadlist_source` (col 4)
  - `status_raw` (col 5) — verbatim
  - `qualified` (col 6)
  - `note` (col 28)
  - `call_ffup` (col 25)
- Skip rows with malformed email addresses (multi-line, no @, etc.) — mirrors `probe_outbound_v4.py:pick_probes()` filter.
- Print a summary: total parsed, total upserted, total skipped (with reasons).

Does NOT insert any messages. Follow-up bodies/dates come from `sent_messages` via Phase 2 sync; the CSV's ffup columns are only used as a sanity check during validation.

### 3.2 Validation

```sql
select count(*) from lead_outcomes;                          -- ~1,231
select qualified, count(*) from lead_outcomes group by 1;    -- Qualified=46, No=57, Pending=14
select count(*) from lead_outcomes where note <> '';         -- ~few hundred
select count(*) from lead_outcomes
  where lead_email not in (select lead_email from replies);
-- expect 0 — every tracker lead should have at least one reply in the existing inbound pipeline
```

### 3.3 Long-term

After Phase 3 runs once, **stop maintaining the CSV**. New leads enter `lead_outcomes` via either:
- A future Phase that derives Client/Campaign/Status from existing tables (`leads`, `classifications`) — recommended.
- Or a manual one-row insert when Jam needs to add `Qualified` / `NOTE` for a specific lead.

The CSV is a one-time historical snapshot, not a living document anymore.

---

## Phase 4 — MV creation + NocoDB wiring

After Phase 1/2/3 are in place:

1. Run the DDL in §1.3 to create the MV.
2. Trigger NocoDB meta-sync (the user must do this in NocoDB — same gotcha as `lead_status_mv` per CLAUDE.md).
3. Hide unused columns in NocoDB if needed (e.g., "Email ffup 10" if no leads have that many ffups yet).
4. Set up filtered views in NocoDB:
   - **All leads** — default.
   - **Active in last 14 days** — `"Last Reply At" >= now() - interval '14 days'`.
   - **No reply since first** — `"Last Reply At" = "Initial Reply Date"`.
   - **Status = Booked** — for the success cohort.
   - **Status = Asking for Proposal** — proposals to chase.

### Wire into `run.py refresh`

```python
def cmd_refresh(args):
    cmd_sync(args)                          # NOW runs both passes (received + sent)
    cmd_refresh_status(args)                # unchanged
    cmd_classify(args)                      # unchanged
    update_status_main()                    # unchanged
    # No refresh call needed — followup_tracker_mv / followup_messages_mv
    # were converted from MVs to regular views for NocoDB v2026 schema-sync
    # compatibility. Regular views recompute on every query.
```

**Note:** The original plan called for `refresh_followup_tracker_mv()` helper
that ran `refresh materialized view concurrently followup_tracker_mv;`. That
helper was removed once both objects were converted to regular views; calling
`refresh materialized view ...` against a regular view errors. If a future
revision converts them back to MVs (e.g. if NocoDB starts supporting MVs),
restore the helper next to the existing `refresh_lead_status()` in `db.py`.

---

## Phase 5 — Validation gate

Spot-check before declaring done:

1. **Pick 10 random leads from the MV** — manually confirm:
   - "Email FF 1 what we sent" matches what's in Instantly Unibox for that lead.
   - "Last reply from Instantly" reflects the actual most recent inbound (eyeball against Instantly).
   - Status/Qualified/NOTE values match the CSV.
2. **Compare counts:** `select count(*) from followup_tracker_mv` should be close to `select count(*) from lead_outcomes`.
3. **Test refresh:** run `refresh materialized view concurrently followup_tracker_mv` — should complete in <30s. If slower, investigate the conditional aggregates (might need an index hint).
4. **Test NocoDB display:** verify the "Last reply from Instantly" column renders correctly with timestamps + body previews. Verify Calendar invites show the `[Calendar invite]` prefix.

---

## Cost

| Item | Cost |
|---|---|
| Sync (Instantly API) | $0 (included in plan) |
| CSV ingest | $0 (local) |
| MV creation + refresh | $0 |
| Haiku scoring | **$0 — not used in v3** |
| **Total v3 spend** | **$0** |

v2 estimated ~$0.10 for Haiku selection. v3 drops that — pure capture + display.

---

## Implementation order

1. ✅ Phase 0 — per-lead endpoint probe (done 2026-05-19).
2. ✅ Phase 0.5 — global-feed probe (done 2026-05-19).
3. ✅ Phase 0.6 — `in_reply_to_id` verification (done 2026-05-20, RED — drove v1→v2 redesign).
4. **Phase 1.1** — `sent_messages` schema additions (~30 min). **No `in_reply_to_id`.**
5. **Phase 1.2** — `lead_outcomes` table DDL (~10 min). **Mandatory in v3.**
6. **Phase 2** — extend `instantly_sync.py` outbound pass + 7-day backfill smoke (~1 day).
7. **Phase 3** — CSV ingest (~2 hr). Mandatory.
8. **Phase 1.3** — create `followup_tracker_mv` + NocoDB meta-sync (~1 hr).
9. **Phase 4** — wire into `run.py refresh` + set up NocoDB filtered views (~30 min).
10. **Phase 5** — spot-check 10 random leads with Jam (~1 hr).

Total: **~2 working days** (down from v2's 3–4 days because Phase 4 collapses from "Haiku-as-judge over 2-3 candidates" to "create wide MV").

Dependencies:
- Phase 1.2 must precede Phase 3 (need the table before inserting).
- Phase 2 + Phase 3 must precede Phase 1.3 (MV reads from `sent_messages` + `lead_outcomes`).
- Phase 1.3 must precede Phase 4 (need MV before wiring refresh).

---

## What's NOT in v3 (and why)

| Feature | Status | Reasoning |
|---|---|---|
| Identify "winning reply" per booked lead via Haiku | **Deferred to v4** | v2 spec'd this (Option D + D2). Useful but separate analysis. Stakeholder review prioritized the spreadsheet-replacement view first. |
| Per-message Haiku scoring (relevance, urgency, technique) | Deferred | $1.50/run. No immediate consumer until winning-reply view ships. |
| Template clustering / leaderboards | Out of scope | Different analytical question. |
| A/B testing infrastructure | Out of scope | Would replace gut-pick template selection with random assignment. Operational change, not code. |
| Causal attribution | Not possible | Email metadata alone can't establish causation. A/B is the only path. |

The deferred winning-reply work ships behind a feature flag: `followup_winning_selection` table is in `migrations.sql`, the selector is `select_winning_replies.py`, and `run.py select-winning-replies` runs it on demand. Refresh wiring into `cmd_refresh` is intentionally absent until product wants it.

---

## Known limitations

### 1. The tracker shape doesn't answer "which reply converted"

v3 gives Jam a richer working spreadsheet, automated and always up-to-date. It does NOT answer the original Victor question *"which of our follow-ups convert?"* That requires either (a) the v2 winning-reply selection work, or (b) randomized A/B testing.

If Victor reviews v3 and says *"this is great but I still want the conversion answer"* — Option D from v2 can be added as a sidecar without disturbing v3's MV. ~6 hours of work, ~$0.10 in Haiku.

### 2. Leads with >20 manual outbounds get truncated in the UI (data preserved)

The wide tracker view shows the first 20 chronologically. Anything beyond ffup 20 isn't visible in that view, but the long-form `followup_messages_mv` keeps every message (no cap). The tracker's `Total Follow-ups` column shows the true count, so consumers can see at-a-glance when a lead has > 20 nudges and dig into the messages view if needed.

### 3. The "Last reply from Instantly" can be a Calendar invite

When a lead books via Calendly without typing a "yes" email, the last inbound is the calendar invite from `<calendar-...@google.com>`. MV tags these with a `[Calendar invite]` prefix so Jam knows it's not a real reply — but the column won't show the lead's last *human* message unless we add a second column for that.

If Jam wants both — current Calendar invite + last human reply — we can split it into two columns ("Last inbound" + "Last human reply"). Minor MV change, ~15 minutes.

### 4. First-backfill volume unknown

Phase 2's 180-day sync could be slow (probe v2 timed out on 90-day in 30s). Phase 2 starts with a 7-day smoke and extrapolates. Same caveat as v2.

### 5. `step IS NULL ⟺ ue_type=3` is empirical, not guaranteed

Verified across 537 rows in Phase 0/0.5. If Instantly attaches `step` to a manual reply someday, those rows would miscategorize as `campaign_auto` and disappear from the ffup columns. Re-verify quarterly.

### 6. Multi-workspace outbound gap (the "booked-with-no-follow-ups" anomaly)

`instantly_sync.py` only syncs ONE Instantly workspace's outbound. Dwyer-Enterprises sends from multiple workspaces (e.g. `dwyer-enterprises.com`, `gotscal...`, `outreachecomm.com` — visible in reply bodies as quoted senders like "Bea Anderson" or "Roxy Davis"). Inbound replies all consolidate to the same support inbox we DO sync, so:

- **Outbound** → only one workspace → `sent_messages`
- **Inbound** → every workspace's replies → `replies`

Investigation 2026-06-03 found 81 booked leads (Cat C in the categorization below) whose reply bodies clearly cite outbounds from *other* sending domains, but have zero rows in `sent_messages`. Those leads' campaign-auto + manual follow-up history exists, just not in our DB.

**Mitigation (current).** `followup_tracker_mv` fills `"Email FF 1 what we sent"` with an explanatory marker for booked leads with `Total Follow-ups = 0`, categorized as:

| Cat | Condition | Marker text |
|---|---|---|
| A | Had a campaign_auto send before reply | `No follow-up needed — replied to initial campaign in this workspace` |
| B | First reply before Aug 7 2025 (pre-coverage) | `Pre-backfill (replies before Aug 7 2025; outbound history not captured)` |
| C | No campaign_auto, reply within coverage | `Outreach sent via different Instantly workspace — outbound history not synced here` |
| D | No reply at all | `Booking via external channel — no reply tracked in Instantly` |

Implementation: `scripts/apply_tracker_with_markers.py` (idempotent CREATE OR REPLACE). Rollback: `debug/followup_tracker_mv_current.sql`.

**Real fix (deferred).** Sync the other workspaces' `sent_messages`. Requires an Instantly API key per workspace (or one with multi-workspace access) and extending `instantly_sync.py` to iterate over workspaces. ~4 hours once the additional credentials are available.

---

## Open questions for sign-off

1. **Column order:** "Last reply from Instantly" at the far right (current mockup) or right after "What was their initial reply"?
2. **Calendar-invite emails in the new column:** show with `[Calendar invite]` prefix (current) or hide entirely and show the last *human* reply instead?
3. **Body preview length:** 3-line clamp (mockup) or full body text?
4. **Timestamp format:** `2026-01-08 14:22 EST` (mockup) or Jam's "Mon, Jan 8, 2026 2:22 PM EST" style?
5. **"Days since last reply"** as an additional sortable numeric column?
6. **Status fallback to `leads.auto_status`** when CSV is silent, vs. leaving null. Recommend fallback — keeps new leads visible without manual sheet entry.
7. **Schedule v4 (Option D winning-reply work) before or after v3 lands?** Recommend after — let Victor see v3 first; he may not need the conversion view once the spreadsheet pain is solved.

---

## Things that have bitten before (carry-over)

- **Instantly's `/v2/emails` does not expose `in_reply_to_id`** (Phase 0.6 finding). Plan accordingly.
- **Right API filter parameter is `?lead=<email>`, NOT `?lead_email=`** — the latter is silently ignored.
- **`send_kind` rule is empirically verified, not contractually guaranteed.** Re-verify quarterly.
- **`sync_state` is row-based, not column-based.** Use the existing `('sent', null)` row.
- **Bodies are HTML.** Reuse `html_to_text()` from `instantly_sync.py:293`.
- **NocoDB schema cache:** any DDL change to `followup_tracker_mv` requires meta-sync.
- **The MV needs a unique index** for `refresh concurrently` to work.
- **Calendar invite emails appear as inbound** with `<calendar-...@google.com>` Message-ID headers. v3 tags them in the new column with a `[Calendar invite]` prefix.
- **Existing `sent_messages` is empty** (0 rows verified 2026-05-19). Schema additions cannot break anything.
- **Conditional-aggregate MVs (the v3 shape) can be slow to refresh** if `sent_messages` grows past ~500K rows. Watch performance. If it becomes a problem, replace the wide MV with a narrow MV (one row per (lead, ffup_n)) and let NocoDB do the pivot client-side.

---

## Related docs

- `FOLLOWUP_ANALYSIS.html` — stakeholder doc (this doc's HTML twin).
- `scripts/probe_outbound_v4.py` — Phase 0 API-shape verification (`ue_type` / `step` / per-lead filter).
- `original_data/followup_tracker_2026-05-19.csv` — Jam's manual tracker (Phase 3 source).
