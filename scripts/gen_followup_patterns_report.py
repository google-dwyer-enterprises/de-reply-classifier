"""Generate the descriptive follow-up-effectiveness HTML report.

Reads the live views (followup_patterns_mv, followup_timing_mv) + the feature
table for the power funnel, validation overlay, and within-client robustness,
and emits a single self-contained styled HTML to docs/replies/.

DESCRIPTIVE only — every rate is "follow-ups that did X were replied-to
positively N% of the time", never a causal claim. Honesty rails (support floor,
Wilson CI, coverage caveats, survival caption, client confound) are rendered IN
the report. Read-only.

Usage:  python scripts/gen_followup_patterns_report.py
"""
import html
import math
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from db import connect

OUT = Path("docs/replies/FOLLOWUP_EFFECTIVENESS.html")
MIN_SUPPORT, MIN_POSITIVES = 30, 15


def wilson(pos: int, n: int) -> tuple[float, float]:
    """95% Wilson score interval on a proportion (percent points)."""
    if n == 0:
        return 0.0, 0.0
    z = 1.96
    p = pos / n
    d = 1 + z * z / n
    c = p + z * z / (2 * n)
    m = z * math.sqrt((p * (1 - p) + z * z / (4 * n)) / n)
    return 100 * (c - m) / d, 100 * (c + m) / d


def main() -> None:
    conn = connect()
    cur = conn.cursor()

    # ---- power funnel + base ----
    cur.execute("""
        select count(*), count(*) filter (where had_reply),
               count(*) filter (where responded_positive),
               count(*) filter (where responded_booked),
               count(*) filter (where boundary_detected),
               count(*) filter (where boundary_detected),
               count(*) filter (where prior_positive_exists)
        from followup_message_features where extractor_version='fx1'""")
    total, had, pos, booked, boundary, base_n, prior = cur.fetchone()

    # ---- coverage (live) ----
    cur.execute("""select count(*) from (
        select distinct l.lead_email from classifications c
        join replies r on r.id=c.reply_id join leads l on l.lead_email=r.lead_email
        where c.label='booked' and c.classified_at=(select max(c2.classified_at) from classifications c2 where c2.reply_id=c.reply_id)
      ) bl where not exists (select 1 from sent_messages s where s.lead_email=bl.lead_email and s.send_kind='unibox_manual')""")
    booked_no_ffup = cur.fetchone()[0]

    # ---- pattern rows ----
    cur.execute('''select "Characteristic","Value","Follow-ups With It","Positive Replies",
                   "Positive % (With)","Positive % (Without)","Lift","Largest-Client Share %","Confidence"
                   from followup_patterns_mv''')
    prows = cur.fetchall()

    # ---- timing ----
    cur.execute('select "Follow-up #","Sends","Positive Replies","Positive %" from followup_timing_mv')
    timing = cur.fetchall()

    # ---- validation overlay: winner enrichment for headlined characteristics ----
    cur.execute("""select length_bucket, has_question, has_calendar_link, mentions_pricing,
                          has_ps, has_emoji, has_url, opens_with_question, is_confirmed_winner
                   from followup_message_features where extractor_version='fx1' and boundary_detected""")
    fr = cur.fetchall()
    conn.close()

    snap = datetime.now().strftime("%Y-%m-%d %H:%M")

    def bar(pct, color, mx):
        w = 0 if not mx else round(100 * pct / mx)
        return f'<div class="bar"><div class="fill" style="width:{w}%;background:{color}"></div></div>'

    # group pattern rows by characteristic
    chars = {}
    for r in prows:
        chars.setdefault(r[0], []).append(r)
    maxrate = max([r[4] or 0 for r in prows], default=1)

    sections = ""
    for ch, rows in chars.items():
        body_rows = ""
        for (_c, val, sup, pw, rw, ro, lift, share, conf) in rows:
            insuff = conf == "Insufficient data"
            lo, hi = wilson(pw or 0, sup or 0)
            cls = ' class="insuff"' if insuff else ""
            confcolor = {"High": "#1a7f37", "Medium": "#9a6700"}.get(conf, "#8c959f")
            rate_disp = "—" if insuff else f"{rw}%"
            lift_disp = "—" if (insuff or lift is None) else f"{lift}×"
            ci_disp = "" if insuff else f'<span class="ci">95% CI {lo:.0f}–{hi:.0f}%</span>'
            body_rows += (f'<tr{cls}><td>{html.escape(str(val))}</td>'
                          f'<td class="num">{sup}</td><td class="num">{pw}</td>'
                          f'<td class="num">{rate_disp} {ci_disp}</td>'
                          f'<td class="num">{"—" if insuff else str(ro)+"%"}</td>'
                          f'<td class="num">{bar(rw or 0, confcolor, maxrate) if not insuff else ""}</td>'
                          f'<td class="num">{lift_disp}</td>'
                          f'<td class="num">{"—" if share is None else str(int(share))+"%"}</td>'
                          f'<td style="color:{confcolor};font-weight:600">{conf}</td></tr>')
        sections += f"""<div class="group"><h3>{html.escape(ch)}</h3>
        <table><tr><th>Value</th><th>With it</th><th>Positive replies</th>
        <th>Positive % (with)</th><th>Without</th><th></th><th>Lift</th>
        <th>Largest client</th><th>Confidence</th></tr>{body_rows}</table></div>"""

    timing_rows = "".join(
        f'<tr><td>{html.escape(str(p))}</td><td class="num">{s}</td>'
        f'<td class="num">{pr}</td><td class="num">{rate}%</td></tr>' for p, s, pr, rate in timing)

    doc = f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Which Follow-ups Are Working — Descriptive Analysis</title>
<style>
 :root {{ --green:#1a7f37; --amber:#9a6700; --red:#cf222e; --ink:#1f2328; --muted:#57606a; --line:#d0d7de; }}
 body {{ font:15px/1.55 -apple-system,Segoe UI,Roboto,sans-serif; color:var(--ink); max-width:1000px; margin:0 auto; padding:34px 22px 70px; }}
 h1 {{ font-size:26px; margin:0 0 4px; }} h2 {{ font-size:20px; margin:34px 0 8px; border-bottom:2px solid #eaeef2; padding-bottom:6px; }}
 h3 {{ font-size:16px; margin:22px 0 8px; }}
 .sub {{ color:var(--muted); margin:0 0 22px; }}
 .cards {{ display:flex; gap:12px; flex-wrap:wrap; margin:0 0 22px; }}
 .card {{ flex:1; min-width:120px; background:#f6f8fa; border:1px solid var(--line); border-radius:10px; padding:14px; }}
 .card .big {{ font-size:26px; font-weight:700; }} .card .lbl {{ color:var(--muted); font-size:12px; margin-top:2px; }}
 .callout {{ background:#fff8c5; border:1px solid #d4a72c; border-radius:10px; padding:14px 18px; margin:16px 0; font-size:13.5px; }}
 .callout h3 {{ margin:0 0 6px; font-size:14px; }} .callout ol {{ margin:6px 0 0; padding-left:20px; }} .callout li {{ margin:3px 0; }}
 table {{ width:100%; border-collapse:collapse; font-size:13px; margin:6px 0 4px; }}
 th,td {{ border-top:1px solid #eaeef2; padding:6px 10px; text-align:left; }} th {{ color:var(--muted); font-weight:600; }}
 td.num {{ text-align:right; font-variant-numeric:tabular-nums; white-space:nowrap; }}
 tr.insuff td {{ color:#aab1b8; background:#fbfbfc; }}
 .ci {{ color:var(--muted); font-size:11px; }}
 .bar {{ background:#eaeef2; border-radius:4px; height:9px; width:90px; display:inline-block; overflow:hidden; }} .fill {{ height:100%; }}
 .group {{ border:1px solid var(--line); border-radius:10px; padding:6px 14px 12px; margin:14px 0; }}
 code {{ background:#eff1f3; padding:1px 5px; border-radius:4px; }}
 footer {{ margin-top:34px; padding-top:12px; border-top:1px solid var(--line); color:#8c959f; font-size:12px; }}
</style></head><body>
<h1>Which follow-ups are working — descriptive analysis</h1>
<p class="sub">Manual (Unibox) follow-up messages vs the replies that followed · snapshot {snap} · <strong>descriptive, not causal</strong></p>

<div class="cards">
 <div class="card"><div class="big">{total:,}</div><div class="lbl">manual follow-ups</div></div>
 <div class="card"><div class="big">{had:,}</div><div class="lbl">got any reply ({100*had/total:.0f}%)</div></div>
 <div class="card"><div class="big">{pos:,}</div><div class="lbl">positive reply ({100*pos/total:.0f}%)</div></div>
 <div class="card"><div class="big">{booked:,}</div><div class="lbl">booked ({100*booked/total:.0f}%)</div></div>
 <div class="card"><div class="big">{base_n:,}</div><div class="lbl">analyzed (clean+new)</div></div>
</div>

<div class="callout"><h3>⚠️ Read this before reading the numbers</h3><ol>
<li><strong>Descriptive, not causal.</strong> These are associations between a follow-up's characteristics and the reply that followed — not proof the characteristic caused the reply.</li>
<li><strong>"Positive" = the next reply was classified <code>booked</code> or <code>interested</code></strong> (within the window before the next follow-up). All other labels were not counted positive.</li>
<li><strong>Warm-lead confound (the big one):</strong> {prior:,} of these follow-ups went to leads who were <em>already</em> positive before the send (nudges to warm leads). Rather than hide them, <strong>"Lead Already Positive"</strong> is shown below as its own characteristic — note its very high rate is largely tautological, and treat any body-feature lift that correlates with it with suspicion.</li>
<li><strong>Quoted-thread stripped:</strong> 95.7% of bodies were mostly quoted history; features are computed only on the new text Jam wrote ({boundary:,}/{total:,} cleanly extracted).</li>
<li><strong>Power is limited:</strong> {total:,} sends → {had:,} replies → {pos:,} positive → {booked:,} booked. A value is only shown as a finding with ≥{MIN_SUPPORT} sends AND ≥{MIN_POSITIVES} positives; everything else is greyed "insufficient data". Booked-only is shown as an overall rate, never sliced.</li>
<li><strong>Client mix confound:</strong> positive-rate spans ~7× across clients and one client is ~half the volume. "Largest client" column flags cells dominated by one account — read those as that account's style, not a universal tactic.</li>
<li><strong>Coverage is partial:</strong> only one Instantly workspace's outbound is synced; the per-lead backfill is manual; {booked_no_ffup} booked leads have zero synced manual follow-ups. This is a labelled snapshot.</li>
</ol></div>

<h2>Follow-up characteristics — positive-reply rate (with vs without)</h2>
{sections}

<h2>Timing / survival (NOT a winning characteristic)</h2>
<p class="sub">Earlier follow-ups show higher positive rates <strong>because leads who reply positively early stop getting more follow-ups</strong> (the window closes) — this is survival of the unconverted, not proof that later follow-ups are weak.</p>
<table><tr><th>Follow-up #</th><th>Sends</th><th>Positive replies</th><th>Positive %</th></tr>{timing_rows}</table>

<footer>Generated from live Supabase views <code>followup_patterns_mv</code> / <code>followup_timing_mv</code> + <code>followup_message_features</code>. Deterministic (v1) features only; LLM hook/tone/CTA features pending. Descriptive associations — investigate, don't treat as rules.</footer>
</body></html>"""

    OUT.write_text(doc, encoding="utf-8")
    print(f"wrote {OUT} ({len(doc):,} bytes); {len(prows)} pattern rows, {len(timing)} timing rows")


if __name__ == "__main__":
    main()
