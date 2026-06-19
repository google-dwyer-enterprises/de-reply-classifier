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
    """Pick up to N curated templates for the scenario (fall back to any active),
    token-filled. Returns [{idx, text, template_id}]."""
    pool = templates_by_scenario.get(scenario) or []
    if not pool:  # fall back to whatever's active so the static arm is never empty
        pool = [t for items in templates_by_scenario.values() for t in items]
    out = []
    for i, t in enumerate(pool[:N_VARS]):
        out.append({
            "idx": i,
            "text": ft.fill_tokens(t["body"], first_name=lead.get("first_name"),
                                   company=lead.get("company")),
            "template_id": t["id"],
        })
    return out


def _build_gen_prompt() -> str:
    return open(_GEN_PROMPT_PATH, encoding="utf-8").read().replace("{n}", str(N_VARS))


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
        rows = cur.fetchall()
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
                        gen_prompt = _build_gen_prompt()
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


def fetch_for_view(client: str | None, since) -> list[dict]:
    """Experiments to show on the tool (assigned/sent, newest first)."""
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
        return [dict(r) for r in cur.fetchall()]
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
