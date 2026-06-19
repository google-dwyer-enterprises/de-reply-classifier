"""Generate the Optimized Follow-up Playbook (docs/replies/FOLLOWUP_PLAYBOOK.html).

A single prescriptive guide Jam can copy: a 5-stage follow-up sequence (FU1-FU5)
for leads who've shown interest, with the exact wording, the compliment to use,
the tone, and the structure at each stage — synthesized from the follow-up
effectiveness analysis (which writing characteristics correlate with positive
replies) and the per-stage timing decay.

Read-only. Pulls the live patterns/timing views for the evidence section; the
prescriptive sequence is editorial (grounded in those patterns). Honest caveat:
the patterns are DESCRIPTIVE (warm-lead confound) — the live static-vs-AI A/B
will confirm/refine which actually books more.

Usage: python scripts/gen_followup_playbook.py
"""
import html
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import psycopg2.extras
from db import connect

DATE = "2026-06-19"

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
    {
        "n": "1", "when": "Right after they reply interested",
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
        "why": "Compliment hook (lift 2.7) + value prop (2.3) + direct CTA with a calendar link (1.5 / booking-link 14%).",
    },
    {
        "n": "2", "when": "2–3 days later, no reply",
        "goal": "Make the offer concrete so it's easy to say yes.",
        "hook": "Lead with how it actually works.",
        "tone": "Neutral, confident.", "structure": "Value/how-it-works → direct ask with two time options.",
        "copy": ("Hi {first_name}, following up on the international expansion idea for {company}. "
                 "Quick version of how it works: we purchase your inventory at an agreed wholesale "
                 "price and run the marketplaces (Amazon EU/UK, Walmart, and more) for you — you "
                 "keep full control of pricing, presentation and brand. Are you free for a 15-minute "
                 "call Tuesday or Wednesday? Here's my calendar link."),
        "why": "Names the concrete model (mentions-model 17% vs 8%), direct ask with options, calendar link.",
    },
    {
        "n": "3", "when": "3–4 days later",
        "goal": "Pre-empt the most common objection (cost / effort).",
        "hook": "Remove the risk up front.",
        "tone": "Neutral, reassuring.", "structure": "Objection handle → value → direct ask + link.",
        "copy": ("Hi {first_name}, I know a new channel can feel like a lift, so to be clear: there's "
                 "no monthly retainer and no extra fees — we make our margin on the resale, so we "
                 "only win when {company} wins. If growth in new markets is on your radar this "
                 "quarter, a short call is the fastest way to see if it's a fit. Here's my calendar link."),
        "why": "Pricing/model clarity (mentions-pricing 17% vs 8%) + direct CTA. This is the last 'full-pitch' touch before reply rates drop.",
    },
    {
        "n": "4", "when": "4–5 days later",
        "goal": "Lower the friction to almost zero.",
        "hook": "Offer something useful + exact times.",
        "tone": "Brief, helpful.", "structure": "Specific value → two concrete times → link.",
        "copy": ("Hi {first_name}, still happy to show you what international expansion could look like "
                 "for {company} specifically — I'll bring a quick analysis of which markets fit "
                 "best. Does Thursday at 1pm or Friday at 11am (your time) work? Here's my calendar "
                 "link if another slot is easier."),
        "why": "Direct, specific times + a concrete reason to meet. Reply rate here is ~6-7% — keep it short and easy.",
    },
    {
        "n": "5", "when": "About a week later — the last one",
        "goal": "Get a yes/no and close the loop respectfully.",
        "hook": "A straight priority check.",
        "tone": "Direct, low-pressure.", "structure": "Priority check → permission to close → link if yes.",
        "copy": ("Hi {first_name}, I don't want to keep crowding your inbox — is expanding {company} "
                 "into new marketplaces still a priority right now? If now isn't the time, just say so "
                 "and I'll close the loop. If it is, here's my calendar link and I'll take it from there."),
        "why": "A clear ask still beats a soft fade. After this, reply rates fall under 4% — stop or restart fresh later.",
    },
]


def fetch_evidence(cur):
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
    do = [r for r in rows if float(r["lift"]) >= 1.15][:8]
    avoid = [r for r in rows if float(r["lift"]) <= 0.85][-8:][::-1]
    return do, avoid


def fetch_timing(cur):
    cur.execute('select "Follow-up #" fu, "Sends" s, "Positive %" p from followup_timing_mv order by "Follow-up #"')
    return [dict(r) for r in cur.fetchall()]


def fetch_overview(cur):
    cur.execute("""select count(*) filter (where boundary_detected) analyzed,
        count(*) filter (where responded_positive) positive,
        count(*) filter (where responded_booked) booked
        from followup_message_features where extractor_version='fx1'""")
    return dict(cur.fetchone())


def main():
    conn = connect()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        ov = fetch_overview(cur)
        do, avoid = fetch_evidence(cur)
        timing = fetch_timing(cur)
    finally:
        conn.close()

    def esc(s):
        return html.escape(str(s))

    rule_items = "".join(
        f"<li><b>{esc(t)}.</b> <span class='muted'>{esc(d)}</span></li>" for t, d in RULES)

    stage_cards = ""
    for s in STAGES:
        stage_cards += f"""
    <div class="stage">
      <div class="stage-head"><span class="stage-n">FU{s['n']}</span>
        <div><div class="stage-when">{esc(s['when'])}</div>
        <div class="stage-goal">{esc(s['goal'])}</div></div></div>
      <div class="stage-meta">
        <span><b>Compliment / hook:</b> {esc(s['hook'])}</span>
        <span><b>Tone:</b> {esc(s['tone'])}</span>
        <span><b>Structure:</b> {esc(s['structure'])}</span>
      </div>
      <div class="copybox"><button class="copy-btn" type="button">Copy</button><pre>{esc(s['copy'])}</pre></div>
      <p class="why"><b>Why this works:</b> {esc(s['why'])}</p>
    </div>"""

    def erow(r, kind):
        return (f"<tr><td>{esc(r['ch'])}</td><td>{esc(r['val'])}</td>"
                f"<td class='num'>{r['w']}% <span class='muted'>vs {r['wo']}%</span></td>"
                f"<td class='num {kind}'>{r['lift']}×</td>"
                f"{'<td class=skew>mostly 1 client</td>' if (r['share'] or 0) >= 60 else '<td></td>'}</tr>")
    do_rows = "".join(erow(r, "up") for r in do)
    avoid_rows = "".join(erow(r, "down") for r in avoid)

    tmax = max([float(t["p"]) for t in timing] + [1])
    timing_bars = ""
    for t in timing:
        pct = float(t["p"])
        timing_bars += (f"<div class='tbar'><div class='tlab'>Follow-up #{esc(t['fu'])}</div>"
                        f"<div class='ttrack'><span style='width:{pct/tmax*100:.0f}%'></span></div>"
                        f"<div class='tval'>{pct}% <span class='muted'>({t['s']})</span></div></div>")

    doc = f"""<!doctype html><html><head><meta charset="utf-8">
<title>Optimized Follow-up Playbook · Dwyer Console</title><style>
body{{font-family:-apple-system,Segoe UI,Helvetica,Arial,sans-serif;color:#1a1d23;max-width:860px;margin:0 auto;padding:30px;line-height:1.6;font-size:15px;background:#fafafa}}
h1{{font-size:25px;margin:0 0 6px}} h2{{font-size:19px;margin:34px 0 10px}}
.sub{{color:#5e6470;margin:0 0 22px}}
.box{{background:#fff;border:1px solid #e1e4e8;border-left:4px solid #16a34a;border-radius:10px;padding:16px 20px;margin:16px 0}}
.box.warn{{border-left-color:#b45309;background:#fffdf7}}
.box h3{{margin:0 0 8px;font-size:16px}}
ol,ul{{margin:8px 0;padding-left:20px}} li{{margin:8px 0}}
.muted{{color:#8c959f}}
.stage{{background:#fff;border:1px solid #e1e4e8;border-radius:12px;padding:16px 18px;margin:14px 0}}
.stage-head{{display:flex;gap:14px;align-items:flex-start;margin-bottom:10px}}
.stage-n{{background:#1c2433;color:#fff;font-weight:800;border-radius:8px;padding:6px 12px;font-size:15px;flex:0 0 auto}}
.stage-when{{font-weight:700}} .stage-goal{{color:#5e6470;font-size:13.5px}}
.stage-meta{{display:flex;flex-direction:column;gap:3px;font-size:13px;color:#374151;margin:6px 0 10px}}
.copybox{{position:relative;background:#f7f8fa;border:1px solid #e1e4e8;border-radius:8px;padding:12px 14px}}
.copybox pre{{white-space:pre-wrap;font-family:inherit;font-size:14px;margin:0}}
.copy-btn{{position:absolute;top:10px;right:10px;border:1px solid #2563eb;background:#fff;color:#2563eb;border-radius:6px;padding:3px 12px;cursor:pointer;font-size:12.5px}}
.copy-btn.copied{{background:#16a34a;border-color:#16a34a;color:#fff}}
.why{{font-size:12.5px;color:#5e6470;margin:10px 0 0}}
table{{width:100%;border-collapse:collapse;background:#fff;border:1px solid #e1e4e8;border-radius:10px;overflow:hidden;margin:10px 0}}
th,td{{padding:9px 12px;border-bottom:1px solid #f0f1f3;text-align:left;font-size:13.5px}}
th{{background:#f9fafb;color:#5e6470;font-size:11px;text-transform:uppercase;letter-spacing:.4px}}
td.num{{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}}
td.up{{color:#16a34a;font-weight:700}} td.down{{color:#dc2626;font-weight:700}}
td.skew{{color:#b45309;font-size:11px}}
.tbar{{display:grid;grid-template-columns:120px 1fr 90px;align-items:center;gap:10px;margin:6px 0}}
.tlab{{font-size:13px;font-weight:600}}
.ttrack{{background:#eef1f5;border-radius:5px;height:20px}} .ttrack span{{display:block;height:100%;background:#2563eb;border-radius:5px}}
.tval{{font-size:12.5px;font-variant-numeric:tabular-nums;text-align:right}}
code{{background:#eef2f7;padding:1px 5px;border-radius:4px;font-size:13px}}
</style></head><body>

<h1>The optimized follow-up playbook</h1>
<p class="sub">For leads who've shown <b>interest</b> — the most reply-getting way to follow up, FU1 through FU5,
ready to copy. Built from {ov['analyzed']:,} real follow-ups ({ov['positive']} positive replies, {ov['booked']} booked). Generated {DATE}.</p>

<div class="box"><h3>How to follow up — the rules in one place</h3><ul>{rule_items}</ul></div>

<h2>The 5-stage sequence</h2>
<p class="sub">Use <code>{{first_name}}</code> / <code>{{company}}</code> for the lead's details and fill the
<b>[bracketed]</b> bits per lead. One ask + your calendar link every time.</p>
{stage_cards}

<h2>What the winning replies actually do</h2>
<p class="sub">From the live effectiveness analysis — positive-reply rate with the trait vs without, and the lift.</p>
<h3 style="font-size:15px;color:#166534;margin:6px 0">✓ Do more of this</h3>
<table><thead><tr><th>What the message did</th><th></th><th>Reply rate</th><th>Lift</th><th></th></tr></thead><tbody>{do_rows}</tbody></table>
<h3 style="font-size:15px;color:#991b1b;margin:16px 0 6px">✕ Avoid this</h3>
<table><thead><tr><th>What the message did</th><th></th><th>Reply rate</th><th>Lift</th><th></th></tr></thead><tbody>{avoid_rows}</tbody></table>

<h2>Front-load it — reply rate drops fast by stage</h2>
<div class="box" style="border-left-color:#2563eb">{timing_bars}
<p class="muted" style="margin:12px 0 0">The first three touches earn most of the bookings. After FU5 it's under 4% — stop and restart fresh later rather than keep bumping.</p></div>

<div class="box warn"><h3>⚠️ How sure are we about this?</h3>
<p style="margin:0 0 8px">These patterns are <b>descriptive, not proven cause-and-effect</b>: most of the analyzed follow-ups
went to leads who were <i>already</i> warm, so a style's score is partly <i>who</i> got it, not just <i>how</i> it was written.
Notably, this playbook leans toward compliment/value openers, a clear ask and a businesslike tone — and <b>away</b> from
the GIF-heavy, ultra-casual style currently in heavy use, because that's what the data favors.</p>
<p style="margin:0"><b>The live A/B test</b> (your curated templates vs AI-written, on the Follow-ups page) is what will
<i>prove</i> which actually books more. Treat this as the strong starting hypothesis — and update it as the A/B fills in.</p></div>

<script>
document.querySelectorAll('.copy-btn').forEach(function(b){{
  b.addEventListener('click',function(){{
    navigator.clipboard.writeText(b.parentElement.querySelector('pre').innerText).then(function(){{
      b.classList.add('copied');b.textContent='Copied!';
      setTimeout(function(){{b.classList.remove('copied');b.textContent='Copy';}},1500);}});}});}});
</script>
</body></html>"""

    out = Path("docs/replies/FOLLOWUP_PLAYBOOK.html")
    out.write_text(doc, encoding="utf-8")
    print(f"wrote {out} ({len(STAGES)} stages, {len(do)} do / {len(avoid)} avoid rows, {len(timing)} timing bars)")


if __name__ == "__main__":
    main()
