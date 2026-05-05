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