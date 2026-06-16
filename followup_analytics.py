"""Data + plain-English layer for the /analytics page (follow-up effectiveness).

Turns the technical `followup_patterns_mv` / `followup_timing_mv` / the
`followup_message_features` table into something a non-technical reader can act
on: every characteristic and value gets a friendly label + one-line explanation
(no "CTA", "hook type", "P.S." jargon), each pattern is graded for how much you
can TRUST it (enough data? skewed to one client?), and the warm-lead confound is
surfaced rather than hidden.

Read-only. Reuses db.connect() (psycopg2). No new tables/views.
"""
from __future__ import annotations

import math
import re

import psycopg2.extras

from db import connect

# --- example-text repair ------------------------------------------------------
# Email bodies were stored flattened (newlines dropped WITHOUT a space) during
# sync, so the rep's text comes through with run-ons: "questionsWhich",
# "calendar linkOn", "brand?We". The original break positions are lost in the DB,
# so we re-insert spaces heuristically for DISPLAY ONLY (never mutates the data /
# the analytics stats). Conservative: protects URLs, emails and known camelCase
# brands, and only splits a lower->Upper seam or a '?'/'!' glued to a word — never
# '.'/',' (which would break domains, decimals, "inc.com").
_HUMANIZE_BRANDS = re.compile(
    r"eBay|iPhone|iPad|iPod|iOS|macOS|YouTube|TikTok|LinkedIn|PayPal|WooCommerce|"
    r"BigCommerce|WordPress|GoDaddy|ClickFunnels|ConvertKit|MailChimp|QuickBooks|"
    r"AliExpress|DoorDash|PageFly|ShipBob|GrubHub|"
    r"MoM|YoY|WoW|QoQ|PoP|DtC|McK|MacBook")
_URL_OR_EMAIL = re.compile(r"https?://\S+|www\.\S+|\b[\w.+-]+@[\w.-]+\.\w+\b")


def humanize_text(text: str) -> str:
    if not text:
        return text
    holds: list[str] = []

    def _hold(m):
        holds.append(m.group(0))
        return f"\x00{len(holds) - 1}\x00"

    t = _URL_OR_EMAIL.sub(_hold, text)
    t = _HUMANIZE_BRANDS.sub(_hold, t)
    t = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", t)   # "linkOn" -> "link On"
    t = re.sub(r"([?!])(?=[A-Za-z])", r"\1 ", t)     # "brand?We" -> "brand? We"
    t = re.sub(r"[ \t]{2,}", " ", t)
    for i, v in enumerate(holds):
        t = t.replace(f"\x00{i}\x00", v)
    return t.strip()

# --- plain-English dictionary -------------------------------------------------
# characteristic -> {label, blurb, kind: 'binary'|'multi', values:{raw:(label,blurb)}}
PLAIN: dict[str, dict] = {
    "Hook Type (AI)": {
        "label": "How the message opens",
        "blurb": "The first line — what the rep leads with to grab attention.",
        "kind": "multi",
        "values": {
            "Compliment": ("Opens with a compliment", "Praises the lead, their brand, or recent news."),
            "Value Prop": ("Opens with the offer", "Leads with the benefit — what we can do for them."),
            "Stat": ("Opens with a number / stat", "Leads with a figure or metric."),
            "Question": ("Opens with a question", "First line asks them something."),
            "Reminder": ("Just a bump / 'circling back'", "Only nudges the previous email, no new angle."),
            "Pattern Interrupt": ("Opens with something unusual", "A blunt, quirky, or humorous opener meant to stand out."),
            "Other": ("Something else", "Doesn't fit a clear category."),
        },
    },
    "CTA Style (AI)": {
        "label": "What the message asks them to do",
        "blurb": "Every email ends with an 'ask' (or doesn't). This is how that ask is phrased.",
        "kind": "multi",
        "values": {
            "Direct": ("A clear, direct ask", "e.g. 'Can we meet Tuesday at 2?'"),
            "Soft": ("A gentle nudge", "e.g. 'Worth a quick chat?'"),
            "Permission Based": ("Asks permission first", "e.g. 'OK if I send over a few details?'"),
            "None": ("No ask at all", "The message doesn't ask them to do anything."),
        },
    },
    "Tone (AI)": {
        "label": "Overall tone",
        "blurb": "How formal or casual the writing feels.",
        "kind": "multi",
        "values": {
            "Formal": ("Formal & polished", "Professional, no slang."),
            "Neutral": ("Plain & businesslike", "Straightforward, neither stiff nor chatty."),
            "Casual": ("Casual & friendly", "Informal, conversational, contractions."),
        },
    },
    "Personalization (AI)": {
        "label": "How tailored it is to them",
        "blurb": "How specific the message is to that particular lead.",
        "kind": "multi",
        "values": {
            "Deep": ("Tailored to this lead specifically", "References their product, news, or a prior reply."),
            "Light": ("Lightly tailored", "Mentions their company, role, or industry generically."),
            "None": ("Generic", "Could be sent to anyone."),
        },
    },
    "Length": {
        "label": "Message length",
        "blurb": "How long the message is.",
        "kind": "multi",
        "values": {
            "very_short": ("Very short (15 words or fewer)", "A one-liner."),
            "short": ("Short (16–40 words)", "A couple of sentences."),
            "medium": ("Medium (41–90 words)", "A short paragraph."),
            "long": ("Long (90+ words)", "Several paragraphs."),
        },
    },
    # binary "did the message do X?" — we show only the "Yes" (using the tactic) row
    "Mentions Pricing": {"label": "Mentions price or cost", "blurb": "The message talks about price, fees, or a discount.", "kind": "binary"},
    "Has Booking Link": {"label": "Includes a booking link", "blurb": "Contains a scheduling link (like Calendly).", "kind": "binary"},
    "Has Question": {"label": "Contains a question", "blurb": "The message asks something (has a '?').", "kind": "binary"},
    "Opens With Question": {"label": "Starts with a question", "blurb": "The very first line is a question.", "kind": "binary"},
    "Has Link": {"label": "Includes a web link", "blurb": "Contains any web link.", "kind": "binary"},
    "Has P.S.": {"label": "Has a 'P.S.' line", "blurb": "Ends with a 'P.S.' — an extra note added after the sign-off (a common copywriting trick).", "kind": "binary"},
    "Has Emoji": {"label": "Uses an emoji", "blurb": "The message contains an emoji.", "kind": "binary"},
}
# shown separately as the big caveat, never as a "tactic"
CONFOUND_KEY = "Lead Already Positive"


def _pct(d):
    return float(d) if d is not None else None


def grade(confidence: str, client_share) -> tuple[str, str]:
    """Reliability of a pattern row -> (css_key, label)."""
    if confidence == "Insufficient data":
        return ("thin", "Not enough data")
    if client_share is not None and float(client_share) >= 60:
        return ("skewed", "Mostly one client")
    if confidence == "High":
        return ("solid", "Can trust this")
    return ("fair", "Fairly sure")


def verdict(lift) -> tuple[str, str] | None:
    """Plain-English read of the lift number -> (direction, phrase)."""
    if lift is None:
        return None
    l = float(lift)
    if l >= 1.15:
        return ("up", f"{l:.1f}× more replies" if l >= 1.5 else "a few more replies")
    if l <= 0.85:
        return ("down", f"{(1 / l):.1f}× fewer replies" if l <= 0.66 and l > 0 else "a few fewer replies")
    return ("flat", "about the same")


def fetch_overview(cur) -> dict:
    cur.execute("""
        select count(*) total,
               count(*) filter (where boundary_detected) analyzed,
               count(*) filter (where responded_positive) positive,
               count(*) filter (where responded_booked) booked,
               count(*) filter (where prior_positive_exists and boundary_detected) warm
          from followup_message_features where extractor_version='fx1'
    """)
    r = cur.fetchone()
    pos_rate = round(100 * r["positive"] / r["analyzed"], 1) if r["analyzed"] else 0
    warm_rate = round(100 * r["warm"] / r["analyzed"], 1) if r["analyzed"] else 0
    return {**r, "positive_rate": pos_rate, "warm_rate": warm_rate}


def _examples(cur) -> dict:
    """{(Characteristic, Value): [{text, client, booked}]} — top 3 real positive-outcome messages."""
    cur.execute("""
      with base as (
        select followup_new_text, client, responded_booked, is_confirmed_winner, sent_timestamp,
               length_bucket, has_question, opens_with_question, has_calendar_link,
               mentions_pricing, has_url, has_ps, has_emoji,
               hook_type, tone, cta_style, personalization
        from followup_message_features
        where extractor_version='fx1' and boundary_detected and responded_positive
          and coalesce(btrim(followup_new_text),'') <> ''
      ),
      exploded as (
        select b.*, v.dim, v.val from base b cross join lateral (values
          ('Length', length_bucket),
          ('Has Question', case when has_question then 'Yes' else 'No' end),
          ('Opens With Question', case when opens_with_question then 'Yes' else 'No' end),
          ('Has Booking Link', case when has_calendar_link then 'Yes' else 'No' end),
          ('Mentions Pricing', case when mentions_pricing then 'Yes' else 'No' end),
          ('Has Link', case when has_url then 'Yes' else 'No' end),
          ('Has P.S.', case when has_ps then 'Yes' else 'No' end),
          ('Has Emoji', case when has_emoji then 'Yes' else 'No' end),
          ('Hook Type (AI)', initcap(replace(hook_type,'_',' '))),
          ('Tone (AI)', initcap(tone)),
          ('CTA Style (AI)', initcap(replace(cta_style,'_',' '))),
          ('Personalization (AI)', initcap(personalization))
        ) as v(dim,val) where v.val is not null
      ),
      ranked as (
        select *, row_number() over (partition by dim,val
          order by responded_booked desc, is_confirmed_winner desc, sent_timestamp desc) rn
        from exploded
      )
      select dim, val, client, responded_booked, followup_new_text
      from ranked where rn <= 3 order by dim, val, rn
    """)
    out: dict = {}
    for r in cur.fetchall():
        txt = humanize_text((r["followup_new_text"] or "").replace("�", "").strip())
        if len(txt) > 600:
            txt = txt[:600].rstrip() + "…"
        out.setdefault((r["dim"], r["val"]), []).append(
            {"text": txt, "client": r["client"], "booked": r["responded_booked"]}
        )
    return out


def _row(p, val, label, blurb, ex):
    rel = grade(p["Confidence"], p["Largest-Client Share %"])
    return {
        "value": val, "label": label, "blurb": blurb,
        "n": p["Follow-ups With It"], "positives": p["Positive Replies"],
        "with_pct": _pct(p["Positive % (With)"]), "without_pct": _pct(p["Positive % (Without)"]),
        "lift": _pct(p["Lift"]), "verdict": verdict(p["Lift"]),
        "reliability": rel[0], "reliability_label": rel[1],
        "client_share": _pct(p["Largest-Client Share %"]),
        "examples": ex,
    }


def fetch_analytics() -> dict:
    conn = connect()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        overview = fetch_overview(cur)
        cur.execute('select * from followup_patterns_mv')
        pats = cur.fetchall()
        cur.execute('select * from followup_timing_mv order by "Follow-up #"')
        timing = [dict(t) for t in cur.fetchall()]
        ex = _examples(cur)
    finally:
        conn.close()

    groups, takeaways = [], []
    for char, meta in PLAIN.items():
        rows = []
        if meta["kind"] == "binary":
            p = next((x for x in pats if x["Characteristic"] == char and x["Value"] == "Yes"), None)
            if p:
                rows.append(_row(p, "Yes", meta["label"], meta["blurb"], ex.get((char, "Yes"), [])))
        else:
            vals = [x for x in pats if x["Characteristic"] == char]
            vals.sort(key=lambda x: (x["Lift"] is None, -float(x["Lift"] or 0)))
            for p in vals:
                vl, vb = meta["values"].get(p["Value"], (p["Value"], ""))
                rows.append(_row(p, p["Value"], vl, vb, ex.get((char, p["Value"]), [])))
        if rows:
            groups.append({"char": char, "label": meta["label"], "blurb": meta["blurb"],
                           "kind": meta["kind"], "rows": rows})

    # headline takeaways: only trustworthy rows (solid/fair, not skewed/thin)
    flat = [r for g in groups for r in g["rows"] if r["reliability"] in ("solid", "fair") and r["lift"]]
    wins = sorted([r for r in flat if r["lift"] >= 1.3], key=lambda r: -r["with_pct"])[:6]
    losses = sorted([r for r in flat if r["lift"] <= 0.8], key=lambda r: r["with_pct"])[:6]

    # bar chart scale: round the biggest rate (incl. the average) up to the next 5%
    rates = [r["with_pct"] for r in (wins + losses) if r["with_pct"]] + [overview["positive_rate"]]
    bar_scale = max(5, 5 * math.ceil(max(rates) / 5)) if rates else 25

    return {"overview": overview, "groups": groups, "timing": timing,
            "wins": wins, "losses": losses,
            "bar_scale": bar_scale, "avg_rate": overview["positive_rate"]}
