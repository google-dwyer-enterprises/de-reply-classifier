"""Follow-up playbook — data for the live in-app /followups/playbook page.

The prescriptive FU1-FU5 guide (RULES, STAGES, caveat) is editorial and fixed.
The three data-backed sections — overview counts, the do/avoid evidence
(followup_patterns_mv), and the per-stage timing decay (followup_timing_mv) —
are pulled live on every page load so they stay current. Read-only.

Honest caveat (kept in the page): the patterns are DESCRIPTIVE (warm-lead
confound) — the live static-vs-AI A/B is what proves which actually books more.
"""
from __future__ import annotations

import psycopg2.extras

from db import connect

# ------- the prescriptive sequence (editorial; each stage cites the data) -------
RULES = [
    ("Open with a compliment or the value — never a gimmick",
     "Replies that opened with a specific compliment landed 23% vs 9%; value-prop "
     "openers 17% vs 8%. The GIF / “pattern-interrupt” style did the opposite (3.5% vs 11%)."),
    ("Always end with ONE direct ask + your calendar link",
     "A direct ask beat soft/no-ask (11.5% vs 7.7%), and messages that included a "
     "booking link replied at 14% vs 9% without one."),
    ("Keep the tone businesslike, not overly casual",
     "Neutral/formal tone outperformed casual (11-13% vs 8%). Confident and warm, not chatty."),
    ("Aim for a short paragraph — not a one-liner, not a wall",
     "Very-short one-liners underperformed (7.4%); medium length read best. Give enough to be concrete."),
    ("Name the concrete model / value",
     "Messages that mentioned the actual offer or pricing structure replied at 17% vs 8%."),
    ("Front-load the effort",
     "Reply rate falls fast by stage (#1 19% → #3 10% → #6+ under 4%). The first three touches "
     "earn most of the bookings — make them count and don't drag the sequence out."),
]

STAGES = [
    {"n": "1", "when": "Right after they reply interested",
     "goal": "Convert the interest into a booked call while it's hot.",
     "hook": "A specific compliment about their Amazon store, then the value.",
     "tone": "Warm but businesslike.", "structure": "Compliment → value → direct ask + link.",
     "copy": ("Hi {first_name}, thanks for getting back to me. I took a proper look at "
              "{company}'s Amazon store — [one specific, genuine compliment, e.g. “your "
              "top products are clearly resonating”]. The reason I reached out: brands like "
              "yours usually have real untapped demand in the EU, UK and other marketplaces, and "
              "we run that expansion end-to-end — we buy your inventory at wholesale and you "
              "keep full control of pricing and brand. Worth 15 minutes to map where the biggest "
              "wins are? Here's my calendar link."),
     "why": "Compliment hook (lift 2.7) + value prop (2.3) + direct CTA with a calendar link (1.5 / booking-link 14%)."},
    {"n": "2", "when": "2–3 days later, no reply",
     "goal": "Make the offer concrete so it's easy to say yes.",
     "hook": "Lead with how it actually works.",
     "tone": "Neutral, confident.", "structure": "Value/how-it-works → direct ask with two time options.",
     "copy": ("Hi {first_name}, following up on the international expansion idea for {company}. "
              "Quick version of how it works: we purchase your inventory at an agreed wholesale "
              "price and run the marketplaces (Amazon EU/UK, Walmart, and more) for you — you "
              "keep full control of pricing, presentation and brand. Are you free for a 15-minute "
              "call Tuesday or Wednesday? Here's my calendar link."),
     "why": "Names the concrete model (mentions-model 17% vs 8%), direct ask with options, calendar link."},
    {"n": "3", "when": "3–4 days later",
     "goal": "Pre-empt the most common objection (cost / effort).",
     "hook": "Remove the risk up front.",
     "tone": "Neutral, reassuring.", "structure": "Objection handle → value → direct ask + link.",
     "copy": ("Hi {first_name}, I know a new channel can feel like a lift, so to be clear: there's "
              "no monthly retainer and no extra fees — we make our margin on the resale, so we "
              "only win when {company} wins. If growth in new markets is on your radar this "
              "quarter, a short call is the fastest way to see if it's a fit. Here's my calendar link."),
     "why": "Pricing/model clarity (mentions-pricing 17% vs 8%) + direct CTA. Last 'full-pitch' touch before reply rates drop."},
    {"n": "4", "when": "4–5 days later",
     "goal": "Lower the friction to almost zero.",
     "hook": "Offer something useful + exact times.",
     "tone": "Brief, helpful.", "structure": "Specific value → two concrete times → link.",
     "copy": ("Hi {first_name}, still happy to show you what international expansion could look like "
              "for {company} specifically — I'll bring a quick analysis of which markets fit "
              "best. Does Thursday at 1pm or Friday at 11am (your time) work? Here's my calendar "
              "link if another slot is easier."),
     "why": "Direct, specific times + a concrete reason to meet. Reply rate here is ~6-7% — keep it short and easy."},
    {"n": "5", "when": "About a week later — the last one",
     "goal": "Get a yes/no and close the loop respectfully.",
     "hook": "A straight priority check.",
     "tone": "Direct, low-pressure.", "structure": "Priority check → permission to close → link if yes.",
     "copy": ("Hi {first_name}, I don't want to keep crowding your inbox — is expanding {company} "
              "into new marketplaces still a priority right now? If now isn't the time, just say so "
              "and I'll close the loop. If it is, here's my calendar link and I'll take it from there."),
     "why": "A clear ask still beats a soft fade. After this, reply rates fall under 4% — stop or restart fresh later."},
]


def fetch_playbook() -> dict:
    conn = connect()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""select count(*) filter (where boundary_detected) analyzed,
                count(*) filter (where responded_positive) positive,
                count(*) filter (where responded_booked) booked
                from followup_message_features where extractor_version='fx1'""")
            ov = dict(cur.fetchone())

            cur.execute("""
                select "Characteristic" ch, "Value" val, "Positive % (With)" w,
                       "Positive % (Without)" wo, "Lift" lift, "Largest-Client Share %" share
                  from followup_patterns_mv
                 where "Confidence" <> 'Insufficient data'
                   and "Characteristic" <> 'Lead Already Positive'
                   and "Lift" is not null
                 order by "Lift" desc
            """)
            rows = [dict(r) for r in cur.fetchall()]
            for r in rows:
                r["skew"] = (r["share"] or 0) >= 60
            do = [r for r in rows if float(r["lift"]) >= 1.15][:8]
            avoid = [r for r in rows if float(r["lift"]) <= 0.85][-8:][::-1]

            cur.execute('select "Follow-up #" fu, "Sends" s, "Positive %" p '
                        'from followup_timing_mv order by "Follow-up #"')
            timing = [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()

    tmax = max([float(t["p"]) for t in timing] + [1.0])
    for t in timing:
        t["bar"] = round(float(t["p"]) / tmax * 100)

    return {"overview": ov, "do": do, "avoid": avoid, "timing": timing,
            "rules": RULES, "stages": STAGES}
