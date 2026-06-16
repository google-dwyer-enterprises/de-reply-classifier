"""Build the descriptive follow-up-pattern NocoDB views (plain views).

  followup_patterns_mv : one row per (characteristic, value) with positive-reply
                          rate WITH vs WITHOUT, lift, support, positives, the
                          largest-client concentration, and a confidence flag.
                          Primary population = boundary_detected AND NOT
                          prior_positive_exists (reverse-causality guard).
  followup_timing_mv   : ffup-position SURVIVAL panel (kept OUT of the lift grid).

Plain views (auto-recompute, no refresh, NocoDB v2026 auto-syncs). Postgres-owned
=> grants inherit. Title-Case double-quoted aliases. DESCRIPTIVE only — lift is an
association, never a causal claim. After running: re-register / Sync-Now in NocoDB.

Mirrors scripts/apply_hybrid_views.py. Usage: python scripts/apply_followup_patterns_view.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from db import connect

# Support thresholds (Phase 0 power funnel: 323 positives total — keep conservative).
MIN_SUPPORT = 30      # follow-ups with the characteristic
MIN_POSITIVES = 15    # positive replies among them

PATTERNS_VIEW = f"""
drop view if exists followup_patterns_mv;
create view followup_patterns_mv as
-- Base = all cleanly-extracted manual follow-ups (boundary_detected). We do NOT
-- exclude already-warm leads (that collapses positives 323->16 and kills power);
-- instead "Lead Already Positive" is surfaced as its own characteristic so the
-- reverse-causality confound is VISIBLE rather than hidden.
with base as (
  select responded_positive, client, prior_positive_exists,
         length_bucket, has_question, opens_with_question,
         has_url, has_calendar_link, mentions_pricing, has_ps, has_emoji
  from followup_message_features
  where extractor_version = 'fx1'
    and boundary_detected
),
totals as (select count(*) n_all, count(*) filter (where responded_positive) pos_all from base),
exploded as (
  select responded_positive, client, dim, val
  from base
  cross join lateral (values
    ('Length',                length_bucket),
    ('Has Question',          case when has_question then 'Yes' else 'No' end),
    ('Opens With Question',   case when opens_with_question then 'Yes' else 'No' end),
    ('Has Booking Link',      case when has_calendar_link then 'Yes' else 'No' end),
    ('Mentions Pricing',      case when mentions_pricing then 'Yes' else 'No' end),
    ('Has Link',              case when has_url then 'Yes' else 'No' end),
    ('Has P.S.',              case when has_ps then 'Yes' else 'No' end),
    ('Has Emoji',             case when has_emoji then 'Yes' else 'No' end),
    ('Lead Already Positive', case when prior_positive_exists then 'Yes' else 'No' end)
  ) as v(dim, val)
),
percell as (
  select dim, val, client, count(*) c from exploded group by dim, val, client
),
cli as (
  select dim, val, max(c)::float / nullif(sum(c), 0) top_client_share
  from percell group by dim, val
),
agg as (
  select dim, val,
         count(*) support_with,
         count(*) filter (where responded_positive) positives_with,
         avg(responded_positive::int) rate_with
  from exploded group by dim, val
)
select
  a.dim                                                          as "Characteristic",
  a.val                                                          as "Value",
  a.support_with                                                 as "Follow-ups With It",
  a.positives_with                                               as "Positive Replies",
  round((100.0 * a.rate_with)::numeric, 1)                       as "Positive % (With)",
  round((100.0 * (t.pos_all - a.positives_with)
        / nullif(t.n_all - a.support_with, 0))::numeric, 1)      as "Positive % (Without)",
  round((a.rate_with
        / nullif((t.pos_all - a.positives_with)::numeric
                 / nullif(t.n_all - a.support_with, 0), 0))::numeric, 2) as "Lift",
  round((100.0 * c.top_client_share)::numeric, 0)                as "Largest-Client Share %",
  case when a.support_with >= {MIN_SUPPORT} and a.positives_with >= {MIN_POSITIVES}
       then case when a.support_with >= 100 then 'High' else 'Medium' end
       else 'Insufficient data' end                              as "Confidence"
from agg a
join cli c using (dim, val)
cross join totals t
order by a.dim, a.rate_with desc nulls last;
"""

TIMING_VIEW = """
drop view if exists followup_timing_mv;
create view followup_timing_mv as
with base as (
  select least(ffup_position, 6) pos_bucket, responded_positive
  from followup_message_features
  where extractor_version = 'fx1'
)
select
  case when pos_bucket = 6 then '6+' else pos_bucket::text end   as "Follow-up #",
  count(*)                                                       as "Sends",
  count(*) filter (where responded_positive)                    as "Positive Replies",
  round((100.0 * avg(responded_positive::int))::numeric, 1)     as "Positive %"
from base
group by pos_bucket
order by pos_bucket;
"""


def main() -> None:
    conn = connect()
    conn.autocommit = True
    cur = conn.cursor()
    cur.execute(PATTERNS_VIEW)
    cur.execute(TIMING_VIEW)
    for v in ("followup_patterns_mv", "followup_timing_mv"):
        cur.execute("select attname from pg_attribute where attrelid = %s::regclass "
                    "and attnum > 0 and not attisdropped order by attnum", (f"public.{v}",))
        cols = [r[0] for r in cur.fetchall()]
        cur.execute(f'select count(*) from {v}')
        n = cur.fetchone()[0]
        print(f"{v}: {n} rows | cols: {cols}")
    print("\nNext: re-register in NocoDB (Sync Now / disconnect-reconnect) so the new "
          "views appear. Plain views need no refresh; data updates on query.")
    conn.close()


if __name__ == "__main__":
    main()
