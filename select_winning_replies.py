"""Winning-reply selector — Option D + D2 (FOLLOWUP_ANALYSIS_PLAN.md Phase 5).

For each booked lead, identifies which manual follow-up the lead's commitment
reply was responding to:
  1. Anchor on the reply classified 'booked' (from classifications).
  2. Gather the 2-3 most recent manual outbounds before that reply.
  3. Ask Haiku which candidate the reply was responding to.
  4. Store the selection in followup_winning_selection (or print in --dry-run).

CLI:
    python run.py select-winning-replies [--dry-run] [--limit N]
    python select_winning_replies.py [--dry-run] [--limit N]

Cost: ~$0.0005 per booked lead × ~128 leads = ~$0.06 first full run.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import anthropic
import psycopg2

from db import connect

PROMPT_VERSION = "v1"
MODEL = "claude-haiku-4-5"
PROMPT_TEMPLATE = (Path(__file__).parent / "prompts" / "select_winning_reply.txt").read_text(
    encoding="utf-8"
)
MAX_CANDIDATES = 3
LABELS = ["A", "B", "C"]


def fetch_booked_leads_needing_selection(conn, prompt_version: str,
                                          limit: int | None = None) -> list[str]:
    """Return lead_emails with auto_status='booked' that don't already have
    a selection row for this prompt_version."""
    with conn.cursor() as cur:
        sql = """
            select l.lead_email
            from leads l
            where l.auto_status = 'booked'
              and not exists (
                  select 1 from followup_winning_selection fws
                  where fws.lead_email = l.lead_email
                    and fws.prompt_version = %s
              )
            order by l.lead_email
        """
        if limit:
            sql += f" limit {int(limit)}"
        cur.execute(sql, (prompt_version,))
        return [r[0] for r in cur.fetchall()]


def fetch_booking_reply(conn, lead_email: str) -> dict | None:
    """Find the reply classified as 'booked' for this lead.
    Returns {reply_id, lead_email, reply_timestamp, body} or None."""
    with conn.cursor() as cur:
        cur.execute("""
            select r.id, r.lead_email, r.reply_timestamp, r.body
            from replies r
            join classifications c on c.reply_id = r.id
            where r.lead_email = %s
              and c.label = 'booked'
            order by c.classified_at desc
            limit 1
        """, (lead_email,))
        row = cur.fetchone()
        if not row:
            return None
        return {
            "reply_id": row[0],
            "lead_email": row[1],
            "reply_timestamp": row[2],
            "body": row[3],
        }


def fetch_candidates(conn, lead_email: str,
                      before_ts) -> list[dict]:
    """Return the N most recent manual outbounds before before_ts."""
    with conn.cursor() as cur:
        cur.execute("""
            select id, sent_timestamp, subject, body
            from sent_messages
            where lead_email = %s
              and send_kind = 'unibox_manual'
              and sent_timestamp < %s
            order by sent_timestamp desc
            limit %s
        """, (lead_email, before_ts, MAX_CANDIDATES))
        rows = cur.fetchall()
    # Reverse so oldest is first (chronological order for the prompt)
    rows.reverse()
    return [
        {"id": r[0], "sent_timestamp": r[1], "subject": r[2], "body": r[3]}
        for r in rows
    ]


def fetch_last_manual_before_booking(conn, lead_email: str) -> dict | None:
    """Option A fallback: most recent manual outbound before auto_status flip."""
    with conn.cursor() as cur:
        cur.execute("""
            select s.id, s.sent_timestamp, s.subject, s.body
            from sent_messages s
            join leads l on l.lead_email = s.lead_email
            where s.lead_email = %s
              and s.send_kind = 'unibox_manual'
            order by s.sent_timestamp desc
            limit 1
        """, (lead_email,))
        row = cur.fetchone()
        if not row:
            return None
        return {"id": row[0], "sent_timestamp": row[1],
                "subject": row[2], "body": row[3]}


def build_candidates_block(candidates: list[dict]) -> str:
    """Format candidates for the prompt."""
    parts = []
    for i, c in enumerate(candidates):
        label = LABELS[i]
        ts = c["sent_timestamp"]
        subj = c.get("subject") or "(no subject)"
        body = (c.get("body") or "")[:500]
        parts.append(f'[{label}] {ts}  subject="{subj}"\n    body: """{body}"""')
    return "\n\n".join(parts)


def haiku_pick(client: anthropic.Anthropic, booking_reply: dict,
               candidates: list[dict]) -> dict:
    """Call Haiku to select the winning outbound. Returns parsed JSON."""
    committed_body = (booking_reply.get("body") or "")[:500]
    reply_ts = booking_reply["reply_timestamp"]
    candidates_block = build_candidates_block(candidates)

    prompt_text = (PROMPT_TEMPLATE
                   .replace("{committed_body}", committed_body)
                   .replace("{reply_timestamp}", str(reply_ts))
                   .replace("{candidates_block}", candidates_block))

    resp = client.messages.create(
        model=MODEL,
        max_tokens=200,
        temperature=0,
        messages=[{"role": "user", "content": prompt_text}],
    )

    raw = resp.content[0].text.strip()
    # Strip markdown fences if present
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[1] if "\n" in raw else raw[3:]
        if raw.endswith("```"):
            raw = raw[:-3]
        raw = raw.strip()

    try:
        result = json.loads(raw)
    except json.JSONDecodeError:
        return {"winning_outbound": "A", "confidence": "low",
                "rationale": f"JSON parse failed: {raw[:100]}"}

    # Map letter to index
    letter = result.get("winning_outbound", "A").upper()
    idx = LABELS.index(letter) if letter in LABELS else 0
    if idx >= len(candidates):
        idx = len(candidates) - 1

    return {
        "index": idx,
        "confidence": result.get("confidence", "low"),
        "rationale": result.get("rationale", ""),
    }


def write_selection(conn, lead_email: str, winning: dict,
                    anchor_reply_id: int | None,
                    candidates: list[int],
                    confidence: str, rationale: str,
                    prompt_version: str, model: str,
                    anchor_body: str | None = None) -> None:
    """Insert into followup_winning_selection."""
    with conn.cursor() as cur:
        cur.execute("""
            insert into followup_winning_selection
              (lead_email, winning_sent_message_id, booking_reply_id,
               candidate_message_ids, confidence, rationale,
               model, prompt_version,
               winning_subject, winning_body, booking_reply_body)
            values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            on conflict (lead_email, prompt_version) do update set
              winning_sent_message_id = excluded.winning_sent_message_id,
              booking_reply_id = excluded.booking_reply_id,
              candidate_message_ids = excluded.candidate_message_ids,
              confidence = excluded.confidence,
              rationale = excluded.rationale,
              model = excluded.model,
              winning_subject = excluded.winning_subject,
              winning_body = excluded.winning_body,
              booking_reply_body = excluded.booking_reply_body,
              selected_at = now()
        """, (lead_email, winning["id"], anchor_reply_id or 0,
              candidates, confidence, rationale,
              model, prompt_version,
              winning.get("subject"), winning.get("body"),
              anchor_body))
    conn.commit()


def main(dry_run: bool = False, limit: int | None = None) -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    conn = connect()

    # Check if table exists (for dry-run we might not have it yet)
    table_exists = True
    try:
        with conn.cursor() as cur:
            cur.execute("""
                select 1 from pg_tables
                where schemaname = 'public'
                  and tablename = 'followup_winning_selection'
            """)
            if not cur.fetchone():
                table_exists = False
    except Exception:
        table_exists = False

    if not table_exists and not dry_run:
        sys.exit("FATAL: followup_winning_selection table doesn't exist. "
                 "Run the DDL from migrations.sql first.")

    if table_exists:
        targets = fetch_booked_leads_needing_selection(conn, PROMPT_VERSION, limit)
    else:
        # dry-run without the table: just grab all booked leads
        with conn.cursor() as cur:
            sql = """select lead_email from leads
                     where auto_status = 'booked' order by lead_email"""
            if limit:
                sql += f" limit {int(limit)}"
            cur.execute(sql)
            targets = [r[0] for r in cur.fetchall()]

    print(f"Booked leads to process: {len(targets)}")
    if not targets:
        print("Nothing to do.")
        conn.close()
        return

    haiku_client = anthropic.Anthropic()
    selected = 0
    fallback = 0
    skipped_no_reply = 0
    skipped_no_candidates = 0

    def safe_query(fn, *args):
        """Run a DB function, reconnecting once on stale connection."""
        nonlocal conn
        try:
            return fn(conn, *args)
        except (psycopg2.OperationalError, psycopg2.InterfaceError):
            print("  (reconnecting to Supabase...)")
            conn = connect()
            return fn(conn, *args)

    for lead in targets:
        anchor = safe_query(fetch_booking_reply, lead)
        if anchor is None:
            # Calendly-only booking — Option A fallback
            winning = safe_query(fetch_last_manual_before_booking, lead)
            if winning:
                if dry_run:
                    print(f"\n  [{lead}] FALLBACK (no booking reply classified)")
                    print(f"    Last manual outbound: {winning['sent_timestamp']}")
                    print(f"    Subject: {winning.get('subject')!r}")
                    print(f"    Body: {(winning.get('body') or '')[:120]!r}")
                else:
                    write_selection(conn, lead, winning,
                                   anchor_reply_id=None,
                                   candidates=[winning["id"]],
                                   confidence="fallback",
                                   rationale="No email commitment reply; fell back to most recent manual before booking.",
                                   prompt_version=PROMPT_VERSION,
                                   model="fallback",
                                   anchor_body=None)
                fallback += 1
            else:
                skipped_no_candidates += 1
                if dry_run:
                    print(f"\n  [{lead}] SKIPPED (no booking reply + no manual outbounds)")
            continue

        candidates = safe_query(fetch_candidates, lead, anchor["reply_timestamp"])
        if not candidates:
            skipped_no_candidates += 1
            if dry_run:
                print(f"\n  [{lead}] SKIPPED (booking reply found but no manual outbounds before it)")
                print(f"    Booking reply at: {anchor['reply_timestamp']}")
                print(f"    Lead said: {(anchor.get('body') or '')[:120]!r}")
            continue

        # Haiku selection
        outcome = haiku_pick(haiku_client, anchor, candidates)
        winning = candidates[outcome["index"]]

        if dry_run:
            print(f"\n  [{lead}] SELECTED (confidence={outcome['confidence']})")
            print(f"    Booking reply at: {anchor['reply_timestamp']}")
            print(f"    Lead said: {(anchor.get('body') or '')[:120]!r}")
            print(f"    Candidates ({len(candidates)}):")
            for i, c in enumerate(candidates):
                marker = " <<< WINNER" if i == outcome["index"] else ""
                print(f"      [{LABELS[i]}] {c['sent_timestamp']}  "
                      f"subject={c.get('subject')!r}{marker}")
                print(f"          body: {(c.get('body') or '')[:100]!r}")
            print(f"    Rationale: {outcome['rationale']}")
        else:
            write_selection(conn, lead, winning,
                            anchor_reply_id=anchor["reply_id"],
                            candidates=[c["id"] for c in candidates],
                            confidence=outcome["confidence"],
                            rationale=outcome["rationale"],
                            prompt_version=PROMPT_VERSION,
                            model=MODEL,
                            anchor_body=anchor.get("body"))
        selected += 1

    print(f"\n=== summary ===")
    print(f"  selected:             {selected}")
    print(f"  fallback:             {fallback}")
    print(f"  skipped (no reply):   {skipped_no_reply}")
    print(f"  skipped (no cands):   {skipped_no_candidates}")
    if dry_run:
        print(f"  mode:                 DRY RUN (no DB writes)")

    conn.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(prog="select_winning_replies.py")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print selections without writing to DB")
    parser.add_argument("--limit", type=int, default=None,
                        help="Process only N booked leads (for testing)")
    args = parser.parse_args()
    main(dry_run=args.dry_run, limit=args.limit)
