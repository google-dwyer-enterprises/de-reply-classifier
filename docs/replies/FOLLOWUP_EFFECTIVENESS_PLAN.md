# Implementation Plan — Descriptive Cross-Lead "Which Follow-ups Are Working" Analysis

**Deliverable:** a descriptive cross-lead analysis of manual follow-up effectiveness, surfaced as (a) a NocoDB plain view `followup_patterns_mv` and (b) a committed HTML report `docs/replies/FOLLOWUP_EFFECTIVENESS.html`.
**Status:** ✅ **Phase 0/1 SHIPPED** (deterministic features). Phase 2 (LLM hook/tone/CTA) pending.
**Snapshot facts used below are verified against live data via a 57-agent code+DB pass and an adversarial methodology review; the review's fixes override the raw design where they conflict.**

> **⚠️ As-built divergences from this plan (the shipped code + `FOLLOWUP_EFFECTIVENESS.html` are authoritative):**
> - **Reverse-causality (§3.4/§5.1):** the shipped view does NOT exclude `prior_positive_exists` rows from the primary rate (option (a) below). Excluding them collapsed positives 323→16 and killed statistical power, so the view keeps them and surfaces **"Lead Already Positive"** as its own characteristic instead (the confound is made *visible*, not hidden). The embedded SQL below still shows the `and not prior_positive_exists` clause — that clause was dropped in `scripts/apply_followup_patterns_view.py`.
> - **Greeting/signoff features (§4.1):** authored fresh as `_GREETING`/`_SIGNOFF` in `followup_features.py`; they do NOT reuse `classify._SIG_CUT_PATTERNS` (that set is a signature-truncation list with no greeting detector).
> - **Gap-4 runbook (§5.4):** shipped at `docs/reference/ADDING_A_NOCODB_VIEW.md` (not `docs/replies/`).

---

## 1. Goal & Scope

**Goal.** Answer, descriptively, *"which characteristics of our manual follow-up messages are associated with a positive reply?"* across all leads — not per lead. Surface it in NocoDB (for the client) and as a self-contained HTML report (for internal review first).

**Explicitly descriptive, not causal.** We report *associations* (lift), never "X causes more bookings." This is a decision already made, not a hedge.

**In scope (this plan only):**
- The cross-lead aggregate (a new per-message base table + a plain NocoDB view).
- The committed DB→HTML report generator.
- The NocoDB-view procedure doc (Gap-4 runbook).
- A deterministic (zero-LLM) Phase-1 headline that ships first.

**Out of scope (do NOT build here):**
- **Gap 2 (booked-via-tag attribution)** — confirmed queued NEXT, not in this plan.
- **Causal / A-B / experimental** attribution of any kind.
- **Score weights, revenue figures, the CS column** — none touched.
- **Model-tier choice (Haiku / Sonnet / Opus)** for the LLM feature pass — **DEFERRED**. The LLM is needed in exactly one place (Phase 2 body-feature tagging); we mark it `MODEL_TIER = TBD` and gate it. The deterministic headline ships without any model decision.

**Invariants preserved (hard requirements):**
- Latest classification per reply is chosen by `classifications.classified_at DESC` — **never** filter on a single `prompt_version` (mirrors `select_winning_replies.py` and the CLAUDE.md invariant).
- `leads.manual_status` and `leads.notes` are never written.
- New objects are additive: `create table if not exists`, `create index if not exists`, plain views via drop+recreate.

---

## 2. What Already Exists That We Build On (verified)

| Component | Path | What we reuse |
|---|---|---|
| Real per-recipient bodies | `sent_messages` (migrations.sql:20+) | `body` is the spin-tax-resolved per-recipient text — **320,976 distinct bodies / ~323k rows**. Body-feature analysis is *valid* on this column. `send_kind = 'unibox_manual'` (≈3,722 rows) is the analysis population. |
| Inbound + labels | `replies` (migrations.sql:2) + `classifications` (migrations.sql:38) | Outcome signal. 11-label taxonomy. All 19,086 replies are classified (no null-label drop). Latest label by `classified_at DESC`. |
| Per-lead gold winner | `followup_winning_selection` (migrations.sql:361) + `select_winning_replies.py` | **45 live, look-back-valid** winning selections, all `unibox_manual`, joinable on `winning_sent_message_id = sent_messages.id`. Used as a **validation overlay**, not the primary signal. |
| Plain-view + NocoDB pattern | `scripts/apply_hybrid_views.py` | Proven template: `from db import connect`, autocommit, `drop view if exists` → `create view`, Title-Case double-quoted aliases, postgres-owned (grants auto-inherit), `pg_attribute` column verification, `row_number() over (partition by lead_email order by sent_timestamp asc)` for ffup position (apply_hybrid_views.py:38, 64–71). |
| NocoDB registration | `scripts/nocodb_sync.py` | Registers new views; Sync-Now / disconnect-reconnect after any column-shape (DDL) change. |
| HTML idiom | `debug/_gen_tracker_html.py` | `from db import connect` → `cur.execute` → f-string + inline CSS → write to `docs/replies/`. Reusable `.card`, `.group`, `bar(pct, color)` helper (line 51), `.callout.warn` banner, `datetime.now().strftime` footer, `html.escape`. |
| Direct DB→styled-artifact precedent | `debug/_make_audit_review_sheet.py` | Cursor→artifact idiom if an XLSX twin is later wanted. |
| CLI dispatch | `run.py` | Subparser pattern (run.py:181 `select-winning-replies`); `cmd_refresh` calls `update_status_main()` at run.py:74; daily cron `python run.py refresh` (Railway `cronSchedule`). |
| Latest-label ordering | `select_winning_replies.py` (classified_at desc; reconnect-on-stale `safe_query`) | Copy verbatim for outcome derivation. |

**Confirmed net-new (grep returned nothing):** `followup_message_features`, `followup_patterns_mv`, `followup_analysis_base` — all new.

---

## 3. Methodology (statistically-honest core — review fixes applied)

### 3.1 Analysis unit — one row per manual follow-up message
Grain = one row per `sent_messages.id` where `send_kind = 'unibox_manual'` (~3,722). **Not per lead.** A characteristic ("did *this* follow-up open with a question?") can only be tied to an outcome at the message grain; per-lead would collapse a whole ladder into one outcome and destroy the WITH-vs-WITHOUT contrast. `ffup_position` (1st, 2nd, …) is itself a first-class characteristic via the `row_number()` window already in `apply_hybrid_views.py:38`.

### 3.2 ⚠️ BLOCKING PREREQUISITE — strip the quoted thread before computing ANY feature
**Verified data hazard:** 3,562 of 3,722 manual bodies (**95.7%**) contain `wrote:`; mean body length is **2,260 chars** but the first `On … wrote:` boundary sits at **~326 chars** on average. So ~85% of each stored body is the *quoted reply chain* (the lead's own prior words + the inlined original campaign template), not the new follow-up Jam wrote. Without stripping, every feature — length, `has_question`, `has_url`, pricing token, `has_calendly`, and every LLM feature — measures the **quoted history**, not the new message. This invalidates the entire feature layer as naïvely specified.

**Fix (mandatory, applies to deterministic AND LLM features):** add a deterministic *new-text extractor* that truncates each body at the **earliest** match of:
- `/\nOn .*wrote:/`
- `/-----Original Message-----/`
- `/^From: .*\nSent: /m` (and `From:`/`Sent:`/`To:`/`Subject:` header blocks)
- a leading `Re: <subject>` echo

Store the result as `followup_new_text` (a column on the feature table). **Compute all features only over `followup_new_text`.** Add QA assertions:
- flag rows where `followup_new_text == body AND length(body) > 800` (no boundary found → manual review),
- report **"% of bodies where a quoted-thread boundary was detected"** as a data-quality metric in the HTML.
Rows with no detectable boundary are **excluded from body-feature analysis but still counted in outcome denominators**.

### 3.3 Outcome label + attribution
For follow-up M (lead L, sent at T):
- `next_out` = the next `unibox_manual` send to L after T (window upper bound; `+infinity` if none). **Verified:** zero intra-lead timestamp collisions, so windowing is deterministic.
- `credit_reply` = the first `replies` row for L with `reply_timestamp > T` and (`next_out IS NULL OR reply_timestamp < next_out`), ordered `reply_timestamp ASC`.
- `reply_label` = latest `classifications.label` for `credit_reply` (`ORDER BY classified_at DESC` — no `prompt_version` filter).

Outcome columns:
- `had_reply` (bool).
- `responded_positive` (bool) = `had_reply AND reply_label IN ('booked','interested')` — **PRIMARY**.
- `responded_booked` (bool) = `had_reply AND reply_label = 'booked'` — **headline overall rate only, NOT sliced by feature** (see power below).
- `is_confirmed_winner` (bool) = `sent_messages.id IN (select winning_sent_message_id from followup_winning_selection)` — validation overlay.

**Attribution is last-touch-before-reply, windowed to the next outbound** (one reply credits exactly one follow-up). This deliberately mirrors `select_winning_replies.py` so the two analyses are consistent. Stated in output: *"credit is assigned to the last follow-up before the reply; we do not claim it caused the reply."*

### 3.4 ⚠️ Close the reverse-causality leak
**Verified:** 556 positive replies (booked/interested) occurred *before* any manual follow-up to that lead. Many manual follow-ups are nudges sent to *already-warm* leads ("ready to talk? here's my calendar"). The naïve window would credit a *new* positive reply to a follow-up that was itself reacting to prior interest — inflating short-nudge styles.

**Fix:** add base-view column `prior_positive_exists` (bool) = a booked/interested reply exists for L strictly before T. Then either:
- **(a, default)** drop `prior_positive_exists` rows from the **primary** positive-rate analysis, OR
- **(b)** "first-positive only" — credit a follow-up only when the in-window reply is the lead's **first-ever** positive reply.

> **As-built:** NEITHER (a) nor (b) shipped — excluding warm-lead rows collapsed positives 323→16 and killed power. The shipped view keeps them in the primary population and exposes **"Lead Already Positive"** as its own characteristic so the confound is visible (see the as-built banner at top).

State the exclusion explicitly in the caveat block. `interested` is the label most exposed to this leak — note it.

### 3.5 Lift baseline (never causal)
Denominator = **ALL** manual follow-ups in scope, **including the silent majority that never got a reply** (excluding silent sends would massively inflate every rate — non-negotiable). For each characteristic value C:
- `support_with`, `support_without`
- `pos_rate_with = positives_with / support_with`; `pos_rate_without` likewise
- `lift = pos_rate_with / pos_rate_without`
- `abs_diff = pos_rate_with − pos_rate_without` (pp)
- `wilson_lo`, `wilson_hi` = Wilson 95% CI on `pos_rate_with` (computed in the **Python generator**; SQL keeps raw counts)
- `confidence_flag`: high ≥100 support, medium 30–99, low <30

Frame every output as: *"follow-ups that did X were replied-to positively N% of the time vs M% for those that didn't."*

### 3.6 ⚠️ Verified power funnel — anchor expectations to reality
Simulated on live data, the proposed attribution yields, of 3,722 manual sends:
- **443 (11.9%)** get any in-window reply,
- **323 (8.7%)** `responded_positive`,
- **111 (3.0%)** `responded_booked`.

Consequences baked into the methodology:
- Keep `{booked, interested}` as **primary** (only ~323 positives to split).
- **Demote booked-only** from per-feature slicing to an **overall headline rate only** — 111 positives cannot survive a 2-way slice with the promised confidence bands.
- **Two-armed finding threshold on positive count, not just send count:** a characteristic value is shown as a finding only if `support_with ≥ 30` **AND** `positives_with ≥ 15`. A 200-send / 4-positive cell is greyed "insufficient data — not reported."

### 3.7 ⚠️ ffup_position is a survival panel, not a winning characteristic
**Verified positive-rate by position: 18.5% (ffup 1) → 12.6% → 9.9% → 6.2% → 3.7% (ffup 6+) — monotonic decline.** The raw design's caveat ("later positions look *inflated* by an engaged subset") is **backwards**. The real mechanism: a lead who replies positively early stops getting more manual follow-ups (the window closes), so engaged leads are *removed* from later positions.
**Fix:** render `ffup_position` in a **separate "Timing / Survival" panel**, never in the lift-ranked body-feature table, labelled: *"Earlier follow-ups show higher positive-reply rates BECAUSE leads who reply positively early are removed from later positions (the window closes) — this reflects survival of the unconverted, not that later follow-ups are weak."*

### 3.8 ⚠️ Control for the client/campaign confound (don't just caveat it)
**Verified:** positive-rate by client spans **~7×** (PP 32.8%, Sellervue 28.6% … EC 4.3%, Lumian 4.6%), and volume is wildly skewed (**Epic alone = 1,815 / 3,722 = 49%**; one "Epic | Global Expansion | UK Leads" campaign = 366 sends). An uncontrolled lift on any body feature correlated with a house writing style is likely a client-mix artifact.
**Fixes:**
- Report each feature's lift **within-client** for the top clients (Epic, EC, BG, Zonlabs at minimum) **alongside** the pooled lift.
- Add a column **"Share of WITH-rows from the single largest client/campaign."**
- Add a **Simpson's-reversal flag**: any feature whose pooled lift flips sign or loses its CI separation once stratified is flagged, not headlined.
- State the 7× spread and Epic's 49% share with actual numbers in the caveat block.

### 3.9 Mandatory in-output caveats (HTML banner + a pinned NocoDB header row, verbatim intent)
1. **Descriptive, not causal.** Associations between follow-up characteristics and the replies that followed; not proof the follow-up caused the reply.
2. **Coverage is partial.** Only ONE Instantly workspace's outbound is synced. *(Numbers live-derived at run time — see §3.10.)* Bodies are real; the population is incomplete.
3. **The per-lead `?lead=` backfill (`scripts/backfill_sent_for_tracker.py`) is MANUAL and unscheduled** — this snapshot reflects only what has been backfilled.
4. **"Positive" = next reply classified `booked` or `interested`.** `not_now`, `interested_past`, `not_interested`, `unsubscribe`, `oof`, `customer_service`, `wrong_person`, `no_longer_there`, `other` were deliberately NOT counted positive.
5. **Reverse-causality excluded:** follow-ups sent to leads who were *already* positive before the send are excluded from the primary rate (§3.4).
6. **Quoted-content data quality:** 95.7% of bodies contained quoted history; only the extracted new text is analyzed; X% had no detectable boundary and were excluded from body-feature analysis (still in denominators).
7. **Confounders:** client/campaign mix (7× spread, Epic = 49% of volume), list quality, collaborator identity. Read lift as a signal to investigate, not a rule.
8. **Power:** the funnel (3,722 → 443 any reply → 323 positive → 111 booked) is shown at the top. Booked-only is reported as an overall rate only, not sliced.
9. Snapshot date + base N.

Rows below the §3.6 thresholds render greyed as "insufficient data — not reported."

### 3.10 Live-derive coverage numbers (don't hard-code)
The literal "81 booked leads with 0 follow-ups" and "~189 blank tracker leads" originate from prior memory notes and were **not** re-derived this session (MEMORY cites ~223 elsewhere). Compute at run time and render with the snapshot date:
- booked leads with zero `unibox_manual` sends,
- tracker rows with all-null ffup columns.
Confirmed live anchors to cite alongside: **637 distinct leads have ≥1 manual follow-up; only 534 of those appear in `replies` at all.** If a clean query can't produce 81/189, label them "approximately, from prior audit."

---

## 4. Feature Taxonomy

All features computed over `followup_new_text` (§3.2), **never** the raw body.

### 4.1 V1 — deterministic (ships first, $0, no LLM)
- `ffup_position` (survival panel only, §3.7)
- `length_bucket` over `followup_new_text` word count: `very_short ≤15 / short 16–40 / medium 41–90 / long >90`
- `has_question` (`new_text ~ '\?'`), `opens_with_question` (first non-greeting sentence ends `?`)
- `has_url` (links in new text, excluding unsubscribe/opt-out anchors)
- `has_calendar_link` (`calendly|cal\.com|hubspot.*meeting|book.*(call|time)`)
- `mentions_pricing` (`price|pricing|cost|discount|\$`)
- `has_ps`, `has_greeting`, `has_signoff` (reuse the compiled `_SIG_CUT_PATTERNS` set from `classify.py` — do not re-author)
- `has_emoji`, `all_caps_word_count`
- `send_dow`, `send_hour_utc` (timezone left UTC, noted in output)

### 4.2 V2 — LLM-derived (`MODEL_TIER = TBD`, gated, additive)
Closed enums (mirror the fixed-taxonomy discipline):
- `hook_type`: `question | stat | compliment | pattern_interrupt | value_prop | reminder | other`
- `tone`: `casual | formal | neutral`
- `cta_style`: `soft | direct | permission_based | none`
- `personalization`: `none | light | deep`

Reuse the `classify.py` batching idiom (system-prompt caching, item numbering `[1]..[N]`, `chunks()`, JSON-per-item parse with markdown-fence stripping as in `select_winning_replies.py`), **batch = 25**. Prompt file `prompts/followup_feature.txt` alongside `classifier.txt`. Model id held in a single top-of-file constant so swapping tiers is one line. Gate it: print model + est. cost, require explicit run (no auto-wire), like `llm-resolve-smartscout`. **Prerequisite: §3.2 — an LLM scoring a 2,200-char quoted body is worse and costlier than regex; new-text extraction must land before V2.**

### 4.3 Schema — `followup_message_features` (append to `migrations.sql` after line 374)
One row per manual follow-up; idempotent on `sent_message_id`; LLM block nullable so V1 ships without it.

```sql
-- =========================================================================
-- Follow-up Effectiveness (descriptive cross-lead analysis)
-- One row per unibox_manual follow-up. Idempotent on sent_message_id.
-- =========================================================================
create table if not exists followup_message_features (
  sent_message_id      bigint primary key references sent_messages(id),
  lead_email           text not null,
  ffup_position        int  not null,            -- row_number per lead, asc
  sent_timestamp       timestamptz not null,
  followup_new_text    text,                     -- quoted-thread-stripped body (§3.2)
  boundary_detected    boolean not null,         -- false => excluded from body-feature analysis
  client               text,
  campaign_name        text,

  -- ---- V1 deterministic block (over followup_new_text) ----
  char_len             int,
  word_count           int,
  length_bucket        text,                     -- very_short|short|medium|long
  has_question         boolean,
  opens_with_question  boolean,
  has_url              boolean,
  has_calendar_link    boolean,
  mentions_pricing     boolean,
  has_ps               boolean,
  has_greeting         boolean,
  has_signoff          boolean,
  has_emoji            boolean,
  all_caps_word_count  int,
  send_dow             smallint,
  send_hour_utc        smallint,

  -- ---- outcome attribution (descriptive, §3.3–3.4) ----
  had_reply            boolean not null default false,
  reply_label          text,
  responded_positive   boolean not null default false,  -- booked|interested, PRIMARY
  responded_booked     boolean not null default false,  -- booked only, headline-rate only
  prior_positive_exists boolean not null default false, -- reverse-causality guard (§3.4)
  is_confirmed_winner  boolean not null default false,  -- in followup_winning_selection

  -- ---- V2 LLM block (MODEL_TIER deferred; nullable) ----
  hook_type            text,
  tone                 text,
  cta_style            text,
  personalization      text,
  llm_model            text,
  llm_prompt_version   text,
  llm_classified_at    timestamptz,

  -- ---- provenance / versioning (mirror classifications/PROMPT_VERSION) ----
  extractor_version    text not null,            -- 'fx1'; bump on rule change
  extracted_at         timestamptz default now()
);
create index if not exists fmf_lead_idx     on followup_message_features (lead_email);
create index if not exists fmf_positive_idx on followup_message_features (responded_positive);
create index if not exists fmf_client_idx   on followup_message_features (client);
```

FK to `sent_messages(id)` (bigint, the verified FK target `followup_winning_selection` already uses). Re-extract bumps `extractor_version` and upserts `on conflict (sent_message_id) do update` — same idempotency contract as `classifications`.

---

## 5. Data Model & Surfaces

### 5.1 Plain view `followup_patterns_mv`
Deployed by `scripts/apply_followup_patterns_view.py` (structural clone of `apply_hybrid_views.py`: `connect()`, `autocommit = True`, `drop view if exists` → `create view`, then `pg_attribute` verify, print "Re-register in NocoDB next"). Plain view (auto-recomputes on query, no refresh wiring); postgres-owned so grants auto-inherit; Title-Case double-quoted aliases.

**Body-feature view** — one row per (characteristic, value). UNPIVOT via `LATERAL VALUES`, aggregate against `followup_message_features`, restricted to `boundary_detected AND NOT prior_positive_exists` for the primary rate. The V2 LLM axes are present from V1 (coalesced to `(not yet classified)`) so the view shape is stable across waves.

```sql
drop view if exists followup_patterns_mv;
create view followup_patterns_mv as
with base as (
  select *
  from followup_message_features
  where extractor_version = 'fx1'    -- (or latest extractor_version per message)
    and boundary_detected            -- body features only on extractable new text
    and not prior_positive_exists    -- reverse-causality guard (§3.4)
),
exploded as (
  select responded_positive, responded_booked, client, dim, val
  from base
  cross join lateral (values
    ('Length',               length_bucket),
    ('Has Question',         case when has_question then 'Yes' else 'No' end),
    ('Opens With Question',  case when opens_with_question then 'Yes' else 'No' end),
    ('Has Booking Link',     case when has_calendar_link then 'Yes' else 'No' end),
    ('Mentions Pricing',     case when mentions_pricing then 'Yes' else 'No' end),
    ('Has P.S.',             case when has_ps then 'Yes' else 'No' end),
    ('Has Emoji',            case when has_emoji then 'Yes' else 'No' end),
    ('Hook Type',            coalesce(hook_type,     '(not yet classified)')),  -- V2
    ('Tone',                 coalesce(tone,          '(not yet classified)')),  -- V2
    ('CTA Style',            coalesce(cta_style,     '(not yet classified)')),  -- V2
    ('Personalization',      coalesce(personalization,'(not yet classified)')) -- V2
  ) as v(dim, val)
),
totals as (
  select count(*) n_all,
         avg(responded_positive::int) rate_all
  from base
),
agg as (
  select dim, val,
         count(*)                          as support_with,
         count(*) filter (where responded_positive) as positives_with,
         avg(responded_positive::int)      as rate_with,
         -- largest-client concentration in the WITH arm (§3.8)
         max(per_client.cnt)::float / nullif(count(*),0) as top_client_share
  from exploded e
  left join lateral (
    select count(*) cnt from exploded e2
    where e2.dim = e.dim and e2.val = e.val and e2.client = e.client
  ) per_client on true
  group by dim, val
)
select
  a.dim                                                      as "Characteristic",
  a.val                                                      as "Value",
  a.support_with                                             as "Follow-ups With It",
  a.positives_with                                           as "Positive Replies",
  round(100.0 * a.rate_with, 1)                              as "Positive Reply % (With)",
  round(100.0 * ((t.rate_all*t.n_all - a.positives_with)
        / nullif(t.n_all - a.support_with, 0)), 1)           as "Positive Reply % (Without)",
  round( a.rate_with
        / nullif((t.rate_all*t.n_all - a.positives_with)
                 / nullif(t.n_all - a.support_with, 0), 0), 2) as "Lift",
  round(100.0 * a.top_client_share, 0)                       as "Largest-Client Share %",
  case when a.support_with >= 30 and a.positives_with >= 15 then
         case when a.support_with >= 100 then 'High' else 'Medium' end
       else 'Insufficient data' end                          as "Confidence"
from agg a cross join totals t
order by a.dim, "Positive Reply % (With)" desc nulls last;
```

Notes: `"Positive Reply % (Without)"` is the complement (all-minus-cell), `nullif(...,0)`-guarded; Wilson CI bounds are added by the HTML generator in Python (kept out of SQL). A **separate** `followup_timing_mv` (or a section in the same applier) renders the ffup-position survival panel (§3.7) so it never sits in the lift-ranked grid. Adding a column later = drop+recreate + NocoDB Sync-Now.

### 5.2 Committed HTML generator
`scripts/gen_followup_patterns_report.py` → `docs/replies/FOLLOWUP_EFFECTIVENESS.html`.
Idiom from `debug/_gen_tracker_html.py` (reuse its `<style>`, `.card`, `.group`, `bar()` helper, `.callout.warn`, footer). Reads the **view** (so HTML and NocoDB show identical numbers) plus a few summary queries. Sections:
1. **Power-funnel cards:** total manual follow-ups, any-reply / positive / booked counts and rates (the verified 3,722 → 443 → 323 → 111 funnel).
2. **Coverage + data-quality caveat banner** (`.callout.warn`) — all nine §3.9 items, with live-derived coverage numbers (§3.10) and the quoted-content %-boundary-detected metric.
3. **Body-feature sections** (one per characteristic): sorted bars of "Positive Reply % (With)", with support + positives + Wilson CI shown next to every rate, greyed "insufficient data" rows, the within-client / Simpson's-reversal flag (§3.8), and "Largest-Client Share %".
4. **Timing / Survival panel** (ffup position) with the explicit survival caption (§3.7).
5. **Validation overlay:** the 45 confirmed winners and whether headlined characteristics over-appear among them (§6 gate).
6. Generated-at footer.

### 5.3 Refresh wiring
New subcommand `run.py refresh-followup-patterns` runs: extract features → apply view → regenerate HTML (all idempotent/incremental). The deterministic extract (`extractor_version='fx1'`, cheap, incremental on new `unibox_manual` rows) may be appended as **one line at the end of `cmd_refresh`** (run.py:74, after `update_status_main()`) **only after one clean reviewed run** — it is $0 and the view auto-recomputes. The **LLM pass and the HTML regen stay manual**. The existing Railway daily cron (`python run.py refresh`) then keeps the data fresh with no new cron object.

### 5.4 Gap-4 procedure doc
`docs/reference/ADDING_A_NOCODB_VIEW.md` (one-line pointer added to CLAUDE.md Subsystems). Contents: plain-view-vs-MV decision (plain is default; MV brings the drop-view-before-MV + unique-index + `refresh concurrently` + re-grant dance — cite `lead_status_mv` gotcha and `debug/_mv_view_swap2.py`); Title-Case alias convention; postgres-owned grant inheritance; the `apply_hybrid_views.py` copy-me template; NocoDB Sync-Now (required on DDL change, not on data refresh); add-a-column = drop+recreate; inspect columns via `pg_attribute` (information_schema omits MV columns in this PG version); zombie `idle in transaction` check before any MV DDL.

---

## 6. Build Phases

### Phase 0 — Data-readiness check (read-only, ~0.5 day)
**Files:** `scripts/check_followup_effectiveness_readiness.py` (modeled on `debug/analyze_booked_no_followups.py`). No DB writes.
**Done-criteria (printed report):** confirm the verified funnel (3,722 / 443 / 323 / 111); count manual sends with non-null body; **% of bodies with a detected quoted-thread boundary** (§3.2); the 556 pre-manual positives (§3.4); the client/campaign positive-rate spread + Epic's volume share (§3.8); the 45 winners joinable; live-derived coverage numbers (§3.10). This report feeds the support thresholds and the caveat banner.

### Phase 1 — Deterministic headline (THE MINIMUM SHIPPABLE; ~2 days; lands **Jun 17**)
**Files:**
- `migrations.sql` — append `followup_message_features` (§4.3).
- `followup_features.py` (root, sibling to `select_winning_replies.py`) — new-text extraction (§3.2) FIRST, then deterministic features over `followup_new_text`, then outcome attribution (windowed last-touch, `classified_at DESC`, `prior_positive_exists`), upsert `extractor_version='fx1'`. Reuse `safe_query` reconnect.
- `scripts/apply_followup_patterns_view.py` — clone of `apply_hybrid_views.py`; builds `followup_patterns_mv` + timing panel.
- `scripts/gen_followup_patterns_report.py` — HTML (§5.2).
- `run.py` — subparsers `extract-followup-features` and `refresh-followup-patterns` + dispatch (pattern at run.py:181); do **not** wire into `cmd_refresh` yet.
- CLAUDE.md — document the new subcommands.

**Done-criteria:** `python run.py refresh-followup-patterns` populates the table, the view returns ≥1 row per characteristic with support + positives, the HTML opens with the caveat banner + funnel + survival panel, and the validation gate passes.

### Phase 2 — LLM features (~1–1.5 days; needs the deferred tier decision; fully isolated)
**Files:** `prompts/followup_feature.txt`; extend `followup_features.py` with a gated `--with-llm-features` pass (batch=25) writing the nullable LLM block + `llm_model`/`llm_prompt_version`/`llm_classified_at`; drop+recreate the view (LLM axes light up); regenerate HTML. **Prerequisite: Phase 1 new-text extraction.** Done-criteria: LLM columns populate, view shows LLM axes, validation gate re-run.

### Phase 3 — Refresh automation (~0.5 day)
**Files:** `run.py` `cmd_refresh` gains one line calling the deterministic extract (NOT the LLM pass) after `update_status_main()`. Done-criteria: a `run.py refresh` regenerates `fx1` features + view without error; HTML regen stays manual.

### Validation gate (must pass before publishing HTML / exposing the NocoDB view)
1. **Minimum support:** suppress any cell with `support_with < 30` OR `positives_with < 15` (§3.6) — rendered "insufficient data," never a number.
2. **Quoted-content QA:** spot-check 20 rows where a boundary was detected — confirm `followup_new_text` is the real new message, not quoted history. Target ≥18/20.
3. **Gold-anchor consistency:** re-derive features for the 45 winners; confirm headlined high-lift characteristics over-appear among them vs the population. If `has_calendar_link` (etc.) is NOT enriched among the 45, flag it as an attribution artifact — do not headline.
4. **False-positive spot-check:** 20 `has_url=true` (link in new text, not signature) and 20 `responded_booked=true` (plausible booking actually followed). Target ≥18/20 each (90%, matching the classifier bar).
5. **Stratification check:** at least the top-3 headlined characteristics survive the within-client cut without a Simpson's reversal (§3.8).
6. **Rate sanity:** overall booked-credit rate is in a believable band of `45 / manual-population`; a 10× deviation means the attribution window is mis-joined.

**Publication gate:** ship only if (1) every published cell meets support, (2) ≥3 headlined characteristics pass the gold-anchor + stratification direction checks, (3) spot-checks clear 90%. Otherwise narrow the headline to passing characteristics. **Ship HTML-first for internal review BEFORE exposing the NocoDB client view**, so fixes §3.2–3.4 can be eyeballed on real rows before a non-technical client sees a lift number.

---

## 7. Risks & Mitigations

| Risk | Mitigation |
|---|---|
| **Quoted-thread contamination** (95.7% of bodies; ~85% of chars are quoted) invalidates every feature. | §3.2 new-text extractor (blocking prereq for V1 and V2); QA assertions + %-boundary-detected metric in HTML; rows with no boundary excluded from body features. |
| **False causal read** by client. | Lift framing only; pinned caveat banner; support+positives floor; Wilson CI; explicit confounder list; HTML-first internal review before client view. |
| **ffup_position misread** (decline, not inflation). | Separate survival panel + explicit survival caption (§3.7); kept out of the lift table. |
| **Client/campaign confound** (7× spread, Epic 49%). | Within-client lift + Simpson's-reversal flag + "Largest-Client Share %" column + numbers in caveat (§3.8). |
| **Reverse causality** (556 pre-manual positives). | `prior_positive_exists` exclusion / first-positive-only (§3.4); stated in caveat. |
| **Harsh power** (323 positive / 111 booked). | Booked-only demoted to overall rate; finding threshold on positive count; CI surfaces imprecision (§3.6). |
| **Coverage numbers drift / look fabricated.** | Live-derive 81/189 at run time with snapshot date; cite confirmed anchors (637 / 534) (§3.10). |
| **NocoDB schema cache** after column change. | Plain view (data refresh needs no sync); applier prints "Re-register"; Gap-4 doc codifies Sync-Now; V2 axes shipped in V1 so V2 ideally needs data + regen only. |
| **LLM free-text drift / cost.** | Closed enums; `llm_prompt_version` freezes runs (coexist like `classifications`); rule-based ships at $0; LLM gated behind §3.2. |
| **`unibox_manual` rests on step-IS-NULL rule.** | Re-verify quarterly (existing carry-over caveat); document the dependency. |
| **Latest-classification correctness.** | `ORDER BY classified_at DESC`, never pin a `prompt_version` (mirrors `select_winning_replies.py`). |
| **Plain-view performance** (3,722 × 19k). | Small; fine as plain view. Materialize only if slow (then the `lead_status_mv` drop-dependents-first dance applies). |

---

## 8. Open Questions for Victor (concise)
1. **"Positive" set:** confirm `{booked, interested}` (and that `not_now` / `interested_past` stay excluded — they arguably signal future interest). Load-bearing knob.
2. **Reverse-causality handling:** drop `prior_positive_exists` rows (default) vs first-positive-only (§3.4)?
3. **Support floor:** 30 sends + 15 positives per arm — acceptable, or stricter given partial coverage?
4. **Feature subset for v1:** is the deterministic set (§4.1) enough to ship while the model tier is decided?
5. **Coverage acceptability:** is single-workspace, manual-backfill coverage acceptable to ship as a labelled snapshot, or hold for broader sync?
6. **Sequencing:** HTML-first internal review, then NocoDB view after sign-off — confirm.
7. **Cron:** append deterministic extract to daily `cmd_refresh` once stable (HTML/LLM stay manual) — confirm.
8. Confirm Gap 2 (booked-via-tag) stays NEXT, not here; and that the V2 LLM pass isn't competing for that slot.
*(Model tier Haiku/Sonnet/Opus is DEFERRED by design — not a v1 blocker.)*

---

## 9. Effort Estimate

| Phase | Scope | Estimate | Model decision needed? |
|---|---|---|---|
| 0 | Read-only readiness report | ~0.5 day | No |
| 1 | Table + new-text extractor + deterministic features + view + HTML + 2 CLI cmds (**Jun 17 headline**) | ~2 days | **No** |
| 2 | LLM feature pass (gated, additive) + view recreate + HTML regen | ~1–1.5 days | **Yes (deferred)** — does not block Phase 1 |
| 3 | Append deterministic extract to daily cron | ~0.5 day | No |
| Gap-4 doc | `docs/reference/ADDING_A_NOCODB_VIEW.md` | ~0.25 day (fold into Phase 1) | No |

**Critical path to the headline = Phases 0 + 1 ≈ 2.5 days, zero LLM cost, no model-tier decision.** Phases 2–3 layer on without re-architecting.