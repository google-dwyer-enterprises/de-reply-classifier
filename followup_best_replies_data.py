"""Best replies — data-ranked, for the in-app /followups/best page.

Rebuilds the old (static, template-duplicating) Best replies page as a
DATA-DRIVEN one, answering Victor's 2026-06-24 asks:
  * rank replies by performance (#1/#2/#3),
  * organize by follow-up STAGE (top picks for FU1, FU2, FU3, ...),
  * show a reply-rate metric per stage,
  * surface the *actual* top-performing copy (not a hand-picked list) so we
    can verify which follow-ups really work — the data shows FU1-FU3 carry
    ~69% of all positive replies.

Read-only. Reads the live followup_message_features table on every load.
Method mirrors followup_features / followup_analytics:
  * Universe = cleanly-extracted manual follow-ups (extractor_version='fx1',
    boundary_detected) with non-empty new text.
  * "Reply" = responded_positive (interested or booked); booked is the subset.
  * Per-stage rate = positives / sends at that ffup_position, with a Wilson CI.
  * Best replies per stage = positive-outcome messages, booked-first then
    confirmed-winner then recent, de-duplicated by a normalized opening.
Descriptive only (warm-lead + single-client-skew caveats apply — see template).
"""
from __future__ import annotations

import math
import re

import psycopg2.extras

from db import connect

# Individual stages we surface; everything beyond folds into a "6+" bucket
# (the tail is thin and low-rate — see the by-stage panel).
TOP_STAGES = [1, 2, 3, 4, 5]
LATER_FROM = 6
PER_STAGE = 4          # best replies shown per stage
MIN_SENDS = 20         # below this a stage's rate is shown as a hint only


def _wilson(pos: int, n: int, z: float = 1.96) -> tuple[int, int]:
    if n == 0:
        return (0, 0)
    pos = min(pos, n)
    phat = pos / n
    denom = 1 + z * z / n
    center = (phat + z * z / (2 * n)) / denom
    margin = z * math.sqrt(phat * (1 - phat) / n + z * z / (4 * n * n)) / denom
    return (round(max(0.0, center - margin) * 100), round(min(1.0, center + margin) * 100))


def _stage_label(pos: int) -> str:
    return f"Follow-up {pos}" if pos < LATER_FROM else "Later follow-ups (6+)"


# Greeting openers that are almost always followed by the recipient's first name.
# (?i:...) makes the greeting word case-insensitive ('Hi'/'Hey'/'Good morning')
# while the captured name still requires a leading capital, so 'Hi there,' won't
# false-match on the lowercase 'there'.
_GREETING_RE = re.compile(
    r"^\s*(?i:hi|hey|hello|dear|hiya|heya|good\s+(?:morning|afternoon|evening))\b[\s,]*"
    r"([A-Z][A-Za-z'’\-]{1,})",
)
# Words that follow a greeting but aren't names ("Hi there,", "Hey team,").
_NAME_STOPWORDS = {"there", "team", "all", "everyone", "folks", "guys", "again",
                   "hope", "friend", "friends", "hi", "hey", "hello"}


def _swap_word(text: str, word: str, placeholder: str, ci: bool = False) -> tuple[str, bool]:
    """Whole-word replace (case-sensitive by default to avoid clobbering common
    words that happen to be names, e.g. 'Will'/'Mark' used as verbs)."""
    new = re.sub(rf"\b{re.escape(word)}\b", placeholder, text, flags=(re.I if ci else 0))
    return new, (new != text)


def _tokenize(text: str, first_name: str | None, last_name: str | None,
              company: str | None) -> tuple[str, list[str]]:
    """Swap the LEAD's own identifiers for template placeholders.

    First name is caught two ways: (1) the stored lead name, and (2) — the big
    one — the name in the opening greeting ('Hi Justin,' -> 'Hi {first_name},'),
    which works even with no stored name. Last name and company are swapped when
    present. Everything else — our brand, links, product specifics — is left for
    the human. Returns (tokenized_text, list of what was swapped)."""
    out, swapped = text, []

    fnames: list[str] = []
    if first_name and len(first_name.strip()) >= 2:
        fnames.append(first_name.strip())
    m = _GREETING_RE.match(out)
    if m:
        cand = m.group(1)
        if cand.lower() not in _NAME_STOPWORDS and cand not in fnames:
            fnames.append(cand)
    for nm in fnames:
        out, changed = _swap_word(out, nm, "{first_name}")
        if changed and "first name" not in swapped:
            swapped.append("first name")

    if last_name and len(last_name.strip()) >= 2:
        out, changed = _swap_word(out, last_name.strip(), "{last_name}")
        if changed:
            swapped.append("last name")

    if company and len(company.strip()) >= 3:
        out, changed = _swap_word(out, company.strip(), "{company}", ci=True)
        if changed:
            swapped.append("company")
    return out, swapped


def fetch_best_replies() -> dict:
    from followup_analytics import humanize_text  # reuse run-on/glyph repair

    conn = connect()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            # Per-stage volume + reply rate (individual stages, then 6+ pooled)
            cur.execute("""
                select case when ffup_position >= %(later)s then %(later)s else ffup_position end as stage,
                       count(*) as sends,
                       count(*) filter (where responded_positive) as pos,
                       count(*) filter (where responded_booked) as booked
                  from followup_message_features
                 where extractor_version = 'fx1' and boundary_detected
                   and coalesce(btrim(followup_new_text), '') <> ''
                   and ffup_position >= 1
                 group by 1 order by 1
            """, {"later": LATER_FROM})
            rate_rows = [dict(r) for r in cur.fetchall()]

            # Winning copy per stage (positive outcomes), best-first. Join the
            # lead's name/company so we can offer a tokenized "Add as template".
            cur.execute("""
                select case when f.ffup_position >= %(later)s then %(later)s else f.ffup_position end as stage,
                       f.followup_new_text, f.client, f.responded_booked,
                       f.is_confirmed_winner, f.sent_timestamp,
                       coalesce(lc.first_name, l.first_name)                as first_name,
                       coalesce(lc.last_name, l.last_name)                  as last_name,
                       coalesce(lc.resolved_company_name, lc.company_name)  as company
                  from followup_message_features f
                  left join lead_contacts lc on lc.lead_email = f.lead_email
                  left join leads l on l.lead_email = f.lead_email
                 where f.extractor_version = 'fx1' and f.boundary_detected
                   and f.responded_positive
                   and coalesce(btrim(f.followup_new_text), '') <> ''
                   and f.ffup_position >= 1
                 order by stage, f.responded_booked desc, f.is_confirmed_winner desc, f.sent_timestamp desc
            """, {"later": LATER_FROM})
            win_rows = [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()

    # ---- per-stage rate panel ----
    total_pos = sum(r["pos"] for r in rate_rows) or 1
    max_rate = 1
    stages = {}
    for r in rate_rows:
        rate = round(100.0 * r["pos"] / r["sends"], 1) if r["sends"] else 0.0
        lo, hi = _wilson(r["pos"], r["sends"])
        max_rate = max(max_rate, rate)
        stages[r["stage"]] = {
            "stage": r["stage"], "label": _stage_label(r["stage"]),
            "sends": r["sends"], "pos": r["pos"], "booked": r["booked"],
            "rate": rate, "ci_lo": lo, "ci_hi": hi,
            "share": round(100.0 * r["pos"] / total_pos),
            "thin": r["sends"] < MIN_SENDS, "replies": [],
        }
    panel = sorted(stages.values(), key=lambda s: s["stage"])
    for s in panel:
        s["bar"] = round((s["rate"] / max_rate) * 100) if max_rate else 0

    # ---- best replies per stage (deduped by normalized opening) ----
    seen: set[str] = set()
    for w in win_rows:
        st = stages.get(w["stage"])
        if not st or len(st["replies"]) >= PER_STAGE:
            continue
        txt = humanize_text((w["followup_new_text"] or "").replace("�", "").strip())
        if len(txt) < 20:
            continue
        key = re.sub(r"[^a-z0-9]", "", txt.lower())[:60]
        if key in seen:
            continue
        seen.add(key)
        disp = txt[:900]
        tokenized, swapped = _tokenize(disp, w.get("first_name"), w.get("last_name"), w.get("company"))
        st["replies"].append({
            "text": disp, "client": w["client"],
            "booked": w["responded_booked"],
            "rank": len(st["replies"]) + 1,
            "tokenized": tokenized, "swapped": swapped,
        })

    # Order display: stages that actually have winning copy, by stage number.
    ranked = [s for s in panel if s["replies"]]
    overall_sends = sum(s["sends"] for s in panel)
    return {
        "panel": panel,                 # every stage, for the rate strip
        "ranked": ranked,               # stages with best-reply cards
        "overall_sends": overall_sends,
        "overall_pos": sum(s["pos"] for s in panel),
        "overall_booked": sum(s["booked"] for s in panel),
        "overall_rate": round(100.0 * sum(s["pos"] for s in panel) / (overall_sends or 1), 1),
        "top3_share": round(100.0 * sum(s["pos"] for s in panel if s["stage"] in (1, 2, 3)) / (total_pos or 1)),
        "min_sends": MIN_SENDS,
    }
