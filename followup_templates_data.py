"""Data layer for the curated follow-up template library (Phase 1 of
INTEREST_FOLLOWUP_AB_PLAN.md): the "best replies, use this" page and the
curation admin. Also serves the static arm of the A/B test (Phase 2).

Read/write helpers over `followup_templates`, plus a read-only "candidates"
query that surfaces top-performing REAL follow-ups (from
`followup_message_features`) as suggestions Jam/Victor can approve into the
library. Nothing auto-promotes.
"""
from __future__ import annotations

import re

import psycopg2.extras

from db import connect

# Scenario buckets a template can fit. (key, friendly label) — drives the
# dropdown and the grouping on the best-replies page.
SCENARIOS: list[tuple[str, str]] = [
    ("interested_general", "They're interested — keep it moving"),
    ("pricing_ask",        "They asked about price or details"),
    ("booked_nudge",       "Lock in / confirm a time"),
    ("reengagement",       "Re-warm a lead who went quiet"),
    ("other",              "Other / general"),
]
SCENARIO_LABEL = dict(SCENARIOS)
VALID_SCENARIOS = set(SCENARIO_LABEL)

_TOKEN_RE = re.compile(r"\{(first_name|company)\}")


def fill_tokens(body: str, *, first_name: str | None = None,
                company: str | None = None) -> str:
    """Substitute {first_name}/{company} tokens. Blank values fall back to a
    neutral word so a copied template never shows a literal '{first_name}'."""
    repl = {"first_name": (first_name or "there").strip() or "there",
            "company": (company or "your team").strip() or "your team"}
    return _TOKEN_RE.sub(lambda m: repl[m.group(1)], body or "")


def _conn():
    return connect()


def fetch_active_templates() -> dict[str, list[dict]]:
    """Active templates grouped by scenario (in SCENARIOS order) for the
    best-replies page."""
    conn = _conn()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("""
            select id, scenario_key, title, body, subject, approved_by, updated_at
              from followup_templates
             where is_active
             order by scenario_key, updated_at desc
        """)
        rows = cur.fetchall()
    finally:
        conn.close()
    grouped: dict[str, list[dict]] = {}
    for r in rows:
        grouped.setdefault(r["scenario_key"], []).append(dict(r))
    # return in canonical scenario order, only non-empty buckets
    return {k: grouped[k] for k, _ in SCENARIOS if k in grouped}


def fetch_all_templates() -> list[dict]:
    """Every template (active + inactive) for the curation admin, ORDERED BY
    performance. Performance is attributed from the A/B test: each `static`-arm
    experiment whose chosen variation carried this template's id counts as a
    send, and `responded_positive` as a win. Active templates with the best
    reply rate sort first; templates with no sends yet keep a stable order and
    are flagged 'no data yet' (the A/B only recently started)."""
    conn = _conn()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("""
            with perf as (
              select (variations -> chosen_variation_idx ->> 'template_id')::bigint as tid,
                     count(*) as sends,
                     count(*) filter (where responded_positive) as pos
                from followup_experiments
               where arm = 'static' and chosen_variation_idx is not null
                 and (variations -> chosen_variation_idx ->> 'template_id') is not null
               group by 1
            )
            select t.id, t.scenario_key, t.title, t.body, t.subject, t.is_active,
                   t.approved_by, t.source_note, t.version, t.updated_at,
                   coalesce(p.sends, 0) as sends,
                   coalesce(p.pos, 0)   as pos
              from followup_templates t
              left join perf p on p.tid = t.id
             order by t.is_active desc,
                      (p.pos::float / nullif(p.sends, 0)) desc nulls last,
                      coalesce(p.sends, 0) desc,
                      t.scenario_key, t.updated_at desc
        """)
        rows = [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()
    for r in rows:
        r["rate"] = round(100.0 * r["pos"] / r["sends"]) if r["sends"] else None
    return rows



def upsert_template(*, template_id: int | None, scenario_key: str, title: str,
                    body: str, subject: str | None, approved_by: str | None,
                    source_note: str | None = None, is_active: bool = True) -> None:
    """Insert a new template or update an existing one (bumps version on edit)."""
    if scenario_key not in VALID_SCENARIOS:
        scenario_key = "other"
    conn = _conn()
    try:
        conn.autocommit = True
        with conn.cursor() as cur:
            if template_id:
                cur.execute("""
                    update followup_templates
                       set scenario_key=%s, title=%s, body=%s, subject=%s,
                           approved_by=%s, is_active=%s,
                           version=version+1, updated_at=now()
                     where id=%s
                """, (scenario_key, title, body, subject, approved_by,
                      is_active, template_id))
            else:
                cur.execute("""
                    insert into followup_templates
                      (scenario_key, title, body, subject, approved_by,
                       source_note, is_active)
                    values (%s,%s,%s,%s,%s,%s,%s)
                """, (scenario_key, title, body, subject, approved_by,
                      source_note, is_active))
    finally:
        conn.close()


def set_active(template_id: int, active: bool) -> None:
    conn = _conn()
    try:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute("update followup_templates set is_active=%s, updated_at=now() "
                        "where id=%s", (active, template_id))
    finally:
        conn.close()


def fetch_candidates(limit: int = 12) -> list[dict]:
    """Top-performing real follow-ups as template candidates (read-only).

    Pulls cleanly-extracted manual follow-ups that earned a positive outcome,
    best-first (booked, then confirmed winner, then recent), de-duplicated by a
    normalized opening so near-identical bumps don't crowd the list.
    """
    conn = _conn()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("""
            select followup_new_text, client, responded_booked, sent_timestamp
              from followup_message_features
             where extractor_version='fx1' and boundary_detected
               and responded_positive
               and coalesce(btrim(followup_new_text),'') <> ''
             order by responded_booked desc, is_confirmed_winner desc,
                      sent_timestamp desc
            limit 200
        """)
        rows = cur.fetchall()
    finally:
        conn.close()

    from followup_analytics import humanize_text  # lazy: reuse the run-on/glyph repair

    seen: set[str] = set()
    out: list[dict] = []
    for r in rows:
        txt = humanize_text((r["followup_new_text"] or "").replace("�", "").strip())
        if len(txt) < 20:
            continue
        key = re.sub(r"[^a-z0-9]", "", txt.lower())[:60]
        if key in seen:
            continue
        seen.add(key)
        out.append({
            "text": txt[:800],
            "client": r["client"],
            "booked": r["responded_booked"],
        })
        if len(out) >= limit:
            break
    return out
