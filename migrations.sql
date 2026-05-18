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
