"""Data layer for the 'Winning follow-up patterns' section (task #3): shifts the
templates page from exact emails toward GENERIC STRUCTURES.

Derives, from the LLM-tagged structural features in `followup_message_features`
(hook / tone / CTA / personalization — the FOLLOWUP_FEATURE_SPEC enums), the
best-performing option per building block and assembles a suggested skeleton
("value prop -> neutral -> direct -> light").

DESCRIPTIVE, with two honesty guards baked in:
  * Rates are associations, not causal. Most positive replies come from
    already-warm leads: on FRESH outreach (prior_positive_exists = false) the
    positive rate collapses to ~2% with too few positives to distinguish
    patterns (`fresh_mature` reports this). So this guides wording; it does not
    promise a cold-lead reply rate.
  * The confounded "absence" categories (no hook / no CTA / no personalization —
    dominated by reminder-bumps to warm leads) are shown but EXCLUDED from the
    assembled recommendation (NON_ACTIONABLE).
"""
from __future__ import annotations

import psycopg2.extras

from category_booking_data import wilson  # reuse the Wilson 95% interval
from config import FOLLOWUP_FEATURE_SPEC
from db import connect

# Building blocks, in the order they appear in a message, with friendly labels.
DIMS: list[tuple[str, str, str]] = [
    ("hook_type",       "Opening hook",    "how the message opens"),
    ("tone",            "Tone",            "the voice it's written in"),
    ("cta_style",       "The ask",         "how it asks for the next step"),
    ("personalization", "Personalization", "how tailored it is to the lead"),
]

# 'Absence' categories: confounded (mostly reminder-bumps to already-warm leads)
# and not actionable as a structure element. Shown in the breakdown, but never
# picked as the recommended building block.
NON_ACTIONABLE = {("hook_type", "other"), ("cta_style", "none"), ("personalization", "none")}

MIN_SUPPORT = 30        # follow-ups with the value (Medium floor)
MIN_POSITIVES = 15      # positive replies among them (Medium floor)
HIGH_SUPPORT = 300      # robust: only these can anchor the recommendation
HIGH_POSITIVES = 30
FRESH_MATURITY_MIN = 50  # cold-outreach positives needed before we'd prescribe for cold


def _pretty(val: str) -> str:
    return val.replace("_", " ").capitalize()


def _confidence(n: int, pos: int) -> str:
    if n >= HIGH_SUPPORT and pos >= HIGH_POSITIVES:
        return "High"
    if n >= MIN_SUPPORT and pos >= MIN_POSITIVES:
        return "Medium"
    return "Low"


def _fetch_example(cur, recommended: dict[str, str]) -> dict | None:
    """The best REAL positive follow-up that matches the recommended structure —
    a concrete, copyable, editable email for Jam (not an abstract pattern).

    `recommended` = {feature_column: winning_value} for the dims that have a
    pick. Scores each positive follow-up by how many of those dims it matches,
    best-first (most matches, then booked, then a confirmed winner, then recent),
    and returns the first with clean, non-trivial text.
    """
    if not recommended:
        return None
    cols = list(recommended)
    score_expr = " + ".join(f"({c} = %({c})s)::int" for c in cols)
    cur.execute(f"""
        select followup_new_text, client, responded_booked,
               {', '.join(cols)}, ({score_expr}) as match_score
          from followup_message_features
         where extractor_version = 'fx1' and boundary_detected and responded_positive
           and coalesce(btrim(followup_new_text), '') <> ''
         order by match_score desc, responded_booked desc,
                  is_confirmed_winner desc, sent_timestamp desc
         limit 30
    """, recommended)
    rows = cur.fetchall()
    from followup_analytics import humanize_text  # reuse run-on/glyph repair
    col_label = {col: label for col, label, _ in DIMS}
    for r in rows:
        txt = humanize_text((r["followup_new_text"] or "").replace("�", "").strip())
        if len(txt) < 20:
            continue
        matched = [col_label[c] for c in cols if r[c] == recommended[c]]
        return {
            "text": txt[:900],
            "client": r["client"],
            "booked": r["responded_booked"],
            "matched": matched,
            "total": len(cols),
        }
    return None


def fetch_winning_patterns() -> dict:
    conn = connect()
    base = "extractor_version = 'fx1' and boundary_detected"
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(f"select count(*) n, count(*) filter (where responded_positive) pos "
                    f"from followup_message_features where {base}")
        tot = cur.fetchone()
        n_all, pos_all = tot["n"], tot["pos"]

        dims_out: list[dict] = []
        for col, label, sub in DIMS:
            cur.execute(f"""
                select {col} as val, count(*) n,
                       count(*) filter (where responded_positive) pos
                  from followup_message_features
                 where {base} and {col} is not null
                 group by 1
            """)
            values: list[dict] = []
            for r in cur.fetchall():
                n, pos = r["n"], r["pos"]
                rate = 100.0 * pos / n if n else 0.0
                # lift vs the rest of the population (this value vs everything else)
                n_wo, pos_wo = n_all - n, pos_all - pos
                rate_wo = (pos_wo / n_wo) if n_wo else 0.0
                lift = (rate / 100.0) / rate_wo if rate_wo else None
                lo, hi = wilson(pos, n)
                values.append({
                    "value": r["val"], "label": _pretty(r["val"]),
                    # strip the "(FALLBACK)" spec annotation — it's a tagging-prompt
                    # marker, not user-facing copy.
                    "desc": FOLLOWUP_FEATURE_SPEC.get(col, {}).get(r["val"], "")
                            .replace("(FALLBACK)", "").strip(),
                    "n": n, "pos": pos, "rate": round(rate, 1),
                    "ci_lo": round(lo * 100), "ci_hi": round(hi * 100),
                    "lift": round(lift, 2) if lift else None,
                    "conf": _confidence(n, pos),
                    "actionable": (col, r["val"]) not in NON_ACTIONABLE,
                })
            values.sort(key=lambda v: v["rate"], reverse=True)
            # Recommended building block = highest-rate ACTIONABLE value, but
            # anchored on robust (High-confidence) options first so a thin,
            # marginally-higher value can't beat a well-supported one. Only fall
            # to Medium when no High-confidence actionable value exists.
            actionable = [v for v in values if v["actionable"]]
            high = [v for v in actionable if v["conf"] == "High"]
            pool = high or [v for v in actionable if v["conf"] == "Medium"]
            best = max(pool, key=lambda v: v["rate"]) if pool else None
            dims_out.append({"key": col, "label": label, "sub": sub,
                             "values": values, "best": best})

        # A real, copyable email that best matches the recommended structure.
        recommended = {d["key"]: d["best"]["value"] for d in dims_out if d["best"]}
        example = _fetch_example(cur, recommended)

        # Fresh-outreach guard: how strong is the signal on NOT-already-warm leads?
        cur.execute(f"select count(*) n, count(*) filter (where responded_positive) pos "
                    f"from followup_message_features where {base} and not prior_positive_exists")
        fr = cur.fetchone()
    finally:
        conn.close()

    fresh = {"n": fr["n"], "pos": fr["pos"],
             "rate": round(100.0 * fr["pos"] / fr["n"], 1) if fr["n"] else 0.0}
    return {
        "dims": dims_out,
        "example": example,
        "n_all": n_all, "pos_all": pos_all,
        "base_rate": round(100.0 * pos_all / n_all, 1) if n_all else 0.0,
        "fresh": fresh,
        # Enough cold-outreach positives to prescribe wording for cold leads?
        "fresh_mature": fresh["pos"] >= FRESH_MATURITY_MIN,
    }
