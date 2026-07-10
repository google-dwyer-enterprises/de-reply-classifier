-- Received replies
create table replies (
  id bigserial primary key,
  lead_email text not null,
  campaign_id text,
  campaign_name text,
  client text,
  reply_timestamp timestamptz not null,
  subject text,
  body text,
  instantly_message_id text unique,
  tags text[] not null default '{}',
  created_at timestamptz default now()
);
create index on replies (lead_email);
create index on replies (reply_timestamp desc);
create index on replies using gin (tags);

-- Sent messages
create table sent_messages (
  id bigserial primary key,
  lead_email text not null,
  campaign_id text,
  campaign_name text,
  client text,
  sent_timestamp timestamptz not null,
  subject text,
  body text,
  instantly_message_id text unique,
  tags text[] not null default '{}',
  created_at timestamptz default now()
);
create index on sent_messages (lead_email);
create index on sent_messages (sent_timestamp desc);
create index on sent_messages using gin (tags);

-- Classifications
create table classifications (
  id bigserial primary key,
  reply_id bigint references replies(id),
  lead_email text not null,
  label text not null,
  confidence numeric(3,2),
  model text default 'claude-haiku-4-5',
  prompt_version text,
  classified_at timestamptz default now(),
  raw_response jsonb
);
create index on classifications (lead_email);

-- Sync state tracking
create table sync_state (
  id serial primary key,
  message_type text unique not null,
  last_synced_at timestamptz
);
insert into sync_state (message_type, last_synced_at) values ('received', null), ('sent', null);

-- Additive: campaign tags column for existing deployments
alter table replies add column if not exists tags text[] not null default '{}';
alter table sent_messages add column if not exists tags text[] not null default '{}';
create index if not exists replies_tags_gin on replies using gin (tags);
create index if not exists sent_messages_tags_gin on sent_messages using gin (tags);

-- Additive: Instantly per-lead interest status
alter table replies add column if not exists lead_status_code int;
alter table replies add column if not exists lead_status text;
alter table sent_messages add column if not exists lead_status_code int;
alter table sent_messages add column if not exists lead_status text;
create index if not exists replies_lead_status_idx on replies (lead_status);

-- v3: top-3 classifier statuses + Instantly lead status on leads
alter table leads add column if not exists status1 text;
alter table leads add column if not exists status2 text;
alter table leads add column if not exists status3 text;
alter table leads add column if not exists status4 text;
create index if not exists leads_status1_idx on leads (status1);
create index if not exists leads_status4_idx on leads (status4);

-- v4: per-status reason + source reply body on leads
alter table leads add column if not exists reason1 text;
alter table leads add column if not exists reason2 text;
alter table leads add column if not exists reason3 text;
alter table leads add column if not exists reply1_body text;
alter table leads add column if not exists reply2_body text;
alter table leads add column if not exists reply3_body text;

-- SmartScout: Amazon brand market data + lead match resolution
create table if not exists smartscout_brands (
  brand_norm text primary key,
  brand_original text not null,
  primary_category text,
  primary_subcategory text,
  amazon_in_stock_rate numeric,
  average_number_of_sellers numeric,
  average_price numeric,
  estimated_monthly_revenue numeric,
  estimated_monthly_units_sold numeric,
  one_month_growth numeric,
  twelve_month_growth numeric,
  trailing_12_months numeric,
  average_package_volume numeric,
  average_rating numeric,
  total_ratings_count integer,
  average_number_of_fba_sellers numeric,
  total_product_count integer,
  brand_score numeric,
  storefront text,
  dominant_seller text,
  dominant_seller_sales_percentage numeric,
  dominant_seller_country text,
  last_seen_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create table if not exists lead_smartscout_match (
  lead_email text primary key,
  brand_norm text references smartscout_brands(brand_norm) on delete set null,
  match_score numeric,
  match_method text,  -- 'fuzzy' | 'llm' | 'manual' | 'none'
  resolved_at timestamptz not null default now(),
  use_this_company text  -- the lead company string that was matched (for audit/verification)
);
create index if not exists lead_smartscout_match_brand_idx on lead_smartscout_match (brand_norm);

-- Backfill of use_this_company for existing rows (re-run resolve-smartscout to populate):
alter table lead_smartscout_match add column if not exists use_this_company text;

-- Prospeo lead scraper: new decision-maker leads pulled from Prospeo by domain
create table if not exists prospeo_new_leads (
  id bigserial primary key,
  email text unique not null,
  first_name text,
  last_name text,
  title text,
  company_name text,
  company_domain text,
  company_website text,
  source_domain text,            -- inclusion-list domain queried
  prospeo_raw jsonb,             -- full API payload for audit
  agency_filter_result text,     -- 'brand' | 'agency' | 'reseller' | 'marketplace' | 'unknown'
  agency_filter_method text,     -- 'rule' | 'llm' | 'none'
  agency_filter_reason text,
  rejected boolean not null default false,
  scraped_at timestamptz not null default now(),
  exported_at timestamptz        -- set when row is written to a CSV / promoted to lead_contacts
);
create index if not exists prospeo_new_leads_scraped_idx on prospeo_new_leads (scraped_at desc);
create index if not exists prospeo_new_leads_status_idx on prospeo_new_leads (rejected, agency_filter_result);
create index if not exists prospeo_new_leads_source_idx on prospeo_new_leads (source_domain);

-- Mobile enrichment (10 credits per verified mobile; opt-in via --with-mobile)
alter table prospeo_new_leads add column if not exists mobile text;
alter table prospeo_new_leads add column if not exists mobile_status text;

-- Domain inclusion list driving the scraper (cleaned of .org/.edu/gmail/etc.)
create table if not exists domain_inclusion_list (
  domain text primary key,
  added_at timestamptz not null default now(),
  last_scraped_at timestamptz,
  notes text
);

-- =========================================================================
-- Category-mode scraping support (scrape-leads --mode category)
-- =========================================================================
-- See PROSPEO.html "Category mode" section for design rationale.
--
-- Two parts:
--   1. category_scrape_state — pagination cursor per industry (12 rows total)
--   2. prospeo_new_leads gains source_industry + scrape_mode columns
--      so each row records which mode + which industry produced it.

-- Pagination cursor: one row per Prospeo industry string in PROSPEO_INDUSTRIES.
-- Updated at the end of each page read in a category-mode run.
-- Rows are created lazily by the scraper on first sighting of an industry —
-- no need to pre-seed.
create table if not exists category_scrape_state (
  industry text primary key,                       -- e.g. "Retail Apparel and Fashion"
  countries text[] not null default '{}',          -- last-used location filter, e.g. {"United States","Canada"}
  last_page_consumed int not null default 0,       -- 0 = nothing consumed; next run starts at page 1
  total_pages int,                                 -- as reported by Prospeo's pagination on last call
  exhausted boolean not null default false,        -- last_page_consumed >= total_pages
  last_scraped_at timestamptz,
  total_credits_spent int not null default 0      -- cumulative across runs
);

-- Existing rows in prospeo_new_leads are all domain-mode by definition,
-- so default scrape_mode to 'domain'. source_industry stays NULL for them.
alter table prospeo_new_leads
  add column if not exists source_industry text;
alter table prospeo_new_leads
  add column if not exists scrape_mode text not null default 'domain'
    check (scrape_mode in ('domain', 'category'));
create index if not exists prospeo_new_leads_mode_idx
  on prospeo_new_leads (scrape_mode, rejected);
create index if not exists prospeo_new_leads_industry_idx
  on prospeo_new_leads (source_industry)
  where source_industry is not null;

-- =========================================================================
-- Follow-up Tracker (FOLLOWUP_ANALYSIS_PLAN.md)
-- =========================================================================
-- Replaces Jam's manual "DE Email Master Sheet → Follow Up Tracker"
-- spreadsheet. Phases 1.1 + 1.2 of v3 — additive schema only. The wide
-- materialized view (Phase 1.3) is added in a separate migration after
-- Phase 2 (sync) has populated sent_messages and Phase 3 (CSV ingest)
-- has populated lead_outcomes.
--
-- Phase 0.6 finding (2026-05-20, scripts/probe_in_reply_to.py): Instantly's
-- /v2/emails API does NOT expose `in_reply_to_id` on inbound emails
-- (0/26 inbound messages had it populated). Hence no in_reply_to_id column
-- below — v1/v2 of the plan assumed it existed; v3 sidesteps the question.

-- 1.1 — Extend sent_messages with classification fields from Instantly.
--
-- `send_kind` is a STORED generated column. Postgres derives it from
-- ue_type + step on insert; application code MUST NOT send a value for it.
--
-- EMPIRICAL ASSUMPTION (verified, not contractually guaranteed):
-- Instantly populates `step` for campaign-automated sends and leaves it
-- NULL for Unibox manual replies. All 537 rows observed across Phase 0
-- + 0.5 probes had step IS NULL ⟺ ue_type = 3. Re-verify quarterly.
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

-- campaign_id index for backfill_tags' per-campaign UPDATE ... WHERE campaign_id = X.
-- Without it, each of ~411 per-campaign updates full-scanned sent_messages (~445k
-- rows), timing out the daily cron (statement_timeout 57014). See backfill_tags.py.
create index if not exists replies_campaign_id_idx on replies (campaign_id);
create index if not exists sent_messages_campaign_id_idx on sent_messages (campaign_id);

-- 1.2 — lead_outcomes table for fields the API can't give us.
-- Holds Status, Qualified, NOTE (JOYCE), Call ffup, Leadlist Source — the
-- columns that come from Jam's manual tracker CSV. Phase 3 ingests
-- original_data/followup_tracker_2026-05-19.csv into this table (one-time).
-- After Phase 3, this table is updated only when new leads need manual
-- Status/Qualified/NOTE entries that the existing classifier can't derive.
create table if not exists lead_outcomes (
  lead_email text not null,
  client text not null,
  campaign text not null default '',   -- CSV has campaign for every row; empty-string default for PK compat
  leadlist_source text,                -- CSV "Leadlist Source"
  status_raw text,                     -- CSV "Status" verbatim (e.g. "Booked", "Asking for Proposal")
  qualified text,                      -- 'Qualified' | 'No' | 'Pending'
  note text,                           -- CSV "NOTE (JOYCE)"
  call_ffup text,                      -- CSV "Call ffup"
  source text not null default 'manual_tracker_csv',
  updated_at timestamptz default now(),
  primary key (lead_email, client, campaign)
);
create index if not exists lead_outcomes_lead_idx on lead_outcomes (lead_email);

-- 1.3 — followup_tracker_mv : the wide-pivot MV NocoDB renders.
--
-- One row per (lead_email, client, campaign) — Jam's spreadsheet shape:
--   Client | Email Address | Campaign | Leadlist Source | Status | Qualified
--   | Initial Reply Date  | What was their initial reply
--   | Email ffup 1 Date   | Email FF 1 what we sent
--   | Email ffup 2 Date   | Sent ff 2
--   | ... (up to ffup 10)
--   | Call ffup | NOTE (JOYCE)
--   | Last Reply At  | Last reply from Instantly   <-- NEW v3 column
--
-- Calendar invites (notifications@calendly.com, etc.) come into `replies` with
-- their bot email as `lead_email`, so they're orphaned from real leads and
-- never get joined here. No special tagging needed.
drop materialized view if exists followup_tracker_mv;
create materialized view followup_tracker_mv as
with first_reply as (
  -- The lead's first inbound reply (the one that put them in the tracker)
  select distinct on (lead_email)
    lead_email, reply_timestamp, body
  from replies
  order by lead_email, reply_timestamp asc
),
last_reply as (
  -- The lead's most recent inbound reply (powers the NEW v3 column)
  select distinct on (lead_email)
    lead_email, reply_timestamp, body
  from replies
  order by lead_email, reply_timestamp desc
),
ranked_outbounds as (
  -- Manual outbounds (Jam's typed follow-ups) ranked chronologically per lead
  select
    s.lead_email,
    s.sent_timestamp,
    s.body,
    row_number() over (
      partition by s.lead_email
      order by s.sent_timestamp asc
    ) as ffup_n
  from sent_messages s
  where s.send_kind = 'unibox_manual'
)
select
  lo.client                                                  as "Client",
  lo.lead_email                                              as "Email Address",
  lo.campaign                                                as "Campaign",
  lo.leadlist_source                                         as "Leadlist Source",
  coalesce(lo.status_raw, l.auto_status)                     as "Status",
  lo.qualified                                               as "Qualified",
  fr.reply_timestamp                                         as "Initial Reply Date",
  fr.body                                                    as "What was their initial reply",
  max(case when ro.ffup_n = 1  then ro.sent_timestamp end)   as "Email ffup 1 Date",
  max(case when ro.ffup_n = 1  then ro.body end)             as "Email FF 1 what we sent",
  max(case when ro.ffup_n = 2  then ro.sent_timestamp end)   as "Email ffup 2 Date",
  max(case when ro.ffup_n = 2  then ro.body end)             as "Sent ff 2",
  max(case when ro.ffup_n = 3  then ro.sent_timestamp end)   as "Email ffup 3 Date",
  max(case when ro.ffup_n = 3  then ro.body end)             as "Sent ff 3",
  max(case when ro.ffup_n = 4  then ro.sent_timestamp end)   as "Email ffup 4 Date",
  max(case when ro.ffup_n = 4  then ro.body end)             as "Sent ff 4",
  max(case when ro.ffup_n = 5  then ro.sent_timestamp end)   as "Email ffup 5 Date",
  max(case when ro.ffup_n = 5  then ro.body end)             as "Sent ff 5",
  max(case when ro.ffup_n = 6  then ro.sent_timestamp end)   as "Email ffup 6 Date",
  max(case when ro.ffup_n = 6  then ro.body end)             as "Sent ff 6",
  max(case when ro.ffup_n = 7  then ro.sent_timestamp end)   as "Email ffup 7 Date",
  max(case when ro.ffup_n = 7  then ro.body end)             as "Sent ff 7",
  max(case when ro.ffup_n = 8  then ro.sent_timestamp end)   as "Email ffup 8 Date",
  max(case when ro.ffup_n = 8  then ro.body end)             as "Sent ff 8",
  lo.call_ffup                                               as "Call ffup",
  max(case when ro.ffup_n = 9  then ro.sent_timestamp end)   as "Email ffup 9",
  max(case when ro.ffup_n = 10 then ro.sent_timestamp end)   as "Email ffup 10",
  lo.note                                                    as "NOTE (JOYCE)",
  lr.reply_timestamp                                         as "Last Reply At",
  left(lr.body, 500)                                         as "Last reply from Instantly"
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
  lr.reply_timestamp, lr.body;

-- Required for `refresh materialized view concurrently`.
-- (lead_email, client, campaign) is lead_outcomes' PK and the join key —
-- guaranteed unique here.
create unique index if not exists followup_tracker_mv_pk
  on followup_tracker_mv ("Email Address", "Client", "Campaign");

-- =========================================================================
-- Winning-reply selection (Option D + D2 — see FOLLOWUP_ANALYSIS_PLAN.md Phase 5)
-- =========================================================================
-- For each booked lead, identifies which manual outbound the lead's
-- commitment reply was responding to. Uses the classifications table as
-- the anchor (the reply already classified 'booked') and Haiku as the
-- judge over 2-3 candidate outbounds.
--
-- NOT YET APPLIED — staged here for when the 90-day sent backfill completes
-- and the selection script is ready to run.

-- 1.4 — followup_winning_selection table
create table if not exists followup_winning_selection (
  lead_email text not null,
  winning_sent_message_id bigint not null references sent_messages(id),
  booking_reply_id bigint not null references replies(id),
  candidate_message_ids bigint[] not null,   -- the 2-3 IDs considered (for audit)
  confidence text not null,                  -- 'high'|'medium'|'low'|'fallback'
  rationale text,
  model text not null,                       -- e.g. 'claude-haiku-4-5'
  prompt_version text not null,
  selected_at timestamptz default now(),
  primary key (lead_email, prompt_version)
);
create index if not exists followup_winning_selection_lead_idx
  on followup_winning_selection (lead_email);


-- =========================================================================
-- BetterContact provider support (Anna directive 2026-06-01 — switching
-- category-mode scraping from Prospeo to BetterContact).
-- =========================================================================
-- BetterContact's Lead Finder API has a different filter shape than Prospeo
-- (no revenue floor, seniority enum vs title list, lead_location vs company
-- HQ location, async polling) but the output rows have the same business
-- meaning. We write into the existing prospeo_new_leads table and tag rows
-- with a `provider` column so downstream exports + dedup keep working.

-- Tag every existing row as Prospeo-sourced so legacy data is back-compat.
alter table prospeo_new_leads
  add column if not exists provider text not null default 'prospeo';

-- Store BC's response payload alongside prospeo_raw (different JSON shape).
alter table prospeo_new_leads
  add column if not exists bettercontact_raw jsonb;

-- BetterContact pagination is offset-based (limit 1-200), unlike Prospeo's
-- page-based. We use a separate state table so per-provider cursors stay
-- independent and the existing category_scrape_state semantics don't change.
create table if not exists bettercontact_scrape_state (
  industry text primary key,
  countries text[] not null default '{}',
  last_offset_consumed integer not null default 0,
  total_leads_estimated integer,           -- BC's `summary.leads_found` from probe
  exhausted boolean not null default false,
  last_scraped_at timestamptz,
  total_credits_spent numeric(10,1) not null default 0  -- BC charges fractional
);


-- =========================================================================
-- Lead Scrape Automation (2026-06-04)
-- =========================================================================
-- Jam submits a request in NocoDB → row lands in scrape_requests with
-- status='pending'. A Railway worker polls every 60s, runs the BC scraper,
-- updates status to 'ready', emails Jam. Jam approves in NocoDB →
-- worker copies the request's prospeo_new_leads rows into lead_contacts
-- and flips status to 'moved'. See LEAD_AUTOMATION.md for ops.

create table if not exists scrape_requests (
  id              bigserial primary key,
  requested_leads integer  not null check (requested_leads between 1 and 5000),
  -- NocoDB MultiSelect storage format: comma-separated text. Originally
  -- text[] but NocoDB has no form widget for Postgres array columns, so
  -- the fields were invisible on the public submit form. Migrated via
  -- scripts/apply_multiselect_migration.py.
  industries      text     not null default '',            -- empty = all 12
  skip_industries text     not null default '',
  countries       text     not null default 'United States,Canada',
  notes           text,
  status          text     not null default 'pending'
                  check (status in ('pending','running','ready','moved','rejected','failed')),
  approval        text     not null default 'pending'
                  check (approval in ('pending','approved','rejected')),
  scraped_count   integer  not null default 0,
  moved_count     integer  not null default 0,
  credits_spent   numeric(10,1) not null default 0,
  created_at      timestamptz not null default now(),
  started_at      timestamptz,
  ready_at        timestamptz,
  moved_at        timestamptz,
  failed_at       timestamptz,
  email_sent_at   timestamptz,
  export_csv_path text,
  export_xlsx_path text,
  error_message   text
);
create index if not exists scrape_requests_status_idx on scrape_requests (status);
create index if not exists scrape_requests_approval_idx on scrape_requests (approval);

-- Tag every prospeo_new_leads row with the request that produced it.
-- Lets the worker (a) find what to move on approval and (b) attribute
-- credits/leads per request for reporting later.
alter table prospeo_new_leads
  add column if not exists scrape_request_id bigint references scrape_requests(id);
create index if not exists prospeo_new_leads_scrape_request_id_idx
  on prospeo_new_leads (scrape_request_id) where scrape_request_id is not null;

-- Per-lead approval (Flavor C / granular workflow).
-- NULL on rows from the CLI scraper. For worker-tagged rows: starts
-- 'pending' for BC-accepted leads, 'rejected' for BC-auto-rejected ones.
-- Jam moves leads to 'approved' or 'rejected' inside a NocoDB per-batch
-- grid; the worker keeps moving approved leads into lead_contacts until
-- every lead has a decision, then flips scrape_requests.status='moved'.
alter table prospeo_new_leads
  add column if not exists lead_approval text
    check (lead_approval is null or
           lead_approval in ('pending', 'approved', 'rejected'));
alter table prospeo_new_leads
  add column if not exists lead_moved_at timestamptz;
create index if not exists prospeo_new_leads_pending_move_idx
  on prospeo_new_leads (scrape_request_id)
  where lead_approval = 'approved' and lead_moved_at is null;

-- ---------------------------------------------------------------------------
-- Reseller detection (RESELLER_DETECTION_PLAN.md, Phase 1)
-- ---------------------------------------------------------------------------

-- Per-domain verdict cache. One row per company domain ever judged; repeat
-- domains across batches are never re-researched. Source of truth for the
-- verdict; prospeo_new_leads rows carry a denormalized copy for export/audit.
-- Policy: only decisive verdicts (brand/reseller) are cached — 'unknown' is
-- re-derivable and caching it would block later stages from re-judging.
create table if not exists domain_brand_verdicts (
  domain          text primary key,            -- lowercased, no www
  verdict         text not null,               -- 'brand' | 'reseller'
  method          text not null,               -- 'smartscout' | 'shopify_probe'
                                               -- | 'vendor_llm' | 'site_llm'
                                               -- | 'agentic' | 'human'
  confidence      text,                        -- 'high' | 'medium' | 'low'
  evidence        text,                        -- vendor list / quoted page text
  shopify_vendor_count int,                    -- null when not Shopify
  decided_at      timestamptz not null default now(),
  prompt_version  text                         -- for LLM verdicts (e.g. 'bv1')
);

-- Denormalized verdict on each lead row (audit + export + reviewer UI).
alter table prospeo_new_leads
  add column if not exists brand_verify_result text;
alter table prospeo_new_leads
  add column if not exists brand_verify_method text;
alter table prospeo_new_leads
  add column if not exists brand_verify_evidence text;

-- ---------------------------------------------------------------------------
-- QA gap fixes (ROADMAP_IMPLEMENTATION_PLAN.md, PR "qa-gap-fixes")
-- ---------------------------------------------------------------------------

-- Ground truth from the 2026-06-10/11 full-criteria website audit (312
-- companies, re-graded 6/11 for the sells-in-US/CA foreign rule). Regression
-- target for every new gate: catch the known fails, never reject the passes.
create table if not exists qa_audit_labels (
  domain        text primary key,
  verdict       text not null,         -- 'pass' | 'fail' | 'review'
  issue_group   text,                  -- fail bucket / review reason
  business_type text,
  category      text,
  evidence      text,
  labeled_at    timestamptz not null default now()
);

-- Ownership / true-size verdicts (corporate-parent detection).
alter table domain_brand_verdicts add column if not exists parent_company text;
alter table domain_brand_verdicts add column if not exists size_estimate text;

-- Per-batch accuracy harvested from reviewer decisions (lead_approval vs
-- machine verdicts) when a batch fully closes. Written by worker.py.
create table if not exists qa_metrics (
  id bigserial primary key,
  scrape_request_id bigint not null,
  total_leads int not null,
  machine_pass_human_approved int not null,
  machine_pass_human_rejected int not null,   -- ESCAPES: the number that matters
  machine_flag_human_approved int not null,   -- review queue overcautious
  machine_flag_human_rejected int not null,   -- review queue caught it
  computed_at timestamptz not null default now(),
  unique (scrape_request_id)
);

-- ---------------------------------------------------------------------------
-- MillionVerifier email-verification gate (meeting tasks #8/#9, 2026-06-11)
-- ---------------------------------------------------------------------------
-- Approved leads are verified AFTER human approval, BEFORE moving into
-- lead_contacts (the 200k pool). Only result='ok' moves; definitive non-ok
-- flips lead_approval back to 'rejected' with the reason stamped.
alter table prospeo_new_leads add column if not exists mv_result text;
alter table prospeo_new_leads add column if not exists mv_checked_at timestamptz;
alter table lead_contacts add column if not exists mv_result text;
alter table lead_contacts add column if not exists mv_checked_at timestamptz;

-- Cost resequencing R8: per-domain ICP-gate verdict cache (one Haiku
-- judgment per company domain instead of per lead; measured 42% duplicate
-- judgments under the per-lead loop).
create table if not exists icp_gate_cache (
  domain     text primary key,
  result     text not null,
  reason     text,
  decided_at timestamptz not null default now()
);

-- Cost reseq R3: segment health. A segment burning >=10 credits with zero
-- accepted leads twice in a row gets parked for 30 days (BC inventory
-- refreshes; one good call resets the counter).
alter table bettercontact_scrape_state add column if not exists consecutive_zero_yield int not null default 0;
alter table bettercontact_scrape_state add column if not exists parked_at timestamptz;

-- Phone/email enrichment choice per scrape request (ClickUp 86exxhgek).
-- 'email' (default) or 'both' — phones cost 10 credits each (probe-verified
-- 2026-06-12: 22 credits for 2 leads with both flags on), so the worker
-- scales its credit reservations by 11x when phones are enabled.
alter table scrape_requests add column if not exists enrichment text not null default 'email';
-- Per-client Amazon revenue floor (keep/drop line) for this request; NULL = the
-- $300k default. Set on the submit form; the worker passes it to bettercontact_main.
alter table scrape_requests add column if not exists revenue_floor integer;
-- Revenue-first flow for this request: discover email-free -> verify e-commerce
-- -> Rainforest revenue gate -> enrich only survivors (vs the classic
-- enrich-everyone-first flow). The worker passes revenue_first + a per-batch
-- Rainforest cap (~6 credits/target lead) to bettercontact_main.
alter table scrape_requests add column if not exists revenue_first boolean not null default false;
-- Rainforest credits spent by this batch's Amazon revenue gate (separate from
-- credits_spent, which is BetterContact enrichment). Written by worker.mark_ready
-- from the run summary's amazon_qa_credits, so /batches shows the full spend.
alter table scrape_requests add column if not exists amazon_qa_credits_spent integer not null default 0;
-- Per-request Rainforest cap for a revenue-first batch (NULL = worker derives
-- ~6 credits/target lead, floor 150). Lets a big validation run set an explicit
-- ceiling (e.g. 2000 for a 50-lead target).
alter table scrape_requests add column if not exists amazon_qa_max_credits integer;
alter table lead_contacts add column if not exists mobile text;

-- Provenance: which scrape_requests batch a moved lead came from. Nullable —
-- null for Apollo/vendor-uploaded leads (they don't originate from a batch);
-- set by the worker's move step for BetterContact-sourced leads. ON DELETE SET
-- NULL so deleting a batch nulls the provenance rather than deleting a real
-- lead. Makes per-batch cleanup/audit precise (delete ... where
-- scrape_request_id = any(...)) instead of matching on email + list source.
alter table lead_contacts
  add column if not exists scrape_request_id bigint references scrape_requests(id) on delete set null;
create index if not exists lead_contacts_scrape_request_id_idx
  on lead_contacts (scrape_request_id) where scrape_request_id is not null;

-- "Are they on Amazon" (Victor, 6/12): per-domain Amazon-registry presence
-- from the 275k-brand SmartScout table (guarded fuzzy match), stamped on
-- every verified company independent of the brand/reseller verdict.
alter table domain_brand_verdicts add column if not exists amazon_presence text;
alter table prospeo_new_leads add column if not exists amazon_presence text;

-- Amazon Revenue QA (7/4): cascade SmartScout → Rainforest floor → cache.
-- Stamped by amazon_revenue_qa.qa_companies() on every accepted lead (shadow
-- mode; AMAZON_QA_ENFORCE in bettercontact_sync.py gates auto-drop). Idempotent
-- via ensure_lead_columns(). Revenue floor $500k/yr; grey band $300k–$700k.
alter table prospeo_new_leads add column if not exists amazon_verdict text;
alter table prospeo_new_leads add column if not exists amazon_revenue_annual numeric;
alter table prospeo_new_leads add column if not exists amazon_revenue_source text;
alter table prospeo_new_leads add column if not exists amazon_reason text;

-- =========================================================================
-- Follow-up effectiveness (descriptive cross-lead analysis) — FOLLOWUP_EFFECTIVENESS_PLAN.
-- One row per unibox_manual follow-up. Features computed over the quoted-thread-
-- stripped new text (followup_new_text). Idempotent on sent_message_id.
-- Populated by followup_features.py; aggregated by followup_patterns_mv.
-- =========================================================================
create table if not exists followup_message_features (
  sent_message_id       bigint primary key references sent_messages(id),
  lead_email            text not null,
  ffup_position         int  not null,            -- row_number per lead, asc by sent_timestamp
  sent_timestamp        timestamptz not null,
  followup_new_text     text,                     -- quoted-thread-stripped body
  boundary_detected     boolean not null,         -- false => quote-strip uncertain
  client                text,
  campaign_name         text,
  -- v1 deterministic features (over followup_new_text) --
  char_len              int,
  word_count            int,
  length_bucket         text,                     -- very_short|short|medium|long
  has_question          boolean,
  opens_with_question   boolean,
  has_url               boolean,
  has_calendar_link     boolean,
  mentions_pricing      boolean,
  has_ps                boolean,
  has_greeting          boolean,
  has_signoff           boolean,
  has_emoji             boolean,
  all_caps_word_count   int,
  send_dow              smallint,                 -- 0=Mon
  send_hour_utc         smallint,
  -- outcome attribution (descriptive, windowed last-touch) --
  had_reply             boolean not null default false,
  reply_label           text,
  responded_positive    boolean not null default false,  -- booked|interested (PRIMARY)
  responded_booked      boolean not null default false,  -- booked only (headline rate only)
  prior_positive_exists boolean not null default false,  -- reverse-causality guard
  is_confirmed_winner   boolean not null default false,  -- in followup_winning_selection
  -- v2 LLM block (deferred model tier; nullable) --
  hook_type             text,
  tone                  text,
  cta_style             text,
  personalization       text,
  llm_model             text,
  llm_prompt_version    text,
  llm_classified_at     timestamptz,
  -- provenance --
  extractor_version     text not null,            -- 'fx1'; bump on rule change
  extracted_at          timestamptz default now()
);
create index if not exists fmf_lead_idx     on followup_message_features (lead_email);
create index if not exists fmf_positive_idx on followup_message_features (responded_positive);
create index if not exists fmf_client_idx   on followup_message_features (client);

-- ============================================================================
-- Interest follow-up A/B (INTEREST_FOLLOWUP_AB_PLAN.md)
-- ============================================================================

-- Curated "best replies" template library (Arm A of the A/B + the
-- "best replies, use this" page). Jam/Victor approve entries; nothing
-- auto-promotes. scenario_key buckets templates by the situation they fit.
create table if not exists followup_templates (
  id            bigserial primary key,
  scenario_key  text not null,                       -- e.g. 'interested_general','pricing_ask','booked_nudge'
  title         text,                                -- short label shown to Jam
  body          text not null,                       -- supports {first_name} / {company} tokens
  subject       text,
  is_active     boolean not null default true,
  approved_by   text,
  source_note   text,                                -- provenance, e.g. "from sent_message 123, 31% positive"
  version       int not null default 1,
  created_at    timestamptz not null default now(),
  updated_at    timestamptz not null default now()
);
create index if not exists followup_templates_active_idx
  on followup_templates (is_active, scenario_key);

-- A/B assignments + outcomes. One experiment per interest reply.
create table if not exists followup_experiments (
  id                   bigserial primary key,
  source_reply_id      bigint references replies(id),
  lead_email           text not null,
  client               text,
  arm                  text not null check (arm in ('static','ai')),
  variations           jsonb not null,               -- [{idx,text,template_id?}] shown to Jam
  chosen_variation_idx int,
  chosen_text          text,
  status               text not null default 'assigned'
                       check (status in ('assigned','sent','attributed','skipped')),
  assigned_at          timestamptz not null default now(),
  sent_marked_at       timestamptz,
  sent_message_id      text,                          -- linked sent_messages row (confirms a real send)
  had_reply            boolean,
  responded_positive   boolean,
  responded_booked     boolean,
  outcome_reply_id     bigint,
  attributed_at        timestamptz,
  unique (source_reply_id)
);
create index if not exists fexp_status_idx on followup_experiments (status);
create index if not exists fexp_lead_idx   on followup_experiments (lead_email);
create index if not exists fexp_client_idx on followup_experiments (client);
