"""Apply the followup_winning_selection table DDL."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from db import connect

DDL = """
create table if not exists followup_winning_selection (
  lead_email text not null,
  winning_sent_message_id bigint not null references sent_messages(id),
  booking_reply_id bigint not null references replies(id),
  candidate_message_ids bigint[] not null,
  confidence text not null,
  rationale text,
  model text not null,
  prompt_version text not null,
  selected_at timestamptz default now(),
  primary key (lead_email, prompt_version)
);
create index if not exists followup_winning_selection_lead_idx
  on followup_winning_selection (lead_email);
"""

conn = connect()
conn.autocommit = True
with conn.cursor() as cur:
    cur.execute(DDL)
    cur.execute("""select count(*) from pg_tables
                   where schemaname='public' and tablename='followup_winning_selection'""")
    print(f"[OK] followup_winning_selection exists: {cur.fetchone()[0] == 1}")
conn.close()
