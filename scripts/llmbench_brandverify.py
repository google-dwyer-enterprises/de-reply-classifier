"""Phase-2 brand-verify QUALITY test, cross-provider (site-verdict step only).

The full bv2 funnel's web-search steps are Anthropic-specific and not portable,
but the core SITE-VERDICT step uses no tools: a homepage-signals payload in,
brand/reseller/unknown out. This isolates that step, replays it through
Haiku 4.5 / GPT-5.4 nano / Gemini 3.1 Flash-Lite on the human-labeled audit set
(`qa_audit_labels`), and scores against ground truth:

  - FALSE REJECTION (hard gate = 0): a 'pass' company the model calls 'reseller'.
  - FAIL CATCH: a 'fail' company the model calls 'reseller' OR 'unknown'.
  - plus agreement with Haiku's own site label, and $/call.

Homepage is fetched ONCE per company (provider-independent) and fed identically
to all three. Run: python scripts/llmbench_brandverify.py --n 45
"""
from __future__ import annotations

import argparse
import json
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, ".")
from dotenv import load_dotenv
load_dotenv(".env")

import db
import brand_verify as bv
from scripts.llmbench import call_llm, CHEAP

SITE_SYS = bv.SITE_PROMPT.read_text(encoding="utf-8")


def sample(n, buckets):
    conn = db.connect()
    cur = conn.cursor()
    cur.execute(r"""
        with acc as (
          select distinct on (lower(regexp_replace(company_domain,'^www\.','')))
                 lower(regexp_replace(company_domain,'^www\.','')) as d,
                 company_name, bettercontact_raw->>'company_description' as descr
          from prospeo_new_leads where provider='bettercontact')
        select l.domain, l.verdict, coalesce(acc.company_name, l.domain), acc.descr
        from qa_audit_labels l left join acc on acc.d = l.domain
        where l.verdict = any(%s)
        order by l.verdict, l.domain
    """, (buckets,))
    rows = cur.fetchall()
    cur.close(); conn.close()
    # round-robin across buckets so a capped sample stays balanced
    by = {}
    for dom, v, name, descr in rows:
        by.setdefault(v, []).append((dom, v, name, descr))
    out, i = [], 0
    while len(out) < n and any(by.values()):
        for b in buckets:
            if by.get(b):
                out.append(by[b].pop(0))
                if len(out) >= n: break
        i += 1
        if i > n + 5: break
    return out


def build_payload(dom, name, descr):
    entry = {"company": name, "domain": dom, "description": descr}
    bv._fetch_homepage(entry)
    bv._extract_signals(entry)
    if "homepage" not in entry or "features" not in entry:
        return None, entry.get("fetch_status", "no_signals")
    payload = {"company_name": entry["company"], "domain": entry["domain"],
               "third_party_description": (entry.get("description") or "")[:1200],
               "homepage": entry["homepage"], "features": entry["features"]}
    return json.dumps(payload, ensure_ascii=False), "ok"


def main():
    rows = sample(ARGS.n, ["fail", "review", "pass"])
    print(f"sampling {len(rows)} labeled domains; fetching homepages once...", flush=True)

    # fetch homepages in parallel (once each)
    payloads = {}
    fetch_fail = 0
    def fetch(r):
        dom, v, name, descr = r
        p, status = build_payload(dom, name, descr)
        return dom, v, p, status
    with ThreadPoolExecutor(max_workers=10) as ex:
        for dom, v, p, status in ex.map(fetch, rows):
            if p: payloads[dom] = (v, p)
            else: fetch_fail += 1
    print(f"homepages fetched ok: {len(payloads)} | fetch failed/empty: {fetch_fail}", flush=True)
    if not payloads:
        print("NO homepages fetched (network blocked here?). Run on a networked host.", flush=True)
        return

    # run site-verdict through each provider
    def run(args):
        prov, model, dom, v, p = args
        r = call_llm(prov, model, SITE_SYS, p, max_out=450)
        label = None
        if not r["error"]:
            out = bv._parse_verdict_json(r["text"])
            label = (out or {}).get("label")
        return dom, v, prov, label, r

    tasks = [(prov, model, dom, v, p) for prov, model in CHEAP.items()
             for dom, (v, p) in payloads.items()]
    res = {p: {} for p in CHEAP}        # prov -> {dom: label}
    costs = {p: [] for p in CHEAP}
    errs = {p: 0 for p in CHEAP}
    with ThreadPoolExecutor(max_workers=8) as ex:
        for dom, v, prov, label, r in ex.map(run, tasks):
            if r["error"]: errs[prov] += 1; continue
            res[prov][dom] = label
            costs[prov].append(r["cost"])

    REJECTISH = {"reseller"}   # the site step's reject-class label
    out_rows = []
    haiku = res["anthropic"]
    summary = {}
    for prov in CHEAP:
        labels = res[prov]
        passes = [d for d, (v, _) in payloads.items() if v == "pass"]
        fails = [d for d, (v, _) in payloads.items() if v == "fail"]
        false_rej = [d for d in passes if labels.get(d) in REJECTISH]
        fail_caught = [d for d in fails if labels.get(d) in (REJECTISH | {"unknown"})]
        # agreement with Haiku's site label (over domains both labeled)
        both = [d for d in labels if d in haiku and labels[d] and haiku[d]]
        agree = sum(labels[d] == haiku[d] for d in both) / len(both) if both else None
        n = len(costs[prov])
        summary[prov] = {
            "model": CHEAP[prov], "n": n, "errors": errs[prov],
            "false_rejections": len(false_rej), "false_rej_domains": false_rej,
            "passes": len(passes),
            "fail_caught": len(fail_caught), "fails": len(fails),
            "fail_catch_rate": round(len(fail_caught) / len(fails), 3) if fails else None,
            "agree_with_haiku": round(agree, 3) if agree is not None else None,
            "cost_per_call": round(sum(costs[prov]) / n, 6) if n else None,
        }
    Path("debug").mkdir(exist_ok=True)
    Path("debug/_bv_quality.json").write_text(json.dumps(
        {"n_domains": len(payloads), "fetch_fail": fetch_fail, "providers": summary}, indent=2))

    print("\n=== brand-verify site-step QUALITY (vs ground truth) ===")
    for prov, s in summary.items():
        print(f"  {prov:10} {s['model']:24} agree_haiku={s['agree_with_haiku']} "
              f"false_rej={s['false_rejections']}/{s['passes']} "
              f"fail_catch={s['fail_caught']}/{s['fails']} ${s['cost_per_call']}/call (err {s['errors']})")
    print("\nwrote debug/_bv_quality.json")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=45)
    ARGS = ap.parse_args()
    main()
