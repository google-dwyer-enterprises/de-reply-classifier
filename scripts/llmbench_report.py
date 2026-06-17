"""Render the LLM cost-comparison HTML report + CSV from the bake-off results.

Reads debug/_llmbench_results.json (written by llmbench_run.py) and writes
docs/LLM_COST_COMPARISON.html + debug/_llmbench.csv.
"""
import csv
import datetime as _dt
import html
import json
from pathlib import Path

R = json.load(open("debug/_llmbench_results.json"))
PROVS = [("anthropic", "Anthropic Haiku 4.5"), ("openai", "OpenAI GPT-5.4 nano"),
         ("gemini", "Gemini 3.1 Flash-Lite")]
PRICE = {"anthropic": "$1.00 / $5.00", "openai": "$0.20 / $1.25", "gemini": "$0.25 / $1.50"}
DATE = "2026-06-17"  # generated date (Date.now unavailable in-tool; stamped manually)


def verdict(f):
    P = f["providers"]; h = P.get("anthropic", {}); ha = h.get("agreement")
    if not f.get("note") and ha is not None:
        cands = [(p, P[p]) for p in ("openai", "gemini")
                 if P.get(p, {}).get("agreement") is not None and P[p]["agreement"] >= ha - 0.03]
        cands.sort(key=lambda x: x[1]["proj_monthly"])
        if cands:
            p, v = cands[0]
            nm = dict(PROVS)[p]
            return ("switch", f"Switch to <b>{nm}</b> — matches Haiku on quality "
                    f"(agree {v['agreement']} vs {ha}) and is cheaper "
                    f"(${h['proj_monthly']}→${v['proj_monthly']}/mo).")
        return ("keep", f"<b>Keep Haiku.</b> The cheaper models lose quality here "
                f"(nano {P['openai'].get('agreement')}, Gemini {P['gemini'].get('agreement')} vs Haiku {ha}); "
                f"the saving isn't worth it.")
    # cost-only feature
    return ("review", "Cost-only — quality not yet compared across providers (multi-step + provider-specific "
            "web search). Biggest dollar lever, but a switch is a real rebuild, not a config change.")


def cell(d):
    if not d or not d.get("n"):
        return "<td>—</td><td>—</td><td>—</td>"
    ag = "—" if d["agreement"] is None else f"{d['agreement']:.2f}"
    pf = f" <span class='pf'>({d['parse_fail']} unparsed)</span>" if d.get("parse_fail") else ""
    return (f"<td class='num'>{ag}{pf}</td>"
            f"<td class='num'>${d['cost_per_1k_raw']}</td>"
            f"<td class='num strong'>${d['proj_monthly']}</td>")


rows_html = []
for f in R:
    v_kind, v_text = verdict(f)
    tr = [f"<tr><td class='feat'>{html.escape(f['label'])}"
          f"<span class='vol'>{f['monthly_volume']:,}/mo · {'1 call/item' if f['batch']==1 else f'batch '+str(f['batch'])}"
          + (f" · ~{f['calls_per_item']} calls/item" if f.get('calls_per_item',1)!=1 else "") + "</span></td>"]
    for key, _ in PROVS:
        tr.append(cell(f["providers"].get(key)))
    tr.append("</tr>")
    rows_html.append("".join(tr))
    rows_html.append(f"<tr class='verdict v-{v_kind}'><td colspan='10'>{v_text}</td></tr>")

tot = {k: round(sum(f["providers"][k]["proj_monthly"] for f in R
                    if f["providers"].get(k, {}).get("proj_monthly") is not None), 2) for k, _ in PROVS}

# blended: best provider per feature where quality holds, Haiku where it doesn't / unverified
blend = 0.0
for f in R:
    P = f["providers"]; v_kind, _ = verdict(f)
    if v_kind == "switch":
        best = min((P[p] for p in ("openai", "gemini")
                    if P[p].get("agreement") is not None and P[p]["agreement"] >= P["anthropic"]["agreement"] - 0.03),
                   key=lambda x: x["proj_monthly"])
        blend += best["proj_monthly"]
    else:
        blend += P["anthropic"]["proj_monthly"]   # keep Haiku for 'keep' + 'review' (unverified)
blend = round(blend, 2)

# Phase-2 brand-verify quality (site-verdict step), if present
BV = None
_bvp = Path("debug/_bv_quality.json")
if _bvp.exists():
    BV = json.loads(_bvp.read_text())
bv_section = ""
if BV:
    bvr = []
    for k, _ in PROVS:
        s = BV["providers"].get(k, {})
        bvr.append(f"<tr><td class='feat'>{dict(PROVS)[k]}</td>"
                   f"<td class='num'>{s.get('agree_with_haiku')}</td>"
                   f"<td class='num strong'>{s.get('false_rejections')}/{s.get('passes')}</td>"
                   f"<td class='num'>{s.get('fail_caught')}/{s.get('fails')}</td>"
                   f"<td class='num'>${s.get('cost_per_call')}</td></tr>")
    bv_section = f"""<h2>Brand-verify — quality test (Phase 2, site-verdict step)</h2>
<p class="lede">The bake-off above could only price brand-verify, not judge its quality (multi-step + Anthropic web search).
This isolates the funnel's <b>no-tool site-verdict step</b> (homepage signals → brand / reseller / unknown), replays it
on {BV['n_domains']} human-labeled companies (`qa_audit_labels`), and scores against ground truth.</p>
<table><thead><tr><th>Provider</th><th>Agree w/ Haiku</th><th>False rejections (hard gate=0)</th><th>Fail catch (site step only)</th><th>$/call</th></tr></thead>
<tbody>{''.join(bvr)}</tbody></table>
<div class="box win"><b>Finding.</b> On the core site-verdict judgment, GPT-5.4 nano and Gemini Flash-Lite reproduce Haiku
almost exactly ({BV['providers']['openai'].get('agree_with_haiku')} / {BV['providers']['gemini'].get('agree_with_haiku')} agreement)
and — critically — produce <b>zero false rejections</b> on the `pass` set, same as Haiku. The "fail catch" is low and
identical across all three because the site step alone isn't where most fails are caught (banned-category / MLM / foreign /
ownership live in the funnel's <i>web-search</i> steps). <b>So the cheap models hold up on the part we could test cleanly</b>
— encouraging for the ~$90/mo saving — <b>but a full switch still requires rebuilding + re-validating the 3 web-search steps
per provider</b> (each provider's grounding/tool API differs); that's the real remaining work, not the model swap.</div>
"""

prov_head = "".join(f"<th colspan='3'>{n}<span class='price'>{PRICE[k]}</span></th>" for k, n in PROVS)
sub_head = "<th>Style/feature</th>" + ("<th>Agree</th><th>$/1k</th><th>$/mo</th>" * 3)

doc = f"""<!doctype html><html><head><meta charset="utf-8">
<title>LLM Cost Comparison — Dwyer</title><style>
body{{font-family:-apple-system,Segoe UI,Helvetica,Arial,sans-serif;color:#1a1d23;max-width:1040px;margin:0 auto;padding:28px;line-height:1.55;font-size:14px;background:#fafafa}}
h1{{font-size:25px;margin:0 0 4px}} h2{{font-size:18px;margin:28px 0 10px}}
.lede{{color:#5e6470;margin:0 0 20px}}
.box{{background:#fff;border:1px solid #e1e4e8;border-left:4px solid #2563eb;border-radius:8px;padding:16px 18px;margin:18px 0}}
.box.win{{border-left-color:#16a34a}}
table{{width:100%;border-collapse:collapse;background:#fff;border:1px solid #e1e4e8;border-radius:8px;overflow:hidden;font-size:13px;margin:10px 0}}
th,td{{padding:9px 11px;border-bottom:1px solid #f0f1f3;text-align:left}}
th{{background:#f9fafb;color:#5e6470;font-size:11px;text-transform:uppercase;letter-spacing:.4px}}
td.num{{text-align:right;font-variant-numeric:tabular-nums}} td.strong{{font-weight:700}}
td.feat{{font-weight:600}} .vol{{display:block;color:#9aa0aa;font-weight:400;font-size:11px}}
.price{{display:block;color:#9aa0aa;font-weight:400;font-size:10px;text-transform:none}}
tr.verdict td{{background:#fbfbfd;color:#374151;font-size:12.5px;border-bottom:1px solid #e1e4e8}}
tr.v-switch td{{border-left:3px solid #16a34a}} tr.v-keep td{{border-left:3px solid #b45309}} tr.v-review td{{border-left:3px solid #6b7280}}
.pf{{color:#dc2626;font-size:11px}} ul{{margin:6px 0;padding-left:20px}} li{{margin:4px 0}}
.tot td{{font-weight:700;background:#f3f4f6}}
</style></head><body>
<h1>Which LLM provider should each feature use?</h1>
<p class="lede">A like-for-like bake-off of the cheap/fast tier of three providers — Anthropic Haiku 4.5,
OpenAI GPT-5.4 nano, Google Gemini 3.1 Flash-Lite — across every feature that calls an LLM. Each feature's
real prompt was replayed on real sampled inputs (n=50); we measured token cost and, where a stored answer
exists, how often each model <b>agreed with what Haiku produces today</b>. Costs are projected to monthly
volume. Generated {DATE}.</p>

<div class="box win"><b>Bottom line.</b> At your volumes (20,000 leads/mo, ~1,423 replies/mo, ~489 follow-ups/mo):
<ul>
<li><b>Reply-side features are almost free</b> (&lt;$1/mo total) on any provider — not worth optimizing for cost.</li>
<li><b>Company-name resolution</b> is a clean win: GPT-5.4 nano is <b>more</b> accurate than Haiku here and ~7× cheaper.</li>
<li><b>The Prospeo lead filter</b> should <b>stay on Haiku</b> — the cheap rivals lose 10–20 points of agreement and the saving is only ~$17/mo.</li>
<li><b>Brand-verify is where the real money is</b> (~$122/mo Haiku vs ~$23–30 rivals at 20k leads). The Phase-2 quality test (below) shows the cheap models reproduce its <b>core site-verdict judgment with zero false rejections</b> — encouraging — but the full funnel's web-search steps still need a per-provider rebuild + re-validation before any switch.</li>
</ul>
Total projected monthly: <b>Haiku ${tot['anthropic']}</b> · GPT-5.4 nano ${tot['openai']} · Gemini Flash-Lite ${tot['gemini']}.
A sensible blend (cheapest model per feature where quality holds, Haiku elsewhere) ≈ <b>${blend}/mo</b> — note most of the gap is the unverified brand-verify line.</div>

<h2>Results by feature</h2>
<table><thead><tr><th></th>{prov_head}</tr><tr>{sub_head}</tr></thead><tbody>
{''.join(rows_html)}
<tr class="tot"><td>Total projected $/mo</td>
<td class='num' colspan='3'>${tot['anthropic']}</td><td class='num' colspan='3'>${tot['openai']}</td><td class='num' colspan='3'>${tot['gemini']}</td></tr>
</tbody></table>
<p class="lede" style="font-size:12px"><b>Agree</b> = share of items matching the current Haiku output (for Haiku's own column this is self-consistency on a re-run — the realistic ceiling, since these models aren't deterministic). <b>$/1k</b> = raw cost per 1,000 single-item calls. <b>$/mo</b> = projected at your monthly volume, with the system prompt amortized over the production batch size.</p>

{bv_section}
<h2>How to read it / caveats</h2>
<ul>
<li><b>Haiku's "agree" is a self-comparison</b> — it re-runs Haiku against its own stored labels, so 0.84–0.90 is the noise floor. Read each rival against Haiku's number, not against 100%.</li>
<li><b>Sample = 50 items/feature</b> → roughly ±7–10 points on each agreement figure. Directional, not precise; re-run larger if a close call matters.</li>
<li><b>Brand-verify is cost-only.</b> It's a 0–4 call funnel with server-side web search (~$10 / 1k searches, billed on the Anthropic key today). The web-fetched page content dominates token cost, and a real switch means re-implementing each provider's web-search/tool loop — not reflected in the dollar figures here.</li>
<li><b>Prices</b> are the June 2026 cheap-tier rates (per 1M tokens, input/output) shown in the header; all three offer a −50% batch discount not applied here.</li>
<li>Mid/flagship tiers (Sonnet, GPT-5.4/5.5, Gemini Pro) were intentionally not run — the goal was the cheapest credible tier, since that's what these features use today.</li>
</ul>
</body></html>"""

Path("docs").mkdir(exist_ok=True)
Path("docs/LLM_COST_COMPARISON.html").write_text(doc, encoding="utf-8")

with open("docs/llm_cost_comparison.csv", "w", newline="", encoding="utf-8") as fh:
    w = csv.writer(fh)
    w.writerow(["feature", "monthly_volume", "provider", "model", "n", "errors", "agreement",
                "scored_n", "parse_fail", "avg_in_tok", "avg_out_tok", "cost_per_1k_raw", "proj_monthly_usd"])
    for f in R:
        for k, _ in PROVS:
            d = f["providers"].get(k, {})
            w.writerow([f["key"], f["monthly_volume"], k, d.get("model"), d.get("n"), d.get("err"),
                        d.get("agreement"), d.get("scored_n"), d.get("parse_fail"),
                        d.get("avg_in"), d.get("avg_out"), d.get("cost_per_1k_raw"), d.get("proj_monthly")])

print("wrote docs/LLM_COST_COMPARISON.html and docs/llm_cost_comparison.csv")
print(f"totals: Haiku ${tot['anthropic']}  nano ${tot['openai']}  gemini ${tot['gemini']}  blend ${blend}")
