"""Interest follow-up A/B — assignment, generation, and lifecycle (Phase 2).

For each lead that replied with interest, this:
  - deterministically assigns an arm (static templates vs AI-generated) per reply,
  - generates 1-3 ready-to-send variations (static = curated templates token-filled;
    ai = Haiku drafts tailored to the reply),
  - persists the experiment so variations are generated ONCE, not on every load,
  - records which variation Jam marks "sent".

Outcome attribution + the results view live in followup_experiments_attrib.py.
See docs/replies/INTEREST_FOLLOWUP_AB_PLAN.md.
"""
from __future__ import annotations

import hashlib
import json
import os
import re

import psycopg2.extras

import config
import followup_templates_data as ft
from db import connect

INTEREST_LABELS = list(config.FOLLOWUP_INTEREST_LABELS)
N_VARS = config.FOLLOWUP_GEN_N_VARIATIONS

# interest-reply label -> static-template scenario bucket
SCENARIO_FOR_LABEL = {"interested": "interested_general", "booked": "booked_nudge"}

_GEN_PROMPT_PATH = os.path.join(os.path.dirname(__file__), "prompts", "followup_generate.txt")


def assign_arm(reply_id: int) -> str:
    """Deterministic 50/50 split keyed on the reply id (stable + reproducible,
    so the assignment never changes and there's no Math.random non-determinism)."""
    h = int(hashlib.md5(str(reply_id).encode()).hexdigest(), 16)
    return "ai" if h % 2 else "static"


# --------------------------------------------------------------------------- #
# Candidate interest replies (latest label in the interest set, no experiment yet)
# --------------------------------------------------------------------------- #
def _candidate_sql(with_client: bool) -> str:
    client_clause = "and r.client = %s" if with_client else ""
    return f"""
    with latest as (
      select distinct on (c.reply_id) c.reply_id, c.label
        from classifications c where c.model <> 'rule-based'
        order by c.reply_id, c.classified_at desc
    ),
    cand as (
      select r.id, r.lead_email, r.client, r.subject, r.body, r.reply_timestamp, l.label
        from latest l
        join replies r on r.id = l.reply_id
        left join followup_experiments e on e.source_reply_id = r.id
       where l.label = any(%s) and e.id is null
         and r.reply_timestamp >= %s {client_clause}
         -- drop Google-Calendar notification emails (the 'booked' noise): they're
         -- the booking itself, not a lead message that needs a follow-up.
         and coalesce(r.body,'') not ilike '%%Join with Google Meet%%'
         and coalesce(r.body,'') not ilike '%%meet.google.com%%'
         and coalesce(r.body,'') not ilike '%%This event has been updated%%'
    ),
    dedup as (
      select distinct on (lead_email) * from cand
       order by lead_email, reply_timestamp desc
    )
    select id, lead_email, client, subject, body, reply_timestamp, label
      from dedup order by reply_timestamp desc limit %s
    """


def _lead_info(cur, lead_email: str) -> dict:
    cur.execute("""
        select coalesce(lc.first_name, l.first_name)                       as first_name,
               coalesce(lc.resolved_company_name, lc.company_name)         as company
          from (select %s::text as e) x
          left join lead_contacts lc on lc.lead_email = x.e
          left join leads l on l.lead_email = x.e
    """, (lead_email,))
    r = cur.fetchone()
    return {"first_name": (r[0] if r else None), "company": (r[1] if r else None)}


# --------------------------------------------------------------------------- #
# Variation generation
# --------------------------------------------------------------------------- #
def build_static_variations(scenario: str, lead: dict, templates_by_scenario: dict) -> list[dict]:
    """Pick up to N curated templates for the scenario (fall back to any active).
    Kept as placeholder templates ({first_name}/{last_name}/{company}) — NOT
    filled with the real name — so the suggestion shows placeholders for the user
    to fill. Returns [{idx, text, template_id}]."""
    pool = templates_by_scenario.get(scenario) or []
    if not pool:  # fall back to whatever's active so the static arm is never empty
        pool = [t for items in templates_by_scenario.values() for t in items]
    out = []
    for i, t in enumerate(pool[:N_VARS]):
        out.append({"idx": i, "text": t["body"], "template_id": t["id"]})
    return out


def fetch_top_exemplars(cur, k: int = 6) -> list[str]:
    """Best real follow-ups (booked-first, concise, deduped) to show the AI as
    style exemplars so it prioritises what actually works. Pulled from
    followup_message_features (same source as the Best-replies page)."""
    cur.execute("""
        select followup_new_text, responded_booked
          from followup_message_features
         where extractor_version = 'fx1' and boundary_detected
           and responded_positive
           and coalesce(btrim(followup_new_text), '') <> ''
           and char_len between 30 and 400
         order by responded_booked desc, is_confirmed_winner desc,
                  ffup_position asc, sent_timestamp desc
         limit 40
    """)
    seen, out = set(), []
    for (txt, _booked) in cur.fetchall():
        t = re.sub(r"\s+", " ", (txt or "").replace("�", "")).strip()
        if len(t) < 30:
            continue
        key = re.sub(r"[^a-z0-9]", "", t.lower())[:60]
        if key in seen:
            continue
        seen.add(key)
        out.append(t[:400])
        if len(out) >= k:
            break
    return out


def _build_gen_prompt(exemplars: list[str] | None = None) -> str:
    base = open(_GEN_PROMPT_PATH, encoding="utf-8").read().replace("{n}", str(N_VARS))
    if exemplars:
        ex = "\n".join(f'{i + 1}. "{t}"' for i, t in enumerate(exemplars))
        base += (
            "\n\nFor reference, here are real follow-ups WE have sent that earned a "
            "positive reply or booked a call. Match their natural, concise, direct "
            "style and structure — but do NOT copy them verbatim or reuse their "
            "specific names, numbers, or details:\n" + ex
        )
    return base


def _parse_drafts(text: str) -> list[str]:
    t = re.sub(r"^```\w*\s*|\s*```$", "", (text or "").strip(), flags=re.S).strip()
    m = re.search(r"\[.*\]", t, re.S)
    if m:
        t = m.group(0)
    try:
        arr = json.loads(t)
    except Exception:
        return []
    out = []
    for v in arr if isinstance(arr, list) else []:
        s = (v if isinstance(v, str) else str(v)).strip()
        if s:
            out.append(s)
    return out[:N_VARS]


def build_ai_variations(client_anthropic, system_prompt: str, subject: str | None,
                        reply_text: str, lead: dict) -> list[dict]:
    """Haiku-generate up to N tailored drafts. Returns [{idx, text}] (may be []
    on a parse/API failure — caller then skips creating the experiment)."""
    import classify
    from followup_features import extract_new_text
    new_text, _ = extract_new_text(subject, reply_text or "")
    clean = (new_text or reply_text or "")[:1500]
    user = (f"The lead's reply:\n\"{clean}\"\n\n"
            f"Lead first name: {lead.get('first_name') or '(unknown)'}\n"
            f"Lead company: {lead.get('company') or '(unknown)'}")
    raw = classify.call_haiku(client_anthropic, system_prompt, user,
                              model=config.FOLLOWUP_GEN_MODEL)
    return [{"idx": i, "text": s} for i, s in enumerate(_parse_drafts(raw))]


# --------------------------------------------------------------------------- #
# Lifecycle
# --------------------------------------------------------------------------- #
def ensure_experiments(client: str | None, since, cap: int = 20) -> int:
    """Find interest replies with no experiment yet, assign + generate + persist.
    Bounded by `cap` per call to keep page latency / LLM cost sane. Returns count
    created. AI-arm rows that fail generation are skipped (retried next call)."""
    conn = connect()
    created = 0
    anthropic_client = None
    gen_prompt = None
    ai_disabled = False   # set if this host has no Anthropic key/package
    try:
        cur = conn.cursor()
        params = ([INTEREST_LABELS, since, client, cap] if client
                  else [INTEREST_LABELS, since, cap])
        cur.execute(_candidate_sql(bool(client)), params)
        # Drop excluded senders (internal addresses, bots, do-not-reply, etc.)
        rows = [r for r in cur.fetchall() if not config.is_excluded_sender(r[1])]
        templates_by_scenario = ft.fetch_active_templates()

        for rid, lead_email, rclient, subject, body, ts, label in rows:
            arm = assign_arm(rid)
            lead = _lead_info(cur, lead_email)
            if arm == "static":
                scenario = SCENARIO_FOR_LABEL.get(label, "interested_general")
                variations = build_static_variations(scenario, lead, templates_by_scenario)
            else:
                if ai_disabled:
                    continue   # AI arm unavailable on this host — skip; retried elsewhere
                if anthropic_client is None:
                    try:
                        from anthropic import Anthropic
                        anthropic_client = Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
                        # Feed the AI the top-performing real replies as style
                        # exemplars so it prioritises what actually works.
                        gen_prompt = _build_gen_prompt(fetch_top_exemplars(cur))
                    except Exception:
                        # No ANTHROPIC_API_KEY / anthropic package here (e.g. the web
                        # service). Don't 500 the page — skip AI-arm rows this call.
                        ai_disabled = True
                        continue
                try:
                    variations = build_ai_variations(anthropic_client, gen_prompt, subject, body, lead)
                except Exception:
                    variations = []
            if not variations:
                continue  # nothing to show — skip; retried on the next load
            with conn.cursor() as wc:
                wc.execute("""
                    insert into followup_experiments
                      (source_reply_id, lead_email, client, arm, variations)
                    values (%s,%s,%s,%s,%s)
                    on conflict (source_reply_id) do nothing
                """, (rid, lead_email, rclient, arm, psycopg2.extras.Json(variations)))
            conn.commit()
            created += 1
    finally:
        conn.close()
    return created


def _attach_threads(cur, rows: list[dict]) -> None:
    """Attach the full conversation thread + the follow-up number to each row so
    Jam can see WHICH follow-up a suggestion is and the history behind it.

    Thread = our outbound (sent_messages) + their inbound (replies), merged
    chronologically per lead. Outbound is numbered: first = 'Initial email',
    then 'Follow-up #1, #2…'. `next_followup_num` = the number the suggested
    reply would carry. Best-effort and read-only — reflects only what we've
    synced (an unsynced send can make the number approximate)."""
    from followup_analytics import humanize_text  # run-on / glyph repair
    emails = sorted({(r["lead_email"] or "").lower() for r in rows if r.get("lead_email")})
    if not emails:
        return
    cur.execute("""
        select lower(lead_email) as le, sent_timestamp as ts, 'out' as dir, subject, body
          from sent_messages where lower(lead_email) = any(%s)
        union all
        select lower(lead_email) as le, reply_timestamp as ts, 'in' as dir, subject, body
          from replies where lower(lead_email) = any(%s)
        order by le, ts
    """, (emails, emails))
    by_email: dict[str, list] = {}
    for m in cur.fetchall():
        by_email.setdefault(m["le"], []).append(m)

    for r in rows:
        msgs = by_email.get((r["lead_email"] or "").lower(), [])
        thread, out_i = [], 0
        for m in msgs:
            if m["dir"] == "out":
                out_i += 1
                label = "Initial email" if out_i == 1 else f"Follow-up #{out_i - 1}"
            else:
                label = "Lead replied"
            thread.append({
                "dir": m["dir"], "label": label,
                "date": m["ts"].strftime("%b %d, %Y") if m["ts"] else "",
                "subject": (m["subject"] or "").strip(),
                "body": humanize_text((m["body"] or "").replace("�", "").strip())[:2000],
            })
        r["thread"] = thread
        # next outbound = follow-up #out_i (initial isn't a follow-up); >=1 for display
        r["next_followup_num"] = max(out_i, 1)
        r["thread_partial"] = out_i == 0  # nothing outbound synced -> number is a guess


import datetime as _dt
_SCORE_TS = _dt.datetime(2026, 1, 1, 12, 0)  # only text features used; send timing ignored


def _ordinal(n: int) -> str:
    if 10 <= n % 100 <= 20:
        suf = "th"
    else:
        suf = {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suf}"


def _suggestion_score(text: str) -> float:
    """Deterministic (zero-LLM) pattern-fit score — nudges the estimate by traits
    that historically correlate with replies: a clear ask/booking link (the
    winning 'direct CTA'), concise length, and a question. Transparent + cheap."""
    from followup_features import deterministic_features
    df = deterministic_features(text or "", _SCORE_TS)
    s = 0.0
    if df.get("has_calendar_link"):
        s += 2.0                                                   # real booking link — strongest ask
    elif re.search(r"\b(book|call|calendar|schedule|meet|chat|time)\b", text or "", re.I):
        s += 1.0                                                   # at least a clear ask
    lb = df.get("length_bucket")
    if lb in ("short", "medium"):
        s += 1.0                                                   # concise wins
    elif lb == "long":
        s -= 1.0
    if df.get("has_question"):
        s += 0.5                                                   # a question invites a reply
    return s


def _stage_rates(cur) -> tuple[dict, float]:
    """Real positive-reply rate (%) per follow-up position, from history. Thin
    positions (<20 sends) fall back to the overall rate so we don't show noise."""
    cur.execute("""
        select case when ffup_position >= 6 then 6 else ffup_position end as stage,
               count(*) as sends,
               count(*) filter (where responded_positive) as pos
          from followup_message_features
         where extractor_version = 'fx1' and boundary_detected and ffup_position >= 1
         group by 1
    """)
    rows = cur.fetchall()
    total_sends = sum(r["sends"] for r in rows) or 1
    overall = 100.0 * sum(r["pos"] for r in rows) / total_sends
    rates = {r["stage"]: (100.0 * r["pos"] / r["sends"]) if r["sends"] >= 20 else overall
             for r in rows}
    return rates, overall


def _tokenize_variations(cur, rows: list[dict]) -> None:
    """Replace the lead's real first/last name + company in every suggested reply
    with {first_name}/{last_name}/{company} placeholders, so the user fills them
    deliberately (never a wrong auto-filled name). Covers both arms and existing
    rows; the static arm is also stored unfilled at generation as a backstop."""
    from followup_best_replies_data import _tokenize
    emails = sorted({(r["lead_email"] or "").lower() for r in rows if r.get("lead_email")})
    if not emails:
        return
    cur.execute("""
        select x.e as le,
               coalesce(lc.first_name, l.first_name)               as first_name,
               coalesce(lc.last_name,  l.last_name)                as last_name,
               coalesce(lc.resolved_company_name, lc.company_name) as company
          from unnest(%s::text[]) as x(e)
          left join lead_contacts lc on lower(lc.lead_email) = x.e
          left join leads l on lower(l.lead_email) = x.e
    """, (emails,))
    info = {r["le"]: r for r in cur.fetchall()}
    for r in rows:
        i = info.get((r["lead_email"] or "").lower()) or {}
        for v in (r.get("variations") or []):
            v["text"], _ = _tokenize(v.get("text", ""), i.get("first_name"),
                                     i.get("last_name"), i.get("company"))


def _attach_estimates(cur, rows: list[dict]) -> None:
    """Attach a friendly follow-up ordinal, the real stage reply rate, and a
    per-suggestion ESTIMATED reply chance (stage rate nudged by pattern-fit).
    Estimate, not a promise — the UI says so."""
    rates, overall = _stage_rates(cur)
    for r in rows:
        n = r.get("next_followup_num") or 1
        base = rates.get(min(n, 6), overall)
        r["followup_ordinal"] = _ordinal(n)
        r["stage_rate"] = round(base)
        variations = r.get("variations") or []
        scores = [_suggestion_score(v.get("text", "")) for v in variations]
        lo, hi = (min(scores), max(scores)) if scores else (0.0, 0.0)
        rng = hi - lo
        best_i = scores.index(hi) if scores else -1
        for i, v in enumerate(variations):
            norm = ((scores[i] - lo) / rng) if rng else 0.5       # 0..1 (0.5 if all equal)
            v["est_chance"] = max(1, round(base * (0.8 + 0.4 * norm)))  # 0.8x..1.2x of base
            v["star"] = (i == best_i)


def fetch_for_view(client: str | None, since) -> list[dict]:
    """Experiments to show on the tool (assigned/sent, newest first), each with
    its conversation thread + follow-up number attached."""
    conn = connect()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        clause = "and e.client = %s" if client else ""
        params = ([since, client] if client else [since])
        cur.execute(f"""
            select e.id, e.lead_email, e.client, e.arm, e.variations,
                   e.chosen_variation_idx, e.status, e.assigned_at,
                   r.subject, r.body, r.reply_timestamp
              from followup_experiments e
              join replies r on r.id = e.source_reply_id
             where e.status in ('assigned','sent')
               and r.reply_timestamp >= %s {clause}
             order by e.status asc, r.reply_timestamp desc
        """, params)
        rows = [dict(r) for r in cur.fetchall()
                if not config.is_excluded_sender(r["lead_email"])]
        _attach_threads(cur, rows)
        _tokenize_variations(cur, rows)
        _attach_estimates(cur, rows)
        return rows
    finally:
        conn.close()


def fetch_clients() -> list[str]:
    conn = connect()
    try:
        cur = conn.cursor()
        cur.execute("select distinct client from replies where coalesce(btrim(client),'')<>'' "
                    "order by client")
        return [r[0] for r in cur.fetchall()]
    finally:
        conn.close()


def mark_sent(exp_id: int, variation_idx: int) -> None:
    conn = connect()
    try:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute("select variations from followup_experiments where id=%s", (exp_id,))
            row = cur.fetchone()
            if not row:
                return
            variations = row[0] or []
            chosen = next((v.get("text") for v in variations
                           if v.get("idx") == variation_idx), None)
            cur.execute("""
                update followup_experiments
                   set chosen_variation_idx=%s, chosen_text=%s,
                       status='sent', sent_marked_at=now()
                 where id=%s
            """, (variation_idx, chosen, exp_id))
    finally:
        conn.close()


def skip(exp_id: int) -> None:
    conn = connect()
    try:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute("update followup_experiments set status='skipped' where id=%s "
                        "and status='assigned'", (exp_id,))
    finally:
        conn.close()
