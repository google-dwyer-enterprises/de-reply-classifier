"""Create the interest-follow-up A/B tables (idempotent).

  followup_templates   : curated "best replies" library (Arm A + the best-replies page).
  followup_experiments : A/B assignments + outcomes (Arm A static vs Arm B AI).

Additive only (create table if not exists) — safe to re-run. DDL is the same
as the block at the bottom of migrations.sql; this script just applies it.

Usage: python scripts/apply_interest_followup_tables.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from db import connect

DDL = """
create table if not exists followup_templates (
  id            bigserial primary key,
  scenario_key  text not null,
  title         text,
  body          text not null,
  subject       text,
  is_active     boolean not null default true,
  approved_by   text,
  source_note   text,
  version       int not null default 1,
  created_at    timestamptz not null default now(),
  updated_at    timestamptz not null default now()
);
create index if not exists followup_templates_active_idx
  on followup_templates (is_active, scenario_key);

create table if not exists followup_experiments (
  id                   bigserial primary key,
  source_reply_id      bigint references replies(id),
  lead_email           text not null,
  client               text,
  arm                  text not null check (arm in ('static','ai')),
  variations           jsonb not null,
  chosen_variation_idx int,
  chosen_text          text,
  status               text not null default 'assigned'
                       check (status in ('assigned','sent','attributed','skipped')),
  assigned_at          timestamptz not null default now(),
  sent_marked_at       timestamptz,
  sent_message_id      text,
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
"""


def main() -> None:
    conn = connect()
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute(DDL)
        for t in ("followup_templates", "followup_experiments"):
            cur.execute("select count(*) from " + t)
            print(f"{t}: ready ({cur.fetchone()[0]} rows)")
    conn.close()
    print("done.")


if __name__ == "__main__":
    main()
