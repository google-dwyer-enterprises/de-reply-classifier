"""Interest follow-up A/B — linking + outcome attribution + results (Phase 2b).

Run via `python run.py attribute-followup-experiments` (chain on the daily cron):
  1. LINK: confirm a marked-sent experiment actually went out — match it to a
     unibox_manual send in sent_messages (per-protocol gate).
  2. ATTRIBUTE: for confirmed sends, find the first inbound reply after the send
     and read its latest classification → positive / booked. Finalize once a reply
     lands OR the 30-day window closes (no reply).

fetch_results() reads the followup_ab_results view and layers on Wilson CIs + a
winner verdict (only declared past the support floor and with non-overlapping CIs —
no peeking-driven false winners). See docs/replies/INTEREST_FOLLOWUP_AB_PLAN.md.
"""
from __future__ import annotations

import math

import psycopg2.extras

import config
from db import connect

POSITIVE_LABELS = list(config.FOLLOWUP_INTEREST_LABELS)   # ('interested','booked')
OUTCOME_WINDOW_DAYS = 30
# Support floor before a verdict is shown (signed-off): >=30 decided AND >=15 positive per arm.
MIN_DECIDED = 30
MIN_POSITIVES = 15

# Confirm a marked-sent experiment by matching the nearest unibox_manual send
# at/after the moment Jam clicked "I sent this" (10-min grace for clock skew).
LINK_SQL = """
update followup_experiments e
   set sent_message_id = s.iid
  from (
    select distinct on (e2.id) e2.id as eid, s2.instantly_message_id as iid
      from followup_experiments e2
      join sent_messages s2
        on s2.lead_email = e2.lead_email
       and s2.send_kind = 'unibox_manual'
       and s2.sent_timestamp >= e2.sent_marked_at - interval '10 minutes'
     where e2.status = 'sent' and e2.sent_message_id is null
       and e2.sent_marked_at is not null
     order by e2.id, s2.sent_timestamp asc
  ) s
 where e.id = s.eid
"""

# Attribute the outcome: first inbound reply after the send, within the window.
# Finalize when a reply is found OR the window has closed (no reply = had_reply false).
ATTRIB_SQL = """
update followup_experiments e
   set had_reply           = (o.reply_id is not null),
       responded_positive  = coalesce(o.positive, false),
       responded_booked    = coalesce(o.booked, false),
       outcome_reply_id     = o.reply_id,
       attributed_at        = now(),
       status               = 'attributed'
  from (
    select e2.id as eid, x.reply_id, x.positive, x.booked
      from followup_experiments e2
      left join lateral (
        select r.id as reply_id,
               (lab.label = any(%(pos)s)) as positive,
               (lab.label = 'booked')      as booked
          from replies r
          join lateral (
            select c.label from classifications c
             where c.reply_id = r.id and c.model <> 'rule-based'
             order by c.classified_at desc limit 1
          ) lab on true
         where r.lead_email = e2.lead_email
           and r.reply_timestamp > e2.sent_marked_at
           and r.reply_timestamp <= e2.sent_marked_at + make_interval(days => %(win)s)
         order by r.reply_timestamp asc limit 1
      ) x on true
     where e2.status = 'sent' and e2.sent_message_id is not null
       and e2.attributed_at is null
       and (x.reply_id is not null
            or e2.sent_marked_at <= now() - make_interval(days => %(win)s))
  ) o
 where e.id = o.eid
"""


def attribute() -> dict:
    """Run linking + attribution. Returns {linked, attributed} counts."""
    conn = connect()
    try:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute(LINK_SQL)
            linked = cur.rowcount
            cur.execute(ATTRIB_SQL, {"pos": POSITIVE_LABELS, "win": OUTCOME_WINDOW_DAYS})
            attributed = cur.rowcount
    finally:
        conn.close()
    return {"linked": linked, "attributed": attributed}


def wilson(pos: int, n: int) -> tuple[float, float]:
    """95% Wilson score interval for a proportion, as percentages."""
    if n == 0:
        return (0.0, 0.0)
    z = 1.96
    p = pos / n
    denom = 1 + z * z / n
    center = (p + z * z / (2 * n)) / denom
    margin = (z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))) / denom
    return (round(100 * max(0.0, center - margin), 1), round(100 * min(1.0, center + margin), 1))


def fetch_results() -> dict:
    """Per-arm scoreboard + Wilson CIs + a winner verdict (only past the support
    floor and with non-overlapping CIs)."""
    conn = connect()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("select * from followup_ab_results")
        by_arm = {r["arm"]: dict(r) for r in cur.fetchall()}
    finally:
        conn.close()

    arms = {}
    for key, label in (("static", "Templates"), ("ai", "AI-written")):
        d = by_arm.get(key, {})
        decided = d.get("decided") or 0
        positives = d.get("positives") or 0
        ci = wilson(positives, decided)
        arms[key] = {
            "label": label, "decided": decided, "positives": positives,
            "booked": d.get("booked") or 0, "pending": d.get("pending_outcome") or 0,
            "awaiting": d.get("awaiting_send") or 0,
            "positive_pct": float(d["positive_pct"]) if d.get("positive_pct") is not None else None,
            "booked_pct": float(d["booked_pct"]) if d.get("booked_pct") is not None else None,
            "ci_lo": ci[0], "ci_hi": ci[1],
            "enough": decided >= MIN_DECIDED and positives >= MIN_POSITIVES,
        }

    s, a = arms["static"], arms["ai"]
    if not (s["enough"] and a["enough"]):
        verdict = {"state": "thin",
                   "text": "Not enough data yet to call a winner — keep sending."}
    elif s["ci_lo"] > a["ci_hi"]:
        verdict = {"state": "static", "text": "Templates are winning (clearly ahead)."}
    elif a["ci_lo"] > s["ci_hi"]:
        verdict = {"state": "ai", "text": "AI-written follow-ups are winning (clearly ahead)."}
    else:
        verdict = {"state": "tie", "text": "Too close to call — no clear winner yet."}

    return {"arms": arms, "verdict": verdict,
            "min_decided": MIN_DECIDED, "min_positives": MIN_POSITIVES}
