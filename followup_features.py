"""Follow-up message feature extraction (descriptive cross-lead analysis).

For every outbound MANUAL follow-up (`sent_messages.send_kind='unibox_manual'`):
  1. Strip the quoted thread -> the actual new text Jam wrote (BLOCKING prereq:
     95.7% of bodies are mostly quoted history; features on the raw body would
     measure the quote, not the message — verified Phase 0).
  2. Compute deterministic features over that new text (v1, zero-LLM).
  3. Attribute an outcome by windowed last-touch: the first inbound reply after
     this send and before the NEXT manual send to the same lead, labelled by the
     latest classification (`classified_at DESC` — never pin a prompt_version).
  4. Flag prior_positive_exists (reverse-causality guard) + is_confirmed_winner
     (overlay vs followup_winning_selection).
  5. Upsert into `followup_message_features` (idempotent on sent_message_id).

This feeds `followup_patterns_mv` (NocoDB) and the HTML report. DESCRIPTIVE only.

Usage:
    python followup_features.py            # extract/refresh deterministic features
    python followup_features.py --limit 50 # smoke test on a few rows
"""
from __future__ import annotations

import argparse
import re
import sys
from datetime import datetime, timezone

from db import connect

EXTRACTOR_VERSION = "fx1"
POSITIVE_LABELS = ("booked", "interested")

# --------------------------------------------------------------------------
# New-text extraction (quoted-thread stripping). Bodies are FLATTENED to a
# single line (no newlines) — verified — so boundary markers are NOT anchored
# to \n. The dominant marker is "On <date> at <time> <addr> wrote:".
# --------------------------------------------------------------------------
BOUNDARY_PATTERNS = [
    re.compile(r"On\s+.{0,160}?\bwrote:", re.IGNORECASE),
    re.compile(r"-{2,}\s*Original [Mm]essage\s*-{2,}", re.IGNORECASE),
    re.compile(r"\bFrom:\s.{0,160}?\bSubject:\s", re.IGNORECASE),
    re.compile(r"\bSent from my \w+", re.IGNORECASE),
]


def extract_new_text(subject: str | None, body: str | None) -> tuple[str, bool]:
    """Return (new_text, boundary_detected). Strips a leading echoed
    "Re:/Fwd: <subject>" then truncates at the earliest quoted-thread marker."""
    if not body:
        return "", False
    s = body
    if subject:
        s = re.sub(r"^\s*(re|fwd|fw)\s*:\s*" + re.escape(subject.strip()) + r"\s*",
                   "", s, flags=re.IGNORECASE)
    cut = len(s)
    found = False
    for pat in BOUNDARY_PATTERNS:
        m = pat.search(s)
        if m and m.start() < cut:
            cut = m.start()
            found = True
    return s[:cut].strip(), found


# --------------------------------------------------------------------------
# Deterministic features (computed over new_text ONLY)
# --------------------------------------------------------------------------
_URL = re.compile(r"https?://\S+", re.IGNORECASE)
_CAL = re.compile(r"calendly|cal\.com|savvycal|meetings?\.|hubspot.*meeting|book(ing)?\b.*\b(call|time|meeting)", re.IGNORECASE)
_PRICE = re.compile(r"\bpric(e|ing)\b|\bcost\b|\bfee\b|\bdiscount\b|\$\s?\d", re.IGNORECASE)
_PS = re.compile(r"\bP\.?\s?S\.?\b")
_GREETING = re.compile(r"^\s*(hi|hey|hello|dear|good (morning|afternoon|evening))\b", re.IGNORECASE)
_SIGNOFF = re.compile(r"\b(thanks|thank you|best|cheers|regards|talk soon|speak soon|warm(ly)?|sincerely)\b", re.IGNORECASE)
_EMOJI = re.compile("[\U0001F300-\U0001FAFF\U00002600-\U000027BF\U0001F000-\U0001F0FF]")
_ALLCAPS = re.compile(r"\b[A-Z]{3,}\b")
_SUBJ_PREFIX = re.compile(r"^\s*(re|fwd|fw)\s*:\s*[^.?!]{0,80}?\s+(?=(hi|hey|hello|dear)\b)", re.IGNORECASE)


def _strip_lead_subject(text: str) -> str:
    """Drop a leading 'Re: <subject>' echo up to the greeting, for the
    opens_with_question / greeting checks (best-effort)."""
    return _SUBJ_PREFIX.sub("", text, count=1)


def length_bucket(words: int) -> str:
    if words <= 15:
        return "very_short"
    if words <= 40:
        return "short"
    if words <= 90:
        return "medium"
    return "long"


def deterministic_features(new_text: str, sent_ts: datetime) -> dict:
    t = new_text or ""
    words = len(t.split())
    body_for_open = _strip_lead_subject(t)
    first_sentence = re.split(r"[.?!]", body_for_open, maxsplit=1)[0]
    # opens_with_question: the first terminator after the greeting is a '?'
    after_greet = _GREETING.sub("", body_for_open).lstrip(" ,")
    m_term = re.search(r"[.?!]", after_greet)
    opens_q = bool(m_term and after_greet[m_term.start()] == "?")
    # has_url but ignore obvious unsubscribe/opt-out anchors
    urls = [u for u in _URL.findall(t) if "unsub" not in u.lower() and "optout" not in u.lower()]
    return {
        "char_len": len(t),
        "word_count": words,
        "length_bucket": length_bucket(words),
        "has_question": "?" in t,
        "opens_with_question": opens_q,
        "has_url": bool(urls),
        "has_calendar_link": bool(_CAL.search(t)),
        "mentions_pricing": bool(_PRICE.search(t)),
        "has_ps": bool(_PS.search(t)),
        "has_greeting": bool(_GREETING.search(t)),
        "has_signoff": bool(_SIGNOFF.search(t)),
        "has_emoji": bool(_EMOJI.search(t)),
        "all_caps_word_count": len(_ALLCAPS.findall(t)),
        "send_dow": sent_ts.weekday() if sent_ts else None,        # 0=Mon
        "send_hour_utc": sent_ts.astimezone(timezone.utc).hour if sent_ts else None,
    }


# --------------------------------------------------------------------------
# Outcome attribution (windowed last-touch) + reverse-causality flag.
# Mirrors select_winning_replies.py: latest classification by classified_at,
# never filtered on a single prompt_version.
# --------------------------------------------------------------------------
ATTRIB_SQL = """
with manual as (
  select id, lead_email, sent_timestamp
  from sent_messages where send_kind = 'unibox_manual'
),
withnext as (
  select m.*,
         lead(sent_timestamp) over (partition by lead_email order by sent_timestamp) as next_out
  from manual m
)
select
  w.id,
  cr.credit_reply_id,
  (select cl.label from classifications cl
     where cl.reply_id = cr.credit_reply_id
     order by cl.classified_at desc limit 1)                              as reply_label,
  exists (
    select 1 from replies r2
    join classifications c2 on c2.reply_id = r2.id
    where r2.lead_email = w.lead_email
      and r2.reply_timestamp < w.sent_timestamp
      and c2.label in ('booked','interested')
      and c2.classified_at = (select max(c3.classified_at) from classifications c3 where c3.reply_id = r2.id)
  )                                                                        as prior_positive_exists
from withnext w
cross join lateral (
  select (select r.id from replies r
            where r.lead_email = w.lead_email
              and r.reply_timestamp > w.sent_timestamp
              and (w.next_out is null or r.reply_timestamp < w.next_out)
            order by r.reply_timestamp asc limit 1) as credit_reply_id
) cr
"""


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=None, help="cap rows (smoke test)")
    args = ap.parse_args()

    conn = connect()
    cur = conn.cursor()

    print("Fetching attribution (windowed last-touch)...")
    cur.execute(ATTRIB_SQL)
    attrib = {}
    for sid, credit_reply_id, reply_label, prior_pos in cur.fetchall():
        attrib[sid] = {
            "credit_reply_id": credit_reply_id,
            "reply_label": reply_label,
            "had_reply": credit_reply_id is not None,
            "responded_positive": reply_label in POSITIVE_LABELS,
            "responded_booked": reply_label == "booked",
            "prior_positive_exists": bool(prior_pos),
        }
    print(f"  attributed {len(attrib)} manual sends")

    cur.execute("select winning_sent_message_id from followup_winning_selection")
    winners = {r[0] for r in cur.fetchall() if r[0] is not None}
    print(f"  {len(winners)} confirmed-winner sends")

    sql = ("select id, lead_email, subject, body, sent_timestamp, client, campaign_name "
           "from sent_messages where send_kind='unibox_manual' order by lead_email, sent_timestamp")
    if args.limit:
        sql += f" limit {int(args.limit)}"
    cur.execute(sql)
    sends = cur.fetchall()

    # ffup_position per lead (asc by sent_timestamp)
    pos_counter: dict[str, int] = {}
    rows = []
    boundary_hits = 0
    for sid, lead, subject, body, ts, client, campaign in sends:
        pos_counter[lead] = pos_counter.get(lead, 0) + 1
        nt, found = extract_new_text(subject, body)
        if found:
            boundary_hits += 1
        feats = deterministic_features(nt, ts)
        a = attrib.get(sid, {})
        rows.append((
            sid, lead, pos_counter[lead], ts, nt, found, client, campaign,
            feats["char_len"], feats["word_count"], feats["length_bucket"],
            feats["has_question"], feats["opens_with_question"], feats["has_url"],
            feats["has_calendar_link"], feats["mentions_pricing"], feats["has_ps"],
            feats["has_greeting"], feats["has_signoff"], feats["has_emoji"],
            feats["all_caps_word_count"], feats["send_dow"], feats["send_hour_utc"],
            a.get("had_reply", False), a.get("reply_label"),
            a.get("responded_positive", False), a.get("responded_booked", False),
            a.get("prior_positive_exists", False), sid in winners,
            EXTRACTOR_VERSION,
        ))

    print(f"  extracted {len(rows)} rows; boundary detected on {boundary_hits} "
          f"({100*boundary_hits/max(len(rows),1):.1f}%)")

    cols = ("sent_message_id, lead_email, ffup_position, sent_timestamp, followup_new_text, "
            "boundary_detected, client, campaign_name, char_len, word_count, length_bucket, "
            "has_question, opens_with_question, has_url, has_calendar_link, mentions_pricing, "
            "has_ps, has_greeting, has_signoff, has_emoji, all_caps_word_count, send_dow, "
            "send_hour_utc, had_reply, reply_label, responded_positive, responded_booked, "
            "prior_positive_exists, is_confirmed_winner, extractor_version")
    ncols = len(cols.split(","))
    placeholders = ",".join(["%s"] * ncols)
    update = ", ".join(f"{c.strip()}=excluded.{c.strip()}" for c in cols.split(",")
                       if c.strip() != "sent_message_id")
    from psycopg2.extras import execute_values
    upsert = (f"insert into followup_message_features ({cols}) values %s "
              f"on conflict (sent_message_id) do update set {update}, extracted_at=now()")
    execute_values(cur, upsert, rows, page_size=500)
    conn.commit()
    print(f"Upserted {len(rows)} feature rows (extractor_version={EXTRACTOR_VERSION}).")
    conn.close()


if __name__ == "__main__":
    main()
