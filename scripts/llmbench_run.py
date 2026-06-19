"""Run the cross-provider bake-off over every LLM feature.

For each feature: sample real inputs, replay the feature's EXACT prompt through
Haiku 4.5 / gpt-5.4-nano / gemini-3.1-flash-lite, measure token usage + cost, and
(where a stored reference exists) score agreement vs the current Haiku output.
Projects monthly production cost at the configured volumes. Writes results JSON.

Usage: python scripts/llmbench_run.py --n 40   (samples per feature)
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, ".")
from dotenv import load_dotenv
load_dotenv(".env")

import db
from scripts.llmbench import call_llm, cost, CHEAP, LADDER, PRICING  # noqa

import classify
import followup_llm_features as ff
import resolve_company_names as rcn

PROMPTS = Path("prompts")

# monthly production volumes (user-provided / measured)
VOL = {"classifier": 1423, "followup": 489, "prospeo": 20000,
       "company": 20000, "smartscout": 20000, "name": 20000, "brand_verify": 20000}


def _rows(sql, n):
    conn = db.connect()
    cur = conn.cursor()
    cur.execute(sql + f" limit {int(n)}")
    cols = [d[0] for d in cur.description]
    out = [dict(zip(cols, r)) for r in cur.fetchall()]
    cur.close(); conn.close()
    return out


def _norm(s):
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


# ---- per-feature: build(row)->(system,user), parse(text)->pred, ref(row), score(pred,ref) ----

def feat_classifier():
    sysp = classify.build_system_prompt()
    rows = _rows("""
      with latest as (select distinct on (c.reply_id) c.reply_id, c.label
        from classifications c where c.model<>'rule-based'
        order by c.reply_id, c.classified_at desc)
      select r.id, r.lead_email, r.subject, r.body, l.label as ref
      from latest l join replies r on r.id=l.reply_id
      where coalesce(btrim(r.body),'')<>''""", ARGS.n)
    def build(row): return sysp, classify.format_batch_user_message([row])
    def parse(t):
        p = classify.parse_response(t); return (p[0].get("label") if p else None)
    def score(pred, row): return None if pred is None else float(pred == row["ref"])
    return dict(key="classifier", label="Reply classifier", rows=rows, build=build,
                parse=parse, score=score, batch=25, max_out=500)


def feat_followup():
    sysp = ff.build_system_prompt()
    rows = _rows("""select sent_message_id, followup_new_text,
        hook_type, tone, cta_style, personalization
      from followup_message_features
      where boundary_detected and coalesce(btrim(followup_new_text),'')<>'' and hook_type is not null
      order by sent_message_id""", ARGS.n)
    dims = ["hook_type", "tone", "cta_style", "personalization"]
    def build(row): return sysp, ff.format_batch_user_message(
        [{"sent_message_id": row["sent_message_id"], "followup_new_text": row["followup_new_text"]}])
    def parse(t):
        p = classify.parse_response(t)
        return ff.coerce_features(p[0]) if p else None
    def score(pred, row):
        if not pred: return None
        return sum(float(pred.get(d) == row[d]) for d in dims) / len(dims)
    return dict(key="followup", label="Follow-up tagging (4 dims)", rows=rows, build=build,
                parse=parse, score=score, batch=25, max_out=400)


def feat_prospeo():
    sysp = (PROMPTS / "agency_filter.txt").read_text(encoding="utf-8")
    rows = _rows("""select email, company_name, company_website, company_domain, title,
        split_part(email,'@',2) as email_domain,
        coalesce(prospeo_raw->'enrich'->>'company_description',
                 prospeo_raw->'search'->>'company_description') as company_description,
        agency_filter_result as ref
      from prospeo_new_leads where agency_filter_method='llm'""", ARGS.n)
    def build(row):
        payload = {k: row.get(k) for k in ("company_name", "company_website", "company_description", "title", "email_domain")}
        return sysp, json.dumps(payload)
    def parse(t):
        t = re.sub(r"^```\w*|```$", "", t.strip(), flags=re.M).strip()
        try: return (json.loads(t) or {}).get("result")
        except Exception: return None
    def score(pred, row): return None if pred is None else float(pred == row["ref"])
    return dict(key="prospeo", label="Prospeo lead filter", rows=rows, build=build,
                parse=parse, score=score, batch=1, max_out=200)


def feat_company():
    rows = _rows("""select lead_email, apollo_company_name, company_name,
        resolved_company_name as ref,
        (string_to_array(lower(split_part(lead_email,'@',2)),'.'))[
          array_length(string_to_array(lower(split_part(lead_email,'@',2)),'.'),1)-1] as domain_root
      from lead_contacts
      where resolved_company_name is not null
        and nullif(trim(apollo_company_name),'') is not null and nullif(trim(company_name),'') is not null
        and lower(trim(apollo_company_name))<>lower(trim(company_name))""", ARGS.n)
    def build(row):
        return rcn.SYSTEM_PROMPT, rcn.format_user_message([{
            "domain_root": row["domain_root"], "apollo_company_name": row["apollo_company_name"],
            "company_name": row["company_name"]}])
    def parse(t):
        t = re.sub(r"^```\w*\s*|\s*```$", "", (t or "").strip(), flags=re.S).strip()
        m = re.search(r"\[.*\]", t, re.S)
        if m: t = m.group(0)
        arr = json.loads(t)
        return arr[0].get("picked") if arr else None
    def score(pred, row):
        if pred is None: return None
        return float(_norm(pred) == _norm(row["ref"]))
    return dict(key="company", label="Company-name resolution", rows=rows, build=build,
                parse=parse, score=score, batch=25, max_out=400)


def feat_brand_verify():
    sysp = (PROMPTS / "brand_verify.txt").read_text(encoding="utf-8")
    rows = _rows("""select domain, verdict as ref, evidence
      from domain_brand_verdicts
      where method in ('site_llm','agentic','vendor_llm','vendor_llm+search','ownership_search')""", ARGS.n)
    filler = ("We design and manufacture our own products. " * 120)[:4000]  # realistic homepage body size
    def build(row):
        payload = {"company_name": row["domain"], "domain": row["domain"],
                   "third_party_description": (row.get("evidence") or "")[:300],
                   "homepage": {"title": row["domain"], "meta_description": "", "nav": "Shop About Contact",
                                "footer": "", "body_excerpt": filler},
                   "features": {"shopify_vendor_count": 1, "nav_has_shop_by_brand": False,
                                "reseller_phrase_hits": 0, "brand_phrase_hits": 3,
                                "mlm_signal_hits": 0, "geo_signals": []}}
        return sysp, json.dumps(payload)
    def parse(t): return "ok" if t else None  # cost-only; quality not cross-provider comparable
    def score(pred, row): return None
    return dict(key="brand_verify", label="Brand verify (site step; cost-only)", rows=rows, build=build,
                parse=parse, score=score, batch=1, max_out=450, calls_per_item=2.0, scored=False,
                note="multi-step + web_search (provider-specific); cost = site-step × ~2 LLM calls/company; web_search billed separately")


def run_feature(f):
    scored = f.get("scored", True)
    agg = {lbl: {"in": [], "out": [], "cost": [], "score": [], "err": 0, "parsefail": 0}
           for lbl, _, _ in LADDER}

    def work(args):
        lbl, prov, model, row = args
        system, user = f["build"](row)
        r = call_llm(prov, model, system, user, max_out=f["max_out"])
        if r["error"]:
            return lbl, None, r, False
        try:
            pred = f["parse"](r["text"])
            sc = f["score"](pred, row)
            parsed_ok = pred is not None
        except Exception:
            sc, parsed_ok = None, False
        return lbl, sc, r, parsed_ok

    # Gemini 3.1 Pro forces thinking mode (~100s/call) — cap its rows so one slow model
    # doesn't make the whole run take an hour. Its smaller n is shown transparently.
    SLOW_CAP = {"Gemini 3.1 Pro": 12}
    tasks = [(lbl, prov, model, row) for lbl, prov, model in LADDER
             for row in f["rows"][:SLOW_CAP.get(lbl, len(f["rows"]))]]
    with ThreadPoolExecutor(max_workers=8) as ex:
        for lbl, sc, r, parsed_ok in ex.map(work, tasks):
            a = agg[lbl]
            if r["error"]:
                a["err"] += 1; continue
            a["in"].append(r["in_tok"]); a["out"].append(r["out_tok"]); a["cost"].append(r["cost"])
            if scored:
                if sc is not None: a["score"].append(sc)
                else: a["parsefail"] += 1

    sample_sys = f["build"](f["rows"][0])[0] if f["rows"] else ""
    S = len(sample_sys) // 4
    out = {}
    for lbl, prov, model in LADDER:
        a = agg[lbl]
        n = len(a["in"])
        if n == 0:
            out[lbl] = {"provider": prov, "model": model, "n": 0, "err": a["err"]}; continue
        avg_in = sum(a["in"]) / n; avg_out = sum(a["out"]) / n
        item_in = max(avg_in - S, 1)
        proj_in = S / f.get("batch", 1) + item_in
        pin, pout = PRICING[model]
        proj_item_cost = (proj_in * pin + avg_out * pout) / 1e6 * f.get("calls_per_item", 1.0)
        out[lbl] = {
            "provider": prov, "model": model, "n": n, "err": a["err"],
            "avg_in": round(avg_in, 1), "avg_out": round(avg_out, 1),
            "agreement": round(sum(a["score"]) / len(a["score"]), 3) if a["score"] else None,
            "scored_n": len(a["score"]), "parse_fail": a["parsefail"],
            "cost_per_1k_raw": round(sum(a["cost"]) / n * 1000, 4),
            "proj_monthly": round(proj_item_cost * VOL[f["key"]], 2),
        }
    return {"key": f["key"], "label": f["label"], "monthly_volume": VOL[f["key"]],
            "batch": f.get("batch", 1), "calls_per_item": f.get("calls_per_item", 1.0),
            "note": f.get("note"), "models": out}


def main():
    feats = [feat_classifier(), feat_followup(), feat_prospeo(), feat_company(), feat_brand_verify()]
    results = []
    for f in feats:
        print(f"\n=== {f['label']} (n={len(f['rows'])}) ===", flush=True)
        res = run_feature(f)
        now = res["models"].get("Haiku 4.5 (current)", {}).get("proj_monthly")
        for lbl, d in res["models"].items():
            if d.get("n"):
                flag = "" if now is None or d["proj_monthly"] is None else (
                    " [<= now]" if d["proj_monthly"] <= now else " [OVER now]")
                print(f"  {lbl:24} agree={d['agreement']} ${d['cost_per_1k_raw']}/1k "
                      f"-> ${d['proj_monthly']}/mo{flag}  (err {d['err']})", flush=True)
            else:
                print(f"  {lbl:24} NO DATA (err {d['err']})", flush=True)
        results.append(res)
    Path("debug").mkdir(exist_ok=True)
    Path("debug/_llmbench_ladder.json").write_text(json.dumps(results, indent=2))
    print("\nwrote debug/_llmbench_ladder.json", flush=True)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=40)
    ARGS = ap.parse_args()
    main()
