"""Track 2 hybrid pilot (~$15): Prospeo discovery -> free QA -> BC
enrichment -> MV cross-check. Settles the three unknowns:

  1. Prospeo inventory depth per industry (census via page-1 total_count)
  2. BC enrichment-API find-rate from name+company inputs (misses cost 0)
  3. Email quality of BC-enriched leads (MV pass rate; bar = 95%)

Read-only on the DB. Spend: ~11 Prospeo cr (census) + <=75 BC cr (only on
found verified emails) + <=75 MV cr.
"""
import json
import os
import sys
import time
sys.path.insert(0, '.')
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
from dotenv import load_dotenv
load_dotenv(".env")
import requests as rq
from db import connect
import millionverifier as mv
from bettercontact_sync import BC_INDUSTRIES

PKEY = os.environ["PROSPEO_API_KEY"].strip()
BKEY = os.environ["BETTERCONTACT_API_KEY"].strip()
PH = {"X-KEY": PKEY, "Content-Type": "application/json"}
BH = {"X-API-Key": BKEY, "Content-Type": "application/json"}
ALLOWED_TLDS = (".com", ".co", ".net", ".us", ".ca", ".shop", ".store")

# --- 1. census + collect -----------------------------------------------------
print("=== 1. Prospeo inventory census (Founder/Owner, US/CA) ===")
people = []
census = {}
for ind in BC_INDUSTRIES:
    body = {"page": 1, "filters": {
        "person_seniority": {"include": ["Founder/Owner"]},
        "company_industry": {"include": [ind]},
        "company_location_search": {"include": ["United States", "Canada"]},
    }}
    try:
        r = rq.post("https://api.prospeo.io/search-person", headers=PH,
                    json=body, timeout=60)
        d = r.json()
        if d.get("error"):
            census[ind] = f"ERROR: {d.get('filter_error') or d.get('error_code')}"
            continue
        total = (d.get("pagination") or {}).get("total_count", 0)
        census[ind] = total
        for p in d.get("results", []):
            p["_industry"] = ind
            people.append(p)
    except Exception as e:
        census[ind] = f"EXC: {type(e).__name__}"
total_inventory = sum(v for v in census.values() if isinstance(v, int))
for ind, v in census.items():
    print(f"  {ind:48s} {v}")
print(f"  TOTAL Founder/Owner US-CA inventory: {total_inventory:,} "
      f"(collected {len(people)} people for the pilot)")

# --- 2. free QA --------------------------------------------------------------
conn = connect()
cur = conn.cursor()
cur.execute(r"""select distinct lower(regexp_replace(company_domain,'^www\.',''))
               from prospeo_new_leads where company_domain is not null""")
known = {r[0] for r in cur.fetchall()}
conn.close()

survivors, dropped = [], {"bad_tld": 0, "dup_company": 0, "no_site": 0,
                          "no_name": 0}
seen_doms = set()
for p in people:
    per, com = p.get("person", {}), p.get("company", {})
    site = (com.get("website") or "").lower()
    dom = site.replace("https://", "").replace("http://", "").replace("www.", "").split("/")[0]
    if not dom:
        dropped["no_site"] += 1
        continue
    if not any(dom.endswith(t) for t in ALLOWED_TLDS):
        dropped["bad_tld"] += 1
        continue
    if dom in known or dom in seen_doms:
        dropped["dup_company"] += 1
        continue
    if not (per.get("first_name") and per.get("last_name")):
        dropped["no_name"] += 1
        continue
    seen_doms.add(dom)
    survivors.append({"first_name": per["first_name"],
                      "last_name": per["last_name"],
                      "company": com.get("name") or dom,
                      "company_domain": dom,
                      "custom_fields": {"uid": f"pilot{len(survivors)}"}})
print(f"\n=== 2. free QA: {len(survivors)} survivors of {len(people)} "
      f"(dropped: {dropped}) — zero email credits spent ===")

# --- 3. BC enrichment --------------------------------------------------------
batch = survivors[:75]
print(f"\n=== 3. BC enrichment API on {len(batch)} survivors ===")
r = rq.post("https://app.bettercontact.rocks/api/v2/async", headers=BH,
            json={"data": batch, "enrich_email_address": True,
                  "enrich_phone_number": False}, timeout=60)
print("submit:", r.status_code, r.text[:200])
rid = r.json().get("id") or r.json().get("request_id")
result = None
for _ in range(60):
    time.sleep(10)
    g = rq.get(f"https://app.bettercontact.rocks/api/v2/async/{rid}",
               headers={"X-API-Key": BKEY}, timeout=30)
    d = g.json()
    status = d.get("status")
    if status in ("terminated", "completed", "done", "finished"):
        result = d
        break
    print(f"  polling... status={status}")
if not result:
    sys.exit("enrichment did not terminate in time")

rows = result.get("data") or []
print(f"credits_consumed: {result.get('credits_consumed')}  "
      f"credits_left: {result.get('credits_left')}  rows: {len(rows)}")
if rows:
    print("first row keys:", sorted(rows[0].keys()))
found = []
for row in rows:
    email = (row.get("contact_email_address") or row.get("email") or "")
    status = (row.get("contact_email_address_status")
              or row.get("email_status") or "")
    if email:
        found.append((email, status))
print(f"\nfind-rate: {len(found)}/{len(batch)} = "
      f"{len(found)*100//max(len(batch),1)}%")
from collections import Counter
print("status distribution:", Counter(s for _, s in found))

# --- 4. MV cross-check -------------------------------------------------------
deliverable = [e for e, s in found if s == "deliverable" or not s]
print(f"\n=== 4. MV cross-check of {len(deliverable)} BC emails ===")
if deliverable:
    res = mv.verify_emails(deliverable)
    dist = Counter(v["result"] for v in res.values())
    print("MV distribution:", dict(dist))
    ok = dist.get("ok", 0)
    print(f"MV-ok rate: {ok}/{len(deliverable)} = "
          f"{ok*100//len(deliverable)}% (BC-search-sourced bar: 95%)")

# --- 5. economics ------------------------------------------------------------
print("\n=== 5. pilot economics ===")
print(f"  Prospeo census/discovery: ~{len(census)} cr for {len(people)} people")
print(f"  BC enrichment: {result.get('credits_consumed')} cr for "
      f"{len(found)} emails")
if found:
    bc_cr_per_email = float(result.get("credits_consumed") or 0) / len(found)
    print(f"  BC cr per delivered email: {bc_cr_per_email:.2f}")
