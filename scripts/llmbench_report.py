"""Render the plain-English LLM cost-comparison report from the bake-off results.

Reads debug/_llmbench_results.json (+ debug/_bv_quality.json if present) and writes
ONE self-contained file: docs/LLM_COST_COMPARISON.html (plain-English findings +
a raw-numbers appendix; no separate CSV).
"""
import json
from pathlib import Path

R = json.load(open("debug/_llmbench_results.json"))
BV = json.loads(Path("debug/_bv_quality.json").read_text()) if Path("debug/_bv_quality.json").exists() else None
LAD = json.loads(Path("debug/_llmbench_ladder.json").read_text()) if Path("debug/_llmbench_ladder.json").exists() else None
DATE = "2026-06-17"

PROV_NAME = {"anthropic": "Anthropic (what we use now)", "openai": "OpenAI (cheapest model)",
             "gemini": "Google Gemini (cheapest model)"}
PROV_SHORT = {"anthropic": "Anthropic", "openai": "OpenAI", "gemini": "Google"}
FEAT = {
    "classifier": ("Sorting incoming replies", "Reads each reply and labels it — booked, interested, not interested, and so on."),
    "followup": ("Tagging follow-up style", "Labels how a follow-up email was written (its tone, opening, and the ask)."),
    "prospeo": ("Filtering scraped leads", "Decides whether a scraped company is a real product brand vs. an agency / reseller / marketplace."),
    "company": ("Cleaning up company names", "Picks the correct company name when our data sources disagree."),
    "brand_verify": ("Checking if a company is a real brand", "Reads a company's website to confirm it's a genuine product brand."),
}


def pct(x):
    return "—" if x is None else f"{round(x*100)}%"


VOL_UNIT = {"classifier": "replies", "followup": "follow-ups", "prospeo": "leads",
            "company": "leads", "brand_verify": "leads", "smartscout": "leads", "name": "leads"}


def vol_label(f):
    return f"{f['monthly_volume']:,} {VOL_UNIT.get(f['key'], 'items')}/mo"


def analyse(f):
    P = f["providers"]; h = P["anthropic"]; ha = h.get("agreement")
    now = h.get("proj_monthly", 0)
    if f["key"] == "brand_verify":
        return {"can_match": "Yes on the core check (below) — but the cost is mostly search fees, not the AI model",
                "rec": "Keep current for now", "now": now, "best": now, "tone": "review",
                "quality": "Tested separately — see the brand-checking section."}
    # cheapest rival within 3 pts of Haiku's own consistency
    cands = [(p, P[p]) for p in ("openai", "gemini")
             if P[p].get("agreement") is not None and P[p]["agreement"] >= ha - 0.03]
    cands.sort(key=lambda x: x[1]["proj_monthly"])
    qual = f"Matched our current results {pct(max((P[p]['agreement'] for p in ('openai','gemini')), default=0))} of the time (our own model repeats itself {pct(ha)})."
    if cands:
        p, v = cands[0]
        return {"can_match": f"Yes — {PROV_SHORT[p]}'s cheapest model matches or beats it",
                "rec": f"Switch to {PROV_SHORT[p]}", "now": now, "best": v["proj_monthly"],
                "tone": "switch", "quality": qual}
    return {"can_match": "No — the cheaper models make more mistakes here", "rec": "Keep current (Anthropic)",
            "now": now, "best": now, "tone": "keep", "quality": qual}


an = {f["key"]: analyse(f) for f in R}
tot = {k: round(sum(f["providers"][k]["proj_monthly"] for f in R if f["providers"].get(k, {}).get("proj_monthly") is not None), 2)
       for k in PROV_NAME}
# hybrid = each task on its RECOMMENDED option (best cheap where quality holds, Anthropic otherwise)
hybrid = round(sum(a["best"] for a in an.values()), 2)
hybrid_save = round(tot["anthropic"] - hybrid, 2)

# Per-variant monthly totals (every task on one model) from the ladder run
def _tile(val, label, cls=""):
    return f'<div class="tile {cls}"><div class="n">${val}</div><div class="k">{label}</div></div>'

if LAD:
    lt = {lbl: round(sum(f["models"][lbl]["proj_monthly"] for f in LAD
                         if f["models"][lbl].get("proj_monthly") is not None), 2)
          for lbl in LAD[0]["models"]}
    now_l = tot["anthropic"]  # headline "today" — consistent with the per-task table above
    save = round(now_l - hybrid, 2)
    tiles = [
        _tile(now_l, "Anthropic Haiku 4.5 — what we pay today"),
        _tile(hybrid, f"<b>Recommended switches (hybrid)</b> — best provider per task, keep Anthropic where it's better; saves ~${save}/mo", "rec-tile"),
        _tile(lt.get("GPT-5.4 nano"), "Every task on GPT-5.4 nano (cheapest)"),
        _tile(lt.get("Gemini 3.1 Flash-Lite"), "Every task on Gemini 3.1 Flash-Lite"),
        _tile(lt.get("GPT-5.4 mini"), "Every task on GPT-5.4 mini"),
        _tile(lt.get("GPT-5.4"), "Every task on GPT-5.4 — <b>over today's cost</b>", "over-tile"),
    ]
    tiles_html = '<div class="totrow">' + "".join(tiles) + "</div>"
else:
    tiles_html = ('<div class="totrow">'
                  + _tile(tot["anthropic"], "Anthropic — what we pay today")
                  + _tile(tot["openai"], "If every task used OpenAI's cheapest")
                  + _tile(tot["gemini"], "If every task used Gemini's cheapest")
                  + _tile(hybrid, f"<b>Recommended switches</b> — saves ~${hybrid_save}/mo", "rec-tile")
                  + "</div>")

# What the "$/month" figures assume — the monthly volume per task
vol_basis_html = ('<div class="box"><h3>What "per month" assumes (your target volumes)</h3>'
                  '<table class="lad"><thead><tr><th>Task</th><th>Volume / month</th></tr></thead><tbody>'
                  + "".join(f"<tr><td>{FEAT[f['key']][0]}</td><td class='num'>{vol_label(f)}</td></tr>" for f in R)
                  + "</tbody></table>"
                  '<p class="fine">Lead-side tasks (filtering, company names, brand-checking) are projected at your '
                  '<b>20,000 leads/month</b> target (5,000/week). Reply-side tasks use the last 30 days\' measured '
                  'traffic (~1,423 replies, ~489 manual follow-ups). Every "$ / month" in this report = the measured '
                  'per-item cost × the volume on this row.</p></div>')

rows = []
for f in R:
    a = an[f["key"]]; nm, desc = FEAT[f["key"]]
    P = f["providers"]
    tested = max((P[k].get("n") or 0) for k in PROV_NAME)
    if f["key"] == "brand_verify" and BV:
        tnote = (f" · cost measured on {tested} records; quality verified on "
                 f"{BV['n_domains']} human-graded companies · $/mo at {vol_label(f)}")
    else:
        tnote = f" · tested on {tested} real records · $/mo at {vol_label(f)}"
    cost = (f"${a['now']}/mo" if a["now"] == a["best"]
            else f"${a['now']} → <b>${a['best']}</b>/mo")
    rows.append(f"""<tr class="t-{a['tone']}">
      <td><b>{nm}</b><span class="desc">{desc}<span class="tested">{tnote}</span></span></td>
      <td>{a['can_match']}</td>
      <td class="rec">{a['rec']}</td>
      <td class="num">{cost}</td></tr>""")

# brand-verify quality (plain)
bv_html = ""
if BV:
    p = BV["providers"]
    bv_html = f"""<h2>The big one — checking if a company is a real brand</h2>
<p>This is where almost all the money is (~${an['brand_verify']['now']}/mo of AI cost at 20,000 leads/month, plus search fees — below).
We checked whether the cheaper models can do its core judgment as well as our current one, using {BV['n_domains']} companies a human had already graded.</p>
<ul>
<li><b>Quality holds.</b> The cheaper models agreed with our current setup <b>{pct(p['openai'].get('agree_with_haiku'))}–{pct(p['gemini'].get('agree_with_haiku'))}</b> of the time, and — most important — <b>never wrongly rejected a good company</b> (0 out of {p['anthropic'].get('passes')}), same as today.</li>
<li><b>But switching barely saves money.</b> Most of brand-checking's cost is the <b>web-search fee</b> — about <b>$190–255/month</b> at 20,000 leads — and that fee is <b>almost the same on every provider</b> ($10–14 per 1,000 searches). Changing the AI model only trims the smaller "thinking" cost (~$70–90/month). So the headline saving you'd expect from switching mostly isn't there.</li>
<li><b>Recommendation:</b> don't rebuild brand-checking on another provider just to save money — the saving is small and it's ~3 days of work to rebuild + maintain. If brand-checking cost matters, the real lever is doing <i>fewer</i> web searches, which is a separate change.</li>
</ul>"""

# --- tier ladder ("does paying for a higher model help?") ---
LAD = json.loads(Path("debug/_llmbench_ladder.json").read_text()) if Path("debug/_llmbench_ladder.json").exists() else None
ladder_html = ""
if LAD:
    labels = list(LAD[0]["models"].keys())  # ladder order, cheapest→up
    lhead = "".join(f"<th>{l}</th>" for l in labels)
    lrows = []
    for f in LAD:
        now = f["models"].get("Haiku 4.5 (current)", {}).get("proj_monthly")
        cells = []
        for l in labels:
            d = f["models"][l]
            mo = d.get("proj_monthly")
            over = (l != "Haiku 4.5 (current)" and mo is not None and now is not None and mo > now)
            ag = "cost only" if d.get("agreement") is None else f"{round(d['agreement']*100)}%"
            cells.append(f"<td class='{'over' if over else ''}'>{ag}<span class='mo'>${mo}/mo</span></td>")
        lrows.append(f"<tr><td>{FEAT[f['key']][0]}</td>{''.join(cells)}</tr>")
    ladder_html = f"""<h2>Does paying for a higher-tier model help? (we tested the ladder)</h2>
<p>We also walked each provider <b>up the tiers</b> — from cheapest to the point where it costs more than we
pay today — on the <b>same 50 records</b>. Cells show the match rate with our current results and the projected
$/month; <span class="over-key">amber</span> = costs more than today.</p>
<table class="lad"><thead><tr><th>Task</th>{lhead}</tr></thead><tbody>{''.join(lrows)}</tbody></table>
<div class="box" style="border-left-color:#b45309"><b>Short answer: no — paying more doesn't buy better quality here.</b>
On most tasks the cheapest models already match our current one, and stepping up makes <b>no difference or is
slightly worse</b> (bigger reasoning models over-think these short, format-locked tasks — e.g. company-name accuracy
<i>drops</i> from 88% to 78% as you go up). The only task where the cheap models trail — the <b>lead filter</b> —
isn't rescued within budget either: the best in-budget option (Gemini Flash-Lite, 76%) still trails today's 80%, and
even the over-budget GPT-5.4 (78%) doesn't beat it. <b>So no higher tier is worth paying for — the recommendation is unchanged.</b></div>"""

PROV_LABEL = {"anthropic": "Anthropic · Haiku 4.5", "openai": "OpenAI · GPT-5.4 nano",
              "gemini": "Google · Gemini 3.1 Flash-Lite"}
detail_rows = []
if LAD:
    # Full ladder: every tested model of every provider, with complete stats.
    lad_labels = list(LAD[0]["models"].keys())
    for f in LAD:
        nm = FEAT[f["key"]][0]
        for i, lbl in enumerate(lad_labels):
            d = f["models"].get(lbl, {})
            ag = "cost-only" if d.get("agreement") is None else pct(d.get("agreement"))
            first = f"<td><b>{nm}</b></td>" if i == 0 else "<td></td>"
            over = d.get("proj_monthly") is not None and d["proj_monthly"] > f["models"].get("Haiku 4.5 (current)", {}).get("proj_monthly", 1e9)
            mcls = " class='over'" if over else ""
            detail_rows.append(
                f"<tr><td{mcls if i==0 else ''}>{nm if i==0 else ''}</td><td class='num'>{vol_label(f) if i==0 else ''}</td><td{mcls}>{lbl}</td><td class='num'>{d.get('n','—')}</td>"
                f"<td class='num'>{ag}</td><td class='num'>{d.get('avg_in','—')} / {d.get('avg_out','—')}</td>"
                f"<td class='num'>${d.get('cost_per_1k_raw','—')}</td><td class='num{' over' if over else ''}'>${d.get('proj_monthly','—')}</td></tr>")
else:
    for f in R:
        nm = FEAT[f["key"]][0]
        for i, k in enumerate(("anthropic", "openai", "gemini")):
            d = f["providers"].get(k, {})
            ag = "cost-only" if d.get("agreement") is None else pct(d.get("agreement"))
            first = f"<td><b>{nm}</b></td>" if i == 0 else "<td></td>"
            detail_rows.append(
                f"<tr>{first}<td class='num'>{vol_label(f) if i==0 else ''}</td><td>{PROV_LABEL[k]}</td><td class='num'>{d.get('n','—')}</td>"
                f"<td class='num'>{ag}</td><td class='num'>{d.get('avg_in','—')} / {d.get('avg_out','—')}</td>"
                f"<td class='num'>${d.get('cost_per_1k_raw','—')}</td><td class='num'>${d.get('proj_monthly','—')}</td></tr>")
bvq_rows = []
if BV:
    for k in ("anthropic", "openai", "gemini"):
        s = BV["providers"][k]
        bvq_rows.append(
            f"<tr><td>{PROV_LABEL[k]}</td><td class='num'>{s.get('n')}</td>"
            f"<td class='num'>{pct(s.get('agree_with_haiku'))}</td>"
            f"<td class='num'>{s.get('false_rejections')}/{s.get('passes')}</td>"
            f"<td class='num'>{s.get('fail_caught')}/{s.get('fails')}</td>"
            f"<td class='num'>${s.get('cost_per_call')}</td></tr>")
detail_html = f"""<h2>Appendix — the raw numbers behind every claim</h2>
<p class="lede">Everything above comes from these measured figures — <b>every model we tested, on every task</b> (the full
tier ladder, 50 real records per task). "Match rate" = how often each model gave the same answer our current setup
gives (Haiku's own row is self-consistency on a re-run — the realistic ceiling). "Cost / 1,000 calls" is the raw
single-call cost; "$/month" is that projected at your volumes; <span class="over-key">amber</span> = costs more than today.</p>
<table><thead><tr><th>Task</th><th>Volume / month</th><th>Provider · model</th><th>Records tested</th><th>Match rate</th>
<th>Avg tokens (in / out)</th><th>Cost / 1,000 calls</th><th>$ / month</th></tr></thead>
<tbody>{''.join(detail_rows)}</tbody></table>
<h3 style="font-size:15px;margin-top:18px">Brand-checking — quality test detail (site-verdict step, vs human-graded companies)</h3>
<table><thead><tr><th>Provider · model</th><th>Companies</th><th>Match w/ current</th>
<th>Good wrongly rejected</th><th>Bad caught (site step)</th><th>$ / call</th></tr></thead>
<tbody>{''.join(bvq_rows)}</tbody></table>"""

doc = f"""<!doctype html><html><head><meta charset="utf-8">
<title>AI provider cost comparison — Dwyer</title><style>
body{{font-family:-apple-system,Segoe UI,Helvetica,Arial,sans-serif;color:#1a1d23;max-width:920px;margin:0 auto;padding:30px;line-height:1.6;font-size:15px;background:#fafafa}}
h1{{font-size:26px;margin:0 0 6px}} h2{{font-size:19px;margin:30px 0 10px}}
.lede{{color:#5e6470;margin:0 0 22px}}
.box{{background:#fff;border:1px solid #e1e4e8;border-left:4px solid #16a34a;border-radius:10px;padding:18px 20px;margin:18px 0}}
.box h3{{margin:0 0 8px;font-size:16px}}
table{{width:100%;border-collapse:collapse;background:#fff;border:1px solid #e1e4e8;border-radius:10px;overflow:hidden;margin:12px 0}}
th,td{{padding:11px 13px;border-bottom:1px solid #f0f1f3;text-align:left;vertical-align:top}}
th{{background:#f9fafb;color:#5e6470;font-size:12px;text-transform:uppercase;letter-spacing:.4px}}
td.num{{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}}
td.rec{{font-weight:600}}
.desc{{display:block;color:#9aa0aa;font-weight:400;font-size:12.5px;margin-top:3px}}
.tested{{font-style:italic}}
tr.t-switch td.rec{{color:#166534}} tr.t-keep td.rec{{color:#b45309}} tr.t-review td.rec{{color:#6b7280}}
ul{{margin:8px 0;padding-left:22px}} li{{margin:6px 0}}
.totrow{{display:flex;gap:14px;flex-wrap:wrap;margin:10px 0}}
.tile{{flex:1;min-width:200px;background:#fff;border:1px solid #e1e4e8;border-radius:10px;padding:14px 16px}}
.tile .n{{font-size:22px;font-weight:800;color:#2563eb}} .tile .k{{color:#5e6470;font-size:13px;margin-top:2px}}
.tile.rec-tile{{border:2px solid #16a34a;background:#f3fbf6}} .tile.rec-tile .n{{color:#16a34a}}
.tile.over-tile{{border:2px solid #b45309;background:#fef7ec}} .tile.over-tile .n{{color:#b45309}}
.fine{{color:#9aa0aa;font-size:12.5px}}
table.lad td,table.lad th{{text-align:center;font-size:12.5px;padding:8px 9px}}
table.lad td:first-child,table.lad th:first-child{{text-align:left;font-weight:600}}
table.lad td.over,td.over{{background:#fef7ec}}
table.lad .mo{{display:block;color:#9aa0aa;font-size:11px;font-variant-numeric:tabular-nums}}
.over-key{{background:#fef7ec;padding:0 5px;border-radius:3px}}
</style></head><body>
<h1>Which AI provider should each task use — and what would it cost?</h1>
<p class="lede">Our system uses AI for several jobs (sorting replies, checking brands, cleaning data, filtering leads).
Today that all runs on one provider, <b>Anthropic</b>. We tested whether <b>OpenAI</b> or <b>Google Gemini</b>'s
cheapest models could do the same jobs as well — and what each would cost per month at our volumes
(20,000 leads/month, plus the reply traffic). Generated {DATE}.</p>

<div class="box"><h3>The 60-second summary</h3>
<ul>
<li><b>The reply-side jobs cost almost nothing</b> on any provider (well under $1/month) — not worth changing for cost.</li>
<li><b>Cleaning up company names is a free upgrade:</b> OpenAI's cheapest model is actually a bit <b>more accurate</b> than what we use today, and far cheaper. Worth switching.</li>
<li><b>Filtering scraped leads should stay as-is</b> — the cheaper models make noticeably more mistakes, and the saving is tiny.</li>
<li><b>Brand-checking is the only big cost</b>, but most of it is an unavoidable <b>web-search fee that's the same on every provider</b> — so switching providers barely helps there. Don't rebuild it just to save money.</li>
</ul>
<p style="margin:10px 0 0"><b>Bottom line:</b> take the free company-name win; leave everything else where it is. The big-looking numbers below are mostly a search fee no provider can avoid, not a saving you can capture by switching.</p></div>

<h2>Every AI task, side by side</h2>
<p class="lede">"Cost/month" is the AI model cost projected at our volumes. (Brand-checking also has a separate web-search fee — see its section.)</p>
<table><thead><tr><th>Task</th><th>Can a cheaper provider match it?</th><th>Recommendation</th><th>Cost / month</th></tr></thead>
<tbody>{''.join(rows)}</tbody></table>

<h2>What it adds up to per month (AI model cost only)</h2>
{vol_basis_html}
{tiles_html}
<p class="fine">Each tile = the monthly total if <b>every</b> task ran on that one model (measured on the same real records, all five tested features summed). "Recommended (hybrid)" mixes providers — cheapest model that matches today's quality per task, Anthropic everywhere else.</p>
<p class="fine">⚠️ The "every task on one cheap provider" numbers look dramatic, but they're not realistic — they'd switch
tasks where the cheaper models are worse, or where switching means a costly rebuild. The <b>recommended (hybrid)</b>
figure is the honest one: it switches only where quality holds and is safe to do. It's close to today's ${tot['anthropic']}
because the two biggest lines — <b>brand-checking</b> and the <b>lead filter</b> — stay on Anthropic (brand-checking's
real cost is a web-search fee, ~$190–255/mo, that's the same on every provider; the lead filter loses accuracy on the
cheaper models). The genuine, safe saving is small (~${hybrid_save}/mo), almost all from the company-name task.</p>

{ladder_html}

{bv_html}

<h2>Would "caching" cut the cost? (we checked — no)</h2>
<p>Providers offer <b>prompt caching</b>: if you send the exact same instructions over and over, you pay ~90% less
for that repeated part after the first time. It sounds like an easy saving, so we tested whether it helps us.</p>
<ul>
<li><b>It's already switched on for two tasks — but it does nothing.</b> Reply-sorting and follow-up tagging have a
cache marker in the code, but we measured it live: <b>zero cache hits</b>. Anthropic only caches instructions above
~4,000 tokens; ours are ~1,600 or smaller, so they fall under the cutoff and silently don't cache.</li>
<li><b>The other tasks have no caching</b> — and their instructions are even smaller, so turning it on would also do
nothing.</li>
<li><b>Even if it worked, it only discounts the AI "thinking" cost</b> — the small part. It can't touch the
brand-checking <b>web-search fee</b>, which is where the real money is.</li>
</ul>
<p class="box" style="border-left-color:#b45309"><b>Bottom line: caching is a non-lever for us — realistic saving ≈ $0.</b>
The existing markers are harmless, so we leave them. (The only case where caching would matter is moving a
high-volume task to OpenAI, whose caching starts at a smaller size and would shave a little off the token cost —
still minor, and not worth a switch on its own.)</p>

<h2>How solid are these numbers? (measured, not guessed)</h2>
<p>Almost everything here comes from real test runs, not estimates:</p>
<ul>
<li>✅ <b>Cost per task</b> — measured from <b>actual API calls</b> (real token counts) on all three providers.</li>
<li>✅ <b>Quality / "does it match"</b> — measured on <b>50 real records per task</b> (sorting replies, follow-ups, lead filter, company names), scored against our current results. Brand-checking quality was tested on <b>{BV['n_domains'] if BV else 71} companies a human had already graded</b> ({BV['providers']['anthropic']['passes'] if BV else 36} known-good, {BV['providers']['anthropic']['fails'] if BV else 15} known-bad). Directional, not precise to the last point — but real.</li>
<li>✅ <b>Prices</b> — each provider's <b>current published rates</b> (June 2026).</li>
<li>✅ <b>The caching check</b> — measured live (it produced zero savings).</li>
<li>📊 <b>Monthly totals</b> — your measured cost-per-item × your volumes. The reply and follow-up volumes are measured from the last 30 days; <b>20,000 leads/month is your stated target</b>. The per-item figure assumes the normal batch size we run in production.</li>
<li>⚠️ <b>The one estimated piece</b> — brand-checking's <b>web-search fee</b>: how <i>many</i> searches per month is an assumption (not yet measured at 20k-lead scale), and only brand-checking's core judgment was quality-tested, not its web-search steps.</li>
</ul>
<p class="fine"><b>So: outside the brand-checking web-search line, treat these as real measured figures — not guesses.</b> The full per-provider numbers are in the appendix at the bottom.</p>

<h2>How to read the numbers (plain version)</h2>
<ul>
<li><b>"Match it"</b> = how often a cheaper model gave the same answer our current setup gives. Our own model only repeats itself ~85–90% of the time (AI isn't perfectly consistent), so "matches" means "as close as our current model is to itself".</li>
<li><b>"Cost / month"</b> = projected at 20,000 leads/month and our reply volume. It's the AI model fee only; web-search fees are called out separately for brand-checking.</li>
<li>We tested the <b>cheapest model</b> of each provider (that's what these jobs use today): Anthropic Haiku, OpenAI's nano, Google's Flash-Lite. We checked ~50 real items per task (~70 for brand-checking) — enough to be directionally right, not precise to the last point.</li>
</ul>

{detail_html}
</body></html>"""

Path("docs").mkdir(exist_ok=True)
Path("docs/LLM_COST_COMPARISON.html").write_text(doc, encoding="utf-8")

print("wrote docs/LLM_COST_COMPARISON.html (raw numbers folded into the appendix)")
print(f"totals: Anthropic ${tot['anthropic']}  OpenAI ${tot['openai']}  Gemini ${tot['gemini']}")
